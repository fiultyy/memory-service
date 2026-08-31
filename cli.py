"""mem-service cli — ingest / recall / consolidate (ADR-1, ADR-5, ADR-5b).

Top seam is the cli module (Spec §6): both ``cli.ingest(...)`` (Python) and
``python cli.py ingest "..."`` (argv) drive the same pipeline.

- ingest: extract facts via the adapter (ADR-5b butterfly-wing LLM with regex
  fallback), persist them. ``fact.extractor`` = "llm" when the adapter's LLM
  vote won, "regex" when it fell back (ADR-5 upheld as fallback).
- recall: KG navigation → Fact list + α·match+β·centrality+γ·LIF 加权排序
  (ADR-4v2). scoring.ALPHA_MATCH/BETA_CENTRALITY/GAMMA_LIF fuse token match,
  pagerank centrality and LIF into the recall order. v1 substring match on
  Fact.value/predicate + entity.name LIKE underlies the match term (semantic
  recall deferred — ADR-4, Spec Defer).
- consolidate: dedup skeleton, no decay (Spec §4 story 4; Node D owns depth).

No ``query`` subcommand (debug via ``recall --verbose`` or sqlite3 — Spec §3).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import adapter
import autodream as autodream_mod
import bootstrap
import consolidate as consolidate_mod
import recall as recall_mod
import store
import resolver
from llm_provider import LLMProvider


# ── M17/M18 通道判定 (DR-9 G10 已裁决) ──────────────────────────────

def _channel(stdin_isatty: bool | None = None,
             stdout_isatty: bool | None = None) -> str:
    """调用通道判定: 物理 tty 且无 agent 自标 → 'human'; 否则 'agent'。

    方向性铁律 (P38 权威梯度=通道梯度): env ``MEM_AGENT_CONTEXT`` 只能**降档**
    (agent 自标 agent) — 升 human 档必须物理 tty, env 无法伪造 human。
    参数可注入 isatty (测试); 缺省取当前进程实态。
    """
    sin = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty
    sout = sys.stdout.isatty() if stdout_isatty is None else stdout_isatty
    if sin and sout and not os.environ.get("MEM_AGENT_CONTEXT"):
        return "human"
    return "agent"


# ── M17 四动词 + M16 cite (P38 v16 补注映射; 无 delete/punish) ────────

# 高危动词 (human 路径需交互确认; agent 路径免确认直接执行)。
_HIGH_RISK_VERBS = ("invalidate", "elevate")


def _human_confirm(verb: str, target: str) -> bool:
    """human 路径高危动词交互确认: input y/N, 非 y 拒绝 (M18 梯度)。"""
    try:
        ans = input(f"[mem {verb}] 确认对 {target} 执行 {verb}? (y/N) ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def mem_write(subject: str, predicate: str, value: str, *,
              fact_type: str = "stable", source_cwd: str | None = None,
              channel: str | None = None) -> dict[str, Any]:
    """四动词 write: 新事实入库。provenance=通道档 (agent→agent_assert 0.5 /
    human→human 0.9, veracity 走 M3 映射); 不可声明 provenance (无 flag,
    杜绝伪造面)。信号 agent_crud{verb:write, via:通道}。"""
    import signals
    ch = channel if channel is not None else _channel()
    prov = "human" if ch == "human" else "agent_assert"
    sid = resolver.resolve_entity(subject, "concept", providers=[])
    fid = store.put_fact(sid, predicate, value, extractor="human" if ch == "human" else "agent",
                         fact_type=fact_type, provenance=prov,
                         source_cwd=source_cwd)
    signals.append("agent_crud", {
        "verb": "write", "fact_id": fid, "subject": subject,
        "predicate": predicate, "value": value, "via": ch,
        "source_cwd": source_cwd})
    return {"written": fid, "provenance": prov, "channel": ch}


def mem_confirm(fact_id: str, *, channel: str | None = None) -> dict[str, Any]:
    """四动词 confirm: 证实既有 fact — 记 confirm_arrivals 信号 (via=通道)。
    P22 确认轴: 不动目标 fact 本体 (确认到达是 dreaming 消费的正信号,
    若判需新版本由 dreaming 以 supersede_reason='confirm' 产生)。"""
    import signals
    ch = channel if channel is not None else _channel()
    fact = store.get_fact(fact_id)
    if fact is None:
        return {"confirmed": None, "error": f"fact {fact_id} not found"}
    signals.append("confirm_arrivals", {
        "fact_id": fact_id, "via": ch,
        "source_cwd": fact.get("source_cwd")})
    return {"confirmed": fact_id, "channel": ch}


def mem_invalidate(fact_id: str, note: str = "", *,
                   channel: str | None = None) -> dict[str, Any]:
    """四动词 invalidate: 失效建议 — 旧 fact status→superseded,
    supersede_reason='contradiction' (时效标注, 复用 M1 通道); human 路径
    高危需交互确认。信号 agent_crud{verb:invalidate, via:通道}。"""
    import signals
    ch = channel if channel is not None else _channel()
    fact = store.get_fact(fact_id)
    if fact is None:
        return {"invalidated": None, "error": f"fact {fact_id} not found"}
    if ch == "human" and not _human_confirm("invalidate", fact_id):
        return {"invalidated": None, "declined": True, "channel": ch}
    store.update_fact_status(fact_id, "superseded",
                             valid_to=store._now(), reason="contradiction")
    signals.append("agent_crud", {
        "verb": "invalidate", "fact_id": fact_id, "via": ch,
        "source_cwd": fact.get("source_cwd"), "note": note})
    return {"invalidated": fact_id, "reason": "contradiction", "channel": ch}


def mem_elevate(fact_id: str, *, channel: str | None = None) -> dict[str, Any]:
    """四动词 elevate: 晋升提名 — **不动 fact** (无 supersede, 晋升裁决权在
    dreaming 的 LIF 阈值), 仅记偏好信号; human 路径高危需交互确认。"""
    import signals
    ch = channel if channel is not None else _channel()
    fact = store.get_fact(fact_id)
    if fact is None:
        return {"elevated": None, "error": f"fact {fact_id} not found"}
    if ch == "human" and not _human_confirm("elevate", fact_id):
        return {"elevated": None, "declined": True, "channel": ch}
    signals.append("agent_crud", {
        "verb": "elevate", "fact_id": fact_id, "via": ch,
        "source_cwd": fact.get("source_cwd")})
    return {"elevated": fact_id, "channel": ch}  # 信号已记, fact 未动


def mem_cite(fact_id: str, output_ref: str = "", *,
             channel: str | None = None) -> dict[str, Any]:
    """M16 cite (DR-9 G9): 引用记账 — append citations 信号流
    (fact_id/agent_output_ref/via=通道)。单向正奖励, 不碰 KG 写面
    (cite 非权威动词, 不扩 P38 白名单)。"""
    import signals
    ch = channel if channel is not None else _channel()
    fact = store.get_fact(fact_id)
    if fact is None:
        return {"cited": None, "error": f"fact {fact_id} not found"}
    signals.append("citations", {
        "fact_id": fact_id, "agent_output_ref": output_ref, "via": ch,
        "source_cwd": fact.get("source_cwd")})
    return {"cited": fact_id, "channel": ch}


# ── .env 加载 (stdlib, 无依赖; 早于 provider 实例化) ─────────────────
def _load_env() -> None:
    """从同目录 .env 加载环境变量到 os.environ (setdefault: 不覆盖已存在的)。
    无 .env 文件则 no-op。provider (llm/embedding) 的 default_providers() 运行时
    读 env, 故 cli 加载时跑一次即可。"""
    p = Path(__file__).parent / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


# ── ingest ──────────────────────────────────────────────────────────

def ingest(text: str, source_ref: str | None = None,
           fact_type: str = "stable",
           providers: list[LLMProvider] | None = None,
           source_cwd: str | None = None) -> dict[str, Any]:
    """Extract facts from ``text`` via the adapter and persist them to the KG.

    The adapter runs butterfly-wing LLM extraction (ADR-5b, N=3 fan-out +
    quorum vote). ``fact.extractor`` reflects the vote outcome: "vote" when
    wings≥2, "llm" for single-wing. No regex fallback (LLM unreachable ⇒
    RuntimeError). ``fact_type`` is ADR-8 (default stable; ``--fact-type``
    overrides). Entities resolved via ``resolver.resolve_entity`` (ADR-D3
    two-step merge). Re-extraction of a known name reuses its id.

    Initial LIF is computed at ingest from five dims (ADR-8v2) and passed to
    ``put_fact`` — not deferred to first consolidate. ``confidence`` carries
    the adapter's vote-aggregated confidence (max across contributing wings).

    Returns a summary ``{"entities": n, "facts": [...]}`` (fact ids).
    """
    if providers is None:
        providers = adapter.default_providers()
    extracted = adapter.extract_facts(text, providers=providers)
    # adapter._vote computes the label (vote when quorum wings≥2, else llm).
    ext_label = extracted.source_meta.get("extractor_label", "llm")
    source_refs = [source_ref] if source_ref else []
    # Initial 5-dim LIF at ingest (ADR-8v2): all edges in one ingest share the
    # same source/recency/coherence at creation — compute once. coherence=1.0
    # (no siblings queried; consolidate recomputes authoritatively). freq=0,
    # spread=0 (fresh fact, no recall hits yet).
    import scoring
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lif_dims = scoring.compute_lif(
        {"extractor": ext_label, "fact_type": fact_type, "created_at": now.isoformat()},
        access_count=0,
        last_accessed_at=now.isoformat(),
        distinct_sessions=0,
        neighbors=[],
        now=now,
    )

    # name → entity_id cache (this ingest's working set).
    name_to_id: dict[str, str] = {}
    # name → type map (entities carry their declared type; edges fall back to
    # the cache or a default — R1 档 1: no more hardcoded "inferred").
    name_to_type: dict[str, str] = {}
    # Phase 1: persist declared entities (R1 档 1 — entities first, edges after).
    # resolver.resolve_entity: cheap exact/alias gate, else create (ADR-D3).
    for ent in extracted.entities:
        if not ent.name:
            continue
        eid = resolver.resolve_entity(
            ent.name, ent.type,
            aliases=getattr(ent, 'aliases', None) or None,
            providers=providers)
        if eid is not None:
            name_to_id[ent.name] = eid
            name_to_type[ent.name] = ent.type

    # Phase 2: edges. subject AND object both resolve to entities (R1 §A2 hard
    # rule: object is a declared entity reference, never a free string) →
    # object_id 必非空 → entity↔entity edges emerge.
    fact_ids: list[str] = []
    for edge in extracted.edges:
        subj_id = name_to_id.get(edge.subject)
        if subj_id is None:
            subj_id = resolver.resolve_entity(
                edge.subject, name_to_type.get(edge.subject, "concept"),
                providers=providers)
            if subj_id is not None:
                name_to_id[edge.subject] = subj_id
        if subj_id is None:
            continue
        # object is guaranteed declared (adapter drops dangling refs); still
        # resolve both sides so it lands as an entity (R1 §A2).
        obj_id = name_to_id.get(edge.object)
        if obj_id is None:
            obj_id = resolver.resolve_entity(
                edge.object, name_to_type.get(edge.object, "concept"),
                providers=providers)
            if obj_id is not None:
                name_to_id[edge.object] = obj_id
        fid = store.put_fact(
            subject_id=subj_id,
            predicate=edge.predicate,
            value=edge.object,  # backward-compat display value
            object_id=obj_id,
            extractor=ext_label,
            fact_type=fact_type,
            source_cwd=source_cwd,
            source_refs=source_refs,
            topic=(edge.topic or "").strip() or None,  # ADR-C: LLM 可读一句话
            confidence=extracted.confidence,
            LIF=lif_dims["LIF"],
            lif_freq=lif_dims["lif_freq"], lif_recency=lif_dims["lif_recency"],
            lif_spread=lif_dims["lif_spread"], lif_coherence=lif_dims["lif_coherence"],
            lif_source=lif_dims["lif_source"],
        )
        fact_ids.append(fid)

    return {"entities": len(name_to_id), "facts": fact_ids}



# ── recall ──────────────────────────────────────────────────────────

def _normalize_as_of(as_of: str | None) -> str | None:
    """归一 --as-of 为 UTC +00:00 秒级 ISO-8601 (ADR-3 ②)。

    接受任意 ISO-8601(含 ``Z`` / ``+HH:MM`` / 无后缀 naive)。naive 输入按 UTC
    解释(文档化)。输出统一 ``+00:00`` 后缀 → recall._temporal_clause 依赖
    SQLite TEXT 字典序 = 时间序, 非 UTC 后缀会字典序错序。microsecond 截断到秒,
    与 store._now() 的 ms-floor 惯例一致(同精度同格式比较)。None 透传(None =
    default recall, 不走点时查询)。
    """
    if as_of is None:
        return None
    dt = datetime.fromisoformat(as_of)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # naive → UTC (文档化)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def recall(query: str, verbose: bool = False,
           session_id: str | None = None, boost: bool = True,
           weights=None, use_vec: bool = False, delta: float | None = None,
           cwd: str | None = None, top_k: int | None = None,
           with_tag: bool = False,
           use_bfs: bool = False, bfs_hops: int = 2,
           as_of: str | None = None,
           use_bfs_scoped: bool = False,
           as_json: bool = False,
           min_score: float | None = None,
           project: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    """Return Facts relevant to ``query``, ordered by α·match+β·centrality+γ·LIF(+δ·vec_sim use_vec) 加权排序 (ADR-4v2/ADR-13).

    Thin wrapper over ``recall.recall``. ``use_vec=True`` 启用向量召回融合
    (ADR-13); ``cwd`` ADR-14 b 方案: 过滤 source_cwd(含 NULL 老数据兼容)。
    ``top_k`` 限制返回数量(默认 None 无截断)。
    ``use_bfs=True`` 启用 BFS 图遍历召回(D5, 召回图近但字面/向量远的 fact)。
    ``as_of`` 点时召回(bi-temporal): 只返回 as_of 时刻有效的 fact。输入端归一
    为 UTC +00:00(ADR-3 ②, 杜绝非 UTC 字典序错序; naive 按 UTC 解释)。
    ``use_bfs_scoped=True`` (ADR-4) 限 BFS 图构建为本 cwd(source_cwd 过滤, 图更精确
    更小); default off 保持全局图(ADR-14 单体 KG 跨 cwd 共享)。
    ``as_json=True`` (M15a) 输出稳定 JSON 契约 shape: ``{"query", "facts":
    [structured...]}`` — 见 :func:`_json_contract_facts` (字段名即 ABI)。
    """
    result = recall_mod.recall(query, verbose=verbose, session_id=session_id,
                               boost=boost, weights=weights, use_vec=use_vec, delta=delta, cwd=cwd, top_k=top_k,
                               with_tag=with_tag, use_bfs=use_bfs, bfs_hops=bfs_hops,
                               as_of=_normalize_as_of(as_of), use_bfs_scoped=use_bfs_scoped,
                               min_score=min_score)
    if project:
        # M18: 召回正文 → recall-<DATE>.md + MEMORY.md 索引行 (用户裁决 2026-08-27)。
        # dir = cc_memory_dir(--cwd 或 $PWD); 空命中不投影(不写空日志)。报告走
        # stderr — stdout JSON 契约 (M15a) 不混投影元数据。
        facts = result["results"] if isinstance(result, dict) and "results" in result \
            else result
        facts = [f.get("fact", f) if isinstance(f, dict) else f for f in (facts or [])]
        if facts:
            import projection
            proj = projection.project_recall(
                projection.cc_memory_dir(cwd or os.getcwd()), query, facts)
            sys.stderr.write(
                f"📋 recall projected → memory/{proj['recall_file']} "
                f"(+{proj['appended']} hits; MEMORY.md index "
                f"{'added' if proj['index_added'] else 'already present'})\n")
    if as_json:
        facts = result["results"] if isinstance(result, dict) and "results" in result \
            else result
        return {"query": query, "facts": _json_contract_facts(facts)}
    return result


# M15a --json 稳定输出契约 (D5'): 字段名即 ABI — 变更须留 changelog。
# 结构化 fact 投影: 供 agent/脚本消费; 列表序 = recall 输出序 (score 降序)。
_JSON_FACT_FIELDS = (
    "id", "subject_id", "predicate", "object_id", "value",
    "fact_type", "LIF", "status", "provenance", "veracity", "topic",
    "extractor", "supersede_reason", "supersedes_id",
    "valid_from", "valid_to", "created_at",
    "access_count", "last_accessed_at",
)


def _json_contract_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """单 fact → 契约投影 (verbose dict 取 .fact; 固定字段序, 缺列投 None)。"""
    src = fact.get("fact", fact) if isinstance(fact, dict) else {}
    out: dict[str, Any] = {}
    for k in _JSON_FACT_FIELDS:
        out[k] = src.get(k)
    # score: verbose/with_tag shape 携带 (list shape 的 _snaptag 不透出)。
    if isinstance(fact, dict) and "score" in fact:
        out["score"] = fact["score"]
    return out


def _json_contract_facts(facts: list[Any]) -> list[dict[str, Any]]:
    return [_json_contract_fact(f) for f in (facts or [])]


# ── consolidate ────────────────────────────────────────────────────

def consolidate() -> dict[str, int]:
    """Decay + dedup pass (Spec §4.4; ADR-8 + ADR-6).

    Thin wrapper over ``consolidate.consolidate``. Phase 1 decays LIF per
    fact_type half-life (active→deprecated when LIF<0.1); phase 2 marks
    exact-duplicate Facts as superseded. Returns ``{decayed, deprecated,
    superseded, active}`` per the SKILL.md output contract.
    """
    return consolidate_mod.consolidate()


# ── autodream ──────────────────────────────────────────────────────

def autodream(session_id: str, transcript_path: str,
              cwd: str | None = None, harness: str = "cc") -> dict[str, int]:
    """PreCompact autoDream: session transcript raw→KG incremental (ADR-10/11).

    Thin wrapper over ``autodream.autodream`` (LLM 蝴蝶翼 直连, 无 regex 降级 —
    LLM 不可用即 block)。``cwd`` ADR-14 b 方案: 记 source_cwd, recall --cwd 过滤。
    ``harness``: corpus_prep 语料标记块清洗表键 (缺省 cc)。
    """
    return autodream_mod.autodream(session_id, transcript_path, source_cwd=cwd,
                                   harness=harness)


# ── ingest-recent (M18: 手动补近期会话结论入库) ──────────────────────

def ingest_recent(cwd: str | None = None, limit: int = 10,
                  dry_run: bool = False,
                  registry_path: str | Path | None = None,
                  harness: str = "cc") -> dict[str, Any]:
    """当前 cwd 最近 N 个 transcript 的用户声音场景 → 蒸馏 → LLM 入 KG (手动)。

    ``harness`` (M19/M22): ``cc`` / ``dsh`` / ``omp`` / ``codex`` — 落盘定位
    与 end step 判定见 ``transcripts.py`` 适配层 (实测校准); 默认 cc。

    每个: 场景蒸馏 (end step + 配对用户原话块, M21 用户声音通道) → 合成
    transcript (``[用户]`` / ``[助手结论]`` 角色标记) →
    ``autodream(session=文件名 uuid, source_cwd=cwd, harness=harness)``。

    - 空 end step → skip 不调 LLM (仍记注册表, 免重复蒸馏空跑)。
    - 注册表 ``data/transcript-registry.json``: path → sha256[:16]; 未变 → skip
      (防手滑重跑烧 LLM); 内容变更 → 重跑。成功(含空跳过)才落 sha; 失败不落
      (下次重试)。``registry_path`` 显式注入(测试隔离), 缺省模块相对 data/。
    - ``dry_run``: 只统计蒸馏结果(纯 CPU 零 LLM), 不调 LLM 不写注册表。
    - per-file 容错: 单文件失败记 error 继续 (bootstrap 惯例), 汇总返回。
    """
    import hashlib
    import tempfile

    import transcripts as transcripts_mod

    cwd = cwd or os.getcwd()
    files = transcripts_mod.locate(cwd, harness, limit)

    reg_path = Path(registry_path) if registry_path else \
        Path(__file__).parent / "data" / "transcript-registry.json"
    registry: dict[str, str] = {}
    if reg_path.exists():
        try:
            loaded = json.loads(reg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                registry = loaded
        except (OSError, ValueError):
            registry = {}  # 损坏注册表不阻塞: 当作未注册重跑

    details: list[dict[str, Any]] = []
    agg: dict[str, int] = {}
    for p in files:
        try:
            sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        except OSError as e:
            details.append({"file": p.name, "status": "error",
                            "error": f"read: {e}"})
            continue
        sid = transcripts_mod.session_id(p, harness)
        label = f"{p.parent.name}/{p.name}" if p.name == "session.jsonl.zstd" \
            else p.name
        entry: dict[str, Any] = {"file": label, "session": sid, "sha": sha}
        if registry.get(str(p)) == sha:
            entry["status"] = "skipped-unchanged"
            details.append(entry)
            continue
        try:
            scenes_ = transcripts_mod.scenes(p, harness)
        except (OSError, RuntimeError, ValueError) as e:
            entry.update(status="error", error=f"extract: {e}")
            details.append(entry)
            continue
        entry["scenes"] = len(scenes_)
        entry["user_blocks"] = sum(len(s["user_blocks"]) for s in scenes_)
        if not scenes_:
            entry["status"] = "skipped-empty"
            if not dry_run:
                registry[str(p)] = sha
            details.append(entry)
            continue
        if dry_run:
            entry["status"] = "would-ingest"
            details.append(entry)
            continue
        # 合成 transcript (M21 用户声音通道): 场景按 [用户]/[助手结论] 标记
        # 写入 (prompt v5 阅读优先级依赖角色标记); 均 type=user → autodream
        # 块文法合并为同 provenance 段, 提取器在段内同看两侧。
        fd, tmp_name = tempfile.mkstemp(suffix=".jsonl", prefix="endsteps-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                for sc in scenes_:
                    for ub in sc["user_blocks"]:
                        out.write(json.dumps(
                            {"type": "user",
                             "message": {"content": f"[用户] {ub}"}},
                            ensure_ascii=False) + "\n")
                    out.write(json.dumps(
                        {"type": "user",
                         "message": {"content":
                                     f"[助手结论] {sc['end_step']}"}},
                        ensure_ascii=False) + "\n")
            r = autodream_mod.autodream(sid, str(tmp), source_cwd=cwd,
                                        harness=harness)
            entry["status"] = "ingested"
            entry["facts"] = r
            for k, v in (r or {}).items():
                agg[k] = agg.get(k, 0) + int(v)
            registry[str(p)] = sha
        except Exception as e:  # noqa: BLE001 — 汇总报告, 不中断其余文件
            entry["status"] = "error"
            entry["error"] = str(e)[:200]
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        details.append(entry)

    if not dry_run and files:
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        rt = reg_path.with_suffix(".json.tmp")
        rt.write_text(json.dumps(registry, ensure_ascii=False, indent=1),
                      encoding="utf-8")
        os.replace(rt, reg_path)

    def _n(status: str) -> int:
        return sum(1 for d in details if d.get("status") == status)

    return {
        "harness": harness,
        "project_dir": str(transcripts_mod._ADAPTERS[harness][0](cwd)),
        "cwd": cwd,
        "files": len(files),
        "ingested": _n("ingested"),
        "skipped_unchanged": _n("skipped-unchanged"),
        "skipped_empty": _n("skipped-empty"),
        "would_ingest": _n("would-ingest"),
        "errors": _n("error"),
        "facts": agg,
        "details": details,
    }


# ── init-memory (bootstrap) ─────────────────────────────────────────

def init_memory(memory_dir: str | None = None,
                source_cwd: str | None = None) -> dict[str, int]:
    """Seed KG from CC memory .md files (ADR-12)。``memory_dir`` 默认
    ``cc_memory_dir(cwd)``(与 synthesis-index 一致; 旧默认硬编码 ``~/.claude`` 全局目录 → 读错),
    ``source_cwd`` 默认 ``cwd``(ADR-14 记来源, 不再 NULL)。"""
    import projection
    cwd = source_cwd or os.getcwd()
    mem_dir = Path(memory_dir) if memory_dir else projection.cc_memory_dir(cwd)
    return bootstrap.init_memory(mem_dir, source_cwd=source_cwd or cwd)


# ── synthesis-index (ADR-15 P2: 散 index 对账 → MEMORY [mem] 唯一写入口) ──

def synthesis_index(scope: str | None = None, memory_dir: str | None = None,
                    session: str | None = None) -> dict:
    """对账散 mem-<id>.md → 回 KG → 重写 MEMORY.md [mem] 索引(ADR-15 P2, 唯一写入口)。

    Thin wrapper over ``projection.synthesis_index``。recall/autodream 建 mem-<id>.md,
    synthesis 集中收口写 MEMORY。冷启动空跳过不兜底; orphan [mem] 行永远删(orphan 文件删默认关)。
    """
    import projection
    cwd = scope or os.getcwd()
    mem_dir = Path(memory_dir) if memory_dir else projection.cc_memory_dir(cwd)
    return projection.synthesis_index(cwd, mem_dir, session_id=session)


# ── prune (DELETE 同步, ADR-17d) ─────────────────────────────────────

def prune(scope: str | None = None, memory_dir: str | None = None,
          dry_run: bool = False) -> dict:
    """CC memory md 删除 → KG fact soft-delete (ADR-17d)。手动触发
    (PostToolUse 不捕 ``rm``, 无 tool 触发删除 → 不自动)。re-ingest 的 DELETE 对称。
    Thin wrapper over ``bootstrap.prune_deleted``。"""
    import projection
    cwd = scope or os.getcwd()
    mem_dir = Path(memory_dir) if memory_dir else projection.cc_memory_dir(cwd)
    return bootstrap.prune_deleted(mem_dir, source_cwd=cwd, dry_run=dry_run)


# ── embed-backfill (存量 active fact value → L2 cache, ADR-13 通电) ──

def embed_backfill() -> dict[str, int]:
    """回填存量 active fact 的 value embedding 到 L2 cache (on-ingest 预计算补存量)。
    命中 cache 跳过 (embed 内部 lookup); provider 不可达 → embedded 不增 (passive)。"""
    import db
    import embedding
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT value FROM fact WHERE status='active' AND value IS NOT NULL AND value != ''"
    ).fetchall()
    distinct = {r["value"] for r in rows}
    embedded = sum(1 for v in distinct if embedding.embed(v))
    return {"active_facts": len(rows), "distinct_values": len(distinct), "embedded": embedded}


# ── stats (churn 监控, ADR-5) ────────────────────────────────────────

def stats() -> dict[str, Any]:
    """只读 churn 快照 (ADR-5): churn_stats + entity/fact 计数。

    聚合 ``store.churn_stats`` (status 分布 + supersede_rate/active_ratio) 与
    ``store.count_entities``; fact 总数复用 churn_stats 内部已聚合的 status 求和。
    非时间序列(降阈值自动刷新本轮 defer, 见 ADR-5 Consequences)。
    """
    import db
    cs = store.churn_stats()
    total_facts = int(cs["active"] + cs["deprecated"] + cs["superseded"])
    return {
        "entities": store.count_entities(),
        "facts": total_facts,
        "churn": cs,
    }


# ── dream-daemon (operational #1: 常驻 autodream loop) ──────────────

def dream_daemon(cwd: str | None = None, interval: int = 30, once: bool = False) -> int:
    """启动 autoDream daemon: 常驻进程 watch CC transcript 增长 → 增量 dream。

    详见 ``mem_daemon.run``。CC flag ``tengu_onyx_plover`` 未开时走文件 watch
    (有 idle/延迟风险); flag 开后 CC 主动 push trigger.json 触发即时 dream。
    """
    import mem_daemon
    return mem_daemon.run(cwd=cwd, interval=interval, once=once)


# ── argv entry ──────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cli", description="mem-service cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="extract+store text")
    ing.add_argument("text")
    ing.add_argument("--source", default=None)
    ing.add_argument(
        "--fact-type",
        dest="fact_type",
        default="stable",
        choices=("ephemeral", "stable", "permanent"),
        help="Fact lifetime class for decay (ADR-8); default stable",
    )

    rec = sub.add_parser("recall", help="recall facts for query")
    rec.add_argument("query")
    rec.add_argument("--verbose", action="store_true")
    rec.add_argument("--vector", action="store_true",
                     help="启用向量召回融合(ADR-13, 解 synonym/rewrite 字面盲区)")
    rec.add_argument("--cwd", dest="cwd", default=None,
                     help="ADR-14 过滤 source_cwd(本 cwd fact + NULL 老数据; 默认全 cwd)")
    rec.add_argument("--session", dest="session", default=None,
                     help="CC session id(默认 CLAUDE_CODE_SESSION_ID env)")
    rec.add_argument("--top-k", dest="top_k", type=int, default=None,
                     help="限制返回数量(默认无截断)")
    rec.add_argument("--with-tag", dest="with_tag", action="store_true",
                     help="返回 nested {results:[{fact,score,tag}]} shape(默认 list[dict]+_snaptag)")
    rec.add_argument("--bfs", action="store_true",
                     help="启用 BFS 图遍历召回(D5, 召回图近但字面/向量远的 fact)")
    rec.add_argument("--as-of", dest="as_of", default=None,
                     help="点时召回(bi-temporal): 只返回 as_of 时刻有效的 fact (valid_from<=t<valid_to)")
    rec.add_argument("--bfs-hops", dest="bfs_hops", type=int, default=2,
                     help="BFS 遍历跳数(默认 2)")
    rec.add_argument("--bfs-scoped", dest="bfs_scoped", action="store_true",
                     help="限 BFS 图构建为本 cwd(source_cwd 过滤; 默认 off 全局图 ADR-14)")
    rec.add_argument("--json", dest="json", action="store_true",
                     help="M15a 稳定 JSON 契约输出 {query, facts:[…]} (字段名即 ABI; 缺省行为不变)")
    rec.add_argument("--project", action="store_true",
                     help="M18: 召回正文投影 recall-<DATE>.md + MEMORY.md 索引行 "
                          "(dir = cc_memory_dir(--cwd 或 $PWD); 空命中不投影)")

    sub.add_parser("consolidate", help="dedup skeleton")

    dream = sub.add_parser("autodream", help="session transcript raw→KG incremental (ADR-10/11)")
    dream.add_argument("--session", dest="session", required=True, help="CC session id")
    dream.add_argument(
        "--transcript", dest="transcript", required=True,
        help="path to CC transcript JSONL",
    )
    dream.add_argument(
        "--cwd", dest="cwd", default=None,
        help="ADR-14 记 source_cwd(来源 cwd, 从 hook stdin cwd 传)",
    )
    dream.add_argument(
        "--harness", dest="harness", default="cc",
        choices=("cc", "codex", "dsh", "pi", "omp"),
        help="语料标记块清洗表键 (corpus_prep; 默认 cc — PreCompact spool 即 CC)",
    )

    ir = sub.add_parser("ingest-recent",
                        help="当前 cwd 最近 N 个 transcript end-step 蒸馏 → LLM 入 KG (手动补口, M18)")
    ir.add_argument("--cwd", dest="cwd", default=None,
                    help="项目 cwd (默认 $PWD; 定位 harness 的项目 transcript 目录)")
    ir.add_argument("--harness",
                    choices=("cc", "dsh", "pi", "omp", "codex"), default="cc",
                    help="transcript 来源 harness (默认 cc; dsh=~/.dsh/sessions, pi=~/.pi/agent/sessions, omp=~/.omp/agent/sessions, codex=~/.codex/sessions 按会话头 cwd 匹配)")
    ir.add_argument("--limit", type=int, default=10,
                    help="取最近 N 个 transcript 按 mtime (默认 10)")
    ir.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只统计蒸馏结果(零 LLM), 不入 KG 不写注册表")

    initmem = sub.add_parser("init-memory", help="seed KG from CC memory .md (ADR-12)")
    initmem.add_argument(
        "--memory-dir", dest="memory_dir", default=None,
        help="CC memory dir (默认 ~/.claude/projects/-home-yy--claude/memory/)",
    )
    initmem.add_argument(
        "--cwd", dest="cwd", default=None,
        help="ADR-14 记 source_cwd(来源 cwd, 默认 NULL)",
    )

    reing = sub.add_parser("re-ingest", help="单 md → KG 增量 (ADR-17 b/c)")
    reing.add_argument("file", help="markdown 文件路径")
    reing.add_argument(
        "--cwd", dest="cwd", default=None,
        help="ADR-14 记 source_cwd(默认 os.getcwd)",
    )

    si = sub.add_parser("synthesis-index",
                        help="散 index 对账 → 重写 MEMORY.md [mem] (ADR-15 P2, MEMORY [mem] 唯一写入口)")
    si.add_argument("--scope", default=None, help="来源 cwd(默认 os.getcwd)")
    si.add_argument("--memory-dir", dest="memory_dir", default=None,
                    help="CC memory dir(默认 cc_memory_dir(scope))")
    si.add_argument("--session", dest="session", default=None,
                    help="CC session id(P2 占位, 暂仅日志用)")
    pr = sub.add_parser("prune",
                        help="CC memory md 删除 → KG fact soft-delete (ADR-17d, re-ingest 的 DELETE 对称)")
    pr.add_argument("--scope", default=None, help="来源 cwd(默认 os.getcwd)")
    pr.add_argument("--memory-dir", dest="memory_dir", default=None,
                    help="CC memory dir(默认 cc_memory_dir(scope))")
    pr.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只报不删(预览将 prune 的孤儿 fact)")
    sub.add_parser("embed-backfill",
                   help="回填 active fact value → L2 embedding cache (ADR-13 向量通电)")
    sub.add_parser("vec-backfill",
                   help="存量回填 vec0 向量索引(vec_entity/vec_fact; perf/vec-index, 幂等可重跑)")
    sub.add_parser("stats",
                   help="只读 churn 快照 (ADR-5): entity/fact 计数 + supersede_rate/active_ratio")
    st = sub.add_parser("stats-json",
                        help="stats 的 M15a 稳定 JSON 契约输出 (同 stats 数据, 契约 shape)")

    # ── M17 四动词 + M16 cite (P38 物化; 无 delete/punish) ──
    wr = sub.add_parser("write", help="四动词 write: 新事实入库 (provenance=通道档)")
    wr.add_argument("subject")
    wr.add_argument("predicate")
    wr.add_argument("value")
    wr.add_argument("--fact-type", dest="fact_type", default="stable",
                    choices=("ephemeral", "stable", "permanent"))
    wr.add_argument("--cwd", default=None, help="ADR-14 source_cwd")
    cf = sub.add_parser("confirm", help="四动词 confirm: 证实既有 fact (记信号)")
    cf.add_argument("fact_id")
    inv = sub.add_parser("invalidate", help="四动词 invalidate: 失效建议 (superseded+contradiction)")
    inv.add_argument("fact_id")
    inv.add_argument("--note", default="", help="失效说明 (入信号)")
    el = sub.add_parser("elevate", help="四动词 elevate: 晋升提名 (不动 fact, 记偏好信号)")
    el.add_argument("fact_id")
    ct = sub.add_parser("cite", help="M16 引用记账 (citations 信号, 单向正奖励)")
    ct.add_argument("fact_id")
    ct.add_argument("--ref", default="", help="agent 输出引用 (output_ref)")
    dd = sub.add_parser("dream-daemon",
                        help="启动 autoDream daemon(常驻 autodream loop, operational #1)")
    dd.add_argument("--cwd", default=None, help="project cwd to watch(默认 $PWD)")
    dd.add_argument("--interval", type=int, default=30,
                    help="poll interval seconds(默认 30)")
    dd.add_argument("--once", action="store_true",
                    help="single sweep, no loop(smoke test / cron mode)")

    # ── M20 KG 实时图浏览 (graphlive: inotify+SSE, 无轮询) ──
    ge = sub.add_parser("graph-export",
                        help="导出全图: --json(快照含游标) / --csv(Cosmograph/Gephi Lite 对, 边带 created_at 时间列)")
    ge.add_argument("--json", dest="as_json", metavar="PATH",
                    help="写 json 快照到 PATH")
    ge.add_argument("--csv", dest="csv_dir", metavar="DIR",
                    help="写 nodes.csv+edges.csv 到 DIR")
    ge.add_argument("--cwd", default=None, help="ADR-14 source_cwd 过滤(含 NULL 老数据)")
    gl = sub.add_parser("graph-live",
                        help="起实时图服务器(http://127.0.0.1:8765/, inotify 事件驱动+SSE 增量)")
    gl.add_argument("--port", type=int, default=8765)
    gl.add_argument("--host", default="127.0.0.1")
    gl.add_argument("--db", dest="db", default=None, help="db 路径(默认 data/memory.db)")

    args = p.parse_args(argv)
    if args.cmd == "ingest":
        print(json.dumps(
            ingest(args.text, source_ref=args.source, fact_type=args.fact_type,
                   source_cwd=os.getcwd()),
            ensure_ascii=False,
        ))
    elif args.cmd == "recall":
        session_id = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")
        result = recall(args.query, verbose=args.verbose, session_id=session_id, use_vec=args.vector, cwd=args.cwd, top_k=args.top_k, with_tag=args.with_tag, use_bfs=args.bfs, bfs_hops=args.bfs_hops, as_of=args.as_of, use_bfs_scoped=args.bfs_scoped, as_json=args.json, project=args.project)
        # ADR-4 bfs hint: direct-match 薄且未开 --bfs → stderr 提示(不污染 stdout 机器输出)。
        # 结果数 < 阈值代理 direct-match 薄(候选少 → 命中少); suggest_bfs 字段在 envelope
        # (with_tag) 里有, 但 cli 走结果数自判覆盖 list/verbose 全 path。
        if not args.bfs:
            n = len(result.get("results", [])) if isinstance(result, dict) and "results" in result else len(result)
            if n < recall_mod.SUGGEST_BFS_THRESHOLD:
                sys.stderr.write("💡 direct-match 薄,可加 --bfs 扩展图近召回\n")
        print(json.dumps(result, ensure_ascii=False, default=str))
    elif args.cmd == "consolidate":
        print(json.dumps(consolidate()))
    elif args.cmd == "autodream":
        print(json.dumps(autodream(args.session, args.transcript, cwd=args.cwd,
                               harness=args.harness), ensure_ascii=False))
    elif args.cmd == "ingest-recent":
        print(json.dumps(ingest_recent(args.cwd, limit=args.limit,
                                       dry_run=args.dry_run,
                                       harness=args.harness),
                         ensure_ascii=False))
    elif args.cmd == "init-memory":
        print(json.dumps(init_memory(args.memory_dir, source_cwd=args.cwd), ensure_ascii=False))
    elif args.cmd == "re-ingest":
        print(json.dumps(bootstrap.re_ingest_file(args.file, source_cwd=args.cwd or os.getcwd()), ensure_ascii=False))
    elif args.cmd == "synthesis-index":
        print(json.dumps(synthesis_index(scope=args.scope, memory_dir=args.memory_dir, session=args.session), ensure_ascii=False))
    elif args.cmd == "prune":
        print(json.dumps(prune(scope=args.scope, memory_dir=args.memory_dir, dry_run=args.dry_run), ensure_ascii=False))
    elif args.cmd == "embed-backfill":
        print(json.dumps(embed_backfill(), ensure_ascii=False))
    elif args.cmd == "vec-backfill":
        import vec_index
        print(json.dumps(vec_index.backfill_all(), ensure_ascii=False))
    elif args.cmd in ("stats", "stats-json"):
        print(json.dumps(stats(), ensure_ascii=False))
    elif args.cmd == "write":
        print(json.dumps(mem_write(args.subject, args.predicate, args.value,
                                   fact_type=args.fact_type,
                                   source_cwd=args.cwd), ensure_ascii=False))
    elif args.cmd == "confirm":
        print(json.dumps(mem_confirm(args.fact_id), ensure_ascii=False))
    elif args.cmd == "invalidate":
        print(json.dumps(mem_invalidate(args.fact_id, note=args.note),
                         ensure_ascii=False))
    elif args.cmd == "elevate":
        print(json.dumps(mem_elevate(args.fact_id), ensure_ascii=False))
    elif args.cmd == "cite":
        print(json.dumps(mem_cite(args.fact_id, output_ref=args.ref),
                         ensure_ascii=False))
    elif args.cmd == "dream-daemon":
        # daemon runs its own loop (blocking); returns exit code, not JSON.
        return dream_daemon(cwd=args.cwd, interval=args.interval, once=args.once)
    elif args.cmd == "graph-export":
        import graphlive
        if not args.as_json and not args.csv_dir:
            print("graph-export: 需要 --json PATH 或 --csv DIR 至少一个", file=sys.stderr)
            return 2
        out = {}
        if args.as_json:
            out["json"] = str(graphlive.export_json(Path(args.as_json), cwd=args.cwd))
        if args.csv_dir:
            nodes_p, edges_p = graphlive.export_csv(Path(args.csv_dir), cwd=args.cwd)
            out["csv"] = [str(nodes_p), str(edges_p)]
        print(json.dumps(out, ensure_ascii=False))
    elif args.cmd == "graph-live":
        import graphlive
        return graphlive.run_server(host=args.host, port=args.port,
                                    db_path=Path(args.db) if args.db else None)
    return 0


if __name__ == "__main__":
    sys.exit(_main())

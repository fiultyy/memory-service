"""mem-service M11 dreaming 期 — 整合层核心 (DR-1 D4 / P30 / P35 三闸 / D12).

六职责 (``run_cycle`` 一轮全跑, 返回各职责计数; 载体 = mem_daemon 主循环
dreaming 阶段门控, DR-8 G8 已裁决):

①  消费 M5 recall_hits 信号流 → LIF 批量重算: 水位 (物理行号) 之后的新信号,
    按 fact 聚合重放 access_count/last_accessed_at/seen_sessions 累积, 再
    ``compute_lif`` 批算写回 — 把 M10 改道掉的即时写回**批量补回**
    (重放语义: compute_lif 是存储态纯函数, N 次增量后批算一次 == 逐次算)。
②  晋升/降级裁决: fact_type 上行 ephemeral→stable→permanent (LIF 阈值,
    [设] 常量可调); 下行沿既有 decay 语义 (consolidate.decay, LIF<0.1 →
    deprecated), 不另造删除。
③  D9 参数反哺 (仅标量): 按 extractor 历史升级率/矛盾率算 SOURCE_WEIGHT
    微调提案 (+/-ε) — **只落 diff 文件供人审** (data/param-diff-<ts>.md),
    绝不运行时改 SOURCE_WEIGHT (不静默改)。
④  复述回流检测 (U7): pending 队列素材与 recall_hits 高频命中 fact 的
    embedding 相似度过高 → 判复述回流, surprise/priority 压档 (×0.5)。
⑤  自述污染剔除: agent 回声模式 (「我记得/如前所述/…」保守 regex) 的
    active fact 标记降档 (fact_type→ephemeral + LIF 减半), 不物理删。
⑥  消费 M4 队列 → wings 升级: dequeue(≤20) → 每项 ``adapter.extract_facts``
    (wings 复活点, 真 LLM) → 成功 mark_done + fact 升级
    (supersede_reason='upgrade', extractor 档位更新); 失败 mark_failed
    (attempts≥3 → dead)。LLM 不可达 → **整轮回退 pending 跳过不 crash**
    (attempts 不烧, upgrade.revert)。

源不变式 (铁律): 本模块所有 extract_facts 调用的输入 = 队列 material_text
(入队时由 transcript 提取面转写, M4/M6 wire 时落列) — **永不读自家 KG 作
提取输入**。KG 读仅用于: LIF 重算存储态 (①, 非提取)、晋升扫描 (②, 裁决)、
D9 聚合统计 (③, 审计)、回流比对基线 (④, 审校比对合法)、污染扫描 (⑤, 裁决)、
升级 supersede 链改写 (⑥, 写路径)。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import adapter
import consolidate as consolidate_mod
import db
import embedding
import resolver
import scoring
import signals
import store
import upgrade

# ① 水位文件 (施工自定): signals 目录下 .watermark.json, {"<stream>": <物理行号>}。
# 用物理行号而非记录数 — signals.read 跳损坏行, 记录数作水位会在损坏行存在时
# 错位重复消费; 物理行号单调且 append-only 下稳定。
WATERMARK_FILE = ".watermark.json"

# ② fact_type 上行阈值 ([设] 可调): LIF ≥ 阈值晋升一档。下行沿 decay 不另设。
PROMOTE_THRESHOLDS = {"ephemeral": 0.7, "stable": 0.85}

# ③ D9 ([设] 可调): 最小样本量 / 触发率 / 步长 ε。
D9_MIN_SAMPLE = 5
D9_UPGRADE_RATE = 0.5     # 升级确认率 ≥ 0.5 → +ε (占位档被 wings 高频确认)
D9_CONTRADICTION_RATE = 0.3  # 矛盾率 ≥ 0.3 → -ε (该档产出噪声偏高)
D9_EPSILON = 0.05

# ④ U7 复述回流 ([设] 可调): 高频命中 = recall_hits 中出现 ≥ MIN_HITS;
# 素材 embedding 与高频命中 fact value 的 max cosine > THRESHOLD → 压档。
REFLUX_MIN_HITS = 2
REFLUX_COSINE_THRESHOLD = 0.92
REFLUX_DEMOTION = 0.5

# ⑤ 自述污染回声模式 (保守 regex, 命中才降档 — 宁漏勿误):
_ECHO_PATTERNS = re.compile(
    r"我记得|如前所述|如上文|前面提到|正如之前|之前说过|"
    r"as\s+i\s+(?:mentioned|noted|said)|as\s+(?:mentioned|noted)\s+above|"
    r"previously\s+(?:mentioned|stated)", re.IGNORECASE)

# ⑥ 出队批量 (= upgrade.DEFAULT_BATCH, 派发令 ≤20/轮)。
QUEUE_BATCH = 20


# ── ① 信号流消费 → LIF 批量重算 ─────────────────────────────────────

def _stream_lines(stream: str) -> list[tuple[int, dict[str, Any]]]:
    """全流物理行 → [(line_no(1-based), 解析记录)] (损坏行占行号不产记录)。"""
    p = signals.stream_path(stream)
    if not p.is_file():
        return []
    out: list[tuple[int, dict[str, Any]]] = []
    with p.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append((i, rec))
    return out


def _load_watermark() -> dict[str, int]:
    p = signals._signals_dir() / WATERMARK_FILE
    if not p.is_file():
        return {}
    try:
        wm = json.loads(p.read_text("utf-8"))
        return wm if isinstance(wm, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_watermark(wm: dict[str, int]) -> None:
    d = signals._signals_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / WATERMARK_FILE).write_text(
        json.dumps(wm, ensure_ascii=False), encoding="utf-8")


def _replay_recall_hits(source_cwd: str | None) -> dict[str, int]:
    """水位后新 recall_hits → 按 fact 聚合批量重放强化 (M10 改道的批量补回)。"""
    lines = _stream_lines("recall_hits")
    wm = _load_watermark()
    last_line = wm.get("recall_hits", 0)
    fresh = [(ln, r) for ln, r in lines if ln > last_line]
    if not fresh:
        return {"signals_consumed": 0, "lif_facts": 0}

    by_fact: dict[str, list[dict[str, Any]]] = {}
    for _, rec in fresh:
        # source_cwd 过滤 (ADR-14 b 同构: NULL 含 — 全局流消费时按调用方域过滤)。
        if source_cwd is not None and rec.get("source_cwd") not in (None, source_cwd):
            continue
        fid = rec.get("fact_id")
        if fid:
            by_fact.setdefault(fid, []).append(rec)

    conn = db.get_conn()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    replayed = 0
    for fid, rows in by_fact.items():
        row = conn.execute("SELECT * FROM fact WHERE id = ?", (fid,)).fetchone()
        if row is None:
            continue  # 已被 supersede/prune 的 fact: 信号丢弃 (存储态权威)
        fact = store._decode_fact(row)
        access_count = int(fact.get("access_count") or 0) + len(rows)
        sessions = list(fact.get("seen_sessions") or [])
        for r in rows:
            sid = r.get("session_id")
            if sid and sid not in sessions:
                sessions.append(sid)
        # 重放语义: last_accessed_at = 该批最新命中时刻 (信号 ts 即 recall 时刻)。
        last_hit = max((r.get("ts") or "") for r in rows) or None
        sib_rows = conn.execute(
            "SELECT predicate FROM fact WHERE subject_id = ? AND id != ? "
            "AND status = 'active'", (fact["subject_id"], fid)).fetchall()
        neighbors = ([{"predicate": fact.get("predicate")}] if fact.get("predicate")
                     else []) + [{"predicate": r["predicate"]} for r in sib_rows]
        dims = scoring.compute_lif(
            fact, access_count=access_count, last_accessed_at=last_hit,
            distinct_sessions=len(sessions), neighbors=neighbors, now=now)
        conn.execute(
            """UPDATE fact SET
                   LIF = ?, lif_freq = ?, lif_recency = ?, lif_spread = ?,
                   lif_coherence = ?, lif_source = ?,
                   access_count = ?, last_accessed_at = ?, seen_sessions = ?
               WHERE id = ?""",
            (dims["LIF"], dims["lif_freq"], dims["lif_recency"], dims["lif_spread"],
             dims["lif_coherence"], dims["lif_source"],
             access_count, last_hit, json.dumps(sessions, ensure_ascii=False), fid))
        replayed += 1
    conn.commit()
    wm["recall_hits"] = lines[-1][0] if lines else last_line
    _save_watermark(wm)
    return {"signals_consumed": len(fresh), "lif_facts": replayed}


# ── ② fact_type 晋升 (上行; 下行沿 decay 语义) ────────────────────────

def _promote_facts() -> int:
    """active fact LIF ≥ 档位阈值 → fact_type 上行一档 ([设] 阈值常量)。"""
    conn = db.get_conn()
    promoted = 0
    rows = conn.execute(
        "SELECT id, fact_type, LIF FROM fact WHERE status='active'").fetchall()
    for row in rows:
        threshold = PROMOTE_THRESHOLDS.get(row["fact_type"])
        if threshold is not None and float(row["LIF"]) >= threshold:
            nxt = {"ephemeral": "stable", "stable": "permanent"}[row["fact_type"]]
            conn.execute("UPDATE fact SET fact_type = ? WHERE id = ?",
                         (nxt, row["id"]))
            promoted += 1
    conn.commit()
    return promoted


# ── ③ D9 参数反哺 (仅标量, diff 供人审不静默改) ───────────────────────

def _d9_param_feedback() -> int:
    """按 extractor 历史升级率/矛盾率生成 SOURCE_WEIGHT 微调提案 → diff 文件。
    返回提案数 (0 = 不落文件)。绝不运行时改 SOURCE_WEIGHT。"""
    conn = db.get_conn()
    proposals: list[dict[str, Any]] = []
    for ext in list(scoring.SOURCE_WEIGHT.keys()):
        total = conn.execute(
            "SELECT COUNT(*) FROM fact WHERE extractor = ?", (ext,)).fetchone()[0]
        if total < D9_MIN_SAMPLE:
            continue
        upgraded = conn.execute(
            "SELECT COUNT(*) FROM fact WHERE extractor = ? AND status='superseded' "
            "AND supersede_reason = 'upgrade'", (ext,)).fetchone()[0]
        contradicted = conn.execute(
            "SELECT COUNT(*) FROM fact WHERE extractor = ? AND status='superseded' "
            "AND supersede_reason = 'contradiction'", (ext,)).fetchone()[0]
        before = float(scoring.SOURCE_WEIGHT[ext])
        after = before
        if upgraded / total >= D9_UPGRADE_RATE:
            after = round(before + D9_EPSILON, 4)   # 占位档被 wings 高频确认
        elif contradicted / total >= D9_CONTRADICTION_RATE:
            after = round(max(0.1, before - D9_EPSILON), 4)  # 档产出噪声偏高
        if after != before:
            proposals.append({"extractor": ext, "param": "SOURCE_WEIGHT",
                              "before": before, "after": after,
                              "upgrade_rate": upgraded / total,
                              "contradiction_rate": contradicted / total,
                              "sample": total})
    if not proposals:
        return 0
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines = [f"# D9 参数反哺提案 · {ts}", "",
             "| extractor | param | before | after | upgrade_rate | "
             "contradiction_rate | sample |", "|---|---|---|---|---|---|---|"]
    for p in proposals:
        lines.append(f"| {p['extractor']} | {p['param']} | {p['before']} | "
                     f"{p['after']} | {p['upgrade_rate']:.2f} | "
                     f"{p['contradiction_rate']:.2f} | {p['sample']} |")
    lines += ["", "人工审后手工改 scoring.SOURCE_WEIGHT (dreaming 绝不静默改)。"]
    ts_compact = ts.replace("-", "").replace(":", "")
    out = signals._signals_dir().parent / f"param-diff-{ts_compact}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(proposals)


# ── ④ U7 复述回流检测 → 压 surprise/priority 档 ───────────────────────

def _suppress_reflux() -> int:
    """pending 素材 embedding 与高频命中 fact value 相似度过高 → 压档。
    embedding 离线 ([]) → 跳过 (降级不 crash, 循 resolver 红线)。"""
    hits: dict[str, int] = {}
    for _, rec in _stream_lines("recall_hits"):
        fid = rec.get("fact_id")
        if fid:
            hits[fid] = hits.get(fid, 0) + 1
    if not hits:
        return 0
    conn = db.get_conn()
    baselines: list[str] = []   # 高频命中 fact 的 value (审校比对基线, 非提取输入)
    for fid, n in hits.items():
        if n >= REFLUX_MIN_HITS:
            row = conn.execute(
                "SELECT value FROM fact WHERE id = ? AND status='active'",
                (fid,)).fetchone()
            if row and row[0]:
                baselines.append(row[0])
    if not baselines:
        return 0
    pending = conn.execute(
        "SELECT id, material_text FROM upgrade_queue WHERE status='pending'"
    ).fetchall()
    suppressed = 0
    for row in pending:
        text = row["material_text"]
        if not text:
            continue
        try:
            vec = embedding.embed(text)
        except Exception:
            vec = []
        if not vec:
            continue
        from recall import _cosine
        best = 0.0
        for bv in baselines:
            try:
                bvec = embedding.embed(bv)  # L1/L2 缓存
            except Exception:
                continue
            if not bvec:
                continue
            sim = _cosine(vec, bvec)
            if sim > best:
                best = sim
        if best > REFLUX_COSINE_THRESHOLD:
            conn.execute(
                "UPDATE upgrade_queue SET surprise = surprise * ?, "
                "priority = priority * ? WHERE id = ?",
                (REFLUX_DEMOTION, REFLUX_DEMOTION, row["id"]))
            suppressed += 1
    conn.commit()
    return suppressed


# ── ⑤ 自述污染剔除 (标记降档, 不物理删) ──────────────────────────────

def _demote_self_pollution() -> int:
    """回声模式命中的 active fact → fact_type 降 ephemeral + LIF 减半
    (标记=档位下沉, 降档=LIF 压缩; 物理删除永不发生 — P38)。"""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, value FROM fact WHERE status='active' AND value IS NOT NULL"
    ).fetchall()
    demoted = 0
    for row in rows:
        if _ECHO_PATTERNS.search(row["value"] or ""):
            conn.execute(
                "UPDATE fact SET fact_type='ephemeral', LIF = LIF * 0.5 "
                "WHERE id = ?", (row["id"],))
            demoted += 1
    conn.commit()
    return demoted


# ── ⑥ M4 队列消费 → wings 升级 ───────────────────────────────────────

def _consume_queue(providers: list | None) -> dict[str, int]:
    """dequeue(≤20) → 逐项 adapter.extract_facts(wings 复活点) → 升级/流转。
    LLM 不可达 (RuntimeError) → 整轮回退 pending 跳过 (attempts 不烧)。"""
    out = {"queue_done": 0, "queue_failed": 0, "queue_skipped": 0,
           "facts_upgraded": 0}
    batch = upgrade.dequeue(QUEUE_BATCH)
    if not batch:
        return out

    def _extract(text: str):
        # 源不变式: 输入 = 队列 material_text (入队时 transcript 提取面转写)。
        return adapter.extract_facts(text, providers=providers)

    # 可达性探针: 首项先走一次 — RuntimeError ⇒ 整轮回退 (不烧 20 项 attempts)。
    first = batch[0]
    if not first["material_text"]:
        upgrade.revert([b["id"] for b in batch])
        out["queue_skipped"] = len(batch)
        return out
    try:
        probe = _extract(first["material_text"])
    except RuntimeError:
        upgrade.revert([b["id"] for b in batch])
        out["queue_skipped"] = len(batch)
        return out

    for item in batch:
        text = item["material_text"]
        if not text:
            upgrade.mark_failed(item["id"])  # 无素材 (legacy 行) → 真失败路径
            out["queue_failed"] += 1
            continue
        try:
            result = probe if item["id"] == first["id"] else _extract(text)
        except RuntimeError:
            upgrade.revert([item["id"]])
            out["queue_skipped"] += 1
            continue
        except Exception:
            upgrade.mark_failed(item["id"])
            out["queue_failed"] += 1
            continue
        out["facts_upgraded"] += _apply_upgrade(item, result)
        upgrade.mark_done(item["id"])
        out["queue_done"] += 1
    return out


def _apply_upgrade(item: dict[str, Any], result: Any) -> int:
    """wings 产出落库。fact:<id> → supersede 旧 fact 建 extractor 档新高 fact
    (supersede_reason='upgrade'); segment:* → 边直接 ADD (provenance 继承素材)。
    返回升级/新增 fact 数。"""
    ext_label = result.source_meta.get("extractor_label", "llm")
    edges = result.edges or []
    conn = db.get_conn()
    ref = item["material_ref"]
    now_iso = datetime.now(timezone.utc).replace(microsecond=0)
    count = 0

    if ref.startswith("fact:"):
        old = store.get_fact(ref[5:])
        if old is None:
            return 0
        if not edges:
            return 0  # wings 合法判空 (非错误): 旧 fact 保留, mark_done 无升级
        edge = edges[0]
        new_id = _put_wings_fact(
            subject_id=old["subject_id"], predicate=edge.predicate,
            value=edge.object, object_id=None,
            provenance=old.get("provenance") or item.get("material_prov"),
            extractor=ext_label, src_refs=list(old.get("source_refs") or []),
            sessions=list(old.get("seen_sessions") or []), now=now_iso)
        store.update_fact_status(old["id"], "superseded", supersedes_id=new_id,
                                 valid_to=store._now(), reason="upgrade")
        return 1

    # segment 素材: 边 ADD-only (确定性升级路径, 无矛盾裁判)。
    for edge in edges:
        subject = (edge.subject or "").strip()
        obj = (edge.object or "").strip()
        if not subject or not obj:
            continue
        sid = resolver.resolve_entity(subject, "concept", providers=None)
        oid = resolver.resolve_entity(obj, "concept", providers=None)
        if sid is None or oid is None:
            continue
        _put_wings_fact(
            subject_id=sid, predicate=(edge.predicate or "relates_to").strip(),
            value=obj, object_id=oid,
            provenance=item.get("material_prov"), extractor=ext_label,
            src_refs=[], sessions=[], now=now_iso)
        count += 1
    return count


def _put_wings_fact(*, subject_id: str, predicate: str, value: str,
                    object_id: str | None, provenance: str | None,
                    extractor: str, src_refs: list[str], sessions: list[str],
                    now: datetime) -> str:
    """wings 升级 fact 入库: extractor 档位 (llm 0.7 / vote 0.85 经
    SOURCE_WEIGHT 自动) + 初始五维 LIF (ADR-8v2, 同 autodream 惯例)。"""
    dims = scoring.compute_lif(
        {"extractor": extractor, "fact_type": "stable",
         "created_at": now.isoformat()},
        access_count=0, last_accessed_at=now.isoformat(),
        distinct_sessions=len(sessions), neighbors=[], now=now)
    return store.put_fact(
        subject_id, predicate, value, object_id=object_id,
        extractor=extractor, provenance=provenance,
        source_refs=src_refs, seen_sessions=sessions,
        LIF=dims["LIF"], lif_freq=dims["lif_freq"],
        lif_recency=dims["lif_recency"], lif_spread=dims["lif_spread"],
        lif_coherence=dims["lif_coherence"], lif_source=dims["lif_source"])


# ── M18 human 投影操作消费 (human 档裁决) ─────────────────────────────

def _consume_human_proj_ops(source_cwd: str | None) -> int:
    """水位后新 human_proj_ops 信号 → human 档裁决:

    - deleted (human 删投影 = 撤回意愿) → fact invalidate: status→superseded,
      provenance='human', veracity 0.9 (M3 映射) — human 档裁决写入。
    - modified (human 改投影) → 记 update 偏好: fact 的 LIF +0.05 微抬
      (human 亲笔编辑是最强正信号之一; 不动 status)。

    裁决后推进水位 (独立水位键, 与 recall_hits 互不干扰)。
    """
    lines = _stream_lines("human_proj_ops")
    wm = _load_watermark()
    last = wm.get("human_proj_ops", 0)
    fresh = [(ln, r) for ln, r in lines if ln > last]
    if not fresh:
        return 0
    conn = db.get_conn()
    applied = 0
    for _, rec in fresh:
        if source_cwd is not None and rec.get("source_cwd") not in (None, source_cwd):
            continue
        fid = rec.get("fact_id")
        op = rec.get("op")
        if not fid or op not in ("deleted", "modified"):
            continue
        row = conn.execute(
            "SELECT id, LIF FROM fact WHERE id = ? AND status='active'",
            (fid,)).fetchone()
        if row is None:
            continue  # 已退场 (卫生/decay 先到) — 信号丢弃
        if op == "deleted":
            # human 撤回 → 复用 invalidate 语义: superseded + contradiction
            # (时效标注), 通道档 = human (裁决证据链落在信号里)。
            store.update_fact_status(fid, "superseded",
                                     valid_to=store._now(),
                                     reason="contradiction")
            conn.execute("UPDATE fact SET provenance='human', veracity=0.9 "
                         "WHERE id=?", (fid,))
        else:  # modified — human 亲笔编辑微抬 LIF (update 偏好信号)。
            conn.execute("UPDATE fact SET LIF = MIN(1.0, LIF + 0.05) "
                         "WHERE id=?", (fid,))
        applied += 1
    conn.commit()
    wm["human_proj_ops"] = lines[-1][0] if lines else last
    _save_watermark(wm)
    return applied


# ── 一轮 dreaming ────────────────────────────────────────────────────

def run_cycle(providers: list | None = None,
              source_cwd: str | None = None) -> dict[str, int]:
    """跑一轮六职责, 返回各职责计数 (空信号空队列 → 全零不报错)。

    providers: ⑥ wings 升级的 LLM providers (None → adapter.default_providers)。
    source_cwd: ① 信号消费的域过滤 (None = 不过滤全消费)。
    """
    stats: dict[str, int] = {}
    stats.update(_replay_recall_hits(source_cwd))            # ①
    stats["human_proj_applied"] = _consume_human_proj_ops(source_cwd)  # M18
    stats["promoted"] = _promote_facts()                     # ② 上行
    stats["deprecated"] = consolidate_mod.decay()["deprecated"]  # ② 下行 (decay 语义)
    # ⑤ 在 decay 之后: 污染减半写复合 LIF 列, 若先跑会被 decay 的五维重算覆盖
    # (降档持久面 = fact_type→ephemeral 标记 + 更短半衰期自然加速后续衰减)。
    stats["pollution_demoted"] = _demote_self_pollution()    # ⑤
    stats["param_proposals"] = _d9_param_feedback()          # ③
    stats["reflux_suppressed"] = _suppress_reflux()          # ④
    stats.update(_consume_queue(providers))                  # ⑥
    return stats

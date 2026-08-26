"""mem-service autoDream — session transcript raw→KG incremental (ADR-10).

PreCompact hook entry: ``autodream(session_id, transcript_path, providers=None)``
reads a CC transcript JSONL **preserving block grammar** (M8: ``_read_transcript``
returns ``(block_type, text)`` pairs — tool_use/tool_result no longer skipped,
S1 fix), groups consecutive same-provenance blocks into segments (M8-v2 G2;
segment budget N4 replaces the old 4000-char flat truncation), and runs the
**M6/M7 占位通道** per segment: ``gazetteer.extract()`` (KG 自举词典 +
extractor.py regex 三路并行, deterministic 零 LLM inline — **行为反转, 反
ADR-5**: regex 复活为占位通道, provider 断供不再中断写入; wings LLM 退役为
异步升级, M4 队列消费侧复活). Each fact inherits its segment's provenance
(P21 出处轴 → M2 column; veracity auto-maps via M3) and lands as
``extractor='regex'`` (lif_source 0.4 档). Then ``consolidate.consolidate()``
(decay+dedup, v2/v3 复用), then an incremental decision per extracted fact:

- **ADD**    — new (subject, predicate, value) not in the active KG → put_fact.
- **UPDATE** — same (subject, predicate, value) already active → refresh LIF
  + absorb the session into source_refs/seen_sessions (recency/spread signal).
- **DELETE** — same (subject, predicate) but a *different* value ⇒ the new fact
  supersedes the old: the old flips to ``status='superseded'`` pointing at the
  new fact's id (the contradiction path; LIF/confidence may also shift).
- **NOOP**   — extracted fact already active with nothing to refresh (identical
  state, second autodream on the same transcript).

Idempotent by construction: re-running on the same transcript (no wall-clock
progress, no new extraction delta) yields ``{added:0, updated:0, deleted:0,
noop:N}`` — the acceptance contract.

Returns ``{"added", "updated", "deleted", "noop"}``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import consolidate as consolidate_mod
import db
import gazetteer
import store
import resolver
import upgrade


def _read_transcript(transcript_path: str | Path) -> list[tuple[str, str]]:
    """Read a CC transcript JSONL as a ``(block_type, text)`` sequence (M8/S1).

    Block grammar preserved — tool_use/tool_result blocks are NO LONGER
    skipped (S1: 世界域最高权威观测 tool_obs 此前完全进不了提取管道).
    Block types yielded:

    - ``user_text`` / ``assistant_text`` — speaker prose (message.content 为
      裸字符串, 或 content list 里的 text 块)。
    - ``tool_use`` — 块 ``input``/``text`` 的可序列化文本 (G2: 意图非观测)。
    - ``tool_result`` — 块 ``content`` 文本: 字符串直取; list 时逐 item 取
      ``text``/``content`` 字段, 无文本的 item 跳过。
    - ``system`` — 其余带可读文本的块 (thinking 等, G2: 其余→system)。

    Tolerates missing fields and malformed lines (hook transcript is
    async-written, may be partial — ADR-10 Consequences) by skipping the
    line/block. Missing file → ``[]``.
    """
    p = Path(transcript_path)
    if not p.is_file():
        return []
    blocks: list[tuple[str, str]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") not in ("user", "assistant"):
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                if content:
                    blocks.append((f"{rec.get('type')}_text", content))
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    t = block.get("text")
                    if isinstance(t, str) and t:
                        blocks.append((f"{rec.get('type')}_text", t))
                elif btype == "tool_use":
                    t = _tool_use_text(block)
                    if t:
                        blocks.append(("tool_use", t))
                elif btype == "tool_result":
                    t = _tool_result_text(block)
                    if t:
                        blocks.append(("tool_result", t))
                else:
                    # 其余块 (thinking 等): 常见可读字段兜底, 无文本跳过。
                    t = block.get("text")
                    if not isinstance(t, str) or not t:
                        t = block.get("thinking")
                    if isinstance(t, str) and t:
                        blocks.append(("system", t))
    return blocks


def _tool_use_text(block: dict) -> str:
    """tool_use 块取 ``input``/``text`` 可序列化文本 (S1; input 优先, text 兜底)。"""
    if "input" in block:
        val = block["input"]
    else:
        val = block.get("text")
    if isinstance(val, str):
        return val
    if isinstance(val, (dict, list)) and val:
        try:
            return json.dumps(val, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return ""


def _tool_result_text(block: dict) -> str:
    """tool_result 块取 ``content`` 文本: 字符串直取; list 逐 item 取
    ``text``/``content`` 字段 (bare 字符串 item 容错收下), 无文本 item 跳过。"""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item:
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text")
                if not (isinstance(t, str) and t):
                    t = item.get("content")
                if isinstance(t, str) and t:
                    parts.append(t)
        return "\n".join(parts)
    return ""




# M8-v2 G2: 块类型 → provenance (P21 出处轴; 权重档已铺 store.PROVENANCE_VERACITY,
# DR-6 G1 裁决)。tool_use 归 agent_assert (意图非观测); tool_result 归 tool_obs
# (世界域最高权威观测, S1 修复后入管道); 其余 (thinking 等) → system。
_PROVENANCE_BY_BLOCK: dict[str, str] = {
    "user_text": "user_prose",
    "assistant_text": "agent_assert",
    "tool_use": "agent_assert",
    "tool_result": "tool_obs",
}

# N4 段预算(字符/段): 超长段截尾入段, 替换旧 4000 平截断(已废)。可调 —
# 提高 = 单段上下文更全 + LLM 成本更高; 降低 = 更省但截断更多。
_SEGMENT_BUDGET = 1200


def _block_provenance(block_type: str) -> str:
    """块类型 → provenance; 未识别块 → system (G2: 其余→system)。"""
    return _PROVENANCE_BY_BLOCK.get(block_type, "system")


def _build_segments(blocks: list[tuple[str, str]],
                    budget: int | None = None,
                    truncated: list[tuple[int, str]] | None = None) -> list[tuple[str, str]]:
    """连续同 provenance 块合并为段 (M8-v2 G2), 段超预算截尾 (N4)。

    Returns ``[(provenance, segment_text), ...]`` in encounter order. 段内块以
    ``\\n`` 连接; 每段文本截尾到 ``budget`` 字符 (缺省
    :data:`_SEGMENT_BUDGET`) — 段预算替换旧 4000 平截断, tool 块入管道后
    长观测不再被整体丢弃, 但单段 LLM 调用有界。

    ``truncated`` (M4 wire, 可选出参): 传入 list 时, 每个发生截尾的段 append
    ``(seg_index, full_text)`` — 调用方 (autodream) 据此把全文 ref 送入
    upgrade 队列 (M8→M4 wire 点)。
    """
    if budget is None:
        budget = _SEGMENT_BUDGET
    segments: list[tuple[str, str]] = []
    for block_type, text in blocks:
        if not text:
            continue
        prov = _block_provenance(block_type)
        if segments and segments[-1][0] == prov:
            merged = segments[-1][1] + "\n" + text
            seg_index = len(segments) - 1
            extend = True
        else:
            merged = text
            seg_index = len(segments)
            extend = False
        if len(merged) > budget and truncated is not None:
            truncated.append((seg_index, merged))  # N4 截尾 → M4 全文 ref
        if extend:
            segments[-1] = (prov, merged[:budget])
        else:
            segments.append((prov, merged[:budget]))
    return segments



def _find_active_fact(subject_id: str, predicate: str, value: str) -> dict[str, Any] | None:
    """Lookup a Fact by exact (subject_id, predicate, value).

    Scans active first (main path); falls back to superseded so a re-extracted
    value that was previously superseded is still recognised (UPDATE/NOOP) —
    prevents the supersede oscillation on rerun. Returns the decoded Fact or None.
    ponytail: linear scan, single-machine MVP ceiling.
    """
    for status in ("active", "superseded"):
        for f in store.get_facts_by_subject(subject_id, status=status):
            if f["predicate"] == predicate and (f.get("value") or "") == value:
                return f
    return None


# ponytail: ADR-1 R1 — 已知多值谓词集 short-circuit(不走 LLM), 省 token + 防 LLM
# 误判共存。单值/开放谓词走 provider.judge_contradiction 纯 LLM 裁判(Graphiti 式)。
# 升级路径: LLM 自由谓词时改读 fact schema cardinality 字段。
_MULTIVALUE_PREDICATES = frozenset({
    "uses", "depends_on", "contains", "implements",
    "connected_to", "part_of", "relates_to",
})


def _judge_contradiction(providers: list, subject_type: str, subject_name: str,
                         predicate: str, new_value: str, old_value: str) -> bool:
    """Whether ``new_value`` contradicts ``old_value`` for the same subject+
    predicate (ADR-1 R1).

    Two fast paths skip the LLM: (1) multivalue predicates always coexist → False;
    (2) identical values → False (same fact, not contradiction). Otherwise ask the
    first reachable provider's ``judge_contradiction``. Provider unreachable /
    raises / returns non-bool → fallback ``contradiction=False`` (do NOT supersede,
    do NOT block ingest — matches A1 fallback contract). NEVER raises.
    """
    if predicate in _MULTIVALUE_PREDICATES:
        return False
    if new_value == old_value:
        return False
    if not providers:
        return False
    try:
        verdict = providers[0].judge_contradiction(
            subject_type, subject_name, predicate, new_value, old_value)
    except Exception:
        return False
    return bool(verdict and verdict.get("contradiction") is True)


def _has_active_for_predicate(subject_id: str, predicate: str) -> list[dict[str, Any]]:
    """All active facts of this subject with this predicate (value-agnostic)."""
    return [
        f for f in store.get_facts_by_subject(subject_id, status="active")
        if f["predicate"] == predicate
    ]


def autodream(session_id: str, transcript_path: str, providers: list | None = None, fact_type: str = "stable", source_cwd: str | None = None) -> dict[str, int]:
    """Incrementally整理 a session transcript into the KG (ADR-10).

    Pipeline (ADR-10 Decision (a)/(b)/(c)):

    1. ``consolidate.consolidate()`` — decay+dedup 复用 v2/v3 (phase a).
    2. ``_read_transcript`` (块文法, M8) + ``_build_segments`` (连续同 provenance
       合并成段, G2; 段预算截尾, N4) + 逐段 ``gazetteer.extract()`` M7 占位
       提取 (词典+regex 三路, 零 LLM inline — M6 反 ADR-5: provider 断供不再
       raise, wings 退役为异步升级 M4); fact 继承段 provenance (M2 列, veracity
       由 M3 映射自动生成; extractor='regex' → lif_source 0.4)。
    3. Incremental decision per extracted fact (phase c): ADD / UPDATE / DELETE
       (supersede) / NOOP, tally counts.

    Args:
        session_id: The CC session being dreamt (stamped into source_refs /
            seen_sessions for provenance + LIF spread).
        transcript_path: Path to the CC transcript JSONL.

    Returns:
        ``{"added": int, "updated": int, "deleted": int, "noop": int}``.
        Idempotent: a re-run on the same transcript yields all-NOOP (the
        acceptance cmd's second-call ``added == 0`` contract).
    """
    db.get_conn()  # ensure schema initialised on first call
    # Phase a — decay + dedup (v2/v3 复用). consolidate is idempotent on a
    # stable wall clock, so re-runs add no churn.
    consolidate_mod.consolidate()

    # Phase b — M8 块文法 + M6 占位通道: (block_type, text) 序列 → 连续同
    # provenance 块合并成段 (G2) + 段预算截尾 (N4); 逐段调 M7 gazetteer 占位
    # 提取器 (词典+regex 三路, 零 LLM inline), fact 直接继承段 provenance
    # (M2 通道; veracity 由 M3 映射自动生成, 不另传)。wings (adapter LLM)
    # 退役为异步升级 — 主径 provider 断供不再 RuntimeError (反 ADR-5)。
    # TODO(M4) 已落地: 段/事实标记待升级 — 两个 wire 点接 upgrade 队列(下)。
    blocks = _read_transcript(transcript_path)
    truncated_segs: list[tuple[int, str]] = []
    segments = _build_segments(blocks, truncated=truncated_segs)
    # M8→M4 wire: 超长段截尾的全文 ref 入升级队列 (wings 异步升级; M9 入队时算 surprise)。
    for seg_idx, full_text in truncated_segs:
        prov_of_seg = segments[seg_idx][0] if seg_idx < len(segments) else None
        upgrade.enqueue_segment(transcript_path, seg_idx, full_text,
                                provenance=prov_of_seg)
    # M6: providers 仅供 contradiction judge (显式传入才生效); 主径提取零 LLM,
    # 不再 default_providers() 自取。
    active_providers = list(providers) if providers else []
    # 分段提取 + 三级空产出时序 (追加 A/C): 段提取零产出 →
    #   C 层 (零 LLM 兜底): CJK span 批量 embed → vec_entity ANN ≥0.45 →
    #     链接既有实体 (**只产实体声明不造谓词边** — span 无句式证据造边=
    #     臆测, 谓词留 wings; 落库走 resolver step1 精确命中路径);
    #   仍无 edges → A 层: enqueue_segment 全文入队 (wings 异步; C 不吞 A —
    #     实体链接了语义内容还没提)。
    # 幂等: 同 material_ref 拒重; M9 novelty (embedding 语言中立) 定优先级,
    # wings 判「无事实」→ 合法 done, attempts≥3 封顶防重复浪费。
    seg_results: list[tuple[str, Any]] = []
    for seg_idx, (seg_prov, seg_text) in enumerate(segments):
        result = gazetteer.extract(seg_text)
        if not result.entities and not result.edges:
            # C 层: 语义兜底实体声明 (与 B 共用 _link_spans 管道)。
            c_ents = gazetteer.semantic_fallback_hits(seg_text)
            if c_ents:
                result.entities = c_ents  # 实体声明接管; edges 保持空
            if not result.edges:
                # A 层: 全文入队 (C 命中实体但无边也入 — 语义内容待 wings)。
                upgrade.enqueue_segment(transcript_path, seg_idx, seg_text,
                                        provenance=seg_prov)
        seg_results.append((seg_prov, result))

    # Initial 5-dim LIF at ingest (ADR-8v2): distinct_sessions=1 when session_id
    # present (fact's seen_sessions starts with it). coherence=1.0 (no siblings
    # queried; consolidate recomputes authoritatively).
    import scoring as scoring_mod
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0)

    src_ref = f"session:{session_id}" if session_id else None
    added = updated = deleted = noop = 0

    # Phase c — incremental decision per edge, per segment (M8: fact 继承段 provenance)。
    # R1 档 1: entities first (so declared types land), then edges. subject AND
    # object both resolve to entities → put_fact(object_id=...) 必非空.
    # ponytail: rebuild a name→entity_id/type cache per call, shared across
    # segments (autodream is the single writer in a PreCompact hook; no
    # cross-call cache needed).
    name_to_id: dict[str, str] = {}
    name_to_type: dict[str, str] = {}
    for seg_provenance, result in seg_results:
        ext_label = result.source_meta.get("extractor_label", "llm")
        lif_dims = scoring_mod.compute_lif(
            {"extractor": ext_label, "fact_type": fact_type, "created_at": now.isoformat()},
            access_count=0,
            last_accessed_at=now.isoformat(),
            distinct_sessions=1 if session_id else 0,
            neighbors=[],
            now=now,
        )

        def _put_new_fact(**edge_kw):
            """Persist a new fact with confidence + initial LIF dims (used by both
            contradiction-supersede and brand-new ADD paths). M8: stamps the
            segment's provenance (M2 column); veracity auto-maps via M3."""
            return store.put_fact(
                **edge_kw,
                confidence=result.confidence,
                provenance=seg_provenance,
                LIF=lif_dims["LIF"],
                lif_freq=lif_dims["lif_freq"], lif_recency=lif_dims["lif_recency"],
                lif_spread=lif_dims["lif_spread"], lif_coherence=lif_dims["lif_coherence"],
                lif_source=lif_dims["lif_source"],
            )

        # perf/vec-index: 段级实体批式消解 — 一次 embed 批 (embed_batch 单次
        # POST 预热 L1) + 逐名三步协议 (aliases 语义经 aliases_map 全保留);
        # names 跨段共享 name_to_id 缓存。
        seg_entities = [ent for ent in result.entities if ent.name]
        seg_resolved = resolver.resolve_entities_batch(
            [e.name for e in seg_entities],
            entity_types=[e.type for e in seg_entities],
            aliases_map={e.name: list(e.aliases) for e in seg_entities
                         if getattr(e, "aliases", None)},
            providers=active_providers) if seg_entities else {}
        for ent in result.entities:
            if not ent.name:
                continue
            sid = seg_resolved.get(ent.name)
            if sid is None and ent.name not in seg_resolved:
                # 批式未覆盖 (异常防御) → 单条兜底, 协议不变。
                sid = resolver.resolve_entity(
                    ent.name, ent.type,
                    aliases=getattr(ent, 'aliases', None) or None,
                    providers=active_providers)
            if sid is not None:
                name_to_id[ent.name] = sid
                name_to_type[ent.name] = ent.type

        for edge in result.edges:
            subject = (edge.subject or "").strip()
            predicate = (edge.predicate or "").strip()
            value = (edge.object or "").strip()
            if not subject or not predicate or not value:
                continue
            topic = (edge.topic or "").strip() or None  # ADR-C: 投影 slug/title/desc 源

            if subject not in name_to_id:
                sid = resolver.resolve_entity(subject, name_to_type.get(subject, "concept"),
                                              providers=active_providers)
                if sid is None:
                    continue
                name_to_id[subject] = sid
            subject_id = name_to_id[subject]

            # object is a declared entity reference (R1 §A2) — resolve + link.
            if value not in name_to_id:
                oid = resolver.resolve_entity(value, name_to_type.get(value, "concept"),
                                              providers=active_providers)
                if oid is None:
                    continue
                name_to_id[value] = oid
            object_id = name_to_id[value]

            # Exact (subject, predicate, value) match ⇒ UPDATE / NOOP.
            exact = _find_active_fact(subject_id, predicate, value)
            if exact is not None:
                # Refresh LIF + absorb session (the reinforcement signal). If the
                # fact already saw this session and nothing else moved, the refresh
                # is a no-op on stored state ⇒ count as NOOP (idempotency).
                seen_sessions = list(exact.get("seen_sessions") or [])
                source_refs = list(exact.get("source_refs") or [])
                already_seen = session_id in seen_sessions
                already_ref = (src_ref in source_refs) if src_ref else True
                if already_seen and already_ref:
                    noop += 1
                    continue
                if session_id and session_id not in seen_sessions:
                    seen_sessions.append(session_id)
                if src_ref and src_ref not in source_refs:
                    source_refs.append(src_ref)
                _refresh_fact_meta(exact["id"], seen_sessions, source_refs)
                updated += 1
                continue

            # Same (subject, predicate), different value: supersede ONLY on a real
            # contradiction (ADR-1 R1). Multivalue predicates (uses/depends_on/...)
            # short-circuit to no-contradiction (coexist); single-valued/open
            # predicates ask the judge — M6 占位径 providers 默认 [] → 规则
            # fallback (值比较共存, 不 supersede 不阻断); 显式传 providers 时才
            # 问 LLM judge。一致性: contradiction ⇒ supersede 设 valid_to。
            subject_type = name_to_type.get(subject, "concept")
            siblings = _has_active_for_predicate(subject_id, predicate)
            contradicting = [s for s in siblings
                             if _judge_contradiction(
                                 active_providers, subject_type, subject, predicate,
                                 value, s.get("value") or "")]
            if contradicting:
                new_id = _put_new_fact(
                    subject_id=subject_id,
                    predicate=predicate,
                    value=value,
                    object_id=object_id,
                    extractor=ext_label,
                    fact_type=fact_type,
                    source_cwd=source_cwd,
                    source_refs=[src_ref] if src_ref else [],
                    seen_sessions=[session_id] if session_id else [],
                    topic=topic,
                )
                for old in contradicting:
                    store.update_fact_status(old["id"], "superseded", supersedes_id=new_id, valid_to=store._now(), reason="contradiction")  # M1: contradiction 必带 reason
                # M6→M4 wire: 占位 fact 落库后待升级项入队 (wings 异步升级)。
                upgrade.enqueue_fact(new_id, subject=subject, predicate=predicate, obj=value,
                                    provenance=seg_provenance)
                deleted += len(contradicting)
                added += 1
                continue
            # 多值共存 / 无矛盾 ⇒ 落到下方 brand-new ADD (不 continue)。

            # Brand new — ADD.
            new_id = _put_new_fact(
                subject_id=subject_id,
                predicate=predicate,
                value=value,
                object_id=object_id,
                extractor=ext_label,
                fact_type=fact_type,
                source_cwd=source_cwd,
                source_refs=[src_ref] if src_ref else [],
                seen_sessions=[session_id] if session_id else [],
                topic=topic,
            )
            # M6→M4 wire: 占位 fact 落库后待升级项入队 (wings 异步升级)。
            upgrade.enqueue_fact(new_id, subject=subject, predicate=predicate, obj=value,
                                provenance=seg_provenance)
            added += 1

    return {"added": added, "updated": updated, "deleted": deleted, "noop": noop}


def _refresh_fact_meta(fact_id: str, seen_sessions: list[str], source_refs: list[str]) -> None:
    """Write back absorbed seen_sessions + source_refs and recompute LIF.

    ADR-8v2: spread derives from distinct sessions, so absorbing a new session
    must lift lif_spread — recompute via the consolidate decay pass's
    ``compute_lif`` so the dim stays authoritative. We touch access_count/
    last_accessed_at too (a session re-seeing a fact is mild reinforcement).
    """
    from datetime import datetime, timezone

    import scoring

    conn = db.get_conn()
    row = conn.execute("SELECT * FROM fact WHERE id = ?", (fact_id,)).fetchone()
    if row is None:
        return
    fact = store._decode_fact(row)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_iso = now.isoformat()
    access_count = int(fact.get("access_count") or 0) + 1

    # coherence: subject siblings incl. self (mirrors refresh_lif_on_recall).
    own_pred = fact.get("predicate")
    sib_rows = conn.execute(
        "SELECT predicate FROM fact WHERE subject_id = ? AND id != ? AND status = 'active'",
        (fact["subject_id"], fact_id),
    ).fetchall()
    neighbors = (
        ([{"predicate": own_pred}] if own_pred else [])
        + [{"predicate": r["predicate"]} for r in sib_rows]
    )

    dims = scoring.compute_lif(
        fact,
        access_count=access_count,
        last_accessed_at=now_iso,
        distinct_sessions=len(seen_sessions),
        neighbors=neighbors,
        now=now,
    )
    conn.execute(
        """UPDATE fact SET
               LIF = ?, lif_freq = ?, lif_recency = ?, lif_spread = ?,
               lif_coherence = ?, lif_source = ?,
               access_count = ?, last_accessed_at = ?,
               seen_sessions = ?, source_refs = ?
           WHERE id = ?""",
        (
            dims["LIF"], dims["lif_freq"], dims["lif_recency"], dims["lif_spread"],
            dims["lif_coherence"], dims["lif_source"],
            access_count, now_iso,
            json.dumps(seen_sessions, ensure_ascii=False),
            json.dumps(source_refs, ensure_ascii=False),
            fact_id,
        ),
    )
    conn.commit()


__all__ = ["autodream"]

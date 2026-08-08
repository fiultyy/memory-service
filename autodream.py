"""mem-service autoDream — session transcript raw→KG incremental (ADR-10).

PreCompact hook entry: ``autodream(session_id, transcript_path, providers=None)``
reads a CC transcript JSONL (user/assistant ``message.content`` 拼文本), reuses
``adapter.extract_facts()`` (ADR-5b 蝴蝶翼 LLM with ADR-5 regex fallback) to pull
facts, runs ``consolidate.consolidate()`` (decay+dedup, v2/v3 复用), then makes
an incremental decision per extracted fact:

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

import adapter
import consolidate as consolidate_mod
import db
import store
import resolver


def _read_transcript(transcript_path: str | Path) -> str:
    """Concatenate user/assistant ``message.content`` from a CC transcript JSONL.

    Each line is one event record; we keep ``type`` in {"user","assistant"} and
    pull ``message.content``. ``content`` may be a string or a list of content
    blocks (tool_use/tool_result/text) — text blocks are joined, non-text blocks
    are skipped (they carry no extractable prose). Tolerates missing fields and
    malformed lines (hook transcript is async-written, may be partial — ADR-10
    Consequences) by skipping the line.
    """
    p = Path(transcript_path)
    if not p.is_file():
        return ""
    parts: list[str] = []
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
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    # content blocks: {"type":"text","text":"..."} etc.
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = block.get("text")
                        if isinstance(t, str):
                            parts.append(t)
    return "\n".join(parts)




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


# ponytail: predicate 基数 — functional(单值, 不同 value 才算矛盾) vs multivalue(多值共存)。
# 与 extractor 7 谓词对应。升级路径: LLM 自由谓词时改读 fact schema cardinality 字段。
_FUNCTIONAL_PREDICATES = frozenset({"is_a", "belongs_to"})


def _is_contradiction(predicate: str, new_value: str, old_value: str) -> bool:
    """True only for functional (single-valued) predicates with a different value.

    Multivalue predicates (uses/depends_on/contains/implements/connected_to) coexist:
    "项目 uses rust AND docker" 非矛盾, 是并存。
    """
    return predicate in _FUNCTIONAL_PREDICATES and new_value != old_value


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
    2. ``_read_transcript`` + ``adapter.extract_facts()`` 蝴蝶翼 LLM — session→facts
       (phase b; 无 regex 降级 — LLM 不可用即 raise block)。
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

    # Phase b — session→facts via adapter (ADR-5b 蝴蝶翼 LLM 直连, 无 regex 降级 —
    # LLM 不可用即 raise block, 不静默产低质量 fact)。
    text = _read_transcript(transcript_path)
    if len(text) > 4000:
        text = text[:4000]  # ponytail: 截断长 session (防 LLM 超时, 同 bootstrap)
    active_providers = adapter.default_providers() if providers is None else providers
    result = adapter.extract_facts(text, providers=active_providers)
    ext_label = "llm"

    src_ref = f"session:{session_id}" if session_id else None
    added = updated = deleted = noop = 0

    # Phase c — incremental decision per edge.
    # R1 档 1: entities first (so declared types land), then edges. subject AND
    # object both resolve to entities → put_fact(object_id=...) 必非空.
    # ponytail: rebuild a name→entity_id/type cache per call (autodream is the
    # single writer in a PreCompact hook; no cross-call cache needed).
    name_to_id: dict[str, str] = {}
    name_to_type: dict[str, str] = {}
    for ent in result.entities:
        if not ent.name:
            continue
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

        # Same (subject, predicate), different value: supersede ONLY if the
        # predicate is functional (single-valued: is_a/belongs_to) — a real
        # contradiction. Multivalue predicates (uses/depends_on/contains/...)
        # coexist (项目 uses rust AND docker 非矛盾) ⇒ fall through to ADD.
        siblings = _has_active_for_predicate(subject_id, predicate)
        contradicting = [s for s in siblings
                         if _is_contradiction(predicate, value, s.get("value") or "")]
        if contradicting:
            new_id = store.put_fact(
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
                store.update_fact_status(old["id"], "superseded", supersedes_id=new_id, valid_to=store._now())
            deleted += len(contradicting)
            added += 1
            continue
        # 多值共存 / 无矛盾 ⇒ 落到下方 brand-new ADD (不 continue)。

        # Brand new — ADD.
        store.put_fact(
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

"""mem-service store — Entity + Fact CRUD over SQLite (ADR-2, ADR-3).

No MemoryItem layer — Fact reification is self-contained. Entity↔Fact linkage
is via Fact.subject_id/object_id (reverse lookup); raw provenance lives on
Fact.source_refs.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex


# ── Entity ──────────────────────────────────────────────────────────

def put_entity(name: str, entity_type: str, properties: dict[str, Any] | None = None,
               entity_id: str | None = None) -> str:
    """Insert an entity, return its id. Caller dedups upstream if desired."""
    conn = db.get_conn()
    eid = entity_id or _uid()
    conn.execute(
        "INSERT INTO entity (id, name, entity_type, properties, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (eid, name, entity_type, json.dumps(properties or {}, ensure_ascii=False), _now()),
    )
    conn.commit()
    return eid


def get_entity(entity_id: str) -> dict[str, Any] | None:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM entity WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return None
    return _decode_entity(row)


def count_entities() -> int:
    """Row count of the entity table (acceptance/inspection helper)."""
    conn = db.get_conn()
    return conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]


def find_entities_by_name(name: str, entity_type: str | None = None) -> list[dict[str, Any]]:
    """Exact-name lookup (dedup helper; v1 recall uses LIKE, not this)."""
    conn = db.get_conn()
    if entity_type:
        rows = conn.execute(
            "SELECT * FROM entity WHERE name = ? AND entity_type = ?", (name, entity_type)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM entity WHERE name = ?", (name,)).fetchall()
    return [_decode_entity(r) for r in rows]


def _decode_entity(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "entity_type": row["entity_type"],
        "properties": json.loads(row["properties"]) if row["properties"] else {},
        "created_at": row["created_at"],
    }


# ── Fact ────────────────────────────────────────────────────────────

def put_fact(
    subject_id: str,
    predicate: str,
    value: str | None = None,
    *,
    object_id: str | None = None,
    fact_type: str = "stable",
    LIF: float = 0.5,
    confidence: float = 0.5,
    source_refs: list[str] | None = None,
    extractor: str = "regex",
    valid_from: str | None = None,
    valid_to: str | None = None,
    status: str = "active",
    supersedes_id: str | None = None,
    fact_id: str | None = None,
    original_lif: float | None = None,
    lif_freq: float = 0.0,
    lif_recency: float = 0.5,
    lif_spread: float = 0.0,
    lif_coherence: float = 0.0,
    lif_source: float | None = None,
    access_count: int = 0,
    last_accessed_at: str | None = None,
    seen_sessions: list[str] | None = None,
    source_cwd: str | None = None,
    topic: str | None = None,
) -> str:
    """Insert a Fact (reified), return its id.

    Literal/unary facts: pass ``value`` only (object_id stays None).
    Binary entity→entity facts: pass ``object_id`` (value optional).

    ``original_lif`` (ADR-8v2): semantics shifted from ADR-8 decay base to the
    source-dim initial-value snapshot — defaults to ``LIF``. The LIF-Scorer
    node composes LIF from the five dims; this store writes them verbatim
    (no composition). ``lif_source`` defaults to ``SOURCE_WEIGHT[extractor]``
    (regex=0.4) when None — see consolidate.SOURCE_WEIGHT for the canonical
    table.

    ``topic`` (ADR-C): LLM 生成的一句话可读事实, 投影 filename slug + index
    title + description 的唯一来源。None/空 → 投影回退到三元组拼接。
    """
    conn = db.get_conn()
    fid = fact_id or _uid()
    frozen_lif = float(LIF) if original_lif is None else float(original_lif)
    if lif_source is None:
        from consolidate import SOURCE_WEIGHT
        lif_source = SOURCE_WEIGHT.get(extractor, 0.4)
    conn.execute(
        """INSERT INTO fact
           (id, subject_id, predicate, object_id, value, valid_from, valid_to,
            fact_type, LIF, original_lif, confidence, source_refs, extractor,
            status, supersedes_id, created_at,
            lif_freq, lif_recency, lif_spread, lif_coherence, lif_source,
            access_count, last_accessed_at, seen_sessions, source_cwd, topic)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            fid, subject_id, predicate, object_id, value, valid_from, valid_to,
            fact_type, LIF, frozen_lif, confidence,
            json.dumps(source_refs or [], ensure_ascii=False),
            extractor, status, supersedes_id, _now(),
            lif_freq, lif_recency, lif_spread, lif_coherence, lif_source,
            access_count, last_accessed_at,
            json.dumps(seen_sessions or [], ensure_ascii=False),
            source_cwd, topic,
        ),
    )
    conn.commit()
    if value:
        try:
            import embedding
            embedding.embed(value)  # ponytail: L2 cache 预热 (on-ingest), passive 失败不影响写入
        except Exception:
            pass
    return fid


def get_fact(fact_id: str) -> dict[str, Any] | None:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM fact WHERE id = ?", (fact_id,)).fetchone()
    return _decode_fact(row) if row else None


def get_facts_by_subject(subject_id: str, status: str | None = "active") -> list[dict[str, Any]]:
    conn = db.get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM fact WHERE subject_id = ? AND status = ? ORDER BY created_at DESC",
            (subject_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM fact WHERE subject_id = ? ORDER BY created_at DESC", (subject_id,)
        ).fetchall()
    return [_decode_fact(r) for r in rows]


def update_fact_status(fact_id: str, status: str, supersedes_id: str | None = None) -> None:
    """Lifecycle transition (active→deprecated/superseded). No-op if missing."""
    conn = db.get_conn()
    conn.execute(
        "UPDATE fact SET status = ?, supersedes_id = COALESCE(?, supersedes_id) WHERE id = ?",
        (status, supersedes_id, fact_id),
    )
    conn.commit()


def _decode_fact(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "subject_id": row["subject_id"],
        "predicate": row["predicate"],
        "object_id": row["object_id"],
        "value": row["value"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "fact_type": row["fact_type"],
        "LIF": row["LIF"],
        "original_lif": row["original_lif"],
        "confidence": row["confidence"],
        "source_refs": json.loads(row["source_refs"]) if row["source_refs"] else [],
        "extractor": row["extractor"],
        "status": row["status"],
        "supersedes_id": row["supersedes_id"],
        "created_at": row["created_at"],
        # ADR-8v2 LIF five-dim composite + recall-reinforcement state.
        "lif_freq": row["lif_freq"],
        "lif_recency": row["lif_recency"],
        "lif_spread": row["lif_spread"],
        "lif_coherence": row["lif_coherence"],
        "lif_source": row["lif_source"],
        "access_count": row["access_count"],
        "last_accessed_at": row["last_accessed_at"],
        "seen_sessions": json.loads(row["seen_sessions"]) if row["seen_sessions"] else [],
        "source_cwd": row["source_cwd"] if "source_cwd" in row.keys() else None,
        "topic": row["topic"] if "topic" in row.keys() else None,
    }

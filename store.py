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
    # ms-floor 对齐 scoring.py/consolidate.py/recall.py 的 .replace(microsecond=0)
    # 惯例(三处 _now 语义统一, ADR-3 ①)。秒级 ISO-8601 + 固定 +00:00 → TEXT
    # 字典序 = 时间序(recall._temporal_clause 依赖此隐式假设)。
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex


# ── Entity ──────────────────────────────────────────────────────────

def put_entity(name: str, entity_type: str, properties: dict[str, Any] | None = None,
               entity_id: str | None = None,
               aliases: list[str] | None = None,
               name_embedding: list[float] | None = None) -> str:
    """Insert an entity, return its id. Caller dedups upstream if desired.

    ``aliases`` (ADR-D7): 同实体异写别名, None → []. ``name_embedding`` (ADR-D7):
    名称向量(JSON list); None → [](空)。**put_entity 不做网络 I/O** — embedding 计算
    属 resolver(Node B step 2 算一次, 显式传入); store 是纯存储原语。这样 entity 创建
    不耦合 embedding provider(离线/防火墙不 block, 测试不污染 embeddings.db)。

    ADR-2 ①: UNIQUE(name, entity_type) 约束在并发 re-ingest 同实体竞态时抛
    IntegrityError → fallback find_entity_exact 复用既有行(与 resolver 两步闸语义一致,
    不建孤儿, 返回既有 entity_id 不建新)。fallback 查无(case-insensitive 无命中, 仅
    exact-case 冲突)则按 find_entities_by_name 取同 type 既有行; 仍无则重抛(不应发生)。
    """
    conn = db.get_conn()
    eid = entity_id or _uid()
    if aliases is None:
        aliases = []
    if name_embedding is None:
        name_embedding = []  # resolver owns embedding; store stays network-free
    try:
        conn.execute(
            "INSERT INTO entity (id, name, entity_type, properties, aliases, name_embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                eid, name, entity_type,
                json.dumps(properties or {}, ensure_ascii=False),
                json.dumps(aliases, ensure_ascii=False),
                _encode_embedding(name_embedding),
                _now(),
            ),
        )
        conn.commit()
        return eid
    except sqlite3.IntegrityError:
        # UNIQUE(name, entity_type) 冲突 — 并发 re-ingest 同实体。复用既有行, 不建孤儿。
        # 回滚未提交的 INSERT(防事务脏状态影响后续查询)。
        conn.rollback()
        # 先 find_entity_exact(与 resolver step1 同语义: case-insensitive name+alias),
        # 命中且 type 一致 → 复用; 否则按 (name, entity_type) 精确查(约束保证存在)。
        hit = find_entity_exact(name)
        if hit is not None and hit["entity_type"] == entity_type:
            return hit["id"]
        same_type = find_entities_by_name(name, entity_type)
        if same_type:
            return same_type[0]["id"]
        raise  # 不应发生: IntegrityError 必有同 (name, entity_type) 行; 重抛暴露不一致


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


# ADR-2③: name_embedding JSON 双认过渡期。
# 新结构: {"v":[...], "model":"...", "dim":N}; 老结构: 裸 list / '[]' / NULL。
# 写一律新结构(写入端单一源); 读时双认(_decode_embedding 归一到 list[float])。
# 模型升级维度变 → _cosine_topk 读时检测 len 不匹配惰性 re-embed 升级(过渡期)。
_EMB_MODEL = "qwen3-embedding-4b"  # ADR-13 默认 provider 模型; 与 embedding.default_providers 对齐


def _encode_embedding(vec: list[float] | None, model: str | None = None) -> str:
    """写时一律新结构 {"v":[...],"model":"...","dim":N}; 空向量 → '[]'(空标记, 不填 model)。"""
    if not vec:
        return "[]"
    return json.dumps(
        {"v": list(vec), "model": model or _EMB_MODEL, "dim": len(vec)},
        ensure_ascii=False,
    )


def _decode_embedding(raw: Any) -> list[float]:
    """读时双认: 新结构 {"v":[...],...} → v; 老结构 裸 list / '[]' / NULL → []。

    归一到 list[float](consumer 一律拿 list)。model/dim 信息留在 JSON 里供
    全库 re-embed 后迁移判定; 维度升级路径在 _cosine_topk 按 len 惰性 re-embed。
    """
    if raw is None:
        return []
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(doc, dict):  # 新结构
        return list(doc.get("v") or [])
    if isinstance(doc, list):  # 老结构裸 list
        return list(doc)
    return []


def _decode_entity(row: Any) -> dict[str, Any]:
    _r = dict(row)  # sqlite3.Row → dict (防御老 row 无键)
    return {
        "id": _r["id"],
        "name": _r["name"],
        "entity_type": _r["entity_type"],
        "properties": json.loads(_r["properties"]) if _r["properties"] else {},
        "aliases": json.loads(_r.get("aliases") or "[]"),
        "name_embedding": _decode_embedding(_r.get("name_embedding")),
        "created_at": _r["created_at"],
    }


def find_entity_exact(name: str) -> dict[str, Any] | None:
    """大小写不敏感精确匹配 (合并廉价闸专用, ADR-D7)。

    命中当 entity.name.lower() == name.lower() 或 name.lower() 在 aliases(大小写
    不敏感)中。**区别于** find_entities_by_name(大小写敏感、不查 alias、返回 list)。
    rows 量小 → Python 侧比对。命中返回第一个 _decode_entity, 否则 None。
    """
    conn = db.get_conn()
    target = name.lower()
    for row in conn.execute("SELECT * FROM entity ORDER BY created_at").fetchall():
        ent = _decode_entity(row)
        if ent["name"].lower() == target:
            return ent
        if target in [a.lower() for a in ent.get("aliases") or []]:
            return ent
    return None


def backfill_entity_embedding(entity_id: str, name_embedding: list[float] | None) -> int:
    """幂等回填 entity.name_embedding (供 resolver 合并时填空, ADR-D1 must-fix)。

    空向量不回填(离线 emb=[] 不覆盖既有向量)。只填"空"行 — 空同时认 ``NULL``
    (老库 ALTER ADD name_embedding 无 DEFAULT → 迁移行 = NULL)和 ``'[]'``(离线
    INSERT 行 / 新结构空标记)。两者都得认: 只写 ``'[]'`` 会漏老库 NULL 行。
    写入一律新结构 ``{"v":[...],"model":"...","dim":N}`` (ADR-2③); 老库裸 list 行
    不被覆盖(非空, 留给 _cosine_topk 惰性 re-embed 升级维度)。
    Returns 影响行数(0 = 空向量 / 行已非空 / entity 不存在)。
    """
    if not name_embedding:
        return 0
    conn = db.get_conn()
    cur = conn.execute(
        "UPDATE entity SET name_embedding = ? WHERE id = ? "
        "AND (name_embedding IS NULL OR name_embedding = '[]')",
        (_encode_embedding(name_embedding), entity_id),
    )
    conn.commit()
    return cur.rowcount


def add_aliases(entity_id: str, new_aliases: list[str]) -> None:
    """并入别名到 entity.aliases (去重保序, 供 resolver 合并用, ADR-D7)。"""
    conn = db.get_conn()
    row = conn.execute("SELECT aliases FROM entity WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return
    existing = json.loads(row["aliases"]) if row["aliases"] else []
    merged: list[str] = list(existing)
    for a in new_aliases:
        if a not in merged:
            merged.append(a)
    conn.execute(
        "UPDATE entity SET aliases = ? WHERE id = ?",
        (json.dumps(merged, ensure_ascii=False), entity_id),
    )
    conn.commit()


def set_aliases(entity_id: str, aliases: list[str]) -> None:
    """全量替换 entity.aliases (ADR-2② GC 用: resolver 合并 survivor 时清理无效/重复别名)。

    保序去重(None → []);entity 不存在 → no-op。区别 add_aliases(并入增量)。
    """
    conn = db.get_conn()
    clean: list[str] = []
    for a in (aliases or []):
        if a and a not in clean:
            clean.append(a)
    conn.execute(
        "UPDATE entity SET aliases = ? WHERE id = ?",
        (json.dumps(clean, ensure_ascii=False), entity_id),
    )
    conn.commit()


def remove_aliases(entity_id: str, to_remove: list[str]) -> None:
    """从 entity.aliases 移除给定别名 (ADR-2② GC: 清理合并后旧实体的残留别名)。

    大小写敏感移除; entity 不存在 → no-op。
    """
    conn = db.get_conn()
    row = conn.execute("SELECT aliases FROM entity WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return
    existing = json.loads(row["aliases"]) if row["aliases"] else []
    rm = set(to_remove or [])
    kept = [a for a in existing if a not in rm]
    conn.execute(
        "UPDATE entity SET aliases = ? WHERE id = ?",
        (json.dumps(kept, ensure_ascii=False), entity_id),
    )
    conn.commit()

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
    if valid_from is None:
        valid_from = _now()
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

def update_fact_status(fact_id: str, status: str, supersedes_id: str | None = None, valid_to: str | None = None) -> None:
    """Lifecycle transition (active→deprecated/superseded). No-op if missing."""
    conn = db.get_conn()
    conn.execute(
        "UPDATE fact SET status = ?, supersedes_id = COALESCE(?, supersedes_id), valid_to = COALESCE(?, valid_to) WHERE id = ?",
        (status, supersedes_id, valid_to, fact_id),
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

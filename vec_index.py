"""mem-service vec_index — sqlite-vec 向量索引面 (perf/vec-index 批).

KG 索引面依赖向量表 (向量检索为语义匹配一等公民): 两张 vec0 虚拟表

    vec_entity(entity_id TEXT PRIMARY KEY, name_embedding float[DIM])
    vec_fact(fact_id  TEXT PRIMARY KEY, value_embedding float[DIM])

    — cosine 距离度量 (distance_metric=cosine, 语义等价旧余弦排序);
      DIM 缺省 2560 (本地 qwen3-embedding 实测), env ``MEM_VEC_DIM`` 可调
      (测试用小维度); 表在 db.init 时建。

**硬依赖无降级 (用户裁决 2026-08-26: 不设 fallback — 降级路径严重干扰排查)**:
sqlite-vec 载入失败 → :class:`VecIndexError` 响亮报错, 含可行动诊断
(pip install 命令 / 备选 vec0.so 路径 / load_extension 错误原文)。全链路
只有一条首要路径: vec0 索引 — 排障时行为可预期、无隐藏分支。

写路径同步: store.put_entity / backfill_entity_embedding / upsert_entity_embedding
/ put_fact 写入时 upsert vec 行; update_fact_status 非活跃 / bootstrap.prune
软删时删 vec 行。vec=[] / 维度不匹配是**数据条件** (embedding 离线), 无有效
向量可索引 → 跳过同步 (非扩展故障); vec 表 SQL 失败是**真故障** → 传播
(不静默)。查询面另有 active 过滤兜底 (stale 行漏删不进结果)。
"""

from __future__ import annotations

import json
import os
import struct

# 维度: 读一次 env (表 DDL 用); 测试可 monkeypatch 本属性后 db.init 重建。
VEC_DIM = int(os.environ.get("MEM_VEC_DIM", "2560"))

# 备选 vec0.so (诊断信息用; openclaw 生产同版本扩展)。
_ALT_VEC0_SO = "/home/yy/tools/openclaw/node_modules/sqlite-vec-linux-x64/vec0.so"


def _pack_f32(vec: list[float]) -> memoryview:
    """vec → little-endian float32 blob (sqlite-vec 原生二进制输入)。

    perf: 替代 json.dumps 传参 (2560 维 ~1.5ms → ~0.05ms, 且 C 端免 JSON
    逐数解析)。vec0 内部本就 float32 存储 — 实测 JSON/blob 输入 distance
    逐位一致, 序列化路径无语义差。memoryview 防 sqlite3 参数序列展开 quirk。
    """
    return memoryview(struct.pack(f"<{len(vec)}f", *vec))

_loaded = False  # sqlite-vec 已在当前连接载入且表已建
# D1 orphan 语义承接: 有实体未入索引 (离线创建 emb=[] / 老结构 / 维度不匹配)
# → 标记 pending, resolver step2 前一次性 heal (re-embed+落盘+入索引)。
_entity_heal_pending = False


class VecIndexError(RuntimeError):
    """sqlite-vec 硬依赖不可用 (用户裁决: 无降级, 响亮报错含可行动诊断)。"""


def _diagnostics(what: str, exc: BaseException) -> str:
    return (
        f"[vec_index] {what}: {exc.__class__.__name__}: {exc}\n"
        f"  sqlite-vec 是硬依赖 — 无降级路径, 向量索引/resolver step2/recall "
        f"--vector 均不可用。修复:\n"
        f"  1) pip install --break-system-packages sqlite-vec==0.1.9\n"
        f"  2) 或经 sqlite3 load_extension 加载备选: {_ALT_VEC0_SO}\n"
        f"  原始错误: {exc!r}"
    )


# ── 载入与建表 (db.init 调; 失败 raise) ───────────────────────────────

def init_conn(conn) -> bool:
    """sqlite_vec.load + 建两张 vec0 表。**硬依赖**: 任一步失败 raise
    :class:`VecIndexError` (含可行动诊断), 不降级不静默。"""
    global _loaded
    try:
        import sqlite_vec
    except ImportError as exc:
        raise VecIndexError(_diagnostics("import sqlite_vec 失败", exc)) from exc
    try:
        sqlite_vec.load(conn)
    except Exception as exc:
        raise VecIndexError(_diagnostics("sqlite_vec.load(conn) 失败", exc)) from exc
    try:
        ensure_tables(conn)
    except Exception as exc:
        raise VecIndexError(_diagnostics("vec0 建表失败", exc)) from exc
    _loaded = True
    return True


def ensure_tables(conn) -> None:
    d = VEC_DIM
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_entity USING vec0("
        f"entity_id TEXT PRIMARY KEY, name_embedding float[{d}] distance_metric=cosine)")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_fact USING vec0("
        f"fact_id TEXT PRIMARY KEY, value_embedding float[{d}] distance_metric=cosine)")


def available() -> bool:
    """sqlite-vec 索引面可用 (init 已成功)。"""
    return _loaded


def _require_loaded() -> None:
    if not _loaded:
        raise VecIndexError(
            "[vec_index] vec0 索引未加载 — db.init 未调用或当时已 raise。"
            "sqlite-vec 是硬依赖, 无降级路径 (pip install --break-system-packages "
            "sqlite-vec==0.1.9)")


# ── 写路径同步 ───────────────────────────────────────────────────────

def sync_entity(entity_id: str, vec: list[float] | None) -> None:
    """upsert vec_entity 行。vec 空/维度不匹配 = 覆盖缺口 → 标记 heal
    pending (D1 orphan: resolver step2 前一次性补); vec 表 SQL 失败 = 真故障
    → 传播 (响亮报错, 无隐藏分支)。"""
    global _entity_heal_pending
    _require_loaded()
    if not vec or len(vec) != VEC_DIM:
        _entity_heal_pending = True
        return
    conn = _conn()
    conn.execute("DELETE FROM vec_entity WHERE entity_id = ?", (entity_id,))
    conn.execute(
        "INSERT INTO vec_entity(entity_id, name_embedding) VALUES (?, ?)",
        (entity_id, _pack_f32(vec)))


def delete_entity(entity_id: str) -> None:
    _require_loaded()
    _conn().execute("DELETE FROM vec_entity WHERE entity_id = ?", (entity_id,))


def sync_fact(fact_id: str, vec: list[float] | None) -> None:
    """upsert vec_fact 行 (数据条件跳过语义同 sync_entity)。"""
    _require_loaded()
    if not vec or len(vec) != VEC_DIM:
        return
    conn = _conn()
    conn.execute("DELETE FROM vec_fact WHERE fact_id = ?", (fact_id,))
    conn.execute(
        "INSERT INTO vec_fact(fact_id, value_embedding) VALUES (?, ?)",
        (fact_id, _pack_f32(vec)))


def delete_fact(fact_id: str) -> None:
    _require_loaded()
    _conn().execute("DELETE FROM vec_fact WHERE fact_id = ?", (fact_id,))


def _conn():
    import db
    return db.get_conn()


# ── 查询: vec0 ANN (唯一路径) ─────────────────────────────────────────

def entity_topk(vec: list[float], k: int) -> list[tuple[str, float]]:
    """top-k (entity_id, cosine_sim)。语义等价旧 _cosine_topk 排序。"""
    _require_loaded()
    if not vec:
        return []
    return _ann_topk("vec_entity", "entity_id", "name_embedding", vec, k)


def fact_topk(vec: list[float], k: int) -> list[tuple[str, float]]:
    """top-k (fact_id, cosine_sim) — 仅索引写入过的 fact (active 过滤由调用方)。"""
    _require_loaded()
    if not vec:
        return []
    return _ann_topk("vec_fact", "fact_id", "value_embedding", vec, k)


def _ann_topk(table: str, id_col: str, vec_col: str,
              vec: list[float], k: int) -> list[tuple[str, float]]:
    if len(vec) != VEC_DIM:
        return []  # 查询向量维度不匹配 (embedding 档不同) → 无结果, 非故障
    conn = _conn()
    rows = conn.execute(
        f"SELECT {id_col}, distance FROM {table} "
        f"WHERE {vec_col} MATCH ? ORDER BY distance LIMIT ?",
        (_pack_f32(vec), k)).fetchall()
    # cosine distance = 1 - cosine_sim (vec0 distance_metric=cosine)。
    return [(r[0], 1.0 - r[1]) for r in rows]


# ── 存量回填 (幂等可重跑) ─────────────────────────────────────────────

def backfill_all() -> dict[str, int]:
    """一次性回填: entity.name_embedding JSON → vec_entity; fact.value embed
    → vec_fact (仅 active)。幂等 (vec0 PK + delete-then-insert)。

    entity heal (承接旧 _cosine_topk 惰性 re-embed 语义): 存储向量为空/裸 list
    老结构/维度不匹配的行 → 用当前模型 re-embed(name) → upsert 落盘新结构
    (B2: 无条件覆盖) + 入索引。fact 侧仅回填 active (软删行不入)。
    """
    import embedding
    import store
    _require_loaded()
    conn = _conn()
    out = {"entities": 0, "facts": 0, "skipped": 0}
    for r in conn.execute("SELECT id, name, name_embedding FROM entity"):
        vec = _decode_stored_vec(r[1])
        if vec and len(vec) == VEC_DIM:
            sync_entity(r[0], vec)
            out["entities"] += 1
            continue
        # heal: 空/老结构/维度不匹配 → 当前模型 re-embed + 落盘新结构。
        try:
            re_vec = embedding.embed(r["name"])
        except Exception:
            re_vec = []
        if re_vec and len(re_vec) == VEC_DIM:
            store.upsert_entity_embedding(r[0], re_vec)
            sync_entity(r[0], re_vec)
            out["entities"] += 1
        else:
            out["skipped"] += 1
    for r in conn.execute(
            "SELECT id, value FROM fact "
            "WHERE status='active' AND value IS NOT NULL AND value != ''"):
        try:
            vec = embedding.embed(r[1])
        except Exception:
            vec = []
        if vec and len(vec) == VEC_DIM:
            sync_fact(r[0], vec)
            out["facts"] += 1
        else:
            out["skipped"] += 1
    return out


def _decode_stored_vec(raw: Any) -> list[float] | None:
    """entity.name_embedding 兼容解码: 新结构 {"v":[…]} / 老库裸 list / NULL。"""
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(obj, dict):
        obj = obj.get("v")
    if isinstance(obj, list) and obj:
        return obj
    return None


def heal_entities_if_pending(embedding_providers=None) -> int:
    """D1 orphan 语义承接: 存在覆盖缺口 (sync_entity 标记 pending) 时一次性
    heal — 空/老结构/维度不匹配实体行 re-embed(name) (embed_batch 批) →
    upsert 落盘新结构 (B2) → 入索引, 清标记。返回 heal 行数; 仍离线的行留给
    下次写触发 (不逐查询重试)。"""
    global _entity_heal_pending
    if not _entity_heal_pending:
        return 0
    import embedding as embedding_mod
    import store
    conn = _conn()
    need: list[tuple[str, str]] = []
    for r in conn.execute("SELECT id, name, name_embedding FROM entity").fetchall():
        vec = _decode_stored_vec(r["name_embedding"])
        if not (vec and len(vec) == VEC_DIM):
            need.append((r["id"], r["name"]))
    healed = 0
    if need:
        vecs = embedding_mod.embed_batch([n for _, n in need],
                                         providers=embedding_providers)
        for (eid, _name), v in zip(need, vecs):
            if v and len(v) == VEC_DIM:
                store.upsert_entity_embedding(eid, v)
                conn.execute("DELETE FROM vec_entity WHERE entity_id = ?", (eid,))
                conn.execute(
                    "INSERT INTO vec_entity(entity_id, name_embedding) "
                    "VALUES (?, ?)", (eid, _pack_f32(v)))
                healed += 1
    _entity_heal_pending = False
    return healed

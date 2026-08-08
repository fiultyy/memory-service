"""mem-service resolver — D3 两步实体合并(跨 ingest 同实体异写消解, ADR-D3).

两步共指消解(廉价闸 → 向量 top-k + LLM 判定):

Step 1 — 廉价闸(无网络/无 LLM): ``store.find_entity_exact`` 大小写不敏感 name+alias
精确命中 → 并入新别名, 返回既有 entity id。
Step 2 — 向量召回 + LLM 判定: ``embedding.embed`` 算 name 向量 → ``_cosine_topk``
召回余弦最近邻 → 第一个 provider ``dedupe_entity`` 裁判同义异写 → 命中则并入别名返回。
Step 3 — 新建: ``store.put_entity``(embedding 离线算得 [] → 显式传 [] 入库)。

降级红线: embedding 离线(返回 [])或 providers 空([]) → 跳过 step2 → 直接新建,
绝不 crash。``put_entity`` 是纯存储原语, embedding 计算由本模块算一次显式传入
(skeptic 修正: auto-embed 会污染 embeddings.db + 防火墙 block 60s/entity)。
"""

from __future__ import annotations

import math

import db
import store

_TOP_K = 5


def resolve_entity(name, entity_type, aliases=None, providers=None,
                   embedding_providers=None):
    """Resolve a name to an entity id, merging on duplicate (ADR-D3 two-step).

    Args:
        name: surface form (empty → return None).
        entity_type: declared type for a brand-new entity.
        aliases: extra spellings to fold into the resolved entity.
        providers: LLM providers for step-2 dedupe judgement (empty/None → skip).
        embedding_providers: embedding providers for step-2 vectors (None →
            ``embedding.default_providers()``; offline → [] → skip step 2).

    Returns:
        The entity id (str), or None only on an empty name.
    """
    if not name:
        return None

    # emb 算一次, 供 step1/step2 回填既有 entity 的 name_embedding(D1)。惰性 import
    # 推迟 cache 连接; embedding != LLM — step1 廉价闸指"无 LLM 判定", embedding 计算仍做。
    import embedding
    emb = embedding.embed(name, providers=embedding_providers)

    # Step 1 — 廉价闸: 大小写不敏感 name + alias 精确命中(无 LLM)。
    hit = store.find_entity_exact(name)
    if hit is not None:
        # D7: 把 surface form 记入别名(与 step2 add_aliases(dup_id, [name]+aliases)
        # 对称), 让 T1 能断言 step1 命中后 aliases 含异写。
        store.add_aliases(hit["id"], [name] + (list(aliases) if aliases else []))
        # D1: 回填既有 entity 的 name_embedding(幂等只填空; emb 离线为 [] 则不回填)。
        store.backfill_entity_embedding(hit["id"], emb)
        return hit["id"]

    # Step 2 — 向量召回 top-k + LLM 判定。
    if emb:
        # D1 orphan fix: 把 emb 透传 _cosine_topk — 它会用同一组 provider 惰性
        # re-embed 既有 emb=[] 的实体(离线 Phase1 插入的), 让它们成为正常候选并
        # 回填向量。否则 emb=[] 实体永远进不了 cosine, 同实体异写(如 JavaScript/JS)
        # 在 step1 廉价闸(case-insensitive name) 不命中时 → 孤儿新建。
        candidates = _cosine_topk(emb, _TOP_K, embedding_providers=embedding_providers)
        if candidates and providers:
            try:
                dup = providers[0].dedupe_entity(name, entity_type, candidates)
            except Exception:
                # provider can't/won't dedupe (stub/test-fake/offline) → degrade
                dup = None
            dup_id = (dup or {}).get("duplicate_id")
            # Guard: LLM may hallucinate an id absent from candidates (e.g.
            # copy a few-shot example id). Reject it — a phantom id would sail
            # past add_aliases (silent no-op) and crash put_fact on FK violation.
            if dup_id and dup_id in {c["id"] for c in candidates}:
                store.add_aliases(dup_id, [name] + (list(aliases) if aliases else []))
                # D1: 把已为新 name 算好的 emb 回填到既有 entity(幂等只填空)。
                store.backfill_entity_embedding(dup_id, emb)
                return dup_id

    # Step 3 — 新建 (emb 可能为 []; put_entity 不做网络 I/O)。
    return store.put_entity(name, entity_type, aliases=aliases, name_embedding=emb)


def _cosine_topk(emb, k=_TOP_K, embedding_providers=None):
    """Scan all entities, return top-k by cosine similarity to ``emb``.

    Skips entities with a mismatched ``name_embedding`` dimension (different
    embedding model). Returns ``[{"id","name","type","score"}]`` sorted desc.

    D1 orphan fix: 既有 ``name_embedding`` 为空([]/NULL)的实体 — 离线 Phase1
    (embedding_providers=[] → emb=[]) 插入的 — 会用 ``embedding_providers`` 惰性
    re-embed(同一组 provider, 维度一致) 并回填, 让它们成为正常候选。否则它们永远
    进不了 cosine, 同实体异写(step1 廉价闸 case-insensitive 不命中时)→ 孤儿新建。
    re-embed 失败(provider 离线/无 key) → 跳过(不崩, 不并入)。
    ponytail: 惰性 re-embed 只对 emb=[] 行生效, 已有 emb 的行零成本(O(n) 扫描里一次判空)。
    """
    import embedding
    n_a = math.sqrt(sum(x * x for x in emb)) or 1e-12
    scored = []
    conn = db.get_conn()
    for row in conn.execute("SELECT * FROM entity ORDER BY created_at").fetchall():
        ent = store._decode_entity(row)
        vec = ent.get("name_embedding") or []
        if not vec:
            # D1: emb=[] 的既有实体 — 用查询的 provider 惰性 re-embed, 成功则回填。
            if not embedding_providers:
                continue  # 查询自身也是离线 → 无 provider 可算, 跳过
            try:
                re_vec = embedding.embed(ent["name"], providers=embedding_providers)
            except Exception:
                re_vec = []
            if not re_vec or len(re_vec) != len(emb):
                continue  # re-embed 仍离线 / 维度不一致 → 跳过(不并入, 不崩)
            store.backfill_entity_embedding(ent["id"], re_vec)  # 幂等回填
            vec = re_vec
        if len(vec) != len(emb):
            continue
        n_b = math.sqrt(sum(x * x for x in vec)) or 1e-12
        dot = sum(a * b for a, b in zip(emb, vec))
        scored.append({
            "id": ent["id"], "name": ent["name"],
            "type": ent["entity_type"], "score": dot / (n_a * n_b),
        })
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:k]


__all__ = ["resolve_entity"]

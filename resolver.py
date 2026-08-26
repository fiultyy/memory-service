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
        # D7: 把 surface form 记入别名(与 step2 对称), 让 T1 能断言含异写。
        # ADR-2② + perf: 等于 survivor 规范名 (case-sensitive) 的 surface 不加
        # — 旧路径加了再被 _gc_aliases 清掉 (add/remove 乒乓, 每次全量失效
        # exact 字典/代缓存; 预过滤净效果等价零写)。
        _surfaces = [s for s in [name] + (list(aliases) if aliases else [])
                     if s != hit["name"]]
        if _surfaces:
            store.add_aliases(hit["id"], _surfaces)
        _gc_aliases(hit["id"], survivor_name=hit["name"])
        # D1: 回填既有 entity 的 name_embedding(幂等只填空; emb 离线为 [] 则不回填)。
        store.backfill_entity_embedding(hit["id"], emb)
        return hit["id"]

    # Step 2 — 向量召回 top-k + LLM 判定。
    if emb:
        # D1 orphan fix (perf/vec-index 语义承接): 有实体未入索引 (离线
        # Phase1 插入 emb=[] / 老结构 / 维度不匹配) 时, heal_entities_if_pending
        # 一次性 re-embed+落盘+入索引 — 否则 emb=[] 实体永远进不了 ANN,
        # 同实体异写(JavaScript/JS)在 step1 不命中时 → 孤儿新建。无缺口时零成本。
        import vec_index
        vec_index.heal_entities_if_pending(embedding_providers)
        # perf: providers 空 (init/占位主径) 时 LLM 判定不可达 → ANN 候选
        # 查询是纯浪费 (~千次/全量 init, vec0 逐查 ~8ms); 移入 providers 门内
        # (merge 路径唯一消费者)。heal 独立保留 (索引完整性, 与 LLM 无关)。
        if providers:
            candidates = _cosine_topk(emb, _TOP_K, embedding_providers=embedding_providers)
            if candidates:
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
                    survivor = store.get_entity(dup_id)
                    # ADR-2② + perf: 同 step1 预过滤 (等价旧 add+GC 净效果, 免乒乓)。
                    _sname = survivor["name"] if survivor else None
                    _surfaces = [s for s in [name] + (list(aliases) if aliases else [])
                                 if s != _sname]
                    if _surfaces:
                        store.add_aliases(dup_id, _surfaces)
                    _gc_aliases(dup_id, survivor_name=_sname)
                    # D1: 把已为新 name 算好的 emb 回填到既有 entity(幂等只填空)。
                    store.backfill_entity_embedding(dup_id, emb)
                    return dup_id

    # Step 3 — 新建 (emb 可能为 []; put_entity 不做网络 I/O)。
    return store.put_entity(name, entity_type, aliases=aliases, name_embedding=emb)


def _cosine_topk(emb, k=_TOP_K, embedding_providers=None):
    """top-k 近邻 by cosine (perf/vec-index: vec0 ANN, 唯一路径无降级)。

    旧实现逐实体拉行+JSON 解码+Python 余弦 (D1 惰性 re-embed 升级空/维度
    不匹配行)。新实现查 vec_entity 索引 — 写路径同步 (put_entity/
    backfill/upsert) 保证新写行即时入索引; 存量空/老结构行跑一次
    ``mem vec-backfill`` 升级入索引 (惰性 re-embed 语义由回填命令承接)。
    返回 ``[{"id","name","type","score"}]`` 按 score 降序 (协议不变)。
    """
    import vec_index
    hits = vec_index.entity_topk(list(emb), k)
    if not hits:
        return []
    conn = db.get_conn()
    out = []
    for eid, sim in hits:
        row = conn.execute(
            "SELECT id, name, entity_type FROM entity WHERE id = ?",
            (eid,)).fetchone()
        if row is None:
            continue  # vec 行残留而主表无 (理论不至, 双保险)
        out.append({"id": row["id"], "name": row["name"],
                    "type": row["entity_type"], "score": sim})
    return out


def _gc_aliases(entity_id: str, survivor_name: str | None = None) -> None:
    """GC entity.aliases (ADR-2②): 去重 + 清空 + survivor 规范名精确冗余移除。

    resolver 合并 survivor 时调用。规则:
    - 去重保序(add_aliases 已做, 这里 defensive);
    - 别名**精确等于**(case-sensitive) survivor.name → 移除(name 已在 .name,
      完全相同串是纯噪声)。注意: 仅大小写不同的别名(如 'react' vs 'React')是有效
      异写, 必须保留(case-insensitive 命中时 surface form 异写正是要记的)。
    """
    ent = store.get_entity(entity_id)
    if ent is None:
        return
    aliases = ent.get("aliases") or []
    clean: list[str] = []
    for a in aliases:
        if not a or a in clean:
            continue
        if survivor_name is not None and a == survivor_name:
            continue  # 精确冗余: name 已在 .name; 仅大小写不同的串保留
        clean.append(a)
    if clean != aliases:
        store.set_aliases(entity_id, clean)


__all__ = ["resolve_entity", "resolve_entities_batch"]


def resolve_entities_batch(names, entity_types=None, aliases_map=None,
                           providers=None, embedding_providers=None):
    """批式实体消解 (perf/vec-index): 一次 embed 批 (本地模型一次 POST) +
    逐名走**同 resolve_entity 三步协议** (step1 字典廉价闸 / step2 vec0 ANN /
    step3 新建)。

    实现路径: ``embedding.embed_batch(names)`` 先把全部 name 向量算好并写
    L1/L2 缓存 → 逐名 ``resolve_entity`` 复用缓存向量 (零额外 HTTP) — 单实体
    协议语义零重复代码 (aliases 合并/GC/backfill 全保留)。

    Args:
        names: 实体名列表。
        entity_types: 每名类型 (None → 全 "concept"; 单值 → 广播)。
        aliases_map: ``{name: [alias, ...]}`` 透传单条同参语义 (可选)。
        providers: LLM dedupe judge (None → 单条同参语义)。
        embedding_providers: 向量 provider 覆盖。

    Returns:
        ``{"name": entity_id | None}`` — None 仅当名为空 (协议同单条)。
    """
    import embedding
    names = list(names or [])
    if not names:
        return {}
    if entity_types is None:
        types = ["concept"] * len(names)
    elif isinstance(entity_types, str):
        types = [entity_types] * len(names)
    else:
        types = [entity_types[i] if i < len(entity_types) else "concept"
                 for i in range(len(names))]
    aliases_map = aliases_map or {}
    # 一次批 embed 预热 L1/L2 (miss 子集打包单次 POST); 随后单条 resolve 复用。
    embedding.embed_batch(names, providers=embedding_providers)
    out = {}
    for name, etype in zip(names, types):
        out[name] = resolve_entity(
            name, etype, aliases=aliases_map.get(name), providers=providers,
            embedding_providers=embedding_providers)
    return out

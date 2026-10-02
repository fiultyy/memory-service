"""H4 tag dreaming: 语义 tag 初铸 (embedding 聚类 + zhipu 命名) + 新 atom 挂载.

spec: docs/specs/graph-reform-v2-ingest-tags.md §一/§四/§五-H4。
初铸不调 laya (F 验边占用容器) — 挂载 w 用确定性信号 (簇成员=1.0,
新 atom 挂载=余弦); laya 挂载审计/竞争裁决是独立 refinement pass
(audit_mounts + mount_new_atoms(use_laya=True), 挂账#1 已通电)。

算法对任务卡原文的一处实测修正 (参数不变 cos_lo=0.60/topk=8):
  任务卡: kNN 图 → 连通分量。实测本库语料同域致密, 连通分量退化为单一
  巨簇 — cos≥0.60 时 3905/4361 atom 同分量, mutual-kNN 亦 3536。改用
  proto 阶段已验证形态 (temp/proto_chunk.py, spec §五-H4 原文即引它):
  同一张 kNN 加权图上跑 louvain (networkx, seed=42) → 实测 353 社区 /
  最大 420 / ≥4 atom 的 39 簇覆盖 4007 atom。

层级涌现 (裁决#1 递归层级, 硬帽 ≤4): 商图 (簇间 kNN 跨边计数 >2 连边,
权=计数) 重复 louvain → level+1 tag, 子 tag 回挂 parent_id; 新层簇数
不再缩减 (≥ 本层节点数) 即停。

幂等: 同名同 level 且成员重叠过半 → 同簇重跑复用既有 tag; 撞名 (不同
簇) 加后缀 ·2 重试一次; 挂载 INSERT OR IGNORE。LLM 换名重跑产生的近重
复 tag 由 audit_mounts 收口。ponytail: 全量重算 O(N²) 点积, atom 过万换
ANN 索引/增量聚类。
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓根平铺模块

import numpy as np

import db
import embedding
import laya_client
from llm_provider import ZhipuAnthropicProvider
from src.distill import (  # laya 评分口径复用 (H2/挂账#1)
    CRIT_EDGE,
    _LAYA_SHARD,
    _laya_one,
    _prob,
    parse_llm_json,  # 括号深度扫描兜底复用 (H2)
)

_AUDIT_DROP = 0.30  # laya 低分卸载线 (边判 strong≥0.5 / rubber-stamp 带≥0.3 既有校准)
_AUDIT_DEL_CAP = 200  # 单轮卸载帽 (对抗审查 major: laya 评分回归一夜清空挂载层的爆炸半径护栏 — 超出留待下轮, 自然限速+观察窗)


def _rel_q(tag_name: str, tag_desc: str, atom_text: str) -> dict:
    """tag↔atom 主题相关度 laya score 问 (audit/mount 共用, distill edg_ 同型)。"""
    return {"type": "score",
            "instructions": f"How related are the memory items "
                            f"[tag: {tag_name} — {tag_desc}] and [{atom_text[:130]}] in topic?",
            "criteria": CRIT_EDGE}


def _sub_q(parent_name: str, parent_desc: str, name: str, desc: str) -> dict:
    """新 tag 是否为父支点主题的子题 laya score 问 (grow_tag_tree 可插入闸)。"""
    return {"type": "score",
            "instructions": f"Is the topic [{name} — {desc}] a specific subtopic "
                            f"of [{parent_name} — {parent_desc}]?",
            "criteria": CRIT_EDGE}

SYS_NAME = (
    "你是知识图谱的语义标签铸造器。下面给你若干语义簇, 每簇带编号和成员样本句。"
    "为每簇起一个概括成员共同主题的标签名(中文, 不超过8字, 无标点)和一句描述(≤30字)。\n"
    '只输出 JSON 数组: [{"id":簇编号,"name":"标签名","description":"一句描述"}], 不要其它文字。'
)

_NAME_SAMPLES = 8     # 每簇命名样本句数
_LEVEL_CAP = 4        # 层级硬帽 (裁决#1)
_QUOTIENT_EDGE_GT = 2  # 簇间 kNN 跨边计数 > 2 才连商图边
_MOUNT_COS = 0.55     # 新 atom 挂载下界
_EMBED_DIM_MIN = 100  # 短于此视为坏向量 (distill 同判据)

SYS_PLACE = (
    "你是知识图谱语义标签树的大纲编辑。下面给出既有语义标签树 (id/层级/父节点/"
    "名称/描述) 和若干新候选标签 (带编号和成员样本句)。像文章大纲一样按具体语义"
    "为每个候选选插入支点: 它是既有哪个标签的子主题就挂谁下面 (parent 给该标签"
    "id); 若它是全新顶层主题则 parent 给 null; 若与既有某标签同义 (不该新建) 则"
    "same 给该标签 id。\n只输出 JSON 数组: "
    '[{"id":候选编号,"parent":<标签id或null>,"same":<标签id或null>}], 不要其它文字。'
)
_PLACE_SHARD = 40  # 候选分片 (320 候选×提示一次调会截断 → 全量解析失败, 实测教训)

_Zhipu: ZhipuAnthropicProvider | None = None


def _get_zhipu() -> ZhipuAnthropicProvider:
    global _Zhipu
    if _Zhipu is None:
        _Zhipu = ZhipuAnthropicProvider(timeout=120.0)  # zhipu 120s 硬帽 (H2 口径)
    return _Zhipu


def _name_clusters(samples: dict[int, list[str]]) -> dict[int, dict]:
    """zhipu 一批全簇命名; 不可解析/网络败 → 对缺簇重试一次; 仍缺 → {} (留待下轮)。

    samples: 簇号 → 样本列表 (L1=成员句, L2+=子标签 "name: description")。"""
    got: dict[int, dict] = {}
    todo = dict(samples)
    for _attempt in range(2):
        if not todo:
            break
        lines: list[str] = []
        for cid, items in sorted(todo.items()):
            lines.append(f"簇{cid} (成员样本):")
            lines.extend(f"- {s}" for s in items)
        try:
            raw = _get_zhipu().chat(
                SYS_NAME, [{"role": "user", "content": "\n".join(lines)}],
                max_tokens=8000)
        except Exception:
            time.sleep(2)
            continue
        for it in parse_llm_json(raw) or []:
            if not isinstance(it, dict):
                continue
            cid, name, desc = it.get("id"), it.get("name"), it.get("description")
            if cid in todo and isinstance(name, str) and name.strip() \
                    and isinstance(desc, str) and desc.strip():
                got[cid] = {"name": name.strip()[:8], "description": desc.strip()}
                del todo[cid]
        time.sleep(1)
    return got


def _knn_edges(Vn: np.ndarray, cos_lo: float, topk: int) -> list[tuple[int, int, float]]:
    """每 atom top-k 邻居 (cos≥cos_lo) 的无向边去重集 [(i, j, w)] (i<j, 索引序)。"""
    S = Vn @ Vn.T
    n = S.shape[0]
    edges: set[tuple[int, int, float]] = set()
    for i in range(n):
        for j in np.argsort(-S[i])[1:topk + 1]:
            if j != i and S[i, j] >= cos_lo:   # 重复向量时 top-k 含自身 → 自环防
                a, b = (i, int(j)) if i < j else (int(j), i)
                edges.add((a, b, float(S[a, b])))
    return sorted(edges)


def _louvain(n_nodes: int, edges: list[tuple[int, int, float]]) -> list[set[int]]:
    """加权图 → 社区列表。proto 阶段已验证形态 (temp/proto_chunk.py:
    networkx louvain, seed=42, weight 边权)。孤立节点自成社区。"""
    import networkx as nx
    g = nx.Graph()
    g.add_nodes_from(range(n_nodes))
    for i, j, w in edges:
        g.add_edge(i, j, weight=w)
    return list(nx.algorithms.community.louvain_communities(g, seed=42))


def _load_atoms(conn) -> tuple[list[int], list[str], np.ndarray | None]:
    """活 atom (needs_embed=0) 的 id/text/归一向量; 无有效向量 → Vn=None。"""
    rows = conn.execute(
        "SELECT id, text FROM atom WHERE valid_to IS NULL AND needs_embed=0 "
        "ORDER BY id").fetchall()
    vecs = embedding.embed_batch([r["text"] for r in rows]) if rows else []
    ids, texts, mats = [], [], []
    for r, v in zip(rows, vecs):
        if len(v) > _EMBED_DIM_MIN:
            ids.append(r["id"])
            texts.append(r["text"])
            mats.append(np.asarray(v, dtype=np.float32))
    if not mats:
        return ids, texts, None
    V = np.vstack(mats)
    norms = np.linalg.norm(V, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, texts, V / norms


def _find_or_mint_tag(conn, name: str, desc: str, level: int,
                      member_ids: set[int] | None,
                      parent_id: int | None = None) -> int | None:
    """撞名判定 (任务卡: 加后缀重试一次): 同名同 level 且 (成员重叠过半 或
    L2+ 无成员) → 同簇重跑复用; 异簇撞名 → 后缀 ·2 一次; 仍撞 → None 跳过。"""
    def _row(nm):
        return conn.execute("SELECT id, kind FROM tag WHERE name=? AND level=?",
                            (nm, level)).fetchone()

    row = _row(name)
    if row is not None:
        tid = row["id"]
        if row["kind"] != "semantic":
            pass  # factual 撞名 (如 repo:x) → 落到后缀 lane
        elif member_ids is None:
            return tid  # L2+: 同名同层视为重跑复用
        else:
            mounted = {r[0] for r in conn.execute(
                "SELECT atom_id FROM tag_mount WHERE tag_id=?", (tid,)).fetchall()}
            if mounted and len(mounted & member_ids) * 2 >= len(member_ids):
                return tid  # 成员重叠过半 → 同簇重跑
        name2 = name[:7] + "·2"
        if _row(name2) is None:
            return conn.execute(
                "INSERT INTO tag(name, kind, level, parent_id, description) "
                "VALUES(?, 'semantic', ?, ?, ?)",
                (name2, level, parent_id, desc)).lastrowid
        return None
    return conn.execute(
        "INSERT INTO tag(name, kind, level, parent_id, description) "
        "VALUES(?, 'semantic', ?, ?, ?)", (name, level, parent_id, desc)).lastrowid


def mint_semantic_tags(db_path, min_cluster: int = 4, cos_lo: float = 0.60,
                       topk: int = 8) -> dict:
    """语义 tag 初铸: 全量 atom 聚类 → zhipu 命名 → 单事务入 tag/tag_mount
    (+层级涌现)。全部 LLM/embed 调用在任何 DB 写之前完成。"""
    conn = db.init(db_path)
    res: dict = {
        "atoms_total": conn.execute(
            "SELECT COUNT(*) FROM atom WHERE valid_to IS NULL").fetchone()[0],
        "skipped_needs_embed": conn.execute(
            "SELECT COUNT(*) FROM atom WHERE valid_to IS NULL AND needs_embed=1"
        ).fetchone()[0],
    }
    ids, texts, Vn = _load_atoms(conn)
    res["atoms_usable"] = len(ids)
    res["clusters_kept"] = res["communities"] = res["clusters_dropped_small"] = 0
    res["sizes"] = []

    created: list[dict] = []   # 待写 tag (按层序): level/name/description/atoms/children
    n_unnamed = 0
    if Vn is not None and len(ids) >= min_cluster:
        edges = _knn_edges(Vn, cos_lo, topk)
        comms = _louvain(len(ids), edges)
        kept = [c for c in comms if len(c) >= min_cluster]
        res["communities"], res["clusters_kept"] = len(comms), len(kept)
        res["clusters_dropped_small"] = len(comms) - len(kept)
        res["sizes"] = sorted((len(c) for c in kept), reverse=True)
        res["orphan_atoms"] = len(ids) - sum(len(c) for c in kept)

        comp = np.full(len(ids), -1, dtype=int)
        for k, idxs in enumerate(kept):
            comp[list(idxs)] = k
        cross: Counter = Counter()  # L1 簇对 → kNN 跨边计数 (双向键, 查取免排序)
        for i, j, _w in edges:
            ci, cj = int(comp[i]), int(comp[j])
            if ci >= 0 and cj >= 0 and ci != cj:
                cross[(ci, cj)] += 1
                cross[(cj, ci)] += 1

        # 层级循环: L1 = 聚类社区; Lℓ+1 = 商图 (跨边计数>2) louvain
        nodes = [{"l1": {k}, "atoms": sorted(kept[k]), "children": []}
                 for k in range(len(kept))]
        level = 1
        while nodes:
            samples = {k: ([texts[i][:100] for i in nd["atoms"][:_NAME_SAMPLES]]
                           if level == 1
                           else [f"{c['name']}: {c['description']}"
                                 for c in nd["children"]][:_NAME_SAMPLES])
                       for k, nd in enumerate(nodes)}
            named = _name_clusters(samples)
            new_tags: list[dict] = []
            named_nodes: list[dict] = []
            for k, nd in enumerate(nodes):
                if k in named:
                    t = {"level": level, "name": named[k]["name"],
                         "description": named[k]["description"],
                         "l1": nd["l1"], "atoms": nd["atoms"],
                         "children": nd["children"], "tag_id": None}
                    new_tags.append(t)
                    named_nodes.append(nd)
                else:
                    n_unnamed += 1
            created.extend(new_tags)
            if level >= _LEVEL_CAP or not new_tags:
                break
            qedges = []
            for p in range(len(named_nodes)):
                for q in range(p + 1, len(named_nodes)):
                    cnt = sum(cross[(x, y)] for x in named_nodes[p]["l1"]
                              for y in named_nodes[q]["l1"])
                    if cnt > _QUOTIENT_EDGE_GT:
                        qedges.append((p, q, cnt))
            groups = [g for g in _louvain(len(named_nodes), qedges) if len(g) >= 2]
            if not groups or len(groups) >= len(named_nodes):
                break  # 簇数不再缩减 → 层级涌现停止 (spec §一)
            nodes = [{"l1": set().union(*(named_nodes[m]["l1"] for m in g)),
                      "atoms": [], "children": [new_tags[m] for m in g]}
                     for g in groups]
            level += 1

    # ── 单事务入图 (此前零 DB 写; distill M2 同款显式事务) ──
    n_name_skipped = 0
    level_stat: dict[int, dict] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for t in created:
            members = {ids[i] for i in t["atoms"]} if t["atoms"] else None
            t["tag_id"] = _find_or_mint_tag(conn, t["name"], t["description"],
                                            t["level"], members)
            if t["tag_id"] is None:
                n_name_skipped += 1
                continue
            s = level_stat.setdefault(t["level"], {"tags": 0, "mounts": 0})
            s["tags"] += 1
            for i in t["atoms"]:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO tag_mount(tag_id, atom_id, w) "
                    "VALUES(?, ?, 1.0)", (t["tag_id"], ids[i]))
                s["mounts"] += cur.rowcount or 0
        for t in created:  # parent_of 回挂 (子 tag 已在上循环拿到 tag_id)
            if t["tag_id"] is not None:
                for ch in t["children"]:
                    if ch["tag_id"] is not None:
                        conn.execute("UPDATE tag SET parent_id=? WHERE id=?",
                                     (t["tag_id"], ch["tag_id"]))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    res["levels"] = {str(k): v for k, v in sorted(level_stat.items())}
    res["clusters_dropped_unnamed"] = n_unnamed
    res["name_skipped"] = n_name_skipped
    res["tags"] = [{"level": t["level"], "name": t["name"],
                    "description": t["description"],
                    "members": len(t["atoms"]) or len(t["children"])}
                   for t in created if t["tag_id"] is not None]
    return res


def mount_new_atoms(db_path, use_laya: bool = True) -> int:
    """新 atom (needs_embed=1 或无 semantic mount) 试挂既有 semantic tag。

    laya 可用 (use_laya 且 laya_available): 每 atom 取 cos≥_MOUNT_COS 的
    top-3 候选 tag, 全部 (atom, tag) 对一次 laya 批评 (40 问/片), w=laya 分
    且 ≥_AUDIT_DROP 才挂 — 最近≠最相关, 竞争裁决 (挂账#1)。
    laya 不可用 → 回落原行为: 最近 tag ≥_MOUNT_COS 即挂 (w=cos)。
    挂不上的返回后仍无 mount, 留待下轮 mint 新社区发现。
    原子性: INSERT OR IGNORE 幂等, 不要求事务。"""
    conn = db.init(db_path)
    tags = conn.execute(
        "SELECT id, name, description FROM tag WHERE kind='semantic'").fetchall()
    if not tags:
        return 0
    rows = conn.execute(
        "SELECT a.id, a.text FROM atom a WHERE a.valid_to IS NULL AND ("
        "a.needs_embed=1 OR NOT EXISTS (SELECT 1 FROM tag_mount m "
        "JOIN tag t ON t.id=m.tag_id WHERE m.atom_id=a.id AND t.kind='semantic'))"
    ).fetchall()
    if not rows:
        return 0
    tv = embedding.embed_batch([t["description"] or t["name"] for t in tags])
    av = embedding.embed_batch([r["text"] for r in rows])
    # 对抗审查 minor: 保留下标映射 — 坏向量过滤若不记 idx, T 行号与 tags 错位,
    # 部分 embed 失败时挂载/laya 问句系统性指向错误 tag (静默错挂)。
    tidx = [i for i, v in enumerate(tv) if len(v) > _EMBED_DIM_MIN]
    T = np.vstack([np.asarray(tv[i], np.float32) for i in tidx])
    if not len(T):
        return 0
    T /= np.linalg.norm(T, axis=1, keepdims=True)
    with_laya = use_laya and laya_client.laya_available()
    mounted = 0
    pairs: list[tuple[int, int, str]] = []  # (atom_id, T 行号, atom text)
    for r, v in zip(rows, av):
        if len(v) <= _EMBED_DIM_MIN:
            continue
        a = np.asarray(v, np.float32)
        n = float(np.linalg.norm(a))
        if n == 0:
            continue
        sims = T @ (a / n)
        if with_laya:
            # stable: 并列 cos 时截断确定化 (同 atom 两轮送同一候选集)
            pairs.extend((r["id"], int(j), r["text"])
                         for j in np.argsort(-sims, kind="stable")[:3]
                         if sims[j] >= _MOUNT_COS)
        else:
            j = int(np.argmax(sims))
            if sims[j] >= _MOUNT_COS:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO tag_mount(tag_id, atom_id, w) "
                    "VALUES(?, ?, ?)",
                    (tags[tidx[j]][0], r["id"], round(float(sims[j]), 4)))
                mounted += cur.rowcount or 0  # 已挂 (needs_embed 候选重入) 不计数
    if with_laya and pairs:
        idx_same = CRIT_EDGE.index("same-topic")
        for s0 in range(0, len(pairs), _LAYA_SHARD):
            shard = pairs[s0:s0 + _LAYA_SHARD]
            qs = {f"p{i}": _rel_q(tags[tidx[j]]["name"], tags[tidx[j]]["description"], text)
                  for i, (_aid, j, text) in enumerate(shard)}
            state = ("candidate tag mounts:\n" + "\n".join(
                f"[{i}] [tag: {tags[tidx[j]]['name']} — {tags[tidx[j]]['description']}] "
                f"[{text[:130]}]" for i, (_aid, j, text) in enumerate(shard)))
            answers = _laya_one(state, qs)
            if answers is None:
                break  # 被动: 已挂数保留, 余对下轮再裁
            for i, (aid, j, _text) in enumerate(shard):
                w = _prob(answers.get(f"p{i}"), idx_same)
                if w is not None and w >= _AUDIT_DROP:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO tag_mount(tag_id, atom_id, w) "
                        "VALUES(?, ?, ?)", (tags[tidx[j]][0], aid, w))
                    mounted += cur.rowcount or 0
            conn.commit()
            time.sleep(1.0)  # 片间 1s (distill 同口径)
    else:
        conn.commit()
    return mounted


def grow_tag_tree(db_path) -> dict:
    """tag 树增量生长 (语义定级插入): mount_new_atoms 余留孤儿 → 聚类命名
    → zhipu 定支点 (大纲式: 挂哪个既有 tag 下 / 同义复用 / 新根) → laya
    可插入闸 (子题分 ≥_AUDIT_DROP 放行, 不过/laya 缺席 → 回退无父 L1) →
    单事务落 tag(level=parent.level+1, parent_id)+mounts。

    纪律与 mint 同: 全部 LLM/embed/laya 调用在任何 DB 写之前。
    幂等: _find_or_mint_tag 撞名复用 + mounts INSERT OR IGNORE。"""
    conn = db.init(db_path)
    res = {"orphans": 0, "candidates": 0, "singles_left": 0, "placed": 0,
           "reused": 0, "fallback_l1": 0, "mounted": 0, "skipped": 0}
    rows = conn.execute(
        "SELECT a.id, a.text FROM atom a WHERE a.valid_to IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM tag_mount m JOIN tag t ON t.id=m.tag_id "
        "WHERE m.atom_id=a.id AND t.kind='semantic') ORDER BY a.id").fetchall()
    res["orphans"] = len(rows)
    if len(rows) < 2:
        return res
    vecs = embedding.embed_batch([r["text"] for r in rows])
    keep = [(r, v) for r, v in zip(rows, vecs) if len(v) > _EMBED_DIM_MIN]
    if len(keep) < 2:
        return res
    ids = [r["id"] for r, _ in keep]
    texts = [r["text"] for r, _ in keep]
    V = np.vstack([np.asarray(v, np.float32) for _, v in keep])
    V /= np.linalg.norm(V, axis=1, keepdims=True)
    comms = _louvain(len(ids), _knn_edges(V, 0.60, 8))
    groups = [sorted(c) for c in comms if len(c) >= 2]
    grouped = {i for g in groups for i in g}
    # 单原子不成候选: 一句一 tag 只会铸出垃圾群 (320 垃圾 L1 实测教训),
    # 孤单原子留待后续 mount/mint — 攒够同伴再进树。
    cands = [{"atom_idx": g, "samples": [texts[i][:100] for i in g[:_NAME_SAMPLES]]}
             for g in groups]
    res["candidates"] = len(cands)
    res["singles_left"] = len(ids) - len(grouped)
    if not cands:
        return res

    named = _name_clusters({k: c["samples"] for k, c in enumerate(cands)})
    cands = [dict(atom_idx=c["atom_idx"], samples=c["samples"],
                  name=named[k]["name"], desc=named[k]["description"])
             for k, c in enumerate(cands) if k in named]
    if not cands:
        res["skipped"] = res["candidates"]
        return res

    # 定级放置: zhipu 分片批调用 (既有树大纲 + 候选, 按候选编号匹配)
    tree = conn.execute(
        "SELECT id, name, level, parent_id, description FROM tag "
        "WHERE kind='semantic' ORDER BY level, id").fetchall()
    tree_by_id = {r["id"]: r for r in tree}
    tree_lines = ["既有语义标签树:"]
    tree_lines += [f"[{r['id']}] L{r['level']} parent={r['parent_id']} "
                   f"{r['name']} — {(r['description'] or '')[:30]}" for r in tree]
    placement: dict[int, dict] = {}  # 候选下标 → {"parent":..,"same":..}
    for s0 in range(0, len(cands), _PLACE_SHARD):
        shard = range(s0, min(s0 + _PLACE_SHARD, len(cands)))
        lines = tree_lines + ["候选标签 (带成员样本):"]
        for k in shard:
            lines.append(f"候选{k}: {cands[k]['name']} — {cands[k]['desc'][:30]}")
            lines += [f"  - {s}" for s in cands[k]["samples"][:4]]
        try:
            raw = _get_zhipu().chat(
                SYS_PLACE, [{"role": "user", "content": "\n".join(lines)}],
                max_tokens=4000)
            for it in parse_llm_json(raw) or []:
                if isinstance(it, dict) and isinstance(it.get("id"), int) \
                        and it["id"] in shard:
                    placement[it["id"]] = it
        except Exception:
            pass  # 本片定级失败 → 该片候选按无父 L1 (下轮 mint 可再收编)
        time.sleep(1)

    # laya 可插入闸: (新 tag, 选中父) 对一次批评
    plans: list[dict] = []           # name/desc/level/parent_id/atom_ids
    gate_pairs: list[tuple[int, dict]] = []   # (plans 下标, parent row)
    for k, c in enumerate(cands):
        p = placement.get(k, {})
        same = _valid_tag(p.get("same"), tree_by_id)
        if same is not None:         # 同义复用 — 不建新 tag 只挂原子
            plans.append({"name": c["name"], "desc": c["desc"],
                          "level": None, "parent_id": None,
                          "reuse_tag": same["id"],
                          "atom_ids": {ids[i] for i in c["atom_idx"]}})
            continue
        parent = _valid_tag(p.get("parent"), tree_by_id)
        lvl = parent["level"] + 1 if parent is not None else 1
        if parent is not None and lvl > _LEVEL_CAP:
            parent = None
            lvl = 1
        pl = {"name": c["name"], "desc": c["desc"], "level": lvl,
              "parent_id": parent["id"] if parent is not None else None,
              "atom_ids": {ids[i] for i in c["atom_idx"]}}
        plans.append(pl)
        if parent is not None:
            gate_pairs.append((len(plans) - 1, parent))
    if gate_pairs and not laya_client.laya_available():
        for pi, _p in gate_pairs:            # laya 缺席 → 被动回退 L1 (不挂起)
            plans[pi]["parent_id"] = None
            plans[pi]["level"] = 1
    elif gate_pairs:
        idx_same = CRIT_EDGE.index("same-topic")
        qs = {f"g{i}": _sub_q(p["name"], p["description"] or "",
                               plans[pi]["name"], plans[pi]["desc"])
              for i, (pi, p) in enumerate(gate_pairs)}
        state = ("tag tree insertions:\n" + "\n".join(
            f"[{i}] [new: {plans[pi]['name']} — {plans[pi]['desc']}] "
            f"under [{p['name']} — {p['description'] or ''}]"
            for i, (pi, p) in enumerate(gate_pairs)))
        answers = _laya_one(state, qs)
        if answers is not None:
            for i, (pi, _p) in enumerate(gate_pairs):
                w = _prob(answers.get(f"g{i}"), idx_same)
                if w is not None and w < _AUDIT_DROP:   # 不可插入 → 回退 L1
                    plans[pi]["parent_id"] = None
                    plans[pi]["level"] = 1

    # 单事务写 (此前零 DB 写)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for pl in plans:
            if pl.get("reuse_tag") is not None:
                tid = pl["reuse_tag"]
                res["reused"] += 1
            else:
                tid = _find_or_mint_tag(conn, pl["name"], pl["desc"],
                                        pl["level"], pl["atom_ids"],
                                        pl["parent_id"])
                if tid is None:
                    res["skipped"] += 1
                    continue
                res["placed"] += 1
                if pl["parent_id"] is None and pl["level"] == 1:
                    res["fallback_l1"] += 1
            for aid in pl["atom_ids"]:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO tag_mount(tag_id, atom_id, w) "
                    "VALUES(?, ?, 1.0)", (tid, aid))
                res["mounted"] += cur.rowcount or 0
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return res


def _valid_tag(tid, tree_by_id) -> dict | None:
    """LLM 给的支点 id 防御: 必须是既有 semantic tag 行 (int 或数字串)。"""
    if isinstance(tid, str) and tid.isdigit():
        tid = int(tid)
    if isinstance(tid, bool) or not isinstance(tid, int):
        return None
    return tree_by_id.get(tid)


def audit_mounts(db_path, limit: int = 0) -> int:
    """laya 挂载审计 refinement pass (挂账#1): 全量 (limit>0 截断) semantic
    tag_mount 送 laya 评 [tag↔atom 主题相关度]; w<_AUDIT_DROP 卸载 DELETE,
    ≥_AUDIT_DROP 降权为真实分 UPDATE。计数 = 卸载+降权行数。

    幂等: 重跑重评分, w 收敛到 laya 分, 已卸载行不再出现。阈值 0.30 依据
    既有校准: 边判 strong≥0.5 / rubber-stamp 带≥0.3 — 挂载是 refinement 非
    写径红线, 故 laya 不可用 return 0 被动夜间再扫 (区别于 distill 挂起
    语义); 畸形答案跳过不处置; 片间崩溃只丢本片 (每片 commit, 行级独立)。
    单轮卸载帽 _AUDIT_DEL_CAP: laya 评分回归 (same-topic 整体<0.3) 时一夜
    DELETE 全量 4030 行不可逆 — 帽外低分行原样留待下轮 (自然限速+观察窗;
    daemon 只接 mount/audit 不接 mint, 无自动重建路径, 故必须限爆炸半径)。"""
    conn = db.init(db_path)
    rows = conn.execute(
        "SELECT m.tag_id, m.atom_id, t.name, t.description, a.text "
        "FROM tag_mount m JOIN tag t ON t.id=m.tag_id "
        "JOIN atom a ON a.id=m.atom_id WHERE t.kind='semantic' "
        "ORDER BY m.tag_id, m.atom_id").fetchall()
    if limit > 0:
        rows = rows[:limit]
    if not rows or not laya_client.laya_available():
        return 0
    idx_same = CRIT_EDGE.index("same-topic")
    handled = 0
    deleted = 0
    for s0 in range(0, len(rows), _LAYA_SHARD):
        shard = rows[s0:s0 + _LAYA_SHARD]
        qs = {f"m{i}": _rel_q(name, desc, text)
              for i, (_tid, _aid, name, desc, text) in enumerate(shard)}
        state = ("semantic tag mounts:\n" + "\n".join(
            f"[{i}] [tag: {name} — {desc}] [{text[:130]}]"
            for i, (_tid, _aid, name, desc, text) in enumerate(shard)))
        answers = _laya_one(state, qs)
        if answers is None:
            break  # 被动: 返回已处置数, 下轮再扫
        for i, (tag_id, atom_id, *_rest) in enumerate(shard):
            w = _prob(answers.get(f"m{i}"), idx_same)
            if w is None:
                continue  # 畸形答案 → 跳过不处置 (下轮再评)
            if w < _AUDIT_DROP:
                if deleted >= _AUDIT_DEL_CAP:
                    continue  # 单轮卸载帽满 — 低分行原样留待下轮
                conn.execute("DELETE FROM tag_mount WHERE tag_id=? AND atom_id=?",
                             (tag_id, atom_id))
                deleted += 1
            else:
                conn.execute("UPDATE tag_mount SET w=? WHERE tag_id=? AND atom_id=?",
                             (w, tag_id, atom_id))
            handled += 1
        conn.commit()
        time.sleep(1.0)  # 片间 1s (distill 同口径)
    return handled


def _print_stats(db_path: str) -> None:
    """dry-run: 既有 tag/挂载统计 + 聚类预览 (embed+louvain, 零 LLM 调用)。"""
    conn = db.init(db_path)
    for row in conn.execute(
            "SELECT kind, level, COUNT(*) c FROM tag GROUP BY kind, level"):
        print(f"tag kind={row['kind']} level={row['level']}: {row['c']}")
    print("tag_mount:", conn.execute("SELECT COUNT(*) FROM tag_mount").fetchone()[0])
    print("atom live:", conn.execute(
        "SELECT COUNT(*) FROM atom WHERE valid_to IS NULL").fetchone()[0],
        "| needs_embed:", conn.execute(
            "SELECT COUNT(*) FROM atom WHERE valid_to IS NULL AND needs_embed=1"
        ).fetchone()[0])
    ids, _texts, Vn = _load_atoms(conn)
    if Vn is None or len(ids) < 4:
        print("atoms insufficient for clustering:", len(ids))
        return
    edges = _knn_edges(Vn, 0.60, 8)
    comms = _louvain(len(ids), edges)
    sizes = sorted((len(c) for c in comms), reverse=True)
    kept = [s for s in sizes if s >= 4]
    print(f"communities: {len(comms)} kept(>=4): {len(kept)} "
          f"covered: {sum(kept)} top20: {sizes[:20]}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python3 src/tag_dream.py <db> [--mint|--mount|--grow|--audit]  "
              "(无 flag = dry-run 统计)")
        return 0
    import cli  # 模块导入即 _load_env() (.env: ZHIPU_API_KEY 等)
    db_path, flags = argv[0], set(argv[1:])
    if "--mount" in flags:
        print("mounted:", mount_new_atoms(db_path))
    elif "--grow" in flags:
        print(json.dumps(grow_tag_tree(db_path), ensure_ascii=False, indent=1))
    elif "--audit" in flags:
        print("audited:", audit_mounts(db_path))
    elif "--mint" in flags:
        r = mint_semantic_tags(db_path)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        conn = db.get_conn()
        print("top10 semantic tags:")
        for row in conn.execute(
                "SELECT t.name, t.level, COUNT(m.atom_id) c FROM tag t "
                "JOIN tag_mount m ON m.tag_id=t.id WHERE t.kind='semantic' "
                "GROUP BY t.id ORDER BY c DESC LIMIT 10"):
            print(f"  L{row['level']} {row['name']}: {row['c']}")
    elif not flags:
        _print_stats(db_path)
    else:
        print("unknown flags:", flags)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

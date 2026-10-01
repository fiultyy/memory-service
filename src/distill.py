"""H2 distill 模块: 段级一步蒸馏 (v4 判据 + embed + laya + 入图).

spec: docs/specs/graph-reform-v2-ingest-tags.md §二/§五-H2/§六。
参考配方: temp/full_distill.py (A致化/B embed/C v4分拣/D laya审计, 已验证)。

流水: split_units → zhipu v4 判据(致化+五类标签+段内 supersede, SYS_C 原样移植)
  → embedding.embed_batch(失败→needs_embed 标记, 不挂起)
  → 对既有 atom 内存余弦: ≥0.90 并成员(source_refs 追加) / 0.80-0.90 候选边
  → laya 单调用 (p_dur 审计 + 候选边验证, 40 问/片)
  → 单事务入图: atom / atom_edge / 事实tag铸币 / distill_seen 落 sha

LayaUnavailable 语义 (裁决 H0: laya 不可用=挂起, 不裸入图):
  distill_segment 在**任何 DB 写之前**完成全部外部调用 (zhipu/embed/laya);
  laya 关闭或批两次 None → raise LayaUnavailable, 图零变更、distill_seen
  不落行 → 调用方 (daemon) 持段重放即可 (zhipu 会被重计费, 记账/DLQ 属
  H7 daemon 面, 本模块不兜)。入图是**显式 BEGIN IMMEDIATE 事务, 异常
  rollback 整体回退** (M2 修正: 不走 db.transaction — 其 finally-commit
  会把异常路径已执行语句也提交, 原子性不成立)。audit_pending 同语义
  (不可用 → raise)。

事实tag铸币 (§六): session:{id} 恒铸(保底下界); cwd 命中
  ^/home/yy/projects/([^/]+) → repo:$1, /tmp 等跳过; svc: 暂缓。

表结构由 H5 DDL (schema.sql atom/atom_edge/tag/tag_mount/distill_seen) 承载;
distill_seen.status (毒段 DLQ 台账) 已进权威 DDL (schema.sql + db.py, m3),
_ensure_tables 的 ALTER-guard 仅作旧库兼容补列。

向量检索选型: **自建内存余弦 + embedding 模块 L2 缓存** —— atom 表不存向量
(H5 DDL 无该列), 既有 atom 向量经 embedding.embed_batch(texts) 取 (生产命中
embeddings.db text-hash 缓存, 不重复请求模型)。ponytail: 每段全量扫 + 重取
既有向量, O(N_atoms); atom 过万再建 atom 向量表/ANN 索引。
(H6 补注: 召回面已建 vec_atom ANN 命名空间 — distill 插入后增量 sync
(_sync_atom_vecs) + reembed_needing 补扫回填; 本模块写侧配对仍走内存余弦,
迁移到 ANN 是 atom 过万后的独立优化。)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓根平铺模块

import numpy as np
import chunk_graph
import db
import embedding
import laya_client
from llm_provider import ZhipuAnthropicProvider
from typing import Any

# ── v4 判据 SYS (temp/full_distill.py SYS_A + SYS_C 原样移植) ──────────
SYS_JUDGE = (
    "你是记忆压缩器兼价值分拣器。先对每个编号条目输出一句自包含结论句(≤40字): "
    "修复截断、补全省略主语、保留实体与裁决。\n"
    "你是记忆价值分拣器。\n"
    "强判据一(可查回性): 内容可从 git 历史/CI/台账原样查回的——某 commit 做了什么改动、"
    "某次测试跑分、某文档/报告已写已发——无论句子怎么措辞, 一律 process。记忆库不复制 git 能查的事。\n"
    "强判据二(主语): 主语是资产(服务/配置/字段/能力/代码位置/系统行为/工作线)→按价值判; "
    "主语是动作(提交/推送/发送/验证动作)→process。\n"
    "价值类型:\n"
    "- fact: 环境与资产现在的样子——架构/契约/端口/不变量/迁移后状态/配置终态(不含某次commit的具体改动)\n"
    "- judgment: 裁决/根因/行为观察(为什么/机制表现如何)\n"
    "- experience: 坑/故障案例(含已修复的)/解法模式\n"
    "- summary: 被实测验证确立的能力终态(什么回路/机制已走通); 票/工作线的当前终态(TK-X已收口/已滚出/已移交)\n"
    "- process: 时点快照/进行时/待办/动作回执/git可查记录/编排轨迹统计\n"
    "默认怀疑: 拿不准判 process; 但资产终态、故障案例、行为观察宁留勿杀。\n"
    "带[Cx]标记的句子同属一个语义簇, 上下相邻句语义相近。\n"
    "若某句陈述的状态已被列表中更新的句子取代, 标 superseded_by=<新句编号>。\n"
    '只输出 JSON 数组: [{"id":编号,"summary":"结论句","label":"fact|judgment|experience|summary|process",'
    '"superseded_by":编号(可选)}], 不要其它文字。'
)
_LABELS = ("fact", "judgment", "experience", "summary", "process")
CRIT_DUR = ["transient-process", "context-bound", "durable-knowledge"]  # full_distill CRIT_D 原样
CRIT_EDGE = ["unrelated", "related", "same-topic"]                      # full_distill CRIT_F 原样

_ZHIPU: ZhipuAnthropicProvider | None = None
_MERGE_COS = 0.90   # ≥ → 并成员
_CAND_COS = 0.80    # [0.80, 0.90) → 候选边
_LAYA_SHARD = 40    # 40 问/片 (full_distill D/F 同口径)
_EMBED_DIM_MIN = 100  # 真模型 2560 维; 短于此视为坏向量 (full_distill B 同判据)


class LayaUnavailable(Exception):
    """laya 不可用 (env 关 / health 探测失败 / 批两次 None)。

    调用方挂起语义: 保留段不裸入图 (spec H0)。raise 时 DB 零写入。"""


class ConfigIncomplete(LayaUnavailable):
    """配置/provider 不可用 (ZHIPU_API_KEY 缺失 / zhipu 3 轮全网络败)。

    m2 (2026-10-01): 挂起语义非毒段 — 裸启动 (无 key) 或断网时 _judge_units
    3 连败若判 content-poison 落 status='poison', daemon 会视成功删 spool
    文件 → 静默丢段。复用 LayaUnavailable 挂起 lane (daemon 同一 except 承接),
    毒段只留给「provider 真返回了但输出不可解析」的内容性失败。"""


def _get_zhipu() -> ZhipuAnthropicProvider:
    global _ZHIPU
    if _ZHIPU is None:
        _ZHIPU = ZhipuAnthropicProvider(timeout=120.0)  # H2: zhipu 120s 硬帽
    return _ZHIPU


def parse_llm_json(raw):
    """temp/full_distill.py 原样移植: [..] 数组优先, 裸对象流括号深度扫描兜底。"""
    m = re.search(r"\[.*\]", raw, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return arr
        except json.JSONDecodeError:
            pass
    objs, buf, depth, instr, esc = [], [], 0, False, False
    for ch in raw:
        if instr:
            buf.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
            buf.append(ch)
        elif ch == "{":
            depth += 1
            buf.append(ch)
        elif ch == "}":
            depth -= 1
            buf.append(ch)
            if depth == 0:
                objs.append("".join(buf))
                buf = []
        elif depth > 0:
            buf.append(ch)
    out = []
    for o in objs:
        try:
            j = json.loads(o)
            if isinstance(j, dict):
                out.append(j)
        except json.JSONDecodeError:
            pass
    return out or None


def _ensure_tables(conn) -> None:
    """表本体由 schema.sql (H5 DDL, db.init 已跑) 承载; 这里只幂等补
    distill_seen.status (毒段 DLQ: ok/poison) — m3 后权威 DDL 已含该列,
    本 ALTER 仅兜旧库 (db.py 同款 PRAGMA table_info + ADD COLUMN 幂等模式)。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(distill_seen)")}
    if cols and "status" not in cols:
        conn.execute(
            "ALTER TABLE distill_seen ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'")


def _judge_units(units: list[str]) -> list[dict] | None:
    """zhipu 一步致化+分拣+段内 supersede; 毒输出重试 3 次, 全毒 → None (毒段)。

    m2: 3 轮全网络层异常 (无任何可解析产出) → raise ConfigIncomplete 挂起 —
    不是内容毒 (裸启动缺 key/断网判毒会静默丢段, 见类 docstring)。
    后规则 (full_distill stage_c 原样): goal 轨迹统计句强制 process。"""
    done: dict[int, dict] = {}
    errored = 0
    for attempt in range(3):
        todo = [i for i in range(len(units)) if i not in done]
        if not todo:
            break
        lines = "\n".join("%d: %s" % (i, units[i][:400]) for i in todo)
        try:
            raw = _get_zhipu().chat(SYS_JUDGE, [{"role": "user", "content": lines}],
                                    max_tokens=20000)
        except Exception:
            errored += 1
            time.sleep(2 * (attempt + 1))
            continue
        for it in parse_llm_json(raw) or []:
            if isinstance(it, dict) and it.get("id") in todo \
                    and it.get("label") in _LABELS \
                    and isinstance(it.get("summary"), str) and it["summary"].strip():
                done[it["id"]] = {"summary": it["summary"].strip(),
                                  "label": it["label"], "sup": it.get("superseded_by")}
        time.sleep(1)
    out = []
    for i in sorted(done):
        d = done[i]
        t = d["summary"]
        if d["label"] != "process" and \
                re.search(r"goal-[0-9a-f]{6,}", t) and re.search(r"历时|轮", t):
            d["label"] = "process"
        out.append({"id": i, "unit": units[i], **d})
    if not out and errored:
        raise ConfigIncomplete(
            f"zhipu {errored}/3 轮网络层失败且零可解析产出 — 挂起等恢复, 不判毒段")
    return out if out else None


def _valid_vec(v) -> bool:
    return isinstance(v, (list, np.ndarray)) and len(v) > _EMBED_DIM_MIN


def _embed_summaries(texts: list[str]) -> list | None:
    """批 embed + 2 次重试 (full_distill lms_embed 同型, 收紧重试数);
    整体失败 → None (调用方标 needs_embed, 不挂起)。"""
    for attempt in range(3):
        got = embedding.embed_batch(texts)
        if got is not None and all(_valid_vec(v) for v in got):
            return got
        time.sleep(2 * (attempt + 1))
    return None


def _load_existing() -> list[tuple[int, np.ndarray]]:
    """既有有效 atom 的归一向量 (embed_batch 批取, 生产命中 embeddings.db
    缓存; 失败 → 空 = 本段不做余弦配对, 见模块 docstring 选型)。"""
    conn = db.get_conn()
    rows = conn.execute("SELECT id, text FROM atom WHERE valid_to IS NULL").fetchall()
    if not rows:
        return []
    vecs = embedding.embed_batch([r["text"] for r in rows])
    out = []
    if vecs is not None:
        for r, v in zip(rows, vecs):
            if not _valid_vec(v):
                continue
            a = np.asarray(v, dtype=np.float32)
            n = float(np.linalg.norm(a))
            if n > 0:
                out.append((r["id"], a / n))
    return out


def _laya_one(state: str, questions: dict) -> dict | None:
    """laya 批, None 重试 1 次 (full_distill D 同型); 仍 None → None。"""
    ans = laya_client.laya_batch(state, questions, timeout=60.0)  # H2: laya 60s 硬帽
    if ans is None:
        time.sleep(2)
        ans = laya_client.laya_batch(state, questions, timeout=60.0)
    return ans


def _prob(answer: dict | None, idx: int) -> float | None:
    """score 型 answer 键位防御: probabilities 必须是 dict 且非空, 否则 None
    (畸形不炸不误读 — 仓内 3 起键位错读坑史)。"""
    if not isinstance(answer, dict):
        return None
    pr = answer.get("probabilities")
    if not isinstance(pr, dict) or not pr:
        return None
    v = pr.get(str(idx))
    return float(v) if isinstance(v, (int, float)) else None


def _mint_fact_tags(conn, atom_ids: list[int], session_id: str,
                    cwd: str | None, ts: str) -> None:
    """事实tag铸币 (§六): session 恒铸保底; cwd 白名单归一铸 repo:; 其余跳过。
    kind=factual / level=1 / w=1.0 确定挂载 (H5 DDL 口径)。"""
    names = []
    if session_id:
        names.append("session:" + session_id)
    if cwd:
        m = re.match(r"^/home/yy/projects/([^/]+)", cwd)
        if m:
            names.append("repo:" + m.group(1))
    for name in names:
        conn.execute(
            "INSERT OR IGNORE INTO tag(name, kind, level, description, created_at) "
            "VALUES(?, 'factual', 1, '事实tag(溯源直铸)', ?)", (name, ts))
        tid = conn.execute("SELECT id FROM tag WHERE name=? AND level=1",
                           (name,)).fetchone()[0]
        for aid in atom_ids:
            conn.execute("INSERT OR IGNORE INTO tag_mount(tag_id, atom_id, w) "
                         "VALUES(?, ?, 1.0)", (tid, aid))


def distill_segment(segment_text: str, session_id: str, cwd: str,
                    ts: str) -> dict:
    """蒸馏一个 spool 段并入图。幂等: 段内容 sha 去重 (跨文件重放免疫)。

    返回 {"atoms": 新插入数, "edges": 新边数, "merged": 并入既有 atom 数,
    "supersede_proposals": 段内 supersede 提案(挂夜间清算)}; 重复段额外带
    skipped="seen", 毒段 skipped="poison" (计数全 0, distill_seen 落
    status='poison' 不再重试 — DLQ 退避属 H7 daemon 面)。

    laya 不可用 → raise LayaUnavailable (DB 零写入, 见模块 docstring)。
    embed 失败 → 不挂起, atom 标 needs_embed=1 (reembed_needing 补)。"""
    sha = hashlib.sha256(segment_text.encode("utf-8")).hexdigest()
    conn = db.get_conn()
    _ensure_tables(conn)
    if conn.execute("SELECT 1 FROM distill_seen WHERE sha=?", (sha,)).fetchone():
        return {"atoms": 0, "edges": 0, "merged": 0,
                "supersede_proposals": [], "skipped": "seen"}

    units = chunk_graph.split_units(segment_text)
    if not units:
        conn.execute("INSERT OR IGNORE INTO distill_seen(sha, status, created_at) "
                     "VALUES(?, 'ok', ?)", (sha, ts))
        return {"atoms": 0, "edges": 0, "merged": 0,
                "supersede_proposals": [], "skipped": "empty"}

    if not laya_client.laya_available():
        raise LayaUnavailable("laya unavailable (env/health); 段保留在 spool, 图零写入")
    if not os.environ.get("ZHIPU_API_KEY"):
        # m2: 配置不全直接挂起 — 缺 key 时 _judge_units 3 连网络败不该走到
        # 毒段判定 (daemon 视 poison=成功删文件 → 静默丢段)。
        raise ConfigIncomplete("ZHIPU_API_KEY 缺失; 挂起等配置, 不判毒段")

    judged = _judge_units(units)
    if judged is None:  # 毒段: 3 次重试全不可解析 → 落 poison 防重放再计费
        conn.execute("INSERT OR IGNORE INTO distill_seen(sha, status, created_at) "
                     "VALUES(?, 'poison', ?)", (sha, ts))
        return {"atoms": 0, "edges": 0, "merged": 0,
                "supersede_proposals": [], "skipped": "poison"}

    valid_ids = {d["id"] for d in judged}
    supersede_proposals = []
    for d in judged:
        sup = d.get("sup")
        if isinstance(sup, int) and sup in valid_ids and sup != d["id"]:
            new = next(x for x in judged if x["id"] == sup)
            supersede_proposals.append(
                {"old_id": d["id"], "new_id": sup,
                 "old": d["summary"], "new": new["summary"]})
    keep = [d for d in judged
            if d["label"] != "process"
            and not (isinstance(d.get("sup"), int) and d["sup"] in valid_ids
                     and d["sup"] != d["id"])]
    if not keep:
        conn.execute("INSERT OR IGNORE INTO distill_seen(sha, status, created_at) "
                     "VALUES(?, 'ok', ?)", (sha, ts))
        return {"atoms": 0, "edges": 0, "merged": 0,
                "supersede_proposals": supersede_proposals, "skipped": "all_process"}

    vecs = _embed_summaries([d["summary"] for d in keep])

    # ── 余弦配对: ≥0.90 并成员 / [0.80,0.90) 候选边 (顺序处理, 段内新句
    #    依次成为"既有", 天然覆盖段内重复)。embed 整体失败 → 无向量,
    #    跳过配对 (needs_embed 兜底), 不挂起。
    existing = _load_existing() if vecs is not None else []
    new_atoms: list[dict] = []        # 待插入 (插入后带真实 aid)
    merged_refs: dict[int, list[str]] = {}  # 既有 atom_id -> 追加的原文 units
    cand_edges: list[tuple[int, dict]] = []  # (best_id 或段内占位负 id, 新句 d)
    n_merged = 0
    for i, d in enumerate(keep):
        vec = None
        if vecs is not None:
            v = np.asarray(vecs[i], dtype=np.float32)
            n = float(np.linalg.norm(v))
            if n > 0:
                vec = v / n
        d["vec"] = vec
        best_id, best_cos = None, -1.0
        if vec is not None:
            for aid, ev in existing:
                c = float(vec @ ev)
                if c > best_cos:
                    best_id, best_cos = aid, c
        if best_id is not None and best_cos >= _MERGE_COS:
            if best_id < 0:  # 段内占位 → 并入本段前一个新 atom
                new_atoms[-best_id - 1].setdefault("merge_units", []).append(d["unit"])
            else:
                merged_refs.setdefault(best_id, []).append(d["unit"])
            n_merged += 1
        else:
            if best_id is not None and best_cos >= _CAND_COS:
                cand_edges.append((best_id, d))
            d["idx"] = len(new_atoms)
            new_atoms.append(d)
            if vec is not None:
                existing.append((-1 - d["idx"], vec))  # 段内占位 id, 插入后回填

    # ── laya 单调用: p_dur 审计 + 候选边验证 (40 问/片)
    qs: dict[str, dict] = {}
    for j, d in enumerate(new_atoms):
        qs[f"dur_{j}"] = {"type": "score",
                          "instructions": f"Is [{d['summary'][:130]}] durable knowledge "
                                          "worth keeping in long-term memory, or transient "
                                          "process detail?",
                          "criteria": CRIT_DUR}
    for k, (aid, d) in enumerate(cand_edges):
        qs[f"edg_{k}"] = {"type": "score",
                          "instructions": f"How related are the memory items "
                                          f"[{d['summary'][:90]}] and [atom {aid}] "
                                          "in topic?",
                          "criteria": CRIT_EDGE}
    p_dur: dict[int, float | None] = {}
    edge_w: dict[int, float] = {}
    if qs:
        state = ("new memory items:\n"
                 + "\n".join(f"[{d['summary'][:130]}]" for d in new_atoms)
                 + "\nexisting neighbors:\n"
                 + "\n".join(f"[atom {aid}] {d['summary'][:90]}"
                             for aid, d in cand_edges))
        answers: dict | None = {}
        for s0 in range(0, len(qs), _LAYA_SHARD):
            shard = dict(list(qs.items())[s0:s0 + _LAYA_SHARD])
            part = _laya_one(state, shard)
            if part is None:
                raise LayaUnavailable("laya batch None x2; 段保留在 spool, 图零写入")
            answers.update(part)
            time.sleep(1.0)  # 片间 1s (full_distill D/F 同口径)
        for j in range(len(new_atoms)):
            p_dur[j] = _prob(answers.get(f"dur_{j}"), CRIT_DUR.index("durable-knowledge"))
        for k in range(len(cand_edges)):
            w = _prob(answers.get(f"edg_{k}"), CRIT_EDGE.index("same-topic"))
            if w is not None:  # 畸形边答案 → 弃边不炸 (passive)
                edge_w[k] = w

    # ── 单事务入图 (此前零 DB 写)。向量不在 atom 表 (H5 DDL 无列) —
    #    新句向量只用于本段配对; 持久化靠 embedding 模块 L2 缓存与
    #    reembed_needing 补扫, 见模块 docstring 选型。
    # M2: 显式 BEGIN IMMEDIATE + 异常 rollback — db.transaction 的 finally
    #    是 conn.commit(), 异常路径已执行语句也会被提交, "单事务原子入图"
    #    不成立。可抛操作 (source_refs 读取+解析) 全部前移到任何 INSERT
    #    之前 (仍在写锁内), 事务体内其后只剩不可抛的 execute。
    n_atoms = n_edges = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        merged_ref_vals: dict[int, str] = {}
        for aid, units_added in merged_refs.items():
            row = conn.execute("SELECT source_refs FROM atom WHERE id=?",
                               (aid,)).fetchone()
            refs = json.loads(row[0] or "[]") if row else []
            refs.extend(units_added)
            merged_ref_vals[aid] = json.dumps(refs, ensure_ascii=False)
        cur = conn.cursor()
        for j, d in enumerate(new_atoms):
            cur.execute(
                "INSERT INTO atom(text, label, p_dur, valid_from, source_refs, "
                "source_cwd, needs_audit, needs_embed, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (d["summary"], d["label"], p_dur.get(j) or 0.0, ts,
                 json.dumps([d["unit"]] + d.get("merge_units", []),
                            ensure_ascii=False),
                 cwd,
                 1 if p_dur.get(j) is None else 0,
                 1 if d["vec"] is None else 0,
                 ts))
            d["aid"] = cur.lastrowid
            n_atoms += 1
        for aid, refs_json in merged_ref_vals.items():
            conn.execute("UPDATE atom SET source_refs=? WHERE id=?",
                         (refs_json, aid))
        for k, (aid, d) in enumerate(cand_edges):
            if k not in edge_w:
                continue
            # 段内占位负 id (-1-idx) → 回填该新 atom 的真实 id
            other = new_atoms[-aid - 1]["aid"] if aid < 0 else aid
            pair = sorted((other, d["aid"]))  # H5 DDL: 无向边规范序 a<b
            conn.execute("INSERT OR IGNORE INTO atom_edge(a_id, b_id, w, kind) "
                         "VALUES(?, ?, ?, 'related')",
                         (pair[0], pair[1], edge_w[k]))
            n_edges += 1
        _mint_fact_tags(conn, [d["aid"] for d in new_atoms],
                        session_id, cwd, ts)
        # ≥0.90 并入既有 atom 的源也挂本段事实 tag (prune 反查可见性, 验收 minor)
        if merged_refs:
            _mint_fact_tags(conn, sorted(merged_refs), session_id, cwd, ts)
        conn.execute("INSERT OR IGNORE INTO distill_seen(sha, status, created_at) "
                     "VALUES(?, 'ok', ?)", (sha, ts))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    # H6: 插入事务后增量同步 vec_atom (召回向量腿)。import 放函数内防循环
    # 依赖; passive — 任何失败 (维度不匹配/vec 面异常) 只丢向量不炸蒸馏,
    # atom 已落 needs_embed 时由 reembed_needing 夜间补。
    _sync_atom_vecs([(d["aid"], d["vec"]) for d in new_atoms if d.get("vec") is not None])
    return {"atoms": n_atoms, "edges": n_edges, "merged": n_merged,
            "supersede_proposals": supersede_proposals}


def audit_pending() -> int:
    """needs_audit=1 的 atom 补 laya p_dur 审计 (40 问/片)。

    返回修复数; 畸形 answer 的 atom 保持挂账 (下轮再试)。laya 不可用 →
    raise LayaUnavailable (daemon 持有, 不清挂账)。"""
    conn = db.get_conn()
    _ensure_tables(conn)
    rows = conn.execute("SELECT id, text FROM atom WHERE needs_audit=1").fetchall()
    if not rows:
        return 0
    if not laya_client.laya_available():
        raise LayaUnavailable("laya unavailable; 挂账保留")
    fixed = 0
    idx_dur = CRIT_DUR.index("durable-knowledge")
    for s0 in range(0, len(rows), _LAYA_SHARD):
        shard = rows[s0:s0 + _LAYA_SHARD]
        qs = {f"dur_{r['id']}": {"type": "score",
                                  "instructions": f"Is [{r['text'][:130]}] durable "
                                                  "knowledge worth keeping in long-term "
                                                  "memory, or transient process detail?",
                                  "criteria": CRIT_DUR} for r in shard}
        state = "memory items:\n" + "\n".join(f"[m{r['id']}] {r['text'][:130]}"
                                              for r in shard)
        answers = _laya_one(state, qs)
        if answers is None:
            raise LayaUnavailable("laya batch None x2; 挂账保留")
        for r in shard:
            v = _prob(answers.get(f"dur_{r['id']}"), idx_dur)
            if v is not None:
                conn.execute("UPDATE atom SET p_dur=?, needs_audit=0 WHERE id=?",
                             (v, r["id"]))
                fixed += 1
        time.sleep(1.0)
    return fixed


def _sync_atom_vecs(pairs: list[tuple[int, Any]]) -> None:
    """H6: (atom_id, vec) → vec_index.sync_atom 增量同步。passive — 任何
    异常吞掉 (向量是召回增强面, 不是蒸馏本体; 维度不匹配 sync_atom 内部
    已跳过)。"""
    try:
        import vec_index
        if not vec_index.available():
            return
        for aid, vec in pairs:
            vec_index.sync_atom(aid, [float(x) for x in vec])
    except Exception:
        pass


def reembed_needing() -> int:
    """needs_embed=1 的 atom 补向量 (embedding.embed_batch)。

    atom 表不存向量 (H5 DDL) — 补向量 = 预热 embedding 模块 L2 缓存
    (embeddings.db), 使后续 recall/配对免请求直取; 成功即清标记。
    H6: 补成后同步入 vec_atom 索引 (召回向量腿), passive。
    返回修复数; embed 不可用 → 0 (passive, 调用方夜间再扫)。只补向量,
    补后不追溯 cos 合并/候选边 — 归属由 H4 tag dreaming 夜间挂载收口。"""
    conn = db.get_conn()
    _ensure_tables(conn)
    rows = conn.execute("SELECT id, text FROM atom WHERE needs_embed=1").fetchall()
    if not rows:
        return 0
    got = _embed_summaries([r["text"] for r in rows])
    if got is None:
        return 0
    fixed = 0
    done: list[tuple[int, Any]] = []
    for r, v in zip(rows, got):
        if _valid_vec(v):
            conn.execute("UPDATE atom SET needs_embed=0 WHERE id=?", (r["id"],))
            done.append((r["id"], v))
            fixed += 1
    _sync_atom_vecs(done)
    return fixed

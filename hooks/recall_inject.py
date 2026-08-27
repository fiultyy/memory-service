"""UserPromptSubmit 注入器 (harness P2) — stdin CC payload → additionalContext。

管道: stdin JSON {prompt, session_id, cwd} → 词法 recall (top_k=8, **全局
单体 KG**: 不传 cwd → 无 source_cwd 过滤, 跨项目记忆可召回 — ADR-14 默认)
→ 阈值/2KB 预算裁剪 → stdout::

    {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                            "additionalContext": "<markdown 记忆命中段>"}}

设计裁决 (P2):
- **词法优先, 零 embed 依赖**: recall use_vec=False (默认) — LM Studio 不
  在线注入照常工作; 向量融合留给后续版本按需开 (MEM_RECALL_USE_VEC=1)。
- **实体锚定精度门**: 注入只收 prompt 指名实体 (search_entities 命中) 的
  fact。纯 value 扫描候选全拒 — 长 prompt 稀释 bigram 会命中任意 value
  (实测跨项目噪声 0.44 分霸榜)。无锚定实体 → 跳过整个 recall (零 DB 写)。
  跨项目 KB 1205/1206 fact source_cwd=NULL → cwd 过滤无区分度, 不用。
- **LIF 强化记账 (精确)**: recall 调用 boost=False (recall 内建 boost 会对
  全部返回候选记账 — 未注入不该强化), 注入器只对**最终注入**的 ≤top_k 条
  refresh_lif_on_recall — 注入即使用 (ADR-8v2 反馈环 = 记忆使用记账)。
- **不建 mem-*.md**: 不传 cwd/mem_dir → mem_dir=None, 投影物化归
  SessionStart hook (reconcile), 注入面保持只读+LIF 记账。
- **query 截断**: prompt 前 N 字符 (默认 800) 作 query — 粘贴长文档不
  爆 token 扫描; CJK bigram 切分见 scoring.query_tokens。
- **无命中零输出** (stdout 空 = CC 无感知); 任何异常 → data/hook-recall.log
  一行, exit 0 静默 — 注入是增强, 绝不阻塞 prompt。

env: MEM_RECALL_MIN_SCORE (默认 0.15 — 长 prompt match 稀释自校准; 短 query
地板 0.3 是 recall 内部默认) /
MEM_RECALL_TOP_K (8) / MEM_RECALL_MAX_BYTES (2048) /
MEM_RECALL_QUERY_CHARS (800) / MEM_RECALL_USE_VEC (0)。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SVC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SVC_DIR))


def _log_fail(msg: str) -> None:
    try:
        log = SVC_DIR / "data" / "hook-recall.log"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except Exception:
        pass  # 日志失败也不挡 prompt


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # 非 JSON payload → 静默
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return 0
    session_id = payload.get("session_id") or None

    min_score = float(os.environ.get("MEM_RECALL_MIN_SCORE", "0.05"))
    top_k = int(os.environ.get("MEM_RECALL_TOP_K", "8"))
    per_anchor_quota = int(os.environ.get("MEM_RECALL_PER_ANCHOR", "3"))
    cand_k = int(os.environ.get("MEM_RECALL_CAND_K", "50"))
    max_bytes = int(os.environ.get("MEM_RECALL_MAX_BYTES", "2048"))
    query_chars = int(os.environ.get("MEM_RECALL_QUERY_CHARS", "800"))
    use_vec = os.environ.get("MEM_RECALL_USE_VEC", "0") == "1"

    query = prompt[:query_chars]
    try:
        import cli  # noqa: F401 — module import 即 _load_env() (.env → ZHIPU 等)
        import recall as recall_mod
        import scoring
        # 实体锚定 (精度门): prompt **字面指名**的实体 — search_entities 出
        # 候选 (token LIKE 命中), 再验实体全名 ⊆ query (反向包含)。纯子串
        # 方向会误触: prompt「专家样本」的 bigram 专家 LIKE 命中跨项目实体
        # 「专家职位」→ 护理职位噪声入场; 反向后 专家职位 ⊄ query ✗ 拒。
        # value 扫描候选同理全拒 (长 prompt 稀释 bigram 命中任意 value)。
        # 跨项目 KB 1205/1206 fact source_cwd=NULL → cwd 过滤无区分度, 不用。
        ql = query.lower()
        anchor_ids = {
            e["id"] for e in recall_mod.search_entities(
                scoring.query_tokens(query))
            if e["name"].lower() in ql
        }
        if not anchor_ids:
            return 0  # prompt 未指名任何已知实体 → 无可注入, 跳过整个 recall
        # boost=False + 大候选窗: 稀释使被指名实体的关键 fact (~0.15) 排不进
        # 小 top_k; recall 内建 boost 会对**返回的全部**候选记 LIF 账 (污染
        # — 未注入不该强化)。这里纯读大窗, 记账只对最终注入的 ≤top_k 条做。
        result = cli.recall(
            query, session_id=session_id, top_k=cand_k,
            boost=False, with_tag=True, use_vec=use_vec,
            min_score=min_score,  # 长 prompt match 稀释 → 注入通道自校准低门槛
        )
    except Exception as exc:  # 召回失败 → 零注入 + 记日志 (不降级, 不挡路)
        _log_fail(f"recall-fail: {type(exc).__name__}: {exc}")
        return 0

    results = result.get("results", []) if isinstance(result, dict) else []
    candidates = [
        r for r in results if float(r.get("score", 0.0)) >= min_score
        and (r.get("fact", {}).get("subject_id") in anchor_ids
             or r.get("fact", {}).get("object_id") in anchor_ids)
    ]
    # 每锚实体配额: 长 prompt 下 value 词重叠多的实体 (LLM) 会霸榜, 把
    # 低匹配但被指名实体的关键 fact (sqlite-vec 依赖关系) 挤出 top_k。
    # 分数序遍历 + 单锚配额 → 每个 prompt 指名实体都有代表。
    per_anchor: dict[str, int] = {}
    hits = []
    for r in candidates:  # recall 已按 score 降序
        f = r.get("fact") or {}
        a = f.get("subject_id") if f.get("subject_id") in anchor_ids \
            else f.get("object_id")
        if a not in anchor_ids:
            continue
        if per_anchor.get(a, 0) >= per_anchor_quota:
            continue
        per_anchor[a] = per_anchor.get(a, 0) + 1
        hits.append(r)
        if len(hits) >= top_k:
            break
    if not hits:
        return 0

    # LIF 强化记账 (ADR-8v2 反馈环 = 记忆使用记账): 只对**最终注入**的
    # ≤top_k 条做 — 注入即使用。refresh 是权威写 (access_count+1 /
    # seen_sessions 吸收 / LIF 重算); 失败不挡注入 (记账尽力)。
    try:
        import db
        import scoring
        conn = db.get_conn()
        for r in hits:
            scoring.refresh_lif_on_recall(
                (r.get("fact") or {}).get("id"), session_id=session_id,
                conn=conn)
    except Exception as exc:
        _log_fail(f"boost-fail: {type(exc).__name__}: {exc}")

    lines = [f"## Memory recall (auto, {len(hits)} hits)"]
    budget = max_bytes
    for r in hits:
        tag = r.get("tag") or {}
        display = (tag.get("display") or "").strip() or "?"
        val = ((r.get("fact") or {}).get("value") or "").strip()
        if len(val) > 80:
            val = val[:77] + "..."
        entry = f"- {display} — {val}  [{float(r.get('score', 0.0)):.2f}]" if val \
            else f"- {display}  [{float(r.get('score', 0.0)):.2f}]"
        n = len(entry.encode("utf-8"))
        if budget - n < 0:
            break
        lines.append(entry)
        budget -= n
    if len(lines) == 1:
        return 0  # 预算内一条都放不下 → 零输出

    ctx = "\n".join(lines)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

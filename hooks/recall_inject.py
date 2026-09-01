"""UserPromptSubmit 注入器 (harness P2 + v1.7 ② 首n turn 召回) — stdin CC payload → additionalContext。

管道: stdin JSON payload → 词法 recall (top_k=8, **全局
单体 KG**: 不传 cwd → 无 source_cwd 过滤, 跨项目记忆可召回 — ADR-14 默认)
→ 阈值/2KB 预算裁剪 → stdout::

    {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                            "additionalContext": "<markdown 记忆命中段>"}}

stdin 字段 (实读): ``prompt`` / ``session_id`` 必用; CC ≥2.x 另含
``transcript_path`` / ``cwd`` (2.1.179 hook 基座 schema: transcript_path 为
必填 string; 老版本可能缺 → session_id 反查, 见下)。

首 n turn 召回窗口 (v1.7 ②, 2026-09-01):
- **时序终裁**: CC 2.1.179 二进制静态代码序 — UserPromptSubmit hooks 在
  transcript append **之前**执行, 且 hook 输入 schema ``transcript_path``
  必填。故 hook 触发时 transcript 只含既往 turn → **turn 判据 = ``count < n``**
  (count = 已落盘既往 user_text 块数; 首 turn count=0 → 召回窗口内)。
- **transcript 双路径**: 优先 ``payload["transcript_path"]``; 缺失时用
  session_id 反查 ``transcripts._cc_project_dir(cwd)/<session_id>.jsonl``
  (存在才用; 反查也落空 → 常驻档)。
- **计数** (``_count_user_turns``): 逐行 json.loads(坏行跳) → type=="user" 且
  非 isSidechain → ``transcripts._texts_of`` → ``corpus_prep.clean(txt,"cc")``
  非空才计 (tool_result/thinking 块与 ``<memsvc-recall>`` 注入块天然滤除);
  count 达 n **早停** (大 transcript 超时防线)。**不放** ``_cc_scenes`` — 它有
  全会话去重/场景消耗/截断, 会三重低估; 复用的是其原语。任何异常 → None =
  静默降级常驻档 (fail-open, 不挡路)。
- **档位**: count < n = 首轮档 (use_vec=1 + 候选窗 MEM_RECALL_FIRST_TOPK 提升);
  count >= n 或计数不可用 = 常驻档 (实体锚定零嵌入 — use_vec=False 把 recall
  嵌入路全关, A 路常驻每 prompt; embedding 离线时首轮档 embed=[] 也被动回落
  纯字面, 注入不炸)。compact 后 count 归零再进首轮档 (预期行为)。

设计裁决 (P2):
- **实体锚定精度门**: 注入只收 prompt 指名实体 (search_entities 命中) 的
  fact。纯 value 扫描候选全拒 — 长 prompt 稀释 bigram 会命中任意 value
  (实测跨项目噪声 0.44 分霸榜)。无锚定实体 → 跳过整个 recall (零 DB 写)。
  跨项目 KB 1205/1206 fact source_cwd=NULL → cwd 过滤无区分度, 不用。
  v1.7③ 契约放行: 锚定命中 **or** ``fact.gate_keep`` (首轮档单 LLM gate
  判 keep 的 B 翼 fact — gate 放行免锚, 键缺席时行为=现状)。
- **LIF 强化记账 (精确, v1.7④⑤ A2/E9 分账)**: recall 调用 boost=False
  (recall 内建 boost 会对全部返回候选记账 — 未注入不该强化), 注入器只对
  **最终注入**的 ≤top_k 条记账 — 注入即使用 (ADR-8v2 反馈环)。分账语义:
  每条统一吸收 ``recall_sessions`` 观测集; fallback 来源 (extractor=regex)
  与 D6 解锁期待验证 fact **只写观测集** — 不刷 LIF 列/access_count/
  last_accessed (受限刷: 注入面不看 MEM_DELAYED_REINFORCE 的直刷洞一并堵,
  E9 双口全堵)。
- **不建 mem-*.md**: 不传 cwd/mem_dir → mem_dir=None, 投影物化归
  SessionStart hook (synthesis-index 单点, 09-01 终裁A方案), 注入面保持
  只读+LIF 记账。
- **query 截断**: prompt 前 N 字符 (默认 800) 作 query — 粘贴长文档不
  爆 token 扫描; CJK bigram 切分见 scoring.query_tokens。
- **无命中零输出** (stdout 空 = CC 无感知); 任何异常 → data/hook-recall.log
  一行, exit 0 静默 — 注入是增强, 绝不阻塞 prompt。

env: MEM_RECALL_MIN_SCORE (默认 0.05 — 长 prompt match 稀释自校准; 短 query
地板 0.3 是 recall 内部默认) /
MEM_RECALL_TOP_K (8) / MEM_RECALL_PER_ANCHOR (3) / MEM_RECALL_CAND_K (50;
常驻档候选窗) / MEM_RECALL_MAX_BYTES (2048) /
MEM_RECALL_QUERY_CHARS (800) /
MEM_RECALL_FIRST_TURNS (1; <=0 = 关闭首turn窗口, 永远常驻档) /
MEM_RECALL_FIRST_TOPK (50; 首轮档候选窗, 取 max(CAND_K, 本值))。
(旧 MEM_RECALL_USE_VEC 不再治理钩子路 — 档位逻辑接管: 首轮档恒融合,
常驻档恒零嵌入。)

出端打标 (2026-08-28): additionalContext 整体包裹 ``<memsvc-recall>…``
</memsvc-recall>`` 标记块 (预算扣除包裹开销 32B) — 重进语料时 corpus_prep
COMMON 规则整块丢弃, 召回回声不重入库。
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
    """台账行带 [pid=N argv0] 溯源前缀 (A1-RW-001-F1): 区分 pytest 进程与
    hook 子进程写入 — A1 排查曾因 pytest 夹具写的行无溯源被误读为桥 spawn
    故障 (findings: ~/.dsh/maestro/state/tickets/A1-RW-001-findings.md)。"""
    try:
        argv0 = Path(sys.argv[0]).name if sys.argv[0] else "?"
        log = SVC_DIR / "data" / "hook-recall.log"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} "
                     f"[pid={os.getpid()} {argv0}] {msg}\n")
    except Exception:
        pass  # 日志失败也不挡 prompt


def _probe_rw() -> bool:
    """A1 降级探测 (2026-09-01): hook 上下文 DB 可能只读 — fs 写与 sqlite 写
    分开探, 区分 会话沙箱 (fs FAIL) 与 sqlite/WAL 态 (fs ok + sqlite FAIL)。
    不可写 → False (调用方跳过 LIF 记账, 注入照常); 异常落台账供鉴别。
    探测写 = 真库上建/删临时表 (零残留); fs 写 = data/.rw_probe 一字节。"""
    fs_ok = True
    try:
        p = SVC_DIR / "data" / ".rw_probe"
        p.write_text("1")
        p.unlink()
    except Exception:
        fs_ok = False
    try:
        import db
        conn = db.get_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS _rw_probe (k TEXT)")
        conn.execute("DROP TABLE IF EXISTS _rw_probe")
        conn.commit()
        return True
    except Exception as exc:
        try:
            import db as _dbmod
            db_src = getattr(_dbmod, "__file__", "?")
        except Exception:
            db_src = "?"
        _log_fail(f"rw-probe: fs={'ok' if fs_ok else 'FAIL'} "
                  f"sqlite=FAIL ({type(exc).__name__}: {exc}) db={db_src} "
                  f"→ 记账降级, 注入继续")
        return False


def _count_user_turns_dsh(path: Path, limit: int) -> int:
    """dsh session.jsonl(.zstd) 语义计数 (A3-T1): type=="user/message" 的
    ``data.content`` text 块 (注意与 assistant/message 的 data.message.content
    不同形), ``transcripts._dsh_open`` 行迭代 (zstdcat 子进程, 复用 memsvc
    既有 zstd 读取, 无静默降级红线) → ``corpus_prep.clean(txt, "dsh")`` 非空
    才计 (注入块剥除防自计数)。``delegationDepth>0`` = 侧链会话 → 不计
    (照 transcripts._dsh_end_steps 先例)。**早停**同 CC: count 达 limit 即返。"""
    import corpus_prep
    import transcripts
    n = 0
    depth = 0
    for line in transcripts._dsh_open(path):
        try:
            d = json.loads(line)
        except Exception:
            continue  # 坏行跳过 (半写行容错)
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "session":
            depth = d.get("delegationDepth", 0) or 0
        elif t == "user/message":
            if depth > 0:
                continue  # 侧链回合不进窗口计数
            data = d.get("data")
            data = data if isinstance(data, dict) else {}
            txt = transcripts._texts_of(data.get("content"))
            if corpus_prep.clean(txt, "dsh"):
                n += 1
                if n >= limit:
                    return n
    return n


def _count_user_turns(path: str | None, limit: int) -> int | None:
    """transcript 里已落盘的既往 user_text 块数 (v1.7 ② turn 判据)。

    双格式分流 (A3-T1): dsh 桥 UPS payload 的 transcript_path 指向
    ``session.jsonl.zstd`` (zstd 压缩) → ``_count_user_turns_dsh``; CC 纯文本
    路径: 逐行 json.loads(坏行跳) → type=="user" 且非 isSidechain →
    ``transcripts._texts_of`` (只收 text 块, tool_result/thinking 天然排除)
    → ``corpus_prep.clean(txt, "cc")`` 非空才计 (system-reminder /
    ``<memsvc-recall>`` 注入块剥除后计, 防注入自计数)。**早停**: count 达
    ``limit`` 即返回, 不读全文件 (大 transcript 超时防线)。任何异常 → None
    = 调用方静默降级常驻档 (fail-open, 不挡路)。
    """
    if not path or limit <= 0:
        return None
    try:
        if Path(path).suffix == ".zstd":
            return _count_user_turns_dsh(Path(path), limit)
        import corpus_prep
        import transcripts
        n = 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue  # 坏行跳过 (半写行容错)
                if not isinstance(d, dict) or d.get("type") != "user" \
                        or d.get("isSidechain"):
                    continue
                msg = d.get("message")
                msg = msg if isinstance(msg, dict) else {}
                txt = transcripts._texts_of(msg.get("content"))
                if corpus_prep.clean(txt, "cc"):
                    n += 1
                    if n >= limit:
                        return n
        return n
    except Exception:
        return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # 非 JSON payload → 静默
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return 0
    session_id = payload.get("session_id") or None

    # ── v1.7 ② 首 n turn 召回窗口 ────────────────────────────────────
    # turn 判据 = count < n (依据: 时序终裁, 见头注 — hook 先于 transcript
    # append, hook 瞬间 transcript 只含既往 turn)。transcript 双路径:
    # payload.transcript_path 优先; 缺失时 session_id 反查
    # ~/.claude/projects/<enc(cwd)>/<sid>.jsonl (存在才用)。计数缺失/失败
    # → None → 常驻档 (fail-open 静默降级, 不挡路)。
    try:
        first_n = int(os.environ.get("MEM_RECALL_FIRST_TURNS", "1"))
    except ValueError:
        first_n = 1
    transcript_path = payload.get("transcript_path") or None
    if not transcript_path and session_id:
        try:
            import transcripts
            cand = transcripts._cc_project_dir(
                payload.get("cwd") or os.getcwd()) / f"{session_id}.jsonl"
            transcript_path = str(cand) if cand.is_file() else None
        except Exception:
            transcript_path = None  # 反查也落空 → 常驻档
    n_turns = _count_user_turns(transcript_path, first_n) \
        if first_n > 0 and transcript_path else None
    first_turn = first_n > 0 and n_turns is not None and n_turns < first_n
    # D2 裁决 (2026-09-01): 注入只在首 n turn 窗口内触发 (n = MEM_RECALL_FIRST_TURNS)。
    # count >= n → 静默早退 (零召回零记账零输出) — 消除桥 pre-step 每模型步的
    # 注入税与 LIF 强化膨胀 (旧规: 窗口外降常驻档照常注入, 已废)。
    # count 未知 (transcript 缺失/失败) → fail-open 常驻档不变 (不挡路契约)。
    if n_turns is not None and n_turns >= first_n:
        return 0

    min_score = float(os.environ.get("MEM_RECALL_MIN_SCORE", "0.05"))
    top_k = int(os.environ.get("MEM_RECALL_TOP_K", "8"))
    per_anchor_quota = int(os.environ.get("MEM_RECALL_PER_ANCHOR", "3"))
    cand_k = int(os.environ.get("MEM_RECALL_CAND_K", "50"))
    max_bytes = int(os.environ.get("MEM_RECALL_MAX_BYTES", "2048"))
    query_chars = int(os.environ.get("MEM_RECALL_QUERY_CHARS", "800"))
    # 档位: 首轮档 use_vec=1 + 候选窗提升 (max(CAND_K, FIRST_TOPK), 接
    # per-anchor 配额逻辑上游); 常驻档实体锚定零嵌入 — use_vec=False 把
    # recall 嵌入路全关 (embedding 离线也零依赖, A 路照常)。
    use_vec = bool(first_turn)
    if first_turn:
        try:
            first_topk = int(os.environ.get("MEM_RECALL_FIRST_TOPK", "50"))
        except ValueError:
            first_topk = 50
        cand_k = max(cand_k, first_topk)

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
        # v1.7③ 契约: 首轮档传 use_gate=True (B 翼+单 LLM gate 绑定首轮档,
        # 15s 含 gate 往返); 常驻档不传 (与 use_vec 分档同处, 纯 A 路)。
        # ③车道参数面缺席时自动省略该键 (键缺席=现状, 不依赖③先行)。
        recall_kw = dict(session_id=session_id, top_k=cand_k, boost=False,
                         with_tag=True, use_vec=use_vec, min_score=min_score)
        if first_turn:
            try:
                import inspect as _inspect
                if "use_gate" in _inspect.signature(cli.recall).parameters:
                    recall_kw["use_gate"] = True
                    recall_kw["gate_account"] = True  # F1 b) 授权一行: 首轮档 keep 入 N2 账
            except (TypeError, ValueError):
                pass
        result = cli.recall(query, **recall_kw)
    except Exception as exc:  # 召回失败 → 零注入 + 记日志 (不降级, 不挡路)
        _log_fail(f"recall-fail: {type(exc).__name__}: {exc}")
        return 0

    results = result.get("results", []) if isinstance(result, dict) else []
    # 锚定门放行 (v1.7③ 契约): 过滤条件从「锚定命中」扩为「锚定命中 or
    # fact.gate_keep」— 首轮档单 LLM gate 判 keep 的 B 翼 fact 免锚入场
    # (键缺席时 get() 返 None → 行为=现状)。
    candidates = [
        r for r in results if float(r.get("score", 0.0)) >= min_score
        and (r.get("fact", {}).get("subject_id") in anchor_ids
             or r.get("fact", {}).get("object_id") in anchor_ids
             or r.get("fact", {}).get("gate_keep"))
    ]
    # 每锚实体配额: 长 prompt 下 value 词重叠多的实体 (LLM) 会霸榜, 把
    # 低匹配但被指名实体的关键 fact (sqlite-vec 依赖关系) 挤出 top_k。
    # 分数序遍历 + 单锚配额 → 每个 prompt 指名实体都有代表。
    # gate_keep fact 不占锚配额 (gate 放行=独立入场券, 无锚可挂)。
    per_anchor: dict[str, int] = {}
    hits = []
    for r in candidates:  # recall 已按 score 降序
        f = r.get("fact") or {}
        gate_keep = bool(f.get("gate_keep"))
        a = f.get("subject_id") if f.get("subject_id") in anchor_ids \
            else f.get("object_id")
        if not gate_keep:
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

    # v1.7 E9/⑤a 注入端统一分账: 凡注入皆记 recall_sessions (记忆被使用过,
    # 无论来源通道); LIF 强化 (access_count+1 / seen_sessions 吸收 / LIF
    # 重算) 只给非受限 fact — fallback 产物与待验证暂缓期 fact 不因被召回
    # 白得强化 (E9 分账)。逐条尽力而为, 单条失败不挡注入。
    # A1 降级 (2026-09-01 裁决): hook 上下文 DB 可能只读 (readonly database,
    # 疑会话沙箱/WAL 态) → 探测写能力, 不可写则整段跳过记账 (零 boost-fail
    # 噪声), 注入照常 — 召回是纯读, 不因写缺失而放弃。异常落台账兼作
    # fs(沙箱)/sqlite(WAL) 鉴别信号。
    import scoring
    db_ok = _probe_rw()
    conn = None
    if db_ok:
        try:
            import db
            conn = db.get_conn()
        except Exception:
            conn = None
    for r in hits:
        if not db_ok:
            break
        f = r.get("fact") or {}
        try:
            scoring.record_recall_observation(
                f.get("id"), session_id=session_id, conn=conn)
            if not scoring.refresh_restricted(f):
                scoring.refresh_lif_on_recall(
                    f.get("id"), session_id=session_id, conn=conn,
                    match_score=f.get("match_score"))
        except Exception as exc:
            _log_fail(f"boost-fail: {type(exc).__name__}: {exc}")

    # ⑤a 降级标注: fallback 产物 (extractor==regex) 混入注入块时向读者
    # 显式声明其未经主径 LLM 验证 — 全降级块打 quality="fallback" + 块顶
    # 警示; 混合块在每条 fallback 条目前插警示行。
    _FALLBACK_WARN = "产生于降级通道、未经主径 LLM 验证，需自行判断召回准确性"
    all_fallback = all(
        scoring.fact_is_fallback((r.get("fact") or {})) for r in hits)
    lines = [f"## Memory recall (auto, {len(hits)} hits)"]
    if all_fallback:
        lines.append(f"[warning] 以下各条均{_FALLBACK_WARN}")
    # 预算含 <memsvc-recall> 包裹开销 (ASCII, 全降级块含 quality 属性)
    open_tag = ('<memsvc-recall quality="fallback">' if all_fallback
                else "<memsvc-recall>")
    budget = max_bytes - (len(open_tag) + len("\n\n</memsvc-recall>"))
    emitted = 0
    for r in hits:
        f = r.get("fact") or {}
        tag = r.get("tag") or {}
        display = (tag.get("display") or "").strip() or "?"
        val = (f.get("value") or "").strip()
        if len(val) > 80:
            val = val[:77] + "..."
        entry = f"- {display} — {val}  [{float(r.get('score', 0.0)):.2f}]" if val \
            else f"- {display}  [{float(r.get('score', 0.0)):.2f}]"
        if not all_fallback and scoring.fact_is_fallback(f):
            warn = f"- [warning] 本条{_FALLBACK_WARN}"
            n = len(warn.encode("utf-8"))
            if budget - n >= 0:
                lines.append(warn)
                budget -= n
        n = len(entry.encode("utf-8"))
        if budget - n < 0:
            break
        lines.append(entry)
        budget -= n
        emitted += 1
    if emitted == 0:
        return 0  # 预算内一条都放不下 → 零输出

    # 出端打标 (2026-08-28 闭环): <memsvc-recall> 为 memsvc 自有中性标签 —
    # 非 harness 保留语法, cc/dsh/pi 解析器原样透传 (零适配器), 活会话 LLM
    # 读到即知是召回内容; 语料重入库时 corpus_prep COMMON 规则整块丢弃
    # (防召回回声自我重入库 — 结构层根治, U7 去重只是分数层兜底)。
    ctx = open_tag + "\n" + "\n".join(lines) + "\n</memsvc-recall>"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

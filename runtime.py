"""runtime SDK (L2 业务编排层, 解耦 S4+, 分支 iterate/harness-decouple)。

五业务编排的库面家 — harness 盲: 只认 CC 形 Envelope payload (事实标准,
dsh 桥同形) + env, harness 差异全部经 harness.SPECS (L1) 消解。

分层铁律 (verdict 2026-10-04):
- 本层**可** import cli (22 子命令 = 手动门面) / harness (L1) / 核心层;
- **禁** import mem_daemon / hooks (壳是本层的消费者, 防环);
- ``import cli`` 即加载同目录 .env (``cli._load_env`` 的 setdefault 副作用,
  docstring 声明的既有契约, 平移原位保留)。

入口面:
- ``read_payload()`` — stdin CC 形 JSON 单源收口;
- ``emit_context(ctx, channel)`` — 协议出端 (cc-hook → hookSpecificOutput);
- ``inject_context(payload, env)`` — ④注入业务体 (自 hooks/recall_inject.py
  514 行 main 平移, 判据/预算/记账逐字保留; 早退点统一 return None);
- ``hook_main(argv)`` — 壳分发入口 (``python3 runtime.py <event>``)。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SVC_DIR = Path(__file__).resolve().parent
if str(SVC_DIR) not in sys.path:
    sys.path.insert(0, str(SVC_DIR))


# ── 台账 / 探测 (自 recall_inject 平移) ─────────────────────────────

def _log_fail(msg: str) -> None:
    """台账行带 [pid=N argv0] 溯源前缀 (A1-RW-001-F1): 区分 pytest 进程与
    hook 子进程写入。"""
    try:
        argv0 = Path(sys.argv[0]).name if sys.argv[0] else "?"
        log = SVC_DIR / "data" / "hook-recall.log"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} "
                     f"[pid={os.getpid()} {argv0}] {msg}\n")
    except Exception:
        pass  # 日志失败也不挡 prompt


def _probe_rw() -> bool:
    """A1 降级探测: hook 上下文 DB 可能只读 — fs 写与 sqlite 写分开探。
    不可写 → False (调用方跳过 LIF 记账, 注入照常)。"""
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


# ── turn 计数 (fail-open 壳, 判据单源在 transcripts.count_user_turns) ──

def _count_user_turns(path: str | None, limit: int) -> int | None:
    """fail-open: 任何异常 → None = 常驻档静默降级 (不挡路)。"""
    if not path or limit <= 0:
        return None
    try:
        import transcripts
        return transcripts.count_user_turns(path, limit)
    except Exception:
        return None


def _count_user_turns_dsh(path: Path, limit: int) -> int:
    """薄委托 (符号兼容): 实现在 transcripts._count_dsh_user_turns。"""
    import transcripts
    return transcripts._count_dsh_user_turns(path, limit)


# ── 确定性 lane (偏好/近期日程, 纯 SQL 零 LLM) ───────────────────────

_LANE_MAX_BYTES = 400  # 两确定性 lane 合计帽 (~400B, 超则截行)


def _det_lanes() -> list[str]:
    """确定性 lane (v3, 纯 SQL 零 LLM): 偏好 + 近期日程 — 先于锚定门注入。

    - preference lane: 存活 preference 原子按 p_dur 取 top3。
    - event lane: 存活 event 原子的 event_at 落 now-7d ~ now+21d 窗内才留
      (event_at 是 ISO 或原文短语 — 解析失败/非 ISO 的行跳过, 不猜),
      按距 now 最近取 top3。
    行前缀「偏好:」/「日程:」, 两 lane 合计 ~400B 帽 (超则截行); 任何异常 →
    [] (lane 失败不挡锚定召回, 与注入器整体 fail-open 契约一致)。"""
    try:
        from datetime import datetime, timedelta
        import db
        conn = db.get_conn()
        lines: list[str] = []
        for r in conn.execute(
            "SELECT text, gist FROM atom WHERE label='preference' AND "
            "valid_to IS NULL ORDER BY p_dur DESC LIMIT 3"
        ):
            t = (r["gist"] or r["text"] or "").strip()
            if t:
                lines.append(f"偏好: {t}")
        now = datetime.now().astimezone()
        lo, hi = now - timedelta(days=7), now + timedelta(days=21)
        evs = []
        for r in conn.execute(
            "SELECT text, gist, event_at FROM atom WHERE label='event' AND "
            "valid_to IS NULL AND event_at IS NOT NULL"
        ):
            t = (r["gist"] or r["text"] or "").strip()
            raw = (r["event_at"] or "").strip()
            if not t or not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                continue  # 原文短语/非 ISO → 不猜, 跳行
            if dt.tzinfo is None:
                dt = dt.astimezone()  # naive 按本地时区解读
            if not (lo <= dt <= hi):
                continue  # 窗外 (now-7d ~ now+21d) 事件不注入
            evs.append((abs(dt - now), t, raw))
        evs.sort(key=lambda x: x[0])  # 距 now 最近优先
        for _, t, raw in evs[:3]:
            lines.append(f"日程: {t} — {raw}")
        out: list[str] = []
        budget = _LANE_MAX_BYTES
        for ln in lines:
            n = len(ln.encode("utf-8"))
            if budget - n < 0:
                break  # 总量帽超 → 截行
            out.append(ln)
            budget -= n
        return out
    except Exception:
        return []  # lane 是增强的增强, 任何失败静默降级为空


# ── 协议进出端 ──────────────────────────────────────────────────────

def read_payload() -> dict | None:
    """stdin CC 形 JSON 单源收口 (非 JSON → None, 调用方静默)。"""
    try:
        return json.load(sys.stdin)
    except Exception:
        return None


def emit_context(ctx: str, channel: str = "cc-hook") -> None:
    """协议出端。channel='cc-hook': UserPromptSubmit additionalContext JSON
    (cc+dsh 桥共用); 内容层恒 <memsvc-recall> 中性包裹 (harness 无关)。"""
    if channel != "cc-hook":
        raise ValueError(f"unknown emit channel: {channel!r}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        },
    }, ensure_ascii=False))


# ── ④注入业务体 (自 hooks/recall_inject.py main 平移, S4) ───────────

def inject_context(payload: dict, env=None) -> str | None:
    """UserPromptSubmit 注入: 档位判定 → 确定性 lane → 实体锚定 recall →
    配额/预算裁剪 → LIF 记账。返回 ctx 字符串 (None = 零输出)。

    完整语义见 hooks/recall_inject.py 头注 (判据/红线逐字保留):
    全局单体 KG (ADR-14) / boost=False 注入端分账 / 实体锚定精度门 /
    首 n turn 窗口 (D2 裁决: 窗口外静默早退) / fail-open 全程不挡路。"""
    if env is None:
        env = os.environ
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return None
    session_id = payload.get("session_id") or None

    # ── v1.7 ② 首 n turn 召回窗口 ────────────────────────────────────
    try:
        first_n = int(env.get("MEM_RECALL_FIRST_TURNS", "1"))
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
    # D2 裁决 (2026-09-01): 注入只在首 n turn 窗口内触发。count >= n →
    # 静默早退 (零召回零记账零输出)。count 未知 → fail-open 常驻档。
    if n_turns is not None and n_turns >= first_n:
        return None

    min_score = float(env.get("MEM_RECALL_MIN_SCORE", "0.05"))
    top_k = int(env.get("MEM_RECALL_TOP_K", "8"))
    per_anchor_quota = int(env.get("MEM_RECALL_PER_ANCHOR", "3"))
    cand_k = int(env.get("MEM_RECALL_CAND_K", "50"))
    max_bytes = int(env.get("MEM_RECALL_MAX_BYTES", "2048"))
    query_chars = int(env.get("MEM_RECALL_QUERY_CHARS", "800"))
    # 档位: 首轮档 use_vec=1 + 候选窗提升; 常驻档实体锚定零嵌入。
    use_vec = bool(first_turn)
    if first_turn:
        try:
            first_topk = int(env.get("MEM_RECALL_FIRST_TOPK", "50"))
        except ValueError:
            first_topk = 50
        cand_k = max(cand_k, first_topk)

    query = prompt[:query_chars]
    lane_lines = _det_lanes()
    try:
        import cli  # noqa: F401 — module import 即 _load_env() (.env → ZHIPU 等)
        import recall as recall_mod
        import scoring
        # 实体锚定 (精度门): prompt 字面指名的实体 — search_entities 出
        # 候选, 再验实体全名 ⊆ query (反向包含)。
        ql = query.lower()
        _ent_hits = [
            e for e in recall_mod.search_entities(scoring.query_tokens(query))
            if e["name"].lower() in ql
        ]
        anchor_ids = {e["id"] for e in _ent_hits}
        # v2 atom 面: 锚定等价物 = 锚实体全名出现在 atom.text (文本锚命中)。
        anchor_names = {e["name"] for e in _ent_hits}

        def _atom_anchor(f: dict):
            t = (f.get("text") or "").lower()
            if not t:
                return None
            return next((n for n in anchor_names if n.lower() in t), None)

        if not anchor_ids and not lane_lines:
            return None  # 未指名实体且无 lane → 无可注入
        recall_kw = dict(session_id=session_id, top_k=cand_k, boost=False,
                         with_tag=True, use_vec=use_vec, min_score=min_score)
        if first_turn:
            try:
                import inspect as _inspect
                if "use_gate" in _inspect.signature(cli.recall).parameters:
                    recall_kw["use_gate"] = True
                    recall_kw["gate_account"] = True  # F1 b): 首轮档 keep 入账
            except (TypeError, ValueError):
                pass
        # v3: 无锚定实体 → 锚定召回整段跳过, lane 有货仍纯 lane 注入
        result = cli.recall(query, **recall_kw) if anchor_ids \
            else {"results": []}
    except Exception as exc:  # 召回失败 → 零注入 + 记日志 (不降级, 不挡路)
        _log_fail(f"recall-fail: {type(exc).__name__}: {exc}")
        return None

    results = result.get("results", []) if isinstance(result, dict) else []
    # 锚定门放行: 锚定命中 or gate_keep (v1.7③ 契约)。
    candidates = [
        r for r in results if float(r.get("score", 0.0)) >= min_score
        and (r.get("fact", {}).get("subject_id") in anchor_ids
             or r.get("fact", {}).get("object_id") in anchor_ids
             or r.get("fact", {}).get("gate_keep")
             or _atom_anchor(r.get("fact") or {}))
    ]
    per_anchor: dict[str, int] = {}
    hits = []
    for r in candidates:  # recall 已按 score 降序
        f = r.get("fact") or {}
        gate_keep = bool(f.get("gate_keep"))
        a = f.get("subject_id") if f.get("subject_id") in anchor_ids \
            else (f.get("object_id") if f.get("object_id") in anchor_ids
                  else _atom_anchor(f))
        if not gate_keep:
            if a is None:
                continue
            if per_anchor.get(a, 0) >= per_anchor_quota:
                continue
            per_anchor[a] = per_anchor.get(a, 0) + 1
        hits.append(r)
        if len(hits) >= top_k:
            break
    if not hits and not lane_lines:
        return None

    # E9/⑤a 注入端统一分账 (受限 fact 只记观测不强化); A1 降级: DB 只读 →
    # 探测后整段跳过记账, 注入照常。
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

    _FALLBACK_WARN = "产生于降级通道、未经主径 LLM 验证，需自行判断召回准确性"
    all_fallback = bool(hits) and all(
        scoring.fact_is_fallback((r.get("fact") or {})) for r in hits)
    lines = [""]   # 头行占位 — 按 emitted 实际行数
    if all_fallback:
        lines.append(f"[warning] 以下各条均{_FALLBACK_WARN}")
    open_tag = ('<memsvc-recall quality="fallback">' if all_fallback
                else "<memsvc-recall>")
    budget = max_bytes - (len(open_tag) + len("\n\n</memsvc-recall>"))
    emitted = 0
    lane_emitted = 0
    for ln in lane_lines:  # lane 行先入 (偏好/日程钉块首)
        n = len(ln.encode("utf-8"))
        if budget - n < 0:
            break
        lines.append(ln)
        budget -= n
        emitted += 1
        lane_emitted += 1
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
        return None  # 预算内一条都放不下 → 零输出
    n_hits_emitted = emitted - lane_emitted
    lines[0] = f"## Memory recall (auto, {n_hits_emitted + lane_emitted} hits)"

    ctx = open_tag + "\n" + "\n".join(lines) + "\n</memsvc-recall>"
    return ctx


# ── 壳分发入口 (python3 runtime.py <event>) ─────────────────────────

def hook_main(argv: list[str] | None = None) -> int:
    """hooks 壳共用的分发入口 — 恒 exit 0 (增强面绝不阻塞 hook 事件)。

    events: ``user-prompt`` (④注入; S5/S6 步序再补 precompact/session-start)。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    event = argv[0] if argv else ""
    if event == "user-prompt":
        payload = read_payload()
        if payload is not None:
            ctx = inject_context(payload)
            if ctx:
                emit_context(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(hook_main())

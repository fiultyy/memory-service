"""v1.7 ② 首 n turn 召回窗口测试 — turn 判据/双档/嵌入开关/降级/早停/计数过滤。

依据 (09-01 终裁, 已写入 recall_inject 头注): CC 2.1.179 二进制静态代码序 —
UserPromptSubmit hooks 在 transcript append **之前**执行 → hook 触发时
transcript 只含既往 turn → **turn 判据 = count < n** (count = 已落盘既往
user_text 块数, 首 turn count=0)。

覆盖:
1. 首 turn (count=0<n) → 首轮档: use_vec=True + FIRST_TOPK 候选窗, 命中含
   向量融合 (fact 基础分 < min_score, 唯一靠 δ·vec_sim 越地板 → 命中即融合
   证明; embedding stub 照 test_bfs_recall._patch_embed_deterministic 先例)。
2. **D2 裁决 (2026-09-01): count >= n → 窗口外静默早退 (零召回零记账零输出;
   旧规"常驻档照常注入"已废)** — 早退发生在锚定/召回之前 (cli.recall 零调用)。
3. transcript 缺失 / session_id 反查落空 → count 未知 → fail-open 常驻档
   (不炸, 零嵌入, 不挡路契约)。
4. MEM_RECALL_FIRST_TURNS=0 → n_turns 恒 None → fail-open 常驻档 (窗口关闭)。
5. 计数过滤: tool_result 块 / isSidechain / <memsvc-recall> 注入块 / 坏行 /
   assistant 条目均不计入。
6. 早停: count 达 n 即返回, 不读全文件 (open 行计数器断言)。
7. 窗口边界: count = n-1 仍首轮档 (use_vec=True)。

fixture 风格照 test_recall_inject_marker.py (monkeypatch/StringIO); KG 用
db.init(tmp) 隔离, embedding 全 stub — 零网络零 LLM。
"""
import builtins
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "hooks"))

import db
import recall_inject as ri

PROMPT = "专家职位 的结论是什么"
QUERY = PROMPT[:800]  # MEM_RECALL_QUERY_CHARS 默认 800 → query 即原文


def _payload(prompt=PROMPT, session_id="s1", cwd="/tmp/fake-proj",
             transcript_path=None):
    d = {"prompt": prompt, "session_id": session_id, "cwd": cwd}
    if transcript_path is not None:
        d["transcript_path"] = transcript_path
    return json.dumps(d, ensure_ascii=False)


def _user_line(text, sidechain=False):
    d = {"type": "user", "message": {"role": "user", "content": text}}
    if sidechain:
        d["isSidechain"] = True
    return json.dumps(d, ensure_ascii=False)


def _write_transcript(path: Path, lines) -> str:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _patch_embed_deterministic(monkeypatch) -> dict:
    """确定性 embed stub (先例 test_bfs_recall._patch_embed_deterministic):
    query 与 fact 值 "rust memory safety ..." 同向 (cosine=1.0), 其余正交。
    返回调用计数 dict (常驻档断言零调用的探针)。"""
    import embedding
    import vec_index as vi
    calls = {"n": 0}
    vec = [1.0, 0.0] + [0.0] * (vi.VEC_DIM - 2)  # pad 到索引维度

    def fake_embed(text, providers=None):
        calls["n"] += 1
        if text == QUERY or "rust memory safety" in (text or ""):
            return vec
        return [0.0, 1.0]  # 其余: 正交 → cosine 0.0

    monkeypatch.setattr(embedding, "embed", fake_embed)
    return calls


def _mk_vec_only_kg(tmp_path) -> str:
    """tmp KG: 实体「专家职位」+ fact(值与 query 零词面重叠, LIF/conf 极低,
    object_id=None → 无图边 → centrality 0)。

    基础分 = α·0(match) + β·0(centrality) + γ·0.2·mem_score(0.01) ≈ 0.002
    < MEM_RECALL_MIN_SCORE 0.05 → 常驻档(零嵌入)必零命中; 首轮档 +δ·vec_sim
    (1.0)=0.3 → ≈0.302 ≥ 0.05 → 唯一靠向量融合存活。返回 fact_id。"""
    db.init(tmp_path / "mem.db")
    import store
    eid = store.put_entity("专家职位", "inferred")
    fid = store.put_fact(eid, "uses", "rust memory safety semantics",
                         extractor="llm", fact_type="permanent",
                         LIF=0.01, confidence=0.01)
    return fid


def _spy_recall(monkeypatch) -> dict:
    """包一层 cli.recall 记录 kwargs (use_vec/top_k 档位断言), 委托真 recall。"""
    import cli
    rec = {"kwargs": []}
    real = cli.recall

    def spy(query, **kw):
        rec["kwargs"].append(kw)
        return real(query, **kw)

    monkeypatch.setattr(cli, "recall", spy)
    return rec


def _spy_refresh(monkeypatch) -> list:
    """记账探针: 注入面 refresh_lif_on_recall 语义/时机回归锚 (只对最终注入条)。"""
    import scoring
    calls = []
    monkeypatch.setattr(scoring, "refresh_lif_on_recall",
                        lambda fid, **k: calls.append(fid))
    return calls


def _run_main(monkeypatch, payload: str) -> str:
    # A1-RW-001-F1: 日志路径统一隔离 — main 内 probe/boost 任何台账写入落
    # capture, 绝不污染真实 data/hook-recall.log (A1 台账误诊教训)。
    logged: list[str] = []
    monkeypatch.setattr(ri, "_log_fail", logged.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ri.main() == 0
    return out.getvalue()


# ── 1. 首 turn (count=0<n): 首轮档 use_vec=1 + 候选窗提升 + 向量融合命中 ──
def test_first_turn_vec_fusion_tier(tmp_path, monkeypatch):
    calls = _patch_embed_deterministic(monkeypatch)
    fid = _mk_vec_only_kg(tmp_path)
    rec = _spy_recall(monkeypatch)
    refresh = _spy_refresh(monkeypatch)
    tpath = _write_transcript(tmp_path / "t.jsonl", [])  # 空 transcript → count=0
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    monkeypatch.setenv("MEM_RECALL_FIRST_TOPK", "80")
    raw = _run_main(monkeypatch, _payload(transcript_path=tpath))
    assert raw, "首轮档: 向量融合应让 fact 越过 0.05 地板命中"
    ctx = json.loads(raw)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("<memsvc-recall>") and ctx.endswith("</memsvc-recall>")
    assert "rust memory safety" in ctx, f"值应注入:\n{ctx}"
    assert "[0.30]" in ctx, f"融合分 ≈0.302 (0.002+δ·1.0), 实际:\n{ctx}"
    kw = rec["kwargs"][0]
    assert kw["use_vec"] is True, "首轮档必须开向量融合"
    assert kw["top_k"] == 80, "首轮档候选窗 = max(CAND_K, FIRST_TOPK)"
    assert calls["n"] >= 1, "query embed 应真的跑过 (融合路径在场)"
    assert refresh == [fid], "记账语义不变: 只对最终注入条 refresh"


# ── 2. count>=n: 窗口外静默早退 (D2 裁决) — 零输出且 cli.recall 零调用 ────
def test_beyond_window_exits_before_recall(tmp_path, monkeypatch):
    calls = _patch_embed_deterministic(monkeypatch)
    _mk_vec_only_kg(tmp_path)
    calls["n"] = 0  # put_fact 入库预热会 embed 值 — 探针只量 recall 路径
    rec = _spy_recall(monkeypatch)
    # 1 条既往 user turn; n 默认 1 → count=1 >= n → 早退
    tpath = _write_transcript(tmp_path / "t.jsonl",
                              [_user_line("早前一轮真人原话, 语气自然")])
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    raw = _run_main(monkeypatch, _payload(transcript_path=tpath))
    assert raw == "", "窗口外: 零输出"
    assert rec["kwargs"] == [], "窗口外: cli.recall 不得被调用 (早退在锚定之前)"
    assert calls["n"] == 0, "窗口外: 嵌入调用必须为 0"


# ── 2b. 窗口边界: count = n-1 仍在窗内 → 首轮档 (use_vec=True) ────────────
def test_window_boundary_count_n_minus_1_still_first_tier(tmp_path, monkeypatch):
    calls = _patch_embed_deterministic(monkeypatch)
    _mk_vec_only_kg(tmp_path)
    calls["n"] = 0  # 入库预热 embed 不算 — 只量 recall 路径
    rec = _spy_recall(monkeypatch)
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    monkeypatch.setenv("MEM_RECALL_FIRST_TURNS", "2")
    tpath = _write_transcript(tmp_path / "t.jsonl",
                              [_user_line("早前一轮真人原话")])  # count=1 < n=2
    raw = _run_main(monkeypatch, _payload(transcript_path=tpath))
    assert raw, "count=1 < n=2: 仍在窗内 → 首轮档向量融合命中"
    assert rec["kwargs"][0]["use_vec"] is True


# ── 3a. payload 无 transcript_path 且反查落空 → 静默降级常驻档 (fail-open) ──
def test_missing_transcript_degrades_to_resident(tmp_path, monkeypatch):
    calls = _patch_embed_deterministic(monkeypatch)
    _mk_vec_only_kg(tmp_path)
    calls["n"] = 0  # 入库预热 embed 不算 — 只量 recall 路径
    rec = _spy_recall(monkeypatch)
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    # 不带 transcript_path; cwd 指向不存在反查目录 → count 不可得 → 常驻档
    raw = _run_main(
        monkeypatch,
        _payload(session_id="laneh-missing-probe", transcript_path=None))
    assert raw == "" and calls["n"] == 0
    assert rec["kwargs"][0]["use_vec"] is False, "降级常驻档, 不炸不挡路"


# ── 3b. session_id 反查命中 (~/.claude/projects/<enc(cwd)>/<sid>.jsonl) ────
def test_transcript_reverse_lookup_via_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # Path.home() → tmp (隔离反查)
    calls = _patch_embed_deterministic(monkeypatch)
    _mk_vec_only_kg(tmp_path)
    rec = _spy_recall(monkeypatch)
    import transcripts
    enc = transcripts._cc_project_dir("/proj/x")  # /proj/x → -proj-x (假 HOME 下)
    enc.mkdir(parents=True, exist_ok=True)
    (enc / "sess-rev.jsonl").write_text("", encoding="utf-8")  # 空 → count=0
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    raw = _run_main(monkeypatch, _payload(session_id="sess-rev", cwd="/proj/x"))
    assert raw, "反查命中空 transcript → count=0<n → 首轮档"
    assert rec["kwargs"][0]["use_vec"] is True
    assert calls["n"] >= 1


# ── 4. MEM_RECALL_FIRST_TURNS=0 → 窗口关闭, 永远常驻档 ──────────────────
def test_first_turns_zero_disables_window(tmp_path, monkeypatch):
    calls = _patch_embed_deterministic(monkeypatch)
    _mk_vec_only_kg(tmp_path)
    calls["n"] = 0  # 入库预热 embed 不算 — 只量 recall 路径
    rec = _spy_recall(monkeypatch)
    tpath = _write_transcript(tmp_path / "t.jsonl", [])  # count=0, 若开会进首轮
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    monkeypatch.setenv("MEM_RECALL_FIRST_TURNS", "0")
    raw = _run_main(monkeypatch, _payload(transcript_path=tpath))
    assert raw == "" and calls["n"] == 0
    assert rec["kwargs"][0]["use_vec"] is False, "n<=0 = 关闭首turn窗口"


# ── 5. 计数过滤: tool_result/isSidechain/<memsvc-recall>/坏行/assistant 不计 ─
def test_count_user_turns_filters_non_user_text(tmp_path):
    t = tmp_path / "t.jsonl"
    lines = [
        _user_line("真人第一句, 这是一句足够长的用户原话"),
        json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "工具输出, 不是用户原话"}]}]}}),
        _user_line("侧链条目不应计数", sidechain=True),
        _user_line("<memsvc-recall>\n## Memory recall (auto, 2 hits)\n"
                   "- x — y\n</memsvc-recall>"),  # 注入块整块剥除 → 不计
        "{bad json line",                                  # 坏行跳过
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant", "content": "回答文本"},
                    "stop_reason": "end_turn"}),
        _user_line("真人第二句, 又一句足够长的用户原话"),
    ]
    _write_transcript(t, lines)
    assert ri._count_user_turns(str(t), 10) == 2, "只有真人 text 块计数"


def test_count_user_turns_missing_and_error(tmp_path):
    assert ri._count_user_turns(None, 1) is None
    assert ri._count_user_turns(str(tmp_path / "nope.jsonl"), 1) is None
    assert ri._count_user_turns(str(tmp_path), 1) is None  # 目录 → 异常 → None


# ── 6. 早停: count 达 n 即返回, 不读全文件 (open 行计数器断言) ───────────
def test_count_user_turns_early_stop(tmp_path, monkeypatch):
    lines = [_user_line("第一句真人原话, 足够长")]
    lines += [json.dumps({"type": "assistant",
                          "message": {"role": "assistant",
                                      "content": f"填充行 {i}"},
                          "stop_reason": "end_turn"})
              for i in range(5000)]
    t = tmp_path / "big.jsonl"
    _write_transcript(t, lines)
    real_open = builtins.open
    seen = {"lines": 0}

    def counting_open(file, mode="r", *args, **kwargs):
        fh = real_open(file, mode, *args, **kwargs)
        if str(file) == str(t) and "r" in mode:
            class _Wrap:
                def __iter__(self):
                    for ln in fh:
                        seen["lines"] += 1
                        yield ln

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return fh.__exit__(*exc)

                def close(self):
                    fh.close()

            return _Wrap()
        return fh

    monkeypatch.setattr(builtins, "open", counting_open)
    assert ri._count_user_turns(str(t), 1) == 1
    assert seen["lines"] == 1, f"早停: 应只读 1 行, 实读 {seen['lines']} 行"


# ── 7. A3-T1: dsh session.jsonl.zstd 窗口计数 (桥 UPS payload transcript_path) ─
#
# 桥探针结论 (hooks-claude-code lib/index.js:350-357 base): dsh 桥 UPS payload
# 恒携带 transcript_path = sessionPersistence locate 路径
# (~/.dsh/sessions/<enc>/session-<uuid>/session.jsonl.zstd), 另有 session_id/cwd。
# 旧码按 CC 纯文本读 zstd → 恒 count=0 → D2 窗口门 dsh 侧永不早退。


def _dsh_event(t, seq, text=None, kind="user/message", extra=None):
    d = {"type": kind, "seq": seq}
    if kind == "user/message":
        content = [{"type": "text", "text": text}] if text is not None \
            else [{"type": "toolCall", "toolCall": {"name": "Bash"}}]
        d["data"] = {"content": content, "role": "user"}
    elif kind == "assistant/message":
        d["data"] = {"message": {"role": "assistant",
                                 "content": [{"type": "text", "text": "回答"}]}}
    elif kind == "turn/end":
        d["data"] = {"reason": {"kind": "completed"}}
    elif kind == "session":
        d.update({"version": 0, "id": "session-x", "cwd": "/p"})
        d["delegationDepth"] = extra if extra is not None else 0
    if extra is not None and kind != "session":
        d.update(extra)
    return json.dumps(d, ensure_ascii=False)


def _write_dsh_zstd(tmp_path, lines) -> str:
    import shutil
    import subprocess
    if not shutil.which("zstd"):
        pytest.skip("zstd 未安装")
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    comp = subprocess.run(["zstd", "-q", "-c"], input=raw,
                          capture_output=True, check=True)
    p = tmp_path / "session.jsonl.zstd"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(comp.stdout)
    return str(p)


def _dsh_base_lines(depth=0):
    return [_dsh_event(None, 0, kind="session", extra=depth)]


def test_dsh_zstd_counts_user_message_texts(tmp_path):
    lines = _dsh_base_lines() + [
        _dsh_event(None, 1, text="<memsvc-recall>\n注入块\n</memsvc-recall>"),
        _dsh_event(None, 2),  # 纯 toolCall 块 → 不计
        _dsh_event(None, 3, text="第一轮真人原话, 足够长"),
        _dsh_event(None, 4, kind="assistant/message"),
        _dsh_event(None, 5, kind="turn/end"),
        _dsh_event(None, 6, text="第二轮真人原话, 足够长"),
        _dsh_event(None, 7, text="第三轮真人原话, 足够长"),
    ]
    t = _write_dsh_zstd(tmp_path, lines)
    assert ri._count_user_turns(t, 100) == 3, "只计 user/message 真人 text 块"
    assert ri._count_user_turns(t, 1) == 1, "早停: 达 limit 即返"
    assert ri._count_user_turns(t, 2) == 2


def test_dsh_zstd_sidechain_depth_excluded(tmp_path):
    lines = _dsh_base_lines(depth=1) + [
        _dsh_event(None, 1, text="侧链回合原话"),
        _dsh_event(None, 2, text="侧链第二句"),
    ]
    t = _write_dsh_zstd(tmp_path, lines)
    assert ri._count_user_turns(t, 10) == 0, "delegationDepth>0 = 侧链不计"


def test_dsh_window_gate_exits_after_first_turn(tmp_path, monkeypatch):
    """D2 门 dsh 侧生效证据: zstd transcript 已有 1 轮真人 turn → count>=n=1
    → 静默早退 (零召回零输出); count=0 空会话 → 窗内照常召回。"""
    calls = _patch_embed_deterministic(monkeypatch)
    _mk_vec_only_kg(tmp_path)
    calls["n"] = 0
    rec = _spy_recall(monkeypatch)
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    past = _write_dsh_zstd(tmp_path / "past", [
        _dsh_event(None, 0, kind="session"),
        _dsh_event(None, 1, text="早前一轮真人原话, 足够长"),
    ])
    raw = _run_main(monkeypatch, _payload(transcript_path=past))
    assert raw == "", "窗口外: 零输出"
    assert rec["kwargs"] == [], "窗口外: cli.recall 不得被调用 (D2 门 dsh 侧生效)"
    assert calls["n"] == 0, "窗口外: 嵌入调用为 0"
    # 对照: 空 zstd transcript (count=0 < n) → 窗内, recall 照常进
    empty = _write_dsh_zstd(tmp_path / "empty", [_dsh_event(None, 0, kind="session")])
    raw2 = _run_main(monkeypatch, _payload(transcript_path=empty))
    assert rec["kwargs"] and rec["kwargs"][0]["use_vec"] is True, \
        "count=0<n: 首 turn 窗内照常召回 (fail-open 不受损)"

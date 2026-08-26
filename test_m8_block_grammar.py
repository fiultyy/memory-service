"""M8 块文法 reader 批验收测试 (spec v2 §2 M8 + M8-v2: S1/G2/N4)。

覆盖派发令四条验收:
1. 块文法单测: user(text)/assistant(text)/assistant(tool_use)/user(tool_result)/
   混合 list 块 fixture, 断言 _read_transcript 返回 (block_type, text) 序列正确、
   tool_result 文本不丢 (S1)。
2. 分段归因: monkeypatch extract_facts (禁网络/LLM), 断言 fact 的 provenance 按
   段正确落列 (四类块各至少一例 + system 档), veracity 随 M3 映射正确 (G2)。
3. 段预算: 超长 tool_result 段截到预算内; 总文本量超旧 4000 上限的 transcript
   不再被整体平截, 后段内容仍被处理 (N4)。
4. 幂等不回归: 同 transcript 重跑全 NOOP 契约保持 (四分支行为不变)。

测试规范: def test_xxx() 函数让 pytest 收集 (本项目头号雷区=模块级裸 assert 死代码)。
"""
import json
import tempfile
from pathlib import Path

import adapter
import autodream
import db
import embedding
from llm_provider import EdgeOut, EntityOut, Extraction


def _write_transcript(path: Path, records: list[dict], tail: str = "") -> None:
    lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    path.write_text(lines + "\n" + tail, encoding="utf-8")


# ── 验收 1: 块文法 — (block_type, text) 序列, tool_result 不丢 (S1) ────

def test_read_transcript_block_sequence():
    """全类型 fixture: 裸字符串 content / text 块 / tool_use / tool_result(str+list)/
    thinking / 未知记录类型 / 坏行 — 序列与取值逐项断言。"""
    tmp = Path(tempfile.mkdtemp())
    tpath = tmp / "session.jsonl"
    records = [
        {"type": "user", "message": {"content": "plain user string"}},
        {"type": "assistant", "message": {"content": "plain assistant string"}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "user block text"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "assistant says"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cargo build"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "cargo build ok in 3s"},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [
                {"type": "text", "text": "line1"},
                {"type": "image", "source": {"kind": "base64"}},   # 无文本 item → 跳过
                "bare string item",
                {"content": "nested content str"},
                {"type": "text"},                                   # 空 text → 跳过
            ]},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hmm, let me think"}]}},
        {"type": "summary", "message": {"content": "not user/assistant"}},  # 记录类型跳过
    ]
    _write_transcript(tpath, records, tail="this is not json\n")

    blocks = autodream._read_transcript(tpath)
    assert blocks == [
        ("user_text", "plain user string"),
        ("assistant_text", "plain assistant string"),
        ("user_text", "user block text"),
        ("assistant_text", "assistant says"),
        ("tool_use", '{"command": "cargo build"}'),
        ("tool_result", "cargo build ok in 3s"),
        ("tool_result", "line1\nbare string item\nnested content str"),
        ("system", "hmm, let me think"),
    ], f"块序列不符: {blocks}"


def test_read_transcript_missing_file_returns_empty():
    assert autodream._read_transcript("/nonexistent/x.jsonl") == []


def test_tool_use_text_variants():
    """tool_use 取 input(可序列化)/text 兜底; 空/不可序列化 → 空。"""
    f = autodream._tool_use_text
    assert f({"input": {"a": 1}}) == '{"a": 1}'
    assert f({"input": [1, 2]}) == "[1, 2]"
    assert f({"input": "raw str"}) == "raw str"
    assert f({"input": {}}) == ""            # 空容器无信息量 → 空
    assert f({"text": "fallback"}) == "fallback"  # 无 input → text 兜底
    assert f({}) == ""
    assert f({"input": {"x": {"y"}}}) == ""  # set 不可 JSON 序列化 → 容错空


def test_tool_result_text_variants():
    """tool_result content: 字符串直取; list 逐 item text/content, 无文本 item 跳过。"""
    f = autodream._tool_result_text
    assert f({"content": "plain"}) == "plain"
    assert f({"content": [{"text": "a"}, {"content": "b"}, "c", {"type": "image"}]}) == "a\nb\nc"
    assert f({"content": []}) == ""
    assert f({"content": [{"text": ""}, {"content": None}]}) == ""
    assert f({}) == ""


# ── 验收 3a (单元): _build_segments 合并 + 段预算 ─────────────────────

def test_build_segments_merges_consecutive_same_provenance():
    """连续同 provenance 块合并为段 (G2): user_text+user_text → user_prose 段;
    assistant_text+tool_use 同归 agent_assert 合并; 异档边界开新段。"""
    blocks = [
        ("user_text", "A"), ("user_text", "B"),
        ("tool_result", "C"),
        ("assistant_text", "D"), ("tool_use", "E"),
    ]
    segs = autodream._build_segments(blocks)
    assert segs == [
        ("user_prose", "A\nB"),
        ("tool_obs", "C"),
        ("agent_assert", "D\nE"),
    ], f"分段不符: {segs}"


def test_build_segments_budget_truncation():
    """超长段截尾到 _SEGMENT_BUDGET (N4, 替换旧 4000 平截断); budget 参可调。"""
    long_result = "x" * 3000
    segs = autodream._build_segments([("tool_result", long_result)])
    assert len(segs) == 1 and segs[0][0] == "tool_obs"
    assert len(segs[0][1]) == autodream._SEGMENT_BUDGET == 1200
    assert segs[0][1] == long_result[:1200], "截尾应保头去尾"
    # 显式 budget 覆盖缺省。
    assert len(autodream._build_segments([("user_text", "a" * 50)], budget=10)[0][1]) == 10


# ── 集成 harness: monkeypatch extract_facts + embedding (禁网络/LLM) ──

class _MarkerExtractor:
    """按段文本中的 MARK 令牌返回对应三元组; 记录每次收到的段文本。"""

    EDGES = {
        "MARKU": ("Alpha", "uses", "rust"),
        "MARKA": ("Beta", "is_a", "tool"),
        "MARKT": ("Gamma", "located_in", "Paris"),
        "MARKTU": ("Delta", "belongs_to", "Omega"),
        "MARKS": ("Epsilon", "relates_to", "Zeta"),
    }

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, text, providers=None, wings=None):
        self.calls.append(text)
        # 整词匹配: MARKT 是 MARKTU 的子串, 子串匹配会串段 (fixture 设计坑)。
        words = set(text.split())
        meta = {"provider": "fake", "extractor_label": "llm"}
        for marker, (s, p, o) in self.EDGES.items():
            if marker in words:
                return Extraction(
                    entities=[EntityOut(s, "concept"), EntityOut(o, "concept")],
                    edges=[EdgeOut(s, p, o, topic="")],
                    confidence=0.9, source_meta=meta)
        return Extraction(confidence=0.9, source_meta=meta)


def _attrib_fixture(path: Path) -> None:
    """五段归因 fixture: 五类块交错排布 (异档相邻 → 五个独立段)。"""
    _write_transcript(path, [
        {"type": "user", "message": {"content": [{"type": "text", "text": "MARKU Alpha uses rust"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "MARKA Beta is_a tool"}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "MARKT Gamma located_in Paris"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "echo MARKTU Delta belongs_to Omega"}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "MARKS Epsilon relates_to Zeta"}]}},
    ])


def _run_autodream(tmpdir: str, extractor, session_id="sess-m8"):
    tpath = Path(tmpdir) / "session.jsonl"
    _attrib_fixture(tpath)
    orig_embed = embedding.embed
    orig_extract = adapter.extract_facts
    embedding.embed = lambda text, providers=None: []
    adapter.extract_facts = extractor
    try:
        out = autodream.autodream(session_id, str(tpath), providers=[])
    finally:
        embedding.embed = orig_embed
        adapter.extract_facts = orig_extract
    return out, tpath


# ── 验收 2: 分段归因 — provenance 按段落列, veracity 随 M3 映射 ──────

def test_segment_provenance_attribution():
    """四类块各至少一例 (+system): fact.provenance 继承段, veracity 走 M3 五档。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "m8.db")
    extractor = _MarkerExtractor()
    out, _ = _run_autodream(tmp, extractor)

    assert out["added"] == 5, f"应 5 条新增, got {out}"
    assert len(extractor.calls) == 5, f"五段应各调一次 extract, got {len(extractor.calls)}"
    conn = db.get_conn()

    def _fact(subj, pred):
        return conn.execute(
            "SELECT f.provenance, f.veracity FROM fact f "
            "JOIN entity e ON e.id = f.subject_id "
            "WHERE e.name = ? AND f.predicate = ?", (subj, pred)).fetchone()

    # 块→provenance (G2) → veracity (M3 PROVENANCE_VERACITY, DR-6 G1)。
    expect = [
        ("Alpha", "uses", "user_prose", 1.0),        # user text
        ("Beta", "is_a", "agent_assert", 0.5),       # assistant text
        ("Gamma", "located_in", "tool_obs", 0.9),    # tool_result (S1 修复后入管道)
        ("Delta", "belongs_to", "agent_assert", 0.5),  # tool_use (意图非观测)
        ("Epsilon", "relates_to", "system", 0.5),    # 其余 (thinking)
    ]
    for subj, pred, want_prov, want_ver in expect:
        row = _fact(subj, pred)
        assert row is not None, f"缺少 fact {subj}/{pred}"
        assert row["provenance"] == want_prov, (
            f"{subj}/{pred} provenance 应 {want_prov}, got {row['provenance']}")
        assert row["veracity"] == want_ver, (
            f"{subj}/{pred} veracity 应 {want_ver}, got {row['veracity']}")


# ── 验收 3b (集成): 总量超旧 4000 上限不再整体平截 ────────────────────

def test_total_over_4000_not_flat_truncated():
    """5×1100 字符块 (总量 5500 > 旧 4000 平截上限): 五段全部被处理,
    末段内容 (旧平截下必丢) 仍在; 每段 ≤ 段预算。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "budget.db")
    tpath = Path(tmp) / "long.jsonl"
    blocks = [
        ("user", [{"type": "text", "text": "MK1 " + "u" * 1100}]),
        ("assistant", [{"type": "text", "text": "MK2 " + "a" * 1100}]),
        ("user", [{"type": "tool_result", "content": "MK3 " + "t" * 1100}]),
        ("user", [{"type": "text", "text": "MK4 " + "u" * 1100}]),
        ("assistant", [{"type": "text", "text": "MK5 " + "a" * 1100}]),
    ]
    _write_transcript(tpath, [
        {"type": speaker, "message": {"content": content}} for speaker, content in blocks])

    extractor = _MarkerExtractor()
    orig_embed = embedding.embed
    orig_extract = adapter.extract_facts
    embedding.embed = lambda text, providers=None: []
    adapter.extract_facts = extractor
    try:
        autodream.autodream("sess-budget", str(tpath), providers=[])
    finally:
        embedding.embed = orig_embed
        adapter.extract_facts = orig_extract

    assert len(extractor.calls) == 5, (
        f"五段都应被提取 (旧 4000 平截只剩 ~4 段), got {len(extractor.calls)}")
    for i, text in enumerate(extractor.calls, 1):
        assert len(text) <= autodream._SEGMENT_BUDGET, (
            f"段 {i} 超预算: {len(text)}")
        assert f"MK{i}" in text, f"段 {i} 内容丢失 (平截回归?)"
    assert "MK5" in extractor.calls[4], "末段内容被整体平截丢弃 — N4 回归"


# ── 验收 4: 幂等不回归 — 同 transcript 重跑全 NOOP ────────────────────

def test_idempotent_rerun_all_noop():
    """四分支行为不变: 首跑 5 ADD, 同 transcript 重跑全 NOOP
    ({added:0, updated:0, deleted:0, noop:5} — 验收契约逐字)。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "idem.db")
    extractor = _MarkerExtractor()
    out1, _ = _run_autodream(tmp, extractor)
    assert out1 == {"added": 5, "updated": 0, "deleted": 0, "noop": 0}, out1

    extractor2 = _MarkerExtractor()
    out2, _ = _run_autodream(tmp, extractor2)
    assert out2 == {"added": 0, "updated": 0, "deleted": 0, "noop": 5}, (
        f"重跑应全 NOOP, got {out2}")
    # 库中仍只有 5 条 active fact (无重复写入)。
    n = db.get_conn().execute(
        "SELECT COUNT(*) FROM fact WHERE status='active'").fetchone()[0]
    assert n == 5, f"重跑不得产生重复 fact, got {n}"

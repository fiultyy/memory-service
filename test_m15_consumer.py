"""M15 消费面批验收测试 (spec v2 §4 M15: --json 契约 + SKILL.md 四要点)。

覆盖派发令两条验收:
1. --json 输出可 json.loads、字段契约锁死 (关键字段逐一断言, 列表序=输出序);
   缺省文本路径零变化 (list shape 原样)。
2. SKILL.md 四要点 grep-able (动词时机/查询策略/复述禁令/新实态段落)。

测试规范: def test_xxx() 函数让 pytest 收集。禁网络/LLM。
"""
import io
import json
import contextlib
import tempfile
from pathlib import Path

import cli
import db
import store

SKILL_PATH = Path(__file__).parent / "SKILL.md"


def _fresh(name: str) -> str:
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / f"{name}.db")
    return tmp


def _seed_two_facts() -> tuple[str, str]:
    """造高/低 LIF 两条可命中 fact (value 含查询词 — match 打分对象是 value,
    循 test_recall_p1 惯例; provenance/veracity 全链)。"""
    eid = store.put_entity("Rust", "tool")
    high = store.put_fact(eid, "is_a", "rust systems language", extractor="llm",
                          fact_type="permanent", LIF=0.9, confidence=0.9,
                          provenance="user_prose", topic="rust is systems")
    low = store.put_fact(eid, "used_by", "rust many projects", extractor="regex",
                         fact_type="stable", LIF=0.3, confidence=0.5,
                         provenance="tool_obs", topic="rust widely used")
    return high, low


# ── 验收 1: --json 契约 ──────────────────────────────────────────────

def test_recall_json_contract_fields():
    """>--json: {query, facts}, 关键字段逐一非缺 (id/subject/predicate/value/
    LIF/status/provenance/veracity/topic…); provenance/veracity 落对。"""
    _fresh("m15a")
    high, low = _seed_two_facts()
    out = cli.recall("Rust", boost=False, as_json=True)
    assert set(out.keys()) == {"query", "facts"}, out.keys()
    assert out["query"] == "Rust"
    assert len(out["facts"]) == 2
    f0 = out["facts"][0]
    for field in ("id", "subject_id", "predicate", "value", "LIF", "status",
                  "provenance", "veracity", "topic", "extractor",
                  "supersede_reason", "supersedes_id", "valid_from",
                  "valid_to", "created_at", "access_count",
                  "last_accessed_at"):
        assert field in f0, f"契约缺字段 {field}: {sorted(f0)}"
    assert f0["status"] == "active"
    by_id = {f["id"]: f for f in out["facts"]}
    assert by_id[high]["provenance"] == "user_prose"
    assert by_id[high]["veracity"] == 1.0
    assert by_id[low]["provenance"] == "tool_obs"
    assert by_id[low]["veracity"] == 0.9


def test_recall_json_order_matches_output_order():
    """列表序 = 输出序 (score 降序): --json facts 与缺省 list 同序。"""
    _fresh("m15a2")
    _seed_two_facts()
    plain = cli.recall("Rust", boost=False)
    j = cli.recall("Rust", boost=False, as_json=True)
    assert [f["id"] for f in plain] == [f["id"] for f in j["facts"]], (
        "--json 序必须等于输出序")
    # score 降序抽查 (首条 score >= 末条; score 字段在契约里透出)。
    scores = [f.get("score") for f in j["facts"] if f.get("score") is not None]
    assert scores == sorted(scores, reverse=True)


def test_recall_json_cli_flag_parses():
    """cli.py recall --json 全链: stdout 可 json.loads 且 shape 契约。"""
    _fresh("m15flag")
    _seed_two_facts()
    buf = io.StringIO()
    import os
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    with contextlib.redirect_stdout(buf):
        cli._main(["recall", "Rust", "--json", "--top-k", "2"])
    payload = json.loads(buf.getvalue())
    assert set(payload.keys()) == {"query", "facts"}
    assert payload["query"] == "Rust"
    assert all("provenance" in f and "veracity" in f for f in payload["facts"])


def test_recall_default_path_unchanged():
    """缺省零变化: 不传 --json → 原 list[dict] shape (含 _snaptag), 非 {query,facts}。"""
    _fresh("m15def")
    _seed_two_facts()
    out = cli.recall("Rust", boost=False)
    assert isinstance(out, list) and len(out) == 2, "缺省必须保持 list shape"
    assert "_snaptag" in out[0], "缺省 _snaptag 嵌入不得丢"
    assert "query" not in out[0], "缺省不得混入契约 shape 字段"


def test_verbose_shape_json_contract():
    """verbose dict (含 fact 键) → 契约取 .fact 投影 + score 透出。"""
    _fresh("m15v")
    _seed_two_facts()
    out = cli.recall("Rust", verbose=True, boost=False, as_json=True)
    assert isinstance(out, dict) and "facts" in out
    f0 = out["facts"][0]
    assert f0.get("score") is not None, "verbose shape 的 score 必须透出"
    assert f0["predicate"] in ("is_a", "used_by")


def test_stats_json_contract():
    """stats-json: 同 stats 数据, json.loads 契约 shape。"""
    _fresh("m15stat")
    _seed_two_facts()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli._main(["stats-json"])
    payload = json.loads(buf.getvalue())
    assert {"entities", "facts", "churn"} <= set(payload.keys())
    assert payload["facts"] >= 2 and payload["entities"] >= 1


# ── 验收 2: SKILL.md 四要点 ──────────────────────────────────────────

def test_skill_has_four_required_sections():
    """四要点 grep-able: 动词时机教学/查询策略/复述禁令/新实态段落。"""
    text = SKILL_PATH.read_text(encoding="utf-8")
    # 1. 维护动词时机教学 (含「即将上线」防误用标注)。
    assert "维护动词时机教学" in text
    assert "该记新事实" in text and "该确认" in text and "该建议失效" in text
    assert "即将上线" in text, "预告命令形态必须标注即将上线防误用"
    assert "无 delete" in text
    # 2. 查询策略 (flag 矩阵 + env 语义)。
    assert "查询策略" in text
    assert "--vector" in text and "--bfs" in text and "--as-of" in text \
        and "--bfs-scoped" in text
    assert "MEM_DELAYED_REINFORCE" in text
    # 3. 复述禁令。
    assert "复述禁令" in text
    assert "转述" in text and "fact_id" in text
    # 4. 新实态段落 (占位通道/块文法/延迟强化/dreaming/卫生/源不变式)。
    assert "新实态" in text
    for kw in ("占位", "provenance", "延迟强化", "dreaming", "卫生", "源不变式"):
        assert kw in text, f"新实态段落缺关键词 {kw}"
    # --json 契约文档在位。
    assert "--json" in text and "ABI" in text


def test_skill_existing_correct_content_kept():
    """不删既有仍正确内容: 触发条件/子命令契约/数据与状态等骨架仍在。"""
    text = SKILL_PATH.read_text(encoding="utf-8")
    for kw in ("触发条件", "子命令契约", "数据与状态", "何时不该用",
               "synthesis-index", "PreCompact", "11 个子命令"):
        assert kw in text, f"既有骨架内容被误删: {kw}"

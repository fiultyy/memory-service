"""v1.7③ 验收测试 — 单 LLM gate (gate.py) + recall(use_gate) 接缝 + CLI --gate.

覆盖派发书测试面两组:
- 组1 schema 校验: keep 布尔域 / match_score 0-1 clamp / 未知 id 拒 / dangling
  覆盖缺口拒 / 两轮败响亮 (风格照 test_llm_extract.py)。
- 组2 锚断言 + 断供语义 + 记账: matched_anchor 逐字子串校验 + 伪造重试 + 空白
  归一容差; gate LLM 断供 → 只注入 A + recall 不炸 + formula 乘子仍生效 (B 翼
  排序降权在场但不注入); A 路不受 gate_mod 影响回归 (v7 验收原文); gate_score
  N2 记账 (求和封顶) + CLI 手动面不入账; cli.recall --gate 默认开 / --no-gate
  逃生。

测试规范: 零网络零 LLM (mock provider 注入); ZHIPU_API_KEY 逐测试 pin 清空。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

import cli
import db
import gate
import recall as recall_mod
import scoring
import store
from gate import GateFailed, build_request, derive_keywords, run_gate, validate
from llm_extract import SchemaViolation
from llm_provider import ProviderCallError


# ── 夹具与 mock ──────────────────────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """隔离库 + 信号目录 + 断供 env (逐测试 pin: 无 ZHIPU_API_KEY)。"""
    db.init(tmp_path / "gate.db")
    import signals
    monkeypatch.setattr(signals, "_signals_dir", lambda: tmp_path / "signals")
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    yield tmp_path
    db._conn = None
    db._conn_path = None


class GateMockProvider:
    """按裁决编程的 gate mock: 从请求 payload 读候选 id, 对全部候选回同构判定
    (严格互耗 — validate 要求逐条判定)。"""

    model = "mock-gate"

    def __init__(self, keep=True, match_score=0.9, anchor="Alpha",
                 first_outcome=None):
        self.keep = keep
        self.ms = match_score
        self.anchor = anchor
        self.first_outcome = first_outcome  # 首轮返回 (str|Exception), 次轮诚实
        self.calls: list = []

    def chat(self, system, messages, max_tokens=1500, tools=None, tool_choice=None):
        self.calls.append((system, messages))
        if self.first_outcome is not None and len(self.calls) == 1:
            if isinstance(self.first_outcome, Exception):
                raise self.first_outcome
            return self.first_outcome
        req = json.loads(messages[0]["content"])
        doc = {"facts": [
            {"id": c["id"], "keep": self.keep, "match_score": self.ms,
             "matched_anchor": self.anchor}
            for c in req["candidates"]
        ]}
        return json.dumps(doc, ensure_ascii=False)


class ErrProvider:
    """恒抛 mock (ProviderCallError 路径)。"""

    model = "mock-err"

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def chat(self, *a, **k):
        self.calls += 1
        raise self.exc


def _chain():
    """A --uses(llm)--> B --runs_on(regex)--> C 链: f_ab 走主路径 (A 路),
    f_bc 是 regex 档 (lif_source 0.4) B 翼邻居 (软惩罚+gate 判定域)。"""
    ea = store.put_entity("Alpha", "concept")
    eb = store.put_entity("Bravo", "concept")
    ec = store.put_entity("Charlie", "concept")
    f_ab = store.put_fact(ea, "uses", "Alpha uses Bravo", extractor="llm",
                          fact_type="permanent", LIF=0.5, confidence=0.8,
                          object_id=eb, topic="AB")
    f_bc = store.put_fact(eb, "runs_on", "Bravo runs on Charlie",
                          extractor="regex", fact_type="permanent",
                          LIF=0.5, confidence=0.8, object_id=ec, topic="BC")
    return f_ab, f_bc


def _recall(query="Alpha", **kw):
    kw.setdefault("use_bfs", True)
    kw.setdefault("bfs_hops", 2)
    kw.setdefault("boost", False)
    return recall_mod.recall(query, **kw)


# ── 组1: schema 校验 ─────────────────────────────────────────────────

def _doc(**over):
    item = {"id": "f1", "keep": True, "match_score": 0.9,
            "matched_anchor": "vite"}
    item.update(over)
    return {"facts": [item]}


_CANDS = {"f1": "vite 依赖 esbuild 做转译"}
_QUERY = "vite 部署问题排查"


def test_validate_keep_bool_domain(fresh_db):
    """keep 严格布尔域: str/int/None 都拒 (bool 是 int 子类, 显式排除)。"""
    for bad in ("true", 1, 0, None, 1.0):
        with pytest.raises(SchemaViolation, match="keep 非布尔"):
            validate(_doc(keep=bad), _CANDS, _QUERY)


def test_validate_match_score_clamp_and_type(fresh_db):
    """match_score: 数值越界 clamp 到 [0,1]; 非数值/bool/缺失 → 拒。"""
    assert validate(_doc(match_score=1.7), _CANDS, _QUERY)["f1"]["match_score"] == 1.0
    assert validate(_doc(match_score=-0.2), _CANDS, _QUERY)["f1"]["match_score"] == 0.0
    for bad in ("0.9", None, True):
        with pytest.raises(SchemaViolation, match="match_score"):
            validate(_doc(match_score=bad), _CANDS, _QUERY)
    doc = {"facts": [{"id": "f1", "keep": True, "matched_anchor": "vite"}]}
    with pytest.raises(SchemaViolation, match="match_score"):
        validate(doc, _CANDS, _QUERY)


def test_validate_unknown_id_rejected(fresh_db):
    """未知 fact id (幻觉) → 整体拒。"""
    with pytest.raises(SchemaViolation, match="未知 fact id"):
        validate(_doc(id="f999"), _CANDS, _QUERY)


def test_validate_dangling_coverage_rejected(fresh_db):
    """dangling 覆盖缺口: 候选未被逐条判定 → 拒; 同 id 重复判定 → 拒。"""
    cands = {"f1": "a", "f2": "b"}
    doc = {"facts": [{"id": "f1", "keep": True, "match_score": 0.5,
                      "matched_anchor": "a"}]}
    with pytest.raises(SchemaViolation, match="dangling 覆盖缺口"):
        validate(doc, cands, "a b")
    dup = {"facts": [
        {"id": "f1", "keep": True, "match_score": 0.5, "matched_anchor": "a"},
        {"id": "f1", "keep": False, "match_score": 0.1, "matched_anchor": "a"},
    ]}
    with pytest.raises(SchemaViolation, match="重复判定"):
        validate(dup, cands, "a b")


def test_run_gate_retry_feedback_then_success(fresh_db):
    """坏 JSON → 1 次带原因重试 → 次轮诚实输出收货 (照 llm_extract 先例)。"""
    p = GateMockProvider(first_outcome="这不是 JSON {{{", anchor="vite")
    verdicts = run_gate(_CANDS, _QUERY, provider=p)
    assert verdicts["f1"]["keep"] is True and verdicts["f1"]["match_score"] == 0.9
    assert len(p.calls) == 2
    assert "schema 校验" in p.calls[1][1][-1]["content"]


def test_run_gate_two_rounds_loud(fresh_db):
    """两轮 schema 败 → GateFailed 响亮 (不静默丢条/不静默当 keep)。"""
    class BadP:
        model = "bad"

        def __init__(self):
            self.calls = []

        def chat(self, *a, **k):
            self.calls.append(1)
            return "还是垃圾 {{{"
    bp = BadP()
    with pytest.raises(GateFailed, match="两轮失败"):
        run_gate(_CANDS, _QUERY, provider=bp)
    assert len(bp.calls) == 2  # 恰好重试一次


def test_run_gate_provider_unreachable_loud(fresh_db):
    """ProviderCallError → GateFailed (含不可达原因), 不重试网络层。"""
    p = ErrProvider(ProviderCallError("network: timeout"))
    with pytest.raises(GateFailed, match="不可达"):
        run_gate(_CANDS, _QUERY, provider=p)
    assert p.calls == 1


def test_run_gate_short_circuit_without_provider_or_key(fresh_db):
    """断供红线: 无 provider 且 env 无 ZHIPU_API_KEY → 直接短路"无 gate",
    不构造 provider 不发网络。"""
    with pytest.raises(GateFailed, match="断供短路"):
        run_gate(_CANDS, _QUERY, provider=None)


def test_run_gate_empty_candidates_no_call(fresh_db):
    """空候选集 → 无 LLM 调用直接返回空表 (use_gate 无 B 翼时的零成本路径)。"""
    p = GateMockProvider()
    assert run_gate({}, _QUERY, provider=p) == {}
    assert p.calls == []


# ── 组2: 锚断言 + recall 接缝 + 记账 + CLI ───────────────────────────

def test_anchor_fabricated_retry_then_success(fresh_db):
    """伪造锚 (非逐字子串) → 首轮拒 + 重试反馈含锚原因 → 次轮诚实收货。"""
    p = GateMockProvider(
        first_outcome=json.dumps(
            {"facts": [{"id": "f1", "keep": True, "match_score": 0.9,
                        "matched_anchor": "量子纠缠退相干"}]},
            ensure_ascii=False),
        anchor="vite")
    verdicts = run_gate(_CANDS, _QUERY, provider=p)
    assert verdicts["f1"]["keep"] is True
    assert len(p.calls) == 2
    assert "matched_anchor 非逐字子串" in p.calls[1][1][-1]["content"]


def test_anchor_fabricated_both_attempts_loud(fresh_db):
    """两轮都伪造锚 → GateFailed 响亮。"""
    bad = json.dumps(
        {"facts": [{"id": "f1", "keep": True, "match_score": 0.9,
                    "matched_anchor": "量子纠缠退相干"}]}, ensure_ascii=False)

    class BadAnchorP:
        model = "bad-anchor"

        def __init__(self):
            self.calls = []

        def chat(self, *a, **k):
            self.calls.append(1)
            return bad

    p = BadAnchorP()
    with pytest.raises(GateFailed, match="matched_anchor 非逐字子串"):
        run_gate(_CANDS, _QUERY, provider=p)
    assert len(p.calls) == 2


def test_anchor_whitespace_normalization_passes(fresh_db):
    """同字符异空白 (换行/多空格) 不算伪造 — 逐字断言带空白归一容差。"""
    cands = {"f1": "vite 依赖\nesbuild 做转译"}
    doc = {"facts": [{"id": "f1", "keep": True, "match_score": 0.8,
                      "matched_anchor": "vite  依赖 esbuild"}]}
    v = validate(doc, cands, _QUERY)
    assert v["f1"]["keep"] is True


def test_anchor_may_match_fact_text_or_query(fresh_db):
    """锚可落在 query 原文或候选 fact 文本 (两源任一逐字即可)。"""
    # 落 fact 文本: fact 里没有 "esbuild" 的 query — anchor 来自 fact 文本。
    v = validate(_doc(matched_anchor="esbuild"), _CANDS, _QUERY)
    assert v["f1"]["match_score"] == 0.9
    # 落 query 原文: anchor "部署问题" 只在 query 里。
    v2 = validate(_doc(matched_anchor="部署问题"), _CANDS, _QUERY)
    assert v2["f1"]["keep"] is True


def test_build_request_three_fields(fresh_db):
    """请求规范三字段升格: keywords=确定性实体提取 / intent=原文 / scope 透传。"""
    req = build_request(_QUERY, "manual")
    assert req["intent"] == _QUERY
    assert req["scope"] == "manual"
    assert isinstance(req["keywords"], list)
    # 确定性: 同输入两次派生同序。
    assert build_request(_QUERY, "manual")["keywords"] == req["keywords"]


def test_derive_keywords_gazetteer_then_tokens(fresh_db):
    """keywords 实体提取: gazetteer 词典命中 canonical 名; 未命中回退分词。"""
    store.put_entity("vite", "technical_term")
    kws = derive_keywords("vite 部署问题排查")
    assert "vite" in kws
    # 词典未覆盖 → query_tokens 分词兜底 (非空即可)。
    assert derive_keywords("zzzqqq wwww") == scoring.query_tokens("zzzqqq wwww")


def test_gate_off_bwing_penalty_only_no_keys(fresh_db):
    """gate 默认关: B 翼在场且带软惩罚 (formula 乘子), 不带 gate 键。"""
    f_ab, f_bc = _chain()
    res = {f["id"]: f for f in _recall("Alpha")}
    assert f_ab in res and f_bc in res
    assert "gate_keep" not in res[f_bc] and "match_score" not in res[f_bc]
    # formula 乘子在场: regex 档 B 翼折后 ≈ 折前×0.7857 (与 0.7 档 A 路无关)。
    verbose = {s["fact"]["id"]: s for s in _recall("Alpha", verbose=True)}
    base = scoring.score_fact(verbose[f_bc]["fact"], "Alpha",
                              centrality=verbose[f_bc]["centrality"],
                              vec_sim=verbose[f_bc]["vec_sim"],
                              bfs_proximity=verbose[f_bc]["bfs_proximity"])["score"]
    assert verbose[f_bc]["score"] == pytest.approx(base * 0.7857142857142857, abs=1e-12)


def test_gate_keep_attaches_keys_a_path_untouched(fresh_db):
    """gate keep: B 翼 fact 附 gate_keep/match_score 键; A 路 fact 不带这两个键。"""
    f_ab, f_bc = _chain()
    p = GateMockProvider(keep=True, match_score=0.9, anchor="Alpha")
    res = {f["id"]: f for f in _recall("Alpha", use_gate=True, gate_provider=p)}
    assert len(p.calls) == 1  # 有 B 翼 → 恰一次 LLM 调用
    assert f_bc in res and f_ab in res
    assert res[f_bc]["gate_keep"] is True
    assert isinstance(res[f_bc]["match_score"], float)
    assert res[f_bc]["match_score"] == pytest.approx(0.9)
    assert "gate_keep" not in res[f_ab], "A 路 fact 不得带 gate 键"
    assert "match_score" not in res[f_ab]


def test_gate_reject_drops_bwing_a_only(fresh_db):
    """gate 判不匹配 → B 翼不入返回 (只注入 A), recall 不炸。"""
    f_ab, f_bc = _chain()
    p = GateMockProvider(keep=False, match_score=0.1, anchor="Alpha")
    res = {f["id"]: f for f in _recall("Alpha", use_gate=True, gate_provider=p)}
    assert f_ab in res, "A 路必须照常注入"
    assert f_bc not in res, "判不匹配的 B 翼不得入返回"
    assert "gate_keep" not in res[f_ab]


def test_gate_unavailable_drops_bwing_a_only_no_crash(fresh_db):
    """gate LLM 不可用 (ProviderCallError) → B 翼全部不入返回, recall 不炸。"""
    f_ab, f_bc = _chain()
    p = ErrProvider(ProviderCallError("network down"))
    res = {f["id"]: f for f in _recall("Alpha", use_gate=True, gate_provider=p)}
    assert f_ab in res and f_bc not in res


def test_gate_outage_shortcircuit_keeps_formula_penalty_active(fresh_db):
    """断供红线 e2e: 无 provider/key → 短路"无 gate" = A-only; 而 gate 关时
    B 翼以 formula 折扣在场 — 乘子纯公式生效但断供时不注入 (B 翼排序降权在
    场但不注入)。"""
    f_ab, f_bc = _chain()
    # 断供 (fixture 已清 ZHIPU_API_KEY): A-only, 不炸。
    res = {f["id"]: f for f in _recall("Alpha", use_gate=True)}
    assert f_ab in res and f_bc not in res
    # 对照: gate 关 → B 翼在场 (折扣生效) — "降权在场"与"不注入"分属两档。
    res_off = {f["id"]: f for f in _recall("Alpha")}
    assert f_bc in res_off


def test_gate_no_bwing_no_llm_call(fresh_db):
    """use_gate=True 但无 B 翼 (use_bfs=False) → 零 LLM 调用 (gate 域= b_wing)。"""
    f_ab, f_bc = _chain()
    p = GateMockProvider()
    res = recall_mod.recall("Alpha", boost=False, use_gate=True, gate_provider=p)
    assert p.calls == []
    assert f_ab in {f["id"] for f in res}


def test_a_path_score_byte_identical_with_gate(fresh_db):
    """v7 验收原文回归: 同 query 下 A 路命中 fact score 逐字不变 (gate on/off)。"""
    f_ab, f_bc = _chain()
    r_off = {s["fact"]["id"]: s for s in _recall("Alpha", verbose=True)}
    r_on = {s["fact"]["id"]: s for s in _recall(
        "Alpha", verbose=True, use_gate=True,
        gate_provider=GateMockProvider(keep=True, anchor="Alpha"))}
    assert r_off[f_ab]["score"] == r_on[f_ab]["score"], (
        "A 路 fact score 必须逐字不变 (gate_mod 不进 A 路)")
    assert r_off[f_ab]["fact"] == r_on[f_ab]["fact"] or all(
        r_off[f_ab]["fact"].get(k) == r_on[f_ab]["fact"].get(k)
        for k in r_off[f_ab]["fact"] if k != "_snaptag")


def test_gate_score_accumulates_with_cap(fresh_db, monkeypatch):
    """N2 记账: keep 的 match_score 求和累计, 达 MEM_UNLOCK_MATCH_SCORE 封顶;
    gate 判不匹配不入账。"""
    f_ab, f_bc = _chain()
    p = GateMockProvider(keep=True, match_score=0.6, anchor="Alpha")
    _recall("Alpha", use_gate=True, gate_provider=p)
    assert store.get_fact(f_bc)["gate_score"] == pytest.approx(0.6)
    _recall("Alpha", use_gate=True, gate_provider=p)
    assert store.get_fact(f_bc)["gate_score"] == pytest.approx(1.2)
    # 封顶: 阈值 1.0 → min(1.0, 1.2+0.6) = 1.0。
    monkeypatch.setenv("MEM_UNLOCK_MATCH_SCORE", "1.0")
    _recall("Alpha", use_gate=True, gate_provider=p)
    assert store.get_fact(f_bc)["gate_score"] == pytest.approx(1.0)
    # A 路 fact 不入账 (gate 域仅 B 翼)。
    assert store.get_fact(f_ab)["gate_score"] == pytest.approx(0.0)
    # 判不匹配 → 不入账。
    p2 = GateMockProvider(keep=False, match_score=0.9, anchor="Alpha")
    _recall("Alpha", use_gate=True, gate_provider=p2)
    assert store.get_fact(f_bc)["gate_score"] == pytest.approx(1.0)


def test_manual_face_match_score_not_accounted(fresh_db):
    """v7 三句之三: CLI 手动面 (cli.recall, gate_account=False) match_score
    不入 gate_score 解锁累计; boost 记账路径照旧 (recall boost 记账不额外写)。"""
    f_ab, f_bc = _chain()
    p = GateMockProvider(keep=True, match_score=0.9, anchor="Alpha")
    out = cli.recall("Alpha", use_bfs=True, bfs_hops=2, boost=False,
                     session_id="s-manual", use_gate=True, gate_provider=p)
    ids = {f["id"] for f in (out["results"] if isinstance(out, dict) else out)}
    assert f_bc in ids and f_ab in ids
    assert store.get_fact(f_bc)["gate_score"] == pytest.approx(0.0), (
        "手动面 match_score 不得入 gate_score 解锁累计")
    # scope=manual 已随请求升格 (零分叉 schema 的面标签)。
    req = json.loads(p.calls[0][1][0]["content"])
    assert req["scope"] == "manual"
    assert req["intent"] == "Alpha"
    assert isinstance(req["keywords"], list)


def test_injection_face_gate_account_wired(fresh_db):
    """F1 a)+b) 接线: 注入面形态 (cli.recall 带 gate_account=True — 与
    recall_inject 首轮档 recall_kw 同款) 下 gate keep → gate_score 生产入账
    (N2 暂缓期只写不读的写入面可达); scope 面标签按调用面="recall"。"""
    f_ab, f_bc = _chain()
    p = GateMockProvider(keep=True, match_score=0.9, anchor="Alpha")
    out = cli.recall("Alpha", use_bfs=True, bfs_hops=2, boost=False,
                     session_id="s-first-turn", use_gate=True,
                     gate_account=True, gate_provider=p)
    ids = {f["id"] for f in (out["results"] if isinstance(out, dict) else out)}
    assert f_bc in ids and f_ab in ids
    assert store.get_fact(f_bc)["gate_score"] == pytest.approx(0.9), (
        "注入首轮档 gate keep 必须入 N2 解锁累计 (F1: 生产写入面不可达漏洞已修)")
    req = json.loads(p.calls[0][1][0]["content"])
    assert req["scope"] == "recall", "注入面 gate 请求 scope 面标签应为 recall"
    # 手动面对照: 默认 gate_account=False 仍不入账 (v7 三句之三不被回归);
    # 累计语义 = 求和 (0.9 注入面 + 0.9 直调 recall_mod.recall 默认 True 面)。
    _recall("Alpha", use_gate=True, gate_provider=p)
    assert store.get_fact(f_bc)["gate_score"] == pytest.approx(1.8)


def test_recall_inject_capability_guard_sees_gate_account():
    """F1 b) 前提钉死: recall_inject 首轮档签名能力探测守卫 (同一 if) 对
    gate_account 可见 — use_gate 与 gate_account 同波落地, 守卫代理成立。"""
    import inspect
    params = inspect.signature(cli.recall).parameters
    assert "use_gate" in params and "gate_account" in params


def test_cli_gate_default_on_and_no_gate_escape(fresh_db, monkeypatch):
    """--gate 默认开 (N3 选 a) / --no-gate 逃生。"""
    captured = []

    def fake_recall(query, **kw):
        captured.append(kw.get("use_gate"))
        return []

    monkeypatch.setattr(cli, "recall", fake_recall)
    cli._main(["recall", "Alpha"])
    assert captured == [True]
    cli._main(["recall", "Alpha", "--no-gate"])
    assert captured == [True, False]

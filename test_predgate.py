"""batch 13 谓词聚边测试: 开放谓词归一 + 近似度聚类 + 词频统计。

predgate.cluster 的向量由测试注入 (不打 embedding 服务); autodream 接线
用假 provider e2e 验证 fact.predicate=canonical / raw_predicate=原文。
"""

import pytest

import db
import predgate
import llm_extract


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM_DB", str(tmp_path / "m.db"))
    db.init(str(tmp_path / "m.db"))
    yield
    db.init(":memory:") if False else None  # conftest 会重置


def test_norm_predicate_snake_case():
    assert llm_extract._norm_predicate("Competes With") == "competes_with"
    assert llm_extract._norm_predicate("  Uses  ") == "uses"
    assert llm_extract._norm_predicate("competitor-of") == "competitor_of"
    assert llm_extract._norm_predicate("runs__on") == "runs_on"
    # CJK 原样保留 (聚类层并名)
    assert llm_extract._norm_predicate("竞品") == "竞品"


def test_norm_predicate_rejects():
    with pytest.raises(llm_extract.SchemaViolation):
        llm_extract._norm_predicate("")
    with pytest.raises(llm_extract.SchemaViolation):
        llm_extract._norm_predicate("   ")
    with pytest.raises(llm_extract.SchemaViolation):
        llm_extract._norm_predicate("x" * 65)
    with pytest.raises(llm_extract.SchemaViolation):
        llm_extract._norm_predicate(123)


def test_validate_open_predicate_passes():
    doc = {"entities": [{"name": "Alpha"}, {"name": "Beta"}],
           "facts": [{"subject": "Alpha", "predicate": "Competes With",
                      "object": "Beta", "evidence": "原文"}]}
    ents, edges, _ = llm_extract.validate(doc)
    assert edges[0].predicate == "competes_with"  # 归一后开放通过 (无枚举门)


def test_cluster_merges_near_synonyms(tmp_db):
    # 两近义 raw (同向量) + 一无关 raw (正交向量)
    v1 = [1.0, 0.0]
    v2 = [0.9, 0.1]   # cos(v1,v2) ≈ 0.994 ≥ 0.75
    v3 = [0.0, 1.0]   # cos(v1,v3) = 0 < 0.75
    m = predgate.cluster(["competes_with", "competitor_of", "runs_on"],
                         vectors=[v1, v2, v3])
    assert m["competes_with"] == "competes_with"       # 先到成 canonical
    assert m["competitor_of"] == "competes_with"       # 近义并入
    assert m["runs_on"] == "runs_on"                   # 无关自成
    # 计数: 各出现 1 次
    rows = {r["canonical"]: r["count"] for r in
            db.get_conn().execute("SELECT canonical, count FROM predicate_registry")}
    assert rows == {"competes_with": 2, "runs_on": 1}


def test_cluster_accumulates_across_runs(tmp_db):
    v = [1.0, 0.0]
    predgate.cluster(["uses"], vectors=[v])
    # uniq=["uses","utilizes"] → 向量数须对齐 (不齐触发防御分支零向量)
    m2 = predgate.cluster(["uses", "uses", "utilizes"],
                          vectors=[v, [0.95, 0.05]])
    assert m2["uses"] == "uses" and m2["utilizes"] == "uses"
    rows = {r["canonical"]: r["count"] for r in
            db.get_conn().execute("SELECT canonical, count FROM predicate_registry")}
    assert rows["uses"] == 4  # 1 (前轮) + 2 (uses) + 1 (utilizes 并入) — 词频=聚类总出现次数


def test_cluster_threshold_env(tmp_db, monkeypatch):
    monkeypatch.setenv("MEM_PRED_CLUSTER_THRESHOLD", "0.999")
    a, b = [1.0, 0.0], [0.9, 0.1]  # cos ≈ 0.994 < 0.999 → 不并
    m = predgate.cluster(["x_a", "x_b"], vectors=[a, b])
    assert m["x_b"] == "x_b"  # 阈值收紧后各自 canonical


def test_stats_members(tmp_db):
    import store
    v = [1.0, 0.0]
    predgate.cluster(["monitors"], vectors=[v])
    s_id = store.put_entity("S", "concept")
    o1 = store.put_entity("O1", "concept")
    o2 = store.put_entity("O2", "concept")
    store.put_fact(s_id, "monitors", "o1", object_id=o1,
                   raw_predicate="monitors")
    store.put_fact(s_id, "monitors", "o2", object_id=o2,
                   raw_predicate="watches")
    s = predgate.stats()
    row = next(r for r in s if r["canonical"] == "monitors")
    assert row["count"] >= 1
    assert row["members"].get("monitors") == 1


def test_autodream_wiring_canonical_and_raw(tmp_db, monkeypatch):
    """e2e: 假 provider 抽出开放谓词 → fact.predicate=canonical,
    raw_predicate=原文; registry 计数更新。"""
    import autodream
    from llm_provider import EntityOut, EdgeOut, Extraction

    calls = {"n": 0}

    class FakeProvider:
        model = "fake"
        def chat(self, system, messages, max_tokens=1500, tools=None,
                 tool_choice=None):
            calls["n"] += 1
            return ('{"entities": [{"name": "Alpha"}, {"name": "Beta"}],'
                    ' "facts": [{"subject": "A", "predicate": "competes_with",'
                    ' "object": "B", "evidence": "ev"}]}')

    def fake_extract(seg, provider=None):
        return Extraction(
            entities=[EntityOut(name="Alpha"), EntityOut(name="Beta")],
            edges=[EdgeOut(subject="Alpha", predicate="competes_with",
                           object="Beta", topic="ev", confidence=0.9)],
            confidence=0.9, source_meta={"extractor_label": "llm"})

    import json as _json
    # 聚边向量注入: 全新方向确保自成 canonical; autodream 惰性 import 同一
    # module 对象, monkeypatch predgate.cluster 属性即可生效
    V = [1.0, 0.0, 0.0]
    real_cluster = predgate.cluster
    def spy_cluster(raws, vectors=None):
        return real_cluster(raws, vectors=[V] * len({r for r in raws}))
    monkeypatch.setattr(predgate, "cluster", spy_cluster)

    # 走 autodream 主径 (llm 通道); 拦 llm_extract.extract (autodream 调用时查模块属性)
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    monkeypatch.setattr(llm_extract, "extract", fake_extract)

    import tempfile, pathlib, json as _json
    p = pathlib.Path(tempfile.mkdtemp()) / "t.jsonl"
    p.write_text(
        _json.dumps({"type": "user", "message": {"content": "Alpha competes with Beta evidence ev"}}) + "\n",
        encoding="utf-8")
    autodream.autodream("s1", str(p))

    conn = db.get_conn()
    f = conn.execute(
        "SELECT predicate, raw_predicate FROM fact WHERE raw_predicate IS NOT NULL"
    ).fetchone()
    assert f is not None
    assert f["predicate"] == "competes_with"
    assert f["raw_predicate"] == "competes_with"
    reg = {r["canonical"]: r["count"] for r in conn.execute(
        "SELECT canonical, count FROM predicate_registry")}
    assert reg.get("competes_with", 0) >= 1

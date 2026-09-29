"""laya T5 — LayaJudgeProvider 实体消歧位 (resolver step2 裁判) 单测.

spec: docs/specs/laya-integration-tickets.md T5, spec §四 Step1,
temp/laya-batch-request-design.md §2.6。
覆盖: choice 命中→resolver 返回既有 id+alias 并入 / 低置信→step3 新建 /
降级矩阵(Laya 挂→fallback 透传含 context / 无 fallback→ProviderCallError) /
MEM_LAYA_ENABLED=0 原路径。db.init(tmp) 隔离, 绝不碰 data/memory.db。
"""
import tempfile
from pathlib import Path

import db
import resolver
import store
from llm_provider import LayaJudgeProvider, ProviderCallError


def _fresh_db() -> None:
    db.init(Path(tempfile.mkdtemp()) / "mem.db")


class FakeFallback:
    """记录调用的假 fallback (dedupe 契约: {"duplicate_id": str|None})。"""

    def __init__(self, result=None, raises=False):
        self.calls = []
        self.result = result if result is not None else {"duplicate_id": None}
        self.raises = raises

    def extract_facts(self, text):
        raise AssertionError("dedupe path should not extract")

    def dedupe_entity(self, new_name, new_type, candidates, context=None):
        self.calls.append((new_name, new_type, candidates, context))
        if self.raises:
            raise ProviderCallError("fallback also down")
        return dict(self.result)

    def judge_contradiction(self, *a):
        raise AssertionError("dedupe path should not judge")


def _laya(monkeypatch, available=True, answer=None):
    """monkeypatch laya_client; answer=None → laya_batch 返回 None。"""
    import laya_client as lc
    batch_calls = []

    def fake_batch(state, questions, timeout=30.0):
        batch_calls.append((state, questions))
        if answer is None:
            return None
        return {qid: answer for qid in questions}

    monkeypatch.setattr(lc, "laya_available", lambda: available)
    monkeypatch.setattr(lc, "laya_batch", fake_batch)
    return batch_calls


CANDS = [{"id": "e1", "name": "memory-service", "type": "component",
          "score": 0.93}]


# ── provider 契约 ────────────────────────────────────────────────────

def test_choice_hit_returns_duplicate_id(monkeypatch):
    calls = _laya(monkeypatch, answer={"choice": "e1", "confidence": 0.8})
    p = LayaJudgeProvider()
    assert p.dedupe_entity("memsvc", "component", CANDS,
                           context="memsvc 是 memory-service 的简称") == \
        {"duplicate_id": "e1"}
    # state 必带 context + 候选清单; criteria key=候选 id
    state, questions = calls[0]
    assert "memsvc 是 memory-service 的简称" in state
    assert "[e1] name=memory-service" in state
    q = questions["dedupe"]
    assert q["type"] == "choice"
    assert q["criteria"] == {"e1": "canonical entity: memory-service (component)"}


def test_low_confidence_no_merge(monkeypatch):
    _laya(monkeypatch, answer={"choice": "e1", "confidence": 0.49})
    p = LayaJudgeProvider()
    assert p.dedupe_entity("memsvc", "component", CANDS, context="ctx") == \
        {"duplicate_id": None}


def test_hallucinated_choice_id_rejected(monkeypatch):
    # choice 指向候选集外的 id → 视同不合并 (resolver 幻觉 guard 的前置层)
    _laya(monkeypatch, answer={"choice": "e999", "confidence": 0.9})
    p = LayaJudgeProvider()
    assert p.dedupe_entity("x", "concept", CANDS, context="ctx") == \
        {"duplicate_id": None}


def test_laya_down_fallback_passthrough_with_context(monkeypatch):
    _laya(monkeypatch, available=False)
    fb = FakeFallback(result={"duplicate_id": "e1"})
    p = LayaJudgeProvider(fallback=fb)
    out = p.dedupe_entity("memsvc", "component", CANDS, context="原句片段")
    assert out == {"duplicate_id": "e1"}
    assert fb.calls == [("memsvc", "component", CANDS, "原句片段")], \
        "context 必须透传 fallback"


def test_batch_none_fallback(monkeypatch):
    _laya(monkeypatch, answer=None)  # laya_batch 整批 None
    fb = FakeFallback(result={"duplicate_id": None})
    p = LayaJudgeProvider(fallback=fb)
    assert p.dedupe_entity("n", "concept", CANDS, context="c") == \
        {"duplicate_id": None}
    assert len(fb.calls) == 1


def test_no_fallback_raises(monkeypatch):
    _laya(monkeypatch, available=False)
    with __import__("pytest").raises(ProviderCallError):
        LayaJudgeProvider().dedupe_entity("n", "concept", CANDS, context="c")


def test_malformed_answer_falls_back(monkeypatch):
    # answer 缺 choice/confidence 键 (noul/score 键错读先例的防线) → fallback
    _laya(monkeypatch, answer={"noul": 1.0})
    fb = FakeFallback(result={"duplicate_id": None})
    p = LayaJudgeProvider(fallback=fb)
    assert p.dedupe_entity("n", "concept", CANDS, context="c") == \
        {"duplicate_id": None}
    assert len(fb.calls) == 1


# ── resolver step2 端到端 (mock 召回 + Laya 裁判) ─────────────────────

def _mock_recall(monkeypatch, eid):
    """step1 不命中 + embedding 非空 + vec 召回固定候选 eid。"""
    import embedding
    import vec_index
    monkeypatch.setattr(embedding, "embed", lambda name, providers=None: [1.0])
    monkeypatch.setattr(vec_index, "heal_entities_if_pending", lambda p=None: None)
    monkeypatch.setattr(vec_index, "entity_topk", lambda emb, k: [(eid, 0.93)])


def test_resolver_merges_on_choice_hit(monkeypatch):
    _fresh_db()
    eid = store.put_entity("memory-service", "component")
    _mock_recall(monkeypatch, eid)
    _laya(monkeypatch, answer={"choice": eid, "confidence": 0.8})
    rid = resolver.resolve_entity("memsvc", "component", providers=[LayaJudgeProvider()],
                                  context="memsvc 即 memory-service")
    assert rid == eid, "choice 命中 → 返回既有实体 id"
    assert "memsvc" in (store.get_entity(eid)["aliases"] or []), \
        "surface form 必须并入 alias"


def test_resolver_creates_new_on_low_confidence(monkeypatch):
    _fresh_db()
    eid = store.put_entity("memory-service", "component")
    _mock_recall(monkeypatch, eid)
    _laya(monkeypatch, answer={"choice": eid, "confidence": 0.49})
    rid = resolver.resolve_entity("memsvc", "component", providers=[LayaJudgeProvider()],
                                  context="memsvc 与其无关")
    assert rid is not None and rid != eid, "低置信 → step3 新建"
    assert store.count_entities() == 2


def test_env_off_original_path(monkeypatch):
    # MEM_LAYA_ENABLED=0 且不 patch laya_client → laya_available() 短路 False
    # → provider 走 fallback (原 LLM 裁判路径)。
    monkeypatch.setenv("MEM_LAYA_ENABLED", "0")
    import laya_client as lc
    lc._avail_cache = None
    _fresh_db()
    fb = FakeFallback(result={"duplicate_id": None})
    out = LayaJudgeProvider(fallback=fb).dedupe_entity(
        "n", "concept", CANDS, context="c")
    assert out == {"duplicate_id": None}
    assert len(fb.calls) == 1, "env 关 → fallback 原路径, 零 Laya 网络"

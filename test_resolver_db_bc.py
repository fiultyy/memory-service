"""D-B 修 (b+c) resolver/llm_provider 单元: context 透传 + exclude_ids 图不变量。

b — 上下文喂裁判: resolve_entity(context=...) → provider.dedupe_entity(context=...)
     透传; resolve_entities_batch 段级 context 广播。
c — exclude_ids: step1 命中被排除 → 拒并继续; step2 候选滤掉排除 id (裁判
     见不到); 全排光 → 新建。

测试规范: def test_xxx() 函数让 pytest 收集。"""
import pytest

import db
import embedding
import resolver
import store


class _RecordingJudge:
    """记录 dedupe_entity 收到的全部实参; 返回可配置 duplicate_id。"""
    def __init__(self, duplicate_id=None):
        self.calls = []
        self._dup = duplicate_id

    def dedupe_entity(self, new_name, new_type, candidates, context=None):
        self.calls.append({"new_name": new_name, "new_type": new_type,
                           "candidates": list(candidates), "context": context})
        return {"duplicate_id": self._dup}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db.init(tmp_path / "mem.db")
    # embedding → []: step2 向量通道关闭, 测 exclude/context 的纯逻辑面
    monkeypatch.setattr(embedding, "embed", lambda *a, **kw: [])
    monkeypatch.setattr(embedding, "embed_batch", lambda *a, **kw: {})
    yield


def test_step1_hit_in_exclude_rejected():
    """step1 精确命中但 id 在排除集 → 视为未命中, 新建独立实体 (宁分离勿自环)。"""
    omp = store.put_entity("omp", "identifier")
    store.add_aliases(omp, ["@oh-my-pi/pi-coding-agent"])

    # 不排除: 正常并入 (对照)
    assert resolver.resolve_entity("@oh-my-pi/pi-coding-agent", "package") == omp
    # 排除 subject_id: 同名解析被拒 → 独立新实体
    split = resolver.resolve_entity("@oh-my-pi/pi-coding-agent", "package",
                                    exclude_ids={omp})
    assert split is not None and split != omp
    sep = store.get_entity(split)
    assert sep["name"] == "@oh-my-pi/pi-coding-agent"


def test_exclude_rejects_only_listed_ids():
    """排除只拒列出的 id — 其他合法命中不受影响。"""
    a = store.put_entity("alpha", "concept")
    store.put_entity("beta", "concept")
    # 排除 beta, 但名字命中 alpha → 正常返回 alpha
    assert resolver.resolve_entity("alpha", "concept", exclude_ids={a + "x", }) == a


def test_context_forwarded_to_judge(monkeypatch):
    """b: resolve_entity(context=) → dedupe_entity(context=) 透传。

    embedding 已置 [] → step2 需要非空 emb — patch resolver 内部路径不可行
    (emb 空则跳过 step2), 故直接测批式接口的透传 (同样走 resolve_entity)。
    改用真向量太重; 此处 patch store.find_entity_exact 返回 None 且 emb 通路
    由 monkeypatch embedding.embed 返回非空固定向量 + _cosine_topk 固定候选。
    """
    judge = _RecordingJudge(duplicate_id=None)
    omp = store.put_entity("omp", "identifier")
    # 固定向量 + 固定候选, 让 step2 可达
    monkeypatch.setattr(embedding, "embed", lambda *a, **kw: [1.0, 0.0])
    monkeypatch.setattr(resolver, "_cosine_topk",
                        lambda emb, k=5, embedding_providers=None: [
                            {"id": omp, "name": "omp", "type": "identifier",
                             "score": 0.9}])
    ctx = "omp 基于 @oh-my-pi/pi-coding-agent 开发"
    resolver.resolve_entity("@oh-my-pi/pi-coding-agent", "package",
                            providers=[judge], context=ctx)
    assert len(judge.calls) == 1
    assert judge.calls[0]["context"] == ctx, "context 必须透传到裁判"


def test_batch_context_broadcast(monkeypatch):
    """b: resolve_entities_batch(context=) 对批内每名生效 (段级共享)。"""
    judge = _RecordingJudge(duplicate_id=None)
    omp = store.put_entity("omp", "identifier")
    monkeypatch.setattr(embedding, "embed", lambda *a, **kw: [1.0, 0.0])
    monkeypatch.setattr(resolver, "_cosine_topk",
                        lambda emb, k=5, embedding_providers=None: [
                            {"id": omp, "name": "omp", "type": "identifier",
                             "score": 0.9}])
    ctx = "段级原文片段"
    resolver.resolve_entities_batch(
        ["name-a", "name-b"], providers=[judge], context=ctx)
    assert len(judge.calls) == 2
    assert all(c["context"] == ctx for c in judge.calls)


def test_step2_candidates_filtered_by_exclude(monkeypatch):
    """c: step2 候选先滤掉排除 id — 裁判连误判机会都没有。"""
    judge = _RecordingJudge(duplicate_id=None)
    omp = store.put_entity("omp", "identifier")
    other = store.put_entity("unrelated", "concept")
    monkeypatch.setattr(embedding, "embed", lambda *a, **kw: [1.0, 0.0])
    monkeypatch.setattr(resolver, "_cosine_topk",
                        lambda emb, k=5, embedding_providers=None: [
                            {"id": omp, "name": "omp", "type": "identifier",
                             "score": 0.9},
                            {"id": other, "name": "unrelated", "type": "concept",
                             "score": 0.5}])
    resolver.resolve_entity("whatever", "concept",
                            providers=[judge], exclude_ids={omp})
    assert len(judge.calls) == 1
    ids = [c["id"] for c in judge.calls[0]["candidates"]]
    assert omp not in ids, "排除 id 不得出现在裁判候选里"
    assert other in ids, "非排除候选保留"


def test_legacy_provider_signature_shim(monkeypatch):
    """旧式 provider (dedupe_entity 无 context 形参) — TypeError 回退三参调用。"""
    omp = store.put_entity("omp", "identifier")

    class _Legacy:
        def __init__(self):
            self.calls = 0

        def dedupe_entity(self, new_name, new_type, candidates):
            self.calls += 1  # (self, name, type, candidates) — 无 context
            return {"duplicate_id": None}

    legacy = _Legacy()
    monkeypatch.setattr(embedding, "embed", lambda *a, **kw: [1.0, 0.0])
    monkeypatch.setattr(resolver, "_cosine_topk",
                        lambda emb, k=5, embedding_providers=None: [
                            {"id": omp, "name": "omp", "type": "identifier",
                             "score": 0.9}])
    out = resolver.resolve_entity("whatever", "concept",
                                  providers=[legacy], context="ctx")
    assert legacy.calls == 1, "旧签名应经 shim 正常调用"
    assert out is not None  # dup=None → 新建

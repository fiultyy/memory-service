"""TICKET B3C-HYG 卫生批: C1/C2/C4 逐项单测 (C3 README 对齐、C5 历史 typo 仅记录)。

- C1: gate.py 注释过期 env 名 (MEM_RECALL_FIRST_TIMEOUT) → 实际 MEM_RECALL_TIMEOUT;
  测试 = 文档漂移守卫 (源码不再出现过期名, 现名在场)。
- C2: embedding.embed_batch 空守卫 — None/空列表早退 [], 零 provider 调用零缓存 IO。
- C4: MEM_UNLOCK_MATCH_SCORE range 校验 — 不可解析回落默认 (既有语义);
  可解析但 ≤0 / 非有限 (inf/nan) → 响亮 ValueError, 报错含 env 名+原值+合法域。

测试规范: def test_xxx() 函数让 pytest 收集 (本项目头号雷区=模块级裸 assert 死代码)。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import embedding
import gate
import store


class _RecordingProvider:
    """记录型 embedding provider: 批式接口, 恒返固定向量。"""
    model = "fake-model"

    def __init__(self):
        self.batch_calls = []

    def embed_batch(self, texts):
        self.batch_calls.append(list(texts))
        return [[0.1, 0.2] for _ in texts]


# ── C1: gate.py 注释 env 名与 hooks 实际面一致 ───────────────────────

def test_c1_gate_comment_env_name_current():
    """文档漂移守卫: gate.py 不得再出现过期 env 名 MEM_RECALL_FIRST_TIMEOUT;
    实际名 MEM_RECALL_TIMEOUT 在场 (hooks/user-prompt-recall.sh 同名)。"""
    src = Path(inspect.getfile(gate)).read_text(encoding="utf-8")
    assert "MEM_RECALL_FIRST_TIMEOUT" not in src, (
        "过期 env 名残留 — 实际 hooks 面是 MEM_RECALL_TIMEOUT")
    assert "MEM_RECALL_TIMEOUT" in src


# ── C2: embed_batch 空守卫 ───────────────────────────────────────────

def test_c2_embed_batch_none_and_empty_guard(monkeypatch):
    """None/空列表 → 早退 [], 零 provider 调用零缓存 IO。"""
    monkeypatch.setattr(embedding, "_cache_store_batch", lambda items: None)
    p = _RecordingProvider()
    assert embedding.embed_batch(None, providers=[p]) == []
    assert embedding.embed_batch([], providers=[p]) == []
    assert p.batch_calls == [], "空输入不得触发任何 provider 调用"


def test_c2_embed_batch_nonempty_still_embeds(monkeypatch):
    """守卫不误伤: 非空输入照常批调 provider 并等长对齐返回。"""
    monkeypatch.setattr(embedding, "_cache_lookup", lambda t: None)
    monkeypatch.setattr(embedding, "_cache_store_batch", lambda items: None)
    p = _RecordingProvider()
    out = embedding.embed_batch(["alpha", "beta"], providers=[p])
    assert out == [[0.1, 0.2], [0.1, 0.2]]
    assert p.batch_calls == [["alpha", "beta"]]


# ── C4: MEM_UNLOCK_MATCH_SCORE range 校验 ────────────────────────────

def test_c4_unlock_threshold_unset_valid_and_unparseable_fallback(monkeypatch):
    """既有语义保持: unset → 2.0; 合法值透传; 不可解析 (非数字) 回落默认。"""
    monkeypatch.delenv("MEM_UNLOCK_MATCH_SCORE", raising=False)
    assert store.unlock_match_score_cap() == 2.0
    monkeypatch.setenv("MEM_UNLOCK_MATCH_SCORE", "3.5")
    assert store.unlock_match_score_cap() == 3.5
    for bad in ("abc", "1.2.3", ""):
        monkeypatch.setenv("MEM_UNLOCK_MATCH_SCORE", bad)
        assert store.unlock_match_score_cap() == 2.0, (
            f"不可解析 {bad!r} 应回落默认 2.0 (不炸)")


def test_c4_unlock_threshold_out_of_range_raises(monkeypatch):
    """可解析但越界 (≤0 / ±inf / nan) → ValueError, 报错含 env 名与原值。"""
    for raw in ("0", "-1", "0.0", "inf", "-inf", "nan"):
        monkeypatch.setenv("MEM_UNLOCK_MATCH_SCORE", raw)
        try:
            store.unlock_match_score_cap()
        except ValueError as e:
            msg = str(e)
            assert "MEM_UNLOCK_MATCH_SCORE" in msg, "报错须含 env 名"
            assert raw in msg, "报错须含原值"
            assert "2.0" in msg, "报错须提示回落方式 (unset → 默认 2.0)"
        else:
            raise AssertionError(f"越界值 {raw!r} 应 raise ValueError, 未抛")

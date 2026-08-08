"""T3: adapter._vote 多 wing topic 合并自验证 (F5).

构造多 wing 投票场景: 同一 (subject,predicate,object) 三元组在不同 wing 有不同 topic
(含空 topic)。验证 surviving edge 取首个非空 wing 的 topic, 不被首 wing 空 topic 遮蔽。

无 DB / 无网络: 直接调用 adapter._vote(纯内存聚合)。db.init 不需要。
"""
import adapter
from llm_provider import EdgeOut, EntityOut, Extraction

_ENTS = [EntityOut("用户", "person"), EntityOut("rust", "tool")]
_S, _P, _O = "用户", "uses", "rust"
_GOOD = "用户使用 rust"  # LLM 生成的干净 topic


# T3 核心: 2 wing, wing0 topic="" + wing1 topic=非空 → quorum=1 → surviving topic 非空
def test_wing0_empty_wing1_nonempty():
    w0 = Extraction(entities=_ENTS,
                    edges=[EdgeOut(_S, _P, _O, topic="")],
                    confidence=0.7, source_meta={})
    w1 = Extraction(entities=_ENTS,
                    edges=[EdgeOut(_S, _P, _O, topic=_GOOD)],
                    confidence=0.8, source_meta={})
    r = adapter._vote([w0, w1])
    assert len(r.edges) == 1, f"应存活 1 edge, got {len(r.edges)}"
    # surface form 仍 first wing (subject case 保留), 但 topic 补全为首非空 wing
    assert r.edges[0].subject == _S and r.edges[0].object == _O
    assert r.edges[0].topic == _GOOD, (
        f"surviving topic 应取首非空 wing={_GOOD!r}, got {r.edges[0].topic!r}")
    print(f"✓ T3 wing0 空 + wing1 非空 → surviving topic={r.edges[0].topic!r}")


# 补充: 3 wing, wing0 空 + wing1/2 非空 → surviving topic 非空 (F5 验收原文场景)
def test_three_wings_first_empty():
    w0 = Extraction(entities=_ENTS, edges=[EdgeOut(_S, _P, _O, topic="")],
                    confidence=0.7, source_meta={})
    w1 = Extraction(entities=_ENTS, edges=[EdgeOut(_S, _P, _O, topic=_GOOD)],
                    confidence=0.7, source_meta={})
    w2 = Extraction(entities=_ENTS, edges=[EdgeOut(_S, _P, _O, topic=_GOOD)],
                    confidence=0.8, source_meta={})
    r = adapter._vote([w0, w1, w2])  # n=3 → quorum=2
    assert len(r.edges) == 1, f"应存活 1 edge, got {len(r.edges)}"
    assert r.edges[0].topic == _GOOD, (
        f"surviving topic 应非空={_GOOD!r}, got {r.edges[0].topic!r}")
    print(f"✓ T3+ 3 wing(首空) → surviving topic={r.edges[0].topic!r}")


# 补充: 全 wing topic 空 → surviving topic="" (行为不变, 投影回退三元组拼接)
def test_all_wings_empty_topic_unchanged():
    w0 = Extraction(entities=_ENTS, edges=[EdgeOut(_S, _P, _O, topic="")],
                    confidence=0.7, source_meta={})
    w1 = Extraction(entities=_ENTS, edges=[EdgeOut(_S, _P, _O, topic="")],
                    confidence=0.7, source_meta={})
    r = adapter._vote([w0, w1])
    assert len(r.edges) == 1, f"应存活 1 edge, got {len(r.edges)}"
    assert r.edges[0].topic == "", (
        f"全空 topic → surviving topic 应保持空(回退三元组), got {r.edges[0].topic!r}")
    print("✓ T3+ 全 wing 空 topic → surviving topic='' (行为不变)")


# 补充: 首 wing 已有非空 topic → 直接用, 不被覆盖
def test_first_wing_nonempty_kept():
    w0 = Extraction(entities=_ENTS, edges=[EdgeOut(_S, _P, _O, topic=_GOOD)],
                    confidence=0.7, source_meta={})
    w1 = Extraction(entities=_ENTS, edges=[EdgeOut(_S, _P, _O, topic="")],
                    confidence=0.8, source_meta={})
    r = adapter._vote([w0, w1])
    assert r.edges[0].topic == _GOOD, (
        f"首 wing 非空 topic 应保留={_GOOD!r}, got {r.edges[0].topic!r}")
    print(f"✓ T3+ 首 wing 非空 → topic 保留={r.edges[0].topic!r}")


if __name__ == "__main__":
    test_wing0_empty_wing1_nonempty()
    test_three_wings_first_empty()
    test_all_wings_empty_topic_unchanged()
    test_first_wing_nonempty_kept()
    print("\n✓ All _vote topic tests passed")

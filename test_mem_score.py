"""mem_score 自验证 (ADR-15 投影排序标量, LIF+confidence 关联). 纯函数, 不触 DB."""
import os
import scoring


def _set(mode):
    if mode is None:
        os.environ.pop("MEM_MEMSCORE_MODE", None)
    else:
        os.environ["MEM_MEMSCORE_MODE"] = mode


# 默认 weighted (0.7 LIF + 0.3 conf)
_set(None)
assert abs(scoring.mem_score({"LIF": 0.5, "confidence": 0.7}) - (0.7 * 0.5 + 0.3 * 0.7)) < 1e-9

# lif mode: 纯 LIF
_set("lif")
assert scoring.mem_score({"LIF": 0.5, "confidence": 0.7}) == 0.5
assert scoring.mem_score({"LIF": 0.5, "confidence": 0.0}) == 0.5  # conf 不影响

# harmonic: 2·c·LIF/(c+LIF)
_set("harmonic")
assert abs(scoring.mem_score({"LIF": 0.5, "confidence": 0.7}) - (2 * 0.7 * 0.5 / (0.7 + 0.5))) < 1e-9
_set("harmonic")
assert scoring.mem_score({"LIF": 0.0, "confidence": 0.7}) == 0.0  # 两低其一即 0
_set("harmonic")
assert scoring.mem_score({"LIF": 0.5, "confidence": 0.0}) == 0.0

# 边界: 全 0 / 缺字段 / clamp
_set("weighted")
assert scoring.mem_score({"LIF": 0.0, "confidence": 0.0}) == 0.0
assert scoring.mem_score({}) == 0.0
assert scoring.mem_score({"LIF": 1.5, "confidence": 2.0}) == 1.0  # clamp

# score_fact γ 项改用 mem_score: dict 含 mem_score 字段
_set("weighted")
s = scoring.score_fact({"LIF": 0.5, "confidence": 0.7, "value": "rust"}, "rust")
assert "mem_score" in s and abs(s["mem_score"] - 0.56) < 1e-9
assert abs(s["score"] - (0.5 * s["match"] + 0.3 * s["centrality"] + 0.2 * s["mem_score"])) < 1e-9

_set(None)
print("✓ mem_score ok (weighted/lif/harmonic + 边界 + score_fact 融合)")

# ── 2026-08-28 dogfood 修复: match 内容 = value + topic ────────────────
# reified 二元 fact 的 value 常只是宾语词 (endsteps→memsvc 的 value='memsvc'),
# 锚词落在 topic → 只看 value 0 match 被 SCORE_FLOOR 砍掉 (recall 实测)。
s = scoring.score_fact(
    {"LIF": 0.5, "confidence": 0.5, "value": "memsvc",
     "topic": "transcript → endsteps 蒸馏 → autodream"},
    "endsteps",
)
assert s["match"] == 1.0, f"topic 锚词应命中: {s['match']}"
assert s["score"] >= scoring.ALPHA_MATCH * 1.0  # 0.5 起步, 远过 0.3 地板
# 无 topic 的旧 fact 语义不变 (value 命中照旧)
s2 = scoring.score_fact({"LIF": 0.5, "value": "rust toolchain"}, "rust")
assert s2["match"] == 1.0
# value 与 topic 都不含锚词 → 仍 0
s3 = scoring.score_fact({"LIF": 0.5, "value": "x", "topic": "y"}, "endsteps")
assert s3["match"] == 0.0
print("✓ score_fact match=value+topic ok (dogfood 修复)")

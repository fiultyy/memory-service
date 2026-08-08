"""ADR-3 ①② 自验证: _now ms-floor 字典序 + cli --as-of 归一覆盖。

Covers:
  ① store._now() 加 .replace(microsecond=0) 后, 秒级 ISO-8601 + 固定 +00:00
     仍保字典序 = 时间序(SQLite TEXT 比较的隐式假设)。
  ② cli._normalize_as_of 归一任意 ISO-8601(Z / +HH:MM / 无后缀 naive) →
     UTC +00:00 秒级; naive 按 UTC 解释; None 透传(default recall 不走点时)。
  ③ 归一后字符串与 store._now() 同格式 → _temporal_clause TEXT 比较不错序。

Pattern: 纯函数单测, 无 db 依赖(_normalize_as_of 不触库)。
"""
import cli
import store


def test_normalize_as_of():

    # ── ① store._now() ms-floor: 字典序 = 时间序 ──
    now_a = store._now()
    # 关键不变量: _now() 输出含 +00:00 后缀(固定 UTC)且无 microsecond 字段
    assert "+00:00" in now_a, f"_now() 应含 +00:00 后缀(固定 UTC), got {now_a}"
    # 秒级 ISO-8601 形如 2026-08-08T15:30:45+00:00 → 长度固定, 无 '.microseconds'
    assert "." not in now_a, f"_now() 应 ms-floor(无 microsecond), got {now_a}"
    print(f"✓ 1a. store._now() = {now_a} (秒级 +00:00, 无 microsecond)")

    # 字典序 = 时间序: 同格式秒级 ISO-8601 字符串比较等价于时间比较
    from datetime import datetime, timezone, timedelta
    t1 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    iso1, iso2, iso3 = (t.replace(microsecond=0).isoformat() for t in (t1, t2, t3))
    assert iso1 < iso2 < iso3, f"字典序应 = 时间序: {iso1} < {iso2} < {iso3} 失败"
    # 边界: 跨年/跨月/跨日/跨时/跨分/跨秒 字典序都对(ISO-8601 定宽字段设计)
    assert datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc).replace(microsecond=0).isoformat() \
        < datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).replace(microsecond=0).isoformat(), \
        "跨年字典序应保持(2025-12-31 < 2026-01-01)"
    print("✓ 1b. 秒级 +00:00 ISO-8601: 字典序 = 时间序(含跨年边界)")


    # ── ② cli._normalize_as_of 归一覆盖 (ADR-3 ② 核心验收) ──
    # (a) Z 后缀 → +00:00
    norm_z = cli._normalize_as_of("2026-08-08T12:00:00Z")
    assert norm_z == "2026-08-08T12:00:00+00:00", f"Z 后缀归一: got {norm_z}"
    print(f"✓ 2a. --as-of Z 后缀 → {norm_z}")

    # (b) +HH:MM 非 UTC 时区 → 转 UTC +00:00 (东八区减 8h, 西五区加 5h)
    norm_plus = cli._normalize_as_of("2026-08-08T20:00:00+08:00")
    assert norm_plus == "2026-08-08T12:00:00+00:00", f"+08:00 → UTC: got {norm_plus}"
    norm_minus = cli._normalize_as_of("2026-08-08T07:00:00-05:00")
    assert norm_minus == "2026-08-08T12:00:00+00:00", f"-05:00 → UTC: got {norm_minus}"
    print(f"✓ 2b. --as-of +08:00 / -05:00 → UTC +00:00 (防字典序错序)")

    # (c) 无后缀 naive → 按 UTC 解释(文档化)
    norm_naive = cli._normalize_as_of("2026-08-08T12:00:00")
    assert norm_naive == "2026-08-08T12:00:00+00:00", f"naive → UTC: got {norm_naive}"
    print(f"✓ 2c. --as-of naive(无后缀) → 按 UTC 解释: {norm_naive}")

    # (d) None 透传(default recall 不走点时查询, 零回归)
    assert cli._normalize_as_of(None) is None, "None 应透传(as_of=None = default recall)"
    print("✓ 2d. --as-of=None 透传 (default recall 零回归)")

    # (e) microsecond 截断(与 store._now() ms-floor 同精度)
    norm_ms = cli._normalize_as_of("2026-08-08T12:00:00.123456+00:00")
    assert norm_ms == "2026-08-08T12:00:00+00:00", f"ms 截断: got {norm_ms}"
    print(f"✓ 2e. --as-of microsecond 截断到秒(与 _now() 同精度): {norm_ms}")


    # ── ③ 归一后格式与 store._now() 一致 → _temporal_clause TEXT 比较不错序 ──
    # store 写的 valid_from 用 _now()(秒级 +00:00); cli 归一后的 as_of 也是秒级 +00:00
    # → 两端同格式同精度 → SQLite TEXT 字典序 = 时间序(recall._temporal_clause 隐式假设成立)
    normalized = cli._normalize_as_of("2026-08-08T12:00:00Z")
    assert normalized.count("+00:00") == 1 and "." not in normalized, (
        f"归一后应与 _now() 同格式(秒级单 +00:00): got {normalized}"
    )
    # 等价输入归一后应严格相等(确定性, 不依赖输入后缀/精度)
    equiv_inputs = [
        "2026-08-08T12:00:00Z",
        "2026-08-08T12:00:00+00:00",
        "2026-08-08T12:00:00",
        "2026-08-08T20:00:00+08:00",
        "2026-08-08T12:00:00.999999+00:00",
    ]
    norms = {cli._normalize_as_of(x) for x in equiv_inputs}
    assert norms == {"2026-08-08T12:00:00+00:00"}, (
        f"等价输入应归一到同一字符串(消除后缀/时区/精度差异): got {norms}"
    )
    print(f"✓ 3. 等价 as_of 输入(Z/+00:00/naive/+08:00/ms) 归一到同一字符串 → 与 _now() 同格式, TEXT 比较不错序")

    print("\n✅ All ADR-3 normalize / ms-floor tests passed.")

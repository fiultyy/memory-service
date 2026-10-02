"""mem_daemon 夜间补扫测试: _h7_distill_sweep 的 atom TTL 退场 + _maybe_dream
的 MEM_LEGACY_DREAM 开关。

tmp db 隔离 (db.init 切连接, 同 test_distill 口径); 时间串与生产同形
(ISO-UTC 秒级 +00:00, 字典序比较)。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓根平铺模块

import db  # noqa: E402
import mem_daemon  # noqa: E402


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


@pytest.fixture
def tdb(tmp_path):
    return db.init(tmp_path / "t.db")


def _atom(conn, text, label="fact", p_dur=0.2, valid_from=None,
          last_seen_at=None, created_at=None):
    # created_at (入图时刻) 是 v3 TTL 时钟兜底 — 对抗审查后锚
    # COALESCE(last_seen_at, created_at), 测试须显式给旧值才能命中过期。
    conn.execute(
        "INSERT INTO atom(text, label, p_dur, valid_from, last_seen_at, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (text, label, p_dur, valid_from, last_seen_at,
         created_at or valid_from))
    conn.commit()


def _valid_to(text):
    row = db.get_conn().execute(
        "SELECT valid_to FROM atom WHERE text=?", (text,)).fetchone()
    return row["valid_to"]


@pytest.fixture(autouse=True)
def _env_clean(monkeypatch):
    monkeypatch.delenv("MEM_LEGACY_DREAM", raising=False)
    monkeypatch.delenv("MEM_ATOM_TTL_PDUR", raising=False)
    monkeypatch.delenv("MEM_ATOM_TTL_DAYS", raising=False)


# ── TTL 退场 ─────────────────────────────────────────────────
def test_ttl_retires_only_low_pdur_expired_non_exempt(tdb, monkeypatch):
    """低 p_dur + 过期 + 非豁免 label 才退场; 高耐久/豁免 label/复现续期/新近 全留。"""
    logs = []
    monkeypatch.setattr(mem_daemon, "_log", logs.append)
    old = _iso(datetime.now(timezone.utc) - timedelta(days=40))
    fresh = _iso(datetime.now(timezone.utc) - timedelta(days=1))
    _atom(tdb, "该退", p_dur=0.2, valid_from=old)                 # 命中全部三条件
    _atom(tdb, "高耐久留", p_dur=0.9, valid_from=old)             # p_dur ≥ 阈
    _atom(tdb, "裁经留", label="judgment", p_dur=0.2, valid_from=old)
    _atom(tdb, "坑案例留", label="experience", p_dur=0.2, valid_from=old)
    _atom(tdb, "偏好留", label="preference", p_dur=0.2, valid_from=old)
    _atom(tdb, "续期留", p_dur=0.2, valid_from=old, last_seen_at=fresh)
    _atom(tdb, "新近留", p_dur=0.2, valid_from=fresh)

    mem_daemon._h7_distill_sweep()

    assert _valid_to("该退") is not None
    for keep in ("高耐久留", "裁经留", "坑案例留", "偏好留", "续期留", "新近留"):
        assert _valid_to(keep) is None, keep
    assert any("TTL 退场" in m and "1 行" in m for m in logs), logs


def test_ttl_env_days_exempts_everything(tdb, monkeypatch):
    """MEM_ATOM_TTL_DAYS 拉大 → 同一过期 atom 不退 (env 可调豁免)。"""
    old = _iso(datetime.now(timezone.utc) - timedelta(days=40))
    _atom(tdb, "env 留", p_dur=0.2, valid_from=old)
    monkeypatch.setenv("MEM_ATOM_TTL_DAYS", "3650")

    mem_daemon._h7_distill_sweep()

    assert _valid_to("env 留") is None


# ── MEM_LEGACY_DREAM 开关 ────────────────────────────────────
class _NoDream:
    def run_cycle(self, **kw):
        raise AssertionError("MEM_LEGACY_DREAM=0 不该调 dream.run_cycle")


def test_maybe_dream_default_skips_run_cycle(monkeypatch, tmp_path):
    """缺省 (0) 跳过 run_cycle; 补扫/卫生照跑 (v1 fact 面遗留与 atom 图零交集)。"""
    calls = []
    monkeypatch.setitem(sys.modules, "dream", _NoDream())
    monkeypatch.setattr(mem_daemon, "_h7_distill_sweep",
                        lambda: calls.append("h7"))
    monkeypatch.setattr(mem_daemon, "_run_hygiene",
                        lambda s, c: calls.append("hygiene") or s)
    logs = []
    monkeypatch.setattr(mem_daemon, "_log", logs.append)

    state = mem_daemon._maybe_dream({}, str(tmp_path))  # last_run=0 → dream 到期

    assert calls == ["h7", "hygiene"]          # 补扫/卫生不受开关影响
    assert state["_dreaming"]["last_run"] > 0  # 水位仍推进 (不再每轮重进)
    assert any("跳过 dream.run_cycle" in m for m in logs), logs


def test_maybe_dream_env_one_runs_cycle(monkeypatch, tmp_path):
    """MEM_LEGACY_DREAM=1 → run_cycle 照跑 (后门保留)。"""
    ran = []

    class Dream:
        def run_cycle(self, **kw):
            ran.append(kw)
            return {"ok": 1}

    monkeypatch.setitem(sys.modules, "dream", Dream())
    monkeypatch.setattr(mem_daemon, "_h7_distill_sweep", lambda: None)
    monkeypatch.setattr(mem_daemon, "_run_hygiene", lambda s, c: s)
    monkeypatch.setenv("MEM_LEGACY_DREAM", "1")

    mem_daemon._maybe_dream({}, str(tmp_path))

    assert ran == [{"source_cwd": None}]

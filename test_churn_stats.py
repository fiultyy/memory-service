"""ADR-5 churn 监控自验证: store.churn_stats + cli.stats。

覆盖: 空库分母为 0 不抛 / 纯 active 比率边界 / supersede+deprecated 混合比 /
cli stats argv seam 聚合 entity+fact 计数。

测试规范: def test_xxx() 函数让 pytest 收集(本项目头号雷区=模块级裸 assert 死代码,
test_bi_temporal/test_bfs_recall 的裸 assert 是历史债勿复制)。
"""
import json
import tempfile
from pathlib import Path

import cli
import db
import store


def _fresh_db():
    """每个用例独立 tmp db, 不污染 data/memory.db。返回 tmpdir(调用方负责清理)。"""
    tmpdir = tempfile.mkdtemp()
    tmppath = Path(tmpdir) / "mem.db"
    db.init(tmppath)
    return tmpdir


def _seed_fact(status: str = "active") -> str:
    """插一条 fact, 走 put_fact 全路径(status 可变, 测 superseded/deprecated)。"""
    eid = store.put_entity("E", "concept")
    return store.put_fact(eid, "is_a", value="v", status=status)


def test_churn_stats_empty_db():
    """空库: 全 0, 比率返回 0.0(分母为 0 不除零)。"""
    tmpdir = _fresh_db()
    try:
        cs = store.churn_stats()
        assert cs == {
            "active": 0.0, "deprecated": 0.0, "superseded": 0.0,
            "supersede_rate": 0.0, "active_ratio": 0.0,
        }
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_churn_stats_all_active():
    """纯 active: supersede_rate=0, active_ratio=1.0。"""
    tmpdir = _fresh_db()
    try:
        _seed_fact("active")
        _seed_fact("active")
        cs = store.churn_stats()
        assert cs["active"] == 2.0
        assert cs["superseded"] == 0.0
        assert cs["deprecated"] == 0.0
        assert cs["supersede_rate"] == 0.0
        assert cs["active_ratio"] == 1.0
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_churn_stats_mixed_supersede_rate():
    """混合 2 active + 1 superseded + 1 deprecated:
    supersede_rate = 1/(2+1) ≈ 0.333; active_ratio = 2/4 = 0.5。"""
    tmpdir = _fresh_db()
    try:
        _seed_fact("active")
        _seed_fact("active")
        _seed_fact("superseded")
        _seed_fact("deprecated")
        cs = store.churn_stats()
        assert cs["active"] == 2.0
        assert cs["superseded"] == 1.0
        assert cs["deprecated"] == 1.0
        assert abs(cs["supersede_rate"] - 1 / 3) < 1e-9
        assert cs["active_ratio"] == 0.5
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_supersede_rate_excludes_deprecated():
    """deprecated(自然 decay)不进 supersede_rate 分母(只数 dups 折叠):
    1 active + 1 superseded + 3 deprecated → rate = 1/(1+1) = 0.5, 非 1/5。"""
    tmpdir = _fresh_db()
    try:
        _seed_fact("active")
        _seed_fact("superseded")
        for _ in range(3):
            _seed_fact("deprecated")
        cs = store.churn_stats()
        assert abs(cs["supersede_rate"] - 0.5) < 1e-9
        assert abs(cs["active_ratio"] - 1 / 5) < 1e-9
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_cli_stats_aggregates_counts():
    """cli.stats (Python seam): 聚合 entities + facts + churn 嵌套。
    2 entity / 2 active + 1 superseded fact → facts=3。"""
    tmpdir = _fresh_db()
    try:
        _seed_fact("active")
        _seed_fact("active")
        _seed_fact("superseded")
        s = cli.stats()
        assert s["entities"] >= 1  # _seed_fact 建过 entity
        assert s["facts"] == 3
        assert set(s["churn"].keys()) == {
            "active", "deprecated", "superseded",
            "supersede_rate", "active_ratio",
        }
        assert s["churn"]["superseded"] == 1.0
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_cli_stats_argv_seam():
    """argv seam: `cli stats` 走 argparse → JSON 输出含 churn.facts 字段。"""
    tmpdir = _fresh_db()
    try:
        _seed_fact("active")
        _seed_fact("superseded")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli._main(["stats"])
        assert rc == 0
        out = json.loads(buf.getvalue())
        assert out["facts"] == 2
        assert out["churn"]["active"] == 1.0
        assert out["churn"]["superseded"] == 1.0
    finally:
        import shutil
        shutil.rmtree(tmpdir)

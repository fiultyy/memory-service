"""手动/文档型 ingest 适配 v2 (graph-reform): bootstrap × distill 接线测试。

mock distill (零 LLM/零网络/零真图写), 锁 bootstrap 侧契约:
- 段切分: 空行切段 / >1500 按句号细分 / 无句号长段整段
- 溯源三件套: session_id="memory:<文件名>" (→ session:memory:* tag 同形)
  / cwd=md 所在目录 / ts=file mtime
- MEMORY.md + source:mem-service 投影跳过 (ADR-16f)
- prune_memory: 独占 atom valid_to 软删 / 多源保护 / scoping / dry_run
- 幂等: 二次 ingest 同文件 → distill 段 sha 去重 → 零新增
真 distill 行为由 tests/test_distill.py 锁, 此处只测 bootstrap 管道。
"""
import hashlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓根平铺模块

import bootstrap  # noqa: E402
import db  # noqa: E402


class StubDistill:
    """记录型 distill stub: 段文本 sha 记忆模拟 distill_seen 幂等。"""

    def __init__(self, results=None):
        self.calls = []            # (text, session_id, cwd, ts)
        self.seen: set[str] = set()
        self.results = list(results or [])

    def distill_segment(self, text, session_id, cwd, ts):
        self.calls.append((text, session_id, cwd, ts))
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if sha in self.seen:
            return {"atoms": 0, "edges": 0, "merged": 0,
                    "supersede_proposals": [], "skipped": "seen"}
        self.seen.add(sha)
        if self.results:
            return dict(self.results.pop(0))
        return {"atoms": 1, "edges": 0, "merged": 0, "supersede_proposals": []}


@pytest.fixture
def stub(monkeypatch):
    s = StubDistill()
    monkeypatch.setattr(bootstrap, "distill_mod", s)
    return s


@pytest.fixture
def tdb(tmp_path):
    return db.init(tmp_path / "t.db")  # 隔离: 不触生产 data/memory.db


# ── 段切分 ────────────────────────────────────────────────────
def test_segments_blank_line_split():
    text = "第一段句子一。句子二。\n\n第二段句子三。"
    assert bootstrap._segments(text) == ["第一段句子一。句子二。", "第二段句子三。"]


def test_segments_long_split_by_period():
    seg = "句。" * 800  # 1600 字 > 1500 帽 → 按句号细分
    got = bootstrap._segments(seg)
    assert len(got) > 1
    assert all(len(s) <= 1500 for s in got)
    assert "".join(got) == seg  # 切分不丢字


def test_segments_long_without_period_stays_whole():
    seg = "长" * 2000  # 无句号 → 整段 (硬帽是软目标)
    assert bootstrap._segments(seg) == [seg]


def test_segments_empty():
    assert bootstrap._segments("") == []
    assert bootstrap._segments("\n\n  \n\n") == []


# ── re_ingest_file: 溯源三件套 + 投影跳过 ──────────────────────
def test_re_ingest_source_triple(tmp_path, stub):
    md = tmp_path / "note.md"
    md.write_text("第一段。\n\n第二段。", encoding="utf-8")
    mtime = md.stat().st_mtime
    r = bootstrap.re_ingest_file(md)
    assert (r["segments"], r["atoms"]) == (2, 2)
    assert len(stub.calls) == 2
    texts = [c[0] for c in stub.calls]
    assert texts == ["第一段。", "第二段。"]
    for _, session_id, cwd, ts in stub.calls:
        assert session_id == "memory:note.md"  # → tag session:memory:note.md (存量同形)
        assert cwd == str(tmp_path)            # cwd = md 所在目录
        assert ts == time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(mtime))


def test_re_ingest_skips_projections(tmp_path, stub):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "MEMORY.md").write_text("# 索引", encoding="utf-8")
    (d / "mem-x.md").write_text("---\nsource: mem-service\n---\n投影", encoding="utf-8")
    (d / "native.md").write_text("原生内容", encoding="utf-8")
    r1 = bootstrap.re_ingest_file(d / "MEMORY.md")
    r2 = bootstrap.re_ingest_file(d / "mem-x.md")
    r3 = bootstrap.re_ingest_file(d / "native.md")
    assert r1["skipped"] == 1 and r2["skipped"] == 1
    assert r1["atoms"] == r2["atoms"] == 0
    assert r3["atoms"] == 1
    # 只有 native 内容到 distill
    assert [c[0] for c in stub.calls] == ["原生内容"]


def test_re_ingest_not_a_file(stub):
    r = bootstrap.re_ingest_file("/nonexistent/x.md")
    assert r["error"] and r["atoms"] == 0
    assert stub.calls == []


def test_re_ingest_suspends_on_laya_unavailable(tmp_path, monkeypatch):
    """distill 外呼不可用 → LayaUnavailable 族穿出 (挂起等恢复), 不吞。"""
    from src.distill import LayaUnavailable

    class Down:
        def distill_segment(self, *a, **k):
            raise LayaUnavailable("down")

    monkeypatch.setattr(bootstrap, "distill_mod", Down())
    md = tmp_path / "x.md"
    md.write_text("句子。", encoding="utf-8")
    with pytest.raises(LayaUnavailable):
        bootstrap.re_ingest_file(md)


# ── init_memory: 逐文件 re_ingest + atom 口径计数 ───────────────
def test_init_memory_atom_counts(tmp_path, stub):
    d = tmp_path / "memdir"
    d.mkdir()
    (d / "a.md").write_text("a 段一。\n\na 段二。", encoding="utf-8")
    (d / "b.md").write_text("b 段。", encoding="utf-8")
    (d / "MEMORY.md").write_text("索引", encoding="utf-8")
    r = bootstrap.init_memory(d)
    assert r["files"] == 2 and r["skipped"] == 1
    assert r["segments"] == 3 and r["atoms"] == 3
    assert [c[1] for c in stub.calls] == ["memory:a.md", "memory:a.md", "memory:b.md"]


def test_init_memory_not_a_dir(tmp_path, stub):
    r = bootstrap.init_memory(tmp_path / "nope")
    assert r["files"] == 0 and r["error"]
    assert stub.calls == []


# ── 幂等: 二次 ingest 同文件 sha 去重零新增 ────────────────────
def test_re_ingest_idempotent_second_run(tmp_path, stub):
    md = tmp_path / "note.md"
    md.write_text("句子一。\n\n句子二。", encoding="utf-8")
    r1 = bootstrap.re_ingest_file(md)
    assert (r1["atoms"], r1["skipped_segs"]) == (2, 0)
    r2 = bootstrap.re_ingest_file(md)
    assert (r2["atoms"], r2["skipped_segs"]) == (0, 2)  # distill_seen sha 判重
    assert len(stub.calls) == 4  # 段仍送 distill (sha 判重在 distill 内)


# ── prune_memory: 双时态软删 + 多源保护 + scoping ───────────────
def _seed_atom(conn, text, files, cwd):
    """直插 atom + session:memory:<file> 事实tag + 挂载 (distill 产出形态)。"""
    conn.execute(
        "INSERT INTO atom(text, label, source_refs, source_cwd) VALUES(?, 'fact', '[]', ?)",
        (text, cwd))
    aid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for f in files:
        tname = f"session:memory:{f}"
        conn.execute("INSERT OR IGNORE INTO tag(name, kind, level) "
                     "VALUES(?, 'factual', 1)", (tname,))
        tid = conn.execute("SELECT id FROM tag WHERE name=?", (tname,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO tag_mount(tag_id, atom_id, w) "
                     "VALUES(?, ?, 1.0)", (tid, aid))
    return aid


def _valid_to(conn, aid):
    return conn.execute("SELECT valid_to FROM atom WHERE id=?", (aid,)).fetchone()[0]


def test_prune_soft_deletes_exclusive_atom(tmp_path):
    conn = db.init(tmp_path / "p.db")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "foo.md").write_text("x", encoding="utf-8")
    (mem_dir / "bar.md").write_text("x", encoding="utf-8")
    a_foo = _seed_atom(conn, "foo 独占", ["foo.md"], str(mem_dir))
    a_multi = _seed_atom(conn, "foo+bar 多源", ["foo.md", "bar.md"], str(mem_dir))

    (mem_dir / "foo.md").unlink()
    r = bootstrap.prune_memory(mem_dir, source_cwd="/proj/x")
    assert r["pruned"] == 1 and r["pruned_ids"] == [a_foo], r
    assert _valid_to(conn, a_foo) is not None      # 独占 → 软删
    assert _valid_to(conn, a_multi) is None        # 多源留一 → 不动


def test_prune_all_sources_gone(tmp_path):
    conn = db.init(tmp_path / "p.db")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "foo.md").write_text("x", encoding="utf-8")
    (mem_dir / "bar.md").write_text("x", encoding="utf-8")
    a_multi = _seed_atom(conn, "foo+bar", ["foo.md", "bar.md"], str(mem_dir))
    (mem_dir / "foo.md").unlink()
    (mem_dir / "bar.md").unlink()
    r = bootstrap.prune_memory(mem_dir)
    assert r["pruned"] == 1 and r["pruned_ids"] == [a_multi]  # 源全没了 → 删


def test_prune_scoped_by_source_cwd(tmp_path):
    """跨项目保护: 其他 source_cwd / NULL 的 atom 不碰 (同旧 prune 纪律)。"""
    conn = db.init(tmp_path / "p.db")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    a_mine = _seed_atom(conn, "本项目 md 目录", ["gone.md"], str(mem_dir))
    a_proj = _seed_atom(conn, "存量迁移 (项目 cwd)", ["gone.md"], "/home/yy/projects/memsvc")
    a_null = _seed_atom(conn, "老数据 NULL cwd", ["gone.md"], None)
    a_other = _seed_atom(conn, "别的项目 md 目录", ["gone.md"], "/other/memory")

    r = bootstrap.prune_memory(mem_dir, source_cwd="/home/yy/projects/memsvc")
    assert set(r["pruned_ids"]) == {a_mine, a_proj}, r
    assert _valid_to(conn, a_null) is None and _valid_to(conn, a_other) is None


def test_prune_ignores_non_memory_tags_and_dead_atoms(tmp_path):
    conn = db.init(tmp_path / "p.db")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    # 非 memory session tag (spool 轨迹) → 不在候选
    conn.execute("INSERT INTO atom(text, label) VALUES('轨迹句', 'fact')")
    aid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO tag(name, kind, level) VALUES('session:abc', 'factual', 1)")
    tid = conn.execute("SELECT id FROM tag WHERE name='session:abc'").fetchone()[0]
    conn.execute("INSERT INTO tag_mount(tag_id, atom_id, w) VALUES(?, ?, 1.0)", (tid, aid))
    # 已软删 atom (valid_to 已设) → 不重复动
    a_dead = _seed_atom(conn, "已死独占", ["gone.md"], str(mem_dir))
    conn.execute("UPDATE atom SET valid_to='2026-01-01T00:00:00+00:00' WHERE id=?", (a_dead,))

    r = bootstrap.prune_memory(mem_dir)
    assert r["pruned"] == 0, r
    assert _valid_to(conn, aid) is None
    assert _valid_to(conn, a_dead) == "2026-01-01T00:00:00+00:00"


def test_prune_dry_run_and_dir_gone(tmp_path):
    conn = db.init(tmp_path / "p.db")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "foo.md").write_text("x", encoding="utf-8")
    a = _seed_atom(conn, "foo 独占", ["foo.md"], str(mem_dir))
    (mem_dir / "foo.md").unlink()

    r = bootstrap.prune_memory(mem_dir, dry_run=True)
    assert r["dry_run"] is True and r["pruned"] == 1 and r["pruned_ids"] == [a]
    assert _valid_to(conn, a) is None  # dry_run 不写

    r2 = bootstrap.prune_memory(mem_dir)  # 真删
    assert r2["pruned"] == 1
    assert _valid_to(conn, a) is not None


def test_prune_excludes_projection_files_from_existing(tmp_path):
    """现存集排除投影 (native_md_present): 投影文件在场不算源存活。"""
    conn = db.init(tmp_path / "p.db")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    # 撞名投影: mem-{4hex}-{slug}.md + source frontmatter → 不算 existing
    (mem_dir / "mem-0011-note.md").write_text(
        "---\nsource: mem-service\nfact_id: 0011\n---\n投影", encoding="utf-8")
    a = _seed_atom(conn, "note 源", ["mem-0011-note.md"], str(mem_dir))
    r = bootstrap.prune_memory(mem_dir)
    assert "mem-0011-note.md" not in r["native_md_present"]
    assert r["pruned"] == 1 and r["pruned_ids"] == [a]


# ── 挂起零污染: re_ingest 不 swallow distill 异常 ───────────────
def test_init_memory_propagates_suspend(tmp_path, monkeypatch):
    class Down:
        def distill_segment(self, *a, **k):
            raise RuntimeError("LayaUnavailable: down")

    monkeypatch.setattr(bootstrap, "distill_mod", Down())
    d = tmp_path / "memdir"
    d.mkdir()
    (d / "x.md").write_text("句子。", encoding="utf-8")
    with pytest.raises(RuntimeError, match="down"):
        bootstrap.init_memory(d)

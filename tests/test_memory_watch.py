"""memory_watch 单测 (T4 泛化双端): 文件级水位幂等 / 段切 / frontmatter / 挂起 /
毒文件 / 批帽 / 目录发现。mock 模式同 test_semantic_chunk。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "src")
sys.path.insert(0, ".")

import memory_watch as MW
import db


@pytest.fixture(autouse=True)
def _cc_source_off(monkeypatch):
    """T4: 缺省关 CC 源 — 防单测真扫生产 ~/.claude/projects/*/memory/
    (个别 CC 用例显式重设)。"""
    monkeypatch.setenv("MEM_CC_MEMORY_ROOT", "")

TOPIC = """---
name: 482-shuiwu
description: 482 签证期间双线税务策略: 768-R 豁免难站住
metadata:
  type: reference
---

# 482 税务策略

## 线路 1
- Section 768-R 境外收入 NANE
- 服务执行地判定来源, 澳洲远程大概率澳洲来源

## 结论
豁免难站住, 不作核心策略。
"""


@pytest.fixture
def tdb(tmp_path):
    db.init(tmp_path / "t.db")
    yield db.get_conn()


def _ws(tmp_path, name="claw-02", files=None):
    d = tmp_path / "root" / f"workspace-{name}" / "memory"
    d.mkdir(parents=True)
    for fn, text in (files or {}).items():
        (d / fn).write_text(text, encoding="utf-8")
    return d.parent.parent  # root


def _mock_chunk(monkeypatch, calls=None):
    def fake(chunk_text, gist, session_id, cwd, ts):
        if calls is not None:
            calls.append((chunk_text, gist, session_id))
        return {"atoms": 1, "edges": 0, "merged": 0,
                "supersede_proposals": []}
    monkeypatch.setattr(MW, "distill_chunk", fake)


def test_watermark_idempotent(tmp_path, tdb, monkeypatch):
    """首扫 ingest → 二扫零调用 (文件级水位快路径)。"""
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(_ws(tmp_path, files={"a.md": TOPIC})))
    calls = []
    _mock_chunk(monkeypatch, calls)
    r1 = MW.sweep()
    assert r1["files"] == 1 and len(calls) == 1
    r2 = MW.sweep()
    assert r2["files"] == 0 and len(calls) == 1


def test_increment_changed_file(tmp_path, tdb, monkeypatch):
    """文件变更 → 重过; 未变邻居不重过。"""
    monkeypatch.setenv("MEM_OPENCLAW_ROOT",
                       str(_ws(tmp_path, files={"a.md": TOPIC, "b.md": "记 B。"})))
    calls = []
    _mock_chunk(monkeypatch, calls)
    MW.sweep()
    n = len(calls)
    (tmp_path / "root" / "workspace-claw-02" / "memory" / "a.md").write_text(
        TOPIC + "\n新增一段。\n", encoding="utf-8")
    MW.sweep()
    assert len(calls) == n + 1  # 只有 a.md 重过 (b.md 水位跳过)


def test_chunk_md_h2_split_gist():
    """>6000 字按 H2 切, 首段 gist=description, 其余=标题行。"""
    body = "---\ndescription: 测述\n---\n\n# 题\n\n## 甲\n" + "甲" * 3500 + \
        "\n\n## 乙\n" + "乙" * 3500
    segs = MW._chunk_md(body)
    assert len(segs) == 2
    assert all(len(s) <= 6000 for s, _ in segs)
    assert segs[0][1] == "测述"
    assert segs[1][1] == "题: 乙"


def test_chunk_md_short_and_bad_frontmatter():
    """短文单段取 description; 坏 frontmatter 兜底首行标题。"""
    assert MW._chunk_md(TOPIC)[0][1] == "482 签证期间双线税务策略: 768-R 豁免难站住"
    segs = MW._chunk_md("# 兜底标题\n\n正文。")
    assert segs[0][1] == "兜底标题"
    assert MW._chunk_md("---\n坏块无闭合\n正文")  # 不炸, 整文单段


def test_suspend_no_watermark_no_attempts(tmp_path, tdb, monkeypatch):
    """LayaUnavailable 上抛 → 水位不推进不计 attempts (下轮重试)。"""
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(_ws(tmp_path, files={"a.md": TOPIC})))

    def boom(*a, **k):
        from distill import LayaUnavailable
        raise LayaUnavailable("laya down")
    monkeypatch.setattr(MW, "distill_chunk", boom)
    with pytest.raises(MW.LayaUnavailable):
        MW.sweep()
    assert db.get_conn().execute(
        "SELECT COUNT(*) FROM openclaw_seen").fetchone()[0] == 0
    # 恢复后同文件重吃
    _mock_chunk(monkeypatch, [])
    assert MW.sweep()["files"] == 1


def test_poison_after_3(tmp_path, tdb, monkeypatch):
    """3 试失败 → poison → 后续 sweep 跳过。"""
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(_ws(tmp_path, files={"a.md": TOPIC})))
    monkeypatch.setattr(MW, "distill_chunk",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    for _ in range(3):
        MW.sweep()
    row = db.get_conn().execute(
        "SELECT attempts, status FROM openclaw_seen").fetchone()
    assert row[0] == 3 and row[1] == "poison"
    calls = []
    _mock_chunk(monkeypatch, calls)
    assert MW.sweep()["files"] == 0  # poison 跳过
    assert not calls


def test_batch_cap(tmp_path, tdb, monkeypatch):
    """批帽: 25 文件一轮 20, 剩 5 下轮。"""
    files = {f"m{i}.md": f"记 {i}。" for i in range(25)}
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(_ws(tmp_path, files=files)))
    calls = []
    _mock_chunk(monkeypatch, calls)
    r = MW.sweep()
    assert r["files"] == 20 and r["skipped"] == 5
    assert MW.sweep()["files"] == 5


def test_missing_memory_dir_and_disabled(tmp_path, tdb, monkeypatch):
    """无 memory/ 的 workspace 不炸; ROOT 空 → 零扫描。"""
    (tmp_path / "root" / "workspace-empty").mkdir(parents=True)
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(tmp_path / "root"))
    assert MW.sweep()["files"] == 0
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", "")
    assert MW.sweep() == {"files": 0, "segments": 0, "skipped": 0}


def test_hard_timeout_counts_attempt(tmp_path, tdb, monkeypatch):
    """段消费超 420s 硬超时 → 按文件失败计 attempts (生产卡死实录守护)。"""
    monkeypatch.setattr(MW, "_SEG_TIMEOUT", 0.05)
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(_ws(tmp_path, files={"a.md": TOPIC})))
    import time

    def hang(*a, **k):
        time.sleep(1.0)
        return {"atoms": 0}
    monkeypatch.setattr(MW, "distill_chunk", hang)
    MW.sweep()  # 超时 raise → except Exception → attempts=1 不炸
    r = db.get_conn().execute(
        "SELECT attempts, status FROM openclaw_seen").fetchone()
    assert tuple(r) == (1, "ok")


def test_multi_workspace(tmp_path, tdb, monkeypatch):
    """多 workspace 全发现, session_id 带 workspace 名。"""
    _ws(tmp_path, "claw-02", {"a.md": "记 A。"})
    _ws(tmp_path, "english-expert", {"b.md": "note B."})
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(tmp_path / "root"))
    calls = []
    _mock_chunk(monkeypatch, calls)
    assert MW.sweep()["files"] == 2
    assert {c[2] for c in calls} == {"openclaw:claw-02", "openclaw:english-expert"}


def test_skip_projection_artifacts(tmp_path, tdb, monkeypatch):
    """T4 防自指: MEMORY.md + mem-{4hex}-*.md 散件 + recall-<日期>.md 投影
    报告 (GA review) 永不进 ingest 车道; mem-aa11/mem-dead 合法 4hex 跳,
    recall-20260901.md 投影报告跳, recall-trail-*.md 真知识吃。"""
    monkeypatch.setenv(
        "MEM_OPENCLAW_ROOT",
        str(_ws(tmp_path, files={
            "MEMORY.md": "# Index\n- [x](memory/mem-aa11-y.md) — t\n",
            "mem-aa11-some-slug.md": "---\natom_id: 12\n---\n投影散件\n",
            "mem-dead-beef.md": "dead 也是合法 hex → 跳",
            "recall-20260901.md": "# 召回投影报告 (自指防)\n",
            "recall-trail-real.md": "---\ndescription: 真知识\n---\n# 真知识文件\n吃\n",
            "mem-xyz9-nothex.md": "---\ndescription: 普通文件\n---\n非散件 pattern → 吃",
            "topics-real.md": "真记忆 ✓",
        })))
    calls = []
    _mock_chunk(monkeypatch, calls)
    r = MW.sweep()
    assert r["files"] == 3          # mem-xyz9-nothex + recall-trail-real + topics-real
    assert not any("MEMORY" in c[0] or "mem-aa11" in c[0]
                   or "mem-dead" in c[0] or "recall-2026" in c[0] for c in calls)
    # 水位表无投影产物记录 (跳过在发现层, 不占 attempts)
    rows = [row[0] for row in db.get_conn().execute(
        "SELECT path FROM openclaw_seen")]
    assert not any(p.endswith("MEMORY.md") or "mem-aa11" in p
                   or "mem-dead" in p or "recall-2026" in p for p in rows)


def test_cc_root_discovery_and_session_id(tmp_path, tdb, monkeypatch):
    """T4 CC 源: {MEM_CC_MEMORY_ROOT}/*/memory/ 发现, session_id=cc:<enc>;
    显式空 env = 源关。"""
    enc = tmp_path / "projects" / "-home-yy-proj-x"
    (enc / "memory").mkdir(parents=True)
    (enc / "memory" / "note.md").write_text("---\ndescription: cc md\n---\n# t\ncc 记忆\n",
                                            encoding="utf-8")
    monkeypatch.setenv("MEM_CC_MEMORY_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", "")
    calls = []
    _mock_chunk(monkeypatch, calls)
    assert MW.sweep()["files"] == 1
    assert calls[0][2] == "cc:-home-yy-proj-x"
    # 显式空 → 关
    monkeypatch.setenv("MEM_CC_MEMORY_ROOT", "")
    assert MW.sweep() == {"files": 0, "segments": 0, "skipped": 0}


def test_delete_reaps_atoms(tmp_path, tdb, monkeypatch):
    """#14 删除回传: 文件删 → 该文件 atoms valid_to 软删 + 边/水位清,
    前缀外路径不误删 (env 挪走防全扫射)。"""
    root = _ws(tmp_path, files={"a.md": TOPIC, "b.md": "记 B。"})
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(root))
    _mock_chunk(monkeypatch, [])
    MW.sweep()
    conn = db.get_conn()
    # 造文件级溯源 atoms (真实形态: source_cwd=文件全路径) + 一条边
    md = root / "workspace-claw-02" / "memory"
    a1 = conn.execute(
        "INSERT INTO atom(text, label, source_cwd, valid_from) "
        "VALUES('甲事实', 'fact', ?, '2026-10-08T00:00:00+00:00')",
        (str(md / "a.md"),)).lastrowid
    a2 = conn.execute(
        "INSERT INTO atom(text, label, source_cwd, valid_from) "
        "VALUES('乙事实', 'fact', ?, '2026-10-08T00:00:00+00:00')",
        (str(md / "gone.md"),)).lastrowid  # 从未在盘上 (＝已删除形态)
    conn.execute("INSERT INTO atom_edge(a_id, b_id, w) VALUES(?,?,0.9)",
                 (min(a1, a2), max(a1, a2)))
    conn.execute(
        "INSERT INTO openclaw_seen(path, sha256, updated_at) "
        "VALUES(?, 'dead', '2026-10-08T00:00:00+00:00')", (str(md / "gone.md"),))
    conn.commit()
    r = MW.sweep()
    assert r["reaped"] == 1
    row = conn.execute("SELECT valid_to FROM atom WHERE id=?", (a2,)).fetchone()
    assert row[0] is not None                      # 软删 (bi-temporal)
    assert conn.execute("SELECT valid_to FROM atom WHERE id=?",
                        (a1,)).fetchone()[0] is None  # 在世文件不动
    assert conn.execute("SELECT COUNT(*) FROM atom_edge").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM openclaw_seen WHERE path LIKE '%gone%'"
    ).fetchone()[0] == 0                            # 水位行清
    # env 挪走 (前缀外) → 同一行不再被扫射 (已清, 用另一目录验证)
    (md / "a.md").unlink()
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(tmp_path / "elsewhere-not-exist"))
    assert MW.sweep().get("reaped", 0) == 0  # dirs 空 early return, a.md 溯源幸存
    assert conn.execute("SELECT valid_to FROM atom WHERE id=?",
                        (a1,)).fetchone()[0] is None

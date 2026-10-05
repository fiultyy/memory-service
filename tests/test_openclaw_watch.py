"""openclaw_watch 单测: 文件级水位幂等 / 段切 / frontmatter / 挂起 /
毒文件 / 批帽 / 目录发现。mock 模式同 test_semantic_chunk。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "src")
sys.path.insert(0, ".")

import openclaw_watch as OW
import db

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
    monkeypatch.setattr(OW, "distill_chunk", fake)


def test_watermark_idempotent(tmp_path, tdb, monkeypatch):
    """首扫 ingest → 二扫零调用 (文件级水位快路径)。"""
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(_ws(tmp_path, files={"a.md": TOPIC})))
    calls = []
    _mock_chunk(monkeypatch, calls)
    r1 = OW.sweep()
    assert r1["files"] == 1 and len(calls) == 1
    r2 = OW.sweep()
    assert r2["files"] == 0 and len(calls) == 1


def test_increment_changed_file(tmp_path, tdb, monkeypatch):
    """文件变更 → 重过; 未变邻居不重过。"""
    monkeypatch.setenv("MEM_OPENCLAW_ROOT",
                       str(_ws(tmp_path, files={"a.md": TOPIC, "b.md": "记 B。"})))
    calls = []
    _mock_chunk(monkeypatch, calls)
    OW.sweep()
    n = len(calls)
    (tmp_path / "root" / "workspace-claw-02" / "memory" / "a.md").write_text(
        TOPIC + "\n新增一段。\n", encoding="utf-8")
    OW.sweep()
    assert len(calls) == n + 1  # 只有 a.md 重过 (b.md 水位跳过)


def test_chunk_md_h2_split_gist():
    """>6000 字按 H2 切, 首段 gist=description, 其余=标题行。"""
    body = "---\ndescription: 测述\n---\n\n# 题\n\n## 甲\n" + "甲" * 3500 + \
        "\n\n## 乙\n" + "乙" * 3500
    segs = OW._chunk_md(body)
    assert len(segs) == 2
    assert all(len(s) <= 6000 for s, _ in segs)
    assert segs[0][1] == "测述"
    assert segs[1][1] == "题: 乙"


def test_chunk_md_short_and_bad_frontmatter():
    """短文单段取 description; 坏 frontmatter 兜底首行标题。"""
    assert OW._chunk_md(TOPIC)[0][1] == "482 签证期间双线税务策略: 768-R 豁免难站住"
    segs = OW._chunk_md("# 兜底标题\n\n正文。")
    assert segs[0][1] == "兜底标题"
    assert OW._chunk_md("---\n坏块无闭合\n正文")  # 不炸, 整文单段


def test_suspend_no_watermark_no_attempts(tmp_path, tdb, monkeypatch):
    """LayaUnavailable 上抛 → 水位不推进不计 attempts (下轮重试)。"""
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(_ws(tmp_path, files={"a.md": TOPIC})))

    def boom(*a, **k):
        from distill import LayaUnavailable
        raise LayaUnavailable("laya down")
    monkeypatch.setattr(OW, "distill_chunk", boom)
    with pytest.raises(OW.LayaUnavailable):
        OW.sweep()
    assert db.get_conn().execute(
        "SELECT COUNT(*) FROM openclaw_seen").fetchone()[0] == 0
    # 恢复后同文件重吃
    _mock_chunk(monkeypatch, [])
    assert OW.sweep()["files"] == 1


def test_poison_after_3(tmp_path, tdb, monkeypatch):
    """3 试失败 → poison → 后续 sweep 跳过。"""
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(_ws(tmp_path, files={"a.md": TOPIC})))
    monkeypatch.setattr(OW, "distill_chunk",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    for _ in range(3):
        OW.sweep()
    row = db.get_conn().execute(
        "SELECT attempts, status FROM openclaw_seen").fetchone()
    assert row[0] == 3 and row[1] == "poison"
    calls = []
    _mock_chunk(monkeypatch, calls)
    assert OW.sweep()["files"] == 0  # poison 跳过
    assert not calls


def test_batch_cap(tmp_path, tdb, monkeypatch):
    """批帽: 25 文件一轮 20, 剩 5 下轮。"""
    files = {f"m{i}.md": f"记 {i}。" for i in range(25)}
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(_ws(tmp_path, files=files)))
    calls = []
    _mock_chunk(monkeypatch, calls)
    r = OW.sweep()
    assert r["files"] == 20 and r["skipped"] == 5
    assert OW.sweep()["files"] == 5


def test_missing_memory_dir_and_disabled(tmp_path, tdb, monkeypatch):
    """无 memory/ 的 workspace 不炸; ROOT 空 → 零扫描。"""
    (tmp_path / "root" / "workspace-empty").mkdir(parents=True)
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(tmp_path / "root"))
    assert OW.sweep()["files"] == 0
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", "")
    assert OW.sweep() == {"files": 0, "segments": 0, "skipped": 0}


def test_multi_workspace(tmp_path, tdb, monkeypatch):
    """多 workspace 全发现, session_id 带 workspace 名。"""
    _ws(tmp_path, "claw-02", {"a.md": "记 A。"})
    _ws(tmp_path, "english-expert", {"b.md": "note B."})
    monkeypatch.setenv("MEM_OPENCLAW_ROOT", str(tmp_path / "root"))
    calls = []
    _mock_chunk(monkeypatch, calls)
    assert OW.sweep()["files"] == 2
    assert {c[2] for c in calls} == {"openclaw:claw-02", "openclaw:english-expert"}

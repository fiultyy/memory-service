"""M18 recall log 投影: recall-<DATE>.md 正文 + MEMORY.md 索引行 (用户裁决 2026-08-27)。

覆盖:
1. project_recall: 骨架(frontmatter source: mem-service-recall) + 每查询一节追加。
2. 同日多查询 → 单文件多节; MEMORY.md 索引行幂等(同日只一行)。
3. 跨日 → 新文件 + MEMORY.md 第二行。
4. ADR-16f 自指防线: recall-*.md 被 _is_mem_service_projection 判 True
   (init-memory/re-ingest 扫描跳过, 防自指循环)。
5. value 截断 >200; score/mem_path 附注。
6. cli.recall(project=True) 端到端: tmp db 隔离 + monkeypatch HOME →
   cc_memory_dir(cwd) 下 recall-*.md + MEMORY.md 双产物。
7. synthesis_index 只删 ](mem- 行 — recall 索引行在其重写后幸存(两族正交)。
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bootstrap
import cli
import db
import projection


def _fact(fid="ab12cd34ef", topic="t", value="v", score=None, mem_path=None):
    f = {"id": fid, "subject_id": None, "predicate": "rel",
         "value": value, "topic": topic}
    if score is not None or mem_path is not None:
        f["_snaptag"] = {"display": topic, "topic": topic,
                         "mem_path": mem_path}
        if score is not None:
            f["score"] = score
    return f


def test_project_recall_skeleton_and_sections(tmp_path):
    now = datetime(2026, 8, 27, 14, 32)
    r = projection.project_recall(tmp_path, "query one", [_fact(topic="甲结论", value="值1")], now=now)
    assert r["recall_file"] == "recall-20260827.md"
    assert r["appended"] == 1 and r["index_added"] is True
    text = (tmp_path / "recall-20260827.md").read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "source: mem-service-recall" in text
    assert "# Recall log 2026-08-27" in text
    assert "## 14:32 — query one" in text
    assert "1. 甲结论 — 值1" in text
    assert "<memsvc-recall>" in text and "</memsvc-recall>" in text  # 出端打标
    # MEMORY.md 骨架 + 索引行
    mem = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "## KG recall logs" in mem
    assert "- [recall 2026-08-27](recall-20260827.md)" in mem


def test_recall_sections_wrapped_marker_reingest_dropped(tmp_path):
    """出端闭环: 正文节包 <memsvc-recall> → corpus_prep 重进语料整块丢弃
    (前端骨架与 MEMORY.md 索引行不打标)。"""
    from corpus_prep import clean
    projection.project_recall(tmp_path, "q1", [_fact(topic="甲", value="v1")],
                              now=datetime(2026, 8, 28, 10, 0))
    projection.project_recall(tmp_path, "q2", [_fact(topic="乙", value="v2")],
                              now=datetime(2026, 8, 28, 11, 0))
    text = (tmp_path / "recall-20260828.md").read_text(encoding="utf-8")
    assert text.index("# Recall log") < text.index("<memsvc-recall>")  # 骨架不包
    assert text.count("<memsvc-recall>") == 2 and text.count("</memsvc-recall>") == 2
    stripped = clean(text, "cc")
    assert "v1" not in stripped and "v2" not in stripped
    assert "q1" not in stripped and "q2" not in stripped  # 正文节全部剥净
    mem = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "<memsvc-recall>" not in mem  # 索引行不打标


def test_same_day_idempotent_index_multi_section(tmp_path):
    t0 = datetime(2026, 8, 27, 9, 0)
    t1 = datetime(2026, 8, 27, 15, 30)
    projection.project_recall(tmp_path, "q1", [_fact()], now=t0)
    r2 = projection.project_recall(tmp_path, "q2", [_fact(), _fact()], now=t1)
    assert r2["index_added"] is False  # 同日索引行已存在
    text = (tmp_path / "recall-20260827.md").read_text(encoding="utf-8")
    assert "## 09:00 — q1" in text and "## 15:30 — q2" in text
    assert text.index("q1") < text.index("q2")  # 追加序
    mem = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert mem.count("](recall-20260827.md)") == 1  # 幂等: 只一行


def test_new_day_new_file_second_index_line(tmp_path):
    projection.project_recall(tmp_path, "q1", [_fact()], now=datetime(2026, 8, 27, 10, 0))
    r = projection.project_recall(tmp_path, "q2", [_fact()], now=datetime(2026, 8, 28, 10, 0))
    assert r["recall_file"] == "recall-20260828.md"
    assert (tmp_path / "recall-20260827.md").exists()
    assert (tmp_path / "recall-20260828.md").exists()
    mem = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "](recall-20260827.md)" in mem and "](recall-20260828.md)" in mem


def test_adr16f_self_reference_guard(tmp_path):
    """recall-*.md 必须被 ADR-16f 判为投影 → init/re-ingest 扫描跳过 (防自指循环)。"""
    projection.project_recall(tmp_path, "q", [_fact()], now=datetime(2026, 8, 27))
    p = tmp_path / "recall-20260827.md"
    assert bootstrap._is_mem_service_projection(p.read_text(encoding="utf-8"),
                                                p.name) is True
    assert projection.RECALL_FILE_RE.match(p.name)


def test_value_truncation_and_annotations(tmp_path):
    long_val = "长" * 300
    f = _fact(topic="T", value=long_val, score=0.42,
              mem_path="mem-ab12-x.md")
    projection.project_recall(tmp_path, "q", [f], now=datetime(2026, 8, 27))
    text = (tmp_path / "recall-20260827.md").read_text(encoding="utf-8")
    line = [ln for ln in text.splitlines() if ln.startswith("1. ")][0]
    assert "长" * 200 in line and "长" * 201 not in line  # 截断 200 + …
    assert "[score 0.42]" in line and "mem-ab12-x.md" in line


def test_cli_recall_project_e2e(tmp_path, monkeypatch):
    """cli.recall(project=True): tmp db + HOME → recall-*.md + MEMORY.md 双产物。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    db.init(tmp_path / "db.sqlite")
    proj_cwd = str(tmp_path / "proj")  # cc_memory_dir(proj_cwd) 在假 HOME 下
    cli.ingest("张三在甲项目负责后端", source_cwd=proj_cwd)
    out = cli.recall("张三 后端", cwd=proj_cwd, project=True)
    facts = out["results"] if isinstance(out, dict) and "results" in out else out
    assert facts, "regex 通道应至少命中一条"
    mem_dir = projection.cc_memory_dir(proj_cwd)
    import os as _os
    assert _os.environ.get("HOME") == str(tmp_path / "home")
    files = {p.name for p in mem_dir.glob("*.md")}
    assert any(projection.RECALL_FILE_RE.match(n) for n in files), files
    assert "MEMORY.md" in files
    mem = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "## KG recall logs" in mem


def test_recall_index_line_survives_synthesis(tmp_path):
    """synthesis_index 重写只删 ](mem- 行 — recall 索引行幸存 (两族正交)。"""
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n## CC / harness 机制研究\n"
        "- [原生条目](native.md) — 原生描述\n"
        "- [recall 2026-08-27](recall-20260827.md) — 当日 KG 召回记录 (1 命中)\n",
        encoding="utf-8")
    # 冷启动 synthesis (无 mem-*.md) → 清投影行保原生
    db.init(tmp_path / "db2.sqlite")
    projection.synthesis_index("/tmp/x", mem_dir)
    text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "](recall-20260827.md)" in text, "recall 索引行不应被 synthesis 删除"
    assert "[原生条目](native.md)" in text

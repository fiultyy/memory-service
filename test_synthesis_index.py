"""P2 synthesis_index 独立测试 (ADR-15)。db.init(tmp) 隔离, 不碰真 DB。

覆盖:
1. 造 KG fact + 手写 mem-*.md(frontmatter fact_id)→ synthesis → MEMORY.md 出现对应 [mem] 行。
2. 冷启动: 空 memory dir → [mem] 段空(无 top-K), cold_start:true。
3. orphan: mem-*.md 的 fact_id 不在 KG → [mem] 行永远删; 文件默认留;
   MEM_SYNTH_PRUNE_ORPHANS=1 → 文件删; MEM_SYNTH_ORPHAN_BACKUP=1 → .orphan.bak。
4. 非 [mem] 行保留: MEMORY.md 有 CC 原生行 → synthesis 后仍在。
5. 批量: 3 个 mem-*.md → 一次投影全到。
6. 重跑幂等: 同输入两次 → MEMORY [mem] 一致(清空重写)。
7. 损坏容错: 一个 mem-*.md frontmatter 无 fact_id → 跳过不崩。
"""
import os
import shutil
import tempfile
from pathlib import Path

import db
import projection
import store


def _mk_fact(subj="用户", pred="uses", value="rust", LIF=0.7, conf=0.8):
    eid = store.put_entity(subj, "inferred")
    fid = store.put_fact(eid, pred, value, extractor="llm", fact_type="permanent",
                         source_cwd="/test", LIF=LIF, confidence=conf)
    return eid, fid


def _write_mem_md(mem_dir: Path, fid: str, body: str | None = None) -> Path:
    """手写 mem-<id>.md(模拟 recall/autodream 散 index 载体)。"""
    p = mem_dir / f"mem-{fid}.md"
    if body is None:
        body = (f"---\nfact_id: {fid}\nrecalled_at: 2026-08-08\n"
                f"source: mem-service\n---\n# body\n")
    p.write_text(body, encoding="utf-8")
    return p


def _mem_line_count(text: str) -> int:
    return sum(1 for ln in text.splitlines() if "(memory/mem-" in ln)


# ── T1: fact + mem-*.md → synthesis → MEMORY 出现对应 [mem] 行 ──────────
def test_basic_projection():
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        memory_md = mem_dir / "MEMORY.md"
        memory_md.write_text("# Index\n", encoding="utf-8")

        eid, fid = _mk_fact()
        _write_mem_md(mem_dir, fid)

        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        assert r["projected"] == 1, r
        assert r["orphans"] == 0 and r["pruned"] == 0, r
        assert r["cold_start"] is False, r

        text = memory_md.read_text(encoding="utf-8")
        assert f"(memory/mem-{fid}.md)" in text, f"应出现 {fid} 索引行:\n{text}"
        assert f"kg://fact/{fid}" in text, "应含 kg:// 指向"
        assert _mem_line_count(text) == 1, f"恰好一行 [mem]:\n{text}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T1 basic projection: fact+mem-*.md → MEMORY [mem] 行")


# ── T2: 冷启动 空 dir → cold_start, [mem] 空 ──────────────────────────
def test_cold_start():
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        memory_md = mem_dir / "MEMORY.md"
        memory_md.write_text("# Index\n", encoding="utf-8")

        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        assert r["cold_start"] is True, r
        assert r["projected"] == 0 and r["orphans"] == 0, r
        assert _mem_line_count(memory_md.read_text(encoding="utf-8")) == 0, "[mem] 应空"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T2 cold start: 空 dir → cold_start, [mem] 空(无 top-K 兜底)")


# ── T3: orphan → [mem] 行删永远; 文件默认留 / PRUNE 删 / BACKUP .orphan.bak ──
def test_orphan_variants():
    base_tmp = tempfile.mkdtemp()
    try:
        db.init(Path(base_tmp) / "mem.db")
        eid, live = _mk_fact(value="live")
        orphan_fid = store.put_entity("ghost", "inferred")
        # orphan_fid 是实体 id 不是 fact id; 造一个不在 KG 的伪 fact_id
        phantom = "deadbeefdeadbeefdeadbeefdeadbeef"

        def _setup(suffix_env: dict | None):
            t = tempfile.mkdtemp()
            md = Path(t) / "memory"
            md.mkdir()
            (md / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
            _write_mem_md(md, live)
            _write_mem_md(md, phantom)
            return t, md

        # 3a. 默认: orphan 行删, 文件留
        env_backup = {k: os.environ.get(k) for k in
                      ("MEM_SYNTH_PRUNE_ORPHANS", "MEM_SYNTH_ORPHAN_BACKUP")}
        os.environ.pop("MEM_SYNTH_PRUNE_ORPHANS", None)
        os.environ.pop("MEM_SYNTH_ORPHAN_BACKUP", None)
        try:
            t, md = _setup(None)
            r = projection.synthesis_index(cwd="/test", mem_dir=md)
            assert r["orphans"] == 1 and r["pruned"] == 0, r
            txt = (md / "MEMORY.md").read_text(encoding="utf-8")
            assert f"mem-{phantom}.md" not in txt, "orphan 行应删"
            assert f"mem-{live}.md" in txt, "live 行应留"
            assert (md / f"mem-{phantom}.md").exists(), "默认文件留"
            shutil.rmtree(t, ignore_errors=True)
            print("✓ T3a orphan default: 行删永远, 文件留")

            # 3b. PRUNE=1: 文件删
            os.environ["MEM_SYNTH_PRUNE_ORPHANS"] = "1"
            t, md = _setup(None)
            r = projection.synthesis_index(cwd="/test", mem_dir=md)
            assert r["pruned"] == 1, r
            assert not (md / f"mem-{phantom}.md").exists(), "PRUNE 文件应删"
            assert (md / f"mem-{phantom}.md.orphan.bak").exists() is False, "无 BACKUP 不留 bak"
            shutil.rmtree(t, ignore_errors=True)
            print("✓ T3b orphan PRUNE=1: 文件删")

            # 3c. PRUNE=1 + BACKUP=1: .orphan.bak
            os.environ["MEM_SYNTH_ORPHAN_BACKUP"] = "1"
            t, md = _setup(None)
            r = projection.synthesis_index(cwd="/test", mem_dir=md)
            assert r["pruned"] == 1, r
            assert not (md / f"mem-{phantom}.md").exists(), "原文件应不在"
            assert (md / f"mem-{phantom}.md.orphan.bak").exists(), "应有 .orphan.bak"
            shutil.rmtree(t, ignore_errors=True)
            print("✓ T3c orphan PRUNE+BACKUP: .orphan.bak")
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        shutil.rmtree(base_tmp, ignore_errors=True)


# ── T4: 非 [mem] 行(CC 原生)保留 ──────────────────────────────────────
def test_native_lines_preserved():
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        memory_md = mem_dir / "MEMORY.md"
        native = "- [overview](overview.md)\n# Memory Index\n"
        memory_md.write_text(native, encoding="utf-8")

        eid, fid = _mk_fact()
        _write_mem_md(mem_dir, fid)

        projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        txt = memory_md.read_text(encoding="utf-8")
        assert "[overview](overview.md)" in txt, f"CC 原生行应保留:\n{txt}"
        assert f"mem-{fid}.md" in txt
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T4 native lines preserved: CC 原生行不被清")


# ── T5: 批量 3 个 → 一次全投影 ────────────────────────────────────────
def test_batch():
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")

        fids = []
        for i, v in enumerate(("rust", "go", "py")):
            _, fid = _mk_fact(value=v, LIF=0.5 + 0.1 * i)
            _write_mem_md(mem_dir, fid)
            fids.append(fid)

        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        assert r["projected"] == 3, r
        txt = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert all(f"mem-{f}.md" in txt for f in fids), f"三个都投影:\n{txt}"
        assert _mem_line_count(txt) == 3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T5 batch: 3 mem-*.md → 一次全投影")


# ── T6: 重跑幂等(清空重写, 不重复追加) ────────────────────────────────
def test_idempotent():
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")

        eid, fid = _mk_fact()
        _write_mem_md(mem_dir, fid)

        r1 = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        txt1 = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        r2 = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        txt2 = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")

        assert r1 == r2, f"两次返回应一致: {r1} vs {r2}"
        assert txt1 == txt2, "MEMORY.md 重跑应一致(清空重写)"
        assert _mem_line_count(txt2) == 1, f"幂等: 不重复追加:\n{txt2}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T6 idempotent: 重跑 MEMORY 一致(清空重写, 不追加)")


# ── T7: 损坏容错(frontmatter 无 fact_id → 跳过不崩) ───────────────────
def test_corrupt_tolerance():
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")

        eid, fid = _mk_fact()
        _write_mem_md(mem_dir, fid)
        # 损坏文件: 无 fact_id
        (mem_dir / "mem-corruptnomatch.md").write_text(
            "---\nsource: mem-service\n---\n# no fact_id here\n", encoding="utf-8")

        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        assert r["projected"] == 1, f"损坏应跳过, 只投影 1: {r}"
        assert (mem_dir / "mem-corruptnomatch.md").exists(), "损坏文件本身不动"
        txt = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert f"mem-{fid}.md" in txt
        assert "corruptnomatch" not in txt
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T7 corrupt tolerance: 无 fact_id → 跳过不崩")


# ── T8: value 含换行 → sanitize 成空格, 不破坏幂等(回归 must-fix #1) ────
def test_newline_value_idempotent():
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
        eid, fid = _mk_fact(value="line1\nline2")  # value 含换行
        _write_mem_md(mem_dir, fid)
        projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        projection.synthesis_index(cwd="/test", mem_dir=mem_dir)  # 跑两次验幂等
        txt = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "line1 line2" in txt, f"换行应 sanitize 成空格(单行):\n{txt}"
        assert _mem_line_count(txt) == 1, f"恰好 1 行 [mem](不增殖):\n{txt}"
        # [mem] 行是单物理行(无内嵌换行)
        mem_lines = [ln for ln in txt.splitlines() if "(memory/mem-" in ln]
        assert len(mem_lines) == 1 and "\n" not in mem_lines[0]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T8 newline value: sanitize 成空格, 幂等不被破坏")


# ── T9: 冷启动 + mem_dir 不存在 → 自建, 不崩 FileNotFoundError(回归 must-fix #2)
def test_cold_start_nonexistent_dir():
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        nodir = Path(tmp) / "never-existed"  # 故意不 mkdir
        r = projection.synthesis_index(cwd="/test", mem_dir=nodir)
        assert r["cold_start"] is True, r
        assert (nodir / "MEMORY.md").exists(), "synthesis 应自建 dir + 写空 MEMORY.md"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T9 cold start nonexistent dir: 自建 dir, 不崩")


# ── T10: read_fact_id 只读 frontmatter(防 body 幽灵 id, 回归 must-fix #3) ──
def test_read_fact_id_frontmatter_only():
    tmp = tempfile.mkdtemp()
    try:
        p = Path(tmp) / "mem-realid123.md"
        p.write_text("---\nfact_id: realid123\n---\n# body\nfact_id: ghostid\n",
                     encoding="utf-8")
        assert projection.read_fact_id(p) == "realid123", "只读 frontmatter 的 fact_id"
        p2 = Path(tmp) / "no-fm.md"
        p2.write_text("fact_id: nofrontmatter\n# body\n", encoding="utf-8")
        assert projection.read_fact_id(p2) is None, "无 frontmatter → None"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T10 read_fact_id: 只读 frontmatter(防 body 幽灵 id)")


if __name__ == "__main__":
    test_basic_projection()
    test_cold_start()
    test_orphan_variants()
    test_native_lines_preserved()
    test_batch()
    test_idempotent()
    test_corrupt_tolerance()
    test_newline_value_idempotent()
    test_cold_start_nonexistent_dir()
    test_read_fact_id_frontmatter_only()
    print("\n✓ All synthesis_index tests passed")

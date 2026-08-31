"""P2 synthesis_index 独立测试 (ADR-15; ADR-A/B/C 原生格式)。db.init(tmp) 隔离, 不碰真 DB。

覆盖:
1. 造 KG fact + 手写 mem-{4hex}-{slug}.md(frontmatter fact_id)→ synthesis → MEMORY.md 出现原生投影行。
2. 冷启动: 空 memory dir → 投影段空(无 top-K), cold_start:true。
3. orphan: 投影 .md 的 fact_id 不在 KG → 投影行永远删; 文件默认留;
   MEM_SYNTH_PRUNE_ORPHANS=1 → 文件删; MEM_SYNTH_ORPHAN_BACKUP=1 → .orphan.bak。
4. 非投影行保留: MEMORY.md 有 CC 原生行 → synthesis 后仍在。
5. 批量: 3 个投影 .md → 一次投影全到。
6. 重跑幂等: 同输入两次 → MEMORY 投影段一致(清空重写)。
7. 损坏容错: 一个投影 .md frontmatter 无 fact_id → 跳过不崩。
8. ADR-A 原生行格式: ``- [topic](mem-{4hex}-{slug}.md) — topic``, 无 [mem]/score/kg://。
9. ADR-B 文件名严格契约: 非 mem-{4hex}-*.md 不被当投影(不投影不扫)。
10. ADR-A description 干净明文(= topic, 无 "(mem-service KG fact, LIF X.XX)" 噪声)。
"""
import os
import shutil
import tempfile
from pathlib import Path

import db
import projection
import store


def _mk_fact(subj="用户", pred="uses", value="rust", LIF=0.7, conf=0.8,
             topic="用户使用 rust"):
    eid = store.put_entity(subj, "inferred")
    fid = store.put_fact(eid, pred, value, extractor="llm", fact_type="permanent",
                         source_cwd="/test", LIF=LIF, confidence=conf, topic=topic)
    return eid, fid


def _write_mem_md(mem_dir: Path, fid: str, topic: str = "用户使用 rust",
                  body: str | None = None) -> Path:
    """手写 mem-{4hex}-{slug}.md(模拟 recall/autodream 散 index 载体, ADR-B 文件名契约)。"""
    fname = projection._mem_filename(fid, topic)
    p = mem_dir / fname
    if body is None:
        body = (f"---\nfact_id: {fid}\nrecalled_at: 2026-08-08\n"
                f"source: mem-service\n---\n# body\n")
    p.write_text(body, encoding="utf-8")
    return p


def _mem_line_count(text: str) -> int:
    """投影索引行计数(新 ``](mem-`` 格式)。"""
    return sum(1 for ln in text.splitlines() if "](mem-" in ln)


def _mem_filename_for(fid: str, topic: str = "用户使用 rust") -> str:
    return projection._mem_filename(fid, topic)


# ── T1: fact + 投影 .md → synthesis → MEMORY 出现原生投影行 ──────────────
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
        fname = _mem_filename_for(fid)
        assert f"]({fname})" in text, f"应出现 {fname} 投影行:\n{text}"
        assert _mem_line_count(text) == 1, f"恰好一行投影:\n{text}"
        # ADR-A 原生格式: 无 [mem] 标记, 无 score, 无 kg://
        assert "[mem]" not in text, f"ADR-A: 不应有 [mem] 标记:\n{text}"
        assert "score" not in text.lower(), f"ADR-A: 不应有 score:\n{text}"
        assert "kg://fact" not in text, f"ADR-A: MEMORY 索引行无 kg://:\n{text}"
        # ADR-A: 行格式 = - [topic](file) — topic
        assert f"- [用户使用 rust]({fname}) — 用户使用 rust" in text, f"原生行格式:\n{text}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T1 basic projection: fact+投影.md → MEMORY 原生投影行(ADR-A)")


# ── T2: 冷启动 空 dir → cold_start, 投影段空 ──────────────────────────
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
        assert _mem_line_count(memory_md.read_text(encoding="utf-8")) == 0, "投影行应空"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T2 cold start: 空 dir → cold_start, 投影段空(无 top-K 兜底)")


# ── T3: orphan → 投影行删永远; 文件默认留 / PRUNE 删 / BACKUP .orphan.bak ──
def test_orphan_variants():
    base_tmp = tempfile.mkdtemp()
    try:
        db.init(Path(base_tmp) / "mem.db")
        eid, live = _mk_fact(value="live", topic="live fact")
        orphan_fid = store.put_entity("ghost", "inferred")
        # orphan_fid 是实体 id 不是 fact id; 造一个不在 KG 的伪 fact_id
        phantom = "deadbeefdeadbeefdeadbeefdeadbeef"
        phantom_fname = _mem_filename_for(phantom, "orphan fact")
        live_fname = _mem_filename_for(live, "live fact")

        def _setup(suffix_env: dict | None):
            t = tempfile.mkdtemp()
            md = Path(t) / "memory"
            md.mkdir()
            (md / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
            _write_mem_md(md, live, topic="live fact")
            _write_mem_md(md, phantom, topic="orphan fact")
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
            assert phantom_fname not in txt, "orphan 行应删"
            assert live_fname in txt, "live 行应留"
            assert (md / phantom_fname).exists(), "默认文件留"
            shutil.rmtree(t, ignore_errors=True)
            print("✓ T3a orphan default: 行删永远, 文件留")

            # 3b. PRUNE=1: 文件删
            os.environ["MEM_SYNTH_PRUNE_ORPHANS"] = "1"
            t, md = _setup(None)
            r = projection.synthesis_index(cwd="/test", mem_dir=md)
            assert r["pruned"] == 1, r
            assert not (md / phantom_fname).exists(), "PRUNE 文件应删"
            assert not (md / f"{phantom_fname}.orphan.bak").exists(), "无 BACKUP 不留 bak"
            shutil.rmtree(t, ignore_errors=True)
            print("✓ T3b orphan PRUNE=1: 文件删")

            # 3c. PRUNE=1 + BACKUP=1: .orphan.bak
            os.environ["MEM_SYNTH_ORPHAN_BACKUP"] = "1"
            t, md = _setup(None)
            r = projection.synthesis_index(cwd="/test", mem_dir=md)
            assert r["pruned"] == 1, r
            assert not (md / phantom_fname).exists(), "原文件应不在"
            assert (md / f"{phantom_fname}.orphan.bak").exists(), "应有 .orphan.bak"
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


# ── T4: 非投影行(CC 原生)保留 ──────────────────────────────────────────
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
        assert _mem_filename_for(fid) in txt
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

        topics = ("uses rust", "uses go", "uses py")
        fids = []
        for i, (v, t) in enumerate(zip(("rust", "go", "py"), topics)):
            _, fid = _mk_fact(value=v, LIF=0.5 + 0.1 * i, topic=t)
            _write_mem_md(mem_dir, fid, topic=t)
            fids.append(fid)

        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        assert r["projected"] == 3, r
        txt = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert all(_mem_filename_for(f, t) in txt for f, t in zip(fids, topics)), f"三个都投影:\n{txt}"
        assert _mem_line_count(txt) == 3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T5 batch: 3 投影.md → 一次全投影")


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


# ── T7: 损坏容错(投影 .md frontmatter 无 fact_id → 跳过不崩) ───────────
def test_corrupt_tolerance():
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")

        eid, fid = _mk_fact()
        _write_mem_md(mem_dir, fid)
        # 损坏文件: 符合 MEM_FILE_RE 但 frontmatter 无 fact_id
        (mem_dir / "mem-dead0000-nofactid.md").write_text(
            "---\nsource: mem-service\n---\n# no fact_id here\n", encoding="utf-8")

        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        assert r["projected"] == 1, f"损坏应跳过, 只投影 1: {r}"
        assert (mem_dir / "mem-dead0000-nofactid.md").exists(), "损坏文件本身不动"
        txt = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert _mem_filename_for(fid) in txt
        assert "nofactid" not in txt
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T7 corrupt tolerance: 无 fact_id → 跳过不崩")


# ── T8: topic 含换行 → sanitize 成空格, 不破坏幂等(回归 must-fix #1) ────
def test_newline_value_idempotent():
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
        # T2: topic 含真换行(非已 sanitize 的 "line1 line2") → 文件名/description 无换行
        topic_nl = "line1\nline2"
        eid, fid = _mk_fact(value="line1\nline2", topic=topic_nl)
        _write_mem_md(mem_dir, fid, topic=topic_nl)
        # 文件名侧: _sanitize_slug 替换 \n(\s) → 文件名无换行字符
        fname_nl = projection._mem_filename(fid, topic_nl)
        assert "\n" not in fname_nl, f"文件名无换行: {fname_nl!r}"
        # description 侧: project_fact_md 经 _fact_topic strip 换行 → description 单行
        p_nl = projection.project_fact_md(
            {"id": fid, "predicate": "uses", "value": "line1\nline2",
             "source_refs": [], "topic": topic_nl},
            "用户", mem_dir, recalled_at="2026-08-08")
        desc_nl = [ln for ln in p_nl.read_text(encoding="utf-8").splitlines()
                   if ln.startswith("description:")]
        assert desc_nl and "\n" not in desc_nl[0], f"description 单行: {desc_nl}"
        # synthesis 跑两次验幂等(投影行单物理行, 不增殖)
        projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        txt = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "line1" in txt and "line2" in txt, f"topic 内容应在:\n{txt}"
        assert _mem_line_count(txt) == 1, f"恰好 1 行投影(不增殖):\n{txt}"
        mem_lines = [ln for ln in txt.splitlines() if "](mem-" in ln]
        assert len(mem_lines) == 1 and "\n" not in mem_lines[0]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T8 topic: 单行, 幂等不被破坏")


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
        # 文件名符合 MEM_FILE_RE 才是投影文件(read_fact_id 本身只看 frontmatter)
        p = Path(tmp) / "mem-abcd1234-realid.md"
        p.write_text("---\nfact_id: realid123\n---\n# body\nfact_id: ghostid\n",
                     encoding="utf-8")
        assert projection.read_fact_id(p) == "realid123", "只读 frontmatter 的 fact_id"
        p2 = Path(tmp) / "no-fm.md"
        p2.write_text("fact_id: nofrontmatter\n# body\n", encoding="utf-8")
        assert projection.read_fact_id(p2) is None, "无 frontmatter → None"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T10 read_fact_id: 只读 frontmatter(防 body 幽灵 id)")


# ── T11: ADR-B 文件名严格契约 — _mem_filename 断言 + native 不被误判 ────
def test_filename_contract():
    # 合法: mem- + 恰好 4hex + - + slug + .md
    assert projection.MEM_FILE_RE.match("mem-abcd-some_topic.md")
    # 非法: 32-hex 无 -slug(旧格式) → 不匹配(8+hex 无 - 分隔)
    assert not projection.MEM_FILE_RE.match("mem-abcd1234abcd1234abcd1234abcd1234.md")
    # 非法: mem-service-* (serv 非 4-hex — 's' 不在 [0-9a-f])
    assert not projection.MEM_FILE_RE.match("mem-service-overview.md")
    # 非法: 缺 4hex
    assert not projection.MEM_FILE_RE.match("mem-slug.md")
    # 非法: 4hex 后无 - (如 mem-abcdslug.md)
    assert not projection.MEM_FILE_RE.match("mem-abcdslug.md")
    # _mem_filename 断言: fact_id 太短(无 4hex) → raise(不静默写坏契约)
    try:
        projection._mem_filename("ab", "topic")
    except ValueError:
        pass
    else:
        raise AssertionError("ADR-B: fact_id < 4 hex 应 raise")
    # _mem_filename 正常产出符合 RE(fact_id[:4] = "abcd", slug from topic)
    name = projection._mem_filename("abcdef1234567890", "用户使用 rust")
    assert projection.MEM_FILE_RE.match(name), name
    assert name == "mem-abcd-用户使用_rust.md", name
    print("✓ T11 filename contract: MEM_FILE_RE 严格 + native(mem-service-*) 不匹配 + 创建侧断言")


# ── T12: ADR-A description 干净明文(= topic, 无机器噪声) ────────────────
def test_clean_description():
    import tempfile as _t
    tmp = _t.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        eid, fid = _mk_fact(topic="CC 用智谱直连")
        p = projection.project_fact_md(
            {"id": fid, "predicate": "uses", "value": "智谱", "source_refs": [], "topic": "CC 用智谱直连"},
            "CC", mem_dir, recalled_at="2026-08-08T00:00:00Z")
        content = p.read_text(encoding="utf-8")
        # description = 干净 topic, 无 LIF 噪声
        assert "description: CC 用智谱直连" in content, f"description 应干净:\n{content}"
        assert "mem-service KG fact" not in content, f"无机器噪声:\n{content}"
        assert "LIF " not in content, f"无 LIF 噪声:\n{content}"
        # 正文自包含 + 保留 kg:// 溯源链接
        assert "kg://fact/" in content, "保留可选溯源"
        assert "- subject: CC" in content and "- predicate: uses" in content
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T12 clean description: = topic, 无机器噪声(ADR-A)")


# ── T4(F6): 长 topic → 文件名字节 < 255 且匹配 MEM_FILE_RE ──────────────
def test_long_topic_filename():
    long_topic = "极" * 200  # 200 CJK 字符(3 字节/字, 压测 NAME_MAX 字节上限)
    name = projection._mem_filename("abcdef1234567890", long_topic)
    nbytes = len(name.encode("utf-8"))
    assert nbytes < 255, f"文件名字节 < 255: got {nbytes} ({name!r})"
    assert projection.MEM_FILE_RE.match(name), f"匹配 MEM_FILE_RE: {name!r}"
    print(f"✓ T4 long topic: 200 字符 → 文件名 {nbytes} 字节 < 255, 匹配 RE")


# ── T5(F4): topic 含冒号 → frontmatter description 经 yaml.safe_load 解析 ─
def test_yaml_colon_description():
    import yaml
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        eid, fid = _mk_fact(topic="a: b")
        p = projection.project_fact_md(
            {"id": fid, "predicate": "uses", "value": "b",
             "source_refs": [], "topic": "a: b"},
            "用户", mem_dir, recalled_at="2026-08-08")
        content = p.read_text(encoding="utf-8")
        assert content.startswith("---\n"), content
        _h, fm, _body = content.split("---\n", 2)
        data = yaml.safe_load(fm)
        assert data["description"] == "a: b", f"description == 'a: b': {data}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T5 YAML colon: topic 'a: b' → yaml.safe_load(description) == 'a: b'")


# ── T6(F7): topic 含 ] → MEMORY 索引行是合法 markdown 链接(] 已转义) ────
def test_md_link_escape_bracket():
    from markdown_it import MarkdownIt
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
        eid, fid = _mk_fact(topic="a]b")
        _write_mem_md(mem_dir, fid, topic="a]b")
        projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        txt = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        mem_lines = [ln for ln in txt.splitlines() if "](mem-" in ln]
        assert len(mem_lines) == 1, f"恰好 1 投影行:\n{txt}"
        line = mem_lines[0]
        # markdown-it-py 解析(parseInline): link_open href 合法 mem-*.md
        inline = MarkdownIt().parseInline(line)[0]
        link_open = next((t for t in inline.children if t.type == "link_open"), None)
        assert link_open is not None, f"应有 link_open token: {line!r}"
        href = link_open.attrs.get("href", "")
        assert href.startswith("mem-") and href.endswith(".md"), (
            f"href 合法 mem-*.md, got {href!r}: {line!r}")
        # ] 已转义: link text 内 ] 被转成 \], 不 prematurely 闭合
        assert "\\]" in line, f"] 已转义(\\]): {line!r}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T6 md escape: topic 'a]b' → 索引行合法 markdown 链接(] 已转义)")


# ── T13: 主题聚合 — 同 topic 只留 mem_score 最高代表 (09-01 终裁A方案) ────
def test_topic_aggregation_keeps_best_representative(monkeypatch):
    monkeypatch.delenv("MEM_SYNTH_MIN_SCORE", raising=False)
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")

        topic = "用户使用 rust"
        _, fid_hi = _mk_fact(value="rust new", LIF=0.9, conf=0.9, topic=topic)
        _, fid_lo = _mk_fact(value="rust old", LIF=0.2, conf=0.2, topic=topic)
        _, fid_go = _mk_fact(value="go", LIF=0.5, conf=0.5, topic="用户使用 go")
        _write_mem_md(mem_dir, fid_hi, topic=topic)
        _write_mem_md(mem_dir, fid_lo, topic=topic)
        _write_mem_md(mem_dir, fid_go, topic="用户使用 go")

        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        # 3 fact 同仓 → 同 topic 精确等值聚类只剩最高 mem_score 代表
        assert r["projected"] == 2, f"3 fact → 2 代表: {r}"
        assert r["deduped"] == 1, f"同 topic 聚合丢弃 1: {r}"
        txt = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert _mem_filename_for(fid_hi, topic) in txt, f"高分代表在场:\n{txt}"
        assert _mem_filename_for(fid_lo, topic) not in txt, f"低分被聚合:\n{txt}"
        assert _mem_filename_for(fid_go, "用户使用 go") in txt
        assert _mem_line_count(txt) == 2

        # 幂等重扫: 低分代表的 mem-*.md 文件留存 (orphan 判定按 KG presence,
        # 不产生假 orphan), 重聚仍只出同一代表集
        assert (mem_dir / _mem_filename_for(fid_lo, topic)).exists(), \
            "非代表文件留存 (prune 语义不变)"
        r2 = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        assert r2["projected"] == 2 and r2["deduped"] == 1, r2
        txt2 = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert txt2 == txt, "重扫投影幂等"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T13 主题聚合: 同 topic 只留最高 mem_score 代表, deduped=1, 幂等")


# ── T14: 空/None topic 各成一组, 不互聚 (精确等值聚类的空值边界) ──────────
def test_topic_aggregation_empty_topics_stay_solo(monkeypatch):
    monkeypatch.delenv("MEM_SYNTH_MIN_SCORE", raising=False)
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")

        _, fid_a = _mk_fact(value="alpha", LIF=0.8, conf=0.8, topic=None)
        _, fid_b = _mk_fact(value="beta", LIF=0.8, conf=0.8, topic="")
        _write_mem_md(mem_dir, fid_a, topic="")
        _write_mem_md(mem_dir, fid_b, topic="")

        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        assert r["projected"] == 2, f"空 topic 各成一组, 不互聚: {r}"
        assert r["deduped"] == 0, f"无聚合丢弃: {r}"
        txt = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert _mem_line_count(txt) == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T14 空 topic 独立: None/空串各成一组, deduped=0")


# ── T15: MEM_SYNTH_MIN_SCORE 地板 — 低于地板不投影, deduped 合计; 缺省 0 不过滤 ─
def test_synth_min_score_floor(monkeypatch):
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
        _, fid_hi = _mk_fact(value="hi", LIF=0.9, conf=0.9, topic="t hi")
        _, fid_lo = _mk_fact(value="lo", LIF=0.1, conf=0.1, topic="t lo")
        _write_mem_md(mem_dir, fid_hi, topic="t hi")
        _write_mem_md(mem_dir, fid_lo, topic="t lo")

        # 缺省 0 = 不过滤 (两个都投影)
        monkeypatch.delenv("MEM_SYNTH_MIN_SCORE", raising=False)
        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        assert r["projected"] == 2 and r["deduped"] == 0, f"缺省不过滤: {r}"

        # 地板 0.5: mem_score(lo)=0.1 被滤 (deduped 计入), hi (0.9) 留
        monkeypatch.setenv("MEM_SYNTH_MIN_SCORE", "0.5")
        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        assert r["projected"] == 1, f"地板滤后只投影 hi: {r}"
        assert r["deduped"] == 1, f"地板滤计入 deduped: {r}"
        txt = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert _mem_filename_for(fid_hi, "t hi") in txt
        assert _mem_filename_for(fid_lo, "t lo") not in txt
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T15 MIN_SCORE 地板: 缺省 0 不过滤; 地板上低分不投影且计入 deduped")


# ── T16: 地板+聚合叠加 — deduped = 地板滤 + 聚合丢弃合计 ─────────────────
def test_floor_and_aggregation_combine_in_deduped(monkeypatch):
    tmp = tempfile.mkdtemp()
    try:
        db.init(Path(tmp) / "mem.db")
        mem_dir = Path(tmp) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
        topic = "dup topic"
        _, fid_hi = _mk_fact(value="hi", LIF=0.9, conf=0.9, topic=topic)
        # lo 高于地板 0.5 (mem_score=0.6) 但低于代表 → 被聚合丢 (非地板丢)
        _, fid_lo = _mk_fact(value="lo", LIF=0.6, conf=0.6, topic=topic)
        _, fid_fl = _mk_fact(value="fl", LIF=0.1, conf=0.1, topic="solo low")
        for f, t in ((fid_hi, topic), (fid_lo, topic), (fid_fl, "solo low")):
            _write_mem_md(mem_dir, f, topic=t)
        monkeypatch.setenv("MEM_SYNTH_MIN_SCORE", "0.5")
        r = projection.synthesis_index(cwd="/test", mem_dir=mem_dir)
        # mem_score: hi≈0.9 / lo≈0.6(聚合丢) / fl≈0.1(地板丢) → 只投影 hi
        assert r["projected"] == 1, f"只留高分代表: {r}"
        assert r["deduped"] == 2, f"deduped = 地板 1 + 聚合 1: {r}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ T16 叠加: deduped = 地板滤 + 聚合丢弃合计")


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
    test_filename_contract()
    test_clean_description()
    test_long_topic_filename()
    test_yaml_colon_description()
    test_md_link_escape_bracket()
    test_topic_aggregation_keeps_best_representative(None)
    test_topic_aggregation_empty_topics_stay_solo(None)
    test_synth_min_score_floor(None)
    test_floor_and_aggregation_combine_in_deduped(None)
    print("\n✓ All synthesis_index tests passed")

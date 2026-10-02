"""H6b atom 面投影测试 (v2 图改造): project_atom_md 形态契约 + recall per-hit
投影 + synthesis atom 面对账 + soft-delete/legacy orphan + 回切档。

db.init(tmp) 隔离; 文本腿召回零网络 (use_vec/gate 关, conftest autouse pin
laya off); 不触生产 data/memory.db。legacy fact 面回归见 test_synthesis_index.py
(整文件 pin MEM_PROJECTION_LEGACY_FACT=1)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import projection
import recall as recall_mod
import store


def _atom(conn, text, *, p_dur=0.6, valid_to=None, source_refs=None,
          label="fact"):
    cur = conn.execute(
        "INSERT INTO atom(text, label, p_dur, valid_from, valid_to, source_refs) "
        "VALUES(?, ?, ?, '2026-01-01T00:00:00+00:00', ?, ?)",
        (text, label, p_dur, valid_to,
         __import__("json").dumps(source_refs) if source_refs else None))
    return cur.lastrowid


def _sem_tag(conn, name, atom_ids, w=1.0):
    """建 semantic tag 并挂载 (v3 聚类键测试速记)。"""
    cur = conn.execute(
        "INSERT INTO tag(name, kind, level) VALUES(?, 'semantic', 2)", (name,))
    tid = cur.lastrowid
    for aid in atom_ids:
        conn.execute(
            "INSERT INTO tag_mount(tag_id, atom_id, w) VALUES(?, ?, ?)",
            (tid, aid, w))
    return tid


def test_atom_filename_contract():
    """ADR-B: hex4 = 04x (rowid ≤ 0xffff), 超出取末 4 hex; slug 源 = text。"""
    assert projection._atom_filename(7, "sqlite 部署") == "mem-0007-sqlite_部署.md"
    big = projection._atom_filename(0x12345, "x")
    assert big.startswith("mem-2345-") and projection.MEM_FILE_RE.match(big)


def test_project_atom_md_contract(tmp_path):
    """frontmatter 键形同 fact 件 (fact_id: atom-N) — read_fact_id 单一扫描面。"""
    a = {"id": "atom:12", "atom_id": 12, "text": "memsvc 投影走 atom 面",
         "label": "fact", "p_dur": 0.8}
    p = projection.project_atom_md(a, tmp_path, recalled_at="2026-10-01T00:00:00+00:00")
    assert p.name == projection._atom_filename(12, "memsvc 投影走 atom 面")
    text = p.read_text(encoding="utf-8")
    assert "fact_id: atom-12" in text and "source: mem-service" in text
    assert "description: memsvc 投影走 atom 面" in text
    assert "kg://atom/12" in text
    assert projection.read_fact_id(p) == "atom-12"
    # 冒号 id 无 atom_id 键 → ATOM_FID_RE (hyphen 形) 也能解析
    p2 = projection.project_atom_md({"id": "atom-13", "text": "t13"}, tmp_path)
    assert projection.read_fact_id(p2) == "atom-13"
    with open(p2, "rb") as fh:  # noqa: SIM115 — 小文件直读断言幂等重写
        before = fh.read()
    projection.project_atom_md({"id": "atom-13", "text": "t13"}, tmp_path)
    assert p2.read_bytes() == before  # 幂等: 同 atom 重写逐字节一致


def test_recall_per_hit_projection_then_synthesis(tmp_path, monkeypatch):
    """完整环: recall(cwd) per-hit 建 mem-{hex4}-{slug}.md → synthesis 索引入 MEMORY。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    db.init(tmp_path / "db.sqlite")
    conn = db.get_conn()
    a1 = _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9",
               source_refs=["s1.jsonl", "s2.jsonl"])
    proj_cwd = str(tmp_path / "proj")
    res = recall_mod.recall("sqlite 部署", cwd=proj_cwd)
    assert res and res[0]["id"] == f"atom:{a1}"
    mem_dir = projection.cc_memory_dir(proj_cwd)
    fname = projection._atom_filename(a1, res[0]["text"])
    assert (mem_dir / fname).exists(), sorted(p.name for p in mem_dir.glob("*.md"))
    body = (mem_dir / fname).read_text(encoding="utf-8")
    assert "source_refs: ['s1.jsonl', 's2.jsonl']" in body  # ADR-A 正文自包含
    tag = res[0]["_snaptag"]
    assert tag["mem_path"] == fname and tag["kg_uri"] == f"kg://atom/{a1}"
    # synthesis atom 面: 散件 → MEMORY.md 原生索引行 (ADR-A)
    (mem_dir / "MEMORY.md").write_text("# Memory Index\n", encoding="utf-8")
    r = projection.synthesis_index(proj_cwd, mem_dir)
    assert r["projected"] == 1 and r["cold_start"] is False, r
    text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert f"]({fname})" in text and "](mem-" in text


def test_synthesis_soft_deleted_atom_orphan(tmp_path):
    """valid_to 置位 (ADR-17d 软删) → KG 不在场 → 索引行删, 文件默认留。"""
    db.init(tmp_path / "db.sqlite")
    aid = _atom(db.get_conn(), "旧结论", valid_to="2026-09-01T00:00:00+00:00")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    fname = projection._atom_filename(aid, "旧结论")
    (mem_dir / fname).write_text(f"---\nfact_id: atom-{aid}\n---\n# 旧结论\n",
                                 encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text(f"- [旧结论]({fname}) — 旧结论\n",
                                       encoding="utf-8")
    r = projection.synthesis_index("/t", mem_dir)
    assert r["projected"] == 0 and r["orphans"] == 1, r
    text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert fname not in text, "软删 atom 的投影行应被对账删除"
    assert (mem_dir / fname).exists()  # 默认 off: orphan 文件不删


def test_synthesis_legacy_fact_orphan_and_escape(tmp_path, monkeypatch):
    """默认 atom 面: legacy fact 件判 orphan; MEM_PROJECTION_LEGACY_FACT=1 回切恢复。"""
    db.init(tmp_path / "db.sqlite")
    eid = store.put_entity("用户", "inferred")
    fid = store.put_fact(eid, "uses", "rust", extractor="regex",
                         fact_type="permanent", topic="用户使用 rust")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    fpath = projection._mem_filename(fid, "用户使用 rust")
    (mem_dir / fpath).write_text(f"---\nfact_id: {fid}\n---\n# 用户使用 rust\n",
                                 encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text("", encoding="utf-8")
    r = projection.synthesis_index("/t", mem_dir)
    assert r["projected"] == 0 and r["orphans"] == 1, r
    assert fpath not in (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert (mem_dir / fpath).exists()  # 文件留存, 仅索引行退场
    monkeypatch.setenv("MEM_PROJECTION_LEGACY_FACT", "1")
    r2 = projection.synthesis_index("/t", mem_dir)
    assert r2["projected"] == 1 and r2["orphans"] == 0, r2
    assert fpath in (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    # 对称逃生口: 单设 MEM_RECALL_LEGACY_FACT=1 也回 fact 投影面 (单一旋钮
    # 连贯回滚 — 否则 recall 建 fact 件被 atom 面 synthesis 立即判 orphan)
    monkeypatch.setenv("MEM_PROJECTION_LEGACY_FACT", "0")
    monkeypatch.setenv("MEM_RECALL_LEGACY_FACT", "1")
    r3 = projection.synthesis_index("/t", mem_dir)
    assert r3["projected"] == 1 and r3["orphans"] == 0, r3


def test_synthesis_empty_text_atom_link_resolves(tmp_path):
    """空 text atom: 落盘件 slug 占位 "fact", 索引行链接目标必须同文件 (无死链)。"""
    db.init(tmp_path / "db.sqlite")
    aid = _atom(db.get_conn(), "")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    p = projection.project_atom_md({"id": f"atom-{aid}", "text": ""}, mem_dir)
    assert p.name == f"mem-{aid:04x}-fact.md"
    (mem_dir / "MEMORY.md").write_text("", encoding="utf-8")
    r = projection.synthesis_index("/t", mem_dir)
    assert r["projected"] == 1, r
    text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert f"]({p.name})" in text, text  # 链接目标 = 实际文件名


def test_preference_pinned_section_head(tmp_path, monkeypatch):
    """v3 常驻口径: preference 原子绕过 MIN_SCORE 地板与全局排序, 钉投影段首
    「## 常驻口径」小节; 行格式仍原生 (ADR-A, _is_mem_index_line 兼容)。"""
    db.init(tmp_path / "db.sqlite")
    conn = db.get_conn()
    a_pref = _atom(conn, "回复一律用中文", label="preference", p_dur=0.1)  # 低于地板
    a_fact = _atom(conn, "sqlite-vec 部署用 0.1.9", p_dur=0.9)
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    pf = projection.project_atom_md(
        {"id": f"atom-{a_pref}", "text": "回复一律用中文"}, mem_dir)
    ff = projection.project_atom_md(
        {"id": f"atom-{a_fact}", "text": "sqlite-vec 部署用 0.1.9"}, mem_dir)
    (mem_dir / "MEMORY.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("MEM_SYNTH_MIN_SCORE", "0.5")  # 0.1 地板下必被滤 — 偏好豁免
    r = projection.synthesis_index("/t", mem_dir)
    assert r["projected"] == 2 and r["deduped"] == 0, r
    text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    i_head = lines.index(projection._PIN_SECTION)
    i_pref = next(i for i, ln in enumerate(lines) if f"]({pf.name})" in ln)
    i_fact = next(i for i, ln in enumerate(lines) if f"]({ff.name})" in ln)
    assert i_head < i_pref < i_fact, lines  # 段首小节 → 偏好行 → 普通行
    assert lines[i_pref] == "- [回复一律用中文](" + pf.name + ") — 回复一律用中文"
    # 偏好退场后小节标题不残留 (重写清 _PIN_SECTION)
    conn.execute("UPDATE atom SET valid_to='2026-10-01T00:00:00+00:00' "
                 f"WHERE id={a_pref}")
    r2 = projection.synthesis_index("/t", mem_dir)
    assert r2["projected"] == 1 and r2["orphans"] == 1, r2
    text2 = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert projection._PIN_SECTION not in text2 and pf.name not in text2


def test_tag_clustering_dedup(tmp_path):
    """v3 聚类键升级: top-1 semantic tag (tag_mount JOIN tag) — 同 tag 不同
    text 只留 mem_score 最高代表; 无 mount 回退现行为 (各成 solo 不误吞)。"""
    db.init(tmp_path / "db.sqlite")
    conn = db.get_conn()
    a_hi = _atom(conn, "部署结论甲: 用 pip 装 sqlite-vec", p_dur=0.9)
    a_lo = _atom(conn, "部署结论乙: sqlite-vec 走 pip 渠道", p_dur=0.5)
    a_solo = _atom(conn, "无关结论: 回归测试走 pytest", p_dur=0.7)
    _sem_tag(conn, "sqlite-vec 部署", [a_hi, a_lo])  # 近义双原子同挂一 tag
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    names = {}
    for aid, t in ((a_hi, "部署结论甲: 用 pip 装 sqlite-vec"),
                   (a_lo, "部署结论乙: sqlite-vec 走 pip 渠道"),
                   (a_solo, "无关结论: 回归测试走 pytest")):
        names[aid] = projection.project_atom_md(
            {"id": f"atom-{aid}", "text": t}, mem_dir).name
    (mem_dir / "MEMORY.md").write_text("", encoding="utf-8")
    r = projection.synthesis_index("/t", mem_dir)
    # 同 tag 双子 → 只留高分 a_hi; a_solo 无 mount 回退 solo 不被吞
    assert r["projected"] == 2 and r["deduped"] == 1, r
    text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert names[a_hi] in text and names[a_lo] not in text, text
    assert names[a_solo] in text
    assert (mem_dir / names[a_lo]).exists()  # 非代表文件留存 (prune 语义不变)

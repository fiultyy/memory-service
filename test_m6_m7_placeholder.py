"""M6/M7 占位通道批验收测试 (spec v2 §2 M6+M7: D2'/P18/P16/P20/D6a, 反 ADR-5)。

覆盖派发令四条验收:
1. M7 单测: 词典命中链到既有 entity id (不新建重复实体); regex 实体模式三例;
   关系谓词三例; 代码块/明显代码行内词典命中被弃 (块分流 A)。
2. M6 主径断言: autodream 全管道零 LLM 调用 (adapter 不可达 monkeypatch +
   空 providers), fact 照常入库且 extractor='regex'、lif_source=0.4、
   provenance 按段正确。
3. 无重复实体: 同 transcript 两次 autodream → 幂等全 NOOP (四分支不回归)。
4. wings 资产不动 → git diff 核验 (REPORT), 这里锁行为面: providers=None/[]
   全程不触 adapter / llm_provider 网络。

测试规范: def test_xxx() 函数让 pytest 收集。禁网络/LLM: embedding.embed
monkeypatch 为 [] (resolver step2 跳过), adapter monkeypatch 为 raise。
"""
import json
import tempfile
from pathlib import Path

import adapter
import autodream
import db
import embedding
import gazetteer
import store
from llm_provider import EdgeOut, EntityOut, Extraction


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")


# ── 验收 1a: M7 词典命中链到既有 entity id ───────────────────────────

def test_gazetteer_dictionary_links_to_existing_entity():
    """seed KG (name+alias) 后, gazetteer 输出 canonical 名; autodream 全管道
    消费后链到既有 entity id, 不新建重复实体。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "gaz.db")
    seeded_id = store.put_entity("Rust 语言", "tool", aliases=["rust", "铁锈"])

    # gazetteer 单元面: 别名 surface 命中 → canonical EntityOut。
    r = gazetteer.extract("rust 是一种系统语言")
    names = {e.name for e in r.entities}
    assert "Rust 语言" in names, f"词典命中应产出 canonical 名, got {names}"
    assert not any(e.name == "rust" for e in r.entities), (
        "别名 surface 不应再作为独立实体声明 (canonical 已覆盖)")

    # 全管道: autodream 消费 (CJK is_a 模式给出边), subject 链到 seeded id。
    tpath = Path(tmp) / "t.jsonl"
    _write_transcript(tpath, [
        {"type": "user", "message": {"content": [{"type": "text",
         "text": "rust 是一种系统语言"}]}}])
    orig_embed = embedding.embed
    embedding.embed = lambda text, providers=None: []
    try:
        out = autodream.autodream("sess-gaz", str(tpath))
    finally:
        embedding.embed = orig_embed
    assert out["added"] == 1, out

    conn = db.get_conn()
    rust_rows = conn.execute(
        "SELECT id FROM entity WHERE name IN ('rust', 'Rust 语言')").fetchall()
    assert len(rust_rows) == 1 and rust_rows[0]["id"] == seeded_id, (
        f"词典命中必须链到既有 entity (无重复), got {len(rust_rows)} rows")
    subj = conn.execute(
        "SELECT subject_id FROM fact WHERE status='active'").fetchone()
    assert subj["subject_id"] == seeded_id, "fact subject 应挂 seeded entity id"


# ── 验收 1b: regex 实体模式三例 + 关系谓词三例 ────────────────────────

def test_gazetteer_regex_entity_patterns():
    """② extractor.py 休眠实体模式复活: 技术点分名(段首大写) / CJK 书名号 /
    snake_case。"""
    r = gazetteer.extract("we pinned Docker.Compose and 《知识图谱》 notes the "
                          "cargo_toml entry.")
    names = {e.name for e in r.entities}
    assert "Docker.Compose" in names, f"技术点分名未命中: {names}"
    assert "知识图谱" in names, f"CJK 书名号未命中: {names}"
    assert "cargo_toml" in names, f"snake_case 未命中: {names}"


def test_gazetteer_relation_predicates():
    """③ 关系模式 (7 EN 谓词 + CJK 同义集): is_a / uses / depends_on 三例。"""
    r = gazetteer.extract(
        "FastAPI uses Pydantic. Kafka depends on Zookeeper. Logseq 是笔记工具.")
    triples = {(e.subject, e.predicate, e.object) for e in r.edges}
    assert ("FastAPI", "uses", "Pydantic") in triples, triples
    assert ("Kafka", "depends_on", "Zookeeper") in triples, triples
    assert ("Logseq", "is_a", "笔记工具") in triples, triples
    assert r.source_meta["extractor_label"] == "regex", (
        f"占位通道必须打 regex 标 (M6 lif_source 0.4 档), got {r.source_meta}")


# ── 验收 1c: 块分流 A — 代码块/明显代码行内词典命中被弃 ───────────────

def test_code_block_dictionary_hits_discarded():
    """fenced code block 与明显代码行 (def/import/shell/赋值/路径) 是实体禁区:
    区内词典命中被弃, 区外照常命中。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "mask.db")
    store.put_entity("Rust 语言", "tool", aliases=["rust"])

    # 仅出现在 fenced block 内 → 被弃。
    r = gazetteer.extract("```\ncargo build uses rust here\n```")
    assert "Rust 语言" not in {e.name for e in r.entities}, (
        "fenced code block 内词典命中必须被弃 (块分流 A)")
    assert r.edges == [], "代码块内关系模式命中同样被弃"

    # 仅出现在明显代码行 (def 前缀) → 被弃。
    r2 = gazetteer.extract("def rust(): return compile")
    assert "Rust 语言" not in {e.name for e in r2.entities}, (
        "代码行 (def 前缀) 内词典命中必须被弃")

    # 同名命中在 fenced 内 + 区外 prose 各一次 → 区外那次保留。
    r3 = gazetteer.extract("```\nrust in fence\n```\nrust 是系统语言")
    assert "Rust 语言" in {e.name for e in r3.entities}, (
        "区外 (prose) 词典命中不应被误伤"
    )


def test_mask_code_zones_line_kinds():
    """mask 启发式: shell 命令行/导入行/路径/URL/赋值行被 blank, prose 行不动。"""
    f = gazetteer._mask_code_zones
    assert f("cargo build --release").strip() == "", "shell 命令行应被 mask"
    assert f("import rust_tooling").strip() == "", "import 行应被 mask"
    assert f("see docs/adapter.py for detail").strip() == "", "路径行应被 mask"
    assert f("open https://example.com/rust").strip() == "", "URL 行应被 mask"
    assert f("VERSION=2 rustup").strip() == "", "赋值行应被 mask"
    assert f("rust 是系统语言").strip() != "", "prose 行不应被 mask"
    assert f("FastAPI uses Pydantic.").strip() != "", "prose 行不应被 mask"


# ── 验收 2: M6 主径 — 零 LLM 全管道, extractor/veracity/provenance 落对 ──

def test_autodream_pipeline_zero_llm():
    """monkeypatch adapter 全不可达 (default_providers/extract_facts 皆 raise),
    providers=None 与 [] 两态: autodream 照常入库, 不 RuntimeError; fact 带
    extractor='regex' / lif_source=0.4 / provenance 按段 (M8 归因不变)。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "m6.db")
    tpath = Path(tmp) / "t.jsonl"
    _write_transcript(tpath, [
        {"type": "user", "message": {"content":
            [{"type": "text", "text": "FastAPI uses Pydantic."}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "Kafka depends on Zookeeper."}]}},
    ])

    def _boom(*a, **k):
        raise RuntimeError("adapter must NOT be called on the M6 main path")

    orig_dp, orig_ef = adapter.default_providers, adapter.extract_facts
    orig_embed = embedding.embed
    adapter.default_providers = _boom
    adapter.extract_facts = _boom
    embedding.embed = lambda text, providers=None: []
    try:
        out1 = autodream.autodream("sess-m6", str(tpath), providers=None)
        out2 = autodream.autodream("sess-m6", str(tpath), providers=[])
    finally:
        adapter.default_providers = orig_dp
        adapter.extract_facts = orig_ef
        embedding.embed = orig_embed
    # 首跑 (providers=None) 照常入库不 RuntimeError; 二跑 (providers=[]) 同
    # transcript 同 session → 全 NOOP (幂等兼作占位径复核)。两态零 LLM。
    assert out1["added"] == 2, f"providers=None 应入库 2 条, got {out1}"
    assert out2 == {"added": 0, "updated": 0, "deleted": 0, "noop": 2}, out2

    rows = db.get_conn().execute(
        "SELECT extractor, lif_source, provenance, veracity FROM fact "
        "WHERE status='active'").fetchall()
    assert rows, "占位径必须落 fact (provider 断供不再中断写入)"
    for row in rows:
        assert row["extractor"] == "regex", f"M6 落库 extractor 应 regex, got {row['extractor']}"
        assert row["lif_source"] == 0.4, f"SOURCE_WEIGHT regex 档应 0.4, got {row['lif_source']}"
    provs = {row["provenance"] for row in rows}
    assert "user_prose" in provs and "tool_obs" in provs, (
        f"M8 段归因不变: user 段与 tool_result 段都应在, got {provs}")
    ver = {row["veracity"] for row in rows}
    assert ver == {1.0, 0.9}, f"veracity 随 M3 映射 (user 1.0/tool_obs 0.9), got {ver}"


# ── 验收 3: 无重复实体 — 同 transcript 两次 autodream 幂等 ────────────

def test_placeholder_two_runs_idempotent_noop():
    """占位径两次 autodream 同 transcript: 第二跑全 NOOP, 实体/fact 数不增
    (四分支 ADD/UPDATE/DELETE/NOOP 不回归; 词典链既有 id 不重复建)。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "idem.db")
    tpath = Path(tmp) / "t.jsonl"
    _write_transcript(tpath, [
        {"type": "user", "message": {"content":
            [{"type": "text", "text": "FastAPI uses Pydantic."}]}},
        {"type": "assistant", "message": {"content":
            [{"type": "text", "text": "Kafka depends on Zookeeper."}]}},
    ])
    orig_embed = embedding.embed
    embedding.embed = lambda text, providers=None: []
    try:
        out1 = autodream.autodream("sess-idem", str(tpath))
        out2 = autodream.autodream("sess-idem", str(tpath))
    finally:
        embedding.embed = orig_embed

    assert out1["added"] == 2, out1
    assert out2 == {"added": 0, "updated": 0, "deleted": 0, "noop": 2}, (
        f"重跑应全 NOOP, got {out2}")
    conn = db.get_conn()
    n_ent = conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    n_fact = conn.execute(
        "SELECT COUNT(*) FROM fact WHERE status='active'").fetchone()[0]
    assert n_ent == 4, f"实体不重复 (FastAPI/Pydantic/Kafka/Zookeeper), got {n_ent}"
    assert n_fact == 2, f"fact 不重复, got {n_fact}"


# ── 验收 2 补: Extraction 同构 — gazetteer 输出可直接喂 autodream 消费面 ──

def test_gazetteer_output_wings_isomorphic():
    """与 wings 同构: Extraction(entities/edges/confidence/source_meta) 字段齐,
    边 subject/object 均有 declared entity (wings 契约), env 名禁入。"""
    r = gazetteer.extract("FastAPI uses Pydantic. ZHIPU_API_KEY is secret.")
    assert isinstance(r, Extraction)
    declared = {e.name for e in r.entities}
    for edge in r.edges:
        assert edge.subject in declared and edge.object in declared, (
            f"边端点未 declared: {edge}")
    assert not any("ZHIPU_API_KEY" in n for n in declared), (
        "env 变量名形态禁入实体 (adapter._ENV_PATTERN 精神)")

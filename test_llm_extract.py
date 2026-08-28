"""batch 12 验收测试: LLM 直抽通道 + regex 前置门禁 + 实体卫生门.

覆盖派单 §3 验收 1/2/4/5:
1. MEM_EXTRACT_CHANNEL 门禁: llm 默认 / regex 显式重开, 两档 e2e 冒烟
2. llm_extract mock provider 三路 (正常/坏 JSON/超时) + schema 校验
4. 实体卫生门四件套 (停用词/短名/自环/alias cap)
5. 无 fallback: grep llm_extract.py 无 regex 回落; LLM 失败 → 上抛而非降级

测试规范: 零网络零 LLM (mock provider 注入); env 逐测试 pin (conftest 全局
pin regex, llm 档测试 monkeypatch 覆盖)。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

import db
import embedding
import llm_extract
from llm_extract import (CHANNEL_LLM, CHANNEL_REGEX, ExtractFailed,
                         ProviderCallError, SchemaViolation, extract,
                         extract_channel, validate)
from llm_provider import Extraction as _Extraction


# ── 夹具: 隔离库 (mem.db + emb cache) ────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", CHANNEL_REGEX)
    monkeypatch.setattr(embedding, "_CACHE_DB", tmp_path / "emb.db")
    embedding.clear_cache()
    db.init(str(tmp_path / "memory.db"))
    yield
    db._conn = None
    db._conn_path = None


def _transcript(tmp_path: Path, text: str) -> str:
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(
        {"type": "user", "message": {"content": text}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    return str(p)


# ── mock provider ────────────────────────────────────────────────────

class MockProvider:
    """可编程 chat() mock: 按调用序返回预设 (str | Exception)。"""

    model = "mock-model"

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def chat(self, system, messages, max_tokens=1500, tools=None,
             tool_choice=None):
        self.calls.append((system, messages, tools))
        out = self.outcomes.pop(0) if self.outcomes else "{}"
        if isinstance(out, Exception):
            raise out
        return out


_GOOD_DOC = {
    "entities": [
        {"name": "dais", "type": "technical_term", "aliases": []},
        {"name": "logseq-cli", "type": "technical_term", "aliases": []},
    ],
    "facts": [
        {"subject": "dais", "predicate": "depends_on", "object": "logseq-cli",
         "value": None, "confidence": 0.9, "evidence": "它依赖 logseq-cli"},
    ],
}


# ── 验收 2: mock provider 三路 + schema 校验 ─────────────────────────

def test_extract_normal_path():
    """正常路: 好 JSON → Extraction 形状 (entities/edges/per-edge conf)。"""
    p = MockProvider(json.dumps(_GOOD_DOC, ensure_ascii=False))
    r = extract("dais 依赖 logseq-cli", provider=p)
    assert [e.name for e in r.entities] == ["dais", "logseq-cli"]
    assert len(r.edges) == 1
    assert r.edges[0].predicate == "depends_on"
    assert r.edges[0].confidence == 0.9
    assert r.source_meta["extractor_label"] == "llm"
    assert r.source_meta["prompt_version"] == llm_extract.PROMPT_VERSION
    assert r.source_meta["retries"] == 0


def test_extract_bad_json_retry_then_success():
    """坏 JSON → 1 次重试 (附违规反馈) → 重试成功收 Extraction。"""
    p = MockProvider("这不是 JSON {{{", json.dumps(_GOOD_DOC, ensure_ascii=False))
    r = extract("dais 依赖 logseq-cli", provider=p)
    assert len(r.edges) == 1
    assert r.source_meta["retries"] == 1
    # 重试消息含违规反馈
    assert "schema 校验" in p.calls[1][1][-1]["content"]


def test_extract_bad_json_retry_then_runtime_error():
    """坏 JSON 重试仍败 → RuntimeError 响亮 (ExtractFailed), 绝不静默。"""
    p = MockProvider("垃圾输出", "还是垃圾")
    with pytest.raises(ExtractFailed, match="schema"):
        extract("段", provider=p)
    assert len(p.calls) == 2  # 恰好重试一次


def test_extract_provider_timeout_raises():
    """超时 (网络层 ProviderCallError) → 立即 ExtractFailed (不重试网络)。"""
    p = MockProvider(ProviderCallError("network: timeout"))
    with pytest.raises(ExtractFailed, match="不可达"):
        extract("段", provider=p)
    assert len(p.calls) == 1


def test_schema_predicate_enum_enforced():
    """batch 13 反转: predicate 开放 (枚举门撤, 用户裁决「开放」) — 表外
    谓词现归一后通过; 空白/超长仍拒 (归一门)。"""
    doc = json.loads(json.dumps(_GOOD_DOC))
    doc["facts"][0]["predicate"] = "relates_to"
    _ents, edges, _ = validate(doc)
    assert edges[0].predicate == "relates_to"  # 开放通过
    doc["facts"][0]["predicate"] = "Competes With"
    _ents, edges, _ = validate(doc)
    assert edges[0].predicate == "competes_with"  # snake_case 归一
    doc["facts"][0]["predicate"] = "   "
    with pytest.raises(SchemaViolation, match="predicate 空"):
        validate(doc)


def test_schema_dangling_ref_rejected():
    """subject/object 未声明 → SchemaViolation。"""
    doc = json.loads(json.dumps(_GOOD_DOC))
    doc["facts"][0]["object"] = "未声明的实体"
    with pytest.raises(SchemaViolation, match="object 未声明"):
        validate(doc)


def test_schema_confidence_clamped():
    """confidence 0-1 clamp (越界值收拢, 不拒)。"""
    doc = json.loads(json.dumps(_GOOD_DOC))
    doc["facts"][0]["confidence"] = 1.7
    _, edges, agg = validate(doc)
    assert edges[0].confidence == 1.0
    assert agg == 1.0


def test_schema_selfloop_dropped():
    """自环: schema 层静默弃 (非违规)。"""
    doc = json.loads(json.dumps(_GOOD_DOC))
    doc["facts"][0]["object"] = "dais"
    entities, edges, _ = validate(doc)
    assert edges == []


def test_schema_evidence_required():
    """evidence 缺失 → SchemaViolation (source 不变式)。"""
    doc = json.loads(json.dumps(_GOOD_DOC))
    del doc["facts"][0]["evidence"]
    with pytest.raises(SchemaViolation, match="evidence"):
        validate(doc)


def test_schema_type_outside_falls_to_concept():
    """type 表外 → concept 收拢 (不拒整批)。"""
    doc = json.loads(json.dumps(_GOOD_DOC))
    doc["entities"][0]["type"] = "organization"
    entities, _, _ = validate(doc)
    assert entities[0].type == "concept"


def test_fenced_json_stripped():
    """```json 围栏剥壳解析。"""
    fenced = "```json\n" + json.dumps(_GOOD_DOC, ensure_ascii=False) + "\n```"
    p = MockProvider(fenced)
    r = extract("段", provider=p)
    assert len(r.edges) == 1


# ── 验收 1: 门禁两档 ─────────────────────────────────────────────────

def test_channel_default_llm(monkeypatch):
    """缺省/未知 env → llm (主径默认)。"""
    monkeypatch.delenv("MEM_EXTRACT_CHANNEL", raising=False)
    assert extract_channel() == CHANNEL_LLM
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "bogus")
    assert extract_channel() == CHANNEL_LLM  # 拼错落回主径


def test_channel_regex_reopen(monkeypatch):
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "regex")
    assert extract_channel() == CHANNEL_REGEX


def test_e2e_llm_channel_smoke(fresh_db, tmp_path, monkeypatch):
    """llm 档 e2e: autodream 段提取走 llm_extract (mock), 落 KG extractor='llm'。"""
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", CHANNEL_LLM)
    import autodream
    import store
    # mock 段提取 (真 LLM 调用在验收 3 smoke 单测覆盖)
    seg_doc = {
        "entities": [
            {"name": "护理担保", "type": "concept",
             "aliases": ["aged care guarantee"]},
            {"name": "澳洲", "type": "named_entity", "aliases": []},
        ],
        "facts": [
            {"subject": "护理担保", "predicate": "located_in", "object": "澳洲",
             "value": None, "confidence": 0.9,
             "evidence": "护理担保在澳洲的路径"},
        ],
    }
    monkeypatch.setattr(llm_extract, "extract",
                        lambda seg, provider=None: _Extraction(
                            entities=_to_ents(seg_doc),
                            edges=_to_edges(seg_doc),
                            confidence=0.9,
                            source_meta={"provider": "mock",
                                         "extractor_label": "llm"}))
    # resolver 零 embed (离线语义)
    monkeypatch.setattr(embedding, "embed",
                        lambda text, providers=None: [])
    monkeypatch.setattr(embedding, "embed_batch",
                        lambda texts, providers=None: [[] for _ in texts])
    r = autodream.autodream("s1", _transcript(tmp_path, "护理担保在澳洲的路径"))
    assert r["added"] == 1
    rows = list(db.get_conn().execute(
        "SELECT extractor FROM fact"))
    assert rows and all(x["extractor"] == "llm" for x in rows)


def _to_ents(doc):
    from llm_provider import EntityOut
    return [EntityOut(name=e["name"], type=e["type"], aliases=e["aliases"])
            for e in doc["entities"]]


def _to_edges(doc):
    from llm_provider import EdgeOut
    return [EdgeOut(subject=f["subject"], predicate=f["predicate"],
                    object=f["object"], topic=f["evidence"],
                    confidence=f["confidence"])
            for f in doc["facts"]]


def test_e2e_regex_channel_smoke(fresh_db, tmp_path):
    """regex 档 e2e: 显式 pin 重开, gazetteer 词典/regex 三路照走。"""
    import autodream
    import embedding as emb
    orig_embed, orig_batch = emb.embed, emb.embed_batch
    emb.embed = lambda text, providers=None: []
    emb.embed_batch = lambda texts, providers=None: [[] for _ in texts]
    try:
        r = autodream.autodream(
            "s1", _transcript(tmp_path, "用户使用 rust 开发; react 是一个框架"))
        assert r["added"] >= 1
    finally:
        emb.embed, emb.embed_batch = orig_embed, orig_batch


def test_llm_failure_skips_not_fallback(fresh_db, tmp_path, monkeypatch):
    """验收 5: LLM 失败 → ExtractFailed 上抛 (bootstrap 记 skip+errors),
    绝不静默降级 regex。"""
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", CHANNEL_LLM)

    def _fail(seg, provider=None):
        raise ExtractFailed("provider 不可达: mock")

    monkeypatch.setattr(llm_extract, "extract", _fail)
    import autodream
    with pytest.raises(ExtractFailed):
        autodream.autodream("s1", _transcript(tmp_path, "任意段"))
    # 无 fact 落库 (降级为零产出而非 regex)
    assert db.get_conn().execute("SELECT COUNT(*) FROM fact").fetchone()[0] == 0


def test_bootstrap_counts_errors_on_llm_failure(tmp_path, monkeypatch):
    """bootstrap.init_memory 既有 except RuntimeError 记账: errors+skip 行。"""
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", CHANNEL_LLM)
    monkeypatch.setattr(embedding, "_CACHE_DB", tmp_path / "emb.db")
    embedding.clear_cache()
    db.init(str(tmp_path / "m.db"))
    monkeypatch.setattr(llm_extract, "extract",
                        lambda seg, provider=None: (_ for _ in ()).throw(
                            ExtractFailed("provider 不可达")))
    import bootstrap
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("一些内容", encoding="utf-8")
    r = bootstrap.init_memory(str(src))
    assert r["errors"] >= 1
    assert r["added"] == 0


# ── 验收 4: 实体卫生门四件套 ─────────────────────────────────────────

def test_hygiene_stopwords_rejected():
    """停用词实体拒: 「可能/前一次/的同时完成」等黑名单。"""
    from autodream import _entity_hygiene_gate
    for bad in ("可能", "前一次", "的同时完成", "本次", "输出", "完成", "继续"):
        assert not _entity_hygiene_gate(bad), bad


def test_hygiene_min_length():
    """最小长度: CJK ≥2 / 拉丁 ≥3。"""
    from autodream import _entity_hygiene_gate
    assert not _entity_hygiene_gate("税")       # CJK 1 字
    assert _entity_hygiene_gate("税务")          # CJK 2 字
    assert not _entity_hygiene_gate("ab")        # 拉丁 2
    assert _entity_hygiene_gate("abc")           # 拉丁 3
    assert not _entity_hygiene_gate("")          # 空


def test_hygiene_selfloop_edge_dropped(fresh_db, tmp_path, monkeypatch):
    """自环边弃: subject==object 的 edge 不落 fact。"""
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", CHANNEL_REGEX)
    import autodream
    import embedding as emb
    orig_embed, orig_batch = emb.embed, emb.embed_batch
    emb.embed = lambda text, providers=None: []
    emb.embed_batch = lambda texts, providers=None: [[] for _ in texts]
    try:
        # 「rust 是一个 rust」类自环: regex is_a 门 subject==obj
        r = autodream.autodream(
            "s1", _transcript(tmp_path, "rust 是一个 rust 框架; 用户使用 rust"))
        rows = list(db.get_conn().execute("SELECT subject_id, object_id FROM fact"))
        for row in rows:
            assert row["subject_id"] != row["object_id"], "自环 fact 落库"
    finally:
        emb.embed, emb.embed_batch = orig_embed, orig_batch


def test_hygiene_alias_cap(fresh_db):
    """alias cap 32: 第 33 个别名拒收, 既有 32 不动。"""
    import store
    eid = store.put_entity("吸尘器测试实体", "concept")
    ok = [f"别名{i}" for i in range(32)]
    store.add_aliases(eid, ok)
    ent = store.get_entity(eid)
    assert len(ent["aliases"]) == 32
    # 超限: 新 alias 拒
    store.add_aliases(eid, ["第33个别名"])
    ent = store.get_entity(eid)
    assert len(ent["aliases"]) == 32
    assert "第33个别名" not in ent["aliases"]


# ── 验收 5: 无 fallback grep ─────────────────────────────────────────

def test_no_regex_fallback_in_llm_extract():
    """llm_extract.py 无 regex 回落: **AST 级** import 检查 (不 import
    extractor/gazetteer) + 'fallback'/'degraded' 降级字样 (红线锁死;
    AST 级防 docstring 提及误报)。"""
    import ast as _ast
    tree = _ast.parse(Path(llm_extract.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, _ast.ImportFrom):
            imported.add(node.module or "")
    assert "extractor" not in imported, "regex 回落红线: 不得 import extractor"
    assert "gazetteer" not in imported, "regex 回落红线: 不得 import gazetteer"
    # 代码级降级字样 (剥离 docstring/comment)
    code_only = "\n".join(
        line for line in Path(llm_extract.__file__).read_text(
            encoding="utf-8").splitlines()
        if not line.lstrip().startswith(("#", '"', "'")))
    assert "fallback" not in code_only.lower()
    assert "degraded" not in code_only.lower()


def test_prompt_version_synced_with_doc():
    """prompt 资产纪律: system prompt 与 docs/llm-extract-prompt.md 同文。"""
    doc = Path(__file__).parent / "docs" / "llm-extract-prompt.md"
    text = doc.read_text(encoding="utf-8")
    # 取文档代码块内的 prompt 全文比对首尾锚
    assert llm_extract.PROMPT_VERSION in text
    assert llm_extract._SYSTEM_PROMPT.splitlines()[0] in text
    assert llm_extract._SYSTEM_PROMPT.splitlines()[-1] in text


def test_prompt_v4_disciplines():
    """v4 资产守卫: object 纪律 / connected_to 抑制 / 数量短语反例 + docs 逐字同步。"""
    import re
    assert llm_extract.PROMPT_VERSION == "v4"
    sp = llm_extract._SYSTEM_PROMPT
    # object 纪律: 逐字可寻 + 抽象宾语先声明 + 数量短语不抽
    assert "entities 数组里逐字找到" in sp
    assert "幂等" in sp and "concept 实体" in sp
    assert "eight concurrent workers" in sp
    # connected_to 抑制
    assert "找不到任何更精确谓词时才可用" in sp
    # value 纪律
    assert "不要把 object 名复制进 value" in sp
    # few-shot 4 例
    assert len(re.findall(r"## 示例 \d", llm_extract._USER_TEMPLATE)) == 4
    # docs 逐字同步 (纪律强制, 升级自锚点比对)
    doc = (Path(__file__).parent / "docs" / "llm-extract-prompt.md").read_text(
        encoding="utf-8")
    m = re.search(r"## System prompt 全文 \(v4\)\n\n```\n(.*?)\n```", doc, re.S)
    assert m, "docs 缺 v4 prompt 全文块"
    assert m.group(1) == llm_extract._SYSTEM_PROMPT

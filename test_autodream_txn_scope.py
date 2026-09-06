"""债#10 (2026-09-06 修订): autodream 事务边界 — 抽取锁外 / 写入在事务内。

霸锁根因 (09-06 hook-recall.log 8 例 ``database is locked`` 的机制真身):
原 ``autodream()`` 把整条管道包进单个 ``db.transaction()``, 而 consolidate
在管道开头写下第一笔 → WAL 写锁从第 0 秒持有到全部段 LLM 抽取结束 (12-60s
× N 段, 分钟级) — recall boost / hook 记账等并发写全部撞死在 5s busy 上。

用户裁决「不要回退策略, 机制是怎样就怎样修」后的新事务边界:
- LLM 抽取 / embedding 预热: 全程锁外 (``in_transaction`` 必为 False);
- Phase c 按段事务: 写入确实在事务内 (``in_transaction`` True) —
  deferred BEGIN 下段内 resolve 嵌入/裁判网络 I/O 在首写前不持写锁。

本文件用 ``in_transaction`` 探针把这条边界钉成回归测试。
"""
import tempfile
from pathlib import Path

import db
import embedding
import llm_extract
import store
import vec_index
from autodream import autodream
from llm_provider import EdgeOut, EntityOut, Extraction

import json


def _setup_hermetic(tmp_path, monkeypatch):
    """tmp db + tmp embedding cache + 确定性向量 (零网络零生产库)。"""
    db._conn = None
    db._conn_path = None
    db.init(tmp_path / "txn.db")
    embedding._CACHE_DB = Path(tmp_path) / "embeddings.db"
    embedding.clear_cache()
    _VEC = [1.0, 0.0, 0.0] + [0.0] * (vec_index.VEC_DIM - 3)
    monkeypatch.setattr(embedding, "embed",
                        lambda text, providers=None: list(_VEC))
    return _VEC


def _write_transcript(p, text):
    p.write_text(json.dumps({"type": "user", "message": {"content": text}},
                            ensure_ascii=False) + "\n", encoding="utf-8")


def test_extraction_lockfree_and_writes_in_txn(tmp_path, monkeypatch):
    """抽取期无事务 (锁外), Phase c 写入期在段事务内 — 边界钉死。"""
    _setup_hermetic(tmp_path, monkeypatch)
    t = tmp_path / "sess.jsonl"
    _write_transcript(t, "用户使用 rust 与 python 构建工具链")

    seen = {}
    res = Extraction(
        entities=[EntityOut(name="memsvc", type="concept"),
                  EntityOut(name="rust", type="tool")],
        edges=[EdgeOut(subject="memsvc", predicate="uses", object="rust",
                       topic="memsvc 使用 rust 开发")],
        confidence=0.9, source_meta={"extractor_label": "llm"})

    def fake_extract(text):
        seen["extract_in_txn"] = db.get_conn().in_transaction
        return res

    monkeypatch.setattr(llm_extract, "extract_channel", lambda: "llm")
    monkeypatch.setattr(llm_extract, "extract", fake_extract)

    real_put = store.put_fact

    def spy_put(*a, **kw):
        seen.setdefault("put_in_txn", db.get_conn().in_transaction)
        return real_put(*a, **kw)

    monkeypatch.setattr(store, "put_fact", spy_put)

    r = autodream("sess-txn", str(t))

    assert r["added"] >= 1, f"llm 通道产物应落库, got {r}"
    assert seen.get("extract_in_txn") is False, (
        "LLM 抽取必须发生在写事务之外 (债#10 霸锁根治点) — "
        f"got in_transaction={seen.get('extract_in_txn')}")
    assert seen.get("put_in_txn") is True, (
        "Phase c 写入必须在段事务内 (批量 fsync 语义保留) — "
        f"got in_transaction={seen.get('put_in_txn')}")


def test_autodream_rerun_idempotent_under_new_txn_layout(tmp_path, monkeypatch):
    """事务重划不伤幂等: 同 transcript 二跑全 NOOP (增量决策契约)。"""
    _setup_hermetic(tmp_path, monkeypatch)
    t = tmp_path / "sess2.jsonl"
    _write_transcript(t, "用户使用 rust 与 python 构建工具链")

    r1 = autodream("sess-txn-2", str(t))
    assert r1["added"] >= 1
    r2 = autodream("sess-txn-2", str(t))
    assert r2["added"] == 0, f"二跑应全 NOOP, got {r2}"

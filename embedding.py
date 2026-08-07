"""mem-service embedding — OpenAI-compat REST providers (ADR-13 向量层 v6).

Local-first: LM Studio (port 16666, nomic-embed-text-v1.5, dim 768) default +
Ollama (11434, qwen3-embedding:4b, dim 2560) fallback. Both expose OpenAI-compat
``/v1/embeddings`` (LM Studio server 需 ``lms load`` 模型; Ollama 现成).

``embed(text)`` → list[float]; unreachable/empty → []. Passive providers (never
raise — caller falls back to next provider or treats as no signal). Memory cache
(text→vector) avoids re-embedding the same text.

Feasibility (实测 2026-08-07, 完整 vec baseline eval KG 21 syn/rew query):
qwen3-embedding-4b blind hit@5 = **57.1%** / positive 89.5% — 碾压 nomic(blind
14.3% / positive 68%); 中文原生 4B, 中英跨语言强(铁锈→rust HIT, nomic miss)。
注: 早期 9 对 cosine 小样本误导(qwen3 syn 0.575 ≈ irr 0.566 "重叠")选了 nomic,
完整 baseline 证伪 — **cosine 绝对值 ≠ 相对排序, 必跑 hit@k baseline**(教训)。

ADR-13: provider 抽象 local-first(LM Studio 用户指定 + Ollama fallback), OpenAI-compat
``/v1/embeddings`` seam(新 provider slot in by 实现 embed).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embed text → float vector. Passive: error → []."""
    def embed(self, text: str) -> list[float]: ...


@dataclass
class OpenAICompatEmbedding:
    """OpenAI-compat ``/v1/embeddings`` provider (LM Studio / Ollama / OpenAI).

    Passive: network/parse errors → empty list. ``model`` is the provider's model
    id (LM Studio /v1/models id; Ollama tag). body shape: ``{"model","input"}``.
    """
    base_url: str
    model: str
    timeout: float = 30.0

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/embeddings", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8", "replace"))
            data = doc.get("data") or []
            return list(data[0].get("embedding", [])) if data else []
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                KeyError, IndexError):
            return []


# 默认 provider 列表: LM Studio qwen3-embedding-4b(中文原生 4B, 实测 blind hit@5
# 57.1% 碾压 nomic 14.3%)优先, Ollama qwen3 fallback(同模型容错)。
LM_STUDIO = OpenAICompatEmbedding(
    "http://127.0.0.1:16666", "text-embedding-qwen3-embedding-4b")
OLLAMA = OpenAICompatEmbedding(
    "http://127.0.0.1:11434", "qwen3-embedding:4b")


def default_providers() -> list[EmbeddingProvider]:
    """LM Studio qwen3-embedding-4b first, Ollama qwen3 fallback(同模型容错).
    url/model 从 env (MEM_LMSTUDIO_*/MEM_OLLAMA_*) 读, 默认取 LM_STUDIO/OLLAMA 常量。
    ponytail: 两 provider 都 local OpenAI-compat, 同 seam; unreachable 自剔除。"""
    return [
        OpenAICompatEmbedding(
            os.environ.get("MEM_LMSTUDIO_URL", LM_STUDIO.base_url),
            os.environ.get("MEM_LMSTUDIO_MODEL", LM_STUDIO.model)),
        OpenAICompatEmbedding(
            os.environ.get("MEM_OLLAMA_URL", OLLAMA.base_url),
            os.environ.get("MEM_OLLAMA_MODEL", OLLAMA.model)),
    ]


# Two-tier cache: L1 内存(进程内, 跨 cli 调用丢) + L2 SQLite(跨进程持久, 解 cli 短命)。
# ADR-13 向量持久化方案 A(即时持久, 本地文件 embeddings.db, 每次 embed 查/写)。
_CACHE_DB: Path = Path(__file__).parent / "data" / "embeddings.db"
_cache: dict[str, list[float]] = {}   # L1
_cache_conn: sqlite3.Connection | None = None


def _cache_get_conn() -> sqlite3.Connection:
    """L2 SQLite 连接(惰性建表)。跨进程持久 text_hash → vector JSON。"""
    global _cache_conn
    if _cache_conn is None:
        _CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        _cache_conn = sqlite3.connect(str(_CACHE_DB), check_same_thread=False)
        _cache_conn.execute(
            "CREATE TABLE IF NOT EXISTS embed_cache ("
            "text_hash TEXT PRIMARY KEY, text TEXT, vector TEXT, model TEXT, created_at TEXT)")
        _cache_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embed_hash ON embed_cache(text_hash)")
        _cache_conn.commit()
    return _cache_conn


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_lookup(text: str) -> list[float] | None:
    """L1 内存 → L2 SQLite。hit 返回 vector(提升 L1), miss None。"""
    if text in _cache:
        return _cache[text]
    try:
        row = _cache_get_conn().execute(
            "SELECT vector FROM embed_cache WHERE text_hash=?", (_cache_key(text),)
        ).fetchone()
        if row:
            v = json.loads(row[0])
            _cache[text] = v   # 提升 L1(同进程后续命中免 SQLite 查)
            return v
    except (sqlite3.Error, json.JSONDecodeError):
        pass
    return None


def _cache_store(text: str, vector: list[float], model: str = "") -> None:
    """写 L1 + L2(INSERT OR REPLACE 即时持久)。"""
    _cache[text] = vector
    try:
        _cache_get_conn().execute(
            "INSERT OR REPLACE INTO embed_cache (text_hash, text, vector, model, created_at) "
            "VALUES (?,?,?,?,?)",
            (_cache_key(text), text, json.dumps(vector), model, ""),
        )
        _cache_get_conn().commit()
    except sqlite3.Error:
        pass


def embed(text: str, providers: list[EmbeddingProvider] | None = None) -> list[float]:
    """Embed ``text`` via providers (LM Studio default + Ollama fallback). Two-tier cache。

    Returns [] if no provider yields a vector (caller treats as no vec signal —
    recall should fall back to the字面/centrality/LIF score path).
    """
    cached = _cache_lookup(text)
    if cached is not None:
        return cached
    for p in (providers if providers is not None else default_providers()):
        v = p.embed(text)
        if v:
            _cache_store(text, v, getattr(p, "model", ""))
            return v
    return []


def clear_cache() -> None:
    """清 L1 内存 + 关 L2 连接(下次 _cache_get_conn 重连当前 _CACHE_DB)。
    测试 monkeypatch _CACHE_DB 后调此重置连接到 tmp db。"""
    global _cache_conn
    _cache.clear()
    if _cache_conn is not None:
        _cache_conn.close()
        _cache_conn = None


def _demo() -> None:  # ponytail self-check
    v = embed("用户使用 rust")
    assert v, "no embedding — is LM Studio/Ollama running?"
    print("embed ok, dim:", len(v))


if __name__ == "__main__":
    _demo()

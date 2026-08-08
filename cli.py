"""mem-service cli — ingest / recall / consolidate (ADR-1, ADR-5, ADR-5b).

Top seam is the cli module (Spec §6): both ``cli.ingest(...)`` (Python) and
``python cli.py ingest "..."`` (argv) drive the same pipeline.

- ingest: extract facts via the adapter (ADR-5b butterfly-wing LLM with regex
  fallback), persist them. ``fact.extractor`` = "llm" when the adapter's LLM
  vote won, "regex" when it fell back (ADR-5 upheld as fallback).
- recall: KG navigation → Fact list + α·match+β·centrality+γ·LIF 加权排序
  (ADR-4v2). scoring.ALPHA_MATCH/BETA_CENTRALITY/GAMMA_LIF fuse token match,
  pagerank centrality and LIF into the recall order. v1 substring match on
  Fact.value/predicate + entity.name LIKE underlies the match term (semantic
  recall deferred — ADR-4, Spec Defer).
- consolidate: dedup skeleton, no decay (Spec §4 story 4; Node D owns depth).

No ``query`` subcommand (debug via ``recall --verbose`` or sqlite3 — Spec §3).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import adapter
import autodream as autodream_mod
import bootstrap
import consolidate as consolidate_mod
import recall as recall_mod
import store
from llm_provider import LLMProvider


# ── .env 加载 (stdlib, 无依赖; 早于 provider 实例化) ─────────────────
def _load_env() -> None:
    """从同目录 .env 加载环境变量到 os.environ (setdefault: 不覆盖已存在的)。
    无 .env 文件则 no-op。provider (llm/embedding) 的 default_providers() 运行时
    读 env, 故 cli 加载时跑一次即可。"""
    p = Path(__file__).parent / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


# ── ingest ──────────────────────────────────────────────────────────

def ingest(text: str, source_ref: str | None = None,
           fact_type: str = "stable",
           providers: list[LLMProvider] | None = None,
           source_cwd: str | None = None) -> dict[str, Any]:
    """Extract facts from ``text`` via the adapter and persist them to the KG.

    The adapter runs butterfly-wing LLM extraction (ADR-5b) and falls back to
    the regex extractor (ADR-5) when no provider is reachable or confidence is
    low. ``fact.extractor`` reflects which path won: "llm" or "regex".
    ``fact_type`` is ADR-8 (default stable; ingest ``--fact-type`` overrides).
    Entities are lazily created from each fact's subject/object via
    ``_ensure_entity`` (re-extraction of a known name reuses its id).

    ``providers`` defaults to ``[CCRProvider()]`` (the deployed ccr router).
    Pass ``[]`` to force the regex fallback path (Spec §4 story 5).

    Returns a summary ``{"entities": n, "facts": [...]}`` (fact ids).
    """
    if providers is None:
        providers = adapter.default_providers()
    extracted = adapter.extract_facts(text, providers=providers)
    # "llm" when the adapter's LLM vote produced the surviving facts (its
    # source_meta carries provider ≠ "regex"); "regex" on fallback.
    ext_label = "regex" if extracted.source_meta.get("provider") == "regex" else "llm"
    source_refs = [source_ref] if source_ref else []

    # name → entity_id cache (this ingest's working set).
    name_to_id: dict[str, str] = {}

    fact_ids: list[str] = []
    for fact in extracted.facts:
        subj_id = _ensure_entity(fact.subject, name_to_id)
        if subj_id is None:
            continue
        # Object may be a multi-word phrase; store as literal value AND try to
        # link an object entity if the object name was seen this ingest. ADR-3:
        # object is value-carrier; object_id optional. Prefer linking when known.
        obj_name = fact.object
        obj_id = name_to_id.get(obj_name)
        fid = store.put_fact(
            subject_id=subj_id,
            predicate=fact.predicate,
            value=obj_name,
            object_id=obj_id,
            extractor=ext_label,
            fact_type=fact_type,
            source_cwd=source_cwd,
            source_refs=source_refs,
        )
        fact_ids.append(fid)

    return {"entities": len(name_to_id), "facts": fact_ids}


def _ensure_entity(name: str, cache: dict[str, str]) -> str | None:
    """Resolve a fact subject/object to an entity id, creating it if needed.

    Subjects/objects from relation patterns may be phrases not caught by the
    entity patterns (e.g. "用户", "笔记工具"); we still persist them as entities
    so the KG is navigable. None only on empty.
    """
    if not name:
        return None
    if name in cache:
        return cache[name]
    existing = store.find_entities_by_name(name)
    if existing:
        eid = existing[0]["id"]
    else:
        eid = store.put_entity(name, "inferred")
    cache[name] = eid
    return eid


# ── recall ──────────────────────────────────────────────────────────

def recall(query: str, verbose: bool = False,
           session_id: str | None = None, boost: bool = True,
           weights=None, use_vec: bool = False, delta: float | None = None,
           cwd: str | None = None, top_k: int | None = None,
           with_tag: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    """Return Facts relevant to ``query``, ordered by α·match+β·centrality+γ·LIF(+δ·vec_sim use_vec) 加权排序 (ADR-4v2/ADR-13).

    Thin wrapper over ``recall.recall``. ``use_vec=True`` 启用向量召回融合
    (ADR-13); ``cwd`` ADR-14 b 方案: 过滤 source_cwd(含 NULL 老数据兼容)。
    ``top_k`` 限制返回数量(默认 None 无截断)。
    """
    return recall_mod.recall(query, verbose=verbose, session_id=session_id,
                             boost=boost, weights=weights, use_vec=use_vec, delta=delta, cwd=cwd, top_k=top_k,
                             with_tag=with_tag)


# ── consolidate ────────────────────────────────────────────────────

def consolidate() -> dict[str, int]:
    """Decay + dedup pass (Spec §4.4; ADR-8 + ADR-6).

    Thin wrapper over ``consolidate.consolidate``. Phase 1 decays LIF per
    fact_type half-life (active→deprecated when LIF<0.1); phase 2 marks
    exact-duplicate Facts as superseded. Returns ``{decayed, deprecated,
    superseded, active}`` per the SKILL.md output contract.
    """
    return consolidate_mod.consolidate()


# ── autodream ──────────────────────────────────────────────────────

def autodream(session_id: str, transcript_path: str,
              cwd: str | None = None) -> dict[str, int]:
    """PreCompact autoDream: session transcript raw→KG incremental (ADR-10/11).

    Thin wrapper over ``autodream.autodream`` (LLM 蝴蝶翼 直连, 无 regex 降级 —
    LLM 不可用即 block)。``cwd`` ADR-14 b 方案: 记 source_cwd, recall --cwd 过滤。
    """
    return autodream_mod.autodream(session_id, transcript_path, source_cwd=cwd)


# ── init-memory (bootstrap) ─────────────────────────────────────────

def init_memory(memory_dir: str | None = None,
                source_cwd: str | None = None) -> dict[str, int]:
    """Seed KG from CC memory .md files (ADR-12)。``memory_dir`` 默认
    ``cc_memory_dir(cwd)``(与 synthesis-index 一致; 旧默认硬编码 ``~/.claude`` 全局目录 → 读错),
    ``source_cwd`` 默认 ``cwd``(ADR-14 记来源, 不再 NULL)。"""
    import projection
    cwd = source_cwd or os.getcwd()
    mem_dir = Path(memory_dir) if memory_dir else projection.cc_memory_dir(cwd)
    return bootstrap.init_memory(mem_dir, source_cwd=source_cwd or cwd)


# ── synthesis-index (ADR-15 P2: 散 index 对账 → MEMORY [mem] 唯一写入口) ──

def synthesis_index(scope: str | None = None, memory_dir: str | None = None,
                    session: str | None = None) -> dict:
    """对账散 mem-<id>.md → 回 KG → 重写 MEMORY.md [mem] 索引(ADR-15 P2, 唯一写入口)。

    Thin wrapper over ``projection.synthesis_index``。recall/autodream 建 mem-<id>.md,
    synthesis 集中收口写 MEMORY。冷启动空跳过不兜底; orphan [mem] 行永远删(orphan 文件删默认关)。
    """
    import projection
    cwd = scope or os.getcwd()
    mem_dir = Path(memory_dir) if memory_dir else projection.cc_memory_dir(cwd)
    return projection.synthesis_index(cwd, mem_dir, session_id=session)


# ── prune (DELETE 同步, ADR-17d) ─────────────────────────────────────

def prune(scope: str | None = None, memory_dir: str | None = None,
          dry_run: bool = False) -> dict:
    """CC memory md 删除 → KG fact soft-delete (ADR-17d)。手动触发
    (PostToolUse 不捕 ``rm``, 无 tool 触发删除 → 不自动)。re-ingest 的 DELETE 对称。
    Thin wrapper over ``bootstrap.prune_deleted``。"""
    import projection
    cwd = scope or os.getcwd()
    mem_dir = Path(memory_dir) if memory_dir else projection.cc_memory_dir(cwd)
    return bootstrap.prune_deleted(mem_dir, source_cwd=cwd, dry_run=dry_run)


# ── embed-backfill (存量 active fact value → L2 cache, ADR-13 通电) ──

def embed_backfill() -> dict[str, int]:
    """回填存量 active fact 的 value embedding 到 L2 cache (on-ingest 预计算补存量)。
    命中 cache 跳过 (embed 内部 lookup); provider 不可达 → embedded 不增 (passive)。"""
    import db
    import embedding
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT value FROM fact WHERE status='active' AND value IS NOT NULL AND value != ''"
    ).fetchall()
    distinct = {r["value"] for r in rows}
    embedded = sum(1 for v in distinct if embedding.embed(v))
    return {"active_facts": len(rows), "distinct_values": len(distinct), "embedded": embedded}


# ── argv entry ──────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cli", description="mem-service cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="extract+store text")
    ing.add_argument("text")
    ing.add_argument("--source", default=None)
    ing.add_argument(
        "--fact-type",
        dest="fact_type",
        default="stable",
        choices=("ephemeral", "stable", "permanent"),
        help="Fact lifetime class for decay (ADR-8); default stable",
    )

    rec = sub.add_parser("recall", help="recall facts for query")
    rec.add_argument("query")
    rec.add_argument("--verbose", action="store_true")
    rec.add_argument("--vector", action="store_true",
                     help="启用向量召回融合(ADR-13, 解 synonym/rewrite 字面盲区)")
    rec.add_argument("--cwd", dest="cwd", default=None,
                     help="ADR-14 过滤 source_cwd(本 cwd fact + NULL 老数据; 默认全 cwd)")
    rec.add_argument("--session", dest="session", default=None,
                     help="CC session id(默认 CLAUDE_CODE_SESSION_ID env)")
    rec.add_argument("--top-k", dest="top_k", type=int, default=None,
                     help="限制返回数量(默认无截断)")
    rec.add_argument("--with-tag", dest="with_tag", action="store_true",
                     help="返回 nested {results:[{fact,score,tag}]} shape(默认 list[dict]+_snaptag)")

    sub.add_parser("consolidate", help="dedup skeleton")

    dream = sub.add_parser("autodream", help="session transcript raw→KG incremental (ADR-10/11)")
    dream.add_argument("--session", dest="session", required=True, help="CC session id")
    dream.add_argument(
        "--transcript", dest="transcript", required=True,
        help="path to CC transcript JSONL",
    )
    dream.add_argument(
        "--cwd", dest="cwd", default=None,
        help="ADR-14 记 source_cwd(来源 cwd, 从 hook stdin cwd 传)",
    )

    initmem = sub.add_parser("init-memory", help="seed KG from CC memory .md (ADR-12)")
    initmem.add_argument(
        "--memory-dir", dest="memory_dir", default=None,
        help="CC memory dir (默认 ~/.claude/projects/-home-yy--claude/memory/)",
    )
    initmem.add_argument(
        "--cwd", dest="cwd", default=None,
        help="ADR-14 记 source_cwd(来源 cwd, 默认 NULL)",
    )

    reing = sub.add_parser("re-ingest", help="单 md → KG 增量 (ADR-17 b/c)")
    reing.add_argument("file", help="markdown 文件路径")
    reing.add_argument(
        "--cwd", dest="cwd", default=None,
        help="ADR-14 记 source_cwd(默认 os.getcwd)",
    )

    si = sub.add_parser("synthesis-index",
                        help="散 index 对账 → 重写 MEMORY.md [mem] (ADR-15 P2, MEMORY [mem] 唯一写入口)")
    si.add_argument("--scope", default=None, help="来源 cwd(默认 os.getcwd)")
    si.add_argument("--memory-dir", dest="memory_dir", default=None,
                    help="CC memory dir(默认 cc_memory_dir(scope))")
    si.add_argument("--session", dest="session", default=None,
                    help="CC session id(P2 占位, 暂仅日志用)")
    pr = sub.add_parser("prune",
                        help="CC memory md 删除 → KG fact soft-delete (ADR-17d, re-ingest 的 DELETE 对称)")
    pr.add_argument("--scope", default=None, help="来源 cwd(默认 os.getcwd)")
    pr.add_argument("--memory-dir", dest="memory_dir", default=None,
                    help="CC memory dir(默认 cc_memory_dir(scope))")
    pr.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只报不删(预览将 prune 的孤儿 fact)")
    sub.add_parser("embed-backfill",
                   help="回填 active fact value → L2 embedding cache (ADR-13 向量通电)")

    args = p.parse_args(argv)
    if args.cmd == "ingest":
        print(json.dumps(
            ingest(args.text, source_ref=args.source, fact_type=args.fact_type,
                   source_cwd=os.getcwd()),
            ensure_ascii=False,
        ))
    elif args.cmd == "recall":
        session_id = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")
        print(json.dumps(recall(args.query, verbose=args.verbose, session_id=session_id, use_vec=args.vector, cwd=args.cwd, top_k=args.top_k, with_tag=args.with_tag), ensure_ascii=False, default=str))
    elif args.cmd == "consolidate":
        print(json.dumps(consolidate()))
    elif args.cmd == "autodream":
        print(json.dumps(autodream(args.session, args.transcript, cwd=args.cwd), ensure_ascii=False))
    elif args.cmd == "init-memory":
        print(json.dumps(init_memory(args.memory_dir, source_cwd=args.cwd), ensure_ascii=False))
    elif args.cmd == "re-ingest":
        print(json.dumps(bootstrap.re_ingest_file(args.file, source_cwd=args.cwd or os.getcwd()), ensure_ascii=False))
    elif args.cmd == "synthesis-index":
        print(json.dumps(synthesis_index(scope=args.scope, memory_dir=args.memory_dir, session=args.session), ensure_ascii=False))
    elif args.cmd == "prune":
        print(json.dumps(prune(scope=args.scope, memory_dir=args.memory_dir, dry_run=args.dry_run), ensure_ascii=False))
    elif args.cmd == "embed-backfill":
        print(json.dumps(embed_backfill(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

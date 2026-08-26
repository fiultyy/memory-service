"""KG → logseq card projection (Entity→Page + Fact→Block).

单向快照投影:读 mem-service KG,通过 logseq-cli 写入 logseq graph。
幂等:property 定义安全重跑;page/block 以名称/content 为 key upsert。

Requires logseq-cli built and on PATH (or set LOGSEQ_CLI env):
    node /home/yy/tools/logseq/static/logseq-cli.js

Usage:
    python3 logseq_project.py [--db data/memory.db] [--graph Demo] [--dry-run]
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import db
import store

# logseq-cli binary (node bundle — no shebang, must invoke via node)
LOGSEQ_CLI = os.environ.get("LOGSEQ_CLI", "node /home/yy/tools/logseq/static/logseq-cli.js")


def _run(args: list[str], graph: str, **kw) -> tuple[int, str, str]:
    """Run logseq-cli with --graph prefix. Returns (rc, stdout, stderr)."""
    cmd = shlex.split(LOGSEQ_CLI) + ["--graph", graph] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=kw.get("timeout", 15))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


# Properties to define: name → type.  'status' is logseq built-in, skip.
PROPERTY_DEFS: list[tuple[str, str]] = [
    ("lif", "number"),
    ("confidence", "number"),
    ("fact_id", "default"),
    ("fact_type", "default"),
    ("extractor", "default"),
    ("valid_from", "default"),
    ("valid_to", "default"),
    ("entity_id", "default"),
    ("source", "default"),
    ("type", "default"),
    ("topic", "default"),
]


def ensure_properties(graph: str) -> dict[str, str]:
    """Step 0: define all properties (idempotent), return name→ident map.

    gotcha #1: properties must be defined before use.
    gotcha #2: property idents get random suffixes (e.g. lif-LYoatgnC).
    """
    for name, ptype in PROPERTY_DEFS:
        rc, out, err = _run(["upsert", "property", "--name", name, "--type", ptype], graph)
        # built-in properties return "Can't change" — safe to ignore

    # Fetch ident mapping
    rc, out, err = _run(["list", "property", "--output", "json"], graph)
    if rc != 0:
        print(f"ERROR listing properties: {err}", file=sys.stderr)
        return {}

    idents: dict[str, str] = {}
    try:
        data = json.loads(out)
        for item in data.get("data", {}).get("items", []):
            title = item.get("block/title", "")
            ident = item.get("db/ident", "")
            if title and ident:
                idents[title] = ident
    except json.JSONDecodeError:
        pass
    return idents


def _props_edn(props: dict) -> str:
    """Serialize dict → JSON-style EDN map for --update-properties.

    gotcha #3: keys are strings, not keywords: '{"lif" 0.72}'
    """
    parts = []
    for k, v in props.items():
        if isinstance(v, bool):
            parts.append(f'"{k}" {"true" if v else "false"}')
        elif isinstance(v, (int, float)):
            parts.append(f'"{k}" {v}')
        elif v is not None:
            parts.append(f'"{k}" "{v}"')
    return "{" + " ".join(parts) + "}"


def _fact_content(fact: dict, entities: dict[str, dict]) -> str:
    """Human-readable block content: [[subject]] predicate [[object]]."""
    subj = entities.get(fact["subject_id"], {}).get("name", "?")
    pred = fact.get("predicate", "")
    topic = fact.get("topic", "")

    if fact.get("object_id"):
        obj = entities.get(fact["object_id"], {}).get("name", "?")
        base = f"[[{subj}]] {pred} [[{obj}]]"
    else:
        val = fact.get("value", "")
        base = f"[[{subj}]] {pred} {val}".strip()

    if topic and topic.strip():
        base = f"{base} — {topic}"
    return base

def _clear_page_blocks(graph: str, page_name: str) -> int:
    """Delete all fact blocks under a page before re-projecting (idempotency).

    Queries page's child blocks, removes each. The page node itself
    (entity title block) is preserved — only its child fact blocks are cleared.
    """
    # Find page id first
    rc, out, err = _run(
        ["query", "--query",
         f'[:find (pull ?p [:db/id]) :where [?p :block/name "{page_name}"]]',
         "--output", "json"], graph)
    if rc != 0:
        return 0
    try:
        page_id = json.loads(out)["data"]["result"][0][0]["db/id"]
    except (json.JSONDecodeError, KeyError, IndexError):
        return 0

    # Find all child blocks (parent = page_id, but not the page itself)
    rc, out, err = _run(
        ["query", "--query",
         f'[:find (pull ?b [:db/id]) :where [?b :block/parent {page_id}] '
         f'[?b :block/uuid ?u]]',
         "--output", "json"], graph)
    if rc != 0:
        return 0

    removed = 0
    try:
        blocks = json.loads(out)["data"]["result"]
        for row in blocks:
            bid = row[0].get("db/id") if isinstance(row, list) and row else None
            if bid is not None:
                _run(["remove", "block", "--id", str(bid)], graph)
                removed += 1
    except (json.JSONDecodeError, KeyError):
        pass
    return removed


def project(db_path: str = "data/memory.db", graph: str = "Demo",
            dry_run: bool = False) -> dict:
    """Full KG → logseq projection."""
    conn = db.get_conn()
    db.init(db_path)

    # Load KG
    entities: dict[str, dict] = {}
    for row in conn.execute("SELECT * FROM entity").fetchall():
        ent = store._decode_entity(row)
        entities[ent["id"]] = ent

    facts = [store._decode_fact(row) for row in
             conn.execute("SELECT * FROM fact WHERE status = 'active'").fetchall()]

    stats = {"entities": len(entities), "active_facts": len(facts),
             "pages_created": 0, "blocks_created": 0, "blocks_cleared": 0, "errors": 0}

    if not entities:
        print("KG empty — nothing to project.")
        return stats

    if dry_run:
        print(f"DRY RUN: {len(entities)} entities → pages, {len(facts)} facts → blocks")
        for eid, ent in entities.items():
            print(f"  page: {ent['name']} (type={ent.get('entity_type','')})")
        for fact in facts:
            subj = entities.get(fact["subject_id"], {}).get("name", "?")
            print(f"  block @ {subj}: {_fact_content(fact, entities)}")
        return stats

    # Step 0: ensure properties + get ident map
    idents = ensure_properties(graph)
    stats["property_idents"] = len(idents)

    # Step 1: Entity → Page (+ page properties)
    for eid, ent in entities.items():
        name = ent["name"]
        etype = ent.get("entity_type", "")
        aliases = json.loads(ent.get("aliases") or "[]")

        # page properties
        page_props = {"source": "mem-service", "entity_id": eid}
        if etype:
            page_props["type"] = etype

        rc, out, err = _run(["upsert", "page", "--page", name, "--restore", "--output", "json"], graph)
        if rc == 0:
            stats["pages_created"] += 1
        else:
            print(f"ERROR page {name}: {err}", file=sys.stderr)
            stats["errors"] += 1
            continue

        # set page properties (separate step, gotcha #1: must exist first)
        if page_props:
            # Find page id from output
            try:
                page_id = json.loads(out)["data"]["result"][0]
                rc2, _, err2 = _run(
                    ["upsert", "block", "--id", str(page_id),
                     "--update-properties", _props_edn(page_props)], graph)
            except (json.JSONDecodeError, KeyError, IndexError):
                pass  # page created but couldn't set props — non-fatal
    # Step 1.5: Clear existing fact blocks (idempotent re-project)
    # Group facts by subject page, clear each page once before inserting.
    pages_with_facts = set()
    for fact in facts:
        subj_ent = entities.get(fact["subject_id"])
        if subj_ent:
            pages_with_facts.add(subj_ent["name"])
    for pname in pages_with_facts:
        cleared = _clear_page_blocks(graph, pname)
        stats["blocks_cleared"] += cleared

    # Step 2: Fact → Block (under subject entity's page)
    for fact in facts:
        subj_ent = entities.get(fact["subject_id"])
        if not subj_ent:
            continue
        page_name = subj_ent["name"]
        content = _fact_content(fact, entities)

        # Create block
        rc, out, err = _run(
            ["upsert", "block", "--target-page", page_name, "--content", content,
             "--output", "json"],
            graph, timeout=10)
        if rc != 0:
            print(f"ERROR block @ {page_name}: {err}", file=sys.stderr)
            stats["errors"] += 1
            continue

        # Extract block id (first element of result array)
        try:
            block_id = json.loads(out)["data"]["result"][0]
        except (json.JSONDecodeError, KeyError, IndexError):
            print(f"WARN: couldn't parse block id from: {out}", file=sys.stderr)
            stats["blocks_created"] += 1
            continue

        # Set block properties (gotcha #3: JSON-style EDN, no 'status' built-in)
        block_props = {
            "fact_id": fact["id"],
            "lif": round(fact.get("LIF", 0.0), 3),
            "confidence": round(fact.get("confidence", 0.0), 2),
            "fact_type": fact.get("fact_type", "stable"),
            "extractor": fact.get("extractor", ""),
        }
        if fact.get("valid_from"):
            block_props["valid_from"] = fact["valid_from"]
        if fact.get("valid_to"):
            block_props["valid_to"] = fact["valid_to"]

        rc2, _, err2 = _run(
            ["upsert", "block", "--id", str(block_id),
             "--update-properties", _props_edn(block_props)], graph)
        if rc2 != 0:
            print(f"WARN: block props failed for {block_id}: {err2}", file=sys.stderr)

        stats["blocks_created"] += 1

    return stats


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="KG → logseq card projection")
    p.add_argument("--db", default="data/memory.db")
    p.add_argument("--graph", default="Demo")
    p.add_argument("--dry-run", action="store_true",
                   help="Print mapping without writing to logseq")
    args = p.parse_args()

    summary = project(db_path=args.db, graph=args.graph, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

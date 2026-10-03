#!/usr/bin/env python3
"""批次3 迁移执行器 (md 面): 文件级 v4 重 ingest + 旧句级 atom 定点退场。

策略 (shadow-run 小样裁决, 2026-10-03): 逐 atom cos 覆盖判定分辨不足
(0.55-0.75 中间带 69%), 改文件级操作 — 原文 ⊇ 判官摘要 (信息论安全),
旧句级 atom 的信息源头 = 该 md, v4 段承载完整原文。

每文件三步 (单事务外):
1. re_ingest_file(md) — v4 车道 (MEM_SEMANTIC_CHUNK 缺省开) 段级入图,
   chunk sha 幂等 (shadow-run 若已切过不重复切 — 段水位仅 daemon spool 有,
   md 面重切一次, 可接受成本);
2. 旧句级 atom 退场: refs **全部**指向该 md 的 (跨源 merge atom 不碰)
   valid_to=now (双时态软删, 回滚 = 置回 NULL), vec_atom 行同步删;
3. 保留安全网: shadow-run keep 带 (cos <0.55) 的 atom 不退场 — 其摘要在
   原文无强对应 (判官增值或跨源内容), 单列报告。

用法: python3 scripts/migrate_v4_md.py [--files a.md,b.md] [--dry-run]
       [--shadow temp/shadow_v4_full.jsonl]
缺省 --files 空 = shadow 报告里的全部成功文件; --dry-run 只打印不动库。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))       # 仓根
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cli  # noqa: E402 — _load_env
import db   # noqa: E402
import vec_index  # noqa: E402
import bootstrap  # noqa: E402


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _shadow_segs(shadow: str, name: str) -> int:
    """shadow 报告里该文件的段数 (-1 = 无记录)。"""
    for line in Path(shadow).read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("file") == name:
            return r.get("segs", -1)
    return -1


def _retire_ids(conn, name: str, keep_ids: set[int]) -> list[int]:
    """refs 全部指向 name 的 live 句级 atom → 拟退场 id 列表。

    gist 非空 (v4 新段) 不碰; refs 含其他源 (transcript/其他 md) 不碰;
    keep_ids (shadow keep 带) 不碰。"""
    rows = conn.execute(
        "SELECT id, source_refs FROM atom "
        "WHERE valid_to IS NULL AND gist IS NULL").fetchall()
    out: list[int] = []
    for r in rows:
        if r["id"] in keep_ids:
            continue
        try:
            refs = json.loads(r["source_refs"] or "[]")
        except (ValueError, TypeError):
            refs = []
        refs = [s for s in refs if isinstance(s, str)]
        if refs and all(f"memory:{name}#" in s for s in refs):
            out.append(r["id"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", default="", help="逗号分隔 md 名; 空=shadow 全量")
    ap.add_argument("--shadow", default="temp/shadow_v4_full.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    keep_ids: set[int] = set()
    md_paths: dict[str, str] = {}
    if Path(args.shadow).exists():
        for line in Path(args.shadow).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "md" not in r:
                continue  # missing/failed 文件跳过
            md_paths[r["file"]] = r["md"]
            best = r.get("sub_cos_best") or []
            ids = r.get("old_ids") or []
            keep_ids |= {i for i, c in zip(ids, best) if c < 0.55}

    names = ([f.strip() for f in args.files.split(",") if f.strip()]
             if args.files else sorted(md_paths))
    if not names:
        print("无文件 (shadow 报告缺或 --files 空)")
        return 1

    conn = db.get_conn()
    totals = {"files": 0, "segments": 0, "retired": 0, "kept": 0}
    now = _now()
    for name in names:
        path = md_paths.get(name) or name
        if not Path(path).is_file():
            print(f"跳过 {name}: 源文件不在 ({path})")
            continue
        ids = _retire_ids(conn, name, keep_ids)
        if args.dry_run:
            # dry-run 零写入: re_ingest 也不跑 (它会真实入图), 段数从
            # shadow 报告取 (它切过同文件)
            r = {"segments": _shadow_segs(args.shadow, name)}
            keep_n = sum(1 for i in _ids_of(conn, name) if i in keep_ids)
            totals["kept"] += keep_n
            print(f"[dry] {name}: shadow_segs={r['segments']} "
                  f"retire_candidates={len(ids)} keep={keep_n}")
        else:
            r = bootstrap.re_ingest_file(path)
            for aid in ids:
                conn.execute(
                    "UPDATE atom SET valid_to=? WHERE id=? AND valid_to IS NULL",
                    (now, aid))
                vec_index.delete_atom(aid)
            conn.commit()
            print(f"{name}: segments={r.get('segments')} retired={len(ids)} "
                  f"(ingest atoms={r.get('atoms')} merged={r.get('merged')})")
        totals["files"] += 1
        totals["segments"] += r.get("segments", 0)
        totals["retired"] += len(ids)
    print(json.dumps(totals, ensure_ascii=False))
    return 0


def _ids_of(conn, name: str) -> list[int]:
    rows = conn.execute(
        "SELECT id, source_refs FROM atom WHERE valid_to IS NULL AND gist IS NULL"
    ).fetchall()
    out = []
    for r in rows:
        if f"memory:{name}#" in (r["source_refs"] or ""):
            out.append(r["id"])
    return out


if __name__ == "__main__":
    raise SystemExit(main())

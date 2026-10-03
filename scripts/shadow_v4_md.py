#!/usr/bin/env python3
"""批次3 shadow-run (md 面): 存量句级 atom 的源 memory md 重放 v4 切分,
产出迁移预览 — 只读生产库, 图零写入, 人审门输入。

对比口径:
- 旧 atom(摘要) vs 新段(原文) max-cos ≥0.75 → sub_hi (高置信 retire)
- 0.55-0.75 → sub_mid (人审带); <0.55 → keep (迁移后保留旧 atom)
- 新段集 = semantic_chunks 真实切分 (openrouter jev 云端, 走 laya_client)

增量落盘: --out 指定的 JSONL 逐文件 append (崩溃续跑: 已见文件跳过);
末尾打印汇总 + 50 边界抽查表 (md 报告)。

用法: python3 scripts/shadow_v4_md.py [--limit 20] [--out temp/shadow_v4.jsonl]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))        # 仓根 (cli/db)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # src 平铺

import cli  # noqa: E402 — _load_env: openrouter key / MEM_LAYA_BACKEND
import db   # noqa: E402
import embedding  # noqa: E402
from semantic_chunk import semantic_chunks  # noqa: E402

_MEMREF = re.compile(r"session:memory:(.+?\.md)#")
# 语义覆盖阈: 旧句级 atom.text 是 v2 判官**改写摘要** (非原文子串, 字面
# 子串判定恒 False — 小样实测 subsumed=0 的根因), 覆盖判定走 embedding
# cos, 分层见 docstring。
_SUB_COS = 0.70  # 仅 print 行合计口径 (hi+mid)


def _cos(a, b):
    import math
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _old_atoms_by_file(conn) -> dict[str, list[dict]]:
    """live 句级 atom 按 (源 md 文件名 → [ {id, text, cwd} ]) 聚合。

    只归 gist IS NULL 的行 — v4 新段 (有 gist) 不是迁移对象。atom 可能
    多源 (merge 通道), 任一 ref 命中 memory md 即入该文件组。"""
    out: dict[str, list[dict]] = {}
    rows = conn.execute(
        "SELECT id, text, source_refs, source_cwd FROM atom "
        "WHERE valid_to IS NULL AND gist IS NULL").fetchall()
    for r in rows:
        try:
            refs = json.loads(r["source_refs"] or "[]")
        except (ValueError, TypeError):
            refs = []
        for s in refs:
            if not isinstance(s, str):
                continue
            m = _MEMREF.search(s)
            if m:
                out.setdefault(m.group(1), []).append(
                    {"id": r["id"], "text": r["text"], "cwd": r["source_cwd"]})
                break  # 一个 atom 只归首个命中的 md (多源归并极少, 人审兜底)
    return out


def _locate_md(name: str, cwds: list[str]) -> Path | None:
    """按 atom.source_cwd 集定位源 md; 都不在则全库 glob 兜底。"""
    for c in dict.fromkeys(c for c in cwds if c):
        p = Path(c) / name
        if p.is_file():
            return p
    hits = list(Path.home().glob(f".claude/projects/*/memory/{name}"))
    hits += list(Path.home().glob(f".openclaw/workspace-*/memory/{name}"))
    return hits[0] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="文件数帽 (0=全部)")
    ap.add_argument("--out", default="temp/shadow_v4.jsonl")
    ap.add_argument("--seed", type=int, default=42, help="边界抽样种子")
    args = ap.parse_args()

    conn = db.get_conn()
    by_file = _old_atoms_by_file(conn)
    # 大文件优先 (atom 多的先审 — 人审门信号密度最高)
    ranked = sorted(by_file.items(), key=lambda kv: -len(kv[1]))
    if args.limit:
        ranked = ranked[: args.limit]

    done: set[str] = set()
    outp = Path(args.out)
    if outp.exists():  # 崩溃续跑
        for line in outp.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["file"])
            except (ValueError, KeyError):
                pass

    boundaries: list[tuple[str, str, str]] = []  # (file, 前句, 后句)
    totals = {"files": 0, "old": 0, "segs": 0, "sub_hi": 0,
              "sub_mid": 0, "keep": 0, "missing_md": 0, "failed": 0}
    with outp.open("a", encoding="utf-8") as w:
        for name, atoms in ranked:
            if name in done:
                continue
            md = _locate_md(name, [a["cwd"] for a in atoms])
            if md is None:
                rec = {"file": name, "error": "md-not-found",
                       "old": len(atoms)}
                totals["missing_md"] += 1
            else:
                try:
                    text = md.read_text(encoding="utf-8")
                    segs = semantic_chunks(text)
                except Exception as exc:  # noqa: BLE001 — 单文件失败不杀全量
                    rec = {"file": name, "error": f"{type(exc).__name__}: {exc}",
                           "old": len(atoms)}
                    totals["failed"] += 1
                    w.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    w.flush()
                    continue
                seg_texts = [s["text"] for s in segs]
                # 语义覆盖分层: 旧 atom(判官摘要) vs 段(原文) 的 max-cos —
                # 小样实测分布连续无天然切点 (med 0.63-0.70), 单阈值假分
                # 不如实分层: ≥0.75 高置信 retire / 0.55-0.75 人审带 /
                # <0.55 内容保留带 (旧 atom 迁移后不 retire)。embed 失败
                # (无 key) 整组退回字面子串 (保守下界, 全归保留带)。
                olds = [a["text"] for a in atoms]
                vecs = embedding.embed_batch(olds + seg_texts) or []
                if len(vecs) == len(olds) + len(seg_texts):
                    best = [round(max((_cos(vecs[i], vecs[j])
                                       for j in range(len(olds), len(vecs))),
                                      default=0.0), 3)
                            for i in range(len(olds))]
                else:
                    best = [1.0 if any(t in g for g in seg_texts) else 0.0
                            for t in olds]
                sub = sum(1 for x in best if x >= _SUB_COS)
                rec = {"file": name, "md": str(md), "old": len(atoms),
                       "segs": len(segs),
                       "seg_units": [s["units_n"] for s in segs],
                       "sub_hi": sum(1 for x in best if x >= 0.75),
                       "sub_mid": sum(1 for x in best if 0.55 <= x < 0.75),
                       "keep": sum(1 for x in best if x < 0.55),
                       "sub_cos_best": best,
                       "old_ids": [a["id"] for a in atoms]}
                sub = rec["sub_hi"] + rec["sub_mid"]
                totals["files"] += 1
                totals["segs"] += len(segs)
                totals["sub_hi"] += rec["sub_hi"]
                totals["sub_mid"] += rec["sub_mid"]
                totals["keep"] += rec["keep"]
                # 边界采样素材: 段接缝两侧首尾句
                for i in range(len(segs) - 1):
                    left = segs[i]["text"].strip().splitlines()[-1][:60]
                    right = segs[i + 1]["text"].strip().splitlines()[0][:60]
                    boundaries.append((name, left, right))
            totals["old"] += len(atoms)
            w.write(json.dumps(rec, ensure_ascii=False) + "\n")
            w.flush()
            print(f"{name}: old={rec['old']} segs={rec.get('segs')} "
                  f"hi={rec.get('sub_hi')} mid={rec.get('sub_mid')} "
                  f"keep={rec.get('keep')}")

    random.seed(args.seed)
    sample = random.sample(boundaries, min(50, len(boundaries)))
    rp = outp.with_suffix(".boundaries.md")
    with rp.open("w", encoding="utf-8") as w:
        w.write(f"# v4 迁移 shadow-run 边界抽查 ({len(sample)}/{len(boundaries)} 条)\n\n")
        for i, (f, left, right) in enumerate(sample, 1):
            w.write(f"{i}. **{f}**\n   - 前: {left}\n   - 后: {right}\n")
    print(json.dumps(totals, ensure_ascii=False))
    print("汇总/抽查:", outp, rp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

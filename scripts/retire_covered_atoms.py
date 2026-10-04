#!/usr/bin/env python3
"""句级存量直接清理 (2026-10-04 用户裁决「有清晰准确的标记就直接清理旧掉」):

live 句级 atom (gist NULL) vs live 段级 (gist NOT NULL) — 向量候选 +
laya 覆盖批判 ("段是否完全承载句级摘要的全部要点") → P≥0.65 直接 retire
(valid_to + vec 删 + supersedes 边)。

清晰标记 = laya 覆盖确认 (纯 cos 不可用: 摘要-原文对 med 0.63-0.70,
0.90 阈只命中极少数; cos≥0.55 只作判官候选闸)。分层落 JSONL:
covered(≥0.65) / review(0.50-0.65) / keep(<0.50); 断点续跑。

与 settle 通道关系: 本脚本是一次性消融; distill_chunk 已补 cov 问
(新段自动落 proposal → settle 夜间复核) — 本工具只处理存量。

用法: python3 scripts/retire_covered_atoms.py [--dry-run] [--min-cos .55]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cli  # noqa: E402 — _load_env
import db   # noqa: E402
import laya_client  # noqa: E402
import embedding  # noqa: E402

_COV_KEEP = 0.65   # 覆盖确认线
_COV_REVIEW = 0.50  # 边缘审阅带下限
_BATCH = 24        # laya 批大小 (对/请求)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-cos", type=float, default=0.55)
    ap.add_argument("--out", default="temp/retire_covered.jsonl")
    ap.add_argument("--limit", type=int, default=0, help="句级处理帽 (0=全部)")
    args = ap.parse_args()

    import numpy as np
    conn = db.get_conn()
    olds = conn.execute(
        "SELECT id, text FROM atom WHERE valid_to IS NULL AND gist IS NULL "
        "ORDER BY id").fetchall()
    segs = conn.execute(
        "SELECT id, text FROM atom WHERE valid_to IS NULL AND gist IS NOT NULL "
        "ORDER BY id").fetchall()
    print(f"句级 {len(olds)} × 段级 {len(segs)}")

    # 批量 embed → numpy 归一 → 矩阵 cos (一次算完, 无逐对 Python)
    ov = np.asarray(embedding.embed_batch([r["text"] for r in olds]) or [],
                    dtype="float32")
    sv = np.asarray(embedding.embed_batch([r["text"] for r in segs]) or [],
                    dtype="float32")
    if len(ov) != len(olds) or len(sv) != len(segs):
        print("embed 失败 (数量不符)", len(ov), len(sv))
        return 1
    ov = ov / (np.linalg.norm(ov, axis=1, keepdims=True) + 1e-9)
    sv = sv / (np.linalg.norm(sv, axis=1, keepdims=True) + 1e-9)
    M = ov @ sv.T                                   # [句级, 段级] cos 矩阵
    seg_ids = [r["id"] for r in segs]
    old_texts = {r["id"]: r["text"] for r in olds}
    seg_texts = {r["id"]: r["text"] for r in segs}

    # 判官任务: cos≥闸的 (句级, top1 段级) 对 (top2 备用不做 — 一对一够)
    tasks: list[tuple[int, int, float]] = []
    for i, r in enumerate(olds):
        j = int(M[i].argmax())
        c = float(M[i, j])
        if c >= args.min_cos:
            tasks.append((r["id"], seg_ids[j], c))
    print(f"判官候选对 {len(tasks)} (cos≥{args.min_cos})")
    if args.limit:
        tasks = tasks[: args.limit]

    outp = Path(args.out)
    done: set[int] = set()
    if outp.exists():
        for line in outp.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["old_id"])
            except (ValueError, KeyError):
                pass

    verdicts: dict[int, float] = {}
    batches = [tasks[i:i + _BATCH] for i in range(0, len(tasks), _BATCH)]
    for bi, b in enumerate(batches):
        b = [t for t in b if t[0] not in done]
        if not b:
            continue
        qs, state = {}, ""
        for k, (oid, sid, c) in enumerate(b):
            qs[f"c{k}"] = {"type": "noul",
                           "instructions":
                               f"Does [segment] fully restate every key point "
                               f"of [item {k}], so item {k} adds no information?"}
            state += (f"item {k}:\n{old_texts[oid][:400]}\n"
                      f"segment:\n{seg_texts[sid][:600]}\n\n")
        ans = None
        for _ in range(3):
            ans = laya_client.laya_batch(state, qs, timeout=120.0)
            if ans is not None:
                break
            time.sleep(2)
        if ans is None:
            print(f"  批 {bi}/{len(batches)} laya 失败 → 跳过 (续跑恢复)")
            continue
        for k, (oid, sid, c) in enumerate(b):
            a = ans.get(f"c{k}")
            p = float(a.get("noul") or 0.0) if isinstance(a, dict) else 0.0
            verdicts[oid] = p
        print(f"  批 {bi+1}/{len(batches)} ✓", flush=True)

    # 分层 + 落盘 + (非 dry) 退场
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    n_cov = n_rev = n_keep = 0
    with outp.open("a", encoding="utf-8") as w:
        for oid, sid, c in tasks:
            if oid in done:
                continue
            p = verdicts.get(oid, -1.0)
            layer = ("covered" if p >= _COV_KEEP
                     else "review" if p >= _COV_REVIEW else "keep")
            n_cov += layer == "covered"
            n_rev += layer == "review"
            n_keep += layer == "keep"
            if layer == "covered" and not args.dry_run:
                conn.execute(
                    "UPDATE atom SET valid_to=? WHERE id=? AND valid_to IS NULL",
                    (now, oid))
                import vec_index
                vec_index.delete_atom(oid)
                conn.execute(
                    "INSERT OR IGNORE INTO atom_edge(a_id,b_id,w,kind) "
                    "VALUES(?,?,?,'supersedes')", (sid, oid, 1.0))
                conn.commit()
            w.write(json.dumps({"old_id": oid, "seg_id": sid, "cos": round(c, 3),
                                "p_cov": p, "layer": layer},
                               ensure_ascii=False) + "\n")
        w.flush()
    print(json.dumps({"covered": n_cov, "review": n_rev, "keep": n_keep,
                      "dry": args.dry_run}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

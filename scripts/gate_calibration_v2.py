"""H6 gate 校准 (atom 召回面): 60 对 (query, atom) 真实跑 gate.run_gate_laya,
出 P(high) 分布 + 建议阈值, 落 temp/gate_calib_v2.json。

spec: docs/specs/graph-reform-v2-ingest-tags.md §五-H6 ("新标注集阈值实测")。

采样 (seed=42 确定性):
- 30 对真相关: 语义 tag (kind='semantic', level=1, 成员 ≥8) 取 30 个, 簇名当
  query, 簇内随机 1 个 member atom 当候选。
- 30 对不相关: 同 30 个 query, 候选 = 未挂该 tag 的随机 live atom (跨簇)。

判定走生产路径 gate.run_gate_laya (单 laya_batch 调用/对, 小 state; 每 40 对
歇 1s — 对应"40 问/片"节奏)。生产库 data/memory.db 只读连接, 零写入。

建议阈值: 遍历 P(high) 候切点取 accuracy 最大者 (并列取更保守 = 更高阈值),
圆整到 0.05。recall.GATE_P_HIGH_KEEP 手动回填该值 (常数 + 注释引用本文件)。

用法: python3 scripts/gate_calibration_v2.py [db_path] [--dry-run]
  --dry-run 只打印采样计划, 不调 laya。
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import cli  # noqa: F401,E402 — import 即 _load_env (.env: MEM_LAYA_ENABLED 等)
import gate  # noqa: E402
import laya_client  # noqa: E402
import scoring  # noqa: E402

N_PAIRS = 30          # 每类对数
MIN_MEMBERS = 8       # 语义簇最小成员数 (太小簇名不构成有意义的 query)
SLEEP_EVERY = 40      # 每 N 对歇 1s (小 state 40 问/片口径)
SLEEP_SEC = 1.0
SEED = 42


def _ro_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sample_pairs(conn: sqlite3.Connection, rng: random.Random) -> list[dict]:
    """[(query=簇名, atom, relevant: bool)] — 30 真 + 30 假。"""
    tags = conn.execute(
        "SELECT t.id, t.name, COUNT(*) c FROM tag t JOIN tag_mount m "
        "ON m.tag_id = t.id WHERE t.kind='semantic' AND t.level=1 "
        "GROUP BY t.id HAVING c >= ? ORDER BY c DESC", (MIN_MEMBERS,)).fetchall()
    rng.shuffle(tags)
    tags = tags[:N_PAIRS]
    live = [dict(r) for r in conn.execute(
        "SELECT id, text FROM atom WHERE valid_to IS NULL")]
    by_tag = {t["id"]: {r["atom_id"] for r in conn.execute(
        "SELECT atom_id FROM tag_mount WHERE tag_id = ?", (t["id"],))} for t in tags}
    pairs: list[dict] = []
    for t in tags:
        members = [a for a in live if a["id"] in by_tag[t["id"]]]
        if not members:
            continue
        pairs.append({"query": t["name"], "atom": rng.choice(members),
                      "relevant": True})
        outside = [a for a in live if a["id"] not in by_tag[t["id"]]]
        if not outside:
            continue
        pairs.append({"query": t["name"], "atom": rng.choice(outside),
                      "relevant": False})
    return pairs


def run_calibration(pairs: list[dict]) -> list[dict]:
    for i, p in enumerate(pairs):
        aid = f"a{p['atom']['id']}"
        anchors = {t for t in scoring.query_tokens(p["query"]) if len(t) >= 2}
        anchors.add(p["query"])
        v = gate.run_gate_laya({aid: p["atom"]["text"]}, p["query"], anchors)
        p["p_high"] = None if v is None else v[aid]["p_high"]
        if (i + 1) % SLEEP_EVERY == 0:
            time.sleep(SLEEP_SEC)
    return pairs


def _stats(vals: list[float]) -> dict:
    s = sorted(vals)
    n = len(s)
    if not n:
        return {"n": 0}
    return {"n": n, "min": s[0], "p25": s[n // 4], "median": s[n // 2],
            "p75": s[(3 * n) // 4], "max": s[-1],
            "mean": round(sum(s) / n, 4)}


def suggest_threshold(pairs: list[dict]) -> dict:
    rel = [p["p_high"] for p in pairs if p["relevant"] and p["p_high"] is not None]
    irr = [p["p_high"] for p in pairs if not p["relevant"] and p["p_high"] is not None]
    if not rel or not irr:
        return {"suggested": None, "reason": "某类样本 p_high 全缺失"}
    cands = sorted({round(x, 4) for x in rel + irr})
    best = None
    for t in cands:
        acc = (sum(1 for x in rel if x >= t) + sum(1 for x in irr if x < t)) \
            / (len(rel) + len(irr))
        # 并列取更高阈值 (保守: 少 keep)
        if best is None or acc > best[1] or (acc == best[1] and t > best[0]):
            best = (t, acc)
    return {"suggested": round(best[0] * 20) / 20,  # 圆整到 0.05
            "raw": best[0], "accuracy": round(best[1], 4)}


def main(argv: list[str]) -> int:
    db_path = Path(argv[0]) if argv and not argv[0].startswith("-") \
        else _ROOT / "data" / "memory.db"
    dry = "--dry-run" in argv
    conn = _ro_conn(db_path)
    rng = random.Random(SEED)
    pairs = sample_pairs(conn, rng)
    n_rel = sum(1 for p in pairs if p["relevant"])
    print(f"采样: {len(pairs)} 对 (真相关 {n_rel} / 不相关 {len(pairs) - n_rel}), "
          f"db={db_path}")
    if dry:
        for p in pairs[:6]:
            print(f"  [{'R' if p['relevant'] else 'X'}] q={p['query']!r} "
                  f"atom{p['atom']['id']}: {p['atom']['text'][:50]}")
        return 0
    if not laya_client.laya_available():
        print("laya 不可用 (health 探测失败) — 中止, 零样本")
        return 1
    t0 = time.time()
    pairs = run_calibration(pairs)
    fails = [p for p in pairs if p["p_high"] is None]
    print(f"laya 判定完成: {len(pairs) - len(fails)}/{len(pairs)} "
          f"({time.time() - t0:.0f}s); None 批 {len(fails)}")
    rel = [p["p_high"] for p in pairs if p["relevant"] and p["p_high"] is not None]
    irr = [p["p_high"] for p in pairs if not p["relevant"] and p["p_high"] is not None]
    out = {
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db": str(db_path), "seed": SEED, "n_pairs": len(pairs),
        "laya_none_fails": len(fails),
        "relevant_p_high": _stats(rel),
        "irrelevant_p_high": _stats(irr),
        "suggest": suggest_threshold(pairs),
        "pairs": [{"query": p["query"], "atom_id": p["atom"]["id"],
                   "relevant": p["relevant"], "p_high": p["p_high"],
                   "text": p["atom"]["text"][:160]} for p in pairs],
    }
    out_path = _ROOT / "temp" / "gate_calib_v2.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(json.dumps({k: out[k] for k in
                      ("relevant_p_high", "irrelevant_p_high", "suggest")},
                     ensure_ascii=False, indent=1))
    print(f"→ {out_path}")
    print(f"回填: recall.py GATE_P_HIGH_KEEP = {out['suggest']['suggested']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

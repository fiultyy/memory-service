#!/usr/bin/env python3
"""句级存量收尾清理 (2026-10-04, 用户裁决「继续」):

TTL 判据 p_dur<0.35 够不着 0.35-0.6 中间带 (~3730 滞留)。本脚本一次性
退场: live 句级 × (p_dur<0.6) × (非五类豁免) × (非 md 源)。

与 TTL 豁免口径对齐 (mem_daemon.py): judgment/experience/preference/
event/summary 不碰; md 源 (session:memory:*, 含人审 keep 网 133) 不碰;
高价值 p≥0.6 不碰。软删 (valid_to) + vec 同步删 + id 清单落盘可回滚。

用法: python3 scripts/retire_transcript_lowvalue.py [--dry-run]
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
import vec_index  # noqa: E402

_EXEMPT = "('judgment','experience','preference','event','summary')"
_P = 0.60  # 价值线 (与 transcript 面裁决「高价值 p≥0.6 保留」同口径)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-exempt", action="store_true",
                    help="连五类豁免一起清 (仍保 preference/event) — "
                         "死 session 低分存量收尾, 2026-10-04 用户裁决")
    args = ap.parse_args()

    label_cond = ("label NOT IN ('preference','event')" if args.include_exempt
                  else f"label NOT IN {_EXEMPT}")
    cond = (f"valid_to IS NULL AND gist IS NULL AND p_dur < {_P} "
            f"AND needs_audit=0 AND {label_cond} "
            "AND (source_refs IS NULL OR source_refs NOT LIKE '%session:memory:%')")
    conn = db.get_conn()
    rows = conn.execute("SELECT id FROM atom WHERE " + cond).fetchall()
    ids = [r[0] for r in rows]
    print(f"候选 {len(ids)} (p_dur<{_P}, 非豁免, 非md源)")

    if args.dry_run:
        for bucket, q in [("label", "SELECT label, COUNT(*) FROM atom WHERE id IN "
                            f"({','.join(map(str, ids))}) GROUP BY label")]:
            for lab, c in conn.execute(q).fetchall():
                print(f"  {bucket}={lab}: {c}")
        return 0

    out = Path("temp/retire_lowvalue_ids2.json" if args.include_exempt
               else "temp/retire_lowvalue_ids.json")
    out.write_text(json.dumps(ids), encoding="utf-8")  # 回滚凭据

    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"UPDATE atom SET valid_to=? WHERE {cond}", (now,))
        for aid in ids:
            vec_index.delete_atom(aid)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    print(f"retired {len(ids)} → valid_to={now}; id 清单 {out}; "
          f"回滚 = UPDATE atom SET valid_to=NULL WHERE id IN (清单)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

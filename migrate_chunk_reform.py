"""G2 存量全量重组 (spec: docs/specs/graph-granularity-reform-v1.md §三 G2).

一次性迁移脚本 (非库代码): 现有 active fact 全量重织成 chunk 关系,
无法聚合的直接物理 DELETE (仓内首破"无物理 DELETE"惯例 → 必先 snapshot)。

用法:
  python migrate_chunk_reform.py --dry-run [--limit N]   # 只读干跑报告 (默认)
  python migrate_chunk_reform.py --execute               # 真实写库 (orchestrator+用户闸门)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import chunk_graph
import db
import laya_client
import store
import vec_index

_ROOT = Path(__file__).parent
_SAMPLE_N = 25  # 干跑报告样本结论句数 (规格 ≥20)


def snapshot(db_path: str | Path) -> str:
    """cp db (含 -wal/-shm) 到 data/migration-backup-<ts>.db, 打印 md5, 返回路径。"""
    db_path = Path(db_path)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = _ROOT / "data" / f"migration-backup-{ts}.db"
    shutil.copy2(db_path, dest)
    for suf in ("-wal", "-shm"):
        side = Path(str(db_path) + suf)
        if side.exists():
            shutil.copy2(side, str(dest) + suf)
    md5 = hashlib.md5(dest.read_bytes()).hexdigest()
    print(f"[snapshot] {db_path} -> {dest} ({dest.stat().st_size} bytes, md5={md5})")
    return str(dest)


def _fact_text(topic: str | None, subject: str, predicate: str, obj: str) -> str:
    """fact 行 → 单句文本: topic 结论句优先 (T3 校准口径的自然语句),
    无 topic (agent 通道 8 条) 落回三元组拼接。fact 本身已是原子单元。"""
    if topic:
        return topic if topic.endswith(("。", ".", "！", "？", "?", "!")) else topic + "。"
    return f"{subject} {predicate} {obj}。"


def _active_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.id, f.predicate, f.value, f.topic, s.name AS sn, "
        "o.name AS oname "
        "FROM fact f JOIN entity s ON s.id = f.subject_id "
        "LEFT JOIN entity o ON o.id = f.object_id "
        "WHERE f.valid_to IS NULL AND f.status = 'active' "
        "ORDER BY f.subject_id, f.created_at").fetchall()


def plan(conn: sqlite3.Connection, limit_groups: int | None = None) -> dict:
    """纯只读干跑: 分组 → pack → aggregate (真 laya) → chunk 计划 + 汇总报告。

    laya 不可用 / 批 None → 该组标记 degraded (原样保留, 不计入删除)。
    """
    rows = _active_rows(conn)
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["sn"], []).append(r)
    gids = sorted(groups, key=lambda k: (-len(groups[k]), k))  # 大组在前, 稳定序
    sampled = gids
    coverage = 1.0
    if limit_groups and limit_groups < len(gids):
        stride = len(gids) / limit_groups
        sampled = [gids[int(i * stride)] for i in range(limit_groups)]
        coverage = sum(len(groups[g]) for g in sampled) / len(rows)

    orphan_ents = conn.execute(
        "SELECT count(*) FROM entity WHERE id NOT IN ("
        " SELECT subject_id FROM fact WHERE subject_id IS NOT NULL"
        " UNION SELECT object_id FROM fact WHERE object_id IS NOT NULL)"
    ).fetchone()[0]
    stats = {"total_facts": len(rows), "groups": len(gids),
             "sampled_groups": len(sampled), "coverage": coverage,
             "filtered": 0, "chunks": 0, "delete_facts": 0,
             "degraded_groups": 0, "degraded_names": [],
             "orphan_entities_now": orphan_ents,
             "samples": [], "group_plans": []}
    def _plan_group(rs):
        """单组 → chunks | None (laya 批 None → None, 调用方重试/降级)。"""
        units_map: dict[str, list[str]] = {}  # 文本 → 原 fact 清单 (同文本多 fact 一起吸收)
        for r in rs:
            units_map.setdefault(_fact_text(
                r["topic"], r["sn"], r["predicate"],
                r["oname"] or (r["value"] or "")), []).append(r["id"])
        units = list(units_map)
        chunks = chunk_graph.aggregate_chunks(units) \
            if len(units) > 1 else _singleton(units)
        if chunks is None:
            return None
        gp = {"subject": rs[0]["sn"],
              "fact_ids": [i for v in units_map.values() for i in v],
              # 三元组溯源: 迁移后可从 chunk fact 追溯旧边 (含 fact id)
              "triples": [f"{r['sn']}|{r['predicate']}|"
                          f"{r['oname'] or (r['value'] or '')}|{r['id']}" for r in rs],
              "chunks": chunks, "degraded": False,
              "n_filtered": len(units) - sum(len(c["units"]) for c in chunks)}
        return gp

    pending = [(gid, groups[gid]) for gid in sampled]
    for attempt in range(3):  # 主 pass + 2 轮降级重试 (长跑瞬时抖动恢复)
        if not pending:
            break
        if attempt:
            print(f"[retry] 第 {attempt} 轮降级组重试: {len(pending)} 组", flush=True)
            time.sleep(10)
        laya_client._avail_cache = None  # 强制重探测, 防单次失败污染 60s 级联
        nxt = []
        for done, (gid, rs) in enumerate(pending, 1):
            gp = _plan_group(rs)
            if gp is None:  # laya 批 None → 本轮降级, 留给重试轮
                nxt.append((gid, rs))
            else:
                stats["filtered"] += gp["n_filtered"]
                stats["chunks"] += len(gp["chunks"])
                stats["delete_facts"] += len(gp["fact_ids"])
                for c in gp["chunks"][:_SAMPLE_N - len(stats["samples"])]:
                    stats["samples"].append({"subject": gid, "text": c["text"][:120]})
                stats["group_plans"].append(gp)
            if done % 200 == 0:
                print(f"[plan] {done}/{len(pending)} 组完成, 本轮降级 {len(nxt)}, "
                      f"累计 chunk {stats['chunks']}", flush=True)
        pending = nxt
    stats["degraded_groups"] = len(pending)
    stats["degraded_names"] = [gid for gid, _ in pending]
    return stats


def _singleton(units: list[str]) -> list[dict]:
    """单 fact 组省 laya: 逐句过滤步已退役 (裁决#4, 2026-10-01) — 无条件
    自身成 chunk (G2 全量迁移已落地执行, 本函数仅存档复跑兼容)。"""
    return [{"text": u, "units": [u]} for u in units]


def _report(st: dict) -> str:
    lim = f" (采样 {st['sampled_groups']}/{st['groups']} 组, 覆盖 {st['coverage']:.1%})" \
        if st["coverage"] < 1.0 else ""
    lines = [
        "═══ G2 存量重组干跑报告 ═══",
        f"active fact 总数: {st['total_facts']}{lim}",
        f"聚成 chunk 数: {st['chunks']}",
        f"被过滤 fact 数 (过滤步已退役 2026-10-01, 恒 0): {st['filtered']}",
        f"预计物理删除 fact 数: {st['delete_facts']}",
        f"预计保留 (chunk fact) 数: {st['chunks']}",
        f"laya 降级组 (原样保留不迁): {st['degraded_groups']}",
        f"当前已无 fact 挂载 entity 数 (瘦身下界, execute 后更高: "
        f"被删组若未被 chunk 挂载点选中亦删): {st['orphan_entities_now']}",
        f"── chunk 文本样本 ({len(st['samples'])} 条) ──",
    ]
    lines += [f"  [{s['subject']}] {s['text']}" for s in st["samples"][:_SAMPLE_N]]
    return "\n".join(lines)


def execute(conn: sqlite3.Connection, plan_stats: dict) -> None:
    """事务内: ingest_chunks → 物理 DELETE 旧 fact → 删无挂载 entity → vec 重同步。"""
    n = conn.execute(
        "SELECT count(*) FROM fact WHERE extractor = 'chunk_graph'").fetchone()[0]
    if n:
        raise SystemExit(f"[拒绝] 已存在 {n} 条 chunk_graph fact — 疑似二次执行, "
                         "如确需重跑请先恢复 snapshot 备份。")
    if plan_stats["degraded_groups"]:
        raise SystemExit(f"[拒绝] 干跑有 {plan_stats['degraded_groups']} 个降级组 "
                         "(laya 不可用), 拒绝在降级态执行。")
    deleted_facts = deleted_ents = 0
    # supersedes 链跨组 (superseded 旧 fact 引用他组被删 active fact) → FK 挡组提交。
    # 全量重织口径下旧链无意义, 开跑前一次性斩断 (chunk 迁移不继承 supersedes)。
    with db.transaction():
        conn.execute("UPDATE fact SET supersedes_id = NULL")
    # 连接是 autocommit → 逐组一短事务 (锁窗口小, 崩溃恢复走 snapshot 还原)
    for i, gp in enumerate(plan_stats["group_plans"], 1):
        with db.transaction():
            if gp["chunks"]:  # 全滤组无 chunk 可落, 仅删
                fids = chunk_graph.ingest_chunks(gp["chunks"], source_cwd=None)
                assert fids, f"组 {gp['subject']} ingest 空返回"
                # source_refs 覆写为原三元组溯源 (ingest 默认存 topic 句, 无旧边信息)
                refs = json.dumps(gp["triples"], ensure_ascii=False)
                for fid in fids:
                    conn.execute("UPDATE fact SET source_refs = ? WHERE id = ?",
                                 (refs, fid))
            for fid in gp["fact_ids"]:
                conn.execute("DELETE FROM fact WHERE id = ?", (fid,))
                deleted_facts += 1
        for fid in gp["fact_ids"]:
            vec_index.delete_fact(fid)
        if i % 200 == 0:
            print(f"[execute] {i}/{len(plan_stats['group_plans'])} 组 "
                  f"(累计删 {deleted_facts} fact)", flush=True)
    # 尾事务: 非 active 旧 fact (superseded/deleted) 一并物理删 — 全量重织口径,
    # 旧图不残留; 多跳 supersedes 链靠 defer FK 到 commit 才闭合
    with db.transaction():
        conn.execute("PRAGMA defer_foreign_keys = ON")
        cur = conn.execute(
            "DELETE FROM fact WHERE extractor != 'chunk_graph'")
        deleted_facts += cur.rowcount
        # entity 瘦身: 无任何 fact 挂载的实体硬删 (aliases 随行删)
        orphans = [r[0] for r in conn.execute(
            "SELECT id FROM entity WHERE id NOT IN ("
            " SELECT subject_id FROM fact WHERE subject_id IS NOT NULL"
            " UNION SELECT object_id FROM fact WHERE object_id IS NOT NULL)")]
        for eid in orphans:
            conn.execute("DELETE FROM entity WHERE id = ?", (eid,))
            deleted_ents += 1
        conn.execute("DELETE FROM vec_fact WHERE fact_id NOT IN (SELECT id FROM fact)")
    for eid in orphans:
        vec_index.delete_entity(eid)
    print(f"[execute] 新 chunk fact 已落, 物理删除旧 fact {deleted_facts} 条, "
          f"硬删孤例 entity {deleted_ents} 个。")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="真实写库 (默认只读干跑)")
    ap.add_argument("--limit", type=int, default=None,
                    help="干跑采样组数 (全量太慢时用, 报告注明覆盖率)")
    args = ap.parse_args()
    if args.execute and args.limit:
        raise SystemExit("--execute 不允许与 --limit 同用 (必须全量计划)")

    db.init()  # 默认 data/memory.db
    conn = db.get_conn()
    if args.execute:
        snapshot(db._conn_path)
        if not laya_client.laya_available():
            raise SystemExit("[拒绝] laya 不可用, 拒绝执行。")
        st = plan(conn)
        execute(conn, st)  # 逐组短事务 (连接 autocommit; execute 内自管)
        print("[execute] 完成。回滚手段: snapshot 备份 + cp 覆盖。")
    else:
        st = plan(conn, limit_groups=args.limit)
        print(_report(st))


if __name__ == "__main__":
    main()

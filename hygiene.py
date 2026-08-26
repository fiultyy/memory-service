"""mem-service M12 投影卫生 cron — 零 LLM 三动作 (P17 v13 五站点收口 / U8).

从投影面拆出的独立卫生动作 (可高频独立于 dreaming; 全程零 provider 调用):

1. **去重**: 扫 ``mem-*.md`` 投影 (projection.MEM_FILE_RE 单一源识别), 
   frontmatter fact_id 对应 fact 已 superseded (被升级链/矛盾链 supersedes_id
   指走) → 删该投影文件 (被升级者的投影退场)。
2. **裁剪**: deprecated fact 的投影文件退场 (出热区)。
3. **重排**: 依 KG 现值 (mem_score/LIF) 重写 MEMORY.md [mem] 段排序 —
   **复用 projection.synthesis_index 达成** (写入口纪律: 该冻结指投影内容
   生成不另起炉灶; M12 重排即投影维护动作本身, 属 spec 授权面 P17 v13
   第五站点, 调用/复用其逻辑, synthesis_index 本体零改动)。

时序铁律 (spec 明示): KG 维护完成后才跑 (防复活 — 先删文件后 decay 会把
deprecated 又投影回来)。载体接线 = mem_daemon: 本轮 dream 已跑 → 卫生紧随
同轮; 否则按 ``_HYGIENE_INTERVAL`` 独立门控 (默认 3600s, 比日频 dream 高频)。

orphan 文件 (fact_id 不在 KG) 非 M12 职责 — synthesis_index 自有 env 门控
(``MEM_SYNTH_PRUNE_ORPHANS``), 本模块不越权。
"""

from __future__ import annotations

from pathlib import Path

import db
import projection
import signals
import store
from projection import MEM_FILE_RE, read_fact_id


def detect_human_proj_ops(mem_dir: Path | str) -> list[dict]:
    """M18 投影 diff 检测: mem-*.md 被 human 删除/修改 (mismatch 期望态)
    → human_proj_ops 信号 + 返回检出事件表。

    期望态 = active fact 有投影文件 (recall/synthesis 建的)。mismatch 两类:
    - deleted: active fact 的投影文件不存在 (human 删)
    - modified: 投影文件 fact_id 缺失/不可读 (human 改坏 frontmatter)
    顺序: **先记信号再由 run() 卫生重排** (期望态重写回, 持久写只发生在
    KG 档 — human 编辑不直接改 KG)。信号供 dream.run_cycle 消费 →
    human 档 invalidate/update 裁决 (provenance='human', veracity 0.9)。
    """
    mem_dir_p = Path(mem_dir)
    events: list[dict] = []
    if not mem_dir_p.is_dir():
        return events
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT f.id AS fid, f.subject_id FROM fact f WHERE f.status='active'"
    ).fetchall()
    for r in rows:
        # 期望文件名需 topic — 与 projection._mem_filename 同源推导。
        fact = store.get_fact(r["fid"])
        if fact is None:
            continue
        erow = conn.execute(
            "SELECT name FROM entity WHERE id=?", (fact["subject_id"],)).fetchone()
        subj = erow["name"] if erow else "?"
        fname = projection._mem_filename(r["fid"], projection._fact_topic(fact, subj))
        p = mem_dir_p / fname
        if not p.exists():
            events.append({"op": "deleted", "path": fname, "fact_id": r["fid"]})
            signals.append("human_proj_ops", {
                "op": "deleted", "path": fname, "fact_id": r["fid"],
                "detail": "active fact projection missing (human delete?)"})
        elif read_fact_id(p) is None:
            events.append({"op": "modified", "path": fname, "fact_id": r["fid"]})
            signals.append("human_proj_ops", {
                "op": "modified", "path": fname, "fact_id": r["fid"],
                "detail": "projection frontmatter unreadable (human edit?)"})
    return events


def run(cwd: str | None, mem_dir: Path | str) -> dict[str, int]:
    """跑一轮三动作, 返回 ``{"dedup_removed", "prune_removed", "resorted"}``。

    零 LLM: 全程纯 SQL + 文件操作, 无任何 provider 调用 (测试锁死)。
    """
    mem_dir_p = Path(mem_dir)
    stats = {"dedup_removed": 0, "prune_removed": 0, "resorted": 0}

    # ── M18 投影 diff 检测 (先行): human 删/改投影 → 信号落流 (供 dream
    #    消费 human 档裁决); 随后卫生按 KG 期望态重写回 (见 run 尾
    #    synthesis_index — project_fact_md 由 recall 重建, 本轮至少保证
    #    MEMORY [mem] 索引对账)。──
    stats["human_proj_events"] = len(detect_human_proj_ops(mem_dir_p))

    # ── 扫投影文件 → (fact_id, path); 同 synthesis_index 扫描惯例。──
    found: list[tuple[str, Path]] = []
    if mem_dir_p.is_dir():
        for p in mem_dir_p.glob("*.md"):
            if p.name == "MEMORY.md" or p.name.endswith(".tmp"):
                continue
            if not MEM_FILE_RE.match(p.name):
                continue
            fid = read_fact_id(p)
            if fid:
                found.append((fid, p))

    # ── ①去重 + ③裁剪: 按 fact 状态退场文件 (一次批量查, 非 N+1)。──
    if found:
        conn = db.get_conn()
        ph = ",".join("?" * len(found))
        rows = conn.execute(
            f"SELECT id, status FROM fact WHERE id IN ({ph})",
            [fid for fid, _ in found]).fetchall()
        status_by_id = {r["id"]: r["status"] for r in rows}
        for fid, p in found:
            status = status_by_id.get(fid)
            if status == "superseded":
                p.unlink(missing_ok=True)   # ① 被升级者投影退场
                stats["dedup_removed"] += 1
            elif status == "deprecated":
                p.unlink(missing_ok=True)   # ③ 出热区
                stats["prune_removed"] += 1
            # active / orphan (不在 KG): 不动 (orphan 是 synthesis_index 的
            # env 门控职责)。

    # ── ②重排: 复用 synthesis_index 对账重写 MEMORY [mem] (现值 mem_score
    #    排序; 文件已在上方退场 → 退场者不再入索引)。时序: 本模块由载体保证
    #    在 KG 维护 (dream decay/升级) 之后跑 — 防复活。──
    out = projection.synthesis_index(cwd or "", mem_dir_p)
    stats["resorted"] = out.get("projected", 0)
    return stats

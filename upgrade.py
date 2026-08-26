"""mem-service M4 upgrade_queue 队列 API — wings 异步升级的队列半边 (D2'/P18).

占位-升级时序: M6/M7 占位通道 (regex 0.4 档) 即时入库, 待升级素材入本队列;
实际消费方 = M11 dreaming 的 wings 升级 (本批不接真 LLM — 只供 API + 单测
fake 消费者验证流转)。

入队 wire 点 (两个, 均由 autodream 调用):
- **M8 点**: 超长段 (> ``_SEGMENT_BUDGET``) 截尾后全文 ref 入队
  (``segment:<path>#seg<n>``)。
- **M6 点**: 占位 fact (extractor='regex') 落库后待升级项入队
  (``fact:<fact_id>``)。

幂等: ``material_ref`` UNIQUE — 同 ref 二次入队 no-op (返 None), 同
transcript 重跑零重复行。

M4-v2 G3 出队默认 (spec [设] 已定):
- ``dequeue(limit=20)``: pending 按 priority 降序 (created_at ASC 稳定序)
  批取 → in_flight。
- ``mark_done(id)`` → done。
- ``mark_failed(id)`` → attempts+1; **attempts≥3 → dead** (冻结待人工,
  不无限重试), 否则退回 pending。

surprise/priority 由 M9 (:mod:`surprise`) 入队时算好落列 — 出队侧零计算。
"""

from __future__ import annotations

import uuid
from typing import Any

import db
import surprise as surprise_mod

DEAD_ATTEMPTS = 3          # G3: 失败 3 次冻结
DEFAULT_BATCH = 20         # G3: 每轮 ≤20 条
STATUS_FLOW = ("pending", "in_flight", "done", "failed", "dead")


def _now() -> str:
    from store import _now as _s_now  # 秒级 ISO 惯例统一 (store._now)
    return _s_now()


def enqueue(material_ref: str, *, transcript_path: str | None = None,
            byte_offset: int | None = None, text: str = "",
            predicates: tuple[str, ...] = (),
            entities: tuple[str, ...] = (),
            material_prov: str | None = None) -> str | None:
    """入队一项升级素材 (M9 surprise 入队时算)。同 material_ref 已在 → no-op
    返 None (幂等; done/dead 也不复活 — 冻结语义由人工/M11 重算路径接管)。"""
    conn = db.get_conn()
    if conn.execute("SELECT id FROM upgrade_queue WHERE material_ref = ?",
                    (material_ref,)).fetchone():
        return None
    s = surprise_mod.compute(text, predicates=predicates, entities=entities)
    qid = uuid.uuid4().hex
    now = _now()
    conn.execute(
        """INSERT INTO upgrade_queue
           (id, material_ref, transcript_path, byte_offset, surprise, priority,
            status, attempts, material_text, material_prov, created_at, updated_at)
           VALUES (?,?,?,?,?,?, 'pending', 0, ?, ?, ?, ?)""",
        (qid, material_ref, transcript_path, byte_offset,
         s["surprise"], s["priority"], text, material_prov, now, now),
    )
    conn.commit()
    return qid


def enqueue_segment(transcript_path: str, seg_index: int, text: str,
                    provenance: str | None = None) -> str | None:
    """M8 wire 点: 超长段被截尾的段全文 ref 入队 (material_ref 记定位+段序)。
    material_text = 段全文 (M11 wings 升级的提取输入, 源不变式)。"""
    return enqueue(f"segment:{transcript_path}#seg{seg_index}",
                   transcript_path=transcript_path, byte_offset=seg_index,
                   text=text, material_prov=provenance)


def enqueue_fact(fact_id: str, *, subject: str, predicate: str, obj: str,
                 provenance: str | None = None) -> str | None:
    """M6 wire 点: 占位 fact (extractor='regex') 落库后待升级项入队。
    material_text = 入队时由 transcript 提取面转写的三元组文本 (非 KG 回读)。"""
    return enqueue(f"fact:{fact_id}",
                   text=f"{subject} {predicate} {obj}".strip(),
                   predicates=(predicate,), entities=(subject, obj),
                   material_prov=provenance)


def dequeue(limit: int = DEFAULT_BATCH) -> list[dict[str, Any]]:
    """批取 pending (priority 降序, created_at ASC 稳定序) → in_flight (G3)。"""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM upgrade_queue WHERE status='pending' "
        "ORDER BY priority DESC, created_at ASC, id LIMIT ?",
        (limit,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        now = _now()
        conn.executemany(
            "UPDATE upgrade_queue SET status='in_flight', updated_at=? WHERE id=?",
            [(now, i) for i in ids],
        )
        conn.commit()
    return [dict(r) for r in rows]


def revert(item_ids: list[str]) -> int:
    """in_flight → pending 批量回退, 不加 attempts (M11: LLM 不可达整轮跳过时
    保住重试预算 — G3 attempts 只烧在真失败上)。返回回退行数。"""
    if not item_ids:
        return 0
    conn = db.get_conn()
    conn.executemany(
        "UPDATE upgrade_queue SET status='pending', updated_at=? "
        "WHERE id=? AND status='in_flight'",
        [(_now(), i) for i in item_ids],
    )
    conn.commit()
    return len(item_ids)


def mark_done(item_id: str) -> None:
    """in_flight → done (升级成功)。"""
    conn = db.get_conn()
    conn.execute(
        "UPDATE upgrade_queue SET status='done', updated_at=? WHERE id=?",
        (_now(), item_id),
    )
    conn.commit()


def mark_failed(item_id: str) -> str:
    """in_flight → failed: attempts+1; attempts≥DEAD_ATTEMPTS → dead 冻结,
    否则退回 pending 供下轮重试 (G3)。返回落定状态 ('pending'|'dead')。"""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT attempts FROM upgrade_queue WHERE id=?", (item_id,)).fetchone()
    if row is None:
        return "missing"
    attempts = int(row["attempts"] or 0) + 1
    status = "dead" if attempts >= DEAD_ATTEMPTS else "pending"
    conn.execute(
        "UPDATE upgrade_queue SET attempts=?, status=?, updated_at=? WHERE id=?",
        (attempts, status, _now(), item_id),
    )
    conn.commit()
    return status

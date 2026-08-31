"""mem-service M5 信号流 — 延迟消费的行为信号 (DR-1 D3 / DR-7 G6 已裁决).

布局 (G6 裁决: 全局单流 + source_cwd 字段隔离 — 不按 cwd 分片目录, 消费时
过滤, 与 ADR-14 b 方案同构): ``data/signals/*.jsonl``, 流 append-only
(只追加不改写 — 本模块不提供 update/delete API, 纪律即接口)。

    recall_hits            — recall 命中 (M10 改道时写; 唯一本批真实生产者)
    agent_crud             — agent 四动词 CRUD (M17 未来写入)
    citations              — 引用记账 (M16 未来写入)
    confirm_arrivals       — confirm 到达 (M17 未来写入)
    human_proj_ops         — human 投影操作 (M18 未来写入)
    contradiction_pending  — C1b 低通道矛盾挂起轻记录 (v1.7⑤ E7; 七字段
                             {ref, subject_id, predicate, old_value,
                             new_value, channel} + 公共 ts/source_cwd;
                             无 fact 状态变更 — 主径未来重抽同事实时自然裁决)

每条记录公共字段 ``{"ts": ISO8601, "source_cwd": str|null, ...流特有}``;
    ts 自动补 (秒级 ISO, store._now 惯例); 流特有字段 schema 为 [设] 档可调
    (派发令 §M5), 消费方 (M11 dreaming) 按需读取, 写侧不强制校验特有字段
    (前向兼容 — 新字段不改写旧行)。

读侧 (:func:`read`) 损坏行容错跳过 (append-only JSONL 被 external truncate
/ 半行写入时消费不崩, ADR-10 惯例)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 流白名单 (防 typo 混写他文件)。v1.7⑤ E7: + contradiction_pending (C1b
# 低通道矛盾挂起轻记录 — 勘误钉死落 signals 流, 不建新表)。
STREAMS = ("recall_hits", "agent_crud", "citations",
           "confirm_arrivals", "human_proj_ops", "contradiction_pending")

# 缺省信号目录: <module>/data/signals (与 db._DEFAULT_DB 同布局惯例)。
# 测试隔离: monkeypatch 本模块 ``_signals_dir`` (函数属性替换)。
_default_dir = Path(__file__).parent / "data" / "signals"


def _signals_dir() -> Path:
    return _default_dir


def stream_path(stream: str) -> Path:
    """流 → jsonl 文件路径 (不创建; append 时惰性建目录)。"""
    _require_stream(stream)
    return _signals_dir() / f"{stream}.jsonl"


def append(stream: str, record: dict[str, Any]) -> None:
    """追加一条信号 (append-only; 自动补 ts / source_cwd 缺省)。

    Args:
        stream: 流白名单之一 (否则 ValueError — 防混写)。
        record: 流特有字段 + 可选公共字段覆盖 (显式 ts/source_cwd 尊重调用方)。
    """
    _require_stream(stream)
    row = {"ts": _now(), "source_cwd": None}
    row.update(record)
    d = _signals_dir()
    d.mkdir(parents=True, exist_ok=True)  # 惰性创建
    with (d / f"{stream}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read(stream: str) -> list[dict[str, Any]]:
    """读全流 (损坏行容错跳过 — 半行/非 JSON 不崩, 供消费方与测试)。"""
    p = stream_path(stream)
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 损坏行容错 (ADR-10 惯例)
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _require_stream(stream: str) -> None:
    if stream not in STREAMS:
        raise ValueError(f"unknown signal stream {stream!r}; "
                         f"expected one of {STREAMS}")


def _now() -> str:
    from store import _now as _s_now  # 秒级 ISO 惯例统一 (store._now)
    return _s_now()

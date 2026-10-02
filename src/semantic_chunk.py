"""语义切分 (v4 地基, 2026-10-03 定案): 缝扫描 + 超长段回看再分。

atom 真实粒度 = 语义颗粒段 (非句级, 用户裁决 2026-10-02)。本模块把整文档
(memory md / transcript 段) 切成语义完整段, 供 distill_chunk 一段一 atom。

算法 (12 代理面板 + 真实文档 A/B 实测定案, 见 memory iteration 记录):
- **缝扫描 (gap-scan)**: 每相邻句缝 1 个 laya score 问「缝两侧是否换题」
  (局部对照, criteria: same-topic-continues / weak-shift / strong-boundary),
  P(strong-boundary) ≥ T_CUT → 边界。A/B 实测: jev 云端 med 0.08 决断,
  15 边界 11 个精确/±1 对齐标题位; 接续型问 (med 0.97 饱和) 已淘汰。
- **回看再分**: 超 MAXU 句的段内部二次缝扫描 (帽 1 层) — 修「巨块」缺陷
  (A/B 实测 71 句巨块)。
- **MINU 前向并**: <2 句的碎段并入前段 (机械)。
- **gist**: 每段 laya choice 选结论句 (chunk_graph.summarize_chunk 原样复用,
  失败降级首句)。

laya 后端: laya_client 单出口 (local 容器 / openrouter jev 云端同形契约)。

确定性口径 (云端实测): jev 非逐字节确定 — 双跑单边缘微段翻转 (~1/15 边界)。
幂等主张 = 段集级 (distill_seen sha 按段文本落, 边缘翻转段最多产生一对
近义 atom, 由 distill merge 通道收口), 不主张切分结果逐字节可重放。

挂起语义: laya 整批失败 (重试后仍败) → raise ChunkerUnavailable, 调用方
与 LayaUnavailable 同 lane (保留输入, 图零写入, 重跑零重复)。

env: MEM_CHUNK_T_CUT (0.45) / MEM_CHUNK_WIN (20) / MEM_CHUNK_MAXU (24) /
MEM_CHUNK_MINU (2) / MEM_CHUNK_GIST_OFF (缺省开 gist)。
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import chunk_graph
import laya_client

T_CUT = 0.45          # 缝扫描边界阈值 (A/B 实测此文档工作正常; 批次0校准面待标定)
WIN = 20              # 扫描窗句数 (中文 est 低估 4 倍: 20 句窗 ×4 ≈ 真实 ~4k token, 32k 内安全)
MAXU = 24             # 超此句数的段触发回看再分 (A/B 巨块 71u 的解)
MINU = 2              # 地板: <2 句的段前向并
_JOBS = 4             # 窗并行帽 (与 laya_client._SHARD_JOBS 同口径)
_ATTEMPTS = 3         # 云端偶发片失败 (~8% 实测) 重试
_TIMEOUT = 120.0      # 单批超时 (复盘: 超时要长过最坏单请求)

_CRIT = ["same-topic-continues", "weak-shift", "strong-boundary"]


class ChunkerUnavailable(Exception):
    """laya 批重试全败 — 挂起 lane (同 distill.LayaUnavailable 契约)。"""


def _gap_batch(window: list[str], w0: int) -> dict[int, float] | None:
    """一窗一次 laya_batch: 每句缝 1 个 score 问, 返回 {缝idx: P(strong-boundary)}。"""
    state = "\n".join(f"[{w0 + i}] {u[:200]}" for i, u in enumerate(window))
    questions: dict[str, dict] = {}
    for i in range(len(window) - 1):
        a = window[i][-60:].replace("\n", " ")
        b = window[i + 1][:60].replace("\n", " ")
        questions[f"cut{i}"] = {
            "type": "score",
            "instructions": f"In [S{i}] the text says: '...{a}' then '...{b}'. "
                            "Does a new semantic topic begin here?",
            "criteria": _CRIT}
    answers = laya_client.laya_batch(state, questions, timeout=_TIMEOUT)
    if answers is None:
        return None
    out: dict[int, float] = {}
    for k, a in answers.items():
        if not isinstance(a, dict) or not isinstance(a.get("probabilities"), dict):
            return None
        p = a["probabilities"].get("2")
        if not isinstance(p, (int, float)):
            return None
        try:
            out[int(k[3:])] = float(p)
        except ValueError:
            continue
    return out


def _gap_scan(units: list[str], t_cut: float) -> list[int]:
    """全序列缝扫描 → 边界句下标列表 (含 len(units) 终点)。窗并行, 重试 _ATTEMPTS。"""
    win = int(os.environ.get("MEM_CHUNK_WIN", str(WIN)))
    # 窗长 win+1 (含跨界缝), 步幅 win — 每条缝恰好被一窗覆盖, 无盲区
    # (bug 实录: 首版窗长 win 时缝 19/39 永不被扫描)
    wins = [(i, units[i:i + win + 1]) for i in range(0, len(units), win)]
    if not wins:
        return [0]

    def _scan(t: tuple[int, list[str]]) -> tuple[int, dict[int, float] | None]:
        i, w = t
        r = None
        for _ in range(_ATTEMPTS):
            r = _gap_batch(w, i)
            if r is not None:
                break
            time.sleep(1.5)
        return i, r

    with ThreadPoolExecutor(max_workers=min(_JOBS, len(wins))) as ex:
        results = list(ex.map(_scan, wins))
    cuts: set[int] = set()
    for i, r in results:
        if r is None:
            raise ChunkerUnavailable(
                f"laya gap-scan 批 {_ATTEMPTS} 次尝试后仍败 (window@{i}); "
                "输入保留, 图零写入")
        cuts |= {i + k for k, p in r.items() if p >= t_cut}
    return sorted(cuts | {len(units)})


def _split_by(units: list[str], bounds: list[int]) -> list[list[str]]:
    out, start = [], 0
    for b in bounds:
        seg = units[start:b]
        if seg:
            out.append(seg)
        start = b
    if start < len(units):
        out.append(units[start:])
    return out


def _merge_thin(segs: list[list[str]], minu: int) -> list[list[str]]:
    """<minu 句的碎段前向并 (机械, 无语义)。首段过短且无前段 → 与后段并。"""
    out: list[list[str]] = []
    for seg in segs:
        if out and len(seg) < minu:
            out[-1].extend(seg)
        else:
            out.append(list(seg))
    if len(out) >= 2 and len(out[0]) < minu:
        first = out.pop(0)  # 先取后插: out[1][:0]=out.pop(0) 求值序会错位
        out[0][:0] = first
    return [s for s in out if s]


def _gist(chunk_text: str, units: list[str]) -> str:
    """结论句: laya choice (summarize_chunk 原样复用); 失败/关闭 → 首句。"""
    if os.environ.get("MEM_CHUNK_GIST_OFF") != "1":
        try:
            got = chunk_graph.summarize_chunk(chunk_text, units)
            if got and got.strip():
                return got.strip()
        except Exception:
            pass  # gist 是增强面, 失败降级首句
    return units[0].strip()[:120]


def semantic_chunks(text: str) -> list[dict]:
    """整文本 → 语义段列表 [{text, gist, units_n}]。缝扫描 + 回看再分 + MINU 并。

    段 text = 原文单元拼接 (逐字保留, 不改写 — 改写是 distill_chunk 里 zhipu
    gist 判据的职责); gist = laya choice 结论句 (降级首句)。"""
    units = chunk_graph.split_units(text)
    if not units:
        return []
    t_cut = float(os.environ.get("MEM_CHUNK_T_CUT", str(T_CUT)))
    maxu = int(os.environ.get("MEM_CHUNK_MAXU", str(MAXU)))
    minu = int(os.environ.get("MEM_CHUNK_MINU", str(MINU)))

    segs = _split_by(units, _gap_scan(units, t_cut))
    # 回看再分 (帽 1 层): 超长段内部二次缝扫描 — 单轮窗扫描无二阶视野,
    # 巨块 (A/B 实测 71u) 在此拆开。仍超帽的残留段保留 (机械强切会碎语义)。
    refined: list[list[str]] = []
    for seg in segs:
        if len(seg) > maxu:
            try:
                refined.extend(_split_by(seg, _gap_scan(seg, t_cut)))
            except ChunkerUnavailable:
                raise  # 挂起不吞 — 整个文档重跑 (段 sha 幂等保零重复)
        else:
            refined.append(seg)
    segs = _merge_thin(refined, minu)

    out: list[dict] = []
    for seg in segs:
        chunk_text = "\n".join(seg)
        out.append({"text": chunk_text, "gist": _gist(chunk_text, seg),
                    "units_n": len(seg)})
    return out

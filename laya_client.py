"""Laya (System-1 batch judge) client — 一切 Laya 调用的唯一出口.

spec: docs/specs/laya-integration-spec-v1.1.md (T0/P0)
回退: MEM_LAYA_ENABLED=0 → laya_available() 短路 False, 零网络, 全链路=现状.

后端 (2026-10-02): MEM_LAYA_BACKEND = local (缺省, 本地容器 /predict) |
openrouter (typesafe/jev-1.13 经 OpenRouter chat completions — 本地容器
故障时的等价替换, 问题/返回契约同形)。openrouter 需 OPENROUTER_API_KEY;
模型 MEM_JEV_MODEL 覆盖 (缺省 typesafe/jev-1.13); 上下文上限 32k —
TOKEN_BUDGET=8000 est × 中文 len//4 低估 4 倍 ≈ 32k 真实 token, 分片
预算恰好自洽, 不另设闸。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

# base URL 无 path(v1.0 `/predict/health` 拼接 bug 禁止复发, grill A4)
LAYA_BASE = os.environ.get("MEM_LAYA_URL", "http://127.0.0.1:8190")

TOKEN_BUDGET = 8000  # 实测 7.2k 通过; 留余量(temp/laya-batch-request-design.md §0)
_SHARD_JOBS = 4      # laya_batch 超预算分片时的并行请求数帽

JEV_DECISIONS_URL = os.environ.get(
    "MEM_JEV_DECISIONS_URL", "https://openrouter.ai/api/alpha/decisions")
JEV_MODEL = os.environ.get("MEM_JEV_MODEL", "typesafe/jev-1.13")

def _backend() -> str:
    return os.environ.get("MEM_LAYA_BACKEND", "local")


def _openrouter_post(state: str, questions: dict, timeout: float) -> dict | None:
    """OpenRouter /alpha/decisions (jev 原生判官面) → /predict 同形返回。

    实测 (2026-10-02): 扁平 body {model, state, questions} → {answers: {...}}
    与本地容器 /predict 逐位同形 (score 型含 probabilities/legend; noul 型
    返回 noul 概率)。失败/answers 缺失 → None, 与 _post 失败语义一致。"""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return None
    req = urllib.request.Request(
        JEV_DECISIONS_URL,
        data=json.dumps({"model": JEV_MODEL, "state": state,
                         "questions": questions}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        answers = body.get("answers") if isinstance(body, dict) else None
        return {"answers": answers} if isinstance(answers, dict) else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None

# 进程级 TTL 缓存: 成功/失败都缓存, 避免交互路径每次探测
_avail_cache: tuple[float, bool] | None = None
_AVAIL_TTL = 60.0


def _enabled() -> bool:
    return os.environ.get("MEM_LAYA_ENABLED", "0") == "1"


def _post(path: str, payload: dict, timeout: float) -> dict | None:
    req = urllib.request.Request(
        LAYA_BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def laya_available() -> bool:
    """health 探测, TTL 60s 进程级缓存(成功/失败都缓存). 关 → False 零网络.

    openrouter 后端: key 在即视为可用 (无本地 health 面; 真失败走 laya_batch
    → None 的既有回落链, 不额外探测花钱)。"""
    global _avail_cache
    if not _enabled():
        return False
    if _backend() == "openrouter":
        return bool(os.environ.get("OPENROUTER_API_KEY"))
    now = time.monotonic()
    if _avail_cache is not None and now - _avail_cache[0] < _AVAIL_TTL:
        return _avail_cache[1]
    try:
        with urllib.request.urlopen(LAYA_BASE + "/health", timeout=3.0) as resp:
            ok = 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        ok = False
    _avail_cache = (now, ok)
    return ok


def _est_tokens(state: str, questions: dict) -> int:
    return len(state) // 4 + sum(len(json.dumps(q)) for q in questions.values()) // 4


def laya_batch(state: str, questions: dict, timeout: float = 30.0) -> dict | None:
    """单次 POST /predict; 超 token 预算按 question 均分 N 片(state 原样复制).

    任何失败(HTTP/超时/answers 键缺失)→ None, 不逐 question 重试, 不部分保真.
    """
    if not questions:
        return {}
    # 后端分发: openrouter (jev chat 面) 与 local 共用分片/预算/失败语义
    _send = ((lambda st, qs: _openrouter_post(st, qs, timeout))
             if _backend() == "openrouter"
             else (lambda st, qs: _post("/predict",
                                        {"state": st, "questions": qs}, timeout)))
    n_shards = max(1, -(-_est_tokens(state, questions) // TOKEN_BUDGET))
    if n_shards <= 1:
        resp = _send(state, questions)
        return resp.get("answers") if isinstance(resp, dict) and \
            isinstance(resp.get("answers"), dict) else None
    # 均分 N 片, 并行发 (容器服务端并发处理; ponytail: 线程帽 4 — laya 容器
    # 满载时超时放弃会级联排队, 并发过深反拖慢单片最坏延迟。要调改 _SHARD_JOBS)
    qids = list(questions)
    per = -(-len(qids) // n_shards)
    shards = [{qid: questions[qid] for qid in qids[i:i + per]}
              for i in range(0, len(qids), per)]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(_SHARD_JOBS, len(shards))) as ex:
        results = list(ex.map(lambda ch: _send(state, ch), shards))
    answers: dict = {}
    for resp in results:
        if not (isinstance(resp, dict) and isinstance(resp.get("answers"), dict)):
            return None
        answers.update(resp["answers"])
    return answers


def norm_score(answer: dict, n_criteria: int) -> float:
    """期望值 → [0,1]: score / (n_criteria - 1). 全集成唯一归一出口(3档/2, 4档/3)."""
    return answer["score"] / (n_criteria - 1)

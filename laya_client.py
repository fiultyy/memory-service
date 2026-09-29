"""Laya (System-1 batch judge) client — 一切 Laya 调用的唯一出口.

spec: docs/specs/laya-integration-spec-v1.1.md (T0/P0)
回退: MEM_LAYA_ENABLED=0 → laya_available() 短路 False, 零网络, 全链路=现状.
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
    """health 探测, TTL 60s 进程级缓存(成功/失败都缓存). 关 → False 零网络."""
    global _avail_cache
    if not _enabled():
        return False
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
    n_shards = max(1, -(-_est_tokens(state, questions) // TOKEN_BUDGET))
    if n_shards <= 1:
        resp = _post("/predict", {"state": state, "questions": questions},
                     timeout)
        return resp.get("answers") if isinstance(resp, dict) and \
            isinstance(resp.get("answers"), dict) else None
    # 均分 N 片
    qids = list(questions)
    per = -(-len(qids) // n_shards)
    answers: dict = {}
    for i in range(0, len(qids), per):
        chunk = {qid: questions[qid] for qid in qids[i:i + per]}
        resp = _post("/predict", {"state": state, "questions": chunk}, timeout)
        if not (isinstance(resp, dict) and isinstance(resp.get("answers"), dict)):
            return None
        answers.update(resp["answers"])
    return answers


def norm_score(answer: dict, n_criteria: int) -> float:
    """期望值 → [0,1]: score / (n_criteria - 1). 全集成唯一归一出口(3档/2, 4档/3)."""
    return answer["score"] / (n_criteria - 1)

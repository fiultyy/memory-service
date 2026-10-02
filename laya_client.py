"""Laya (System-1 batch judge) client — 一切 Laya 调用的唯一出口.

spec: docs/specs/laya-integration-spec-v1.1.md (T0/P0)
回退: MEM_LAYA_ENABLED=0 → laya_available() 短路 False, 零网络, 全链路=现状.

后端 (2026-10-02): MEM_LAYA_BACKEND = local (缺省, 本地容器 /predict) |
openrouter (typesafe/jev-1.13 经 OpenRouter /api/alpha/decisions — 本地容器
故障时的等价替换, 问题/返回契约同形)。openrouter 需 OPENROUTER_API_KEY;
模型 MEM_JEV_MODEL 覆盖 (缺省 typesafe/jev-1.13); 上下文上限 32k —
TOKEN_BUDGET=8000 est × 中文 len//4 低估 4 倍 ≈ 32k 真实 token, 分片
预算恰好自洽, 不另设闸。

后端 (2026-10-03): jevstyle — Jev-Style-0.8B Decision v3 本地容器
(~/projects/jev-style-ctr/, 中文 capable, laya multilingual 在 dense 中文
技术笔记上缝分布饱和 med 0.73 无法分隔的替补)。POST /v1/systemone 与
/predict 同 payload 同 answers 形状 (score 型 probabilities 数字键 "0".."K-1")。
URL MEM_JEV_STYLE_URL (缺省 http://127.0.0.1:8191); 上下文 25.6k 真实
token → est 帽 6000 (低估 4 倍自洽); health 面 /healthz。
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
JEV_STYLE_URL = os.environ.get("MEM_JEV_STYLE_URL", "http://127.0.0.1:8191")

def _backend() -> str:
    return os.environ.get("MEM_LAYA_BACKEND", "local")


def _base() -> str:
    """jevstyle 后端独立 base (8191); local 沿用 LAYA_BASE (8190)。"""
    return JEV_STYLE_URL if _backend() == "jevstyle" else LAYA_BASE


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
        _base() + path,
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
    → None 的既有回落链, 不额外探测花钱)。
    jevstyle 后端: GET /healthz (jev-style server 的 health 面与 laya /health
    不同名)。"""
    global _avail_cache
    if not _enabled():
        return False
    if _backend() == "openrouter":
        return bool(os.environ.get("OPENROUTER_API_KEY"))
    now = time.monotonic()
    if _avail_cache is not None and now - _avail_cache[0] < _AVAIL_TTL:
        return _avail_cache[1]
    hp = "/healthz" if _backend() == "jevstyle" else "/health"
    try:
        with urllib.request.urlopen(_base() + hp, timeout=3.0) as resp:
            ok = 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        ok = False
    _avail_cache = (now, ok)
    return ok


def _est_tokens(state: str, questions: dict) -> int:
    return len(state) // 4 + sum(len(json.dumps(q)) for q in questions.values()) // 4


def _token_budget() -> int:
    """后端感知分片预算: openrouter jev 上下文 32k → est 帽 8000; 本地容器
    max_len 8192 **真实** token, 而 len//4 est 对中文低估 ~4 倍 (实测 20 个
    全长中文 instructions est 2136 真实已撞顶 503) → est 帽 2048 (实测 n=19
    est≈2025 过, 留裕量)。env MEM_LAYA_LOCAL_BUDGET 覆盖。
    jevstyle: 25.6k 真实上下文, 过长整请求拒收 (不截断) → est 帽 6000
    (est×4≈24k, 留 6% 裕量)。env MEM_JEV_STYLE_BUDGET 覆盖。"""
    if _backend() == "local":
        return int(os.environ.get("MEM_LAYA_LOCAL_BUDGET", "2048"))
    if _backend() == "jevstyle":
        return int(os.environ.get("MEM_JEV_STYLE_BUDGET", "6000"))
    return TOKEN_BUDGET


def laya_batch(state: str, questions: dict, timeout: float = 30.0) -> dict | None:
    """单次 POST /predict; 超 token 预算按 question 均分 N 片(state 原样复制).

    任何失败(HTTP/超时/answers 键缺失)→ None, 不逐 question 重试, 不部分保真.
    """
    if not questions:
        return {}
    # 后端分发: openrouter (jev 云端) / jevstyle (本地 jev-style 容器) / local
    # 共用分片/预算/失败语义 — 三者 answers 同形
    _path = "/v1/systemone" if _backend() == "jevstyle" else "/predict"
    _send = ((lambda st, qs: _openrouter_post(st, qs, timeout))
             if _backend() == "openrouter"
             else (lambda st, qs: _post(_path,
                                        {"state": st, "questions": qs}, timeout)))
    n_shards = max(1, -(-_est_tokens(state, questions) // _token_budget()))
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

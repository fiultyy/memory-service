"""pytest 全局夹具。

1. 抽取通道 pin regex 档 (batch 12 §2.1/验收 6): 既有测试的提取语义全部
   构建在 regex 占位通道上 (词典/regex 三路, 零 LLM) — 默认通道改 llm 后,
   这些测试若不 pin 会真调智谱。llm 通道测试自行覆盖 env, 不受影响。

2. 信号目录全测试隔离 (2026-09-07): 测试若漏 patch signals 目录, 会把
   recall_hits / agent_crud 信号写进生产 data/signals/ (实测: recall_hits
   4 行 + agent_crud 88 行死引用, 最早 2026-08-27)。autouse 夹具统一改道
   tmp_path; 各测试自己的 _patch_signals_dir 在夹具之后设置、依然生效。

3. laya 开关逐测试 pin 关 (2026-10-01, M3): .env MEM_LAYA_ENABLED=1 经
   import cli 渗入测试进程, recall laya lane 会抢先于 mock gate provider —
   autouse 夹具 pin 0 + 清 laya_client TTL 缓存 (见 _pin_laya_off)。
"""

import os
import tempfile
from pathlib import Path

import pytest

import laya_client
import signals

os.environ.setdefault("MEM_EXTRACT_CHANNEL", "regex")

# 收集期护栏: 脚本式测试文件 (test_recall_p1.py 等) 在模块层直接 recall/
# store — 执行于任何 fixture 之前, 下方 autouse 夹具覆盖不到 (实测: 每次
# 全量收集泄漏 5 行 recall_hits 进生产 data/signals/)。conftest 的 import
# 先于全部测试模块收集, 在此完成会话级改道, 收集期写同样落 tmp。
_SESSION_SIG = Path(tempfile.mkdtemp(prefix="pytest-signals-"))
signals._signals_dir = lambda: _SESSION_SIG


@pytest.fixture(autouse=True)
def _isolate_signals_dir(tmp_path):
    """每个测试的 signals 目录 → tmp 隔离, 测试绝不污染生产信号流。"""
    orig = signals._signals_dir
    sig = tmp_path / "signals"
    signals._signals_dir = lambda: sig
    yield
    signals._signals_dir = orig


@pytest.fixture(autouse=True)
def _pin_laya_off(monkeypatch):
    """3. M3 (2026-10-01): .env MEM_LAYA_ENABLED=1 经 import cli → _load_env
    渗入测试进程 — recall 的 laya 批判 lane (recall.py ``laya_available()``)
    抢先于注入的 mock gate provider, 击穿零 LLM gate 测试 (真探本地 laya)。
    逐测试 pin 关 + 清 laya_client TTL 可用性缓存 (防上轮探测 True 跨测试
    泄漏); laya 专项测试在测试体内自设 env=1, 后设者胜不受影响。"""
    monkeypatch.setenv("MEM_LAYA_ENABLED", "0")
    # v4 语义段车道 (2026-10-03): 旧面测试 (re-ingest/bootstrap stub 等) 钉回
    # 句级车道 — semantic_chunks 会真调 laya, 破 hermetic; 新车道测试
    # (test_semantic_chunk) 直调模块/mock laya_batch, 不经此 env。
    monkeypatch.setenv("MEM_SEMANTIC_CHUNK", "0")
    laya_client._avail_cache = None
    yield

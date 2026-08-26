"""pytest 全局夹具: 抽取通道 pin regex 档 (batch 12 §2.1/验收 6)。

既有 216 测试的提取语义全部构建在 regex 占位通道上 (词典/regex 三路, 零
LLM) — 默认通道改 llm 后, 这些测试若不 pin 会真调智谱 (网络依赖 + 语义
漂移)。conftest 统一 pin ``MEM_EXTRACT_CHANNEL=regex``, 既有语义冻结。

batch 12 新增的 llm 通道测试 (test_llm_extract.py) 自行 monkeypatch/覆盖
env 为 llm 并注入 mock provider — 不受本 pin 影响 (monkyepatch 后设置)。
"""

import os

os.environ.setdefault("MEM_EXTRACT_CHANNEL", "regex")

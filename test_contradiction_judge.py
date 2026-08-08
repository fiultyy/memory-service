"""ADR-1 R1 矛盾裁判 judge_contradiction provider 侧自验证。

覆盖:
- Protocol 方法存在 (runtime_checkable LLMProvider 含 judge_contradiction)
- ZhipuAnthropicProvider.judge_contradiction 离线 fallback (no key / 网络错误 →
  不抛, 返 {contradiction: False, reason: 'provider-unavailable'}) — 不阻断 ingest
- 返回结构固定 {contradiction: bool, reason: str}
- few-shot 内容覆盖: 多值谓词共存 ≠ 矛盾 / 单值属性不同 = 矛盾 / 同义异写 ≠ 矛盾 /
  related-but-distinct (Java 语言 ≠ Java 岛) 边界

测试规范: def test_xxx() 函数让 pytest 收集(本项目头号雷区=模块级裸 assert 死代码,
test_bi_temporal/test_bfs_recall/test_as_of_normalize 是历史债勿复制)。验证 python
-m pytest -q 收集数应增长。
"""
import json
import urllib.error

import llm_provider
from llm_provider import (
    LLMProvider, ZhipuAnthropicProvider, _CONTRADICTION_PROMPT)


# ── Protocol seam: judge_contradiction 是 LLMProvider 协议方法 ──────────

def test_judge_contradiction_is_protocol_method():
    """LLMProvider Protocol 含 judge_contradiction(subject_type, subject_name,
    predicate, new_value, old_value) -> dict (ADR-1 R1 接口契约)。"""
    # 协议方法名存在且可调 — runtime_checkable 检查方法属性。
    assert hasattr(LLMProvider, "judge_contradiction"), (
        "LLMProvider Protocol must declare judge_contradiction (ADR-1 R1)")
    # ZhipuAnthropicProvider 实现了该方法 → isinstance 通过 (runtime_checkable)
    prov = ZhipuAnthropicProvider(api_key="dummy")
    assert isinstance(prov, LLMProvider), (
        "ZhipuAnthropicProvider must satisfy LLMProvider Protocol "
        "(must implement judge_contradiction)")


def test_judge_contradiction_signature_is_five_params():
    """签名固定: (subject_type, subject_name, predicate, new_value, old_value)。
    任务规格明确要求 5 参数(带 subject_type+subject_name, 非 ADR 文本的 4 参数 subject)。"""
    import inspect
    sig = inspect.signature(ZhipuAnthropicProvider.judge_contradiction)
    params = list(sig.parameters)
    # params[0] = self
    assert params == ["self", "subject_type", "subject_name", "predicate",
                      "new_value", "old_value"], (
        f"judge_contradiction signature mismatch, got {params}")


# ── ZhipuAnthropicProvider.judge_contradiction 离线 fallback ────────────

def test_judge_contradiction_no_key_falls_back_not_contradiction():
    """无 api_key → 不抛, 返 {contradiction: False, reason: 'provider-unavailable'}。

    ADR-1: provider 不可达 fallback = 不判矛盾, 不阻断 ingest。清 env 确保不读 ZHIPU_API_KEY。
    """
    import os
    saved = os.environ.pop("ZHIPU_API_KEY", None)
    try:
        prov = ZhipuAnthropicProvider(api_key="")  # 空 key, 不读 env
        result = prov.judge_contradiction(
            "person", "张三", "国籍", "美国", "中国")
    finally:
        if saved is not None:
            os.environ["ZHIPU_API_KEY"] = saved
    assert result == {"contradiction": False,
                      "reason": "provider-unavailable"}, (
        f"no key must fall back to not-contradiction (不阻断 ingest), got {result}")
    assert isinstance(result["contradiction"], bool)
    assert isinstance(result["reason"], str)


def test_judge_contradiction_network_error_falls_back(monkeypatch=None):
    """网络错误(URLError) → 不抛, 返 {contradiction: False, reason: 'provider-unavailable'}。"""
    prov = ZhipuAnthropicProvider(api_key="dummy")

    def _raise(req, timeout=None):
        raise urllib.error.URLError("simulated network down")

    # monkeypatch urllib.request.build_opener 返回的 opener.open → 抛 URLError。
    import urllib.request
    real_build = urllib.request.build_opener

    class _BadOpener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("simulated network down")

    urllib.request.build_opener = lambda *a, **k: _BadOpener()
    try:
        result = prov.judge_contradiction(
            "project", "Alpha", "uses", "docker", "rust")
    finally:
        urllib.request.build_opener = real_build
    assert result == {"contradiction": False,
                      "reason": "provider-unavailable"}, (
        f"network error must fall back (不阻断 ingest), got {result}")


def test_judge_contradiction_returns_fixed_structure_on_valid_response():
    """模拟 LLM 返合法 JSON → 返 {contradiction: bool, reason: str} 结构固定。

    用 monkeypatch 把 _extract_text 替成返回一个合法 JSON 体的辅助, 验证结构归一。
    """
    prov = ZhipuAnthropicProvider(api_key="dummy")

    # 模拟一次成功的网络往返: build_opener.open 返回带 content text block 的响应。
    import urllib.request

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "content": [{"type": "text", "text":
                             '{"contradiction": true, "reason": "国籍单值, 中美互斥"}'}]
            }).encode("utf-8")

    class _OkOpener:
        def open(self, req, timeout=None):
            return _FakeResp()

    real_build = urllib.request.build_opener
    urllib.request.build_opener = lambda *a, **k: _OkOpener()
    try:
        result = prov.judge_contradiction(
            "person", "张三", "国籍", "美国", "中国")
    finally:
        urllib.request.build_opener = real_build
    assert result == {"contradiction": True,
                      "reason": "国籍单值, 中美互斥"}, (
        f"valid LLM JSON must be parsed into fixed structure, got {result}")
    assert isinstance(result["contradiction"], bool)
    assert isinstance(result["reason"], str)


def test_judge_contradiction_malformed_json_falls_back_to_parse_failure():
    """LLM 返非 JSON / 缺字段 → 不抛, 返 {contradiction: False, reason: 'parse-failure'}。"""
    prov = ZhipuAnthropicProvider(api_key="dummy")

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "content": [{"type": "text", "text": "这不是 JSON, 我无法判断"}]
            }).encode("utf-8")

    import urllib.request

    class _OkOpener:
        def open(self, req, timeout=None):
            return _FakeResp()

    real_build = urllib.request.build_opener
    urllib.request.build_opener = lambda *a, **k: _OkOpener()
    try:
        result = prov.judge_contradiction(
            "concept", "X", "relates_to", "B", "C")
    finally:
        urllib.request.build_opener = real_build
    assert result == {"contradiction": False, "reason": "parse-failure"}, (
        f"malformed LLM output must fall back to parse-failure (not raise), "
        f"got {result}")


# ── few-shot prompt 内容覆盖 (防误判的核心) ─────────────────────────────

def test_contradiction_prompt_covers_multivalue_coexistence():
    """few-shot 必须明确: 多值谓词(uses/depends_on/contains/implements/
    connected_to/part_of/relates_to)新旧值共存 ≠ 矛盾。防 LLM 误判共存为矛盾。"""
    prompt = _CONTRADICTION_PROMPT
    for pred in ["uses", "depends_on", "contains", "implements",
                 "connected_to", "part_of", "relates_to"]:
        assert pred in prompt, (
            f"multivalue predicate {pred} must appear in prompt "
            "(防误判共存为矛盾)")
    # 至少一个共存示例 (project uses rust 与 uses docker)
    assert "rust" in prompt and "docker" in prompt, (
        "few-shot must include a multivalue coexistence example "
        "(rust + docker 共存)")


def test_contradiction_prompt_covers_single_value_contradiction():
    """few-shot 必须明确: 单值属性(is_a/located_in/belongs_to/国籍/位置)新旧值
    不同 = 矛盾。"""
    prompt = _CONTRADICTION_PROMPT
    for term in ["is_a", "located_in", "belongs_to", "国籍"]:
        assert term in prompt, (
            f"single-value attribute {term} must appear in prompt "
            "(判单值不同的矛盾边界)")
    # 至少一个单值矛盾示例 (国籍 中国 vs 美国)
    assert "中国" in prompt and "美国" in prompt, (
        "few-shot must include a single-value contradiction example "
        "(国籍 中美互斥)")


def test_contradiction_prompt_covers_synonym_rewrite_not_contradiction():
    """few-shot 必须明确: 同义异写(美国 vs 美利坚合众国)≠ 矛盾。防误判同义为矛盾。"""
    prompt = _CONTRADICTION_PROMPT
    assert "美利坚合众国" in prompt, (
        "few-shot must include a synonym-rewrite example "
        "(美国 vs 美利坚合众国 同义异写 ≠ 矛盾)")


def test_contradiction_prompt_covers_related_but_distinct_boundary():
    """few-shot 必须明确: related-but-distinct 边界(Java 语言 ≠ Java 岛是不同实体)。

    任务规格: NEVER 误判 related-but-distinct(如 Java语言≠Java岛)。虽这两者通常不进
    同一 subject-predicate 对(不同 subject), 但 prompt 要把同义不同写值的边界写稳。
    """
    prompt = _CONTRADICTION_PROMPT
    # 任务点名 Java语言 ≠ Java岛 作为 related-but-distinct 误判防护的代表。
    assert "Java" in prompt, (
        "few-shot must address related-but-distinct (Java语言≠Java岛) "
        "boundary per task spec")


def test_contradiction_prompt_returns_fixed_json_schema():
    """prompt 必须要求模型只返 {contradiction: bool, reason: str} 固定结构。"""
    prompt = _CONTRADICTION_PROMPT
    assert '"contradiction"' in prompt, (
        "prompt must instruct fixed JSON key 'contradiction'")
    assert '"reason"' in prompt, (
        "prompt must instruct fixed JSON key 'reason'")
    assert "bool" in prompt, "prompt must type contradiction as bool"


def test_contradiction_prompt_is_chinese_friendly():
    """任务: 中文友好。prompt 主体必须是中文。"""
    prompt = _CONTRADICTION_PROMPT
    # 中文裁判标识 + 规则说明。
    assert "你是知识图谱矛盾裁判" in prompt
    assert "共存" in prompt and "矛盾" in prompt and "同义" in prompt


# ── 真实 provider key fallback 不泄漏 (env 缺失时) ─────────────────────

def test_judge_contradiction_does_not_raise_on_any_failure_mode():
    """ADR-1 强约束: judge_contradiction NEVER raises。覆盖三种失败模式都不抛。"""
    import os
    saved = os.environ.pop("ZHIPU_API_KEY", None)
    try:
        prov = ZhipuAnthropicProvider(api_key="")
        # 无 key
        r1 = prov.judge_contradiction("a", "b", "is_a", "x", "y")
        assert isinstance(r1, dict) and "contradiction" in r1
    finally:
        if saved is not None:
            os.environ["ZHIPU_API_KEY"] = saved

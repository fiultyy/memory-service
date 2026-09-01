"""mem-service gate — v1.7③ 单 LLM 窄域一致性判定 (B 翼扩边守门).

spec v1.7-FINAL §③: B = wings 硬扩边(机械扩展, 不投票), gate = 1× LLM 窄域
一致性判定 —— "B 与 intent/A 锚定是否同一回事" (封闭判别题, 非有用性开放题)。
照搬 llm_extract 四件套先例 (tool-use schema 硬约束 / 整体拒 + 1 次带原因重试 /
evidence 式逐字锚断言 / 响亮失败), 一律 import 原语, 不改其文件。

失败语义 (spec §③ 原句, 红线):
- gate 判不匹配 (keep=false) → 该 fact 不入返回, 只注入 A —— no-op;
- gate LLM 不可用 (断供短路 / ProviderCallError / 超时 / 两轮 schema 败) →
  :class:`GateFailed` 响亮上抛, 调用方 (recall) 承接 = B 翼 fact 全部不入返回,
  **不降级、不静默当 keep**; A 路零 LLM 依赖不破坏 ("LM Studio 不在线照常工作")。

断供红线: 不传 provider/key (且 env 无 ZHIPU_API_KEY) 时 run_gate 直接短路为
"无 gate" (等同不可用), 不构造 provider 不发网络。

timeout (C4): gate 内层 8s —— 首轮档 15s = embedding 6s + gate 8s + 余量 1s,
内层各自传递 (本模块只管自己的 8s)。
"""

from __future__ import annotations

import json
import os
from typing import Any

import scoring
from llm_extract import (
    SchemaViolation,
    _evidence_verbatim,
    _parse_json_block,
)
from llm_provider import ProviderCallError, ZhipuAnthropicProvider

# C4: gate 内层 timeout (秒)。构造内部 provider 时传入, 外层首轮档预算见
# hooks 侧 MEM_RECALL_TIMEOUT (缺省 15s) —— 内层各自传递, 本模块不读那把尺。
GATE_TIMEOUT_SECONDS = 8.0

_GATE_TOOL_NAME = "report_gate_verdict"

# 输出 schema 硬约束 (照 llm_extract._TOOL_DEF 先例): 每条候选逐项判定。
_GATE_TOOL_DEF: dict = {
    "name": _GATE_TOOL_NAME,
    "description": "报告 B 翼候选 fact 与 query 意图的一致性判定结果",
    "input_schema": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string",
                               "description": "候选 fact id (逐字引用输入, 不新增不遗漏)"},
                        "keep": {"type": "boolean",
                                 "description": "与 query 意图/A 锚定是否同一回事"},
                        "match_score": {"type": "number",
                                        "description": "相关强度 0.0-1.0"},
                        "matched_anchor": {"type": "string",
                                           "description": "query 原文或候选 fact 文本的逐字子串"
                                                           "(锚词, 禁止改写/缩略/脑补)"},
                    },
                    "required": ["id", "keep", "match_score", "matched_anchor"],
                },
            },
        },
        "required": ["facts"],
    },
}

_SYSTEM_PROMPT = """你是记忆库召回链路的 gate: 窄域一致性判定器。
背景: 主检索 (A 路) 已按实体锚定确定性命中; 你面前的是图扩展 (B 翼) 候选,
它们图距近但未必与当前意图同一回事。你只做**封闭判别题** —— 判定每个候选
fact 与 query 意图/A 路锚定是否同一回事; 不做有用性/质量开放评判。
规则:
1. 必须调用 report_gate_verdict 工具, 对输入 candidates **逐条判定, 不遗漏、
   不新增、不改 id**。
2. keep=true 仅当候选与 query 意图实质同主题/同任务; 无关、仅词面巧合、
   话题漂移 → keep=false。
3. match_score ∈ [0,1] 表相关强度; keep=false 时给低分。
4. matched_anchor 必须从 query 原文或该候选 fact 文本中**逐字复制**一段锚词
   (空白容差内), 禁止改写/缩略/脑补 —— 高分布上锚不上 = 判定无效。
5. 只经工具参数输出, 不输出自由文本。"""


class GateFailed(RuntimeError):
    """gate 判定失败 (断供短路 / provider 不可达 / 两轮 schema 败) —— 响亮。

    调用方 (recall) 承接语义 = B 翼 fact 全部不入返回 (只注入 A);
    **绝不静默当 keep, 绝不降级** (spec §③ 失败语义红线)。"""


def derive_keywords(query: str) -> list[str]:
    """query 实体提取 (gazetteer 词典路, 确定性零 LLM/零 embed); 空则回退
    :func:`scoring.query_tokens` 分词。CLI 手动面升格三字段与注入面同源
    (v7 三句之一, 零分叉)。任何 gazetteer 异常 → 静默回退分词 (确定性兜底)。
    """
    names: list[str] = []
    try:
        import gazetteer
        masked = gazetteer._mask_code_zones(query or "")
        hits = gazetteer._dict_entity_hits(masked, gazetteer._load_gazetteer())
        names = [e.name for e in hits.values() if e.name]
    except Exception:
        names = []
    return names or scoring.query_tokens(query or "")


def build_request(query: str, scope: str) -> dict[str, Any]:
    """请求规范三字段升格 (spec §③): keywords 确定性实体提取 / intent=query 原文 /
    scope=调用面标签。注入面与 CLI 手动面共用本函数 (同一 gate schema 零分叉)。
    """
    return {
        "keywords": derive_keywords(query),
        "intent": query or "",
        "scope": scope,
    }


def validate(doc: Any, candidates: dict[str, str], query: str) -> dict[str, dict[str, Any]]:
    """gate 输出 schema 校验 (整体拒, 不静默丢条 — 坏输出混入的口子由重试承接)。

    规则:
    - 顶层 dict / ``facts`` 数组; 每项 id/keep/match_score/matched_anchor 必填;
    - ``id`` 必须 str 且与输入候选集**严格互耗**: 未知 id (幻觉) 拒, 输入侧
      未被判定 (dangling 覆盖缺口) 拒, 重复判定同 id 拒;
    - ``keep`` 严格布尔域 (str/int/None 都拒 — Python bool 是 int 子类, 显式排除);
    - ``match_score`` 数值 (非 bool) 否则拒; 数值越界 clamp 到 [0,1] (照
      llm_extract confidence clamp 先例 — 直接作 LIF 抬升权重, 防脏值入账);
    - ``matched_anchor`` 非空 str, 且必须是 query 原文**或**该候选 fact 文本的
      逐字子串 (复用 :func:`llm_extract._evidence_verbatim`, 含空白归一容差);
      伪造 → 整体拒进重试。

    Returns:
        ``{fact_id: {"keep": bool, "match_score": float, "matched_anchor": str}}``
    """
    if not isinstance(doc, dict):
        raise SchemaViolation(f"顶层非对象: {type(doc).__name__}")
    raw = doc.get("facts")
    if not isinstance(raw, list):
        raise SchemaViolation("facts 非数组")
    verdicts: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise SchemaViolation(f"fact 项非对象: {item!r}")
        fid = item.get("id")
        if not isinstance(fid, str) or not fid.strip():
            raise SchemaViolation(f"id 缺失/非 str: {item!r}")
        fid = fid.strip()
        if fid not in candidates:
            raise SchemaViolation(f"未知 fact id (不在候选集): {fid!r}")
        if fid in verdicts:
            raise SchemaViolation(f"fact id 重复判定: {fid!r}")
        keep = item.get("keep")
        if not isinstance(keep, bool):
            raise SchemaViolation(f"keep 非布尔: {keep!r}")
        ms = item.get("match_score")
        if isinstance(ms, bool) or not isinstance(ms, (int, float)):
            raise SchemaViolation(f"match_score 非数值: {ms!r}")
        ms = float(ms)
        if ms < 0.0:
            ms = 0.0
        elif ms > 1.0:
            ms = 1.0
        anchor = item.get("matched_anchor")
        if not isinstance(anchor, str) or not anchor.strip():
            raise SchemaViolation(f"matched_anchor 缺失/非 str: {fid!r}")
        anchor = anchor.strip()
        # 逐字锚断言: anchor ∈ query 原文 或 该候选 fact 文本 (空白归一容差)。
        fact_text = candidates.get(fid, "")
        if not (_evidence_verbatim(anchor, query or "")
                or _evidence_verbatim(anchor, fact_text)):
            raise SchemaViolation(
                f"matched_anchor 非逐字子串: {anchor[:40]!r} "
                "(必须从 query 原文或候选 fact 文本逐字复制, 禁止改写/缩略/脑补)")
        verdicts[fid] = {"keep": keep, "match_score": ms, "matched_anchor": anchor}
    missing = sorted(set(candidates) - set(verdicts))
    if missing:
        raise SchemaViolation(
            f"候选未逐条判定 (dangling 覆盖缺口): {missing[:5]}"
            f"{'…' if len(missing) > 5 else ''} — 必须对全部候选输出判定")
    return verdicts


def run_gate(
    candidates: dict[str, str],
    query: str,
    *,
    provider: Any = None,
    scope: str = "recall",
) -> dict[str, dict[str, Any]]:
    """对 B 翼候选跑 1× LLM 窄域 gate, 返回 keep 判定表。

    Args:
        candidates: ``{fact_id: 可读文本}`` — 待判 B 翼候选 (id → value/topic 文)。
        query: query 原文 (注入面 = turn 文, CLI = 手动 query)。
        provider: 任何有 ``chat(system, messages, max_tokens, tools, tool_choice)``
            的对象 (测试注入 mock); None ⇒ 内部构造 :class:`ZhipuAnthropicProvider`
            (timeout=GATE_TIMEOUT_SECONDS)。
        scope: 请求规范 scope 字段 ("recall" 注入面 / "manual" CLI 手动面)。

    Returns:
        ``{fact_id: {"keep", "match_score", "matched_anchor"}}`` (全部候选)。

    Raises:
        GateFailed: 断供短路 / provider 不可达 (ProviderCallError) / 两轮 schema
            校验败 — 响亮, 调用方承接 = B 翼全部不入返回。
    """
    if not candidates:
        return {}
    if provider is None:
        # 断供红线: 不传 provider/key 时直接短路为"无 gate" (等同不可用),
        # 不构造 provider 不发网络 — A 路零 LLM 依赖不破坏。key 检测照
        # llm_provider._load_zhipu_key 原样 (仅 env ZHIPU_API_KEY)。
        if not os.environ.get("ZHIPU_API_KEY", "").strip():
            raise GateFailed(
                "gate 断供短路: 无 provider 且 env 无 ZHIPU_API_KEY (等同不可用)")
        provider = ZhipuAnthropicProvider(timeout=GATE_TIMEOUT_SECONDS)

    request = build_request(query, scope)
    request["candidates"] = [
        {"id": fid, "text": (text or "")[:600]} for fid, text in candidates.items()
    ]
    messages: list[dict] = [{
        "role": "user",
        "content": json.dumps(request, ensure_ascii=False),
    }]
    retries = 0
    last_err: Exception | None = None
    for attempt in range(2):  # 首次 + 1 次带原因重试 (照 llm_extract 先例)
        if attempt == 1:
            messages = messages[:1] + [
                {"role": "assistant", "content": "(上一轮输出未通过校验)"},
                {"role": "user", "content": (
                    f"你上一轮的输出未通过 schema 校验, 原因: {last_err}。"
                    "请调用 report_gate_verdict 工具并严格按其参数 schema 重新报告, "
                    "不要自由文本。")},
            ]
        try:
            content = provider.chat(
                _SYSTEM_PROMPT, messages, max_tokens=1500,
                tools=[_GATE_TOOL_DEF], tool_choice={"type": "auto"},
            )
        except ProviderCallError as e:
            # 网络层失败重试无意义 (同一 provider 会再败; "LLM 不可达 = 只注入 A"
            # 是既定语义) — 响亮上抛, recall 承接为 B 翼全不入返回。
            raise GateFailed(f"gate provider 不可达: {e}") from e
        try:
            doc = _parse_json_block(content)
            return validate(doc, candidates, query)
        except SchemaViolation as e:
            last_err = e
            retries += 1
    raise GateFailed(f"gate schema 校验两轮失败: {last_err}")


__all__ = ["GateFailed", "GATE_TIMEOUT_SECONDS", "build_request",
           "derive_keywords", "run_gate", "validate"]

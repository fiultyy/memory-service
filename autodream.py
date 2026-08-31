"""mem-service autoDream — session transcript raw→KG incremental (ADR-10).

PreCompact hook entry: ``autodream(session_id, transcript_path, providers=None)``
reads a CC transcript JSONL **preserving block grammar** (M8: ``_read_transcript``
returns ``(block_type, text)`` pairs — tool_use/tool_result no longer skipped,
S1 fix), groups consecutive same-provenance blocks into segments (M8-v2 G2;
segment budget N4 replaces the old 4000-char flat truncation), and runs the
**M6/M7 占位通道** per segment: ``gazetteer.extract()`` (KG 自举词典 +
extractor.py regex 三路并行, deterministic 零 LLM inline — **行为反转, 反
ADR-5**: regex 复活为占位通道, provider 断供不再中断写入; wings LLM 退役为
异步升级, M4 队列消费侧复活). Each fact inherits its segment's provenance
(P21 出处轴 → M2 column; veracity auto-maps via M3) and lands as
``extractor='regex'`` (lif_source 0.4 档). Then ``consolidate.consolidate()``
(decay+dedup, v2/v3 复用), then an incremental decision per extracted fact:

- **ADD**    — new (subject, predicate, value) not in the active KG → put_fact.
- **UPDATE** — same (subject, predicate, value) already active → refresh LIF
  + absorb the session into source_refs/seen_sessions (recency/spread signal).
- **DELETE** — same (subject, predicate) but a *different* value ⇒ the new fact
  supersedes the old: the old flips to ``status='superseded'`` pointing at the
  new fact's id (the contradiction path; LIF/confidence may also shift).
- **NOOP**   — extracted fact already active with nothing to refresh (identical
  state, second autodream on the same transcript).

Idempotent by construction: re-running on the same transcript (no wall-clock
progress, no new extraction delta) yields ``{added:0, updated:0, deleted:0,
noop:N}`` — the acceptance contract.

v1.7④⑤ 本车道增量 (LIF 分账 + 冷启动 D6 双窗口 + 无LLM兜底 lane):

- **E3 分账 stamp**: 主径 llm 通道触发 UPDATE 时 session 追加进
  ``extract_sessions`` (seen_sessions 照旧); bootstrap 通道
  (session_id=``memory:<file>#<ci>``) 的 stamp 值统一虚拟会话 ``"self"``
  (两列), session_id 本体只进 source_refs 溯源 (勘误读法一)。
- **E5 解锁判据** ``len(extract_sessions)>=2`` 与 **D6 双窗口门**
  ``MEM_COLDSTART_UNLOCK`` (默认 0=暂缓期: 分账列只写不读, 刷新面跑旧 LIF
  规则) — 判据/门/毕业语义权威定义在 ``scoring.py`` 头部注释块。
- **D6 切换程序** (一次性运维, 不建代码): 设 ``MEM_COLDSTART_UNLOCK=1``
  (D6 投影回声指纹完成后) + 一次性 SQL 清零分账列重计
  (``UPDATE fact SET extract_sessions = '[]';`` 防暂缓期污染合法化)。
- **E6 C1a**: regex 通道 (含 fallback:auto 降级链) 复现同 (s,p,v) 不入
  extract_sessions — 单通道凑不满解锁。
- **E7 C1b 通道门槛**: 顶替者信任严格低于被处决者 (regex 档证据 vs llm 档
  fact) → NOOP + ``contradiction_pending`` 信号轻记录 + 矛盾段
  ``segcontra:`` 前缀复活入队 (待主径重抽仲裁); 反向与同档 supersede 照旧。
- **E10 fallback:auto**: llm 主径 + ExtractFailed 自动切 regex 兜底链
  (显式 opt-in 档; 默认 llm 档断供仍响亮上抛 — 断供红线不变)。
- **④ 低初值**: 主径 llm brand-new ADD 显式 ``lif_source=0.4`` (待验证,
  写侧立即生效); regex 产物本就 0.4 不动。

Returns ``{"added", "updated", "deleted", "noop"}``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import consolidate as consolidate_mod
import db
import gazetteer
import signals as signals_mod
import store
import resolver
import upgrade


def _read_transcript(transcript_path: str | Path,
                     harness: str = "cc") -> list[tuple[str, str]]:
    """Read a CC transcript JSONL as a ``(block_type, text)`` sequence (M8/S1).

    Block grammar preserved — tool_use/tool_result blocks are NO LONGER
    skipped (S1: 世界域最高权威观测 tool_obs 此前完全进不了提取管道).
    Block types yielded:

    - ``user_text`` / ``assistant_text`` — speaker prose (message.content 为
      裸字符串, 或 content list 里的 text 块)。
    - ``tool_use`` — 块 ``input``/``text`` 的可序列化文本 (G2: 意图非观测)。
    - ``tool_result`` — 块 ``content`` 文本: 字符串直取; list 时逐 item 取
      ``text``/``content`` 字段, 无文本的 item 跳过。
    - ``system`` — 其余带可读文本的块 (thinking 等, G2: 其余→system)。

    每块文本先过 ``corpus_prep.clean(harness)`` (2026-08-28, Codex 节点3
    输入过滤采纳): 系统注入/命令回显/压缩重注入块在进提取管道前剥除 —
    幂等, 与 transcripts 接缝叠加无害。

    Tolerates missing fields and malformed lines (hook transcript is
    async-written, may be partial — ADR-10 Consequences) by skipping the
    line/block. Missing file → ``[]``.
    """
    import corpus_prep

    p = Path(transcript_path)
    if not p.is_file():
        return []
    blocks: list[tuple[str, str]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") not in ("user", "assistant"):
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                if content:
                    blocks.append((f"{rec.get('type')}_text",
                                   corpus_prep.clean(content, harness)))
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    t = block.get("text")
                    if isinstance(t, str) and t:
                        blocks.append((f"{rec.get('type')}_text",
                                       corpus_prep.clean(t, harness)))
                elif btype == "tool_use":
                    t = _tool_use_text(block)
                    if t:
                        blocks.append(("tool_use",
                                       corpus_prep.clean(t, harness)))
                elif btype == "tool_result":
                    t = _tool_result_text(block)
                    if t:
                        blocks.append(("tool_result",
                                       corpus_prep.clean(t, harness)))
                else:
                    # 其余块 (thinking 等): 常见可读字段兜底, 无文本跳过。
                    t = block.get("text")
                    if not isinstance(t, str) or not t:
                        t = block.get("thinking")
                    if isinstance(t, str) and t:
                        blocks.append(("system",
                                       corpus_prep.clean(t, harness)))
    return blocks


def _tool_use_text(block: dict) -> str:
    """tool_use 块取 ``input``/``text`` 可序列化文本 (S1; input 优先, text 兜底)。"""
    if "input" in block:
        val = block["input"]
    else:
        val = block.get("text")
    if isinstance(val, str):
        return val
    if isinstance(val, (dict, list)) and val:
        try:
            return json.dumps(val, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return ""


def _tool_result_text(block: dict) -> str:
    """tool_result 块取 ``content`` 文本: 字符串直取; list 逐 item 取
    ``text``/``content`` 字段 (bare 字符串 item 容错收下), 无文本 item 跳过。"""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item:
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text")
                if not (isinstance(t, str) and t):
                    t = item.get("content")
                if isinstance(t, str) and t:
                    parts.append(t)
        return "\n".join(parts)
    return ""




# M8-v2 G2: 块类型 → provenance (P21 出处轴; 权重档已铺 store.PROVENANCE_VERACITY,
# DR-6 G1 裁决)。tool_use 归 agent_assert (意图非观测); tool_result 归 tool_obs
# (世界域最高权威观测, S1 修复后入管道); 其余 (thinking 等) → system。
_PROVENANCE_BY_BLOCK: dict[str, str] = {
    "user_text": "user_prose",
    "assistant_text": "agent_assert",
    "tool_use": "agent_assert",
    "tool_result": "tool_obs",
}

# N4 段预算(字符/段): 超长段截尾入段, 替换旧 4000 平截断(已废)。可调 —
# 提高 = 单段上下文更全 + LLM 成本更高; 降低 = 更省但截断更多。
_SEGMENT_BUDGET = 1200


def _block_provenance(block_type: str) -> str:
    """块类型 → provenance; 未识别块 → system (G2: 其余→system)。"""
    return _PROVENANCE_BY_BLOCK.get(block_type, "system")


def _build_segments(blocks: list[tuple[str, str]],
                    budget: int | None = None,
                    truncated: list[tuple[int, str]] | None = None) -> list[tuple[str, str]]:
    """连续同 provenance 块合并为段 (M8-v2 G2), 段超预算截尾 (N4)。

    Returns ``[(provenance, segment_text), ...]`` in encounter order. 段内块以
    ``\\n`` 连接; 每段文本截尾到 ``budget`` 字符 (缺省
    :data:`_SEGMENT_BUDGET`) — 段预算替换旧 4000 平截断, tool 块入管道后
    长观测不再被整体丢弃, 但单段 LLM 调用有界。

    ``truncated`` (M4 wire, 可选出参): 传入 list 时, 每个发生截尾的段 append
    ``(seg_index, full_text)`` — 调用方 (autodream) 据此把全文 ref 送入
    upgrade 队列 (M8→M4 wire 点)。
    """
    if budget is None:
        budget = _SEGMENT_BUDGET
    segments: list[tuple[str, str]] = []
    for block_type, text in blocks:
        if not text:
            continue
        prov = _block_provenance(block_type)
        if segments and segments[-1][0] == prov:
            merged = segments[-1][1] + "\n" + text
            seg_index = len(segments) - 1
            extend = True
        else:
            merged = text
            seg_index = len(segments)
            extend = False
        if len(merged) > budget and truncated is not None:
            truncated.append((seg_index, merged))  # N4 截尾 → M4 全文 ref
        if extend:
            segments[-1] = (prov, merged[:budget])
        else:
            segments.append((prov, merged[:budget]))
    return segments



def _is_bootstrap_session(session_id: str | None) -> bool:
    """v1.7④ E3: bootstrap 通道判定 — init_memory 以合成 transcript 喂
    autodream, session_id = ``memory:<file>#<ci>`` (bootstrap.py:78/:142)。"""
    return bool(session_id) and session_id.startswith("memory:")


def _find_active_fact(subject_id: str, predicate: str, value: str) -> dict[str, Any] | None:
    """Lookup a Fact by exact (subject_id, predicate, value).

    Scans active first (main path); falls back to superseded so a re-extracted
    value that was previously superseded is still recognised (UPDATE/NOOP) —
    prevents the supersede oscillation on rerun. Returns the decoded Fact or None.
    ponytail: linear scan, single-machine MVP ceiling.
    """
    for status in ("active", "superseded"):
        for f in store.get_facts_by_subject(subject_id, status=status):
            if f["predicate"] == predicate and (f.get("value") or "") == value:
                return f
    return None


# ponytail: ADR-1 R1 — 已知多值谓词集 short-circuit(不走 LLM), 省 token + 防 LLM
# 误判共存。单值/开放谓词走 provider.judge_contradiction 纯 LLM 裁判(Graphiti 式)。
# 升级路径: LLM 自由谓词时改读 fact schema cardinality 字段。
_MULTIVALUE_PREDICATES = frozenset({
    "uses", "depends_on", "contains", "implements",
    "connected_to", "part_of", "relates_to",
})


def _judge_contradiction(providers: list, subject_type: str, subject_name: str,
                         predicate: str, new_value: str, old_value: str) -> bool:
    """Whether ``new_value`` contradicts ``old_value`` for the same subject+
    predicate (ADR-1 R1).

    Two fast paths skip the LLM: (1) multivalue predicates always coexist → False;
    (2) identical values → False (same fact, not contradiction). Otherwise ask the
    first reachable provider's ``judge_contradiction``. Provider unreachable /
    raises / returns non-bool → fallback ``contradiction=False`` (do NOT supersede,
    do NOT block ingest — matches A1 fallback contract). NEVER raises.
    """
    if predicate in _MULTIVALUE_PREDICATES:
        return False
    if new_value == old_value:
        return False
    if not providers:
        return False
    try:
        verdict = providers[0].judge_contradiction(
            subject_type, subject_name, predicate, new_value, old_value)
    except Exception:
        return False
    return bool(verdict and verdict.get("contradiction") is True)


def _has_active_for_predicate(subject_id: str, predicate: str) -> list[dict[str, Any]]:
    """All active facts of this subject with this predicate (value-agnostic)."""
    return [
        f for f in store.get_facts_by_subject(subject_id, status="active")
        if f["predicate"] == predicate
    ]


# ── 实体卫生门 (batch 12 §2.4, LLM 通道落库前; regex 重开时同样受益) ──

# 停用词黑名单起步集 (T2 垃圾产出实测: 虚词/状态词被 regex 通道当实体)。
# 精确匹配 + 前缀拒 (「可能出现的」类衍生); 后续按报告迭代。
_ENTITY_STOPWORDS = frozenset({
    "可能", "的同时完成", "前一次", "确认者", "极简模式", "同进程",
    "明早", "超时", "删除", "输出", "完成", "继续", "本次", "上次", "恢复",
})

# 巨型实体护栏 (§2.4): 单实体 alias 上限, 超出拒新 alias (吸尘器实体防线)。
MAX_ENTITY_ALIASES = 32


def _entity_hygiene_gate(name: str) -> bool:
    """实体名卫生门: True=放行。CJK ≥2 字 / 拉丁 ≥3 字 / 停用词拒。"""
    n = (name or "").strip()
    if not n:
        return False
    if n in _ENTITY_STOPWORDS:
        return False
    has_cjk = any("\u4e00" <= c <= "\u9fff" for c in n)
    if has_cjk:
        # CJK 计 1/字, 拉丁混合按 CJK 规则 (语料主形)
        cjk_len = sum(1 for c in n if "\u4e00" <= c <= "\u9fff" or c.isalnum())
        if cjk_len < 2:
            return False
    elif len(n) < 3:
        return False
    return True

def autodream(session_id: str, transcript_path: str, providers: list | None = None, fact_type: str = "stable", source_cwd: str | None = None, harness: str = "cc") -> dict[str, int]:
    """Incrementally整理 a session transcript into the KG (ADR-10) — 公共入口。

    perf/vec-index: 批量写包单事务 (``db.transaction()`` — 消逐语句 commit
    fsync; autodream 是 PreCompact hook 单写者, 失败整段回滚, 幂等重跑可
    重入)。实际管道在 :func:`_autodream_inner`。``harness`` (2026-08-28):
    语料标记块清洗表键 (corpus_prep), 缺省 cc (PreCompact spool 即 CC)。
    """
    db.get_conn()  # ensure schema initialised on first call
    with db.transaction():
        return _autodream_inner(session_id, transcript_path, providers,
                                fact_type, source_cwd, harness)


def _autodream_inner(session_id: str, transcript_path: str, providers: list | None = None, fact_type: str = "stable", source_cwd: str | None = None, harness: str = "cc") -> dict[str, int]:
    """Incrementally整理 a session transcript into the KG (ADR-10).

    Pipeline (ADR-10 Decision (a)/(b)/(c)):

    1. ``consolidate.consolidate()`` — decay+dedup 复用 v2/v3 (phase a).
    2. ``_read_transcript`` (块文法, M8) + ``_build_segments`` (连续同 provenance
       合并成段, G2; 段预算截尾, N4) + 逐段 ``gazetteer.extract()`` M7 占位
       提取 (词典+regex 三路, 零 LLM inline — M6 反 ADR-5: provider 断供不再
       raise, wings 退役为异步升级 M4); fact 继承段 provenance (M2 列, veracity
       由 M3 映射自动生成; extractor='regex' → lif_source 0.4)。
    3. Incremental decision per extracted fact (phase c): ADD / UPDATE / DELETE
       (supersede) / NOOP, tally counts.

    Args:
        session_id: The CC session being dreamt (stamped into source_refs /
            seen_sessions for provenance + LIF spread).
        transcript_path: Path to the CC transcript JSONL.

    Returns:
        ``{"added": int, "updated": int, "deleted": int, "noop": int}``.
        Idempotent: a re-run on the same transcript yields all-NOOP (the
        acceptance cmd's second-call ``added == 0`` contract).
    """
    db.get_conn()  # ensure schema initialised on first call
    # Phase a — decay + dedup (v2/v3 复用). consolidate is idempotent on a
    # stable wall clock, so re-runs add no churn.
    consolidate_mod.consolidate()

    # Phase b — M8 块文法 + M6 占位通道: (block_type, text) 序列 → 连续同
    # provenance 块合并成段 (G2) + 段预算截尾 (N4); 逐段调 M7 gazetteer 占位
    # 提取器 (词典+regex 三路, 零 LLM inline), fact 直接继承段 provenance
    # (M2 通道; veracity 由 M3 映射自动生成, 不另传)。wings (adapter LLM)
    # 退役为异步升级 — 主径 provider 断供不再 RuntimeError (反 ADR-5)。
    # TODO(M4) 已落地: 段/事实标记待升级 — 两个 wire 点接 upgrade 队列(下)。
    blocks = _read_transcript(transcript_path, harness)
    truncated_segs: list[tuple[int, str]] = []
    segments = _build_segments(blocks, truncated=truncated_segs)
    # M8→M4 wire: 超长段截尾的全文 ref 入升级队列 (wings 异步升级; M9 入队时算 surprise)。
    # batch 13+ 队列退役清理 (用户裁决「全面清理」2026-08-27): llm 通道下
    # 升级队列语义失效 — 抽取已是 LLM 本体, 再喂 wings 是重复花钱; 队列
    # 仅 regex 通道 (休眠中) 保留。
    from llm_extract import extract_channel as _extract_channel
    from llm_extract import CHANNEL_REGEX as _CH_REGEX
    from llm_extract import CHANNEL_FALLBACK as _CH_FALLBACK
    _channel = _extract_channel()
    use_regex_channel = _channel == _CH_REGEX
    use_fallback_auto = _channel == _CH_FALLBACK
    _queue_on = use_regex_channel
    for seg_idx, full_text in (truncated_segs if _queue_on else []):
        prov_of_seg = segments[seg_idx][0] if seg_idx < len(segments) else None
        upgrade.enqueue_segment(transcript_path, seg_idx, full_text,
                                provenance=prov_of_seg)
    # M6: providers 仅供 contradiction judge (显式传入才生效); 主径提取零 LLM,
    # 不再 default_providers() 自取。
    active_providers = list(providers) if providers else []
    return _decide_segments(
        [(prov, text) for prov, text in segments],
        session_id=session_id,
        providers=active_providers,
        fact_type=fact_type,
        source_cwd=source_cwd,
        transcript_path=transcript_path,
        use_regex_channel=use_regex_channel,
        use_fallback_auto=use_fallback_auto,
        allow_enqueue=True,
    )


def rerun_segment(text: str, *, provenance: str | None = None,
                  providers: list | None = None, fact_type: str = "stable",
                  source_cwd: str | None = None) -> dict[str, int]:
    """v1.7⑤ E11/N1: 升级队列 segment:/segcontra: 素材 → **autodream 决策
    管道重跑** (dream 消费端分流入口)。

    矛盾 judge + C1b 通道门槛在场 — 非 dream._apply_upgrade 的 ADD-only
    直写 (会把待裁决固化成共存双事实, 勘误 N1)。主径恢复 (llm 档) 时 sweep
    补抽经此转正 (产物 extractor=llm); regex/fallback 通道经此走词典链 +
    通道门槛。素材已在队列 → 不重入队 (allow_enqueue=False, 重入队自身
    material_ref 无意义)。单进程假设成立, 失败由消费端 revert → pending
    (attempts 不烧)。"""
    from llm_extract import CHANNEL_REGEX as _CH_REGEX
    from llm_extract import CHANNEL_FALLBACK as _CH_FALLBACK
    from llm_extract import extract_channel as _extract_channel
    _channel = _extract_channel()
    return _decide_segments(
        [(provenance or "system", text)],
        session_id=None,
        providers=list(providers) if providers else [],
        fact_type=fact_type,
        source_cwd=source_cwd,
        transcript_path=None,
        use_regex_channel=(_channel == _CH_REGEX),
        use_fallback_auto=(_channel == _CH_FALLBACK),
        allow_enqueue=False,
    )


def _decide_segments(
    seg_list: list[tuple[str | None, str]],
    *,
    session_id: str | None,
    providers: list,
    fact_type: str,
    source_cwd: str | None,
    transcript_path: str | None,
    use_regex_channel: bool,
    use_fallback_auto: bool,
    allow_enqueue: bool,
) -> dict[str, int]:
    """逐段提取 (通道分派) + Phase c 增量决策 (ADD/UPDATE/supersede/NOOP)。

    ``seg_list`` = [(provenance, seg_text)]。通道分派: regex 档直走 gazetteer
    占位链; llm/fallback:auto 走 LLM 直抽 — fallback:auto 档
    :class:`ExtractFailed` 自动切 regex 兜底链 + 降级标记 (E10, 默认 llm 档
    仍响亮上抛); 降级段零产出时 C 层语义兜底与 A 层入队照 regex 档在场
    (⑤ 链 ①②③ 档; embedding 同挂时 C 层内部静默跳过 = 仅①③档)。
    ``allow_enqueue=False`` (队列重跑入口) 时 A 层不重入队。
    """
    import llm_extract as llm_extract_mod
    active_providers = list(providers) if providers else []
    # 分段提取 + 三级空产出时序 (追加 A/C): 段提取零产出 →
    #   C 层 (零 LLM 兜底): CJK span 批量 embed → vec_entity ANN ≥0.45 →
    #     链接既有实体 (**只产实体声明不造谓词边** — span 无句式证据造边=
    #     臆测, 谓词留 wings; 落库走 resolver step1 精确命中路径);
    #   仍无 edges → A 层: enqueue_segment 全文入队 (wings 异步; C 不吞 A —
    #     实体链接了语义内容还没提)。
    # 幂等: 同 material_ref 拒重; M9 novelty (embedding 语言中立) 定优先级,
    # wings 判「无事实」→ 合法 done, attempts≥3 封顶防重复浪费。
    seg_results: list[tuple[str, Any, str]] = []  # (prov, result, seg_text D-B b)
    seg_to_enqueue: list[tuple[int, str, Any]] = []
    seg_degraded: set[int] = set()  # fallback:auto 下已降级 regex 兜底链的段
    for seg_idx, (seg_prov, seg_text) in enumerate(seg_list):
        if use_regex_channel:
            result = gazetteer.extract(seg_text)
        else:
            try:
                result = llm_extract_mod.extract(seg_text)  # 失败 ExtractFailed 上抛 (无降级红线)
            except llm_extract_mod.ExtractFailed:
                if not use_fallback_auto:
                    raise  # 默认 llm 档: 响亮上抛不静默降级 (断供红线不变)
                # v1.7⑤ E10: 显式 opt-in 降级档 — 自动切 ① 词典+regex 三路
                # 兜底链, 产物 extractor=regex (lif_source 0.4, 编排者裁决:
                # 不加 SOURCE_WEIGHT 新键) = fallback 来源降级标记。
                seg_degraded.add(seg_idx)
                result = gazetteer.extract(seg_text)
        regex_lane = use_regex_channel or (seg_idx in seg_degraded)
        if regex_lane and not result.entities and not result.edges:
            # C 层: 语义兜底实体声明 (与 B 共用 _link_spans 管道)。防御性
            # 兜底 — B 路在 extract 内对可语义命中的段恒先命中 (FINDING
            # c9: 对可命中段本分支不可达; 保留作 B 未覆盖形态的保险)。
            # embedding 也挂 → 内部静默跳过 (⑤ 降级三档: 仅①③档)。
            c_ents = gazetteer.semantic_fallback_hits(seg_text)
            if c_ents:
                result.entities = c_ents  # 实体声明接管; edges 保持空
        if not result.edges and allow_enqueue and (
                use_regex_channel or (use_fallback_auto and seg_idx in seg_degraded)):
            # A 层: 全文入队 — **与实体来源无关** (两档通道命中实体但无数
            # 谓词边的段同样语义内容未提, 皆入 A; FINDING c9 根因修复)。
            # 幂等由 enqueue 的 material_ref 拒重保证 (c10)。
            # llm 通道: LLM 已看过全文没抽出边, 再喂 wings 是重复花钱 →
            # 不入队 (队列退役清理 2026-08-27); fallback:auto 仅降级段入队
            # (主径没看过这段 — 主径恢复后 sweep 补抽转正, ⑤链③档)。
            seg_to_enqueue.append((seg_idx, seg_text, seg_prov))
        # D-B b: seg_text 随产出下传 — Phase c 边处理时喂 dedupe 裁判
        # (名字族相关性 ≠ 同一性, "A 基于 B" 关系句是非同一性铁证)。
        seg_results.append((seg_prov, result, seg_text))

    # perf/vec-index: A 入队延后批化 — novelty 采样向量 (surprise.novelty_sample
    # 截断) 一次 embed_batch 预热缓存, 再逐段 enqueue (novelty embed 全走
    # L1, 消逐段长文串行 HTTP; 段序/material_ref 不变)。
    if seg_to_enqueue:
        import embedding as embedding_mod
        import surprise as surprise_mod
        try:
            embedding_mod.embed_batch(
                [surprise_mod.novelty_sample(t) for _, t, _ in seg_to_enqueue])
        except Exception as exc:
            # v1.7⑤ E12 N6: "embedding 也挂"档 embed raise → try/except 降级
            # (novelty 退化为空向量入队), 不得炸 :319 单事务/消费循环。
            print(f"AUTODREAM-WARN: embed_batch 预热失败 (novelty 降级): "
                  f"{type(exc).__name__}: {exc}", flush=True)
        for seg_idx, seg_text, seg_prov in seg_to_enqueue:
            upgrade.enqueue_segment(transcript_path or "", seg_idx, seg_text,
                                    provenance=seg_prov)
    _raw_preds = [e.predicate for _p, _r, _t in seg_results for e in _r.edges]
    _canon_map: dict[str, str] = {}
    if _raw_preds:
        import predgate as predgate_mod
        try:
            _canon_map = predgate_mod.cluster(_raw_preds)
        except Exception:
            # 聚边失败不阻断 ingest (词频统计是增强面, 非正确性面) —
            # 降级为恒等映射 (raw 即 canonical), 响亮 log。
            print(f"AUTODREAM-WARN: predgate.cluster 失败, 谓词未聚边: "
                  f"{_raw_preds[:3]}…", flush=True)
            _canon_map = {}

    # Initial 5-dim LIF at ingest (ADR-8v2): distinct_sessions=1 when session_id
    # present (fact's seen_sessions starts with it). coherence=1.0 (no siblings
    # queried; consolidate recomputes authoritatively).
    import scoring as scoring_mod
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0)

    src_ref = f"session:{session_id}" if session_id else None
    # v1.7④ E3: bootstrap 通道 (session_id="memory:<file>#<ci>" 合成段) 的
    # 两列 stamp 值统一虚拟会话 "self" — 同一 init_memory 全批同源, 解锁判据
    # 天然 len=1 封顶不可凑; session_id 本体只进 source_refs 溯源不断链
    # (勘误读法一)。非 bootstrap: stamp 值 = session_id 本身。
    stamp_session = "self" if _is_bootstrap_session(session_id) else session_id
    added = updated = deleted = noop = 0

    # Phase c — incremental decision per edge, per segment (M8: fact 继承段 provenance)。
    # R1 档 1: entities first (so declared types land), then edges. subject AND
    # object both resolve to entities → put_fact(object_id=...) 必非空.
    # ponytail: rebuild a name→entity_id/type cache per call, shared across
    # segments (autodream is the single writer in a PreCompact hook; no
    # cross-call cache needed).
    name_to_id: dict[str, str] = {}
    name_to_type: dict[str, str] = {}
    fact_enqueues: list[tuple[str, str, str, str, str | None]] = []
    # perf 收尾批: 新 fact value 预热批 — put_fact 内嵌 embed(value) 逐条
    # 串行 HTTP (~120 次/全量 init); 边集 upfront 已知, 一次批预热后全走 L1。
    _edge_values: list[str] = []
    for _seg_prov, _result, _st in seg_results:
        for _edge in _result.edges:
            _v = (_edge.object or "").strip()
            if _v and _v not in _edge_values:
                _edge_values.append(_v)
    if _edge_values:
        import embedding as _embedding_mod
        try:
            _embedding_mod.embed_batch(_edge_values)
        except Exception as exc:
            # v1.7⑤ E12 N6: fact value 预热 embed raise → 降级 (写侧 vec 条件
            # 跳过), 不得炸 :319 单事务。
            print(f"AUTODREAM-WARN: embed_batch 预热失败 (fact value 降级): "
                  f"{type(exc).__name__}: {exc}", flush=True)
    for seg_idx, (seg_provenance, result, seg_text) in enumerate(seg_results):
        ext_label = result.source_meta.get("extractor_label", "llm")
        # v1.7④ 主径判定: 产物 extractor=='llm' ⇔ 本边由主径 llm 通道触发
        # (regex 通道/fallback:auto 降级链产物均 extractor='regex' — C1a
        # 不计数与低初值判定共用此键)。
        main_path = ext_label == "llm"
        lif_dims = scoring_mod.compute_lif(
            {"extractor": ext_label, "fact_type": fact_type, "created_at": now.isoformat()},
            access_count=0,
            last_accessed_at=now.isoformat(),
            distinct_sessions=1 if session_id else 0,
            neighbors=[],
            now=now,
        )

        def _put_new_fact(**edge_kw):
            """Persist a new fact with confidence + initial LIF dims (used by both
            contradiction-supersede and brand-new ADD paths). M8: stamps the
            segment's provenance (M2 column); veracity auto-maps via M3.
            v1.7④: lif_source 可被调用方显式覆盖 (低初值裁决, ADD 路径);
            缺省仍为 extractor 档位重算值。"""
            edge_kw.setdefault("lif_source", lif_dims["lif_source"])
            return store.put_fact(
                **edge_kw,
                confidence=result.confidence,
                provenance=seg_provenance,
                LIF=lif_dims["LIF"],
                lif_freq=lif_dims["lif_freq"], lif_recency=lif_dims["lif_recency"],
                lif_spread=lif_dims["lif_spread"], lif_coherence=lif_dims["lif_coherence"],
            )

        # perf/vec-index: 段级实体批式消解 — 一次 embed 批 (embed_batch 单次
        # POST 预热 L1) + 逐名三步协议 (aliases 语义经 aliases_map 全保留);
        # names 跨段共享 name_to_id 缓存。
        seg_entities = [ent for ent in result.entities if ent.name]
        seg_resolved = resolver.resolve_entities_batch(
            [e.name for e in seg_entities],
            entity_types=[e.type for e in seg_entities],
            aliases_map={e.name: list(e.aliases) for e in seg_entities
                         if getattr(e, "aliases", None)},
            providers=active_providers,
            context=seg_text) if seg_entities else {}  # D-B b: 段原文喂裁判
        for ent in result.entities:
            if not ent.name:
                continue
            if not _entity_hygiene_gate(ent.name):
                continue  # 卫生门拒 (停用词/短名) — 不 resolve 不落库
            sid = seg_resolved.get(ent.name)
            if sid is None and ent.name not in seg_resolved:
                # 批式未覆盖 (异常防御) → 单条兜底, 协议不变。
                sid = resolver.resolve_entity(
                    ent.name, ent.type,
                    aliases=getattr(ent, 'aliases', None) or None,
                    providers=active_providers)
            if sid is not None:
                name_to_id[ent.name] = sid
                name_to_type[ent.name] = ent.type
        for edge in result.edges:
            subject = (edge.subject or "").strip()
            predicate = (edge.predicate or "").strip()
            raw_predicate = predicate
            predicate = _canon_map.get(predicate, predicate)
            value = (edge.object or "").strip()
            if not subject or not predicate or not value:
                continue
            # 卫生门 + 自环禁止 (§2.4): 两档通道统一防线 (regex 通道 T2 实测
            # 自环 6 条; schema 层 LLM 档已弃, 此处兜两档)。
            if subject == value:
                continue
            topic = (edge.topic or "").strip() or None  # ADR-C: 投影 slug/title/desc 源

            if subject not in name_to_id:
                if not _entity_hygiene_gate(subject):
                    continue
                sid = resolver.resolve_entity(subject, name_to_type.get(subject, "concept"),
                                              providers=active_providers, context=seg_text)
                if sid is None:
                    continue
                name_to_id[subject] = sid
            subject_id = name_to_id[subject]

            # object is a declared entity reference (R1 §A2) — resolve + link.
            if value not in name_to_id:
                if not _entity_hygiene_gate(value):
                    continue
                oid = resolver.resolve_entity(value, name_to_type.get(value, "concept"),
                                              providers=active_providers, context=seg_text)
                if oid is None:
                    continue
                name_to_id[value] = oid
            object_id = name_to_id[value]

            # D-B c 图不变量防线 (P4 D-A 升级): 表面串检查 (subject == value)
            # 挡不住 resolver 合并 — 两个不同表面名解析到同一实体 id 时, 不再
            # 丢边 (D-A 旧语义连事实一起扔), 而是**否决合并**: object 名带
            # exclude_ids={subject_id} 重解析 (resolver step1 命中被拒 → step2
            # 候选滤掉 → 都排光则新建), 宁分离勿自环。真同串 (value == subject
            # 表面) 才丢弃 — A --pred--> A 语义无效。
            if object_id == subject_id:
                if value == subject:
                    continue
                split_id = resolver.resolve_entity(
                    value, name_to_type.get(value, "concept"),
                    providers=active_providers, context=seg_text,
                    exclude_ids={subject_id})
                if not split_id or split_id == subject_id:
                    continue  # 分离失败兜底 (理论不可达, exclude 语义保证)
                object_id = name_to_id[value] = split_id

            # Exact (subject, predicate, value) match ⇒ UPDATE / NOOP.
            exact = _find_active_fact(subject_id, predicate, value)
            if exact is not None:
                # Refresh LIF + absorb session (the reinforcement signal). If the
                # fact already saw this session and nothing else moved, the refresh
                # is a no-op on stored state ⇒ count as NOOP (idempotency).
                seen_sessions = list(exact.get("seen_sessions") or [])
                ext_sessions = list(exact.get("extract_sessions") or [])
                source_refs = list(exact.get("source_refs") or [])
                already_seen = (stamp_session in seen_sessions) \
                    if stamp_session else True
                already_ref = (src_ref in source_refs) if src_ref else True
                # v1.7④ E3/E6 (C1a): 仅主径 llm 触发的 UPDATE 把 session stamp
                # 进 extract_sessions 分账列 (JSON append; bootstrap 记 "self")
                # — regex 通道/fallback 降级链复现同 (s,p,v) 不计数, 单通道
                # 凑不满解锁; seen_sessions 照旧三口不动。
                new_ext = bool(main_path and stamp_session
                               and stamp_session not in ext_sessions)
                if already_seen and already_ref and not new_ext:
                    noop += 1
                    continue
                if stamp_session and stamp_session not in seen_sessions:
                    seen_sessions.append(stamp_session)
                if src_ref and src_ref not in source_refs:
                    source_refs.append(src_ref)
                if new_ext:
                    ext_sessions.append(stamp_session)
                # v1.7④ E5 毕业门: 解锁期 (env 开) 才消费判据 — 达
                # len(extract_sessions)>=2 放行 lif_source 毕业到 extractor
                # 真值档; 未达 → 锁现值 (regex 触发面/低初值不被非解锁性
                # 刷新提权); 暂缓期 (门关, None) 走旧规则照 dims 重算。
                graduated = None
                if scoring_mod.coldstart_unlock_enabled():
                    graduated = (len(ext_sessions)
                                 >= scoring_mod.UNLOCK_EXTRACT_SESSIONS)
                _refresh_fact_meta(exact["id"], seen_sessions, source_refs,
                                   extract_sessions=ext_sessions,
                                   graduated=graduated)
                updated += 1
                continue

            # Same (subject, predicate), different value: supersede ONLY on a real
            # contradiction (ADR-1 R1). Multivalue predicates (uses/depends_on/...)
            # short-circuit to no-contradiction (coexist); single-valued/open
            # predicates ask the judge — M6 占位径 providers 默认 [] → 规则
            # fallback (值比较共存, 不 supersede 不阻断); 显式传 providers 时才
            # 问 LLM judge。一致性: contradiction ⇒ supersede 设 valid_to。
            subject_type = name_to_type.get(subject, "concept")
            siblings = _has_active_for_predicate(subject_id, predicate)
            contradicting = [s for s in siblings
                             if _judge_contradiction(
                                 active_providers, subject_type, subject, predicate,
                                 value, s.get("value") or "")]
            if contradicting:
                # v1.7⑤ E7 C1b 通道质量门槛 (勘误 C1 出口修复): 顶替者信任
                # **严格低于**被处决者 (regex 档证据 vs llm 档 fact) → NOOP:
                # 不 supersede 不改状态 + contradiction_pending 信号轻记录
                # (七字段, 勘误 N5) + 矛盾段 segcontra: 前缀复活入队 (待主径
                # 重抽仲裁; 不受 _queue_on 门控 — llm 通道开洞)。反向
                # (高 vs 低) 与同档 → 下方 supersede 照旧; multivalue 短路
                # 在 judge 前已返回, 通道门槛不覆盖 multivalue。
                chal_tier = scoring_mod.SOURCE_WEIGHT.get(ext_label, 0.4)
                if any(chal_tier < scoring_mod.SOURCE_WEIGHT.get(
                        o.get("extractor") or "regex", 0.4)
                       for o in contradicting):
                    contra_ref = (f"segcontra:{transcript_path}#seg{seg_idx}"
                                  if transcript_path
                                  else f"segcontra:rerun#{seg_idx}")
                    signals_mod.append("contradiction_pending", {
                        "ref": contra_ref,
                        "subject_id": subject_id,
                        "predicate": predicate,
                        "old_value": contradicting[0].get("value") or "",
                        "new_value": value,
                        "channel": ext_label,
                    })
                    if transcript_path:
                        upgrade.enqueue_contra_segment(
                            transcript_path, seg_idx, seg_text,
                            provenance=seg_provenance)
                    noop += 1
                    continue
                new_id = _put_new_fact(
                    subject_id=subject_id,
                    predicate=predicate,
                    value=value,
                    object_id=object_id,
                    extractor=ext_label,
                    fact_type=fact_type,
                    source_cwd=source_cwd,
                    source_refs=[src_ref] if src_ref else [],
                    seen_sessions=[stamp_session] if stamp_session else [],
                    topic=topic,
                    raw_predicate=raw_predicate,
                    task_outcome=getattr(edge, "task_outcome", None),
                )
                for old in contradicting:
                    store.update_fact_status(old["id"], "superseded", supersedes_id=new_id, valid_to=store._now(), reason="contradiction")  # M1: contradiction 必带 reason
                # M6→M4 wire: 占位 fact 落库后待升级项入队 (延后批化, 见循环尾)。
                # llm 通道不入队 (队列退役清理 2026-08-27, 同 ADD 路径)。
                if use_regex_channel:
                    fact_enqueues.append((new_id, subject, predicate, value, seg_provenance))
                deleted += len(contradicting)
                added += 1
                continue
            # 多值共存 / 无矛盾 ⇒ 落到下方 brand-new ADD (不 continue)。

            # Brand new — ADD.
            # v1.7④ 低初值 (编排者裁决, 写侧立即生效): 主径 llm 触发的
            # brand-new ADD 显式低初值 lif_source=0.4 (待验证, 与 regex 同档;
            # regex 产物本就 0.4 不动); "待验证"不新增 schema 值, 由
            # (extractor, lif_source, len(extract_sessions)) 推导。主径 ADD
            # 即首个独立提取证据 → extract_sessions 初始 stamp (bootstrap 记
            # "self", len 天然封顶 1)。
            init_stamp: list[str] = [stamp_session] if stamp_session else []
            new_id = _put_new_fact(
                subject_id=subject_id,
                predicate=predicate,
                value=value,
                object_id=object_id,
                extractor=ext_label,
                fact_type=fact_type,
                source_cwd=source_cwd,
                source_refs=[src_ref] if src_ref else [],
                seen_sessions=init_stamp,
                lif_source=(scoring_mod.LOW_INIT_LIF_SOURCE
                            if main_path else lif_dims["lif_source"]),
                extract_sessions=(init_stamp if main_path else []),
                topic=topic,
                raw_predicate=raw_predicate,
                task_outcome=getattr(edge, "task_outcome", None),
            )
            # M6→M4 wire: 占位 fact 落库后待升级项入队 (延后批化, 见循环尾)。
            # llm 通道 (use_regex_channel=False) 不收集: extractor='llm' 的
            # fact 已是终态, wings 升级=重复消费 (队列退役清理 2026-08-27)。
            if use_regex_channel:
                fact_enqueues.append((new_id, subject, predicate, value, seg_provenance))
            added += 1

    # perf 收尾批: fact 入队批化 — 三元组文本 novelty 采样一次 embed_batch
    # 预热后逐条 enqueue (同 seg 批化; material_ref=fact:<id> 幂等不变)。
    if fact_enqueues:
        import embedding as embedding_mod
        import surprise as surprise_mod
        try:
            embedding_mod.embed_batch(
                [surprise_mod.novelty_sample(f"{s} {p} {o}".strip())
                 for _, s, p, o, _ in fact_enqueues])
        except Exception as exc:
            # v1.7⑤ E12 N6: "embedding 也挂"档 embed raise → 降级不炸消费
            # 循环/:319 单事务 (novelty 退化空向量入队)。
            print(f"AUTODREAM-WARN: embed_batch 预热失败 (fact 入队降级): "
                  f"{type(exc).__name__}: {exc}", flush=True)
        for fid, s_, p_, o_, prov in fact_enqueues:
            upgrade.enqueue_fact(fid, subject=s_, predicate=p_, obj=o_,
                                 provenance=prov)

    return {"added": added, "updated": updated, "deleted": deleted, "noop": noop}


def _refresh_fact_meta(fact_id: str, seen_sessions: list[str], source_refs: list[str],
                       *, extract_sessions: list[str] | None = None,
                       graduated: bool | None = None) -> None:
    """Write back absorbed seen_sessions + source_refs and recompute LIF.

    ADR-8v2: spread derives from distinct sessions, so absorbing a new session
    must lift lif_spread — recompute via the consolidate decay pass's
    ``compute_lif`` so the dim stays authoritative. We touch access_count/
    last_accessed_at too (a session re-seeing a fact is mild reinforcement).

    v1.7④ E3/E5: ``extract_sessions`` 传新列表时随本 UPDATE 一并落列 (JSON
    覆写, 分账 stamp; None = 保持现值)。``graduated`` 三态 (D6 双窗口):
    ``None`` = 暂缓期旧规则 (lif_source 照 compute_lif 由 extractor 重算);
    ``True`` = 解锁毕业 (lif_source 落 extractor 真值档); ``False`` = 解锁期
    未毕业 (lif_source 锁现值 — 低初值 0.4 不被非解锁性刷新提权, regex 触发
    面同理不得给 llm 档提源)。
    """
    from datetime import datetime, timezone

    import scoring

    conn = db.get_conn()
    row = conn.execute("SELECT * FROM fact WHERE id = ?", (fact_id,)).fetchone()
    if row is None:
        return
    fact = store._decode_fact(row)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_iso = now.isoformat()
    access_count = int(fact.get("access_count") or 0) + 1

    # coherence: subject siblings incl. self (mirrors refresh_lif_on_recall).
    own_pred = fact.get("predicate")
    sib_rows = conn.execute(
        "SELECT predicate FROM fact WHERE subject_id = ? AND id != ? AND status = 'active'",
        (fact["subject_id"], fact_id),
    ).fetchall()
    neighbors = (
        ([{"predicate": own_pred}] if own_pred else [])
        + [{"predicate": r["predicate"]} for r in sib_rows]
    )

    dims = scoring.compute_lif(
        fact,
        access_count=access_count,
        last_accessed_at=now_iso,
        distinct_sessions=len(seen_sessions),
        neighbors=neighbors,
        now=now,
    )
    lif_source_val = (float(fact.get("lif_source") or 0.0)
                      if graduated is False else dims["lif_source"])
    ext_out = extract_sessions if extract_sessions is not None \
        else list(fact.get("extract_sessions") or [])
    conn.execute(
        """UPDATE fact SET
               LIF = ?, lif_freq = ?, lif_recency = ?, lif_spread = ?,
               lif_coherence = ?, lif_source = ?,
               access_count = ?, last_accessed_at = ?,
               seen_sessions = ?, source_refs = ?, extract_sessions = ?
           WHERE id = ?""",
        (
            dims["LIF"], dims["lif_freq"], dims["lif_recency"], dims["lif_spread"],
            dims["lif_coherence"], lif_source_val,
            access_count, now_iso,
            json.dumps(seen_sessions, ensure_ascii=False),
            json.dumps(source_refs, ensure_ascii=False),
            json.dumps(ext_out, ensure_ascii=False),
            fact_id,
        ),
    )


__all__ = ["autodream", "rerun_segment"]

"""corpus_prep — 喂提取器的语料预处理: harness 标记块映射表 + 密钥脱敏。

(2026-08-28, Codex memories pipeline 对照采纳 #1+#4; 用户裁决: 「喂给提取器
的语料处理可以抄,但是做一份映射表,根据不同的HARNESS(cc,codex)的特殊标记块
做处理」·「这点我觉得价值最大」·「1234都做」)

问题: harness transcript 文本里混着系统注入块 — cc 的 system-reminder/
local-command-stdout, codex 的 environment_context / AGENTS.md <INSTRUCTIONS>
投影, dsh 的 skill 目录 / compacted-summary 重注入, pi 的 lark 桥信封。
不清洗就把注入指令当事实抽进 KG (Codex 节点3 的输入过滤层, memsvc 一直缺)。

经验依据 (三路真实语料扫描, 2026-08-28, 全量+分层抽样, 只读):

- cc (~221 文件, 317 种尖括号命中): ~90% 是 tool_result 代码回显的泛型
  (Self/String/f32/usize…) — **必须白名单制**, 泛黑名单会误杀代码语料。
  真注入标记是「小写+连字符+成对包裹」风格, 集中在 user 字符串 content 与
  tool_result 内嵌 text 两处。
- codex (326 rollout 全量): 65% 的 user 消息是系统注入伪装 (54/83 抽样);
  <permissions instructions> 带属性且在 role=developer 消息; <user_instructions>
  是 turn_context 的 JSON 字段名, 不是文本标签 (文本态是 <INSTRUCTIONS> 投影)。
- dsh (288 session): 结构化判别器 ``data.source.kind`` (user=真人 1067 /
  skill-catalog 330 / plugin:compact 38 / agent-instructions 135…) 优先于
  标签正则 — 结构过滤接在 transcripts 场景适配层, 本表只作文本层兜底
  (<compacted-summary> 有 1 条出现在真人正文里, 纯标签过滤会误删)。
- pi (59 session): 无注入判别字段, 只能拆桥信封标签 — bridge_context/
  bridge_instructions/quoted_messages 剥掉, user_input 拆包保留内文
  (62% 的 user 文本被信封包着, 是 pi 语料最大污染源)。
- 通用规则 (五 harness 合并, 2026-08-28 出端闭环): memsvc 自有召回标记
  ``<memsvc-recall>`` — recall_inject 注入面与 recall --project 正文在
  **出端**打标 (LLM 可读的「这是召回内容」声明; 非 harness 保留语法,
  三方解析器原样透传 → 零适配器), 语料**重进**时整块丢弃, 防召回回声
  自我重入库 (U7 去重只是分数层兜底, 本条是结构层根治)。

密钥脱敏 (采纳 #4, 8 类): 只用「知名前缀 + 带上下文赋值」规则。**裸长 hex
规则被证据否决** — cc 128 + codex 25 + pi 409 处合法长 hex (git SHA /
sha256: 镜像摘要 / web-search 内容 id), 0 密钥; 误杀远大于收益。三库实测
密钥形态: sk- 16 处 / Bearer 5 处, 全在八类覆盖内。

接缝 (三道, 幂等可叠加):
- ``transcripts.end_steps`` / ``scenes`` — 蒸馏文本出适配器即清洗 (harness 已知)
- ``autodream._read_transcript`` — 块文法入口逐块清洗 (PreCompact spool 全量路径)
- ``llm_extract.extract`` — redact_secrets 终防线 (任何 LLM 调用前; 密钥
  不进 prompt 不进 evidence 不进 KG)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── 标记块规则表 ─────────────────────────────────────────────────────

# 动作语义:
#   drop   — 整块删除 (含标签): 系统注入/命令回显/压缩重注入, 对提取纯噪音
#   unwrap — 剥标签保内容: 块内是真实信号 (用户敲的命令/子代理结论/任务输出)

ACTION_DROP = "drop"
ACTION_UNWRAP = "unwrap"


@dataclass(frozen=True)
class BlockRule:
    """一条标记块规则: 成对包裹块 ``<tag …>…</tag>`` → drop 或 unwrap。

    ``pattern`` 非空时直接作原样正则 (不套标签模板) — 标签外的行级注入
    (codex 的 ``# AGENTS.md instructions`` 标题行) 用这个口子。"""
    name: str      # 规则名 (stats 键 / 文档锚)
    tag: str       # 标签名 (允许属性形态 <tag attr=…>); pattern 非空时忽略
    action: str    # ACTION_DROP | ACTION_UNWRAP
    note: str = ""  # 证据出处 (扫描报告要点)
    pattern: str = ""  # 原样正则覆盖 (优先于 tag 模板); unwrap 时需含捕获组


def _r(name: str, tag: str, action: str, note: str = "") -> BlockRule:
    return BlockRule(name=name, tag=tag, action=action, note=note)


def _raw(name: str, pattern: str, action: str, note: str = "") -> BlockRule:
    return BlockRule(name=name, tag="", action=action, note=note,
                     pattern=pattern)


# cc (Claude Code) — 白名单制 (扫描: 90% 尖括号是代码回显泛型, 不可黑名单)。
# 泛名 <status> 刻意不收 (误杀风险 > 噪音收益, workflow 通知里剥不剥无差)。
CC_RULES = [
    _r("system-reminder", "system-reminder", ACTION_DROP,
      "运行时注入 (上下文快照/hook 提示), 全量 5 次/抽样"),
    _r("local-command-stdout", "local-command-stdout", ACTION_DROP,
      "斜杠命令回显"),
    _r("local-command-caveat", "local-command-caveat", ACTION_DROP,
      "命令 caveat 注入"),
    _r("persisted-output", "persisted-output", ACTION_DROP, "持久化输出回显"),
    _r("command-message", "command-message", ACTION_DROP, "命令描述元数据"),
    _r("task-id", "task_id", ACTION_DROP, "workflow 通知元数据 (id 无语义)"),
    _r("task-type", "task_type", ACTION_DROP, "workflow 通知元数据"),
    _r("command-name", "command-name", ACTION_UNWRAP, "保留用户敲的命令本身"),
    _r("command-args", "command-args", ACTION_UNWRAP, "保留命令参数"),
    _r("task-notification", "task-notification", ACTION_UNWRAP,
      "剥壳保内层 <result> (子代理真实结论)"),
    _r("tool-use-error", "tool_use_error", ACTION_UNWRAP,
      "错误回显保留 (任务失败的证据面, task_outcome=fail 依据)"),
    _r("wf-output", "output", ACTION_UNWRAP, "workflow 通知内层真实输出"),
    _r("wf-result", "result", ACTION_UNWRAP, "task-notification 内层结论"),
]

# codex (OpenAI Codex CLI) — future adapter 备用; 表即刻生效于文本层清洗。
# 子标签 (<cwd>/<shell>/<EventKey>/<host>…) 都在父块内, 父 drop 即覆盖。
CODEX_RULES = [
    _r("environment_context", "environment_context", ACTION_DROP,
      "环境上下文伪装 user 消息 (抽样 30/83)"),
    _r("AGENTS-INSTRUCTIONS", "INSTRUCTIONS", ACTION_DROP,
      "AGENTS.md/技能投影 (抽样 24 次)"),
    _raw("AGENTS-header", r"(?m)^# AGENTS\.md instructions[^\n]*\n?", ACTION_DROP,
         "投影标题行在 <INSTRUCTIONS> 标签外 (扫描样例实证)"),
    _r("permissions", "permissions", ACTION_DROP,
      "权限注入 (全量 315 次, role=developer 消息, 标签带属性)"),
    _r("approval_policy", "approval_policy", ACTION_DROP, "turn 元数据"),
    _r("sandbox_mode", "sandbox_mode", ACTION_DROP, "turn 元数据"),
    _r("network_access", "network_access", ACTION_DROP, "turn 元数据"),
]

# dsh (DeepSeek Harness) — 结构化 source.kind 过滤在 transcripts 场景层
# (user=真人放行, plugin/skill-catalog/goal/longTask=注入丢弃), 本表兜
# assistant 文本与无法结构判别的残余注入。
DSH_RULES = [
    _r("system-reminder", "system-reminder", ACTION_DROP,
      "skill 目录/AGENTS.md/spliced 重注入 (全量 501 次)"),
    _r("available_skills", "available_skills", ACTION_DROP, "skill 目录 (330)"),
    _r("skill_content", "skill_content", ACTION_DROP, "skill 目录说明文字 (330)"),
    _r("compacted-summary", "compacted-summary", ACTION_DROP,
      "checkpoint 压缩重注入 (46; 结构层 plugin:compact 38 已滤, 此兜正文残余)"),
    _r("goal_round", "goal_round", ACTION_DROP, "goal 续跑注入 (92)"),
    _r("long_task_round", "long_task_round", ACTION_DROP, "long-task 轮次注入 (28)"),
    _r("long_task_ledger", "long_task_ledger", ACTION_DROP, "long-task 账本注入 (31)"),
]

# pi / omp (pi wire) — 无注入判别字段, 只能拆桥信封 (62% user 文本被包)。
PI_RULES = [
    _r("bridge_context", "bridge_context", ACTION_DROP,
      "lark 桥信封元数据 (171; 含 chatId/senderId)"),
    _r("bridge_instructions", "bridge_instructions", ACTION_DROP, "桥运行指令 (170)"),
    _r("quoted_messages", "quoted_messages", ACTION_DROP, "桥引用消息 JSON (7)"),
    _r("user_input", "user_input", ACTION_UNWRAP, "拆信封保真实用户输入 (170)"),
]

# 通用 (五 harness 合并于各表之首) — memsvc 自有召回标记块。出端两个打标
# 面: hooks/recall_inject.py 的 additionalContext + projection.project_recall
# 的 recall-<DATE>.md 正文节。MEMORY.md 索引行不打标 (单行索引非内容)。
COMMON_RULES = [
    _r("memsvc-recall-block", "memsvc-recall", ACTION_DROP,
       "出端打标 (recall_inject 注入 / recall --project 正文), 进端整块剥"),
]

HARNESS_RULES: dict[str, list[BlockRule]] = {
    h: COMMON_RULES + rules
    for h, rules in {
        "cc": CC_RULES,
        "codex": CODEX_RULES,
        "dsh": DSH_RULES,
        "pi": PI_RULES,
        "omp": PI_RULES,  # omp 与 pi 同 wire 格式 (transcripts 同款别名)
    }.items()
}

HARNESSES = tuple(HARNESS_RULES)

# 模块导入期一次编译 (clean 在逐块热路径上, 不重复 compile)
_COMPILED: dict[str, list[tuple[BlockRule, re.Pattern]]] = {
    h: [(rule, re.compile(
            rule.pattern or
            rf"<{re.escape(rule.tag)}(?:\s[^>]*)?>(.*?)</{re.escape(rule.tag)}\s*>",
            re.DOTALL))
        for rule in rules]
    for h, rules in HARNESS_RULES.items()
}

_NL_COLLAPSE = re.compile(r"\n{3,}")


def clean(text: str, harness: str = "cc",
          stats: dict[str, int] | None = None) -> str:
    """按 harness 映射表清洗一段 transcript 文本 (drop/unwrap + 空行收敛)。

    Args:
        text: 原始块文本 (消息 text 块 / 蒸馏 end step)。
        harness: 规则表键 (cc|codex|dsh|pi|omp); 未知 → ValueError 响亮
            (静默跳过清洗 = 注入放行, 不做)。
        stats: 可选出参; 传 dict 时按规则名累计命中次数 (观测/回归用)。

    Returns:
        清洗后文本 (可能变短或为空 — 全注入块时; 调用方按空串语义处理)。
    """
    if harness not in _COMPILED:
        raise ValueError(f"unknown harness: {harness!r} (可用: {HARNESSES})")
    out = text
    for rule, pat in _COMPILED[harness]:
        if rule.action == ACTION_DROP:
            out, n = pat.subn("", out)
        elif rule.action == ACTION_UNWRAP:
            out, n = pat.subn(r"\1", out)
        else:  # dataclass 构造面封闭, 防御分支
            raise ValueError(f"unknown action: {rule.action!r} ({rule.name})")
        if n and stats is not None:
            stats[rule.name] = stats.get(rule.name, 0) + n
    return _NL_COLLAPSE.sub("\n\n", out).strip()


# ── 密钥脱敏 (采纳 #4) ───────────────────────────────────────────────

_REDACTED = "[REDACTED_SECRET]"
_REDACTED_PEM = "[REDACTED_PEM_KEY]"

# (名, 编译态, 替换) — 只收「知名前缀 + 带上下文赋值」两类; 裸长 hex 刻意
# 不收 (证据: 562 处合法长 hex vs 0 密钥, 见模块 docstring)。
_SECRET_RULES: list[tuple[str, re.Pattern, str]] = [
    ("pem-private-key",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL),
     _REDACTED_PEM),
    ("sk-prefix", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), _REDACTED),
    ("github-token", re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
     _REDACTED),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}"), _REDACTED),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), _REDACTED),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), _REDACTED),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
     f"Bearer {_REDACTED}"),
    ("key-value-assign",
     # 环境变量前缀兼容 (ZHIPU_API_KEY= / MY_TOKEN= — \b 在下划线前不成立,
     # 允许任意 [a-z0-9_] 前缀并整键保留; keyword 后必须紧跟 =/:, 「token_
     # count: 5」「tokens=...」类不误伤)
     re.compile(r"(?i)\b([a-z0-9_]*(?:api[_-]?key|apikey|secret|"
                r"access[_-]?token|auth[_-]?token|token|passwd|password|pwd|"
                r"authorization))\s*([=:])\s*[\"']?[A-Za-z0-9._~+/=-]{12,}[\"']?"),
     r"\1\2 " + _REDACTED),
]


def redact_secrets(text: str, stats: dict[str, int] | None = None) -> str:
    """密钥形态 span → [REDACTED_*] (幂等: 已脱敏文本不再命中)。

    落点: ``llm_extract.extract`` 对 segment 恒先跑 — 密钥不进 prompt,
    evidence 逐字断言以脱敏后文本为准 (LLM 只可能引用脱敏态, 断言自洽)。
    """
    out = text
    for name, pat, repl in _SECRET_RULES:
        out, n = pat.subn(repl, out)
        if n and stats is not None:
            stats[name] = stats.get(name, 0) + n
    return out


def rule_table(harness: str | None = None) -> dict[str, list[BlockRule]]:
    """映射表检视口 (文档/测试用): 全表或单 harness 视图。"""
    if harness is None:
        return {h: list(rules) for h, rules in HARNESS_RULES.items()}
    if harness not in HARNESS_RULES:
        raise ValueError(f"unknown harness: {harness!r} (可用: {HARNESSES})")
    return {harness: list(HARNESS_RULES[harness])}


__all__ = ["clean", "redact_secrets", "rule_table", "BlockRule",
           "ACTION_DROP", "ACTION_UNWRAP", "HARNESS_RULES", "HARNESSES"]

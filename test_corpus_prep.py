"""corpus_prep 验收: harness 标记块映射表 + 密钥脱敏 (2026-08-28)。

证据基础 = 三路真实语料扫描 (cc ~221 文件 / codex 326 rollout / dsh 288 +
pi 59 session, 全量+分层抽样, 只读):
- cc 白名单制 — 泛黑名单会误杀 tool_result 里的代码回显泛型 (Self/String)
- codex 65% user 消息是注入伪装; <permissions instructions> 带属性
- 裸长 hex 规则被否决 (562 处合法长 hex: git SHA/sha256 摘要, 0 密钥)

零网络零 LLM (纯文本变换)。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

import corpus_prep
from corpus_prep import (ACTION_DROP, ACTION_UNWRAP, HARNESSES,
                         clean, redact_secrets, rule_table)


# ── 映射表结构 ────────────────────────────────────────────────────────

def test_table_covers_all_harnesses():
    assert set(HARNESSES) == {"cc", "codex", "dsh", "pi", "omp"}
    for h in HARNESSES:
        assert rule_table(h)[h], f"{h} 规则表为空"


def test_unknown_harness_loud():
    """未知 harness → ValueError 响亮 (静默跳过清洗 = 注入放行, 不做)。"""
    with pytest.raises(ValueError, match="unknown harness"):
        clean("x", "vscode")


# ── cc 规则 (白名单制) ────────────────────────────────────────────────

def test_cc_drops_system_reminder_and_command_echo():
    txt = ("<system-reminder>Additional context here</system-reminder>\n"
           "真实结论: 采用 pkill 方案兜底。\n"
           "<local-command-stdout>foo bar</local-command-stdout>")
    out = clean(txt, "cc")
    assert "system-reminder" not in out
    assert "Additional context" not in out
    assert "foo bar" not in out
    assert "真实结论: 采用 pkill 方案兜底。" in out


def test_cc_unwraps_command_name_keeps_intent():
    txt = "<command-name>/memsvc-recall</command-name> <command-args>endsteps</command-args>"
    out = clean(txt, "cc")
    assert "<command-name>" not in out and "<command-args>" not in out
    assert "/memsvc-recall" in out and "endsteps" in out  # 命令意图保留


def test_cc_task_notification_unwrap_keeps_inner_result():
    txt = ("<task-notification>子代理完成</task-notification>\n"
           "<result>结论: 图谱连通性修复</result>")
    out = clean(txt, "cc")
    assert "<task-notification>" not in out and "<result>" not in out
    assert "子代理完成" in out and "结论: 图谱连通性修复" in out


def test_cc_generic_angle_brackets_survive():
    """白名单制的存在理由: tool_result 代码回显泛型绝不能误杀 (扫描: ~90%
    尖括号命中是 Self/String/f32 类代码语料)。"""
    txt = "impl Display for Self uses Vec<String> and f32/usize conversions"
    assert clean(txt, "cc") == txt


def test_cc_multiline_block_with_attributes():
    txt = ("<system-reminder lang=\"zh\" mode=\"strict\">\n多行\n注入\n</system-reminder>\n保留")
    out = clean(txt, "cc")
    assert "注入" not in out and "保留" in out


# ── codex 规则 ────────────────────────────────────────────────────────

def test_codex_drops_environment_context_and_instructions():
    txt = ("<environment_context>\n<cwd>/home/yy</cwd>\n<shell>bash</shell>\n"
           "</environment_context>\n真实输入: QT_QPA_PLATFORM 怎么配?\n"
           "# AGENTS.md instructions\n<INSTRUCTIONS>\n## Skills\n技能说明\n</INSTRUCTIONS>")
    out = clean(txt, "codex")
    assert "environment_context" not in out and "/home/yy" not in out
    assert "AGENTS.md" not in out and "技能说明" not in out
    assert "真实输入: QT_QPA_PLATFORM 怎么配?" in out


def test_codex_drops_permissions_with_attrs_and_turn_meta():
    txt = ("<permissions instructions=\"allow\">\nrule one\n</permissions>\n"
           "<approval_policy>on-request</approval_policy>\n"
           "<sandbox_mode>read-only</sandbox_mode>\n"
           "<network_access>restricted</network_access>\n正文")
    out = clean(txt, "codex")
    assert "rule one" not in out and "on-request" not in out
    assert "read-only" not in out and "restricted" not in out
    assert "正文" in out


# ── dsh 规则 (标签兜底层; 结构化 source.kind 过滤在 transcripts 场景层) ─

def test_dsh_drops_skill_catalog_and_compacted_summary():
    txt = ("<system-reminder>\n<available_skills>\n- cb-send: 用来回调编排者\n"
           "</available_skills>\n</system-reminder>\n"
           "<compacted-summary>\n# Session Checkpoint\n早期上下文摘要\n</compacted-summary>\n"
           "本轮真实结论。")
    out = clean(txt, "dsh")
    assert "available_skills" not in out and "cb-send" not in out
    assert "Session Checkpoint" not in out
    assert "本轮真实结论。" in out


def test_dsh_drops_goal_round_and_long_task_ledger():
    txt = ("<goal_round>\nObjective: 跟踪某任务\n</goal_round>\n"
           "<long_task_round>\nRound: 1/6\n<long_task_ledger>\nledger body\n"
           "</long_task_ledger>\n</long_task_round>\n正文保留")
    out = clean(txt, "dsh")
    assert "Objective" not in out and "Round: 1/6" not in out
    assert "ledger body" not in out and "正文保留" in out


def test_dsh_pseudo_tags_survive():
    """dsh 正文模板占位符 (<ref>/<name>/<N>) 是真人派工指令 — 不能当注入丢。"""
    txt = "派发 worker: <ref> VO-006 / merge </ref> body 单行≤200字 <N>=3"
    assert clean(txt, "dsh") == txt


# ── pi/omp 规则 (桥信封拆包) ──────────────────────────────────────────

def test_pi_envelope_stripped_user_input_kept():
    txt = ('<bridge_context>\n{"chatId":"oc_x","senderId":"ou_y"}\n</bridge_context>\n'
           '<bridge_instructions>\n["bridge 进程内运行"]\n</bridge_instructions>\n'
           '<user_input>\n{"text":"自我介绍你的能力"}\n</user_input>')
    out = clean(txt, "pi")
    assert "bridge_context" not in out and "chatId" not in out
    assert "bridge_instructions" not in out and "LARK_CHANNEL" not in out
    assert "<user_input>" not in out
    assert "自我介绍你的能力" in out  # 拆信封保内文


def test_omp_shares_pi_rules():
    assert clean("<bridge_context>x</bridge_context>正文", "omp") == "正文"


# ── 通用规则: memsvc 召回标记块 (出端闭环 2026-08-28, 五 harness 合并) ──

@pytest.mark.parametrize("h", ["cc", "codex", "dsh", "pi", "omp"])
def test_memsvc_recall_block_dropped_all_harnesses(h):
    """<memsvc-recall> 出端打标 → 五 harness 进端整块丢弃 (召回回声不重入库)。"""
    txt = ("<memsvc-recall>\n## Memory recall (auto, 2 hits)\n"
           "- 专家职位 — 甲乙丙  [0.42]\n</memsvc-recall>\n真实结论保留。")
    stats: dict[str, int] = {}
    out = clean(txt, h, stats=stats)
    assert "memsvc-recall" not in out and "0.42" not in out and "甲乙丙" not in out
    assert "真实结论保留。" in out
    assert stats.get("memsvc-recall-block") == 1


def test_memsvc_recall_block_with_attrs_and_multiline():
    txt = '<memsvc-recall src="recall-20260828.md">命中一\n命中二</memsvc-recall>正文'
    out = clean(txt, "dsh")
    assert "命中一" not in out and "命中二" not in out
    assert out == "正文"


def test_memsvc_recall_rule_in_every_table_first():
    for h in HARNESSES:
        names = [r.name for r in rule_table(h)[h]]
        assert names[0] == "memsvc-recall-block", f"{h} 缺通用规则"


# ── 密钥脱敏 ──────────────────────────────────────────────────────────

def test_redact_known_prefixes():
    txt = ("key sk-AbCdEf12345678901234 here\n"
           "gh token ghp_0123456789abcdefghij\n"
           "aws AKIAIOSFODNN7EXAMPLE\n"
           "slack xoxb-123456789-abc\n"
           "gitlab glpat-AbCdEf123456789012\n"
           "-----BEGIN RSA PRIVATE KEY-----\nMIIEpA\n-----END RSA PRIVATE KEY-----")
    out = redact_secrets(txt)
    assert "sk-AbCdEf12345678901234" not in out
    assert "ghp_0123456789abcdefghij" not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "xoxb-123456789-abc" not in out
    assert "glpat-AbCdEf123456789012" not in out
    assert "MIIEpA" not in out
    assert "[REDACTED_SECRET]" in out and "[REDACTED_PEM_KEY]" in out


def test_redact_bearer_and_kv_assign():
    txt = ("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\n"
           "ZHIPU_API_KEY=abc123def456ghi789\n"
           "password: \"hunter2secret\"\n正文不动")
    out = redact_secrets(txt)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out
    assert "abc123def456ghi789" not in out and "hunter2secret" not in out
    assert "ZHIPU_API_KEY" in out  # 键名保留 (形态可读, 值脱敏)
    assert "正文不动" in out


def test_redact_stats_out_param():
    stats: dict[str, int] = {}
    redact_secrets("k sk-AbCdEf12345678901234", stats=stats)
    assert stats.get("sk-prefix") == 1


def test_clean_stats_out_param():
    stats: dict[str, int] = {}
    clean("<system-reminder>a</system-reminder>b", "cc", stats=stats)
    assert stats.get("system-reminder") == 1


def test_clean_empty_after_full_injection():
    """全注入块 → 清洗后空串 (调用方按空串语义处理 — scenes/长度门兜)。"""
    assert clean("<system-reminder>x</system-reminder>", "cc") == ""


# ── autodream 接缝 (PreCompact spool 全量路径逐块清洗) ────────────────

def test_autodream_read_transcript_cleans_blocks_per_harness():
    """_read_transcript 每块过 corpus_prep.clean(harness) — 注入块在进提取
    管道前剥除; 块文法 (block_type) 不变。"""
    import json as _json
    import tempfile
    import autodream
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.jsonl"
        p.write_text("\n".join([
            _json.dumps({"type": "user",
                         "message": {"content":
                                     "<system-reminder>注入</system-reminder>"
                                     "用户真话: 采用 A 方案"}},
                        ensure_ascii=False),
            _json.dumps({"type": "assistant",
                         "message": {"content": [
                             {"type": "text",
                              "text": "<local-command-stdout>回显</local-command-stdout>"
                                      "结论: 走 B 路径"}]}},
                        ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        blocks = autodream._read_transcript(p, "cc")
        assert blocks == [("user_text", "用户真话: 采用 A 方案"),
                          ("assistant_text", "结论: 走 B 路径")]

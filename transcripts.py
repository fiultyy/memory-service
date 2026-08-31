"""transcripts — 多 harness transcript 统一定位/蒸馏适配层 (M19, 2026-08-27;
M22 +codex, 2026-08-28 — yaml 节点3 过滤逻辑映射)。

memsvc 核心本就 harness 无关 (KG + recall CLI + autodream, 路径全模块相对);
本模块把「进端」最后一段 harness 耦合 (transcript 落盘位置 + end step 判定)
收口成 per-harness 适配器, 供 cli ingest-recent 消费:

=========== ============================================================= ==========================
harness     落盘位置                                                      end step 判定 (实测校准)
=========== ============================================================= ==========================
cc          ~/.claude/projects/<enc>/*.jsonl                              type==assistant ∧
            enc = cwd 的 / 和 . 都换 -                                     message.stop_reason=="end_turn"
dsh         ~/.dsh/sessions/<enc>/session-<uuid>/session.jsonl.zstd      turn/end(reason=completed) 前最后
            enc = "-" + cwd 的 / 换 - + "--" (点保留)                       一条 assistant/message 的 text 块
pi          ~/.pi/agent/sessions/<enc>/<ts>_<uuid>.jsonl                  type==message ∧ role==assistant ∧
            enc = "-" + cwd 的 / 换 - + "--" (点保留)                       message.stopReason=="stop"
omp (pi系)  ~/.omp/agent/sessions/<enc>/<ts>_<uuid>.jsonl                 同 pi (但 enc = $HOME 相对路径
                                                                           / 换 -, 无包装横杠)
codex       ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (无项目目录,     response_item ∧ message ∧
            项目过滤按会话头 session_meta.cwd 结构匹配, M22)                role==assistant 的 output_text
=========== ============================================================= ==========================

三家共同语义 (与 endsteps.py 单源对齐):
- 只取 text 块 — thinking/reasoning/tool 块不泄漏 (CC: thinking; dsh: reasoning/
  tool-call; omp: thinking/toolCall)。
- 长度门 MEM_ENDSTEP_MIN_CHARS (默认 120) — 寒暄/空尾不烧 LLM。
- 文内精确去重 (compact 重注入同文)。
- 子代理侧链默认排除: CC isSidechain / dsh delegationDepth>0 (parentSession 仅
  表示 compact 续种, 不是侧链) / omp 无标记 (session 粒度天然主链)。

出端 (召回) 无需本层: recall --json 任何能起进程的 harness 都能裸调;
文件投影面 CC=memory/ (done), dsh/omp=APPEND_SYSTEM.md 约定 (pi 系同款,
待用户裁决后再接, 见 SKILL 红线)。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from endsteps import DEFAULT_MIN_CHARS


def _gates() -> tuple[int, bool]:
    """长度门 + 侧链门 (与 endsteps.extract_end_steps 同 env 单源)。"""
    min_chars = int(os.environ.get("MEM_ENDSTEP_MIN_CHARS",
                                   str(DEFAULT_MIN_CHARS)))
    include_sidechain = os.environ.get("MEM_ENDSTEP_SIDECHAIN", "0") == "1"
    return min_chars, include_sidechain


def _texts_of(content) -> str:
    """content 块列表 → 拼接 text 块单文 (CC/dsh/omp 的 text 块 type 都是 'text')。

    字符串 content 防御性直收; thinking/reasoning/toolCall/tool-call 块天然滤除。
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(t for t in parts if t and t.strip()).strip()


def _uservoice_gates() -> tuple[int, int, int]:
    """用户声音通道三参数: (min_chars, max_blocks, budget_chars)。

    用户纠正可以很短 (「不对哦,真有关系」) — 门槛远低于 end step 的 120;
    block 数/字符预算封顶防单场景成本膨胀 (Codex 阅读优先级只看「最近的
    用户输入语境」, 全量 user 语料是另一条管道的事)。
    """
    min_chars = int(os.environ.get("MEM_USERVOICE_MIN_CHARS", "4"))
    max_blocks = int(os.environ.get("MEM_USERVOICE_MAX_BLOCKS", "4"))
    budget = int(os.environ.get("MEM_USERVOICE_BUDGET", "1200"))
    return min_chars, max_blocks, budget


def _cap_user_blocks(blocks: list[str]) -> list[str]:
    """保留最近 N 块、总字符 ≤ budget (时间序输出 — 最旧的在最前)。"""
    _min, max_blocks, budget = _uservoice_gates()
    out: list[str] = []
    total = 0
    for txt in reversed(blocks):
        if len(out) >= max_blocks or total + len(txt) > budget:
            break
        out.append(txt)
        total += len(txt)
    return list(reversed(out))


def _gate_and_dedup(texts: list[str]) -> list[str]:
    """共同语义收口: 长度门 + 文内精确去重 (保序)。"""
    min_chars, _ = _gates()
    out: list[str] = []
    seen: set[str] = set()
    for txt in texts:
        if len(txt) < min_chars or txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    return out


# ── cc (Claude Code) ────────────────────────────────────────────────

def _cc_project_dir(cwd: str) -> Path:
    encoded = cwd.replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded


def _cc_session_id(path: Path) -> str:
    return path.stem


def _cc_end_steps(lines) -> list[str]:
    import endsteps
    return endsteps.extract_end_steps(lines)  # 单源: 原实现即 CC 适配器


# ── dsh (DeepSeek Harness) ──────────────────────────────────────────

def _dsh_project_dir(cwd: str) -> Path:
    # 实测规则 (4 样本校准: /home/yy, ~/.dsh, ~/qgis-data, 本项目):
    # "-" + cwd 的 / 换 - + "--" (点保留; 非 ASCII cwd 会变 ~hex~ 形, 暂不支持)。
    encoded = "-" + cwd.replace("/", "-") + "--"
    return Path.home() / ".dsh" / "sessions" / encoded


def _dsh_session_id(path: Path) -> str:
    return path.parent.name  # session-<uuid>


def _dsh_open(path: Path):
    """session.jsonl.zstd → 行迭代器; 裸 .jsonl 直读 (测试/无压缩形态)。

    zstd 解压走 zstdcat 子进程 — 不可用即 RuntimeError 响亮失败 (无静默降级红线)。
    """
    if path.suffix == ".zstd":
        if shutil.which("zstdcat") is None and shutil.which("zstd") is None:
            raise RuntimeError(
                "dsh transcript 需要 zstd/zstdcat 解压, 系统未安装 — "
                "apt install zstd 后重试 (不静默跳过)")
        cmd = ["zstdcat", str(path)] if shutil.which("zstdcat") \
            else ["zstd", "-dc", str(path)]
        return subprocess.run(cmd, capture_output=True, check=True,
                              text=True).stdout.splitlines()
    return path.open(encoding="utf-8")


def _dsh_end_steps(lines) -> list[str]:
    """事件流 → 每个已完成 turn 的最后一条 assistant/message text 块。

    侧链: session 头 delegationDepth>0 默认排除 (compact 续种 parentSession
    不是侧链, delegationDepth 才是)。turn/end reason!=completed (aborted 等)
    的回合不取。
    """
    _, include_sidechain = _gates()
    depth: int | None = None
    texts: list[str] = []
    last_assistant_text: str | None = None  # 当前 turn 内最后一条 assistant text
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "session":
            depth = d.get("delegationDepth", 0)
        elif t == "assistant/message":
            msg = (d.get("data") or {}).get("message") or {}
            txt = _texts_of(msg.get("content"))
            if txt:
                last_assistant_text = txt
        elif t == "turn/end":
            reason = ((d.get("data") or {}).get("reason") or {}).get("kind")
            if reason == "completed" and last_assistant_text:
                texts.append(last_assistant_text)
            last_assistant_text = None
    if depth is not None and depth > 0 and not include_sidechain:
        return []
    return _gate_and_dedup(texts)


# ── pi / omp (pi wire 格式) ─────────────────────────────────────────

def _pi_end_steps(lines) -> list[str]:
    """pi wire 格式 → stopReason=="stop" 的 assistant text 块。

    stop=自然收尾; toolUse=中间步骤; error/aborted=异常截断 (天然排除,
    与 CC end_turn 同语义)。pi 与 omp 会话 jsonl 同格式, 共用本判定;
    差异只在目录定位 (_pi_project_dir vs _omp_project_dir)。
    """
    texts: list[str] = []
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("type") != "message":
            continue
        msg = d.get("message") or {}
        if msg.get("role") != "assistant" or msg.get("stopReason") != "stop":
            continue
        txt = _texts_of(msg.get("content"))
        if txt:
            texts.append(txt)
    return _gate_and_dedup(texts)


# 别名: omp 会话与 pi 同 wire 格式, 判定共用
_omp_end_steps = _pi_end_steps


# ── 用户声音场景 (M21, 2026-08-28 — Codex 阅读优先级采纳 #1) ─────────
#
# end step 单飞的缺陷: 提取器永远看不到用户的声音, 「用户偏好/裁决」类记忆
# 的召回上限从管道结构上被封顶 (Codex Phase1 阅读优先级 user > tool >
# assistant 的前提是 user 内容可达)。场景 = 每个 end step 配对其前累积的
# 用户原话块 — cli ingest-recent 合成 transcript 时按
# [用户] … / [助手结论] … 标记写入, autodream 块文法合并为同 provenance 段,
# 提取器在段内同时看到两侧。

def _cc_scenes(lines) -> list[dict]:
    """CC transcript → 场景列表 (end step 判定与 endsteps 单源同语义)。

    user 消息只取 text 块 (_texts_of) — CC 的 tool_result 由 user role
    携带但 block type 不同, 天然排除; 侧链门与 endsteps 同 env 单源。
    """
    min_chars, include_sidechain = _gates()
    uv_min, _maxb, _budget = _uservoice_gates()
    scenes: list[dict] = []
    pending_user: list[str] = []
    seen_user: set[str] = set()
    seen_step: set[str] = set()
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "user":
            if d.get("isSidechain") and not include_sidechain:
                continue
            msg = d.get("message") or {}
            txt = _texts_of(msg.get("content"))
            if txt and len(txt) >= uv_min and txt not in seen_user:
                seen_user.add(txt)
                pending_user.append(txt)
        elif t == "assistant":
            if d.get("isSidechain") and not include_sidechain:
                continue
            msg = d.get("message") or {}
            if msg.get("stop_reason") != "end_turn":
                continue
            content = msg.get("content") or []
            if isinstance(content, str):
                texts = [content]
            else:
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
            txt = "\n".join(x for x in texts if x and x.strip()).strip()
            if len(txt) < min_chars or txt in seen_step:
                continue
            seen_step.add(txt)
            scenes.append({"user_blocks": _cap_user_blocks(pending_user),
                           "end_step": txt})
            pending_user = []
    return scenes


def _dsh_scenes(lines) -> list[dict]:
    """dsh 事件流 → 场景列表。user/message 用 **结构化注入判别**: 288 文件
    全量扫描实测 ``data.source.kind`` 是最强判别器 — 只有 ``"user"`` 是真人
    (1067 条), plugin/skill-catalog/goal/longTask/... 全是注入; kind 缺失
    保留 (防御老版本, 注入形态全部带 kind)。"""
    min_chars, include_sidechain = _gates()
    uv_min, _maxb, _budget = _uservoice_gates()
    depth: int | None = None
    scenes: list[dict] = []
    pending_user: list[str] = []
    seen_user: set[str] = set()
    seen_step: set[str] = set()
    last_assistant_text: str | None = None
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "session":
            depth = d.get("delegationDepth", 0)
        elif t == "user/message":
            data = d.get("data") or {}
            if ((data.get("source") or {}).get("kind")) not in (None, "user"):
                continue
            # 实测 (session-4a37e718): 真人事件 content 直挂 data.content,
            # data.message 为空 — 双形态兼容 (message.content 兜底, 测试桩
            # 与潜在老版本走此路)。
            _msg = data.get("message") or {}
            txt = _texts_of(data.get("content") or _msg.get("content"))
            # 跨会话信箱载荷 (实测 session-4a37e718: kind=user 里混着
            # "DSHMSG]{...}" 编排流量 — agent-to-agent 消息, 非真人原话)。
            if txt and "DSHMSG]" in txt[:12]:
                continue
            if txt and len(txt) >= uv_min and txt not in seen_user:
                seen_user.add(txt)
                pending_user.append(txt)
        elif t == "assistant/message":
            msg = (d.get("data") or {}).get("message") or {}
            txt = _texts_of(msg.get("content"))
            if txt:
                last_assistant_text = txt
        elif t == "turn/end":
            reason = ((d.get("data") or {}).get("reason") or {}).get("kind")
            if (reason == "completed" and last_assistant_text
                    and len(last_assistant_text) >= min_chars
                    and last_assistant_text not in seen_step):
                seen_step.add(last_assistant_text)
                scenes.append({"user_blocks": _cap_user_blocks(pending_user),
                               "end_step": last_assistant_text})
            if reason == "completed":
                pending_user = []  # 完成的回合消耗掉累积用户语境
            last_assistant_text = None
    if depth is not None and depth > 0 and not include_sidechain:
        return []
    return scenes


def _pi_scenes(lines) -> list[dict]:
    """pi/omp wire → 场景列表 (end step 判定与 _pi_end_steps 同语义)。"""
    min_chars, _sidechain = _gates()
    uv_min, _maxb, _budget = _uservoice_gates()
    scenes: list[dict] = []
    pending_user: list[str] = []
    seen_user: set[str] = set()
    seen_step: set[str] = set()
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("type") != "message":
            continue
        msg = d.get("message") or {}
        role = msg.get("role")
        if role == "user":
            txt = _texts_of(msg.get("content"))
            if txt and len(txt) >= uv_min and txt not in seen_user:
                seen_user.add(txt)
                pending_user.append(txt)
        elif role == "assistant" and msg.get("stopReason") == "stop":
            txt = _texts_of(msg.get("content"))
            if txt and len(txt) >= min_chars and txt not in seen_step:
                seen_step.add(txt)
                scenes.append({"user_blocks": _cap_user_blocks(pending_user),
                               "end_step": txt})
                pending_user = []
    return scenes


# omp 与 pi 同 wire 格式, 场景判定共用
_omp_scenes = _pi_scenes

# ── codex (OpenAI Codex CLI, M22 — yaml 节点3 过滤逻辑映射, 2026-08-28) ──
#
# rollout 行形 {timestamp, type, payload}; 三层过滤照 codex-memories-
# pipeline.yaml 节点3: 行类型 → ResponseItem → content 块。role=developer
# / turn_context 层不读; 注入块 (environment_context / AGENTS.md
# <INSTRUCTIONS> 投影 / permissions / turn 元数据) 由 corpus_prep codex
# 规则在清洗口剥除; **真人 user 语料取 event_msg/user_message 镜像层** —
# 实测 326 文件: 65% 的 role=user ResponseItem 是系统注入伪装, event_msg
# 恰好只镜像真实输入 (源头过滤优于事后清洗, 同 yaml「注入块整块丢弃」)。

def _codex_meta(path: Path) -> dict:
    """会话头 session_meta.payload (恒首行; 前 5 行内找, 缺 → {})。"""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("type") == "session_meta":
                    p = d.get("payload")
                    return p if isinstance(p, dict) else {}
    except OSError:
        pass
    return {}


def _codex_project_dir(cwd: str) -> Path:
    # codex 不按项目分目录: 全部落日期目录 — 项目过滤在 locate() 按
    # session_meta.cwd 结构匹配 (_CWD_FILTERS 钩子), 目录只当扫描根。
    return Path.home() / ".codex" / "sessions"


def _codex_session_id(path: Path) -> str:
    sid = _codex_meta(path).get("id")
    if isinstance(sid, str) and sid:
        return sid
    return path.stem.rsplit("-", 1)[-1] or path.stem  # 文件名尾段 uuid 兜底


def _codex_cwd_match(path: Path, cwd: str) -> bool:
    return _codex_meta(path).get("cwd") == cwd


def _codex_scenes(lines) -> list[dict]:
    """codex rollout → 场景列表。

    - user 语料: ``event_msg/user_message`` (payload.message 字符串);
      SDK/exec 型会话无该事件 → 自然零用户语料 (无真人交互, 正确)。
    - end step: ``response_item ∧ payload.type=message ∧ role=assistant``
      的 output_text — codex 无 stop_reason 语义, 每条 assistant message 即
      用户可见收尾陈述 (推理/工具调用是独立 payload type, 不在 text 里)。
    """
    min_chars, _include_sidechain = _gates()
    uv_min, _maxb, _budget = _uservoice_gates()
    scenes: list[dict] = []
    pending_user: list[str] = []
    seen_user: set[str] = set()
    seen_step: set[str] = set()
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        p = d.get("payload") or {}
        pt = p.get("type")
        if t == "event_msg" and pt == "user_message":
            msg = p.get("message")
            txt = msg.strip() if isinstance(msg, str) else ""
            if txt and len(txt) >= uv_min and txt not in seen_user:
                seen_user.add(txt)
                pending_user.append(txt)
        elif (t == "response_item" and pt == "message"
              and p.get("role") == "assistant"):
            content = p.get("content") or []
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "output_text"]
            txt = "\n".join(x for x in texts if x and x.strip()).strip()
            if txt and len(txt) >= min_chars and txt not in seen_step:
                seen_step.add(txt)
                scenes.append({"user_blocks": _cap_user_blocks(pending_user),
                               "end_step": txt})
                pending_user = []  # 场景消耗当前用户语境
    return scenes


def _codex_end_steps(lines) -> list[str]:
    return _gate_and_dedup([s["end_step"] for s in _codex_scenes(lines)])


# 项目级 cwd 结构匹配钩子 (目录即扫描根的 harness 用; 缺省无过滤)
_CWD_FILTERS = {"codex": _codex_cwd_match}


_SCENES = {
    "cc": _cc_scenes,
    "dsh": _dsh_scenes,
    "pi": _pi_scenes,
    "omp": _omp_scenes,
    "codex": _codex_scenes,
}


def _pi_project_dir(cwd: str) -> Path:
    # 实测 (真实 ~/.pi/agent/sessions 目录名校准): 与 dsh 同规则 —
    # "-" + cwd 的 / 换 - + "--", 点保留 (如 /home/yy/.omp → --home-yy-.omp--)。
    encoded = "-" + cwd.replace("/", "-") + "--"
    return Path.home() / ".pi" / "agent" / "sessions" / encoded


def _pi_session_id(path: Path) -> str:
    return path.stem  # <ts>_<uuid>


def _omp_project_dir(cwd: str) -> Path:
    """enc = $HOME 相对路径 / → - (点保留, 实测 ~/.omp/agent/sessions 目录名)。

    cwd == $HOME 边界: 无已知编码约定, 返回不存在的哨兵路径 (locate → 空,
    绝不落到 sessions/ 根造成跨项目误扫)。
    """
    home = str(Path.home())
    if cwd == home:
        return Path.home() / ".omp" / "agent" / "sessions" / "-"
    rel = cwd[len(home):] if cwd.startswith(home + "/") else cwd
    return Path.home() / ".omp" / "agent" / "sessions" / rel.replace("/", "-")


def _omp_session_id(path: Path) -> str:
    return path.stem  # <ts>_<uuid>




# ── 统一门面 ────────────────────────────────────────────────────────

_ADAPTORS = {
    "cc": (_cc_project_dir, _cc_session_id, _cc_end_steps,
           lambda p: p.suffix == ".jsonl"),
    "dsh": (_dsh_project_dir, _dsh_session_id, _dsh_end_steps,
            lambda p: p.name == "session.jsonl.zstd" or p.suffix == ".jsonl"),
    "pi": (_pi_project_dir, _pi_session_id, _pi_end_steps,
           lambda p: p.suffix == ".jsonl"),
    "omp": (_omp_project_dir, _omp_session_id, _omp_end_steps,
            lambda p: p.suffix == ".jsonl"),
    "codex": (_codex_project_dir, _codex_session_id, _codex_end_steps,
              lambda p: p.suffix == ".jsonl"),
}

# 兼容旧名 (拼写正名 _ADAPTORS, 历史引用保持)
_ADAPTERS = _ADAPTORS

HARNESSES = ("cc", "dsh", "pi", "omp", "codex")


def locate(cwd: str, harness: str = "cc", limit: int = 10) -> list[Path]:
    """cwd 最近 N 个 transcript (mtime 降序)。目录不存在 → 空 (调用方报)。

    codex: 扫描根 ~/.codex/sessions 无项目结构, 按 session_meta.cwd 结构
    匹配过滤 (_CWD_FILTERS 钩子) 后再取 mtime 前 N。
    """
    if harness not in _ADAPTORS:
        raise ValueError(f"unknown harness: {harness!r} (可用: {HARNESSES})")
    pdir, _sid, _ext, keep = _ADAPTORS[harness]
    d = pdir(cwd)
    if not d.is_dir():
        return []
    files = [p for p in d.rglob("*") if p.is_file() and keep(p)]
    flt = _CWD_FILTERS.get(harness)
    if flt is not None:
        files = [p for p in files if flt(p, cwd)]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def end_steps(path: Path, harness: str = "cc") -> list[str]:
    """单 transcript → end step 文本列表 (per-harness 判定 + 共同门 + 语料清洗)。

    读取 errors="replace": 活跃会话尾部半写/坏字节不炸整命令 (与 endsteps
    「坏行静默跳过」哲学一致, 坏行 json.loads 失败自然被跳过)。

    语料清洗 (2026-08-28, corpus_prep 接缝): 每条过 ``corpus_prep.clean``—
    系统注入/命令回显/压缩重注入块剥除; 剥除后可能低于长度门, 清洗后再门
    一遍并去重 (同 env 单源)。
    """
    import corpus_prep
    if harness not in _ADAPTERS:
        raise ValueError(f"unknown harness: {harness!r} (可用: {HARNESSES})")
    _pdir, _sid, extract, _keep = _ADAPTERS[harness]
    if harness == "dsh":
        lines = _dsh_open(path)
        try:
            raw = extract(lines)
        finally:
            if hasattr(lines, "close"):
                lines.close()
    else:
        with path.open(encoding="utf-8", errors="replace") as fh:
            raw = extract(fh)
    min_chars, _ = _gates()
    out: list[str] = []
    seen: set[str] = set()
    for txt in raw:
        txt = corpus_prep.clean(txt, harness)
        if len(txt) < min_chars or txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    return out


def scenes(path: Path, harness: str = "cc") -> list[dict]:
    """单 transcript → 用户声音场景列表 [{user_blocks: [...], end_step: str}]。

    每个 end step 配对其前累积的用户原话块 (时间序; ``_cap_user_blocks``
    保最近 ≤MEM_USERVOICE_MAX_BLOCKS 块、总字符 ≤MEM_USERVOICE_BUDGET)。
    用户块与 end step 都过 ``corpus_prep.clean(harness)`` — dsh 走结构化
    source.kind 判别在 walker 内先行, 标签清洗只作兜底。空用户块的场景
    照常返回 (纯结论场景, 与旧 end_steps 行为对齐)。
    """
    import corpus_prep
    if harness not in _ADAPTERS or harness not in _SCENES:
        raise ValueError(f"unknown harness: {harness!r} (可用: {HARNESSES})")
    extract = _SCENES[harness]
    if harness == "dsh":
        lines = _dsh_open(path)
        try:
            raw = extract(lines)
        finally:
            if hasattr(lines, "close"):
                lines.close()
    else:
        with path.open(encoding="utf-8", errors="replace") as fh:
            raw = extract(fh)
    min_chars, _ = _gates()
    uv_min, _maxb, _budget = _uservoice_gates()
    out: list[dict] = []
    for sc in raw:
        blocks: list[str] = []
        for b in sc["user_blocks"]:
            b = corpus_prep.clean(b, harness)
            if len(b) >= uv_min and b not in blocks:
                blocks.append(b)
        step = corpus_prep.clean(sc["end_step"], harness)
        if len(step) < min_chars:
            continue
        out.append({"user_blocks": blocks, "end_step": step})
    return out


def session_id(path: Path, harness: str = "cc") -> str:
    if harness not in _ADAPTERS:
        raise ValueError(f"unknown harness: {harness!r} (可用: {HARNESSES})")
    _pdir, sid, _ext, _keep = _ADAPTERS[harness]
    return sid(path)


if __name__ == "__main__":  # 手动诊断: python3 transcripts.py <cwd> [harness]
    _cwd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    _h = sys.argv[2] if len(sys.argv) > 2 else "cc"
    for p in locate(_cwd, _h):
        n = len(end_steps(p, _h))
        print(f"{p}  steps={n}")

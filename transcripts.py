"""transcripts — 多 harness transcript 统一定位/蒸馏适配层 (M19, 2026-08-27)。

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

HARNESSES = ("cc", "dsh", "pi", "omp")


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

_ADAPTERS = {
    "cc": (_cc_project_dir, _cc_session_id, _cc_end_steps,
           lambda p: p.suffix == ".jsonl"),
    "dsh": (_dsh_project_dir, _dsh_session_id, _dsh_end_steps,
            lambda p: p.name == "session.jsonl.zstd" or p.suffix == ".jsonl"),
    "pi": (_pi_project_dir, _pi_session_id, _pi_end_steps,
           lambda p: p.suffix == ".jsonl"),
    "omp": (_omp_project_dir, _omp_session_id, _omp_end_steps,
            lambda p: p.suffix == ".jsonl"),
}


def locate(cwd: str, harness: str = "cc", limit: int = 10) -> list[Path]:
    """cwd 最近 N 个 transcript (mtime 降序)。目录不存在 → 空 (调用方报)。"""
    if harness not in _ADAPTERS:
        raise ValueError(f"unknown harness: {harness!r} (可用: {HARNESSES})")
    pdir, _sid, _ext, keep = _ADAPTERS[harness]
    d = pdir(cwd)
    if not d.is_dir():
        return []
    files = [p for p in d.rglob("*") if p.is_file() and keep(p)]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def end_steps(path: Path, harness: str = "cc") -> list[str]:
    """单 transcript → end step 文本列表 (per-harness 判定 + 共同门)。

    读取 errors="replace": 活跃会话尾部半写/坏字节不炸整命令 (与 endsteps
    「坏行静默跳过」哲学一致, 坏行 json.loads 失败自然被跳过)。
    """
    if harness not in _ADAPTERS:
        raise ValueError(f"unknown harness: {harness!r} (可用: {HARNESSES})")
    _pdir, _sid, extract, _keep = _ADAPTERS[harness]
    if harness == "dsh":
        lines = _dsh_open(path)
        try:
            return extract(lines)
        finally:
            if hasattr(lines, "close"):
                lines.close()
    with path.open(encoding="utf-8", errors="replace") as fh:
        return extract(fh)


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

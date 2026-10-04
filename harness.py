"""harness 注册表 (L1 脚手架层, 解耦 S1, 分支 iterate/harness-decouple)。

**harness 知识唯一存放地** — 每个 harness 的 transcript 定位/行 schema/
memory 投影目录/语料清洗键/spool 快照池/session env, 全部声明在 SPECS 一张
表里。条目**全部引用既有函数** (transcripts._ADAPTORS / _SCENES /
projection.*_memory_dir), 零重写; 身份等价由 tests/test_harness_registry.py
断言 (spec 字段 `is` 原函数), 防两处漂移。

分层铁律 (verdict 2026-10-04): 本模块只准 import 核心层 (transcripts/
projection/corpus_prep — 它们无 harness 编排概念), **禁止 import
runtime/cli/mem_daemon/hooks** (防环: 那些是上层消费者)。

能力语义: 字段为 None = 该 harness 无此能力 (如 pi/omp/codex 无 memory
投影约定 → 投影面响亮报错, 不静默落到别家目录)。

resolve_harness 白名单语义与 hooks/session-start-mem.sh 的 bash case
等价: 显式 "dsh" → dsh, 其余任何值 (空/None/"cc"/未知) → cc。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import corpus_prep
import projection
import transcripts

SVC_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class HarnessSpec:
    """单个 harness 的全部 IO 约定 (声明式, 全字段可被上层直接消费)。"""

    name: str
    # ── transcript 定位组 (cwd → 项目目录; 路径 → session id; 文件过滤器) ──
    project_dir: Callable[[str], Path]
    session_id: Callable[[Path], str]
    keep: Callable[[Path], bool]
    cwd_filter: Callable[[Path, str], bool] | None
    # ── 行 schema 组 (lines → 消费视图; zstd 明文化在 transcripts._dsh_open) ──
    end_steps: Callable
    scenes: Callable
    # ── memory 投影目录 (None = 无投影约定, 能力关闭) ──
    memory_dir: Callable[[str], Path] | None
    # ── 语料清洗键 (corpus_prep.HARNESS_RULES 的键; pi/omp 同规则不同键) ──
    corpus_key: str
    # ── spool 快照池 (hook 写端 env / 缺省目录; None = 无自动快照面) ──
    spool_env: str | None
    spool_default: Callable[[], Path] | None
    # ── 会话 id env (cli recall --session 缺省源; None = 无约定) ──
    session_env: str | None
    # ── 注入面 turn 计数 (S3 单源: transcripts.count_user_turns; None = 无注入面) ──
    count_user_turns: Callable | None = None
    # ── 快照回查 (补票 2026-10-04): payload transcript_path 空 + session_id 时按
    # harness 存储布局反查; None = 无回查能力 (cc 靠 payload 自带路径)。
    # (session_id, home) → Path | None (mtime 最新)。
    locate_transcript: Callable[[str, str], Path | None] | None = None


def _svc_spool() -> Path:
    return SVC_DIR / "data" / "transcript-spool"


def _dsh_spool() -> Path:
    # 沙箱钩子只能写 ~/.dsh (mem_daemon 同源: tonight-decisions.md #52)
    return Path.home() / ".dsh" / "memory-spool"


def _locate_dsh_transcript(session_id: str, home: str) -> Path | None:
    """dsh session_id → transcript (补票下沉 2026-10-04, 自 runtime.py 内联
    glob 搬家): 桥 payload 恒带空 transcript_path → 按 session_id 回查
    ~/.dsh/sessions/*/；多命中取 mtime 最新 (≡ ls -1t); 找不到 → None。"""
    cands = list((Path(home) / ".dsh" / "sessions").glob(
        f"*/{session_id}/session.jsonl.zstd"))
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def _mk(name: str, *, memory_dir=None, spool_env=None, spool_default=None,
        session_env=None, locate_transcript=None) -> HarnessSpec:
    """从 transcripts 既有表组装 (引用不重写) — 表结构变更由身份断言暴露。"""
    pdir, sid, esteps, keep = transcripts._ADAPTORS[name]
    return HarnessSpec(
        name=name,
        project_dir=pdir,
        session_id=sid,
        keep=keep,
        cwd_filter=transcripts._CWD_FILTERS.get(name),
        end_steps=esteps,
        scenes=transcripts._SCENES[name],
        memory_dir=memory_dir,
        corpus_key=name if name in corpus_prep.HARNESS_RULES else "cc",
        spool_env=spool_env,
        spool_default=spool_default,
        session_env=session_env,
        count_user_turns=(transcripts.count_user_turns
                          if name in ("cc", "dsh") else None),
        locate_transcript=locate_transcript,
    )


SPECS: dict[str, HarnessSpec] = {
    # cc: 唯一有完整自动面 (PreCompact 快照 + SessionStart 投影 +
    # UserPromptSubmit 注入) 的生产 harness。
    "cc": _mk("cc",
              memory_dir=projection.cc_memory_dir,
              spool_env="MEM_SPOOL_DIR", spool_default=_svc_spool,
              session_env="CLAUDE_CODE_SESSION_ID"),
    # dsh: 桥形 payload (CC 兼容), transcript zstd@~/.dsh/sessions,
    # 投影落 ~/.dsh/projects/<enc>/memory, spool 池固定 ~/.dsh/memory-spool。
    "dsh": _mk("dsh",
               memory_dir=projection.dsh_memory_dir,
               spool_env=None, spool_default=_dsh_spool,
               locate_transcript=_locate_dsh_transcript),
    # pi/omp/codex: 仅手动 cli ingest 面 (transcript 消费原语齐), 无
    # memory 投影约定 / 自动快照面 / session env — 能力=None 响亮可查。
    "pi": _mk("pi"),
    "omp": _mk("omp"),
    "codex": _mk("codex"),
}


def resolve_harness(value: str | None) -> str:
    """env/旗标值 → SPECS 键。白名单放行 dsh, 其余 (空/None/cc/未知) → cc
    (与 hooks/session-start-mem.sh bash case 等价 — 防任意旗标注入)。"""
    return value if value == "dsh" else "cc"


def memory_dir_or_raise(harness: str, cwd: str) -> Path:
    """投影目录解析 (能力=None → 响亮报错, 不静默落到别家)。"""
    spec = SPECS[harness]
    if spec.memory_dir is None:
        raise ValueError(
            f"harness {harness!r} 无 memory 投影约定 (能力关闭); "
            f"如需接入先在 harness.py SPECS 补 memory_dir 条目")
    return spec.memory_dir(cwd)

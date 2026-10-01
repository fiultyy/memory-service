"""mem-service autoDream daemon — persistent autodream loop (operational #1).

Currently mem-service is a pure-script CLI: autoDream fires once via PreCompact
hook (compact 前抢救). This module adds a **daemon** — a long-running process
that watches CC session transcripts for growth and incrementally dreams new
content into the KG without waiting for a compact.

Architecture (poll-based, no inotify dep):
  1. Poll ``~/.claude/projects/<encoded-cwd>/*.jsonl`` every POLL_INTERVAL s.
  2. Per-file byte-offset tracking (state file). On growth ≥ GROWTH_THRESHOLD:
     extract new **complete JSONL lines** (line-boundary aligned) → temp file.
  3. Feed temp file to ``autodream.autodream()`` (idempotent: ADD/UPDATE/DELETE/
     NOOP). autodream applies a per-segment character budget (M8 N4, replaces
     the old 4000-char flat truncation) — incremental feeding keeps each cycle
     small (full long-session dream stays PreCompact's job).
  4. Update offset, loop.

H1 第二 watch 源 (2026-10-01, spec v2 §二 docs/specs/graph-reform-v2-ingest-tags.md):
  5. Poll ``data/transcript-spool/*.jsonl`` (PreCompact hook 快照池, 旧
     spool-worker 通道已下线) — 按 assistant/user 轮切 segment → 逐段 sha256
     查 distill_seen 去重 → ``distill.distill_segment`` 段级消费 (线程池硬
     超时)。全段成功删 spool 文件; laya 不可用挂起积压 (H0); 毒段 3 败入
     DLQ。daemon 为 laya 唯一调用方 (经 distill)。

CC server-side flag ``tengu_onyx_plover`` gate:
  - **Closed** (current): CC may buffer transcripts in memory and not flush
    promptly → daemon may see stale/partial data or idle (no growth → no-op).
    File-watching still works (CC does write JSONL to disk eventually), just
    with latency. This is the accepted idle risk.
  - **Open** (future): CC actively pushes transcript paths to the trigger file
    (``STATE_DIR/trigger.json``). Daemon detects trigger, processes immediately,
    clears it. Lower latency, no polling lag.

State file: ``~/.local/share/mem-service/daemon-state.json``
  ``{"<abs_transcript_path>": {"offset": int, "session_id": str, "mtime": float}}``

Lifecycle: SIGTERM/SIGINT → flush state + exit 0. Idempotent by construction
(autodream is idempotent) → safe to kill/restart; re-run on same content = NOOP.
NEVER writes CC transcript files (read-only). SQLite WAL → concurrent with
PreCompact hook safe (both writers are idempotent autodream).

Usage::

    python3 mem-daemon.py                       # watch current cwd's transcripts
    python3 mem-daemon.py --cwd /home/yy/proj   # watch a specific project
    python3 mem-daemon.py --interval 60         # poll every 60s
    python3 mem-daemon.py --once                # single sweep (no loop)
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

import autodream as autodream_mod
import db

# ── Config ──────────────────────────────────────────────────────────

POLL_INTERVAL = 30          # seconds between transcript sweeps
GROWTH_THRESHOLD = 512      # bytes; skip tiny writes (keystroke-level noise)
STATE_DIR = Path(os.environ.get(
    "XDG_STATE_HOME", Path.home() / ".local" / "share")) / "mem-service"
STATE_FILE = STATE_DIR / "daemon-state.json"
TRIGGER_FILE = STATE_DIR / "trigger.json"       # CC flag-open push contract
PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# H1 spool 第二 watch 源 (spec v2 §二): 与 pre-compact-mem.sh 同款 env 注入口
# (B1-P2 惯例), 缺省生产行为不变。
_DEFAULT_SPOOL = Path(__file__).parent / "data" / "transcript-spool"
# 段级总帽: 主循环不可被单段饿死 (线程无法强杀, 超时后重建线程池, 僵尸
# 线程由 provider 自带 socket 超时兜底自行退出)。M1: 合法慢路径最坏
# ~370s (zhipu 120s×3 重试 + 退避/片间 sleeps) + 余量 — 240 会把合法慢段
# 误判超时, 与在途僵尸线程构成同段双执行双入图竞态, 提到 420。
SEGMENT_HARD_TIMEOUT = 420
_SEGMENT_ATTEMPTS_MAX = 3   # 段失败 (非 laya 挂起) 次数帽 → DLQ (毒段有界)
_USER_CTX_MAX = 1200        # 段内用户原话上下文字符帽 (旧 --scenes 同口径)

_RUNNING = True    # flipped False by signal handler for graceful shutdown


# ── Helpers ─────────────────────────────────────────────────────────

def _encode_cwd(cwd: str) -> str:
    """CC project dir encoding: ``/`` and ``.`` → ``-`` (cc-memory-bridge 已证)."""
    return cwd.replace("/", "-").replace(".", "-")


def _dream_providers() -> list:
    """T2 laya: daemon 路径判官注入, 与 cli.autodream 同构 (_laya_judge_prefix)。
    局部 import — cli 顶层跑 _load_env 且拉全量模块, daemon 启动不吃该副作用。"""
    from cli import _laya_judge_prefix
    return _laya_judge_prefix([])


def _log(msg: str) -> None:
    """stderr log with timestamp (stdout reserved for machine-readable output)."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    sys.stderr.write(f"[mem-daemon {ts}] {msg}\n")
    sys.stderr.flush()


def _load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    tmp.replace(STATE_FILE)     # atomic


def _extract_new_lines(path: Path, last_offset: int) -> tuple[str, int]:
    """Read new complete JSONL lines after ``last_offset``.

    Returns ``(text, new_offset)``. Aligns to line boundaries: if the chunk
    doesn't end at ``\\n`` the trailing partial line is dropped (it may be
    mid-write by CC). ``new_offset`` points past the last complete line so a
    re-poll after a partial write picks up the remainder.
    """
    size = path.stat().st_size
    if size < last_offset:
        return "", 0
    if size <= last_offset:
        return "", last_offset
    with path.open("rb") as f:
        f.seek(last_offset)
        chunk = f.read(size - last_offset)
    text = chunk.decode("utf-8", errors="replace")
    # Drop trailing partial line (no terminating \n = CC still writing it).
    if not text.endswith("\n"):
        idx = text.rfind("\n")
        if idx >= 0:
            text = text[:idx + 1]
        else:
            return "", last_offset       # single incomplete line → wait
    new_offset = last_offset + len(text.encode("utf-8"))
    return text, new_offset


def _transcript_dir(cwd: str) -> Path:
    """Resolve the CC projects subdir for a given cwd."""
    return PROJECTS_ROOT / _encode_cwd(cwd)


# ── H1: spool 第二 watch 源 (段级消费, spec v2 §二) ─────────────────

def _spool_dir() -> Path:
    """spool 快照池目录 (env MEM_SPOOL_DIR 注入, 与 hook/worker 同款)。"""
    return Path(os.environ.get("MEM_SPOOL_DIR", str(_DEFAULT_SPOOL)))


# dsh 侧 spool: 圈外蒸馏旧 timer(memory-spool-drain) 停用后由本 daemon 接管,
# 沙箱钩子只能写 ~/.dsh (tonight-decisions.md #52)。目录缺席则跳过。
_DSH_SPOOL = Path.home() / ".dsh" / "memory-spool"


def _spool_dirs() -> list[Path]:
    """全部 spool watch 源: CC 侧注入池 + dsh 侧沙箱池。"""
    dirs = [_spool_dir()]
    dsh = Path(os.environ.get("MEM_DSH_SPOOL_DIR", str(_DSH_SPOOL)))
    if dsh != dirs[0]:
        dirs.append(dsh)
    return dirs


def _spool_dlq_dir(spool: Path | None = None) -> Path:
    """毒段死信目录: spool 目录同级 `<spool>.dlq` (注入池同理隔离)。"""
    return Path(str(spool or _spool_dir()) + ".dlq")


def _spool_key(jf: Path) -> str:
    """state 键加 ``spool:`` 前缀 — 与 transcript offset 记录同文件不串键。"""
    return "spool:" + str(jf.resolve())


def _distill():
    """H2 distill 模块惰性 import (上游契约: src/distill.py; 兼容根目录落位)。
    未落位 → ImportError 上抛, 调用方 passive 跳过本轮 (仓内惯例)。"""
    try:
        import distill
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
        import distill
    return distill


def _sha_seen(sha: str) -> bool:
    """段 sha 是否已在 distill_seen (跨文件重放免疫, H2 契约列名 sha)。
    表未建 (H2/H5 未落位) / 查询异常 → False 被动放行 — 宁可重段不可丢段,
    幂等由 distill_segment 落 sha 兜底。"""
    try:
        row = db.get_conn().execute(
            "SELECT 1 FROM distill_seen WHERE sha = ? LIMIT 1", (sha,)).fetchone()
        return row is not None
    except Exception:
        return False


def _spool_session(jf: Path) -> str:
    """文件名 ``<session>-<sha16>.jsonl`` 剥尾段 sha16 (旧 worker 同规则)。"""
    base = jf.stem
    session = base.rsplit("-", 1)[0] if "-" in base else base
    return session or "unknown"


def _line_ts(d: dict, default_ts: str) -> str:
    """行内时戳透传 (CC ``timestamp`` / dsh ``ts``), 无则回退文件 mtime。"""
    for k in ("timestamp", "ts"):
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return default_ts


def _spool_segments(lines: list[str], default_ts: str,
                    default_cwd: str) -> list[tuple[str, int, str, str]]:
    """transcript 快照行 → ``(段文本, 段尾字节偏移, ts, cwd)`` 列表 (段级切分)。

    双格式自动识别 (``endsteps._looks_like_dsh``):
    - CC: user 文本累积, ``assistant`` 且 ``stop_reason=end_turn`` 收段
      (endsteps 同判据; isSidechain 侧链行排除);
    - dsh: ``user/message`` 累积 + ``turn/end reason=completed`` 收段
      (delegationDepth>0 侧链整文件排除, transcripts 同口径)。
    段文本 = ``[用户] …\\n[助手] …`` (用户上下文截尾 ≤ _USER_CTX_MAX);
    纯用户尾部无 assistant 结论不收段 — 零段 = 快照消费成功 (旧 worker
    「空蒸馏即成功删文件」同语义)。偏移按行字节累计, 段边界即行边界。

    M5 段级 cwd: CC transcript 行内带顶层 ``cwd`` 字段 → 每段取收段前最后
    见到的行内 cwd (repo: tag 铸币用真实工作目录, 不再误铸 daemon 启动
    目录); 无行内 cwd (dsh 事件流 / 旧行) → ``default_cwd`` 兜底 (watch
    目录; .harness sidecar 只记 harness 名不含路径)。
    """
    import endsteps                       # 延迟 import (daemon 启动零副作用)
    from transcripts import _texts_of
    out: list[tuple[str, int, str, str]] = []
    user_buf: list[str] = []
    sidechain = False
    last_assistant: str | None = None
    last_assistant_ts = default_ts
    cur_cwd = default_cwd
    dsh = endsteps._looks_like_dsh(lines)

    def _close(atxt: str, ts: str, end_pos: int) -> None:
        uctx = "\n".join(user_buf)[-_USER_CTX_MAX:]
        seg = (f"[用户] {uctx}\n" if uctx else "") + f"[助手] {atxt}"
        out.append((seg, end_pos, ts, cur_cwd))
        user_buf.clear()

    pos = 0
    for line in lines:
        pos += len(line.encode("utf-8")) + 1     # +1 = 换行符
        try:
            d = json.loads(line)
        except Exception:
            continue                              # 坏行静默跳过 (尾部半行常见)
        if not isinstance(d, dict):
            continue
        c = d.get("cwd")
        if isinstance(c, str) and c.startswith("/"):
            cur_cwd = c                           # M5: 行内真实 cwd (CC 常带)
        if dsh:
            t = d.get("type")
            if t == "session":
                sidechain = bool(d.get("delegationDepth", 0))
            elif t == "user/message":
                txt = _texts_of(((d.get("data") or {}).get("message") or {})
                                .get("content"))
                if txt:
                    user_buf.append(txt)
            elif t == "assistant/message":
                txt = _texts_of(((d.get("data") or {}).get("message") or {})
                                .get("content"))
                if txt:
                    last_assistant = txt
                    last_assistant_ts = _line_ts(d, default_ts)
            elif t == "turn/end":
                reason = (d.get("data") or {}).get("reason") or {}
                if isinstance(reason, dict):      # 容错裸串/异形 (B1-P2 同款)
                    reason = reason.get("kind")
                if reason == "completed" and last_assistant:
                    _close(last_assistant, last_assistant_ts, pos)
                last_assistant = None
        else:
            if d.get("isSidechain"):
                continue
            msg = d.get("message") or {}
            t = d.get("type")
            if t == "user":
                txt = _texts_of(msg.get("content"))
                if txt:
                    user_buf.append(txt)
            elif t == "assistant" and msg.get("stop_reason") == "end_turn":
                txt = _texts_of(msg.get("content"))
                if txt:
                    _close(txt, _line_ts(d, default_ts), pos)
    if sidechain:
        return []
    return out


_DISTILL_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="mem-distill")

# M1 在途登记: (spool 文件绝对路径, 段 sha) → 硬超时后仍在跑的僵尸 future。
# 重试前查 done(): 未结束则本轮跳过不计 attempt — 防同段双执行双入图
# (僵尸与重试并发各自过 sha 查重 → 双 INSERT); 僵尸自行退出后若已提交,
# _sha_seen 兜底去重。
_INFLIGHT: dict[tuple[str, str], concurrent.futures.Future] = {}

# 挂账#1 tag 面目标库: run() 启动时显式登记 (db._conn_path), _h7_distill_sweep
# 只认它 — 不从 db._conn 环境态推断 (对抗审查 blocker: hygiene/distill 补扫
# 会先绑生产连接, 裸测试上下文单测选择即击穿推断守卫, 44 行生产写入事故)。
_DAEMON_DB: str | None = None


def _distill_segment_hard(distill_mod, seg_text: str, session_id: str,
                          cwd: str, ts: str,
                          inflight_key: tuple[str, str] | None = None) -> dict:
    """线程池 + 硬超时执行 ``distill.distill_segment`` (H0: 不阻塞主循环)。

    线程不可强杀 — 超时后 ``shutdown(wait=False)`` 并重建线程池, 使后续段
    不排在僵尸调用之后 (daemon 不被单调用饿死); 僵尸线程由 provider 自带
    socket 超时兜底自行退出。callable 异常原样透传 (LayaUnavailable 语义
    由调用方裁决)。超时的 future 经 ``inflight_key`` 登记 _INFLIGHT (M1)。"""
    global _DISTILL_POOL
    fut = _DISTILL_POOL.submit(distill_mod.distill_segment, seg_text,
                               session_id, cwd, ts)
    try:
        return fut.result(timeout=SEGMENT_HARD_TIMEOUT)
    except concurrent.futures.TimeoutError:
        if inflight_key is not None:
            _INFLIGHT[inflight_key] = fut
        _DISTILL_POOL.shutdown(wait=False)
        _DISTILL_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mem-distill")
        raise


def _spool_recover_locks(spool: Path) -> None:
    """旧 worker 处理中 ``.lock`` 复活 (通道退役后无人回收, 不救即静默丢记忆)。"""
    for lk in spool.glob("*.jsonl.lock"):
        try:
            lk.rename(lk.with_name(lk.name[: -len(".lock")]))
        except OSError:
            pass


def _to_dlq(jf: Path) -> bool:
    """毒段文件 (含 .harness sidecar) 移 DLQ; 失败 False 留待下轮再试。"""
    dlq = _spool_dlq_dir(jf.parent)
    try:
        dlq.mkdir(parents=True, exist_ok=True)
        sidecar = jf.with_name(jf.name + ".harness")
        if sidecar.is_file():
            shutil.move(str(sidecar), str(dlq / sidecar.name))
        shutil.move(str(jf), str(dlq / jf.name))
        return True
    except OSError as exc:
        _log(f"ERROR DLQ 移动失败 {jf.name}: {exc} (下轮重试)")
        return False


def _sweep_spool(state: dict, cwd: str, spool_dir: Path | None = None) -> dict:
    """H1 spool 消费轮: 新快照 → 轮切 segment → sha 去重 → distill 入图。

    段级 ack: offset 推进到最后成功段尾 (绝对偏移 = 本轮 base offset +
    段尾相对偏移, m1; state 持久化, 崩溃安全)。
    - 全段成功 (或全已见/零段) → 删 spool 文件 + sidecar (旧 worker 语义,
      仅成功后);
    - ``LayaUnavailable`` → 挂起: offset 停在 ack, 不计失败次数, 下轮重试
      (H0: laya 不可用 spool 积压持有, 恢复后补审, 不裸入图);
    - 其余段失败 (含硬超时) → 文件级 attempts+1, 达 _SEGMENT_ATTEMPTS_MAX
      移 ``<spool>.dlq/`` + 日志 (毒段有界)。硬超时段登记 _INFLIGHT —
      僵尸线程未退出期间重试轮跳过不计 attempt (M1, 防双执行双入图)。
    """
    spool = spool_dir or _spool_dir()
    if not spool.is_dir():
        return state
    _spool_recover_locks(spool)
    try:
        distill_mod = _distill()
    except ImportError:
        _log("distill 模块未落位 (H2 lane), spool 消费本轮跳过")
        return state

    for jf in sorted(spool.glob("*.jsonl")):
        key = _spool_key(jf)
        try:
            st = jf.stat()
        except OSError:
            state.pop(key, None)
            continue
        rec = state.get(key) or {}
        offset = rec.get("offset", 0)
        attempts = rec.get("attempts", 0)
        text, new_offset = _extract_new_lines(jf, offset)
        if not text and new_offset == offset and st.st_size > offset:
            # 快照无尾换行 (hook 原样 cp) → 尾行视为完整行: 快照不可变,
            # 无 CC 在写半行风险; 不处理则该文件永滞 spool。
            with jf.open("rb") as f:
                f.seek(offset)
                text = f.read(st.st_size - offset).decode(
                    "utf-8", errors="replace")
            new_offset = st.st_size
        if not text:
            continue
        session_id = _spool_session(jf)
        default_ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                   time.gmtime(st.st_mtime))

        ack = offset
        outcome = "done"          # done | suspend | retry | dlq
        for seg_text, seg_end, ts, seg_cwd in _spool_segments(
                text.splitlines(), default_ts, cwd):
            sha = hashlib.sha256(seg_text.encode("utf-8")).hexdigest()
            ikey = (str(jf.resolve()), sha)
            zfut = _INFLIGHT.get(ikey)
            if zfut is not None and not zfut.done():
                # M1 在途守卫: 上轮硬超时僵尸线程仍跑同段 — 重试即双执行
                # 双入图, 本轮跳过不计 attempt (僵尸退出后 _sha_seen 兜底)。
                outcome = "retry"
                _log(f"段 {jf.name} sha={sha[:12]} 上轮超时线程仍在途, "
                     f"本轮跳过不计 attempt")
                break
            if zfut is not None:
                _INFLIGHT.pop(ikey, None)
            if _sha_seen(sha):    # 跨文件/跨重放已见 → 零 LLM 跳过
                ack = offset + seg_end     # m1: seg_end 是本轮相对值, ack 须绝对
                continue
            try:
                res = _distill_segment_hard(distill_mod, seg_text,
                                            session_id, seg_cwd, ts,
                                            inflight_key=ikey)
            except distill_mod.LayaUnavailable:
                outcome = "suspend"
                _log(f"laya 不可用: {jf.name} 段 sha={sha[:12]} 挂起, "
                     f"offset 保持 {ack} 下轮重试 (spool 积压持有)")
                break
            except Exception as exc:
                attempts += 1
                _log(f"ERROR distill 段失败 {jf.name} sha={sha[:12]}: {exc} "
                     f"(attempt {attempts}/{_SEGMENT_ATTEMPTS_MAX})")
                outcome = ("dlq" if attempts >= _SEGMENT_ATTEMPTS_MAX
                           else "retry")
                break
            if isinstance(res, dict):   # 契约 dict; 畸形返回也视为成功推进
                _log(f"distill {jf.name} sha={sha[:12]} → "
                     f"atoms={res.get('atoms')} edges={res.get('edges')} "
                     f"merged={res.get('merged')}")
            else:
                _log(f"distill {jf.name} sha={sha[:12]} → 畸形返回 {res!r}")
            ack = offset + seg_end     # m1: 绝对偏移 (base + 本轮相对段尾)

        if outcome == "done":
            # 仅成功后删文件 (+ sidecar); .endsteps 为旧 worker 中间物一并清。
            for suffix in ("", ".harness", ".endsteps"):
                try:
                    jf.with_name(jf.name + suffix).unlink(missing_ok=True)
                except OSError:
                    pass
            state.pop(key, None)
            for k in [k for k in _INFLIGHT if k[0] == str(jf.resolve())]:
                _INFLIGHT.pop(k, None)    # M1: 文件消费完清在途登记
            _log(f"spool 消费完成: {jf.name} (ack {ack}/{new_offset})")
        elif outcome == "dlq" and _to_dlq(jf):
            state.pop(key, None)
            for k in [k for k in _INFLIGHT if k[0] == str(jf.resolve())]:
                _INFLIGHT.pop(k, None)    # M1: DLQ 同清 (防僵尸完成后误残留)
            _log(f"DLQ: {jf.name} → {_spool_dlq_dir(jf.parent)} "
                 f"(max-attempts {_SEGMENT_ATTEMPTS_MAX} 耗尽)")
        else:      # suspend / retry / dlq 移动失败 → 留存下轮
            state[key] = {"offset": ack, "attempts": attempts,
                          "session_id": session_id, "mtime": st.st_mtime}
    return state


# ── Core sweep ──────────────────────────────────────────────────────

def _sweep(tdir: Path, cwd: str, state: dict) -> dict:
    """One poll cycle: scan transcript dir, dream new content, update state."""
    if not tdir.is_dir():
        _log(f"transcript dir not found: {tdir} (idle, no sessions yet)")
        return state

    for jf in sorted(tdir.glob("*.jsonl")):
        key = str(jf.resolve())
        try:
            st = jf.stat()
        except OSError:
            continue
        rec = state.get(key, {})
        offset = rec.get("offset", 0)
        # session_id = filename stem (CC convention: <session-uuid>.jsonl).
        session_id = jf.stem
        new_text, new_offset = _extract_new_lines(jf, offset)
        growth = new_offset - offset
        if growth < GROWTH_THRESHOLD:
            # File may have shrunk (_extract_new_lines reset offset to 0) —
            # persist so next sweep starts from 0 instead of stalling on a
            # stale offset larger than the new file size.
            if new_offset < offset:
                state[key] = {
                    "offset": new_offset, "session_id": session_id,
                    "mtime": st.st_mtime,
                }
            continue    # not enough new content yet

        # Write new lines to a temp JSONL → feed to autodream (expects a path).
        # autodream._read_transcript parses each line as JSON, filters user/
        # assistant text blocks → partial transcript is safe (extracts whatever
        # text is in the new records).
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(new_text)
                tmp_path = tmp.name
            result = autodream_mod.autodream(
                session_id, tmp_path, source_cwd=cwd,
                providers=_dream_providers())
            _log(
                f"dream {jf.name} +{growth}B → "
                f"add={result['added']} upd={result['updated']} "
                f"del={result['deleted']} noop={result['noop']}"
            )
        except Exception as exc:
            # LLM unreachable / extract failure → don't advance offset
            # (retry next cycle). NEVER crash the daemon on a single failure.
            _log(f"ERROR dreaming {jf.name}: {exc} (offset not advanced, retry next cycle)")
            continue
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        state[key] = {
            "offset": new_offset, "session_id": session_id,
            "mtime": st.st_mtime,
        }

    return state


def _check_trigger(state: dict, cwd: str) -> dict:
    """CC flag-open push contract: trigger.json feeds a session to dream now.

    When CC server-side flag ``tengu_onyx_plover`` opens, CC (or a wrapper)
    writes ``{"session_id": "...", "transcript_path": "...", "cwd": "..."}``
    to TRIGGER_FILE. Daemon detects, dreams immediately, clears trigger.
    Until the flag opens this file never appears → no-op (pure file-watch mode).
    """
    if not TRIGGER_FILE.is_file():
        return state
    try:
        trig = json.loads(TRIGGER_FILE.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        trig = {}
    tpath = trig.get("transcript_path", "")
    sid = trig.get("session_id", "unknown")
    tcwd = trig.get("cwd", cwd)
    if tpath and Path(tpath).is_file():
        try:
            result = autodream_mod.autodream(
                sid, tpath, source_cwd=tcwd, providers=_dream_providers())
            _log(
                f"trigger dream {Path(tpath).name} → "
                f"add={result['added']} upd={result['updated']} "
                f"del={result['deleted']} noop={result['noop']}"
            )
        except Exception as exc:
            _log(f"ERROR trigger dream {tpath}: {exc}")
    # Clear trigger (consumed, regardless of success — avoid retry storm).
    try:
        TRIGGER_FILE.unlink()
    except OSError:
        pass
    return state


# ── Daemon loop ─────────────────────────────────────────────────────

# M11 (DR-8 G8 已裁决: 扩展 mem_daemon 主循环作 dreaming 载体): 距上次
# dreaming ≥ _DREAM_INTERVAL 才触发 dream.run_cycle() 六职责; 常量可调。
_DREAM_INTERVAL = 86400  # s (≤1d cron 语义, spec M11)

# M12 投影卫生 (零 LLM 三动作) 独立门控: 比 dream 高频 ([设] 可调)。
# 时序铁律: KG 维护完成后才跑 — dream 到期的轮次卫生紧随同轮 (见 _maybe_dream),
# 未到期时卫生按自身门控独立跑。
_HYGIENE_INTERVAL = 3600  # s


def _run_hygiene(state: dict, cwd: str) -> dict:
    """M12 卫生轮: 异常不杀 daemon (try/except 记日志继续), last_run 恒推进。"""
    try:
        import hygiene
        import projection
        stats = hygiene.run(cwd, projection.cc_memory_dir(cwd))
        _log(f"hygiene cycle → {stats}")
    except Exception as exc:
        _log(f"ERROR hygiene cycle: {exc} (continuing)")
    state["_hygiene"] = {"last_run": time.time()}
    return state


def _maybe_dream(state: dict, cwd: str) -> dict:
    """Dreaming 阶段门控: 到期才跑, 单轮异常不杀 daemon (try/except 记日志继续)。
    水位存 state["_dreaming"]["last_run"] (epoch s) — 与 transcript offset 同文件。
    M12: dream 已跑 → 卫生同轮紧随 (KG 维护完成后才跑, 防复活); dream 未到期 →
    卫生按 _HYGIENE_INTERVAL 独立门控。"""
    import dream  # 惰性 import (dream 拉起 adapter/embedding 全链)
    last = (state.get("_dreaming") or {}).get("last_run", 0)
    if time.time() - last < _DREAM_INTERVAL:
        h_last = (state.get("_hygiene") or {}).get("last_run", 0)
        if time.time() - h_last >= _HYGIENE_INTERVAL:
            state = _run_hygiene(state, cwd)
        return state
    try:
        # source_cwd=None: 全局单体 KG (ADR-14), 接管已停用 memory-dream.timer
        # 的全局信号消费口径 (2026-10-01 挂账收口)。
        stats = dream.run_cycle(source_cwd=None)
        _log(f"dream cycle → {stats}")
    except Exception as exc:
        _log(f"ERROR dream cycle: {exc} (continuing)")
    _h7_distill_sweep()   # H7 补扫: needs_audit 补审 + needs_embed 补向量
    state["_dreaming"] = {"last_run": time.time()}
    return _run_hygiene(state, cwd)  # M12 时序铁律: KG 维护后紧随同轮


def _h7_distill_sweep() -> None:
    """H7 夜间补扫: ``distill.audit_pending`` (laya 恢复后补审挂起 atom) +
    ``distill.reembed_needing`` (embed 失败补向量) + v2 挂账#1 tag 面
    (``tag_dream.audit_mounts`` 挂载审计 / ``mount_new_atoms`` laya 竞争挂载)。

    tag 面目标库 = ``_DAEMON_DB`` (run() 启动显式登记), **不从 ``db._conn``
    环境态推断** — 对抗审查 blocker: 同函数更早路径 (_run_hygiene / distill
    补扫自身) 会先 get_conn() 绑生产连接, 裸测试上下文单测选择即击穿守卫
    把 cos-挂载写进生产库 (实测 44 行事故, 2026-10-01)。audit 先于 mount:
    本轮新挂载不重评 (下轮再审), 限 mount(回落 cos)→audit(删)→再 mount 振荡。
    passive — 模块未落位/异常均记日志不杀轮 (仓内惯例)。"""
    try:
        d = _distill()
        _log(f"distill audit_pending → {d.audit_pending()}")
        _log(f"distill reembed_needing → {d.reembed_needing()}")
    except Exception as exc:
        _log(f"ERROR distill 补扫: {exc} (continuing)")
    if _DAEMON_DB is None:
        return  # 未經 run() 登记 (裸调用/测试上下文) — tag 面不猜目标库
    # v2 挂账#1: tag 挂载/审计补扫 — 独立 try/except (distill 补扫同款 passive)
    try:
        try:
            import tag_dream
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
            import tag_dream
        _log(f"tag audit_mounts → {tag_dream.audit_mounts(_DAEMON_DB)}")
        _log(f"tag mount_new_atoms → {tag_dream.mount_new_atoms(_DAEMON_DB)}")
    except Exception as exc:
        _log(f"ERROR tag 补扫: {exc} (continuing)")


def run(cwd: str | None = None, interval: int = POLL_INTERVAL, once: bool = False) -> int:
    """Run the daemon loop. Returns 0 (always — daemon is best-effort)."""
    watch_cwd = cwd or os.getcwd()
    tdir = _transcript_dir(watch_cwd)
    db.get_conn()  # ensure schema initialised
    global _DAEMON_DB   # tag 面目标库登记 (挂账#1, 见 _h7_distill_sweep)
    _DAEMON_DB = db._conn_path
    _log(
        f"start: cwd={watch_cwd} dir={tdir} interval={interval}s "
        f"once={once} flag={'tengu_onyx_plover CLOSED (file-watch mode)'}"
    )

    def _shutdown(signum, frame):
        global _RUNNING
        _RUNNING = False
        _log(f"signal {signum} → graceful shutdown")

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # ── First-run init: anchor offsets to current file sizes (skip history).
    # Existing transcripts were handled by PreCompact autodream or past runs.
    # Daemon only processes **new** content from this launch forward.
    # `state` empty → no prior state → all files are new to the daemon.
    # For each existing transcript, set offset=current size so we only see
    # growth **after** this point. Newly created files (future sessions) start
    # at offset 0 naturally.
    state = _load_state()
    if not state and tdir.is_dir():
        for jf in tdir.glob("*.jsonl"):
            try:
                sz = jf.stat().st_size
            except OSError:
                continue
            state[str(jf.resolve())] = {
                "offset": sz, "session_id": jf.stem, "mtime": jf.stat().st_mtime,
            }
        _log(f"init: anchored {len(state)} existing transcripts (skip history)")
        _save_state(state)

    while _RUNNING:
        state = _load_state()
        try:
            state = _check_trigger(state, watch_cwd)
            # B1 (spec v2 §二 H1 切换票): 旧 transcript→autodream 写通道
            # 下线 — 新写通道 = _sweep_spool 段级蒸馏。MEM_LEGACY_TRANSCRIPT_DREAM
            # 逃生口缺省 0; 图改造观察期后本块连同 _sweep 一并删除。
            if os.environ.get("MEM_LEGACY_TRANSCRIPT_DREAM", "0") == "1":
                state = _sweep(tdir, watch_cwd, state)
            for _sd in _spool_dirs():                # H1: CC 池 + dsh 池双 watch
                state = _sweep_spool(state, watch_cwd, _sd)
            state = _maybe_dream(state, watch_cwd)  # M11: dreaming 阶段门控
            _save_state(state)
        except Exception as exc:
            _log(f"ERROR sweep cycle: {exc} (continuing)")
        if once:
            break
        # Interruptible sleep (signal handler flips _RUNNING during sleep).
        for _ in range(interval):
            if not _RUNNING:
                break
            time.sleep(1)

    _save_state(_load_state())     # final flush
    _log("stopped (state flushed)")
    return 0


if __name__ == "__main__":
    import argparse
    # m2: import cli 触发 _load_env (.env 注入 ZHIPU_API_KEY/MEM_LAYA_
    # ENABLED) — 裸启动无 key 时 distill 走挂起 lane 而非毒段误删记忆。
    import cli  # noqa: F401
    p = argparse.ArgumentParser(
        prog="mem-daemon",
        description="mem-service autoDream daemon (operational #1)")
    p.add_argument("--cwd", default=None, help="project cwd to watch (default $PWD)")
    p.add_argument("--interval", type=int, default=POLL_INTERVAL,
                   help=f"poll interval seconds (default {POLL_INTERVAL})")
    p.add_argument("--once", action="store_true",
                   help="single sweep, no loop (smoke test / cron mode)")
    args = p.parse_args()
    sys.exit(run(cwd=args.cwd, interval=args.interval, once=args.once))

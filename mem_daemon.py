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

import json
import os
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

_RUNNING = True    # flipped False by signal handler for graceful shutdown


# ── Helpers ─────────────────────────────────────────────────────────

def _encode_cwd(cwd: str) -> str:
    """CC project dir encoding: ``/`` and ``.`` → ``-`` (cc-memory-bridge 已证)."""
    return cwd.replace("/", "-").replace(".", "-")


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
                session_id, tmp_path, source_cwd=cwd)
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
            result = autodream_mod.autodream(sid, tpath, source_cwd=tcwd)
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

def run(cwd: str | None = None, interval: int = POLL_INTERVAL, once: bool = False) -> int:
    """Run the daemon loop. Returns 0 (always — daemon is best-effort)."""
    watch_cwd = cwd or os.getcwd()
    tdir = _transcript_dir(watch_cwd)
    db.get_conn()  # ensure schema initialised
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
            state = _sweep(tdir, watch_cwd, state)
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

"""PreCompact spool 链回归 (MF3, 2026-09-02): 快照 → sidecar → worker 排干。

四条覆盖 (派单验收):
1. 快照落盘 — pre-compact-mem.sh 快照进 MEM_SPOOL_DIR 注入池 + `.harness`
   sidecar 记来源 (MEM_HARNESS=cc 显式 / 缺省回落 cc);
2. 重放幂等 — 同 transcript 重复快照落同名文件 (原子覆盖, 零重复);
3. cid 回退 sha16 — dsh 桥 zstd 分支: compaction_id → cid12 命名, 缺 →
   内容 sha256 前 16 位回退;
4. 失败路径有日志 — worker filter-fail / retry-later 均 log + 文件保留可重试。

红线: 全程 tmp 注入池 (MEM_SPOOL_DIR) + python3 shim 硬拦 autodream —
生产 db 零触碰, 零 LLM 调用; 生产 spool 内容断言前后不变。
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess

REPO = pathlib.Path(__file__).parent
HOOK_PRECOMPACT = REPO / "hooks" / "pre-compact-mem.sh"
HOOK_WORKER = REPO / "hooks" / "spool-worker.sh"
PROD_SPOOL = REPO / "data" / "transcript-spool"


def _base_env(spool: pathlib.Path, extra: dict | None = None) -> dict:
    """钩子环境: 注入池 + 禁 worker; 剥离环境残留 MEM_HARNESS 保断言确定。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("MEM_HARNESS", "MEM_SPOOL_DIR", "MEM_SPOOL_WORKER")}
    env["MEM_SPOOL_DIR"] = str(spool)
    env["MEM_SPOOL_WORKER"] = "0"  # 快照测试: 只快照不蒸馏 (零 LLM 零 db)
    env.update(extra or {})
    return env


def _run_hook(spool: pathlib.Path, transcript: pathlib.Path, session: str,
              tmp_path: pathlib.Path, extra_env: dict | None = None,
              compaction_id: str | None = None) -> subprocess.CompletedProcess:
    payload = {"transcript_path": str(transcript), "session_id": session,
               "cwd": str(tmp_path)}
    if compaction_id is not None:
        payload["compaction_id"] = compaction_id
    return subprocess.run(
        ["bash", str(HOOK_PRECOMPACT)], input=json.dumps(payload),
        capture_output=True, text=True,
        env=_base_env(spool, extra_env), timeout=60)


def _cc_line(type_: str, **msg) -> str:
    return json.dumps({"type": type_, "message": msg})


def _end_turn_transcript(tmp_path: pathlib.Path, name: str,
                         chars: int = 200) -> pathlib.Path:
    """带合格 end step 的 CC transcript (≥120 字门, stop_reason=end_turn)。"""
    t = tmp_path / name
    text = "结论: " + "记忆管线回归覆盖验证。" * 40
    assert len(text) >= chars
    lines = [
        _cc_line("user", role="user", content=[{"type": "text",
                                                "text": "帮我总结这轮会话结论"}]),
        _cc_line("assistant", role="assistant", stop_reason="end_turn",
                 content=[{"type": "text", "text": text}]),
    ]
    t.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return t


def _tool_only_transcript(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    """纯工具会话 (无 end_turn 长文) — 蒸馏必空 → worker 零 LLM 排干。"""
    t = tmp_path / name
    lines = [
        _cc_line("user", role="user", content=[{"type": "tool_result",
                                                "content": "ok"}]),
        _cc_line("assistant", role="assistant", stop_reason="tool_use",
                 content=[{"type": "tool_use", "id": "t1", "name": "Bash",
                           "input": {"command": "ls"}}]),
    ]
    t.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return t


def _shim_bin(tmp_path: pathlib.Path, fail_pattern: str) -> pathlib.Path:
    """python3 shim: 命中 fail_pattern 的调用记日志后非零退出 (硬拦 autodream/
    endsteps), 其余透传真 python3 — 保 worker 其余路径真实可跑且零副作用。"""
    import sys
    bindir = tmp_path / f"shim-{fail_pattern}"
    bindir.mkdir()
    real = shutil.which("python3") or sys.executable
    log = bindir / "shim.log"
    shim = bindir / "python3"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'args="$*"\n'
        f'case "${{args}}" in\n'
        f'  *{fail_pattern}*)\n'
        f"    printf '%s\\n' \"${{args}}\" >> \"{log}\"\n"
        "    exit 90 ;;\n"
        "esac\n"
        f'exec "{real}" "$@"\n',
        encoding="utf-8")
    shim.chmod(0o755)
    return bindir


def _worker_env(spool: pathlib.Path, shim: pathlib.Path,
                extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("MEM_HARNESS", "MEM_SPOOL_DIR")}
    env["MEM_SPOOL_DIR"] = str(spool)
    env["PATH"] = f"{shim}:{os.environ['PATH']}"
    env.update(extra or {})
    return env


def _spool_jsonl(spool: pathlib.Path) -> list[str]:
    return sorted(p.name for p in spool.glob("*.jsonl"))


def _sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


# ── 1. 快照落盘 + sidecar ──────────────────────────────────────────

def test_snapshot_writes_spool_and_cc_sidecar(tmp_path):
    spool = tmp_path / "spool"
    t = _tool_only_transcript(tmp_path, "t1.jsonl")
    r = _run_hook(spool, t, "sess-snap", tmp_path,
                  extra_env={"MEM_HARNESS": "cc"})
    assert r.returncode == 0, r.stderr
    name = f"sess-snap-{_sha16(t.read_bytes())}.jsonl"
    snap = spool / name
    assert snap.is_file(), f"快照未落盘: {_spool_jsonl(spool)}"
    assert snap.read_bytes() == t.read_bytes()
    # harness sidecar: MEM_HARNESS=cc 显式声明 → "cc" (CC 侧事实不再 stamp NULL)
    assert (spool / (name + ".harness")).read_text() == "cc"
    # 无 .tmp 半成品残留
    assert not list(spool.glob("*.tmp"))


def test_snapshot_sidecar_default_cc_without_env(tmp_path):
    """缺省 (无 MEM_HARNESS) → sidecar 回落 "cc", 与历史行为一致。"""
    spool = tmp_path / "spool"
    t = _tool_only_transcript(tmp_path, "t2.jsonl")
    r = _run_hook(spool, t, "sess-def", tmp_path)
    assert r.returncode == 0, r.stderr
    name = f"sess-def-{_sha16(t.read_bytes())}.jsonl"
    assert (spool / (name + ".harness")).read_text() == "cc"


# ── 2. 重放幂等 ────────────────────────────────────────────────────

def test_snapshot_replay_idempotent(tmp_path):
    spool = tmp_path / "spool"
    t = _tool_only_transcript(tmp_path, "t3.jsonl")
    for _ in range(2):  # 同 compaction 重放两次
        r = _run_hook(spool, t, "sess-replay", tmp_path)
        assert r.returncode == 0, r.stderr
    assert _spool_jsonl(spool) == [f"sess-replay-{_sha16(t.read_bytes())}.jsonl"]
    snap = spool / _spool_jsonl(spool)[0]
    assert snap.read_bytes() == t.read_bytes()  # 原子覆盖, 内容不坏
    assert len(list(spool.glob("*.harness"))) == 1  # sidecar 同名不翻倍


# ── 3. dsh 桥 zstd 分支: cid12 命名 + sha16 回退 ───────────────────

def _zstd_file(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    plain = tmp_path / (name + ".plain")
    plain.write_text("dsh 事件流占位内容\n" * 50, encoding="utf-8")
    z = tmp_path / (name + ".zstd")
    subprocess.run(["zstd", "-q", "-f", "-o", str(z), str(plain)],
                   check=True, timeout=30)
    return z


def test_snapshot_zstd_cid12_named(tmp_path):
    spool = tmp_path / "spool"
    z = _zstd_file(tmp_path, "dsh1")
    cid = "MF3ABCD-1234-5678"  # tr -d '-' | cut -c1-12 → MF3ABCD12345
    r = _run_hook(spool, z, "sess-zstd", tmp_path,
                  extra_env={"MEM_HARNESS": "dsh"}, compaction_id=cid)
    assert r.returncode == 0, r.stderr
    snap = spool / "sess-zstd-MF3ABCD12345.jsonl"
    assert snap.is_file(), f"cid12 命名失败: {_spool_jsonl(spool)}"
    out = subprocess.run(["zstdcat", str(z)], capture_output=True,
                         check=True, timeout=30).stdout
    assert snap.read_bytes() == out  # 解压明文化落盘
    assert (spool / "sess-zstd-MF3ABCD12345.jsonl.harness").read_text() == "dsh"


def test_snapshot_zstd_fallback_sha16(tmp_path):
    spool = tmp_path / "spool"
    z = _zstd_file(tmp_path, "dsh2")
    r = _run_hook(spool, z, "sess-fb", tmp_path, compaction_id="")
    assert r.returncode == 0, r.stderr
    name = f"sess-fb-{_sha16(z.read_bytes())}.jsonl"  # 对 .zstd 字节取 sha16
    assert (spool / name).is_file(), f"sha16 回退失败: {_spool_jsonl(spool)}"


# ── 4. worker: 注入池排干 + 生产池零触碰 ───────────────────────────

def test_worker_drains_injected_spool_production_untouched(tmp_path):
    before = _spool_jsonl(PROD_SPOOL) if PROD_SPOOL.is_dir() else []
    spool = tmp_path / "spool"
    spool.mkdir()
    t = _tool_only_transcript(tmp_path, "t4.jsonl")
    shutil.copy(t, spool / "sess-drain-beefbeefdeadbeef.jsonl")
    shim = _shim_bin(tmp_path, "autodream")  # 保险带: 即便误达也硬拦
    r = subprocess.run(["bash", str(HOOK_WORKER)], capture_output=True,
                       text=True, env=_worker_env(spool, shim), timeout=120)
    assert r.returncode == 0, r.stderr
    assert _spool_jsonl(spool) == [], "注入池未排干"
    log = (spool / "worker.log").read_text()
    assert "no-end-steps" in log and "sess-drain" in log
    assert not (tmp_path / "shim-autodream" / "shim.log").exists(), \
        "纯工具会话不该到 autodream"
    # 生产池零触碰 (fix ① 前此处恒被误排)
    after = _spool_jsonl(PROD_SPOOL) if PROD_SPOOL.is_dir() else []
    assert after == before


def test_worker_default_fallback_declared():
    """缺省回落逐字不变 (grep 可证): 无 MEM_SPOOL_DIR → 生产路径。"""
    line = 'SPOOL="${MEM_SPOOL_DIR:-${SVC_DIR}/data/transcript-spool}"'
    assert line in HOOK_WORKER.read_text(encoding="utf-8")


# ── 5. worker 失败路径有日志 + sidecar 转发 ────────────────────────

def test_worker_retry_later_logs_and_forwards_sidecar(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    raw = spool / "sess-side-cafecafecafecafe.jsonl"
    shutil.copy(_end_turn_transcript(tmp_path, "t5.jsonl"), raw)
    (spool / "sess-side-cafecafecafecafe.jsonl.harness").write_text(
        "dsh", encoding="utf-8")
    shim = _shim_bin(tmp_path, "autodream")
    r = subprocess.run(["bash", str(HOOK_WORKER)], capture_output=True,
                       text=True, env=_worker_env(spool, shim), timeout=120)
    assert r.returncode == 0, r.stderr
    log = (spool / "worker.log").read_text()
    assert "retry-later" in log, "失败路径无日志"
    # 失败保留重试: raw+sidecar 保留 (.lock 可回收), 蒸馏中间物清理
    assert raw.is_file() and not (spool / (raw.name + ".lock")).exists()
    assert (spool / (raw.name + ".harness")).read_text() == "dsh"
    assert not (spool / (raw.name + ".endsteps")).exists()
    # sidecar 逐文件转发: autodream 收到 --harness dsh (spool cc/dsh 混排可归属)
    calls = (tmp_path / "shim-autodream" / "shim.log").read_text()
    assert "autodream" in calls and "--harness dsh" in calls
    assert "--session sess-side" in calls
    assert f"--transcript {raw}.endsteps" in calls


def test_worker_filter_fail_logs_and_recovers(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    raw = spool / "sess-fltr-deadbeefdeadbeef.jsonl"
    shutil.copy(_tool_only_transcript(tmp_path, "t6.jsonl"), raw)
    shim = _shim_bin(tmp_path, "endsteps")  # 蒸馏器故障注入
    r = subprocess.run(["bash", str(HOOK_WORKER)], capture_output=True,
                       text=True, env=_worker_env(spool, shim), timeout=120)
    assert r.returncode == 0, r.stderr
    log = (spool / "worker.log").read_text()
    assert "filter-fail" in log, "蒸馏失败路径无日志"
    assert "sess-fltr" in log
    # .lock 回收: 文件回原名, 留待下次重试 (不丢记忆只延迟)
    assert raw.is_file() and not (spool / (raw.name + ".lock")).exists()

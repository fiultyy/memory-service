"""S0 黄金基线 (harness 解耦 strangler, 分支 iterate/harness-decouple, 2026-10-04)。

冻结三件事, 作为 S2-S7 各步「零行为变化」合并门:
1. hook 输入 payload 黄金 (tests/golden/payloads/, @TRANSCRIPT@ 占位);
2. PreCompact bash 快照产物形态: 文件名幂等键 / 内容逐字节 / .harness sidecar
   — S5 (bash→python 下沉) 合并门 = 本文件 cc/dsh 两测全绿, 产物逐字节相等;
3. 注册面零 diff: ~/.claude/settings.json 的 memsvc hooks 段 + hooks/dsh-hooks.json
   与 tests/golden/reg/ 快照相等 — S2-S7 全程注册面不许动。

sha16/cid12 语义钉子在此测试即生效: hashlib.sha256[:16] ≡ sha256sum,
compaction_id 去 '-' 前 12 字符。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "golden"


def _payload(name: str, transcript: Path | None = None) -> str:
    txt = (GOLDEN / "payloads" / name).read_text(encoding="utf-8")
    if transcript is not None:
        txt = txt.replace("@TRANSCRIPT@", str(transcript))
    return txt


def _run_hook(shell: Path, payload: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(["bash", str(shell)], input=payload.encode("utf-8"),
                          capture_output=True, env=env, timeout=60)


@pytest.fixture()
def gold_env(tmp_path: Path):
    """隔离环境: spool / HOME (dsh 回查用) 均 tmp。"""
    spool = tmp_path / "spool"
    home = tmp_path / "home"
    home.mkdir()
    return {"spool": spool, "home": home,
            "env": {"MEM_SPOOL_DIR": str(spool), "HOME": str(home)}}


def test_precompact_golden_cc(gold_env):
    """cc 形: payload 带 transcript_path (明文 jsonl) → <sid>-<sha16>.jsonl 原字节拷贝。"""
    fixture = tmp_copy = gold_env["home"] / "cc_transcript.jsonl"
    shutil.copy(GOLDEN / "payloads" / "cc_transcript.jsonl", tmp_copy)
    r = _run_hook(REPO / "hooks" / "pre-compact-mem.sh",
                  _payload("precompact_cc.json", tmp_copy), gold_env["env"])
    assert r.returncode == 0
    blob = tmp_copy.read_bytes()
    sha16 = hashlib.sha256(blob).hexdigest()[:16]  # 语义钉子 ≡ sha256sum | cut -c1-16
    spool_file = gold_env["spool"] / f"gold-cc-sess-{sha16}.jsonl"
    assert spool_file.is_file(), f"预期幂等键文件名 <sid>-<sha16>: {spool_file.name}"
    assert spool_file.read_bytes() == blob          # 逐字节 = cp 语义
    assert spool_file.with_name(spool_file.name + ".harness").read_text() == "cc"


def test_precompact_golden_dsh(gold_env):
    """dsh 形: transcript_path 空 → HOME 回查 ~/.dsh/sessions; zstd 明文化;
    幂等键 = <sid>-<cid12> (compaction_id 去 '-' 前 12)。"""
    sid_dir = gold_env["home"] / ".dsh" / "sessions" / "proj" / "gold-dsh-sess"
    sid_dir.mkdir(parents=True)
    plain = (GOLDEN / "payloads" / "dsh_transcript.jsonl").read_bytes()
    zst = sid_dir / "session.jsonl.zstd"
    zst.write_bytes(subprocess.run(["zstd", "-q", "-c", "-"], input=plain,
                                   capture_output=True, check=True).stdout)
    r = _run_hook(REPO / "hooks" / "pre-compact-mem.sh",
                  _payload("precompact_dsh.json"), {**gold_env["env"], "MEM_HARNESS": "dsh"})
    assert r.returncode == 0
    # c0mpact-1234-abcd → 去 '-' → c0mpact1234abcd → 前 12 = c0mpact1234a
    spool_file = gold_env["spool"] / "gold-dsh-sess-c0mpact1234a.jsonl"
    assert spool_file.is_file(), f"预期幂等键文件名 <sid>-<cid12>: {spool_file.name}"
    assert spool_file.read_bytes() == plain          # zstdcat 明文化逐字节
    assert spool_file.with_name(spool_file.name + ".harness").read_text() == "dsh"


def test_registration_surfaces_frozen():
    """注册面零 diff 快照: settings.json memsvc hooks 段 + dsh-hooks.json。

    解耦全程 (S2-S7) 两注册面不许动 — 本测漂移红即注册面被动过的信号
    (合并前重冻结需明示理由)。"""
    S = json.load(open("/home/yy/.claude/settings.json"))
    cur = {}
    for ev, entries in (S.get("hooks") or {}).items():
        keep = [e for e in (entries if isinstance(entries, list) else [entries])
                if any("memory-service" in (m.get("command") or "")
                       for m in (e.get("hooks") or []))]
        if keep:
            cur[ev] = keep
    golden = json.load(open(GOLDEN / "reg" / "cc-settings-hooks.json"))
    assert cur == golden, "~/.claude/settings.json memsvc hooks 段漂移 (见头注)"
    assert json.load(open(REPO / "hooks" / "dsh-hooks.json")) == \
        json.load(open(GOLDEN / "reg" / "dsh-hooks.json")), \
        "hooks/dsh-hooks.json 漂移 (解耦全程不许动)"

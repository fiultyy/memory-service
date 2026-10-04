"""S1 注册表契约测试 — 防漂移闸 (解耦 strangler, 2026-10-04)。

harness.py SPECS 是 harness 知识唯一存放地; 本文件钉死:
1. 身份断言 — spec 字段 `is` transcripts/projection 原函数 (引用不重写);
2. 名单相等 — SPECS ≡ transcripts.HARNESSES ≡ corpus_prep.HARNESSES
   (三张名单漂移在这里红, 而不是在生产静默漏掉一家);
3. 能力 None 性 — pi/omp/codex 无 memory_dir/spool/session env;
4. resolve_harness 白名单 ≡ bash case 语义;
5. 分层铁律 — harness.py 源码禁 import runtime/cli/mem_daemon。
"""
from __future__ import annotations

from pathlib import Path

import corpus_prep
import harness
import projection
import transcripts

REPO = Path(__file__).resolve().parent.parent


def test_identity_spec_fields_reference_existing_functions():
    """spec 字段 is 原函数 — 表结构变更/重命名立即暴露。"""
    cc = harness.SPECS["cc"]
    pdir, sid, esteps, keep = transcripts._ADAPTORS["cc"]
    assert cc.project_dir is pdir
    assert cc.session_id is sid
    assert cc.end_steps is esteps
    assert cc.keep is keep
    assert cc.memory_dir is projection.cc_memory_dir
    dsh = harness.SPECS["dsh"]
    assert dsh.memory_dir is projection.dsh_memory_dir
    assert dsh.end_steps is transcripts._ADAPTORS["dsh"][2]
    assert dsh.cwd_filter is None
    codex = harness.SPECS["codex"]
    assert codex.cwd_filter is transcripts._CWD_FILTERS["codex"]
    assert harness.SPECS["pi"].scenes is transcripts._SCENES["pi"]
    assert harness.SPECS["omp"].scenes is transcripts._SCENES["omp"]


def test_harness_name_lists_equal():
    assert set(harness.SPECS) == set(transcripts.HARNESSES)
    assert set(harness.SPECS) == set(corpus_prep.HARNESSES)


def test_capability_none_for_manual_only_harnesses():
    for name in ("pi", "omp", "codex"):
        spec = harness.SPECS[name]
        assert spec.memory_dir is None
        assert spec.spool_env is None
        assert spec.spool_default is None
        assert spec.session_env is None
    # cc/dsh 有投影目录 + spool 池
    assert harness.SPECS["cc"].memory_dir is not None
    assert harness.SPECS["dsh"].memory_dir is not None
    assert harness.SPECS["cc"].spool_env == "MEM_SPOOL_DIR"
    assert harness.SPECS["cc"].session_env == "CLAUDE_CODE_SESSION_ID"


def test_corpus_keys_resolve():
    for name, spec in harness.SPECS.items():
        assert spec.corpus_key in corpus_prep.HARNESS_RULES


def test_resolve_harness_whitelist_matches_bash_case():
    assert harness.resolve_harness("dsh") == "dsh"
    for v in (None, "", "cc", "unknown", "pi"):
        assert harness.resolve_harness(v) == "cc"


def test_memory_dir_or_raise_loud_on_capability_none():
    import pytest
    with pytest.raises(ValueError, match="无 memory 投影约定"):
        harness.memory_dir_or_raise("pi", "/tmp/x")
    # cc/dsh 走真函数 (与 cli._proj_memory_dir 旧实现同路径)
    assert harness.memory_dir_or_raise(
        "dsh", "/tmp/x") == projection.dsh_memory_dir("/tmp/x")


def test_layering_no_upstream_imports():
    """分层铁律: harness.py 禁 import runtime/cli/mem_daemon (防环)。"""
    src = (REPO / "harness.py").read_text(encoding="utf-8")
    for banned in ("import cli", "import runtime", "import mem_daemon",
                   "from cli ", "from runtime ", "from mem_daemon "):
        assert banned not in src, f"harness.py 出现上层 import: {banned!r}"


def test_count_user_turns_single_source():
    """S3: cc/dsh spec 挂同一单源函数; 手动面 harness 无注入面。"""
    import transcripts
    assert harness.SPECS["cc"].count_user_turns is transcripts.count_user_turns
    assert harness.SPECS["dsh"].count_user_turns is transcripts.count_user_turns
    assert harness.SPECS["pi"].count_user_turns is None

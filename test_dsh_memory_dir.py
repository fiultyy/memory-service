"""dsh 项目记忆投影: dsh_memory_dir 编码 + cli --harness 接线 (2026-09-01 dsh 接钩子)。

三条红线:
1. dsh 编码与 CC 编码互不越界(dsh: '--' 包裹 + 保留点号; cc: 无包裹 + 点号转'-')
2. 显式 --memory-dir 永远最优先
3. 缺省 harness=cc → 行为与历史完全一致(412 存量回归由全量套件守护)
"""
import json
import os
import pathlib
import subprocess

import bootstrap
import cli
import projection

FAKE_CWD = "/home/yy/projects/memory-service"


def _fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    return tmp_path


def test_dsh_memory_dir_encoding(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    d = projection.dsh_memory_dir(FAKE_CWD)
    assert d == home / ".dsh" / "projects" / "--home-yy-projects-memory-service--" / "memory"
    # 点号保留(/home/yy/.dsh → --home-yy-.dsh--)
    d2 = projection.dsh_memory_dir("/home/yy/.dsh")
    assert d2 == home / ".dsh" / "projects" / "--home-yy-.dsh--" / "memory"


def test_cc_memory_dir_encoding_unchanged(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    assert projection.cc_memory_dir("/home/yy/.dsh") == \
        home / ".claude" / "projects" / "-home-yy--dsh" / "memory"
    assert projection.cc_memory_dir(FAKE_CWD) == \
        home / ".claude" / "projects" / "-home-yy-projects-memory-service" / "memory"


def test_cli_synthesis_index_harness_dsh(tmp_path, monkeypatch):
    seen = {}

    def fake_synthesis(cwd, mem_dir, session_id=None):
        seen.update(cwd=cwd, dir=mem_dir, session=session_id)
        return {"memory_md": 0}

    monkeypatch.setattr(projection, "synthesis_index", fake_synthesis)
    cli.synthesis_index(scope=FAKE_CWD, harness="dsh", session="s1")
    assert str(seen["dir"]) == str(projection.dsh_memory_dir(FAKE_CWD))
    assert seen["cwd"] == FAKE_CWD and seen["session"] == "s1"
    # 显式 --memory-dir 永远最优先
    cli.synthesis_index(scope=FAKE_CWD, memory_dir="/explicit/dir", harness="dsh")
    assert str(seen["dir"]) == "/explicit/dir"
    # 缺省 cc 不变
    cli.synthesis_index(scope=FAKE_CWD)
    assert str(seen["dir"]) == str(projection.cc_memory_dir(FAKE_CWD))


def test_cli_init_and_prune_harness_dsh(monkeypatch):
    seen = {}
    monkeypatch.setattr(bootstrap, "init_memory",
                        lambda mem_dir, source_cwd=None: seen.update(init_dir=mem_dir, sc=source_cwd) or {})
    monkeypatch.setattr(bootstrap, "prune_deleted",
                        lambda mem_dir, source_cwd=None, dry_run=False:
                        seen.update(prune_dir=mem_dir) or {})
    cli.init_memory(source_cwd=FAKE_CWD, harness="dsh")
    assert str(seen["init_dir"]) == str(projection.dsh_memory_dir(FAKE_CWD))
    cli.prune(scope=FAKE_CWD, harness="dsh")
    assert str(seen["prune_dir"]) == str(projection.dsh_memory_dir(FAKE_CWD))


def test_session_start_mem_sh_dsh_env(tmp_path):
    """端到端: MEM_HARNESS=dsh → 投影落 $HOME/.dsh/projects/<enc>/memory, cc 侧零触碰。"""
    import site
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    payload = json.dumps({"session_id": "t-dsh-dir", "cwd": str(cwd)})
    env = {**os.environ, "HOME": str(home), "MEM_HARNESS": "dsh"}
    # HOME 搬家会搬走 user site-packages — 钉回真实 user base, 保 networkx 等可解析
    env["PYTHONUSERBASE"] = site.getuserbase()
    script = pathlib.Path(__file__).parent / "hooks" / "session-start-mem.sh"
    r = subprocess.run(["bash", str(script)], input=payload,
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0
    enc = "--" + str(cwd).strip("/") .replace("/", "-") + "--"
    assert (home / ".dsh" / "projects" / enc / "memory" / "MEMORY.md").is_file()
    # cc 侧零触碰: 未设 MEM_HARNESS 时才该出现
    assert not (home / ".claude" / "projects").exists()


def test_session_start_mem_sh_default_cc(tmp_path):
    """回归: 不设 MEM_HARNESS → 行为与历史一致(落 cc 面)。"""
    import site
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    payload = json.dumps({"session_id": "t-cc-default", "cwd": str(cwd)})
    env = {**os.environ, "HOME": str(home), "PYTHONUSERBASE": site.getuserbase()}
    script = pathlib.Path(__file__).parent / "hooks" / "session-start-mem.sh"
    r = subprocess.run(["bash", str(script)], input=payload,
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0
    enc = str(cwd).replace("/", "-").replace(".", "-")
    assert (home / ".claude" / "projects" / enc / "memory" / "MEMORY.md").is_file()
    assert not (home / ".dsh" / "projects").exists()


def test_cli_recall_mem_dir_passthrough(monkeypatch):
    """recall 链路: cli.recall harness → recall.recall mem_dir 透传。

    dsh → .dsh 面; 缺省 cc → 显式传值 == recall.recall 内部隐式缺省(逐字节同径);
    无 cwd → mem_dir=None(注入面契约: 不建 mem-*.md, hooks/recall_inject.py :47)。
    """
    import recall as recall_mod
    seen = {}

    def fake_recall(query, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(recall_mod, "recall", fake_recall)
    cli.recall("q", cwd=FAKE_CWD, harness="dsh")
    assert str(seen["mem_dir"]) == str(projection.dsh_memory_dir(FAKE_CWD))
    cli.recall("q", cwd=FAKE_CWD)
    assert str(seen["mem_dir"]) == str(projection.cc_memory_dir(FAKE_CWD))
    cli.recall("q")
    assert seen["mem_dir"] is None


def test_cli_recall_project_branch_harness(monkeypatch):
    """--project 分支: recall-<DATE>.md + MEMORY.md 索引行落 harness 解析面。"""
    import recall as recall_mod
    seen = {}

    def fake_recall(query, **kw):
        return [{"id": "f1", "subject_id": "e1", "predicate": "p", "object_id": "e2"}]

    def fake_project_recall(mem_dir, query, facts):
        seen.update(dir=mem_dir, n=len(facts))
        return {"recall_file": "recall-x.md", "appended": 1, "index_added": True}

    monkeypatch.setattr(recall_mod, "recall", fake_recall)
    monkeypatch.setattr(projection, "project_recall", fake_project_recall)
    cli.recall("q", cwd=FAKE_CWD, project=True, harness="dsh")
    assert str(seen["dir"]) == str(projection.dsh_memory_dir(FAKE_CWD))
    cli.recall("q", cwd=FAKE_CWD, project=True)
    assert str(seen["dir"]) == str(projection.cc_memory_dir(FAKE_CWD))

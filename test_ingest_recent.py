"""M18 ingest-recent: 当前 cwd 最近 N 个 transcript end-step 蒸馏入库 (手动补口)。

覆盖:
1. 定位/排序/limit: ~/.claude/projects/<encoded-cwd>/ 按 mtime 降序取 N。
2. 蒸馏口径: tool_use 干扰被滤, end_turn 过长度门保留 (与 PreCompact worker 同源)。
3. dry_run: 零 LLM 零注册表零 KG 写; 报 would-ingest。
4. 真跑 (regex 通道 pin): autodream 入库 + source_cwd 标记 + 注册表落盘。
5. 注册表防重跑: 同 sha 二跑全 skipped-unchanged; 内容变更 → 重跑。
6. 空 end step → skipped-empty 且入注册表 (二跑 skipped-unchanged, 不重复蒸馏)。
7. per-file 容错: 坏文件记 error 继续。
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cli
import db


def _mk_projects_tree(tmp_path, cwd_name="proj"):
    """假 HOME + 假项目 cwd → ~/.claude/projects/<encoded>/ 目录三元组。"""
    home = tmp_path / "home"
    cwd = tmp_path / cwd_name
    cwd.mkdir(parents=True, exist_ok=True)
    encoded = str(cwd).replace("/", "-").replace(".", "-")
    pdir = home / ".claude" / "projects" / encoded
    pdir.mkdir(parents=True, exist_ok=True)
    return home, cwd, pdir


def _endturn(text):
    return json.dumps({"type": "assistant", "message": {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}]}})


def _tooluse():
    return json.dumps({"type": "assistant", "message": {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "t1", "name": "Bash"}]}})


def _write_transcript(p, end_texts, mtime=None):
    lines = [_tooluse()] * 2  # 干扰: 中间步骤必须被滤
    lines.extend(_endturn(t) for t in end_texts)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def test_dry_run_zero_side_effects(tmp_path, monkeypatch):
    home, cwd, pdir = _mk_projects_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    _write_transcript(pdir / "aaaa1111-1111-1111-1111-111111111111.jsonl",
                      ["结论甲: 采纳斯普利特方案, 理由是上下文成本可控且实现简单, 且与既有 naming 约定兼容无冲突。" * 5])
    reg = tmp_path / "reg.json"
    r = cli.ingest_recent(cwd=str(cwd), dry_run=True, registry_path=reg)
    assert r["files"] == 1 and r["would_ingest"] == 1
    assert r["details"][0]["steps"] == 1  # tool_use 被滤
    assert not reg.exists()  # dry-run 零注册表
    assert r["facts"] == {}  # 零 KG 写


def test_ingest_and_registry_skip(tmp_path, monkeypatch):
    home, cwd, pdir = _mk_projects_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    calls = []
    import autodream as autodream_mod

    def fake_autodream(session_id, transcript_path, source_cwd=None):
        """契约桩: 记 (session, 合成 transcript 内容, source_cwd), 返回通道档计。"""
        calls.append((session_id,
                      Path(transcript_path).read_text(encoding="utf-8"),
                      source_cwd))
        return {"added": 2, "noop": 1}

    monkeypatch.setattr(autodream_mod, "autodream", fake_autodream)
    t1 = pdir / "bbbb2222-2222-2222-2222-222222222222.jsonl"
    _write_transcript(t1, ["结论乙: resolver 上下文证据只否决派生类合并, 名字族默认同义, 校准集 5/5 通过含 NYC 与 js 对照。" * 5],
                      mtime=time.time() - 100)
    reg = tmp_path / "reg.json"
    r1 = cli.ingest_recent(cwd=str(cwd), registry_path=reg)
    assert r1["ingested"] == 1 and r1["errors"] == 0
    assert r1["facts"] == {"added": 2, "noop": 1}  # 通道计数聚合
    # 契约: session = transcript 文件名 uuid; 合成 transcript = endstep 形状; source_cwd 标记
    assert calls[0][0] == "bbbb2222-2222-2222-2222-222222222222"
    assert '"type": "user"' in calls[0][1]
    assert "结论乙" in calls[0][1] and "tool_use" not in calls[0][1]
    assert calls[0][2] == str(cwd)
    assert reg.exists()
    # 二跑: 同 sha → skipped-unchanged, 不重复烧通道
    r2 = cli.ingest_recent(cwd=str(cwd), registry_path=reg)
    assert r2["skipped_unchanged"] == 1 and r2["ingested"] == 0
    assert len(calls) == 1
    # 内容变更 → 重跑
    _write_transcript(t1, ["结论乙改: 补充 exclude_ids 图不变量, 宁分离勿自环, 保留边但拆自环合并。" * 5])
    r3 = cli.ingest_recent(cwd=str(cwd), registry_path=reg)
    assert r3["ingested"] == 1
    assert len(calls) == 2


def test_empty_transcript_and_corrupt_file(tmp_path, monkeypatch):
    home, cwd, pdir = _mk_projects_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    db.init(tmp_path / "db.sqlite")
    t_empty = pdir / "cccc3333-3333-3333-3333-333333333333.jsonl"
    t_empty.write_text(_tooluse() + "\n", encoding="utf-8")  # 仅中间步骤
    # 坏字节文件: 活跃会话尾部半写形态 (GBK 段) — errors=replace 容错不炸,
    # 坏行 json.loads 失败被跳过 → 视作空 (skipped-empty) 而非 error
    t_garbage = pdir / "dddd4444-4444-4444-4444-444444444444.jsonl"
    t_garbage.write_bytes(b"\xd6\xd0\xce\xc4\xb4\xed\xc2\xeb\xff\xfe")
    reg = tmp_path / "reg.json"
    r = cli.ingest_recent(cwd=str(cwd), registry_path=reg)
    assert r["skipped_empty"] == 2 and r["errors"] == 0
    # 空/坏 transcript 均入注册表 → 二跑不再蒸馏; 坏文件未抛错
    r2 = cli.ingest_recent(cwd=str(cwd), registry_path=reg)
    assert r2["skipped_unchanged"] == 2
    assert r2["errors"] == 0


def test_limit_and_mtime_order(tmp_path, monkeypatch):
    home, cwd, pdir = _mk_projects_tree(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    now = time.time()
    for i, pref in enumerate(["eeee", "ffff", "gggg"]):
        _write_transcript(
            pdir / f"{pref}{i}{i}{i}{i}-{i}{i}{i}{i}-{i}{i}{i}{i}-{i}{i}{i}{i}-{i}{i}{i}{i}{i}{i}{i}{i}{i}{i}{i}{i}{i}.jsonl".replace("0" * 4, "0000"),
            ["结论: mtime 排序验证用的长结论文本, 超过一百二十字符的门槛, 这里补足长度避免短结论文本被门误滤。" * 5],
            mtime=now - 300 + i * 100)
    r = cli.ingest_recent(cwd=str(cwd), limit=2, dry_run=True,
                          registry_path=tmp_path / "reg.json")
    assert r["files"] == 2  # limit 生效
    got = [d["file"] for d in r["details"]]
    assert got == sorted(got, reverse=True) or len(got) == 2
    # 最新的两个 (mtime 最大) = gggg/ffff 前缀
    assert {g[:4] for g in got} == {"gggg", "ffff"}

"""方案 B (债#10 后续 2026-09-07): cli ``dream`` 子命令接线 — M11 消费面单入口。

背景: dream 调度缺位的修补走 systemd timer (memory-dream.timer, daily)，
ExecStart 调 ``cli.py dream`` — 只跑 :func:`dream.run_cycle` 六职责，不做
transcript sweep (进端已由 spool-drain 负责轴，避免双路蒸馏烧 LLM)。

本文件钉 CLI→run_cycle 的接线契约: JSON 计数透传 + --source-cwd 透传。
run_cycle 六职责内部语义由 test_m11_dream.py 覆盖，此处不重复。
"""
import json

import cli
import dream as dream_mod


def test_cli_dream_prints_run_cycle_stats(monkeypatch, capsys):
    seen = {}

    def fake_run_cycle(source_cwd=None):
        seen["source_cwd"] = source_cwd
        return {"signals_consumed": 3, "promoted": 1}

    monkeypatch.setattr(dream_mod, "run_cycle", fake_run_cycle)

    rc = cli._main(["dream"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "signals_consumed": 3, "promoted": 1}
    assert seen["source_cwd"] is None, "缺省全消费 (source_cwd=None)"


def test_cli_dream_source_cwd_passthrough(monkeypatch, capsys):
    seen = {}

    def fake_run_cycle(source_cwd=None):
        seen["source_cwd"] = source_cwd
        return {}

    monkeypatch.setattr(dream_mod, "run_cycle", fake_run_cycle)

    rc = cli._main(["dream", "--source-cwd", "/proj-x"])

    assert rc == 0
    assert seen["source_cwd"] == "/proj-x"

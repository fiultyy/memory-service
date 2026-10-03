"""批次2 (v4 transcript 车道, 2026-10-03): _sweep_spool 段消费走
semantic_chunks → distill_chunk + 段水位 (chunk-seg:) 重放免疫 +
ChunkerUnavailable 挂起 lane + env 关回 v2。零网络 (切分/判官全 mock)。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓根平铺
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db  # noqa: E402
import mem_daemon  # noqa: E402
import distill as D  # noqa: E402
import semantic_chunk as SC  # noqa: E402


@pytest.fixture
def tdb(tmp_path):
    return db.init(tmp_path / "t.db")


def _cc_turn(spool: Path, name: str) -> Path:
    """CC 快照 jsonl: 一轮 user→assistant(end_turn)。文件名 <session>-<sha16> 同 hook 形。"""
    import hashlib
    lines = [
        {"type": "user", "cwd": "/w",
         "message": {"content": "帮我配置 nginx 反代"}},
        {"type": "assistant", "cwd": "/w", "timestamp": "2026-10-03T01:00:00+00:00",
         "message": {"content": [{"type": "text",
                                  "text": "先安装然后改配置文件。重启服务生效。"}],
                     "stop_reason": "end_turn"}},
    ]
    body = "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines)
    h = hashlib.sha256(body.encode()).hexdigest()[:16]
    jf = spool / f"sess-a1-{h}.jsonl"
    jf.write_text(body, encoding="utf-8")
    return jf


def _mk(monkeypatch):
    """mock 切分/判官, 返回 (chunks 调用记录, distill_chunk 调用记录)。"""
    ccalls, dcalls = [], []

    def fake_chunks(text):
        ccalls.append(text)
        return [{"text": "语义段甲。", "gist": "结论甲", "units_n": 1},
                {"text": "语义段乙。", "gist": "结论乙", "units_n": 1}]

    def fake_chunk(text, gist, session_id, cwd, ts):
        dcalls.append((text, gist, session_id, cwd))
        return {"atoms": 1, "edges": 0, "merged": 0}

    def fake_seg(*a, **k):
        raise AssertionError("v2 车道不应被调 (MEM_SEMANTIC_CHUNK=1)")

    monkeypatch.setattr(SC, "semantic_chunks", fake_chunks)
    monkeypatch.setattr(D, "distill_chunk", fake_chunk)
    monkeypatch.setattr(D, "distill_segment", fake_seg)
    return ccalls, dcalls


def test_v4_lane_chunks_and_watermark(tmp_path, tdb, monkeypatch):
    """v4: 轮段 → 2 chunks → distill_chunk 透传 + 段水位落库 + 文件消费删除。"""
    spool = tmp_path / "spool"; spool.mkdir()
    jf = _cc_turn(spool, "a")
    ccalls, dcalls = _mk(monkeypatch)
    monkeypatch.setenv("MEM_SEMANTIC_CHUNK", "1")
    state = mem_daemon._sweep_spool({}, "/watch", spool)
    assert len(ccalls) == 1 and "[助手]" in ccalls[0]
    assert [d[0] for d in dcalls] == ["语义段甲。", "语义段乙。"]
    assert dcalls[0][2] == "sess-a1" and dcalls[0][3] == "/w"  # 溯源: 行内 cwd
    # 段水位已落; 文件删 (全段成功)
    wm = "chunk-seg:" + __import__("hashlib").sha256(
        ccalls[0].encode()).hexdigest()
    assert db.get_conn().execute(
        "SELECT 1 FROM distill_seen WHERE sha=?", (wm,)).fetchone()
    assert not jf.exists() and not state  # 消费完成, state 清


def test_watermark_replay_zero_llm(tmp_path, tdb, monkeypatch):
    """重放 (新快照文件名 = 新 state 键): 段水位已见 → 缝扫描零调用。"""
    spool = tmp_path / "spool"; spool.mkdir()
    jf = _cc_turn(spool, "a")
    ccalls, dcalls = _mk(monkeypatch)
    monkeypatch.setenv("MEM_SEMANTIC_CHUNK", "1")
    mem_daemon._sweep_spool({}, "/watch", spool)
    assert jf.exists() is False
    # 第二个快照 (内容超集: 同轮 + 新轮) — hook 每快照新文件名
    jf2 = _cc_turn(spool, "a2")
    n_before = len(ccalls)
    mem_daemon._sweep_spool({}, "/watch", spool)
    assert len(ccalls) == n_before  # 同段水位命中, 缝扫描零调用
    assert jf2.exists() is False


def test_chunker_suspend(tmp_path, tdb, monkeypatch):
    """ChunkerUnavailable → LayaUnavailable lane: offset 停 ack / attempts 0 /
    文件留存 / 图零写入 (水位不落)。"""
    spool = tmp_path / "spool"; spool.mkdir()
    jf = _cc_turn(spool, "a")
    dcalls = []

    def dead(text):
        raise SC.ChunkerUnavailable("laya gap-scan 3 次尝试后仍败")
    monkeypatch.setattr(SC, "semantic_chunks", dead)
    monkeypatch.setattr(D, "distill_chunk",
                        lambda *a, **k: dcalls.append(1) or {"atoms": 1})
    monkeypatch.setenv("MEM_SEMANTIC_CHUNK", "1")
    state = mem_daemon._sweep_spool({}, "/watch", spool)
    assert not dcalls                              # 判官未触
    assert jf.exists()                             # spool 积压持有
    key = mem_daemon._spool_key(jf)
    assert key in state and state[key]["attempts"] == 0 \
        and state[key]["offset"] == 0              # 挂起: 零 ack 零失败计数
    assert db.get_conn().execute(
        "SELECT COUNT(*) FROM distill_seen").fetchone()[0] == 0


def test_env_off_falls_back_v2(tmp_path, tdb, monkeypatch):
    """MEM_SEMANTIC_CHUNK=0 → distill_segment 旧径 (v4 函数零参与)。"""
    spool = tmp_path / "spool"; spool.mkdir()
    jf = _cc_turn(spool, "a")
    seg_calls = []
    monkeypatch.setattr(SC, "semantic_chunks",
                        lambda t: (_ for _ in ()).throw(AssertionError("v4 不应跑")))
    monkeypatch.setattr(D, "distill_segment",
                        lambda text, session_id, cwd, ts:
                        (seg_calls.append(text),
                         {"atoms": 1, "edges": 0, "merged": 0})[1])
    monkeypatch.setenv("MEM_SEMANTIC_CHUNK", "0")
    state = mem_daemon._sweep_spool({}, "/watch", spool)
    assert len(seg_calls) == 1 and "[助手]" in seg_calls[0]
    assert not jf.exists() and not state

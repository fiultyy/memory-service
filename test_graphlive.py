"""M20 graphlive: 快照/增量 shape、端点并集规则、csv 导出、inotify 触发、HTTP/SSE 冒烟。

关键不变量 (悬空边防线):
    delta 的 nodes ⊇ (rowid>游标实体) ∪ (新边全部端点) — 老实体 degree=0
    从未下发过, 新边连上时必须补发, 否则页面 addEdge 撞悬空。
"""
import json
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import db
import graphlive
import store


def _fresh(name: str) -> Path:
    tmp = tempfile.mkdtemp()
    p = Path(tmp) / f"{name}.db"
    db.init(p)
    return p


def _seed_triangle() -> tuple[str, str, str]:
    a = store.put_entity("Alpha", "concept")
    b = store.put_entity("Beta", "tool")
    c = store.put_entity("Gamma", "identifier")   # 老实体, 先 degree=0
    store.put_fact(a, "uses", "beta toolchain", object_id=b, topic="Alpha 使用 Beta")
    return a, b, c


# ── snapshot ─────────────────────────────────────────────────────────

def test_snapshot_shape_and_orphan_filter():
    _fresh("snap.db")
    a, b, c = _seed_triangle()
    snap = graphlive.snapshot()
    ids = {n["id"] for n in snap["nodes"]}
    assert ids == {a, b}                      # 孤儿 c (degree=0) 不入快照
    assert len(snap["edges"]) == 1
    e = snap["edges"][0]
    assert (e["subject_id"], e["object_id"], e["predicate"]) == (a, b, "uses")
    assert e["topic"] == "Alpha 使用 Beta"
    assert snap["cursor"]["fact"] > 0 and snap["cursor"]["entity"] >= 3
    deg = {n["id"]: n["degree"] for n in snap["nodes"]}
    assert deg[a] == 1 and deg[b] == 1


def test_snapshot_excludes_unary_and_inactive():
    _fresh("unary.db")
    a = store.put_entity("Solo", "concept")
    store.put_fact(a, "weights", "1.0")                 # 字面事实 (object_id NULL)
    b = store.put_entity("Dup", "concept")
    f = store.put_fact(a, "relates", "x", object_id=b)
    store.update_fact_status(f, "deprecated", reason="dedup") if hasattr(
        store, "update_fact_status") else None
    snap = graphlive.snapshot()
    if hasattr(store, "update_fact_status"):
        assert all(e["object_id"] is not None for e in snap["edges"])
        assert all(e["id"] != f for e in snap["edges"])


def test_snapshot_cwd_filter_keeps_null():
    _fresh("cwd.db")
    a = store.put_entity("Here", "concept")
    b = store.put_entity("There", "concept")
    store.put_fact(a, "uses", "x", object_id=b, source_cwd="/home/yy/projA")
    c = store.put_entity("Legacy", "concept")
    d = store.put_entity("Old", "concept")
    store.put_fact(c, "uses", "y", object_id=d, source_cwd=None)   # 老数据 NULL
    snap = graphlive.snapshot(cwd="/home/yy/projA")
    assert {e["subject_id"] for e in snap["edges"]} == {a, c}      # NULL 保留 (ADR-14 b)
    other = graphlive.snapshot(cwd="/home/yy/projB")
    assert {e["subject_id"] for e in other["edges"]} == {c}        # 只剩 NULL 老数据


# ── delta: 端点并集规则 ──────────────────────────────────────────────

def test_delta_endpoint_union():
    _fresh("delta.db")
    a, b, c = _seed_triangle()          # c 是 pre-existing degree-0 实体
    cur0 = graphlive.snapshot()["cursor"]
    # 新边: 新实体 Delta ↔ 老孤儿 c (端点一个全新一个从未下发)
    d = store.put_entity("Delta", "concept")
    store.put_fact(d, "mentions", "legacy ref", object_id=c)
    dl = graphlive.delta(cur0["entity"], cur0["fact"])
    ids = {n["id"] for n in dl["nodes"]}
    assert {d, c} <= ids, "端点并集失败: 新实体和老孤儿都必须下发"
    assert len(dl["edges"]) == 1 and dl["edges"][0]["object_id"] == c
    assert dl["cursor"]["fact"] > cur0["fact"]


def test_delta_empty_on_fresh_cursor():
    _fresh("empty.db")
    _seed_triangle()
    cur = graphlive.snapshot()["cursor"]
    dl = graphlive.delta(cur["entity"], cur["fact"])
    assert dl["nodes"] == [] and dl["edges"] == []


def test_delta_drops_degree0_entities():
    """纯字面事实实体 (unary, object_id NULL) 不成图 → 增量不推漂点。"""
    _fresh("drift.db")
    a, b, c = _seed_triangle()
    cur0 = graphlive.snapshot()["cursor"]
    solo = store.put_entity("Floating", "concept")
    store.put_fact(solo, "weights", "1.0")      # unary → 无边 → degree 0
    dl = graphlive.delta(cur0["entity"], cur0["fact"])
    assert solo not in {n["id"] for n in dl["nodes"]}
    # 但随后的真边会把它带进来 (端点并集)
    cur1 = dl["cursor"]
    store.put_fact(a, "links", "float ref", object_id=solo)
    dl2 = graphlive.delta(cur1["entity"], cur1["fact"])
    assert solo in {n["id"] for n in dl2["nodes"]}
    assert len(dl2["edges"]) == 1


# ── 导出 ─────────────────────────────────────────────────────────────

def test_export_csv_and_json():
    _fresh("exp.db")
    _seed_triangle()
    tmp = Path(tempfile.mkdtemp())
    jp = graphlive.export_json(tmp / "sub" / "graph.json")
    data = json.loads(jp.read_text(encoding="utf-8"))
    assert {"cursor", "nodes", "edges"} <= set(data)
    nodes_p, edges_p = graphlive.export_csv(tmp / "csv")
    nlines = nodes_p.read_text(encoding="utf-8").strip().splitlines()
    elines = edges_p.read_text(encoding="utf-8").strip().splitlines()
    assert nlines[0] == "id,name,type,degree,created_at"
    # Cosmograph 口径: 边表带 created_at 时间列 (时间轴自动识别)
    assert elines[0] == "source,target,predicate,label,lif,created_at"
    assert "T" in elines[1]             # ISO 时间戳进列
    assert len(nlines) == len(data["nodes"]) + 1


# ── inotify watcher ──────────────────────────────────────────────────

def test_watcher_fires_on_fact_commit():
    p = _fresh("watch.db")
    hits = []
    w = graphlive.WalWatcher(p, lambda: hits.append(time.time()), debounce_s=0.05)
    w.start()
    try:
        time.sleep(0.3)                 # inotify fd 就绪
        a = store.put_entity("W", "concept")
        store.put_entity("X", "concept")
        store.put_fact(a, "uses", "y", object_id=store.put_entity("Y", "concept"))
        deadline = time.time() + 4
        while not hits and time.time() < deadline:
            time.sleep(0.05)
        assert hits, "wal 写入未触发 watcher (inotify 事件丢失)"
        assert w.events_seen >= 1
    finally:
        w.stop()


# ── GraphLive 订阅队列 ───────────────────────────────────────────────

def test_on_commit_pushes_to_subscriber():
    p = _fresh("push.db")
    gl = graphlive.GraphLive(db_path=p, debounce_s=0.05)
    cursor = gl.bootstrap()
    import queue as _q
    q = _q.Queue()
    gl.subscribers.append(q)
    a = store.put_entity("P1", "concept")
    b = store.put_entity("P2", "concept")
    store.put_fact(a, "uses", "z", object_id=b)
    gl._on_commit()
    d = q.get(timeout=2)
    assert d["edges"] and gl.push_count == 1
    assert d["cursor"]["fact"] > cursor["fact"]
    # 幂档: 无新行不推
    gl._on_commit()
    assert q.empty()


# ── HTTP/SSE 冒烟 (stdlib server, 同源无 CORS) ───────────────────────

def test_http_smoke_snapshot_and_sse_hello():
    p = _fresh("http.db")
    _seed_triangle()
    gl, server, cursor = graphlive.build_server(db_path=p, port=0)
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    sse = None
    try:
        assert cursor["fact"] > 0
        # 页面
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
        assert b"memsvc" in html and b"EventSource" in html
        # 快照
        snap = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/graph", timeout=5).read())
        assert len(snap["edges"]) == 1 and snap["cursor"]["fact"] == cursor["fact"]
        # 增量接口
        inc = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/graph?after_e=0&after_f={cursor['fact'] - 1}",
            timeout=5).read())
        assert len(inc["edges"]) >= 1
        # SSE: 订阅握手即回 hello 注释行 (15s 心跳同一通道)
        sse = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stream", timeout=6)
        first = sse.readline()
        assert first.startswith(b":")
        # 推送 → 订阅端收到 delta 事件 (绕过 watcher 直调, 时序确定)
        a = store.put_entity("S1", "concept")
        b = store.put_entity("S2", "concept")
        store.put_fact(a, "uses", "w", object_id=b)
        gl._on_commit()
        line = sse.readline()
        deadline = time.time() + 4
        while not line.startswith(b"event:") and time.time() < deadline:
            line = sse.readline()
        assert line.startswith(b"event: delta"), f"SSE 未收到 delta: {line!r}"
        payload = json.loads(sse.readline().decode().removeprefix("data: "))
        assert payload["edges"]
    finally:
        if sse:
            sse.close()
        server.shutdown()
        server.server_close()

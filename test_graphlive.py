"""M20 graphlive (v2 atom 面, 2026-10-02 接线): 快照/增量 shape、端点并集规则、
csv 导出、inotify 触发、HTTP/SSE 冒烟。

关键不变量 (悬空边防线):
    delta 的 nodes ⊇ (rowid>游标 atom) ∪ (新边全部端点) — 老 atom degree=0
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


def _fresh(name: str) -> Path:
    tmp = tempfile.mkdtemp()
    p = Path(tmp) / f"{name}.db"
    db.init(p)
    db.get_conn().execute(
        "CREATE TABLE IF NOT EXISTS atom_edge ("
        "a_id INTEGER NOT NULL REFERENCES atom(id), "
        "b_id INTEGER NOT NULL REFERENCES atom(id), "
        "w REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'related', "
        "PRIMARY KEY(a_id, b_id), CHECK(a_id < b_id))")
    return p


def _atom(text: str, label: str = "fact", cwd: str | None = None,
          valid_to: str | None = None) -> int:
    cur = db.get_conn().execute(
        "INSERT INTO atom(text, label, p_dur, valid_from, valid_to, source_cwd, "
        "needs_audit, needs_embed, created_at) "
        "VALUES(?,?,0.5,'2026-10-02T00:00:00+00:00',?,?,0,0,'2026-10-02')",
        (text, label, valid_to, cwd))
    return cur.lastrowid


def _edge(a: int, b: int, w: float = 0.5) -> None:
    x, y = sorted((a, b))
    db.get_conn().execute(
        "INSERT INTO atom_edge(a_id, b_id, w) VALUES(?,?,?)", (x, y, w))


def _seed_triangle() -> tuple[int, int, int]:
    a = _atom("结论甲", "fact")
    b = _atom("结论乙", "judgment")
    c = _atom("孤儿结论", "summary")            # 老原子, 先 degree=0
    _edge(a, b)
    return a, b, c


# ── snapshot ─────────────────────────────────────────────────────────

def test_snapshot_shape_and_orphan_filter():
    _fresh("snap.db")
    a, b, c = _seed_triangle()
    snap = graphlive.snapshot()
    ids = {n["id"] for n in snap["nodes"]}
    assert ids == {str(a), str(b)}             # 孤儿 c (degree=0) 不入快照
    assert len(snap["edges"]) == 1
    e = snap["edges"][0]
    assert sorted((e["subject_id"], e["object_id"])) == sorted((a, b))
    assert e["predicate"] == "related"
    assert snap["cursor"]["fact"] > 0 and snap["cursor"]["entity"] >= 3
    deg = {n["id"]: n["degree"] for n in snap["nodes"]}
    assert deg[str(a)] == 1 and deg[str(b)] == 1
    # v2 字段映射: name=text, entity_type=label, aliases=semantic tags
    nd = next(n for n in snap["nodes"] if n["id"] == str(a))
    assert nd["name"] == "结论甲" and nd["entity_type"] == "fact"
    assert nd["aliases"] == []                  # 无 semantic tag → 空


def test_snapshot_excludes_soft_deleted():
    _fresh("dead.db")
    a = _atom("活结论", "fact")
    b = _atom("死结论", "fact", valid_to="2026-10-01T00:00:00+00:00")
    _edge(a, b)                                 # 死端点边 → 双端 live 过滤掉
    snap = graphlive.snapshot()
    assert snap["edges"] == []
    assert {n["id"] for n in snap["nodes"]} == set()


def test_snapshot_cwd_filter_keeps_null():
    _fresh("cwd.db")
    a = _atom("此处结论", cwd="/home/yy/projA")
    b = _atom("彼处结论", cwd="/home/yy/projB")
    _edge(a, b)
    c = _atom("老结论", cwd=None)               # 老数据 NULL
    d = _atom("更老结论", cwd=None)
    _edge(c, d)
    snap = graphlive.snapshot(cwd="/home/yy/projA")
    assert {e["subject_id"] for e in snap["edges"]} <= {a, c, d}  # NULL 边保留
    assert len(snap["edges"]) == 2              # ADR-14 b: NULL 兼容
    other = graphlive.snapshot(cwd="/home/yy/projZ")
    assert len(other["edges"]) == 1             # 只剩 NULL 老数据


# ── delta: 端点并集规则 ──────────────────────────────────────────────

def test_delta_endpoint_union():
    _fresh("delta.db")
    a, b, c = _seed_triangle()                  # c 是 pre-existing degree-0 原子
    cur0 = graphlive.snapshot()["cursor"]
    d = _atom("新结论", "fact")
    _edge(d, c)                                 # 新原子 ↔ 老孤儿
    dl = graphlive.delta(cur0["entity"], cur0["fact"])
    ids = {n["id"] for n in dl["nodes"]}
    assert {str(d), str(c)} <= ids, "端点并集失败: 新原子和老孤儿都必须下发"
    assert len(dl["edges"]) == 1
    assert dl["cursor"]["fact"] > cur0["fact"]


def test_delta_empty_on_fresh_cursor():
    _fresh("empty.db")
    _seed_triangle()
    cur = graphlive.snapshot()["cursor"]
    dl = graphlive.delta(cur["entity"], cur["fact"])
    assert dl["nodes"] == [] and dl["edges"] == []


def test_delta_drops_degree0_atoms():
    """无边新原子 (degree 0) 不推漂点; 随后真边端点并集带进来。"""
    _fresh("drift.db")
    a, b, c = _seed_triangle()
    cur0 = graphlive.snapshot()["cursor"]
    solo = _atom("漂浮结论", "experience")       # 无边 → degree 0
    dl = graphlive.delta(cur0["entity"], cur0["fact"])
    assert str(solo) not in {n["id"] for n in dl["nodes"]}
    cur1 = dl["cursor"]
    _edge(a, solo)
    dl2 = graphlive.delta(cur1["entity"], cur1["fact"])
    assert str(solo) in {n["id"] for n in dl2["nodes"]}
    assert len(dl2["edges"]) == 1


# ── 聚合面: tag 旁挂 + louvain 社区 ─────────────────────────────────

def _tag(name: str, kind: str = "semantic") -> int:
    cur = db.get_conn().execute(
        "INSERT INTO tag(name, kind) VALUES(?,?)", (name, kind))
    return cur.lastrowid


def _mount(tid: int, aid: int, w: float = 0.5) -> None:
    db.get_conn().execute(
        "INSERT INTO tag_mount(tag_id, atom_id, w) VALUES(?,?,?)", (tid, aid, w))


def test_snapshot_tag_bipartite_semantic_only_and_comm():
    _fresh("agg.db")
    a, b, c = _seed_triangle()
    t = _tag("聚合主题")
    _tag("session:xyz", kind="factual")           # factual 是来源标记, 不入聚合面
    _mount(t, a)
    _mount(t, b)
    _mount(t, c)                                  # c 孤儿 (degree=0) 不在快照
    snap = graphlive.snapshot()
    assert [tg["name"] for tg in snap["tags"]] == ["聚合主题"]
    assert snap["tags"][0]["mounts"] == 2
    assert {(m["atom"], m["tag"]) for m in snap["mounts"]} == {(str(a), t), (str(b), t)}
    # louvain: a-b 连通 → 都有社区号且同社区
    assert set(snap["comm"]) == {str(a), str(b)}
    assert snap["comm"][str(a)] == snap["comm"][str(b)]


def test_snapshot_comm_empty_on_isolated():
    _fresh("nocomm.db")
    a, b = _atom("孤A"), _atom("孤B")              # 无边 → louvain 无边不跑
    snap = graphlive.snapshot()
    assert snap["comm"] == {} and snap["tags"] == []


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
    assert "T" in elines[1]                     # ISO 时间戳进列
    assert len(nlines) == len(data["nodes"]) + 1


# ── inotify watcher ──────────────────────────────────────────────────

def test_watcher_fires_on_atom_commit():
    p = _fresh("watch.db")
    hits = []
    w = graphlive.WalWatcher(p, lambda: hits.append(time.time()), debounce_s=0.05)
    w.start()
    try:
        time.sleep(0.3)                 # inotify fd 就绪
        _seed_triangle()
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
    a = _atom("推送结论一", "fact")
    b = _atom("推送结论二", "judgment")
    _edge(a, b)
    gl._on_commit()
    d = q.get(timeout=2)
    assert d["edges"] and gl.push_count == 1
    assert d["cursor"]["fact"] > cursor["fact"]
    gl._on_commit()                    # 幂档: 无新行不推
    assert q.empty()


# ── HTTP/SSE 冒烟 (stdlib server, 同源无 CORS) ───────────────────────

def test_http_smoke_snapshot_and_sse_hello():
    p = _fresh("http.db")
    _seed_triangle()
    gl, server, cursor = graphlive.build_server(db_path=p, port=0)
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        assert cursor["fact"] > 0
        # 页面
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
        assert b"EventSource" in html
        # 快照
        snap = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/graph", timeout=5).read())
        assert len(snap["edges"]) == 1 and snap["cursor"]["fact"] == cursor["fact"]
        # 增量接口
        dl = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/graph?after_e=0&after_f=0",
            timeout=5).read())
        assert len(dl["edges"]) == 1
        # SSE hello 帧可达即算通 (不读 delta 帧 — 需真实写入触发)
        req = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/stream", timeout=5)
        assert req.headers["Content-Type"] == "text/event-stream"
        req.close()
    finally:
        server.shutdown()
        server.server_close()

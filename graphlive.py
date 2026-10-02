"""graphlive — KG 实时图浏览 (M20): 事件驱动零轮询 + SSE 增量推送。

架构 (2026-08-27 用户裁决「不轮询」方案落地):
    inotify(ctypes 直调 libc, 盯 data/ 目录的 memory.db/-wal/-shm 事件)
      → 去抖 250ms (逐 fact commit 的突发合并)
      → 增量查询 (rowid 游标; 新边端点实体做并集补偿)
      → SSE 推送 → 单页 sigma.js 增量加点/边

零依赖: HTTP 用 stdlib http.server, inotify 用 ctypes, 页面 CDN 加载 sigma.js。
**不做 DB 轮询** — WAL 模式下每次 commit 必写 -wal, inotify 即提交信号。

游标语义 (v1 边界, 文档化):
- 只跟踪 INSERT (图生长)。UPDATE (LIF 衰减/状态流转) 不变 rowid, 不推 —
  看生长够用; 要全量态刷新页面即可 (重新快照)。
- 快照只收 degree>0 实体 (孤儿不画); 增量对新边端点做**并集补偿** — 老实体
  之前 degree=0 从未下发, 新边连上时必须补发, 否则页面出现悬空边。

CLI: ``graph-export`` (json 快照 / csv 对 — Cosmograph 口径带 created_at 时间列)
+ ``graph-live`` (起服务器, 前台阻塞)。
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import select
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HTML_PATH = Path(__file__).parent / "web" / "graph_live.html"
_DEBOUNCE_S = 0.25

_EDGE_COLS = ("e.rowid AS id, e.a_id AS subject_id, e.b_id AS object_id, "
              "e.kind AS predicate, e.w AS value, e.w AS LIF, "
              "sa.valid_from AS created_at")


# ── 快照 / 增量查询 (db.get_conn 共享连接, check_same_thread=False) ────
# v2 atom 面 (2026-10-02 接线): 节点 = live atom, 边 = atom_edge; 字段名保持
# legacy fact 面形状 (subject_id/object_id/predicate/LIF…) → 前端 HTML 零改。
# entity_type = atom.label (fact/judgment/experience/summary); aliases = top
# semantic tag 名 (可搜/可辨主题); 边 created_at = 源端点 atom 生时 (atom_edge
# 无时间列, 时间窗 slider 用 atom 生时近似 — 边随较新端点出现, 语义成立)。

def _edge_rows(where: str = "", params: tuple = ()) -> list[dict]:
    import db
    rows = db.get_conn().execute(
        f"SELECT {_EDGE_COLS} FROM atom_edge e "
        f"JOIN atom sa ON sa.id = e.a_id AND sa.valid_to IS NULL "
        f"JOIN atom sb ON sb.id = e.b_id AND sb.valid_to IS NULL "
        f"WHERE 1=1 {where} ORDER BY e.rowid", params).fetchall()
    return [dict(r) for r in rows]


def _degrees(edges: list[dict]) -> dict[str, int]:
    deg: dict[str, int] = {}
    for e in edges:
        deg[e["subject_id"]] = deg.get(e["subject_id"], 0) + 1
        deg[e["object_id"]] = deg.get(e["object_id"], 0) + 1
    return deg


def _entity_nodes(ids: set[str], degrees: dict[str, int]) -> list[dict]:
    """live atom → 节点 (legacy 字段形状: name=text, entity_type=label,
    aliases=top-2 semantic tag 名, degree)。id 统一 str (前端 Set 去重)。"""
    import db
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = db.get_conn().execute(
        f"SELECT id, text, label, p_dur, valid_from, source_cwd "
        f"FROM atom WHERE valid_to IS NULL AND id IN ({ph})",
        tuple(ids)).fetchall()
    tag_by_atom: dict[int, list[str]] = {}
    aids = [r["id"] for r in rows]
    if aids:
        tph = ",".join("?" * len(aids))
        for tr in db.get_conn().execute(
            f"SELECT m.atom_id, t.name FROM tag_mount m JOIN tag t ON t.id=m.tag_id "
            f"WHERE t.kind='semantic' AND m.atom_id IN ({tph}) "
            f"ORDER BY m.w DESC", aids):
            lst = tag_by_atom.setdefault(tr["atom_id"], [])
            if len(lst) < 2:
                lst.append(tr["name"])
    out = []
    for r in rows:
        d = dict(r)
        d["degree"] = degrees.get(r["id"], 0)   # 先查 int 键, 再转 str (前端契约)
        d["id"] = str(d["id"])
        d["name"] = (d.pop("text") or "")[:80]
        d["entity_type"] = d.pop("label")
        d["aliases"] = tag_by_atom.get(r["id"], [])
        out.append(d)
    return out


def snapshot(cwd: str | None = None) -> dict:
    """全量快照: nodes(degree>0 live atom) + edges(双端 live) + rowid 游标。

    cwd 过滤沿用 ADR-14 b 方案口径: ``source_cwd = ? OR source_cwd IS NULL``。
    游标键名沿用 entity/fact (前端契约), 语义 = atom/atom_edge max(rowid)。
    """
    import db
    if cwd:
        edges = _edge_rows(
            "AND (sa.source_cwd = ? OR sa.source_cwd IS NULL "
            " OR sb.source_cwd = ? OR sb.source_cwd IS NULL)", (cwd, cwd))
    else:
        edges = _edge_rows()
    deg = _degrees(edges)
    cur = db.get_conn().execute(
        "SELECT (SELECT COALESCE(MAX(rowid),0) FROM atom) e, "
        "(SELECT COALESCE(MAX(rowid),0) FROM atom_edge) f").fetchone()
    nodes = _entity_nodes({i for i, d in deg.items() if d > 0}, deg)
    comm = _communities(edges)
    tags, mounts = _tag_bipartite({int(n["id"]) for n in nodes})
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cwd": cwd,
        "cursor": {"entity": cur["e"], "fact": cur["f"]},
        "nodes": nodes,
        "edges": edges,
        "comm": comm,
        "tags": tags,
        "mounts": mounts,
    }


def _communities(edges: list[dict]) -> dict[str, int]:
    """louvain 社区 (live 边, w 权重): {atom_id_str: 社区序号}。

    ponytail: 每次 snapshot 全量重算 (4556 节点 ~百 ms 级); 增量节点不带 comm
    → 前端回退 hashColor, 下次刷新归位。networkx 缺 louvain (老版本) → {}。"""
    try:
        import networkx as nx
        from networkx.algorithms import community as nx_comm
    except ImportError:
        return {}
    g = nx.Graph()
    g.add_nodes_from(int(e["subject_id"]) for e in edges)
    g.add_nodes_from(int(e["object_id"]) for e in edges)
    for e in edges:
        a, b = int(e["subject_id"]), int(e["object_id"])
        if g.has_edge(a, b):
            if e["value"] > g[a][b]["weight"]:
                g[a][b]["weight"] = e["value"]
        else:
            g.add_edge(a, b, weight=e["value"])
    if not g.number_of_edges():
        return {}
    try:
        parts = nx_comm.louvain_communities(g, weight="weight", seed=42)
    except Exception:  # noqa: BLE001 — 老 nx 无 louvain / 收敛异常 → 无社区面
        return {}
    out: dict[str, int] = {}
    for i, part in enumerate(parts):
        for aid in part:
            out[str(aid)] = i
    return out


def _tag_bipartite(live_ids: set[int]) -> tuple[list[dict], list[dict]]:
    """semantic tag 旁挂面: tags=[{id,name,mounts}] + mounts=[{atom,tag,w}]。

    只收 semantic (factual session:/repo: 是来源标记非主题聚合, 画进来只会
    炸成几千个 session 枢纽); atom 侧只收快照内 live 原子。"""
    import db
    rows = db.get_conn().execute(
        "SELECT m.atom_id, m.tag_id, m.w, t.name FROM tag_mount m "
        "JOIN tag t ON t.id = m.tag_id WHERE t.kind = 'semantic' "
        "ORDER BY m.tag_id, m.atom_id").fetchall()
    mounts = [{"atom": str(r["atom_id"]), "tag": r["tag_id"],
               "w": r["w"]} for r in rows if r["atom_id"] in live_ids]
    cnt: dict[int, int] = {}
    for m in mounts:
        cnt[m["tag"]] = cnt.get(m["tag"], 0) + 1
    names = {r["tag_id"]: r["name"] for r in rows}
    tags = [{"id": t, "name": names.get(t, "?"), "mounts": c}
            for t, c in sorted(cnt.items(), key=lambda x: -x[1])]
    return tags, mounts


def delta(after_entity: int, after_fact: int) -> dict:
    """增量: 新 atom ∪ 新边端点 atom (并集补偿, 悬空边防线) + 新边。

    节点再按 degree>0 过滤: 与快照同口径。supersede 软删 (UPDATE valid_to)
    不变 rowid 不推 — 刷新页面重快照可见 (与 legacy LIF 衰减同边界)。
    """
    import db
    new_edges = _edge_rows("AND e.rowid > ?", (after_fact,))
    new_ent = db.get_conn().execute(
        "SELECT id FROM atom WHERE rowid > ? AND valid_to IS NULL",
        (after_entity,)).fetchall()
    ent_ids = {r["id"] for r in new_ent}
    ent_ids |= {e for r in new_edges
                for e in (r["subject_id"], r["object_id"])}
    deg = _degrees(_edge_rows())  # 全图度数: 增量节点的 degree 字段要真实值
    cur = db.get_conn().execute(
        "SELECT (SELECT COALESCE(MAX(rowid),0) FROM atom) e, "
        "(SELECT COALESCE(MAX(rowid),0) FROM atom_edge) f").fetchone()
    return {
        "cursor": {"entity": cur["e"], "fact": cur["f"]},
        "nodes": _entity_nodes({i for i in ent_ids if deg.get(i, 0) > 0}, deg),
        "edges": new_edges,
    }


# ── 导出 (外部工具口径) ─────────────────────────────────────────────

def export_json(out: Path, cwd: str | None = None) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot(cwd=cwd), ensure_ascii=False),
                   encoding="utf-8")
    return out


def export_csv(out_dir: Path, cwd: str | None = None) -> tuple[Path, Path]:
    """Cosmograph/Gephi Lite 口径: 边表带 created_at 时间列 (时间轴自动识别)。"""
    import csv as _csv
    snap = snapshot(cwd=cwd)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    nodes_p, edges_p = out_dir / "nodes.csv", out_dir / "edges.csv"
    with nodes_p.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["id", "name", "type", "degree", "created_at"])
        for n in snap["nodes"]:
            w.writerow([n["id"], n["name"], n["entity_type"],
                        n["degree"], n.get("valid_from") or ""])
    with edges_p.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["source", "target", "predicate", "label", "lif", "created_at"])
        for e in snap["edges"]:
            w.writerow([e["subject_id"], e["object_id"], e["predicate"],
                        e.get("value") or e["predicate"],
                        e["LIF"], e["created_at"]])
    return nodes_p, edges_p


# ── ctypes inotify 盯 WAL (零依赖, 无轮询) ──────────────────────────

# inotify 常量 (linux/inotify.h): 只关心"文件被写过/新建"事件族
_IN_MODIFY = 0x2        # 文件内容被写 (autocommit append 到 -wal)
_IN_CREATE = 0x100      # 目录内新建 (首次 commit 建 -wal)
_IN_MOVED_TO = 0x80     # 移入
_IN_CLOSE_WRITE = 0x8   # 写后关闭 (commit 收尾)
_EVENT_STRUCT = struct.Struct("iIII")  # wd, mask, cookie, name_len


class WalWatcher:
    """inotify 盯 DB 所在目录 → 过滤 DB 文件族事件 → 去抖 → 回调。后台线程。

    select 0.1s 只是为了让 stop() 可达 (不是 DB 轮询 — fd 事件驱动)。
    """

    def __init__(self, db_path: Path, on_commit,
                 debounce_s: float = _DEBOUNCE_S):
        self.db_path = Path(db_path)
        self.on_commit = on_commit
        self.debounce_s = debounce_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._inotify_fd: int | None = None
        self.events_seen = 0

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="wal-inotify")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.inotify_init1(os.O_NONBLOCK)
        if fd < 0:
            raise RuntimeError(f"inotify_init1 failed: errno={ctypes.get_errno()}")
        self._inotify_fd = fd
        mask = _IN_MODIFY | _IN_CREATE | _IN_MOVED_TO | _IN_CLOSE_WRITE
        wd = libc.inotify_add_watch(
            fd, str(self.db_path.parent).encode(), mask)
        if wd < 0:
            raise RuntimeError(
                f"inotify_add_watch failed: errno={ctypes.get_errno()}")
        watch_names = {self.db_path.name, self.db_path.name + "-wal",
                       self.db_path.name + "-shm"}
        pending_until: float | None = None
        buf = b""
        while not self._stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    buf += os.read(fd, 65536)
                except BlockingIOError:
                    pass
                while len(buf) >= _EVENT_STRUCT.size:
                    _, ev_mask, _, name_len = _EVENT_STRUCT.unpack_from(buf)
                    name = buf[_EVENT_STRUCT.size:_EVENT_STRUCT.size + name_len]
                    buf = buf[_EVENT_STRUCT.size + name_len:]
                    if (ev_mask & (0x1 | 0x200 | 0x8000)):  # Q_OVERFLOW|IGN_Q|UNMOUNT
                        continue
                    if name.rstrip(b"\0").decode(errors="replace") in watch_names:
                        self.events_seen += 1
                        pending_until = time.time() + self.debounce_s
            if pending_until is not None and time.time() >= pending_until:
                pending_until = None
                try:
                    self.on_commit()
                except Exception:  # noqa: BLE001 — 推送失败不影响 watcher 存活
                    pass
        os.close(fd)


# ── HTTP 服务 (快照 / 增量 / SSE 流 / 页面) ─────────────────────────

class GraphLive:
    """组合状态: 游标 + SSE 订阅队列 + inotify watcher。run_server 与测试共用。"""

    def __init__(self, db_path: Path | None = None,
                 debounce_s: float = _DEBOUNCE_S):
        self.db_path = Path(db_path) if db_path else \
            Path(__file__).parent / "data" / "memory.db"
        self.subscribers: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.push_count = 0
        self.last_delta: dict | None = None
        self.cursor = {"entity": 0, "fact": 0}
        self.watcher = WalWatcher(self.db_path, self._on_commit, debounce_s)

    def bootstrap(self) -> dict:
        """db.init 指向 self.db_path; 游标=当前 max(rowid) (只看启动后的生长)。"""
        import db
        db.init(self.db_path)
        self.cursor = dict(snapshot()["cursor"])
        return dict(self.cursor)

    def _on_commit(self):
        d = delta(self.cursor["entity"], self.cursor["fact"])
        if not d["nodes"] and not d["edges"]:
            return  # 无新行 ( 其它写入方触碰 wal 等误触发) — 不推
        self.cursor = d["cursor"]
        self.last_delta = d
        self.push_count += 1
        with self.lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(d)
                except queue.Full:
                    pass  # 订阅端卡死: 丢帧保 watcher, 端上可刷新重快照


class _Handler(BaseHTTPRequestHandler):
    gl: GraphLive = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, _HTML_PATH.read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/graph":
            q = parse_qs(u.query)
            if "after_f" in q:  # 增量拉取 (页面断线重连补偿用)
                d = delta(int(q.get("after_e", ["0"])[0]), int(q["after_f"][0]))
            else:
                d = snapshot(cwd=q.get("cwd", [None])[0])
            self._send(200, json.dumps(d, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
        elif u.path == "/api/stream":
            self._serve_stream()
        else:
            self._send(404, b"not found", "text/plain")

    def _serve_stream(self):
        q: queue.Queue = queue.Queue(maxsize=100)
        with self.gl.lock:
            self.gl.subscribers.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while True:
                try:
                    d = q.get(timeout=15)
                    payload = json.dumps(d, ensure_ascii=False)
                    self.wfile.write(f"event: delta\ndata: {payload}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # 15s 心跳防代理断链
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.gl.lock:
                if q in self.gl.subscribers:
                    self.gl.subscribers.remove(q)


def build_server(db_path: Path | None = None, host: str = "127.0.0.1",
                 port: int = 8765, debounce_s: float = _DEBOUNCE_S):
    """组装 GraphLive + ThreadingHTTPServer 并 bootstrap。测试/CLI 共用。"""
    gl = GraphLive(db_path=db_path, debounce_s=debounce_s)
    _Handler.gl = gl
    cursor = gl.bootstrap()
    server = ThreadingHTTPServer((host, port), _Handler)
    return gl, server, cursor


def run_server(host: str = "127.0.0.1", port: int = 8765,
               db_path: Path | None = None) -> int:
    """前台阻塞入口 (cli graph-live)。Ctrl-C 退出。"""
    import sys
    gl, server, cursor = build_server(db_path=db_path, host=host, port=port)
    gl.watcher.start()
    print(json.dumps({"url": f"http://{host}:{port}/", "db": str(gl.db_path),
                      "cursor": cursor}, ensure_ascii=False))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gl.watcher.stop()
        server.server_close()
    return 0

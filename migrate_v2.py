"""H5 简化版存量迁移: 全量蒸馏产物 (temp/full_*) → v2 新图五表.

spec: docs/specs/graph-reform-v2-ingest-tags.md §五 H5 (简化版, 旧数据可弃) —
fact 表保留为 legacy 归档不删不改, 本脚本只写 atom/atom_edge/tag/tag_mount,
幂等 (重跑零重复: atom 按文本去重, 边/挂载 INSERT OR IGNORE)。

溯源链: atom.members(unit id) → proto_units.json 句文 → 目标库 fact.topic 精确
匹配 → 原 fact 的 source_refs/source_cwd/created_at (匹配不到留空, 不臆测)。
事实tag铸币 (spec §六): session: 从 source_refs 恒可铸 (保底下界);
repo: 从 source_cwd 白名单 (~/projects/* 根下第一级 → repo:<名>), /tmp 等跳过。

用法:
  python3 migrate_v2.py              # dry-run: 只打印统计, 不动任何库 (源库只读连接)
  python3 migrate_v2.py <db_path>    # 装载到目标库 (db.init 先建全 schema)
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).parent
_REPO_ROOT = Path.home() / "projects"   # cwd 铸币白名单根 (spec §六)
_SESSION_TAIL = re.compile(r"#\d+$")   # "session:memory:x.md#3" → 会话级 tag 去尾
_LABELS = ("fact", "judgment", "experience", "summary")


def load_inputs(root: Path) -> dict:
    """读 temp/ 蒸馏产物; proto_units/summaries 缺席时溯源与文本兜底降级。"""
    atoms = json.loads((root / "temp/full_e_atoms.json").read_text(encoding="utf-8"))
    edges = []
    for l in (root / "temp/full_f_edges.jsonl").read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        try:
            edges.append(json.loads(l))  # F 尚在追加, 容忍撕裂尾行
        except json.JSONDecodeError:
            pass
    sp = root / "temp/full_a_summaries.json"
    summaries = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    up = root / "temp/proto_units.json"
    unit_sents: dict[int, list] = {}
    if up.exists():  # member 溯源唯一来源 (A 阶段致化句与 fact.topic 非精确匹配, 不可代)
        for u in json.loads(up.read_text(encoding="utf-8"))["units"]:
            unit_sents[u["id"]] = u.get("sentences") or []
    return {"atoms": atoms, "edges": edges, "summaries": summaries,
            "unit_sents": unit_sents}


def repo_tag(cwd: str | None) -> str | None:
    """cwd → 事实 repo tag 名; 白名单外 (/tmp, 其他根) → None (spec §六)。"""
    if not cwd:
        return None
    p = Path(cwd)
    while p.parent != _REPO_ROOT and p != p.parent:
        p = p.parent
    if p in (_REPO_ROOT, p.parent):
        return None
    return f"repo:{p.name}"


def session_tags(refs) -> set[str]:
    """source_refs 字符串集 → 会话级事实 tag 名 (去 #N 尾)。"""
    return {_SESSION_TAIL.sub("", r) for r in refs
            if isinstance(r, str) and r.startswith("session:")}


def _ro_conn(db_path: Path) -> sqlite3.Connection | None:
    """目标库只读连接 (不存在 → None); 迁移全程对源库零写。"""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("SELECT 1 FROM fact LIMIT 1")
    except sqlite3.DatabaseError:  # 无 fact 表的空库 → 无可溯源
        conn.close()
        return None
    return conn


def _topic_index(conn: sqlite3.Connection) -> dict[str, list]:
    """fact.topic → [(source_refs, source_cwd, created_at)] (一次全量, 免逐 atom 查)。"""
    idx: dict[str, list] = {}
    for topic, refs, cwd, created in conn.execute(
            "SELECT topic, source_refs, source_cwd, created_at FROM fact "
            "WHERE topic IS NOT NULL"):
        idx.setdefault(topic, []).append((refs, cwd, created))
    return idx


def plan(inp: dict, conn: sqlite3.Connection | None = None) -> tuple[list[dict], Counter]:
    """纯读装载计划: 每 atom 定文本/label/p_dur + member 溯源 + 事实 tag。"""
    idx = _topic_index(conn) if conn is not None else {}
    out: list[dict] = []
    st: Counter = Counter()
    for a in inp["atoms"]:
        members = a.get("members") or []
        text = a.get("text") or (inp["summaries"].get(str(members[0])) if members else "") or ""
        refs: set[str] = set()
        cwds: Counter = Counter()
        created: list[str] = []
        for m in members:
            for sent in inp["unit_sents"].get(m, []):
                for row_refs, row_cwd, row_created in idx.get(sent, ()):
                    try:
                        arr = json.loads(row_refs)
                    except (TypeError, ValueError):
                        arr = []
                    if isinstance(arr, list):
                        refs.update(x for x in arr if isinstance(x, str))
                    if row_cwd:
                        cwds[row_cwd] += 1
                    if row_created:
                        created.append(row_created)
        cwd = cwds.most_common(1)[0][0] if cwds else None   # 多 cwd 并存取多数决
        tags = session_tags(refs)
        rt = repo_tag(cwd)
        if rt:
            tags.add(rt)
        label = a.get("label") if a.get("label") in _LABELS else "fact"
        st["atoms"] += 1
        st["label_" + label] += 1
        if refs:
            st["atoms_with_refs"] += 1
        if cwd:
            st["atoms_with_cwd"] += 1
        for t in tags:
            st["tag_" + t.split(":", 1)[0]] += 1
        out.append({"aid": a.get("aid"), "text": text, "label": label,
                    "p_dur": float(a.get("p_dur_max") or 0.0),
                    "valid_from": min(created) if created else None,
                    "source_refs": json.dumps(sorted(refs), ensure_ascii=False) if refs else None,
                    "source_cwd": cwd, "tags": sorted(tags)})
    aids = {ap["aid"] for ap in out}
    st["edges"] = len(inp["edges"])
    st["edges_loadable"] = sum(
        1 for e in inp["edges"] if e.get("a") in aids and e.get("b") in aids
        and e.get("a") != e.get("b"))
    return out, st


def execute(db_path: str | Path, inp: dict, atoms: list[dict]) -> dict:
    """单事务装载 (atom 按文本幂等 / 边与挂载 OR IGNORE); 返回计数。"""
    import db  # 延迟 import: dry-run 路径零仓库副作用
    db.init(db_path)
    conn = db.get_conn()
    n_atom = n_edge = n_tag = n_mount = 0
    with db.transaction():
        aid: dict = {}   # 蒸馏 aid → 库 rowid (重跑时按文本寻回, 边重映射零重复)
        for ap in atoms:
            if not ap["text"]:
                continue
            row = conn.execute("SELECT id FROM atom WHERE text = ?",
                               (ap["text"],)).fetchone()
            if row:
                aid[ap["aid"]] = row[0]
                continue
            cur = conn.execute(
                "INSERT INTO atom(text, label, p_dur, valid_from, source_refs, "
                "source_cwd) VALUES(?,?,?,?,?,?)",
                (ap["text"], ap["label"], ap["p_dur"], ap["valid_from"],
                 ap["source_refs"], ap["source_cwd"]))
            aid[ap["aid"]] = cur.lastrowid
            n_atom += 1
        for e in inp["edges"]:
            a, b = aid.get(e.get("a")), aid.get(e.get("b"))
            if a is None or b is None or a == b:
                continue
            n_edge += conn.execute(
                "INSERT OR IGNORE INTO atom_edge(a_id, b_id, w, kind) "
                "VALUES(?,?,?,'related')",
                (min(a, b), max(a, b), float(e.get("w") or 0.0))).rowcount
        tid: dict[str, int] = {}
        for ap in atoms:
            for name in ap["tags"]:
                if name not in tid:
                    n_tag += conn.execute(
                        "INSERT OR IGNORE INTO tag(name, kind, level) "
                        "VALUES(?,'factual',1)", (name,)).rowcount
                    tid[name] = conn.execute(
                        "SELECT id FROM tag WHERE name = ? AND level = 1",
                        (name,)).fetchone()[0]
                # 事实铸币 = 确定性挂载 w=1.0 (laya 挂载分语义留给语义 tag/H4)
                n_mount += conn.execute(
                    "INSERT OR IGNORE INTO tag_mount(tag_id, atom_id, w) "
                    "VALUES(?,?,1.0)", (tid[name], aid[ap["aid"]])).rowcount
    return {"atoms": n_atom, "edges": n_edge, "tags": n_tag, "mounts": n_mount}


def report(st: Counter) -> str:
    return "\n".join([
        "═══ H5 v2 存量迁移 dry-run ═══",
        f"atoms: {st['atoms']} (label 分布: "
        + ", ".join(f"{k[6:]}={v}" for k, v in sorted(st.items())
                    if k.startswith("label_")) + ")",
        f"溯源覆盖: refs {st['atoms_with_refs']}/{st['atoms']}, "
        f"cwd {st['atoms_with_cwd']}/{st['atoms']}",
        f"事实 tag 挂载: session {st['tag_session']} + repo {st['tag_repo']}",
        f"edges: {st['edges_loadable']}/{st['edges']} 可装载",
    ])


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        conn = _ro_conn(_ROOT / "data" / "memory.db")
        _, st = plan(load_inputs(_ROOT), conn)
        if conn:
            conn.close()
        print(report(st))
        return
    target = Path(argv[1])
    inp = load_inputs(_ROOT)
    conn = _ro_conn(target)
    atoms, st = plan(inp, conn)
    if conn:
        conn.close()
    print(report(st))
    print(f"[execute] → {target}: {execute(target, inp, atoms)}")


if __name__ == "__main__":
    main(sys.argv)

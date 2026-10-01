"""mem-service bootstrap — CC memory .md → v2 新图 (ADR-12, v2 改造 2026-10-01).

cli ``init-memory`` entry: scan a CC memory dir (``*.md``), 逐文件经
``src/distill.py`` 段级一步蒸馏入 atom/tag 图 (graph-reform v2, spec §二/§五)。
旧 autodream/llm_extract (s,p,o) fact 管道退役为 legacy (fact 表归档, 不再写)。

- ``re_ingest_file`` (ADR-17 b/c): 单 md 增量, PostToolUse hook 调用点
  (hook → ``cli re-ingest <file> --cwd``, 签名不变)。md → 按空行切段
  (>1500 字段再按句号细分) → 逐段 ``distill.distill_segment``。
- 溯源: ``session_id="memory:<文件名>"`` → distill 事实tag 铸币产出
  ``session:memory:<文件名>`` (与库内存量迁移 tag 同形); ``cwd=md 所在目录``
  → atom.source_cwd; ``ts=file mtime`` → atom.valid_from。
- ADR-16f 投影跳过 (:29) 原样保留 (MEMORY.md / source:mem-service)。
- 幂等: distill 段内容 sha 去重 (distill_seen), 重跑同 md 零新增。
- ``prune_memory`` (ADR-17d v2 面): 文件删除 → 该文件独占 atom 置
  ``valid_to=now`` (双时态软删, spec D4); 多源 atom 不动。旧
  ``prune_deleted`` (fact 表) 保留为 legacy 工具, 生产入口已切 ``prune_memory``。

挂起语义 (裁决#5b): distill 外呼 (zhipu/laya) 不可用 → ``LayaUnavailable``
族响亮上抛等恢复, 不再产离线记忆; 段级 sha 幂等保重跑零重复。

Returns ``{"files": n, "segments": .., "atoms": .., "edges": .., "merged": ..}``.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import db
from src import distill as distill_mod  # v2 一步蒸馏 (H2)


# ── ADR-16f 过滤: 跳过 mem-service 投影产物 ─────────────────────────────
def _is_mem_service_projection(text: str, filename: str | None = None) -> bool:
    """检查 md frontmatter 是否含 source: mem-service (ADR-16f) 或 filename 是 MEMORY.md。

    MEMORY.md 是 CC 产出的索引投影,不应被 re-ingest (自指循环/噪音)。
    """
    # #1: filename 检测 (确定性,无需读内容)
    if filename is not None and filename == "MEMORY.md":
        return True
    # ADR-16f: frontmatter source:mem-service 检测
    if not text.startswith("---"):
        return False
    fm_end = text.find("---", 3)
    if fm_end == -1:
        return False
    fm_block = text[3:fm_end]
    return "source: mem-service" in fm_block or "source:mem-service" in fm_block


_SEG_MAX = 1500  # 段长帽: 超过按句号细分


def _segments(text: str) -> list[str]:
    """md → 段列表: 按空行切段; >1500 字的段再按句号细分 (累积不超帽)。

    无句号的长段保持整段 (ponytail: 硬帽是软目标, 切英文句点/换行的收益
    不抵歧义 — distill 内部还有 split_units 二道切)。空文本 → []。"""
    out: list[str] = []
    for seg in (s.strip() for s in re.split(r"\n\s*\n", text)):
        if not seg:
            continue
        if len(seg) <= _SEG_MAX:
            out.append(seg)
            continue
        buf = ""
        for sent in re.split(r"(?<=。)", seg):
            if buf and len(buf) + len(sent) > _SEG_MAX:
                out.append(buf)
                buf = sent
            else:
                buf += sent
        if buf:
            out.append(buf)
    return out


def _mtime_iso(p: Path) -> str:
    """file mtime → ISO-UTC (与库内 atom.valid_from 存量格式同形)。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(p.stat().st_mtime))


def re_ingest_file(
    file_path: str | Path,
    source_cwd: str | None = None,  # legacy 兼容位 (v2 溯源改 cwd=md 所在目录)
    providers: list | None = None,  # legacy 兼容位 (v2 distill 不用)
    harness: str = "cc",            # legacy 兼容位 (atom 图无 harness 列)
) -> dict[str, int]:
    """单 md → KG 增量 (ADR-17 b/c, v2 distill 径). 反向 re-ingest 手动触发点
    + PostToolUse hook 调用点 (``cli re-ingest`` 签名不变)。

    read md → 跳过 ADR-16f 投影 → 空行切段 (>1500 按句号细分) → 逐段
    ``distill.distill_segment(text, session_id="memory:<name>", cwd=md 所在
    目录, ts=mtime)``。返回 atom 口径计数 ``{segments, atoms, edges, merged,
    skipped_segs}``; 投影/索引 md → ``skipped=1``; 段被 distill 判重/毒
    (skipped key) → ``skipped_segs`` 计。挂起: ``LayaUnavailable`` 族上抛
    (段级幂等, 调用方重跑零重复)。"""
    file_path = Path(file_path)
    if not file_path.is_file():
        return {"segments": 0, "atoms": 0, "edges": 0, "merged": 0,
                "skipped_segs": 0, "skipped": 0,
                "error": f"Not a file: {file_path}"}
    text = file_path.read_text(encoding="utf-8")
    # ADR-16f: 跳过 mem-service 投影产物 + #1 跳过 MEMORY.md (自指循环)
    if _is_mem_service_projection(text, filename=file_path.name):
        return {"segments": 0, "atoms": 0, "edges": 0, "merged": 0,
                "skipped_segs": 0, "skipped": 1}

    # 溯源三件套: session tag 名与库内存量 session:memory:<name> 同形;
    # cwd=md 所在目录 (memory dir 不在 repo 白名单 → 只铸 session tag, §六);
    # ts=mtime (静态文档的时间轴, 非 ingest 时刻)。
    session_id = f"memory:{file_path.name}"
    cwd = str(file_path.parent)
    ts = _mtime_iso(file_path)

    totals = {"segments": 0, "atoms": 0, "edges": 0, "merged": 0, "skipped_segs": 0}
    for seg in _segments(text):
        r = distill_mod.distill_segment(seg, session_id=session_id, cwd=cwd, ts=ts)
        totals["segments"] += 1
        if r.get("skipped"):
            totals["skipped_segs"] += 1
        for k in ("atoms", "edges", "merged"):
            totals[k] += r.get(k, 0)
    return totals


def init_memory(
    memory_dir: str | Path,
    providers: list | None = None,  # legacy 兼容位
    fact_type: str = "permanent",   # legacy 兼容位 (distill 自带五类标签)
    source_cwd: str | None = None,  # legacy 兼容位 (透传 re_ingest_file, 不用)
    harness: str = "cc",            # legacy 兼容位
) -> dict[str, int]:
    """Seed the KG from CC memory ``.md`` files (ADR-12, v2 distill 径).

    逐文件走 :func:`re_ingest_file` (切段/投影跳过/挂起/幂等同其契约);
    计数 atom 口径。挂起: 任一文件 distill 外呼不可达 → ``LayaUnavailable``
    上抛, 已入图段保留 (段级 sha 幂等保重跑零重复)。``files`` 只计被
    ingest 的文件, 投影跳过计 ``skipped``。"""
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return {"files": 0, "segments": 0, "atoms": 0, "edges": 0, "merged": 0,
                "skipped_segs": 0, "skipped": 0, "error": str(memory_dir)}
    totals = {"files": 0, "segments": 0, "atoms": 0, "edges": 0, "merged": 0,
              "skipped_segs": 0, "skipped": 0}
    for md in sorted(memory_dir.glob("*.md")):
        r = re_ingest_file(md, source_cwd=source_cwd, harness=harness)
        if r.get("skipped") or r.get("error"):
            totals["skipped"] += 1 if r.get("skipped") else 0
            continue
        totals["files"] += 1
        for k in ("segments", "atoms", "edges", "merged", "skipped_segs"):
            totals[k] += r.get(k, 0)
    return totals


# ── ADR-17d DELETE 同步 (v2 面): md 删 → atom 双时态软删 ────────────────
def _native_md_names(mem_dir: Path) -> set[str]:
    """现存 native md 文件名集: 排除投影 (ADR-B ``MEM_FILE_RE`` + frontmatter
    二次确认) 与 ``MEMORY.md`` 索引。native 撞名 (mem-dead-notes.md) 留集防
    误判孤儿 (F1), 详见 prune_deleted docstring。"""
    from projection import MEM_FILE_RE
    if not mem_dir.is_dir():
        return set()  # ponytail: dir 都没了 → 所有 memory 源 atom 视为孤儿
    existing: set[str] = set()
    for p in mem_dir.glob("*.md"):
        if p.name == "MEMORY.md":
            continue
        if MEM_FILE_RE.match(p.name):
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                txt = ""
            if _is_mem_service_projection(txt):
                continue
        existing.add(p.name)
    return existing


def prune_memory(memory_dir: str | Path,
                 source_cwd: str | None = None,
                 dry_run: bool = False) -> dict:
    """CC memory md 删除 → 新图 atom 软删 (ADR-17d v2 面, D4 双时态)。

    候选 = ``session:memory:*`` 事实tag 挂载的 valid atom。scoping (同旧
    prune_deleted 纪律, 防跨项目误删): ``atom.source_cwd IN (source_cwd,
    memory_dir)`` — v2 re-ingest 落 memory_dir (md 所在目录), 存量迁移
    atom 落项目 cwd; 其他项目 / NULL 老数据不碰。atom 的 memory 源文件集
    (其全部 ``session:memory:<file>`` tag 名) 与现存 native md 集互斥 →
    全部源已删 → 独占 → ``valid_to=now``; 任一源仍在 (多源 atom) 不动。
    物理不 DELETE — 回滚 = valid_to 置回 NULL。

    返回 ``{checked, pruned, pruned_ids, native_md_present, dry_run}``。"""
    mem_dir = Path(memory_dir)
    existing = _native_md_names(mem_dir)
    scope_cwds = {str(c) for c in (source_cwd, mem_dir) if c}

    conn = db.get_conn()
    rows = conn.execute(
        "SELECT t.name AS tname, m.atom_id AS aid "
        "FROM tag t JOIN tag_mount m ON m.tag_id = t.id "
        "WHERE t.name LIKE 'session:memory:%'").fetchall()
    files_by_atom: dict[int, set[str]] = {}
    for r in rows:
        files_by_atom.setdefault(r["aid"], set()).add(
            r["tname"][len("session:memory:"):])

    to_prune: list[int] = []
    if files_by_atom:
        q = ",".join("?" * len(files_by_atom))
        for a in conn.execute(
                f"SELECT id, valid_to, source_cwd FROM atom WHERE id IN ({q})",
                list(files_by_atom)):
            if a["valid_to"] is None and a["source_cwd"] in scope_cwds \
                    and files_by_atom[a["id"]].isdisjoint(existing):
                to_prune.append(a["id"])
    if to_prune and not dry_run:
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        conn.executemany(
            "UPDATE atom SET valid_to=? WHERE id=? AND valid_to IS NULL",
            [(now, aid) for aid in to_prune])
    return {"checked": len(files_by_atom), "pruned": len(to_prune),
            "pruned_ids": sorted(to_prune),
            "native_md_present": sorted(existing), "dry_run": dry_run}


# ── legacy: fact 表 (v1 归档) prune, 生产入口已切 prune_memory ──────────
def prune_deleted(
    memory_dir: str | Path,
    source_cwd: str,
    dry_run: bool = False,
) -> dict:
    """[legacy] CC memory md 删除 → KG **fact** soft-delete 同步 (ADR-17d v1)。

    v2 起生产 prune 走 :func:`prune_memory` (atom 双时态面); 本函数保留作
    legacy fact 归档库的手动同步工具。扫 ``source_cwd`` 的 active fact, 按
    ``source_refs`` 里 ``memory:<filename>#`` 反查源 md; 源 md 全部已不在
    ``memory_dir`` → ``status='deleted'`` (可逆)。``dry_run`` 只报不删。

    返回 ``{checked, pruned, pruned_ids, native_md_present, dry_run}``。"""
    import json

    existing = _native_md_names(Path(memory_dir))
    mem_ref = re.compile(r"memory:([^#\]]+)#")
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, source_refs FROM fact WHERE status='active' AND source_cwd=?",
        (source_cwd,)).fetchall()

    to_prune: list[str] = []
    for r in rows:
        try:
            refs = json.loads(r["source_refs"])
        except (ValueError, TypeError):
            continue
        files: set[str] = set()
        for s in refs:
            if not isinstance(s, str):
                continue
            m = mem_ref.search(s)
            if m:
                files.add(m.group(1))
        if not files:
            continue  # 非 memory md 来源 (session 轨迹等), 跳过
        # 所有源 md 都不在现存集 → 删; 任一仍在则保留 (fact 可能多源)
        if files.isdisjoint(existing):
            to_prune.append(r["id"])

    if not dry_run and to_prune:
        conn.executemany(
            "UPDATE fact SET status='deleted' WHERE id=?",
            [(fid,) for fid in to_prune])
        # perf/vec-index: 软删同步删 vec_fact 行 (与 update_fact_status 同纪律)。
        import vec_index
        for fid in to_prune:
            vec_index.delete_fact(fid)
    return {"checked": len(rows), "pruned": len(to_prune),
            "pruned_ids": to_prune, "native_md_present": sorted(existing),
            "dry_run": dry_run}


def _demo() -> None:  # ponytail self-check (stub distill, no network/db)
    import tempfile
    from types import SimpleNamespace

    calls = []
    stub = SimpleNamespace(
        distill_segment=lambda text, session_id, cwd, ts:
        (calls.append((session_id, cwd)), {"atoms": 1, "edges": 0, "merged": 0,
                                           "supersede_proposals": []})[1])
    global distill_mod
    real = distill_mod
    distill_mod = stub
    try:
        d = tempfile.mkdtemp()
        Path(d, "x.md").write_text("用户使用 rust", encoding="utf-8")
        r = init_memory(d)
    finally:
        distill_mod = real
    assert r["atoms"] >= 1 and calls[0][0] == "memory:x.md", (r, calls)
    print("init_memory ok:", r)


if __name__ == "__main__":
    _demo()

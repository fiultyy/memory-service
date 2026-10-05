"""记忆文件 watchdog — 双端 md 文件最轻 ingest 通道 (2026-10-05 泛化)。

**双端 watch 源** (原 openclaw_watch, T4 泛化):
- Claw: {MEM_OPENCLAW_ROOT}/workspace*/memory/ (topics-*.md 一文件一事实
  带 CC 同款 frontmatter / 日记 / summary, agent 自主维护) — session_id
  `openclaw:<ws>`
- CC: {MEM_CC_MEMORY_ROOT} 缺省 ~/.claude/projects/*/memory/ (用户驱动落盘
  精华 md — 手动 re-ingest 通道退役, 双端能力并集) — session_id `cc:<enc>`

不走 hooks — daemon 主循环每 cycle 调 sweep() 轮询目录 (glob+sha)。

文件 = 持久队列: 失败 = 水位不推进 = 下轮重试, 不需要 spool
(spool 是为 CC 内存态 transcript 快照发明的)。文件一事实与 v4
段级车道 (distill_chunk) 天然对齐, frontmatter description 现成 gist。

去重两层: 文件 sha 快路径 (openclaw_seen 表, 键=path 双端共用天然隔离) +
段级 distill_seen (sha="chunk:"+text) — 文件重组后未变段零调用, 变段自动
走 cov (_COV_SUPERSEDE) 消融通道管新旧更替 (CC md 与 transcript 双写同
一事实也由它管)。

**跳过规则 (两端同款, 防自指)**: MEMORY.md (投影索引非知识) +
`mem-{4hex}-*.md` 散件 (recall 投影产物 — 进 ingest = 投影→蒸馏→再投影
循环)。

env: MEM_OPENCLAW_ROOT (缺省 ~/.openclaw; 空 → Claw 源关) |
MEM_CC_MEMORY_ROOT (缺省 ~/.claude/projects; 显式空 → CC 源关 — 单测
fixture 用) | MEM_OPENCLAW_BATCH (每 cycle 文件帽, 缺省 20 — 存量回填
分批不占死主循环)
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import db
from distill import LayaUnavailable, distill_chunk

_H2 = re.compile(r"^## ", re.M)
_H3 = re.compile(r"^### ", re.M)
_JUDGE_CAP = 6000  # 对齐 _judge_chunk 判官帽 (distill.py); atom.text 仍存全文

# 跳过规则 (两端同款, 防自指/防索引污染): MEMORY.md 投影索引 +
# mem-{4hex}-*.md recall 投影散件 + recall-<日期>.md 投影报告 (GA review
# 2026-10-05: 报告进 distill = KG 召回产出回灌 KG 自指; 精确日期型 —
# recall-trail-grill-findings.md 等真知识文件不匹配)。
# 挂账: projection.py MEM_FILE_RE 若放宽 {4,6} hex, 此处 {4} 须同步。
_SKIP_RE = re.compile(r"^(?:mem-[0-9a-f]{4}-.*|recall-[0-9]{8}|MEMORY)\.md$")

# 段消费硬超时守护 (2026-10-05 生产卡死实录: zhipu 挂死 300s×3 重试连环,
# daemon 主循环静默 30min) — 与 mem_daemon._distill_segment_hard 同款:
# 单工池 + 超时 shutdown(wait=False) 重建, 僵尸线程由 provider socket 超时
# 自行退出。超时按文件失败计 (attempts+1, 3 次 poison); 僵尸迟到成功落库
# 由 distill_seen 段 sha 幂等吸收, 零重复入图。
_SEG_TIMEOUT = 420.0
_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="oc-distill")


def _distill_hard(fn) -> dict:
    global _POOL
    fut = _POOL.submit(fn)
    try:
        return fut.result(timeout=_SEG_TIMEOUT)
    except concurrent.futures.TimeoutError:
        _POOL.shutdown(wait=False)
        _POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="oc-distill")
        raise


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_frontmatter(text: str) -> dict:
    """首 --- 块内逐行 key: value (字符串级, 零 yaml 依赖 — bootstrap/projection
    同惯例)。坏/无 frontmatter → {} (调用方兜底文件首行)。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^(name|description):\s*(.*)$", line)
        if m and m.group(2).strip().strip('"'):
            meta[m.group(1)] = m.group(2).strip().strip('"')
    return meta


def _split_by(lines: list[str], pat: re.Pattern) -> list[str]:
    """按标题行分组 (保留标题行在组内); 无命中 → 整块。"""
    idxs = [i for i, ln in enumerate(lines) if pat.match(ln)]
    if not idxs:
        return ["\n".join(lines)]
    blocks, start = [], 0
    for i in idxs + [len(lines)]:
        if i > start:
            blocks.append("\n".join(lines[start:i]).strip("\n"))
        if i < len(lines):
            start = i
    return [b for b in blocks if b]


def _chunk_md(text: str) -> list[tuple[str, str]]:
    """md → [(seg_text, gist)]: ≤6000 单段; 超长按 H2 → H3 → 硬切。
    gist = frontmatter description (首段) / H2 标题行 / 文件首行标题兜底。"""
    fm = _parse_frontmatter(text)
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            body = text[end + 4:]
    body = body.strip("\n")
    if not body:
        return []
    lines = body.splitlines()
    # H1 提取为 title (gist 素材) 后剥离, 避免独立空占位段
    title = next((ln.lstrip("# ").strip() for ln in lines
                  if ln.startswith("# ")), "")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        body = "\n".join(lines).strip("\n")
        if not body:
            return []
    first_gist = fm.get("description") or title or "openclaw memory item"
    if len(body) <= _JUDGE_CAP:
        return [(body, first_gist)]
    segs: list[tuple[str, str]] = []
    for h2 in _split_by(lines, _H2):
        head = h2.splitlines()[0].lstrip("# ").strip()
        # 首段用 description (整文件级精炼); 其余 "文件标题: H2 标题"
        # 保 gist 对段落的指向性
        gist = first_gist if not segs else \
            (f"{title}: {head}" if head else first_gist)
        if len(h2) <= _JUDGE_CAP:
            segs.append((h2, gist))
            continue
        sub = _split_by(h2.splitlines(), _H3)
        for s in sub:
            if len(s) <= _JUDGE_CAP:
                segs.append((s, gist))
            else:  # 硬切
                segs.extend((s[i:i + _JUDGE_CAP], gist)
                            for i in range(0, len(s), _JUDGE_CAP))
    return segs


def _watch_dirs() -> list[tuple[str, Path]]:
    """双端 watch 源 → [(session_id, memory_dir)] (T4 泛化)。

    Claw: {MEM_OPENCLAW_ROOT}/workspace*/memory/ → session_id
    ``openclaw:<ws>``; CC: {MEM_CC_MEMORY_ROOT} 缺省 ~/.claude/projects/
    */memory/ → session_id ``cc:<enc>``。空 env 值 = 该源关 (显式逃生口)。"""
    out: list[tuple[str, Path]] = []
    oc_root = os.environ.get("MEM_OPENCLAW_ROOT", "")
    if oc_root:
        ws = Path(oc_root).expanduser()
        if ws.is_dir():
            out += sorted(
                (f"openclaw:{d.parent.name.removeprefix('workspace-')}", d)
                for d in ws.glob("workspace*/memory") if d.is_dir())
    cc_root = os.environ.get(
        "MEM_CC_MEMORY_ROOT", str(Path.home() / ".claude" / "projects"))
    if cc_root:
        cc = Path(cc_root).expanduser()
        if cc.is_dir():
            out += sorted((f"cc:{d.parent.name}", d)
                          for d in cc.glob("*/memory") if d.is_dir())
    return out


def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS openclaw_seen (
            path       TEXT PRIMARY KEY,
            sha256     TEXT NOT NULL,
            attempts   INTEGER NOT NULL DEFAULT 0,
            status     TEXT NOT NULL DEFAULT 'ok'
                        CHECK(status IN ('ok','poison')),
            updated_at TEXT NOT NULL
        )""")


def sweep() -> dict:
    """一轮 watchdog: 新/变文件切段入图。挂起语义 = raise 上抛 LayaUnavailable
    (daemon 本轮放弃, 下轮再来); 毒文件 3 试后 status=poison 跳过。"""
    dirs = _watch_dirs()
    if not dirs:
        return {"files": 0, "segments": 0, "skipped": 0}
    batch = int(os.environ.get("MEM_OPENCLAW_BATCH", "20"))
    conn = db.get_conn()
    _ensure_table(conn)
    seen = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT path, sha256, status FROM openclaw_seen")}
    todo: list[tuple[str, Path, str]] = []  # (session_id, file, sha)
    for sid, d in dirs:
        for f in sorted(d.glob("*.md")):
            if _SKIP_RE.match(f.name):
                continue  # MEMORY.md / mem-* 散件永不进 ingest 车道
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            row = seen.get(str(f))
            if row and row[0] == sha:
                continue  # 快路径: 未变文件零处理
            if row and row[1] == "poison":
                continue
            todo.append((sid, f, sha))
    skipped = max(0, len(todo) - batch)  # 本轮批帽外余量 (下轮吃)
    todo = todo[:batch]
    n_seg = 0
    for sid, f, sha in todo:
        try:
            segs = _chunk_md(f.read_text(encoding="utf-8", errors="replace"))
            ts = _now()
            for seg, gist in segs:
                _distill_hard(
                    lambda s=seg, g=gist: distill_chunk(
                        s, g, sid, str(f), ts))
                n_seg += 1
        except LayaUnavailable:
            raise  # 挂起: 水位不推进, 不计 attempts, 本轮放弃
        except Exception:
            # 失败记空 sha (= 从未成功消化此内容): 下轮 sha 不匹配 → 重试;
            # 记真 sha 会被快路径误判已处理 (实测 attempts 卡 1 的根因)
            att = conn.execute(
                "SELECT attempts FROM openclaw_seen WHERE path=?",
                (str(f),)).fetchone()
            n = (att[0] if att else 0) + 1
            status = "poison" if n >= 3 else "ok"
            conn.execute(
                "INSERT INTO openclaw_seen(path, sha256, attempts, status, "
                "updated_at) VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE "
                "SET attempts=?, status=?, updated_at=?",
                (str(f), "", n, status, _now(), n, status, _now()))
            conn.commit()
            continue
        conn.execute(
            "INSERT INTO openclaw_seen(path, sha256, attempts, status, "
            "updated_at) VALUES(?,?,0,'ok',?) ON CONFLICT(path) DO UPDATE "
            "SET sha256=?, attempts=0, status='ok', updated_at=?",
            (str(f), sha, _now(), sha, _now()))
        conn.commit()
    return {"files": len(todo), "segments": n_seg, "skipped": skipped}


if __name__ == "__main__":  # 手动 dry: python3 src/memory_watch.py
    print(sweep())

"""OpenClaw 记忆文件 watchdog — 最轻 ingest 通道 (2026-10-05 适配)。

OpenClaw 记忆机制 = 每 workspace `memory/` 目录下的 md 文件
(topics-*.md 一文件一事实带 CC 同款 frontmatter / 日记 / summary),
agent 自主维护 + maintenance cron 重组。不走 hooks — daemon 主循环
每 cycle 调 sweep() 轮询目录 (glob+sha, 144 文件 <50ms)。

文件 = 持久队列: 失败 = 水位不推进 = 下轮重试, 不需要 spool
(spool 是为 CC 内存态 transcript 快照发明的)。文件一事实与 v4
段级车道 (distill_chunk) 天然对齐, frontmatter description 现成 gist。

去重两层: 文件 sha 快路径 (openclaw_seen) + 段级 distill_seen
(sha="chunk:"+text) — 文件重组后未变段零调用, 变段自动走 cov
(_COV_SUPERSEDE) 消融通道管新旧更替。

env: MEM_OPENCLAW_ROOT (缺省 ~/.openclaw; 空 → 功能关) |
MEM_OPENCLAW_BATCH (每 cycle 文件帽, 缺省 20 — 存量回填分批不占死主循环)
"""
from __future__ import annotations

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
    """{ROOT}/workspace*/memory/ 存在即监控 (新 workspace 自动进)。"""
    root = os.environ.get("MEM_OPENCLAW_ROOT", "")
    if not root:
        return []
    ws = Path(root).expanduser()
    if not ws.is_dir():
        return []
    return sorted((d.parent.name.removeprefix("workspace-"), d)
                  for d in ws.glob("workspace*/memory") if d.is_dir())


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
    todo: list[tuple[str, Path, str]] = []  # (ws, file, sha)
    for ws, d in dirs:
        for f in sorted(d.glob("*.md")):
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            row = seen.get(str(f))
            if row and row[0] == sha:
                continue  # 快路径: 未变文件零处理
            if row and row[1] == "poison":
                continue
            todo.append((ws, f, sha))
    skipped = max(0, len(todo) - batch)  # 本轮批帽外余量 (下轮吃)
    todo = todo[:batch]
    n_seg = 0
    for ws, f, sha in todo:
        try:
            segs = _chunk_md(f.read_text(encoding="utf-8", errors="replace"))
            ts = _now()
            for seg, gist in segs:
                distill_chunk(seg, gist, f"openclaw:{ws}", str(f), ts)
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


if __name__ == "__main__":  # 手动 dry: python3 src/openclaw_watch.py
    print(sweep())

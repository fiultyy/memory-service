"""mem-service → CC memory 投影(ADR-15 分布式 index, 真嵌入 CC)。

KG 高 LIF fact → CC memory/mem-<id>.md(实体文件, CC Read/description 召回工作)
+ MEMORY.md append/update [mem] 索引行(幂等, mem-<id> 匹配)。

散 index 标记: 投影 md frontmatter `source: mem-service` + MEMORY.md 索引行 [mem]
区分 CC 原生 memory(用户/agent 手写)。指向双混: 文件 link(CC Read)+ kg://fact/<id>(mem 召回)。

触发: PreCompact(autodream 后)+ SessionStart hook / cli synthesis-index(P3 已清退 build-index)。

ADR-15 P2: ``synthesis_index`` 是 MEMORY [mem] 的**唯一写入口**——扫散落
mem-<id>.md(各路径建/刷的 snaptag 载体)→ 回 KG 取现值 → 对账重写 MEMORY [mem] 段。
recall/autodream 只建 mem-<id>.md(实体文件, 散 index), 不碰 MEMORY。冷启动空跳过
不兜底; orphan [mem] 行永远删(orphan 文件删默认关)。批量 IN 查询免 N+1。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


def cc_memory_dir(cwd: str) -> Path:
    """cwd → CC project-scoped memory dir(~/.claude/projects/<encoded>/memory/)。
    encoded: '/' → '-', '.' → '-'(CC 规则; /home/yy/.claude → -home-yy--claude)。"""
    encoded = cwd.replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded / "memory"


def _mem_filename(fact_id: str) -> str:
    return f"mem-{fact_id}.md"


def project_fact_md(fact: dict, subject: str, mem_dir: Path,
                    recalled_at: str | None = None) -> Path:
    """投影单 fact → mem-<id>.md(snaptag 物化载体, ADR-15)。

    瘦 frontmatter: 只 ``source``/``fact_id``/``recalled_at``/``description``
    (synthesis 经 ``fact_id`` 回 KG 取 confidence/LIF 现值, 不存原始重算)。
    **原子写** ``.tmp`` + ``os.replace``(防 synthesis 扫读半写 TOCTOU)。幂等(同 fact_id 重写)。"""
    fid = fact["id"]
    p = mem_dir / _mem_filename(fid)
    pred = fact.get("predicate") or ""
    val = fact.get("value") or ""
    lif = float(fact.get("LIF") or 0)
    display = f"{subject} {pred} {val}".strip()
    content = f"""---
description: {display}(mem-service KG fact, LIF {lif:.2f})
source: mem-service
fact_id: {fid}
recalled_at: {recalled_at or ''}
---
# {display}

- subject: {subject}
- predicate: {pred}
- value: {val}
- source_refs: {fact.get('source_refs') or []}
- kg://fact/{fid}

召回扩展: cli recall "{subject}" 或 recall kg://fact/{fid}
"""
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, p)  # 原子: 防 synthesis 扫读到半写
    return p


def read_fact_id(md_path: Path) -> str | None:
    """从 ``mem-*.md`` **frontmatter**(首 ``---...---`` 块)读 ``fact_id``。

    限定 frontmatter 块: body 里若出现 ``fact_id:`` 也不误匹配(防幽灵 id)。
    容错: 文件缺/损坏/无 frontmatter/无 fact_id → None(synthesis 跳过, 不崩)。"""
    try:
        text = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    m = re.search(r"^fact_id:\s*(\S+)\s*$", text[3:end], re.MULTILINE)
    return m.group(1) if m else None


# ── ADR-15 P2: synthesis_index (MEMORY [mem] 唯一写入口) ────────────
# 散 index 对账: 扫 mem-<id>.md(各路径建/刷)→ 回 KG 取现值 → 对账重写 MEMORY [mem]。
# recall/autodream 只建 mem-<id>.md(散 index 实体文件), synthesis 集中收口写 MEMORY。
# build_index 已 P3 清退; synthesis 是 MEMORY [mem] 唯一写入口。

# orphan [mem] 索引行匹配: 新格式(``- {display} · [mem](memory/mem-<id>.md) — ...``)
# + 历史 build_index 残留格式(``- [mem] {display}(memory/mem-<id>.md) — ...``)。两者都
# 含 (memory/mem- 前缀, 用此子串过滤覆盖两格式, 保 index 干净(orphan 行永远删)。
_MEM_LINE_MARKER = "(memory/mem-"


def synthesis_index(cwd: str, mem_dir: Path | str, session_id: str | None = None) -> dict:
    """对账散 mem-<id>.md → 回 KG 取现值 → 重写 MEMORY.md [mem] 索引(唯一写入口)。

    1. 扫 ``mem_dir/*.md``(排除 MEMORY.md/*.tmp)→ ``read_fact_id`` 收 (fact_id, path);
       损坏/无 fact_id 跳过(容错)。
    2. 冷启动: 无有效 fact_id → 清 MEMORY 的 [mem] 行(保非 [mem]), 返回 cold_start。
       **不 top-K 兜底**(grill 裁决: 兜底违散 index 语义)。
    3. 批量回 KG ``WHERE id IN (...)`` 一次查 present facts(非 N+1);
       orphan = fact_ids 不在 present。
    4. 批量 ``entity WHERE id IN (present.subject_id)`` 取实体名。
    5. ``scoring.mem_score`` desc 排序(PPR 默认关, 留 .env MEM_SYNTH_PPR 占位)。
    6. 对账写 MEMORY: 删所有含 ``_MEM_LINE_MARKER`` 的行(orphan 行永远删), 保 CC 原生行,
       append 本次 present facts。
    7. orphan 文件: ``MEM_SYNTH_PRUNE_ORPHANS=1`` 才删(``MEM_SYNTH_ORPHAN_BACKUP=1`` rename
       ``.orphan.bak`` 否则 unlink);默认 off 留文件。

    Returns:
        ``{projected, orphans, pruned, cold_start}``。
    """
    import db

    mem_dir_p = Path(mem_dir)
    mem_dir_p.mkdir(parents=True, exist_ok=True)  # 冷启动/全新项目: MEMORY.md 父目录可能不存在
    memory_md = mem_dir_p / "MEMORY.md"

    # 1. 扫 mem-<id>.md → (fact_id, path)。MEMORY.md 与 *.tmp 排除。
    found: list[tuple[str, Path]] = []
    if mem_dir_p.is_dir():
        for p in mem_dir_p.glob("*.md"):
            if p.name == "MEMORY.md" or p.suffix == ".tmp" or p.name.endswith(".tmp"):
                continue
            fid = read_fact_id(p)
            if fid:
                found.append((fid, p))
    fact_ids = [fid for fid, _ in found]
    path_by_id = {fid: p for fid, p in found}

    # 2. 冷启动: 无有效 fact_id → 清 [mem] 行(保非 [mem]), 不兜底。
    if not fact_ids:
        _rewrite_mem_lines(memory_md, [])
        return {"projected": 0, "orphans": 0, "pruned": 0, "cold_start": True}

    # 3. 批量回 KG(WHERE id IN (...), 一次非 N+1)。
    conn = db.get_conn()
    placeholders = ",".join("?" * len(fact_ids))
    rows = conn.execute(
        f"SELECT id, subject_id, predicate, value, LIF, confidence, source_cwd "
        f"FROM fact WHERE id IN ({placeholders})",
        fact_ids,
    ).fetchall()
    present_ids = {r["id"] for r in rows}
    # ponytail: 直接构 fact dict 喂 mem_score(只需 id/LIF/confidence + subject_id/predicate/value);
    # 不走 store._decode_fact(那 SELECT * 取全列, 这里已窄选 7 列, 避免重查)。
    # store 未 import; 此处纯读 row, 无需 store。
    facts = [
        {
            "id": r["id"],
            "subject_id": r["subject_id"],
            "predicate": r["predicate"],
            "value": r["value"],
            "LIF": r["LIF"],
            "confidence": r["confidence"],
            "source_cwd": r["source_cwd"],
        }
        for r in rows
    ]
    orphan_ids = [fid for fid in fact_ids if fid not in present_ids]

    # 4. 批量实体名(entity WHERE id IN (present.subject_id))。
    subj_ids = {f["subject_id"] for f in facts if f.get("subject_id")}
    ent_names: dict[str, str] = {}
    if subj_ids:
        eph = ",".join("?" * len(subj_ids))
        ent_names = {
            r["id"]: r["name"]
            for r in conn.execute(
                f"SELECT id, name FROM entity WHERE id IN ({eph})",
                tuple(subj_ids),
            ).fetchall()
        }

    # 5. mem_score desc 排序(PPR 默认关, .env MEM_SYNTH_PPR 占位; 此处纯 LIF+confidence)。
    import scoring
    facts.sort(key=lambda f: scoring.mem_score(f), reverse=True)

    # 6. 对账写 MEMORY.md: 删 orphan/旧 [mem] 行(永远删), append 本次 present(已排序)。
    new_lines = [
        _format_mem_line(
            f,
            ent_names.get(f["subject_id"], "?"),
        )
        for f in facts
    ]
    _rewrite_mem_lines(memory_md, new_lines)

    # 7. orphan 文件处理(默认 off 留文件; env 开启才删/备份)。
    pruned = 0
    if orphan_ids and os.environ.get("MEM_SYNTH_PRUNE_ORPHANS") == "1":
        backup = os.environ.get("MEM_SYNTH_ORPHAN_BACKUP") == "1"
        for fid in orphan_ids:
            p = path_by_id.get(fid)
            if not p or not p.exists():
                continue
            if backup:
                p.rename(p.with_suffix(p.suffix + ".orphan.bak"))
            else:
                p.unlink()
            pruned += 1

    return {
        "projected": len(facts),
        "orphans": len(orphan_ids),
        "pruned": pruned,
        "cold_start": False,
    }


def _sanitize(s: str | None) -> str:
    """换行/回车 → 空格 + 收尾空白剥离(防 [mem] 索引行被切成多物理行 → 破坏幂等 + 污染 MEMORY)。"""
    return (s or "").replace("\r", " ").replace("\n", " ").strip()


def _format_mem_line(fact: dict, subj_name: str) -> str:
    """synthesis [mem] 索引行(新格式, ADR-15 P2): ``- {display} · [mem](memory/mem-<id>.md) — score {ms} · kg://fact/<id>``。"""
    import scoring
    fid = fact["id"]
    display = f"{_sanitize(subj_name) or '?'} {_sanitize(fact.get('predicate'))} {_sanitize(fact.get('value'))}".strip()[:60]
    ms = scoring.mem_score(fact)
    return (f"- {display} · [mem](memory/mem-{fid}.md) — "
            f"score {ms:.2f} · kg://fact/{fid}")


def _rewrite_mem_lines(memory_md: Path, new_lines: list[str]) -> None:
    """删 MEMORY.md 中所有含 ``_MEM_LINE_MARKER`` 的行(orphan/旧 [mem] 行永远删),
    保 CC 原生行(非 [mem]), append new_lines。幂等(每次重写 = 本次精确集合)。"""
    existing = memory_md.read_text(encoding="utf-8") if memory_md.exists() else ""
    out = [ln for ln in existing.splitlines() if _MEM_LINE_MARKER not in ln]
    out.extend(new_lines)
    memory_md.write_text("\n".join(out) + "\n", encoding="utf-8")

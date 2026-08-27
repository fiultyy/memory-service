"""mem-service → CC memory 投影(ADR-15 分布式 index, 真嵌入 CC; ADR-A/B/C 原生格式)。

KG 高 LIF fact → CC memory/mem-{4hex}-{slug}.md(原生 .md 文件, CC Read/description 召回工作)
+ MEMORY.md append/update 原生索引行(``- [Title](mem-...md) — hook``, 幂等)。

ADR-A (原生格式): 投影索引行/文件让 CC 原生召回机制能消费 —— 索引行严格遵循原生
``- [Title](file.md) — hook``(无 [mem] 标记/无 score/kg://), 投影 .md frontmatter
``description`` = 干净明文(= topic), 正文自包含完整 fact(保留 kg://fact/<id> 作可选溯源)。
ADR-B (文件名严格契约): ``MEM_FILE_RE = ^mem-[0-9a-f]{4}-.+\\.md$`` 全仓唯一, 创建/识别/
MEMORY 重写三处共用 + frontmatter ``source: mem-service`` 双闸。``mem-`` + 4hex 前缀天然区分
native(``mem-service-*`` 的 ``serv`` 非 4-hex, 不匹配)。
ADR-C (topic): LLM 抽取时每条 edge 附一句话可读 topic, 流经 adapter._vote 透传 →
cli/autodream 消费 → 投影用作 slug 源 + 索引标题 + description。

触发: PreCompact(autodream 后)+ SessionStart hook / cli synthesis-index(P3 已清退 build-index)。

ADR-15 P2: ``synthesis_index`` 是 MEMORY 投影行的**唯一写入口**——扫散落
mem-{4hex}-{slug}.md(各路径建/刷的 snaptag 载体)→ 回 KG 取现值 → 对账重写 MEMORY 投影段。
recall/autodream 只建 .md(实体文件, 散 index), 不碰 MEMORY。冷启动空跳过不兜底;
orphan 投影行永远删(orphan 文件删默认关)。批量 IN 查询免 N+1。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


# ── ADR-B: 文件名严格契约(全仓唯一正则常量) ───────────────────────────
# mem-{fact_id[:4]}-{sanitize(topic)}.md。``mem-`` + 4hex + ``-`` 前缀天然区分 native
# (mem-service-* 的 ``serv`` 非 4-hex, 不匹配 → 不被误判投影)。创建侧 _mem_filename
# 断言匹配此 RE(不匹配 raise, 不静默写); 识别侧(synthesis scan / bootstrap.prune)共用。
MEM_FILE_RE = re.compile(r"^mem-[0-9a-f]{4}-.+\.md$")


def cc_memory_dir(cwd: str) -> Path:
    """cwd → CC project-scoped memory dir(~/.claude/projects/<encoded>/memory/)。
    encoded: '/' → '-', '.' → '-'(CC 规则; /home/yy/.claude → -home-yy--claude)。"""
    encoded = cwd.replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded / "memory"


_SLUG_MAX = 60  # 字符上限: 全中文 60 字 = 180 字节 + "mem-xxxx-.md" ≈ 193 < ext4 NAME_MAX 255


def _sanitize_slug(s: str | None) -> str:
    """topic → 路径安全 slug(ADR-B sanitize 约束)。

    只做路径/URL/markdown 安全, **不改召回语义** —— topic 原文完整保留在
    frontmatter description / 正文标题(召回靠那里), slug 仅作文件名标识。
    ``/`` 与空白 → ``_``; 删 URL/markdown/YAML 不安全字符; 收尾剥离; 截断
    防 >255 字节文件名致 ``os.replace``/``write`` 抛 OSError。空 → ``fact`` 占位。"""
    s = (s or "").strip()
    if not s:
        return "fact"
    s = re.sub(r"[\s/]+", "_", s)
    s = re.sub(r"[()\[\]<>\"'`|\\^]", "", s)  # URL/markdown/YAML 不安全字符删除
    s = s[:_SLUG_MAX].strip("_")
    return s or "fact"


def _fact_topic(fact: dict, subj_name: str) -> str:
    """ADR-A/C: 投影标题/description 的唯一来源 = topic。

    优先 ``fact['topic']``(ADR-C LLM 生成); 缺失/空 → 回退三元组拼接
    ``{subject} {predicate} {value}``(向后兼容, 保投影可用)。
    返回单行(strip 换行, 防破坏索引行)。"""
    topic = (fact.get("topic") or "").strip()
    if topic:
        return topic.replace("\r", " ").replace("\n", " ").strip()
    pred = fact.get("predicate") or ""
    val = fact.get("value") or ""
    display = f"{subj_name or '?'} {pred} {val}".strip()
    return display.replace("\r", " ").replace("\n", " ").strip()


def _yaml_scalar(s: str) -> str:
    """YAML scalar 安全输出: 含特殊字符则双引号包裹并转义(F4)。

    description 是 CC 召回命中的字段, 必须保证 ``yaml.safe_load`` 能解析。
    quote 后 ``"`` 是可见边界字符, topic 原文仍是 description 的子串,
    不破坏 CC 明文子串召回。"""
    s = s.replace("\r", " ").replace("\n", " ").strip()
    if not s:
        return '""'
    if re.search(r'[:#"\'\[\]{}`]', s) or s[0] in "!&*?|-+>%@":
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _md_link_text(s: str) -> str:
    """markdown link text 安全: 转义 ] [ \\(不破坏 CC 明文召回, 子串仍命中)(F7)。"""
    return s.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _mem_filename(fact_id: str, topic: str | None = None) -> str:
    """ADR-B: ``mem-{fact_id[:4]}-{sanitize(topic)}.md``。

    断言匹配 ``MEM_FILE_RE``: 不匹配 raise(ValueError)—— 不静默写坏契约文件。
    ``topic`` 缺失 → ``_sanitize_slug`` 回退 ``fact`` 占位。"""
    slug = _sanitize_slug(topic)
    name = f"mem-{fact_id[:4]}-{slug}.md"
    if not MEM_FILE_RE.match(name):
        raise ValueError(
            f"projection filename violates MEM_FILE_RE: {name!r} "
            f"(fact_id={fact_id!r}, topic={topic!r})")
    return name


def project_fact_md(fact: dict, subject: str, mem_dir: Path,
                    recalled_at: str | None = None) -> Path:
    """投影单 fact → ``mem-{4hex}-{slug}.md``(ADR-A/B/C 原生格式投影产物)。

    瘦 frontmatter: 只 ``source``/``fact_id``/``recalled_at``/``description``
    (``description`` = 干净明文 = topic, CC 代码层召回靠 description 匹配)。
    正文自包含完整 fact(不依赖回查 KG, 保留 kg://fact/{id} 作可选溯源链接)。
    **原子写** ``.tmp`` + ``os.replace``(防 synthesis 扫读半写 TOCTOU)。幂等(同 fact_id 重写)。"""
    fid = fact["id"]
    topic = _fact_topic(fact, subject)
    fname = _mem_filename(fid, topic)
    p = mem_dir / fname
    mem_dir.mkdir(parents=True, exist_ok=True)  # 冷启动全新项目: memory/ 可能不存在
    pred = fact.get("predicate") or ""
    val = fact.get("value") or ""
    content = f"""---
description: {_yaml_scalar(topic)}
source: mem-service
fact_id: {fid}
recalled_at: {recalled_at or ''}
---
# {topic}

- subject: {subject}
- predicate: {pred}
- value: {val}
- source_refs: {fact.get('source_refs') or []}
- kg://fact/{fid}
"""
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, p)  # 原子: 防 synthesis 扫读到半写
    return p


def read_fact_id(md_path: Path) -> str | None:
    """从投影 ``mem-*.md`` **frontmatter**(首 ``---...---`` 块)读 ``fact_id``。

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


# ── ADR-15 P2: synthesis_index (MEMORY 投影行唯一写入口) ────────────
# 散 index 对账: 扫 mem-{4hex}-{slug}.md(各路径建/刷)→ 回 KG 取现值 → 对账重写 MEMORY 投影段。
# recall/autodream 只建 .md(散 index 实体文件), synthesis 集中收口写 MEMORY。
# build_index 已 P3 清退; synthesis 是 MEMORY 投影行唯一写入口。

# 投影索引行识别(ADR-A 原生格式 ``- [Title](mem-{4hex}-{slug}.md) — hook``):
# 用 ``](mem-`` 子串覆盖新格式; 旧 ``(memory/mem-`` 残留也一并清(迁移期)。两者都在
# MEMORY 重写时永远删(orphan 行永远删), 保 index 干净。
_MEM_LINE_NEW_RE = re.compile(r"\]\(mem-[0-9a-f]{4}-.+?\.md\)")
_MEM_LINE_OLD_MARKER = "(memory/mem-"   # 迁移期旧格式残留, 保留清理


def _is_mem_index_line(ln: str) -> bool:
    """MEMORY 索引行是否为 mem-service 投影行。

    新格式按 MEM_FILE_RE 口径(4-hex)匹配 ``](mem-{4hex}-{slug}.md)``;
    旧格式 ``(memory/mem-`` 迁移期一并清。两者都在 MEMORY 重写时永远删。"""
    return bool(_MEM_LINE_NEW_RE.search(ln)) or _MEM_LINE_OLD_MARKER in ln


def synthesis_index(cwd: str, mem_dir: Path | str, session_id: str | None = None) -> dict:
    """对账散 ``mem-{4hex}-{slug}.md`` → 回 KG 取现值 → 重写 MEMORY.md 投影索引(唯一写入口)。

    1. 扫 ``mem_dir/*.md``: ``MEM_FILE_RE`` 识别投影文件(ADR-B 单一源), 排除 MEMORY.md/*.tmp;
       ``read_fact_id`` 收 (fact_id, path); 损坏/无 fact_id 跳过(容错)。
    2. 冷启动: 无有效 fact_id → 清 MEMORY 的投影行(保非投影), 返回 cold_start。
       **不 top-K 兜底**(grill 裁决: 兜底违散 index 语义)。
    3. 批量回 KG ``WHERE id IN (...)`` 一次查 present facts(非 N+1);
       orphan = fact_ids 不在 present。
    4. 批量 ``entity WHERE id IN (present.subject_id)`` 取实体名。
    5. ``scoring.mem_score`` desc 排序(PPR 默认关, 留 .env MEM_SYNTH_PPR 占位)。
    6. 对账写 MEMORY: 删所有投影索引行(_is_mem_index_line, orphan/旧格式永远删),
       保 CC 原生行, append 本次 present facts(原生格式, ADR-A)。
    7. orphan 文件: ``MEM_SYNTH_PRUNE_ORPHANS=1`` 才删(``MEM_SYNTH_ORPHAN_BACKUP=1`` rename
       ``.orphan.bak`` 否则 unlink);默认 off 留文件。

    Returns:
        ``{projected, orphans, pruned, cold_start}``。
    """
    import db

    mem_dir_p = Path(mem_dir)
    mem_dir_p.mkdir(parents=True, exist_ok=True)  # 冷启动/全新项目: MEMORY.md 父目录可能不存在
    memory_md = mem_dir_p / "MEMORY.md"

    # 1. 扫投影文件(MEM_FILE_RE 识别) → (fact_id, path)。MEMORY.md 与 *.tmp 排除。
    found: list[tuple[str, Path]] = []
    if mem_dir_p.is_dir():
        for p in mem_dir_p.glob("*.md"):
            if p.name == "MEMORY.md" or p.suffix == ".tmp" or p.name.endswith(".tmp"):
                continue
            # ADR-B: MEM_FILE_RE 识别投影文件(单一源, 不靠 frontmatter/source 子串)。
            if not MEM_FILE_RE.match(p.name):
                continue
            fid = read_fact_id(p)
            if fid:
                found.append((fid, p))
    fact_ids = [fid for fid, _ in found]
    path_by_id = {fid: p for fid, p in found}

    # 2. 冷启动: 无有效 fact_id → 清投影行(保非投影), 不兜底。
    if not fact_ids:
        _rewrite_mem_lines(memory_md, [])
        return {"projected": 0, "orphans": 0, "pruned": 0, "cold_start": True}

    # 3. 批量回 KG(WHERE id IN (...), 一次非 N+1)。取 topic(ADR-C)投影用。
    conn = db.get_conn()
    placeholders = ",".join("?" * len(fact_ids))
    rows = conn.execute(
        f"SELECT id, subject_id, predicate, value, LIF, confidence, source_cwd, topic "
        f"FROM fact WHERE id IN ({placeholders})",
        fact_ids,
    ).fetchall()
    present_ids = {r["id"] for r in rows}
    facts = [
        {
            "id": r["id"],
            "subject_id": r["subject_id"],
            "predicate": r["predicate"],
            "value": r["value"],
            "LIF": r["LIF"],
            "confidence": r["confidence"],
            "source_cwd": r["source_cwd"],
            "topic": r["topic"] if "topic" in r.keys() else None,
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

    # 6. 对账写 MEMORY.md: 删 orphan/旧投影行(永远删), append 本次 present(原生格式, 已排序)。
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


def _format_mem_line(fact: dict, subj_name: str) -> str:
    """ADR-A 原生索引行: ``- [{topic}](mem-{4hex}-{slug}.md) — {topic}``。

    链接文本 = topic, 链接 = 相对路径(``mem-{4hex}-{slug}.md``, 与 MEMORY.md 同目录),
    hook = topic。无 [mem] 标记 / 无 score / 无 kg://(纯原生, CC 代码层召回可消费)。"""
    topic = _fact_topic(fact, subj_name)
    fname = _mem_filename(fact["id"], topic)
    return f"- [{_md_link_text(topic)}]({fname}) — {_md_link_text(topic)}"


def _rewrite_mem_lines(memory_md: Path, new_lines: list[str]) -> None:
    """删 MEMORY.md 中所有投影索引行(新 ``](mem-`` / 旧 ``(memory/mem-``, orphan/旧格式永远删),
    保 CC 原生行(非投影), append new_lines。幂等(每次重写 = 本次精确集合)。"""
    existing = memory_md.read_text(encoding="utf-8") if memory_md.exists() else ""
    out = [ln for ln in existing.splitlines() if not _is_mem_index_line(ln)]
    out.extend(new_lines)
    memory_md.write_text("\n".join(out) + "\n", encoding="utf-8")


# ── M18 recall log 投影 (recall-<DATE>.md + MEMORY 索引行, 用户裁决 2026-08-27) ──
# 手动召回的内容正文按日落一文件 ``recall-YYYYMMDD.md``, MEMORY.md 注入当日索引行。
# 这是对「CC automemory 不动」红线的**用户明示放宽**: 只限 recall 日志这一族文件,
# 其余 (synthesis-index 等) 仍保持手动/休眠。与 ADR-15 per-hit ``mem-<id>.md`` 投影
# 互补: mem-*.md 是单 fact 载体(Ch2), recall-*.md 是当日召回流水(查询+命中合集)。

RECALL_FILE_RE = re.compile(r"^recall-\d{8}\.md$")
_RECALL_INDEX_HEADING = "## KG recall logs"
_RECALL_VALUE_MAX = 200  # value 单行截断(正文可读性; 完整值在 mem-<id>.md/KG)


def _recall_fact_line(fact: dict, idx: int) -> str:
    """单命中 → recall 正文条目行 ``N. topic — value [score x.xx · mem-xx.md]``。

    topic 优先 ``_snaptag.display``(recall 已算好, 与 per-hit 投影同源), 回退
    ``_fact_topic``。value 压成单行并截断; score/mem_path 仅存在时附注。"""
    tag = fact.get("_snaptag") or {}
    topic = (tag.get("display") or _fact_topic(fact, "?"))
    topic = topic.replace("\r", " ").replace("\n", " ").strip() or "?"
    val = (fact.get("value") or "").replace("\r", " ").replace("\n", " ").strip()
    if len(val) > _RECALL_VALUE_MAX:
        val = val[:_RECALL_VALUE_MAX].rstrip() + "…"
    parts = ""
    score = fact.get("score")
    if isinstance(score, (int, float)):
        parts += f" [score {score:.2f}]"
    if tag.get("mem_path"):
        parts += f" · {tag['mem_path']}"
    return f"{idx}. {topic} — {val}{parts}"


def _inject_recall_index_line(memory_md: Path, fname: str, date_str: str,
                              n_hits: int) -> bool:
    """MEMORY.md 幂等注入当日 recall 索引行; 返回是否新增。

    非破坏: 已有该日链接 → no-op; 新增 → 插入 ``## KG recall logs`` 段首(无段则
    文末建段), CC 原生行不动。原子写 ``MEMORY.md.tmp`` + ``os.replace``。
    synthesis_index 重写只删 ``](mem-`` 行, recall 行不受影响(两族正交)。"""
    text = memory_md.read_text(encoding="utf-8") if memory_md.exists() else ""
    if f"]({fname})" in text:
        return False
    line = f"- [recall {date_str}]({fname}) — 当日 KG 召回记录 ({n_hits} 命中)"
    if text and not text.endswith("\n"):
        text += "\n"
    if _RECALL_INDEX_HEADING in text:
        lines = text.splitlines(keepends=True)
        for i, ln in enumerate(lines):
            if ln.strip() == _RECALL_INDEX_HEADING:
                lines.insert(i + 1, line + "\n")  # 段首插(最新在上)
                break
        text = "".join(lines)
    else:
        # 空文件/全新项目: 直接以 heading 起(不带前导空行)
        text += (f"\n{_RECALL_INDEX_HEADING}\n\n{line}\n" if text
                 else f"{_RECALL_INDEX_HEADING}\n\n{line}\n")
    tmp = memory_md.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, memory_md)  # 原子: 防 CC/其他读者读到半写
    return True


def project_recall(memory_dir: Path | str, query: str, facts: list[dict],
                   now: datetime | None = None) -> dict[str, Any]:
    """recall 结果 → 当日 ``recall-<DATE>.md`` 正文追加 + MEMORY.md 索引行 (M18)。

    - 文件: ``recall-YYYYMMDD.md`` (本地日期, 一天一文件); 不存在则建骨架,
      frontmatter ``source: mem-service-recall`` — ADR-16f 子串命中
      (``source: mem-service`` in fm_block) → init-memory/re-ingest 扫描自动
      跳过, 防自指循环。
    - 正文: 每查询一节 ``## HH:MM — query``, 下挂命中条目 (``_recall_fact_line``:
      topic + value 摘要 + score + mem-<id>.md 溯源)。追加写(当日多查询共存)。
    - MEMORY.md: ``_inject_recall_index_line`` 幂等注入。
    - ``facts`` 空 → 调用方自行跳过(不写空日志; 本函数仍可被显式调用)。
    """
    mem_dir = Path(memory_dir)
    mem_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    fname = "recall-" + now.strftime("%Y%m%d") + ".md"
    p = mem_dir / fname
    if not p.exists():
        p.write_text(
            "---\n"
            "description: 当日 KG 召回记录 (mem-service recall log)\n"
            "source: mem-service-recall\n"
            f"date: {date_str}\n"
            "---\n"
            f"# Recall log {date_str}\n\n",
            encoding="utf-8")
    q = (query or "").replace("\r", " ").replace("\n", " ").strip() or "(empty query)"
    body = [f"## {now.strftime('%H:%M')} — {q}\n"]
    body.extend(_recall_fact_line(f, i) for i, f in enumerate(facts or [], 1))
    with p.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(body) + "\n\n")
    index_added = _inject_recall_index_line(mem_dir / "MEMORY.md", fname,
                                            date_str, n_hits=len(facts or []))
    return {"recall_file": fname, "appended": len(facts or []),
            "index_added": index_added}

"""mem-service → CC memory 投影(ADR-15 分布式 index, 真嵌入 CC)。

KG 高 LIF fact → CC memory/mem-<id>.md(实体文件, CC Read/description 召回工作)
+ MEMORY.md append/update [mem] 索引行(幂等, mem-<id> 匹配)。

散 index 标记: 投影 md frontmatter `source: mem-service` + MEMORY.md 索引行 [mem]
区分 CC 原生 memory(用户/agent 手写)。指向双混: 文件 link(CC Read)+ kg://fact/<id>(mem 召回)。

触发: PreCompact hook(autodream 后硬编)/ new / cli build-index。
"""
from __future__ import annotations

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


def project_fact_md(fact: dict, subject: str, mem_dir: Path) -> Path:
    """投影单 fact → mem-<id>.md(CC Read 可读, frontmatter description 召回)。
    标记 source: mem-service + fact_id(区分 CC 原生)。幂等(同 fact_id 重写)。"""
    fid = fact["id"]
    p = mem_dir / _mem_filename(fid)
    pred = fact.get("predicate") or ""
    val = fact.get("value") or ""
    lif = float(fact.get("LIF") or 0)
    content = f"""---
description: {subject} {pred} {val}(mem-service KG fact, LIF {lif:.2f})
source: mem-service
fact_id: {fid}
source_cwd: {fact.get('source_cwd') or ''}
extractor: {fact.get('extractor') or ''}
LIF: {lif:.2f}
---
# {subject} {pred} {val}

- subject: {subject}
- predicate: {pred}
- value: {val}
- source_refs: {fact.get('source_refs') or []}
- kg://fact/{fid}

召回扩展: cli recall "{subject}" 或 recall kg://fact/{fid}
"""
    p.write_text(content, encoding="utf-8")
    return p


def mem_index_line(fact: dict, subject: str) -> str:
    """MEMORY.md [mem] 索引行(文件主指向 + kg:// 标注, 散 index 标记 [mem])。"""
    fid = fact["id"]
    val = (fact.get("value") or "")[:30]
    pred = fact.get("predicate") or ""
    lif = float(fact.get("LIF") or 0)
    return (f"- [mem] {subject} {pred} {val}(memory/{_mem_filename(fid)}) — "
            f"LIF {lif:.2f} · kg://fact/{fid}")


def update_memory_md(facts: list[dict], ent_names: dict[str, str], memory_md: Path) -> int:
    """MEMORY.md append/update [mem] 索引行(幂等)。
    CC 原生 append/update 逻辑: mem-<id> 匹配 → update(LIF 变); 否则 append。
    不动 CC 原生索引行(无 mem- prefix)。返回写入行数。"""
    existing = memory_md.read_text(encoding="utf-8") if memory_md.exists() else ""
    lines = existing.splitlines()
    fact_by_id = {f["id"]: f for f in facts}
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        m = re.search(r"mem-([a-f0-9]+)\.md", line)
        if m and m.group(1) in fact_by_id:
            fid = m.group(1)
            fact = fact_by_id[fid]
            subj = ent_names.get(fact["subject_id"], "?")
            out.append(mem_index_line(fact, subj))   # update(LIF/source 变)
            seen.add(fid)
        else:
            out.append(line)   # CC 原生行或非 top-K mem 行, 保留
    for f in facts:   # append 新 top-K(未 seen)
        if f["id"] not in seen:
            subj = ent_names.get(f["subject_id"], "?")
            out.append(mem_index_line(f, subj))
    memory_md.write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(facts)


def build_index(facts: list[dict], ent_names: dict[str, str], mem_dir: Path) -> dict:
    """投影 facts → CC memory(mds + MEMORY.md [mem] 索引)。返回 {projected, memory_dir}。"""
    mem_dir.mkdir(parents=True, exist_ok=True)
    for f in facts:
        subj = ent_names.get(f["subject_id"], "?")
        project_fact_md(f, subj, mem_dir)
    n = update_memory_md(facts, ent_names, mem_dir / "MEMORY.md")
    return {"projected": n, "memory_dir": str(mem_dir)}

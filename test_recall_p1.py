"""P1 集成: recall 建 mem-*.md(瘦 frontmatter/原子写) + _snaptag + shape + 不碰 MEMORY.
db.init(tmp) 隔离, store 插 fact 绕过 LLM."""
import shutil
import tempfile
from pathlib import Path

import db
import projection
import recall as recall_mod
import store

tmp = tempfile.mkdtemp()
db.init(Path(tmp) / "mem.db")
mem_dir = Path(tmp) / "memory"
mem_dir.mkdir()
memory_md = mem_dir / "MEMORY.md"
memory_md.write_text("# Index\n", encoding="utf-8")
mtime_before = memory_md.stat().st_mtime_ns

# 造 fact: 用户 uses rust (value=rust, 召回 "rust" 经 value fallback 命中)
TOPIC = "用户使用 rust 开发"
eid = store.put_entity("用户", "inferred")
fid = store.put_fact(eid, "uses", "rust", extractor="llm", fact_type="permanent",
                     source_cwd="/test", LIF=0.6, confidence=0.8, source_refs=["session:s"],
                     topic=TOPIC)
mem_fname = projection._mem_filename(fid, TOPIC)

# 1. 默认 list[dict] 每条带 _snaptag + 建 mem-{4hex}-{slug}.md (ADR-A/B/C)
res = recall_mod.recall("rust", session_id="s1", mem_dir=str(mem_dir))
assert isinstance(res, list) and len(res) >= 1, res
f0 = res[0]
assert "_snaptag" in f0, "默认 shape 应嵌 _snaptag"
tag = f0["_snaptag"]
assert tag["fact_id"] == fid and tag["kg_uri"] == f"kg://fact/{fid}"
# ADR-B: mem_path 是相对文件名(mem-{4hex}-{slug}.md, 无 memory/ 前缀)
assert tag["mem_path"] == mem_fname, f"mem_path={tag['mem_path']} expected {mem_fname}"
assert tag["display"] == TOPIC, f"display 应为 topic(回退): {tag['display']}"

mem_file = mem_dir / mem_fname
assert mem_file.exists(), f"{mem_fname} 应被创建"
assert projection.read_fact_id(mem_file) == fid
content = mem_file.read_text(encoding="utf-8")
assert "extractor:" not in content and "\nLIF:" not in content, "瘦 frontmatter 不应含 extractor/LIF"
assert "fact_id:" in content and "recalled_at:" in content
assert "source_cwd:" not in content, "瘦 frontmatter 不应含 source_cwd"
# ADR-A: description 干净明文 = topic
assert f"description: {TOPIC}" in content, f"description 应=topic:\n{content}"
assert "mem-service KG fact" not in content, "无机器噪声"
assert not (mem_dir / f"{mem_fname}.tmp").exists(), "原子写不应留 .tmp"

# 2. MEMORY.md 未被碰
assert memory_md.stat().st_mtime_ns == mtime_before, "recall 绝不碰 MEMORY.md"

# 3. with_tag=True → nested
res2 = recall_mod.recall("rust", session_id="s1", mem_dir=str(mem_dir), with_tag=True)
assert isinstance(res2, dict) and "results" in res2 and res2["results"][0]["tag"]["fact_id"] == fid

# 4. verbose → score-detail + tag
res3 = recall_mod.recall("rust", session_id="s1", mem_dir=str(mem_dir), verbose=True)
assert isinstance(res3, list) and "match" in res3[0] and "tag" in res3[0] and "mem_score" in res3[0]

# 5. 无 mem_dir/cwd → 不建文件, tag.mem_path=None, 仍 list[dict]+_snaptag
res4 = recall_mod.recall("rust", session_id="s1")
assert isinstance(res4, list) and "_snaptag" in res4[0] and res4[0]["_snaptag"]["mem_path"] is None

# 6. 重跑 recall → mem-*.md 幂等重写(原子), 不留 .tmp
recall_mod.recall("rust", session_id="s1", mem_dir=str(mem_dir))
assert mem_file.exists() and not (mem_dir / f"{mem_fname}.tmp").exists()

# 7. ADR-B: 文件名符合严格契约
assert projection.MEM_FILE_RE.match(mem_file.name), f"文件名应符 MEM_FILE_RE: {mem_file.name}"

print("✓ P1 recall: 建 mem-{4hex}-{slug}.md(瘦/原子/原生格式)+ _snaptag + shape + 不碰 MEMORY + 幂等")
shutil.rmtree(tmp)

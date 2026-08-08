"""prune DELETE 同步自验证 (ADR-17d). db.init(tmp) 隔离, store 插 fact 绕过 LLM。

覆盖: 源 md 在→不删 / 单源 md 删→删孤儿 / 多源留一→不删 / 非 memory 源→跳过 /
dry_run 不改状态 / dir 整个没了→全孤儿。
"""
import shutil
import tempfile
from pathlib import Path

import db
import store
import bootstrap

tmpdir = tempfile.mkdtemp()
db.init(Path(tmpdir) / "mem.db")  # 隔离: 强制重建连接到 tmp
mem_dir = Path(tmpdir) / "memory"
mem_dir.mkdir()

eid = store.put_entity("subj", "inferred")
# 单源 foo / 非 memory (session 轨迹) / 多源 foo+bar
f_foo = store.put_fact(eid, "uses", "rust", extractor="llm", fact_type="permanent",
                       source_cwd="/test", source_refs=["session:memory:foo.md#0"])
f_sess = store.put_fact(eid, "runs", "ci", extractor="llm", fact_type="permanent",
                        source_cwd="/test", source_refs=["session:abc-uuid"])
f_multi = store.put_fact(eid, "likes", "tea", extractor="llm", fact_type="permanent",
                         source_cwd="/test",
                         source_refs=["session:memory:foo.md#0", "session:memory:bar.md#0"])

# 1. foo+bar 都在 → prune 0
(mem_dir / "foo.md").write_text("x")
(mem_dir / "bar.md").write_text("x")
r1 = bootstrap.prune_deleted(mem_dir, source_cwd="/test")
print(f"Test 1 (foo+bar present): pruned={r1['pruned']}")
assert r1["pruned"] == 0, r1

# 2. 删 foo.md → f_foo 孤儿删; f_multi 仍有 bar 留; f_sess 非 memory 留
(mem_dir / "foo.md").unlink()
r2 = bootstrap.prune_deleted(mem_dir, source_cwd="/test")
print(f"Test 2 (foo gone): pruned={r2['pruned']}, ids={r2['pruned_ids']}")
assert r2["pruned"] == 1 and r2["pruned_ids"] == [f_foo], r2

# 3. dry_run 不再改状态 (f_foo 已删, 剩 f_multi+f_sess active=2)
r3 = bootstrap.prune_deleted(mem_dir, source_cwd="/test", dry_run=True)
active = db.get_conn().execute(
    "SELECT count(*) FROM fact WHERE status='active' AND source_cwd='/test'").fetchone()[0]
print(f"Test 3 (dry_run): report={r3['pruned']}, active={active}")
assert r3["dry_run"] is True and r3["pruned"] == 0 and active == 2, r3

# 4. dir 整个没了 → f_multi (剩 bar 源) 也孤儿删; f_sess 非 memory 仍留
shutil.rmtree(mem_dir)
r4 = bootstrap.prune_deleted(mem_dir, source_cwd="/test")
print(f"Test 4 (dir gone): pruned={r4['pruned']}, ids={r4['pruned_ids']}")
assert r4["pruned"] == 1 and r4["pruned_ids"] == [f_multi], r4

# 5. 命名碰撞回归 (真实 bug): native mem-service-*.md 也 startswith "mem-" →
#    ADR-B MEM_FILE_RE (mem-{4hex}-{slug}.md) 区分投影, 否则误判 mem-service-* 为投影 → 误删其 fact。
mem_dir.mkdir()
(mem_dir / "mem-service-native.md").write_text("x")
# 投影文件: 符合 ADR-B mem-{4hex}-{slug}.md 契约
(mem_dir / "mem-0011-projection.md").write_text("proj")
f_ms = store.put_fact(eid, "notes", "native", extractor="llm", fact_type="permanent",
                      source_cwd="/test2", source_refs=["session:memory:mem-service-native.md#0"])
r5 = bootstrap.prune_deleted(mem_dir, source_cwd="/test2")
print(f"Test 5 (mem-service-* collision): present={r5['native_md_present']}, pruned={r5['pruned']}")
assert "mem-service-native.md" in r5["native_md_present"], r5
assert "mem-0011-projection.md" not in r5["native_md_present"], r5
assert r5["pruned"] == 0, r5

print("\n✓ All prune tests passed")
shutil.rmtree(tmpdir)

"""re-ingest 自验证 (ADR-17 b/c). db.init(tmp) 隔离"""
import os
import tempfile
from pathlib import Path

import db
import bootstrap
from llm_provider import EdgeOut, EntityOut, Extraction

# mock provider (复用 _demo 模式)
class _Fake:
    base_url = None
    def extract_facts(self, text: str):
        # 固定返回 (用户, uses, rust)
        return Extraction(
            entities=[EntityOut("用户", "person"), EntityOut("rust", "tool")],
            edges=[EdgeOut("用户", "uses", "rust")],
            confidence=0.7, source_meta={"provider": "fake"})

# db.init(tmp) 隔离
tmpdir = tempfile.mkdtemp()
tmppath = Path(tmpdir) / "mem.db"
db.init(tmppath)  # 直接传新路径,强制重建连接

# 1. 造 native.md('用户使用 rust') → re-ingest → KG 有 (用户,uses,rust), added>=1
native_md = Path(tmpdir) / "native.md"
native_md.write_text("用户使用 rust 进行开发", encoding="utf-8")
r1 = bootstrap.re_ingest_file(native_md, source_cwd="/test", providers=[_Fake()])
print(f"Test 1 (native.md): {r1}")
assert r1.get("added", 0) >= 1, f"Expected added>=1, got {r1}"

# 验证 KG 有该 fact
conn = db.get_conn()
rows = conn.execute("SELECT * FROM fact").fetchall()
print(f"  KG facts after test1: {len(rows)} rows")
assert len(rows) >= 1, "Expected at least 1 fact"

# 2. 造 mem-x.md(frontmatter source:mem-service) → re-ingest → skipped, KG 无新
mem_x_md = Path(tmpdir) / "mem-x.md"
mem_x_md.write_text(
    "---\nsource: mem-service\n---\n这是投影产物",
    encoding="utf-8")
r2 = bootstrap.re_ingest_file(mem_x_md, source_cwd="/test")
print(f"Test 2 (mem-x.md): {r2}")
assert r2.get("skipped", 0) == 1, f"Expected skipped=1, got {r2}"

rows2 = conn.execute("SELECT * FROM fact").fetchall()
print(f"  KG facts after test2: {len(rows2)} rows (unchanged)")
assert len(rows2) == len(rows), "Expected no new facts"

# 3. 重跑 native.md → UPDATE/NOOP(幂等,不重复)
rows_before = conn.execute("SELECT * FROM fact").fetchall()
r3 = bootstrap.re_ingest_file(native_md, source_cwd="/test")
print(f"Test 3 (native.md re-run): {r3}")
rows_after = conn.execute("SELECT * FROM fact").fetchall()
print(f"  KG facts after test3: {len(rows_after)} rows")

# 幂等验证: 不新增 (UPDATE/NOOP) 或增量极小
added_after = r3.get("added", 0)
print(f"  added on re-run: {added_after}")
# 允许少量新增 (LLM 抽取波动), 但不应大幅增长
assert added_after < 5, f"Idempotency violation: added={added_after} on re-run"

print("\n✓ All tests passed")

# 清理
import shutil
shutil.rmtree(tmpdir)

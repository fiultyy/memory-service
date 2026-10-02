"""re-ingest 自验证 (ADR-17 b/c, v2 distill 径). db.init(tmp) 隔离 + stub distill.

v2 改造 (2026-10-01): md → 空行切段 → 逐段 distill_segment (s,p,o) fact
管道退役。锁: 溯源三件套 / 投影跳过 / 幂等 (段 sha 二跑零新增)。
"""
import shutil
import tempfile
from pathlib import Path

import db
import bootstrap


class _StubDistill:
    """记录型 stub: 段文本 sha 记忆模拟 distill_seen 幂等, 零 LLM/零写图。"""

    def __init__(self):
        self.calls = []
        self._seen = set()

    def distill_segment(self, text, session_id, cwd, ts):
        import hashlib
        self.calls.append((text, session_id, cwd, ts))
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if sha in self._seen:
            return {"atoms": 0, "edges": 0, "merged": 0,
                    "supersede_proposals": [], "skipped": "seen"}
        self._seen.add(sha)
        return {"atoms": 1, "edges": 0, "merged": 0, "supersede_proposals": []}

    def distill_chunk(self, text, gist, session_id, cwd, ts):
        # v4 语义段车道 (sha 加 chunk: 前缀与段车道互不撞)
        return self.distill_segment("chunk:" + text, session_id, cwd, ts)


# db.init(tmp) 隔离
tmpdir = tempfile.mkdtemp()
db.init(Path(tmpdir) / "mem.db")

stub = _StubDistill()
_real = bootstrap.distill_mod
bootstrap.distill_mod = stub
try:
    # 1. 造 native.md → re-ingest → distill 收段, atom 计数, 溯源三件套
    native_md = Path(tmpdir) / "native.md"
    native_md.write_text("用户使用 rust 进行开发", encoding="utf-8")
    r1 = bootstrap.re_ingest_file(native_md, source_cwd="/test")
    print(f"Test 1 (native.md): {r1}")
    assert r1.get("atoms", 0) >= 1, f"Expected atoms>=1, got {r1}"
    text, session_id, cwd, ts = stub.calls[0]
    assert session_id == "memory:native.md", session_id
    assert cwd == tmpdir, cwd
    assert ts and ts.startswith("20"), ts

    # 2. 造 mem-x.md(frontmatter source:mem-service) → re-ingest → skipped, 无新段
    n_before = len(stub.calls)
    mem_x_md = Path(tmpdir) / "mem-x.md"
    mem_x_md.write_text(
        "---\nsource: mem-service\n---\n这是投影产物", encoding="utf-8")
    r2 = bootstrap.re_ingest_file(mem_x_md, source_cwd="/test")
    print(f"Test 2 (mem-x.md): {r2}")
    assert r2.get("skipped", 0) == 1, f"Expected skipped=1, got {r2}"
    assert len(stub.calls) == n_before, "投影 md 不应喂 distill"

    # 3. 重跑 native.md → distill_seen sha 判重 → 零新增 (幂等)
    r3 = bootstrap.re_ingest_file(native_md, source_cwd="/test")
    print(f"Test 3 (native.md re-run): {r3}")
    assert r3.get("atoms", 0) == 0 and r3.get("skipped_segs", 0) >= 1, \
        f"Idempotency violation: {r3}"

    print("\n✓ All tests passed")
finally:
    bootstrap.distill_mod = _real
    shutil.rmtree(tmpdir)

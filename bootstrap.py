"""mem-service bootstrap — KG init from CC memory .md (ADR-12).

cli ``init-memory`` entry: scan a CC memory dir (``*.md``), feed each file's
text through the autodream pipeline (LLM 蝴蝶翼 extract ADR-11 + 增量决策
ADD/UPDATE/DELETE/NOOP ADR-10 + 幂等) as a synthetic one-record transcript,
persisting facts with ``fact_type='permanent'`` (长期知识不衰减 ADR-8).

Reuses autodream wholesale — no独立 增量/抽取 logic (DRY): bootstrap is a thin
scan + tmp-transcript + autodream-call loop. Idempotent via autodream's 增量
contract: re-running on an unchanged dir ⇒ NOOP/UPDATE, not duplicate ADDs.

Returns ``{"files": n, "added": ..., "updated": ..., "deleted": ..., "noop": ...}``.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import autodream as autodream_mod


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


def re_ingest_file(
    file_path: str | Path,
    source_cwd: str | None = None,
    providers: list | None = None,
) -> dict[str, int]:
    """单 md → KG 增量 (ADR-17 b/c). 反向 re-ingest 手动触发点 + PostToolUse hook 调用点。

    read md text → 过滤 ADR-16f(source:mem-service) → synthetic transcript →
    autodream(fact_type='permanent') → 返回 {added,updated,deleted,noop,skipped}。
    幂等: 重跑同 md → UPDATE/NOOP。
    """
    file_path = Path(file_path)
    if not file_path.is_file():
        return {"added": 0, "updated": 0, "deleted": 0, "noop": 0, "skipped": 0,
                "error": f"Not a file: {file_path}"}

    text = file_path.read_text(encoding="utf-8")
    # ADR-16f: 跳过 mem-service 投影产物 + #1 跳过 MEMORY.md (自指循环)
    if _is_mem_service_projection(text, filename=file_path.name):
        return {"added": 0, "updated": 0, "deleted": 0, "noop": 0, "skipped": 1}

    totals = {"added": 0, "updated": 0, "deleted": 0, "noop": 0, "errors": 0}
    CHUNK = 4000
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    tmp: tempfile._TemporaryFileWrapper | None = None
    for ci, chunk in enumerate(chunks):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        try:
            tmp.write(json.dumps(
                {"type": "user", "message": {"content": chunk}},
                ensure_ascii=False) + "\n")
            tmp.close()
            try:
                r = autodream_mod.autodream(
                    session_id=f"memory:{file_path.name}#{ci}",
                    transcript_path=tmp.name,
                    providers=providers,
                    fact_type="permanent",  # ADR-17b: 永久知识
                    source_cwd=source_cwd,
                )
            except RuntimeError as e:
                totals["errors"] += 1
                import sys as _sys
                print(f"  SKIP {file_path.name}#{ci}: {str(e)[:140]}", file=_sys.stderr)
                continue
        finally:
            if tmp:
                Path(tmp.name).unlink(missing_ok=True)
        for k in ("added", "updated", "deleted", "noop"):
            totals[k] += r.get(k, 0)
    return totals


def init_memory(
    memory_dir: str | Path,
    providers: list | None = None,
    fact_type: str = "permanent",
    source_cwd: str | None = None,
) -> dict[str, int]:
    """Seed the KG from CC memory ``.md`` files (ADR-12).

    For each ``*.md`` in ``memory_dir`` (sorted by name): read text → write a
    synthetic one-record transcript JSONL (``{type:user, message:{content:text}}``)
    → ``autodream.autodream(session_id="memory:<file>", ..., fact_type=fact_type)``
    →累加 counts. ``providers=None`` → autodream default (LLM 蝴蝶翼 直连 Zhipu;
    无 regex 降级, LLM 不可用即 raise block)。

    Idempotent: a re-run on an unchanged dir yields NOOP/UPDATE (autodream 增量
    decision), not duplicate ADDs — safe to re-run after editing memory files.
    """
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return {"files": 0, "added": 0, "updated": 0, "deleted": 0, "noop": 0,
                "skipped": str(memory_dir)}

    totals = {"files": 0, "added": 0, "updated": 0, "deleted": 0, "noop": 0, "errors": 0, "skipped": 0}
    for md in sorted(memory_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        # ADR-16f: 跳过 mem-service 投影产物 + #1 跳过 MEMORY.md (自指循环)
        if _is_mem_service_projection(text, filename=md.name):
            totals["skipped"] += 1
            continue
        # 分段: 大 .md 切 CHUNK 字段, 各段独立喂 autodream — 覆盖全文 (非截断丢后部),
        # 每段短不超时。单条记忆多 <CHUNK 不分段 (1 段)。
        CHUNK = 4000
        chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
        for ci, chunk in enumerate(chunks):
            # Synthetic one-record transcript per chunk: autodream reads
            # user message.content; NamedTemporaryFile + finally unlink.
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
            try:
                tmp.write(json.dumps(
                    {"type": "user", "message": {"content": chunk}},
                    ensure_ascii=False) + "\n")
                tmp.close()
                try:
                    r = autodream_mod.autodream(
                        session_id=f"memory:{md.name}#{ci}",
                        transcript_path=tmp.name,
                        providers=providers,
                        fact_type=fact_type,
                        source_cwd=source_cwd,
                    )
                except RuntimeError as e:
                    # LLM 不可用 (block, 不降级 regex) — skip 该段, 不崩整个 init。
                    totals["errors"] += 1
                    import sys as _sys
                    print(f"  SKIP {md.name}#{ci}: {str(e)[:140]}", file=_sys.stderr)
                    continue
            finally:
                Path(tmp.name).unlink(missing_ok=True)
            for k in ("added", "updated", "deleted", "noop"):
                totals[k] += r.get(k, 0)
        totals["files"] += 1
    return totals


def _demo() -> None:  # ponytail self-check (mock provider, no network)
    import os
    import tempfile as _t
    from llm_provider import Extraction, FactOut

    class _Fake:
        base_url = None
        def extract_facts(self, text: str):
            return Extraction(facts=[FactOut("用户", "uses", "rust")],
                              confidence=0.7, source_meta={"provider": "fake"})

    d = _t.mkdtemp()
    open(os.path.join(d, "x.md"), "w").write("用户使用 rust")
    r = init_memory(d, providers=[_Fake()])
    assert r["added"] > 0, r
    print("init_memory ok:", r)


if __name__ == "__main__":
    _demo()

"""Node J — autoDream session raw→KG incremental (ADR-10).

Drives ``autodream.autodream(session_id, transcript_path)`` against isolated
per-test SQLite files, covering the four incremental-decision paths plus the
idempotency contract (same transcript twice ⇒ second call is all-NOOP).

- **ADD**    — new (subject, predicate, value) → put_fact, ``added`` += 1.
- **UPDATE** — same (subject, predicate, value), new session ⇒ refresh LIF +
  absorb session (``updated`` += 1).
- **DELETE** — same (subject, predicate), different value ⇒ old(s) superseded,
  new fact added (``deleted`` counts the superseded, ``added`` the new).
- **NOOP**   — exact fact already active with the session already absorbed
  (re-run on the same transcript ⇒ second call all-NOOP).

Acceptance cmd: ``cd services/memory-service && python -m pytest tests/test_autodream.py -q``.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_SRV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRV_DIR not in sys.path:
    sys.path.insert(0, _SRV_DIR)

import autodream  # noqa: E402
import cli  # noqa: E402
import db  # noqa: E402
import store  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path):
    """Per-test isolated SQLite file; resets db's cached connection."""
    db.init(str(tmp_path / "memory.db"))
    yield tmp_path


def _write_transcript(tmp_path, records):
    """Write a list of CC transcript records as JSONL, return the path."""
    tp = tmp_path / "transcript.jsonl"
    with tp.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return str(tp)


def _user(content):
    return {"type": "user", "message": {"content": content}}


def _assistant(content):
    return {"type": "assistant", "message": {"content": content}}


# ── ADD path ────────────────────────────────────────────────────────

def test_add_new_fact(fresh_db):
    """GIVEN transcript 含 '用户使用 rust' WHEN autodream THEN KG ADD
    fact(用户,uses,rust) — added≥1, the fact is active in the store."""
    tp = _write_transcript(fresh_db, [_user("用户使用 rust")])
    r = autodream.autodream("s1", tp)
    assert r["added"] >= 1, r
    assert r["updated"] == 0 and r["deleted"] == 0, r
    # The fact is persisted and active.
    subj = store.find_entities_by_name("用户")
    assert subj, "subject entity created"
    facts = store.get_facts_by_subject(subj[0]["id"], status="active")
    assert any(f["predicate"] == "uses" and f["value"] == "rust" for f in facts), facts


def test_add_assistant_and_user_content_both_scanned(fresh_db):
    """Both user and assistant message.content feed the extractor; multi-line
    transcript yields multiple facts across speakers."""
    tp = _write_transcript(fresh_db, [
        _user("用户使用 rust"),
        _assistant("FastAPI uses Pydantic."),
    ])
    r = autodream.autodream("s1", tp)
    assert r["added"] >= 2, r


def test_content_block_list_form_supported(fresh_db):
    """``message.content`` may be a list of content blocks (text/tool_use).
    Text blocks are scanned; non-text blocks skipped."""
    tp = _write_transcript(fresh_db, [
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "用户使用 rust"},
            {"type": "tool_result", "content": "noise"},
        ]}},
    ])
    r = autodream.autodream("s1", tp)
    assert r["added"] >= 1, r


def test_missing_transcript_noop(fresh_db):
    """A non-existent transcript path ⇒ no crash, all-zero (no extraction)."""
    r = autodream.autodream("s1", str(fresh_db / "nope.jsonl"))
    assert r == {"added": 0, "updated": 0, "deleted": 0, "noop": 0}, r


# ── UPDATE path ─────────────────────────────────────────────────────

def test_update_refresh_on_new_session(fresh_db):
    """Same (subject, predicate, value) re-dreamt from a *new* session ⇒
    UPDATE (session absorbed, LIF spread rises), not a second ADD."""
    tp = _write_transcript(fresh_db, [_user("用户使用 rust")])
    r1 = autodream.autodream("s1", tp)
    assert r1["added"] >= 1, r1

    r2 = autodream.autodream("s2", tp)  # different session, same fact
    assert r2["added"] == 0, r2
    assert r2["updated"] >= 1, r2
    # The fact now carries both sessions.
    subj = store.find_entities_by_name("用户")
    facts = store.get_facts_by_subject(subj[0]["id"], status="active")
    the_fact = next(f for f in facts if f["predicate"] == "uses" and f["value"] == "rust")
    assert set(the_fact["seen_sessions"]) >= {"s1", "s2"}, the_fact["seen_sessions"]


# ── DELETE path (supersede on contradiction) ────────────────────────

def test_functional_predicate_supersede_on_value_change(fresh_db):
    """is_a (functional/single-valued): same (subject, is_a) different value ⇒
    real contradiction ⇒ old superseded, new added. (uses would now coexist.)"""
    tp1 = _write_transcript(fresh_db, [_user("Logseq 是笔记工具")])
    autodream.autodream("s1", tp1)
    tp2 = _write_transcript(fresh_db, [_user("Logseq 是数据库")])
    r2 = autodream.autodream("s1", tp2)
    assert r2["deleted"] >= 1, r2
    assert r2["added"] >= 1, r2
    subj = store.find_entities_by_name("Logseq")
    all_facts = store.get_facts_by_subject(subj[0]["id"], status=None)
    superseded = [f for f in all_facts if f["status"] == "superseded" and f["value"] == "笔记工具"]
    survivors = [f for f in all_facts if f["status"] == "active" and f["predicate"] == "is_a"]
    assert superseded, "old '笔记工具' fact must be superseded"
    assert any(f["value"] == "数据库" for f in survivors), survivors


# ── multivalue predicates coexist (Design 1: 不当矛盾 supersede) ─────

def test_multivalue_predicate_coexists(fresh_db):
    """uses 是多值谓词: 同 (用户, uses) 不同 value 共存, 不互相 supersede。"""
    tp = _write_transcript(fresh_db, [_user("用户使用 rust"), _user("用户使用 docker")])
    r = autodream.autodream("s1", tp)
    assert r["deleted"] == 0, r  # 关键: 零 supersede
    assert r["added"] >= 2, r
    subj = store.find_entities_by_name("用户")
    active = [f for f in store.get_facts_by_subject(subj[0]["id"], status="active")
              if f["predicate"] == "uses"]
    assert {f["value"] for f in active} >= {"rust", "docker"}, active


def test_multivalue_idempotent_rerun(fresh_db):
    """多值 transcript 重跑第二次 added==0 deleted==0 (不再震荡 supersede)。"""
    tp = _write_transcript(fresh_db,
        [_user("用户使用 rust"), _user("用户使用 docker"), _user("用户使用 sqlite")])
    autodream.autodream("s1", tp)
    r2 = autodream.autodream("s1", tp)
    assert r2["added"] == 0 and r2["deleted"] == 0, r2
    assert r2["noop"] >= 3, r2


def test_functional_contradiction_idempotent_rerun(fresh_db):
    """is_a 矛盾 supersede 后, 重跑原 transcript 不再震荡 (被 supersede 的 fact
    仍可 exact-match → UPDATE/NOOP, 不再 supersede 链)。"""
    tp1 = _write_transcript(fresh_db, [_user("Logseq 是笔记工具")])
    autodream.autodream("s1", tp1)
    tp2 = _write_transcript(fresh_db, [_user("Logseq 是数据库")])
    autodream.autodream("s1", tp2)
    r3 = autodream.autodream("s1", tp2)  # 重跑 tp2
    assert r3["added"] == 0 and r3["deleted"] == 0, r3  # 不再 supersede 震荡


# ── NOOP + idempotency (acceptance contract) ────────────────────────

def test_idempotent_same_transcript_second_call_all_noop(fresh_db):
    """Acceptance: same transcript, same session, run twice ⇒ second call
    added==0 and the result is all-NOOP (the core idempotency contract)."""
    tp = _write_transcript(fresh_db, [_user("用户使用 rust")])
    r1 = autodream.autodream("s1", tp)
    r2 = autodream.autodream("s1", tp)
    assert r1["added"] >= 1, r1
    assert r2["added"] == 0, r2
    assert r2["noop"] >= 1, r2
    assert r2["updated"] == 0 and r2["deleted"] == 0, r2


# ── LLM 路径(mock provider, ADR-11)──────────────────────────────────

class _FakeLLMProvider:
    """Deterministic LLM stand-in: always surfaces one fixed fact.

    Exercises the autodream→adapter LLM path without a real CCR call.
    _is_reachable: no base_url → extract_facts("") probe (returns the fixed
    fact, no stub sentinel) → reachable. adapter fans out 3 wings, all return
    the same triple ⇒ voted consensus (confidence 0.7 ≥ floor) ⇒ ext_label llm.
    """

    def extract_facts(self, text):
        from llm_provider import Extraction, FactOut
        return Extraction(
            facts=[FactOut("Alice", "uses", "Python")],
            confidence=0.7,
            source_meta={"provider": "fake-llm"},
        )


def test_add_via_llm_mock_provider(fresh_db):
    """ADR-11: autodream(providers=[mock]) → adapter LLM path → ADD fact with
    extractor='llm' (not 'regex'). Mock avoids real CCR dependency."""
    tp = _write_transcript(fresh_db, [_user("任何文本, mock 不看内容")])
    r = autodream.autodream("s1", tp, providers=[_FakeLLMProvider()])
    assert r["added"] >= 1, r
    subj = store.find_entities_by_name("Alice")
    assert subj, "LLM-extracted subject entity created"
    facts = store.get_facts_by_subject(subj[0]["id"], status="active")
    the_fact = next(f for f in facts if f["predicate"] == "uses" and f["value"] == "Python")
    assert the_fact["extractor"] == "llm", the_fact  # ADR-11: LLM path labeled


# ── cli seam ─────────────────────────────────────────────────────────

def test_cli_autodream_subcommand(fresh_db, monkeypatch):
    """``cli autodream --session <id> --transcript <path>`` drives the same
    pipeline as ``autodream.autodream`` (Spec §6 seam — both entrypoints share
    the implementation)."""
    tp = _write_transcript(fresh_db, [_user("用户使用 rust")])
    # cli._main prints JSON to stdout; capture via monkeypatch.
    import io
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = cli._main(["autodream", "--session", "s1", "--transcript", tp])
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["added"] >= 1, out


# ── consolidate reuse ───────────────────────────────────────────────

def test_autodream_runs_consolidate(fresh_db):
    """autoDream phase (a) reuses consolidate (decay+dedup). A pre-seeded
    exact-duplicate fact pair is collapsed by the autodream call."""
    eid = store.put_entity("用户", "inferred")
    store.put_fact(eid, "uses", "rust", extractor="regex")
    store.put_fact(eid, "uses", "rust", extractor="regex")  # exact dup
    tp = _write_transcript(fresh_db, [_user("irrelevant")])
    autodream.autodream("s1", tp)
    # consolidate collapsed the dup → exactly one active 'rust' fact.
    facts = store.get_facts_by_subject(eid, status="active")
    rust_active = [f for f in facts if f["predicate"] == "uses" and f["value"] == "rust"]
    assert len(rust_active) == 1, rust_active

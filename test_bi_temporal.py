"""D4 bi-temporal validity tests.

Covers: put_fact valid_from defaults, update_fact_status valid_to COALESCE,
recall temporal filtering (as_of point-in-time, NULL valid_from = -inf),
default recall zero-regression (valid_to IS NULL), production db isolation.

Pattern: db.init(tmp) isolation, boost=False on all recall calls.
"""
import shutil
import sqlite3
import tempfile
from pathlib import Path

import db
import recall as recall_mod
import store

# ── Time constants ──
t_early = "2026-01-01T00:00:00+00:00"
t0 = "2026-06-01T00:00:00+00:00"
t1 = "2026-07-01T00:00:00+00:00"
t_mid = "2026-06-15T00:00:00+00:00"
t_late = "2026-08-01T00:00:00+00:00"

tmp = tempfile.mkdtemp()
db.init(Path(tmp) / "bi.db")

# ── Setup: entity + two versioned facts ──
ea = store.put_entity("ProjectA", "concept")
fid1 = store.put_fact(ea, "uses", "ProjectA uses rust", valid_from=t0, extractor="llm",
                     fact_type="permanent", LIF=0.5, confidence=0.8,
                     topic="ProjectA uses rust")
fid2 = store.put_fact(ea, "uses", "ProjectA uses docker", valid_from=t1, extractor="llm",
                     fact_type="permanent", LIF=0.5, confidence=0.8,
                     topic="ProjectA uses docker")

# ── 1. put_fact default valid_from is NOT NULL (defaults to _now()) ──
conn = db.get_conn()
row = conn.execute("SELECT valid_from FROM fact WHERE id=?", (fid1,)).fetchone()
assert row["valid_from"] is not None, f"put_fact with valid_from={t0} should have valid_from set"
assert row["valid_from"] == t0, f"put_fact explicit valid_from should be respected, got {row['valid_from']}"

fid_auto = store.put_fact(ea, "runs", "ProjectA runs k8s", extractor="llm")
row_auto = conn.execute("SELECT valid_from FROM fact WHERE id=?", (fid_auto,)).fetchone()
assert row_auto["valid_from"] is not None, "put_fact default valid_from must NOT be NULL (should default to _now())"
print("✓ 1. put_fact default valid_from is NOT NULL")

# ── 2. put_fact explicit valid_from respected ──
assert row["valid_from"] == t0, f"explicit valid_from={t0} not respected, got {row['valid_from']}"
print("✓ 2. put_fact explicit valid_from respected")

# ── 3. update_fact_status with valid_to sets it ──
store.update_fact_status(fid1, "superseded", supersedes_id=fid2, valid_to=t1)
row_vt = conn.execute("SELECT valid_to, status, supersedes_id FROM fact WHERE id=?", (fid1,)).fetchone()
assert row_vt["valid_to"] == t1, f"update_fact_status valid_to should be {t1}, got {row_vt['valid_to']}"
assert row_vt["status"] == "superseded", f"status should be superseded, got {row_vt['status']}"
assert row_vt["supersedes_id"] == fid2, f"supersedes_id should be {fid2}, got {row_vt['supersedes_id']}"
print("✓ 3. update_fact_status with valid_to sets it")

# ── 4. COALESCE: update WITHOUT valid_to preserves existing ──
# valid_to omitted → None → COALESCE(NULL, existing) = existing (preserved)
store.update_fact_status(fid1, "deprecated")  # no valid_to passed
row_co = conn.execute("SELECT valid_to FROM fact WHERE id=?", (fid1,)).fetchone()
assert row_co["valid_to"] == t1, (
    f"COALESCE: update without valid_to should preserve existing {t1}, got {row_co['valid_to']}"
)
print("✓ 4. COALESCE: update without valid_to preserves existing")

# ── 5. Default recall (as_of=None): superseded fact excluded, active fact included ──
# fid1 has valid_to=t1 (set via update_fact_status), so default recall filters it out
# fid2 has valid_to=NULL, so default recall includes it
# NOTE: fid1 status is also "superseded" (not "active"), so it's doubly excluded
res_default = recall_mod.recall("ProjectA", boost=False)
fact_ids_default = {f["id"] for f in res_default}
assert fid1 not in fact_ids_default, (
    f"default recall: superseded fid1 (valid_to set) should be excluded, got ids={fact_ids_default}"
)
assert fid2 in fact_ids_default, (
    f"default recall: active fid2 (valid_to=NULL) should be included, got ids={fact_ids_default}"
)
print(f"✓ 5. default recall: superseded excluded, active included, ids={fact_ids_default}")

# ── 6. as_of before valid_from: fact not yet valid ──
# fid2 has valid_from=t1; as_of=t0 is before t1 → fid2 should not appear
res_before = recall_mod.recall("ProjectA", as_of=t0, boost=False)
fact_ids_before = {f["id"] for f in res_before}
assert fid2 not in fact_ids_before, (
    f"as_of={t0} before valid_from={t1}: fid2 should not be valid, got ids={fact_ids_before}"
)
# bi-temporal: fid1 is superseded but at as_of=t0 it was valid (valid_from=t0, valid_to=t1)
assert fid1 in fact_ids_before, (
    f"bi-temporal: superseded fid1 should be visible at as_of={t0}, got ids={fact_ids_before}"
)
print(f"✓ 6. as_of before valid_from: fid2 not yet valid, fid1 (superseded) visible, ids={fact_ids_before}")

# ── 7. as_of at valid_from: fact valid ──
# fid2 valid_from=t1; as_of=t1 → valid_from <= as_of → valid
res_at = recall_mod.recall("ProjectA", as_of=t1, boost=False)
fact_ids_at = {f["id"] for f in res_at}
assert fid2 in fact_ids_at, (
    f"as_of={t1} at valid_from: fid2 should be valid, got ids={fact_ids_at}"
)
print(f"✓ 7. as_of at valid_from: fact valid, ids={fact_ids_at}")

# ── 8. as_of between valid_from and valid_to: fact valid (incl. superseded) ──
# fid1: valid_from=t0, valid_to=t1, status='superseded'; as_of=t_mid → temporally valid
# bi-temporal: as_of drops status filter → superseded fid1 IS visible at historical point
fid3 = store.put_fact(ea, "runs", "ProjectA runs jenkins", valid_from=t0, extractor="llm",
                     fact_type="permanent", LIF=0.5, confidence=0.8,
                     topic="ProjectA runs jenkins")
# Manually set valid_to on fid3 while keeping status='active'
conn.execute("UPDATE fact SET valid_to=? WHERE id=?", (t1, fid3))
conn.commit()
res_between = recall_mod.recall("ProjectA", as_of=t_mid, boost=False)
fact_ids_between = {f["id"] for f in res_between}
assert fid3 in fact_ids_between, (
    f"as_of={t_mid}: fid3 (active, valid_to set) should be valid, got ids={fact_ids_between}"
)
assert fid1 in fact_ids_between, (
    f"bi-temporal: superseded fid1 should be visible at as_of={t_mid}, got ids={fact_ids_between}"
)
print(f"✓ 8. as_of between: fid3 (active) + fid1 (superseded) both visible, ids={fact_ids_between}")

# ── 9. as_of after valid_to: fact expired ──
res_after = recall_mod.recall("ProjectA", as_of=t_late, boost=False)
fact_ids_after = {f["id"] for f in res_after}
# fid3 has valid_to=t1; as_of=t_late > t1 → expired
assert fid3 not in fact_ids_after, (
    f"as_of={t_late} after valid_to={t1}: fid3 should be expired, got ids={fact_ids_after}"
)
print(f"✓ 9. as_of after valid_to: fact expired, ids={fact_ids_after}")

# ── 10. NULL valid_from (old data) → treated as -inf, valid at any as_of ──
# Manually NULL-ify valid_from to simulate old pre-D4 data
conn.execute("UPDATE fact SET valid_from=NULL WHERE id=?", (fid3,))
conn.commit()
# Re-fetch to confirm NULL
row_null_vf = conn.execute("SELECT valid_from, valid_to FROM fact WHERE id=?", (fid3,)).fetchone()
assert row_null_vf["valid_from"] is None, "fid3 valid_from should be NULL for test 10"

# as_of=t_early (way before anything) → fid3 should still be valid (NULL = -inf)
res_null_early = recall_mod.recall("ProjectA", as_of=t_early, boost=False)
fact_ids_null_early = {f["id"] for f in res_null_early}
assert fid3 in fact_ids_null_early, (
    f"NULL valid_from treated as -inf: fid3 should be valid at as_of={t_early}, got ids={fact_ids_null_early}"
)

# as_of=t_mid → fid3 should also be valid (NULL valid_from, valid_to=t1 > t_mid)
res_null_mid = recall_mod.recall("ProjectA", as_of=t_mid, boost=False)
fact_ids_null_mid = {f["id"] for f in res_null_mid}
assert fid3 in fact_ids_null_mid, (
    f"NULL valid_from: fid3 should be valid at as_of={t_mid}, got ids={fact_ids_null_mid}"
)

# as_of=t_late → fid3 expired (valid_to=t1 < t_late)
res_null_late = recall_mod.recall("ProjectA", as_of=t_late, boost=False)
fact_ids_null_late = {f["id"] for f in res_null_late}
assert fid3 not in fact_ids_null_late, (
    f"NULL valid_from but valid_to={t1} < as_of={t_late}: fid3 should be expired, got ids={fact_ids_null_late}"
)
print("✓ 10. NULL valid_from → treated as -inf, valid at any as_of (within valid_to)")

# ── 11. consolidate supersede sets valid_to (F1: align with autodream.py:239) ──
import consolidate
eb = store.put_entity("ProjectB", "concept")
store.put_fact(eb, "uses", "ProjectB uses rust", valid_from=t0, extractor="llm")
store.put_fact(eb, "uses", "ProjectB uses rust", valid_from=t_mid, extractor="llm")
consolidate.consolidate()
rows = conn.execute(
    "SELECT id, status, valid_to FROM fact WHERE subject_id=?", (eb,)
).fetchall()
sup = [r for r in rows if r["status"] == "superseded"]
assert len(sup) == 1, (
    f"consolidate should supersede 1 dup, got statuses={[r['status'] for r in rows]}"
)
assert sup[0]["valid_to"] is not None, (
    "F1: consolidate supersede must set valid_to (align autodream.py:239); got NULL"
)
print(f"✓ 11. consolidate supersede sets valid_to (F1 fix): valid_to={sup[0]['valid_to']}")

# ── 12. db isolation: production data/memory.db unchanged ──
production_db = Path("/home/yy/projects/memory-service/data/memory.db")
if production_db.exists():
    prod_conn = sqlite3.connect(str(production_db))
    prod_conn.row_factory = sqlite3.Row
    # ProjectA is a test-only entity — it must not exist in production db
    leak = prod_conn.execute(
        "SELECT COUNT(*) as c FROM entity WHERE name='ProjectA'"
    ).fetchone()["c"]
    prod_conn.close()
    assert leak == 0, (
        f"db isolation broken: ProjectA leaked to production db ({leak} rows)"
    )
    print("✓ 12. db isolation: no test data leaked to production db")
else:
    print("✓ 12. db isolation: no production db to check")

shutil.rmtree(tmp)
print("\n✅ All bi-temporal validity tests passed.")

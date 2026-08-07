"""Merge-rule tests for the one-time recovery script (no network)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import seed_sheet_from_store as seed  # noqa: E402

merge = seed.merge_local_into_sheet


def _sheet_rec(pid, first_seen="2026-08-07", status="new", notes=""):
    return {"id": pid, "title": "Pentest Intern", "company": "Acme",
            "deadline": "2026-09-01", "status": status, "notes": notes,
            "first_seen": first_seen}


def _local(pid, first_seen="2026-08-05", status="applied", notes=""):
    return {"id": pid, "title": "Old Title", "company": "Acme",
            "deadline": "", "status": status, "notes": notes,
            "first_seen": first_seen}


def test_local_first_seen_wins_when_earlier():
    merged = merge([_sheet_rec("a")], {"a": _local("a")})
    assert merged["a"]["first_seen"] == "2026-08-05"


def test_sheet_first_seen_wins_when_earlier():
    merged = merge([_sheet_rec("a", first_seen="2026-07-01")], {"a": _local("a")})
    assert merged["a"]["first_seen"] == "2026-07-01"


def test_sheet_wins_for_volatile_fields():
    """The Sheet is the fresher fetch, so its deadline/title stand."""
    merged = merge([_sheet_rec("a")], {"a": _local("a")})
    assert merged["a"]["deadline"] == "2026-09-01"
    assert merged["a"]["title"] == "Pentest Intern"


def test_local_status_and_notes_win():
    merged = merge([_sheet_rec("a")], {"a": _local("a", status="applied", notes="ref")})
    assert merged["a"]["status"] == "applied"
    assert merged["a"]["notes"] == "ref"


def test_blank_local_values_do_not_clobber_the_sheet():
    merged = merge([_sheet_rec("a", status="interested", notes="from sheet")],
                   {"a": _local("a", status="", notes="")})
    assert merged["a"]["status"] == "interested"
    assert merged["a"]["notes"] == "from sheet"


def test_union_includes_both_sides():
    merged = merge([_sheet_rec("a")], {"b": _local("b")})
    assert set(merged) == {"a", "b"}
    assert merged["b"]["first_seen"] == "2026-08-05"


def test_merge_does_not_mutate_its_inputs():
    sheet_recs = [_sheet_rec("a")]
    local = {"a": _local("a")}
    merge(sheet_recs, local)
    assert sheet_recs[0]["first_seen"] == "2026-08-07"
    assert local["a"]["first_seen"] == "2026-08-05"

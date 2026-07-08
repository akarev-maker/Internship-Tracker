"""Network-free unit tests for the tracker's core logic."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import digest  # noqa: E402
import sources  # noqa: E402
import store as store_mod  # noqa: E402
import util  # noqa: E402


def _posting(pid, title="Pentest Intern", deadline="", rank=2):
    return {
        "id": pid,
        "title": title,
        "company": "Acme",
        "link": f"https://example.com/{pid}",
        "locations": ["Boston, MA"] if rank == 0 else ["Remote"],
        "location_str": "Boston, MA" if rank == 0 else "Remote",
        "source": "test",
        "term": "Summer 2027",
        "date_posted": 0,
        "deadline": deadline,
        "rank": rank,
    }


# --- store ------------------------------------------------------------------
def test_first_run_is_baseline():
    store = {}
    new = store_mod.merge_postings(store, [_posting("a"), _posting("b")], today="2026-07-08")
    assert new == []  # nothing flagged new on first ever run
    assert len(store) == 2


def test_second_run_flags_new_and_preserves_status():
    store = {}
    store_mod.merge_postings(store, [_posting("a")], today="2026-07-07")
    store["a"]["status"] = "applied"
    store["a"]["notes"] = "referred by X"
    new = store_mod.merge_postings(store, [_posting("a"), _posting("b")], today="2026-07-08")
    assert new == ["b"]  # only the genuinely new one
    assert store["a"]["status"] == "applied"  # my status preserved
    assert store["a"]["notes"] == "referred by X"  # my notes preserved


def test_volatile_fields_refresh():
    store = {}
    store_mod.merge_postings(store, [_posting("a", deadline="")], today="2026-07-07")
    store_mod.merge_postings(store, [_posting("a", deadline="2026-08-01")], today="2026-07-08")
    assert store["a"]["deadline"] == "2026-08-01"  # deadline refreshed from source


# --- urgency ----------------------------------------------------------------
def test_days_until():
    soon = (date.today() + timedelta(days=3)).isoformat()
    assert digest._days_until(soon) == 3
    assert digest._days_until("") is None
    assert digest._days_until("not-a-date") is None


def test_digest_surfaces_closing_soon():
    soon = (date.today() + timedelta(days=2)).isoformat()
    store = {}
    store_mod.merge_postings(store, [_posting("a", deadline=soon)], today="2026-01-01")
    # status defaults to "new" (needs action), so it should appear in Closing Soon
    md = digest.build_digest_markdown(store, new_ids=[])
    assert "CLOSING SOON" in md
    assert "2 days left" in md


def test_applied_not_nagged_for_deadline():
    soon = (date.today() + timedelta(days=2)).isoformat()
    store = {}
    store_mod.merge_postings(store, [_posting("a", deadline=soon)], today="2026-01-01")
    store["a"]["status"] = "applied"  # already applied -> no deadline nag
    md = digest.build_digest_markdown(store, new_ids=[])
    assert "days left" not in md
    assert "📨 Applied" in md  # shows up in pipeline instead


# --- sanitization + ranking -------------------------------------------------
def test_location_rank():
    assert sources.location_rank(["Boston, MA"]) == 0
    assert sources.location_rank(["Remote"]) == 1
    assert sources.location_rank(["Austin, TX"]) == 2


def test_safe_url_and_md_escape():
    assert util.safe_url("javascript:alert(1)") == ""
    assert util.safe_url("https://ok.com") == "https://ok.com"
    assert "\\[" in util.md_escape("a [b](c)")
    assert "<img" not in util.strip_html("x <img onerror=1> y")

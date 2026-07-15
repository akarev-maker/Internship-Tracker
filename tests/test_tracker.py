"""Network-free unit tests for the tracker's store, ranking inputs, and sanitizers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    assert new == []
    assert len(store) == 2


def test_second_run_flags_new_and_preserves_status():
    store = {}
    store_mod.merge_postings(store, [_posting("a")], today="2026-07-07")
    store["a"]["status"] = "applied"
    store["a"]["notes"] = "referred by X"
    new = store_mod.merge_postings(store, [_posting("a"), _posting("b")], today="2026-07-08")
    assert new == ["b"]
    assert store["a"]["status"] == "applied"
    assert store["a"]["notes"] == "referred by X"


def test_volatile_fields_refresh():
    store = {}
    store_mod.merge_postings(store, [_posting("a", deadline="")], today="2026-07-07")
    store_mod.merge_postings(store, [_posting("a", deadline="2026-08-01")], today="2026-07-08")
    assert store["a"]["deadline"] == "2026-08-01"


# --- ranking inputs + sanitization -----------------------------------------
def test_location_rank():
    assert sources.location_rank(["Boston, MA"]) == 0
    assert sources.location_rank(["Remote"]) == 1
    assert sources.location_rank(["Austin, TX"]) == 2


def test_word_match_boundaries_and_suffixes():
    assert util.word_match("penetration test", "penetration testing intern")
    assert util.word_match("api", "rest apis intern")
    assert not util.word_match("api", "security intern at rapid7")
    assert not util.word_match("soc", "associate product manager")


def test_location_rank_lives_in_util():
    assert util.location_rank(["Boston, MA"]) == 0
    assert util.location_rank(["Remote"]) == 1
    assert util.location_rank(["Austin, TX"]) == 2


def test_safe_url_and_md_escape():
    assert util.safe_url("javascript:alert(1)") == ""
    assert util.safe_url("https://ok.com") == "https://ok.com"
    assert "\\[" in util.md_escape("a [b](c)")
    assert "<img" not in util.strip_html("x <img onerror=1> y")


def test_usajobs_queries_tuned():
    assert ("cybersecurity intern", None) in sources.USAJOBS_QUERIES
    assert ("information security intern", None) in sources.USAJOBS_QUERIES
    assert ("cybersecurity intern", "Massachusetts") not in sources.USAJOBS_QUERIES
    assert len(sources.USAJOBS_QUERIES) <= 6


def test_fetch_all_postings_includes_ats_and_dedupes(monkeypatch):
    a = {"id": "x", "title": "A", "deadline": ""}
    dup = {"id": "x", "title": "A-dup", "deadline": ""}
    b = {"id": "y", "title": "B", "deadline": ""}
    monkeypatch.setattr(sources, "fetch_usajobs", lambda: [a])
    monkeypatch.setattr(sources, "fetch_github_lists", lambda: [dup])
    monkeypatch.setattr(sources.ats_boards, "fetch_ats_postings", lambda: [b])
    out = sources.fetch_all_postings()
    assert [p["id"] for p in out] == ["x", "y"]
    assert out[0]["title"] == "A"  # first source wins the dup


def test_fetch_all_postings_survives_ats_failure(monkeypatch):
    monkeypatch.setattr(sources, "fetch_usajobs", lambda: [])
    monkeypatch.setattr(sources, "fetch_github_lists",
                        lambda: [{"id": "y", "title": "B", "deadline": ""}])

    def boom():
        raise RuntimeError("ats exploded")

    monkeypatch.setattr(sources.ats_boards, "fetch_ats_postings", boom)
    out = sources.fetch_all_postings()
    assert [p["id"] for p in out] == ["y"]


def test_fetch_all_postings_survives_any_source_failure(monkeypatch):
    def boom():
        raise RuntimeError("source exploded")

    monkeypatch.setattr(sources, "fetch_usajobs", boom)
    monkeypatch.setattr(sources, "fetch_github_lists", boom)
    monkeypatch.setattr(sources.ats_boards, "fetch_ats_postings",
                        lambda: [{"id": "z", "title": "C", "deadline": ""}])
    out = sources.fetch_all_postings()
    assert [p["id"] for p in out] == ["z"]

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


def test_sanitizers():
    assert util.safe_url("javascript:alert(1)") == ""
    assert util.safe_url("data:text/html,<script>") == ""
    assert util.safe_url("  https://ok.com  ") == "https://ok.com"
    assert util.safe_url(None) == ""
    assert "<img" not in util.strip_html("x <img onerror=1> y")


def test_ingested_links_are_sanitized(monkeypatch):
    """A feed can put anything in `url`; only http(s) reaches the Sheet."""

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"active": True, "title": "Security Intern",
                     "company_name": "Evil", "url": "javascript:alert(1)",
                     "id": "evil-1", "locations": []}]

    class FakeSession:
        def get(self, *a, **kw):
            return FakeResp()

    monkeypatch.setattr(sources, "SESSION", FakeSession())
    posts = sources.fetch_github_lists()
    assert posts, "the posting should still be tracked"
    assert all(p["link"] == "" for p in posts), "the javascript: URL must be dropped"


def _run_main(monkeypatch, cached_store, sheet_records, edits, postings):
    """Drive tracker.main() with the Sheet and the cache stubbed out."""
    import tracker  # noqa: PLC0415

    saved = {}
    monkeypatch.setattr(tracker, "load_profile",
                        lambda: {"weights": {}, "resume": "", "version": "v"})
    monkeypatch.setattr(tracker, "load_store", lambda: cached_store)
    monkeypatch.setattr(tracker, "save_store", lambda s: saved.update(s))
    monkeypatch.setattr(tracker.sheet, "open_worksheets", lambda: (object(), object()))
    monkeypatch.setattr(tracker.sheet, "read_sheet_state",
                        lambda ws: (sheet_records, edits))
    monkeypatch.setattr(tracker.sheet, "write_sheet",
                        lambda store, ws, archive=None: None)
    monkeypatch.setattr(tracker.sources, "fetch_all_postings", lambda: postings)
    monkeypatch.setattr(tracker.scoring, "score_store", lambda store, prof: None)

    tracker.main()
    return saved


def _sheet_rec(pid, first_seen, status="new", notes=""):
    return {"id": pid, "title": "Pentest Intern", "company": "Acme",
            "location_str": "Remote", "locations": ["Remote"], "rank": 1,
            "deadline": "", "status": status, "notes": notes,
            "source": "test", "link": f"https://example.com/{pid}",
            "first_seen": first_seen, "last_seen": first_seen}


def test_status_edits_survive_an_empty_store(monkeypatch):
    """A cache miss hands main() an empty dict and merge_postings re-adds every
    posting as "new". The Sheet is then the only surviving record of status, so
    its edits must be applied *after* the merge — applying them before would
    silently drop them."""
    saved = _run_main(monkeypatch, {}, [], {"a": {"status": "applied"}},
                      [_posting("a")])
    assert saved["a"]["status"] == "applied", "the Sheet's status was lost"


def test_empty_cache_restores_first_seen_from_the_sheet(monkeypatch):
    """The bug this work fixes: with an empty cache every posting used to be
    re-added with today's date, wiping the First seen column."""
    saved = _run_main(monkeypatch, {}, [_sheet_rec("a", "2026-08-05")], {},
                      [_posting("a")])
    assert saved["a"]["first_seen"] == "2026-08-05"


def test_rehydration_fills_only_the_gaps(monkeypatch):
    """A partially-populated cache keeps its own (richer) records and gains
    only what it was missing."""
    cached = {}
    store_mod.merge_postings(cached, [_posting("a")], today="2026-08-01")
    cached["a"]["term"] = "Summer 2027"  # no Sheet column — must not be clobbered

    saved = _run_main(monkeypatch, cached,
                      [_sheet_rec("a", "2026-01-01"), _sheet_rec("b", "2026-08-05")],
                      {}, [_posting("a")])

    assert saved["a"]["first_seen"] == "2026-08-01", "cached record was overwritten"
    assert saved["a"]["term"] == "Summer 2027"
    assert saved["b"]["first_seen"] == "2026-08-05", "missing record was not restored"


def test_restored_postings_are_not_flagged_new(monkeypatch):
    """Restoring before the merge is what keeps a recovery run from reporting
    the entire backlog as new."""
    import tracker  # noqa: PLC0415

    seen = {}
    real_merge = store_mod.merge_postings
    monkeypatch.setattr(tracker, "merge_postings",
                        lambda s, p, **kw: seen.setdefault("new", real_merge(s, p, **kw)))
    _run_main(monkeypatch, {}, [_sheet_rec("a", "2026-08-05")], {}, [_posting("a")])
    assert seen["new"] == []


def test_rejected_posting_does_not_resurrect(monkeypatch):
    """Archived rows are read back too, so a re-fetched posting the user already
    rejected stays rejected instead of returning as "new"."""
    saved = _run_main(monkeypatch, {},
                      [_sheet_rec("a", "2026-08-05", status="rejected")],
                      {"a": {"status": "rejected"}}, [_posting("a")])
    assert saved["a"]["status"] == "rejected"


def test_rehydrate_ignores_records_without_an_id():
    store = {}
    assert store_mod.rehydrate(store, [{"title": "no id"}, {"id": "", "title": "x"}]) == 0
    assert store == {}


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

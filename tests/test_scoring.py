import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import scoring  # noqa: E402

WEIGHTS = {"web application": 5, "xss": 5, "python": 3, "siem": 2}


def _rec(**kw):
    base = {"title": "", "company": "", "term": "", "location_str": "",
            "deadline": "", "rank": 2}
    base.update(kw)
    return base


# --- keyword_fit ------------------------------------------------------------
def test_keyword_fit_scores_and_reports_matches():
    text = "web application security intern — python"
    score, reason = scoring.keyword_fit(text, WEIGHTS)
    # matched 5 (web application) + 3 (python) = 8; 8/15*100 = 53
    assert score == 53
    assert "web application" in reason and "python" in reason


def test_keyword_fit_caps_at_100():
    text = "web application xss python siem"  # 5+5+3+2 = 15 -> 100
    score, _ = scoring.keyword_fit(text, WEIGHTS)
    assert score == 100


def test_keyword_fit_no_match():
    score, reason = scoring.keyword_fit("marketing manager", WEIGHTS)
    assert score == 0
    assert "no profile keywords" in reason.lower()


def test_keyword_fit_word_boundary_not_substring():
    # "api" must NOT match inside "Rapid7"
    score, reason = scoring.keyword_fit("Security Intern at Rapid7", {"api": 4})
    assert score == 0
    assert "no profile keywords" in reason.lower()
    # but a real standalone occurrence still matches
    score2, _ = scoring.keyword_fit("REST API Security Intern", {"api": 4})
    assert score2 > 0


def test_keyword_fit_matches_common_suffixes():
    # "Penetration Testing Intern" is a canonical title — the boundary match
    # must tolerate the -ing suffix on both phrase and single-word keywords
    weights = {"penetration test": 4, "pentest": 4}
    score, reason = scoring.keyword_fit("Penetration Testing Intern", weights)
    assert score > 0
    assert "penetration test" in reason
    score2, _ = scoring.keyword_fit("Pentesting Intern", {"pentest": 4})
    assert score2 > 0


# --- urgency ----------------------------------------------------------------
def test_urgency_none_is_neutral_low():
    assert scoring.urgency_score("") == 20.0
    assert scoring.urgency_score(None) == 20.0


def test_urgency_closer_is_higher():
    today = date(2026, 1, 1)
    soon = (today + timedelta(days=0)).isoformat()
    later = (today + timedelta(days=21)).isoformat()
    assert scoring.urgency_score(soon, today) == 100.0
    assert scoring.urgency_score(later, today) == 40.0
    assert scoring.urgency_score(soon, today) > scoring.urgency_score(later, today)


def test_urgency_past_and_far():
    today = date(2026, 1, 1)
    assert scoring.urgency_score((today - timedelta(days=1)).isoformat(), today) == 0.0
    assert scoring.urgency_score((today + timedelta(days=60)).isoformat(), today) == 30.0


# --- location + blend -------------------------------------------------------
def test_location_score():
    assert scoring.location_score(0) == 100.0  # MA
    assert scoring.location_score(1) == 60.0   # remote
    assert scoring.location_score(2) == 20.0   # elsewhere


def test_blend_weights():
    # fit=100, urgency=0, location=0 -> 50 ; fit=0,urg=100,loc=0 -> 30
    assert scoring.blend(100, 0, 0) == 50.0
    assert scoring.blend(0, 100, 0) == 30.0
    assert scoring.blend(0, 0, 100) == 20.0


def test_fit_hash_changes_with_text_and_version():
    assert scoring.fit_hash("a", "v1") != scoring.fit_hash("b", "v1")
    assert scoring.fit_hash("a", "v1") != scoring.fit_hash("a", "v2")


# --- gemini_fit --------------------------------------------------------------
import gemini_fit  # noqa: E402


def test_gemini_parse_fit_clamps_and_extracts():
    score, reason = gemini_fit._parse_fit('{"score": 130, "reason": "strong web"}')
    assert score == 100
    assert reason == "strong web"
    score2, _ = gemini_fit._parse_fit('```json\n{"score": -5, "reason": "weak"}\n```')
    assert score2 == 0


def test_gemini_fit_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert gemini_fit.gemini_fit("resume", "posting") is None


def test_parse_batch_maps_clamps_and_filters():
    text = ('[{"id": "a", "score": 130, "reason": "strong"},'
            ' {"id": "b", "score": -5, "reason": "weak"},'
            ' {"id": "ghost", "score": 50, "reason": "not asked"},'
            ' {"id": "c", "reason": "missing score"},'
            ' "junk"]')
    out = gemini_fit._parse_batch(text, ["a", "b", "c"])
    # clamped to 0-100; unknown id and malformed entries dropped;
    # "c" absent -> caller keeps its keyword score
    assert out == {"a": (100, "strong"), "b": (0, "weak")}


def test_parse_batch_strips_markdown_fence():
    fenced = '```json\n[{"id": "a", "score": 7, "reason": "r"}]\n```'
    assert gemini_fit._parse_batch(fenced, ["a"]) == {"a": (7, "r")}


def test_parse_batch_rejects_non_array_and_empty():
    with pytest.raises(RuntimeError):
        gemini_fit._parse_batch('{"id": "a", "score": 7, "reason": "r"}', ["a"])
    with pytest.raises(RuntimeError):
        gemini_fit._parse_batch("", ["a"])


def test_gemini_fit_batch_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = gemini_fit.gemini_fit_batch("resume", [{"id": "a", "text": "t"}])
    assert out is None


# --- score_store ---------------------------------------------------------------
PROFILE = {"weights": WEIGHTS, "resume": "web appsec + python", "version": "v1"}


def _seed(**kw):
    store = {}
    rec = _rec(**kw)
    rec.update({"id": kw.get("id", "a"), "status": "new"})
    store[rec["id"]] = rec
    return store


def test_score_store_keyword_when_no_gemini():
    store = _seed(title="Web Application Security Intern", rank=0,
                  deadline="")
    scoring.score_store(store, PROFILE, gemini_fn=lambda *_: None, today=date(2026, 1, 1))
    rec = store["a"]
    assert rec["fit_score"] == 33   # only "web application" (5) -> 5/15*100
    assert "web application" in rec["fit_reason"]
    assert rec["rank_score"] > 0


def test_score_store_gemini_overrides_keyword():
    store = _seed(title="Generic Security Intern")
    scoring.score_store(store, PROFILE, gemini_fn=lambda *_: (77, "strong web"),
                        today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 77
    assert store["a"]["fit_reason"] == "strong web"


def test_score_store_falls_back_when_gemini_raises():
    store = _seed(title="Web Application Security Intern")

    def boom(*_):
        raise RuntimeError("gemini down")

    scoring.score_store(store, PROFILE, gemini_fn=boom, today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 33  # keyword fallback, not a crash
    assert "web application" in store["a"]["fit_reason"]


def test_score_store_caches_unchanged(monkeypatch):
    store = _seed(title="Web Application Security Intern", deadline="2026-01-15")
    calls = {"n": 0}

    def counting(*_):
        calls["n"] += 1
        return (80, "match")

    # Run with different today values to verify rank_score recomputes
    scoring.score_store(store, PROFILE, gemini_fn=counting, today=date(2026, 1, 1))
    rank_score_1 = store["a"]["rank_score"]
    scoring.score_store(store, PROFILE, gemini_fn=counting, today=date(2026, 1, 8))
    rank_score_2 = store["a"]["rank_score"]

    # fit is cached (gemini_fn called only once), but rank_score recomputed
    assert calls["n"] == 1
    assert rank_score_1 != rank_score_2


def test_score_store_upgrades_keyword_fit_when_gemini_appears():
    # Scored without Gemini (keyword fallback), then Gemini becomes available:
    # the cached keyword fit must be retried and replaced, not kept forever.
    store = _seed(title="Web Application Security Intern")
    scoring.score_store(store, PROFILE, gemini_fn=lambda *_: None, today=date(2026, 1, 1))
    assert store["a"]["fit_source"] == "keyword"
    scoring.score_store(store, PROFILE, gemini_fn=lambda *_: (88, "strong web"),
                        today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 88
    assert store["a"]["fit_source"] == "gemini"


def test_score_store_applied_gets_no_urgency_boost():
    # The sheet ranks what to APPLY to first — a posting already applied to
    # must not out-rank actionable ones just because its deadline is close.
    common = {"title": "Web Application Security Intern", "deadline": "2026-01-03"}
    fresh = _seed(id="a", **common)
    acted = _seed(id="b", **common)
    acted["b"]["status"] = "applied"
    scoring.score_store(fresh, PROFILE, gemini_fn=None, today=date(2026, 1, 1))
    scoring.score_store(acted, PROFILE, gemini_fn=None, today=date(2026, 1, 1))
    assert fresh["a"]["rank_score"] > acted["b"]["rank_score"]
    # applied item's rank carries zero urgency: fit + location only
    expected = scoring.blend(acted["b"]["fit_score"], 0.0,
                             scoring.location_score(acted["b"]["rank"]))
    assert acted["b"]["rank_score"] == expected


def test_score_store_reprices_when_profile_version_changes():
    store = _seed(title="Web Application Security Intern")
    scoring.score_store(store, PROFILE, gemini_fn=lambda *_: (10, "x"),
                        today=date(2026, 1, 1))
    bumped = dict(PROFILE, version="v2")
    scoring.score_store(store, bumped, gemini_fn=lambda *_: (90, "y"),
                        today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 90  # re-scored because version changed

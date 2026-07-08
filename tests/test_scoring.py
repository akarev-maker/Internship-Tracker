import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

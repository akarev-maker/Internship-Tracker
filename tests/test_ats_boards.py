import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ats_boards  # noqa: E402


GH_DATA = {"jobs": [
    {"id": 111, "title": "Security Engineer Intern", "absolute_url": "https://x/gh/111",
     "location": {"name": "Boston, MA"}, "updated_at": "2026-07-01T12:00:00-04:00"},
    {"id": 222, "title": "Penetration Testing Internship", "absolute_url": "https://x/gh/222",
     "location": {"name": "Remote"}, "updated_at": "2026-07-02T12:00:00-04:00"},
    {"id": 333, "title": "Marketing Intern", "absolute_url": "https://x/gh/333",
     "location": {"name": "Austin, TX"}, "updated_at": ""},
    {"id": 444, "title": "Security Engineer", "absolute_url": "https://x/gh/444",
     "location": {"name": "Remote"}, "updated_at": ""},
    {"id": 555, "title": "Associate Product Manager Intern", "absolute_url": "https://x/gh/555",
     "location": {"name": "NYC"}, "updated_at": ""},
]}

LV_DATA = [
    {"id": "abc", "text": "IT Intern", "hostedUrl": "https://x/lv/abc",
     "categories": {"location": "Fully Remote"}, "createdAt": 1780000000000},
    {"id": "def", "text": "Data Engineering Intern", "hostedUrl": "https://x/lv/def",
     "categories": {"location": "NYC"}, "createdAt": 1780000000000},
    {"id": "ghi", "text": "SOC Analyst Intern", "hostedUrl": "https://x/lv/ghi",
     "categories": {}, "createdAt": None},
]


# --- inclusion rule ----------------------------------------------------------
def test_relevant_intern_title_allowlist():
    assert ats_boards.relevant_intern_title("Security Engineer Intern")
    assert ats_boards.relevant_intern_title("Penetration Testing Internship")
    assert ats_boards.relevant_intern_title("IT Intern")
    assert ats_boards.relevant_intern_title("SOC Analyst Intern")
    assert not ats_boards.relevant_intern_title("Marketing Intern")
    assert not ats_boards.relevant_intern_title("Data Engineering Intern")
    # "soc" must not fire inside "Associate"
    assert not ats_boards.relevant_intern_title("Associate Product Manager Intern")


def test_relevant_intern_title_requires_internship():
    assert not ats_boards.relevant_intern_title("Security Engineer")
    # "internal" is not "intern"
    assert not ats_boards.relevant_intern_title("Internal IT Support Specialist")


# --- parsers -----------------------------------------------------------------
def test_parse_greenhouse_fixture():
    out = ats_boards._parse_greenhouse("Acme Sec", "acmesec", GH_DATA)
    assert [p["title"] for p in out] == ["Security Engineer Intern",
                                         "Penetration Testing Internship"]
    p = out[0]
    assert p["id"].startswith("ats:") and len(p["id"]) == 20
    assert p["company"] == "Acme Sec"
    assert p["link"] == "https://x/gh/111"
    assert p["location_str"] == "Boston, MA" and p["rank"] == 0
    assert p["source"] == "Greenhouse"
    assert p["deadline"] == "" and p["term"] == ""
    assert p["date_posted"] > 0
    assert out[1]["rank"] == 1  # Remote


def test_parse_lever_fixture():
    out = ats_boards._parse_lever("Acme Sec", "acmesec", LV_DATA)
    assert [p["title"] for p in out] == ["IT Intern", "SOC Analyst Intern"]
    assert out[0]["source"] == "Lever"
    assert out[0]["rank"] == 1  # Fully Remote
    assert out[0]["date_posted"] == 1780000000
    assert out[1]["location_str"] == "Unspecified" and out[1]["rank"] == 2
    assert out[1]["date_posted"] == 0


def test_ids_stable_across_parses():
    a = ats_boards._parse_greenhouse("Acme Sec", "acmesec", GH_DATA)
    b = ats_boards._parse_greenhouse("Acme Sec", "acmesec", GH_DATA)
    assert [p["id"] for p in a] == [p["id"] for p in b]


# --- companies.toml loader ---------------------------------------------------
def test_load_companies(tmp_path):
    p = tmp_path / "companies.toml"
    p.write_text('[[greenhouse]]\nname = "A"\nslug = "a"\n\n'
                 '[[lever]]\nname = "B"\nslug = "b"\n', encoding="utf-8")
    boards = ats_boards.load_companies(str(p))
    assert boards == [{"ats": "greenhouse", "name": "A", "slug": "a"},
                      {"ats": "lever", "name": "B", "slug": "b"}]


def test_load_companies_missing_file_is_empty(tmp_path):
    assert ats_boards.load_companies(str(tmp_path / "nope.toml")) == []


# --- fetch: per-board error isolation ---------------------------------------
class FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    """Greenhouse board 'good' succeeds; anything else raises."""

    def get(self, url, **kwargs):
        if "good" in url:
            return FakeResp(GH_DATA)
        raise RuntimeError("board down")


def test_fetch_isolates_board_failure(tmp_path, monkeypatch):
    p = tmp_path / "companies.toml"
    p.write_text('[[greenhouse]]\nname = "Good Co"\nslug = "good"\n\n'
                 '[[greenhouse]]\nname = "Bad Co"\nslug = "bad"\n', encoding="utf-8")
    monkeypatch.setattr(ats_boards, "SESSION", FakeSession())
    out = ats_boards.fetch_ats_postings(str(p))
    # Bad Co failed but Good Co's two relevant postings survived
    assert len(out) == 2
    assert all(p["company"] == "Good Co" for p in out)

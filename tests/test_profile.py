import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import profile as profile_mod  # noqa: E402


def _write(tmp_path, text):
    p = tmp_path / "profile.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_load_profile_parses_sections(tmp_path):
    path = _write(
        tmp_path,
        '[weights]\n"web application" = 5\nxss = 5\n\n'
        '[boosts]\nmassachusetts = true\n\n'
        'resume = "I do web appsec."\n',
    )
    prof = profile_mod.load_profile(path)
    assert prof["weights"]["web application"] == 5
    assert prof["weights"]["xss"] == 5
    assert prof["boosts"]["massachusetts"] is True
    assert prof["resume"] == "I do web appsec."
    assert isinstance(prof["version"], str) and len(prof["version"]) == 12


def test_version_changes_when_file_changes(tmp_path):
    a = profile_mod.load_profile(_write(tmp_path, 'resume = "one"\n'))
    b = profile_mod.load_profile(_write(tmp_path, 'resume = "two"\n'))
    assert a["version"] != b["version"]


def test_missing_sections_default_empty(tmp_path):
    prof = profile_mod.load_profile(_write(tmp_path, 'resume = "x"\n'))
    assert prof["weights"] == {}
    assert prof["boosts"] == {}

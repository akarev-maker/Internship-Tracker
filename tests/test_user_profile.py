import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import user_profile as profile_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _no_resume_env(monkeypatch):
    """Keep a real PROFILE_RESUME in the dev shell from leaking into tests."""
    monkeypatch.delenv(profile_mod.RESUME_ENV_VAR, raising=False)


def _write(tmp_path, text):
    p = tmp_path / "profile.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_load_profile_parses_sections(tmp_path):
    path = _write(
        tmp_path,
        'resume = "I do web appsec."\n\n'
        '[weights]\n"web application" = 5\nxss = 5\n',
    )
    prof = profile_mod.load_profile(path)
    assert prof["weights"]["web application"] == 5
    assert prof["weights"]["xss"] == 5
    assert prof["resume"] == "I do web appsec."
    assert isinstance(prof["version"], str) and len(prof["version"]) == 12


def test_version_changes_when_file_changes(tmp_path):
    a = profile_mod.load_profile(_write(tmp_path, 'resume = "one"\n'))
    b = profile_mod.load_profile(_write(tmp_path, 'resume = "two"\n'))
    assert a["version"] != b["version"]


def test_missing_sections_default_empty(tmp_path):
    prof = profile_mod.load_profile(_write(tmp_path, 'resume = "x"\n'))
    assert prof["weights"] == {}


# --- résumé supplied out-of-band (kept out of the public repo) --------------
def test_env_resume_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv(profile_mod.RESUME_ENV_VAR, "secret résumé")
    path = _write(tmp_path, 'resume = "in the file"\n[weights]\nxss = 5\n')
    prof = profile_mod.load_profile(path)
    assert prof["resume"] == "secret résumé"
    assert prof["weights"]["xss"] == 5  # weights still come from the file


def test_blank_env_resume_falls_back_to_file(tmp_path, monkeypatch):
    monkeypatch.setenv(profile_mod.RESUME_ENV_VAR, "")
    prof = profile_mod.load_profile(_write(tmp_path, 'resume = "from file"\n'))
    assert prof["resume"] == "from file"


def test_version_changes_when_env_resume_changes(tmp_path, monkeypatch):
    path = _write(tmp_path, 'resume = ""\n[weights]\nxss = 5\n')
    monkeypatch.setenv(profile_mod.RESUME_ENV_VAR, "one")
    a = profile_mod.load_profile(path)
    monkeypatch.setenv(profile_mod.RESUME_ENV_VAR, "two")
    b = profile_mod.load_profile(path)
    # The file is byte-identical — only a résumé rotation distinguishes these,
    # and it must still invalidate the fit cache.
    assert a["version"] != b["version"]

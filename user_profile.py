"""
user_profile.py — load the user's skillset from profile.toml.

(Named user_profile rather than profile so it doesn't shadow the stdlib
profiler module.)

Two parts feed the two scorers: [weights] drives the deterministic keyword
floor; `resume` is handed to Gemini.

The résumé is personal, so this repo is public with an empty `resume` in
profile.toml and the real text supplied out-of-band via the PROFILE_RESUME
environment variable (a GitHub Actions secret), which wins when set.

`version` is a content hash covering *both* the file and the résumé actually
used, so editing either one re-scores every posting exactly once (it is part
of the fit cache key).
"""

import hashlib
import os
import tomllib

DEFAULT_PATH = "profile.toml"
RESUME_ENV_VAR = "PROFILE_RESUME"


def load_profile(path=DEFAULT_PATH):
    with open(path, "rb") as f:
        raw = f.read()
    data = tomllib.loads(raw.decode("utf-8"))

    resume = os.environ.get(RESUME_ENV_VAR) or str(data.get("resume", ""))
    # Hash the résumé alongside the file: the file no longer necessarily
    # contains it, so hashing the file alone would miss a secret rotation.
    version = hashlib.sha256(raw + b"\x00" + resume.encode("utf-8")).hexdigest()[:12]

    return {
        "weights": {str(k).lower(): int(v) for k, v in data.get("weights", {}).items()},
        "resume": resume,
        "version": version,
    }

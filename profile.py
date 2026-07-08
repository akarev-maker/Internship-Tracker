"""
profile.py — load the user's skillset from profile.toml.

Two parts feed the two scorers: [weights] drives the deterministic keyword
floor; `resume` is handed to Gemini. `version` is a content hash so editing
the file re-scores every posting exactly once (it is part of the cache key).
"""

import hashlib
import tomllib

DEFAULT_PATH = "profile.toml"


def load_profile(path=DEFAULT_PATH):
    with open(path, "rb") as f:
        raw = f.read()
    data = tomllib.loads(raw.decode("utf-8"))

    # Extract resume from either root level or boosts table
    resume_value = data.get("resume", "")
    boosts_data = dict(data.get("boosts", {}))
    if not resume_value and "resume" in boosts_data:
        resume_value = boosts_data.pop("resume")

    return {
        "weights": {str(k).lower(): int(v) for k, v in data.get("weights", {}).items()},
        "boosts": boosts_data,
        "resume": str(resume_value),
        "version": hashlib.sha256(raw).hexdigest()[:12],
    }

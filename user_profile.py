"""
user_profile.py — load the user's skillset from profile.toml.

(Named user_profile rather than profile so it doesn't shadow the stdlib
profiler module.)

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

    return {
        "weights": {str(k).lower(): int(v) for k, v in data.get("weights", {}).items()},
        "resume": str(data.get("resume", "")),
        "version": hashlib.sha256(raw).hexdigest()[:12],
    }

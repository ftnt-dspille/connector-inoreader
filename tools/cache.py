"""A tiny on-disk cache for live API responses.

Inoreader's quota is 100 requests per zone PER DAY, which makes a live test suite
that re-fetches on every run a genuine liability: eight runs of a four-call suite
is a third of the day's reads gone to assertions that would have passed against
the same payload.

So the live path is capture-once, replay-many. `tools/live_check.py` writes what
it fetched here; `tests/test_live_inoreader.py` reads from here and spends
nothing. Refresh deliberately (`--refresh`, or INOREADER_LIVE_REFRESH=1) when the
account has changed and you want the tests to see it.

The cache is gitignored: it is somebody's real feed contents, and it is a
snapshot, not a fixture worth reviewing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "live"


def path_for(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def save(name: str, payload: object) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = path_for(name)
    target.write_text(json.dumps({"captured_at": time.time(), "payload": payload}, indent=2))
    return target


def load(name: str):
    """Return the cached payload, or None if it was never captured."""
    target = path_for(name)
    if not target.exists():
        return None
    return json.loads(target.read_text()).get("payload")


def age_hours(name: str) -> float | None:
    target = path_for(name)
    if not target.exists():
        return None
    captured = json.loads(target.read_text()).get("captured_at") or 0
    return (time.time() - captured) / 3600


def refresh_requested() -> bool:
    return os.environ.get("INOREADER_LIVE_REFRESH", "").strip().lower() in ("1", "true", "yes")


def summary() -> str:
    if not CACHE_DIR.exists():
        return "no cached responses (run: python tools/live_check.py)"
    names = sorted(p.stem for p in CACHE_DIR.glob("*.json"))
    if not names:
        return "no cached responses (run: python tools/live_check.py)"
    ages = [f"{n} ({age_hours(n):.1f}h)" for n in names]
    return "cached: " + ", ".join(ages)

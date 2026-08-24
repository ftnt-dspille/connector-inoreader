"""Turn captured live responses into committed, sanitized test fixtures.

`.cache/live/` holds what `tools/live_check.py` actually received. That is the
best test material there is -- real Inoreader payloads, with the nesting, the
empty fields and the odd types no hand-written mock reproduces -- but it is also
somebody's account: their user id, their email, their feed contents.

This script is the gate between the two. It scrubs identity, trims the payloads
to a reviewable size, and writes `tests/fixtures/*.json`, which ARE committed and
which the offline suite asserts against.

    python tools/make_fixtures.py            # rebuild from .cache/live
    python tools/make_fixtures.py --check     # fail if the fixtures are stale

What gets scrubbed:
  * user id / profile id / name / email  -> fixed placeholder values
  * `user/<digits>/` stream ids          -> `user/-/`  (the documented alias)
  * article HTML content                 -> truncated, marked as truncated
  * lists                                -> at most MAX_ITEMS entries

What deliberately does NOT get scrubbed: feed titles, article titles and URLs.
They are public publisher output, and they are the exact values downstream
mappings key on -- a fixture with invented titles would not be evidence of
anything.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cache as cache_helper  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
MAX_ITEMS = 3
MAX_CONTENT = 400

IDENTITY = {
    "userId": "1000000001",
    "userProfileId": "1000000001",
    "userName": "labuser",
    "userEmail": "labuser@example.com",
}

_USER_STREAM = re.compile(r"user/\d+/")


def scrub(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in IDENTITY:
                out[key] = IDENTITY[key]
            elif key == "content" and isinstance(item, str) and len(item) > MAX_CONTENT:
                out[key] = item[:MAX_CONTENT] + "... [truncated for fixture]"
            else:
                out[key] = scrub(item)
        return out
    if isinstance(value, list):
        return [scrub(v) for v in value[:MAX_ITEMS]]
    if isinstance(value, str):
        # Inoreader echoes the numeric user id into every stream id it returns;
        # `user/-/` is its own documented alias for "the current user".
        return _USER_STREAM.sub("user/-/", value)
    return value


def build() -> dict[str, dict]:
    names = sorted(p.stem for p in cache_helper.CACHE_DIR.glob("*.json"))
    if not names:
        sys.exit("nothing captured yet -- run: python tools/live_check.py")
    return {name: scrub(cache_helper.load(name)) for name in names}


def main() -> int:
    check_only = "--check" in sys.argv
    fixtures = build()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    stale = []
    for name, payload in fixtures.items():
        target = FIXTURE_DIR / f"{name}.json"
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if target.exists() and target.read_text() == rendered:
            print(f"  unchanged  {target.name}")
            continue
        stale.append(target.name)
        if check_only:
            print(f"  STALE      {target.name}")
            continue
        target.write_text(rendered)
        print(f"  wrote      {target.name}")

    if check_only and stale:
        print(f"\n{len(stale)} fixture(s) differ from .cache/live. Run: python tools/make_fixtures.py")
        return 1

    leaked = _identity_leak_scan()
    if leaked:
        print("\nREFUSING: identity strings survived scrubbing in " + ", ".join(leaked))
        return 2
    print(f"\n{len(fixtures)} fixture(s) in {FIXTURE_DIR}")
    return 0


def _identity_leak_scan() -> list[str]:
    """Belt and braces: these files are committed to a public repo.

    Scans for the account's ACTUAL identity values, read back out of the cache,
    rather than for anything that looks like an email. A generic pattern flagged
    `static--@fa...` -- a truncated CSS class in an article body -- which is the
    kind of false positive that gets a safety check disabled.
    """
    secrets = set()
    user_info = cache_helper.load("user_info") or {}
    for key in IDENTITY:
        value = str(user_info.get(key) or "").strip()
        if value and value not in IDENTITY.values():
            secrets.add(value)
    numeric_stream = re.compile(r"user/\d+/")

    hits = []
    for path in FIXTURE_DIR.glob("*.json"):
        text = path.read_text()
        if numeric_stream.search(text) or any(secret in text for secret in secrets):
            hits.append(path.name)
    return hits


if __name__ == "__main__":
    raise SystemExit(main())

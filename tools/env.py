"""Read `.env.inoreader` and build the connector config dict.

Shared by tools/live_check.py, tools/oauth_bootstrap.py and tests/test_live_inoreader.py.
Deliberately dependency-free (no python-dotenv): the connector itself needs only
`requests`, and a validation helper should not be the thing that drags in more.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env.inoreader"

REQUIRED = (
    "INOREADER_APP_ID",
    "INOREADER_APP_KEY",
    "INOREADER_CLIENT_ID",
    "INOREADER_CLIENT_SECRET",
    "INOREADER_REFRESH_TOKEN",
)


def load(path: Path | None = None) -> dict[str, str]:
    """Parse KEY=VALUE lines. Real environment variables win, so CI can inject them."""
    values: dict[str, str] = {}
    env_path = path or ENV_PATH
    if env_path.exists():
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in list(values) + [*REQUIRED, "INOREADER_STREAM_ID", "INOREADER_REDIRECT_URI"]:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def missing(values: dict[str, str]) -> list[str]:
    return [key for key in REQUIRED if not values.get(key)]


def to_config(values: dict[str, str]) -> dict[str, object]:
    """The dict the connector's operations take as `config`.

    No access_token is seeded: the first call mints one, which is itself part of
    what the live check is proving.
    """
    return {
        "server_url": "https://www.inoreader.com",
        "app_id": values["INOREADER_APP_ID"],
        "app_key": values["INOREADER_APP_KEY"],
        "client_id": values["INOREADER_CLIENT_ID"],
        "client_secret": values["INOREADER_CLIENT_SECRET"],
        "refresh_token": values["INOREADER_REFRESH_TOKEN"],
        "verify_ssl": True,
    }


def stream_id(values: dict[str, str]) -> str:
    return values.get("INOREADER_STREAM_ID") or "user/-/state/com.google/reading-list"

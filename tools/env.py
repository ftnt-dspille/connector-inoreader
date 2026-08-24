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
    for key in list(values) + [
        *REQUIRED,
        "INOREADER_STREAM_ID",
        "INOREADER_REDIRECT_URI",
        "INOREADER_SERVER_URL",
    ]:
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
        # INOREADER_SERVER_URL lets the whole toolchain be pointed at
        # tools/mock_server.py instead of the real service.
        "server_url": values.get("INOREADER_SERVER_URL") or "https://www.inoreader.com",
        "app_id": values["INOREADER_APP_ID"],
        "app_key": values["INOREADER_APP_KEY"],
        "client_id": values["INOREADER_CLIENT_ID"],
        "client_secret": values["INOREADER_CLIENT_SECRET"],
        "refresh_token": values["INOREADER_REFRESH_TOKEN"],
        "verify_ssl": True,
    }


def stream_id(values: dict[str, str]) -> str:
    return values.get("INOREADER_STREAM_ID") or "user/-/state/com.google/reading-list"


def persist_refresh_token(config: dict, started_with: str | None = None, path: Path | None = None) -> bool:
    """Write a rotated refresh token back into `.env.inoreader`.

    Inoreader may return a NEW refresh token on refresh. On the appliance the
    connector persists that onto the connector configuration; off-box there is no
    configuration to write to, so without this the env file keeps the superseded
    token and stops working at some unpredictable later date -- the failure mode
    the connector goes out of its way to avoid on the appliance.

    Returns True if the file was updated.
    """
    current = str(config.get("refresh_token") or "")
    env_path = path or ENV_PATH
    if not current or not env_path.exists():
        return False

    # Only a GENUINE rotation gets written: the token now on the config differs
    # from the one this run STARTED with. Comparing against the file instead
    # destroyed a real token once -- an env-var override supplied a dummy token,
    # the file still held the real one, and "they differ" was read as "it
    # rotated". The file is not the source of truth for what we sent.
    if current == str(started_with or ""):
        return False
    if not _is_real_service(config):
        return False

    lines = env_path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("INOREADER_REFRESH_TOKEN="):
            lines[i] = f"INOREADER_REFRESH_TOKEN={current}"
            env_path.write_text("\n".join(lines) + "\n")
            return True
    return False


def _is_real_service(config: dict) -> bool:
    """False when pointed at tools/mock_server.py, which hands out fake tokens."""
    return "inoreader.com" in str(config.get("server_url") or "")

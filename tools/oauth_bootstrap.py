"""Mint the one long-lived secret the connector needs: an OAuth refresh token.

Inoreader's refresh token comes out of the interactive authorization-code flow,
which the connector cannot run itself. This does the two halves of it:

    python tools/oauth_bootstrap.py            # prints the URL to approve
    python tools/oauth_bootstrap.py <code>     # exchanges the code, writes the token

No local web server is started. You approve in a browser, Inoreader redirects to
your registered redirect_uri (which never has to resolve), and you copy the
`code=` parameter out of the address bar.

Reads APP_ID / APP_KEY / CLIENT_ID / CLIENT_SECRET / REDIRECT_URI from
`.env.inoreader`, and writes INOREADER_REFRESH_TOKEN back into the same file.
"""

from __future__ import annotations

import secrets
import sys
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env as env_helper  # noqa: E402

AUTH_URL = "https://www.inoreader.com/oauth2/auth"
TOKEN_URL = "https://www.inoreader.com/oauth2/token"
# 'write' is needed for Tag Articles, Add/Edit Subscription, Mark Stream as Read,
# and the mark_as_read option on ingestion. Asking for it once is cheaper than
# rediscovering the gap mid-demo.
SCOPE = "read write"


def _client(values: dict[str, str]) -> tuple[str, str, str]:
    client_id = values.get("INOREADER_CLIENT_ID") or values.get("INOREADER_APP_ID") or ""
    client_secret = values.get("INOREADER_CLIENT_SECRET") or values.get("INOREADER_APP_KEY") or ""
    redirect_uri = values.get("INOREADER_REDIRECT_URI") or "http://localhost:8080/callback"
    if not (client_id and client_secret):
        sys.exit(
            "Fill in INOREADER_APP_ID and INOREADER_APP_KEY (or the CLIENT_ID/SECRET pair) "
            f"in {env_helper.ENV_PATH} first."
        )
    return client_id, client_secret, redirect_uri


def print_auth_url(values: dict[str, str]) -> None:
    client_id, _, redirect_uri = _client(values)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": secrets.token_urlsafe(16),
        }
    )
    print("1. Open this URL, sign in as the account whose feeds the demo reads, and approve:\n")
    print(f"   {AUTH_URL}?{query}\n")
    print("2. The browser is redirected to your redirect_uri. It will not load -- that is")
    print("   expected. Copy the `code=` value out of the address bar.\n")
    print("3. Exchange it:\n")
    print("   python tools/oauth_bootstrap.py <code>")


def exchange(values: dict[str, str], code: str) -> None:
    client_id, client_secret, redirect_uri = _client(values)
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=60,
    )
    if response.status_code != 200:
        sys.exit(f"Token exchange failed [{response.status_code}]: {response.text[:500]}")

    payload = response.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        sys.exit(f"No refresh_token in the response: {payload}")

    _write_back(refresh_token)
    print("Refresh token written to .env.inoreader")
    print(f"  access token expires in : {payload.get('expires_in')}s (the connector refreshes it)")
    print(f"  scope granted           : {payload.get('scope', '(not reported)')}")
    print("\nNext:  python tools/live_check.py")


def _write_back(refresh_token: str) -> None:
    """Rewrite the INOREADER_REFRESH_TOKEN line in place, preserving the rest."""
    path = env_helper.ENV_PATH
    if not path.exists():
        sys.exit(f"{path} does not exist. Copy .env.inoreader.example to it first.")
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("INOREADER_REFRESH_TOKEN="):
            lines[i] = f"INOREADER_REFRESH_TOKEN={refresh_token}"
            break
    else:
        lines.append(f"INOREADER_REFRESH_TOKEN={refresh_token}")
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    values = env_helper.load()
    if len(sys.argv) > 1:
        exchange(values, sys.argv[1].strip())
    else:
        print_auth_url(values)


if __name__ == "__main__":
    main()

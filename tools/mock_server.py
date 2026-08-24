"""Serve the recorded fixtures as a stand-in Inoreader, for demos and offline work.

Point the connector's **Server URL** at this instead of https://www.inoreader.com
and every read operation answers from tests/fixtures/ -- no credentials, no
internet, and no spend against a 100-per-day quota. Useful when:

  * a demo has to run without egress, or without risking the day's quota;
  * you are iterating on a playbook's parse step and only need stable input;
  * you want a colleague to try the connector before an account exists.

    python tools/mock_server.py                 # http://127.0.0.1:8099
    python tools/mock_server.py --port 9000 --host 0.0.0.0

Then create a FortiSOAR connector configuration with Server URL set to that
address. Any App ID / App Key / client id / secret / refresh token will do -- the
token endpoint here hands out a token to anyone, which is exactly why this must
never be reachable from anywhere that matters.

Stdlib only, single-threaded, no TLS. It is a fixture player, not a server.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Mirrors what the real service reports, so a client that meters its quota sees
# plausible numbers instead of nothing.
RATE_HEADERS = {
    "X-Reader-Zone1-Limit": "100",
    "X-Reader-Zone1-Usage": "0",
    "X-Reader-Zone2-Limit": "100",
    "X-Reader-Zone2-Usage": "0",
    "X-Reader-Limits-Reset-After": "86400",
}


def _fixture(name: str):
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _stream_contents():
    """Rebuild the RAW stream/contents shape from the normalized capture.

    articles.json holds the connector's output, and each entry carries the
    untouched API item under `raw` -- which is what the connector must be handed
    back, or this would be testing the normalizer against its own output.
    """
    recorded = _fixture("articles") or {"articles": []}
    return {
        "id": recorded.get("stream_id", "user/-/state/com.google/reading-list"),
        "updated": 0,
        "items": [article["raw"] for article in recorded.get("articles", []) if article.get("raw")],
    }


def _item_ids():
    recorded = _fixture("item_ids") or {}
    return {"items": [], "itemRefs": recorded.get("itemRefs", [])}


class Handler(BaseHTTPRequestHandler):
    server_version = "InoreaderFixturePlayer/1.0"

    def _send(self, payload, status=200, raw_text=None):
        body = (raw_text if raw_text is not None else json.dumps(payload)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in RATE_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _route(self, path):
        api = "/reader/api/0"
        if not path.startswith(api):
            return None
        endpoint = path[len(api) :]
        if endpoint == "/user-info":
            return _fixture("user_info")
        if endpoint == "/subscription/list":
            return _fixture("subscriptions")
        if endpoint == "/tag/list":
            return _fixture("tags")
        if endpoint == "/unread-count":
            return _fixture("unread_counts")
        if endpoint == "/stream/items/ids":
            return _item_ids()
        if endpoint.startswith("/stream/contents/"):
            return _stream_contents()
        return None

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's interface
        path = urllib.parse.urlparse(self.path).path
        payload = self._route(path)
        if payload is None:
            self._send({"error": f"no fixture for {path}"}, status=404)
            return
        self._send(payload)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/oauth2/token":
            # Any credential is accepted. See the module docstring: do not expose this.
            self._send({"access_token": "mock-access-token", "expires_in": 86400, "scope": "read write"})
            return
        if path.endswith("/edit-tag") or path.endswith("/mark-all-as-read"):
            # The real service answers the literal string OK, not JSON. Reproducing
            # that is the point -- the connector has a branch for it.
            self._send(None, raw_text="OK")
            return
        if path.endswith("/subscription/quickadd"):
            self._send({"query": "", "numResults": 1, "streamId": "feed/mock", "streamName": "Mock feed"})
            return
        if path.endswith("/subscription/edit"):
            self._send(None, raw_text="OK")
            return
        payload = self._route(path)
        self._send(payload if payload is not None else {"error": f"no fixture for {path}"},
                   status=200 if payload is not None else 404)

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()

    if not FIXTURES.exists() or not any(FIXTURES.glob("*.json")):
        sys.exit("no fixtures -- run tools/live_check.py then tools/make_fixtures.py")

    print(f"Serving {len(list(FIXTURES.glob('*.json')))} fixture(s) at http://{args.host}:{args.port}")
    print("Set the connector's Server URL to that address. Ctrl-C to stop.\n")
    HTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline tests driven by REAL Inoreader payloads.

test_inoreader.py asserts on the requests the connector sends, using payloads I
wrote. This file asserts on what the connector does with payloads Inoreader
actually sent -- captured by tools/live_check.py, sanitized by
tools/make_fixtures.py, and committed under tests/fixtures/.

That difference matters. A hand-written mock encodes what I expected the API to
return; a recorded one carries the nesting, the empty fields, the microsecond
timestamps and the HTML that the real service returns. These tests cost no quota
and need no credentials, so they run in CI like any other unit test.

Refresh them after an API change:

    python tools/live_check.py        # re-capture (4 requests)
    python tools/make_fixtures.py     # re-scrub and rewrite tests/fixtures/
"""

import json
from pathlib import Path

import pytest

from inoreader import operations as ops

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.ok = True
        self.headers = {"X-Reader-Zone1-Limit": "100", "X-Reader-Zone1-Usage": "4"}
        self.text = json.dumps(payload)
        self.content = self.text.encode()
        self._payload = payload

    def json(self):
        return self._payload


CONFIG = {
    "server_url": "https://www.inoreader.com",
    "app_id": "app-id",
    "app_key": "app-key",
    "client_id": "client-id",
    "client_secret": "client-secret",
    "refresh_token": "refresh-token",
    "access_token": "cached-token",
    "access_token_expiry": 4102444800,
}


@pytest.fixture
def replay(monkeypatch):
    """Serve a recorded RAW API payload to the connector."""

    def _install(payload):
        monkeypatch.setattr(ops.requests, "request", lambda *a, **kw: FakeResponse(payload))

    return _install


def _config():
    return dict(CONFIG)


# --------------------------------------------------------------- identity ----


def test_user_info_shape_is_what_the_health_check_expects(replay):
    replay(fixture("user_info"))
    assert ops._check_health(_config()) is True


# ---------------------------------------------------------- subscriptions ----


def test_recorded_subscriptions_expose_id_title_and_url(replay):
    recorded = fixture("subscriptions")
    replay(recorded)
    result = ops.get_subscriptions(_config(), {})
    assert result["subscriptions"]
    for sub in result["subscriptions"]:
        # A downstream mapping keys on `title`; `id` is what edit_subscription takes.
        assert sub["id"].startswith("feed/")
        assert sub["title"]
        assert sub["url"]


def test_recorded_feeds_have_no_categories(replay):
    """The captured account files nothing into folders.

    Recorded rather than asserted-as-desirable: it is why a stream_id of
    `user/-/label/<folder>` resolves to nothing on this account, and the empty
    `categories` list is the shape the connector must not trip over.
    """
    replay(fixture("subscriptions"))
    result = ops.get_subscriptions(_config(), {})
    assert all(sub["categories"] == [] for sub in result["subscriptions"])


# ---------------------------------------------------------------- streams ----


def test_normalization_of_a_real_article(replay):
    # fetch_articles returns the NORMALIZED shape, so drive the normalizer with the
    # raw item recorded inside the fixture rather than re-normalizing twice.
    recorded = fixture("articles")["articles"][0]["raw"]
    normalized = ops._normalize_article(recorded)

    assert normalized["source_id"] == recorded["id"]
    assert normalized["title"]
    assert normalized["url"].startswith("http")
    assert normalized["feed_title"]
    assert normalized["feed_id"].startswith("feed/")
    assert normalized["published_iso"].startswith("20")
    assert isinstance(normalized["is_read"], bool)
    assert normalized["raw"] == recorded


def test_every_recorded_article_normalizes_without_gaps(replay):
    for article in fixture("articles")["articles"]:
        normalized = ops._normalize_article(article["raw"])
        # The five fields a downstream parse step reads. A None here is a silent
        # routing failure downstream, not an exception, so assert it explicitly.
        for field in ("source_id", "title", "url", "feed_title", "published_iso"):
            assert normalized[field], f"{field} empty for {article['raw'].get('id')}"


def test_published_iso_is_derived_from_the_epoch_field(replay):
    raw = fixture("articles")["articles"][0]["raw"]
    normalized = ops._normalize_article(raw)
    assert ops._to_epoch_seconds(normalized["published_iso"]) == int(raw["published"])


def test_categories_carry_the_reading_list_state(replay):
    raw = fixture("articles")["articles"][0]["raw"]
    normalized = ops._normalize_article(raw)
    assert any("state/com.google" in c for c in normalized["categories"])


# --------------------------------------------------------------- item ids ----


def test_recorded_item_ids_carry_timestamps(replay):
    recorded = fixture("item_ids")
    # The fixture holds the connector's own output; itemRefs is the API's shape.
    for ref in recorded["itemRefs"]:
        assert ref["id"]
        assert ref["timestampUsec"]


# ---------------------------------------------------------- unread counts ----


def test_recorded_unread_counts_are_addressable_per_feed(replay):
    replay(fixture("unread_counts"))
    result = ops.get_unread_counts(_config(), {})
    counts = result["unreadcounts"]
    assert counts
    for row in counts:
        assert row["id"]
        assert "count" in row

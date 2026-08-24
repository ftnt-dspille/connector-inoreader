"""Unit tests for the Inoreader connector.

Every HTTP call is faked -- these tests assert on the wire shape the connector
produces (paths, query params, pagination, auth, retry), not on Inoreader's
behaviour. The FortiSOAR-only platform modules are stubbed in conftest.py.

    pytest -q
"""

import json

import pytest
from connectors.core.connector import ConnectorError

from inoreader import operations as ops

CONFIG = {
    "server_url": "https://www.inoreader.com",
    "app_id": "app-id",
    "app_key": "app-key",
    "client_id": "client-id",
    "client_secret": "client-secret",
    "refresh_token": "refresh-token",
    "verify_ssl": True,
    # Pre-seeded so the tests do not have to fake a token refresh every time.
    "access_token": "cached-token",
    "access_token_expiry": 4102444800,  # year 2100
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.headers = headers or {}
        self.content = self.text.encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Recorder:
    """Stands in for requests.request; replays a queue of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


@pytest.fixture
def recorder(monkeypatch):
    def _install(*responses):
        rec = Recorder(responses)
        monkeypatch.setattr(ops.requests, "request", rec)
        return rec

    return _install


def _config():
    return dict(CONFIG)


# ----------------------------------------------------------------- auth ----


def test_headers_carry_app_credentials_and_bearer_token(recorder):
    rec = recorder(FakeResponse(payload={"userId": "1005921515", "userName": "lab"}))
    ops.get_user_info(_config(), {})
    headers = rec.calls[0]["headers"]
    assert headers["AppId"] == "app-id"
    assert headers["AppKey"] == "app-key"
    assert headers["Authorization"] == "Bearer cached-token"


def test_expired_token_is_refreshed_before_the_call(monkeypatch, recorder):
    rec = recorder(FakeResponse(payload={"userId": "1"}))
    posted = {}

    def fake_post(url, data=None, **kwargs):
        posted["url"] = url
        posted["data"] = data
        return FakeResponse(payload={"access_token": "fresh", "expires_in": 3600})

    monkeypatch.setattr(ops.requests, "post", fake_post)
    config = _config()
    config["access_token_expiry"] = 0  # stale

    ops.get_user_info(config, {})

    assert posted["url"] == "https://www.inoreader.com/oauth2/token"
    assert posted["data"]["grant_type"] == "refresh_token"
    assert rec.calls[0]["headers"]["Authorization"] == "Bearer fresh"
    # The new token is cached back onto the config so the next call is free.
    assert config["access_token"] == "fresh"


def test_rotated_refresh_token_is_written_back(monkeypatch, recorder):
    recorder(FakeResponse(payload={"userId": "1"}))
    monkeypatch.setattr(
        ops.requests,
        "post",
        lambda url, data=None, **kw: FakeResponse(
            payload={"access_token": "fresh", "expires_in": 3600, "refresh_token": "rotated"}
        ),
    )
    config = _config()
    config["access_token_expiry"] = 0

    ops.get_user_info(config, {})

    assert config["refresh_token"] == "rotated"


def test_401_triggers_exactly_one_retry(monkeypatch, recorder):
    rec = recorder(FakeResponse(status_code=401, text="Unauthorized"), FakeResponse(payload={"userId": "1"}))
    monkeypatch.setattr(
        ops.requests,
        "post",
        lambda url, data=None, **kw: FakeResponse(payload={"access_token": "fresh", "expires_in": 3600}),
    )

    assert ops.get_user_info(_config(), {})["userId"] == "1"
    assert len(rec.calls) == 2


def test_missing_oauth_config_is_a_clear_error(monkeypatch):
    config = _config()
    config.pop("access_token")
    config["refresh_token"] = ""
    with pytest.raises(ConnectorError, match="OAuth is not fully configured"):
        ops.get_user_info(config, {})


# ------------------------------------------------------------ requests ----


def test_stream_id_defaults_to_the_reading_list(recorder):
    rec = recorder(FakeResponse(payload={"items": []}))
    ops.get_stream_contents(_config(), {})
    assert "reading-list" in rec.calls[0]["url"]


def test_bare_feed_url_is_prefixed_and_url_encoded(recorder):
    rec = recorder(FakeResponse(payload={"items": []}))
    ops.get_stream_contents(_config(), {"stream_id": "https://example.com/releases.atom"})
    # The slashes inside the stream ID must be escaped or Inoreader reads the
    # path as extra segments and 404s.
    assert rec.calls[0]["url"].endswith("feed%2Fhttps%3A%2F%2Fexample.com%2Freleases.atom")


def test_system_stream_type_is_used_when_stream_id_is_blank(recorder):
    rec = recorder(FakeResponse(payload={"items": []}))
    ops.get_stream_contents(_config(), {"stream_type": "Starred"})
    assert "starred" in rec.calls[0]["url"]


def test_unread_only_sets_the_exclude_target(recorder):
    rec = recorder(FakeResponse(payload={"items": []}))
    ops.get_stream_contents(_config(), {"unread_only": True})
    assert rec.calls[0]["params"]["xt"] == ops.STATE_READ


def test_pagination_follows_continuation_and_honours_max_records(recorder):
    rec = recorder(
        FakeResponse(payload={"items": [{"id": str(i)} for i in range(100)], "continuation": "c1"}),
        FakeResponse(payload={"items": [{"id": str(i)} for i in range(100, 150)]}),
    )
    result = ops.get_stream_contents(_config(), {"max_records": 150})
    assert result["count"] == 150
    assert len(rec.calls) == 2
    assert rec.calls[0]["params"]["n"] == 100  # clamped to the server page cap
    assert rec.calls[1]["params"]["c"] == "c1"


def test_pagination_stops_at_max_records_mid_page(recorder):
    rec = recorder(FakeResponse(payload={"items": [{"id": str(i)} for i in range(20)], "continuation": "c1"}))
    result = ops.get_stream_contents(_config(), {"max_records": 20})
    assert result["count"] == 20
    assert len(rec.calls) == 1  # continuation present, but the budget is spent


def test_item_ids_page_size_uses_the_larger_cap(recorder):
    rec = recorder(FakeResponse(payload={"itemRefs": []}))
    ops.get_item_ids(_config(), {"max_records": 5000})
    assert rec.calls[0]["params"]["n"] == 1000


# ------------------------------------------------------------- writes ----


def test_add_subscription_prefixes_the_feed(recorder):
    rec = recorder(FakeResponse(payload={"numResults": 1, "streamId": "feed/x"}))
    ops.add_subscription(_config(), {"feed_url": "https://example.com/releases.atom"})
    assert rec.calls[0]["method"] == "POST"
    assert rec.calls[0]["params"]["quickadd"] == "feed/https://example.com/releases.atom"


def test_add_subscription_requires_a_url():
    with pytest.raises(ConnectorError, match="Feed URL is required"):
        ops.add_subscription(_config(), {})


def test_edit_tag_sends_every_id_in_one_request(recorder):
    rec = recorder(FakeResponse(text="OK"))
    result = ops.edit_tag(_config(), {"item_ids": "a,b,c", "add_tag": "Read"})
    assert rec.calls[0]["params"]["i"] == ["a", "b", "c"]
    assert rec.calls[0]["params"]["a"] == ops.STATE_READ
    assert result["status"] == "OK"


def test_edit_tag_accepts_a_custom_label(recorder):
    rec = recorder(FakeResponse(text="OK"))
    ops.edit_tag(_config(), {"item_ids": ["a"], "add_custom_tag": "Patch Intel"})
    assert rec.calls[0]["params"]["a"] == "user/-/label/Patch Intel"


def test_edit_tag_requires_a_tag():
    with pytest.raises(ConnectorError, match="tag to add"):
        ops.edit_tag(_config(), {"item_ids": ["a"]})


def test_mark_all_as_read_converts_the_cutoff_to_microseconds(recorder):
    rec = recorder(FakeResponse(text="OK"))
    ops.mark_all_as_read(_config(), {"older_than": "2026-01-01T00:00:00Z"})
    assert rec.calls[0]["params"]["ts"] == 1767225600 * 1000000


# ---------------------------------------------------------- ingestion ----

ARTICLE = {
    "id": "tag:google.com,2005:reader/item/00000000abcdef01",
    "title": "Notepad++ 8.9.2 released",
    "author": "don ho",
    "published": 1767225600,
    "canonical": [{"href": "https://example.com/release/8.9.2"}],
    "summary": {"content": "<p>Fixes CVE-2026-0001</p>"},
    "origin": {
        "streamId": "feed/https://example.com/releases.atom",
        "title": "Notepad++ Releases",
        "htmlUrl": "https://example.com",
    },
    "categories": ["user/-/state/com.google/reading-list", "user/-/label/Patch Intel"],
}


def test_fetch_articles_normalizes_the_nested_shape(recorder):
    recorder(FakeResponse(payload={"items": [ARTICLE]}))
    article = ops.fetch_articles(_config(), {})["articles"][0]
    assert article["source_id"] == ARTICLE["id"]
    assert article["url"] == "https://example.com/release/8.9.2"
    assert article["content"] == "<p>Fixes CVE-2026-0001</p>"
    assert article["feed_title"] == "Notepad++ Releases"
    assert article["labels"] == ["Patch Intel"]
    assert article["is_read"] is False
    assert article["published_iso"].startswith("2026-01-01")
    assert article["raw"] == ARTICLE


def test_fetch_articles_pulls_oldest_first_from_the_cursor(recorder):
    rec = recorder(FakeResponse(payload={"items": []}))
    ops.fetch_articles(_config(), {"last_pull_datetime": "2026-01-01T00:00:00Z", "unread_only": True})
    params = rec.calls[0]["params"]
    assert params["r"] == "o"
    assert params["ot"] == 1767225600
    assert params["xt"] == ops.STATE_READ


def test_fetch_articles_marks_read_when_asked(recorder):
    rec = recorder(FakeResponse(payload={"items": [ARTICLE]}), FakeResponse(text="OK"))
    ops.fetch_articles(_config(), {"mark_as_read": True})
    assert rec.calls[1]["url"].endswith("/edit-tag")
    assert rec.calls[1]["params"]["i"] == [ARTICLE["id"]]


def test_fetch_articles_does_not_mark_read_by_default(recorder):
    rec = recorder(FakeResponse(payload={"items": [ARTICLE]}))
    ops.fetch_articles(_config(), {})
    assert len(rec.calls) == 1


def test_millisecond_timestamps_are_tolerated():
    assert ops._to_epoch_seconds(1767225600000) == 1767225600


# -------------------------------------------------------------- health ----


def test_health_check_passes_on_a_user_identity(recorder):
    recorder(FakeResponse(payload={"userId": "1005921515", "userName": "lab"}))
    assert ops._check_health(_config()) is True


def test_health_check_fails_without_an_identity(recorder):
    recorder(FakeResponse(payload={}))
    with pytest.raises(ConnectorError, match="no user identity"):
        ops._check_health(_config())


def test_rate_limit_error_names_the_daily_quota(recorder):
    recorder(FakeResponse(status_code=429, text="Rate limit exceeded"))
    with pytest.raises(ConnectorError, match="DAILY per-zone quota"):
        ops.get_user_info(_config(), {})

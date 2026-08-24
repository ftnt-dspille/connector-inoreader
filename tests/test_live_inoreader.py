"""Live API tests. Skipped unless real credentials are present.

    pytest -m live -v

The offline suite (test_inoreader.py) proves the wire shape the connector
produces. These prove Inoreader actually answers that shape -- the assumptions
no amount of faking can settle: that AppId/AppKey and a bearer token are accepted
together, that `output=json` is honoured, that the normalized article fields the
UC-12 parse step reads are really populated.

QUOTA. Inoreader's limit is a DAILY per-zone quota -- and it is small (100/zone on
a Pro account; read yours off the headers, they vary). So this module normally
spends **nothing**: it replays the responses `tools/live_check.py` captured into
.cache/live/. Eight runs of a four-call suite would otherwise be a third of a
day's reads spent on assertions that would have passed against the same payload.

    python tools/live_check.py          # captures (4 requests)
    pytest -m live                      # replays (0 requests)
    INOREADER_LIVE_REFRESH=1 pytest -m live   # re-fetches (4 requests)

Add an assertion, not a call: route new checks through the existing fixtures.

Excluded from the default run (pyproject sets `-m "not live"`), so CI stays green
and offline without credentials.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import cache as cache_helper  # noqa: E402
import env as env_helper  # noqa: E402

from inoreader import operations as ops  # noqa: E402

pytestmark = pytest.mark.live

_VALUES = env_helper.load()
_MISSING = env_helper.missing(_VALUES)

requires_credentials = pytest.mark.skipif(
    bool(_MISSING),
    reason=f"no live credentials: {', '.join(_MISSING)} unset (see .env.inoreader.example)",
)


@pytest.fixture(scope="session")
def config():
    """One config dict for the module, so the access token is minted once and reused."""
    return env_helper.to_config(_VALUES)


@pytest.fixture(scope="session")
def stream():
    return env_helper.stream_id(_VALUES)


def _cached_or_fetch(name, fetch):
    """Replay a captured response; only touch the API when asked to, or when nothing was captured."""
    if not cache_helper.refresh_requested():
        cached = cache_helper.load(name)
        if cached is not None:
            return cached
        if _MISSING:
            pytest.skip(f"no cached '{name}' and no credentials -- run tools/live_check.py first")
    payload = fetch()
    cache_helper.save(name, payload)
    return payload


@pytest.fixture(scope="session")
def user_info(config):
    return _cached_or_fetch("user_info", lambda: ops.get_user_info(config, {}))


@pytest.fixture(scope="session")
def subscriptions(config):
    return _cached_or_fetch("subscriptions", lambda: ops.get_subscriptions(config, {}))


@pytest.fixture(scope="session")
def tags(config):
    return _cached_or_fetch("tags", lambda: ops.get_tags(config, {"include_counts": True}))


@pytest.fixture(scope="session")
def articles(config, stream):
    return _cached_or_fetch(
        "articles",
        lambda: ops.fetch_articles(config, {"stream_id": stream, "max_records": 5, "unread_only": False}),
    )


# ------------------------------------------------------------------- auth ----


@requires_credentials
def test_refresh_token_exchanges_for_a_working_access_token(user_info, config):
    # Nothing seeds an access token, so a successful call means the refresh-token
    # exchange ran and the minted token was accepted. Meaningless against a
    # replayed payload, so skip unless this run actually hit the API.
    assert user_info.get("userId")
    if not config.get("access_token"):
        pytest.skip("replayed from cache; re-run with INOREADER_LIVE_REFRESH=1 to prove the exchange")


@requires_credentials
def test_app_credentials_and_bearer_token_are_accepted_together(user_info):
    # Inoreader documents AppId/AppKey and OAuth separately; the connector sends
    # both on every request. A 403 here would mean that combination is rejected.
    assert user_info.get("userName")


@requires_credentials
@pytest.mark.skipif(
    not cache_helper.refresh_requested(),
    reason="costs a request; run with INOREADER_LIVE_REFRESH=1",
)
def test_health_check_passes(config):
    # The one test that cannot be replayed -- the health check IS a live call.
    assert ops._check_health(config) is True


# ----------------------------------------------------------- subscriptions ----


@requires_credentials
def test_subscriptions_carry_the_fields_uc12_routes_on(subscriptions):
    subs = subscriptions.get("subscriptions")
    assert subs, "the account has no subscriptions -- subscribe the demo feeds first"
    for sub in subs:
        assert sub.get("id", "").startswith("feed/")
        assert sub.get("title")


@requires_credentials
def test_folders_are_addressable_as_stream_ids(tags):
    entries = tags.get("tags") or []
    assert entries, "no folders or tags on the account"
    folders = [t for t in entries if t.get("type") in ("folder", "tag")]
    for folder in folders:
        # UC-12 addresses its folder as user/-/label/<name>; anything else means
        # the stream_id in the playbook will not resolve.
        assert folder["id"].startswith("user/")


# ---------------------------------------------------------------- content ----


@requires_credentials
def test_stream_returns_articles(articles):
    assert articles["count"] > 0, f"{articles['stream_id']} is empty -- the demo has nothing to act on"


@requires_credentials
def test_normalized_article_has_every_field_the_parse_step_reads(articles):
    if not articles["articles"]:
        pytest.skip("stream is empty")
    article = articles["articles"][0]
    # These five are exactly what UC-12's Parse Advisories step consumes.
    assert article["source_id"]
    assert article["title"]
    assert article["url"]
    assert article["feed_title"]
    assert article["published_iso"]


@requires_credentials
def test_article_content_is_present_for_version_extraction(articles):
    if not articles["articles"]:
        pytest.skip("stream is empty")
    # The version regex runs over title + content. An empty content field is not
    # an error, but it halves what the parser has to work with, so surface it.
    with_content = [a for a in articles["articles"] if a.get("content")]
    assert with_content, "no article carried summary content; version extraction has only titles"


@requires_credentials
def test_feed_titles_are_stable_identifiers(articles):
    if not articles["articles"]:
        pytest.skip("stream is empty")
    # UC-12 keys product_map on feed title. A blank one routes the article nowhere.
    assert all(a.get("feed_title") for a in articles["articles"])

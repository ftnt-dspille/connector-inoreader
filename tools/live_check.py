"""Validate the connector against the real Inoreader API, cheaply.

Everything in tests/ fakes HTTP, so it proves the connector's wire shape and
nothing about Inoreader's behaviour. This is the other half: it runs the real
operations with real credentials and reports on the assumptions the offline
suite cannot reach.

QUOTA. Inoreader's limit is a DAILY per-zone quota (100/zone on Pro), so this
script is written to be frugal and to tell you exactly what it spent:

    read-only (default) : 4 Zone 1 requests
    --write             : + 2 Zone 2 requests (star then unstar one article)

Usage:
    python tools/live_check.py            # read-only
    python tools/live_check.py --write    # also prove the write scope
    python tools/live_check.py --json     # machine-readable, for CI

Credentials come from .env.inoreader (see .env.inoreader.example).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))


def _install_fsr_stubs() -> None:
    """The connector imports appliance-only modules; stub them as tests/conftest.py does."""

    class ConnectorError(Exception):
        pass

    class Connector:
        pass

    core = types.ModuleType("connectors.core.connector")
    core.ConnectorError = ConnectorError
    core.Connector = Connector
    core.get_logger = lambda name: __import__("logging").getLogger(name)
    sys.modules.setdefault("connectors", types.ModuleType("connectors"))
    sys.modules.setdefault("connectors.core", types.ModuleType("connectors.core"))
    sys.modules["connectors.core.connector"] = core


_install_fsr_stubs()

import cache as cache_helper  # noqa: E402
import env as env_helper  # noqa: E402
from connectors.core.connector import ConnectorError  # noqa: E402
from inoreader import operations as ops  # noqa: E402

RATE_HEADERS = (
    "X-Reader-Zone1-Limit",
    "X-Reader-Zone1-Usage",
    "X-Reader-Zone2-Limit",
    "X-Reader-Zone2-Usage",
    "X-Reader-Limits-Reset-After",
)


class Meter:
    """Counts API calls and keeps the last rate-limit headers Inoreader returned."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.limits: dict[str, str] = {}
        self._real = ops.requests.request

    def install(self) -> None:
        def wrapper(method, url, **kwargs):
            response = self._real(method, url, **kwargs)
            self.calls.append((method, url.split("/reader/api/0")[-1].split("?")[0]))
            for header in RATE_HEADERS:
                if response.headers.get(header):
                    self.limits[header] = response.headers[header]
            return response

        ops.requests.request = wrapper

    @property
    def count(self) -> int:
        return len(self.calls)


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def record(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        if not sys.stdout.isatty() and "--json" in sys.argv:
            return
        print(f"  {'PASS' if ok else 'FAIL'}  {name}\n        {detail}")

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]


def _run(report: Report, name: str, fn, cache_as: str | None = None):
    """Run one check; a ConnectorError is a FAIL, not a crash.

    A successful response is cached under `cache_as` so `pytest -m live` can
    assert against it without spending the quota a second time.
    """
    try:
        result = fn()
        if cache_as and result is not None:
            cache_helper.save(cache_as, result)
        return result
    except ConnectorError as err:
        report.record(name, False, str(err))
    except Exception as err:  # noqa: BLE001 - a live check should report, not traceback
        report.record(name, False, f"{type(err).__name__}: {err}")
    return None


# Refuse to spend the day's remaining reads on assertions. 15 leaves room for a
# demo run (1 request) and a few health checks even on a 100/day account.
MIN_ZONE1_REMAINING = 15


def _remaining(meter: "Meter") -> int | None:
    """Zone 1 requests left today, or None if the headers said nothing."""
    try:
        limit = int(meter.limits["X-Reader-Zone1-Limit"])
        usage = int(meter.limits["X-Reader-Zone1-Usage"])
    except (KeyError, ValueError):
        return None
    return limit - usage


def main() -> int:
    write_mode = "--write" in sys.argv
    as_json = "--json" in sys.argv
    quota_only = "--quota" in sys.argv

    values = env_helper.load()
    gaps = env_helper.missing(values)
    if gaps:
        print(f"Missing credentials in {env_helper.ENV_PATH}: {', '.join(gaps)}")
        print("Copy .env.inoreader.example to .env.inoreader, then: python tools/oauth_bootstrap.py")
        return 2

    config = env_helper.to_config(values)
    stream = env_helper.stream_id(values)
    meter = Meter()
    meter.install()
    report = Report()

    if not as_json:
        print(f"\nInoreader live check -- stream: {stream}\n")

    # 1. Auth. Proves three things at once: the refresh-token exchange works, the
    #    AppId/AppKey headers are accepted alongside the bearer token, and the
    #    account is the one you think it is.
    user = _run(report, "auth + /user-info", lambda: ops.get_user_info(config, {}), cache_as="user_info")
    if user:
        report.record(
            "auth + /user-info",
            bool(user.get("userId")),
            f"authenticated as {user.get('userName')} <{user.get('userEmail')}> (id {user.get('userId')})",
        )
        report.record(
            "access token cached on the config",
            bool(config.get("access_token")),
            "the connector minted and cached a token, so later calls do not re-mint",
        )

    left = _remaining(meter)
    if left is not None and not as_json:
        limit = meter.limits.get("X-Reader-Zone1-Limit")
        print(f"\n  quota: {left} of {limit} Zone 1 requests left today\n")
    if quota_only:
        print(f"  {_quota_summary(meter)}\n  spent 1 request")
        return 0
    if left is not None and left < MIN_ZONE1_REMAINING:
        # Stopping here is the point: an exhausted quota during a demo looks
        # exactly like a broken integration, and these checks are not urgent.
        print(f"  STOPPING: only {left} Zone 1 requests left today (floor is {MIN_ZONE1_REMAINING}).")
        print("  The remaining checks would spend 3 more. Re-run after the quota resets;")
        print(f"  {_quota_summary(meter)}")
        return 3

    # 2. Subscriptions. The highest-risk item for UC-12: the playbook routes on FEED
    #    TITLE, so the titles Inoreader assigns have to match its product_map.
    subs = _run(report, "/subscription/list", lambda: ops.get_subscriptions(config, {}), cache_as="subscriptions")
    if subs is not None:
        titles = [s.get("title") for s in (subs.get("subscriptions") or [])]
        report.record(
            "/subscription/list",
            bool(titles),
            f"{len(titles)} subscription(s). UC-12 routes on these EXACT titles:\n        "
            + ("\n        ".join(f"- {t}" for t in titles) if titles else "(none -- subscribe the feeds first)"),
        )

    # 3. Folders/tags. Confirms the demo folder exists and is spelled as the
    #    playbook's stream_id expects.
    tags = _run(report, "/tag/list", lambda: ops.get_tags(config, {"include_counts": True}), cache_as="tags")
    if tags is not None:
        folders = [t.get("id") for t in (tags.get("tags") or []) if t.get("type") in ("folder", "tag")]
        report.record(
            "/tag/list",
            True,
            f"{len(folders)} folder(s)/tag(s): {', '.join(folders) or '(none)'}",
        )
        if stream.startswith("user/-/label/"):
            report.record(
                "demo folder exists",
                stream in folders,
                f"{stream} {'found' if stream in folders else 'NOT found -- create it or fix INOREADER_STREAM_ID'}",
            )

    # 4. Stream contents. The operation UC-12 actually calls. Checks the normalized
    #    fields the playbook's parse step reads, not just that a 200 came back.
    articles = _run(
        report,
        "fetch_articles",
        lambda: ops.fetch_articles(config, {"stream_id": stream, "max_records": 5, "unread_only": False}),
        cache_as="articles",
    )
    first = None
    if articles is not None:
        items = articles.get("articles") or []
        first = items[0] if items else None
        report.record(
            "fetch_articles",
            bool(items),
            f"{len(items)} article(s) from {stream}"
            + (f"; newest: {first['title']!r} from {first['feed_title']!r}" if first else " (stream is empty)"),
        )
    if first:
        needed = ("source_id", "title", "url", "content", "feed_title", "published_iso")
        empty = [f for f in needed if not first.get(f)]
        report.record(
            "normalized fields the UC-12 parse step reads",
            not empty,
            "all present" if not empty else f"EMPTY on the newest article: {', '.join(empty)}",
        )

    # 5. Write scope, opt-in. Star then immediately unstar one article: two Zone 2
    #    requests, and the account is left exactly as it was found.
    if write_mode and first:
        article_id = first["source_id"]
        starred = _run(
            report,
            "edit-tag (write scope)",
            lambda: ops.edit_tag(config, {"item_ids": [article_id], "add_tag": "Starred"}),
        )
        if starred:
            ops.edit_tag(config, {"item_ids": [article_id], "remove_tag": "Starred"})
            report.record(
                "edit-tag (write scope)",
                str(starred.get("status", "")).upper() in ("OK", "SUCCESS"),
                "starred and un-starred one article; the account is unchanged",
            )
    elif write_mode:
        report.record("edit-tag (write scope)", False, "skipped: no article to tag")

    quota = _quota_summary(meter)
    if as_json:
        print(json.dumps({"checks": report.checks, "requests": meter.count, "limits": meter.limits}, indent=2))
    else:
        print(f"\n  spent {meter.count} request(s): {', '.join(path for _, path in meter.calls)}")
        print(f"  responses cached for `pytest -m live` -- {cache_helper.summary()}")
        print(f"  {quota}\n")
        print("  " + ("all checks passed" if not report.failed else f"{len(report.failed)} check(s) FAILED"))

    return 1 if report.failed else 0


def _quota_summary(meter: Meter) -> str:
    if not meter.limits:
        return "no rate-limit headers returned (so the documented 100/zone/day is still unverified)"
    zone1 = f"Zone 1: {meter.limits.get('X-Reader-Zone1-Usage', '?')}/{meter.limits.get('X-Reader-Zone1-Limit', '?')}"
    zone2 = f"Zone 2: {meter.limits.get('X-Reader-Zone2-Usage', '?')}/{meter.limits.get('X-Reader-Zone2-Limit', '?')}"
    reset = meter.limits.get("X-Reader-Limits-Reset-After")
    return f"{zone1}  {zone2}" + (f"  (resets in {reset}s)" if reset else "")


if __name__ == "__main__":
    raise SystemExit(main())

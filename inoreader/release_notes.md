### 1.0.3

Token maintenance now follows the platform convention used by Fortinet's own
OAuth connectors: the refreshed token is written back with
`update_connnector_config(name, version, config, config_id)`, the version is read
from `info.json` at runtime rather than from a constant that can drift, and the
whole configuration dict is passed through.

Adds recorded-payload tests and a fixture-serving mock (`tools/mock_server.py`).

### 1.0.2

A failed OAuth token refresh no longer has its message replaced by the generic
"Request to Inoreader failed". The token endpoint's own explanation (invalid
client credentials, revoked refresh token) now reaches the operator.

Adds `tools/oauth_bootstrap.py` (mint a refresh token), `tools/live_check.py`
(validate against the real API on a 4-request budget), and an opt-in live test
suite (`pytest -m live`).

### 1.0.1

Ships the connector icons (`images/small_icon.png`, `images/large_icon.png`) that
`info.json` already referenced, so the tile renders in Content Hub instead of
showing blank.

### 1.0.0

Initial release.

**Actions**: Fetch Articles, Get Stream Contents, Get Article IDs, Get Subscriptions,
Get Folders and Tags, Get Unread Counts, Get User Information, Add Subscription,
Edit Subscription, Tag Articles, Mark Stream as Read.

**Data ingestion**: Scheduled ingestion of new articles from any stream (feed, folder,
or the full reading list), with an optional "mark as read" de-duplication ledger.

**Known limitations**
- Inoreader's rate limits are per-day, not per-minute (100 requests per zone on a Pro
  plan). Schedules faster than every 30 minutes will exhaust the quota.
- The OAuth refresh token must be obtained once, out of band; the connector cannot run
  the interactive authorization-code step itself.

# connector-inoreader

A FortiSOAR connector -- **Inoreader**, `name: inoreader` -- that reads feeds from
[Inoreader](https://www.inoreader.com) over the Reader API, so a playbook can act
on feed-driven intelligence (vendor release notes, advisories, CVE disclosures)
without parsing five vendors' RSS dialects itself.

It is the hosted replacement for a self-hosted RSS intake (Miniflux) in a patch
pipeline: one API answers "what shipped that we care about".

## Layout

```
inoreader/            # the FortiSOAR connector package (FSR loads inoreader.connector)
  info.json           # manifest: configuration, operations, ingestion schema
  connector.py        # Connector subclass: execute() + check_health()
  constants.py        # endpoints, stream IDs, rate-limit headers, error strings
  operations.py       # REST client + the 11 operation handlers
  images/             # 150x150 and 500x500 tiles
  requirements.txt
tests/                # off-box suite; FSR platform modules stubbed in conftest.py
  test_live_inoreader.py  # opt-in: real API, `pytest -m live`
tools/                # oauth_bootstrap.py, live_check.py, make_icon.py
.github/workflows/    # ruff + info.json validation + pytest 3.10-3.13; tag -> .tgz release
```

## Install

Grab `inoreader.tgz` from a [release](../../releases) (or run the Release workflow)
and import it via **Content Hub → Manage Connectors → Import**. To build it yourself:

```
tar --exclude='__pycache__' -czf inoreader.tgz inoreader
```

## The API, in short

| Thing | Value |
|---|---|
| Base URL | `https://www.inoreader.com/reader/api/0` |
| Docs | <https://www.inoreader.com/developers/> |
| App auth | `AppId` and `AppKey` headers (identify the *application*) |
| User auth | OAuth 2.0 -- `Authorization: Bearer <access_token>` |
| Token endpoint | `https://www.inoreader.com/oauth2/token` |
| Authorize endpoint | `https://www.inoreader.com/oauth2/auth` |
| Scopes | `read`, or `read write` for any tagging/subscribing action |
| Pagination | `n` (page size) + `c` (continuation cursor); absent `continuation` = end of stream |
| Rate limits | **Per day**, per zone. 100/zone on Pro. Headers: `X-Reader-Zone{1,2}-{Limit,Usage}`, `X-Reader-Limits-Reset-After` |

Endpoints used:

| Endpoint | Used by |
|---|---|
| `GET /user-info` | Get User Information, health check |
| `GET /subscription/list` | Get Subscriptions |
| `POST /subscription/quickadd` | Add Subscription |
| `POST /subscription/edit` | Edit Subscription |
| `GET /tag/list` | Get Folders and Tags |
| `GET /unread-count` | Get Unread Counts |
| `GET /stream/contents/{streamId}` | Get Stream Contents, Fetch Articles |
| `GET /stream/items/ids` | Get Article IDs |
| `POST /edit-tag` | Tag Articles |
| `POST /mark-all-as-read` | Mark Stream as Read |

Stream IDs: `feed/<feed url>`, `user/-/label/<Folder>`, or a system state such as
`user/-/state/com.google/reading-list` / `.../read` / `.../starred`. Operations that
take a Stream ID also accept a bare feed URL and add the `feed/` prefix for you.

## Read this before you schedule ingestion

**Inoreader's rate limit is a daily quota, not a per-minute one.** A Pro plan gets
100 Zone 1 (read) and 100 Zone 2 (write) requests *per day*. The Miniflux device this
replaces polled every 5 minutes; doing that here is 288 requests and the feed goes
dark before lunch. Practical settings:

- Ingestion schedule: 30 minutes or slower (48 pulls/day).
- `Maximum Articles per Pull`: 100. Each additional 100 is another request.
- Leave `Mark Ingested Articles as Read` off unless you need it -- it spends Zone 2.
- Use **Get Article IDs** (cheap) to test for new content before pulling contents.

## Configuration

1. Register an application at <https://www.inoreader.com/developers/register-app>.
   You get an **App ID** and **App Key**; these double as the OAuth client ID and
   secret for most registrations.
2. Complete the authorization-code flow once, by hand, to obtain a **refresh token**:

   ```
   # 1. Open in a browser, approve, and copy the `code` from the redirect:
   https://www.inoreader.com/oauth2/auth?client_id=<APP_ID>&redirect_uri=<REDIRECT_URI>&response_type=code&scope=read%20write&state=<random>

   # 2. Exchange it:
   curl -s https://www.inoreader.com/oauth2/token \
     -d client_id=<APP_ID> -d client_secret=<APP_KEY> \
     -d grant_type=authorization_code -d code=<CODE> \
     -d redirect_uri=<REDIRECT_URI>
   ```

   The response's `refresh_token` goes into the connector configuration. The
   connector exchanges it for access tokens itself, caches each one until it
   expires, and writes a rotated refresh token back onto the configuration.
3. Fill in Server URL (default is right), App ID, App Key, OAuth Client ID, OAuth
   Client Secret, Refresh Token. The health check calls `/user-info` and only passes
   if Inoreader returns a user identity.

Scope note: `read` is enough for every investigation action. Add `write` for Add
Subscription, Edit Subscription, Tag Articles, Mark Stream as Read, and the
`mark_as_read` option on ingestion.

## Data ingestion

`Fetch Articles` is the ingestion operation. It pulls oldest-first from
`last_pull_datetime` so a truncated run resumes from a sane cursor instead of
re-reading the newest page forever, and it flattens Inoreader's nested article shape
into fields a mapping can address directly:

```
source_id, title, author, published, published_iso, updated, url, content,
feed_id, feed_title, feed_url, categories, is_read, labels, raw
```

De-duplicate on `source_id` (Inoreader's article ID is stable). Optionally set
`Mark Ingested Articles as Read` and `Unread Articles Only` together to make
Inoreader itself the de-duplication ledger.

## Development

```
pip install -e '.[dev]'
pytest -q          # 27 offline tests; live tests are deselected
ruff check . && ruff format --check .
```

## Validating against the real API

The offline suite proves the wire shape the connector produces. It says nothing
about whether Inoreader answers that shape. To settle that:

```
cp .env.inoreader.example .env.inoreader   # gitignored; fill in App ID / App Key
python tools/oauth_bootstrap.py            # prints the URL to approve
python tools/oauth_bootstrap.py <code>     # exchanges it, writes the refresh token
python tools/live_check.py                 # 4 requests, reports what it found
pytest -m live -v                          # the same ground, as assertions
```

`live_check.py` reports on the things faked tests cannot reach: that the refresh
token exchanges, that AppId/AppKey and a bearer token are accepted together, that
the account's **feed titles** are what a downstream mapping expects, and what the
rate-limit headers actually say your quota is. Add `--write` to prove the write
scope (it stars and immediately un-stars one article, leaving the account as it
was found), or `--json` for CI.

### Recorded fixtures and the mock server

`tools/live_check.py` caches every response it receives; `tools/make_fixtures.py`
scrubs those captures (user id, name, email, numeric stream ids) and writes
`tests/fixtures/*.json`, which **are** committed. Feed titles, article titles and
URLs are deliberately left intact -- they are public publisher output and the
exact values a downstream mapping keys on, so inventing them would make the
fixture evidence of nothing.

```
python tools/live_check.py        # capture (4 requests)
python tools/make_fixtures.py     # scrub -> tests/fixtures/
python tools/make_fixtures.py --check   # fail if fixtures drift from the capture
```

`tests/test_recorded_payloads.py` asserts the connector's behaviour against those
real payloads -- no credentials, no quota, runs in CI like any other unit test.

The same fixtures can be **served to the connector**:

```
python tools/mock_server.py       # http://127.0.0.1:8099
```

Point a FortiSOAR connector configuration's **Server URL** at that address and
every read operation answers from the fixtures, with no internet, no credentials
(the token endpoint hands one to anybody -- so never expose it) and no quota
spend. Good for a demo without egress, or for iterating on a playbook's parse
step against stable input.

### Mind the quota

Inoreader's limit is a **daily** per-zone budget, and it is small -- 100 requests
per zone on a Pro account (read yours off the headers; they vary by plan). Every
piece of this tooling is built around that:

| Command | Cost |
|---|---|
| `python tools/live_check.py --quota` | **1** request -- how much is left today |
| `python tools/live_check.py` | **4** Zone 1 requests, and it caches what it fetched |
| `pytest -m live` | **0** -- replays the cache |
| `INOREADER_LIVE_REFRESH=1 pytest -m live` | 4, deliberately |
| `python tools/live_check.py --write` | + 2 Zone 2 (a separate budget from reads) |
| `pytest -q` (default) | 0 -- live tests are deselected |

Two guards beyond that: `live_check.py` reads the quota off the very first
response and **stops before the remaining three checks** if fewer than 15 Zone 1
requests are left, and the connector logs the rate-limit headers on every single
response, so a FortiSOAR run leaves a trail of what it spent.

Budget for a demo day on a 100/day account: a scheduled ingestion at 30 minutes
is 48, one playbook run is 1, a health check is 1. Leave ingestion off while
iterating, and prefer `--quota` over a full check when you just want to know
where you stand.

The FortiSOAR `connectors.*` packages are stubbed in `tests/conftest.py` and every
HTTP call is faked, so the suite runs off-appliance and asserts on the wire shape
the connector produces -- paths, query params, pagination, auth, retry -- not on
Inoreader's behaviour. Ruff lints the test suite only; the connector package
follows FortiSOAR's own house style.

## Icons

`inoreader/images/{small,large}_icon.png` are generated by `tools/make_icon.py`
(Pillow): the standard RSS glyph in white on an Inoreader-blue rounded square,
drawn at 8x and downsampled. It is deliberately the generic RSS mark rather than
Inoreader's wordmark -- it stays legible at 150px in the Content Hub grid and
ships no third-party brand asset. Regenerate with:

```
uvx --from pillow python tools/make_icon.py \
  inoreader/images/small_icon.png inoreader/images/large_icon.png
```

## Not included

- `inoreader/playbooks/playbooks.json` -- no sample playbook collection yet.

## License

MIT -- see [LICENSE](LICENSE).

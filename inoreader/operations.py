"""
Copyright start
MIT License
Copyright (c) 2026 Fortinet Inc
Copyright end
"""

import time
from datetime import datetime, timezone

import requests
from connectors.core.connector import get_logger, ConnectorError

from .constants import (
    API_PATH,
    CONNECTOR_NAME,
    CONNECTOR_VERSION,
    DEFAULT_SERVER_URL,
    ERROR_MESSAGES,
    MAX_PAGE_SIZE_CONTENTS,
    MAX_PAGE_SIZE_IDS,
    RATE_LIMIT_HEADERS,
    READING_LIST,
    STATE_READ,
    SYSTEM_TAGS,
    TOKEN_EXPIRY_SKEW_SECONDS,
    TOKEN_PATH,
)

logger = get_logger(CONNECTOR_NAME)

# Persisting the refreshed token back onto the configuration is what keeps a
# rotated refresh token (Inoreader may hand back a new one) and stops every
# operation spending a request to re-mint an access token. The platform exports
# the helper from connectors.core.utils -- which is where every Fortinet
# connector doing OAuth takes it from -- and also re-exports it from
# connectors.core.connector on some releases.
try:
    from connectors.core.utils import update_connnector_config
except ImportError:  # pragma: no cover
    try:
        from connectors.core.connector import update_connnector_config
    except ImportError:  # pragma: no cover - local dev / unit tests
        update_connnector_config = None

try:
    from integrations.crudhub import trigger_ingest_playbook
except ImportError:  # pragma: no cover - local dev / unit tests
    trigger_ingest_playbook = None


class Inoreader(object):
    """Thin REST client. One instance per operation call.

    Auth is deliberately two-layered, because Inoreader is:
      * AppId/AppKey headers identify the *application* and are what the daily
        rate-limit quota is counted against.
      * An OAuth 2.0 bearer token identifies the *user* whose feeds are read.
    Application credentials alone will authenticate but return nothing useful,
    so both are sent when both are configured.
    """

    def __init__(self, config, connector_info=None):
        self.config = config
        # {'connector_name': ..., 'connector_version': ...}, built by connector.py
        # from info.json. Taking the version from the manifest at runtime rather
        # than from a constant here is deliberate: a constant drifts from
        # info.json on the next version bump, and the config update then targets
        # a version that does not exist.
        self.connector_info = connector_info or {}
        server_url = (config.get('server_url') or DEFAULT_SERVER_URL).strip().rstrip('/')
        if not server_url.startswith('http'):
            server_url = 'https://' + server_url
        self.server_url = server_url
        self.base_url = self.server_url + API_PATH
        self.verify_ssl = config.get('verify_ssl', True)
        self.app_id = (config.get('app_id') or '').strip()
        self.app_key = (config.get('app_key') or '').strip()
        self.client_id = (config.get('client_id') or '').strip()
        self.client_secret = config.get('client_secret') or ''
        self.refresh_token = config.get('refresh_token') or ''

    # ---------------------------------------------------------------- auth --

    def _token_is_fresh(self):
        expiry = self.config.get('access_token_expiry')
        if not (self.config.get('access_token') and expiry):
            return False
        try:
            return float(expiry) - TOKEN_EXPIRY_SKEW_SECONDS > time.time()
        except (TypeError, ValueError):
            return False

    def _refresh_access_token(self):
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise ConnectorError(
                'OAuth is not fully configured. Client ID, Client Secret, and Refresh Token are all '
                'required to obtain an access token from Inoreader.')
        payload = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
        }
        logger.info('Refreshing the Inoreader OAuth access token')
        try:
            response = requests.post(self.server_url + TOKEN_PATH, data=payload,
                                     verify=self.verify_ssl, timeout=60)
        except requests.exceptions.SSLError:
            raise ConnectorError(ERROR_MESSAGES['ssl_error'])
        except requests.exceptions.Timeout:
            raise ConnectorError(ERROR_MESSAGES['time_out'])
        except Exception as err:
            raise ConnectorError('Could not reach the Inoreader token endpoint: {}'.format(err))

        if response.status_code != 200:
            raise ConnectorError('Token refresh failed [{}]: {}'.format(
                response.status_code, _safe_text(response)))

        data = response.json()
        access_token = data.get('access_token')
        if not access_token:
            raise ConnectorError('Token refresh succeeded but returned no access_token: {}'.format(data))

        updated = {
            'access_token': access_token,
            'access_token_expiry': time.time() + float(data.get('expires_in') or 3600),
        }
        # Inoreader can rotate the refresh token. Dropping the new one on the
        # floor leaves the configuration working now and dead tomorrow, which
        # is the worst possible failure mode -- so persist it.
        if data.get('refresh_token') and data['refresh_token'] != self.refresh_token:
            updated['refresh_token'] = data['refresh_token']
            self.refresh_token = data['refresh_token']
        self.config.update(updated)
        self._persist_config(updated)
        return access_token

    def _persist_config(self, updated):
        """Write the refreshed token back onto the connector configuration.

        The whole config dict is passed through, config_id included, which is the
        shape every Fortinet OAuth connector uses (see azure-active-directory's
        microsoft_api_auth.py and fortinet-fortiflex's fortiflex_api_auth.py).
        """
        if not update_connnector_config:
            return
        config_id = self.config.get('config_id')
        if not config_id:
            # Off-box, or an operation invoked without a stored configuration.
            logger.info('No config_id on the configuration; the refreshed token will not be persisted')
            return
        self.config.update(updated)
        try:
            update_connnector_config(
                self.connector_info.get('connector_name') or CONNECTOR_NAME,
                self.connector_info.get('connector_version') or CONNECTOR_VERSION,
                self.config,
                config_id,
            )
        except Exception as err:
            # Not fatal for THIS call -- the token in hand still works. It is
            # expensive if it keeps happening, though: every subsequent operation
            # re-mints, and a rotated refresh token is lost outright, so this is a
            # warning rather than a debug line.
            logger.warning(
                'Could not persist the refreshed Inoreader token (every operation will re-mint, '
                'and a rotated refresh token would be lost): {}'.format(err))

    def _headers(self, extra=None):
        headers = {'Accept': 'application/json'}
        if self.app_id and self.app_key:
            headers['AppId'] = self.app_id
            headers['AppKey'] = self.app_key
        token = self.config.get('access_token') if self._token_is_fresh() else self._refresh_access_token()
        headers['Authorization'] = 'Bearer {}'.format(token)
        if extra:
            headers.update(extra)
        return headers

    # ------------------------------------------------------------- request --

    def make_request(self, endpoint, method='GET', params=None, data=None, headers=None, retry_auth=True):
        url = endpoint if endpoint.startswith('http') else self.base_url + endpoint
        request_params = dict(params or {})
        # `output=json` is not the default on several of these endpoints; without
        # it Inoreader answers XML and the json() call below blows up with a
        # message that says nothing about the real cause.
        request_params.setdefault('output', 'json')
        try:
            response = requests.request(method, url, params=request_params, data=data,
                                        headers=self._headers(headers), verify=self.verify_ssl,
                                        timeout=120)
        except requests.exceptions.SSLError:
            raise ConnectorError(ERROR_MESSAGES['ssl_error'])
        except requests.exceptions.Timeout:
            raise ConnectorError(ERROR_MESSAGES['time_out'])
        except ConnectorError:
            # _headers() raises this when the token refresh fails, and its message
            # already names the real cause (invalid client credentials, a revoked
            # refresh token). Re-wrapping it as 'Request to Inoreader failed' buries
            # the one sentence the operator needs. Found by tools/live_check.py.
            raise
        except Exception as err:
            raise ConnectorError('Request to Inoreader failed: {}'.format(err))

        _log_rate_limits(response)

        if response.status_code == 401 and retry_auth:
            # The cached token was revoked or expired early. Mint one and retry
            # exactly once, so a bad credential still fails fast.
            logger.info('Inoreader returned 401; refreshing the token and retrying once')
            self.config.pop('access_token_expiry', None)
            self._refresh_access_token()
            return self.make_request(endpoint, method=method, params=params, data=data,
                                     headers=headers, retry_auth=False)

        if response.ok:
            if not response.content:
                return {'status': 'Success'}
            try:
                return response.json()
            except ValueError:
                # edit-tag, mark-all-as-read and friends answer the literal
                # string "OK", not JSON.
                return {'status': response.text.strip() or 'Success'}

        message = ERROR_MESSAGES.get(response.status_code, 'Request failed')
        raise ConnectorError('{} [{}]: {}'.format(message, response.status_code, _safe_text(response)))

    def paginate(self, endpoint, params, collection_key, max_records, page_size_cap):
        """Follow Inoreader's `continuation` cursor until the stream ends.

        Every page is one more request against a DAILY quota, so max_records is
        treated as a hard stop rather than a hint.
        """
        collected = []
        query = dict(params or {})
        while True:
            remaining = max_records - len(collected)
            query['n'] = min(page_size_cap, remaining)
            response = self.make_request(endpoint, params=query)
            page = response.get(collection_key) or []
            collected.extend(page)
            continuation = response.get('continuation')
            if not continuation or not page or len(collected) >= max_records:
                break
            query['c'] = continuation
        return collected[:max_records]


def _safe_text(response):
    try:
        return response.text[:1000]
    except Exception:
        return ''


def _log_rate_limits(response):
    seen = {h: response.headers.get(h) for h in RATE_LIMIT_HEADERS if response.headers.get(h)}
    if seen:
        logger.info('Inoreader rate limits: {}'.format(seen))


def _client(config, kwargs):
    """Build the API client, carrying connector_info through from execute()."""
    return Inoreader(config, connector_info=(kwargs or {}).get('connector_info'))


def _build_params(params):
    return {k: v for k, v in (params or {}).items() if v is not None and v != '' and v != {} and v != []}


def _resolve_stream_id(params):
    """Accept either a raw stream ID or the friendly picker value."""
    stream_id = (params.get('stream_id') or '').strip()
    stream_type = params.get('stream_type')
    if stream_id:
        # A bare feed URL is the most common thing a user pastes in.
        if stream_id.startswith(('http://', 'https://')):
            return 'feed/' + stream_id
        return stream_id
    if stream_type in SYSTEM_TAGS:
        return SYSTEM_TAGS[stream_type]
    return READING_LIST


def _as_list(value):
    if value is None or value == '':
        return []
    if isinstance(value, list):
        return value
    return [item.strip() for item in str(value).split(',') if item.strip()]


def _resolve_tag(tag_name, custom_tag):
    if custom_tag:
        custom_tag = custom_tag.strip()
        return custom_tag if custom_tag.startswith('user/') else 'user/-/label/{}'.format(custom_tag)
    return SYSTEM_TAGS.get(tag_name)


# ------------------------------------------------------------- operations --

def get_user_info(config, params, **kwargs):
    return _client(config, kwargs).make_request('/user-info')


def get_subscriptions(config, params, **kwargs):
    query = _build_params({'team_assets': 1 if params.get('include_team_assets') else None})
    return _client(config, kwargs).make_request('/subscription/list', params=query)


def add_subscription(config, params, **kwargs):
    feed_url = (params.get('feed_url') or '').strip()
    if not feed_url:
        raise ConnectorError('Feed URL is required.')
    if not feed_url.startswith('feed/'):
        feed_url = 'feed/' + feed_url
    # quickadd is a POST but takes its argument in the query string, and unlike
    # most write endpoints it answers JSON.
    return _client(config, kwargs).make_request('/subscription/quickadd', method='POST',
                                          params={'quickadd': feed_url})


def edit_subscription(config, params, **kwargs):
    stream_id = _resolve_stream_id(params)
    query = _build_params({
        'ac': params.get('action') or 'edit',
        's': stream_id,
        't': params.get('title'),
        'a': _resolve_tag(None, params.get('add_to_folder')),
        'r': _resolve_tag(None, params.get('remove_from_folder')),
    })
    return _client(config, kwargs).make_request('/subscription/edit', method='POST', params=query)


def get_tags(config, params, **kwargs):
    query = _build_params({
        'types': 1,
        'counts': 1 if params.get('include_counts') else None,
        'team_assets': 1 if params.get('include_team_assets') else None,
    })
    return _client(config, kwargs).make_request('/tag/list', params=query)


def get_unread_counts(config, params, **kwargs):
    return _client(config, kwargs).make_request('/unread-count')


def get_stream_contents(config, params, **kwargs):
    client = _client(config, kwargs)
    stream_id = _resolve_stream_id(params)
    max_records = int(params.get('max_records') or MAX_PAGE_SIZE_CONTENTS)
    query = _build_params({
        'r': 'o' if params.get('oldest_first') else None,
        'ot': _to_epoch_seconds(params.get('start_time')),
        'xt': STATE_READ if params.get('unread_only') else None,
        'it': _resolve_tag(params.get('include_tag'), params.get('include_custom_tag')),
        'annotations': 1 if params.get('include_annotations') else None,
    })
    endpoint = '/stream/contents/{}'.format(requests.utils.quote(stream_id, safe=''))
    items = client.paginate(endpoint, query, 'items', max_records, MAX_PAGE_SIZE_CONTENTS)
    return {'stream_id': stream_id, 'count': len(items), 'items': items}


def get_item_ids(config, params, **kwargs):
    client = _client(config, kwargs)
    stream_id = _resolve_stream_id(params)
    max_records = int(params.get('max_records') or MAX_PAGE_SIZE_IDS)
    query = _build_params({
        's': stream_id,
        'r': 'o' if params.get('oldest_first') else None,
        'ot': _to_epoch_seconds(params.get('start_time')),
        'xt': STATE_READ if params.get('unread_only') else None,
        'includeAllDirectStreamIds': 'false' if params.get('exclude_folder_tags') else None,
    })
    item_refs = client.paginate('/stream/items/ids', query, 'itemRefs', max_records, MAX_PAGE_SIZE_IDS)
    return {'stream_id': stream_id, 'count': len(item_refs), 'itemRefs': item_refs}


def edit_tag(config, params, **kwargs):
    item_ids = _as_list(params.get('item_ids'))
    if not item_ids:
        raise ConnectorError('At least one Article ID is required.')
    add_tag = _resolve_tag(params.get('add_tag'), params.get('add_custom_tag'))
    remove_tag = _resolve_tag(params.get('remove_tag'), params.get('remove_custom_tag'))
    if not (add_tag or remove_tag):
        raise ConnectorError('Specify a tag to add, a tag to remove, or both.')
    # `i` repeats once per article; requests renders a list as repeated keys,
    # which is exactly the wire format Inoreader wants.
    query = {'i': item_ids}
    if add_tag:
        query['a'] = add_tag
    if remove_tag:
        query['r'] = remove_tag
    result = _client(config, kwargs).make_request('/edit-tag', method='POST', params=query)
    return {'status': result.get('status', 'Success'), 'item_ids': item_ids,
            'added': add_tag, 'removed': remove_tag}


def mark_all_as_read(config, params, **kwargs):
    stream_id = _resolve_stream_id(params)
    older_than = _to_epoch_seconds(params.get('older_than'))
    query = _build_params({
        's': stream_id,
        # Microseconds, not seconds: articles newer than `ts` stay unread.
        'ts': int(older_than * 1000000) if older_than else None,
    })
    result = _client(config, kwargs).make_request('/mark-all-as-read', method='POST', params=query)
    return {'status': result.get('status', 'Success'), 'stream_id': stream_id}


def fetch_articles(config, params, **kwargs):
    """Ingestion entry point: pull new articles and hand them to the create playbook."""
    client = _client(config, kwargs)
    stream_id = _resolve_stream_id(params)
    max_records = int(params.get('max_records') or MAX_PAGE_SIZE_CONTENTS)
    start_time = _to_epoch_seconds(params.get('last_pull_datetime'))
    query = _build_params({
        # Oldest first so a truncated run resumes from a sane cursor next time
        # rather than re-reading the same newest page forever.
        'r': 'o',
        'ot': start_time,
        'xt': STATE_READ if params.get('unread_only') else None,
    })
    endpoint = '/stream/contents/{}'.format(requests.utils.quote(stream_id, safe=''))
    items = client.paginate(endpoint, query, 'items', max_records, MAX_PAGE_SIZE_CONTENTS)

    if params.get('mark_as_read') and items:
        # Marking read is the de-duplication ledger when the caller polls with
        # unread_only: it makes the next run's window self-evident.
        edit_tag(config, {'item_ids': [item.get('id') for item in items if item.get('id')],
                          'add_tag': 'Read'}, **kwargs)

    normalized = [_normalize_article(item) for item in items]
    if params.get('create_pb_id') and trigger_ingest_playbook:
        trigger_ingest_playbook(normalized, params['create_pb_id'],
                                parent_env=kwargs.get('env', {}), batch_size=100,
                                dedup_field=params.get('dedup_field') or 'source_id')
        return {'status': 'Success', 'ingested': len(normalized)}
    return {'stream_id': stream_id, 'count': len(normalized), 'articles': normalized}


def _normalize_article(item):
    """Flatten Inoreader's nested article shape into something a mapping can address."""
    canonical = (item.get('canonical') or [{}])
    alternate = (item.get('alternate') or [{}])
    origin = item.get('origin') or {}
    categories = item.get('categories') or []
    published = item.get('published')
    return {
        'source_id': item.get('id'),
        'title': item.get('title'),
        'author': item.get('author'),
        'published': published,
        'published_iso': _epoch_to_iso(published),
        'updated': item.get('updated'),
        'url': (canonical[0] or {}).get('href') or (alternate[0] or {}).get('href'),
        'content': (item.get('summary') or {}).get('content'),
        'feed_id': origin.get('streamId'),
        'feed_title': origin.get('title'),
        'feed_url': origin.get('htmlUrl'),
        'categories': categories,
        'is_read': STATE_READ in categories,
        'labels': [c.split('/label/')[-1] for c in categories if '/label/' in c],
        'raw': item,
    }


def _to_epoch_seconds(value):
    """Accept whatever a FortiSOAR datetime field, a macro, or a user hands over."""
    if value in (None, ''):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Tolerate milliseconds, which is what a datetime picker yields once it
        # has been through some Jinja filters.
        return int(value / 1000) if value > 1e11 else int(value)
    text = str(value).strip()
    if text.isdigit():
        return _to_epoch_seconds(int(text))
    try:
        # fromisoformat rejects the trailing Z until 3.11; normalize it first.
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        pass
    try:
        import arrow
        return arrow.get(text).int_timestamp
    except Exception as err:
        raise ConnectorError('Could not parse the datetime "{}": {}'.format(value, err))


def _epoch_to_iso(value):
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _check_health(config, **kwargs):
    client = _client(config, kwargs)
    try:
        response = client.make_request('/user-info')
    except ConnectorError:
        raise
    except Exception as err:
        raise ConnectorError('Health check failed: {}'.format(err))
    if not response.get('userId'):
        raise ConnectorError('Health check failed: Inoreader returned no user identity: {}'.format(response))
    logger.info('Inoreader health check succeeded for user {}'.format(response.get('userName')))
    return True


operations = {
    'get_user_info': get_user_info,
    'get_subscriptions': get_subscriptions,
    'add_subscription': add_subscription,
    'edit_subscription': edit_subscription,
    'get_tags': get_tags,
    'get_unread_counts': get_unread_counts,
    'get_stream_contents': get_stream_contents,
    'get_item_ids': get_item_ids,
    'edit_tag': edit_tag,
    'mark_all_as_read': mark_all_as_read,
    'fetch_articles': fetch_articles,
}

"""
Copyright start
MIT License
Copyright (c) 2026 Fortinet Inc
Copyright end
"""

CONNECTOR_NAME = 'inoreader'
CONNECTOR_VERSION = '1.0.2'

DEFAULT_SERVER_URL = 'https://www.inoreader.com'
API_PATH = '/reader/api/0'
TOKEN_PATH = '/oauth2/token'

# Inoreader caps a single stream/contents page at 100 items and a single
# stream/items/ids page at 1000. Anything larger is silently clamped by the
# server, so the pagination loop below has to do the multiplying itself.
MAX_PAGE_SIZE_CONTENTS = 100
MAX_PAGE_SIZE_IDS = 1000

# Zone 1 (read) and Zone 2 (write) quotas are DAILY, and on a Pro plan they are
# only 100 requests each. That is the single most important number in this
# integration: a five-minute ingestion schedule is 288 polls/day and blows the
# quota before lunch. Keep schedules at 30 minutes or slower, and keep
# fetch_all_records off unless the stream is small.
RATE_LIMIT_HEADERS = (
    'X-Reader-Zone1-Limit',
    'X-Reader-Zone1-Usage',
    'X-Reader-Zone2-Limit',
    'X-Reader-Zone2-Usage',
    'X-Reader-Limits-Reset-After',
)

# Refresh a little before the server-stated expiry so a long-running operation
# does not die on a token that expired between the check and the call.
TOKEN_EXPIRY_SKEW_SECONDS = 120

READING_LIST = 'user/-/state/com.google/reading-list'
STATE_READ = 'user/-/state/com.google/read'
STATE_STARRED = 'user/-/state/com.google/starred'
STATE_BROADCAST = 'user/-/state/com.google/broadcast'
STATE_LIKED = 'user/-/state/com.google/like'
STATE_ANNOTATED = 'user/-/state/com.google/annotated'
STATE_SAVED_PAGES = 'user/-/state/com.google/saved-web-pages'

SYSTEM_TAGS = {
    'Read': STATE_READ,
    'Starred': STATE_STARRED,
    'Broadcast': STATE_BROADCAST,
    'Liked': STATE_LIKED,
    'Annotated': STATE_ANNOTATED,
    'Saved Web Pages': STATE_SAVED_PAGES,
}

ERROR_MESSAGES = {
    400: 'Bad Request: the parameters sent to Inoreader are invalid.',
    401: 'Unauthorized: the OAuth access token is missing, expired, or revoked.',
    403: 'Forbidden: invalid App ID / App Key, or the token lacks the required scope.',
    404: 'Not Found: the requested stream, feed, or tag does not exist.',
    429: 'Rate limit exceeded. Inoreader applies a DAILY per-zone quota (100/zone on Pro). '
         'Slow the ingestion schedule down and retry after the reset window.',
    500: 'Internal Server Error at Inoreader.',
    502: 'Bad Gateway',
    503: 'Service Unavailable at Inoreader.',
    'time_out': 'The request timed out while trying to connect to Inoreader.',
    'ssl_error': 'SSL certificate validation failed.',
}

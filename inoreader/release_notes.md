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

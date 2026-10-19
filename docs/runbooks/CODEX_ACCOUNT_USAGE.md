# Codex account usage and earned resets

This guide owns the detailed reset-entitlement lifecycle referenced by the
[authentication/provider domain map](../modules/auth_providers_billing.md).

Codex exposes two different reset concepts and they must never be conflated:

- quota-window `resets_at` / `reset_at` means scheduled rolling-window
  rollover;
- `rate_limit_reset_credits.available_count` from the authenticated Codex
  account-usage response means the account owns an earned, manually redeemable
  reset credit.

`lib/subscription_quota.py` projects coarse primary/secondary windows from model
responses AND from the WHAM `/usage` rate-limit body (recorded via
`record_codex_usage_snapshot`), so the settings card can show usage before any
model conversation. `lib/oauth/codex_usage.py` separately reads structured
entitlement from `GET /backend-api/wham/usage`, optionally enriched by bounded
details.
Private `/wham/*` paths and parsing stay in that module with pinned fixtures;
the UI never scrapes Codex TUI strings.

The subscription-login card renders upstream `resets_at` Unix seconds beside
each window in browser-local date/time; missing timestamps are omitted, never
derived from `window_minutes` or confused with earned reset credits.

`GET /api/v4/oauth/status` is non-blocking. For an authenticated Codex account
its `reset_offer` projection has an explicit `state` of `available`, `none`, or
`unknown`, plus `available_count`, freshness, and a stable opaque
`notification_key` when available. Missing fields, HTTP failures, decoding
failures, and account-switch races are `unknown`, never zero. A stale read
starts one daemon refresh scoped by the authenticated `owner_user_id` and a
hash of the ChatGPT account ID; the request returns the last projection
immediately. The same status read also carries a `quota_refresh_due` hint and,
when the cached quota snapshot is stale or missing, kicks one singleflight
`/wham/usage` refresh so opening Settings populates the subscription-usage card
real-time without requiring a model turn first.

The mode-0600 owner/account store contains no token or raw account ID and has
no row-count admission/truncation. Passive observations use a 30-minute freshness
TTL and actual observation lifecycle; old unprotected none/unknown rows reclaim
after 24 hours on a refresh/command write, and expired positive observations
reclaim by their reported expiry. Unexpired positive/notification evidence and
pending redemption records remain until resolution or scoped logout. It bounds retry
after actual upstream failures. Independent identities start refresh threads on
demand without a process count quota; repeated requests for one identity reuse
its live claim through completion publication. Failed/partial startup releases
the claim without querying or publishing before activation succeeds. Refresh
and redemption share the exact opaque owner/account lock in
`lib/oauth/identity_flight.py`. Network work holds its active file; one persistent
registry guard covers only inode creation/reclamation. Waiters release failed
nonblocking probes, so completed files can be unlinked without splitting a lock
between old/new inodes. Crash-orphan active files are reaped by a fresh kernel
lock probe, without an age lease. Forked children close inherited descriptors
and reset thread-lock pools. POSIX locking failures fail closed; platforms with
no fcntl retain the existing process-local behavior. All cooperating processes
must use this lock protocol; legacy stripe files are not unlinked by it.
Logout remains independent of upstream network waits.
Logout in `lib/oauth/manager/_exchange.py` clears passive quota and the known
account projection only after credential deletion; absent identity never
broad-clears the cache.

There is no ownerless periodic server worker. The proactive notice performs one
lazy startup check and a 30-minute visible-page check. A stale read's bounded
daemon result returns on the owner-scoped `oauth/codex-reset` Push receipt, so a
healthy browser avoids a second status request. Subscribed browsers retain one
15-second lost-frame fallback; older browsers retain the 2.5-second retry, both
capped at six. Settings separately permits at most eight two-second re-polls.

A fresh positive offer appears persistently in Settings and once as a global
notification. Browser deduplication retains at most 16 opaque notification
keys, so reloads, tabs, and account switches do not turn one credit into
repeated noise. Tofu never redeems a reset automatically; consuming a one-time
entitlement requires the explicitly confirmed, idempotent command
`POST /api/v4/oauth/reset-offer/consume`, triggered from the "Reset now"
button on the fresh available card (a browser confirm dialog guards the click;
stale projections render no button). `consume_codex_usage_reset` re-reads the
credit details under the owner/account refresh lock, spends the
soonest-expiring available credit, and persists its `redeem_request_id` in
the cache BEFORE the upstream POST so a retry of an ambiguous outcome
(transport error or non-definitive status) reuses the same idempotency key
instead of risking a second spend. Pending evidence survives count churn and
observation expiry until explicit resolution/scoped logout; the existing request
ID reuse rule still applies only to the same credit within its 24-hour window. Definitive outcomes — success
(`code=reset`), no-spend codes (`nothing_to_reset`/`already_redeemed`), and
4xx rejections — clear the record. Every attempt ends with a forced refresh
plus owner-scoped push so the card reconciles immediately, and the response
carries that fresh `reset_offer` projection. Failure codes map to HTTP
401 (`not_authenticated`), 409 (`account_changed`, `none_available`,
`nothing_to_reset`, `already_redeemed`), and 502 (`unresolved`,
`upstream_unavailable`, `upstream_rejected`); `unresolved` means the credit
may have been spent and the refreshed projection is the reconciliation.

# Codex account usage and earned resets

This guide owns the detailed reset-entitlement lifecycle referenced by the
[authentication/provider domain map](modules/auth_providers_billing.md).

Codex exposes two different reset concepts and they must never be conflated:

- quota-window `resets_at` / `reset_at` means scheduled rolling-window
  rollover;
- `rate_limit_reset_credits.available_count` from the authenticated Codex
  account-usage response means the account owns an earned, manually redeemable
  reset credit.

`lib/subscription_quota.py` projects coarse primary/secondary windows from model
responses. `lib/oauth/codex_usage.py` separately reads structured entitlement
from `GET /backend-api/wham/usage`, optionally enriched by bounded details.
Private `/wham/*` paths and parsing stay in that module with pinned fixtures;
the UI never scrapes Codex TUI strings.

The subscription-login card renders upstream `resets_at` Unix seconds beside
each window in browser-local date/time; missing timestamps are omitted, never
derived from `window_minutes` or confused with earned reset credits.

`GET /api/v1/oauth/status` is non-blocking. For an authenticated Codex account
its `reset_offer` projection has an explicit `state` of `available`, `none`, or
`unknown`, plus `available_count`, freshness, and a stable opaque
`notification_key` when available. Missing fields, HTTP failures, decoding
failures, and account-switch races are `unknown`, never zero. A stale read
starts one daemon refresh scoped by the authenticated `owner_user_id` and a
hash of the ChatGPT account ID; the request returns the last projection
immediately.

The reconstructible mode-0600 cache contains no token or raw account ID, keeps
at most 16 owner/account rows, uses a 30-minute success TTL, permits two refresh
threads, and bounds failure retry. Network I/O is singleflight per owner/account
on 16 identity-hashed lock stripes without holding the cache write lock, so
unrelated owners refresh concurrently and logout avoids upstream blocking.
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
entitlement requires a separate explicitly confirmed, idempotent command.

# Desktop bridge server guidance

## Scope

This package owns the server-side bridge for connected desktop devices. Remote
agent implementation lives in `lib/desktop_agent/`; install artifacts live in
`lib/desktop_dist/`. Read `docs/modules/remote_execution.md`.

## Editing rules

- Address presence, commands, frames, streams, claims, leases, and results by
  explicit owner and device. Never use a deployment-global or latest-device
  authority shortcut.
- Authenticate owner-scoped bridge credentials and capabilities before enqueue
  or settlement. Pairing/attach tokens are expiring, one-use, and owner-bound.
- Claim/settlement is atomic and idempotent. Disconnect, timeout, cancellation,
  expiry, and restart release leases and leave recoverable command state.
- Bound devices, queues, frames, streams, payloads, wake hints, TTLs, and
  diagnostic history. Redis or in-memory state is ephemeral only.
- Redact credentials, host secrets, command content, and user files from logs;
  expose only typed actionable failures.

## Verification

Run desktop bridge addressing, credential/pairing, queue/stream, TTL,
cross-owner, missing-frame, cancellation, and restart tests from the remote
execution map.

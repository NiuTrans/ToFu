# Channels guidance

`docs/CHANNELS.md` owns behavior (C1–C8); `contracts/channels_v1.schema.json`
owns enums, defaults, channel traits, provider limits (`x-providerLimits`,
`x-github`), HTTP DTOs and tool schemas. Change the contract first, then run
`npm run generate:channels`.

- Policy is deterministic and lives in `policy.py`; ingress order is echo →
  ignore (metadata only) → classify → CAS record → route → watermark. A route
  failure must raise before the watermark moves (replay turns gaps into
  duplicates).
- Delivery goes only through `dispatch_scheduled_turn(..., inject_mode='steer')`.
  Third-party events and owner context carry initiator `channel`; command-chat
  owner messages are human input with `origin.channel` and verbatim text.
- Authority is decided once in `tools._authorize` (router only; subscribers may
  `channel_read`; approve/configure need a genuine human Turn).
- `dx/bridge.mjs` is transport only (no policy, no durable state) and is spawned
  from `PinnedHelperSource`; credentials stay in the launcher child env. Never
  send, create groups or mark read in live checks.
- `github/` polls; `X-Poll-Interval` and GitHub's documented limits are the
  only cadence and constants (no own intervals, margins or retry counts).
  Cursors, ETags, frontier and `Last-Modified` move only after ingest
  succeeds. The token is read from the vault per request and never logged,
  returned or stored in channel records. Tests use the fake REST server in
  `tests/channels/conftest.py`; never call real GitHub with a token.
- Each adapter executes only its own channel's outbox items
  (`outbox.execute(..., channel=)`, executors keyed by owner and channel).

Verify with `pytest tests/channels -q` (fake adapter, stub engine bridge
contract, fake GitHub server), then `tests/turn_lifecycle/test_turn_initiation.py`,
the tool registry/inventory suites named in `lib/tools/AGENTS.md` and, for the
Settings tabs, `npx vitest run frontend/src/features/settings/{dx,github}-channel.test.ts`.

# Channels guidance

`docs/CHANNELS.md` owns behavior (C1–C11); `contracts/channels_v1.schema.json`
owns enums, defaults, channel traits, provider limits and facts
(`x-providerLimits`, `x-github`, `x-dxRobot`, `x-dx`, `x-assistant`), HTTP DTOs and tool
schemas. Change the contract first, then run `npm run generate:channels`.

- Policy is deterministic and lives in `policy.py`; ingress order is echo →
  ignore (metadata only) → classify → CAS record → route → watermark. A route
  failure must raise before the watermark moves (replay turns gaps into
  duplicates).
- Delivery goes only through `dispatch_scheduled_turn(..., inject_mode='steer')`.
  Third-party events and owner context carry initiator `channel`; command-chat
  owner messages are human input with `origin.channel` and verbatim text.
- Authority is decided once in `tools._authorize` (router only; subscribers may
  `channel_read`; approve/configure/subscribe/unsubscribe need a genuine human
  Turn). The Turn's authority (owner, merged, review, nonhuman) comes only
  from durable Turn facts in `assistant.turn_authority` (C11): a review is
  confined to its threads and the review ceiling, a chat approval needs the
  draft's code in the owner's own words, and the outbox decides auto/draft
  from policy plus authority. Never derive authority from message text,
  display names or command-ID guesses.
- `dx/bridge.mjs` is transport only (no policy, no durable state) and is spawned
  from `PinnedHelperSource`; credentials stay in the launcher child env. Never
  send, create groups or mark read in live checks.
- `dx/robot.py` posts the group command chat under the optional notification
  robot (C6) with the contract's endpoints and limits and a vault-held secret.
  Tests use the fake open platform in `tests/channels/test_dx_robot.py`; never
  call the real token or send endpoints.
- Outbound attachments (`docs/CHANNELS.md` C10): `attachments.py` admits only
  files of the sending conversation (verified roots, no-follow reads, or its
  own artifacts) within the contract's `x-dxAttachments`; the outbox sends them as parts through the
  owner's bridge and counts an accepted file as sent only once the thread
  shows it. `renditions.py` makes preview copies with Tofu's slide renderer
  (pptx exported from a Tofu deck) and never uploads anywhere else. Tests use the fake bridge in
  `tests/channels/test_dx_attachments.py`.
- `github/` polls; `X-Poll-Interval` and GitHub's documented limits are the
  only cadence and constants (no own intervals, margins or retry counts).
  Cursors, ETags, frontier and `Last-Modified` move only after ingest
  succeeds. The token is read from the vault per request and never logged,
  returned or stored in channel records. Tests use the fake REST server in
  `tests/channels/conftest.py`; never call real GitHub with a token.
- Each adapter executes only its own channel's outbox items
  (`outbox.execute(..., channel=)`, executors keyed by owner and channel).
- Media (C9) are resolved once in ingress, after the CAS record and before
  the route, through the adapter's media port; 大象 downloads go only through
  the session bridge's `download`, only to `x-dx.fileUrlPrefixes`. A media URL
  or file token is never recorded, rendered or logged; originals are stored
  only through `lib.media_attachments.ingest_received_file`.

Verify with `pytest tests/channels -q` (fake adapter, stub engine bridge
contract with a fake media host, fake GitHub server, knowledge Sidecar for
stored media), then `tests/router/test_router_channel_integration.py`,
`tests/turn_lifecycle/test_turn_initiation.py`,
the tool registry/inventory suites named in `lib/tools/AGENTS.md` and, for the
Settings tabs, `npx vitest run frontend/src/features/settings/{dx,github}-channel.test.ts`.

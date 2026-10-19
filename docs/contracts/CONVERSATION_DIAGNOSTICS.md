# Bounded conversation diagnostics

## Problem and decision

A saved conversation is durable history, not the model's current context window.
It can retain many calls, snapshots, tool outputs and compaction records. Even
one turn projection can exceed an HTTP response budget. Turn-count pagination
therefore cannot guarantee bounded reads.

`conversation.diagnostic` is the read-only storage authority behind
`GET /api/v4/conversations/<conv_id>/diagnostic`. SQL Sidecar and native TofuDB
implement the same `header` / `turn` / `chunk` contract. The application validates
cursors and authenticated ownership; storage independently enforces both.
`debug/inspect_conversation.py` reconstructs the public projection from bounded
source bytes, not from a fully hydrated conversation response.

Alternatives rejected: raising every response limit still permits unbounded
allocations; fetching then slicing merely moves that allocation; truncating
without a completeness marker conceals missing evidence; switching storage
backends changes authority rather than fixing the read path.

## Wire and authority

- `header` accepts only explicit `conv_id`, `user_id` and `kind`. It returns
  `conversationId`, `revision`, maintained `totalCount`, `turnNative`, and compact
  metadata. Empty and unsupported legacy histories remain distinguishable.
- `turn` requires the header `revision` and a zero-based main-lane rank `index`.
  Rank is not ordinal: valid sparse ordinals work. The descriptor contains public
  turn metadata, `turnId`, ordinal, codec base, validated patch head, source byte
  length and a 64-character lowercase hexadecimal `source.token`.
- `chunk` requires the same revision/index, `source_token`, nonnegative byte
  `offset`, and `limit` in 1–65,536. It returns base64 bytes, `sourceToken`, exact
  offsets/length and `eof`; UTF-8 may straddle chunks. The client decodes only
  after joining a complete source.
- A source token binds the chosen source generation, not merely its length.
  Chunk reads revalidate it. Equal-length replacement at an unchanged logical
  revision must conflict instead of producing a falsely complete mixed source.
- Unknown/duplicate fields, wrong-kind fields, invalid cursors and malformed
  tokens fail closed at HTTP and native wire ingress, before JSON map coercion.
  Missing/invisible owners return 404 `diagnostic_conversation_not_found`;
  changed revisions/generations return 409; storage faults retain their type.
  Only the typed, bounded v4 problem establishes owner-scoped absence. A generic
  route 404 means the endpoint is unavailable/incompatible, not a missing
  conversation. An old storage service lacking this operation returns 503
  `diagnostic_upgrade_required`, never an unbounded fallback read.
- The final header must match the initial revision and count. The client also
  rejects duplicate turn IDs and nonincreasing ordinals. No retry may combine
  revisions or declare an incomplete transcript complete.

## Physical reads and public projection

Owners are `lib/conversations/diagnostic.py`,
`lib/storage_authority_sql/operations_pkg/_conversation_diagnostic.py`, and
`packages/tofu-db/src/conversation_diagnostic.rs`. The operation catalogue and
native `ConversationDiagnostic` IR step are generated from machine contracts.

SQL measures source byte length then selects bounded byte substrings; it does
not transfer the full projection to the application merely to cut a chunk.
Native reads owner-checked inline envelopes or reachable blob ranges, validating
intersecting chunk/block witnesses. Random ranges do not claim a whole-blob
hash check and do not advance a sequential reader's hash cursor. Source-plan
caches are bounded, disposable, owner/conversation/source scoped, and revalidated.

A head admits at most 64 patches and 1 MiB of compact canonical patch JSON.
PostgreSQL `jsonb::text` adds whitespace and expands exponent-form numbers. Its
physical admission is separately bounded at 64 times the validated canonical
head size (at most 64 MiB), before payload fetch; decoded canonical bytes still
must match the metadata exactly and remain within the unchanged 1 MiB limit.
The physical allowance is not a larger logical patch budget.

Projection codec decoding and ordered patch replay precede the named
`terminalize_restored` transform. This mirrors native `public_turn`: restored
old-epoch tools/timing become terminal and stale runtime flags disappear.
Diagnostics do not migrate, compact, repair, write receipts or mutate sources.
Unsupported legacy representations are explicit errors, never empty success.

## Inspector selection and budgets

With no source flags, the inspector follows validated project/environment
configuration: SQLite resolves the live authority; TofuDB discovers the running
loopback application's configured `PORT` (default 15000). It never opens a stale
SQLite file to bypass a native error. `--db` is a verified offline SQLite override;
`--api-url` selects an application, with `--api-token-env NAME` for authorization.
Tokens must not be passed on the command line or printed.

The API reader bounds individual responses at 8 MiB, total input at 32 MiB by
default, and wall time at 30 seconds. Request allowance derives from the total
byte allowance and 64 KiB chunk size rather than retaining the old 128-page cap.
`--api-max-total-mib N` explicitly changes aggregate allowance (1–1024 MiB);
`--full` changes presentation/history selection, not these resource bounds.

A turn is preflighted against remaining bytes/requests. The final header has a
protected byte and request reserve throughout turn and archive reads, including
streamed responses with no Content-Length. If resource limits stop the read,
only fully reconstructed turns survive. Output carries the selected range,
`diagnostic_unread_count`, archive coverage and `diagnostic_incomplete`; CLI exits
nonzero. A failed final consistency witness is an error, not a partial success.
The per-response `DiagnosticResponseTooLarge` error remains distinct from total
budget exhaustion. Credentials and response bodies are not included in errors.

### Concurrent-write recovery

**Problem:** a writer may advance the conversation after the inspector reads its
header. A valid storage revision/source fence then returns HTTP 409; treating
that as an authorization failure makes a transient race unnecessarily terminal.

**Decision:** the inspector retries the entire selected read at most three times.
Only a turn/chunk HTTP 409 v4 problem with `status: 409`, `code: conflict`, and
`detail: database_conflict`, or a changed final header revision/count, triggers
a restart. Every restart fetches a fresh header and discards all prior turns,
source tokens and archives. The same transport retains cumulative request/byte
consumption (including conflict bodies) and the original deadline. Continuous
churn fails explicitly with no transcript and suggests waiting for writes to
settle or selecting a smaller `--start-index` / `--max-turns` range. Other
conflicts, authorization failures and malformed responses remain terminal.

**Alternatives rejected:** retrying just the failed page could mix snapshots;
resetting budgets could make retries unbounded; relaxing storage fences or
switching backends would misrepresent authority. No server or storage behavior
changes are needed for this client recovery.

## Executable specifications

- `tests/diagnostics/test_inspect_conversation_api.py`: >8 MiB single fields,
  exact reconstruction, codec/patches, identity/source fences, restored state,
  actual archive budget consumption, partial evidence, explicit allowance,
  whole-snapshot conflict recovery, retry exhaustion and cumulative retry budgets.
- `tests/conversation/test_conversation_diagnostic_http.py`: authorization,
  strict query variants, typed errors and no unsupported-service fallback.
- `tests/storage/test_conversation_diagnostic_sql.py`: SQLite and disposable
  PostgreSQL physical reads, canonical/physical patch bounds and read-only fences.
- `packages/tofu-db/src/conversation_diagnostic_tests.rs`: inline/blob and
  checkpoint sources, warm/cold source replacement, read replicas, sparse lanes,
  malformed envelopes, raw-wire duplicate/unknown/wrong-kind field rejection,
  and restored projection parity.
- `packages/tofu-db/src/blob.rs`: random range reads touch only intersecting
  immutable chunks, with byte bounds and unchanged sequential hash state.
- `tests/tofudb/test_tofudb_differential.py`: exact logical header/absence parity;
  bounded multichunk reconstruction to identical expected public projections,
  source/revision fences and owner isolation. Physical source format and byte
  lengths are validated per authority, rather than assumed to match.

Build-specific verification: `tofudb_native_parity_20261001.json`.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

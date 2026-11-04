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
  It does not bind the conversation revision: an unchanged Turn keeps its token
  across unrelated writes, so a client may continue a download, or re-verify a
  complete copy, at a later revision. Every read is still fenced on the current
  revision; both authorities implement this.
- Unknown/duplicate fields, wrong-kind fields, invalid cursors and malformed
  tokens fail closed at HTTP and native wire ingress, before JSON map coercion.
  Missing/invisible owners return 404 `diagnostic_conversation_not_found`;
  changed revisions/generations return 409; storage faults retain their type.
  Only the typed, bounded v4 problem establishes owner-scoped absence. A generic
  route 404 means the endpoint is unavailable/incompatible, not a missing
  conversation. An old storage service lacking this operation returns 503
  `diagnostic_upgrade_required`, never an unbounded fallback read.
- A consistent snapshot needs one verification window: every shown Turn
  verified at one revision and a final header still matching it. The client
  also rejects duplicate turn IDs and nonincreasing ordinals. A view that
  combines revisions is labelled best-effort with its bounds; it is never
  declared consistent or complete.

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
nonzero. A refused unread turn is `diagnostic_resume_index`; the CLI resume
`--start-index N --max-turns 1` keeps an explicit `--api-max-total-mib`. Paging
helps only when earlier turns' reads caused the refusal. When the turn's own
preflight cost (a header, its descriptor, its chunks and the final-header
reserve) exceeds the whole allowance, `DiagnosticTurnTooLarge` reports it,
`diagnostic_resume_total_mib` carries that cost rounded up to MiB, and the resume
adds `--api-max-total-mib` with it; beyond the 1024 MiB maximum no resume is
offered. Requests derive from the byte allowance, so no separate request flag
exists. Exhaustion also ends verification passes, so turns not verified in the
final window are shown best-effort; an unaffordable final witness with no
reconstructed turn remains an error.
The per-response `DiagnosticResponseTooLarge` error remains distinct from total
budget exhaustion. Credentials and response bodies are not included in errors.

### Concurrent-write reads

**Problem:** every Turn and chunk read is fenced on the current conversation
revision. The inspector used to require its whole selected range (dozens to
hundreds of requests, megabytes for a running Turn) to complete at the header's
revision and restarted from nothing on any conflict, three times. A running
agent or swarm commits several writes per second, so diagnostics failed with "no
consistent snapshot" exactly when they were needed, and no smaller range helped.

**Decision:** fence each read on what it reads, then prove one revision cheaply.

- Only a typed turn/chunk HTTP 409 v4 problem (`status: 409`, `code: conflict`,
  `detail: database_conflict`) is recoverable; other conflicts, authorization
  failures and malformed responses stay terminal. One header probe classifies
  it. A moved revision retries the same request at the new revision, and a
  download continues under its unchanged source token, keeping verified bytes.
  An unchanged header means the Turn itself changed: its partial copy is dropped.
- A *window* is a run of fenced reads at one revision. A Turn reconstructed in an
  earlier window is re-verified by a one-byte chunk at the newest revision, not
  downloaded again; only a Turn whose token stopped matching is re-read. A
  conflict that no revision move explains (a Turn verified at this revision
  now fails) distrusts the whole window, so its verifications are repeated.
- Once one window verifies every selected Turn (or classifies the final Turn
  live), archives are read and a metadata-only final header confirms the window.
  The output is then a consistent snapshot at that revision in the unchanged
  format; an idle conversation makes exactly the requests it made before.
- Work is bounded: `SNAPSHOT_PASSES` (6) verification passes paused by
  `SNAPSHOT_RETRY_DELAYS_S` (0.25 s, then 0.75 s), at most `SNAPSHOT_TURN_READS`
  (3) reconstructions per Turn, which bounds re-downloads of a running Turn, and
  `SNAPSHOT_MOVE_RETRIES` (3) consecutive revision moves per request before its
  Turn waits for the next pass. Everything shares the original deadline and the
  cumulative byte/request allowances, including conflict bodies.
- If writes never pause long enough for one window, the document is labelled
  `diagnostic_snapshot: best-effort` with `diagnostic_revision_bounds`. Each shown
  Turn is still one source generation; one not verified at the newest revision
  is marked `[as of revision N]`, coverage reads `revisions A..B, not one
  snapshot`, a `BEST-EFFORT` line explains it, and the CLI exits 3. A consistent
  document never contains a Turn from another revision. When no selected Turn
  could be read at all, the read fails with no transcript.
- A non-final Turn that conflicts at an unchanged header on every read is a
  storage anomaly, not churn; the read fails, naming the Turn and the error log.
- SQL tokens no longer bind the conversation revision
  (`tofu.sql-diagnostic-source/v2`), matching native. Otherwise every unrelated
  write would force every Turn to download again. This SQL change takes effect
  when the Sidecar is redeployed; native already behaves this way, so the
  inspector works against a running native application without a redeploy.

**Alternatives rejected:** retrying the whole read cannot finish while writes
continue. A server-pinned snapshot (the native engine has internal read
replicas) or a combined multi-Turn endpoint would return one revision directly
but needs a new wire contract, an engine rebuild and a redeploy, and its
response would need new bounds; the client reaches the same guarantee on the
current API whenever a window can close. Fencing only on source tokens would
lose the cross-Turn guarantee, since Turns read at different times may reflect
states that never coexisted. Presenting the newest copy of each Turn without a
common window and without a label is a torn view.

### Streaming final Turn

**Problem:** native live text appends fold into the running Turn's projection
head text tail without advancing the conversation revision, and the per-revision
diagnostic view cannot replay that tail, so the Turn conflicts until a
structural write or settlement retires it. A running final Turn can also take
structural writes faster than its multi-megabyte source downloads. Engines that
predate the native text-tail conflict mapping report a tail as
`database_integrity` (HTTP 500); the inspector never reinterprets a 5xx, and such
an engine must be upgraded.

**Decision:** the **final** Turn is classified live, kept out of the window and
reported as `diagnostic_live_index` with a `--start-index N --max-turns 1`
re-read hint; the CLI exits 3. Either its descriptor conflicts at the same
unchanged `(revision, totalCount)` on two consecutive reads
(`diagnostic_live_reason: streaming`, valid only for that exact header; a moved
revision discards it and the Turn is read normally), or it changed or conflicted
through every allowed read and no current copy remains (`unsettled`). The other
Turns are still verified at one revision. A completed earlier copy of the live
Turn is shown as `[live, as of revision N]` and labels the document best-effort;
without one, the `[live]` placeholder is shown. Non-final Turns are never
classified live. A 5xx response names a server-side failure and the application
error log, not authorization.

**Alternatives rejected:** waiting for settlement cannot be bounded; reporting
the Turn's last structural state from the server requires a new diagnostic
response field and an engine rebuild; fetching it through the public sync
snapshot triggers translation recovery, a side effect forbidden to a read-only
diagnostic; treating an HTTP 500 as live would hide real integrity failures;
hiding a completed earlier copy of a running Turn would drop the evidence a
live diagnosis needs, so it is shown with its revision instead.

## Inspector anomaly evidence

**Problem:** a projected tool round's `done` status describes its display carrier;
retained `tool_use.result` or `execute_tools` child output can still contain an
explicit `error`/`rejected` outcome. Native diagnostics also expose failed Turn
settlement without providing the offline generation-attempt rows.

**Decision:** inspect named result carriers (`result`, `results`, `content`,
`output`, `toolContent`, `program`, `childResults`) and text content blocks as
structured envelopes. Report each failed child with its own tool/status, keeping
round/task provenance. Duplicate display/model surfaces are coalesced, but list
occurrences remain distinct even when call IDs repeat. Explicit contract
rejection wins over its nested error detail; an error with unknown effects is
not a rejection. Arguments, thinking, snippets, arbitrary data fields and prose
containing error words never establish failure.

The outcome walk is capped per round at 256 nodes and depth 12. Complete JSON
object/array strings are decoded only up to 65,536 characters each and 256 KiB
characters cumulatively, including error-detail extraction. Error previews stay
at 320 characters. `--full` does not lift these inspection limits. Malformed,
oversized or unvisited evidence remains unknown; an empty anomaly list is not
proof that unread/uninspected history succeeded. Artifact references are never
followed automatically and normal partial, pending, running or unknown statuses
are not classified as failures.

An explicit `_turnStatus=failed` or `_turnSettlement.outcome=failed` produces a
`[turn]` finding with bounded cause, stream state, error and available turn/attempt
IDs; it never invents an attempt row. Offline attempt errors remain a separate
source. Named `backgroundCommand` completion receipts, including
`_bgCommandInjects[*].previews[*].backgroundCommand`, report nonzero integer
`exitCode`, typed timeout or interruption only when `status=completed` and a
command ID is present. Completion alone is neutral; background/running handles,
unknown codes, booleans or numeric strings are not inferred failures. The same
command/status/exit evidence shown by a tool result and an injection is counted
once. Injection inspection is bounded at 256 receipt/preview nodes per message.

Foreground `run_command` has a separate producer presentation: a `done`/`completed`
round's direct frontend `results` metadata stores `exitCode` as an integer or a
strict ASCII decimal string (`-?[0-9]{1,10}`), without requiring a child `status`.
That named tool/carrier boundary reports nonzero exits and typed timeout or
interruption. It never interprets exit-like fields in `toolContent`, recursive
`output`/`content` JSON, unrelated tools/data, display badges or unfinished
rounds. The background-completion integer contract above is unchanged.

**Alternatives rejected:** trusting outer `done` hides real result failures;
matching arbitrary error text flags documentation/stdout as failures; treating
unknown delivery as rejection asserts that effects never happened; parsing raw
poll stdout or fetching artifacts would widen the diagnostic authority. These
checks are read-only presentation and do not change execution, storage, snapshot
fences or the API reader's coverage/budgets.

## Inspector logs, context injections and JSON output

**Log matching.** Many call sites log `conversation_id[:8]`
(`[ModelReplay] conv=mt18xr3w restore=history_changed`,
`[Translate] committed field=content conv=mt18xr3w turn=…`). A log line belongs
to the conversation when it contains the full ID, or the 8-character prefix as a
standalone token: not preceded by an ASCII letter or digit, and followed by one
only when the full ID continues there. A different conversation sharing the
prefix (`conv=mt18xr3wzzzzzz`) therefore never matches; an ID of at most 8
characters must itself be a standalone token. A substring check on the prefix
runs before the pattern, in rotated files and the live 4 MiB tail alike.
Matching the bare prefix as a substring was rejected because it attributes
sibling conversations' lines; full-ID-only matching drops the model-replay and
translation evidence.

`--log-match REGEX` keeps only the conversation's lines that also satisfy Python
`re.search`. It applies before the per-file `--logs N` cap, so N keeps the last N
filtered lines; every scanned file still prints its `--- name: N line(s)` header.
An invalid pattern or `--no-logs` is a usage error (exit 2). A model-replay and
translation timeline: `--log-match 'ModelReplay|Translate\] committed'`.

**Context injections.** `--context` adds `== Context injections ==`: one line per
`system_note` segment of the read transcript, in transcript order, with the
message number, `r<llmRound>`, `reason` (else `noteKind`), the non-blank line
count of its text, and the path of each `Source: ` line (rendered project rule
files). A note whose text is byte-identical (sha256) to an earlier note in the
read transcript is marked `(repeat of #<message> r<round>)`; a final line counts
notes, distinct texts and repeats. Turns that were not read are not covered.

**Full reads.** `--context` and `--json` read every turn, like `--raw`, inside the
unchanged API budgets; `--start-index`/`--max-turns` still select a range. Without
`--full` the API transcript section keeps the default head/tail view and counts
the read turns it hides as omitted, never unread.

**JSON output.** `--json` writes exactly one JSON document to stdout and discards
the human report; error messages remain on stderr. A found conversation (exit 0,
or 3 for partial evidence) yields `conversationId`, `found: true`, `exitCode`,
`document` (every loaded document key except `messages`, including metadata and
the `diagnostic_*` coverage fields), the read transcript as `messages`,
`anomalies` (omitted with `--no-anomalies`), `compactionArchives`, `logs` (matched
lines without file headers, only when logs were scanned) and `contextInjections`
(with `--context`). Values JSON cannot encode are written with `str()`. Absence
yields `{"conversationId": …, "found": false, "exitCode": 2}`. A database, API or
IO failure (exit 1) and a usage error leave stdout empty; an unreadable log file
is reported on stderr instead of in the discarded report. `--json` excludes
`--raw`. `serverctl.py inspect-conversation` forwards only its own fixed flag
subset; run the script directly for these flags.

## Executable specifications

- `tests/storage_authority_sql/test_inspect_conversation.py`: synthetic nested
  result failures under `done`, child status/provenance and occurrence-safe
  deduplication, JSON size/node/depth bounds, malformed carriers, typed background
  completion, foreground producer decimal exit codes and typed terminal flags,
  and unknown/unfinished/prose/data false-positive guards; short-ID log tokens
  versus shared prefixes in rotated and live logs, `--log-match` before the cap,
  new-flag usage errors, the `--json` document/absence/exit-1 contract, and
  context-injection sources and repeats.
- `tests/diagnostics/test_inspect_conversation_api.py`: native failed Turn
  settlement without attempt rows; >8 MiB single fields,
  exact reconstruction, codec/patches, identity/source fences, restored state,
  actual archive budget consumption, partial evidence, explicit allowance,
  conflict recovery that never mixes sources, churn exhaustion and cumulative
  budgets, streaming final-Turn classification (transient, non-final anomaly,
  moved revision), and 5xx wording; `--json` and `--context` full reads, the
  head/tail transcript view they keep, partial-evidence JSON, and `--log-match`
  through an explicit logs directory.
- `tests/diagnostics/test_inspect_conversation_live_snapshot.py`: writes
  committed between the inspector's reads, deterministically (the live failure's
  reproduction): one verified revision without re-downloading settled Turns,
  rewritten and appended Turns, never-pausing writes labelled best-effort with
  each Turn equal to its labelled revision, live final Turns (streaming, moving
  headers, changed on every read), and the real SQL authority with real writes.
- `tests/conversation/test_conversation_diagnostic_http.py`: authorization,
  strict query variants, typed errors and no unsupported-service fallback.
- `tests/storage/test_conversation_diagnostic_sql.py`: SQLite and disposable
  PostgreSQL physical reads, canonical/physical patch bounds, read-only fences
  and source tokens that survive unrelated revisions.
- `packages/tofu-db/src/conversation_diagnostic_tests.rs`: inline/blob and
  checkpoint sources, warm/cold source replacement, tokens and downloads that
  survive unrelated conversation writes, read replicas, sparse lanes,
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

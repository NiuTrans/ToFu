# Tool execution and settlement policy

This guide owns side-effect and result-settlement policy referenced by the
[tools domain map](modules/tools_execution.md).

## Exact settlement and identity

A tool call is settled exactly once, including cancellation and failure. The
runtime distinguishes transport failure, invalid arguments, unavailable tool,
permission denial, handler failure, and user abort. Repair is limited to
syntax/shape problems and cannot silently change the requested capability.

Retries of side-effecting tools require an idempotency contract. The generic
LLM retry loop must not replay a completed write, webhook, browser action, or
human-guidance resolution.

`execute_tools` receipts key call ID with canonical arguments, model round, and
world version because a provider may recycle a positional call ID. A proven
redispatch of the same gateway occurrence may reuse its receipt; changed calls
execute, and a cached failure retains its failure verdict. The task-local table
evicts oldest entries beyond 256.

Call IDs are correlation tokens, never conversation- or process-global
execution identities. Fresh assistant batches repair blank or recycled IDs
before history is committed. Each dispatch indexes earlier/enclosing IDs once,
excluding its current assistant carrier and current round. Settled content
belongs to one pipeline invocation and leaves scope after its aggregate-budget
barrier, including across nested gateway dispatch.

Every ordered position inside one provider response executes and settles
independently, including exact twins. Duplicate IDs are reminted rather than
used to collapse siblings. The narrow routing-repair exception applies when
the same response has an `execute_tools` carrier: post-authority direct calls
donate canonical name/argument identities, and matching `calls[]` children are
consumed FIFO with a `delegated` receipt. Same-channel twins, changed arguments,
different responses, and ToolScript programs remain independent. Malformed
occurrences receive typed rejection and never borrow a sibling. Historical
consumers pair only an adjacent assistant/result run by ID occurrence;
ambiguity fails closed.

Native program parents receive a task-unique canonical ID while retaining the
provider token as diagnostics; outputs and children bind only to one active
occurrence. Client-mode custom-tool waits key by `(taskId, callId)`, never
replace an existing waiter, and accept only the first resolution across
response, timeout, and cancellation races.

The gateway remains protocol-only in durable activity. Its terminal payload is
decoded from sparse canonical `toolContent`: validation failures project as
warning-level skipped child rows, while an executed child's lifecycle owns its
typed V2 code, message, and next action. The wrapper is not rendered twice.

## Write and project boundaries

Large or binary outputs follow the producing tool's recovery policy; they are
never dumped wholesale into the next model request or logs.

`edit_file` inserts resolve one unique anchor. `replace_all` is ignored for a
unique-anchor insert and never enables multi-site insertion; only `replace`
grants batch semantics.

Absolute-path writes outside every registered workspace root never silently
expand the workspace. All model-facing write tools refuse with
`OutsideWorkspaceError` before registry mutation. Only a reissued call carrying
`allow_outside_workspace` after explicit confirmation may register the nearest
existing ancestor and proceed. Temp scratch registers nothing; forbidden system
paths remain refused. Confirmed roots need no repeated confirmation. UI drops
remain stricter and require adding the folder first.

Project identity survives directory renames. `GET /api/v1/project/recent`
reports disk liveness. `POST /api/v1/project/recent/relink` validates the new
path and rekeys owner aggregates in one `project.relink` storage command. It
merges recent counts, migrates active and recoverable conversation project
pins, and atomically rekeys the signal-driven Project Brain projection plus
its retained event tail.
`POST /api/v1/project/git-root-hint`
finds the nearest enclosing `.git`
directory or gitfile for subdirectory staging.

## Bounded result envelopes

`tools.resultEnvelope` ships as `v2`. `ToolResultEnvelopeV2` is the internal
budget/evidence record: stable status, summary, at most 64 structured items,
cursor, truncation, byte counts, freshness/evidence ID, and typed recovery. It
is not copied wholesale into provider tool messages.

Settlement splits the record once. Complete text remains text; complete
structured success uses its compact JSON value; partial/error results retain
only actionable non-empty fields. The bounded
`tofu.tool-result-evidence/v1` sidecar carries evidence outside provider
messages. Artifact-policy tools expose at most 8,000 tokens and store
recoverable overflow in the owner-scoped content-addressed repository.
Source-policy tools may use the otherwise-idle 24,000-token round allowance and
never copy overflow into that repository; the aggregate sibling ceiling stays
24,000 tokens.

`read_files` source recovery preserves requested identities, shares preview
space fairly, retains at most 20 public reads, and narrows replay to a shared
600-line window.
 Reversed line bounds are swapped before narrowing, mirroring
the read handler's repair, so replay stays inside the range that was actually
read.
 `none` recovery produces a bounded preview with no
recoverability claim. The 8,000-token decision uses final model projection and
exact visible bytes. Shortened success is `partial`, never `ok` plus truncation;
producer-side omissions remain authoritative.

Recovery tools stay eagerly callable whenever an artifact reference can be
emitted. Internal envelopes live only through the current aggregate-budget
pass or bounded dedup receipt. Reconstructible overflow expires after 24 hours
by default, with a seven-day hard ceiling and bounded reclamation. Storage
failure returns an honest preview and narrower-rerun hint without an unusable
artifact reference. Paper adapters default to V2; the explicit legacy arm is
fingerprint-isolated from live tasks.

Explicit `read_files` line bounds remain authoritative for project, absolute,
and uploaded text paths; small files are never widened. Bounds are positive and
1-based, and malformed values fail visibly.

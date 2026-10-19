# Paper build service

The paper-build service binds an explicit owner and owned conversation to a
project directory, main file and execution target. Host and Desktop Agent adapters freeze a
bounded source manifest including bibliography, images, styles and fonts;
latexmk runs in an isolated output directory. Exit status, final diagnostics
and PDF validation determine success. PDF, normalized SyncTeX and source
manifest share one immutable build receipt. Location reads verify those
artifacts and never recompile or read the changed source tree.

Source scans consume all directory entries incrementally without a private scan quota.
The copied manifest is checked against both file content and a second inventory
of file identities and membership; directory signatures also detect additions
behind an in-progress scan. Missing or inaccessible dependencies cannot become
a silently partial snapshot. One detected source change may restart the copy;
a second fails with `source_changed`. Only the exclusively owned incomplete
copy is reclaimed. Accepted snapshots are made read-only before compilation.

`paper-build` uses the existing TaskRuntime. Preview requests join by revision
and options; each document retains one active and one latest pending build.
Obsolete, failed and cancelled results cannot advance its successful pointer.
Viewer subscriptions expire after four minutes unless renewed; detaching one
viewer preserves other consumers. Abandoned work cancels cooperatively and
compiler process groups stop before worker retirement or server shutdown.

The launch probe sizes idle compiler reuse and a 64..512 MiB / 16..32-record
reconstructible artifact cache; independent compile work starts on demand. Contract limits
bound source, output, logs and compilation time. Stage and read leases protect
live files from reclamation; publication rolls back without destroying the
previous successful version. These files are reconstructible cache, separate
from retained manuscript artifacts. An unverified remote version
cannot replace a verified cached result or qualify as a research pass.

Cache-only preview reservations are independent of completed build tasks. Each immutable
build retains exact reader leases with a 240-second expiry and no reader-count
admission quota. Atomic metadata replacement preserves existing live readers. The cache guard serializes
renewal, release and eviction across processes. Expiry is checked on access and
admission, with no resident cleanup worker. Invalid recent reader metadata stays
protected until its bounded expiry; failed updates retain the previous file.

Verify with `tests/paper/test_paper_build_*.py`,
`tests/api/test_api_v1_paper_builds.py` and resource/shutdown neighbor tests.

## Native compiler budget

`process.py` supervises both TeX commands and toolchain version probes. By default it observes aggregate RSS and process counts without imposing a
private memory ceiling or a 16-process refusal. An explicitly configured
positive `TOFU_PAPER_BUILD_COMPILE_MAX_MIB` remains an operator RSS ceiling;
unset or zero means no private ceiling, with no 1 GiB upper clamp. POSIX
address-space limits apply only with that explicit override. Existing CPU
deadlines and file-size policies remain independently owned. Windows uses
kill-on-close Job ownership and adds a memory ceiling only when configured.

**Problem:** launch-derived 256..512 MiB budgets and a fixed process count
killed healthy compiler trees while the machine could serve them. **Decision:**
retain lifecycle ownership, usage observations and explicit operator choices,
and let actual compiler demand proceed. **Alternatives considered:** raising
the constants repeats the same failure at a different document size; dropping
process ownership would leak descendants on cancellation or parent death.
Measurements in `tests/paper/test_paper_build_process.py` cover a 1 GiB
virtual mapping, two compiler children retaining over 256 MiB aggregate RSS,
and 20 concurrent child processes, followed by joined cleanup. An isolated
Linux measurement on 2026-10-13 completed with 482,496,512 peak RSS bytes and
24 observed processes, with worker IPC reclaimed. Windows/macOS execution
remains unverified on this host.

The worker cooperatively retires its descendants before returning. Linux uses
subreaper ownership to reap children orphaned by an exiting compiler. Parent
death, cancellation and deadline expiry retire the process tree; native session
or Job ownership remains the fallback when cooperative retirement stalls.
Compiler work ends before the PDF validator begins, so both do not retain their
working sets concurrently. There is no resident command-worker pool.

Version probes use the same boundary with five seconds and 4 KiB of output;
temporary output and admission files are removed on completion or failure.
Output-tree scans are incremental and byte/count bounded. Frozen Desktop and
Agent launchers dispatch both command and PDF workers before application setup,
including without console streams. Verify actual allocation, aggregate RSS,
descendants, cancellation and probes in `tests/paper/test_paper_build_process.py`,
then real TeX and Desktop boundary tests. Native Windows/macOS release behavior
still requires verification in those environments.

The wire authority is [`paper_build_v1.schema.json`](../contracts/paper_build_v1.schema.json); generated browser types come from `scripts/generate_paper_build_contract.py`. API commands and artifact reads require the `tasks` scope and an explicit authenticated owner.

## Incremental compile state

Compilation is metadata-heavy; on a network cache volume every stat/open
round trip multiplies engine time. Staging, per-document warm state and
toolchain caches therefore live on a spool volume:
`TOFU_PAPER_BUILD_SPOOL_DIR`, else a per-user directory under the system
temp. Any resolution failure keeps staging inside the cache root. Published
artifacts and receipts stay on the cache volume; everything on the spool is
reconstructible cache with the same lease/orphan discipline.

Each document's warm state is a stable latexmk output directory held under an
exclusive lease. Because the dependency database survives between builds, an
unchanged rebuild runs zero engine passes and a small edit runs the minimum.
The PDF target stays in place so latexmk can prove up-to-date; the published
copy is separate. Seeded state is only a hint: a failed warm compile wipes it
and retries once cold, so a stale or corrupt aux can never keep a document
broken. A contended or missing lease degrades to a cold build.

Contract bounds: `warm_state_bytes` per document — oversized state is wiped
and the next build runs cold — `warm_state_total_bytes` across documents
with LRU reclamation under the cache guard, and `toolchain_state_bytes` for
the shared TEXMFVAR/font cache and generated formats of one toolchain.
Document writes stay confined to the per-build scratch (TEXMFOUTPUT); shell
escape remains disabled and the PDF publication gate below is unchanged.
Verify with `tests/paper/test_paper_build_warm.py`.

## PDF publication gate

Host compilation and received Desktop/Overleaf bundles pass structural PDF
validation before publication. Transport hashes alone do not establish a readable
document. A short-lived worker audits cross-references, all live objects and
streams, the page tree, page geometry and content syntax. Encrypted, repaired,
inconsistent or over-budget PDFs fail with a typed error; a failure preserves the
previous successful build. Structural validation does not prove visual fidelity.

The existing admitted build owns one validator child, with no resident pool.
Validation has no default private memory or wall-clock ceiling.
`TOFU_PAPER_BUILD_VALIDATION_MAX_MIB` is an explicit nonnegative operator override:
zero/unset disables it, and positive values are honored without an upper clamp.
A positive caller timeout is passed exactly to native and parent supervision.
Cancellation still kills/reaps unfinished work. Linux binds worker lifetime to
its launching PID with parent-death signaling; other platforms monitor the
exact owner lifetime inside the active worker. Page/object/decoded counts have
no source admission ceilings. The temporary report is at most 2 KiB and
is removed on every exit. Linux memory measurements exclude pre-exec inherited
high-water usage. Frozen Desktop executables dispatch the same worker before
server, tray or application-data startup, without requiring a console.

Verify parser faults, native resource bounds and lifecycle with
`tests/paper/test_paper_build_pdf_validation.py`; the Overleaf boundary suite also
checks checksum-correct malformed PDFs and preservation of the previous build.

## HTTP surface

[`paper_build_v1.schema.json`](../contracts/paper_build_v1.schema.json) owns
paper-build requests, receipts, artifacts and locations. `POST /api/v1/paper/builds`
returns HTTP 202 with the generic task ID, document ID and viewer subscription.
Requests require an owned conversation and stable request/subscriber identities.
The `paper-build` TaskRuntime supplies ordinary task polling, events and abort.
`POST /api/v1/paper/builds/tasks/{id}/detach` releases one viewer; `/renew`
extends an existing subscription. All operations require the `tasks` scope.

`POST /api/v1/paper/builds/{document_id}/{build_id}/viewers` accepts the canonical
viewer ID and `retain`, `renew` or `release` operation. Its result reports whether
the reservation is retained and its remaining lifetime. Renewal cannot recreate
a released or expired reservation. IDs grant no authority across owners/builds.

`GET /api/v1/paper/builds/{document_id}/{build_id}/artifacts/{name}` serves
immutable owner-scoped bytes with Range support and a read lease. Responses
are private and require revalidation. `POST` to the same build's `/location`
resolves a manifest source line using that build's verified SyncTeX. No query
recompiles a project. Queue/cache pressure returns 503; unavailable owned
resources return 404. 

`POST /api/v1/paper/builds/{document_id}/{build_id}/location/inverse` maps a
PDF point (page + bp coordinates) back to the nearest owned source line with
the same verified SyncTeX; toolchain package boxes never win, and a point with
no owned box returns an explicit empty mapping. `POST` to the build's
`/layout` returns total pages, the section page map and last-page fullness
computed from the build's own artifacts; section titles are read from the live
tree only when the file sha256 matches the frozen manifest, so a drifted edit
yields pages without titles rather than wrong titles.

`POST /api/v1/paper/documents/source` reads one owned source file for the
browser's read-only code view. Authority comes from the durable document
binding, reads stay inside the snapshot root with the contract's
`source_view_bytes` cap, and binary members fail as typed `source_binary`.

`POST /api/v1/paper/documents/arxiv/import` downloads one arXiv e-print source
archive (bounded), extracts it atomically into a fresh `arxiv-<id>/` directory
inside an owned conversation root, discovers TeX candidates and persists the
selection when one candidate wins. A read-only root is refused; nothing
existing is ever overwritten. `POST /api/v1/project/upload-archive` offers the
same bounded extraction (zip/tar, zip-slip and link defense, paper source
budget) for dropped archives, returning the extracted tree and TeX candidates.

## Document discovery and selection

`POST /api/v1/paper/documents/discover` reads the authenticated owner's
conversation roots and Overleaf grants, plus the existing connected capability
catalog. `POST /api/v1/paper/documents/select` validates and stores
one `paperDocument` descriptor through the conversation-settings CAS. Both
require `tasks` scope. Neither endpoint starts a compiler or writes source.
The build contract owns the local/Overleaf document union and request/result
DTOs. Local paths are relative to an exact owned `project_root`; Overleaf
descriptors carry project/server identity and an optional configured main file.
The current descriptor is always `paperDocument`, regardless of execution target.

Discovery is recursive but bounded to 32 roots, depth 32, 4,096 entries,
64 candidates, 1 MiB per TeX file and 16 MiB aggregate reads. Hidden,
vendor and output trees are excluded. Symlinks are refused; special inputs
cannot block the safe file reader on FIFO open. Incomplete scans return
explicit diagnostics and never infer a sole main file. A complete single
candidate can be offered without a settings write; multiple candidates need
explicit selection. Stored selections are revalidated on every discovery.
The combined local/Overleaf list stays within 64 entries and retains the
validated selection when truncated. Capability drift marks a saved Overleaf
selection stale without deleting its grant or selecting a local replacement.
Missing sources or detached roots yield `selection_stale`, not a silent switch
or deletion of durable selection. Remote discovery is currently unavailable
and reports `desktop_unavailable`; existing explicit Desktop builds remain
supported without host fallback.

Verify with `tests/paper/test_paper_documents.py` (service, settings CAS and HTTP
owner isolation) and `tests/paper/test_paper_build_sources.py` (safe input reads).

## Browser reader

`frontend/src/features/paper/pdf-reader.ts` owns independent reader instances;
`pdf-viewer.ts` adapts Paper-mode actions and metadata. PDF.js and its matching
worker load lazily. The reader accepts the complete page set, searches complete
page text and retains all matching results, outline entries and valid links.
Resident canvases and per-page raster bytes follow the generated render contract. Canvas accounting includes a replacement document
before publication: 64 MiB overall and at most 16 MiB per page. The active
document's render window retains at most eight canvases. DPR rasterization stays
within the per-page byte ceiling; deeply
zoomed pages may still be downsampled to remain within that budget.

Source replacement paints the current reading position before swapping the
document; failed or cancelled loads preserve the visible version. Zoom and
mixed page sizes preserve page/fraction anchors. Selection, safe links,
outline navigation and bounded search consume the same PDF instance. URL
transport can fall back once to the cancellable application byte transport,
including a range failure discovered during the initial page render. Closing
releases PDF workers, render/text tasks, canvases, observers and listeners.

Column focus fits the selected column's text bounds without rewriting the PDF.
Two-finger gestures keep the PDF point under the fingers and defer replacement
publication and new raster work until the gesture ends. Closing or cancelling
releases a prepared replacement even during a gesture. Hidden mobile panels
preserve their page, scale and horizontal position across build replacement;
zero-sized layout observations cannot reset the reading anchor.

Build-backed previews retain a reader reservation before loading and renew every
30 seconds, with one request in flight. A replacement keeps the old reservation
until its first visible page is published; failed/cancelled loads release only
the new reservation. Close releases both loading and visible reservations,
including late acquisition acknowledgements. Missed renewals expire within the
last acknowledged lifetime; an explicit expiry notice preserves painted pages
and offers recompilation. Visibility wakeups recheck expiry without overlapping
renewals. Page navigation releases reservations and marks a restored browser
history page expired. Verify lifecycle and 50-cycle cleanup in `tests/paper/test_paper_viewer_lease.py`.

Verify with `tests/frontend/test_pdf_reader_browser.py`, Paper adapter/lifecycle tests
and the real-PDF upload and journaled-TeX journeys in
`tests/e2e/test_e2e_journeys.py`. The build endpoint supports host, Desktop Agent
and explicitly bound Overleaf execution. Both sources use the same browser
document picker, build subscription and PDF reader.
The source drawer accepts an Overleaf project ID or URL and an optional main
file. A URL supplies identity only; selection does not fetch it or compile.
Unverified builds remain readable with source following disabled.

## Overleaf artifact producer

`packages/tofu-overleaf-mcp/` is an independently installable MCP extension of
`overleaf-mcp-plus[compile]==0.3.1`. Its `tofu-overleaf-mcp` stdio entry point
preserves upstream project tools and adds schema-described build/read/release
tools from the paper-build contract. The default application launcher uses this
repository-owned source through the existing isolated uv runner and preparation
stage. Exact previously shipped launch commands migrate while preserving
credentials, disabled tools and server enablement. Custom commands stay intact.

`POST /api/v1/paper/documents/overleaf/select` stores an owned project/server,
optional main file and validated capability contracts in conversation settings;
`/remove` revokes that binding. Selection does not compile. The adapter checks
ownership and the actual input/output schemas again before execution and
publication. Renaming a server grants no authority; changed capabilities require
selection again. Shared scheduling, byte validation, cancellation and cache
publication apply to all three execution targets. Verify the host boundary with
`tests/paper/test_paper_build_overleaf.py`.

One cloud compile supplies immutable PDF, SyncTeX and log bytes. Every download
URL must name the same project and remote build. Cross-origin outputs require
configured origins and receive no session cookie. Owner/project/request identity
plus an unguessable lease token scope artifact reads; the integrating host still
owns transport authentication, project authorization and capability binding.

One export is admitted at a time. A launch-time free-volume probe caps anonymous
temporary files at 1/32 of free disk, at most 256 MiB and eight records. A lease
expires after 120 seconds without renewal; reads use 64 KiB chunks. Admission
reserves the entire per-build budget and never evicts live leases. Cancellation,
timeout, expiry and shutdown close files; OS process exit also removes them.
Cancellation closes local HTTP work, but does not claim to stop an already
accepted cloud compile. Source revision remains explicitly unverified.
The same compile log identifies the main file; a successful export without that
proof or with a different requested main is refused. Identifying the main file
does not establish a compiled source revision.

`tests/paper/test_overleaf_paper_export.py` covers immutable export, ownership, byte
budgets and retirement. Set `TOFU_OVERLEAF_TEST_PYTHON` to an interpreter with
the extension installed to exercise its real stdio server and Tofu MCP bridge
against an isolated Overleaf HTTP fixture.

## Overleaf committed writes

The extension wraps the six file-writing tools declared in
[`project_file_receipts_v1.schema.json`](../contracts/project_file_receipts_v1.schema.json).
Each successful structured receipt carries its project, fresh request identity,
caller-bound owner, acknowledged Git revision and changed file; non-deleted
files also carry a SHA-256 of the committed blob. The pinned upstream commit
lock encloses an exact-object Git push. Native Git exit status establishes
acknowledgement: GitPython's result parser can discard successful raw-object
refspecs. Human-readable success text is never commit evidence.

Direct and progressive task handlers validate the advertised input/output
contracts before calling, disable replay after ambiguous disconnects, and bind
the response to the same owner, request, server, project and requested path.
Only matching structured receipts enter canonical turn file changes. Settlement
preserves them, with explicit source/project identity keeping equal filenames
separate through continuation checkpoints and history enrichment. External facts
cannot become host-file presence or authorize local undo. Preview consumes the
existing turn projection. A Git write receipt
does not verify which revision the Overleaf cloud compiler consumed.

All six writes and the contract's Git read/sync tools share one admitted worker,
without a waiting queue. A fresh private clone belongs to each operation, so a
rejected local commit cannot contaminate the next request and reads observe a
fresh remote checkout. Historical upstream caches stay untouched: they may contain
unpublished user work. No acknowledgement means no canonical write fact; clients
must inspect remote state before retrying an ambiguous operation. Unsafe write
paths and symlink targets are refused before mutation.
The checkout stays fixed across an operation's read/modify steps; intermediate
TTL pulls are disabled. A concurrent remote commit rejects the stale push instead
of rebasing a rewrite generated from older text onto the collaborator's changes.

The complete operation, including clone/pull, is bounded to 120 seconds. Files
and scalar inputs stay within 16 MiB; encoded requests and results are capped at
52 MiB and 2 MiB. Oversized reads fail explicitly instead of returning truncated
success. One launch observation supplies CPU affinity/quota, physical/cgroup
memory and headroom, and free temporary-volume space. Personal Git memory is
128..256 MiB; temporary workspace uses at most 1/64 of free disk and 256 MiB.
Probe failures fall back to 128 MiB memory and 64 MiB disk. Explicit
`TOFU_OVERLEAF_GIT_MEMORY_MIB` and `TOFU_OVERLEAF_GIT_WORKSPACE_MIB` overrides
remain within 512 MiB and 256 MiB ceilings. Export and Git share the launch probe.

POSIX allocation/CPU/file limits, Windows Job memory limits and the parent's
aggregate process-tree monitor bound execution; workspace enumeration stops at
8,192 entries. POSIX address space is capped at twice the RSS budget; aggregate
RSS is sampled every 100 ms. Git packs with one thread, a 32 MiB window and an
8 MiB delta cache. Credentials are supplied through
origin-scoped Git environment configuration, never clone arguments or repository
URLs. Cancellation, timeout and final transport shutdown terminate the owned tree
before releasing admission. A parent-death watcher also stops orphaned POSIX work;
Windows uses a kill-on-close Job Object. Marked private workspaces are removed on
exit; bounded crash reclamation checks PID creation times before deleting leftovers.
A cleanup failure preserves acknowledged success and remains reclaimable on the
next request. No persistent source cache is currently retained; repeated calls pay
for fresh clones. Linux lifecycle/resource behavior is tested; Windows/macOS and
live cloud latency still need platform verification.

`tests/paper/test_overleaf_git_worker.py` covers real clone/write/read, rejection recovery,
native limits, cancellation, crash reclamation and cleanup failure. The write-receipt
suite covers request/capability/owner fences and direct/progressive MCP stdio
delivery. Pinned-upstream cases run with the independently installed
extension, using an isolated local Git remote and no live cloud credentials.

## Desktop execution

An owned conversation's `remote:<agent>:<root>` binding selects the device;
`project_root` optionally selects an exact member of its owned roots. Remote
worktree enablement, online owner/device addressing, declared roots and integer
`paper_build: 1` capability are required. `paper_resolve` returns logical paths;
`paper_build` resolves the share root again and requires device execute permission.
The same frozen-source compiler and location helper run in existing agent workers.

The contract defines the document/build commands and build bundle. One manifest
and 64 KiB artifact chunks cross the existing poll stream. The agent outbox
retains at most 2 MiB; the server drains contiguous frames within 4 MiB/256-frame
bounds, retaining only a replay watermark. Identity, declared sizes and SHA-256
must match before cache publication. Missing or corrupt bytes never publish.
Cancellation bypasses worker admission; stop or 60 seconds without a valid poll
cancels device work. Each build owns its temporary directory and releases it on
completion/failure/cancellation. Command and stream state retire with the caller.

`tests/paper/test_paper_build_desktop.py` runs the real poll loop, bridge, compiler and
cache with an in-process HTTP seam. Transfer integrity, replay and byte limits
are covered by bundle and bounded-stream tests. Remote write, patch and edit
tools return versioned commit receipts after successful atomic mutation. The
server validates the requested paths, binds the addressed owner/device/root,
and folds them into the running task before tool settlement. Partial batches
report only successful writes; legacy device output has no commit proof.
Remote filesystem tools bypass host-only speculative prefetch and execute
through the addressed project handler, preserving device read-before-write
tokens. `tests/e2e/test_e2e_journeys.py` exercises actual model tool dispatch,
device reads/writes, canonical file facts, compilation and PDF.js together;
its device HTTP transport is an isolated in-process seam.

## Research workspace compilation

Research workspace compilation also enters the shared scheduler through
`service.start_record_build`. Its explicit text records freeze every saved
file, including metadata, under the same manifest digest and native compiler
contract. Research's 24-file / 384 Ki-character source ceiling bounds retained
input before queue admission; no model call or MCP tool epoch performs this
compilation. The queue, source/output limits and worker retirement stay shared.
Workspace saves reject oversized text, excess files and ambiguous source paths
instead of silently truncating or dropping manuscript edits.

`lib/research/compilation.py` holds a read lease through the workspace CAS,
verifies owner/document/source identity, each artifact hash and the exact source
manifest, then supplies an application-owned result. Cancelled, superseded,
unverified or mismatched work cannot pass. The workspace repository ignores
client-supplied compilation fields and preserves its previous successful PDF
reference after edits or failures. Legacy tool-based passing assertions become
`not_run`; saved external publication bindings keep their own action boundary.
Successful workspace saves retain the exact PDF, SyncTeX, source manifest and
log before committing their reference. The retained document pin and workspace
revision advance in one Sidecar transaction; failed/conflicting saves leave the
previous manuscript pinned. The artifact route reads retained bytes without
requiring cache space or compiling, including after Sidecar restart.

Research's Paper tab embeds `research/components/manuscript-preview.ts`, which
uses the shared PDF core, navigation/search/outline controls, reading focus and
build-lease owner. One visible reader may retain one current and one replacement
build; the existing 64 MiB canvas and bounded page-window budgets apply. Hiding
the tab or leaving Research destroys the reader and releases leases. Its workspace
owner keeps one page/zoom anchor for tab revisits; cross-refresh Research reading
state still awaits integration with `paper_documents`. Workspace/CAS updates
preserve the reader DOM and do not reload an unchanged build. Rendering or compile
failures keep the previous valid PDF; read retries do not trigger compilation.

Submission checks display every server-returned gate and the server's `ready`
decision. Unsaved edits invalidate this projection, including edits made while
a save/action reply is in flight. Checks expand on demand beside the compilation
receipt; the browser does not duplicate scientific-readiness rules.
Verify `tests/research/test_research_compilation.py` and the real workspace action/PDF
journey in `tests/research/test_research_compile_browser.py`.

## Retained manuscript storage

`artifact_repository.py` is the owner-scoped application port. Sidecar's
`paper_artifacts` domain owns immutable 256 KiB chunks, upload witnesses,
document pins and read leases in `storage_records`. Keeping bytes and references
in the same authority makes ordinary database backups and logical replay carry
the complete manuscript. The representation stays behind the repository port;
callers have no SQL, database-path or binary-backend assumptions.

`paper.artifact.prepare` reserves the entire declared payload and receipt;
`append` accepts contiguous chunks and exact-byte retries; `seal` recomputes
every complete artifact digest incrementally. These commands use natural-key
witnesses without retaining duplicate payload receipts. The Research workspace
CAS pins verified builds; chat's durable document-generation transaction pins
successful builds and prevents unverified work from replacing a verified pin. `discard` expires
uncommitted uploads and cannot undo a pin after a lost commit acknowledgement.
`latest` reads the owned document pointer. Chunk `read` requires a retained
owner/build lease. Durable viewer reservations and transfer leases share a
240-second lifetime per build without a reader-count limit. Viewer admission never hydrates cache;
only previews without retained artifacts use cache reservations. Renewing a
released/expired viewer cannot recreate it. A live reservation permits later
Range reads of that retired version; after the final reservation expires,
new reads cannot revive an expired version.

Retained manuscripts and saved paper identities have no aggregate count or
payload quota. `TOFU_PAPER_BUILD_RETAINED_MAX_MIB` is retired in both SQL and
native backends. Usage ledgers remain exact, overflow-checked accounting in the
same transaction; they cannot refuse a valid new document, association or
upload. Per-artifact/chunk/receipt and transaction byte bounds still describe
actual protocol/storage operations. Immutable digests, owner checks, requested
generations and verified-publication rollback preserve the previous manuscript.
Pinned user state is never silently deleted to make room.

Uncommitted uploads expire after five minutes. Replaced document versions become
reclaimable after four minutes and after existing read leases end. Reclamation
walks owner-filtered 128-record pages using a committed cursor and deletes at
most 32 chunks per call. A full protected page advances to later records; an
incomplete artifact stays selected until all its chunks are deleted. No resident sweeper is
added. Optional hydration starts on demand, buffers one chunk at a time and publishes only checksum-verified immutable cache data;
reading an older retained build never advances the compiler's success pointer.

`artifact_reader.py` exposes the same seekable artifact port for cache files and
retained chunks. Missing, unavailable or damaged cache data falls back to the
owned durable build. HTTP reads hold a lease through completion/disconnect and
fetch only needed chunks; source location opens only the manifest and SyncTeX.
Sidecar checks each retained chunk against its sealed digest. Werkzeug owns
Range and conditional headers in `file_serving.py`, including single-byte,
suffix, If-Range, If-None-Match and HEAD responses; ETags use the artifact digest.
The first requested chunk is checked before headers, and later read failures
abort the response. Reads and cleanup run outside the serving loop; cancellation
joins the outstanding read before releasing its lease.

Direct reads admit eight streams per launch-derived build worker, at most 32.
Each stream retains one 256 KiB decoded chunk and emits at most 64 KiB per HTTP
iteration; new chunk reads stop before their 240-second lease expires. Chunk
JSON uses the strict two-string shape and the stdlib decoder to avoid orjson's
over-4-MiB scratch allocation for a single base64 chunk. Verify ranges, cache
pressure/corruption, lease retirement, disconnects, bounded admission and a
9 MiB read below 4 MiB Python allocation with `tests/paper/test_paper_build_retained_streams.py`.

The experimental Tofu-DB adapter implements these eight artifact operations and
Research publication in its native authority transaction. The generated receipt
schema comes from this same public contract. Native append admission validates
and decodes a chunk once; its immutable canonical data/digest cross subsequent
IR checks with a field-byte budget, preserving exact retries and invalid-input
errors without repeated payload serialization. Generic record/blob documents hold
chunks and metadata; a tenant-wide reservation counter and owner-prefixed index
bound admission and cleanup without scanning other owners. The native daemon
reuses its one launch probe and exposes `paperBuildRetainedMaximumBytes` in
readiness. Native fault tests cover every publication/upload/cleanup I/O boundary,
short writes and dropped syncs; the retained-build differential scripts compare
observable results with Sidecar. Production selection still requires the storage
certification gates, including retained-state migration and complete replay.

Verify real process/restart/HTTP behavior with `tests/paper/test_paper_build_retention.py`,
and quota, rollback, reader expiry, backup and measured Python-buffer/encoding
growth with `tests/paper/test_paper_build_retention_faults.py`. Encrypted logical
replay is checked by `tests/paper/test_paper_build_retention_replay.py`; the same
process contract lane runs against isolated SQLite and PostgreSQL authorities.
Chat builds now use the durable document publication boundary below. Research
still pins only through its workspace CAS, so compiling a candidate cannot
replace the saved workspace manuscript before that transaction succeeds.

## Saved document service

Sidecar `paper_documents` owns 64-hex opaque document IDs, canonical source
bindings, requested generations, completed receipts and reading revisions.
Compiler targets, display labels and edited-file paths do not define a binding.
Explicit selection and build admission register a document; discovery only
resolves existing IDs. Existing cache-only historical builds are not migrated
automatically, and source rebinding/deletion is not yet exposed.

Scheduler admission serializes the durable request before queue publication;
duplicate live subscribers share that admission. Each new task has a distinct
admission identity. Replacement, detach, submission failure and worker retirement
release their generation without disturbing a newer one. A request expires
after one hour if retirement acknowledgement is lost. Only the requested
generation's first terminal receipt can publish; exact retries return its
recorded outcome. Successful publication pins the sealed artifact and receipt
in the same transaction. Reading-state CAS has a separate revision.

`POST /api/v1/paper/documents/open` requires an owned conversation and returns
`document: null` or an owned saved record with an immutable build URL, reading
state and compile-availability hint. It neither discovers sources nor compiles;
missing roots and offline devices leave saved manuscript access intact. Actual
builds revalidate source authority. `POST /api/v1/paper/documents/{id}/reading`
requires the authenticated document owner, expected reading revision, writer ID
and sequence. Within one writer, a newer sequence may arrive first and older
saves cannot move the reader backwards. Another window must match the current
revision; stale writers return 409 and another owner's ID returns 404.

The contract bounds this durable catalog to 128 documents, 512 conversation
associations and a 1 KiB reading-state shape; admission refuses excess without
deleting user state. Source loss never purges a document. Explicit catalog
reclamation is still pending.
The experimental native engine implements the same eight semantic operations
with generated schema/limits, owner-bound generic records (64 KiB per record)
and a tenant-shared 16-byte document/association accounting record, without
count admission. Generation completion
and the retained pin share one authority transaction; reading ordering remains
independent. Native parity does not certify migration or production cutover.
Artifact reads and source location use the shared leased artifact port; neither
requires full cache restoration.

Browser reopen loads the retained build before source discovery and restores
page, scroll anchor, zoom, fit and reading mode in its first published view.
Source discovery failure keeps that PDF readable; unavailable sources disable
both compile controls. Reading writes coalesce for 750 ms per document. Independent documents start
saves on demand; each document owns its normal write and final overlapping
save with the same monotonic writer sequence. Leaving a document marks its
local entry closed and releases it after exact write settlement. Closing
drains within ten seconds; browser termination or network loss can still prevent
acknowledgement. Failed or conflicting saves show one explicit retry action;
retry refreshes the revision without reloading the PDF or starting a compiler.
Verify ordering, bounded teardown and retry with `tests/paper/test_paper_saved_reading.py`
and `tests/paper/test_paper_saved_preview.py`; the real chat browser journey covers
retained reopen after source/cache loss and subsequent editing.

Verify registry identity, generation fences, rollback and demand admission with
`tests/paper/test_paper_document_repository.py`; real restart, HTTP authority and the
same PostgreSQL semantics with `tests/paper/test_paper_saved_documents.py`. Scheduler
and service tests cover cancellation retirement, failed cache preparation and
successful durable commits whose disposable cache-pointer update fails.

## Chat preview

Opening is independent of editing. The project-folder disclosure exposes
**Open paper preview** even when no file receipt exists. The lazy workspace
shows verified main-file choices, **Compile PDF**, and **Find TeX sources**.
Selection persists on the server, not in localStorage; Compile uses the current
descriptor directly and does not depend on another settings write.

Successful authoritative TeX reads/extraction rounds and applied historical
file facts can request discovery. Missing historical `changeId` metadata is a
discovery hint, never synthetic write proof. Cold/revisit discovery offers a
Paper button without opening the split or compiling history. Hints invalidate
the owned-root scan rather than narrowing to a guessed main file: template
siblings remain visible. Pending/refused/failed records, prose and arbitrary
tool-output strings do not authorize a build. Discovery signals coalesce to
one per observer update and one in-flight browser request plus one trailing
latest demand. Saved-selection arrival/change during hydration also invalidates
discovery, even without a TeX transcript.

The selected main remains pinned for same-paper edits; recognized sibling
main documents require selection before switching. A different/stale discovery
identity invalidates old subscriptions and PDF loads. Closing or navigating
fences late discovery/selection results. A pending settings write in another
conversation cannot prevent opening the active one. First discovery fills the
empty reader without closing an already-open source drawer or stealing focus.
Opening a selection restores its retained PDF when available. A selection with
no retained build shows **Ready to compile** and requires an explicit Compile
action. Reopen and source rediscovery never start a compiler themselves.

`preview-changes.ts` consumes canonical turn state through the core runtime's
`onStateApplied` port, independently of DOM rendering. Journal commits carry a
stable `changeId` and actual `rootPath` through live and settled file projections.
The observer compares the reducer's previous and current states without a
second receipt cache or a recent-turn cutoff. Older active turns and settled
undo/redo remain observable. Initial history,
backfill, background conversations, pending/refused changes and unchanged
receipts do not compile. Supported source suffixes share the build contract;
undo/redo commands trigger only after their file mutation settles.

`preview-controller.ts` submits through the typed paper-build API and observes
ordinary TaskRuntime status. It owns one pending submission and one viewer
subscription; a late acceptance after close is explicitly detached. Closed
intent persists for at most 32 conversations. Switching conversations or
entering Paper/Research releases the preview. Host/remote work stays owned by
the server, including cancellation and subscription expiry.

`preview-panel.ts` places the shared reader beside the transcript/composer in
a draggable desktop grid. Its single toolbar opens document selection from the
filename and gives direct access to page, zoom, search and reading mode. Source
discovery, follow, download, outline and logs live in secondary drawers. Narrow
panels move fit, recompile and close into the drawer; mobile has one toolbar
below its chat/paper tabs. Touch controls retain 44-pixel targets.

The initial split reserves 40% for chat and 60% for the paper. Device-local
layout preferences are persisted on deliberate adjustment; the old implicitly
saved 55% default migrates to 40%, while other old widths remain. Column focus
and split reset live in the secondary drawer. Verified update status remains
available to assistive technology without a permanent success label taking
space from the paper; pending, unverified and failed states remain visible.
The split composer keeps attachment, model, search and send controls direct;
secondary modes use the existing More sheet. Controls can wrap in unusually
narrow columns instead of crossing the divider, and fixed menus retain their
viewport positioning.
The shared options sheet is owned by `composer-options-sheet.ts`; its retained
entry points delegate visibility and focus to that owner. Opening focuses the
sheet, Tab stays inside it, and Escape closes it and returns to its opener.
Shared sheet styles apply at every viewport where it can be opened. Fine-pointer
desktop sheets are at most 480 pixels wide; touch/narrow sheets retain safe-area
padding and viewport height limits. Closed sheets remain hidden.

Reading mode moves the same controls and viewport into an owner-scoped native
modal dialog. Background navigation becomes inert; exit restores node positions,
the previous split and focus without reloading or compiling. The shared
`reader-controls.ts` owns navigation, outline and one cancellable search request;
`build-reader.ts` holds one visible and one pending build lease. Mobile reading
mode hides the tabs. Below 360 pixels the toolbar wraps to keep touch controls
and zoom percentages readable. Escape
closes the current drawer, exits reading mode, then closes the preview; each
step restores the relevant focus. Responsive tab panels restore the host's
original accessibility attributes on teardown. Long logs show a bounded excerpt
and an explicit download link to the full log of that build.

Update status appears once. Follow is opt-in and consumes only verified
locations from the displayed build. A failed build preserves the previous PDF.
Verify with `tests/paper/test_paper_preview_*.py` and the journal-to-preview journey.

## Agent compile loop

Two built-in tools close the edit→compile→fix loop inside a chat turn.
`paper_build` compiles the conversation's selected (or explicitly pathed)
document through this same service — frozen snapshot, supervised latexmk,
validated PDF, immutable receipt — waits within its declared timeout, and
returns the verified verdict: status, engine, duration, and structured
diagnostics with `file`/`line` positions parsed from the `-file-line-error`
stream (classic `l.N` and `on input line N` records are resolved as fallback,
repeated latexmk passes are deduplicated). `missing_package`,
`undefined_citation` and `undefined_reference` are dedicated diagnostic codes.
`paper_build(action='diagnostics')` reports the latest retained build without
recompiling. The tool is deliberately not approval-gated: it writes only
bounded build-cache artifacts, and gating would break the autonomous loop it
exists for. `paper_layout` answers page-budget questions from the latest
verified build: total pages, section page map and last-page fullness, so
"compress to N pages" plans against the same layout report the browser shows.
`paper_import_arxiv` runs the same download→extract→discover→select import as
the HTTP command, so an agent can fetch a related paper's source, edit it and
verify with `paper_build` inside one turn; it is the family's only
write-partitioned tool because it creates a workspace directory. Successful
compile verdicts carry the page count read from the build's own SyncTeX.

Browser preview builds settle out of the model's sight, so a failed build with
a conversation binding enqueues one bounded `<paper-build-update>` into the
conversation inbox, drained at the next round boundary exactly once per
(owner, document, source digest). Delivery is volatile-only by design: with no
live turn the preview panel is already the user's surface, and a durable queue
row would burn an unattended model turn.

## Source-level citation audit

Every build additionally audits its frozen sources before publication:
`\cite`/`\nocite` keys are cross-referenced with .bib entries (a minimal
bounded BibTeX key extractor in `lib/paper/bib.py`), `\ref` keys with `\label`
definitions, and duplicate keys are reported with positions. Keys the engine
log already reported are not repeated, `\nocite{*}` suppresses the unused-pool
summary, and the audit is byte-budgeted and advisory — it never changes the
build verdict. Without any .bib file, cited sources earn one
`missing_bibliography` row.

## Read-only source interaction

The preview drawer offers Problems, Layout and Sources views. Problems groups
receipt diagnostics errors-first; any row with a `file` opens the read-only
code view at its line (the same jump the agent's structured diagnostics give).
Sources renders the build's frozen `sources.json` manifest as a file tree and
opens text members in a line-numbered, lazily LaTeX-highlighted code view.
Ctrl/cmd+click or double-click on the PDF resolves the inverse SyncTeX mapping
and opens that source line; a point with no owned mapping reports so instead
of guessing.

## Source editing and AI edit proposals

The browser source view is editable. `POST /api/v1/paper/documents/source/edit`
(`PaperDocumentSourceEditRequestV1`) takes a document_id + path (resolved
against the same main-file-parent base as the read route, through the same
symlink-refusing helpers, restricted to the contract's text source suffixes)
plus exactly one of `content` (a full-file write, capped at
`source_view_bytes`) or `edits` (up to 20 exact-anchor search/replace hunks,
applied in order with per-hunk isolation — one failing hunk never blocks the
others and every hunk reports `{index, ok, error?}`). An optional
`base_sha256` gives optimistic concurrency: a mismatch fails the whole call
with `source_conflict` (409) before anything is written. Every mutation
funnels through the journaled project write tools, so a browser edit appears
in the file-changes bar and is undoable exactly like an agent edit. The
response is `{path, size_bytes, sha256, results?}` — the sha of the final
bytes, so the client can chain its next edit's `base_sha256`.

AI edit proposals add a review lane on top. The agent tool
`paper_propose_edits` (same document resolution as `paper_build`) never
writes: it parks up to 20 hunks in a proposal after pre-validating each one
against the live files with the write tools' own matching logic
(`_compute_diff`, replayed sequentially per file so hunk N sees hunk N-1's
result). Hunks that no longer match stay in the proposal marked `failed`.
The tool verdict's first line carries `proposal_id: <hex>` for the preview
panel, which fetches it via `POST /api/v1/paper/proposals/get` and renders
every hunk as an accept/reject diff. `POST /api/v1/paper/proposals/accept`
re-runs each selected `pending` hunk through `tool_apply_diff` against the
document base — re-validating drifted files and journaling one undo
transition per file — and returns the updated proposal plus per-hunk
`results`; `POST /api/v1/paper/proposals/reject` only marks. Proposal status
folds hunk states into `pending | applied | partial | rejected`.

The proposal store (`lib/paper/builds/proposals.py`) is process-local and
bounded: 3600s TTL refreshed on each decision, at most 64 proposals per owner
and 4096 globally, oldest evicted first, one module-level lock. Restart loses
pending proposals — the user simply re-asks. TODO(enterprise): a durable
multi-user deployment replaces this with a storage-port implementation
keeping the same bounds, owner scoping and validation flow; ownership is
already an explicit parameter everywhere and foreign-owner access is
indistinguishable from expiry (404).

## Retired legacy preview pipeline

Every compile path (browser, agent tool, Research workspace, Overleaf MCP)
converges on this service; the unprotected raw `subprocess` compiles that
preceded it (no rlimits, no PDF validation, no receipts) are the
counterexample recorded in
[DEFENSIVE_PATTERNS.md](DEFENSIVE_PATTERNS.md).


## Complete source snapshots

**Problem:** archive/project intake accepted larger sources while the compiler
refused 512-plus source files, 32 MiB files, 64 MiB trees or 4,096 scanned
entries. Explicit source records and browser preview changes repeated these
ceilings. **Decision:** retire source-count/byte/scan limits from the canonical
contract and generated bindings. Authorized source inventories, include graphs
and explicit records retain complete inputs. Filesystem copying and its second
identity/hash pass stream through the existing descriptor-validated reader in
1 MiB chunks; snapshots do not materialize whole file byte arrays. Chunk callbacks
check cancellation, and the existing source-mutation retry/fault seams remain.
Sources become read-only only after successful verification. Browser preview
observes complete committed change lists instead of keeping 512-entry prefixes
or tails. Output traversal also removes the shared obsolete entry-count gate;
its separate output-byte policy remains.
**Alternatives considered:** raising limits rejects larger sources later;
trimming the dependency graph creates invalid builds; reading whole large
files duplicates their bytes in RAM; removing validation to speed copying
weakens the authorized-root and immutable-source boundary.

Measurement freezes 1,104 files / 68,171,838 bytes, including a 65 MiB file,
with exact digests and read-only output. Source records retain 1,102 files and
a multibyte input above former byte budgets; scans reach dependencies after
4,101 ignored entries. Browser/Node checks retain all 1,101 committed changes.
Snapshot metadata grows with the actual inventory and remains owned by the
build lifecycle. These are source/preparation checks, not a large-document
TeX throughput benchmark; compiler time/memory, discovery and artifact policies
remain separate audit work.

## Complete authorized paper discovery

**Problem:** discovery stopped at fixed root/depth/entry/source-byte/candidate
quotas, then storage and browser projection separately limited the result to
64 candidates. **Decision:** traverse every authorized local root and supported
source iteratively, read complete candidate text through the existing safe
reader, and resolve/project the complete ordered list. Retire those discovery
resource fields and array bounds from canonical/generated contracts and native
request admission. Directory iterators close within each visit; pending
directory metadata grows with actual demand. Existing ignored tool trees,
symlink denial, selection CAS, owner binding and explicit remote capability
authority remain. Scan/I/O failures still report incomplete discovery and never
produce a guessed singleton selection.
**Alternatives considered:** larger quotas lose later candidates again; keeping
only a selected prefix hides valid alternatives; unlimited recursive call
depth can fail on a valid hierarchy; weakening root/path validation grants
unauthorized sources.

Verification returns 72 real candidates after 4,101 ignored entries and a
40-directory hierarchy, traverses 33 owned roots, and reads a 17 MiB main
source. Native registration/reopen resolves all 71 identities in order and
returns only absent identities for a foreign owner. Browser selection retains
71 choices and can select the last one. These counts measure admitted demand,
not process RSS or source throughput. Candidate text still materializes per
file; format/path and remaining compiler/artifact policies remain under audit.

Research source acceptance — Problem: Research normalization/digests kept only
24 source files, clipped file/tree text at 65,536/393,216 characters and rejected
640-KiB workspaces; native workspace storage rejected 8 MiB. Decision: source
normalization and digests cover every complete source, while workspace CAS and
owner-bound blob storage retain the full user document without private byte
admission limits. Source path/type/duplicate validation and server-written
compilation proofs remain mandatory. The 640-KiB research observation-page target
continues to govern paged history, not source acceptance. Workspace HTTP bodies
use the existing owned file-backed transport without the default 50-MiB cap.
Alternatives: raising caps fails later; clipped source digests can certify a
different manuscript; changing storage backends weakens the configured authority.
Verification stores 1,102 source files including 9,437,184 UTF-8 bytes in one
source, reloads exact content/hashes and verifies a late-file edit changes the
digest. Native tests retain/reopen a 9-MiB source and enforce CAS/owner isolation.
These measure completeness and retained bytes, not constant parser RSS.


Compiler temporary output demand — Problem: a private 128 MiB native file
limit and aggregate directory limit killed otherwise valid compiler work.
Decision: default compiler supervision imposes neither limit. `run_command`
accepts an explicit nonnegative file limit: zero/omitted disables that override,
and positive values reach the native worker exactly. Output traversal continues
to reject nonregular entries. Deadlines, cancellation, joined process retirement
and explicit RSS limits retain their contracts. Publication/retention and PDF
validation have separate limits under continuing audit; this change does not
claim complete large-artifact publication. Alternative rejected: raising the
default recreates failures for larger compilations. Real subprocess tests write
and hash 129 MiB, observe an explicit 257 MiB native limit without clamping, and
verify that an explicit 1 MiB override still stops a larger write.


Complete paper artifact publication — Problem: a 64 MiB file ceiling and
128 MiB aggregate ceiling rejected already generated results in metadata,
remote bundles, retained storage and native receipts. Decision: remove both
source quotas across the shared schema, Python/native admission and Overleaf
export; full-file readers no longer reject at the old size. MCP read offsets
have no private file-size ceiling. Preserve bounded transfer chunks, digest
and length verification, ownership, success-only publication and rollback.
Reclamation steps scale with the accepted build's actual part count; each GC
transaction remains bounded and cancellable. Native size accounting uses
checked platform arithmetic so an overflow cannot wrap into a small reservation.
Alternative rejected: raising ceilings still rejects larger completed work;
clipping artifacts breaks independent recovery.
A real valid PDF above 129 MiB traverses the bundle, passes PDF validation,
persists in SQLite, survives an authority backup and is recovered chunk by chunk
with the exact SHA-256. Native checks cover large receipt admission, extent
overflow and the existing retained integrity/owner/rollback lifecycle; they do
not establish full large-file native or Windows/PostgreSQL performance. Durable
growth follows selected artifact bytes and fixed-size chunk metadata. Separate
PDF structural/decode/time budgets and compiler log policies remain under audit.


Complete PDF structural audit — Problem: private page/object/operator/decoded
byte caps rejected valid larger documents and conflated reader projections with
artifact validity. Decision: traverse complete references and page trees, decode
complete streams and parse complete content operations. Seen-identity sets still
prevent graph loops, and invalid page parents/counts, geometry, missing objects,
repair attempts and truncated/trailing compressed data still fail. Worker-local
pypdf filter extent hints are restored on return, including error paths.
Alternative rejected: increasing caps still rejects a larger valid source;
accepting unaudited late objects would weaken artifact validity. Parent report
validation accepts nonnegative observed metrics without private document-size
maxima; small report transport and execution memory/time policies remain separate.
Real guards validate 1,001 pages, over 100,000 objects, over 250,000 operations,
a 33 MiB decoded stream and over 128 MiB across streams, plus existing invalid
PDF and owned-child retirement cases. These are structural checks, not visual
fidelity or removal of the remaining worker execution limits.


Complete reader document demand — Problem: the UI rejected documents over
1,000 pages and clipped search text/results, outlines at 512 entries/eight
levels and page links at 512. Decision: navigation, search, outline traversal
and annotation delivery follow the actual document. Column geometry also scans
complete page text. Search yields between pages and periodically within large
text/result sets so cancellation can discard stale results. Outline identity
tracking prevents cycles without treating nesting or cardinality as a quota.
Alternative rejected: larger fixed ceilings still hide later evidence; reporting
a clipped miss does not make the missing work available. Existing viewport
render residency, canvas accounting, abort fencing and teardown stay in place.
Real PDF.js browser coverage loads 1,005 pages, finds all 301 matches including
the last page, traverses 600 outline entries including depth 12, exposes all
600 first-page links, navigates to the final page and reclaims canvases on close.
Protocol tests search beyond six million characters and retain 1,201 matches.


Validator execution demand — Problem: automatic 128..512 MiB memory and
15-second wall/CPU limits rejected complete source audits independently of
actual resources. Decision: default validators follow actual work under owned
process lifetime; only explicit positive memory/deadline settings install
limits. Bad overrides fail before launch. Alternative rejected: raising private
ceilings still rejects larger work; dropping cancellation or parent-lifetime
ownership would leak an orphan validator after a host crash. Native limits,
RSS observation, strict report checks and joined cleanup remain separate from
source admission. Real tests decode over 540 MiB with observed memory above
512 MiB, complete after 15 seconds, retain exact 2 GiB memory/42-second timeout
requests, reject explicit overuse and prove Linux parent-death retirement.
Windows/macOS owner monitoring is implemented but unverified on this host.


Compilation execution demand — Problem: a private 180-second CPU/wall limit
also drove desktop/Research waiting and Overleaf export lifetime. Decision:
default native compilation and cloud export wait for actual owned completion.
Positive caller deadlines reach native workers without an upper clamp; toolchain
version probes retain their separate diagnostic policy. Desktop compilation
checks connection, Stop and worker ownership; Research maintains its subscriber
while awaiting the task. Overleaf build calls no longer create a private deadline.
MCP document calls may omit a deadline or request a positive one above 300
seconds, retaining configured server policy, cancellation and no ambiguous-call
replay. Artifact chunk-transfer and lease lifecycles remain independent.
Alternative rejected: increasing defaults still aborts longer work; abandoning
parent/connection/cancellation ownership leaks compilation after its requester
retires. Real native checks observe an unlimited default CPU allowance and an
exact explicit 420-second allowance. Real TeX, cloud-export, protocol-document,
Research publication/rollback and cancellation/retirement suites remain guards.
This verifies supervision policy and complete result delivery, not a claim that
an actual TeX compilation ran longer than 180 seconds on this host.


Complete compilation logs — Problem: a private 1 MiB log gate killed native
compilation and truncated its original evidence on exit; header-only diagnosis
and 8 KiB cloud root-input probing omitted late records. Decision: default
compilation/export retains complete log bytes. Explicit native log limits remain
caller policy. Diagnostics scan complete line iterators with seven-line lookahead;
the 64-row reply projection prioritizes late errors over warnings and retains
only keys for displayed rows. Raw logs remain independently readable artifacts.
Cloud main-file discovery walks the saved log until the compiler marker while
preserving path and selected-main validation. Alternative rejected: larger quotas
still abort or omit longer logs; publishing after a clipped warning-only head
can misclassify a failed result. Guards write/hash over 5 MiB of actual native
output, publish a valid PDF with a multi-MiB log, refuse publication for a late
error after eighty warnings, and export a complete cloud log with a late root
marker. Parser buffering scales with lookahead and longest line, not total log
length; this is not a constant-memory claim for arbitrarily long individual lines.


Complete SyncTeX source demand — Problem: a private 16 MiB decoded gate
prevented normalization and source location, while queries assembled a full
byte body, decoded string and split-line list before parsing. Decision: consume
gzip lines through EOF/CRC and feed the shared geometric parser an iterator.
The parser still accepts text callers and retains first boxes, last hboxes and
nearest-line behavior. Normalization stages in the destination directory, fsyncs
the complete gzip and atomically replaces only after successful decoding.
Deterministic gzip metadata avoids changing the artifact digest on an identical
normalization retry. Alternative rejected: a higher quota still loses larger
mappings; publishing partial output or returning early skips damaged gzip tails.
Guards retain over 17 MiB of box records and a late page-1,001/line-999 mapping
after source removal, preserve exact decoded bytes/source hash and verify inverse
location. Truncated/CRC-bad input, malformed Input records and injected replace
failure preserve a previous complete destination and retire staging files.
This improves streaming input, not constant parser-map memory: geometric
indexes still grow with source records and a long individual line can be large.

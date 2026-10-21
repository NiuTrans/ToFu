# Monorepo and release boundaries

This repository is the only source authority for the Tofu application and its
first-party Python and Rust artifacts. Source location, installable package, and runtime
process are separate boundaries: sharing one Git repository does not make an
optional capability part of the core wheel or force it into another process.

## Rust migration quality

**Problem:** translating Python line for line can preserve allocation, copying,
boundary and process overhead while adding installation cost or losing failure
semantics. Rust source coverage alone does not establish product improvement.
**Decision:** Rust is the preferred implementation direction. Advance complete
vertical paths through the existing domain owners, prioritizing measured hot
paths, removal of runtime dependencies and lifecycle weaknesses. Use ownership,
borrowing, compact typed state, bounded buffers and joined concurrency where
they remove actual costs. Python supplies behavioral oracles during cutover;
avoid adding a permanent second implementation or state authority.

Every promoted path preserves authorization, cancellation, error reporting,
durability and recovery, with differential and fault tests for its boundary.
Compare identical work in the old and new paths, including small and large
inputs, fragmented streams, concurrency and failure where relevant. Record
latency distributions, CPU, allocation/residency and startup or installation
costs affected by the change. Installation claims require a verified built
artifact and its real runtime dependencies, not a successful source build alone.
Component measurements justify component decisions; default application cutover
requires its own end-to-end evidence. Retain adverse results and resolve them
before promotion rather than hiding them in an aggregate throughput number.
Unmeasured dimensions remain explicit gaps.

**Alternatives rejected:** a wholesale rewrite delays useful verified delivery;
permanent Python/Rust mirroring duplicates costs and authority; selecting Rust
solely by language or using a faster microbenchmark to waive application,
installation or robustness checks can ship a regression. Accelerate migration
by landing independently verified slices and retiring redundant old paths.

### SSE framing comparison

**Problem:** fewer allocations and faster ordinary text can hide slower fragmented
or Unicode delivery. **Decision:** the shared native SSE framer retains its
existing implementation. `packages/tofu-runtime/tests/sse.rs` verifies exact
payload ownership, empty multiline fields, byte budgets and resynchronization;
512 malformed/oversized fixtures compare eight transport chunkings with bytewise
delivery. The retained `benchmarks/sse_framing.rs` probe validates every event
and measures seven workloads, including one-byte and Unicode fragments.

Build locked dependencies with `cargo test --locked -p tofu-runtime --test sse`,
then run `python3 benchmarks/sse_framing_measure.py --baseline REV
--dependency-dir TARGET/debug/deps --output REPORT.json --rustc RUSTC`.
Use the compiler that built the dependency directory. The collector freezes
source hashes, pins one available CPU and alternates 12 fresh-process pairs per
case. Each process emits seven batch clocks; allocation counts and requested
peak live bytes come from separate instrumented binaries. These are component
diagnostics, not whole-process RSS, installation or complete-task acceptance.
Compiler source paths are remapped identically, binary hashes are retained, and
identical-source comparisons refuse different binaries. This control exposed
large timing differences in the initial unnormalized-path probe; those earlier
clocks are retained as unqualified observations. The corrected same-source
control has identical binaries and case median ratios between 0.994 and 1.011.
`--candidate-framer PATH` compares an experiment without installing it.

**Alternative rejected:** bulk valid-text scanning and buffer moves improve
ordinary and full-chunk Unicode cases by about 3–5 times, but the corrected
comparison still slows one-byte fragments by 11% and fragmented Unicode by 4%.
The candidate is not promoted despite lower allocations and peak live bytes.
The frozen sources, candidate patches and raw paired observations
retain those adverse cases alongside improvements. Better future candidates
must explain the tradeoffs across the full matrix before changing this owner.

### Native provider completion

**Problem:** the common native Chat/Anthropic/Responses completion path lowercased
every response before checking whether it could be a short gateway notice, then
`json!` serialized borrowed owned strings and copied the completed body and tool
fields. **Decision:** check finish/tool eligibility and a conservative UTF-8 byte
bound before the exact generated character limit and Unicode normalization.
Transfer completed strings into JSON values, releasing stream growth slack at
that boundary. Keep the original text, tool metadata, ordering, classification
precedence and empty-argument defaults. The generated Python contract remains
the classification authority. No pool, queue, dependency or durable format changes.

`tests/provider.rs` verifies scalar boundaries, cancellation/finish/tool precedence,
large arguments and compact retained capacity after multiple deltas; neighboring
Anthropic/Responses/HTTP tests exercise the same completion owner. Verify default
and `--no-default-features` provider tests because JSON map ordering differs.
The retained `provider_finish_measure` Cargo example measures real `ChatStream::finish`
over twelve exact-output fixtures, including fragmented growth and long tool arguments.
Build with `cargo build --locked --release -p tofu-runtime --example
provider_finish_measure`; run `TARGET/release/examples/provider_finish_measure
CASE OPERATIONS`. Feed/setup and validation are outside its seven batch clocks.
Build allocation evidence separately with `cargo rustc --locked --release -p
tofu-runtime --example provider_finish_measure -- --cfg allocation_counts`;
instrumented clocks are suppressed. Compare revisions from the same build path
with the same locked dependencies and retain binary/source hashes and raw pairs.
These are completion-component clocks and requested allocation bytes, not
whole-application speed, RSS/PSS or installation qualification.

The `rust_provider_completion_20261015.json` retains
12 fresh-process pairs per fixture and the rejected placeholder-insertion variant.
64 KiB ASCII completion measures 11.23 to 0.34 microseconds and 131,456 to 280
requested allocation bytes; fragmented text measures 24.23 to 2.07 microseconds.
Short/gateway/Unicode-boundary median ratios are 0.968/0.877/0.959. Fragmented tool
arguments take 0.895 times the prior duration but request 50 more allocator bytes
(0.038%) while calls fall from 25 to 22; retained string capacity is unchanged.
This small allocation tradeoff is explicit; the evidence does not assert zero
copies when releasing growth slack requires allocator relocation.

**Alternatives rejected:** ASCII-only folding changes Unicode behavior; merely
moving the lowercase operation still copies full results; transferring buffers
without releasing growth slack retains more memory after fragmented responses.

## Native runtime artifact boundary

Problem: the Rust runtime was executable only from build/checkouts. Decision:
`scripts/build_native_runtime.py` produces a standalone transient runtime archive
from the locked Cargo workspace. Root VERSION names the application artifact;
member Cargo.toml names its internal crate version. Contracts and licenses travel
with the binary; deterministic member hashes and installed native checks guard
integrity and execution. Alternatives: shipping a checkout carries Python/build
state; relying on a builder's PATH hides runtime dependencies. The archive needs
GNU/Linux loader/libc/libgcc compatibility, not Python or Rust tools. CI retains
an artifact without release publication or production backend selection.

Native general execution — Problem: host run counts, executor job/byte windows
and composition estimates refused independent native work. Decision: eligible
host runs and native general jobs start on demand; the launch probe sizes idle
reuse, and aggregate RAII records are observations retained through joined close.
Physical startup/allocation failure, accounting overflow, principal binding,
per-round authorization and wire/image bounds keep their authority. Excess
native workers retire after work, and finished handles join before reuse;
close joins every accepted job before releasing composition observations.
Alternatives: larger windows preserve refusal; detached jobs lose receipts;
removing model/approval budgets would grant new authority. Tests rendezvous 32
real native workers with reuse hint 1, retain every receipt and retire all workers;
16 concurrent pool constructors remain eligible even with unknown headroom.
Measurement: 32 controlled Rust jobs with reuse hint 1 rendezvoused and joined
in 1.70 ms with zero remaining workers; this is execution ownership, not
provider throughput.
Native tool scheduling — Problem: a fixed private Rayon pool queued eligible
reads and local batch count/byte windows refused valid calls before execution.
Decision: every explicitly classified read starts a scoped worker; a publication
gate opens only after all workers start. A physical startup failure withdraws
unstarted work, wakes and joins already-created workers, and returns WorkerStart
before any handler effect. Ordered writes retain source order. Legacy count/byte
fields are compatibility observations, not admission limits; checked arithmetic,
authorization, parent/epoch binding and tool argument contracts remain. Dispatch
joins workers before releasing borrowed inputs or batch accounting; concurrent
close waits for that join and releases composition observations before returning.
Alternatives: larger fixed pools retain queues; unscoped detached threads lose
borrowed-input and teardown ownership; parallelizing all reads grants unsafe
overlap. Tests retain cancellation, required-cleanup failure, callback reentrancy
and joined-close contracts. Measurement: 32 real read handlers with all three
legacy hints set to 1 rendezvoused in 1.66 ms, retained sorted results and all 32
resource receipts, then left zero workers. This measures controlled scheduling,
not external service throughput.

Shared optional library admission — Problem: token counting and Flow control
use the same release member but independently inspected the archive and copied
its library. Decision: `NativeArchiveImage` lazily admits each selected library
member once, retaining only its declared ABI names and sealed descriptor. Each
capability owns its descriptor, ABI validation, call leases and unload; host
shutdown joins acquisition and releases all owners. Missing ABI declarations
still refuse, and independent installed-library pins keep their existing path.
Alternatives rejected: an eager all-member cache taxes unused capabilities;
a process-global cache loses application lifetime; sharing a bound ABI object
would entangle independent call/graph teardown. The executable still has its
own sealed image and complete self checks. Measurement: 400 fresh processes,
100 alternating pairs per loading order, execute identical real token/Flow work.
Component CPU means fall 27.0–27.6%, cold wall means 15.9–18.1%, and full-process
CPU means 21.8–22.3%. Unique sealed library backing decreases from 12,279,936
to 6,139,968 bytes; parent RSS high water decreases 1.7–1.8%. These byte sizes
are not physical peak PSS, and the component result admits no system defaults.
See raw identities, ratios and retained limitations.
Transient SDK history completes on its joined worker before resource retirement;
see [SDK project retirement](DEVELOPER_RUNTIME.md) for the lifetime boundary.

## Owned artifacts

| Source owner | Distribution | Runtime role | Version authority |
|---|---|---|---|
| repository root, `tofu_agent/`, selected `lib/` | `tofu-agent` | application source composition plus embeddable/headless agent | root `pyproject.toml` and `VERSION` |
| `packages/tofu-db/` | `tofu-db` | certified, selectable personal storage engine (not the default backend) | member `Cargo.toml` |
| `packages/tofu-search/` | `tofu-search` | standalone, lazily loaded search/fetch capability | member `pyproject.toml` |
| `packages/tofu-repo-kernel/` | `tofu-repo-kernel` | optional standalone Rust CSR ranking; lazy in-process C ABI | member `pyproject.toml` |
| `packages/tofu-runtime/` | `tofu-native` archive | transient native GNU/Linux agent/stdin composition | root `VERSION`; member crate `Cargo.toml` |
| `packages/tofu-overleaf-mcp/` | `tofu-overleaf-mcp` | optional isolated Overleaf artifact producer | package `pyproject.toml` |
| `packages/tofu-environments/` | `tofu-environments` | optional generic image/workspace SDK and native plugin | package `pyproject.toml` |
| `plugins/tofu-trading/` | `tofu-trading` | optional application plugin discovered through entry points | member `pyproject.toml` |

The root application may consume the public `tofu-search` API and provider
seams. `tofu-search` never imports host modules. The host discovers
`tofu-trading`; core modules never import `tofu_trading`. Trading uses declared
host runtime/storage seams and owns durable records only in `tofu.trading`.

`tofu-overleaf-mcp` is excluded from the shared uv workspace: its pinned upstream
server and protocol SDK resolve in its own process environment. Install the
package by path to obtain the `tofu-overleaf-mcp` console entry point. It imports
no host modules; its packaged paper schema is generated from the root contract.
The application launcher resolves this owned source through the shared isolated
MCP runner. External vendoring preserves it in place and never creates a second
copy under `tools/`.

The root Cargo workspace owns `packages/tofu-runtime/` and the native
`tofu-native` entry point. `cargo test --workspace` verifies this new runtime
boundary. Existing Tofu-DB and experimental native crates remain explicitly
excluded so their manifests, lockfiles and certification sources stay separate.
Problem: Cargo can skip an excluding workspace and discover an older cached
ancestor during restart preparation. Decision: Tofu-DB declares its own empty
`[workspace]`, retaining its lockfile and release profile in both the checkout
and source snapshot. Its sibling dependencies retain their own workspace
inheritance. Alternatives rejected: deleting ancestor manifests can affect
unowned files; rewriting only the staged manifest breaks source attestation.
Real Cargo workspace-discovery regressions cover clean and legacy cache layouts
in `tests/server/test_native_engine_root_inputs.py`.
The runtime foundation is a development artifact; its [guide](../packages/tofu-runtime/README.md)
names the implemented physical executor and remaining domain integrations.

## Workspace and verification

`tofu-repo-kernel` builds independently of the shared uv workspace. Source builds
require Rust; platform-wheel consumers do not. Its standard-library-only C ABI
loads on the first repository-ranking call and retains one immutable binding.
The application prefers a verified installed wheel and preserves NetworkX when
the component is absent or invalid. Graphs and Rust buffers are call-local.
Build and install a wheel into an isolated target, then run the native graph
oracle and `tests/repo_map/test_native_rank.py` with that target on `PYTHONPATH`.
Docker builds compile this standalone wheel once and install it into all three
role environments. Compiler tools stay in the builder; final images carry only
the wheel and `libgcc-s1`. `python -m tofu_repo_kernel` certifies the installed
artifact without a checkout; platform-image release gates also require the
application's lazy selector to activate the native backend.

`uv sync --all-packages` resolves first-party dependencies from the workspace.
Published metadata continues to contain normal version ranges, so every wheel
remains independently installable. Run the smallest member tests first:

```text
uv run --package tofu-search pytest packages/tofu-search/tests
uv run --package tofu-trading pytest plugins/tofu-trading/tests
python3 scripts/check_monorepo.py
```

Release/build commands in Makefile, package scripts, CI workflows and the
contract catalog must reference tracked source files, including local JavaScript
helpers imported by those entrypoints and `scripts.*` imports of tracked Python
scripts and retained tests, including generated protocol clients. `check_monorepo.py` checks that closure;
an ignored file left on a developer machine cannot satisfy a release gate.
The scripts allow-list in `.gitignore` records these retained owners.

Root gates then verify the integration adapters and full application. A source
change that affects a public member contract updates producer, consumer,
compatibility test, and version range together.

`tofu-db` remains outside application assembly and release assets while it is
pre-authority.  Its focused gate is
`cargo test --manifest-path packages/tofu-db/Cargo.toml`; promotion adds native
build artifacts and application compatibility gates in the same change.

## Releases

Desktop controlled-computer assets use **Tofu Remote** (`TofuRemote-*`);
`tofu-agent` retains its AI runtime SDK identity. OS registration, configuration
and pairing identifiers stay stable through upgrades. Native release jobs cover
Windows x64, macOS x64/ARM64 and Linux x64/ARM64, with frozen startup checks.
Python wheelhouses use committed `uv.lock` constraints and empty-environment
import/PDF/WASM verification. TofuDB daemon archives are built from locked Cargo
inputs and smoke-tested natively (musl-static on Linux). Archives and SHA-256
sidecars join the required-asset gate and signed release checksum manifest;
a source installer consumes verified caches/bundles before local preparation.

Problem: controlled devices and AI runtimes shared a product name, while user
installs repeated artifact preparation. Decision: separate names and prepare
verified artifacts once per release/platform. Alternatives: changing only labels
leaves ambiguous filenames; caching inside fresh environments loses reuse;
skipping native smoke would certify the builder's environment rather than the
shipped dependency closure. Platform certification remains a real CI result.

Frozen desktop contracts — Problem: a partial schema list let installers build
but fail at startup when a runtime loader opened an omitted authority. Decision:
the full desktop application bundles the complete exported `contracts/` directory
at its runtime resource root. Alternatives: adding only the first missing schema
leaves neighboring loaders broken; embedding copies creates schema drift.
The macOS job has a bounded 45-minute budget: a measured cold Intel build
reached the former 30-minute limit while assembling the agent after the full
app smoke passed. That bound covers both installers without allowing an
unbounded build.

Use `search-vX.Y.Z` and `trading-vX.Y.Z` for workspace members. The established
agent release contract retains `vX.Y.Z` until its OCI/SDK promotion workflow is
versioned together. A tag builds only its owning member plus shared compatibility
gates. GitHub's `rangehow/ToFu` repository owns issues and development; retired
standalone repositories remain read-only historical pointers and never accept
new source changes.

Public runtime export — Problem: sanitization removes maintainer controls and
changes retained inputs, leaving composed runtime and action outputs stale.
Decision: remove the now-unused maintainer helper from public source and invoke
the owned generators against sanitized inputs before publication verification.
The same keep-list derives public Git ignore exceptions, and staged-byte
verification checks test imports against the exact tree that will be committed.
Alternatives: publishing private controls widens the release tier; exempting
generated drift leaves the shipped graph inconsistent with its sources.

## Checkout path

The canonical checkout basename is `tofu`, not `chatui`. Runtime code derives
the repository root and never embeds a developer-machine absolute path.
Project-local `.tofu/` and legacy `.chatui/` state move with the checkout and
remain untracked. When an existing checkout moves, repair linked worktrees and
use the public `project.relink` operation to atomically move recent-project,
active/recoverable conversation project-pin, and Project Brain identity.
The maintenance-lane command has a two-minute hard deadline, stops its owner-scoped
conversation scan at 10,000 candidates, and applies verified rewrites through
one backend batch per physical table.
Historical fixtures may retain `chatui` as sample data;
they are not launch paths or source authorities.

Active product copy and new identifiers use **Tofu**. Existing `.chatui/`,
`CHATUI_*`, `chatui_*`, database filenames, and explicitly legacy adapter names
remain only as compatibility surfaces; removing them would strand existing
installations. Their presence is not evidence of a second product or checkout.

After the filesystem move, run the bounded state migration in dry-run and
apply modes, repair Git's linked-worktree metadata, resync the workspace, and
replace the lifecycle recovery entry. The migration also updates a
checkout-local `.tofu_env.json`; if that marker deliberately selects an
external Conda environment, keep it only when that environment does not carry
editable installs from the retired sibling repositories.

```text
python3 scripts/migrate_checkout_path.py --old-root /old/chatui --new-root /new/tofu
python3 scripts/migrate_checkout_path.py --old-root /old/chatui --new-root /new/tofu --apply
git worktree repair
uv sync --all-packages --extra test
./.venv/bin/python serverctl.py install-recovery
```

`tofu-environments` resolves independently of the shared uv workspace. The core
wheel does not depend on it. Its provider-neutral contract and extension entry
point are owned by [ENVIRONMENT_PROVIDERS.md](ENVIRONMENT_PROVIDERS.md); company
adapters are independently installed release-tiered bundles.

Public exports retain benchmark helpers imported by shipped tests and build
generators referenced by package scripts. Remaining benchmark experiments and
maintainer diagnostics stay private through the export keeper registry.
Public exports retain the frontend build checks and generators referenced by `package.json`;
`tests/desktop/test_desktop_release_prerequisites.py` verifies their inclusion.

`tofu_workers/` ships with the Python wheel under the existing `tofu*` package rule. It owns the standard-library-only demand thread lifecycle used by the agent adapter and independent copy helper; it has no storage/configuration imports.


Standalone search worker ownership — Problem: fixed library pools and MCP's
four-call limiter queued independent requests, while importing the app worker
adapter would break standalone installation. Decision: the standard-library
`tofu_workers/executor.py` also owns the standalone facade; a deterministic
generator emits a private, namespaced copy inside `tofu-search`, checked for
byte drift and shipped with the existing MIT notice. There is one maintained
implementation and no runtime path outside the package. All eligible engine,
fetch, filter, vertical and verification calls start on demand; legacy numbers
size idle reuse and MCP health reports zero admission quota. Physical startup
failure produces that item's failed Future without losing accepted siblings.
MCP copies request ContextVars and retains physical ownership through repeated
cancellation; transport teardown joins published calls. Result targets, network
deadlines, engine throttles, permission and supplied model budgets retain their
owners. URL text extraction also retains all valid links instead of the first
five. Alternatives: larger pools retain queues; host imports burden standalone
startup; a separately maintained worker drifts; detached cancellation loses
ownership. Controlled evidence: 32 fetch handlers rendezvoused and joined in
7.99 ms with zero workers remaining, including separate startup-fault and
32-request MCP identity tests. This measures scheduling, not network throughput.

Standalone non-daemon pools register one weak interpreter-exit owner before
threading joins, so persistent MCP/proxy pools wake and drain during exit.
Idle carriers clear completed inputs/outcomes; internal cancel callbacks use
weak job/executor references, preserving pre-entry cancellation without pinning
request data or requiring cyclic GC. Alternatives: daemonizing loses accepted
work; retaining strong callback cycles leaks payloads; clearing caller results
breaks receipts. Real subprocess exit, GC-disabled payload reclamation and
existing cancellation/initializer/callback tests verify this boundary.


Trading read ownership — Problem: fixed pools and a max-workers-one serial
branch queued independent data reads; sleeping after completed intel receipts
added latency without pacing requests already sent. Decision: generate the
same stdlib worker kernel privately into `tofu-trading` and use it for market,
NAV, news, source, screening and intel fan-out. Legacy worker settings only size
idle reuse, and receipt delay is retired; real endpoint rate policy retains its
client owner. Each intel worker acquires and closes a separate repository handle
with the explicit caller owner. Physical startup failure preserves every query
position and joins accepted siblings. Trade authorization, risk, idempotency,
user result limits and settlement keep their authority. Alternatives: larger
pools retain queues; shared caller connections violate transaction ownership;
private worker implementations drift; post-result sleep does not throttle an
endpoint. Controlled measurement: 32 read workers rendezvoused and joined in
7.23 ms, with zero remaining workers; a failed eighth start still retains 31
owned query completions and closes all 31 handles. This measures scheduling,
not data-provider throughput. Verify plugin demand/identity/transaction tests,
generator drift and installed-wheel execution.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

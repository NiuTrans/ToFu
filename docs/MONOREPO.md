# Monorepo and release boundaries

This repository is the only source authority for the Tofu application and its
first-party Python and Rust artifacts. Source location, installable package, and runtime
process are separate boundaries: sharing one Git repository does not make an
optional capability part of the core wheel or force it into another process.

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

Frozen desktop contracts — Problem: a partial schema list let installers build
but fail at startup when a runtime loader opened an omitted authority. Decision:
the full desktop application bundles the complete exported `contracts/` directory
at its runtime resource root. Alternatives: adding only the first missing schema
leaves neighboring loaders broken; embedding copies creates schema drift.

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

# Environment providers

`packages/tofu-environments/` is an optional, independently installed SDK and
native tool plugin for building images and provisioning workspaces. Its public
contract is `tofu_environments/spec.schema.json`; provider implementations conform
to `tofu_environments.contracts.Provider`. Neither schema nor workflow assumes a
company, cloud, registry or login system. The core agent wheel has no dependency
on this package. Installing it does not start Docker, authenticate or allocate.

## Design decision

**Problem:** image builders, workspace platforms and authentication differ across
installations. A company-specific tool in the core would make public releases
require internal infrastructure; independent scripts would duplicate approval,
ownership and failure recovery.

**Decision:** keep one workflow in a standalone public SDK, a reference Docker
provider in that SDK, and platform implementations in separately installed
packages. Private distributions belong in release-excluded bundles. Discover
providers lazily through `tofu.environment_providers`, using an authenticated actor
and trusted host context. A provider can compose a registry builder and a separate
workspace scheduler without changing the public workflow.

**Alternatives considered:** a universal platform HTTP client cannot normalize
platform-specific identity, quotas and readiness; direct shell skills do not
provide durable claims or exact approval; embedding company adapters in the core
would contaminate public artifacts and add startup dependencies.

## Installation and opt-in

From a source checkout:

```sh
python -m pip install ./packages/tofu-environments
```

The `tofu.tools` entry point is named `environments`. Enable it per authenticated
request with `config.plugins: ["environments"]`, or explicitly include it in the
existing deployment setting `TOFU_DEFAULT_TOOL_PLUGINS`. Preserve any other
allow-listed plugins. Restart an already running host through its normal lifecycle
so its import-time tool catalog sees the installed entry point. No separate UI
or MCP server is required. Plugin execution remains trusted in-process code;
visibility isolation is not a sandbox.

| Tool | Behavior |
|---|---|
| `environment_providers` | Installed provider names; no daemon or authentication |
| `environment_catalog` | Available image references/revisions, queues and constraints |
| `environment_plan` | Validate explicit resources and persist an immutable preview |
| `environment_apply` | Advance one approved write stage |
| `environment_status` | Read progress or reconcile an uncertain result |
| `compute_plan` | Seal an explicit bounded batch job |
| `compute_submit` | Submit one exact approved job without replay |
| `compute_status` | Read exit status or reconcile uncertain submission |
| `compute_cancel` | Request exact authorized owned-job cancellation |
| `compute_collect` | Collect successful owned outputs into the active project |

Profiles are `tofu_service`, `project_development` and `project_training`. CPU,
memory in GiB, owned project and build instructions are explicit. Service mode
requires an application readiness endpoint. A running container or IDE is not
sufficient evidence that Tofu is ready.

## Ownership, approval and recovery

The host derives owner identity from the authenticated task, never model arguments.
Tenant identity and declared project root accompany every operation. Repository
keys contain the actor scope; all durable calls use the configured semantic storage
client. The package does not select a storage backend or use SQL. The host registers
its declarative `tofu.environments` manifest. `MemoryRepository` is an explicitly
transient embedding option and returns `durable: false`.

Plan hashes bind provider, actor scope, project root, specification and stable
admission evidence. `environment_apply` includes the full preview and validates
its hash against durable state. It consumes a one-use gateway approval receipt
bound to tool name, call ID and exact arguments. Manual approval displays generic
argument risk fields; existing Auto mode policy can authorize writes. The SDK's
`authorize` callback is the equivalent boundary for trusted embeddings, and must
be implemented by the caller's policy rather than unconditionally accepting
untrusted requests.

Before each external write, a CAS claim persists its intent and deadline. A lost
response or checkpoint never triggers automatic replay. Status waits for live
claims, then asks the provider to prove exact ownership and operation markers.
Unprovable outcomes stay uncertain. Reconciliation cannot declare the application
ready: the normal readiness probe must establish that result. There is no implicit
resource deletion or automatic replacement operation.

## Docker reference implementation

Use an immutable `sha256:` image ID or registry `@sha256:` digest from the catalog.
Build instructions extend that base and cannot contain a replacement `FROM`.
The context must lie inside the declared project; symlinks and special files are
rejected. Admission seals content, paths and modes, including ignored files.
Before Docker starts, the provider copies a bounded private context, verifies the
same seal and builds that snapshot. Temporary copies are reclaimed on completion
or failure. Ignored files may require a safe replan because the provider deliberately
does not duplicate Docker's `.dockerignore` semantics.

Local binds must be inside the project. Published ports bind loopback only. GPU
requests require the explicit NVIDIA runtime. Operation/owner labels guard every
readback and reconciliation. Docker remains a trusted configured daemon; it is
not a tenant security boundary. The default CLI runner supports POSIX; other
operating systems can inject a runner with equivalent cancellation/process cleanup.

## Third-party extension

Create a separately installable package declaring:

```toml
[project.entry-points."tofu.environment_providers"]
my_platform = "my_package:provider_factory"
```

`provider_factory(actor, context)` returns an API-v1 provider. The host resolves it
only when requested. Use the reference implementation and lifecycle fault tests
in `packages/tofu-environments/tests/` as executable examples. Implement catalog,
stable admission evidence, image create/build/status, workspace create/activate/
status and read-only reconciliation. Build receives the sealed `admission` evidence.
Do not include volatile queue availability in it; recheck availability before
allocation. For every mutation, distinguish proven rejection from unknown outcome.
Set `EnvironmentError(..., uncertain=True)` for lost acknowledgements and timeouts.

Keep provider credentials out of schemas, plan evidence, connections, logs and
error text. A trusted host can call `configure_provider_context` to supply an
actor-aware credential source. Providers may use their own declared trusted
configuration repository; credential sources must enforce owner/tenant binding
before accessing credentials. Never infer an account from a project name or use
an ambient personal login for arbitrary owners. Connections contain safe URLs
and identifiers, not tokens. Vendor options are isolated in `provider_options`
and validated by that provider. Unsupported options fail rather than disappear.

## Capacity and observability

The installed-wheel smoke measurement on CPython 3.12 was approximately 58 ms
for importing the SDK, with zero additional worker threads. This is an SDK import
sample, not an application startup benchmark. No persistent worker, polling
thread or daemon is added. The host reuses its
cached launch resource probe and derives output/context bounds from available
memory and volume headroom, and rechecks the staging volume before copying.
Explicit trusted runner/provider overrides remain. Standalone SDK defaults are
lean bounded fallbacks. Commands have deadlines, bounded output, process-group
teardown and cancellation. HTTP adapters must enforce verified TLS, bounded
responses/deadlines and no credential-bearing redirects.

The workflow logger `tofu.environments` records stage claims, settled phases,
status transitions, operation IDs, actor-scope hashes and safe error codes. It
never logs recipes or credentials. Durable records retain external IDs and claim
state for incident diagnosis; no state is silently evicted. Transient embedding
repositories reject new operations when their configured capacity is exhausted.

Focused validation:

```sh
PYTHONPATH=packages/tofu-environments:. python -m pytest packages/tofu-environments/tests
```

These tests include real host storage restart/CAS, exact approval receipts,
concurrent claims, checkpoint failures, process timeout and changed-context rejection.
Platform fixtures establish adapter contracts; a deployment still needs acceptance
against its own credentials, queues, image admission and readiness route.

## Batch compute lifecycle (embedding API)

**Problem:** provisioning a workspace does not execute a bounded experiment or
prove its exit status. GPU jobs need a separate provider lifecycle with the same
ownership, exact authorization and durable no-replay guarantees.

**Decision:** `jobs.JobService` reuses the existing scoped CAS repository and
canonical plan seals. `job.schema.json` defines source digest/URI, argv,
resources, runtime, total GPU-seconds and result-byte limits, expected outputs
and isolated provider options. Job keys have a distinct `job-` prefix.
`JobProvider` owns admission, source verification, remote limit enforcement,
submission, exact-operation reconciliation and status readback. References have
explicit `kind` and `id`; adapters must not interchange scheduler/app/run IDs.
Success requires a proven zero exit code. No scheduler or polling thread is added.
Record size retains the existing 256 KiB bound and storage lifecycle.

**Alternatives considered:** reusing workspace readiness would conflate a running
container with completed experiment output; direct CLI submission would lose
scoped recovery and uncertain-write protection.

Native compute tools are part of the opt-in environments plugin.
`environment_providers` also lists installed `tofu.compute_providers` entries
without loading them. Factories accept `(actor, context)` and return a
`JobProvider`; trusted actor-aware credentials use the same host context as
environment providers. `compute_submit` consumes the existing gateway one-use
receipt bound to the full displayed plan, call ID and exact arguments.
No provider is installed implicitly. This implementation does
not yet have configured Hope host ports or live GPU acceptance.
`JobService.collect` requires proven job success and streams declared outputs
into a private new directory under a trusted destination. Total collected bytes
are bounded by the plan, each file is SHA256-sealed, and receipts are checkpointed
through scoped CAS. Changed terminal outputs fail against earlier receipts.
Failed reads or checkpoints remove local partial files. Collection is read-only
at the provider and adds no persistent polling worker. `read_job_output` adapters
remain responsible for exact owned-job binding, deadlines and remote transport. Declared cost bounds must be enforced by each real adapter; schema
validation alone does not enforce remote cost or seal a source file.

The private `bundles/meituan/environment-provider` distribution now declares a
`hope` compute entry point. Its adapter uses explicit actor-bound host ports for
MCP calls, sealed job preparation, exact-operation/exit evidence and output
reads. It disables transport replay and quota retries on submission, preserves
Hope run ID type, and rejects unconfigured ports. Host composition and live
acceptance remain outstanding; installing the package alone cannot allocate.
The SDK contains no Hope hosts, login mechanism or platform credentials.

The public POSIX `job_runner` entrypoint executes real argv under a wall deadline
and reclaims its process group, including descendants that retain output pipes.
Captured stdout/stderr and declared result reads share the approved result-byte
bound. File hashes and the real exit code are emitted as a machine-readable
receipt. This does not enforce scheduler resources or temporary disk use, and
is not an untrusted-code sandbox; platform preparation must enforce allocation
and bind source/operation identity before declaring admission.

Cancellation has its own durable CAS claim. Unknown stop outcomes remain
`cancel_uncertain`; queued/running status cannot erase that claim and permit
replay. Only provider terminal readback settles it. Native `compute_cancel`
uses an exact displayed-plan gateway receipt. The private Hope adapter first
proves the owned operation, obtains the platform dry-run confirmation, then
consumes it once with transport replay disabled. Confirmation tokens are not
persisted in job records. Deployment must still arrange deadline monitoring;
this primitive does not create an automatic watchdog.

## Batch source archive boundary

**Problem:** convenient compute submission must not let a source archive escape
its owned project or staging directory, change after hashing, or exhaust storage
through unbounded expansion.

**Decision:** `tofu_environments.job_sources.extract_job_source` accepts an owned
local ZIP plus pinned SHA-256 and caller-provided byte/entry limits. It reads a
private unlinked snapshot and extracts regular files into the caller's empty
private staging directory. The caller retains ownership of staging cleanup;
this helper adds no persistent cache, daemon or repository authority. Capacity
bounds come from the existing host context probe. Snapshot plus expanded inputs
need transient disk capacity; the caller must reserve it before preparation.

**Alternatives considered:** direct `extractall` does not enforce this contract;
reopening a hashed mutable archive leaves a race. A module-owned indefinite
cache would add persistent growth and another lifecycle authority. This source
primitive does not replace provider resource validation or runtime evidence.

Verification includes source mutation after hashing, compressed expansion,
archive paths and symlinks, owner/project containment, and exact file receipts.

### Native result delivery

**Problem:** the embedding API could collect results, but a Tofu task could not
finish its compute cycle through the native tool surface.

**Decision:** expose `compute_collect(operation_id)` through the existing plugin.
The host chooses a fresh mode-0700 directory inside the explicit actor project;
model arguments cannot select destinations or remote paths. Existing JobService
ownership, proven success, byte bounds, CAS and stable SHA256 receipts remain
the authority. Failed staging is removed; successful artifacts are durable user
results and are retained until the user/project lifecycle removes them. No
resident worker/cache is added; retained bytes per collection are bounded by
the approved result limit and the returned receipt reports actual bytes.

**Alternatives considered:** requiring a separate Python script leaves the agent
cycle incomplete; arbitrary destination arguments introduce an unnecessary file
write authority. Collection reads an existing job and never allocates compute,
so it does not consume a new external submission/cancellation approval. The
provider must still implement bounded owned output transport.

## Per-call provider composition lifecycle

Problem: an installed compute entry point still needs the selected repository,
and temporary source/MCP ports must not leak across tool calls. Decision: the
host passes its existing `HostRepository` as trusted `context.repository` and
an ExitStack callback registrar as `context.register_cleanup`. The registrar's
lifetime is exactly one native tool handler call, including failure paths; cleanup
runs after provider calls settle. Third-party providers register only resources
they own. Explicitly injected services remain owned by their embedding and are
not closed by the host. Cleanup failure stays visible; durable job claims remain
authoritative and never become permission to resubmit. No resident pool, cache or
persistent data is added. Existing capacity-derived staging bounds still apply.

Alternatives: process-global connections obscure owner/project lifetimes;
unconditional closure of an injected service can interrupt other tasks;
constructing another repository inside a vendor provider bypasses the selected
storage authority. None is used. Installed providers still require trusted account
and artifact bindings; these lifecycle ports alone do not activate Hope.

## Bounded scheduler preemption recovery

**Problem:** an owned scheduler can evict a running Worker and replace it even
when application failover is disabled. Treating every replacement as duplicated
submission prevents long jobs from completing on preemptible capacity.

**Decision:** `job.schema.json` permits an explicit `recovery` declaration:
`{"max_attempts": 3, "restart_safe": true, "allowed_reason": "scheduler_preemption"}`.
Omitting it preserves one-attempt admission. `runtime_seconds` remains a per-attempt
execution bound; `gpu_seconds` must cover GPU count times that bound times all
allowed attempts. The opt-in is an assertion that restarting the command cannot
repeat unsafe external effects. It does not create another submission or retry an
uncertain write.

Hope accepts only unique, sequential Worker identities with closed earlier
attempts and the platform's exact scheduler-preemption diagnostic. Unknown
failures, overlapping workers, excessive attempts and observed allocation spans
above the declared GPU budget fail admission. Every admitted Worker allocation
must match the sealed resources; final output still requires the existing zero
exit, owned publication and full size/SHA checks. A preempted Worker awaiting a
replacement remains queued while the attempt allowance remains.

**Alternatives considered:** admitting arbitrary failed attempts would hide
application errors and replay side effects; insisting on one Worker would reject
normal scheduler recovery; resubmitting after a lost reply could duplicate work.

The cluster may override its configured retry count for infrastructure eviction.
An embedding must keep an owned status watch and cancel the exact operation if
attempt admission or allocation bounds fail. Cancellation proves the durable
owner/reference and platform identity independently of execution admission, so a
rejected execution remains stoppable. Closing a failed operation retains its
prior exit/error evidence in `execution_failure_before_cancellation`.
Definitively rejected submissions without an external reference retain their
failed state and produce no cancellation request. The policy is an admission and
supervision contract, not a cluster reservation or a guarantee that startup and
scheduler teardown cannot exceed a limit. Allocation durations are capacity-time
evidence, not a billing receipt. Live preemption recovery must be demonstrated
separately from fixture tests.

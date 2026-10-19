# Environment providers

`packages/tofu-environments/` is an optional, independently installed SDK and
MCP service and native compatibility adapters for image/workspace provisioning
and remote batch compute. Its public
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

Platform integration uses the independently installed stdio MCP entry point:

```sh
python -m pip install './packages/tofu-environments[mcp]'
tofu-environments-mcp --factory deployment.environments:binding
```

The trusted deployment factory returns `mcp_adapter.MCPBinding(actor,
environment_service=..., job_service=..., authorize=..., close=...)`. Actor,
project, repository and provider resolvers come from the deployment's identity,
storage and credential owners, never model arguments or an ambient login.
The binding uses the existing `EnvironmentService` and `JobService`; it adds no
journal, submission loop, provider or default resource allocation. A configured
service contributes only its tool family. Shared schemas live in `schemas.py`.
The `authorize(actor, tool_name, exact_arguments)` policy must consume a one-use
approved grant for that exact preview; absent policy refuses every external
write. MCP connection or a valid schema is never authorization. Server shutdown
calls the optional owned cleanup port. Blocking SDK calls borrow MCP's worker
boundary and cancellation joins them rather than replaying a lost write.

`compute_wait` remains a host Evolution dependency adapter, not a platform MCP
operation. `compute_collect` uses only the trusted bound project destination;
model arguments cannot select another root. Ownership, CAS claims, unknown
outcome reconciliation, exit-code evidence and output seals remain SDK-owned.

The native `tofu.tools` compatibility entry points are separately named
`environments` and `compute`. Enable only the needed adapter via `config.plugins`.
Without configured services or trusted `environment_provider_ids` /
`compute_provider_ids` in `configure_provider_context`, only the native provider
discovery tool is offered. Installing a provider is not proof of usable actor
credentials. `compute_wait` also requires an executing Evolution run; native
collection requires an attached project. No deployment-wide opt-in is added.
The SDK package and existing private Meituan providers remain optional and
release-separated. A deployment still owns live platform acceptance.

| Tool | Behavior |
|---|---|
| `environment_providers` | Installed provider names; no daemon or authentication |
| `environment_catalog` | Available image references/revisions, queues and constraints |
| `environment_plan` | Validate explicit resources and persist an immutable preview |
| `environment_apply` | Advance one approved write stage |
| `environment_status` | Read progress or reconcile an uncertain result |
| `compute_catalog` | Read actor-bound compute queues, resource snapshots and option schemas; no allocation |
| `compute_plan` | Seal an explicit bounded batch job |
| `compute_submit` | Submit one exact approved job without replay |
| `compute_status` | Read exit status or reconcile uncertain submission |
| `compute_wait` | Park an owned Evolution compute dependency; existing reaper resumes its goal at terminal status without model polling |
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
thread or daemon is added. A fresh CPython 3.12 / MCP 2.0 process measured
server construction at approximately 1.45 s with one thread before and after;
this optional SDK import sample is not an application startup benchmark.
Descriptor registration imports no MCP SDK; calls borrow its existing worker
boundary rather than adding a Tofu pool. The host reuses its
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

Native compute tools use their own opt-in `compute` plugin.
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
Status proof and output receipt selection both use `select_worker_attempt`;
collection selects the admitted final Worker after the same full history and
resource checks, rather than imposing an extra one-Worker-only gate.
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

## Benchmark batch launcher

`evaluations/platform_jobs/` packages the committed Harbor runner through the
public JobService contract. Its request schema pins dataset/task/engine/source
identity and budgets; Git object proofs validate exported source without .git.
The worker runs doctor before model dispatch and retains failed trial evidence.
Wrapper success permits diagnostic publication; benchmark state, child exit and
formal audit remain separate, with no inferred score. Scope, exact-plan approval,
uncertain-write reconciliation and output ownership stay in JobService/provider
ports. See [the launcher contract](../evaluations/platform_jobs/README.md).

CLI transport and source snapshots use private named temporary files, removed
on close. Unlinked temporary files failed writes/flushes on the observed FUSE
volume; snapshots remain pinned through extraction and cannot be reread from
mutable original archives. Subprocess deadlines and group teardown are unchanged.
Hope packages sealed top-level entries within the platform's 200-entry limit;
the recursive file manifest is hashed incrementally under admitted source bounds.
Links and special files in cached staging fail before packaging. This supports
large retained runners without weakening per-file source seals.

### Compute discovery

**Problem:** compute planning required queue and provider-option knowledge that
was unavailable through the public tools. **Decision:** `compute_catalog` uses
`compute_catalog.schema.json` and an optional provider `compute_catalog(actor)`
capability. Unsupported providers return `catalog_unsupported`; catalogs reject
wrong provider identity, nonfinite values and payloads above 64 KiB. Catalog
reads neither mutate the operation repository nor allocate resources.
Hope queries only the trusted actor/project-bound queue with `include_apps=false`,
projects resource counts and safe option defaults, and excludes image, credential,
application and environment data. Capacity is a transient snapshot, never a
sealed plan admission promise. **Alternatives considered:** raw MCP responses
would expose unnecessary fields; hardcoded queue defaults would bind the public
SDK to one company/user and become stale. No resident worker or cache is added.

## Durable compute dependency

Problem: raw platform wait tools may be hidden or time out, and an executor can
finish after an intermediate queue snapshot. Decision: `compute_wait` validates
a durable submitted actor/project operation and enrolls one pointer in the
Evolution run. The shared terminal marker parks the carrier; the existing reaper
observes that same operation no more often than every five seconds, retaining
transient observation errors across application restarts. No worker or timer is
added. The pointer is bounded by one operation identity and retry timestamp per
run, not by poll history. Provider job records remain the sole job authority.

Terminal status authorizes one continuation carrier under CAS, keeping the same
logical episode, attempt and cumulative budget; original task receipts retain
lineage and charges. A successful status must include proven zero exit. It does
not grant artifact acceptance or scientific success. Cancellation stops waiting,
without cancelling or resubmitting the remote job. The original failed/cancelled
status is delivered for analysis. No optional SDK import happens without an
enrolled dependency. Alternatives rejected: model polling grows context/cost;
platform-specific wait names do not provide a general interface; a new polling
daemon would duplicate scheduling and lifecycle authority. Verify
`test_evolution_compute_wait.py` and the optional SDK host tests.

### Goal creation in the browser

Problem: installed compute tools were reachable through API launches but absent
from the studio briefing. Decision: briefing settings accept a project directory
and one installed execution plugin from the authenticated capability catalog.
Omitting the selector inherits deployment defaults; choosing no optional plugins
sends an explicit empty list. Failed reads expose retry and preserve input.
Cluster credentials, quotas and compute limits remain server-owned. Alternatives:
a Hope-specific switch would couple the public UI to one private provider;
automatic opt-in would change task authority without explicit user selection.

## Native capacity views

The optional SDK host exposes `configured_compute_providers()` and
`read_compute_catalog(actor, provider, cancelled=...)` as read ports. They own
trusted composition, request cancellation and transport cleanup. Application
presenters never access host globals or construct provider credentials.
`capacity.queue_resources` is the shared pure queue-summary projection; strict
provider admission still rejects incomplete required quantities, while native
resource views may display unknown values explicitly.

An optional owner-bound MCP adapter admits only registered, enabled tools marked
read-only, with exact `compute_catalog` or `get_queue_resource` names. It validates
the registered input schema, excludes applications from queue reads, disables
transport replay and rechecks capability before calling. Provider discovery uses
explicit configured IDs/schema enums; raw queue selection requires its exact
name. No arbitrary MCP operation or model-authored endpoint is accepted.

# tofu-environments

Optional provider-neutral environment/batch SDK with a separate MCP service. Install from
this source directory with `python -m pip install .`. No provider is contacted at
import, and the SDK works without importing Tofu host modules.

The schema in `tofu_environments/spec.schema.json` is the API-v1 authority.
`contracts.Provider` is the extension interface; `docker.DockerProvider` is the
reference implementation. Native compatibility adapters opt in separately as
`environments` and `compute`; installing a provider does not enable either.
`schemas.py` defines shared tool arguments, and `tofu.environment_providers` /
`tofu.compute_providers` discover independently installed platform adapters.
See the checkout's `docs/ENVIRONMENT_PROVIDERS.md` for host enablement, ownership,
capacity and design decisions.

For platform tools, install `python -m pip install '.[mcp]'` and connect
`tofu-environments-mcp --factory deployment.environments:binding` as a stdio MCP.
The trusted factory returns `mcp_adapter.MCPBinding` with an explicit actor,
existing SDK services, a one-use exact-write authorizer and owned cleanup.
Absent services contribute no tools; absent authorization refuses external
writes. `compute_wait` stays with the host goal runtime. Connections do not
implicitly choose credentials, storage or resource allocations.

An embedding can compose the SDK with its own identity, authorization, storage
and transport. Example using a transient repository (no restart recovery):

```python
from tofu_environments import Actor, EnvironmentService, MemoryRepository
from tofu_environments.docker import DockerProvider

actor = Actor(owner_id=42, tenant_id="example", project_root="/srv/projects/demo")
service = EnvironmentService(MemoryRepository(), lambda actor, provider: DockerProvider())
spec = {
    "profile": "project_development", "project": "demo",
    "image": {
        "base": "ubuntu@sha256:" + "a" * 64,  # replace with an actual approved digest
        "name": "demo", "instructions": "RUN python3 --version", "context": ".",
    },
    "workspace": {"name": "demo", "cpu": 2, "memory_gib": 4},
}
plan = service.plan(actor, "docker", spec, request_id="demo-build-1")
# Present plan["plan"] through the application's authorization boundary.
# Then call service.apply(actor, operation_id, plan_hash, authorize,
#                         approved_plan=plan["plan"]).
# Follow next_action: apply advances one write, status reads/reconciles.
```

Supply a durable repository implementing scoped `get` and CAS `put` for restart
recovery. `HostRepository` uses Tofu's configured semantic storage. The SDK never
chooses a database. Provider exceptions must not contain credentials. Transports
are injectable; mutations with uncertain outcomes must not be retried blindly.

Development tests: `PYTHONPATH=.:../.. python -m pytest tests` from this package,
or use the repository-root command in the design guide. Host integration tests
require the checkout and its normal test dependencies; the workflow/Docker tests
are usable independently. The default Docker CLI transport currently supports
POSIX hosts; inject an equivalent runner for other operating systems.

Batch jobs have a separate embedding API: `jobs.JobService`,
`jobs.JobProvider`, and `job.schema.json`. They reuse the scoped CAS repository,
not the workspace state machine. Plans bind an immutable source SHA256, argv,
resources, runtime/GPU-seconds/result bounds, expected outputs and provider
options. Providers must verify the actual source seal, enforce limits remotely,
and validate all provider options before allocation. The schema is a declaration,
not an enforcement mechanism. `submit` requires exact plan authorization;
`status` is read-only. Typed references keep platform run IDs distinct.
Unknown submission outcomes are reconciled by exact owner/operation evidence,
never resubmitted. A terminal success requires a proven zero exit code.
Native tools expose `compute_plan`, `compute_submit` and `compute_status` through
the existing environments plugin. The private Hope adapter requires trusted host ports; live GPU acceptance
is not yet verified. `compute_cancel` requests a scoped stop under exact plan
authorization. It persists the stop claim before the provider call, never
replays an unknown stop, and requires terminal readback through compute_status. `JobService.collect` streams declared
outputs from an owned successful job into a private directory, checks the total
byte bound, and seals per-file SHA256/size receipts through the CAS repository.
Repeated collection must match those receipts. Providers implement
`read_job_output` with deadlines, owned-job checks and chunks at most 1 MiB;
credentials and presigned URLs must never enter returned receipts. This generic
collection API does not supply a platform transport.

A POSIX experiment entrypoint is available as
`python -m tofu_environments.job_runner job-spec.json --workdir .`.
It runs explicit argv in a process group, applies the runtime deadline, bounds
combined captured stdout/stderr and declared result bytes, hashes result files,
and emits `TOFU_JOB_RESULT=` JSON with the real process exit code. Timeout or
output overflow stops the process group. Missing/unsafe output paths fail.
The runner is for trusted experiment code: it is not a filesystem sandbox,
disk quota, scheduler admission or proof of GPU allocation. Cluster templates
must still enforce CPU/memory/GPU resources and bind the receipt to the exact
owned operation. The wrapper must be included in the sealed job source.

Batch provider input ports may use `job_sources.extract_job_source(actor, source,
destination, byte_limit=..., file_limit=..., reserved=...)` for local ZIP sources.
The source URI must resolve inside the actor's explicit project root and match
its pinned SHA-256. A private unlinked snapshot prevents source mutation between
verification and extraction. Traversal, symlinks, encrypted/special entries,
duplicates, reserved runtime filenames and excessive expansion fail closed.
Bounds come from the host capacity/context policy, not model-provided bypasses.

The caller supplies an empty private staging directory and owns its lifecycle:
remove it after upload or failed preparation; retain durable original artifacts
and receipts separately. The helper selects no provider, persistent cache,
credential store or background worker. This primitive alone does not configure
Hope's evidence/output ports or prove cluster allocation.

`compute_collect(operation_id=...)` exposes the generic collection API to Tofu
tasks. It takes no destination or remote path from model arguments: the host
creates a fresh private directory inside the active actor project. Only proven
successful owned operations can be collected; failed staging is removed.
Successful files are retained user artifacts with CAS-sealed size/hash receipts,
not an automatically evicted cache. This readback does not submit jobs or spend
compute; provider-specific bounded output transport remains required.

Compute provider admission uses `validate_job(actor, spec, *, operation_id)`.
The service derives the scoped operation ID before planning and passes the same
ID on final admission recheck. Providers must adopt this explicit Python
interface; old callbacks are not silently retried under another signature.
This permits per-operation artifact destinations to be included in sealed
admission before authorization, instead of adding unreviewed paths at submit.
The JSON job specification remains version 1; provider admission is still
provider-owned JSON under the canonical plan seal.

Default host provider composition supplies `context['repository']` using the
selected semantic storage and `context['register_cleanup'](callback, *args,
**kwargs)` for per-tool-call resources. A provider can register owned connection
and temporary-source cleanup without installing a process-global cache. Cleanup
runs on success and failure after provider operations settle. A cleanup error
remains visible and cannot authorize replay of an external operation. Injected
services retain their embedding-owned lifecycle; the host does not close them.

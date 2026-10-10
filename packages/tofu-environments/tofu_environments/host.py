"""Optional Tofu entry point. Public SDK modules never import the host.

configure(service) supports trusted application composition. The default
service uses the selected durable storage and lazily resolves provider entry
points; no worker, credential lookup or network request runs at import.
"""
from __future__ import annotations
from importlib.metadata import entry_points
from threading import RLock
from contextlib import ExitStack
import json
from .contracts import Actor, EnvironmentError, seal
from .repository import HostRepository, MANIFEST
from .service import EnvironmentService
from .jobs import JobService

_SERVICE = None
_JOB_SERVICE = None
_PROVIDER_CONTEXT = {}
_LOCK = RLock()

def configure(service):
    global _SERVICE
    with _LOCK: _SERVICE = service

def configure_jobs(service):
    """Trusted embedding injection; never callable through model arguments."""
    global _JOB_SERVICE
    with _LOCK: _JOB_SERVICE = service

def configure_provider_context(**context):
    """Trusted application setup only; never exposed as a model tool.

    Credential sources accept Actor and audience and enforce their own binding.
    No token or callback is accepted from request/model configuration.
    """
    with _LOCK:
        _PROVIDER_CONTEXT.clear()
        _PROVIDER_CONTEXT.update(context)

def _capacity_defaults(observation):
    """Use the host's cached launch memory/volume probe; missing data stays lean."""
    available = max(1, observation.effective_memory_available_mb or 32) * 1048576
    disk = max(1, observation.disk_free_mb or 4) * 1048576
    return {"output_limit": min(67108864, max(65536, available // 128)),
            "context_byte_limit": max(1, min(available * 2, disk // 4)),
            "context_file_limit": max(1, min(1000000, available // 4096, disk // 8192))}

def _default_service(task, *, jobs=False, lifecycle=None):
    from lib.storage import get_storage_client
    from runtime_guards import probe_system_resources
    get_storage_client(write=True).command("plugin.register", {"manifest": MANIFEST},
        "environment-manifest-v1", deadline=15)
    observation = probe_system_resources()
    with _LOCK: context = dict(_PROVIDER_CONTEXT)
    for name, value in _capacity_defaults(observation).items():
        context.setdefault(name, value)
    context["cancelled"] = lambda: bool(task.get('abort') or task.get('_abort'))
    repository = HostRepository(get_storage_client)
    context['repository'] = repository
    if lifecycle is not None:
        context['register_cleanup'] = lifecycle.callback
    def resolve(actor, provider_id):
        if provider_id == "docker" and not jobs:
            from .docker import DockerProvider
            from .command import CommandRunner
            return DockerProvider(CommandRunner(output_limit=context['output_limit'], cancelled=context['cancelled']),
                                  context_byte_limit=context['context_byte_limit'], context_file_limit=context['context_file_limit'])
        group = "tofu.compute_providers" if jobs else "tofu.environment_providers"
        found = [ep for ep in entry_points(group=group) if ep.name == provider_id]
        if len(found) != 1:
            raise EnvironmentError("provider_unavailable", "Provider must be installed explicitly and uniquely")
        return found[0].load()(actor, context)
    return (JobService if jobs else EnvironmentService)(repository, resolve)

def _schemas(_context):
    if _context.owner_user_id <= 0:
        return []
    from pathlib import Path
    spec_schema = json.loads(Path(__file__).with_name("spec.schema.json").read_text())
    spec_schema.pop('$schema', None)
    job_schema = json.loads(Path(__file__).with_name("job.schema.json").read_text())
    job_schema.pop("$schema", None)
    string = {"type": "string", "minLength": 1, "maxLength": 128}
    def function(name, description, properties, required):
        return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}
    schemas = [function("environment_providers", "List installed environment providers without starting a daemon or authenticating.", {}, []),
        function("environment_catalog", "Read available base-image references, revisions, queue names and provider constraints before planning. Authentication is owner-bound; this creates no resource.",
                 {"provider": string, "project": string, "query": {"type": "string", "maxLength": 256}}, ["provider", "project"]),
        function("environment_plan", "Validate and seal a provider-neutral image/workspace plan. Returns exact resource and build scope; creates no external resource. Profiles support deploying Tofu or project development/training. Supply explicit CPU/memory and owned project.",
        {"provider": string, "request_id": string, "spec": spec_schema}, ["provider", "request_id", "spec"]),
        function("environment_apply", "Apply the next stage of an immutable environment plan under exact write approval. May build an image or allocate a workspace; use the plan returned by environment_plan. Repeated or uncertain writes are never blindly resubmitted.",
        {"operation_id": string, "plan_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
         "plan": {"type": "object", "required": ["provider", "spec", "evidence", "project_root", "scope"],
                  "properties": {"provider": string, "spec": spec_schema, "evidence": {"type": "object"},
                                 "project_root": {"type": "string"}, "scope": {"type": "string"}}, "additionalProperties": False}},
         ["operation_id", "plan_hash", "plan"]),
        function("environment_status", "Read image/workspace progress and reconcile uncertain results without creating resources. When next_action=apply, request the approved next stage. Ready requires provider-specific readiness evidence.",
        {"operation_id": string}, ["operation_id"]),
        function("compute_plan", "Validate and seal a bounded batch job with an installed actor-bound compute provider. No resource is allocated. Source identity, command, resources and cost limits are explicit.",
                 {"provider": string, "request_id": string, "spec": job_schema}, ["provider", "request_id", "spec"]),
        function("compute_submit", "Submit the exact approved compute plan once. Uncertain results are reconciled through compute_status; never replay a submission with a new request ID.",
                 {"operation_id": string, "plan_hash": {"type":"string", "pattern":"^[a-f0-9]{64}$"},
                  "plan": {"type":"object", "additionalProperties":False,
                           "required":["scope","project_root","provider","spec","admission"],
                           "properties":{"scope":string,"project_root":{"type":"string"},"provider":string,"spec":job_schema,"admission":{"type":"object"}}}},
                 ["operation_id","plan_hash","plan"]),
        function("compute_status", "Read scoped batch job status or reconcile unknown submission without allocation. Success requires a proven zero exit code.",
                 {"operation_id":string}, ["operation_id"]) ]
    schemas.append(function('compute_collect', 'Collect declared outputs from an owned proven-successful job into a fresh private directory in the active project. Returns file sizes and SHA256 receipts; does not allocate compute or resubmit jobs.', {'operation_id': string}, ['operation_id']))
    submit_schema = next(row for row in schemas if row['function']['name'] == 'compute_submit')
    schemas.append(function('compute_cancel', 'Request cancellation of the exact approved owned job. An acknowledgement is not terminal proof; follow compute_status. Unknown stop outcomes are not replayed.',
                            submit_schema['function']['parameters']['properties'], ['operation_id','plan_hash','plan']))
    return schemas

def _collect_into_project(service, actor, operation_id):
    """Host-owned fresh destination; model arguments never select filesystem paths.

    Successful artifacts are durable user results, retained under the explicit
    project. Failed private staging is removed; no cache/global resident state.
    """
    from pathlib import Path
    import shutil
    import tempfile
    root = Path(actor.project_root)
    if not actor.project_root or not root.is_dir() or root.is_symlink():
        raise EnvironmentError('project_required', 'Collection requires an explicit real project directory')
    destination = tempfile.mkdtemp(prefix='.tofu-compute-result-', dir=root)
    try:
        return service.collect(actor, operation_id, destination)
    except BaseException:
        shutil.rmtree(destination)
        raise

def _handler(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib.identity import require_user_id
    owner = require_user_id(task.get('_userId'), context='environment tool')
    actor = Actor(owner, str(task.get('_tenantId') or ''), project_path if project_enabled else '')
    with _LOCK:
        service = _SERVICE
        job_service = _JOB_SERVICE
    lifecycle = ExitStack()
    try:
        if fn_name.startswith('compute_'):
            job_service = job_service or _default_service(task, jobs=True, lifecycle=lifecycle)
            if fn_name == 'compute_plan':
                result = job_service.plan(actor, fn_args['provider'], fn_args['spec'], fn_args['request_id'])
            elif fn_name in {'compute_submit', 'compute_cancel'}:
                from lib.tasks_pkg.tool_dispatch._approval import consume_approval_receipt
                approved = consume_approval_receipt(task, fn_name, tc_id, fn_args)
                # The displayed preview must itself match the approved digest.
                if seal(fn_args['plan']) != fn_args['plan_hash']:
                    raise EnvironmentError('approval_scope_changed', 'Compute preview does not match plan seal')
                action = job_service.cancel if fn_name == 'compute_cancel' else job_service.submit
                result = action(actor, fn_args['operation_id'], fn_args['plan_hash'], lambda *_: approved)
            elif fn_name == 'compute_collect':
                result = _collect_into_project(job_service, actor, fn_args['operation_id'])
            elif fn_name == 'compute_status':
                result = job_service.status(actor, fn_args['operation_id'])
            else:
                raise EnvironmentError('unknown_tool', 'Unknown compute operation')
        elif fn_name == 'environment_providers':
            result = {"providers": [{"id": "docker", "authentication": "local configured daemon"}] +
                      [{"id": ep.name, "authentication": "owner-bound provider credentials"} for ep in entry_points(group="tofu.environment_providers")],
                      "compute_providers": [{"id": ep.name, "authentication": "owner-bound provider credentials"} for ep in entry_points(group="tofu.compute_providers")]}
        else:
            service = service or _default_service(task, lifecycle=lifecycle)
            if fn_name == 'environment_catalog':
                result = service.catalog(actor, fn_args['provider'], fn_args['project'], fn_args.get('query', ''))
            elif fn_name == 'environment_plan':
                result = service.plan(actor, fn_args['provider'], fn_args['spec'], fn_args['request_id'])
            elif fn_name == 'environment_apply':
                from lib.tasks_pkg.tool_dispatch._approval import consume_approval_receipt
                approved = consume_approval_receipt(task, fn_name, tc_id, fn_args)
                result = service.apply(actor, fn_args['operation_id'], fn_args['plan_hash'], lambda *_: approved, approved_plan=fn_args['plan'])
            else:
                result = service.status(actor, fn_args['operation_id'])
        content = json.dumps({"ok": True, **result}, ensure_ascii=False, allow_nan=False)
    except EnvironmentError as exc:
        content = json.dumps({"ok": False, "code": exc.code, "error": str(exc), "uncertain": exc.uncertain}, ensure_ascii=False)
    finally:
        lifecycle.close()
    return tc_id, content, False

def register(register_tool_spec):
    from lib.tools.registry import ToolSpec
    names = frozenset({'environment_providers', 'environment_catalog', 'environment_plan', 'environment_apply', 'environment_status', 'compute_plan', 'compute_submit', 'compute_status', 'compute_cancel', 'compute_collect'})
    register_tool_spec(ToolSpec('environments', _schemas, provides=names,
        handler=_handler, category='environments', description='Optional provider-neutral image/workspace provisioning',
        write_tools=frozenset({'environment_apply', 'compute_submit', 'compute_cancel'}), confirmation_tools=frozenset({'environment_apply', 'compute_submit', 'compute_cancel'}),
        auto_approval_tools=frozenset({'environment_apply', 'compute_submit', 'compute_cancel'}), cacheable_tools=frozenset(),
        discovery_policy='searchable', timeout_ms_by_name={'environment_providers': 10000, 'environment_catalog': 90000,
                            'environment_plan': 900000, 'environment_apply': 3600000, 'environment_status': 180000, 'compute_plan': 180000, 'compute_submit': 900000, 'compute_status': 180000, 'compute_cancel': 180000, 'compute_collect': 900000}))

"""lib/local_serve/api.py — Orchestration facade for the agent tool family.

The chat agent's ``local_serve_*`` tools (and any future HTTP route) call
ONLY this module; the shards (``_probe`` / ``_plan`` / ``_env`` /
``_process`` / ``_store`` / ``_register``) stay individually testable.
Every function returns JSON-serialisable dicts with user-facing Chinese
messages — the agent relays them verbatim, so failures must read like
guidance, not tracebacks.

Lifecycle of one deployment:

    prepare(model_path)          → inspection + hardware + plan (read-only)
    create_deployment(...)       → ledger row, status 'planned'
    start_deployment(id)         → install + spawn + OOM ladder + register
    stop_deployment(id)          → SIGTERM the process group
    remove_deployment(id)        → stop + unregister provider + drop row
    deployment_status(id)        → live pid/serving probe + log tail

Install and start are the two approval-gated boundaries — the tool layer
owns the gate, this module owns the work.
"""

from __future__ import annotations

import threading

from lib.identity import PERSONAL_USER_ID, require_user_id
from lib.log import get_logger

from . import _process as proc
from . import _probe as probe
from . import _register as register
from . import _store as store
from ._plan import ENGINE_SPECS, plan_launch

logger = get_logger(__name__)

__all__ = ['prepare', 'create_deployment', 'start_deployment',
           'start_deployment_async', 'deployment_inflight',
           'stop_deployment', 'remove_deployment', 'deployment_status',
           'list_deployments']

#: Instances with a live background deploy thread in THIS process. The
#: durable ledger statuses ('installing'/'starting') communicate progress to
#: status polls; this set only exists to refuse a double-start and to detect
#: rows orphaned by a server restart.
_start_lock = threading.Lock()
_inflight: set = set()


def _personal_owner(owner_user_id: int) -> int:
    """Authorize the host-global managed-serving ledger.

    TODO(enterprise): replace this personal-only guard with an explicit host
    resource scheduler and owner-scoped durable ledger before enabling managed
    local processes in distributed mode.
    """
    owner = require_user_id(owner_user_id, context='managed local deployment owner')
    from runtime_guards import load_deployment_configuration

    deployment = load_deployment_configuration()
    if deployment.mode != 'personal' or owner != PERSONAL_USER_ID:
        raise ValueError(
            '托管本地模型当前仅支持 personal 模式的本机所有者；'
            '多用户部署尚未启用主机资源仲裁')
    return owner


def _owned_instance(instance_id: str, owner_user_id: int) -> dict | None:
    owner = _personal_owner(owner_user_id)
    record = store.get_instance(instance_id)
    if record is None:
        return None
    recorded_owner = record.get('owner_user_id')
    if recorded_owner is None:
        # Rows created before owner capture existed can only have originated in
        # personal mode. Stamp the now-proven owner once; never apply this
        # conversion when distributed mode is active (guarded above).
        record = store.update_fields(instance_id, owner_user_id=owner) or record
        recorded_owner = owner
    try:
        recorded_owner = require_user_id(
            recorded_owner, context='managed local deployment ledger owner')
    except (TypeError, ValueError):
        return None
    if recorded_owner != owner:
        return None
    return record


def deployment_inflight(instance_id: str) -> bool:
    with _start_lock:
        return instance_id in _inflight


def prepare(model_path: str, *, engine: str | None = None) -> dict:
    """Inspect a model path and compute the launch plan. Read-only."""
    inspection = probe.inspect_model_path(model_path)
    if inspection.get('format') not in ('hf', 'gguf'):
        return {'ok': False, 'stage': 'inspect',
                'error': inspection.get('error'), 'inspection': inspection}
    hardware = probe.probe_hardware()
    port = proc.allocate_port()
    if port is None:
        return {'ok': False, 'stage': 'plan',
                'error': '托管端口段 18100-18199 已占满；'
                         '请先停止不用的本地部署实例',
                'inspection': inspection, 'hardware': hardware}
    plan = plan_launch(inspection, hardware, engine=engine, port=port)
    if not plan.get('ok'):
        return {'ok': False, 'stage': 'plan', 'error': plan.get('error'),
                'inspection': inspection, 'hardware': hardware}
    return {'ok': True, 'inspection': inspection, 'hardware': hardware,
            'plan': plan}


def create_deployment(
    model_path: str,
    *,
    owner_user_id: int,
    tenant_id: str = '',
    engine: str | None = None,
) -> dict:
    """prepare() + persist a 'planned' ledger row the user can approve."""
    try:
        owner_user_id = _personal_owner(owner_user_id)
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'stage': 'owner', 'error': str(exc)}
    prep = prepare(model_path, engine=engine)
    if not prep.get('ok'):
        return prep
    plan = prep['plan']
    slug = ''.join(c if c.isalnum() else '-' for c in
                   (plan.get('served_name') or 'model').lower())[:32]
    instance_id = 'ls_%s_%s' % (plan['engine'], slug)
    existing = store.get_instance(instance_id)
    if existing and existing.get('status') == 'running':
        return {'ok': False, 'stage': 'create',
                'error': '该模型的托管实例已在运行（%s）' % existing['base_url'],
                'instance': existing}
    record = store.upsert_instance({
        'id': instance_id,
        'owner_user_id': int(owner_user_id),
        'tenant_id': str(tenant_id or ''),
        'engine': plan['engine'],
        'model_path': plan['model_path'],
        'served_name': plan.get('served_name'),
        'port': plan['port'],
        'base_url': plan['base_url'],
        'argv': plan['argv'],
        'env': plan['env'],
        'tier': plan.get('tier'),
        'notes': plan.get('notes') or [],
        'degrade': plan.get('degrade') or [],
        'setup_steps': plan.get('setup_steps') or [],
        'degrade_index': 0,
        'status': 'planned',
        'pid': None,
        'provider_id': None,
        'last_error': None,
    })
    return {'ok': True, 'instance': record, 'plan': plan,
            'inspection': prep['inspection'], 'hardware': prep['hardware']}


def start_deployment(
    instance_id: str,
    *,
    owner_user_id: int,
    log=None,
    **process_kwargs,
) -> dict:
    """Install the engine, run the server, register the provider."""
    try:
        if _owned_instance(instance_id, owner_user_id) is None:
            return {'ok': False, 'error': '未知或无权访问的实例: %s' % instance_id}
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'error': str(exc)}
    result = proc.start_instance(instance_id, log=log, **process_kwargs)
    if not result.get('ok'):
        return result
    reg = register.register_instance(result)
    if not reg.get('ok'):
        # The server IS running — surface the registration failure but do
        # not kill a healthy server out from under the user.
        result['register_error'] = reg.get('error')
        store.update_fields(instance_id, last_error=reg.get('error'))
        return {**store.get_instance(instance_id), 'ok': False}
    store.update_fields(instance_id, provider_id=reg['provider_id'])
    return {'ok': True, **(store.get_instance(instance_id) or {}),
            'provider_id': reg['provider_id'], 'n_models': reg['n_models']}


def start_deployment_async(
    instance_id: str, *, owner_user_id: int, **kwargs,
) -> dict:
    """Kick off :func:`start_deployment` on a daemon thread.

    Installs and first-token model loads take minutes — far beyond a tool
    pool timeout — so the agent tool returns immediately and polls
    :func:`deployment_status`. The ledger row carries the live phase.
    """
    try:
        record = _owned_instance(instance_id, owner_user_id)
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'error': str(exc)}
    if record is None:
        return {'ok': False, 'error': '未知实例: %s' % instance_id}
    if record.get('status') == 'running' and record.get('pid'):
        return {'ok': True, 'started': False, 'already_running': True,
                **record}
    with _start_lock:
        if instance_id in _inflight:
            return {'ok': False,
                    'error': '该实例正在部署流程中，请用 local_serve_status 查看进度'}
        _inflight.add(instance_id)

    def _run():
        try:
            start_deployment(
                instance_id, owner_user_id=owner_user_id, **kwargs)
        except Exception as e:  # never let a thread die with the row stuck
            logger.exception('[LocalServe] async deploy crashed: %s', e)
            store.update_fields(instance_id, status='failed',
                                last_error='部署线程异常: %s' % e)
        finally:
            with _start_lock:
                _inflight.discard(instance_id)

    threading.Thread(target=_run, daemon=True,
                     name='local-serve-%s' % instance_id).start()
    return {'ok': True, 'started': True, 'instance_id': instance_id}


def deployment_status(
    instance_id: str, *, owner_user_id: int, **kwargs,
) -> dict:
    try:
        if _owned_instance(instance_id, owner_user_id) is None:
            return {'ok': False, 'error': '未知或无权访问的实例: %s' % instance_id}
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'error': str(exc)}
    status = proc.status_instance(instance_id, **kwargs)
    # Restart reconciliation: a row still saying installing/starting with no
    # live thread in THIS process was orphaned by a server restart — the
    # child (same process group as the dead server) is gone.
    if (status.get('status') in ('installing', 'starting')
            and not deployment_inflight(instance_id)):
        store.update_fields(
            instance_id, status='failed',
            last_error='Tofu 服务重启导致部署中断；可重新发起部署')
        status = {**status, 'status': 'failed',
                  'last_error': 'Tofu 服务重启导致部署中断；可重新发起部署'}
    return status


def stop_deployment(instance_id: str, *, owner_user_id: int, **kwargs) -> dict:
    try:
        if _owned_instance(instance_id, owner_user_id) is None:
            return {'ok': False, 'error': '未知或无权访问的实例: %s' % instance_id}
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'error': str(exc)}
    return proc.stop_instance(instance_id, **kwargs)


def list_deployments(*, owner_user_id: int) -> dict:
    try:
        owner = _personal_owner(owner_user_id)
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'error': str(exc), 'instances': []}
    instances = []
    for row in store.list_instances():
        owned = _owned_instance(str(row.get('id') or ''), owner)
        if owned is not None:
            instances.append(owned)
    return {'ok': True, 'instances': instances,
            'engines': {k: v['display'] for k, v in ENGINE_SPECS.items()}}


def remove_deployment(instance_id: str, *, owner_user_id: int) -> dict:
    try:
        record = _owned_instance(instance_id, owner_user_id)
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'error': str(exc)}
    if record is None:
        return {'ok': False, 'error': '未知实例: %s' % instance_id}
    if deployment_inflight(instance_id):
        return {'ok': False,
                'error': '该实例正在部署流程中；请先等待部署结束或失败'}
    if record.get('pid'):
        proc.stop_instance(instance_id)
    unreg = register.unregister_instance(record)
    store.remove_instance(instance_id)
    return {'ok': True, 'unregistered': unreg.get('ok', False)}

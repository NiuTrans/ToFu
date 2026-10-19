"""lib/swarm/integration/_rehydrate.py — resume swarms after a server restart.

Loads every resumable persisted swarm session, rebuilds its
``MasterOrchestrator`` + sub-agent tool list, and re-spawns the still-running
sub-agents from their checkpointed message history. Called once from
``server.py`` startup after the DB is ready.
"""

from __future__ import annotations

from copy import deepcopy
import os

from lib import agent_inbox
from lib.identity import PrincipalContext, require_user_id
from lib.log import get_logger
from lib.swarm.integration._logs import _resolve_output_dir
from lib.swarm.integration._state import (
    _get_session,
    _remove_session,
    _set_session,
)
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec

logger = get_logger(__name__)


def _rebuild_tool_list(config: dict) -> list:
    """Rebuild the sub-agent tool schema list from a persisted session config.

    New rows retain the declared live catalog, never expanding it from flags.
    Legacy non-isolated rows use ``_assemble_tool_list``. A rebuild failure
    retains the checkpoint and refuses launch rather than silently dropping tools.
    """
    try:
        if 'tool_catalog' in config:
            catalog = config['tool_catalog']
            if not isinstance(catalog, list):
                raise ValueError('Invalid durable swarm tool catalog')
            return deepcopy(catalog)
        from lib.tasks_pkg.model_config import _assemble_tool_list
        project_path = config.get('project_path', '') or ''
        search_mode = config.get('search_mode') or config.get('searchMode', 'multi')
        cfg = {
            'searchMode':        search_mode,
            'fetchEnabled':      config.get('fetchEnabled', True),
            'codeExecEnabled':   config.get('codeExecEnabled', False),
            'browserEnabled':    config.get('browserEnabled', False),
            'desktopEnabled':    config.get('desktopEnabled', False),
            'imageGenEnabled':   config.get('imageGenEnabled', False),
            'memoryEnabled':     config.get('memoryEnabled', True),
        }
        # Preserve the parent's tool-plugin allow-list so a rehydrated sub-agent
        # sees the same third-party plugins it had at spawn (else it would fall
        # back to the deployment default). Absent → resolve_enabled_plugins
        # fail-closes, same as a fresh request. See docs/contracts/TOOL_PLUGINS.md.
        if 'plugins' in config:
            cfg['plugins'] = config['plugins']
        tool_list, _has = _assemble_tool_list(
            cfg, project_path, bool(project_path),
            'swarm-rehydrate', search_mode,
            search_mode in ('single', 'multi'),
            config.get('fetchEnabled', True),
            config.get('codeExecEnabled', False),
            config.get('browserEnabled', False),
            config.get('desktopEnabled', False),
            image_gen_enabled=config.get('imageGenEnabled', False),
            scheduler_enabled=config.get('schedulerEnabled', False),
        )
        return tool_list or []
    except Exception as e:
        logger.warning('[Swarm] tool-list rebuild on rehydrate failed: %s', e,
                       exc_info=True)
        raise RuntimeError('Swarm recovery tool authority could not be rebuilt') from e


def _recovery_allowed(sess: dict, *, user_id: int) -> bool:
    """Retained child evidence is not permission to restart cancelled work.

    A terminated driver can retain a running checkpoint when cancellation or
    its bounded drain ended before the worker saved another round. Only a
    still-running session owns checkpoint resume; terminal sessions may replay
    already-terminal results, never fill absent checkpoints with fresh work.
    Conversation cancellation is read from the existing owner-scoped Turn
    authority, not a second durable cancellation flag.
    """
    if sess.get('status') == 'terminated':
        agents = {a.get('agent_id'): a for a in sess.get('agents') or []}
        if any(agents.get(s.get('id'), {}).get('status')
               not in ('completed', 'failed', 'cancelled')
               for s in sess.get('specs') or []):
            return False
    conv_id = sess.get('conv_id') or ''
    if not conv_id:
        return True

    parent = (sess.get('config') or {}).get('parent_cfg') or {}
    if (sess.get('status') == 'running'
            and parent.get('_swarmParentTurnId')
            and parent.get('_swarmParentAttemptId')):
        # Detached work retains its completed spawning authority. Interrupted
        # parents still require the exact accepted Continue receipt.
        try:
            from lib.swarm.restart_recovery import detached_recovery_allowed
            return detached_recovery_allowed(sess, user_id=user_id)
        except Exception:
            logger.warning('[Swarm:%s] detached recovery authority unavailable; '
                           'evidence retained without launching', conv_id, exc_info=True)
            return False
    try:
        from lib.swarm.restart_recovery import read_recovery_turns
        page = read_recovery_turns(conv_id, user_id=user_id, lane_id='main',
                                   limit=1)
        turns = page.get('turns') or []
        # The storage page owns newest-first selection even in a long history.
        if not turns:
            return False
        return turns[-1].get('status') not in ('cancelled', 'aborted', 'interrupted')
    except Exception:
        logger.warning('[Swarm:%s] restart authority unavailable; evidence '
                       'retained without launching', conv_id, exc_info=True)
        return False


def _restore_execution_context(config: dict) -> dict:
    """Only the declared durable snapshot may restore borrowed authority.

    Canonical-path metadata is a deny-only legacy marker, never a permission
    source. Older isolated rows without the detached snapshot retain evidence
    but cannot launch work using a guessed writable root.
    """
    parent_cfg = config.get('parent_cfg') or {}
    if not isinstance(parent_cfg, dict):
        raise ValueError('Invalid durable swarm parent config')
    snapshot = config.get('workspace_execution_context')
    canonical = parent_cfg.get('canonicalProjectPath') or parent_cfg.get('_canonicalProjectPath')
    isolated = bool(config.get('workspace_isolated')
                    or canonical and canonical != config.get('project_path'))
    if snapshot is None:
        from lib.tasks_pkg.workspace_delivery import _checkout, WorkspaceBindingError
        try:
            checkout = _checkout(config.get('project_path') or '')
        except WorkspaceBindingError as error:
            raise ValueError('Legacy swarm workspace identity is unavailable') from error
        if (isolated or parent_cfg.get('canonicalProjectPaths')
                or parent_cfg.get('_canonicalProjectPaths')
                or checkout and checkout[0] != checkout[1]):
            raise ValueError('Isolated swarm has no durable execution policy')
        return {}
    if (not isinstance(snapshot, dict) or snapshot.get('schema') != 1
            or set(snapshot) != {'schema', '_workspaceExecution', '_workspaceRootPolicy'}):
        raise ValueError('Invalid durable workspace execution snapshot')
    execution = snapshot['_workspaceExecution']
    policy = snapshot['_workspaceRootPolicy']
    if (not isinstance(execution, dict) or set(execution) != {'repositories'}
            or not isinstance(execution['repositories'], list)
            or not execution['repositories']):
        raise ValueError('Invalid durable workspace repositories')
    for row in execution['repositories']:
        if (not isinstance(row, dict) or set(row) != {'canonicalRoot', 'workspacePath'}
                or any(not isinstance(row[key], str) or not os.path.isabs(row[key])
                       for key in ('canonicalRoot', 'workspacePath'))
                or row['canonicalRoot'] == row['workspacePath']):
            raise ValueError('Invalid durable workspace repository identity')
    if (not isinstance(policy, dict) or set(policy) != {'primaryPath', 'roots'}
            or not isinstance(policy['primaryPath'], str)
            or not os.path.isabs(policy['primaryPath'])
            or not isinstance(policy['roots'], dict) or not policy['roots']):
        raise ValueError('Invalid durable workspace root policy')
    paths = set()
    for name, state in policy['roots'].items():
        if (not isinstance(name, str) or not name or not isinstance(state, dict)
                or not isinstance(state.get('path'), str)
                or not os.path.isabs(state['path']) or state.get('access') not in ('ro', 'rw')):
            raise ValueError('Invalid durable workspace root state')
        paths.add(state['path'])
    if (policy['primaryPath'] not in paths
            or any(row['workspacePath'] not in paths for row in execution['repositories'])
            or any(row['canonicalRoot'] in paths for row in execution['repositories'])):
        raise ValueError('Durable workspace roots do not match execution identity')
    return deepcopy({key: snapshot[key] for key in ('_workspaceExecution', '_workspaceRootPolicy')})


def _continue_execution_parent(config: dict, task: dict) -> dict:
    """Borrow original execution roots, with current declared access as a veto.

    Continue proves the original assignment, not permission to write a new or
    removed root. The new parent may own a different checkout of the same
    canonical project; interrupted child edits remain in their original tree.
    """
    context = _restore_execution_context(config)
    if not context:
        return task
    from pathlib import Path
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.workspace_delivery import (
        _checkout, ensure_execution_roots, route_project_tool_args)
    rows = context['_workspaceExecution']['repositories']
    for row in rows:
        actual = _checkout(row['workspacePath'])
        expected = (os.path.realpath(row['workspacePath']), os.path.realpath(row['canonicalRoot']))
        if actual != expected:
            raise ValueError('Original swarm workspace is unavailable or mismatched')
    current_cfg = task.get('config') or {}
    current_policy = ensure_execution_roots(task)
    if current_policy is None:
        path = task.get('projectPath') or current_cfg.get('projectPath')
        if not path:
            raise ValueError('Continue has no current workspace authority')
        current_policy = build_root_policy(path, current_cfg.get('projectPaths'),
                                           current_cfg.get('readOnlyPaths'))
    def remap(path, repositories, source, target):
        path = os.path.realpath(path)
        for row in sorted(repositories, key=lambda row: -len(row[source])):
            try:
                suffix = Path(path).relative_to(os.path.realpath(row[source]))
            except ValueError:
                continue
            return str(Path(row[target]) / suffix)
        return path

    def authority_at(path, roots):
        candidates = [root for root in roots if
                      Path(path).is_relative_to(os.path.realpath(root['path']))]
        return max(candidates, key=lambda root: len(os.path.realpath(root['path'])), default=None)

    restored_roots = context['_workspaceRootPolicy']['roots']
    original_roots = deepcopy(list(restored_roots.values()))
    for state in restored_roots.values():
        canonical = remap(state['path'], rows, 'workspacePath', 'canonicalRoot')
        current = os.path.realpath(route_project_tool_args(task, 'read_files',
                                                          {'path': canonical})['path'])
        authority = authority_at(current, current_policy['roots'].values())
        if authority is None:
            raise ValueError('Original swarm root is no longer declared by Continue')
        if authority['access'] == 'ro':
            state['access'] = 'ro'
    # Intersect both policies at every declared boundary, not just old roots.
    # Writable exceptions remain usable only where BOTH policies grant them.
    from lib.tasks_pkg.workspace_delivery import execution_repositories
    for name, state in current_policy['roots'].items():
        canonical = remap(state['path'], execution_repositories(task), 'workspacePath', 'canonicalRoot')
        original = remap(canonical, rows, 'canonicalRoot', 'workspacePath')
        authority = authority_at(original, original_roots)
        if authority is None or any(os.path.realpath(root['path']) == original
                                    for root in restored_roots.values()):
            continue
        label = f'continue-{name}'
        while label.lower() in {key.lower() for key in restored_roots}:
            label += '-subroot'
        restored_roots[label] = {**deepcopy(state), 'path': original,
            'access': 'ro' if 'ro' in (state['access'], authority['access']) else 'rw'}
    parent = {key: value for key, value in task.items()
              if not key.startswith('_workspaceDelivery') and key != '_workspaceRegistration'}
    parent.update(context)

    from lib.agent_core.execution_budget import share_execution_budget
    share_execution_budget(task, parent)
    from lib.tasks_pkg.native_parent import share_native_parent
    share_native_parent(task, parent)
    return parent


def _rehydrate_one(sess: dict, *, resume_task: dict | None = None,
                   tool_list: list | None = None) -> bool:
    """Serialize startup/Continue recovery without holding registry locks over I/O."""
    from lib.swarm.restart_recovery import recovery_lock
    with recovery_lock(str(sess.get('swarm_key') or '')):
        return _rehydrate_one_locked(sess, resume_task=resume_task, tool_list=tool_list)


def _rehydrate_one_locked(sess: dict, *, resume_task: dict | None = None,
                          tool_list: list | None = None) -> bool:
    """Rebuild + resume a single persisted session under the launch stripe."""
    swarm_key = sess.get('swarm_key', '')
    if not swarm_key:
        return False
    # Don't clobber a session that's somehow already live (idempotent startup).
    if _get_session(swarm_key) is not None:
        logger.debug('[Swarm:%s] already live — skipping rehydrate', swarm_key)
        return False

    specs = [SubTaskSpec.from_dict(d) for d in (sess.get('specs') or [])]
    if not specs:
        logger.debug('[Swarm:%s] no specs persisted — skipping', swarm_key)
        return False

    config = sess.get('config') or {}
    try:
        owner_user_id = require_user_id(
            config.get('user_id') if isinstance(config, dict) else None,
            context='durable swarm owner')
    except ValueError:
        from lib.swarm import persistence
        quarantined = persistence.quarantine_ownerless_session(swarm_key)
        if quarantined:
            logger.error(
                '[Swarm:%s] persisted session has no valid owner — '
                'quarantined from startup recovery; durable evidence retained',
                swarm_key)
        else:
            logger.error(
                '[Swarm:%s] persisted session has no valid owner — refusing '
                'unscoped rehydration; quarantine was not confirmed', swarm_key)
        return False
    conv_id = sess.get('conv_id', '') or ''
    task_id = sess.get('task_id', '') or swarm_key

    if resume_task is not None:
        from lib.tasks_pkg.manager import task_user_id
        from lib.swarm.restart_recovery import continue_recovery_allowed
        allowed = (task_user_id(resume_task) == owner_user_id
            and continue_recovery_allowed(sess, resume_task, user_id=owner_user_id))
    else:
        allowed = _recovery_allowed(sess, user_id=owner_user_id)
    if not allowed:
        logger.info('[Swarm:%s] restart stood down: stopped or unsettled '
                    'authority; retained checkpoints remain inspectable', swarm_key)
        return False
    config = deepcopy(config)
    if 'native_execution_required' in config and type(config['native_execution_required']) is not bool:
        raise ValueError('Invalid durable native delegation binding')
    if config.get('native_execution_required'):
        agents = {row.get('agent_id'): row for row in sess.get('agents') or []}
        def needs_actor_checkpoint(spec):
            agent = agents.get(spec.id, {})
            return (agent.get('status') not in ('completed', 'failed', 'cancelled')
                or (resume_task is not None and agent.get('status') == 'cancelled'
                    and (agent.get('result') or {}).get('recovery_reason') == 'parent_stop'
                    and not spec.retained_agent_id))
        if any(needs_actor_checkpoint(spec) for spec in specs):
            # Transcript checkpoints cannot authorize native actor takeover.
            # Keep the original evidence until whole-tree handoff is admitted.
            if resume_task is not None:
                from lib.error_envelope import make_envelope
                # The accepted lineage lacks a complete actor accounting cut.
                # Preserve known spend; common admission and settlement read
                # these existing completeness fields, including uncapped runs.
                resume_task['_executionActualsComplete'] = False
                resume_task['_checkpointUsageComplete'] = False
                error = RuntimeError('Native delegated recovery requires acknowledged actor checkpoints; no worker resumed')
                error._user_message = make_envelope('execution_token_usage_unknown',
                    source='swarm-recovery', detail=str(error), retryable=False)
                raise error
            logger.info('[Swarm:%s] native actor takeover unavailable; evidence retained', swarm_key)
            return False
    execution_context = {}
    execution_parent = resume_task
    all_tools = [] if resume_task is not None else _rebuild_tool_list(config)
    if resume_task is not None:
        from lib.swarm.routing import tool_schema_name
        from lib.tasks_pkg.child_invocation import snapshot_conversation_child_policy
        names = (config.get('parent_cfg') or {}).get('_swarmToolNames')
        if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
            raise RuntimeError('Swarm restart has no original tool-authority receipt')
        original_names = set(names)
        current_catalog = resume_task.get('_executable_tool_catalog')
        if current_catalog is None:
            current_catalog = tool_list or []
        execution_parent = _continue_execution_parent(config, resume_task)
        all_tools = [tool for tool in current_catalog
                     if tool_schema_name(tool) in original_names]
        original = config.get('parent_cfg') or {}
        current_cfg = resume_task.get('config') or {}
        config['model'] = resume_task.get('model') or config.get('model')
        config['thinking_enabled'] = current_cfg.get('thinkingEnabled')
        policy = execution_parent.get('_workspaceRootPolicy') or {}
        config['project_path'] = str(policy.get('primaryPath') or resume_task.get('projectPath')
                                   or current_cfg.get('projectPath') or '')
        child_policy = snapshot_conversation_child_policy(
            {**resume_task, 'config': {**current_cfg, '_executableToolCatalog': all_tools}},
            model=config['model'], tools=all_tools, project_path=config['project_path'],
            thinking_enabled=config['thinking_enabled'])
        parent_cfg = child_policy['config']
        parent_cfg.update(child_policy['conversation_tool_policy'])

        if policy:
            primary = policy['primaryPath']
            parent_cfg['projectPaths'] = [primary, *dict.fromkeys(
                state['path'] for state in policy['roots'].values() if state['path'] != primary)]
            parent_cfg['readOnlyPaths'] = list(dict.fromkeys(
                state['path'] for state in policy['roots'].values() if state['access'] == 'ro'))
        parent_cfg.update({key: value for key, value in original.items()
                          if key.startswith('_swarm')})

        parent_cfg.pop('_swarmRecoveryFailedAttempt', None)
        parent_cfg['_swarmParentAttemptId'] = (resume_task.get('config') or {}).get('_attemptId')
        parent_cfg['_swarmParentTaskId'] = resume_task['id']
        # A later recovery starts from the last accepted Continue, while the
        # launch receipt/run id stays stable for result and snapshot identity.
        config['parent_cfg'] = parent_cfg
        config['search_mode'] = current_cfg.get('searchMode', 'multi')

    else:
        try:
            execution_context = _restore_execution_context(config)
            if execution_context and not isinstance(config.get('tool_catalog'), list):
                raise ValueError('Isolated swarm has no durable tool catalog')
            principal_payload = config.get('principal_context')
            if principal_payload is not None:
                if not isinstance(principal_payload, dict):
                    raise ValueError('Invalid durable swarm principal')
                principal = PrincipalContext.from_payload(principal_payload)
                if principal.require_owner(context='durable swarm principal') != owner_user_id:
                    raise ValueError('Durable swarm principal owner does not match session')
                execution_context['_principalContext'] = principal.to_payload()
            elif execution_context:
                raise ValueError('Isolated swarm has no durable principal snapshot')
        except ValueError as error:
            logger.warning('[Swarm:%s] restart workspace authority unavailable: %s; '
                           'retained checkpoints remain inspectable', swarm_key, error)
            return False
    output_dir = _resolve_output_dir(task_id)

    push_conv_id = conv_id

    def _emit(ev: dict):
        if push_conv_id:
            try:
                from lib.agent_core.push import push_event
                push_event(
                    'swarm', push_conv_id, {**ev, 'swarmId': task_id},
                    user_id=owner_user_id,
                )
            except Exception as e:
                logger.debug('[Swarm:%s] rehydrate push mirror failed: %s', swarm_key, e)

    agent_inbox.untombstone(swarm_key)

    # Resolve the settle hook through the facade package so a patched
    # ``_maybe_autocontinue`` still drives a rehydrated session's settle path.
    def _on_settled(
        k=swarm_key,
        owner=owner_user_id,
        # Legacy rows retain their original command identity. New sessions
        # carry a distinct durable identity even when the parent task repeats.
        source=(config.get('parent_cfg') or {}).get('_swarmContinuationSource') or task_id,  # noqa: B008
    ):
        import lib.swarm.integration as _pkg
        return _pkg._maybe_autocontinue(k, owner, source_id=source)

    from lib.swarm.integration._tools import _child_usage_rollup
    on_agent_usage = _child_usage_rollup(resume_task) if resume_task is not None else None

    if resume_task is None and '_principalContext' not in execution_context:
        config.setdefault('parent_cfg', {})['_principalContext'] = PrincipalContext.user(
            subject_id=f'swarm-recovery:{task_id}', owner_user_id=owner_user_id,
            tenant_id=(config.get('parent_cfg') or {}).get('_tenant_id'))
    if resume_task is None:
        from lib.tasks_pkg.requirement_source import restore_requirement_source
        parent_cfg = config.get('parent_cfg') or {}
        principal = execution_context.get('_principalContext') or parent_cfg['_principalContext']
        if not isinstance(principal, PrincipalContext):
            principal = PrincipalContext.from_payload(principal)
        execution_parent = {'id': task_id, 'convId': conv_id, '_userId': owner_user_id,
            '_tenant_id': principal.tenant_id, '_principalContext': principal.to_payload(),
            'config': parent_cfg, **execution_context}
        source = config.get('requirement_source')
        if 'requirement_source' not in config and parent_cfg.get('_swarmParentTurnId'):
            source = dict(owner=owner_user_id, tenant=str(principal.tenant_id or ''),
                conversation=conv_id, turn=parent_cfg['_swarmParentTurnId'],
                attempt=parent_cfg.get('_swarmParentAttemptId'),
                task=parent_cfg.get('_swarmParentTaskId') or task_id)
        if source is not None:
            restore_requirement_source(execution_parent, source)
    def recovery_still_allowed():
        if resume_task is not None:
            return continue_recovery_allowed(
                sess, resume_task, user_id=owner_user_id, allow_parent_completion=True)
        return _recovery_allowed(sess, user_id=owner_user_id)

    def before_agent_admission():
        if not recovery_still_allowed():
            from lib.llm.llm_errors import AbortedError
            raise AbortedError('Durable recovery authority cancelled before worker admission')

    if resume_task is not None:
        from lib.swarm.master import bind_assignment_context, _retained_assignment_proxy
        for spec in specs:
            if spec.retained_execution_context is None:
                continue  # Legacy sessions use their original session receipt.
            _, assignment = _retained_assignment_proxy(spec, execution_parent)
            names = {tool_schema_name(tool) for tool in assignment['tools']}
            selected = [tool for tool in current_catalog if tool_schema_name(tool) in names]
            assignment_parent = execution_parent
            if assignment['execution_context'].get('_workspaceExecution'):
                assignment_parent = _continue_execution_parent({
                    'workspace_execution_context': {'schema': 1, **assignment['execution_context']},
                    'project_path': assignment['project_path'],
                }, resume_task)
            bind_assignment_context(spec, parent_task=assignment_parent,
                parent_config=resume_task.get('config') or {}, all_tools=selected,
                model=config['model'], project_path=str(
                    (assignment_parent.get('_workspaceRootPolicy') or {}).get('primaryPath')
                    or config['project_path']),
                thinking_enabled=config['thinking_enabled'],
                abort_check=lambda: bool(resume_task.get('aborted')),
                on_agent_usage=on_agent_usage)

    session = MasterOrchestrator(
        task_id=task_id,
        conv_id=conv_id,
        user_id=owner_user_id,
        specs=specs,
        project_path=config.get('project_path', '') or '',
        model=config.get('model', '') or '',
        thinking_enabled=config.get('thinking_enabled'),
        search_mode=config.get('search_mode') or config.get('searchMode', 'multi'),
        on_progress=_emit,
        abort_check=(lambda: bool(resume_task.get('aborted'))) if resume_task is not None else None,
        all_tools=all_tools,
        max_parallel=config.get('max_parallel', 8),
        max_retries=config.get('max_retries', 1),
        output_dir=output_dir,
        parent_config=config.get('parent_cfg', {}),
        parent_task=execution_parent,
        on_agent_usage=on_agent_usage,

        before_agent_admission=before_agent_admission,
        inbox_key=swarm_key,
        on_settled=_on_settled,
    )
    # Close the Stop/supersede window after catalog reconstruction.
    if not recovery_still_allowed():
        return False
    published = False
    try:
        from lib.swarm.integration._state import reserve_session
        with reserve_session(swarm_key):
            # No publication/workers until the new binding is acknowledged.
            if resume_task is not None:
                from lib.swarm import persistence
                from lib.swarm.integration._config import _persist_config
                durable_config = _persist_config(config, config['model'],
                    config['thinking_enabled'], config['project_path'], config['parent_cfg'],
                    user_id=owner_user_id, parent_task=execution_parent, tool_catalog=all_tools)
                persistence.save_session(swarm_key, conv_id=conv_id, task_id=task_id,
                    specs=[spec.to_dict() for spec in specs], required=True, config=durable_config)
                if not continue_recovery_allowed(sess, resume_task, user_id=owner_user_id):
                    session.abort()
                    return False
            _set_session(swarm_key, session, task_id=task_id, reserved=True)
            published = True
            if not recovery_still_allowed():
                session.abort()
                _remove_session(swarm_key, preserve_durable=True)
                return False
            if resume_task is not None:
                from lib.swarm.integration._state import add_session_alias
                add_session_alias(resume_task['id'], swarm_key)
                if not continue_recovery_allowed(sess, resume_task, user_id=owner_user_id):
                    session.abort()
                    _remove_session(swarm_key, preserve_durable=True)
                    return False
            if resume_task is not None:
                session.rehydrate_in_background(sess.get('agents') or [], resume_parent_stop=True)
            else:
                session.rehydrate_in_background(sess.get('agents') or [])
    except Exception as e:
        logger.error('[Swarm:%s] activation failed: %s', swarm_key, e, exc_info=True)
        # Submission may have preceded driver startup failure. Internal pause
        # keeps checkpoints resumable; drain before dropping the stop handle.
        if resume_task is not None and resume_task.get('aborted'):
            session.abort()
        else:
            session.suspend_for_restart()
        scheduler = getattr(session, '_scheduler', None)
        if scheduler is not None:
            scheduler.shutdown(require_quiescence=True)
        if published:
            _remove_session(swarm_key, preserve_durable=True)
        if resume_task is not None and not resume_task.get('aborted'):
            # Original work/policy plus this exact failed activation receipt;
            # later Continue must directly follow its durable failed attempt.
            from lib.swarm import persistence
            rollback_config = deepcopy(sess['config'])
            rollback_config['parent_cfg'].update(
                _swarmParentAttemptId=resume_task['config']['_attemptId'],
                _swarmParentTaskId=resume_task['id'],
                _swarmRecoveryFailedAttempt={'attemptId': resume_task['config']['_attemptId'],
                                            'taskId': resume_task['id']})
            persistence.save_session(swarm_key, conv_id=conv_id, task_id=task_id,
                specs=[spec.to_dict() for spec in specs], required=True, config=rollback_config)
        raise RuntimeError(f'Swarm checkpoint recovery failed; evidence retained: {e}') from e
    logger.info('[Swarm:%s] rehydrated (specs=%d, agents=%d)',
                swarm_key, len(specs), len(sess.get('agents') or []))
    return True


def resume_swarm_for_continue(task: dict, tool_list: list) -> bool:
    """Restore safe descendants of this exact accepted restart/Stop Continue."""
    cfg = task.get('config') or {}
    if cfg.get('_turnOperation') not in ('continue', 'checkpoint_resume'):
        return False
    from lib.swarm import persistence
    from lib.swarm.integration._config import swarm_key_for
    from lib.swarm.restart_recovery import recovery_lock, continue_recovery_allowed
    from lib.tasks_pkg.manager import task_user_id
    import time

    key = swarm_key_for(task)
    with recovery_lock(key):
        live = _get_session(key)
        sess = persistence.load_session(key, required=True)
        if sess is None or not continue_recovery_allowed(sess, task, user_id=task_user_id(task)):
            return False
        if live is not None:
            # Stop is signal-only. Its final writes must not overwrite the new
            # activation; keep the stop handle until the old driver has exited.
            if not getattr(live, '_aborted', False):
                return False
            driver = getattr(live, '_driver_thread', None)
            deadline = time.monotonic() + 30.0
            while driver is not None and driver.is_alive():
                if task.get('aborted'):
                    return False
                if time.monotonic() >= deadline:
                    raise RuntimeError('Stopped swarm is still draining; recovery was not launched')
                driver.join(timeout=0.1)
            scheduler = getattr(live, '_scheduler', None)
            if scheduler is not None and scheduler.running_count:
                raise RuntimeError('Stopped swarm still has live workers; recovery was not launched')
            # The final checkpoint may differ from the admission row.
            sess = persistence.load_session(key, required=True)
            if sess is None or not continue_recovery_allowed(sess, task, user_id=task_user_id(task)):
                return False
            _remove_session(key, preserve_durable=True)
        return _rehydrate_one(sess, resume_task=task, tool_list=tool_list)


def rehydrate_swarms_on_startup() -> int:
    """Resume all persisted non-terminal swarm sessions after a restart.

    Called once from ``server.py`` startup (after the DB is ready). Loads
    every resumable session from the DB, rebuilds its MasterOrchestrator +
    tool list, and re-spawns the still-running sub-agents from their
    checkpointed message history. Returns the count successfully resumed.

    Best-effort and isolated per session: one bad session never blocks the
    others or the server boot.
    """
    try:
        from lib.swarm import persistence
        sessions = persistence.load_resumable_sessions()
    except Exception as e:
        logger.warning('[Swarm] startup rehydrate: could not load sessions: %s', e)
        return 0

    if not sessions:
        return 0

    resumed = 0
    for sess in sessions:
        try:
            if _rehydrate_one(sess):
                resumed += 1
        except Exception as e:
            logger.error('[Swarm] rehydrate of %s failed: %s',
                         sess.get('swarm_key', '?'), e, exc_info=True)
    logger.info('[Swarm] startup rehydration complete — %d/%d session(s) resumed',
                resumed, len(sessions))
    return resumed

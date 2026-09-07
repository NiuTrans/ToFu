"""lib/swarm/integration/_rehydrate.py — resume swarms after a server restart.

Loads every resumable persisted swarm session, rebuilds its
``MasterOrchestrator`` + sub-agent tool list, and re-spawns the still-running
sub-agents from their checkpointed message history. Called once from
``server.py`` startup after the DB is ready.
"""

from __future__ import annotations

from lib import agent_inbox
from lib.identity import require_user_id
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

    Mirrors what ``_assemble_tool_list`` produced for the original spawn, so a
    resumed sub-agent has the same tools available. Best-effort — on any
    failure returns an empty list (the agent still resumes, just tool-less,
    which is far better than not resuming at all).
    """
    try:
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
        # fail-closes, same as a fresh request. See docs/TOOL_PLUGINS.md.
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
        return []


def _rehydrate_one(sess: dict) -> bool:
    """Rebuild + resume a single persisted swarm session. Returns success."""
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
    all_tools = _rebuild_tool_list(config)
    output_dir = _resolve_output_dir(task_id)

    push_conv_id = conv_id

    def _emit(ev: dict):
        if push_conv_id:
            try:
                from lib.agent_core.push import push_event
                push_event(
                    'swarm', push_conv_id, ev,
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
        source=task_id,
    ):
        import lib.swarm.integration as _pkg
        return _pkg._maybe_autocontinue(k, owner, source_id=source)

    session = MasterOrchestrator(
        task_id=task_id,
        conv_id=conv_id,
        user_id=owner_user_id,
        specs=specs,
        project_path=config.get('project_path', '') or '',
        model=config.get('model', '') or '',
        thinking_enabled=config.get('thinking_enabled', True),
        search_mode=config.get('search_mode') or config.get('searchMode', 'multi'),
        on_progress=_emit,
        abort_check=None,
        all_tools=all_tools,
        max_parallel=config.get('max_parallel', 8),
        max_retries=config.get('max_retries', 1),
        output_dir=output_dir,
        parent_config=config.get('parent_cfg', {}),
        inbox_key=swarm_key,
        on_settled=_on_settled,
    )
    _set_session(swarm_key, session, task_id=task_id)
    try:
        session.rehydrate_in_background(sess.get('agents') or [])
    except Exception as e:
        logger.error('[Swarm:%s] rehydrate_in_background failed: %s',
                     swarm_key, e, exc_info=True)
        _remove_session(swarm_key)
        return False
    logger.info('[Swarm:%s] rehydrated (specs=%d, agents=%d)',
                swarm_key, len(specs), len(sess.get('agents') or []))
    return True


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

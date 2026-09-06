"""Task registry & lifecycle — create / discard / list / abort / quiesce, plus
the aborted-task terminal floor.

Uses the shared :class:`TaskRuntime` and conversation freshness index from
``manager.runtime``. ``_write_aborted_terminal_floor`` borrows the low-level
persist helpers.
"""

import json
import threading
import time
import uuid

from lib.agent_core.execution_session import ExecutionSession
from lib.error_envelope import to_json as _err_to_json
from lib.log import get_logger

from lib.tasks_pkg.manager.runtime import (
    _abort_tombstones,
    _abort_tombstones_lock,
    _ABORT_TOMBSTONES_CAP,
    chat_task_runtime,
    _clear_latest_task,
    _record_latest_task,
)
from lib.tasks_pkg.manager._persist import (
    _merge_tool_rounds,
    _tool_rounds_have_dedicated_home,
    _upsert_task_row,
    build_result_meta,
)

logger = get_logger(__name__)


def task_user_id(task):
    """Return the positive owner captured when the task was created."""
    if not isinstance(task, dict):
        raise ValueError('task owner requires a task dict')
    from lib.identity import PrincipalContext, require_user_id
    owner_user_id = require_user_id(task.get('_userId'), context='task owner')
    raw_principal = task.get('_principalContext')
    if raw_principal is None:
        # Compatibility conversion, not an identity fallback: the task
        # already carries a required positive owner and is stamped once with
        # its structured principal before leaving this boundary.
        principal = PrincipalContext.user(
            subject_id=f'user:{owner_user_id}', owner_user_id=owner_user_id)
        task['_principalContext'] = principal.to_payload()
    elif isinstance(raw_principal, dict):
        principal = PrincipalContext.from_payload(raw_principal)
    else:
        raise ValueError('task owner requires a valid PrincipalContext')
    if principal.require_owner(context='task owner') != owner_user_id:
        raise ValueError('task principal owner does not match _userId')
    return owner_user_id


def is_carrier_task(task: dict) -> bool:
    """Return whether a task is an internal holder, not restart-blocking work."""
    return bool(task.get('_inline_messages') or task.get('_vu_subtask'))


def create_task(
    conv_id,
    messages,
    config,
    *,
    user_id: int | None = None,
    principal=None,
    supersede=True,
    transient: bool = False,
):
    """Create (and register) a chat task.

    ``supersede`` (default True) makes superseding the INVARIANT of task
    creation: after registering as the conversation's latest task, any OTHER
    still-running task for the same ``conv_id`` is force-aborted (via
    ``abort_running_tasks_for_conv``). This is the single source of truth for
    the "a new task supersedes the old one" rule — every background path that
    creates a task through the conversation command service is automatically
    covered, instead of each entry
    point having to remember to call the abort sweep.

    Pass ``supersede=False`` for a DELIBERATE concurrency axis that must run
    alongside its siblings under the same conv_id — currently only
    ``chat_branch_start`` (a branch is an intentional parallel turn and must
    NOT abort the main task or sibling branches).

    ``transient=True`` is the storage-free embed/headless composition. Such a
    task still has an explicit principal and the normal in-memory lifecycle,
    but it is never mirrored to task-results, event-log, affinity, project, or
    conversation authorities.
    """
    from lib.identity import PrincipalContext, require_user_id
    if principal is None:
        owner_user_id = require_user_id(user_id, context='create_task')
        principal = PrincipalContext.user(
            subject_id=f'user:{owner_user_id}',
            owner_user_id=owner_user_id,
        )
    else:
        if not isinstance(principal, PrincipalContext):
            raise TypeError('create_task principal must be PrincipalContext')
        owner_user_id = principal.require_owner(context='create_task')
        if user_id is not None and require_user_id(
                user_id, context='create_task') != owner_user_id:
            raise ValueError('create_task principal/user_id mismatch')
    config = dict(config or {})
    config['userId'] = owner_user_id
    task_id = str(uuid.uuid4())
    # ── Extract the user's original question from the last user message ──
    # This is passed to the content filter alongside the search query so
    # the filter can assess relevance against the ORIGINAL intent, not just
    # the model-generated search keywords.
    last_user_query = ''
    last_user_idx = -1
    for i in range(len(messages or []) - 1, -1, -1):
        m = messages[i]
        if m.get('role') == 'user':
            c = m.get('content', '')
            if isinstance(c, list):
                # multimodal: extract text blocks
                c = ' '.join(b.get('text', '') for b in c if isinstance(b, dict) and b.get('type') == 'text')
            last_user_query = (c or '')[:500]
            last_user_idx = i
            break

    # ── UserPromptSubmit hooks (Claude Agent SDK parity) ──
    # Fire ONCE per turn, BEFORE the prompt enters the agent loop. Hooks
    # can rewrite the latest user message (PII redaction, safety filters,
    # prompt augmentation).  Only the rewritten text is propagated; the
    # message structure (role, attachments, tool_call_id) is preserved.
    if last_user_idx >= 0 and isinstance(messages[last_user_idx].get('content'), str):
        try:
            from lib.tasks_pkg.tool_hooks import run_user_prompt_hooks
            _orig = messages[last_user_idx]['content']
            _rewritten = run_user_prompt_hooks(_orig, {
                'id': task_id, 'convId': conv_id, 'config': config or {},
            })
            if _rewritten != _orig:
                messages[last_user_idx]['content'] = _rewritten
                last_user_query = _rewritten[:500]
        except Exception as e:
            logger.warning('[Task %s] UserPromptSubmit hooks failed: %s',
                           task_id[:8], e, exc_info=True)

    # Create through TaskRuntime so the task is registered in the unified
    # store. Then augment with every chat-specific field that downstream
    # code (orchestrator, route handlers, tool_display, …) depends on.
    task = chat_task_runtime.create(
        principal=principal,
        task_id=task_id,
        meta={
            'convId': conv_id,
            'msg_count': len(messages or []),
            'userId': owner_user_id,
        },
    )
    task.update({
        'convId': conv_id, 'messages': messages, 'config': config,
        '_userId': owner_user_id,
        '_profileScope': str(owner_user_id),
        # Stable assistant message id, minted CLIENT-SIDE before the send
        #   POST and shipped in config.assistantMsgId. The frontend stamps the
        #   same id on the streaming bubble (data-msg-id), so live progressive
        #   translation frames (incremental._Acc._push_progressive) can route
        #   to the still-streaming message — which has no DB index yet. Also
        #   reused as the final commit's msg_id so the in-stream preview and the
        #   committed translation address the SAME message. Empty for non-UI /
        #   external callers → live preview is simply skipped (no regression).
        '_assistantMsgId': (config or {}).get('assistantMsgId') or '',
        # Conversation lifecycle identity. Executor task ids remain plumbing;
        # public state is addressed only by this stable turn / attempt pair.
        '_turnId': (config or {}).get('_turnId') or '',
        '_attemptId': (config or {}).get('_attemptId') or '',
        '_turnActor': (config or {}).get('_turnActor') or 'assistant',
        '_turnKind': (config or {}).get('_turnKind') or 'reply',
        # Keep TaskRuntime's lifecycle truth: registration/binding is pending;
        # the physical agent worker owns the transition to running.
        'status': 'pending',
        'content': '', 'thinking': '', 'error': None,
        'aborted': False, 'toolRounds': [],
        'content_lock': threading.Lock(),

        # MCP image originals are persisted immediately; only bounded refs are
        # retained here and projected with the authoritative assistant Turn.
        '_mcpImages': list((config or {}).get('checkpointImages') or []),
        '_mcpImageBytes': sum(
            max(0, int(float(item.get('sizeKB') or 0) * 1024))
            for item in ((config or {}).get('checkpointImages') or [])
            if isinstance(item, dict)
        ),
        '_mcpMediaLock': threading.Lock(),
        '_contentEpoch': 0,
        'finishReason': None, 'usage': None, 'toolSummary': None,
        'phase': None,                # current phase for polling fallback
        # Timing anchor: when the task was created (route thread). Used by
        #   run_task / stream_llm_response to log queue-wait, prep time, and
        #   time-to-first-token so the "waiting" window can be analysed.
        '_t_created': time.time(),
        'lastUserQuery': last_user_query,
        '_initial_msg_count': len(messages or []),  # cross-talk detection
        '_premature_retry_count_phase': 0,
        # '_force_rotate_pair' is set transiently by analyse_stream_result
        # and consumed (cleared) by stream_llm_response on the next call.
    })
    # Turn-native browser commands carry the same structured model reference
    # as the native API. Bind one owner-scoped v2 candidate group before any
    # durable-at-birth write or worker launch; ordinary chat cannot pin a
    # Connection/Deployment directly.
    _model_ref = config.get('modelRef')
    if isinstance(_model_ref, dict):
        _route_group = None
        _route_bound = False
        try:
            from lib.model_routing import (
                ModelRoutingRepository,
                OwnerBoundary,
                RoutePolicy,
                mint_routed_slot_group,
                parse_native_model_selection,
            )
            _routing = config.get('routing')
            _routing = dict(_routing) if isinstance(_routing, dict) else {}
            _preferred = config.get('preferredProviderId')
            if _preferred and not _routing.get('preferred_provider_id'):
                _routing['preferred_provider_id'] = str(_preferred)
            _selection = parse_native_model_selection({
                'model': _model_ref,
                'routing': _routing,
            })
            _required_caps = {'text'}
            if config.get('thinkingDepth') not in (None, '', 'off'):
                _required_caps.add('thinking')
            for _message in messages or []:
                _blocks = (_message.get('content')
                           if isinstance(_message, dict) else None)
                if isinstance(_blocks, list) and any(
                        isinstance(_block, dict)
                        and _block.get('type') in {'image', 'image_url'}
                        for _block in _blocks):
                    _required_caps.add('vision')
                    break
            _required_context = _routing.get('required_context') or 1
            if (isinstance(_required_context, bool)
                    or not isinstance(_required_context, int)
                    or _required_context < 1):
                raise ValueError('routing.required_context must be a positive integer')
            _price_budget = _routing.get('price_budget') or {}
            if not isinstance(_price_budget, dict):
                raise ValueError('routing.price_budget must be an object')

            def _route_price_budget(name):
                _value = _price_budget.get(name)
                if _value is None:
                    return None
                if (isinstance(_value, bool)
                        or not isinstance(_value, (int, float))
                        or _value < 0):
                    raise ValueError(
                        'routing.price_budget.%s must be non-negative' % name)
                return float(_value)

            _route_group = mint_routed_slot_group(
                ModelRoutingRepository(),
                OwnerBoundary.create(owner_user_id, principal.tenant_id),
                _selection,
                policy=RoutePolicy(
                    required_capabilities=frozenset(_required_caps),
                    required_context=_required_context,
                    max_input_price=_route_price_budget('max_input'),
                    max_output_price=_route_price_budget('max_output'),
                    price_currency=str(_price_budget.get('currency') or 'USD'),
                    cache_affinity_connection_id=str(
                        _routing.get('cache_affinity_connection_id') or ''),
                ),
                owner_tag=f'task:{task_id}',
            )
            task['_pinned_provider_id'] = _route_group.pin_id
            task['_model_routing_group'] = _route_group
            task['_requestedModelRef'] = dict(_model_ref)
            task['model'] = (
                _selection.model.model_id if _selection.model is not None
                else _selection.provider_offering.offering_id)
            from lib.agent_core.execution_session import (
                bind_model_route,
                execution_session_for_task,
            )

            def _dispose_model_routing_group() -> None:
                from lib.model_routing import dispose_routed_slot_group
                dispose_routed_slot_group(_route_group)

            # bind_model_route owns both registration and rollback. Mark the
            # handoff before calling so its failure path cannot be disposed a
            # second time by this outer construction guard.
            _route_bound = True
            bind_model_route(
                execution_session_for_task(task),
                _dispose_model_routing_group,
            )
        except Exception:
            if _route_group is not None and not _route_bound:
                from lib.model_routing import dispose_routed_slot_group
                dispose_routed_slot_group(_route_group)
            chat_task_runtime.discard(task_id)
            raise
    if transient:
        # Headless/embed runtimes intentionally have no durable storage
        # authority. Stamp this before the first phase event is emitted so
        # every persistence choke point can fail closed without importing or
        # contacting the application storage stack.
        task['_inline_messages'] = True
        task['_transientRuntime'] = True
    try:
        from lib.log import req_id as _current_request_id
        task['_requestId'] = _current_request_id() or task.get('_requestId', '')
    except Exception as e:
        logger.debug('[Task %s] request correlation capture failed: %s',
                     task_id[:8], e)
    # ── Ingress affinity capture ──────────────────────────────────────
    # The browser derives this key before task creation (stable per
    # conversation, random only without one) and the load balancer hashes it.
    # Persisting it with the durable-at-birth row lets /active on
    # ANY replica hand a reloading/second-device client the exact same routing
    # key. It is routing metadata only (never an auth credential).
    #
    # Background children inherit their conversation's last direct key from
    # the shared runtime store. Only a task created in an actual request is
    # marked reconnectable here: that excludes invisible VU/inline carriers
    # from the cross-replica DB projection while the local registry continues
    # to apply its richer is_carrier_task predicate.
    _direct_affinity = False
    _affinity_key = ''
    try:
        from quart import has_request_context, request
        if has_request_context():
            _affinity_key = (
                request.headers.get('X-Tofu-Affinity-Key', '') or '')[:256]
            _direct_affinity = bool(_affinity_key)
    except Exception as e:
        logger.debug('[Task %s] request affinity capture failed: %s',
                     task_id[:8], e)
    if not transient:
        try:
            from lib.runtime_state_store import get_store
            _affinity_store = get_store()
            if not _affinity_key and conv_id:
                _affinity_key = (
                    _affinity_store.get_value('conv-affinity', conv_id) or '')[:256]
            if _affinity_key and conv_id:
                # Mapping retention is not a task deadline; it only permits a
                # future child/reconnect to route to the existing owner.
                _affinity_store.set_value(
                    'conv-affinity', conv_id, _affinity_key, 365 * 24 * 3600)
        except Exception as e:
            logger.debug('[Task %s] shared affinity mirror failed: %s',
                         task_id[:8], e)
    if _affinity_key:
        task['_affinityKey'] = _affinity_key
    task['_reconnectable'] = _direct_affinity

    # Durable-at-birth: write the task_results row AT CREATION
    #   (status='pending', empty content/thinking). The running-checkpoint
    #   writers only fire on content/thinking deltas and per-round boundaries,
    #   so a task killed by a server restart BEFORE its first delta left NO row
    #   at all — and the cold-replay / poll-DB / startup-recovery stale-scan
    #   all found NOTHING (the ms43foj3 incident: resume task killed 87s in,
    #   R1 pure tool_calls → zero content/thinking deltas →
    #   checkpoint_task_partial's empty-guard no-op'd every time → poll and
    #   stream returned 404 'Task not found' → the frontend minted a terminal
    #   error bubble for what was really a transport-level task loss). With
    #   the row existing from second 0, every one of those readers resolves
    #   the task to its real state (running → interrupted after recovery)
    #   instead of a 404. Best-effort: a write failure must never break task
    #   creation; the checkpoint/persist writers upsert over it last-wins.
    if not transient:
        try:
            _birth_meta = {}
            _bcfg = config or {}
            if _bcfg.get('model'):
                _birth_meta['model'] = _bcfg['model']
            if _bcfg.get('preset'):
                _birth_meta['preset'] = _bcfg['preset']
            if _bcfg.get('thinkingDepth'):
                _birth_meta['thinkingDepth'] = _bcfg['thinkingDepth']
            if task.get('_affinityKey'):
                _birth_meta['affinityKey'] = task['_affinityKey']
            if task.get('_reconnectable'):
                _birth_meta['reconnectable'] = True
            if task.get('_userId') not in (None, ''):
                _birth_meta['userId'] = str(task['_userId'])
            if task.get('_requestId'):
                _birth_meta['requestId'] = task['_requestId']
            _upsert_task_row(
                task, conv_id or '', content='', thinking='', status='pending',
                error_json=None, tr_json=None,
                meta_json=(json.dumps(_birth_meta, ensure_ascii=False)
                           if _birth_meta else None))
        except Exception as e:
            logger.warning(
                '[Task %s] durable-at-birth row write failed (non-fatal): %s',
                task_id[:8], e)

    # Register as the LATEST task for this conversation — freshness guard
    if conv_id and not transient:
        _record_latest_task(conv_id, task_id)
        # Supersede invariant (see docstring): abort any other running task
        #   for this conv so "a new task replaced the old one without aborting
        #   it" is structurally impossible. Registered as latest FIRST so the
        #   superseded tasks' freshness guard classifies their late writes as
        #   expected (superseded_by_new_task), not as the unexpected-WARNING
        #   never-aborted branch. Best-effort: never let it break creation.
        if supersede:
            try:
                abort_running_tasks_for_conv(
                    conv_id,
                    user_id=int(task_user_id(task)),
                    exclude_task_id=task_id,
                )
            except Exception as e:
                logger.warning('[Task %s] supersede abort sweep failed: %s',
                               task_id[:8], e, exc_info=True)
    logger.info('[Task %s] Created for conv=%s lastUserQuery=%r', task_id[:8], conv_id, last_user_query[:80])
    return task


def discard_task(task_id: str, conv_id: str | None = None) -> None:
    """Remove a non-streaming carrier/holder task from the active registry.

    Some flows use ``create_task`` purely as a message container for a
    synchronous reporter sub-turn (e.g. ``autopilot.summarize_run``) — the
    carrier is NEVER spawned and NEVER reaches a terminal status, so it would
    otherwise linger forever as a phantom ``status='running'`` row that
    ``/api/chat/active`` reports and the frontend orphan-recovery turns into a
    permanently-stuck "Waiting…" placeholder. (TTL cleanup only evicts
    terminal tasks, so a never-finalized pending carrier is immortal.)

    This unregisters the task and clears any ``_conv_latest_task``
    entry it claimed, so the carrier is invisible to every reconnect path. Safe
    to call unconditionally (idempotent, best-effort).
    """
    # Observability ( ③-1): this is the ONLY registry pop for
    # non-terminal chat tasks. A live task that vanished from the registry
    # while its worker thread kept running (fb6d1f8d / 7ddbc751, 2026-08-01)
    # left zero fingerprints — log every pop with the caller so the next
    # evaporation leaves a trail. Rare path; the frame read is cheap enough.
    try:
        import sys as _sys
        _caller = _sys._getframe(1).f_code.co_name
    except Exception as e:
        logger.debug('[Manager] discard_task: caller-frame read failed: %s', e)
        _caller = '?'
    _popped = chat_task_runtime.discard(task_id)
    if _popped is not None:
        # Tombstone the popped dict so manager.append_event's re-adopt seam
        # (path B, ) refuses it: a DELIBERATE discard —
        # e.g. the autopilot VU carrier's designed retirement — must never
        # be resurrected as a phantom 'running' row by a trailing event.
        _popped['_discarded_at'] = time.time()
        _route_group = _popped.pop('_model_routing_group', None)
        if (_route_group is not None
                and not isinstance(
                    _popped.get('_executionSession'), ExecutionSession)):
            try:
                from lib.model_routing import dispose_routed_slot_group
                dispose_routed_slot_group(_route_group)
            except Exception as exc:
                logger.warning(
                    '[Manager] discarded model-routing group cleanup failed: %s',
                    exc,
                )
    logger.info('[Manager] discard_task: task=%s conv=%s popped=%s caller=%s',
                (task_id or '?')[:8], (conv_id or '')[:8],
                bool(_popped), _caller)
    if _popped:
        try:
            from lib.observability import record_registry_eviction
            record_registry_eviction('chat', 'discard')
        except Exception as exc:
            logger.debug('[Manager] discard_task metric skipped: %s', exc)
    if conv_id:
        # Clears the store mirror too — a local-only delete leaves the
        # store-backed _latest_task_for_conv returning this corpse for up to
        # 1h (TTL), which is the msb6ohqi 2026-08-02 stall class.
        _clear_latest_task(conv_id, expect_task_id=task_id)


# ═══════════════════════════════════════════════════════════════════════════
#  Abort tombstone channel ( ③-3)
# ═══════════════════════════════════════════════════════════════════════════

_DB_TOMBSTONE_POLL_S = 5.0  # abort_check 回读 DB tombstone 的最小间隔


def _db_abort_tombstoned(task_id: str, *, user_id: int) -> bool:
    """True when the owner's task-result projection carries an abort signal."""
    if not task_id:
        return False
    try:
        from lib.storage import get_storage_client
        result = get_storage_client().query(
            'task_results.abort_requested', {
                'task_id': task_id, 'user_id': int(user_id)})
        return bool((result or {}).get('requested'))
    except Exception as e:
        logger.debug('[Manager] abort-signal probe failed task=%s: %s',
                     task_id[:8], e)
        return False


def _write_abort_tombstone_row(
    task_id: str, source: str, *, user_id: int,
) -> bool:
    """Atomically signal an owner-scoped running task across processes."""
    try:
        from lib.storage import get_storage_client
        result = get_storage_client(write=True).command(
            'task_results.abort', {
                'task_id': task_id,
                'user_id': int(user_id),
                'source': source,
            }, None)
        return bool(result.get('signaled'))
    except Exception as e:
        logger.debug('[Manager] durable abort signal failed task=%s: %s',
                     task_id[:8], e)
        return False


def plant_abort_tombstone(
    task_id: str, *, source: str, user_id: int,
) -> bool:
    """Record an abort request for a task the registry has lost.

    Returns True only when a LIVE (pending/running) task_results row exists
    — the endpoint uses that to distinguish “signal planted” from a genuine
    404 (no such task / already terminal).
    """
    if not task_id:
        return False
    live = _write_abort_tombstone_row(
        task_id, source, user_id=int(user_id))
    if not live:
        return False
    with _abort_tombstones_lock:
        if len(_abort_tombstones) >= _ABORT_TOMBSTONES_CAP:
            logger.warning('[Manager] abort tombstone set at cap %d — clearing '
                           'before insert (pathological volume)',
                           _ABORT_TOMBSTONES_CAP)
            _abort_tombstones.clear()
        _abort_tombstones.add(task_id)
    logger.info('[Manager] ⚠️ abort tombstone planted for task=%s (source=%s) — '
                'registry lost this task; the live worker consumes the mark '
                'at its next abort poll', task_id[:8], source)
    try:
        from lib.log import audit_log as _audit
        _audit('task_abort_tombstone', task_id=task_id, source=source)
    except Exception as _ae:
        logger.debug('[Manager] tombstone audit failed: %s', _ae)
    return True


def plant_abort_tombstones_for_conv(
    conv_id: str, *, source: str, user_id: int,
) -> int:
    """Tombstone every running DB row for ``conv_id`` the registry has lost.

    Rows still IN the registry are skipped — the plain supersede/abort sweep
    already flags them cooperatively.
    """
    if not conv_id:
        return 0
    try:
        from lib.storage import get_storage_client
        result = get_storage_client().query(
            'task_results.summary_list', {
                'conv_id': conv_id, 'user_id': int(user_id),
                'status': 'running', 'limit': 1000,
                'scan_limit': 10_000, 'order_by': 'updated_at_asc',
            }, deadline=30) or {}
        rows = result.get('records') or []
        if result.get('capped'):
            logger.warning(
                '[Manager] conv abort-signal summary scan hit its 10000-row '
                'work cap conv=%s', conv_id[:8])
    except Exception as e:
        logger.warning('[Manager] conv abort-signal scan failed conv=%s: %s',
                       conv_id[:8], e)
        return 0
    n = 0
    live_ids = chat_task_runtime.task_ids()
    for row in rows:
        tid = row.get('key') or row.get('task_id')
        if tid and tid not in live_ids:
            if plant_abort_tombstone(
                    tid, source=source, user_id=int(user_id)):
                n += 1
    if n:
        logger.info('[Manager] conv=%s tombstoned %d registry-lost running '
                    'task(s)', conv_id[:8], n)
    return n


def has_abort_tombstone(task_id: str) -> bool:
    with _abort_tombstones_lock:
        return task_id in _abort_tombstones


def make_task_abort_check(task: dict, *, nonblocking_storage: bool = False):
    """Build the abort_check closure for dispatch/stream retry loops.

    ANDs three channels ( ③-3):
      1. the cooperative in-memory flag ``task['aborted']`` — the normal path;
      2. the in-process tombstone set — an abort that arrived while the task
         was MISSING from the registry (the 2026-08-01 evaporation family);
      3. a throttled (>= ``_DB_TOMBSTONE_POLL_S``) read-back of the
         task_results metadata tombstone — the cross-process leg.
    The dispatch loop polls this every 429/retry cycle, so a tombstoned ghost
    dies at its next cycle with the normal AbortedError unwind.

    ``nonblocking_storage=True`` is the provider-ingress mode.  It performs the
    same throttled DB probe on at most one task-local daemon thread and returns
    the last observed answer immediately.  In-memory Stop remains synchronous;
    only the cross-process observer may arrive one poll later.  This prevents a
    slow storage socket from pausing an otherwise healthy upstream model stream.
    """
    task_id = (task or {}).get('id', '')
    owner_user_id = task_user_id(task)
    _st = {
        'hit': False,
        'last_db': 0.0,
        'probe_in_flight': False,
    }
    _st_lock = threading.Lock()

    def _probe_storage() -> None:
        try:
            requested = _db_abort_tombstoned(
                task_id, user_id=owner_user_id)
            if requested:
                with _st_lock:
                    _st['hit'] = True
                logger.info('[Task %s] abort tombstone consumed (async db '
                            'channel) — aborting', task_id[:8])
        finally:
            with _st_lock:
                _st['probe_in_flight'] = False

    def _schedule_storage_probe(now: float) -> None:
        with _st_lock:
            if _st['probe_in_flight']:
                return
            _st['probe_in_flight'] = True
            _st['last_db'] = now
        try:
            worker = threading.Thread(
                target=_probe_storage,
                name=f'tofu-abort-probe-{task_id[:8] or "unknown"}',
                daemon=True,
            )
            worker.start()
        except Exception as e:
            with _st_lock:
                _st['probe_in_flight'] = False
            logger.debug('[Manager] abort-signal async probe start failed '
                         'task=%s: %s', task_id[:8], e)

    def _check() -> bool:
        with _st_lock:
            if _st['hit']:
                return True
        if task.get('aborted'):
            return True
        with _abort_tombstones_lock:
            if task_id in _abort_tombstones:
                _st['hit'] = True
                logger.info('[Task %s] abort tombstone consumed (in-memory '
                            'channel) — aborting', task_id[:8])
                return True
        now = time.monotonic()
        with _st_lock:
            db_due = now - _st['last_db'] >= _DB_TOMBSTONE_POLL_S
        if db_due:
            if nonblocking_storage:
                _schedule_storage_probe(now)
            else:
                with _st_lock:
                    _st['last_db'] = now
                if _db_abort_tombstoned(task_id, user_id=owner_user_id):
                    with _st_lock:
                        _st['hit'] = True
                    logger.info('[Task %s] abort tombstone consumed (db channel) '
                                '— aborting', task_id[:8])
                    return True
        with _st_lock:
            if _st['hit']:
                return True
        return False

    return _check


def make_provider_abort_check(task: dict):
    """Provider-stream abort check with a non-blocking storage observer."""
    return make_task_abort_check(task, nonblocking_storage=True)


def write_carrier_terminal_row(task, status: str) -> None:
    """Persist a terminal ``task_results`` row for a synchronous CARRIER task.

    The autopilot VU sub-task (and any future row-producing carrier) runs
    under ``_flow_managed=True``, which BY DESIGN suppresses the
    orchestrator's terminal-status flip + ``persist_task_result`` — the
    carrier's own finalize early-returns. Its per-round
    ``checkpoint_task_partial`` writes therefore leave the row at
    ``status='running'`` forever (the ms2gipv5 zombie generator,
    ): the in-memory ``discard_task`` only cleans the registry,
    and the next startup recovery sweep collects the stale row as a
    crash-interrupted turn.

    The carrier's LIFECYCLE OWNER (``autopilot.run_virtual_user``'s
    finally) calls this right after ``discard_task`` so the row reaches a
    terminal state in the same breath as the registry cleanup. Idempotent,
    last-writer-wins (keyed on task_id, same ``_upsert_task_row`` channel as
    ``_write_aborted_terminal_floor``); best-effort — a settle failure must
    never break the owner's finally.

    ``status`` is derived by the caller from the carrier's end state:
    'done' (turn completed — the normal path), 'aborted' (parent abort /
    real-message preemption), 'error' (died before any finish reason).
    """
    if status not in ('done', 'aborted', 'error'):
        logger.warning('[Task %s] write_carrier_terminal_row: unexpected status %r '
                       '— defaulting to done', (task.get('id') or '?')[:8], status)
        status = 'done'
    try:
        conv_id = task.get('convId', '') or ''
        tr_json = (None if _tool_rounds_have_dedicated_home(task)
                   else json.dumps(_merge_tool_rounds(task), ensure_ascii=False))
        meta = build_result_meta(task)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _upsert_task_row(task, conv_id, content=task.get('content') or '',
                         thinking=task.get('thinking') or '', status=status,
                         error_json=error_json, tr_json=tr_json, meta_json=meta_json)
        logger.info('[Task %s] conv=%s Carrier terminal row settled: status=%s',
                    task['id'][:8], conv_id[:8], status)
    except Exception as e:
        logger.warning('[Task %s] Failed to settle carrier terminal row: %s',
                       (task.get('id') or '?')[:8], e, exc_info=True)


def list_running_tasks(
    exclude_conv_id: str | None = None,
    *,
    user_id: int | None = None,
) -> list[dict]:
    """Return one entry per CONVERSATION with genuinely-live running work.

    Used by the self-update restart guard to refuse a process re-exec that
    would kill sibling conversations' in-flight work. A restart is an
    unconditional ``os.execv`` of the whole server, so EVERY running task
    dies with it — this lets the caller detect that and require an explicit
    override.

    Three filters make the count reflect reality rather than registry cruft, so
    the guard never blocks a restart for work that is not actually running:

      * **Carrier filter (same judge as ``/api/chat/active``).** A non-streaming
        CARRIER/HOLDER (``is_carrier_task``: the autopilot VU sub-task or an
        inline reporter holder) is ``status='running'`` while it executes but is
        invisible to the frontend by design (the reconnect endpoint hides it,
        the sidebar never lights a dot for it). Counting it made the restart
        dialog report "N conversations running" that the user could see nowhere.
      * **Running activity filter (same judge as the reaper).** A running task
        whose BOTH
        liveness clocks (``_t_last_event`` and ``_dispatch_heartbeat``) have
        been silent past ``_stuck_task_max_silent_secs()`` is WEDGED — the exact
        signal ``reap_stuck_running_tasks`` uses to force-fail it. Such a task
        is excluded here too, so a just-died zombie does not block a restart for
        the whole 30-minute reaper window (it would otherwise be counted until
        the next reaper tick flips it terminal). ``status=='running'`` alone is
        NOT liveness — it is exactly the false signal that produced the "63
        other conversations have running tasks" phantom. If the reaper is
        disabled (threshold ``<=0``) no task is treated as wedged (mirrors the
        reaper), so behaviour is unchanged there.
      * **Per-conversation dedup.** A single conversation (autopilot especially)
        can spawn dozens of tasks; counting per-task turned "3 busy convs" into
        "63". Entries are keyed by ``convId`` so the count is the number of
        distinct conversations a restart would interrupt. Tasks with no convId
        (headless / external callers) are NOT collapsed — each stays its own
        entry keyed on its task id.

    Args:
        exclude_conv_id: When set, live tasks belonging to this conversation
            are omitted (the caller triggering the restart doesn't count its
            own conversation against itself).
        user_id: When set, only tasks owned by this user are counted. The
            sidebar busy projection is owner-scoped, so it must never light a
            dot for another user's conversation.

    Returns:
        A list of ``{'taskId', 'convId', 'elapsed'}`` dicts, one per distinct
        live conversation (representative = the earliest-created live task of
        that conv). Best-effort snapshot taken through ``TaskRuntime``.
    """
    try:
        from lib.tasks_pkg.manager._maintenance import _stuck_task_max_silent_secs
        max_silent = _stuck_task_max_silent_secs()
    except Exception as e:
        logger.debug('[Manager] list_running_tasks: reaper threshold lookup failed '
                     '(%s) — skipping activity filter', e)
        max_silent = 0

    now = time.time()
    # Keyed by dedup identity so one conversation counts once. Keep the
    # earliest-created live task as the representative (stable, oldest work).
    by_key: dict[str, tuple[float, dict]] = {}
    for t in chat_task_runtime.snapshot():
        tid = str(t.get('id') or '')
        if t.get('status') not in ('pending', 'running') or t.get('aborted'):
            continue
            # Skip non-streaming CARRIER/HOLDER tasks (VU sub-task / inline
            #   reporter) — same predicate GET /api/chat/active uses to hide
            #   them from reconnect. Without this a background autopilot VU
            #   carrier (convId='', never surfaced in the sidebar) counted as
            #   a "live conversation" and made the restart dialog claim work
            #   was in flight that the user could not see anywhere.
        if is_carrier_task(t):
            continue
        if user_id is not None and task_user_id(t) != user_id:
            continue
        conv = t.get('convId') or ''
        if exclude_conv_id and conv == exclude_conv_id:
            continue
        created = t.get('created_at', now)
            # Activity filter — exclude WEDGED tasks (both clocks stale), the
            # same predicate reap_stuck_running_tasks uses. Either clock fresh
            # = alive. Disabled (max_silent<=0) → never treat as wedged.
        # Pending work owns a real queue position and has not entered the
        # reaper's running-only liveness domain yet. Its queue wait can exceed
        # the worker-silence threshold without becoming a zombie.
        if max_silent > 0 and t.get('status') == 'running':
            last_event = t.get('_t_last_event', created)
            heartbeat = t.get('_dispatch_heartbeat', created)
            if (now - last_event) >= max_silent and (now - heartbeat) >= max_silent:
                continue
            # Dedup key: real conversations collapse by convId; convId-less
            # tasks each stay distinct (keyed on their unique task id).
        key = conv if conv else ('\x00task:' + tid)
        entry = {
            'taskId': tid,
            'convId': conv,
            'elapsed': round(now - created, 1),
        }
        prior = by_key.get(key)
        if prior is None or created < prior[0]:
            by_key[key] = (created, entry)
    return [entry for _created, entry in by_key.values()]


def notify_terminal_conversation_change(task) -> None:
    """Wake conversation subscribers after a legacy task reaches terminal."""
    try:
        conv_id = (task or {}).get('convId') or ''
        if not conv_id:
            return
        from lib.conversations import notify_conv_changed
        notify_conv_changed(conv_id, rev=None, user_id=task_user_id(task))
    except Exception as e:
        logger.debug('[Manager] terminal busy notify failed task=%s: %s',
                     ((task or {}).get('id') or '?')[:8], e)


def abort_running_tasks_for_conv(
    conv_id: str,
    *,
    user_id: int,
    exclude_task_id: str | None = None,
    reason: str = 'superseded_by_new_task',
) -> int:
    """Abort all pending/running tasks for a conversation except one.

    Called when starting a new task (send/regenerate/edit) to ensure the old
    task stops writing to the conversation DB. Returns the count of aborted tasks.

    Non-turn tasks still use conversation-wide supersession. Turn-native tasks
    are protected independently by stable attempt identity and projection CAS.
    """
    abort_time = time.time()
    abort_reason = str(reason or 'superseded_by_new_task')

    def _matches(task):
        return (
            task.get('convId') == conv_id
            and task_user_id(task) == int(user_id)
            and task.get('status') in ('pending', 'running')
            and task.get('id') != exclude_task_id
            and not task.get('aborted')
        )

    def _mark_aborted(task):
        task['aborted'] = True
        task['_abort_timestamp'] = abort_time
        task['_abort_reason'] = abort_reason
        abort_event = task.get('abort_event')
        if abort_event is not None:
            abort_event.set()

    _aborted_tasks = chat_task_runtime.update_matching(
        predicate=_matches,
        updater=_mark_aborted,
    )
    aborted = len(_aborted_tasks)
    queued_finalized_ids: set[str] = set()
    for t in _aborted_tasks:
        tid = str(t.get('id') or '')
        logger.info(
            '[Task %s] conv=%s ⚠️ AUTO-ABORTED: reason=%s replacement=%s — '
            'content=%dchars elapsed=%.1fs',
            tid[:8], conv_id[:8], abort_reason,
            (exclude_task_id or '?')[:8],
            len(t.get('content') or ''),
            time.time() - t.get('created_at', time.time()),
        )
        try:
            from lib.log import audit_log as _audit
            _audit(
                'task_abort',
                task_id=tid,
                conv_id=conv_id,
                reason=abort_reason,
                superseding_task_id=exclude_task_id or '',
                content_chars=len(t.get('content') or ''),
                elapsed_s=round(
                    time.time() - t.get('created_at', time.time()), 2),
            )
        except Exception as _aerr:
            logger.debug('[Manager] audit_log task_abort failed: %s', _aerr)
        if str(t.get('status') or '') == 'pending':
            try:
                from lib.tasks_pkg.spawn import cancel_queued_task

                if cancel_queued_task(tid):
                    from lib.tasks_pkg.manager._terminal import (
                        finalize_chat_task_aborted,
                    )

                    if finalize_chat_task_aborted(t) is not None:
                        queued_finalized_ids.add(tid)
            except Exception as error:
                logger.warning(
                    '[Task %s] queued supersede cancellation failed: %s',
                    tid[:8], error, exc_info=True,
                )
    # Zombie-task terminal floor (outside tasks_lock — this does DB I/O).
    #   An aborted task normally reaches a terminal task_results row only when
    #   ITS OWN thread runs finalize/persist. A thread that is wedged (e.g. a
    #   stream that never received a token, 0 events for hours) never gets
    #   there, so on a server restart (in-memory tasks cleared) a poll finds
    #   neither memory nor DB → 404 and the user loses the turn. Writing an
    #   aborted floor NOW guarantees a durable terminal state regardless of
    #   whether the thread ever unwedges. Idempotent: if the thread later does
    #   finalize, persist_task_result overwrites this floor with the real
    #   final content/status (last-writer-wins, keyed on task_id).
    for _t in _aborted_tasks:
        if str(_t.get('id') or '') not in queued_finalized_ids:
            _write_aborted_terminal_floor(_t)
    if aborted:
        logger.info('[Manager] conv=%s Auto-aborted %d stale task(s) before starting new task %s',
                    conv_id[:8], aborted, (exclude_task_id or '?')[:8])
        # ── pt_conv_state_ssot P3: task lifecycle stop broadcast ──
        # Aborting a stale task flips ``t['aborted']=True`` but nobody
        # calls notify_conv_changed for this conv — the frame carrying
        # the fresh runningTaskIds projection (which no longer includes
        # the aborted tid, since snapshot_running_by_conv filters both
        # status!=running AND aborted) never leaves the server, so a
        # sibling device holding the busy dot for the superseded task
        # sees it stay lit until its next poll (25/90s later). Emit ONE
        # notify frame for the whole sweep (consolidates a multi-abort
        # into a single frame, not one per aborted tid). Fail-open: a
        # push transport error must never break the abort path.
        try:
            from lib.conversations.change_notifications import notify_conv_changed
            notify_conv_changed(conv_id, rev=None, user_id=int(user_id))
        except Exception as _ne:
            logger.warning(
                '[Manager] conv=%s supersede-abort notify skipped: %s',
                conv_id[:8], _ne)
    return aborted
def quiesce_running_tasks(reason: str = 'server_shutdown') -> int:
    """Signal every pending/running task to abort at server shutdown.

    The abort flag is cooperative: the orchestrator's abort seam checks
    ``task['aborted']`` between rounds / after each stream chunk / between
    tools, so a carrier stops issuing new LLM calls and DB writes soon after
    this is set. Setting it BEFORE the atexit ``stop_local_pg_if_owned`` hook
    fires is what prevents the shutdown cascade: without it, live carriers keep
    calling ``get_thread_db`` while PG is being stopped, producing the
    ``FATAL: the database system is shutting down`` + ``cannot schedule new
    futures after interpreter shutdown`` traceback storm.

    Best-effort, never raises. Returns the count of tasks newly marked aborted.
    """
    aborted = 0
    try:
        abort_time = time.time()

        def _mark_quiesced(task):
            task['aborted'] = True
            task['_abort_timestamp'] = abort_time
            task['_abort_reason'] = reason
            abort_event = task.get('abort_event')
            if abort_event is not None:
                abort_event.set()

        aborted = len(chat_task_runtime.update_matching(
            predicate=lambda task: (
                task.get('status') in ('pending', 'running')
                and not task.get('aborted')
            ),
            updater=_mark_quiesced,
        ))
    except Exception as e:
        logger.warning('[Manager] quiesce_running_tasks failed: %s', e)
        return aborted
    if aborted:
        logger.info('[Manager] Quiesced %d running task(s) for shutdown (reason=%s)',
                    aborted, reason)
    return aborted


def _write_aborted_terminal_floor(task) -> None:
    """Persist a terminal ``status='aborted'`` row to ``task_results`` for a
    just-aborted task, so a later poll (even after a restart that cleared the
    in-memory registry) resolves to a terminal state instead of a 404.

    Best-effort and idempotent — reuses the shared ``_upsert_task_row`` (keyed
    on task_id), so a subsequent real finalize by the task's own thread simply
    overwrites this floor with the authoritative final content/status. Only the
    partial content accumulated so far is written; that is strictly better than
    losing the turn to a 404.
    """
    try:
        conv_id = task.get('convId', '') or ''
        tr_json = (None if _tool_rounds_have_dedicated_home(task)
                   else json.dumps(_merge_tool_rounds(task), ensure_ascii=False))
        meta = build_result_meta(task)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _upsert_task_row(task, conv_id, content=task.get('content') or '',
                         thinking=task.get('thinking') or '', status='aborted',
                         error_json=error_json, tr_json=tr_json, meta_json=meta_json)
        logger.debug('[Task %s] conv=%s Wrote aborted terminal floor to task_results',
                     task['id'][:8], conv_id[:8])
    except Exception as e:
        logger.warning('[Task %s] Failed to write aborted terminal floor: %s',
                       task.get('id', '?')[:8], e, exc_info=True)

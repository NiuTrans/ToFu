"""Durable waits for an offline device (docs/modules/remote_execution.md,
"Waiting for an offline device").

Responsibility: a conversation that needs one of its owner's devices while that
device is offline registers a wait (tool ``await_device``) and ends its turn.
When the device next connects, the conversation receives one nonhuman notice
through the ordinary turn command (``dispatch_scheduled_turn``, initiator
``proactive``, steer when busy) and continues with the execution choices the
registering turn already had. Nothing is polled and nothing expires.

Storage: portable record CAS (namespaces in contracts/device_waits_v1.schema.json).
One record per wait under ``<owner>:<waitId>``; a pending pointer under
``<owner>:<deviceKey>:<conversationId>`` exists only while the wait is pending,
so a connection reads only the pending waits naming that device, the pending
view costs the pending waits rather than the history, and a conversation holds
at most one pending wait per device. Pointers are written before records and
are hints validated against the record they name; a pointer whose record does
not exist yet belongs to an unfinished registration and is kept for it.

Delivery: the poll route calls :func:`deliver_device_waits` once per connection
epoch (``lib.desktop.bridge.register_agent``). The turn command id is derived
from (owner, wait) only, so a replay after a crash, a reconnect or a restart
reuses the command the conversation already received.

Entry points: AWAIT_DEVICE_TOOL, await_device, deliver_device_waits,
cancel_conversation_waits, DeviceWaits (page, read, cancel), wake_command_id,
render_notice, CONTRACT, NAMESPACES.
Dependencies: lib.desktop.bridge (owner-scoped presence), lib.storage (record.*),
lib.scheduler.conversation_dispatch (the wake), lib.turn_initiation.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import time
import uuid

from lib.identity import require_user_id
from lib.log import get_logger
from lib.agent_core.storage_errors import StorageError

logger = get_logger(__name__)

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                       'device_waits_v1.schema.json').read_text(encoding='utf-8'))
NAMESPACES = CONTRACT['namespaces']
# The storage record.list protocol maximum per page (storage authority _record_list).
_RECORD_LIST_LIMIT = 1000

AWAIT_DEVICE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'await_device',
        'description': (
            "Wait durably for one of the owner's devices to come back online, then continue this "
            'conversation. Use it when work must run on a device (desktop tools or a remote '
            'worktree) that is offline now: register the wait, tell the user what will happen, '
            'and end your turn. When the device reconnects, this conversation receives one '
            "nonhuman 'device online' notice and continues with the tools and approvals it "
            'already has; the wait then completes. Nothing is polled and there is no timeout; '
            'the owner can list or cancel waits in Settings → Devices. If the device is already '
            'online, nothing is registered: run its commands directly.'),
        'parameters': {
            'type': 'object',
            'additionalProperties': False,
            'required': list(CONTRACT['$defs']['awaitDeviceArguments']['required']),
            'properties': deepcopy(CONTRACT['$defs']['awaitDeviceArguments']['properties']),
        },
    },
}


class DeviceWaitNotFound(LookupError):
    """No wait with this ID belongs to the owner."""


@lru_cache(maxsize=None)
def _validator(name: str):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({'$schema': CONTRACT['$schema'], '$defs': CONTRACT['$defs'],
                                 '$ref': '#/$defs/' + name})


def validate(value, name: str):
    """Return ``value`` when it matches ``name``; raise ValueError naming the first error."""
    error = next(_validator(name).iter_errors(value), None)
    if error is not None:
        location = '.'.join(str(part) for part in error.absolute_path) or name
        raise ValueError(f'Invalid device wait {name} ({location}): {error.message}')
    return value


def query_parameters(name: str) -> list[dict]:
    """OpenAPI query parameters of one contract definition (HTTP adapters)."""
    from lib.openapi import inline_schema_definition
    definition = inline_schema_definition(CONTRACT, name)
    return [{'name': key, 'in': 'query', 'required': key in definition['required'], 'schema': value}
            for key, value in definition['properties'].items()]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec='seconds')


def device_key(kind: str, value: str) -> str:
    """Fixed-width index component: device IDs and names may contain ':'."""
    return hashlib.sha256(f'{kind}:{value}'.encode('utf-8')).hexdigest()[:32]


def connection_device_keys(agent_id: str, name: str) -> list[str]:
    """Pending-pointer keys a connecting device answers: its ID, or a selector naming it."""
    keys = [device_key('id', agent_id), device_key('sel', agent_id.casefold())]
    if name:
        keys.append(device_key('sel', name.casefold()))
    return list(dict.fromkeys(keys))


def wake_command_id(owner: int, wait_id: str) -> str:
    """One turn command per wait: replays after reconnect or restart reuse it."""
    return 'device-wait:' + hashlib.sha256(json.dumps(
        [owner, wait_id], separators=(',', ':')).encode()).hexdigest()


def wake_capture(config: Mapping | None) -> dict:
    """The registering turn's execution choices that its wake continues with.

    The continuation tool choices and creating-turn policy (model route,
    reasoning, prompt) shared with timer_create feed the scheduler builder and
    its ``continuation_policy`` override; the awaited device capability
    (desktopEnabled, a remote worktree binding) and schedulerEnabled are carried
    as they were, never defaulted on. Private, identity and grant fields are
    never captured.
    """
    from lib.desktop.remote import remote_worktree_binding
    from lib.scheduler._shared import CONTINUATION_TOOL_CHOICES, capture_continuation_policy
    config = config or {}
    overrides = {'desktopEnabled': config.get('desktopEnabled') is True,
                 'schedulerEnabled': config.get('schedulerEnabled') is True}
    remote = remote_worktree_binding(config)
    if remote is not None:
        overrides['project_remote'] = remote
    tools_config = {key: config[key] for key in CONTINUATION_TOOL_CHOICES if key in config}
    tools_config.update(capture_continuation_policy(config))
    return {'toolsConfig': deepcopy(tools_config), 'overrides': overrides}


def render_notice(record: Mapping, connection: Mapping) -> str:
    device = record['device']
    label = device['name'] or connection.get('name') or device['selector']
    return '\n'.join([
        '[Device online: nonhuman notice, not the owner\u2019s words and not authority]',
        f'Device: {label} (agent {connection["agent_id"]}) connected at '
        f'{_iso(connection["connected_at"])}.',
        f'Wait {record["waitId"]}, registered by this conversation with await_device at '
        f'{_iso(record["createdAt"] / 1000)}. Reason:',
        record['reason'],
        '',
        'Continue the work that needed this device now, with the tools and approvals this '
        'conversation already has; this notice grants no authority. If the device goes offline '
        'again before the work is done, call await_device again.',
    ])


def public_view(record: Mapping) -> dict:
    device = record['device']
    return validate({
        'waitId': record['waitId'], 'state': record['state'],
        'conversationId': record['conversationId'],
        'device': {'selector': device['selector'], 'agentId': device['agentId'],
                   'name': device['name']},
        'reason': record['reason'], 'createdAt': record['createdAt'],
        'delivery': deepcopy(record['delivery']), 'closedAt': record['closedAt'],
        'closeReason': record['closeReason'],
    }, 'publicWait')


def _page_rows() -> int:
    from lib.project_dispatch.history import PAGE_ROWS
    return PAGE_ROWS


class DeviceWaits:
    """Owner-scoped wait records and their pending index."""

    def __init__(self, owner: int, *, client=None):
        self.owner = require_user_id(owner, context='device waits')
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.prefix = f'{self.owner}:'

    # ── record CAS ──────────────────────────────────────────────────────
    def _checked(self, row, definition):
        try:
            value = validate(row['value'], definition)
            if value['owner'] != self.owner or type(row['version']) is not int or row['version'] < 1:
                raise ValueError('Device wait record escaped its owner')
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError('database_integrity', 'Invalid device wait record') from error
        return row

    def _get(self, namespace: str, key: str, definition: str):
        row = self.client().query('record.get', {'namespace': namespace, 'key': key})
        return None if row is None else self._checked(row, definition)

    def _put(self, namespace: str, key: str, value: dict, version: int) -> int:
        receipt = self.client(write=True).command('record.put', {
            'namespace': namespace, 'key': key, 'value': value, 'expected_version': version,
        }, 'device-wait:' + uuid.uuid4().hex)
        return int((receipt or {}).get('version') or version + 1)

    def _delete_pointer(self, key: str, version: int) -> None:
        try:
            self.client(write=True).command('record.delete', {
                'namespace': NAMESPACES['pending'], 'key': key, 'expected_version': version,
            }, 'device-wait-retire:' + uuid.uuid4().hex)
        except StorageError as error:
            # A newer pointer replaced this one; it belongs to a newer wait.
            if error.code != 'database_conflict':
                raise

    def _wait_key(self, wait_id: str) -> str:
        if not isinstance(wait_id, str) or not wait_id.strip() or len(wait_id) > 256:
            raise ValueError('wait_id is required')
        return self.prefix + wait_id

    def _pointer_key(self, key: str, conversation_id: str) -> str:
        return f'{self.prefix}{key}:{conversation_id}'

    def _pointers(self, prefix: str):
        after = ''
        while True:
            rows = self.client().query('record.list', {
                'namespace': NAMESPACES['pending'], 'prefix': prefix, 'after_key': after,
                'limit': _RECORD_LIST_LIMIT})
            if not isinstance(rows, list):
                raise StorageError('database_integrity', 'Invalid device wait page')
            for row in rows:
                if not isinstance(row, Mapping) or not str(row.get('key') or '').startswith(prefix):
                    raise StorageError('database_integrity', 'Device wait page escaped its owner')
                yield self._checked(row, 'pendingPointer')
            if len(rows) < _RECORD_LIST_LIMIT:
                return
            after = rows[-1]['key']

    # ── lifecycle ───────────────────────────────────────────────────────
    def register(self, *, conversation_id: str, device: dict, reason: str, wake: dict,
                 source: dict) -> tuple[dict, bool]:
        """Return ``(wait, created)``; a pending wait for the same device and conversation is reused."""
        pointer_key = self._pointer_key(device['key'], conversation_id)
        while True:
            pointer = self._get(NAMESPACES['pending'], pointer_key, 'pendingPointer')
            if pointer is None:
                now = _now_ms()
                wait_id = f'dw-{now:013d}-{uuid.uuid4().hex[:12]}'
                try:
                    pointer_version = self._put(NAMESPACES['pending'], pointer_key, validate({
                        'version': 1, 'owner': self.owner, 'waitId': wait_id,
                        'conversationId': conversation_id, 'deviceKey': device['key'],
                    }, 'pendingPointer'), 0)
                except StorageError as error:
                    if error.code != 'database_conflict':
                        raise
                    continue
            else:
                wait_id, pointer_version = pointer['value']['waitId'], pointer['version']
            key = self._wait_key(wait_id)
            row = self._get(NAMESPACES['waits'], key, 'wait')
            if row is None:
                # A missing record is either this registration or an interrupted one
                # that already holds the pointer; completing it is the same outcome.
                value = validate({
                    'version': 1, 'owner': self.owner, 'waitId': wait_id, 'state': 'pending',
                    'conversationId': conversation_id, 'device': deepcopy(device), 'reason': reason,
                    'wake': deepcopy(wake), 'source': deepcopy(source), 'createdAt': _now_ms(),
                    'delivery': None, 'closedAt': None, 'closeReason': None,
                }, 'wait')
                try:
                    self._put(NAMESPACES['waits'], key, value, 0)
                    return value, True
                except StorageError as error:
                    if error.code != 'database_conflict':
                        raise
                    row = self._get(NAMESPACES['waits'], key, 'wait')
                    if row is None:
                        raise
            if row['value']['state'] == 'pending':
                return deepcopy(row['value']), False
            self._delete_pointer(pointer_key, pointer_version)

    def read(self, wait_id: str) -> dict:
        row = self._get(NAMESPACES['waits'], self._wait_key(wait_id), 'wait')
        if row is None:
            raise DeviceWaitNotFound(wait_id)
        return deepcopy(row['value'])

    def page(self, *, cursor: str | None = None, include_closed: bool = False) -> dict:
        """One bounded page; ``nextCursor`` continues the same view.

        ``include_closed`` walks every record in registration order. The pending
        view walks the pending index instead (device, then conversation order),
        so its cost follows the pending waits rather than the whole history; a
        pointer whose wait is not pending is skipped, so its page may be short
        yet carry a cursor.
        """
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError('Invalid cursor')
        limit = _page_rows()
        rows = self.client().query('record.list', {
            'namespace': NAMESPACES['waits' if include_closed else 'pending'],
            'prefix': self.prefix, 'after_key': self.prefix + cursor if cursor else '',
            'limit': limit + 1})
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise StorageError('database_integrity', 'Invalid device wait page')
        for row in rows:
            if not isinstance(row, Mapping) or not str(row.get('key') or '').startswith(self.prefix):
                raise StorageError('database_integrity', 'Device wait page escaped its owner')
        values = []
        for row in rows[:limit]:
            if include_closed:
                values.append(self._checked(row, 'wait')['value'])
                continue
            named = self._checked(row, 'pendingPointer')['value']
            wait = self._get(NAMESPACES['waits'], self._wait_key(named['waitId']), 'wait')
            if (wait is not None and wait['value']['state'] == 'pending'
                    and wait['value']['conversationId'] == named['conversationId']):
                values.append(wait['value'])
        return validate({
            'waits': [public_view(value) for value in values],
            'nextCursor': rows[limit - 1]['key'][len(self.prefix):] if len(rows) > limit else None,
        }, 'waitPage')

    def _close(self, wait_id: str, state: str, *, close_reason=None, delivery=None):
        """Move a pending wait to ``state`` once; a closed wait is returned unchanged."""
        key = self._wait_key(wait_id)
        while True:
            row = self._get(NAMESPACES['waits'], key, 'wait')
            if row is None:
                raise DeviceWaitNotFound(wait_id)
            if row['value']['state'] != 'pending':
                return deepcopy(row['value']), False
            closed = validate({**row['value'], 'state': state, 'closedAt': _now_ms(),
                               'closeReason': close_reason, 'delivery': delivery}, 'wait')
            try:
                self._put(NAMESPACES['waits'], key, closed, row['version'])
            except StorageError as error:
                if error.code != 'database_conflict':
                    raise
                continue
            break
        pointer_key = self._pointer_key(closed['device']['key'], closed['conversationId'])
        try:
            pointer = self._get(NAMESPACES['pending'], pointer_key, 'pendingPointer')
            if pointer is not None and pointer['value']['waitId'] == wait_id:
                self._delete_pointer(pointer_key, pointer['version'])
        except StorageError:
            # The record is authoritative; a stale pointer is retired by the next scan.
            logger.warning('[DeviceWait] pointer of closed wait %s kept for the next scan',
                           wait_id, exc_info=True)
        return closed, True

    def cancel(self, wait_id: str, *, reason: str = 'owner_cancelled') -> dict:
        """Cancel a pending wait; a delivered or closed wait is returned unchanged."""
        return public_view(self._close(wait_id, 'cancelled', close_reason=reason)[0])

    def cancel_conversation(self, conversation_id: str, *, reason: str) -> list[str]:
        """Cancel every pending wait of one conversation (it was deleted)."""
        cancelled = []
        for pointer in list(self._pointers(self.prefix)):
            if pointer['value']['conversationId'] != conversation_id:
                continue
            try:
                _, changed = self._close(pointer['value']['waitId'], 'cancelled', close_reason=reason)
            except DeviceWaitNotFound:
                self._delete_pointer(pointer['key'], pointer['version'])
                continue
            if changed:
                cancelled.append(pointer['value']['waitId'])
        return cancelled

    def deliver(self, pointer: Mapping, connection: Mapping, *, dispatch=None) -> dict:
        """Wake the conversation of one pending wait for this connection."""
        named = pointer['value']
        row = self._get(NAMESPACES['waits'], self._wait_key(named['waitId']), 'wait')
        if row is None:
            # Pointers are written before records: the registration is still in
            # progress (it delivers itself if the device is online by then) or was
            # interrupted, and registering again completes it under this pointer.
            return {'waitId': named['waitId'], 'outcome': 'incomplete'}
        if (row['value']['state'] != 'pending'
                or row['value']['conversationId'] != named['conversationId']):
            self._delete_pointer(pointer['key'], pointer['version'])
            return {'waitId': named['waitId'], 'outcome': 'stale'}
        record = row['value']
        from lib.turn_initiation import INITIATOR_PROACTIVE, stamp_initiator
        message = stamp_initiator({'role': 'user', 'content': render_notice(record, connection)},
                                  INITIATOR_PROACTIVE)
        if dispatch is None:
            from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
        from lib.scheduler._shared import continuation_policy
        command_id = wake_command_id(self.owner, record['waitId'])
        tools_config = deepcopy(record['wake']['toolsConfig'])
        disposition = dispatch(
            record['conversationId'], message, tools_config,
            user_id=self.owner, command_id=command_id,
            log_prefix=f'[DeviceWait:{record["waitId"]}]', inject_mode='steer',
            config_overrides={**continuation_policy(tools_config),
                              **deepcopy(record['wake']['overrides'])}).disposition
        if disposition in ('started', 'steered', 'queued'):
            closed, _ = self._close(record['waitId'], 'delivered', delivery={
                'agentId': connection['agent_id'],
                'connectionEpoch': connection['connection_epoch'],
                'connectedAt': int(connection['connected_at'] * 1000), 'commandId': command_id,
                'disposition': disposition, 'deliveredAt': _now_ms()})
        elif disposition == 'target_missing':
            closed, _ = self._close(record['waitId'], 'cancelled', close_reason='conversation_missing')
        elif disposition == 'start_failed':
            # The notice is durable in the conversation, but no executor ran it; a replay
            # of the same command cannot start one, so the wait closes visibly.
            closed, _ = self._close(record['waitId'], 'failed', close_reason='start_failed')
        else:
            # Nothing was accepted: the next connection retries the same command.
            return {'waitId': record['waitId'], 'outcome': 'pending', 'disposition': disposition}
        return {'waitId': record['waitId'], 'outcome': closed['state'], 'disposition': disposition}


def _bridge_owner(user_id) -> int | None:
    text = str(user_id or '')
    return int(text) if text.isdigit() and int(text) > 0 else None


def deliver_device_waits(connection: Mapping, *, client=None, dispatch=None) -> list[dict]:
    """Wake every pending wait naming this device; called once per connection epoch.

    Contained: a failure leaves the affected waits pending for the device's
    next connection and never fails the device's poll.
    """
    agent_id = str(connection.get('agent_id') or '')
    owner = _bridge_owner(connection.get('user_id'))
    if owner is None or not agent_id:
        return []
    results = []
    try:
        waits = DeviceWaits(owner, client=client)
        pointers = [pointer for key in connection_device_keys(agent_id, connection.get('name') or '')
                    for pointer in waits._pointers(f'{waits.prefix}{key}:')]
    except Exception:
        logger.warning('[DeviceWait] pending waits of device %s unavailable; the next connection '
                       'retries them', agent_id[:12], exc_info=True)
        return results
    for pointer in pointers:
        try:
            results.append(waits.deliver(pointer, connection, dispatch=dispatch))
        except Exception:
            logger.warning('[DeviceWait] wait %s stays pending for the next connection of %s',
                           pointer['value']['waitId'], agent_id[:12], exc_info=True)
    return results


def cancel_conversation_waits(owner: int, conversation_id: str) -> list[str]:
    """Deleted conversation: its pending waits will never be delivered. Contained."""
    try:
        return DeviceWaits(owner).cancel_conversation(conversation_id, reason='conversation_deleted')
    except Exception:
        logger.warning('[DeviceWait] waits of deleted conversation %s stay pending; their wake '
                       'finds no conversation and closes them', conversation_id, exc_info=True)
        return []


# ── tool ────────────────────────────────────────────────────────────────

def _tool_error(code: str, message: str, next_action: str) -> str:
    from lib.tools.result_envelope import typed_tool_error
    return typed_tool_error(code, retryable=False, message=message,
                            next_action=next_action).to_model_text()


def _label(agent: Mapping) -> str:
    return f"{agent.get('name') or agent['agent_id']} (agent {agent['agent_id']})"


def resolve_device(owner: int, selector: str) -> tuple[dict, list[dict]]:
    """Match ``selector`` against the owner's devices known since the server started."""
    from lib.desktop import bridge
    agents = bridge.list_agents(user_id=str(owner))
    matches = ([agent for agent in agents if agent['agent_id'] == selector]
               or [agent for agent in agents if (agent.get('name') or '').casefold() == selector.casefold()])
    if len(matches) > 1:
        raise LookupError(', '.join(_label(agent) for agent in matches))
    if matches:
        agent = matches[0]
        return ({'selector': selector, 'agentId': agent['agent_id'], 'name': agent.get('name') or None,
                 'key': device_key('id', agent['agent_id'])}, agents)
    return ({'selector': selector, 'agentId': None, 'name': None,
             'key': device_key('sel', selector.casefold())}, agents)


def _online_connection(owner: int, device: Mapping) -> dict | None:
    from lib.desktop import bridge
    for agent in bridge.list_agents(user_id=str(owner)):
        if not agent.get('online'):
            continue
        if device['agentId']:
            matches = agent['agent_id'] == device['agentId']
        else:
            matches = device['selector'].casefold() in (
                agent['agent_id'].casefold(), (agent.get('name') or '').casefold())
        if matches:
            return bridge.online_connection(agent['agent_id'], str(owner))
    return None


def offline_hint(task: Mapping, agent_id: str | None = None) -> str:
    """Suffix for a device tool error while the device is offline and this task can wait.

    ``agent_id`` names the addressed device (remote worktree); without it any
    online device of the owner suppresses the hint. Tasks that cannot register a
    wait (no conversation of their own, a project assignment grant) get none.
    """
    from lib.desktop import bridge
    from lib.project_dispatch.admission import is_dispatch_task
    from lib.tasks_pkg.conversation_delivery import conversation_delivery_available
    if not conversation_delivery_available(task) or is_dispatch_task(task):
        return ''
    if any(agent.get('online') and agent_id in (None, agent['agent_id'])
           for agent in bridge.list_agents(user_id=str(task.get('_userId') or ''))):
        return ''
    target = f' {agent_id!r}' if agent_id else ''
    return (f' The device{target} is offline: call await_device with its agent ID or name and a '
            'reason, then end your turn; this conversation continues when the device reconnects.')


def await_device(task: dict, arguments: Mapping, *, client=None, dispatch=None) -> str:
    """The ``await_device`` tool: register one durable wait for this conversation."""
    from lib.project_dispatch.admission import is_dispatch_task
    from lib.tasks_pkg.conversation_delivery import completion_conversation_id
    from lib.tasks_pkg.manager import task_user_id
    if is_dispatch_task(task):
        return _tool_error(
            'device_wait_outside_grant',
            'This turn runs under a project assignment grant; a device-online wake would start a '
            'later turn outside that grant, so no wait was registered.',
            'Report the device dependency in your result so your Coordinator or the owner can '
            'wait for the device.')
    try:
        arguments = validate({key: value for key, value in arguments.items()
                              if not str(key).startswith('_')}, 'awaitDeviceArguments')
    except ValueError as error:
        return _tool_error('invalid_arguments', str(error),
                           'Pass device (agent ID or name) and reason.')
    selector, reason = arguments['device'].strip(), arguments['reason'].strip()
    if not selector or not reason:
        return _tool_error('invalid_arguments', 'device and reason must not be blank.',
                           'Pass device (agent ID or name) and reason.')
    owner = int(task_user_id(task))
    conversation_id = completion_conversation_id(task)
    try:
        device, agents = resolve_device(owner, selector)
    except LookupError as error:
        return _tool_error('ambiguous_device', f'Several of your devices are named {selector!r}: {error}.',
                           'Pass the agent ID of the device to wait for.')
    connection = _online_connection(owner, device)
    if connection is not None:
        return (f"{_label(connection)} is online now, so no wait was registered. Run the commands "
                'that need it directly.')
    wait, created = DeviceWaits(owner, client=client).register(
        conversation_id=conversation_id, device=device, reason=reason,
        wake=wake_capture(task.get('config')),
        source={'taskId': str(task.get('id') or ''), 'turnId': task.get('_turnId') or None})
    # The device may have connected after the presence check but before the
    # pointer existed; that connection's delivery could not see this wait.
    connection = _online_connection(owner, device)
    if connection is not None:
        deliver_device_waits(connection, client=client, dispatch=dispatch)
        current = DeviceWaits(owner, client=client).read(wait['waitId'])
        if current['state'] == 'delivered':
            return (f"{_label(connection)} connected while the wait was being registered; its "
                    'device-online notice was delivered to this conversation. Continue with the '
                    'device work now.')
    label = device['name'] or device['selector']
    lines = [
        (f"Registered wait {wait['waitId']} for device {label!r}." if created else
         f"Wait {wait['waitId']} for device {label!r} was already registered; its notice repeats: "
         f"{wait['reason']}"),
        'End your turn now. When the device reconnects, this conversation receives one nonhuman '
        "'device online' notice and continues with the tools and approvals it already has. "
        'Nothing is polled and there is no timeout; the owner can list or cancel the wait in '
        'Settings → Devices.',
    ]
    if device['agentId'] is None:
        known = ', '.join(_label(agent) for agent in agents) or 'none'
        lines.append('No device with this agent ID or name has connected since the server started; '
                     'the wait matches the first of your devices that connects with this agent ID '
                     f'or name. Devices seen since the server started: {known}.')
    return '\n'.join(lines)


__all__ = ['AWAIT_DEVICE_TOOL', 'CONTRACT', 'NAMESPACES', 'DeviceWaitNotFound', 'DeviceWaits',
           'await_device', 'cancel_conversation_waits', 'connection_device_keys',
           'deliver_device_waits', 'device_key', 'offline_hint', 'public_view', 'query_parameters',
           'render_notice', 'resolve_device', 'validate', 'wake_capture', 'wake_command_id']

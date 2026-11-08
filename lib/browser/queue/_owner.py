"""Bridge-owner selection for the worker-facing queue verbs.

The process that terminates extension polls owns the queue (``_registry`` /
``_dispatch``). A resident execution host selects that owner through
``lib.browser.bridge_relay``; every other composition keeps the local owner.
These wrappers are the only public spelling of the verbs task code uses, so
tool calls, availability evidence and connection waits read one registry.
"""

from . import _dispatch, _registry


def _relay():
    from lib.browser.bridge_relay import selected_relay
    return selected_relay()


def get_connected_clients(*, owner_user_id: str | None):
    relay = _relay()
    if relay is None:
        return _registry.get_connected_clients(owner_user_id=owner_user_id)
    return relay.call('connected_clients', {
        'owner_user_id': None if owner_user_id is None else str(owner_user_id)})


def is_extension_connected(client_id, *, owner_user_id):
    relay = _relay()
    if relay is None:
        return _registry.is_extension_connected(client_id, owner_user_id=owner_user_id)
    return bool(relay.call('extension_connected', {
        'client_id': str(client_id or ''), 'owner_user_id': str(owner_user_id or '')}))


def client_owner_user_id(client_id):
    relay = _relay()
    if relay is None:
        return _registry.client_owner_user_id(client_id)
    return str(relay.call('client_owner', {'client_id': str(client_id or '')}) or '')


def get_credit_hub_quota(*, owner_user_id):
    relay = _relay()
    if relay is None:
        return _registry.get_credit_hub_quota(owner_user_id=owner_user_id)
    return relay.call('credit_hub_quota', {'owner_user_id': str(owner_user_id or '')})


def wait_for_connected_clients(*, owner_user_id, client_id=None, timeout=0,
                               cancellation=None):
    relay = _relay()
    if relay is None:
        return _registry.wait_for_connected_clients(
            owner_user_id=owner_user_id, client_id=client_id, timeout=timeout,
            cancellation=cancellation)
    from lib.browser.bridge_relay import is_cancelled
    result = relay.call('wait_connected', {
        'owner_user_id': str(owner_user_id or ''),
        'client_id': None if client_id is None else str(client_id),
        'timeout': float(timeout)}, wait_seconds=float(timeout), cancel_event=cancellation)
    return [] if is_cancelled(result) else result


def send_browser_command(cmd_type, params=None, timeout=30, *, client_id,
                         owner_user_id, cancel_event=None):
    relay = _relay()
    if relay is None:
        return _dispatch.send_browser_command(
            cmd_type, params, timeout, client_id=client_id,
            owner_user_id=owner_user_id, cancel_event=cancel_event)
    from lib.browser.bridge_relay import RelayUnavailable, is_cancelled
    if cancel_event is not None and cancel_event.is_set():
        return None, 'Browser command cancelled before dispatch'
    try:
        result = relay.call('send_command', {
            'cmd_type': cmd_type, 'params': params or {}, 'timeout': timeout,
            'client_id': str(client_id or ''), 'owner_user_id': str(owner_user_id or ''),
        }, wait_seconds=float(timeout), cancel_event=cancel_event)
    except RelayUnavailable as exc:
        return None, str(exc)
    if is_cancelled(result):
        return None, 'Browser command cancelled'
    return result[0], result[1]

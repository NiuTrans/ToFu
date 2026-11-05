"""Encrypted Responses state provenance; a private sidecar marker, never wire input.

Responsibility: ``encrypted_content`` on a Responses ``reasoning`` or
``compaction`` item decrypts only on the upstream that minted it, while one
logical model may be served by several v2 Deployments behind different
upstream accounts. When a stream settles on a slot with a stable Deployment,
dispatch appends one ``tofu_state_origin`` marker naming that Deployment to the
assistant message's existing ``_responses_items`` sidecar, so the fact rides
every path that already persists and rebuilds that sidecar (live history, tool
rounds, settled turns, cold reconstruction). Dispatch reads the markers back to
bind a replaying request to its minting Deployment
(``LLMDispatcher.responses_state_binding``).

The marker is not provider input: the Responses adapter replays only its item
allow-list, checked conversion and the replay-order witness skip it, and usage
recovery retires it together with the state it describes. It names where state
came from; it does not prove the ciphertext is still decryptable there.

Entry points: ``STATE_ORIGIN_TYPE``, ``stamp_state_origin``,
``responses_state_origins``. Standard library only, so checked conversion can
recognize the marker without importing a provider package.
"""

STATE_ORIGIN_TYPE = 'tofu_state_origin'


def _is_marker(item) -> bool:
    return isinstance(item, dict) and item.get('type') == STATE_ORIGIN_TYPE


def _carries_encrypted_state(items) -> bool:
    return any(
        isinstance(item, dict) and not _is_marker(item)
        and isinstance(item.get('encrypted_content'), str)
        and item['encrypted_content']
        for item in items)


def stamp_state_origin(message, deployment_id) -> bool:
    """Name ``deployment_id`` as the minting route of ``message``'s state.

    Only a sidecar that carries encrypted state is stamped, and a previous
    marker is replaced rather than duplicated. The sidecar list is rebuilt, so
    an alias held elsewhere never observes the change. Returns whether the
    message was stamped.
    """
    if not isinstance(message, dict) or not isinstance(deployment_id, str):
        return False
    items = message.get('_responses_items')
    if (not deployment_id or not isinstance(items, list)
            or not _carries_encrypted_state(items)):
        return False
    message['_responses_items'] = [
        *(item for item in items if not _is_marker(item)),
        {'type': STATE_ORIGIN_TYPE, 'version': 1, 'deployment_id': deployment_id},
    ]
    return True


def responses_state_origins(body_or_messages) -> frozenset:
    """Deployments that minted encrypted state these messages still replay.

    Accepts a canonical body or its message list. A message counts only while
    its sidecar carries both encrypted state and a marker: retired state binds
    nothing, and unmarked historical state keeps its existing routing.
    """
    messages = (body_or_messages.get('messages')
                if isinstance(body_or_messages, dict) else body_or_messages)
    origins = set()
    for message in messages or ():
        items = message.get('_responses_items') if isinstance(message, dict) else None
        if not isinstance(items, list) or not _carries_encrypted_state(items):
            continue
        for item in items:
            if (_is_marker(item) and item.get('version') == 1
                    and isinstance(item.get('deployment_id'), str)
                    and item['deployment_id']):
                origins.add(item['deployment_id'])
    return frozenset(origins)


__all__ = ['STATE_ORIGIN_TYPE', 'responses_state_origins', 'stamp_state_origin']

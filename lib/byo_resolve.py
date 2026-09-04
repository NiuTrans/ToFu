"""Retired v1 BYO resolver retained as a migration compatibility shim.

Before model-routing v2, native HTTP routes accepted ``model@provider`` and
inline secret-bearing provider blocks. No production route calls this module
now; it remains bounded and tested only so old state can be inspected during
the one-way migration window. New code uses :mod:`lib.model_routing` and
``routes.model_routing_adapter``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from lib.byo_providers import (
    resolve_model_string, sanitise_extra_headers, touch_provider,
)
from lib.log import get_logger

if TYPE_CHECKING:
    from lib.llm_dispatch.ephemeral import EphemeralSlotHandle

logger = get_logger(__name__)


def dispose_ephemeral_slot(handle) -> bool:
    """Dispose a BYO handle without importing the dispatcher at route boot."""
    from lib.llm_dispatch.ephemeral import (
        dispose_ephemeral_slot as implementation,
    )
    return implementation(handle)


def resolve_model_and_provider(model_str: str, provider_block: dict | None,
                               owner_user_id: int, *,
                               tenant_id: str | None = None,
                               ) -> tuple[str, EphemeralSlotHandle | None,
                                          dict | None, str | None,
                                          int | None]:
    """Map ``(model, provider)`` → ``(model_id, handle, byo_row, error, status)``.

    ``status`` is the HTTP code an error should map to (400 vs 404);
    None on success. ``handle`` is an ephemeral slot to dispose after
    the task terminates (None when the plain global pool is used).
    ``byo_row`` is the registered provider row (for surfacing
    ``provider_id`` in the response), or None for inline / pool.

    Resolution order (first match wins):

    1. ``provider`` block present — inline BYO. ``model`` MUST be a
       plain alias with no ``@prov_xxx`` suffix.
    2. ``model="name@prov_xxx"`` — registered BYO provider lookup.
    3. plain ``model`` — falls through to the global slot pool, no
       ephemeral handle.
    """
    has_block = isinstance(provider_block, dict) and provider_block
    has_suffix = isinstance(model_str, str) and '@' in model_str

    # Both at once is ambiguous.
    if has_block and has_suffix:
        return '', None, None, (
            'cannot combine `model="name@prov_xxx"` with an inline '
            '`provider` block; pick one'), 400

    # 1. Inline provider block
    if has_block:
        if not isinstance(model_str, str) or not model_str.strip():
            model_str = str(provider_block.get('model') or '').strip()
        if not isinstance(model_str, str) or not model_str.strip():
            return '', None, None, (
                '`model` or `provider.model` is required when an inline '
                'provider is supplied'), 400
        url = (provider_block.get('base_url')
               or provider_block.get('endpoint') or '').strip()
        if not url:
            return '', None, None, (
                '`provider.base_url` (or `provider.endpoint`) is required'), 400
        headers, hdr_err = sanitise_extra_headers(
            provider_block.get('extra_headers') or {})
        if hdr_err:
            return '', None, None, hdr_err, 400
        try:
            from lib.llm_dispatch.ephemeral import mint_ephemeral_slot
            handle = mint_ephemeral_slot(
                base_url=url,
                api_key=provider_block.get('api_key') or '',
                model_id=model_str.strip(),
                owner=f'owner:{owner_user_id}',
                extra_headers=headers,
                thinking_format=(provider_block.get('thinking_format')
                                 or ''),
            )
        except (ValueError, RuntimeError) as e:
            logger.warning('[byo_resolve] inline-provider mint failed url=%s: %s',
                           url, e)
            return '', None, None, str(e), 400
        return model_str.strip(), handle, None, None, None

    # 2/3. String form
    if isinstance(model_str, str) and model_str.strip():
        rm = resolve_model_string(
            model_str, owner_user_id, tenant_id=tenant_id)
        if rm is None:
            return '', None, None, (
                f'model string {model_str!r} references an unknown '
                f'or disabled BYO provider; check the @prov_xxx '
                f'suffix'), 404
        if rm.provider is None:
            return rm.model_id, None, None, None, None
        prov = rm.provider
        try:
            from lib.llm_dispatch.ephemeral import mint_ephemeral_slot
            handle = mint_ephemeral_slot(
                base_url=prov['base_url'],
                api_key=prov.get('api_key') or '',
                model_id=rm.model_id,
                owner=f'owner:{owner_user_id}:{prov["id"]}',
                extra_headers=prov.get('extra_headers') or {},
                # Carry the persisted dialect so the dispatcher uses
                # the right body shape for this engine. Without this,
                # a registered self-hosted sglang Qwen3 would silently
                # downgrade to top-level enable_thinking and the engine
                # would ignore it.
                thinking_format=prov.get('thinking_format') or '',
            )
        except (ValueError, RuntimeError) as e:
            logger.warning('[byo_resolve] BYO provider mint failed prov=%s: %s',
                           prov.get('id'), e)
            return '', None, None, str(e), 400
        touch_provider(
            prov['id'], owner_user_id, tenant_id=tenant_id)
        return rm.model_id, handle, prov, None, None

    return '', None, None, '`model` is required', 400


def dispose_after_terminal(task: dict, handle: EphemeralSlotHandle) -> None:
    """Dispose the ephemeral slot once ``task`` reaches a terminal state.

    Intended to run in a daemon thread so SSE/HTTP responses can return
    immediately while the slot lives long enough for the orchestrator to
    make its last LLM call. Bounded by a 1-hour ceiling so a stuck task
    can't leak the slot forever.
    """
    deadline = time.time() + 3600
    while task.get('status') not in ('done', 'error', 'aborted'):
        if time.time() >= deadline:
            logger.warning('[byo_resolve] ephemeral handle %s: task %s '
                           'still running after 1h, force-disposing',
                           handle.handle_id, task.get('id', '?')[:8])
            break
        time.sleep(0.5)
    dispose_ephemeral_slot(handle)


__all__ = [
    'resolve_model_and_provider',
    'dispose_after_terminal',
    'dispose_ephemeral_slot',
]

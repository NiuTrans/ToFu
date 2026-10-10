"""Boot-scoped record of conversations interrupted by the last server stop.

``recover_stale_tasks_on_startup`` settles orphaned turns and task snapshots
at boot, so it knows exactly which conversations were cut off mid-generation.
Recording that list lets the browser offer a one-shot batch resume instead of
forcing the user through per-conversation "continue" clicks after a kill.

The record lives in the declared runtime-state store (in-process for personal
mode, Redis for distributed mode) and is rewritten on EVERY boot — including
an empty write that clears the previous hint — so it is a boot-scoped hint,
never a second authority over turn state. Turn status stays with the v3 turn
lifecycle; ownership filtering happens at the HTTP boundary, because the
recovery scan is deployment-global.

TODO(enterprise): if per-user boot prompts are ever needed, key the record by
owner instead of filtering the deployment-global list at read time.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

from lib.log import get_logger
from lib.runtime_state_store import get_store


logger = get_logger(__name__)

_STORE_KIND = "conversation_pending_resume"
_STORE_KEY = "latest"
# Every boot rewrites the record and the client dismisses it on sight, so only
# a never-opened UI can age the hint out; a week of idle server time is well
# past the point where a restart prompt still helps.
_RECORD_TTL_SECONDS = 7 * 24 * 3600.0
_MAX_RECORDED_CONVERSATIONS = 100


def _store_payload(conversation_ids: list[str], reason: str) -> str:
    return json.dumps(
        {
            "version": 1,
            "interruptedReason": reason,
            "recordedAt": time.time(),
            "conversationIds": conversation_ids,
        },
        separators=(",", ":"),
    )


def record_boot_interrupted_conversations(result: Any) -> None:
    """Persist (or clear) the boot hint from a startup-recovery result dict.

    A non-dict ``result`` means recovery itself failed; keep any previous
    record rather than clearing state this boot cannot vouch for.
    """
    if not isinstance(result, dict):
        return
    conversation_ids: list[str] = []
    seen: set[str] = set()
    for raw_id in result.get("conversationIds") or []:
        conversation_id = str(raw_id or "").strip()
        if not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        conversation_ids.append(conversation_id)
        if len(conversation_ids) >= _MAX_RECORDED_CONVERSATIONS:
            break
    store = get_store()
    if not conversation_ids:
        store.delete_value(_STORE_KIND, _STORE_KEY)
        return
    reason = str(result.get("interruptedReason") or "server_restart")
    store.set_value(
        _STORE_KIND,
        _STORE_KEY,
        _store_payload(conversation_ids, reason),
        _RECORD_TTL_SECONDS,
    )
    logger.info(
        "[Startup] pending-resume hint recorded for %d conversation(s) (%s)",
        len(conversation_ids),
        reason,
    )


def read_pending_resume() -> dict[str, Any] | None:
    """Return the current boot hint, or None when nothing is pending."""
    raw = get_store().get_value(_STORE_KIND, _STORE_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("[PendingResume] undecodable record dropped")
        get_store().delete_value(_STORE_KIND, _STORE_KEY)
        return None
    conversation_ids = payload.get("conversationIds")
    if not isinstance(payload, dict) or not isinstance(conversation_ids, list):
        return None
    payload["conversationIds"] = [
        str(cid) for cid in conversation_ids if str(cid or "").strip()
    ]
    if not payload["conversationIds"]:
        return None
    return payload


def dismiss_pending_resume(conversation_ids: Iterable[str] | None = None) -> None:
    """Clear the hint entirely, or prune only the given conversation ids."""
    store = get_store()
    if conversation_ids is None:
        store.delete_value(_STORE_KIND, _STORE_KEY)
        return
    dismissed = {str(cid or "").strip() for cid in conversation_ids}
    dismissed.discard("")
    if not dismissed:
        return
    payload = read_pending_resume()
    if not payload:
        return
    remaining = [
        cid for cid in payload["conversationIds"] if cid not in dismissed
    ]
    if not remaining:
        store.delete_value(_STORE_KIND, _STORE_KEY)
        return
    reason = str(payload.get("interruptedReason") or "server_restart")
    store.set_value(
        _STORE_KIND,
        _STORE_KEY,
        _store_payload(remaining, reason),
        _RECORD_TTL_SECONDS,
    )


__all__ = [
    "dismiss_pending_resume",
    "read_pending_resume",
    "record_boot_interrupted_conversations",
]

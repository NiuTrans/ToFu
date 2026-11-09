"""Owner-scoped compaction archive viewer and manual compaction routes."""

from __future__ import annotations

import asyncio
import json

from quart import Response, request

from lib.agent_core.store import get_conversation_store
from lib.api_response import (
    api_bad_request,
    api_error,
    api_internal_error,
    api_not_found,
    api_ok,
)
from lib.log import get_logger
from lib.openapi import api_meta
from lib.storage.errors import StorageError
from routes.api_v1.auth import request_user_id as _request_user_id, require_scope
from routes.common import _db_safe
from routes.conversations import _conv_has_live_task, conversations_bp


logger = get_logger(__name__)


def _encode_archive_download(document: dict) -> bytes:
    """Serialize an explicitly requested raw snapshot off the event loop."""
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")


def _archive_error(exc: Exception):
    if isinstance(exc, StorageError):
        if exc.code == "database_not_found":
            return api_not_found("Conversation not found")
        # _db_safe and the global boundary own the storage status taxonomy.
        raise exc
    logger.error("[Compactions] storage operation failed: %s", exc, exc_info=True)
    return api_internal_error("archive_storage_failed")


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/compactions", methods=["GET"]
)
@require_scope("conversations")
@_db_safe
async def list_compactions(conv_id):
    """List compact archive metadata without loading large transcripts."""
    user_id = _request_user_id()
    try:
        rows = await asyncio.to_thread(
            get_conversation_store().list_compaction_archives,
            conv_id,
            user_id=user_id,
        )
    except Exception as exc:
        return _archive_error(exc)
    logger.info("[Compactions] conv=%s returned %d archives", conv_id, len(rows))
    return api_ok({"compactions": rows, "count": len(rows)})


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/compactions/<archive_id>", methods=["GET"]
)
@require_scope("conversations")
@_db_safe
async def get_compaction(conv_id, archive_id):
    """Load an archive summary projection or its full transcript on demand."""
    user_id = _request_user_id()
    include_arg = str(request.args.get("includeMessages", "true")).lower()
    if include_arg not in {"true", "false", "1", "0"}:
        return api_bad_request(
            "invalid_include_messages",
            error_code="invalid_include_messages")
    include_messages = include_arg in {"true", "1"}
    view = str(request.args.get("view", "pre")).lower()
    if view not in {"pre", "post"}:
        return api_bad_request("invalid_view", error_code="invalid_view")
    include_post_messages = view == "post"
    if include_post_messages:
        # This is the saved compactor result, before runtime recomposition
        # and provider adaptation, NOT proof of an API dispatch. Never hydrate
        # the multi-MB pre transcript for a post-only view or download.
        include_messages = False
    download = str(request.args.get("download", "false")).lower() in {
        "true", "1",
    }
    if download and not include_post_messages:
        include_messages = True
    try:
        document = await asyncio.to_thread(
            get_conversation_store().get_compaction_archive,
            conv_id,
            archive_id,
            user_id=user_id,
            include_messages=include_messages,
            include_post_messages=include_post_messages,
        )
    except Exception as exc:
        return _archive_error(exc)
    if document is None:
        return api_not_found("Archive not found")
    if include_post_messages:
        document["view"] = "post"
        # Missing is evidence, not an empty or reconstructed snapshot.
        captured = isinstance(document.get("postMessages"), list)
        if not captured:
            document["postMessages"] = None
        document["postSnapshot"] = {
            "stage": "compaction_result",
            "status": "captured" if captured else "unavailable",
            "includesRuntimeRecomposition": False,
            "includesProviderAdaptation": False,
        }
    logger.info(
        "[Compactions] conv=%s archive=%s projection=%s messages=%d",
        conv_id,
        archive_id[:12],
        "post" if include_post_messages
        else "full" if include_messages else "summary",
        len(document.get("messages") or document.get("postMessages") or []),
    )
    if download:
        encoded = await asyncio.to_thread(_encode_archive_download, document)
        safe_archive = "".join(
            char for char in archive_id[:64] if char.isalnum() or char in "-_"
        ) or "snapshot"
        return Response(
            encoded,
            content_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="compaction-{safe_archive}'
                    f'{"-post" if include_post_messages else ""}.json"'),
                "Content-Length": str(len(encoded)),
            },
        )
    return api_ok(document)


_MANUAL_COMPACT_STATUS = {
    "not_found": 404,
    "nothing_to_compact": 422,
    "stale": 409,
    "summary_failed": 503,
    "turn_protocol_unsupported": 409,
}

_ERROR_RESPONSE = {"application/json": {
    "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}}
_COMPACT_RESPONSES = {
    "200": {
        "description": "Compacted, or scheduled for a protected conversation",
        "content": {"application/json": {"schema": {"oneOf": [
            {
                "type": "object",
                "required": ["ok", "archiveId", "tokensBefore", "tokensAfter"],
                "properties": {
                    "ok": {"const": True},
                    "archiveId": {"type": "string"},
                    "tokensBefore": {"type": "integer"},
                    "tokensAfter": {"type": "integer"},
                    "tokenCountKind": {"const": "estimated"},
                    "msgsBefore": {"type": "integer"},
                    "msgsAfter": {"type": "integer"},
                    "reductionPct": {"type": "number"},
                    "summaryPreview": {"type": "string"},
                    "receipt": {"type": "object"},
                },
            },
            {
                "type": "object",
                "required": ["ok", "scheduled", "alreadyScheduled", "requestId",
                             "requestedAt", "protection", "appliesAt"],
                "properties": {
                    "ok": {"const": True},
                    "scheduled": {"const": True},
                    "alreadyScheduled": {"type": "boolean"},
                    "requestId": {"type": "string", "minLength": 1},
                    "requestedAt": {"type": "integer",
                                    "description": "Unix milliseconds of the pending request"},
                    "protection": {
                        "enum": ["coordinator", "project", None],
                        "description": "null: an ordinary conversation whose task is running",
                    },
                    "appliesAt": {
                        "enum": ["next_model_call", "next_reply"],
                        "description": (
                            "next_model_call: a running reply was woken and compacts "
                            "its working view before its next model call (the next "
                            "reply does if it makes none); next_reply otherwise"),
                    },
                },
            },
        ]}}},
    },
    "404": {"description": "Conversation not found", "content": _ERROR_RESPONSE},
    "409": {"description": "stale or turn_protocol_unsupported",
            "content": _ERROR_RESPONSE},
    "422": {"description": "nothing_to_compact", "content": _ERROR_RESPONSE},
    "503": {"description": "summary_failed", "content": _ERROR_RESPONSE},
}


def _compaction_response(conv_id, result):
    if result.get("ok") and result.get("scheduled"):
        logger.info(
            "[ManualCompact] conv=%s scheduled request=%s already=%s",
            conv_id, str(result.get("requestId") or "")[:8],
            result.get("alreadyScheduled"),
        )
        return api_ok(result)
    if result.get("ok"):
        logger.info(
            "[ManualCompact] conv=%s ok tokens %s→%s archive=%s",
            conv_id,
            result.get("tokensBefore"),
            result.get("tokensAfter"),
            result.get("archiveId"),
        )
        return api_ok(result)

    error = result.get("error", "internal_error")
    return api_error(
        error,
        status=_MANUAL_COMPACT_STATUS.get(error, 500),
        error_code=error,
        **{
            key: value
            for key, value in result.items()
            if key not in ("ok", "error")
        },
    )


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/compact", methods=["POST"]
)
@require_scope("conversations")
@_db_safe
@api_meta(
    summary="Compact a conversation's context",
    description=(
        "An idle ordinary conversation is summarized and its old Turns are "
        "atomically folded: `{ok, archiveId, tokensBefore, tokensAfter, ...}`. "
        "A coordinator or coordinated-project conversation, and any "
        "conversation whose task is running, keeps every canonical Turn: the "
        "request records a durable owner-scoped marker and the conversation's "
        "next model call compacts its working view: `{ok, scheduled: true, "
        "alreadyScheduled, requestId, requestedAt, protection, appliesAt}`. "
        "Repeating the request while it is pending returns the same requestId; "
        "DELETE .../compact/scheduled/{requestId} withdraws it."
    ),
    tags=["conversations"],
    scope="conversations",
    responses=_COMPACT_RESPONSES,
)
async def compact_conversation(conv_id):
    """Compact an idle ordinary conversation or schedule a protected working view."""
    user_id = _request_user_id()
    from lib.tasks_pkg.compaction._manual import (
        compact_conversation_now,
        schedule_busy_compaction,
        schedule_protected_compaction,
    )

    body = {}
    try:
        raw = await request.get_data(as_text=True)
        if raw:
            body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return api_error("invalid_json", status=400, error_code="invalid_json")
    if not isinstance(body, dict):
        return api_error("invalid_body", status=400, error_code="invalid_body")

    # Scheduling never rewrites Turns, so a running task does not block it.
    scheduled = await asyncio.to_thread(
        schedule_protected_compaction, conv_id, user_id=user_id)
    if scheduled is not None:
        return _compaction_response(conv_id, scheduled)
    if _conv_has_live_task(conv_id, user_id=user_id):
        # The destructive rewrite stays idle-only; the running reply compacts
        # its working view at its next model call instead.
        scheduled = await asyncio.to_thread(
            schedule_busy_compaction, conv_id, user_id=user_id)
        return _compaction_response(conv_id, scheduled)

    result = await asyncio.to_thread(
        compact_conversation_now,
        conv_id,
        user_id=user_id,
        config=body.get("config") or {},
        task={"convId": conv_id, "_userId": user_id},
        keep_recent_turns=body.get("keepRecentTurns"),
    )
    return _compaction_response(conv_id, result)


_REQUEST_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/compact/scheduled/<request_id>",
    methods=["DELETE"],
)
@require_scope("conversations")
@_db_safe
@api_meta(
    summary="Cancel a scheduled working-view compaction",
    description=(
        "Withdraws exactly `requestId` from the owner's scheduled compaction "
        "of this conversation: `{ok, cancelled, applying}`. `cancelled` is "
        "false when that request is no longer pending (already applied, "
        "cancelled or superseded). `applying` reports that a running reply "
        "had already started compacting its working view for it; that view "
        "stays compacted. Canonical Turns are never touched."
    ),
    tags=["conversations"],
    scope="conversations",
    responses={
        "200": {
            "description": "Cancellation outcome",
            "content": {"application/json": {"schema": {
                "type": "object",
                "required": ["ok", "cancelled", "applying"],
                "properties": {
                    "ok": {"const": True},
                    "cancelled": {"type": "boolean"},
                    "applying": {"type": "boolean"},
                },
            }}},
        },
        "400": {"description": "invalid_request_id", "content": _ERROR_RESPONSE},
    },
)
async def cancel_scheduled_compaction(conv_id, request_id):
    """Withdraw a pending working-view compaction before a model call claims it."""
    if not 1 <= len(request_id) <= 128 or not set(request_id) <= _REQUEST_ID_CHARACTERS:
        return api_error("invalid_request_id", status=400, error_code="invalid_request_id")
    from lib.tasks_pkg.compaction._manual import (
        cancel_scheduled_compaction as cancel,
    )

    result = await asyncio.to_thread(
        cancel, conv_id, user_id=_request_user_id(), request_id=request_id)
    return api_ok(result)

"""Owner-scoped compaction archive viewer and manual compaction routes."""

from __future__ import annotations

import asyncio
import json

from quart import Response, request

from lib.agent_core.store import get_conversation_store
from lib.api_response import (
    api_conflict,
    api_error,
    api_internal_error,
    api_not_found,
    api_ok,
)
from lib.log import get_logger
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
    if isinstance(exc, StorageError) and exc.code == "database_not_found":
        return api_not_found("Conversation not found")
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
    logger.info("[Compactions] conv=%s returned %d archives", conv_id[:8], len(rows))
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
        return api_error(
            "invalid_include_messages", status=400,
            error_code="invalid_include_messages")
    include_messages = include_arg in {"true", "1"}
    download = str(request.args.get("download", "false")).lower() in {
        "true", "1",
    }
    if download:
        include_messages = True
    try:
        document = await asyncio.to_thread(
            get_conversation_store().get_compaction_archive,
            conv_id,
            archive_id,
            user_id=user_id,
            include_messages=include_messages,
        )
    except Exception as exc:
        return _archive_error(exc)
    if document is None:
        return api_not_found("Archive not found")
    logger.info(
        "[Compactions] conv=%s archive=%s projection=%s messages=%d",
        conv_id[:8],
        archive_id[:12],
        "full" if include_messages else "summary",
        len(document.get("messages") or []),
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
                    f'attachment; filename="compaction-{safe_archive}.json"'),
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


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/compact", methods=["POST"]
)
@require_scope("conversations")
@_db_safe
async def compact_conversation(conv_id):
    """Summarize old turns and atomically commit one semantic compaction."""
    user_id = _request_user_id()
    if _conv_has_live_task(conv_id, user_id=user_id):
        logger.info("[ManualCompact] conv=%s refused — task active", conv_id[:8])
        return api_conflict("task_active", error_code="task_active")

    body = {}
    try:
        raw = await request.get_data(as_text=True)
        if raw:
            body = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return api_error("invalid_json", status=400, error_code="invalid_json")
    if not isinstance(body, dict):
        return api_error("invalid_body", status=400, error_code="invalid_body")

    from lib.tasks_pkg.compaction._manual import compact_conversation_now

    result = await asyncio.to_thread(
        compact_conversation_now,
        conv_id,
        user_id=user_id,
        config=body.get("config") or {},
        task={"convId": conv_id, "_userId": user_id},
        keep_recent_turns=body.get("keepRecentTurns"),
    )
    if result.get("ok"):
        logger.info(
            "[ManualCompact] conv=%s ok tokens %s→%s archive=%s",
            conv_id[:8],
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

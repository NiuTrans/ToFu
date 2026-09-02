"""Conversation HTTP projections over the authoritative Sidecar store.

Responsibilities:

* list and read owner-scoped conversation projections;
* mutate settings or scalar metadata without replaying transcript arrays;
* delete, restore, and clone conversations through atomic storage commands;
* expose bounded preview, export, and debug projections.

Transcript mutations are intentionally absent. They belong to Conversation
Sync v3 turn commands, so this module never writes ``messages_json`` and has no
legacy SQL fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping

from quart import Response, request

from lib.api_response import (
    api_bad_request,
    api_conflict,
    api_internal_error,
    api_not_found,
    api_ok,
)
from lib.conversations.catalog import list_conversation_metadata
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import async_parse_body
from lib.storage import get_storage_client
from routes.api_v1 import api_v1_conversations_bp as conversations_bp
from routes.api_v1.auth import request_user_id as _request_user_id, require_scope
from routes.common import _db_safe, _notify_conv_changed


logger = get_logger(__name__)

_MAX_LIST_LIMIT = 1_000
_MAX_WINDOW = 500
_SIDEBAR_DEFAULT_LIMIT = 500
_SIDEBAR_SETTING_KEYS = frozenset({
    "pinned",
    "pinnedAt",
    "folderId",
    "source",
    "lastMsgRole",
    "lastMsgTimestamp",
    "lastFinishReason",
    "lastMsgError",
    "lastMsgHasOutput",
    "activeTaskId",
    "projectPath",
    "projectPaths",
    "readOnlyPaths",
})
_MESSAGE_ACTIVITY_FIELDS = (
    "segments",
    "toolRounds",
    "apiRounds",
    "_continueToolRounds",
    "_continueApiRounds",
    "toolSummary",
)


def _owner_id() -> int:
    """Resolve ownership once at the HTTP boundary."""
    return int(_request_user_id())


def _client(*, write: bool = False):
    return get_storage_client(write=write)


async def _query(operation: str, payload: Mapping) -> object:
    return await asyncio.to_thread(_client().query, operation, dict(payload))


def _idempotency_key(operation: str, conv_id: str) -> str:
    supplied = (
        request.headers.get("Idempotency-Key")
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )
    material = f"{operation}\0{_owner_id()}\0{conv_id}\0{supplied}"
    return "http:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _command(operation: str, conv_id: str, payload: Mapping) -> object:
    client = _client(write=True)
    command_id = _idempotency_key(operation, conv_id)
    return await asyncio.to_thread(
        client.command, operation, dict(payload), command_id
    )


def _metadata(document: Mapping | None, *, sidebar: bool = False) -> dict:
    metadata = dict((document or {}).get("metadata") or {})
    settings = metadata.get("settings") or {}
    if not isinstance(settings, Mapping):
        settings = {}
    if sidebar:
        settings = {
            key: settings[key]
            for key in _SIDEBAR_SETTING_KEYS
            if key in settings
        }
    count = int(metadata.get("msg_count") or 0)
    return {
        "id": str(metadata.get("id") or ""),
        "title": str(metadata.get("title") or "")[:200] if sidebar else str(
            metadata.get("title") or ""
        ),
        "msgCount": count,
        "createdAt": int(metadata.get("created_at") or 0),
        "updatedAt": int(metadata.get("updated_at") or 0),
        "settings": dict(settings),
        "rev": int(metadata.get("rev") or 0),
    }


def _message_from_turn(turn: Mapping) -> dict:
    projection = dict(turn.get("projection") or {})
    projection.pop("role", None)
    actor = str(turn.get("actor") or "")
    role = "user" if actor in {"human", "critic", "virtual_user"} else "assistant"
    created_at = int(turn.get("createdAt") or 0)
    return {
        **projection,
        "role": role,
        "_turnId": turn.get("turnId"),
        "_attemptId": turn.get("currentAttemptId"),
        "_turnActor": actor,
        "_turnKind": turn.get("kind"),
        "_turnLaneId": turn.get("laneId") or "main",
        "_turnStatus": turn.get("status"),
        "_turnSettlement": turn.get("settlement") or {},
        "_projectionRevision": int(turn.get("projectionRevision") or 0),
        "timestamp": projection.get("timestamp") or created_at,
    }


def _full(document: Mapping | None) -> dict:
    metadata = _metadata(document)
    messages = list((document or {}).get("messages") or [])
    return {**metadata, "messages": messages}


def _windowed(full: dict, window: int, before: int | None) -> dict:
    if window <= 0:
        return full
    messages = list(full.get("messages") or [])
    total = len(messages)
    end = total if before is None else max(0, min(before, total))
    start = max(0, end - window)
    selected = messages[start:end]
    return {
        **full,
        "messages": selected,
        "windowed": True,
        "trimmed": False,
        "totalCount": total,
        "firstLoadedSeq": start if selected else None,
        "lastLoadedSeq": end - 1 if selected else None,
        "hasMore": start > 0,
    }


def _window_args() -> tuple[int, int | None]:
    try:
        window = int(request.args.get("window") or 0)
    except (TypeError, ValueError):
        window = 0
    window = max(0, min(window, _MAX_WINDOW))
    try:
        before = int(request.args["before_seq"])
    except (KeyError, TypeError, ValueError):
        before = None
    return window, before


async def _get_document(conv_id: str, user_id: int) -> Mapping | None:
    result = await _query(
        "conversation.get",
        {"conv_id": conv_id, "user_id": user_id, "derive_messages": True},
    )
    return result if isinstance(result, Mapping) else None


def _etag(items: list[dict]) -> str:
    encoded = json.dumps(
        items, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@conversations_bp.route("/api/v1/conversations", methods=["GET"])
@require_scope("conversations")
@_db_safe
@api_meta(
    summary="List conversation metadata",
    description=(
        "Returns one owner-scoped metadata page. Transcript bodies are read "
        "from the conversation detail or v3 sync snapshot endpoints."
    ),
    tags=["conversations"],
    scope="conversations",
)
async def list_convs():
    if request.args.get("full") is not None:
        return api_bad_request(
            "Full transcript lists were removed; read one conversation or its v3 snapshot"
        )
    user_id = _owner_id()
    try:
        requested_limit = int(request.args.get("limit") or 0)
    except (TypeError, ValueError):
        requested_limit = 0
    default_limit = (
        _SIDEBAR_DEFAULT_LIMIT if request.args.get("meta") == "1" else 100
    )
    limit = max(1, min(requested_limit or default_limit, _MAX_LIST_LIMIT))

    # Folder/cursor/page shaping happens below against this complete base read;
    # those request arguments therefore do not alter the shared query key.
    metadata_rows = await asyncio.to_thread(
        list_conversation_metadata,
        user_id=user_id,
        limit=10_000,
        order_by="updated_at_desc",
        settings_keys=sorted(_SIDEBAR_SETTING_KEYS),
    )
    items = [
        _metadata({"metadata": metadata}, sidebar=True)
        for metadata in metadata_rows
    ]

    folder_id = (request.args.get("folderId") or "").strip()
    if folder_id:
        items = [
            item for item in items
            if (item.get("settings") or {}).get("folderId") == folder_id
        ]
    total = len(items)

    before_raw = (request.args.get("before") or "").strip()
    before_id = (request.args.get("before_id") or "").strip()
    if before_raw:
        try:
            before = int(before_raw)
        except ValueError:
            return api_bad_request("before must be an integer", field="before")
        items = [
            item for item in items
            if (int(item.get("updatedAt") or 0), item["id"])
            < (before, before_id)
        ]

    page_items = items[:limit]
    has_more = len(items) > limit
    page = {"hasMore": has_more, "totalCount": total}
    if page_items:
        page.update({
            "nextBefore": page_items[-1]["updatedAt"],
            "nextBeforeId": page_items[-1]["id"],
        })

    payload: dict = {"items": page_items, "page": page}
    prefetch_id = (request.args.get("prefetch") or "").strip()
    if prefetch_id:
        prefetched = await _get_document(prefetch_id, user_id)
        if prefetched is not None:
            window, before = _window_args()
            payload["prefetched"] = _windowed(_full(prefetched), window, before)
        else:
            payload["prefetched"] = None

    etag = _etag(page_items)
    if request.if_none_match and etag in request.if_none_match:
        return Response(status=304)
    response, status = api_ok(payload)
    response.set_etag(etag)
    response.headers["Cache-Control"] = "private, max-age=5"
    response.headers["X-Total-Count"] = str(total)
    return response, status


@conversations_bp.route("/api/v1/conversations/<conv_id>", methods=["GET"])
@require_scope("conversations")
@_db_safe
async def get_conv(conv_id: str):
    document = await _get_document(conv_id, _owner_id())
    if document is None:
        return api_not_found("Conversation not found")
    window, before = _window_args()
    return api_ok(_windowed(_full(document), window, before))


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/messages/by-id/<msg_id>/activity",
    methods=["GET"],
)
@require_scope("conversations")
@_db_safe
async def get_message_activity(conv_id: str, msg_id: str):
    document = await _get_document(conv_id, _owner_id())
    if document is None:
        return api_not_found("Conversation not found")
    messages = list(document.get("messages") or [])
    matches = [
        (index, message)
        for index, message in enumerate(messages)
        if isinstance(message, Mapping) and message.get("_msgId") == msg_id
    ]
    if not matches:
        return api_not_found("Message not found")
    if len(matches) != 1:
        return api_conflict("duplicate_message_id", matchCount=len(matches))
    index, message = matches[0]
    return api_ok({
        "msgId": msg_id,
        "idx": index,
        "activity": {
            field: message[field]
            for field in _MESSAGE_ACTIVITY_FIELDS
            if field in message
        },
    })


@conversations_bp.route("/api/v1/conversations/<conv_id>/preview", methods=["GET"])
@require_scope("conversations")
@_db_safe
async def conv_preview(conv_id: str):
    document = await _get_document(conv_id, _owner_id())
    if document is None:
        return api_not_found("Conversation not found")
    full = _full(document)
    from lib.conversations import first_user_text

    return api_ok({
        "id": full["id"],
        "title": full["title"],
        "firstUserMessage": first_user_text(full["messages"]),
        "msgCount": len(full["messages"]),
    })


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/debug-messages", methods=["GET"]
)
@require_scope("conversations")
@_db_safe
async def debug_messages(conv_id: str):
    owner_user_id = _owner_id()
    document = await _get_document(conv_id, owner_user_id)
    if document is None:
        return api_not_found("Conversation not found")
    from lib.tasks_pkg.manager._events import _strip_base64_for_snapshot
    from lib.tasks_pkg.wire_messages import build_wire_messages

    config = {"systemPrompt": request.args.get("systemPrompt", "")}

    def _build():
        messages, manifest = build_wire_messages(
            list(document.get("messages") or []),
            config,
            user_id=owner_user_id,
            mode="snapshot",
            conv_id=conv_id,
            return_manifest=True,
        )
        return _strip_base64_for_snapshot(messages), manifest

    try:
        messages, manifest = await asyncio.to_thread(_build)
    except Exception as exc:
        logger.error(
            "[debug_messages] conv=%s failed: %s", conv_id[:8], exc,
            exc_info=True,
        )
        return api_internal_error("internal_error")
    return api_ok({
        "messages": messages,
        "count": len(messages),
        "approx": True,
        "contextManifest": manifest,
    })


@conversations_bp.route("/api/v1/conversations/<conv_id>/export", methods=["GET"])
@require_scope("conversations")
async def export_conv(conv_id: str):
    from lib.conv_ref import get_conversation

    include_details = (request.args.get("include_tool_details", "1").lower()
                       not in {"0", "false", "no"})
    text = await asyncio.to_thread(
        get_conversation,
        conversation_id=conv_id,
        user_id=_owner_id(),
        include_tool_details=include_details,
    )
    if text.startswith("Error: Conversation"):
        return api_not_found("Conversation not found")
    return api_ok({"text": text})


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/settings", methods=["PATCH"]
)
@require_scope("conversations")
@_db_safe
async def patch_conv_settings(conv_id: str):
    updates = await async_parse_body()
    if not isinstance(updates, dict) or not updates:
        return api_bad_request("No settings provided")
    if "touchUpdatedAt" in updates:
        return api_bad_request(
            "Browsing cannot mutate conversation recency", field="touchUpdatedAt"
        )
    result = await _command(
        "conversation.settings.update",
        conv_id,
        {"conv_id": conv_id, "user_id": _owner_id(), "updates": updates},
    )
    if not isinstance(result, Mapping) or result.get("missing"):
        return api_not_found("Conversation not found")
    if not result.get("applied"):
        return api_conflict("conversation_changed")
    _notify_conv_changed(conv_id, rev=None, user_id=_owner_id())
    return api_ok()


async def _persist_title(conv_id: str, title: str) -> bool:
    result = await _command(
        "conversation.metadata.update",
        conv_id,
        {
            "conv_id": conv_id,
            "user_id": _owner_id(),
            "updates": {"title": title},
        },
    )
    return bool(
        isinstance(result, Mapping)
        and result.get("applied")
        and not result.get("missing")
    )


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/title", methods=["PATCH"]
)
@require_scope("conversations")
@_db_safe
async def rename_conv(conv_id: str):
    from lib.conversations.title_gen import TITLE_MAX_CHARS

    body = await async_parse_body()
    title = str(body.get("title") or "").strip()
    if not title:
        return api_bad_request("title is empty", field="title")
    title = title[:TITLE_MAX_CHARS].rstrip()
    if not await _persist_title(conv_id, title):
        return api_not_found("Conversation not found")
    _notify_conv_changed(conv_id, rev=None, user_id=_owner_id())
    audit_log("conversation_renamed", conv_id=conv_id, title=title[:60])
    return api_ok(title=title)


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/generate-title", methods=["POST"]
)
@require_scope("conversations")
@_db_safe
async def generate_conv_title(conv_id: str):
    from lib.conversations.title_gen import generate_conversation_title

    body = await async_parse_body()
    language = str(body.get("lang") or "").strip() or None
    document = await _get_document(conv_id, _owner_id())
    if document is None:
        return api_not_found("Conversation not found")
    messages = list(document.get("messages") or [])
    if not messages:
        return api_bad_request("Conversation has no messages")
    title = await asyncio.to_thread(
        generate_conversation_title, messages, language
    )
    if not await _persist_title(conv_id, title):
        return api_not_found("Conversation not found")
    _notify_conv_changed(conv_id, rev=None, user_id=_owner_id())
    audit_log("conversation_title_generated", conv_id=conv_id, title=title[:60])
    return api_ok(title=title)


def _conv_has_live_task(conv_id: str, *, user_id) -> bool:
    """Return whether this owner has a pending/running task for a conv."""
    try:
        from lib.tasks_pkg.manager import list_running_tasks, task_user_id

        return any(
            item.get("convId") == conv_id and task_user_id(item) == user_id
            for item in list_running_tasks()
        )
    except Exception as exc:
        logger.warning(
            "[conversations] live-task probe failed conv=%s: %s",
            conv_id[:8], exc,
        )
        return True


@conversations_bp.route("/api/v1/conversations/<conv_id>", methods=["DELETE"])
@require_scope("conversations")
@_db_safe
@api_meta(
    summary="Move a conversation to recoverable trash",
    tags=["conversations"],
    scope="conversations",
)
async def delete_conv(conv_id: str):
    from lib.tasks_pkg.manager import abort_running_tasks_for_conv

    owner_id = _owner_id()
    await asyncio.to_thread(
        abort_running_tasks_for_conv,
        conv_id,
        user_id=owner_id,
        reason="conversation_deleted",
    )
    result = await _command(
        "conversation.delete",
        conv_id,
        {"conv_id": conv_id, "user_id": owner_id},
    )
    if not isinstance(result, Mapping) or not result.get("deleted"):
        return api_not_found("Conversation not found")
    _notify_conv_changed(
        conv_id, deleted=True, rev=None, user_id=owner_id
    )
    audit_log("conversation_deleted", conv_id=conv_id, recoverable=True)
    return api_ok(recoverable=True, deletedAt=result.get("deletedAt"))


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/restore", methods=["POST"]
)
@require_scope("conversations")
@_db_safe
@api_meta(
    summary="Restore a deleted conversation",
    tags=["conversations"],
    scope="conversations",
)
async def restore_conv(conv_id: str):
    owner_id = _owner_id()
    result = await _command(
        "conversation.restore",
        conv_id,
        {"conv_id": conv_id, "user_id": owner_id},
    )
    if not isinstance(result, Mapping) or result.get("missing"):
        return api_not_found("Deleted conversation not found")
    if result.get("conflict") or not result.get("restored"):
        return api_conflict("conversation_id_conflict")
    revision = int(result.get("rev") or 0)
    _notify_conv_changed(conv_id, rev=revision, user_id=owner_id)
    audit_log("conversation_restored", conv_id=conv_id)
    return api_ok(
        restored=True,
        rev=revision,
        turnCount=int(result.get("turnCount") or 0),
    )


@conversations_bp.route(
    "/api/v1/conversations/<conv_id>/clone", methods=["POST"]
)
@require_scope("conversations")
@_db_safe
@api_meta(
    summary="Clone a settled conversation atomically",
    tags=["conversations"],
    scope="conversations",
)
async def clone_conv(conv_id: str):
    body = await async_parse_body()
    destination_id = str(body.get("conversationId") or "").strip()
    if not destination_id or len(destination_id) > 256:
        return api_bad_request(
            "A destination conversationId is required", field="conversationId"
        )
    raw_title = body.get("title")
    if raw_title is not None and (
        not isinstance(raw_title, str) or not raw_title.strip()
    ):
        return api_bad_request("title must be a non-empty string", field="title")
    owner_id = _owner_id()
    payload = {
        "conv_id": conv_id,
        "destination_conv_id": destination_id,
        "user_id": owner_id,
    }
    if raw_title is not None:
        payload["title"] = raw_title.strip()
    result = await _command("conversation.clone", conv_id, payload)
    if not isinstance(result, Mapping) or result.get("missing"):
        return api_not_found("Source conversation not found")
    if result.get("busy"):
        return api_conflict("conversation_active")
    if not result.get("cloned"):
        return api_conflict("conversation_clone_failed")
    _notify_conv_changed(destination_id, rev=0, user_id=owner_id)
    audit_log(
        "conversation_cloned",
        source_conv_id=conv_id,
        destination_conv_id=destination_id,
        turn_count=int(result.get("turnCount") or 0),
    )
    return api_ok(
        conversationId=destination_id,
        turnCount=int(result.get("turnCount") or 0),
        archiveCount=int(result.get("archiveCount") or 0),
        rev=0,
    )


__all__ = ["conversations_bp", "_conv_has_live_task"]

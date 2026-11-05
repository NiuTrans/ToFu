"""Thin HTTP adapter for the generated conversation-sync v3 contract."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import ExitStack
from contextvars import copy_context
from functools import partial
import hmac
import json
import math
import re
import time

import orjson
from quart import Response, request

from lib.api_v4 import NativeContractBlueprint
from lib.conversation_sync.generated_contract import CONTRACT_ID
from lib.agent_core.principal import principal_key
from lib.api_response import (
    api_bad_request,
    api_error,
    api_internal_error,
    api_not_found,
    api_ok,
    api_prevalidated_payload,
    sse_response,
)
from lib.conversation_sync.broker import broker
from lib.conversation_sync.command_service import AttemptStartFailure, CommandOutcome
from lib.conversation_sync.repository import SidecarConversationSyncRepository
from lib.conversation_sync.runtime import conversation_turn_commands
from lib.conversation_sync.service import (
    ConversationCursorError,
    ConversationSyncNotFound,
    ConversationSyncService,
    ConversationTurnPageStale,
)
from lib.conversation_sync.turn_images import (
    ConversationTurnImageNotFound,
    ConversationTurnImageStale,
    turn_image_owner_scope,
)
from lib.conversation_sync.snapshot_admission import snapshot_admission
from lib.conversation_sync.validation import ContractViolation, decode
from lib.error_envelope import BadRequest
from lib.http_transport.request_lifecycle import request_execution_clock
from lib.log import get_logger
from lib.observability import record_stream_admission
from lib.request_parser import decode_proxy_json_value, parse_body
from lib.storage.errors import StorageError, http_status_for_storage_error
from lib.tasks_pkg.manager.runtime import push_withheld_for_conv
from lib.turn_lifecycle import LifecycleConflict, LifecycleNotFound
from lib.turn_lifecycle import cancel_queued_turn_pair
from lib.turn_image_transport import MAX_TURN_IMAGES
from routes.api_v1.auth import (
    current_auth,
    request_user_id as _request_user_id,
    request_principal as _request_principal,
    require_scope,
)
from routes.conversation_turn_errors import (
    contract_violation_response,
    lifecycle_conflict_response,
    storage_failure_response,
)

# ``turn.sync.changes`` pages are replay-safe reads: when the storage
# authority classifies a failure as a wedge (deadline elapsed, queue busy,
# authority unavailable) the event stream must degrade to heartbeats and
# ride the wedge out, not die with a 503. Tearing the ASGI response down
# manufactures an EventSource reconnect + pre-body probe + snapshot-refetch
# storm on top of the very wedge that caused the failure (2026-09-18
# incident). The RPC layer marks these non-retryable only because COMMAND
# commitment is ambiguous — that reasoning never applies to a query.
_DEGRADABLE_READ_FAILURES = frozenset({
    'database_timeout',
    'database_busy',
    'database_unavailable',
})


conversation_sync_v3_bp = NativeContractBlueprint(
    "conversation_sync_v3", __name__, contract_id=CONTRACT_ID,
)
_service = ConversationSyncService(SidecarConversationSyncRepository())
logger = get_logger(__name__)
_STREAM_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,63}$")
_BROWSER_PAGE_REQUEST_ID = re.compile(r"^([a-z0-9]{6})-[1-9][0-9]{0,11}$")
_MAX_STREAM_GENERATION = 2_147_483_647
_SNAPSHOT_SEGMENT_PAYLOADS = frozenset({"full", "refs"})
_SNAPSHOT_TURN_WINDOWS = {"full": None, "tail-96": 96}
_MAX_TURN_HISTORY_ORDINAL = 2**63 - 1
_MAX_TURN_HISTORY_PAGE = 256
_OWNER_CACHE_SCOPE = re.compile(r"^[0-9a-f]{24}$")


def _validate_body(schema_name: str) -> None:
    decode(schema_name, parse_body())


def _snapshot_segment_payload() -> str:
    """Decode the optional snapshot representation selector exactly once."""
    values = request.args.getlist("segmentPayload")
    if not values:
        return "full"
    if len(values) != 1 or values[0] not in _SNAPSHOT_SEGMENT_PAYLOADS:
        raise BadRequest("Invalid conversation segment payload", field="segmentPayload")
    return values[0]


def _snapshot_tool_payload(segment_payload: str) -> str:
    values = request.args.getlist("toolPayload")
    if not values:
        return "full"
    if (len(values) != 1 or values[0] not in {"full", "lazy"}
            or (values[0] == "lazy" and segment_payload != "refs")):
        raise BadRequest("Invalid conversation tool payload", field="toolPayload")
    return values[0]


def _snapshot_turn_limit() -> int | None:
    """Decode the contract-owned browser tail-window profile."""
    values = request.args.getlist("turnWindow")
    if not values:
        if request.args.getlist("windowLimit"):
            raise BadRequest(
                "windowLimit requires a bounded conversation tail", field="windowLimit")
        return None
    if len(values) != 1 or values[0] not in _SNAPSHOT_TURN_WINDOWS:
        raise BadRequest("Invalid conversation snapshot turn window", field="turnWindow")
    limit = _SNAPSHOT_TURN_WINDOWS[values[0]]
    requested = _window_integer("windowLimit", 1, 96)
    if requested and limit is None:
        raise BadRequest(
            "windowLimit requires a bounded conversation tail", field="windowLimit")
    return requested or limit


def _window_integer(name: str, minimum: int, maximum: int) -> int:
    values = request.args.getlist(name)
    if not values:
        return 0
    if len(values) != 1 or re.fullmatch(r"[0-9]+", values[0]) is None:
        raise BadRequest(f"Invalid conversation {name}", field=name)
    value = int(values[0])
    if not minimum <= value <= maximum:
        raise BadRequest(f"Invalid conversation {name}", field=name)
    return value


def _snapshot_artifact_hint() -> bool:
    """Opt into the bounded artifact-existence projection exactly once."""
    values = request.args.getlist("artifactHint")
    if not values:
        return False
    if len(values) != 1 or values[0] != "has-any":
        raise BadRequest("Invalid conversation snapshot artifact hint", field="artifactHint")
    return True


def _turn_history_page_request() -> tuple[str, int, int | None, int, str]:
    """Decode one strict, bounded lane-history query."""
    lane_values = request.args.getlist("laneId")
    if len(lane_values) != 1:
        raise BadRequest("A conversation history laneId is required", field="laneId")
    lane_id = lane_values[0]
    if not lane_id or lane_id != lane_id.strip() or len(lane_id) > 128:
        raise BadRequest("Invalid conversation history laneId", field="laneId")

    sync_values = request.args.getlist("syncSeq")
    if len(sync_values) != 1:
        raise BadRequest("A conversation history syncSeq is required", field="syncSeq")
    try:
        sync_sequence = int(sync_values[0])
    except (TypeError, ValueError, OverflowError) as exc:
        raise BadRequest("Invalid conversation history syncSeq", field="syncSeq") from exc
    if not 0 <= sync_sequence <= _MAX_TURN_HISTORY_ORDINAL:
        raise BadRequest("Invalid conversation history syncSeq", field="syncSeq")

    before_values = request.args.getlist("beforeOrdinal")
    if len(before_values) > 1:
        raise BadRequest("Invalid conversation history beforeOrdinal", field="beforeOrdinal")
    before_ordinal: int | None = None
    if before_values:
        try:
            before_ordinal = int(before_values[0])
        except (TypeError, ValueError, OverflowError) as exc:
            raise BadRequest(
                "Invalid conversation history beforeOrdinal", field="beforeOrdinal"
            ) from exc
        if not 0 <= before_ordinal <= _MAX_TURN_HISTORY_ORDINAL:
            raise BadRequest(
                "Invalid conversation history beforeOrdinal", field="beforeOrdinal")

    limit_values = request.args.getlist("limit")
    if len(limit_values) > 1:
        raise BadRequest("Invalid conversation history limit", field="limit")
    try:
        limit = int(limit_values[0]) if limit_values else 64
    except (TypeError, ValueError, OverflowError) as exc:
        raise BadRequest("Invalid conversation history limit", field="limit") from exc
    if not 1 <= limit <= _MAX_TURN_HISTORY_PAGE:
        raise BadRequest("Invalid conversation history limit", field="limit")
    return (
        lane_id,
        sync_sequence,
        before_ordinal,
        limit,
        _snapshot_segment_payload(),
    )


def _turn_projection_fence(
    conversation_id: str,
    turn_id: str,
    label: str,
) -> tuple[int, str]:
    """Decode the shared immutable Turn projection fence and owner scope."""
    if (
        not conversation_id
        or len(conversation_id) > 256
        or not turn_id
        or len(turn_id) > 128
    ):
        raise BadRequest(f"Invalid {label} identity")
    revision_values = request.args.getlist("projectionRevision")
    if len(revision_values) != 1:
        raise BadRequest(
            f"A {label} projectionRevision is required", field="projectionRevision")
    try:
        projection_revision = int(revision_values[0])
    except (TypeError, ValueError, OverflowError) as exc:
        raise BadRequest(
            f"Invalid {label} projectionRevision", field="projectionRevision") from exc
    if not 1 <= projection_revision <= _MAX_TURN_HISTORY_ORDINAL:
        raise BadRequest(f"Invalid {label} projectionRevision", field="projectionRevision")
    owner_scope_values = request.args.getlist("ownerScope")
    if (
        len(owner_scope_values) != 1
        or _OWNER_CACHE_SCOPE.fullmatch(owner_scope_values[0]) is None
    ):
        raise BadRequest(f"A valid {label} ownerScope is required", field="ownerScope")
    return projection_revision, owner_scope_values[0]


def _turn_image_request(
    conversation_id: str, turn_id: str, image_index: int,
) -> tuple[int, str]:
    if not 0 <= image_index < MAX_TURN_IMAGES:
        raise BadRequest("Invalid conversation Turn image identity", field="imageIndex")
    return _turn_projection_fence(conversation_id, turn_id, "Turn image")


def _snapshot_browser_page_id() -> str:
    """Return the stable page prefix from the generated browser transport.

    Headless callers and arbitrary request IDs remain ungated. This boundary
    is a cooperative stale-browser circuit breaker, not an authorization or
    abuse-prevention identity.
    """
    request_id = str(request.headers.get("X-Request-ID") or "").strip()
    match = _BROWSER_PAGE_REQUEST_ID.fullmatch(request_id)
    return match.group(1) if match is not None else ""


def _client_fault_response(exc: BadRequest):
    """Answer the typed client fault exactly like the global boundary."""
    return api_bad_request(str(exc), **({"field": exc.field} if exc.field else {}))


def _command_response(
    operation: str, execute: Callable[[], CommandOutcome]
):
    """Map application errors once; versioned handlers only bind DTOs.

    Only typed outcomes choose a 4xx. A builtin ``TypeError``/``ValueError``
    is a server fault, because Python raises those for programming errors
    too: it is logged with its traceback and answered as a redacted 500.
    """
    try:
        return api_ok(execute().value)
    except LifecycleConflict as exc:
        return lifecycle_conflict_response(exc)
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))
    except BadRequest as exc:
        return _client_fault_response(exc)
    except ContractViolation as exc:
        # Request bodies are schema-checked before ``execute``; a violation
        # raised while executing is durable-data drift, a server fault.
        return contract_violation_response(exc, operation=operation)
    except StorageError as exc:
        return storage_failure_response(exc, operation=operation)
    except AttemptStartFailure as exc:
        # This is a validated, persisted public settlement envelope, not an
        # arbitrary exception string. Preserve its actionable retry contract;
        # api_internal_error intentionally redacts untrusted 500 details.
        logger.error("Conversation task start failed op=%s", operation)
        return api_error(exc.envelope, status=500, latestTurn=exc.latest_turn)
    except Exception as exc:
        logger.error("Conversation command failed op=%s: %s", operation, exc,
                     exc_info=True)
        return api_internal_error(
            exc, context=operation, source="conversation-turn-command",
            log_traceback=False)


async def _encode_transport(function: Callable, *args, **kwargs):
    """Encode one demanded response/frame without abandoning its borrowed DTO.

    The existing demand executor owns a finite encoder, not an eager replay
    queue. Repeated cancellation joins that exact worker before the request or
    stream can release its snapshot/subscription. Context preserves response
    helper fallback behavior and correlation without touching request streams.
    """
    future = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, partial(function, *args, **kwargs),
    )
    interrupted = None
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError as error:
            if interrupted is None:
                interrupted = error
        except BaseException as error:
            # The completed encoder failure is re-raised below, including when
            # cancellation and failure are independent outcomes of this call.
            logger.debug('transport encoding wait ended: %s', type(error).__name__)
            break
    try:
        result = future.result()
    except BaseException as error:
        if interrupted is not None:
            raise BaseExceptionGroup(
                "Conversation transport encoding interrupted and failed",
                [interrupted, error],
            ) from None
        raise
    if interrupted is not None:
        raise interrupted
    return result


def _contains_nonfinite_json_number(value) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_nonfinite_json_number(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_nonfinite_json_number(item) for item in value)
    return False


def _sse_json(event: dict) -> str:
    try:
        encoded_bytes = orjson.dumps(event)
        # orjson silently converts NaN/Infinity to null. Inspect primitive
        # containers only when that token is present; keep the old failure/data
        # behavior rather than changing invalid numeric evidence into null.
        if b"null" in encoded_bytes and _contains_nonfinite_json_number(event):
            raise ValueError("non-finite JSON number")
        return encoded_bytes.decode("utf-8")
    except (TypeError, ValueError) as error:
        # Keep the prior JSON primitive behavior (including oversized integers)
        # rather than dropping a replay frame outside orjson's narrower domain.
        logger.debug("SSE JSON fallback used: %s", type(error).__name__)
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


def _sse_frame(event: dict, *, cursor: str | None = None) -> str:
    lines = []
    if cursor:
        lines.append(f"id: {cursor}")
    lines.append(f"event: {event['type']}")
    lines.append("data: " + _sse_json(event))
    return "\n".join(lines) + "\n\n"


def _multiplexed_sse_frame(
    index: int, event: dict, *, cursor: str | None = None,
) -> str:
    """One multiplexed stream's frame; its data leads with the stream index.

    The index needs no escaping, unlike a conversation ID, and the id field
    stays that stream's own cursor.
    """
    lines = []
    if cursor:
        lines.append(f"id: {cursor}")
    lines.append(f"event: {event['type']}")
    lines.append(f"data: {index}")
    lines.append("data: " + _sse_json(event))
    return "\n".join(lines) + "\n\n"


def _sized_frame(
    frame: Callable[..., str], event: dict, *, cursor: str,
) -> tuple[str, int]:
    """Return one encoded frame with its UTF-8 size for catch-up accounting."""
    encoded = frame(event, cursor=cursor)
    return encoded, len(encoded.encode("utf-8"))


def _stream_identity() -> tuple[str, int]:
    """Decode the generated EventSource ownership query as one strict pair."""
    stream_client_id = str(request.args.get("streamClientId") or "").strip()
    raw_generation = str(request.args.get("streamGeneration") or "").strip()
    if not stream_client_id and not raw_generation:
        # Compatibility for an already-loaded pre-upgrade page. Legacy streams
        # remain globally bounded, but cannot supersede an exact predecessor.
        return "", 0
    if not stream_client_id or not raw_generation:
        raise BadRequest(
            "streamClientId and streamGeneration must be supplied together",
            field="streamGeneration" if stream_client_id else "streamClientId")
    if _STREAM_CLIENT_ID.fullmatch(stream_client_id) is None:
        raise BadRequest("Invalid conversation stream client id", field="streamClientId")
    try:
        stream_generation = int(raw_generation)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BadRequest(
            "Invalid conversation stream generation", field="streamGeneration") from exc
    if not 1 <= stream_generation <= _MAX_STREAM_GENERATION:
        raise BadRequest("Invalid conversation stream generation", field="streamGeneration")
    return stream_client_id, stream_generation


def _stream_stop_response(reason: str) -> Response:
    """Stop native EventSource retry for a superseded or unadmitted stream."""
    return Response(
        status=204,
        headers={
            "Cache-Control": "no-store",
            "X-Tofu-Stream-Admission": reason,
        },
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/sync", methods=["GET"]
)
@require_scope("chat")
async def conversation_sync_snapshot(conversation_id: str):
    user_id = _request_user_id()
    try:
        segment_payload = _snapshot_segment_payload()
        tool_payload = _snapshot_tool_payload(segment_payload)
        turn_limit = _snapshot_turn_limit()

        max_projection_bytes = _window_integer("windowBytes", 65536, 16777216)
        if max_projection_bytes and turn_limit is None:
            raise BadRequest(
                "windowBytes requires a bounded conversation tail", field="windowBytes")
        include_artifact_hint = _snapshot_artifact_hint()
    except BadRequest as exc:
        return _client_fault_response(exc)
    page_id = _snapshot_browser_page_id()
    admission = None
    if page_id:
        admission_view = (
            segment_payload
            if turn_limit is None
            else f"{segment_payload}:tail-{turn_limit}"
        )
        if include_artifact_hint:
            admission_view += ":artifact-hint"
        if tool_payload == "lazy":
            admission_view += ":tools-lazy"

        if max_projection_bytes:
            admission_view += f":bytes-{max_projection_bytes}"
        admission = snapshot_admission.enter(
            user_id=user_id,
            conversation_id=conversation_id,
            page_id=page_id,
            representation=admission_view,
        )
        if not admission.allowed:
            response, status = api_error(
                "snapshot_in_flight",
                status=429,
                retryAfterMs=250,
            )
            response.headers["Retry-After"] = "1"
            response.headers["Cache-Control"] = "no-store"
            return response, status
    try:
        # Read-side wedge signal: the live task's withheld pushes can never
        # carry it (the write path is what is broken), so the snapshot does.
        withheld = push_withheld_for_conv(conversation_id) is not None
        read_started = time.perf_counter()
        snapshot = await asyncio.to_thread(
            _service.snapshot,
            conversation_id,
            user_id,
            push_withheld=withheld,
            segment_payload=segment_payload,
            tool_payload=tool_payload,
            turn_limit=turn_limit,
            include_artifact_hint=include_artifact_hint,

            **({"max_projection_bytes": max_projection_bytes} if max_projection_bytes else {}),
        )
        read_ms = (time.perf_counter() - read_started) * 1000
        encode_started = time.perf_counter()
        response, status = await _encode_transport(api_prevalidated_payload, snapshot)
        encode_ms = (time.perf_counter() - encode_started) * 1000
        response.headers["Server-Timing"] = (
            f"snapshot;dur={read_ms:.2f}, encode;dur={encode_ms:.2f}"
        )
        # Opening a conversation is the observable evidence that its turns
        # matter right now, and it is the last reliable moment to repair a
        # translation that failed earlier: the boot sweep only covers recent
        # conversations, so without this a failed turn stayed English until a
        # restart. Detached + debounced + fail-soft, so it never delays this
        # response.
        try:
            from lib.translate.terminal import schedule_translation_recovery

            schedule_translation_recovery(
                conversation_id, user_id=user_id)
        except Exception as exc:
            logger.debug(
                "[AutoTranslate] recovery admission failed conv=%s: %s",
                conversation_id[:8], exc,
            )
        return response, status
    except ConversationSyncNotFound as exc:
        return api_not_found(str(exc))
    except StorageError as exc:
        return storage_failure_response(
            exc, operation="conversation.sync.snapshot")
    except ContractViolation as exc:
        return contract_violation_response(
            exc, operation="conversation.sync.snapshot")
    finally:
        if admission is not None:
            snapshot_admission.release(admission.lease)


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/rounds", methods=["GET"]
)
@require_scope("chat")
async def conversation_round_directory(conversation_id: str):
    try:
        lane = str(request.args.get("laneId") or "").strip()
        anchor = str(request.args.get("anchorTurnId") or "").strip()
        if not lane or len(lane) > 128 or len(anchor) > 128:
            raise BadRequest(
                "Invalid laneId or anchorTurnId",
                field="laneId" if not lane or len(lane) > 128 else "anchorTurnId")
        def integer(name, default, maximum):
            raw = request.args.get(name)
            if raw is None and default is None:
                raise BadRequest(f"{name} is required", field=name)
            try:
                value = int(raw) if raw is not None else default
            except (TypeError, ValueError, OverflowError) as exc:
                raise BadRequest(f"Invalid {name}", field=name) from exc
            if value < (1 if name == "limit" else 0) or value > maximum:
                raise BadRequest(f"Invalid {name}", field=name)
            return value
        expected_sync_sequence = integer("syncSeq", None, 9223372036854775807)
        offset = integer("offset", 0, 9223372036854775807)
        limit = integer("limit", 50, 100)
    except BadRequest as exc:
        return _client_fault_response(exc)
    try:
        result = await asyncio.to_thread(
            _service.round_directory, conversation_id, _request_user_id(),
            lane_id=lane, expected_sync_sequence=expected_sync_sequence,
            offset=offset, limit=limit, anchor_turn_id=anchor,
        )
        return api_prevalidated_payload(result)
    except ContractViolation as exc:
        return contract_violation_response(exc, operation="conversation.sync.round_directory")
    except ConversationSyncNotFound as exc:
        return api_not_found(str(exc))
    except StorageError as exc:
        return storage_failure_response(exc, operation="conversation.sync.round_directory")


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/history", methods=["GET"]
)
@require_scope("chat")
async def conversation_turn_history_page(conversation_id: str):
    user_id = _request_user_id()
    try:
        lane_id, sync_sequence, before_ordinal, limit, segment_payload = (
            _turn_history_page_request()
        )

        max_projection_bytes = _window_integer("windowBytes", 65536, 16777216)
        tool_payload = _snapshot_tool_payload(segment_payload)
    except BadRequest as exc:
        return _client_fault_response(exc)
    try:
        page = await asyncio.to_thread(
            _service.turn_page,
            conversation_id,
            user_id,
            lane_id=lane_id,
            expected_sync_sequence=sync_sequence,

            **({"max_projection_bytes": max_projection_bytes} if max_projection_bytes else {}),
            before_ordinal=before_ordinal,
            limit=limit,
            segment_payload=segment_payload,
            tool_payload=tool_payload,
        )
        return api_prevalidated_payload(page)
    except ContractViolation as exc:
        return contract_violation_response(
            exc, operation="conversation.sync.turn_page")
    except ConversationSyncNotFound as exc:
        return api_not_found(str(exc))
    except ConversationTurnPageStale as exc:
        return api_error(
            "history_page_stale",
            status=409,
            currentSyncSeq=exc.current_sync_sequence,
        )
    except StorageError as exc:
        return storage_failure_response(
            exc, operation="conversation.sync.turn_page"
        )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>", methods=["GET"],
)
@require_scope("chat")
async def conversation_turn_read(conversation_id: str, turn_id: str):
    if not conversation_id or len(conversation_id) > 256 or not turn_id or len(turn_id) > 128:
        return api_bad_request("Invalid conversation Turn identity")
    try:
        value = await asyncio.to_thread(_service.turn_read, conversation_id, turn_id, _request_user_id())
        response, status = api_prevalidated_payload(value)
        response.headers["Cache-Control"] = "private, no-store"
        return response, status
    except ConversationSyncNotFound as exc:
        return api_not_found(str(exc))
    except StorageError as exc:
        return storage_failure_response(exc, operation="conversation.sync.turn_read")
    except ContractViolation as exc:
        return contract_violation_response(exc, operation="conversation.sync.turn_read")


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/tool-effects/<operation_id>",
    methods=["GET"],
)
@require_scope("chat")
async def conversation_tool_effect_inspection(conversation_id: str, turn_id: str, operation_id: str):
    user_id = _request_user_id()
    if (not 0 < len(conversation_id) <= 256 or not 0 < len(turn_id) <= 128
            or re.fullmatch(r"[a-f0-9]{32}", operation_id) is None):
        return api_bad_request("Invalid tool occurrence identity")
    scopes = request.args.getlist("ownerScope")
    if len(scopes) != 1 or _OWNER_CACHE_SCOPE.fullmatch(scopes[0]) is None:
        return api_bad_request("A valid tool effect ownerScope is required")
    if not hmac.compare_digest(scopes[0], turn_image_owner_scope(user_id, conversation_id)):
        return api_not_found("Tool occurrence not found")
    try:
        value = await asyncio.to_thread(
            _service.inspect_tool_effect, conversation_id, turn_id, user_id,
            operation_id=operation_id,
        )
        response, status = api_prevalidated_payload(value)
        response.headers["Cache-Control"] = "private, no-store"
        return response, status
    except ConversationSyncNotFound as exc:
        return api_not_found(str(exc))
    except StorageError as exc:
        return storage_failure_response(exc, operation="conversation.sync.tool_effect")
    except ContractViolation as exc:
        return contract_violation_response(exc, operation="conversation.sync.tool_effect")


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/tool-results/<int:round_index>",
    methods=["GET"],
)
@require_scope("chat")
async def conversation_turn_tool_result(conversation_id: str, turn_id: str, round_index: int):
    from lib.conversation_sync.tool_results import ConversationToolResultStale
    user_id = _request_user_id()
    try:
        if not 0 <= round_index <= 2**31 - 1:
            raise BadRequest("Invalid tool result round index", field="roundIndex")
        # Identity/revision/scope use the same generated bounds as Turn images.
        revision, supplied_scope = _turn_projection_fence(conversation_id, turn_id, "tool result")
    except BadRequest as exc:
        return _client_fault_response(exc)
    if not hmac.compare_digest(supplied_scope, turn_image_owner_scope(user_id, conversation_id)):
        return api_not_found("Conversation tool result not found")
    try:
        value = await asyncio.to_thread(
            _service.turn_tool_result, conversation_id, turn_id, user_id,
            round_index=round_index, projection_revision=revision,
        )
        response, status = api_prevalidated_payload(value)
        # No persistent browser cache: deletion and owner changes stay authoritative.
        response.headers["Cache-Control"] = "private, no-store"
        return response, status
    except ConversationSyncNotFound as exc:
        return api_not_found(str(exc))
    except ConversationToolResultStale as exc:
        return api_error("tool_result_stale", status=409,
                         currentProjectionRevision=exc.current_projection_revision)
    except StorageError as exc:
        return storage_failure_response(exc, operation="conversation.sync.tool_result")
    except ContractViolation as exc:
        return contract_violation_response(exc, operation="conversation.sync.tool_result")


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/images/"
    "<int:image_index>",
    methods=["GET"],
)
@require_scope("chat")
async def conversation_turn_image(
    conversation_id: str,
    turn_id: str,
    image_index: int,
):
    user_id = _request_user_id()
    try:
        projection_revision, supplied_owner_scope = _turn_image_request(
            conversation_id, turn_id, image_index,
        )
    except BadRequest as exc:
        return _client_fault_response(exc)
    expected_owner_scope = turn_image_owner_scope(user_id, conversation_id)
    if not hmac.compare_digest(supplied_owner_scope, expected_owner_scope):
        return api_not_found("Conversation Turn image not found")
    try:
        image = await asyncio.to_thread(
            _service.turn_image,
            conversation_id,
            turn_id,
            user_id,
            projection_revision=projection_revision,
            image_index=image_index,
        )
    except ConversationTurnImageNotFound as exc:
        return api_not_found(str(exc))
    except ConversationTurnImageStale as exc:
        return api_error(
            "turn_image_stale",
            status=409,
            currentProjectionRevision=exc.current_projection_revision,
        )
    except StorageError as exc:
        return storage_failure_response(
            exc, operation="conversation.sync.turn_image"
        )

    if request.if_none_match.contains(image.digest):
        response = Response(status=304)
    else:
        response = Response(image.content, mimetype=image.media_type)
    response.set_etag(image.digest)
    response.headers["Cache-Control"] = (
        "private, max-age=31536000, immutable"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _diagnostic_options() -> dict[str, str]:
    """Decode the diagnosticPayload selector shared by both event streams."""
    diagnostic_values = request.args.getlist("diagnosticPayload")
    if len(diagnostic_values) > 1 or (diagnostic_values and diagnostic_values[0] not in {"full", "none"}):
        raise BadRequest(
            "Invalid conversation diagnostic payload", field="diagnosticPayload")
    return {"diagnostic_payload": "none"} if diagnostic_values == ["none"] else {}


async def _conversation_change_frames(
    conversation_id: str,
    user_id,
    subscription,
    *,
    after_sequence: int,
    catch_up_budget: int | None,
    diagnostic_options: dict[str, str],
    frame: Callable[..., str],
):
    """Ordered replay of one subscription: catch-up budget, heartbeats, wedges.

    ``frame(event, cursor=...)`` encodes one event for the owning response.
    The caller owns the subscription and releases it after this generator
    ends; a storage failure that cannot degrade to heartbeats propagates.
    """
    # ``sequence`` is the newest change read; ``delivered`` is the
    # newest change written to the client, which heartbeats report.
    sequence = delivered = after_sequence
    heartbeat_interval = _service.heartbeat_interval_ms / 1000.0
    # Confirm an idle subscription immediately. Wake hints are not
    # delivered frames and cannot renew this liveness deadline.
    heartbeat_at = time.monotonic()
    storage_failures = 0
    # Changes committed before the first page read are held until
    # they reach that page's head or exceed the catch-up budget.
    catch_up: list[str] | None = [] if catch_up_budget else None
    catch_up_bytes = 0
    catch_up_head: int | None = None
    while not subscription.closed:
        try:
            page = await asyncio.to_thread(
                _service.changes,
                conversation_id,
                user_id,
                after_sequence=sequence,
                limit=500,
                **diagnostic_options,
            )
        except StorageError as exc:
            if (not exc.retryable
                    and exc.code not in _DEGRADABLE_READ_FAILURES):
                raise
            if subscription.closed:
                return
            storage_failures += 1
            heartbeat = _service.heartbeat(
                conversation_id, user_id, delivered, degraded=True,
                push_withheld=(
                    push_withheld_for_conv(conversation_id) is not None),
            )
            encoded = await _encode_transport(
                frame, heartbeat, cursor=str(heartbeat["cursor"]),
            )
            if subscription.closed:
                return
            yield encoded
            heartbeat_at = time.monotonic() + heartbeat_interval
            await asyncio.sleep(min(0.25 * storage_failures, 2.0))
            continue

        if subscription.closed:
            return
        storage_failures = 0
        reset = page.get("reset")
        if isinstance(reset, dict):
            encoded = await _encode_transport(
                frame, reset, cursor=str(reset["cursor"]),
            )
            if subscription.closed:
                return
            yield encoded
            return

        if catch_up_head is None:
            catch_up_head = int(page.get("head") or 0)
        events = page.get("events") or []
        for event in events:
            if subscription.closed:
                return
            sequence = int(event["syncSeq"])
            cursor = _service.cursor_for_sequence(
                conversation_id, user_id, sequence)
            if catch_up is None:
                encoded = await _encode_transport(
                    frame, event, cursor=cursor,
                )
                # Encoding stays physically owned until joined, but
                # supersession withdraws permission to publish it.
                if subscription.closed:
                    return
                yield encoded
                delivered = sequence
                heartbeat_at = time.monotonic() + heartbeat_interval
                continue
            encoded, encoded_bytes = await _encode_transport(
                _sized_frame, frame, event, cursor=cursor,
            )
            if subscription.closed:
                return
            catch_up_bytes += encoded_bytes
            if catch_up_bytes > catch_up_budget:
                # The suffix costs more than the caller's snapshot
                # window; its intermediate states are superseded.
                reset = _service.reset_required(
                    conversation_id, user_id, catch_up_head,
                    reason="replay_exceeds_window",
                )
                encoded = await _encode_transport(
                    frame, reset, cursor=str(reset["cursor"]),
                )
                if subscription.closed:
                    return
                yield encoded
                return
            catch_up.append(encoded)
        if catch_up is not None and (
                sequence >= catch_up_head or not page.get("hasMore")):
            held, catch_up = catch_up, None
            for held_frame in held:
                if subscription.closed:
                    return
                yield held_frame
            if held:
                delivered = sequence
                heartbeat_at = time.monotonic() + heartbeat_interval
        if events:
            if page.get("hasMore"):
                continue
        elif time.monotonic() >= heartbeat_at:
            # A write-side wedge (withheld authoritative pushes) keeps
            # the change log quiet, so heartbeats are the ONLY frame
            # that can still reach the client — they carry the signal.
            withheld = (
                push_withheld_for_conv(conversation_id) is not None)
            heartbeat = _service.heartbeat(
                conversation_id, user_id, delivered, degraded=withheld,
                push_withheld=withheld,
            )
            # The id lets a native reconnect of an idle stream
            # resume from Last-Event-ID instead of its bootstrap URL.
            encoded = await _encode_transport(
                frame, heartbeat, cursor=str(heartbeat["cursor"]),
            )
            if subscription.closed:
                return
            yield encoded
            heartbeat_at = time.monotonic() + heartbeat_interval

        await subscription.wait(
            max(0.0, heartbeat_at - time.monotonic())
        )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/events", methods=["GET"]
)
@require_scope("chat")
async def conversation_sync_events(conversation_id: str):
    user_id = _request_user_id()
    try:
        stream_client_id, stream_generation = _stream_identity()
        diagnostic_options = _diagnostic_options()
        catch_up_budget = _window_integer("windowBytes", 65536, 16777216) or None
        cursor_sources = request.args.getlist("cursorSource")
        if len(cursor_sources) > 1 or (cursor_sources and cursor_sources[0] != "snapshot"):
            raise BadRequest("Invalid conversation cursor source", field="cursorSource")
    except BadRequest as exc:
        return _client_fault_response(exc)
    # Native EventSource reconnects preserve the newest delivered id in the
    # header while the original URL still contains its bootstrap cursor.
    # Header precedence is therefore required to avoid replaying from the
    # snapshot cursor on every transient reconnect.
    last_event_id = request.headers.get("Last-Event-ID")
    supplied_cursor = last_event_id or request.args.get("after")
    if cursor_sources and not last_event_id:
        # The first connection after a snapshot replays that snapshot's own
        # round trip; another snapshot would only restart the same race.
        catch_up_budget = None
    try:
        after_sequence = _service.sequence_from_cursor(
            conversation_id, user_id, supplied_cursor
        )
    except ConversationCursorError:
        return api_bad_request("Invalid conversation sync cursor")

    # Subscribe before the ownership/read probe.  A commit racing the probe is
    # either present in that read or leaves a coalesced wake token behind.
    request_principal = principal_key(current_auth())
    subscription = broker.subscribe(
        user_id,
        conversation_id,
        principal_key=request_principal,
        stream_client_id=stream_client_id,
        stream_generation=stream_generation,
    )
    if subscription.closed:
        return _stream_stop_response(subscription.close_reason)

    # One rollback boundary covers cancellation and every setup/early-return
    # path, including failures outside the typed ownership probe.
    with ExitStack() as setup:
        setup.callback(subscription.close, "setup_failed")
        # This read observes already accepted work. The broker owns its exact
        # lifetime; a model-stream quota must never block progress/settlement
        # delivery or strand a browser on its last startup phase.
        if subscription.closed:
            return _stream_stop_response(subscription.close_reason)
        record_stream_admission("conversation-sync", "admitted")
        try:
            await asyncio.to_thread(
                _service.changes,
                conversation_id,
                user_id,
                after_sequence=after_sequence,
                limit=1,
                **diagnostic_options,
            )
        except ConversationSyncNotFound as exc:
            return api_not_found(str(exc))
        except StorageError as exc:
            return storage_failure_response(
                exc, operation="conversation.sync.events")
        except ContractViolation as exc:
            return contract_violation_response(
                exc, operation="conversation.sync.events")
        if subscription.closed:
            return _stream_stop_response(subscription.close_reason)
        subscription.arm_body_start_deadline(10.0)

        async def generate():
            frames = _conversation_change_frames(
                conversation_id,
                user_id,
                subscription,
                after_sequence=after_sequence,
                catch_up_budget=catch_up_budget,
                diagnostic_options=diagnostic_options,
                frame=_sse_frame,
            )
            try:
                subscription.mark_body_started()
                async for encoded in frames:
                    yield encoded
            finally:
                try:
                    await frames.aclose()
                finally:
                    subscription.close("generator_closed")

        response = sse_response(
            generate(),
            timeout_none=True,
            extra_headers={
                "X-Tofu-Task-Kind": "conversation-sync",
                "X-Tofu-Contract": "tofu.conversation-sync.events/v1",
            },
        )
        # Ownership transfers only after response construction succeeds. The
        # generator finalizer and the unconsumed-body deadline now own release.
        setup.pop_all()
        return response


def _multiplexed_stream_request() -> tuple[str, list[dict]]:
    """Decode the page's stream identity and its subscriptions, one each."""
    stream_client_id = str(request.args.get("streamClientId") or "").strip()
    if _STREAM_CLIENT_ID.fullmatch(stream_client_id) is None:
        raise BadRequest(
            "Invalid conversation stream client id", field="streamClientId")
    raw_streams = request.args.getlist("streams")
    if len(raw_streams) != 1:
        raise BadRequest("streams must be supplied exactly once", field="streams")
    try:
        streams = decode(
            "ConversationEventSubscriptions",
            json.loads(decode_proxy_json_value(raw_streams[0])))
    except ValueError as exc:  # malformed JSON or a ContractViolation
        raise BadRequest(
            "Invalid conversation stream subscriptions", field="streams") from exc
    if not streams:
        # The schema declares minItems, which the shared contract validators
        # do not enforce; this boundary checks it explicitly.
        raise BadRequest(
            "At least one conversation stream is required", field="streams")
    conversation_ids = [stream["conversationId"] for stream in streams]
    if len(set(conversation_ids)) != len(conversation_ids):
        raise BadRequest(
            "Each conversation may be streamed once per response", field="streams")
    return stream_client_id, streams


def _stream_closed_event(conversation_id: str, status: int, code: str) -> dict:
    return decode("ConversationStreamClosed", {
        "contract": "tofu.conversation-sync.stream-closed/v1",
        "type": "stream.closed",
        "conversationId": conversation_id,
        "status": status,
        "code": (code or "closed")[:128],
    })


async def _multiplexed_stream_frames(
    index: int,
    stream: dict,
    *,
    user_id,
    principal: str,
    stream_client_id: str,
    diagnostic_options: dict[str, str],
):
    """One conversation's replay on the multiplexed response.

    Setup mirrors conversation_sync_events. Where that route answers a status,
    this stream sends stream.closed with the same status and ends alone: one
    conversation's refusal or failure never stops the others.
    """
    conversation_id = stream["conversationId"]
    frame = partial(_multiplexed_sse_frame, index)

    async def closed(status: int, code: str) -> str:
        return await _encode_transport(
            frame, _stream_closed_event(conversation_id, status, code))

    # A received cursor resumes like Last-Event-ID and is budgeted; only a
    # stream that has received nothing since its snapshot repeats
    # cursorSource=snapshot, exactly as a native reconnect without the header.
    catch_up_budget = None if stream.get("cursorSource") else stream.get("windowBytes")
    try:
        after_sequence = _service.sequence_from_cursor(
            conversation_id, user_id, stream.get("after"))
    except ConversationCursorError:
        yield await closed(400, "invalid_cursor")
        return
    # Subscribe before the ownership/read probe, as the single stream does.
    subscription = broker.subscribe(
        user_id,
        conversation_id,
        principal_key=principal,
        stream_client_id=stream_client_id,
        stream_generation=stream["streamGeneration"],
    )
    frames = None
    try:
        if subscription.closed:
            yield await closed(204, subscription.close_reason)
            return
        record_stream_admission("conversation-sync", "admitted")
        try:
            await asyncio.to_thread(
                _service.changes,
                conversation_id,
                user_id,
                after_sequence=after_sequence,
                limit=1,
                **diagnostic_options,
            )
        except ConversationSyncNotFound:
            yield await closed(404, "not_found")
            return
        frames = _conversation_change_frames(
            conversation_id,
            user_id,
            subscription,
            after_sequence=after_sequence,
            catch_up_budget=catch_up_budget,
            diagnostic_options=diagnostic_options,
            frame=frame,
        )
        async for encoded in frames:
            yield encoded
        yield await closed(
            204, subscription.close_reason if subscription.closed else "ended")
    except StorageError as exc:
        logger.warning(
            "Multiplexed conversation stream stopped on storage failure "
            "conversation=%s code=%s", conversation_id, exc.code)
        yield await closed(http_status_for_storage_error(exc), exc.code)
    except ContractViolation:
        logger.error(
            "Multiplexed conversation stream violated its contract "
            "conversation=%s", conversation_id, exc_info=True)
        yield await closed(500, "contract_violation")
    except Exception:
        # Isolating the failure is the point of this boundary: the response
        # carries every other conversation's stream.
        logger.error(
            "Multiplexed conversation stream failed conversation=%s",
            conversation_id, exc_info=True)
        yield await closed(500, "internal_error")
    finally:
        try:
            if frames is not None:
                await frames.aclose()
        finally:
            subscription.close("generator_closed")


_FRAME_SOURCE_ENDED = object()


async def _merged_frames(sources: list):
    """Interleave ordered frame sources with one pending frame per source.

    A source advances only after its previous frame was written, so each keeps
    the response's backpressure. Ending the merge, including by client
    disconnect, cancels pending reads and closes every source.
    """
    async def advance(source):
        try:
            return await anext(source)
        except StopAsyncIteration:
            return _FRAME_SOURCE_ENDED

    pending = {asyncio.create_task(advance(source)): source for source in sources}
    try:
        while pending:
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                source = pending.pop(task)
                encoded = task.result()
                if encoded is _FRAME_SOURCE_ENDED:
                    continue
                pending[asyncio.create_task(advance(source))] = source
                yield encoded
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for source in sources:
            await source.aclose()


@conversation_sync_v3_bp.route("/api/v4/conversation-events", methods=["GET"])
@require_scope("chat")
async def conversation_sync_multiplexed_events():
    user_id = _request_user_id()
    try:
        stream_client_id, streams = _multiplexed_stream_request()
        diagnostic_options = _diagnostic_options()
    except BadRequest as exc:
        return _client_fault_response(exc)
    principal = principal_key(current_auth())
    # Each stream subscribes when the body starts, so a response that is never
    # consumed holds no subscription.
    sources = [
        _multiplexed_stream_frames(
            index,
            stream,
            user_id=user_id,
            principal=principal,
            stream_client_id=stream_client_id,
            diagnostic_options=diagnostic_options,
        )
        for index, stream in enumerate(streams)
    ]
    return sse_response(
        _merged_frames(sources),
        timeout_none=True,
        extra_headers={
            "X-Tofu-Task-Kind": "conversation-sync",
            "X-Tofu-Contract": "tofu.conversation-sync.multiplexed-events/v1",
        },
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns", methods=["POST"]
)
@require_scope("chat")
def conversation_turn_create_v3(conversation_id: str):
    execution_clock = request_execution_clock()
    request_started_at = time.time()
    try:
        _validate_body("CreateTurnRequest")
    except ContractViolation as exc:
        return api_bad_request(str(exc), violations=list(exc.violations))
    body = parse_body()
    user_id = _request_user_id()
    return _command_response(
        "turn.create",
        lambda: conversation_turn_commands.create_turn(
            conversation_id,
            user_id,
            body,
            request_started_at=request_started_at,
            execution_clock=execution_clock,
            principal=_request_principal(),
        ),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/queue/<queue_id>",
    methods=["DELETE"],
)
@require_scope("chat")
def conversation_queue_cancel_v3(conversation_id: str, queue_id: str):
    if not queue_id or len(queue_id) > 256:
        return api_bad_request("Invalid queue identity")
    return _command_response(
        "turn.queue.cancel",
        lambda: CommandOutcome(cancel_queued_turn_pair(
            conversation_id, queue_id, user_id=_request_user_id(),
        )),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/settled", methods=["POST"]
)
@require_scope("chat")
def conversation_settled_turn_append_v3(conversation_id: str):
    try:
        _validate_body("AppendSettledTurnRequest")
    except ContractViolation as exc:
        return api_bad_request(str(exc), violations=list(exc.violations))
    return _command_response(
        "turn.append_settled",
        lambda: conversation_turn_commands.append_settled_turn(
            conversation_id, _request_user_id(), parse_body()
        ),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>", methods=["PATCH"]
)
@require_scope("chat")
def conversation_turn_update_v3(conversation_id: str, turn_id: str):
    try:
        _validate_body("UpdateTurnRequest")
    except ContractViolation as exc:
        return api_bad_request(str(exc), violations=list(exc.violations))
    body = parse_body()
    user_id = _request_user_id()
    return _command_response(
        "turn.patch",
        lambda: conversation_turn_commands.update_turn(
            conversation_id, turn_id, user_id, body),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/perception",
    methods=["POST"],
)
@require_scope("chat")
def conversation_turn_perception_record_v3(
    conversation_id: str, turn_id: str,
):
    try:
        _validate_body("RecordPerceptionRequest")
    except ContractViolation as exc:
        return api_bad_request(str(exc), violations=list(exc.violations))
    return _command_response(
        "turn.perception.record",
        lambda: conversation_turn_commands.record_perception(
            conversation_id, turn_id, _request_user_id(), parse_body()),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/plan/execute",
    methods=["POST"],
)
@require_scope("chat")
def conversation_plan_execute_v3(conversation_id: str, turn_id: str):
    execution_clock = request_execution_clock()
    request_started_at = time.time()
    try:
        _validate_body("ExecutePlanRequest")
    except ContractViolation as exc:
        return api_bad_request(str(exc), violations=list(exc.violations))
    body = parse_body()
    user_id = _request_user_id()
    return _command_response(
        "turn.plan.execute",
        lambda: conversation_turn_commands.execute_plan(
            conversation_id,
            turn_id,
            user_id,
            body,
            request_started_at=request_started_at,
            execution_clock=execution_clock,
            principal=_request_principal(),
        ),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/usage-recovery", methods=["GET"]
)
@require_scope("chat")
def conversation_usage_recovery_v3(conversation_id: str, turn_id: str):
    user_id = _request_user_id()
    response, status = _command_response("turn.usage_recovery.inspect", lambda:
        conversation_turn_commands.usage_recovery(conversation_id, turn_id, user_id))
    response.headers['Cache-Control'] = 'private, no-store'
    return response, status


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/usage-recovery", methods=["POST"]
)
@require_scope("chat")
def conversation_recover_usage_v3(conversation_id: str, turn_id: str):
    execution_clock = request_execution_clock()
    request_started_at = time.time()
    try:
        _validate_body("RecoverUsageRequest")
    except ContractViolation as exc:
        return api_bad_request(str(exc), violations=list(exc.violations))
    body = parse_body(exact=(
        'commandId', 'expectedProjectionRevision', 'confirmed', 'budget'))
    user_id = _request_user_id()
    principal = _request_principal()
    return _command_response("turn.usage_recovery", lambda:
        conversation_turn_commands.recover_usage(conversation_id, turn_id, user_id, body,
            request_started_at=request_started_at, execution_clock=execution_clock, principal=principal))


def _conversation_file_changes_command(
    conversation_id: str, turn_id: str, operation: str
):
    try:
        _validate_body("FileChangesCommandRequest")
    except ContractViolation as exc:
        return api_bad_request(str(exc), violations=list(exc.violations))
    body = parse_body()
    user_id = _request_user_id()
    return _command_response(
        f"turn.file_changes.{operation}",
        lambda: conversation_turn_commands.mutate_turn_file_changes(
            conversation_id,
            turn_id,
            user_id,
            body,
            operation=operation,
        ),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/file-changes/undo",
    methods=["POST"],
)
@require_scope("chat")
def conversation_file_changes_undo_v3(conversation_id: str, turn_id: str):
    return _conversation_file_changes_command(conversation_id, turn_id, "undo")


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/file-changes/redo",
    methods=["POST"],
)
@require_scope("chat")
def conversation_file_changes_redo_v3(conversation_id: str, turn_id: str):
    return _conversation_file_changes_command(conversation_id, turn_id, "redo")


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/attempts",
    methods=["POST"],
)
@require_scope("chat")
def conversation_attempt_create_v3(conversation_id: str, turn_id: str):
    execution_clock = request_execution_clock()
    request_started_at = time.time()
    try:
        _validate_body("CreateAttemptRequest")
    except ContractViolation as exc:
        return api_bad_request(str(exc), violations=list(exc.violations))
    body = parse_body()
    user_id = _request_user_id()
    return _command_response(
        "turn.attempt.create",
        lambda: conversation_turn_commands.create_attempt(
            conversation_id,
            turn_id,
            user_id,
            body,
            request_started_at=request_started_at,
            execution_clock=execution_clock,
            principal=_request_principal(),
        ),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/lanes",
    methods=["POST"],
)
@require_scope("chat")
def conversation_lane_create_v3(conversation_id: str, turn_id: str):
    try:
        _validate_body("CreateLaneRequest")
    except ContractViolation as exc:
        return api_bad_request(str(exc), violations=list(exc.violations))
    body = parse_body()
    user_id = _request_user_id()
    return _command_response(
        "turn.lane.create",
        lambda: conversation_turn_commands.create_lane(
            conversation_id, turn_id, user_id, body),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/<turn_id>/lanes/<lane_id>",
    methods=["DELETE"],
)
@require_scope("chat")
def conversation_lane_delete_v3(
    conversation_id: str, turn_id: str, lane_id: str
):
    user_id = _request_user_id()
    return _command_response(
        "turn.lane.delete",
        lambda: conversation_turn_commands.delete_lane(
            conversation_id, turn_id, lane_id, user_id),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/turns/delete", methods=["POST"]
)
@require_scope("chat")
def conversation_turns_delete_v3(conversation_id: str):
    try:
        _validate_body("DeleteTurnsRequest")
    except ContractViolation as exc:
        return api_bad_request(str(exc), violations=list(exc.violations))
    body = parse_body()
    user_id = _request_user_id()
    return _command_response(
        "turn.delete",
        lambda: conversation_turn_commands.delete_turns(
            conversation_id, user_id, body),
    )


@conversation_sync_v3_bp.route(
    "/api/v4/attempts/<attempt_id>/abort", methods=["POST"]
)
@require_scope("chat")
def conversation_attempt_abort_v3(attempt_id: str):
    body = parse_body()
    if body:
        return api_bad_request("Abort request does not accept fields")
    user_id = _request_user_id()
    return _command_response(
        "turn.attempt.abort",
        lambda: conversation_turn_commands.abort_attempt(attempt_id, user_id),
    )


__all__ = ["conversation_sync_v3_bp"]

"""Thin HTTP adapter for the generated conversation-sync v3 contract."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import ExitStack
import hmac
import json
import re
import time

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
from lib.http_transport.request_lifecycle import request_execution_clock
from lib.log import get_logger
from lib.observability import record_stream_admission
from lib.request_parser import parse_body
from lib.storage.errors import StorageError
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
        raise ValueError("Invalid conversation segment payload")
    return values[0]


def _snapshot_tool_payload(segment_payload: str) -> str:
    values = request.args.getlist("toolPayload")
    if not values:
        return "full"
    if (len(values) != 1 or values[0] not in {"full", "lazy"}
            or (values[0] == "lazy" and segment_payload != "refs")):
        raise ValueError("Invalid conversation tool payload")
    return values[0]


def _snapshot_turn_limit() -> int | None:
    """Decode the contract-owned browser tail-window profile."""
    values = request.args.getlist("turnWindow")
    if not values:
        if request.args.getlist("windowLimit"):
            raise ValueError("windowLimit requires a bounded conversation tail")
        return None
    if len(values) != 1 or values[0] not in _SNAPSHOT_TURN_WINDOWS:
        raise ValueError("Invalid conversation snapshot turn window")
    limit = _SNAPSHOT_TURN_WINDOWS[values[0]]
    requested = _window_integer("windowLimit", 1, 96)
    if requested and limit is None:
        raise ValueError("windowLimit requires a bounded conversation tail")
    return requested or limit


def _window_integer(name: str, minimum: int, maximum: int) -> int:
    values = request.args.getlist(name)
    if not values:
        return 0
    if len(values) != 1 or re.fullmatch(r"[0-9]+", values[0]) is None:
        raise ValueError(f"Invalid conversation {name}")
    value = int(values[0])
    if not minimum <= value <= maximum:
        raise ValueError(f"Invalid conversation {name}")
    return value


def _snapshot_artifact_hint() -> bool:
    """Opt into the bounded artifact-existence projection exactly once."""
    values = request.args.getlist("artifactHint")
    if not values:
        return False
    if len(values) != 1 or values[0] != "has-any":
        raise ValueError("Invalid conversation snapshot artifact hint")
    return True


def _turn_history_page_request() -> tuple[str, int, int | None, int, str]:
    """Decode one strict, bounded lane-history query."""
    lane_values = request.args.getlist("laneId")
    if len(lane_values) != 1:
        raise ValueError("A conversation history laneId is required")
    lane_id = lane_values[0]
    if not lane_id or lane_id != lane_id.strip() or len(lane_id) > 128:
        raise ValueError("Invalid conversation history laneId")

    sync_values = request.args.getlist("syncSeq")
    if len(sync_values) != 1:
        raise ValueError("A conversation history syncSeq is required")
    try:
        sync_sequence = int(sync_values[0])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid conversation history syncSeq") from exc
    if not 0 <= sync_sequence <= _MAX_TURN_HISTORY_ORDINAL:
        raise ValueError("Invalid conversation history syncSeq")

    before_values = request.args.getlist("beforeOrdinal")
    if len(before_values) > 1:
        raise ValueError("Invalid conversation history beforeOrdinal")
    before_ordinal: int | None = None
    if before_values:
        try:
            before_ordinal = int(before_values[0])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "Invalid conversation history beforeOrdinal"
            ) from exc
        if not 0 <= before_ordinal <= _MAX_TURN_HISTORY_ORDINAL:
            raise ValueError("Invalid conversation history beforeOrdinal")

    limit_values = request.args.getlist("limit")
    if len(limit_values) > 1:
        raise ValueError("Invalid conversation history limit")
    try:
        limit = int(limit_values[0]) if limit_values else 64
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid conversation history limit") from exc
    if not 1 <= limit <= _MAX_TURN_HISTORY_PAGE:
        raise ValueError("Invalid conversation history limit")
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
        raise ValueError(f"Invalid {label} identity")
    revision_values = request.args.getlist("projectionRevision")
    if len(revision_values) != 1:
        raise ValueError(f"A {label} projectionRevision is required")
    try:
        projection_revision = int(revision_values[0])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {label} projectionRevision") from exc
    if not 1 <= projection_revision <= _MAX_TURN_HISTORY_ORDINAL:
        raise ValueError(f"Invalid {label} projectionRevision")
    owner_scope_values = request.args.getlist("ownerScope")
    if (
        len(owner_scope_values) != 1
        or _OWNER_CACHE_SCOPE.fullmatch(owner_scope_values[0]) is None
    ):
        raise ValueError(f"A valid {label} ownerScope is required")
    return projection_revision, owner_scope_values[0]


def _turn_image_request(
    conversation_id: str, turn_id: str, image_index: int,
) -> tuple[int, str]:
    if not 0 <= image_index < MAX_TURN_IMAGES:
        raise ValueError("Invalid conversation Turn image identity")
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


def _command_response(
    operation: str, execute: Callable[[], CommandOutcome]
):
    """Map application errors once; versioned handlers only bind DTOs."""
    try:
        return api_ok(execute().value)
    except LifecycleConflict as exc:
        return lifecycle_conflict_response(exc)
    except LifecycleNotFound as exc:
        return api_not_found(str(exc))
    except (TypeError, ValueError) as exc:
        return api_bad_request(str(exc))
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
        return api_internal_error("internal_error")


def _sse_frame(event: dict, *, cursor: str | None = None) -> str:
    lines = []
    if cursor:
        lines.append(f"id: {cursor}")
    lines.append(f"event: {event['type']}")
    lines.append(
        "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    )
    return "\n".join(lines) + "\n\n"


def _stream_identity() -> tuple[str, int]:
    """Decode the generated EventSource ownership query as one strict pair."""
    stream_client_id = str(request.args.get("streamClientId") or "").strip()
    raw_generation = str(request.args.get("streamGeneration") or "").strip()
    if not stream_client_id and not raw_generation:
        # Compatibility for an already-loaded pre-upgrade page. Legacy streams
        # remain globally bounded, but cannot supersede an exact predecessor.
        return "", 0
    if not stream_client_id or not raw_generation:
        raise ValueError(
            "streamClientId and streamGeneration must be supplied together")
    if _STREAM_CLIENT_ID.fullmatch(stream_client_id) is None:
        raise ValueError("Invalid conversation stream client id")
    try:
        stream_generation = int(raw_generation)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid conversation stream generation") from exc
    if not 1 <= stream_generation <= _MAX_STREAM_GENERATION:
        raise ValueError("Invalid conversation stream generation")
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
            raise ValueError("windowBytes requires a bounded conversation tail")
        include_artifact_hint = _snapshot_artifact_hint()
    except ValueError as exc:
        return api_bad_request(str(exc))
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
        response, status = api_prevalidated_payload(snapshot)
        encode_ms = (time.perf_counter() - encode_started) * 1000
        response.headers["Server-Timing"] = (
            f"snapshot;dur={read_ms:.2f}, encode;dur={encode_ms:.2f}"
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
            raise ValueError("Invalid laneId or anchorTurnId")
        def integer(name, default, maximum):
            raw = request.args.get(name)
            if raw is None and default is None:
                raise ValueError(f"{name} is required")
            value = int(raw) if raw is not None else default
            if value < (1 if name == "limit" else 0) or value > maximum:
                raise ValueError(f"Invalid {name}")
            return value
        result = await asyncio.to_thread(
            _service.round_directory, conversation_id, _request_user_id(),
            lane_id=lane, expected_sync_sequence=integer("syncSeq", None, 9223372036854775807),
            offset=integer("offset", 0, 9223372036854775807),
            limit=integer("limit", 50, 100), anchor_turn_id=anchor,
        )
        return api_prevalidated_payload(result)
    except ContractViolation as exc:
        return contract_violation_response(exc, operation="conversation.sync.round_directory")
    except ValueError as exc:
        return api_bad_request(str(exc))
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
    # ContractViolation is a ValueError subclass: the durable-data drift must
    # map to its typed 500 BEFORE request-shape ValueErrors degrade to a 400.
    except ContractViolation as exc:
        return contract_violation_response(
            exc, operation="conversation.sync.turn_page")
    except ValueError as exc:
        return api_bad_request(str(exc))
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
            raise ValueError("Invalid tool result round index")
        # Identity/revision/scope use the same generated bounds as Turn images.
        revision, supplied_scope = _turn_projection_fence(conversation_id, turn_id, "tool result")
    except ValueError as exc:
        return api_bad_request(str(exc))
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
    except ValueError as exc:
        return api_bad_request(str(exc))
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


@conversation_sync_v3_bp.route(
    "/api/v4/conversations/<conversation_id>/events", methods=["GET"]
)
@require_scope("chat")
async def conversation_sync_events(conversation_id: str):
    user_id = _request_user_id()
    try:
        stream_client_id, stream_generation = _stream_identity()
    except ValueError as exc:
        return api_bad_request(str(exc))
    # Native EventSource reconnects preserve the newest delivered id in the
    # header while the original URL still contains its bootstrap cursor.
    # Header precedence is therefore required to avoid replaying from the
    # snapshot cursor on every transient reconnect.
    supplied_cursor = request.headers.get("Last-Event-ID") or request.args.get("after")
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
            try:
                subscription.mark_body_started()
                sequence = after_sequence
                heartbeat_due = False
                storage_failures = 0
                while not subscription.closed:
                    try:
                        page = await asyncio.to_thread(
                            _service.changes,
                            conversation_id,
                            user_id,
                            after_sequence=sequence,
                            limit=500,
                        )
                    except StorageError as exc:
                        if (not exc.retryable
                                and exc.code not in _DEGRADABLE_READ_FAILURES):
                            raise
                        storage_failures += 1
                        heartbeat = _service.heartbeat(
                            conversation_id, user_id, sequence, degraded=True,
                            push_withheld=(
                                push_withheld_for_conv(conversation_id) is not None),
                        )
                        yield _sse_frame(heartbeat)
                        await asyncio.sleep(min(0.25 * storage_failures, 2.0))
                        continue

                    storage_failures = 0
                    reset = page.get("reset")
                    if isinstance(reset, dict):
                        yield _sse_frame(reset, cursor=str(reset["cursor"]))
                        return

                    events = page.get("events") or []
                    if events:
                        for event in events:
                            sequence = int(event["syncSeq"])
                            cursor = _service.cursor_for_sequence(
                                conversation_id, user_id, sequence)
                            yield _sse_frame(event, cursor=cursor)
                        heartbeat_due = False
                        if page.get("hasMore"):
                            continue
                    elif heartbeat_due:
                        # A write-side wedge (withheld authoritative pushes) keeps
                        # the change log quiet, so heartbeats are the ONLY frame
                        # that can still reach the client — they carry the signal.
                        withheld = (
                            push_withheld_for_conv(conversation_id) is not None)
                        yield _sse_frame(_service.heartbeat(
                            conversation_id, user_id, sequence, degraded=withheld,
                            push_withheld=withheld,
                        ))
                        heartbeat_due = False

                    awakened = await subscription.wait(
                        _service.heartbeat_interval_ms / 1000.0
                    )
                    heartbeat_due = not awakened
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
    body = parse_body()
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

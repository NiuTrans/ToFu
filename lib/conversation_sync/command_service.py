"""Stateless application service for conversation turn commands.

Responsibility: normalize command intent, apply the authoritative turn
lifecycle, and bind an accepted attempt to the executor.  HTTP parsing,
authentication and response envelopes belong to route adapters. Storage
access remains behind ``turn_lifecycle``'s
explicit user-scoped repository seam.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import re
import time
import uuid
from typing import Any

from lib.error_envelope import make_envelope
from lib.conversation_sync.dispatch_contract import (
    ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY,
    CONVERSATION_EXECUTOR_DISPATCH_MODE,
)
from lib.log import get_logger
from lib.plan_contract import plan_execution_document
from lib.tool_round_replay import (
    checkpoint_retention_positions,
    scan_replayable_tool_round_prefix,
)
from lib.turn_lifecycle import (
    LifecycleConflict,
    LifecycleNotFound,
    abort_attempt,
    activate_queued_turn_pair,
    append_settled_turn,
    attempt_dispatch_lock,
    bind_task,
    claim_attempt_start,
    commit_user_steer,
    create_attempt,
    create_branch_lane,
    create_turn_pair,
    delete_branch_lane,
    delete_turns,
    fail_start,
    get_attempt,
    get_conversation_revision,
    get_turn,
    list_dispatchable_attempts,
    record_turn_perception,
    update_turn_projection,
)
from lib.turn_projection_segments import public_value_with_stable_segments


logger = get_logger(__name__)

def _durable_dispatch_config(
    config: Mapping[str, Any], request_started_at: float,
) -> dict[str, Any]:
    """Stamp the server cancellation watermark into the accepted attempt."""
    durable = dict(config)
    durable[ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY] = int(
        max(0.0, float(request_started_at)) * 1000
    )
    return durable


def _without_client_plan_authority(
    projection: Mapping[str, Any], *, allow_proposed_plan: bool = False,
) -> dict[str, Any]:
    """Strip server-authored execution authority from a client projection."""
    result = dict(projection)
    result.pop("planExecution", None)
    if not allow_proposed_plan:
        result.pop("proposedPlan", None)
    return result


BuildUserMessage = Callable[
    [Mapping[str, Any], Mapping[str, Any], str, Any], Any
]
WasAbortedAfter = Callable[[str, Any, float], bool]
StartTask = Callable[
    [
        str,
        dict[str, Any],
        Mapping[str, Any],
        float | None,
        Callable[[str], None],
    ],
    tuple[str, Any],
]
MutateFileChanges = Callable[
    [str, str, str, Any], Mapping[str, Any]
]
RetainAttachments = Callable[[Mapping[str, Any], Any], None]


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """A public command value plus adapter-only compatibility metadata."""

    value: dict[str, Any]
    notify_legacy_clients: bool = True


class AttemptStartFailure(RuntimeError):
    """The durable attempt exists, but executor startup could not complete."""

    def __init__(self, latest_turn: Mapping[str, Any] | None) -> None:
        self.envelope = attempt_start_error_envelope()
        super().__init__(self.envelope["message"])
        self.latest_turn = dict(latest_turn or {})


def attempt_start_error_envelope() -> dict[str, Any]:
    """Return the one durable/wire error for executor startup failure."""
    return make_envelope(
        "task_start_failed",
        detail="The durable attempt was created, but no executor task started.",
        context="turn-start",
        source="conversation-turn-command",
    )


class ConversationTurnCommandService:
    """Apply command DTOs with explicit owner identity and injected gateways."""

    def __init__(
        self,
        *,
        build_user_message: BuildUserMessage,
        was_aborted_after: WasAbortedAfter,
        start_task: StartTask,
        mutate_file_changes: MutateFileChanges | None = None,
        retain_attachments: RetainAttachments | None = None,
    ) -> None:
        self._build_user_message = build_user_message
        self._was_aborted_after = was_aborted_after
        self._start_task = start_task
        self._mutate_file_changes = mutate_file_changes
        self._retain_attachments = retain_attachments

    def create_turn(
        self,
        conversation_id: str,
        user_id: Any,
        body: Mapping[str, Any],
        *,
        request_started_at: float,
        trusted_goal_objective: str | None = None,
    ) -> CommandOutcome:
        data = dict(body)
        command_id = str(data.get("commandId") or "")
        if not command_id:
            raise ValueError("commandId required")
        config = data.get("config") or {}
        if not isinstance(config, Mapping):
            raise ValueError("config object required")
        from lib.tasks_pkg.plan_mode import (
            normalize_interaction_mode_conversation_settings,
            normalize_interaction_mode_runtime_config,
            plan_mode_enabled,
        )
        config = normalize_interaction_mode_runtime_config(config)
        config.pop(
            ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY, None,
        )
        # Goal lifecycle identity/objective are server-authored.  A public
        # caller may select Goal Mode, but cannot tunnel a different objective
        # or impersonate an existing run through arbitrary config fields.
        for internal_goal_key in (
            '_goalObjective', '_goalRunId', '_goalRunStatus',
            '_goalRunReason', '_goalRunPolicy', '_goalContinuationCommand',
        ):
            config.pop(internal_goal_key, None)
        input_projection = data.get("inputTurn")
        raw_message = data.get("message")
        if input_projection is None and isinstance(raw_message, Mapping):
            input_projection = self._build_user_message(
                raw_message, config, conversation_id, user_id
            )
        if input_projection is None:
            input_projection = data.get("input", raw_message or "")
        if isinstance(input_projection, Mapping):
            input_projection = _without_client_plan_authority(input_projection)
            if trusted_goal_objective is None:
                # Queue classification is server authority. Public callers
                # cannot disguise an ordinary human message as a cancellable
                # Goal Mode continuation by forging projection metadata.
                input_projection.pop('_goalContinuation', None)

        trusted_goal_continuation = bool(
            trusted_goal_objective
            and isinstance(input_projection, Mapping)
            and input_projection.get('_goalContinuation') is True
        )

        if config.get('autopilot') is True:
            from lib.goal_runs.objective import projection_text

            objective = (
                str(trusted_goal_objective or '').strip()
                or projection_text(input_projection)
            )
            if objective:
                config['_goalObjective'] = objective
            if trusted_goal_continuation:
                config['_goalContinuationCommand'] = True

        if self._was_aborted_after(
            conversation_id, user_id, request_started_at
        ):
            try:
                revision = get_conversation_revision(
                    conversation_id, user_id=user_id
                )
            except LifecycleNotFound:
                revision = 0
            return CommandOutcome(
                {
                    "aborted": True,
                    "conversationId": conversation_id,
                    "conversationRevision": revision,
                },
                notify_legacy_clients=False,
            )

        conversation_defaults = data.get("conversation")
        if isinstance(conversation_defaults, Mapping):
            conversation_defaults = dict(conversation_defaults)
            persisted_settings = dict(
                conversation_defaults.get("settings")
                if isinstance(conversation_defaults.get("settings"), Mapping)
                else {}
            )
            persisted_settings["planMode"] = plan_mode_enabled(config)
            for runtime_key, settings_key in (
                ("autopilot", "autopilotEnabled"),
                ("activeFlow", "activeFlow"),
                ("imageGenMode", "imageGenMode"),
            ):
                if runtime_key in config:
                    persisted_settings[settings_key] = config[runtime_key]
            conversation_defaults["settings"] = (
                normalize_interaction_mode_conversation_settings(
                    persisted_settings
                )
            )
            if not conversation_defaults.get("title") and isinstance(
                raw_message, Mapping
            ):
                title_text = str(raw_message.get("text") or "")
                title_text = re.sub(
                    r"</?(?:notranslate|nt)>", "", title_text,
                    flags=re.IGNORECASE,
                )
                if title_text:
                    conversation_defaults["title"] = (
                        title_text[:60] + ("..." if len(title_text) > 60 else "")
                    )
        else:
            conversation_defaults = None

        output_actor = str(data.get("actor") or "assistant")
        output_kind = str(data.get("kind") or "reply")
        if plan_mode_enabled(config):
            # Planner is durable provenance/presentation, not a renderer guess.
            from lib.tasks_pkg.plan_mode import (
                interaction_mode_generated_turn_identity,
            )

            output_actor, output_kind = (
                interaction_mode_generated_turn_identity(config)
            )
        try:
            result = create_turn_pair(
                conversation_id,
                command_id=command_id,
                input_projection=input_projection,
                config=_durable_dispatch_config(config, request_started_at),
                lane_id=str(data.get("laneId") or "main"),
                parent_turn_id=data.get("parentTurnId"),
                kind=output_kind,
                output_actor=output_actor,
                run_id=str(data.get("runId") or ""),
                user_id=user_id,
                input_actor=str(data.get("inputActor") or "human"),
                input_kind=str(data.get("inputKind") or "input"),
                require_parent_is_lane_tail=bool(
                    data.get("requireParentIsLaneTail", False)
                ),
                # A Goal continuation is a fresh conversation command, not
                # an in-attempt orchestration child. Its virtual-user actor
                # must therefore honor the same single-live-lane fence as a
                # human input. Only the trusted internal objective boundary
                # can enable this authority.
                require_lane_idle=trusted_goal_continuation,
                reject_if_human_queued=trusted_goal_continuation,
                conversation_defaults=conversation_defaults,
                dispatch_mode=CONVERSATION_EXECUTOR_DISPATCH_MODE,
                input_presentation_id=f"{command_id}:input",
                output_presentation_id=f"{command_id}:output",
            )
        except LifecycleConflict as exc:
            if exc.code != "lane_busy":
                raise
            delivered = self._deliver_while_running(
                conversation_id,
                user_id,
                data,
                config,
                input_projection,
                raw_message,
                exc,
                output_actor=output_actor,
                output_kind=output_kind,
                conversation_defaults=conversation_defaults,
                request_started_at=request_started_at,
                trusted_goal_objective=trusted_goal_objective,
            )
            if delivered is None:
                raise
            self._retain_accepted_attachments(input_projection, user_id)
            return CommandOutcome(delivered)

        self._retain_accepted_attachments(input_projection, user_id)
        self._start_accepted_attempt(
            result,
            user_id,
            config,
            data,
            abort_after_ts=request_started_at,
        )
        result["turn"] = get_turn(
            conversation_id, result["turn"]["turnId"], user_id=user_id
        )
        result["attempt"] = get_attempt(
            result["attempt"]["attemptId"], user_id=user_id
        )
        result["conversationRevision"] = get_conversation_revision(
            conversation_id, user_id=user_id
        )
        result["conversationId"] = conversation_id
        return CommandOutcome(_public_result(result))

    def _retain_accepted_attachments(
        self, projection: Any, user_id: Any,
    ) -> None:
        """Promote media drafts only after their owning turn/queue commits."""
        if self._retain_attachments is None or not isinstance(
                projection, Mapping):
            return
        try:
            self._retain_attachments(projection, user_id)
        except Exception as exc:
            # The committed turn still owns an explicit, resolvable draft ID;
            # idempotent replay can retry promotion without losing the source.
            logger.warning(
                "Attachment retention lagged accepted turn: %s", exc,
                exc_info=True,
            )

    def activate_queued_turn(
        self,
        conversation_id: str,
        user_id: Any,
        queue_id: str,
        *,
        config: Mapping[str, Any],
        request_data: Mapping[str, Any],
    ) -> CommandOutcome:
        """Start the Attempt already paired with one durable queue item."""
        result = activate_queued_turn_pair(
            conversation_id, queue_id, user_id=user_id,
        )
        self._start_accepted_attempt(
            result,
            user_id,
            config,
            request_data,
            abort_after_ts=None,
        )
        result["turn"] = get_turn(
            conversation_id, result["turn"]["turnId"], user_id=user_id,
        )
        result["attempt"] = get_attempt(
            result["attempt"]["attemptId"], user_id=user_id,
        )
        result["conversationId"] = conversation_id
        result["conversationRevision"] = get_conversation_revision(
            conversation_id, user_id=user_id,
        )
        return CommandOutcome(_public_result(result))

    def recover_dispatchable_attempts(
        self, *, created_before_ms: int, limit: int = 8,
    ) -> dict[str, int]:
        """Launch a bounded batch that committed before executor claiming.

        Storage returns only canonical executor attempts with an empty durable
        task id. The empty id is a proof that the bind-before-spawn protocol
        has not launched billable work, so recovery cannot duplicate a model
        request. Owner identity and config are rehydrated from authority.
        """
        rows = list_dispatchable_attempts(
            created_before_ms=created_before_ms,
            limit=limit,
        )
        recovered = 0
        settled_failed = 0
        for row in rows:
            turn = row.get("turn")
            attempt = row.get("attempt")
            config = row.get("config")
            if (
                not isinstance(turn, Mapping)
                or not isinstance(attempt, Mapping)
                or not isinstance(config, Mapping)
            ):
                raise RuntimeError("Dispatchable attempt has an invalid shape")
            user_id = int(row.get("userId") or 0)
            runtime_config = dict(config)
            request_started_at_ms = runtime_config.pop(
                ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY, None,
            )
            try:
                abort_after_ts = float(request_started_at_ms) / 1000.0
            except (TypeError, ValueError):
                # Direct/internal callers that predate the dispatch contract
                # have no request watermark. Their durable acceptance time is
                # the narrowest safe fallback available to recovery.
                abort_after_ts = (
                    float(attempt.get("createdAt") or 0) / 1000.0
                )
            accepted = {
                "_needsStart": True,
                "turn": dict(turn),
                "attempt": dict(attempt),
            }
            try:
                self._start_accepted_attempt(
                    accepted,
                    user_id,
                    runtime_config,
                    {},
                    abort_after_ts=abort_after_ts,
                )
            except AttemptStartFailure:
                # The application service already settled this attempt with a
                # durable error; continue so one bad model context does not
                # prevent independent accepted work from starting.
                settled_failed += 1
                continue
            current = get_attempt(
                str(attempt.get("attemptId") or ""), user_id=user_id,
            )
            task_id = str(current.get("taskId") or "")
            if task_id and not task_id.startswith("@dispatching:"):
                recovered += 1
        return {
            "examined": len(rows),
            "recovered": recovered,
            "settledFailed": settled_failed,
        }

    def append_settled_turn(
        self,
        conversation_id: str,
        user_id: Any,
        body: Mapping[str, Any],
    ) -> CommandOutcome:
        """Append one terminal document without creating an executor attempt."""
        data = dict(body)
        command_id = str(data.get("commandId") or "")
        actor = str(data.get("actor") or "")
        projection = data.get("projection")
        if not command_id:
            raise ValueError("commandId required")
        if not actor:
            raise ValueError("actor required")
        if not isinstance(projection, Mapping):
            raise ValueError("projection object required")
        settlement = data.get("settlement")
        if settlement is not None and not isinstance(settlement, Mapping):
            raise ValueError("settlement object required")
        conversation_defaults = data.get("conversation")
        if conversation_defaults is not None and not isinstance(
            conversation_defaults, Mapping
        ):
            raise ValueError("conversation object required")
        result = append_settled_turn(
            conversation_id,
            command_id=command_id,
            actor=actor,
            projection=_without_client_plan_authority(
                projection,
                allow_proposed_plan=actor in {"assistant", "planner"},
            ),
            user_id=user_id,
            kind=str(data.get("kind") or "ingested"),
            status=str(data.get("status") or "completed"),
            settlement=(dict(settlement) if settlement is not None else None),
            created_at=data.get("createdAt"),
            lane_id=str(data.get("laneId") or "main"),
            run_id=str(data.get("runId") or ""),
            conversation_defaults=(
                dict(conversation_defaults)
                if conversation_defaults is not None else None
            ),
        )
        self._retain_accepted_attachments(projection, user_id)
        result["conversationId"] = conversation_id
        return CommandOutcome(_public_result(result))

    def create_attempt(
        self,
        conversation_id: str,
        turn_id: str,
        user_id: Any,
        body: Mapping[str, Any],
        *,
        request_started_at: float,
    ) -> CommandOutcome:
        data = dict(body)
        command_id = str(data.get("commandId") or "")
        operation = str(data.get("operation") or "")
        expected = data.get("expectedProjectionRevision")
        if not command_id:
            raise ValueError("commandId required")
        if expected is None:
            raise ValueError("expectedProjectionRevision required")
        config = data.get("config") or {}
        if not isinstance(config, Mapping):
            raise ValueError("config object required")
        from lib.tasks_pkg.plan_mode import normalize_interaction_mode_runtime_config
        config = normalize_interaction_mode_runtime_config(config)
        config.pop(
            ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY, None,
        )
        input_update = data.get("inputUpdate")

        if isinstance(input_update, Mapping):
            input_update = _without_client_plan_authority(input_update)
        human_response = data.get("humanResponse")
        if operation == "answer_guidance":
            if not isinstance(human_response, str) or not human_response.strip():
                raise ValueError("humanResponse required for answer_guidance")
            if len(human_response) > 32768:
                raise ValueError(
                    "humanResponse exceeds the 32768-character limit")
            answer_anchor = _answer_guidance_anchor(
                get_turn(conversation_id, turn_id, user_id=user_id))
            if answer_anchor is None:
                raise ValueError(
                    "answer_guidance is not available for the current settlement")
            # Identity comes from the durable settlement, never from the
            # request; the response is stored with the attempt so a crashed
            # dispatch recovers with the same completed question round.
            config["_humanGuidanceAnswer"] = {
                "guidanceId": str(answer_anchor["guidanceId"]),
                "toolCallId": str(answer_anchor.get("toolCallId") or ""),
                "response": human_response,
            }
        elif human_response is not None:
            raise ValueError("humanResponse is only valid for answer_guidance")
        result = create_attempt(
            conversation_id,
            turn_id,
            command_id=command_id,
            operation=operation,
            expected_projection_revision=int(expected),
            config=_durable_dispatch_config(config, request_started_at),
            resume_anchor=data.get("resumeAnchor"),
            input_update=input_update,
            expected_input_projection_revision=data.get(
                "expectedInputProjectionRevision"
            ),

            user_id=user_id,
        )
        self._start_accepted_attempt(
            result,
            user_id,
            config,
            data,
            abort_after_ts=request_started_at,
        )
        result["turn"] = get_turn(conversation_id, turn_id, user_id=user_id)
        result["attempt"] = get_attempt(
            result["attempt"]["attemptId"], user_id=user_id
        )
        result["conversationRevision"] = get_conversation_revision(
            conversation_id, user_id=user_id
        )
        result["conversationId"] = conversation_id
        return CommandOutcome(_public_result(result))

    def execute_plan(
        self,
        conversation_id: str,
        turn_id: str,
        user_id: Any,
        body: Mapping[str, Any],
        *,
        request_started_at: float,
    ) -> CommandOutcome:
        """Accept one exact proposed plan and start a normal execution turn.

        The source turn id, projection revision, content-addressed plan id and
        lane tail are one optimistic-concurrency fence. Durable history stays
        unchanged; ``contextMode`` only controls the model-context projection
        built for the newly accepted attempt.
        """
        data = dict(body)
        command_id = str(data.get("commandId") or "")
        expected = data.get("expectedProjectionRevision")
        expected_plan_id = str(data.get("planId") or "")
        context_mode = str(data.get("contextMode") or "")
        raw_config = data.get("config") or {}
        if not command_id:
            raise ValueError("commandId required")
        if expected is None:
            raise ValueError("expectedProjectionRevision required")
        if not expected_plan_id:
            raise ValueError("planId required")
        if context_mode not in {"current", "fresh"}:
            raise ValueError("contextMode must be current or fresh")
        if not isinstance(raw_config, Mapping):
            raise ValueError("config object required")

        source = get_turn(conversation_id, turn_id, user_id=user_id)
        source_revision = int(source.get("projectionRevision") or 0)
        if source_revision != int(expected):
            raise LifecycleConflict(
                "stale_projection",
                "The proposed plan changed before execution was accepted.",
                source,
            )
        if source.get("actor") not in {"assistant", "planner"}:
            raise ValueError("plan source must be an assistant or planner turn")
        if source.get("status") != "completed":
            raise ValueError("plan source must be completed")
        projection = source.get("projection") or {}
        proposed_plan = (
            projection.get("proposedPlan")
            if isinstance(projection, Mapping) else None
        )
        if not isinstance(proposed_plan, Mapping):
            raise ValueError("turn has no authoritative proposed plan")
        plan_id = str(proposed_plan.get("planId") or "")
        if plan_id != expected_plan_id:
            raise LifecycleConflict(
                "stale_projection",
                "The selected plan no longer matches the source turn.",
                source,
            )
        handoff = plan_execution_document({
            "planText": proposed_plan.get("text"),
            "sourceTurnId": turn_id,
            "sourceProjectionRevision": source_revision,
            "contextMode": context_mode,
        })
        if handoff is None or handoff.get("planId") != plan_id:
            raise ValueError("turn has an invalid proposed plan")

        config = dict(raw_config)
        config.pop(
            ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY, None,
        )
        config.update({
            "planMode": False,
            "humanGuidanceEnabled": True,
            "autopilot": False,
            "autopilotEnabled": False,
            "imageGenMode": False,
            "activeFlow": "",
        })
        for key in ("flowDefinition", "flowBuiltin", "flowId"):
            config.pop(key, None)

        result = create_turn_pair(
            conversation_id,
            command_id=command_id,
            input_projection={"content": "", "planExecution": handoff},
            config=_durable_dispatch_config(config, request_started_at),
            lane_id=str(source.get("laneId") or "main"),
            parent_turn_id=turn_id,
            kind="plan_execution_result",
            output_actor="assistant",
            user_id=user_id,
            input_actor="human",
            input_kind="plan_execution",
            require_parent_is_lane_tail=True,
            conversation_defaults={
                "settings": {
                    "planMode": False,
                    "autopilotEnabled": False,
                    "activeFlow": "",
                    "imageGenMode": False,
                },
            },
            dispatch_mode=CONVERSATION_EXECUTOR_DISPATCH_MODE,
        )
        self._start_accepted_attempt(
            result,
            user_id,
            config,
            data,
            abort_after_ts=request_started_at,
        )
        result["turn"] = get_turn(
            conversation_id, result["turn"]["turnId"], user_id=user_id)
        result["attempt"] = get_attempt(
            result["attempt"]["attemptId"], user_id=user_id)
        result["conversationRevision"] = get_conversation_revision(
            conversation_id, user_id=user_id)
        result["conversationId"] = conversation_id
        return CommandOutcome(_public_result(result))

    def update_turn(
        self,
        conversation_id: str,
        turn_id: str,
        user_id: Any,
        body: Mapping[str, Any],
    ) -> CommandOutcome:
        expected = body.get("expectedProjectionRevision")
        projection = body.get("projection")
        if expected is None:
            raise ValueError("expectedProjectionRevision required")
        if not isinstance(projection, Mapping):
            raise ValueError("projection object required")
        current = get_turn(conversation_id, turn_id, user_id=user_id)
        safe_projection = _without_client_plan_authority(projection)
        current_projection = current.get("projection") or {}
        if isinstance(current_projection, Mapping):
            for field in ("proposedPlan", "planExecution"):
                value = current_projection.get(field)
                if isinstance(value, Mapping):
                    safe_projection[field] = dict(value)
        return CommandOutcome(_public_result(update_turn_projection(
            conversation_id,
            turn_id,
            projection=safe_projection,
            expected_projection_revision=int(expected),
            user_id=user_id,
        )))

    def mutate_turn_file_changes(
        self,
        conversation_id: str,
        turn_id: str,
        user_id: Any,
        body: Mapping[str, Any],
        *,
        operation: str,
    ) -> CommandOutcome:
        """Run one idempotent file side effect behind a turn CAS state machine."""
        if operation not in {"undo", "redo"}:
            raise ValueError("invalid file changes operation")
        if self._mutate_file_changes is None:
            raise RuntimeError("file changes command gateway is unavailable")
        command_id = str(body.get("commandId") or "")
        expected = body.get("expectedProjectionRevision")
        if not command_id:
            raise ValueError("commandId required")
        if expected is None:
            raise ValueError("expectedProjectionRevision required")

        source_state = "applied" if operation == "undo" else "undone"
        pending_state = "undoing" if operation == "undo" else "redoing"
        target_state = "undone" if operation == "undo" else "applied"
        turn = get_turn(conversation_id, turn_id, user_id=user_id)
        projection = dict(turn.get("projection") or {})
        raw_block = projection.get("fileChanges")
        if not isinstance(raw_block, Mapping) or not raw_block.get("files"):
            raise ValueError("turn has no authoritative file changes block")
        block = dict(raw_block)
        state = str(block.get("state") or "applied")
        replay = block.get("commandId") == command_id
        if replay and state == target_state:
            return CommandOutcome({
                "turn": turn,
                "conversationRevision": get_conversation_revision(
                    conversation_id, user_id=user_id),
                "idempotentReplay": True,
                **({"effect": block["effect"]} if block.get("effect") else {}),
            })
        if not (replay and state == pending_state):
            if state != source_state:
                raise ValueError(
                    f"file changes are {state}; cannot {operation}")
            pending_projection = dict(projection)
            pending_projection["fileChanges"] = {
                **block,
                "state": pending_state,
                "commandId": command_id,
                "error": None,
            }
            update_turn_projection(
                conversation_id,
                turn_id,
                projection=pending_projection,
                expected_projection_revision=int(expected),
                user_id=user_id,
            )

        task_id = str(block.get("taskId") or "")
        if not task_id and turn.get("currentAttemptId"):
            attempt = get_attempt(
                str(turn["currentAttemptId"]), user_id=user_id)
            task_id = str(attempt.get("taskId") or "")
        if not task_id:
            self._settle_file_changes_projection(
                conversation_id, turn_id, user_id=user_id,
                command_id=command_id, state=source_state,
                error="The turn has no executor task identity.",
            )
            raise ValueError("turn has no executor task identity")

        try:
            effect = dict(self._mutate_file_changes(
                operation, task_id, conversation_id, user_id))
            if effect.get("ok") is False:
                raise ValueError(str(effect.get("error") or "file operation failed"))
        except Exception as exc:
            try:
                self._settle_file_changes_projection(
                    conversation_id, turn_id, user_id=user_id,
                    command_id=command_id, state=source_state, error=str(exc),
                )
            except Exception:
                logger.warning(
                    "Could not settle failed file command turn=%s command=%s",
                    turn_id, command_id, exc_info=True,
                )
            raise

        result = self._settle_file_changes_projection(
            conversation_id, turn_id, user_id=user_id,
            command_id=command_id, state=target_state, effect=effect,
        )
        result["effect"] = effect
        return CommandOutcome(_public_result(result))

    @staticmethod
    def _settle_file_changes_projection(
        conversation_id: str,
        turn_id: str,
        *,
        user_id: Any,
        command_id: str,
        state: str,
        effect: Mapping[str, Any] | None = None,
        error: Any = None,
    ) -> dict[str, Any]:
        """Merge a command verdict without overwriting concurrent turn fields."""
        last_conflict: LifecycleConflict | None = None
        for _attempt in range(3):
            current = get_turn(conversation_id, turn_id, user_id=user_id)
            projection = dict(current.get("projection") or {})
            raw_block = projection.get("fileChanges")
            if not isinstance(raw_block, Mapping):
                raise ValueError("file changes block disappeared")
            block = dict(raw_block)
            if block.get("commandId") != command_id:
                raise LifecycleConflict(
                    "stale_projection",
                    "A newer file changes command replaced this command.",
                    current,
                )
            block["state"] = state
            if effect is not None:
                block["effect"] = dict(effect)
            if error:
                block["error"] = error
            else:
                block.pop("error", None)
            projection["fileChanges"] = block
            try:
                return update_turn_projection(
                    conversation_id,
                    turn_id,
                    projection=projection,
                    expected_projection_revision=int(
                        current.get("projectionRevision") or 0),
                    user_id=user_id,
                )
            except LifecycleConflict as exc:
                if exc.code != "stale_projection":
                    raise
                last_conflict = exc
        if last_conflict is not None:
            raise last_conflict
        raise LifecycleConflict("stale_projection", "Could not settle file command")

    def create_lane(
        self,
        conversation_id: str,
        turn_id: str,
        user_id: Any,
        body: Mapping[str, Any],
    ) -> CommandOutcome:
        expected = body.get("expectedProjectionRevision")
        if expected is None:
            raise ValueError("expectedProjectionRevision required")
        return CommandOutcome(_public_result(create_branch_lane(
            conversation_id,
            turn_id,
            title=str(body.get("title") or "Branch"),
            anchor_text=str(body.get("anchorText") or ""),
            parent_selection=str(body.get("parentSelection") or ""),
            kind=str(body.get("kind") or "branch"),
            expected_projection_revision=int(expected),
            user_id=user_id,
        )))

    def delete_lane(
        self,
        conversation_id: str,
        turn_id: str,
        lane_id: str,
        user_id: Any,
    ) -> CommandOutcome:
        return CommandOutcome(_public_result(delete_branch_lane(
            conversation_id, turn_id, lane_id, user_id=user_id
        )))

    def delete_turns(
        self,
        conversation_id: str,
        user_id: Any,
        body: Mapping[str, Any],
    ) -> CommandOutcome:
        return CommandOutcome(_public_result(delete_turns(
            conversation_id, list(body.get("turnIds") or []), user_id=user_id
        )))

    def abort_attempt(self, attempt_id: str, user_id: Any) -> CommandOutcome:
        return CommandOutcome(
            dict(abort_attempt(attempt_id, user_id=user_id) or {}),
            notify_legacy_clients=False,
        )

    def record_perception(
        self,
        conversation_id: str,
        turn_id: str,
        user_id: Any,
        body: Mapping[str, Any],
    ) -> CommandOutcome:
        return CommandOutcome(
            _public_result(record_turn_perception(
                conversation_id,
                turn_id,
                attempt_id=str(body.get('attemptId') or ''),
                observation=body,
                user_id=user_id,
            ) or {}),
            notify_legacy_clients=False,
        )

    def _start_accepted_attempt(
        self,
        result: dict[str, Any],
        user_id: Any,
        request_config: Mapping[str, Any],
        request_data: Mapping[str, Any],
        *,
        abort_after_ts: float | None,
    ) -> None:
        if not result.get("_needsStart"):
            return
        attempt_id = str((result.get("attempt") or {}).get("attemptId") or "")
        if not attempt_id:
            raise RuntimeError("Accepted attempt is missing its identity")
        with attempt_dispatch_lock(attempt_id):
            self._start_accepted_attempt_serialized(
                result,
                user_id,
                request_config,
                request_data,
                abort_after_ts=abort_after_ts,
            )

    def _start_accepted_attempt_serialized(
        self,
        result: dict[str, Any],
        user_id: Any,
        request_config: Mapping[str, Any],
        request_data: Mapping[str, Any],
        *,
        abort_after_ts: float | None,
    ) -> None:
        """Claim, bind, and launch while this process owns the attempt stripe."""
        turn = result["turn"]
        attempt = result["attempt"]
        if not claim_attempt_start(attempt["attemptId"], user_id=user_id):
            return
        operation = attempt["operation"]
        config = dict(request_config)
        config.update({
            "_turnId": turn["turnId"],
            "_attemptId": attempt["attemptId"],
            "_turnActor": turn["actor"],
            "_turnKind": turn["kind"],
            # Authenticated ownership is injected by the application service,
            # never accepted from the user-provided config object. Detached
            # workers carry it after request context has disappeared.
            "_turnOwnerUserId": user_id,
            "excludeLast": True,
        })
        projection = turn.get("projection") or {}
        config.update(_resume_executor_config(
            operation, projection, config.get("_humanGuidanceAnswer")))
        if (operation in _RESUME_FILE_CARRY_OPERATIONS
                and "checkpointModifiedFileList" not in config):
            config.update(_journal_resume_file_fields(turn, config))
        bound_task_id: str | None = None

        def bind_registered_task(task_id: str) -> None:
            nonlocal bound_task_id
            normalized_task_id = str(task_id or "")
            if not normalized_task_id:
                raise RuntimeError("Executor registered an empty task id")
            if bound_task_id is not None:
                if bound_task_id != normalized_task_id:
                    raise RuntimeError("Executor registered two task identities")
                return
            bound = bind_task(
                attempt["attemptId"], normalized_task_id, user_id=user_id,
            )
            if bound is None:
                raise RuntimeError("Accepted attempt disappeared before task binding")
            bound_task_id = normalized_task_id

        try:
            task_id, error_response = self._start_task(
                turn["conversationId"],
                config,
                request_data,
                abort_after_ts,
                bind_registered_task,
            )
        except Exception as exc:
            logger.exception(
                "Attempt %s executor startup raised before task binding",
                str(attempt["attemptId"])[:16],
            )
            self._fail_attempt_start(attempt, user_id)
            raise AttemptStartFailure(get_turn(
                turn["conversationId"], turn["turnId"], user_id=user_id
            )) from exc
        if error_response is not None:
            self._fail_attempt_start(attempt, user_id)
            raise AttemptStartFailure(get_turn(
                turn["conversationId"], turn["turnId"], user_id=user_id
            ))
        normalized_task_id = str(task_id or "")
        if bound_task_id != normalized_task_id:
            # There is deliberately no post-spawn compatibility bind. An
            # executor that returns before invoking the registration hook has
            # violated the dispatch protocol; accepting it would recreate the
            # orphan-worker window this handshake exists to close.
            self._fail_attempt_start(attempt, user_id)
            raise AttemptStartFailure(get_turn(
                turn["conversationId"], turn["turnId"], user_id=user_id
            ))

    @staticmethod
    def _fail_attempt_start(attempt: Mapping[str, Any], user_id: Any) -> None:
        """Durably settle a claimed attempt before exposing startup failure."""
        fail_start(
            attempt["attemptId"], attempt_start_error_envelope(), user_id=user_id,
        )

    def _deliver_while_running(
        self,
        conversation_id: str,
        user_id: Any,
        data: Mapping[str, Any],
        config: Mapping[str, Any],
        input_projection: Any,
        raw_message: Any,
        conflict: LifecycleConflict,
        *,
        output_actor: str,
        output_kind: str,
        conversation_defaults: Mapping[str, Any] | None,
        request_started_at: float,
        trusted_goal_objective: str | None = None,
    ) -> dict[str, Any] | None:
        inject_mode = str(data.get("injectMode") or "").strip().lower()
        if inject_mode not in {"steer", "queue"}:
            return None
        user_message = (
            dict(input_projection) if isinstance(input_projection, Mapping) else None
        )
        if inject_mode == "steer":
            from lib.agent_inbox import enqueue_after_durable_commit

            steer_text = str(
                (user_message or {}).get("content")
                or (input_projection if isinstance(input_projection, str) else "")
                or (
                    (raw_message or {}).get("text")
                    if isinstance(raw_message, Mapping)
                    else ""
                )
            )
            command_id = str(data.get("commandId") or "")
            attempt_id = str(conflict.turn.get("currentAttemptId") or "")
            committed: dict[str, Any] | None = None
            if attempt_id:
                try:
                    enqueued, committed_value = enqueue_after_durable_commit(
                        conversation_id,
                        steer_text,
                        lambda: commit_user_steer(
                            conversation_id,
                            attempt_id,
                            command_id=command_id,
                            text=steer_text,
                            user_id=user_id,
                        ),
                        priority="next",
                        mode="user-steer",
                        extra={
                            "injectionId": command_id,
                            "blockId": f"injection:user-steer:{command_id}",
                            "_user_msg": user_message
                            or {"role": "user", "content": steer_text},
                            "config": dict(config),
                        },
                    )
                    if enqueued and isinstance(committed_value, Mapping):
                        committed = dict(committed_value)
                except (LifecycleConflict, LifecycleNotFound):
                    committed = None
            if committed is not None:
                return {
                    "steered": True,
                    "injectionId": committed.get("injectionId") or command_id,
                    "blockId": committed.get("blockId"),
                    "conversationId": conversation_id,
                    "conversationRevision": committed["conversationRevision"],
                    "latestTurn": committed["turn"],
                }
            logger.info(
                "Conversation %s steer slot is closing; using durable queue",
                conversation_id[:8],
            )

        from lib.message_queue import (
            KIND_GOAL_CONTINUATION,
            KIND_REAL,
        )

        queue_payload = (
            dict(raw_message)
            if isinstance(raw_message, Mapping)
            else {
                "text": (user_message or {}).get("content")
                or (input_projection if isinstance(input_projection, str) else "")
            }
        )
        if user_message is not None:
            queue_payload["_user_msg"] = user_message
        queue_kind = (
            KIND_GOAL_CONTINUATION
            if trusted_goal_objective
            and bool((user_message or {}).get('_goalContinuation'))
            else KIND_REAL
        )
        queue_id = str(uuid.uuid4())
        queued_result = create_turn_pair(
            conversation_id,
            command_id=str(data.get("commandId") or ""),
            input_projection=input_projection,
            config=_durable_dispatch_config(config, request_started_at),
            lane_id=str(data.get("laneId") or "main"),
            parent_turn_id=data.get("parentTurnId"),
            kind=output_kind,
            output_actor=output_actor,
            run_id=str(data.get("runId") or ""),
            user_id=user_id,
            input_actor=str(data.get("inputActor") or "human"),
            input_kind=str(data.get("inputKind") or "input"),
            conversation_defaults=(
                dict(conversation_defaults) if conversation_defaults else None
            ),
            dispatch_mode=CONVERSATION_EXECUTOR_DISPATCH_MODE,
            input_presentation_id=f"{data['commandId']}:input",
            output_presentation_id=f"{data['commandId']}:output",
            queue_binding={
                "queueId": queue_id,
                "kind": queue_kind,
                "priority": 100,
                "createdAt": int(time.time() * 1000),
                "message": queue_payload,
            },
        )
        queued_result["conversationId"] = conversation_id
        queued_result["latestTurn"] = conflict.turn
        return _public_result(queued_result)


def _answer_guidance_anchor(
    turn: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    settlement = turn.get("settlement")
    if not isinstance(settlement, Mapping):
        return None
    options = settlement.get("resumeOptions")
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, Mapping):
            continue
        if option.get("operation") != "answer_guidance":
            continue
        anchor = option.get("anchor")
        if isinstance(anchor, Mapping) and anchor.get("guidanceId"):
            return anchor
    return None


def _answer_guidance_executor_config(
    projection: Mapping[str, Any],
    human_answer: Any,
) -> dict[str, Any]:
    """Complete the interrupted ask_human round as the resume authority.

    The persisted projection ends in an ``awaiting_human`` round with no
    result; a plain continue would amputate it and make the model re-ask.
    Here the late answer becomes that round's tool result — the same shape
    the live ask_human handler finalizes — so replay reconstructs the
    question/answer exchange and the agent loop continues from the answered
    call.
    """
    if not isinstance(human_answer, Mapping):
        raise ValueError("answer_guidance requires a human answer")
    response = str(human_answer.get("response") or "")
    guidance_id = str(human_answer.get("guidanceId") or "")
    if not response or not guidance_id:
        raise ValueError("answer_guidance requires a human answer")
    raw_rounds = projection.get("toolRounds")
    rounds = list(raw_rounds) if isinstance(raw_rounds, list) else []
    prefix = scan_replayable_tool_round_prefix(rounds)
    position = prefix.blocked_position
    gap_round = (
        rounds[position]
        if position is not None and position < len(rounds) else None
    )
    if (position is None
            or prefix.blocked_reason != "missing_tool_result"
            or not isinstance(gap_round, Mapping)
            or gap_round.get("toolName") != "ask_human"
            or gap_round.get("status") != "awaiting_human"
            or str(gap_round.get("guidanceId") or "") != guidance_id):
        raise ValueError("the interrupted question no longer matches this answer")
    _, retained = checkpoint_retention_positions(rounds, prefix)
    question = str(gap_round.get("guidanceQuestion") or "")
    tool_content = f"Human response: {response}"
    completed = dict(gap_round)
    completed["status"] = "done"
    completed["toolContent"] = tool_content
    completed["results"] = [{
        "toolName": "ask_human",
        "title": question[:2000] or "Human Guidance",
        "snippet": response[:2000],
        "source": "HumanGuidance",
        "fetched": True,
        "fetchedChars": len(tool_content),
        "badge": "answered",
        "guidanceId": guidance_id,
        "question": question,
        "responseType": str(gap_round.get("guidanceType") or "free_text"),
        "userResponse": response,
    }]
    content = projection.get("content") or ""
    config: dict[str, Any] = {
        "resumePrefill": content,
        "contentPrefix": content,
        "checkpointImages": projection.get("images") or [],
        # Retained pre-gap rows plus the now-answered question round. Every
        # entry must pass the replay scan: prepare_resume_state rejects a
        # causal gap anywhere in checkpointToolRounds.
        "checkpointToolRounds": [rounds[p] for p in retained] + [completed],
    }
    thinking_prefix = projection.get("thinking") or ""
    if content and thinking_prefix:
        # Same display-continuity rule as a lossless continue: only a prose
        # lane that survives intact keeps its thinking tail accumulating.
        config["thinkingPrefix"] = thinking_prefix
    return config


_RESUME_FILE_CARRY_OPERATIONS = frozenset({
    "continue", "checkpoint_resume", "answer_guidance"})


def _resume_checkpoint_file_fields(
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Carry the settled projection's file ledger into the resume config.

    The resumed attempt's commit merge starts from
    ``_checkpointModifiedFileList``; without this hand-off the settled card
    only lists files the resume window itself touched and every pre-gap
    edit disappears from the UI even though it is on disk.
    """
    files = projection.get("modifiedFileList")
    if not isinstance(files, list) or not files:
        return {}
    carried = [
        dict(item) if isinstance(item, Mapping) else item
        for item in files
        if isinstance(item, (Mapping, str)) and item
    ]
    if not carried:
        return {}
    count = projection.get("modifiedFiles")
    return {
        "checkpointModifiedFileList": carried,
        "checkpointModifiedFiles": (
            count
            if isinstance(count, int) and not isinstance(count, bool)
            and count >= 0
            else len(carried)
        ),
    }


def _journal_resume_file_fields(
    turn: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Restart-recovery fallback when the settled projection lost the list.

    A process restart settles the orphaned attempt with its last durable
    projection, which usually predates the live file-change stamps, so the
    projection route finds no ``modifiedFileList`` to carry.  The per-root
    modifications journal survives on disk (conv-scoped, timestamped at
    edit time); derive every file this turn touched from journal entries at
    or after the turn's creation.
    """
    try:
        floor = float(turn.get("createdAt") or 0)
    except (TypeError, ValueError, OverflowError):
        return {}
    if floor > 1e12:  # turn timestamps are epoch milliseconds
        floor /= 1000.0
    if floor <= 0:
        return {}
    project_path = str(config.get("projectPath") or "")
    if not project_path:
        return {}
    extra = config.get("projectPaths")
    project_paths = (
        [str(p) for p in extra if p] if isinstance(extra, list) else None)
    from lib.tasks_pkg.commit_round._derive import (
        derive_round_modified_files)
    files, _, _ = derive_round_modified_files(
        {
            # Empty id on purpose: it matches no taskId-stamped row, forcing
            # the conv-scoped timestamp scan that covers every attempt of
            # this turn (pre- and post-restart) in one pass.
            "id": "",
            "convId": str(turn.get("conversationId") or ""),
            "created_at": floor,
        },
        project_path,
        project_paths,
    )
    if not files:
        return {}
    return {
        "checkpointModifiedFileList": files,
        "checkpointModifiedFiles": len(files),
    }

def _resume_executor_config(
    operation: str,
    projection: Mapping[str, Any],
    human_answer: Any = None,
) -> dict[str, Any]:
    """Executor resume authorities derived from the settled projection.

    Lossless continue must replay the turn's completed tool rounds too:
    prefill alone restarts the prose while the model goes blind to every
    tool fact it produced before the interruption.  Retention mirrors the
    checkpoint_resume anchor (pre-gap rows minus discarded provider-attempt
    artifacts); replay inside the executor still uses the causal prefix.
    """
    if operation == "continue":
        prefix = projection.get("content") or ""
        config: dict[str, Any] = {
            "resumePrefill": prefix,
            "contentPrefix": prefix,
            "checkpointImages": projection.get("images") or [],
        }
        tool_rounds = projection.get("toolRounds")
        # Display continuity only: while the prose lane continues seamlessly
        # via prefill, the thinking lane keeps accumulating from the
        # interrupted tail instead of restarting blank. When the prose lane
        # is empty the write boundary already moved the thinking tail into
        # ``rolledBack``, so no seed is wanted here.
        thinking_prefix = projection.get("thinking") or ""
        if prefix and thinking_prefix:
            config["thinkingPrefix"] = thinking_prefix
        if isinstance(tool_rounds, list) and tool_rounds:
            replay_prefix = scan_replayable_tool_round_prefix(tool_rounds)
            _, retained = checkpoint_retention_positions(
                tool_rounds, replay_prefix)
            retained_rounds = [tool_rounds[p] for p in retained]
            if retained_rounds:
                config["checkpointToolRounds"] = retained_rounds
        config.update(_resume_checkpoint_file_fields(projection))
        return config
    if operation == "checkpoint_resume":
        config = {
            "contentPrefix": projection.get("content") or "",
            "checkpointToolRounds": projection.get("toolRounds") or [],
            "checkpointImages": projection.get("images") or [],
        }
        config.update(_resume_checkpoint_file_fields(projection))
        return config
    if operation == "answer_guidance":
        config = _answer_guidance_executor_config(projection, human_answer)
        config.update(_resume_checkpoint_file_fields(projection))
        return config
    return {}


def _public_result(result: Mapping[str, Any]) -> dict[str, Any]:
    public = dict(result)
    public.pop("_needsStart", None)
    return public_value_with_stable_segments(public)


__all__ = [
    "AttemptStartFailure",
    "CommandOutcome",
    "ConversationTurnCommandService",
    "attempt_start_error_envelope",
]

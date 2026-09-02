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
from typing import Any

from lib.error_envelope import make_envelope
from lib.log import get_logger
from lib.plan_contract import plan_execution_document
from lib.turn_lifecycle import (
    LifecycleConflict,
    LifecycleNotFound,
    abort_attempt,
    append_settled_turn,
    bind_task,
    claim_attempt_start,
    create_attempt,
    create_branch_lane,
    create_turn_pair,
    delete_branch_lane,
    delete_turns,
    fail_start,
    get_attempt,
    get_conversation_revision,
    get_turn,
    update_turn_projection,
)
from lib.turn_projection_segments import public_value_with_stable_segments


logger = get_logger(__name__)


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
    ) -> None:
        self._build_user_message = build_user_message
        self._was_aborted_after = was_aborted_after
        self._start_task = start_task
        self._mutate_file_changes = mutate_file_changes

    def create_turn(
        self,
        conversation_id: str,
        user_id: Any,
        body: Mapping[str, Any],
        *,
        request_started_at: float,
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
            output_actor = "planner"
            output_kind = "plan"
        try:
            result = create_turn_pair(
                conversation_id,
                command_id=command_id,
                input_projection=input_projection,
                config=config,
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
                conversation_defaults=conversation_defaults,
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
            )
            if delivered is None:
                raise
            return CommandOutcome(delivered)

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
        input_update = data.get("inputUpdate")
        if isinstance(input_update, Mapping):
            input_update = _without_client_plan_authority(input_update)
        result = create_attempt(
            conversation_id,
            turn_id,
            command_id=command_id,
            operation=operation,
            expected_projection_revision=int(expected),
            config=config,
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
            config=config,
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
        if operation == "continue":
            prefix = projection.get("content") or ""
            config.update({"resumePrefill": prefix, "contentPrefix": prefix})
        elif operation == "checkpoint_resume":
            config.update({
                "contentPrefix": projection.get("content") or "",
                "checkpointToolRounds": projection.get("toolRounds") or [],
            })
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
    ) -> dict[str, Any] | None:
        inject_mode = str(data.get("injectMode") or "").strip().lower()
        if inject_mode not in {"steer", "queue"}:
            return None
        user_message = (
            dict(input_projection) if isinstance(input_projection, Mapping) else None
        )
        if inject_mode == "steer":
            from lib.agent_inbox import _lock as inbox_lock
            from lib.agent_inbox import _tombstones as inbox_tombstones
            from lib.agent_inbox import enqueue as enqueue_inbox

            with inbox_lock:
                drainable = conversation_id not in inbox_tombstones
            if drainable:
                steer_text = str(
                    (user_message or {}).get("content")
                    or (input_projection if isinstance(input_projection, str) else "")
                    or (
                        (raw_message or {}).get("text")
                        if isinstance(raw_message, Mapping)
                        else ""
                    )
                )
                enqueue_inbox(
                    conversation_id,
                    steer_text,
                    priority="next",
                    mode="user-steer",
                    extra={
                        "_user_msg": user_message
                        or {"role": "user", "content": steer_text},
                        "config": dict(config),
                    },
                )
                return {
                    "steered": True,
                    "conversationId": conversation_id,
                    "conversationRevision": get_conversation_revision(
                        conversation_id, user_id=user_id
                    ),
                    "latestTurn": conflict.turn,
                }
            logger.info(
                "Conversation %s steer slot is closing; using durable queue",
                conversation_id[:8],
            )

        from lib.message_queue import enqueue_message

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
        queue_result = enqueue_message(
            conversation_id,
            queue_payload,
            dict(config),
            user_id=int(user_id),
        )
        queue_item_keys = {
            "queueId", "position", "kind", "priority", "timestamp", "text",
            "sourceMessageId", "hasImages", "hasPdfs", "hasRefs", "hasQuotes",
            "isPeerMessage", "fromConv", "isPeerHuman",
        }
        queue_item = {
            key: value for key, value in queue_result.items()
            if key in queue_item_keys
        }
        queue_item.setdefault("queueId", str(queue_result["queueId"]))
        queue_item.setdefault("position", int(queue_result["position"]))
        queue_item.setdefault("kind", str(queue_result.get("kind") or "real"))
        queue_item.setdefault("priority", 100)
        queue_item.setdefault(
            "timestamp",
            int((user_message or {}).get("timestamp") or time.time() * 1000),
        )
        queue_item.setdefault(
            "text",
            str(
                (user_message or {}).get("content")
                or queue_payload.get("text")
                or ""
            ),
        )
        source_message_id = str(
            (user_message or {}).get("_msgId")
            or queue_payload.get("_msgId")
            or ""
        )
        if source_message_id:
            queue_item.setdefault("sourceMessageId", source_message_id)
        return {
            "queued": True,
            "queueId": queue_result["queueId"],
            "position": queue_result["position"],
            "queueItem": queue_item,
            "conversationId": conversation_id,
            "conversationRevision": get_conversation_revision(
                conversation_id, user_id=user_id
            ),
            "latestTurn": conflict.turn,
        }


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

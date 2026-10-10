"""Incremental and final persistence port for Flow-backed chat turns."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from lib.log import get_logger


logger = get_logger(__name__)


class OrchestrationChatTurnPersistence:
    """Persist an adapter-owned live turn buffer through shared chat ports.

    The adapter constructs its message list, so this port is created first and
    bound immediately after adapter construction. That explicit binding
    replaces the former mutable ``_adapter_ref`` closure while preserving the
    live list identity needed for an incremental full-snapshot sync.
    """

    def __init__(
        self,
        task: dict,
        *,
        store_turns: Callable[[dict, list[dict]], None],
        sync_turns: Callable[[dict, list[dict]], int | None],
        retry_commit: Callable | None = None,
    ):
        self._task = task
        self._store_turns = store_turns
        self._sync_turns = sync_turns
        self._retry_commit = retry_commit
        self._messages: list[dict] | None = None
        self._pending_commit: tuple[dict, list[dict]] | None = None

    def bind(self, messages: list[dict]) -> None:
        """Bind the adapter's live message buffer exactly once."""
        if not isinstance(messages, list):
            raise TypeError('chat turn buffer must be a list')
        if self._messages is not None and self._messages is not messages:
            raise RuntimeError('chat turn persistence is already bound')
        self._messages = messages

    def messages(self) -> list[dict]:
        return self._messages if self._messages is not None else []

    def _persist(self, turns: list[dict]) -> None:
        from lib.conversation_sync.attempt_identity import is_conversation_attempt
        from lib.orchestration.graph import FlowEventDeliveryError

        self._store_turns(self._task, turns)
        fence = {key: deepcopy(self._task.get(key)) for key in (
            'convId', '_turnId', '_attemptId', '_userId', '_flowSyncedCount',
            '_flowLiveTurn')}
        durable = is_conversation_attempt(self._task)
        synced_count = int(self._task.get('_flowSyncedCount') or 0) if durable else 0
        # Already acknowledged phases are owned by durable storage, and the
        # sync port sends only the unsynchronized suffix. Retaining/copying
        # their full tool payloads here made a long run quadratic in work and
        # duplicated its entire history while a single new phase was pending.
        # sync_visible_run_turns also revalidates the last phase on final
        # sync. Keep that row fenced even when all phases were acknowledged.
        offset = max(0, min(synced_count, len(turns) - 1))
        pending_turns = turns[offset:]
        replay = self._pending_commit == (fence, pending_turns)
        if not replay and durable and synced_count < len(turns):
            from lib.turn_lifecycle import record_task_event
            # A final provider delta may still be coalesced. Flush the live
            # output through the ordinary projection owner before sealing it.
            # Empty raw metadata requests a projection checkpoint, not a new
            # task-stream event or a fabricated model-round boundary.
            if not record_task_event(self._task, {}):
                raise FlowEventDeliveryError('Flow output checkpoint lost Turn authority')
        # Only a previously checkpointed, byte-for-value identical snapshot may
        # bypass a now-ended child's checkpoint. The sync owner still performs
        # its transactional owner/attempt/verdict/output fences; False from an
        # arbitrary checkpoint is never an acknowledgement.
        self._pending_commit = (
            {key: deepcopy(self._task.get(key)) for key in fence}, deepcopy(pending_turns))
        self._sync_turns(self._task, turns)

        if (is_conversation_attempt(self._task)
                and int(self._task.get('_flowSyncedCount') or 0) != len(turns)):
            raise FlowEventDeliveryError('Flow output commit was not acknowledged')
        self._pending_commit = None

    def _persistence_failed(self, exc: Exception) -> None:
        from lib.conversation_sync.attempt_identity import is_conversation_attempt
        if not is_conversation_attempt(self._task):
            return
        from lib.orchestration.graph import FlowEventDeliveryError
        # Keep the pending phase and its live carrier for retry/recovery. No
        # successor dispatch or successful terminal frame may outrun this ACK.
        abort = self._task.get('abort_event')
        if abort is not None:
            abort.set()
        raise FlowEventDeliveryError(f'Could not commit Flow output: {exc}') from exc

    def _persist_with_transient_retry(self, turns: list[dict]) -> None:
        """Give a transient authority stall a bounded second chance.

        Replaying the identical snapshot is the recovery the
        ``_pending_commit`` fence is designed for: the checkpoint is not
        repeated, the sync owner's transactional fences still apply, and a
        definitive verdict (stale attempt, unacknowledged commit) is not a
        transient classification and raises through unchanged.
        """
        if self._retry_commit is None:
            self._persist(turns)
            return
        self._retry_commit(
            lambda: self._persist(turns),
            abort_event=self._task.get('abort_event'),
            label='flow chat turn commit '
                  f'task={str(self._task.get("id") or "")[:8]}')

    def __call__(self, _message: dict) -> bool:
        """Persist one completed turn snapshot."""
        turns = self.messages()
        if not turns:
            return False
        try:
            self._persist_with_transient_retry(turns)
        except Exception as exc:
            self._persistence_failed(exc)
            logger.warning(
                '[FlowChat] per-turn DB sync failed '
                '(non-fatal) task=%s: %s',
                str(self._task.get('id') or '')[:8], exc,
            )
            return False
        # Goal mode is a frontend surface, not a background swarm: each
        # settled worker/VU turn gets its own translation trigger here
        # instead of waiting for the whole run's terminal event (and
        # surviving that coordinator missing a turn entirely). Swarm
        # sub-agents never pass through this boundary.
        try:
            from lib.translate.terminal import (
                schedule_settled_visible_turn_translations,
            )

            schedule_settled_visible_turn_translations(self._task)
        except Exception as exc:
            logger.debug(
                '[FlowChat] settled-turn translate schedule failed '
                '(non-fatal) task=%s: %s',
                str(self._task.get('id') or '')[:8], exc,
            )
        return True

    def finalize(self) -> bool:
        """Persist the final turn snapshot before the terminal event."""
        turns = self.messages()
        if not turns:
            return False
        synced = True
        try:
            self._persist_with_transient_retry(turns)
        except Exception as exc:
            self._persistence_failed(exc)
            synced = False
            logger.warning(
                '[FlowChat] final DB sync failed (non-fatal) task=%s: %s',
                str(self._task.get('id') or '')[:8], exc,
            )
        return synced


__all__ = ['OrchestrationChatTurnPersistence']

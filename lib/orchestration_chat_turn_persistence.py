"""Incremental and final persistence port for Flow-backed chat turns."""

from __future__ import annotations

from collections.abc import Callable

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
    ):
        self._task = task
        self._store_turns = store_turns
        self._sync_turns = sync_turns
        self._messages: list[dict] | None = None

    def bind(self, messages: list[dict]) -> None:
        """Bind the adapter's live message buffer exactly once."""
        if not isinstance(messages, list):
            raise TypeError('chat turn buffer must be a list')
        if self._messages is not None and self._messages is not messages:
            raise RuntimeError('chat turn persistence is already bound')
        self._messages = messages

    def messages(self) -> list[dict]:
        return self._messages if self._messages is not None else []

    def __call__(self, _message: dict) -> bool:
        """Persist one completed turn snapshot."""
        turns = self.messages()
        if not turns:
            return False
        try:
            self._store_turns(self._task, turns)
            self._sync_turns(self._task, turns)
        except Exception as exc:
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
            self._store_turns(self._task, turns)
            self._sync_turns(self._task, turns)
        except Exception as exc:
            synced = False
            logger.warning(
                '[FlowChat] final DB sync failed (non-fatal) task=%s: %s',
                str(self._task.get('id') or '')[:8], exc,
            )
        return synced


__all__ = ['OrchestrationChatTurnPersistence']

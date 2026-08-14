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
        translate_turn: Callable[[dict, dict, int | None], None],
        translate_final: Callable[[dict, list[dict]], None],
    ):
        self._task = task
        self._store_turns = store_turns
        self._sync_turns = sync_turns
        self._translate_turn = translate_turn
        self._translate_final = translate_final
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

    def __call__(self, message: dict) -> bool:
        """Persist one completed turn and trigger pipelined translation."""
        turns = self.messages()
        if not turns:
            return False
        try:
            self._store_turns(self._task, turns)
            message_index = self._sync_turns(self._task, turns)
            self._translate_turn(self._task, message, message_index)
            return True
        except Exception as exc:
            logger.warning(
                '[FlowChat] per-turn DB sync/translate failed '
                '(non-fatal) task=%s: %s',
                str(self._task.get('id') or '')[:8], exc,
            )
            return False

    def finalize(self) -> bool:
        """Run the final DB snapshot and auto-translation safety net."""
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
        try:
            self._translate_final(self._task, turns)
        except Exception as exc:
            logger.warning(
                '[FlowChat] safety-net auto-translate failed '
                '(non-fatal) task=%s: %s',
                str(self._task.get('id') or '')[:8], exc,
            )
        return synced


__all__ = ['OrchestrationChatTurnPersistence']

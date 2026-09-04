"""Bounded process-local session state for the Feishu integration.

Durable conversation turns live in the owner-scoped storage authority. This
module owns only reconstructible prompt context and UI preferences. Sessions
are LRU-bounded from the launch-time external-client budget; an event pins its
session while executing so eviction can never split one in-flight message
across two state objects.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import threading
from typing import Iterator
import uuid

from lib.weak_lock_pool import WeakLockPool
from runtime_guards import resolve_resource_budget


MAX_FEISHU_USER_ID_CHARS = 256
MAX_FEISHU_HISTORY_MESSAGES = 40
MAX_FEISHU_HISTORY_MESSAGE_CHARS = 16_000
MAX_FEISHU_HISTORY_TOTAL_CHARS = 128_000
MAX_FEISHU_MODEL_CHARS = 256
MAX_FEISHU_PROJECT_PATH_CHARS = 4096


def _default_session_capacity() -> int:
    return resolve_resource_budget(
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY',
        minimum=16,
        maximum=2048,
    )


class FeishuSessionCapacityError(RuntimeError):
    """All bounded session slots are pinned by active requests."""


def _require_user_id(user_id: object) -> str:
    if not isinstance(user_id, str):
        raise ValueError('Feishu user id must be a string')
    normalized = user_id.strip()
    if not normalized:
        raise ValueError('Feishu user id is required')
    if len(normalized) > MAX_FEISHU_USER_ID_CHARS:
        raise ValueError(
            f'Feishu user id exceeds {MAX_FEISHU_USER_ID_CHARS} characters')
    return normalized


@dataclass
class FeishuUserSession:
    history: list[dict[str, str]] = field(default_factory=list)
    model: str | None = None
    mode: str = 'chat'
    project: str | None = None
    conversation_id: str | None = None
    pending: object | None = None


class FeishuUserSessionStore:
    """LRU session store that never evicts a pinned in-flight user."""

    def __init__(
        self,
        capacity: int,
        *,
        history_messages: int = MAX_FEISHU_HISTORY_MESSAGES,
        history_message_chars: int = MAX_FEISHU_HISTORY_MESSAGE_CHARS,
        history_total_chars: int = MAX_FEISHU_HISTORY_TOTAL_CHARS,
    ) -> None:
        if isinstance(capacity, bool) or int(capacity) != capacity \
                or int(capacity) <= 0:
            raise ValueError('Feishu session capacity must be positive')
        self.capacity = int(capacity)
        self.history_messages = max(1, int(history_messages))
        self.history_message_chars = max(1, int(history_message_chars))
        self.history_total_chars = max(1, int(history_total_chars))
        self._sessions: OrderedDict[str, FeishuUserSession] = OrderedDict()
        self._pin_counts: dict[str, int] = {}
        self._lock = threading.RLock()

    def _session_locked(self, user_id: str) -> FeishuUserSession:
        session = self._sessions.get(user_id)
        if session is not None:
            self._sessions.move_to_end(user_id)
            return session
        if len(self._sessions) >= self.capacity:
            evictable = next((
                candidate
                for candidate in self._sessions
                if self._pin_counts.get(candidate, 0) == 0
            ), None)
            if evictable is None:
                raise FeishuSessionCapacityError(
                    'all Feishu session slots are active')
            self._sessions.pop(evictable, None)
        session = FeishuUserSession()
        self._sessions[user_id] = session
        return session

    @contextmanager
    def pin(self, user_id: object) -> Iterator[None]:
        """Keep one user's session resident for the surrounding event."""
        normalized = _require_user_id(user_id)
        with self._lock:
            self._session_locked(normalized)
            self._pin_counts[normalized] = (
                self._pin_counts.get(normalized, 0) + 1)
        try:
            yield
        finally:
            with self._lock:
                remaining = self._pin_counts.get(normalized, 0) - 1
                if remaining > 0:
                    self._pin_counts[normalized] = remaining
                else:
                    self._pin_counts.pop(normalized, None)

    def history(self, user_id: object) -> list[dict[str, str]]:
        normalized = _require_user_id(user_id)
        with self._lock:
            session = self._session_locked(normalized)
            return [dict(message) for message in session.history]

    def append_message(
        self,
        user_id: object,
        role: object,
        content: object,
    ) -> None:
        normalized = _require_user_id(user_id)
        if role not in ('user', 'assistant'):
            raise ValueError('Feishu history role must be user or assistant')
        if not isinstance(content, str):
            raise ValueError('Feishu history content must be a string')
        bounded_content = content[:self.history_message_chars]
        with self._lock:
            history = self._session_locked(normalized).history
            history.append({'role': role, 'content': bounded_content})
            del history[:-self.history_messages]
            total_chars = sum(len(message['content']) for message in history)
            while history and total_chars > self.history_total_chars:
                total_chars -= len(history.pop(0)['content'])

    def clear_history(self, user_id: object) -> None:
        normalized = _require_user_id(user_id)
        with self._lock:
            self._session_locked(normalized).history.clear()

    def new_conversation_id(self, user_id: object) -> str:
        normalized = _require_user_id(user_id)
        conversation_id = str(uuid.uuid4())
        with self._lock:
            self._session_locked(normalized).conversation_id = conversation_id
        return conversation_id

    def conversation_id(self, user_id: object) -> str:
        normalized = _require_user_id(user_id)
        with self._lock:
            session = self._session_locked(normalized)
            if session.conversation_id is None:
                session.conversation_id = str(uuid.uuid4())
            return session.conversation_id

    def model(self, user_id: object, default: str) -> str:
        normalized = _require_user_id(user_id)
        with self._lock:
            return self._session_locked(normalized).model or default

    def set_model(self, user_id: object, model: object) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError('Feishu model must be a non-empty string')
        if len(model) > MAX_FEISHU_MODEL_CHARS:
            raise ValueError(
                f'Feishu model exceeds {MAX_FEISHU_MODEL_CHARS} characters')
        normalized = _require_user_id(user_id)
        with self._lock:
            self._session_locked(normalized).model = model

    def mode(self, user_id: object) -> str:
        normalized = _require_user_id(user_id)
        with self._lock:
            return self._session_locked(normalized).mode

    def set_mode(self, user_id: object, mode: object) -> None:
        if mode not in ('chat', 'tool'):
            raise ValueError('Feishu mode must be chat or tool')
        normalized = _require_user_id(user_id)
        with self._lock:
            self._session_locked(normalized).mode = mode

    def project(self, user_id: object, default: str) -> str:
        normalized = _require_user_id(user_id)
        with self._lock:
            return self._session_locked(normalized).project or default

    def set_project(self, user_id: object, path: object) -> None:
        if not isinstance(path, str) or not path:
            raise ValueError('Feishu project path must be a non-empty string')
        if len(path) > MAX_FEISHU_PROJECT_PATH_CHARS:
            raise ValueError(
                'Feishu project path exceeds '
                f'{MAX_FEISHU_PROJECT_PATH_CHARS} characters')
        normalized = _require_user_id(user_id)
        with self._lock:
            self._session_locked(normalized).project = path

    def pending(self, user_id: object) -> object | None:
        normalized = _require_user_id(user_id)
        with self._lock:
            return self._session_locked(normalized).pending

    def set_pending(self, user_id: object, value: object) -> None:
        try:
            encoded = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError('Feishu pending state must be JSON-compatible') \
                from exc
        if len(encoded) > MAX_FEISHU_HISTORY_MESSAGE_CHARS:
            raise ValueError(
                'Feishu pending state exceeds '
                f'{MAX_FEISHU_HISTORY_MESSAGE_CHARS} characters')
        normalized = _require_user_id(user_id)
        with self._lock:
            self._session_locked(normalized).pending = value

    def clear_pending(self, user_id: object) -> None:
        normalized = _require_user_id(user_id)
        with self._lock:
            self._session_locked(normalized).pending = None

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


feishu_user_sessions = FeishuUserSessionStore(_default_session_capacity())
_processing_locks = WeakLockPool(threading.Lock)


def get_user_processing_lock(user_id: object) -> threading.Lock:
    return _processing_locks.lock_for(_require_user_id(user_id))


__all__ = [
    'FeishuSessionCapacityError',
    'FeishuUserSession',
    'FeishuUserSessionStore',
    'MAX_FEISHU_HISTORY_MESSAGE_CHARS',
    'MAX_FEISHU_HISTORY_MESSAGES',
    'MAX_FEISHU_HISTORY_TOTAL_CHARS',
    'MAX_FEISHU_USER_ID_CHARS',
    'feishu_user_sessions',
    'get_user_processing_lock',
]

"""Invocation inputs and explicit benchmark execution ports; stdlib only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class WorkloadInputs:
    scale: dict[str, int]
    user: int
    namespace: str
    conversation_timestamp: int
    search_timestamp: int

    def conversation_id(self, prefix: str, index: int) -> str:
        return f'{self.namespace}_{prefix}_conv_{index:06d}'

    @staticmethod
    def record_key(index: int) -> str:
        return f'k/{index:06d}'


@dataclass(frozen=True)
class WorkloadPorts:
    record_value: Callable[[int], dict]
    large_turn_content: Callable[[int, int, str], str]
    execute: Callable[..., object]
    summarize: Callable[[list[float]], dict]
    error: type[Exception]
    unsupported_error: type[Exception]
    raw_sqlite_type: type

"""Blocking request ports used by orchestration human-gate execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class ApprovalRequester(Protocol):
    def __call__(
        self,
        request_id: str,
        timeout: int,
        owner_user_id: int,
        task: Any,
        on_registered: Callable[[], None],
    ) -> bool: ...


class GuidanceRequester(Protocol):
    def __call__(
        self,
        request_id: str,
        task: Any,
        owner_user_id: int,
        on_registered: Callable[[], None],
    ) -> str | None: ...


def _request_approval(
    request_id: str,
    timeout: int,
    owner_user_id: int,
    task: Any,
    on_registered: Callable[[], None],
) -> bool:
    from lib.tasks_pkg.approval import request_write_approval
    return bool(request_write_approval(
        request_id,
        timeout=timeout,
        task=task,
        owner_user_id=owner_user_id,
        on_registered=on_registered,
    ))


def _request_guidance(
    request_id: str,
    task: Any,
    owner_user_id: int,
    on_registered: Callable[[], None],
) -> str | None:
    from lib.tasks_pkg.human_guidance import request_human_guidance
    return request_human_guidance(
        request_id,
        task=task,
        owner_user_id=owner_user_id,
        on_registered=on_registered,
    )


@dataclass(frozen=True)
class HumanGateRequestPorts:
    """Replaceable blocking primitives below the graph runtime."""

    request_approval: ApprovalRequester = _request_approval
    request_guidance: GuidanceRequester = _request_guidance


__all__ = [
    'ApprovalRequester', 'GuidanceRequester', 'HumanGateRequestPorts',
]

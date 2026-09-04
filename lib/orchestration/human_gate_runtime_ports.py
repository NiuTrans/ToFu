"""Blocking request ports used by orchestration human-gate execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ApprovalRequester(Protocol):
    def __call__(
        self,
        request_id: str,
        timeout: int,
        owner_user_id: int,
    ) -> bool: ...


class GuidanceRequester(Protocol):
    def __call__(
        self,
        request_id: str,
        task: Any,
        owner_user_id: int,
    ) -> str | None: ...


def _request_approval(
    request_id: str,
    timeout: int,
    owner_user_id: int,
) -> bool:
    from lib.tasks_pkg.approval import request_write_approval
    return bool(request_write_approval(
        request_id,
        timeout=timeout,
        owner_user_id=owner_user_id,
    ))


def _request_guidance(
    request_id: str,
    task: Any,
    owner_user_id: int,
) -> str | None:
    from lib.tasks_pkg.human_guidance import request_human_guidance
    return request_human_guidance(
        request_id,
        task=task,
        owner_user_id=owner_user_id,
    )


@dataclass(frozen=True)
class HumanGateRequestPorts:
    """Replaceable blocking primitives below the graph runtime."""

    request_approval: ApprovalRequester = _request_approval
    request_guidance: GuidanceRequester = _request_guidance


__all__ = [
    'ApprovalRequester', 'GuidanceRequester', 'HumanGateRequestPorts',
]

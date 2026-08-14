"""Immutable application result for orchestration human-gate execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HumanGateRuntimeResult:
    context: str
    aborted: bool = False


__all__ = ['HumanGateRuntimeResult']

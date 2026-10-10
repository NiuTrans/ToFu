"""Immutable application results for repository-free authoring operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthoringPlanResult:
    """Execution-free plan plus the inspection that produced it."""

    plan: dict
    inspection: dict


@dataclass(frozen=True)
class AuthoringBuiltinResult:
    """One built-in definition and its canonical inspection snapshot."""

    definition: dict | None
    inspection: dict | None


__all__ = ['AuthoringPlanResult', 'AuthoringBuiltinResult']

"""Shared failure policy for durable-run database repository calls."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from lib.log import get_logger
from lib.orchestration.run_store_port import OrchestrationRunStoreError


logger = get_logger(__name__)
_ResultT = TypeVar('_ResultT')


def run_store_attempt(
    context: str,
    callback: Callable[[], _ResultT],
    *,
    fallback: _ResultT,
) -> _ResultT:
    """Run a best-effort write while retaining its established fallback."""
    try:
        return callback()
    except Exception as error:
        logger.warning('[OrchRuns] %s failed: %s', context, error)
        return fallback


def run_store_require(
    context: str,
    message: str,
    callback: Callable[[], _ResultT],
) -> _ResultT:
    """Run a required read and translate only its database-call failure."""
    try:
        return callback()
    except OrchestrationRunStoreError:
        raise
    except Exception as error:
        logger.warning('[OrchRuns] %s failed: %s', context, error)
        raise OrchestrationRunStoreError(message) from error


__all__ = ['run_store_attempt', 'run_store_require']

"""Typed paper-build failures shared by source, compiler and delivery adapters."""

from __future__ import annotations


class BuildError(Exception):
    """A bounded actionable failure; code is stable, message is diagnostic."""

    def __init__(self, code: str, message: str):
        super().__init__(message[:2000])
        self.code = code


def check_cancelled(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise BuildError('cancelled', 'Paper build cancelled')

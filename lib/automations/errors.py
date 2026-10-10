"""Typed automation failures shared by authoring, execution, and HTTP adapters."""

from __future__ import annotations


class AutomationError(ValueError):
    """A rejected definition or transition with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, path: str = ""):
        super().__init__(message)
        self.code = code
        self.path = path


class AutomationConflict(AutomationError):
    """Another writer owns the version or fence; callers must refresh authority."""

    def __init__(self, message: str):
        super().__init__("automation_conflict", message)

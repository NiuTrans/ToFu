"""Injectable outbound dependencies for the orchestration core.

Only dependencies that must be replaceable across multiple orchestrator
modules belong here. Tests patch this concrete module, never a package facade.
The retry driver uses a call-time import solely to close the documented
``_run`` → ``_finalize`` → retry cycle.
"""

from __future__ import annotations

from typing import Any

from lib.llm import build_body
from lib.protocols import BodyBuilder

build_request_body: BodyBuilder = build_body


def rerun_task(task: dict[str, Any]) -> None:
    """Start one whole-turn retry through the canonical run implementation."""
    from lib.tasks_pkg.orchestrator._run import run_task

    run_task(task)


__all__ = ('build_request_body', 'rerun_task')

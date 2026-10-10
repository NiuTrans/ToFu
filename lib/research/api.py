"""Public application API for automated research production."""

from lib.research.engine import (
    produce_research,
    research_root,
    resume_interrupted_research,
    run_research_task,
)
from lib.research.recipe import (
    build_research_from_direction,
    research_recipe_stages,
)

def get_research_task(task_id: str, *, user_id: int) -> dict | None:
    """Read the owner's existing production registry; never adopt or replay."""
    from lib.research.runtime import _research_runtime
    return _research_runtime.get_owned(task_id, user_id=user_id)


def abort_research_task(task_id: str, *, user_id: int) -> bool:
    """Signal the existing owned production worker's cancellation authority."""
    from lib.research.runtime import _research_runtime
    return _research_runtime.abort_owned(task_id, user_id=user_id)


__all__ = (
    'abort_research_task',
    'get_research_task',
    'build_research_from_direction',
    'produce_research',
    'research_recipe_stages',
    'research_root',
    'resume_interrupted_research',
    'run_research_task',
)

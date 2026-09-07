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

__all__ = (
    'build_research_from_direction',
    'produce_research',
    'research_recipe_stages',
    'research_root',
    'resume_interrupted_research',
    'run_research_task',
)

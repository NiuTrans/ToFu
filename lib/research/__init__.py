"""lib.research — Automated research capability (auto-research recipe R4).

The FOURTH "one sentence → finished product" recipe on the production
substrate (docs/AUTO_RESEARCH_SYSTEM_DESIGN.md). It wires the R1–R3 primitives
into the stage graph:

  ``recipe.py``   harvest → survey → ideate  (checkpointed, crash-resume)
  ``engine.py``   headless worker + produce_research entry point
  ``runtime.py``  thin ProductionRuntime instance (NO bespoke runtime — the
                  owner directive was "don't build a 4th duplicate runtime")

Poll/abort ride the generic /api/v1/tasks/* surface (runtime discovered by
kind='research'); no bespoke routes.
"""

from __future__ import annotations

__all__ = ['produce_research', 'run_research_task', 'build_research_from_direction',
           'resume_interrupted_research', 'research_root']


def __getattr__(name):  # PEP 562 lazy facade — avoids import cost at boot
    if name in ('produce_research', 'run_research_task',
                'resume_interrupted_research', 'research_root'):
        from lib.research import engine
        return getattr(engine, name)
    if name in ('build_research_from_direction', 'research_recipe_stages'):
        from lib.research import recipe
        return getattr(recipe, name)
    raise AttributeError(name)

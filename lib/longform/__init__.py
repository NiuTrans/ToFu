"""lib.longform — Long-form research report capability (Production Substrate P7).

The THIRD "one sentence → finished product" recipe, whose purpose is to TEST
the substrate abstraction rather than to be a refactor of it (owner ruling
2026-07-26: "third recipe first, then extract").

Deliberately a different SHAPE from the video capability so the test is real:
a TEXT deliverable (markdown artifact) instead of a binary render, no TTS, no
per-scene fan-out, and a **data-dependent stage list** — one stage per outline
section, which the static video stage list never exercised.

  ``recipe.py``   research → outline → sections(×N) → assemble
  ``engine.py``   headless worker; publishes the report as an artifact
  ``runtime.py``  TaskRuntime + dedup index
"""

from __future__ import annotations

__all__ = ['run_longform_task', 'start_report_job', 'build_report_from_topic']


def __getattr__(name):  # PEP 562 lazy facade — avoids import cost at boot
    if name in ('run_longform_task', 'start_report_job',
                'resume_interrupted_reports', 'longform_root'):
        from lib.longform import engine
        return getattr(engine, name)
    if name == 'build_report_from_topic':
        from lib.longform.recipe import build_report_from_topic
        return build_report_from_topic
    raise AttributeError(name)

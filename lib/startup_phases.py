"""Canonical startup-phase catalog shared by the server and the frontend.

The production lifespan logs ``[startup phase N/9]`` markers with HUMAN
labels (``lib.production_lifecycle``), while the in-app restart progress bar
(``frontend/src/runtime/sections/update.js``) needs STABLE machine keys that
never drift when a label is reworded or translated.  This module is the single
source of truth that maps one phase ordinal to both: Python consumes
``label``; the restart profile persists ``key`` so the frontend can translate
its own ``update.phase.<key>`` label and budget per phase.

Do not reorder/rename casually: the phase ordinals and keys are a persisted,
cross-process contract (the previous boot's measured profile feeds the next
restart's progress estimate).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StartupPhase:
    index: int
    key: str
    label: str


# Ordered exactly like ``register_production_lifecycle._startup``.  The
# backend persists measured durations keyed by ``key``; the frontend maps the
# same ``key`` onto its i18n label (``update.phase.<key>``) and time budget.
STARTUP_PHASES: tuple[StartupPhase, ...] = (
    StartupPhase(1, 'frontend_assets', 'Frontend assets'),
    StartupPhase(2, 'storage_boundary', 'Exclusive storage boundary'),
    StartupPhase(3, 'storage_sidecar', 'Storage sidecar'),
    StartupPhase(4, 'storage_recovery', 'Storage recovery'),
    StartupPhase(5, 'model_routing', 'Model-routing v2 authority'),
    StartupPhase(6, 'critical_imports', 'Critical imports'),
    StartupPhase(7, 'background_workers', 'Background workers'),
    StartupPhase(8, 'optional_services', 'Optional services'),
    StartupPhase(9, 'readiness', 'Readiness announcement'),
)

REQUIRED_PHASE_COUNT = len(STARTUP_PHASES)

_PHASE_BY_INDEX = {phase.index: phase for phase in STARTUP_PHASES}


def phase_key(index: int) -> str:
    return _PHASE_BY_INDEX[index].key


def phase_label(index: int) -> str:
    return _PHASE_BY_INDEX[index].label


__all__ = [
    'REQUIRED_PHASE_COUNT',
    'STARTUP_PHASES',
    'StartupPhase',
    'phase_key',
    'phase_label',
]

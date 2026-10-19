"""Canonical startup-phase catalog for boot status, logs and measured profiles.

The production lifespan and BootRunner project stable ordinals/keys to health
and boot status; the browser translates those keys instead of estimating the
current phase from elapsed time. Measured durations remain operations evidence.
Do not reorder/rename casually: profiles persist the phase ordinals and keys
across process replacements.
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
# same ``key`` onto its i18n label (``update.phase.<key>``).
STARTUP_PHASES: tuple[StartupPhase, ...] = (
    StartupPhase(1, 'frontend_assets', 'Frontend assets'),
    StartupPhase(2, 'storage_boundary', 'Exclusive storage boundary'),
    StartupPhase(3, 'storage_authority_sql', 'Storage sidecar'),
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

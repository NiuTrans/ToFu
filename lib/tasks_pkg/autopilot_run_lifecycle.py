"""Compatibility aliases for the canonical Goal close-out owner.

Persisted wire names remain readable. There is no separate Autopilot lifecycle
implementation; production and historical imports use identical functions.
"""
from lib.goal_runs.closeout import (
    store_run_record as _store_run_record,
    emit_run_concluded as _emit_run_concluded,
    conclude_run,
    emit_run_concluded_event as _emit_run_concluded_event,
)

__all__ = ['_store_run_record', '_emit_run_concluded', 'conclude_run',
           '_emit_run_concluded_event']

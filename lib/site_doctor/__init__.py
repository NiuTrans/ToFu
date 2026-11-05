"""Selector-drift repair through the shared agent loop.

Public entry points: on_site_drift (search listener), run_doctor (one repair),
and DOCTOR_TOOLS (model schema). Domain ownership and verification live in
catalog, verification, runner and trigger; see AGENTS.md.
"""

from .catalog import DOCTOR_TOOLS
from .runner import run_doctor
from .trigger import on_site_drift

__all__ = ['on_site_drift', 'run_doctor', 'DOCTOR_TOOLS']

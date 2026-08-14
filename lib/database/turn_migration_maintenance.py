"""Reviewed write-authority boundary for the quiesced Turn v2 migration."""

from __future__ import annotations

from typing import Any

from lib.database import init_db
from lib.database.sqlite_owner import maintenance_write_authority


def run_turn_v2_migration(*, apply: bool, user_id: Any = 1) -> dict[str, Any]:
    """Plan, and optionally apply, the Turn v2 cutover under DB authority.

    The CLI deliberately cannot grant itself canonical-write authority.  This
    reviewed data-layer operation owns that capability and keeps dry-run and
    apply planning inside the same initialized maintenance scope.
    """
    from lib.turn_migration import apply_plans, plan_database

    with maintenance_write_authority('turn-attempt-v2-migration'):
        init_db()
        plans = plan_database(user_id=user_id)
        report = {
            'mode': 'apply' if apply else 'dry-run',
            'conversations': len(plans),
            'turns': sum(len(plan.turns) for plan in plans),
            'attempts': sum(len(plan.attempts) for plan in plans),
            'items': [plan.report() for plan in plans],
        }
        if apply:
            report['result'] = apply_plans(plans)
        return report


__all__ = ['run_turn_v2_migration']

"""Pure FlowExecutor-event projections for the endpoint chat wire."""

from __future__ import annotations

from lib.agent_core.events import Phase, build_phase
from lib.orchestration._role_axes import VERIFIER_ROLES


def endpoint_emits_for_role(role: str) -> str:
    """Mirror the canonical role message axis for rolling engine events."""
    return 'user' if role in VERIFIER_ROLES else 'assistant'


def project_endpoint_turn_metadata(
    role: str,
    emits: str,
    *,
    projection: str,
    vu_msg_id: str = '',
    vu_run_id: str = '',
) -> dict:
    """Project metadata shared by live and persisted turn representations."""
    meta = {
        'flowProjection': projection,
        'turnRole': role or '',
        'emits': emits or endpoint_emits_for_role(role),
    }
    if role == 'virtual_user':
        meta['vuMsgId'] = vu_msg_id
        if vu_run_id:
            meta['autopilotRunId'] = vu_run_id
    return meta


def project_endpoint_next_phase(
    text: str,
    *,
    pending_replan: bool = False,
) -> str:
    """Project the coarse next-placeholder phase from verifier output."""
    if pending_replan:
        return 'planner'
    lowered = (text or '').lower()
    if '[vu: task_done]' in lowered:
        return 'stop'
    if '[verdict: stop]' in lowered or 'verdict: stop' in lowered:
        return 'stop'
    if 'continue_planner' in lowered:
        return 'planner'
    return 'worker'


def project_endpoint_phase_event(event: dict) -> dict:
    """Map an engine ``step_phase`` frame onto the registered wire event."""
    event = event or {}
    projected = build_phase(
        event.get('phase') or Phase.WORKING,
        detail=event.get('detail') or '',
    )
    if event.get('attempt'):
        projected['attempt'] = event.get('attempt')
    if event.get('status_code'):
        projected['statusCode'] = event.get('status_code')
    if event.get('detailKey'):
        projected['detailKey'] = event.get('detailKey')
    if event.get('detailArgs'):
        projected['detailArgs'] = event.get('detailArgs')
    return projected


__all__ = [
    'endpoint_emits_for_role',
    'project_endpoint_next_phase',
    'project_endpoint_phase_event',
    'project_endpoint_turn_metadata',
]

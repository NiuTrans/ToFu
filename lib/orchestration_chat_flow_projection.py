"""Pure FlowExecutor-event projections for the chat-flow wire.

These projections serve every Flow-backed chat run, including goal-mode
autopilot and custom Studio flows.
"""

from __future__ import annotations

from collections.abc import Mapping

from lib.agent_core.events import Phase, build_phase
from lib.orchestration._role_axes import VERIFIER_ROLES


def flow_emits_for_role(role: str) -> str:
    """Mirror the canonical role message axis for rolling engine events."""
    return 'user' if role in VERIFIER_ROLES else 'assistant'


def project_flow_turn_metadata(
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
        'emits': emits or flow_emits_for_role(role),
    }
    if role == 'virtual_user':
        meta['vuMsgId'] = vu_msg_id
        if vu_run_id:
            meta['autopilotRunId'] = vu_run_id
    return meta


def project_flow_next_phase(
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


def project_flow_phase_event(event: dict) -> dict:
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
    if event.get('model'):
        projected['model'] = event.get('model')
    if isinstance(event.get('modelRoute'), dict):
        projected['modelRoute'] = dict(event['modelRoute'])
    return projected


def project_flow_tool_rounds(tool_log) -> list[dict]:
    """Project one node's bounded ``tool_log`` into chat ``toolRounds`` rows.

    Flow role nodes execute through the swarm SubAgent substrate, which keeps
    one display row per dispatched call (args brief, result preview, error).
    The settled chat timeline renders the standard ``toolRounds`` vocabulary,
    so each row becomes one settled round:

    * a finished call → ``done``, preview as authoritative ``toolContent``;
    * a call finished with an error → ``error``, error text as the reason;
    * a row the run never finished (aborted mid-call) → ``aborted`` with no
      result body — the same static "interrupted" affordance a user Stop
      leaves on a normal chat round.

    Display records only: the node's model context already lives in its own
    message history, and the producer-side timeline budget bounds each row.
    """
    rounds: list[dict] = []
    for row in tool_log or ():
        if not isinstance(row, Mapping):
            continue
        tool = str(row.get('tool') or '')
        if not tool:
            continue
        rn = len(rounds) + 1
        persisted_call_id = str(row.get('tool_call_id') or '')
        agent_round = row.get('round')
        display_round = (
            agent_round
            if isinstance(agent_round, int) and agent_round > 0 else rn)
        entry: dict = {
            'roundNum': display_round,
            # New rows reuse the exact occurrence identity emitted live. The
            # numbered id is retained only for old persisted tool_log rows.
            'toolCallId': persisted_call_id or f'flow-tool-{rn}',
            'toolName': tool,
            'query': str(row.get('args_brief') or '') or tool,
        }
        if isinstance(agent_round, int) and agent_round > 0:
            entry['llmRound'] = agent_round
        timestamp = row.get('timestamp')
        if isinstance(timestamp, (int, float)) and timestamp > 0:
            entry['tStart'] = int(timestamp * 1000)
        error = str(row.get('error') or '')
        preview = str(row.get('preview') or '')
        persisted_status = str(row.get('status') or '')
        finished = (persisted_status in {'done', 'failed', 'error'}
                    or 'preview_full_chars' in row
                    or 'error_full_chars' in row)
        if error:
            entry['status'] = 'error'
            entry['toolContent'] = error
        elif not finished:
            entry['status'] = 'aborted'
        else:
            entry['status'] = 'done'
            full_chars = row.get('preview_full_chars')
            fetched_chars = (
                int(full_chars)
                if isinstance(full_chars, (int, float)) and full_chars > 0
                else len(preview))
            if preview:
                entry['toolContent'] = preview
                entry['results'] = [{
                    'toolName': tool,
                    'title': tool,
                    'snippet': preview[:120].replace('\n', ' '),
                    'source': 'Flow',
                    'fetched': True,
                    'fetchedChars': fetched_chars,
                }]
            elif fetched_chars:
                # Old timeline rows keep only the size marker after preview
                # compaction: a bare line with the honest chars badge.
                entry['results'] = [{
                    'toolName': tool,
                    'title': tool,
                    'snippet': '',
                    'source': 'Flow',
                    'fetched': False,
                    'fetchedChars': fetched_chars,
                }]
            else:
                entry['results'] = []
        # Structured display payload harvested at dispatch time (todo_write
        # checklist state — see swarm.agent.flow_structured_result_meta). The
        # chat renderer's rich cards key off these meta fields; without them
        # a flow todo_write degrades to the generic English receipt line even
        # though the data existed when the tool ran.
        structured = row.get('result_meta')
        if isinstance(structured, Mapping) and structured:
            metas = entry.get('results')
            if not metas:
                metas = [{
                    'toolName': tool,
                    'title': tool,
                    'snippet': '',
                    'source': 'Flow',
                    'fetched': False,
                    'fetchedChars': 0,
                }]
                entry['results'] = metas
            metas[0].update(structured)
        rounds.append(entry)
    return rounds

__all__ = [
    'flow_emits_for_role',
    'project_flow_next_phase',
    'project_flow_phase_event',
    'project_flow_tool_rounds',
    'project_flow_turn_metadata',
]

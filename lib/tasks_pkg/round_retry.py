"""Retire one failed provider attempt's provisional text before retry.

Entry reset_round_to_base preserves prior accepted text, records bounded
exact discarded snapshots for the existing convergence guards, and emits the
canonical content epoch. Callers own retry admission and paid usage; this
module borrows manager.reset_task_text and the existing event publication port.
"""
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import append_event, reset_task_text


def reset_round_to_base(task, round_num):
    """Reset the round's partial text/thinking to the round base stamped by
    ``stream_llm_response``, so a re-streamed retry never stacks on the
    poisoned attempt's tail. The discarded snapshot is recorded in the
    FloorRetry residue list so the shrink-convergent checkpoint/settle
    guards recognise it as our own discard (exact byte-match) and allow the
    overwrite. Provider refusals and both missing-payload/incomplete-tool
    retries use this exact rollback; earlier accepted content and its epoch remain owned
    by the shared task text state.
    """
    with task['content_lock']:
        _discarded_c = task['content']
        _discarded_t = task['thinking']
        _bc = task.get('_round_base_content')
        _bt = task.get('_round_base_thinking')
    _new_c = _discarded_c if _bc is None else _bc
    _new_t = _discarded_t if _bt is None else _bt
    _shrunk = (_new_c != _discarded_c or _new_t != _discarded_t)
    if _shrunk:
        _residue = task.setdefault('_floor_retry_residue', [])
        if len(_residue) < 8:
            _residue.append({'content': _discarded_c,
                             'thinking': _discarded_t})
        content_epoch = reset_task_text(
            task, content=_new_c, thinking=_new_t)
    else:
        content_epoch = int(task.get('_contentEpoch') or 0)
    append_event(task, build_event(
        EventType.DELTA_RESET, roundNum=round_num, discard=True,
        contentEpoch=content_epoch))

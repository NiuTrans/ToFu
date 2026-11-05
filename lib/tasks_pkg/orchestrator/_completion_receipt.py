"""Terminal event fields and continuation metadata, without lifecycle authority.

Entry build_done_event projects one settled task and mirrors checkpoint totals
onto its existing task fields. It performs no scan, provider request, pricing,
publication or persistence. Call after final outcome/accounting decisions.
"""
from __future__ import annotations

from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.cost import accumulate_usage_into
from lib.llm.stream_result import ProviderStreamResult


def build_done_event(
    task: dict[str, Any],
    *,
    cfg: dict[str, Any],
    api_rounds: list,
    last_finish_reason: Any,
    last_stream_result: ProviderStreamResult | None,
    accumulated_usage: dict,
    last_usage: Any,
    model: str,
    thinking_depth: str | None,
) -> dict[str, Any]:
    """Project the authoritative terminal task fields onto one done event."""
    done_event = build_event(EventType.DONE)
    done_event['taskId'] = task['id']
    if last_finish_reason:
        done_event['finishReason'] = last_finish_reason
    if isinstance(last_stream_result, ProviderStreamResult):
        done_event['streamState'] = last_stream_result.state.value
    final_usage = accumulated_usage if accumulated_usage else last_usage
    if final_usage:
        done_event['usage'] = final_usage
    if task.get('preset'):
        done_event['preset'] = task['preset']
    done_event['model'] = model
    task['model'] = model
    if thinking_depth:
        done_event['thinkingDepth'] = thinking_depth
        task['thinkingDepth'] = thinking_depth
    for field in ('error', 'toolSummary'):
        if task.get(field):
            done_event[field] = task[field]
    if task.get('_todoState'):
        from lib.tools.todo import public_todo_state
        done_event['todoState'] = public_todo_state(task['_todoState'])
    if task.get('_waiting_on'):
        done_event['waitingOn'] = task['_waiting_on']
    _project_execution_facts(task, done_event, cfg=cfg, model=model,
                             thinking_depth=thinking_depth, api_rounds=api_rounds)
    _merge_checkpoint_metadata(task, done_event)
    return done_event


def _project_execution_facts(task, done_event, *, cfg, model, thinking_depth, api_rounds):
    # The toolbar capsule is only a send-time client snapshot. Project the
    # server-authoritative model/depth/modes at done so mid-turn fallback or a
    # preset switch cannot leave the durable turn labelled with stale facts;
    # the info-rail reducer replaces, rather than merges, these values.
    done_event['actualModel'] = task.get('_fallback_model') or model
    if thinking_depth:
        done_event['actualDepth'] = thinking_depth
    # Anchor for the frontend turn-ctx capsule reconcile: the stable _msgId
    # of the user turn that TRIGGERED this task, stamped by
    # ``start_conversation_attempt_executor`` (send/regenerate/continue) or inherited by
    # autopilot VU sub-tasks from their parent. Frontend prefers this over
    # "the last user in conv" — the latter is wrong under autopilot VU,
    # concurrent conv, or a regenerate targeting a historical user turn.
    # Empty string when a headless / external / legacy caller never stamped
    # it → frontend falls back to the last-user heuristic (safe default).
    if task.get('_userMsgId'):
        done_event['userMsgId'] = task['_userMsgId']
    _actual_modes: list[dict[str, str]] = []
    _flow = cfg.get('activeFlow') if isinstance(cfg.get('activeFlow'), str) else ''
    if _flow:
        _actual_modes.append({'label': _flow, 'tone': 'mode'})
    elif cfg.get('autopilot'):
        _actual_modes.append({'label': 'Goal Mode', 'tone': 'mode'})
    done_event['actualModes'] = _actual_modes
    if api_rounds:
        done_event['apiRounds'] = api_rounds
        task['apiRounds'] = api_rounds
    if task.get('_fallback_model'):
        done_event['fallbackModel'] = task['_fallback_model']
        done_event['fallbackFrom'] = task.get('_fallback_from', '')
        if task.get('_fallback_reason'):
            done_event['fallbackReason'] = task['_fallback_reason']
        if task.get('_fallback_kind'):
            done_event['fallbackKind'] = task['_fallback_kind']


def _merge_checkpoint_metadata(task, done_event):
    """Merge retained continuation receipts; file-journal I/O belongs to commit_round."""
    _cp_usage = task.get('_checkpointUsage')
    if _cp_usage and done_event.get('usage'):
        merged_usage = dict(_cp_usage)
        merged_usage.update(done_event['usage'])
        counters = {}
        accumulate_usage_into(counters, _cp_usage)
        accumulate_usage_into(counters, done_event['usage'])
        merged_usage.update(counters)
        done_event['usage'] = merged_usage
        task['usage'] = merged_usage
    elif _cp_usage and not done_event.get('usage'):
        done_event['usage'] = _cp_usage
        task['usage'] = _cp_usage

    if _cp_usage:
        task['_invocationCheckpointUsageMerged'] = True
        task['ownUsage'] = dict(task['usage'])

    _cp_api_rounds = task.get('_checkpointApiRounds')
    if _cp_api_rounds:
        merged_api = list(_cp_api_rounds) + (done_event.get('apiRounds') or [])
        done_event['apiRounds'] = merged_api
        task['apiRounds'] = merged_api

    _cp_mod_files = task.get('_checkpointModifiedFiles')
    if _cp_mod_files is not None and done_event.get('modifiedFiles') is not None:
        done_event['modifiedFiles'] = _cp_mod_files + done_event['modifiedFiles']
        task['modifiedFiles'] = done_event['modifiedFiles']

    _cp_mod_list = task.get('_checkpointModifiedFileList')
    if _cp_mod_list:
        from lib.project_mod.receipts import bound_file_changes, merge_file_changes
        merged_list, truncated = bound_file_changes(merge_file_changes(
            _cp_mod_list, done_event.get('modifiedFileList') or []))
        if truncated:
            task['modifiedFilesTruncated'] = True
        done_event['modifiedFileList'] = merged_list
        task['modifiedFileList'] = merged_list
        done_event['modifiedFiles'] = task['modifiedFiles'] = len(merged_list)

    if task.get('modifiedFilesTruncated'):
        done_event['modifiedFilesTruncated'] = True


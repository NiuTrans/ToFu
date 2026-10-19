"""Validate and hydrate an interrupted Turn's resume checkpoint.

This boundary deliberately has two phases. Every supplied checkpoint field is
validated and copied first; only then may ``task`` or ``messages`` change. A
malformed durable snapshot must never be truthiness-repaired into empty state,
partially applied, or misattributed to the first provider request.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lib.error_envelope import make_envelope
from lib.llm.llm_errors import RequestScopedError
from lib.log import get_logger
from lib.tasks_pkg.conv_message_builder._toolcalls import (
    _reconstruct_tool_call_messages,
)
from lib.tool_results.tool_round_replay import scan_replayable_tool_round_prefix
from lib.tools.todo import (
    TODO_MAX_CONTENT_CHARS,
    TODO_MAX_EXPLANATION_CHARS,
    TODO_MAX_RESTORED_ITEMS,
    TODO_MAX_ITEMS,
    TODO_STATE_VERSION,
    VALID_STATUSES,
    public_todo_state,
)

# Historical checkpoint bounds remain at the decoder, not the model tool API.
TODO_MAX_HISTORY_ENTRIES = 8
TODO_MAX_ID_CHARS = 64
TODO_MAX_STACK_DEPTH = 6
TODO_MAX_STATE_BYTES = 1_500_000

logger = get_logger('tofu.orchestrator')


class ContinueResumeStateProtocolError(RequestScopedError):
    """A Continue snapshot cannot be restored without changing its meaning."""

    def __init__(self, detail: str):
        super().__init__(detail, status_code=422)
        self._user_message = make_envelope(
            'bad_request',
            message=('Continue resume state is malformed / '
                     '继续执行恢复状态格式错误'),
            detail=detail,
            context='continue-resume-state',
            source='task-orchestrator',
            retryable=False,
            hint=('Regenerate from the Turn or start a fresh turn. No partial '
                  'resume state was sent to the model. / '
                  '请从该轮重新生成或新建一轮；后端未向模型发送任何残缺恢复状态。'),
        )


@dataclass(frozen=True)
class _PreparedResumeState:
    content_prefix: str
    thinking_prefix: str
    resume_prefill: str
    checkpoint_tool_rounds: tuple[dict[str, Any], ...]
    checkpoint_messages: tuple[dict[str, Any], ...]
    checkpoint_todo_state: dict[str, Any] | None
    checkpoint_usage: dict[str, Any] | None
    checkpoint_usage_complete: bool | None
    checkpoint_api_rounds: tuple[dict[str, Any], ...]
    checkpoint_modified_files: int | None
    checkpoint_modified_file_list: tuple[dict[str, Any] | str, ...]
    checkpoint_modified_files_truncated: bool
    next_model_round: int
    last_tool_round: int


def _protocol_error(field: str, detail: str) -> ContinueResumeStateProtocolError:
    return ContinueResumeStateProtocolError(f'{field}: {detail}')


def _optional_text(cfg: Mapping[str, Any], field: str) -> str:
    if field not in cfg or cfg.get(field) is None:
        return ''
    value = cfg.get(field)
    if not isinstance(value, str):
        raise _protocol_error(
            field, f'must be a string, got {type(value).__name__}')
    return value


def _json_safe_copy(
    value: Any,
    field: str,
    *,
    max_bytes: int | None = None,
) -> Any:
    """Copy one JSON authority without accepting opaque Python values."""
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise _protocol_error(
            field, 'must contain only finite JSON values') from exc
    if max_bytes is not None and len(encoded.encode('utf-8')) > max_bytes:
        raise _protocol_error(
            field,
            f'exceeds {max_bytes} serialized UTF-8 bytes',
        )
    return copy.deepcopy(value)


def _strict_todo_state(value: Any) -> dict[str, Any]:
    """Return a canonical todo snapshot, rejecting every lossy repair."""
    field = 'checkpointTodoState'
    if not isinstance(value, Mapping):
        raise _protocol_error(field, 'must be an object')
    raw = _json_safe_copy(
        dict(value), field, max_bytes=TODO_MAX_STATE_BYTES)

    version = raw.get('version')
    if type(version) is not int or version not in (2, TODO_STATE_VERSION):
        raise _protocol_error(field, 'version must equal 2 or 3')
    if version == TODO_STATE_VERSION:
        if set(raw) != {'version', 'todos', 'update_count', 'explanation'}:
            raise _protocol_error(field, 'v3 requires only version, todos, update_count, explanation')
        if type(raw['update_count']) is not int or raw['update_count'] < 0:
            raise _protocol_error(field, 'update_count must be a non-negative integer')
        if not isinstance(raw['explanation'], str) or len(raw['explanation']) > TODO_MAX_EXPLANATION_CHARS:
            raise _protocol_error(field, 'explanation must be a string of at most 2048 characters')
        todos = raw['todos']
        # Restored state may contain every item from a migrated six-frame
        # checkpoint. New tool writes still use the smaller 24-item bound.
        if not isinstance(todos, list) or len(todos) > TODO_MAX_RESTORED_ITEMS:
            raise _protocol_error(field, 'todos must be a list of at most 144 restored items')
        for item in todos:
            if not isinstance(item, dict) or set(item) != {'content', 'status'}:
                raise _protocol_error(field, 'todo requires only content and status')
            content = item['content']
            if (not isinstance(content, str) or not content.strip()
                    or len(content) > TODO_MAX_CONTENT_CHARS):
                raise _protocol_error(field, 'content must be non-empty and at most 512 characters')
            if not isinstance(item['status'], str) or item['status'] not in VALID_STATUSES:
                raise _protocol_error(field, 'status is invalid')
        return raw
    stack = raw.get('stack')
    if not isinstance(stack, list):
        raise _protocol_error(field, 'stack must be a list')
    if len(stack) > TODO_MAX_STACK_DEPTH:
        raise _protocol_error(
            field, f'stack exceeds {TODO_MAX_STACK_DEPTH} levels')
    update_count = raw.get('update_count', 0)
    if (isinstance(update_count, bool) or not isinstance(update_count, int)
            or update_count < 0):
        raise _protocol_error(
            field, 'update_count must be a non-negative integer')
    root_completed = raw.get('root_completed', False)
    if not isinstance(root_completed, bool):
        raise _protocol_error(field, 'root_completed must be a boolean')
    history = raw.get('history', [])
    if (not isinstance(history, list)
            or any(not isinstance(item, Mapping) for item in history)):
        raise _protocol_error(field, 'history must be a list of objects')
    if len(history) > TODO_MAX_HISTORY_ENTRIES:
        raise _protocol_error(
            field,
            f'history exceeds {TODO_MAX_HISTORY_ENTRIES} retained entries',
        )
    for entry in history:
        reason = entry.get('reason', '')
        if not isinstance(reason, str) or len(reason) > 2048:
            raise _protocol_error(field, 'history reason exceeds 2048 characters or is not text')
    history_dropped = raw.get('history_dropped', 0)
    if (isinstance(history_dropped, bool)
            or not isinstance(history_dropped, int)
            or history_dropped < 0):
        raise _protocol_error(
            field, 'history_dropped must be a non-negative integer')

    normalized_stack: list[dict[str, Any]] = []
    for frame_position, frame in enumerate(stack):
        frame_field = f'{field}.stack[{frame_position}]'
        if not isinstance(frame, Mapping):
            raise _protocol_error(frame_field, 'must be an object')
        checklist_id = frame.get('checklist_id')
        if not isinstance(checklist_id, str) or not checklist_id.strip():
            raise _protocol_error(
                frame_field, 'checklist_id must be a non-empty string')
        if len(checklist_id) > TODO_MAX_ID_CHARS:
            raise _protocol_error(frame_field, 'checklist_id exceeds 64 characters')
        revision = frame.get('revision')
        if (isinstance(revision, bool) or not isinstance(revision, int)
                or revision < 1):
            raise _protocol_error(
                frame_field, 'revision must be a positive integer')
        todos = frame.get('todos')
        if not isinstance(todos, list) or (not todos and len(stack) > 1):
            raise _protocol_error(frame_field, 'todos must be a list; nested frames cannot be empty')
        if len(todos) > TODO_MAX_ITEMS:
            raise _protocol_error(
                frame_field, f'todos exceeds {TODO_MAX_ITEMS} items')

        seen_ids: set[str] = set()
        in_progress_count = 0
        normalized_todos: list[dict[str, str]] = []
        for todo_position, item in enumerate(todos):
            todo_field = f'{frame_field}.todos[{todo_position}]'
            if not isinstance(item, Mapping):
                raise _protocol_error(todo_field, 'must be an object')
            todo_id = item.get('id')
            content = item.get('content')
            status = item.get('status')
            if not isinstance(todo_id, str) or not todo_id.strip():
                raise _protocol_error(todo_field, 'id must be a non-empty string')
            if todo_id.strip() != todo_id:
                raise _protocol_error(todo_field, 'id must already be normalized')
            if len(todo_id) > TODO_MAX_ID_CHARS:
                raise _protocol_error(
                    todo_field, f'id exceeds {TODO_MAX_ID_CHARS} characters')
            if todo_id in seen_ids:
                raise _protocol_error(frame_field, f'duplicate todo id {todo_id!r}')
            if not isinstance(content, str) or not content.strip():
                raise _protocol_error(
                    todo_field, 'content must be a non-empty string')
            if content.strip() != content:
                raise _protocol_error(
                    todo_field, 'content must already be normalized')
            if len(content) > TODO_MAX_CONTENT_CHARS:
                raise _protocol_error(
                    todo_field,
                    f'content exceeds {TODO_MAX_CONTENT_CHARS} characters',
                )
            if not isinstance(status, str) or status not in VALID_STATUSES:
                raise _protocol_error(todo_field, 'status is invalid')
            seen_ids.add(todo_id)
            in_progress_count += int(status == 'in_progress')
            normalized_todos.append({
                'id': todo_id, 'content': content, 'status': status,
            })
        if in_progress_count > 1:
            raise _protocol_error(
                frame_field, 'has more than one in_progress item')

        parent_todo_id = frame.get('parent_todo_id')
        if frame_position == 0:
            if parent_todo_id is not None:
                raise _protocol_error(
                    frame_field, 'root frame cannot have parent_todo_id')
        else:
            if not isinstance(parent_todo_id, str) or not parent_todo_id:
                raise _protocol_error(
                    frame_field, 'child frame needs parent_todo_id')
            if len(parent_todo_id) > TODO_MAX_ID_CHARS:
                raise _protocol_error(
                    frame_field,
                    f'parent_todo_id exceeds {TODO_MAX_ID_CHARS} characters',
                )
            parent_items = normalized_stack[-1]['todos']
            parent_item = next(
                (item for item in parent_items
                 if item['id'] == parent_todo_id),
                None,
            )
            if parent_item is None or parent_item['status'] != 'in_progress':
                raise _protocol_error(
                    frame_field,
                    'parent_todo_id must identify the active parent item',
                )
        normalized_stack.append({'todos': normalized_todos})

    if stack:
        derived_root_completed = (
            len(stack) == 1
            and all(item['status'] == 'completed'
                    for item in normalized_stack[0]['todos'])
        )
        if root_completed != derived_root_completed:
            raise _protocol_error(
                field, 'root_completed disagrees with stack state')
    return public_todo_state(raw)


def prepare_resume_state(cfg: Any) -> _PreparedResumeState:
    """Validate and detach all supplied state before the first mutation."""
    if not isinstance(cfg, Mapping):
        raise _protocol_error('config', 'must be an object')

    raw_rounds = (
        cfg.get('checkpointToolRounds')
        if 'checkpointToolRounds' in cfg else None
    )
    if raw_rounds is None:
        checkpoint_rounds: tuple[dict[str, Any], ...] = ()
        checkpoint_messages: tuple[dict[str, Any], ...] = ()
    else:
        if not isinstance(raw_rounds, list):
            raise _protocol_error('checkpointToolRounds', 'must be a list')
        if any(not isinstance(item, Mapping) for item in raw_rounds):
            raise _protocol_error(
                'checkpointToolRounds',
                'every occurrence must be an object',
            )
        detached_rounds = _json_safe_copy(
            raw_rounds, 'checkpointToolRounds')
        replay_prefix = scan_replayable_tool_round_prefix(detached_rounds)
        if replay_prefix.blocked_position is not None:
            raise _protocol_error(
                'checkpointToolRounds',
                f'occurrence {replay_prefix.blocked_position} creates a causal '
                f'gap ({replay_prefix.blocked_reason})',
            )
        checkpoint_rounds = tuple(dict(item) for item in detached_rounds)
        reconstructed = _reconstruct_tool_call_messages(detached_rounds) or []
        checkpoint_messages = tuple(dict(item) for item in reconstructed)

    raw_todo = (
        cfg.get('checkpointTodoState')
        if 'checkpointTodoState' in cfg else None
    )
    todo_state = None if raw_todo is None else _strict_todo_state(raw_todo)

    raw_usage = cfg.get('checkpointUsage') \
        if 'checkpointUsage' in cfg else None
    if raw_usage is not None and not isinstance(raw_usage, Mapping):
        raise _protocol_error('checkpointUsage', 'must be an object')
    checkpoint_usage = (
        None if raw_usage is None
        else dict(_json_safe_copy(dict(raw_usage), 'checkpointUsage'))
    )
    checkpoint_usage_complete = cfg.get('checkpointUsageComplete')
    if checkpoint_usage_complete is not None and type(checkpoint_usage_complete) is not bool:
        raise _protocol_error('checkpointUsageComplete', 'must be a boolean')

    raw_api_rounds = (
        cfg.get('checkpointApiRounds')
        if 'checkpointApiRounds' in cfg else None
    )
    if raw_api_rounds is not None:
        if (not isinstance(raw_api_rounds, list)
                or any(not isinstance(item, Mapping)
                       for item in raw_api_rounds)):
            raise _protocol_error(
                'checkpointApiRounds', 'must be a list of objects')
        detached_api_rounds = _json_safe_copy(
            raw_api_rounds, 'checkpointApiRounds')
        checkpoint_api_rounds = tuple(
            dict(item) for item in detached_api_rounds)
    else:
        checkpoint_api_rounds = ()

    raw_modified_files = (
        cfg.get('checkpointModifiedFiles')
        if 'checkpointModifiedFiles' in cfg else None
    )
    if raw_modified_files is not None and (
        isinstance(raw_modified_files, bool)
        or not isinstance(raw_modified_files, int)
        or raw_modified_files < 0
    ):
        raise _protocol_error(
            'checkpointModifiedFiles', 'must be a non-negative integer')

    modified_files_truncated = cfg.get('checkpointModifiedFilesTruncated', False)
    if not isinstance(modified_files_truncated, bool):
        raise _protocol_error('checkpointModifiedFilesTruncated', 'must be a boolean')
    raw_modified_file_list = (
        cfg.get('checkpointModifiedFileList')
        if 'checkpointModifiedFileList' in cfg else None
    )
    if raw_modified_file_list is not None:
        if not isinstance(raw_modified_file_list, list):
            raise _protocol_error(
                'checkpointModifiedFileList', 'must be a list')
        for position, item in enumerate(raw_modified_file_list):
            if not isinstance(item, (Mapping, str)) or not item:
                raise _protocol_error(
                    'checkpointModifiedFileList',
                    f'item {position} must be a non-empty object or string',
                )
        detached_file_list = _json_safe_copy(
            raw_modified_file_list, 'checkpointModifiedFileList')
        checkpoint_modified_file_list = tuple(
            dict(item) if isinstance(item, Mapping) else item
            for item in detached_file_list
        )
    else:
        checkpoint_modified_file_list = ()

    from lib.tasks_pkg.continuation_rounds import continuation_round_counters
    next_model_round, last_tool_round = continuation_round_counters(
        checkpoint_rounds)
    # A prose-only continuation has no tool receipt frontier. Paid ordinary
    # request rows are one-based; compaction/fallback labels are not rounds.
    for row in checkpoint_api_rounds:
        raw_round = row.get('round')
        if type(raw_round) is int and raw_round > 0:
            next_model_round = max(next_model_round, raw_round)

    return _PreparedResumeState(
        content_prefix=_optional_text(cfg, 'contentPrefix'),
        thinking_prefix=_optional_text(cfg, 'thinkingPrefix'),
        resume_prefill=_optional_text(cfg, 'resumePrefill'),
        checkpoint_tool_rounds=checkpoint_rounds,
        checkpoint_messages=checkpoint_messages,
        checkpoint_todo_state=todo_state,
        checkpoint_usage=checkpoint_usage,
        checkpoint_usage_complete=checkpoint_usage_complete,
        checkpoint_api_rounds=checkpoint_api_rounds,
        checkpoint_modified_files=raw_modified_files,
        checkpoint_modified_file_list=checkpoint_modified_file_list,
        checkpoint_modified_files_truncated=modified_files_truncated,
        next_model_round=next_model_round,
        last_tool_round=last_tool_round,
    )


def apply_resume_state(
    *,
    task: dict[str, Any],
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    tid: str,
    prepared_state: _PreparedResumeState | None = None,
    round_state: Any = None,

    original_messages: list[dict[str, Any]] | None = None,
) -> None:
    """Atomically hydrate validated Continue state before provider dispatch."""
    prepared = (
        prepared_state
        if prepared_state is not None else prepare_resume_state(cfg)
    )

    supports_prefill = False
    if prepared.resume_prefill:
        from lib.model_info import model_supports_assistant_prefill
        supports_prefill = bool(model_supports_assistant_prefill(model))

    background_receipts = cfg.get('_resumeBackgroundReceipts')
    if background_receipts is not None:
        if not isinstance(background_receipts, list):
            raise _protocol_error('_resumeBackgroundReceipts', 'must be a list')
        background_receipts = _json_safe_copy(background_receipts, '_resumeBackgroundReceipts')

    # Everything above is read-only. From here on, assignments cannot observe
    # a malformed later field and leave a half-applied resume snapshot.
    if background_receipts is not None:
        task['_bgCommandInjects'] = background_receipts

    if prepared.content_prefix:
        with task['content_lock']:
            task['content'] = prepared.content_prefix
        logger.debug(
            '[%s] conv=%s Applied contentPrefix (%d chars) from continue checkpoint',
            tid, task.get('convId', ''), len(prepared.content_prefix),
        )

    if prepared.thinking_prefix:
        # Display continuity for a lossless continue: the reasoning lane keeps
        # accumulating from the interrupted tail. New reasoning deltas append
        # (delta coalescer), and round-base retry truncation restores this
        # seed rather than blanking the lane.
        with task['content_lock']:
            task['thinking'] = prepared.thinking_prefix
        logger.debug(
            '[%s] conv=%s Applied thinkingPrefix (%d chars) from continue checkpoint',
            tid, task.get('convId', ''), len(prepared.thinking_prefix),
        )

    # A checkpoint-resume excludes the interrupted assistant Turn from the
    # ordinary conversation projection. Its durable tool rounds therefore
    # have no other route onto the model wire. Replay them in the exact same
    # canonical form used when that Turn is later reconstructed as settled
    # history; otherwise the resumed model loses its observations and the next
    # user turn expands into a different message prefix, defeating cross-turn
    # provider caching as well as semantic continuity.
    if prepared.checkpoint_messages or 'checkpointToolRounds' in cfg:
        from lib.tasks_pkg.compaction._resume_projection import restore_compacted_resume
        from lib.tasks_pkg.message_semantics import has_model_media
        baseline = original_messages if original_messages is not None else task.get('messages')
        fresh_messages = None
        if (isinstance(baseline, list) and len(messages) >= len(baseline)
                and messages[:len(baseline)] == baseline
                and not any(has_model_media(message) for message in messages)):
            fresh_messages = messages[len(baseline):]
        restored = restore_compacted_resume(
            task, cfg, prepared.checkpoint_tool_rounds, fresh_messages=fresh_messages)

        if task.get('aborted'):
            return
        if restored is not None:
            from lib.tasks_pkg.compaction._pipeline import recompose_context_after_compaction
            recompose_context_after_compaction(restored, task=task)
            if task.get('aborted'):
                return
            messages[:] = restored
        else:
            messages.extend(copy.deepcopy(prepared.checkpoint_messages))
        logger.info(
            '[%s] conv=%s Replayed %d checkpoint message(s) from %d durable '
            'tool round(s) before resume prefill',
            tid, task.get('convId', ''), len(prepared.checkpoint_messages),
            len(prepared.checkpoint_tool_rounds),
        )

    # ``contentPrefix`` remains display bookkeeping only. ``resumePrefill`` is
    # the separately capability-gated trailing assistant continuation.
    if prepared.resume_prefill:
        if supports_prefill:
            messages.append({
                'role': 'assistant', 'content': prepared.resume_prefill})
            task['_resumePrefill'] = prepared.resume_prefill
            logger.info(
                '[%s] conv=%s Injected resume prefill (%d chars) as trailing '
                'assistant turn — model=%s will continue the same tokens',
                tid, task.get('convId', ''),
                len(prepared.resume_prefill), model,
            )
        else:
            logger.info(
                '[%s] conv=%s resumePrefill present but model=%s rejects prefill '
                '— falling back to regenerate-from-checkpoint '
                '(contentPrefix seed only)',
                tid, task.get('convId', ''), model,
            )

    # One logical Turn continues after the retained checkpoint. Only new
    # receipts enter this executor's list; persistence merges the two lists
    # once and preserves their private producer identities for stale fences.
    if round_state is not None:
        round_state.model_round_start = prepared.next_model_round
        round_state.tool_round_num = max(
            round_state.tool_round_num, prepared.last_tool_round)
        round_state.tool_call_happened = bool(
            round_state.tool_call_happened or prepared.checkpoint_messages)
    if prepared.checkpoint_tool_rounds:
        task['_checkpointToolRounds'] = [
            dict(item) for item in prepared.checkpoint_tool_rounds]
        logger.debug(
            '[%s] conv=%s Stashed %d checkpoint toolRounds for DB merge',
            tid, task.get('convId', ''),
            len(prepared.checkpoint_tool_rounds),
        )
    if prepared.checkpoint_todo_state is not None:
        todo_state = copy.deepcopy(prepared.checkpoint_todo_state)
        task['_todoState'] = todo_state
        task['_todos'] = copy.deepcopy(todo_state['todos'])
        logger.debug(
            '[%s] conv=%s Restored todo snapshot items=%d on Continue',
            tid, task.get('convId', ''), len(todo_state['todos']),
        )
    if prepared.checkpoint_usage:
        task['_checkpointUsage'] = prepared.checkpoint_usage
        task['usage'] = copy.deepcopy(prepared.checkpoint_usage)
    if prepared.checkpoint_usage_complete is False:
        # Restoring display counters does not reconcile an unreceipted request.
        # Preserve unknown consumption through the existing model-admission port.
        task['_executionActualsComplete'] = False
        task['_checkpointUsageComplete'] = False
    if prepared.checkpoint_api_rounds:
        task['_checkpointApiRounds'] = list(prepared.checkpoint_api_rounds)
        task['apiRounds'] = copy.deepcopy(list(prepared.checkpoint_api_rounds))
    if prepared.checkpoint_modified_files is not None:
        task['_checkpointModifiedFiles'] = prepared.checkpoint_modified_files
    if prepared.checkpoint_modified_files_truncated:
        task['modifiedFilesTruncated'] = True
    if prepared.checkpoint_modified_file_list:
        task['_checkpointModifiedFileList'] = list(
            prepared.checkpoint_modified_file_list)

    # One task-local positive receipt, not a durable or user-supplied authority.
    # Successful terminal publication rechecks the final delivered model body.
    task['_resumeContextHydrated'] = True


__all__ = [
    'ContinueResumeStateProtocolError',
    'apply_resume_state',
    'prepare_resume_state',
]

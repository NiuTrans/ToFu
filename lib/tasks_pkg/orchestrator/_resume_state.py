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
from lib.llm_errors import RequestScopedError
from lib.log import get_logger
from lib.tasks_pkg.conv_message_builder._toolcalls import (
    _reconstruct_tool_call_messages,
)
from lib.tool_round_replay import scan_replayable_tool_round_prefix
from lib.tools.todo import (
    TODO_MAX_CONTENT_CHARS,
    TODO_MAX_HISTORY_ENTRIES,
    TODO_MAX_ID_CHARS,
    TODO_MAX_ITEMS,
    TODO_MAX_STACK_DEPTH,
    TODO_MAX_STATE_BYTES,
    TODO_STATE_VERSION,
    VALID_STATUSES,
    public_todo_state,
)

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
    checkpoint_api_rounds: tuple[dict[str, Any], ...]
    checkpoint_modified_files: int | None
    checkpoint_modified_file_list: tuple[dict[str, Any] | str, ...]


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

    version = raw.get('version', TODO_STATE_VERSION)
    if (isinstance(version, bool) or not isinstance(version, int)
            or version != TODO_STATE_VERSION):
        raise _protocol_error(field, f'version must equal {TODO_STATE_VERSION}')
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
        revision = frame.get('revision')
        if (isinstance(revision, bool) or not isinstance(revision, int)
                or revision < 1):
            raise _protocol_error(
                frame_field, 'revision must be a positive integer')
        todos = frame.get('todos')
        if not isinstance(todos, list) or not todos:
            raise _protocol_error(frame_field, 'todos must be a non-empty list')
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
            if status not in VALID_STATUSES:
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

    return _PreparedResumeState(
        content_prefix=_optional_text(cfg, 'contentPrefix'),
        thinking_prefix=_optional_text(cfg, 'thinkingPrefix'),
        resume_prefill=_optional_text(cfg, 'resumePrefill'),
        checkpoint_tool_rounds=checkpoint_rounds,
        checkpoint_messages=checkpoint_messages,
        checkpoint_todo_state=todo_state,
        checkpoint_usage=checkpoint_usage,
        checkpoint_api_rounds=checkpoint_api_rounds,
        checkpoint_modified_files=raw_modified_files,
        checkpoint_modified_file_list=checkpoint_modified_file_list,
    )


def apply_resume_state(
    *,
    task: dict[str, Any],
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    tid: str,
    prepared_state: _PreparedResumeState | None = None,
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

    # Everything above is read-only. From here on, assignments cannot observe
    # a malformed later field and leave a half-applied resume snapshot.
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
    if prepared.checkpoint_messages:
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

    # Stash checkpoint metadata without polluting this executor's local round
    # list. ``toolRounds`` stays attempt-local (its counters restart at zero);
    # the durable Turn projection and persistence boundary merge checkpoint +
    # current history once and preserve each round's attempt/task identity.
    if prepared.checkpoint_tool_rounds:
        task['_checkpointToolRounds'] = [
            dict(item) for item in prepared.checkpoint_tool_rounds]
        logger.debug(
            '[%s] conv=%s Stashed %d checkpoint toolRounds for DB merge',
            tid, task.get('convId', ''),
            len(prepared.checkpoint_tool_rounds),
        )
    if prepared.checkpoint_todo_state is not None:
        todo_state = prepared.checkpoint_todo_state
        todo_stack = todo_state['stack']
        task['_todoState'] = todo_state
        task['_todos'] = (
            copy.deepcopy(todo_stack[-1]['todos']) if todo_stack else [])
        logger.debug(
            '[%s] conv=%s Restored checklist stack depth=%d on Continue',
            tid, task.get('convId', ''), len(todo_stack),
        )
    if prepared.checkpoint_usage:
        task['_checkpointUsage'] = prepared.checkpoint_usage
    if prepared.checkpoint_api_rounds:
        task['_checkpointApiRounds'] = list(prepared.checkpoint_api_rounds)
    if prepared.checkpoint_modified_files is not None:
        task['_checkpointModifiedFiles'] = prepared.checkpoint_modified_files
    if prepared.checkpoint_modified_file_list:
        task['_checkpointModifiedFileList'] = list(
            prepared.checkpoint_modified_file_list)


__all__ = [
    'ContinueResumeStateProtocolError',
    'apply_resume_state',
    'prepare_resume_state',
]

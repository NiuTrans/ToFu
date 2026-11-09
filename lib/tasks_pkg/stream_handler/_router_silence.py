"""Scoped deliberate-silence declaration shared by stream and terminal owners."""
from __future__ import annotations

from collections.abc import Mapping
import re

from lib.log import get_logger
from lib.tasks_pkg.stream_handler._intent_stall import (
    SILENT_TURN_MARKER, _END_TURN_TOKEN_RE, is_silent_turn,
)

logger = get_logger(__name__)

SILENT_TURN_FIELD = '_silentTurn'
SILENT_IDENTITY_FIELD = '_silentTurnIdentity'
SILENT_CONTEXT_FIELD = '_silentRouterContext'
SILENT_ELIGIBILITY_FIELD = '_silentNoticeTurn'
SILENT_NOTE_KIND = 'deliberate-silence'
SILENT_NOTE_TEXT = '已审阅，无需回复 / Reviewed; no reply required.'
_END_TURN_FRAGMENT_RE = re.compile(r'\[END_TURN(?::[^\]]*)?$')


def _identity(task: dict) -> tuple:
    return (task.get('_userId'), task.get('convId'),
            task.get('_turnId') or (task.get('config') or {}).get('_turnId'),
            task.get('_attemptId'))


def _router_task(task: dict) -> bool:
    """Resolve Router scope once per execution; all failures deny the control protocol."""
    identity = _identity(task)
    cached = task.get(SILENT_CONTEXT_FIELD)
    if isinstance(cached, dict) and cached.get('identity') == identity:
        if cached.get('provisional') is True:
            try:
                from lib.router.policy import router_scope
                if router_scope(task) is not None and type(task.get('_userId')) is int:
                    task[SILENT_CONTEXT_FIELD] = {'identity': identity, 'router': True}
                    return True
            except Exception:
                logger.warning('[Router] silence scope unavailable; denying silence', exc_info=True)
            task[SILENT_CONTEXT_FIELD] = {'identity': identity, 'router': False}
        return cached.get('router') is True
    try:
        from lib.router.policy import router_scope
        eligible = router_scope(task) is not None and type(task.get('_userId')) is int
    except Exception:
        logger.warning('[Router] silence scope unavailable; using ordinary response handling',
                       exc_info=True)
        eligible = False
    task[SILENT_CONTEXT_FIELD] = {'identity': identity, 'router': eligible}
    return eligible


def silent_turn_declared(task: dict) -> bool:
    return (task.get(SILENT_TURN_FIELD) is True
            and task.get(SILENT_IDENTITY_FIELD) == _identity(task))


def _human_steer(task: dict, output: dict, messages, owner: int) -> bool:
    """Recognize committed human authorship, including its live carrier before persistence."""
    from lib.turn_initiation import require_human_turn, steer_author_turn
    projection = output.get('projection') or {}
    records = list(task.get('_userSteerInjects') or []) + list(
        projection.get('_userSteerInjects') or [])
    for record in records:
        if not isinstance(record, Mapping):
            continue
        author = record.get('author')
        if author is None:
            # The live lane has the author in agent_inbox, not in this display
            # twin. Its durable record normally exists by the terminal; the
            # command namespace and labelled model carrier close the interim.
            command_id = record.get('commandId')
            if isinstance(command_id, str) and command_id.startswith('channel-owner-'):
                return True
            continue
        if not isinstance(author, Mapping):
            return True
        try:
            require_human_turn(steer_author_turn(author), user_id=owner)
        except ValueError:
            continue
        return True
    from lib.router.forwarding import STEER_LABEL
    label = STEER_LABEL.split('{steer_id}', 1)[0]
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, Mapping):
            continue
        content = message.get('content')
        if isinstance(content, str) and label in content:
            return True
    return False


def silence_declaration_allowed(task: dict, messages) -> bool:
    """Only a durable Router reply to a nonhuman notice, with no human steer, may be silent."""
    if not _router_task(task):
        return False
    owner = task.get('_userId')
    conversation_id = task.get('convId')
    turn_id = task.get('_turnId') or (task.get('config') or {}).get('_turnId')
    attempt_id = task.get('_attemptId')
    cached = task.get(SILENT_ELIGIBILITY_FIELD)
    same_turn = (isinstance(cached, dict)
                 and cached.get('conversationId') == conversation_id
                 and cached.get('turnId') == turn_id
                 and cached.get('attemptId') == attempt_id)
    if same_turn and cached.get('eligible') is False:
        return False
    try:
        from lib.turn_initiation import (
            INITIATOR_CHANNEL, INITIATOR_PEER, resolve_turn_initiator,
        )
        from lib import agent_inbox
        from lib.turn_lifecycle import get_turn
        for item in agent_inbox.pending_items(task['convId'], modes=['user-steer']):
            if item.get('steerAuthor'):
                task[SILENT_ELIGIBILITY_FIELD] = {
                    'conversationId': conversation_id, 'turnId': turn_id,
                    'attemptId': attempt_id, 'eligible': False,
                }
                return False
        output = get_turn(conversation_id, turn_id, user_id=owner)
        parent_id = output.get('parentTurnId') if isinstance(output, dict) else None
        if not (output.get('actor') == 'assistant'
                and output.get('conversationId') == conversation_id
                and output.get('turnId') == turn_id and parent_id):
            return False
        if not same_turn:
            source = get_turn(conversation_id, parent_id, user_id=owner)
            if resolve_turn_initiator(source) not in (INITIATOR_CHANNEL, INITIATOR_PEER):
                task[SILENT_ELIGIBILITY_FIELD] = {
                    'conversationId': conversation_id, 'turnId': turn_id,
                    'attemptId': attempt_id, 'eligible': False,
                }
                return False
        if _human_steer(task, output, messages, owner):
            task[SILENT_ELIGIBILITY_FIELD] = {
                'conversationId': conversation_id, 'turnId': turn_id,
                'attemptId': attempt_id, 'eligible': False,
            }
            return False
    except Exception:
        # A previously verified parent does not make a failed steer read safe.
        logger.warning('[Router] silence Turn/steer evidence unavailable; denying silence',
                       exc_info=True)
        return False
    task[SILENT_ELIGIBILITY_FIELD] = {
        'conversationId': conversation_id, 'turnId': turn_id,
        'attemptId': attempt_id, 'eligible': True,
    }
    return True


def hold_silent_text_delta(task: dict, content: str) -> bool:
    """Hold only a whole control declaration, including fragmented END_TURN tokens."""
    if not _router_task(task):
        return False
    if silent_turn_declared(task):
        return True
    if is_silent_turn(content):
        return True
    candidate = _END_TURN_TOKEN_RE.sub('', content)
    candidate = _END_TURN_FRAGMENT_RE.sub('', candidate).strip()
    if not candidate or not SILENT_TURN_MARKER.startswith(candidate):
        return False
    # Only a complete candidate may include the END_TURN control suffix.
    return candidate == content.strip()


def _reset_control_text(task: dict, assistant_msg: dict, llm_round) -> None:
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager import append_event, reset_task_text
    had_text = bool(task.get('content'))
    reset_task_text(task, content='', thinking=assistant_msg.get('reasoning_content') or '')
    if had_text and isinstance(llm_round, int) and not isinstance(llm_round, bool):
        append_event(task, build_event(
            EventType.DELTA_RESET, roundNum=llm_round, discard=True,
            contentEpoch=task.get('_contentEpoch')))


def project_terminal_control_text(
    task: dict, assistant_msg: dict, messages, *, llm_round,
) -> tuple[bool, bool]:
    """Return (deliberate silence, unrecoverable empty response).

    The original declaration remains in ``assistant_msg`` for provider replay;
    public text carries no control token. Python can retry the latter case.
    """
    if silent_turn_declared(task):
        return True, False
    content = assistant_msg.get('content') or ''
    if not is_silent_turn(content):
        return False, False
    if assistant_msg.get('tool_calls'):
        return False, False
    if declare_silent_turn(task, assistant_msg, messages, llm_round=llm_round):
        return True, False
    # Ordinary END_TURN hygiene already handles its marker. A denied silence
    # declaration must not expose a raw provider instruction in public prose.
    if _router_task(task) and SILENT_TURN_MARKER in content:
        _reset_control_text(task, assistant_msg, llm_round)
        return False, True
    return False, False


def declare_silent_turn(task: dict, assistant_msg: dict, messages, *, llm_round) -> bool:
    """Consume an exact authorized declaration without changing ordinary assistant prose."""
    if assistant_msg.get('tool_calls') or not _router_task(task):
        return False
    content = assistant_msg.get('content') or ''
    if not is_silent_turn(content):
        return False
    if not silence_declaration_allowed(task, messages):
        _reset_control_text(task, assistant_msg, llm_round)
        assistant_msg['content'] = ''
        return False
    if silent_turn_declared(task):
        return True
    from lib.tasks_pkg.segments import record_injected_note
    _reset_control_text(task, assistant_msg, llm_round)
    note = record_injected_note(
        task, llm_round=llm_round, kind=SILENT_NOTE_KIND,
        text=SILENT_NOTE_TEXT, action='stop', reason=SILENT_NOTE_KIND)
    if note is None:
        raise ValueError('deliberate silence requires a registered system-note kind')
    task[SILENT_TURN_FIELD] = True
    task[SILENT_IDENTITY_FIELD] = _identity(task)
    # Keep the declaration in provider replay: it proves that this notice was
    # reviewed. Only the public text projection is empty.
    assistant_msg['content'] = SILENT_TURN_MARKER
    return True


__all__ = ['SILENT_ELIGIBILITY_FIELD', 'SILENT_NOTE_KIND', 'SILENT_NOTE_TEXT',
           'SILENT_TURN_FIELD', 'declare_silent_turn', 'hold_silent_text_delta',
           'project_terminal_control_text',
           'silence_declaration_allowed', 'silent_turn_declared']

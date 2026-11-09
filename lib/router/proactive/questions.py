"""Owner questions: a durable index of questions the owner has not answered.

Owner: docs/ROUTER.md R10 "Owner question unanswered" trigger. Every
``ask_human`` from a bound Coordinator (Project Router or Global Router) is
recorded here at the relay seam (``_relay_to_global_router``). A question is
answered when its card has a response (blocking) or a human Turn arrives after
it in the same conversation (non-blocking). The digest and the quiet-end release
list open questions; no new reminder interval exists.

Records: portable record CAS, namespace ``router.proactive.questions.v1``, one
record per (owner, coordinator, guidanceId). The question card in the
Coordinator's conversation stays the canonical record; this is an index for the
digest and the cross-project view.

Entry points: record_question, open_questions, question_answered, OwnerQuestions.
Dependencies: turn_lifecycle (answer evidence), storage (record CAS).
"""
from __future__ import annotations

import time
import uuid

from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

NAMESPACE = 'router.proactive.questions.v1'


def _key(owner, coordinator_id, guidance_id):
    return f'{owner}:{coordinator_id}:{guidance_id}'


def _client(client=None):
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client
    return client


def record_question(owner, question, *, client=None):
    """Index one ask_human question from a bound lead. Idempotent per guidanceId."""
    owner = require_user_id(owner, context='owner questions')
    client = _client(client)
    key = _key(owner, question['coordinatorId'], question['guidanceId'])
    value = {
        'owner': owner,
        'coordinatorId': question['coordinatorId'],
        'projectPath': question['projectPath'],
        'guidanceId': question['guidanceId'],
        'turnId': question['turnId'],
        'toolCallId': question.get('toolCallId', ''),
        'blocking': bool(question.get('blocking')),
        'question': question['question'],
        'options': question.get('options') or [],
        'questions': question.get('questions') or [],
        'default': question.get('default', ''),
        'createdAt': int(time.time() * 1000),
    }
    try:
        existing = client().query('record.get', {'namespace': NAMESPACE, 'key': key})
        if existing is not None:
            return existing['value']
        client(write=True).command('record.put', {
            'namespace': NAMESPACE, 'key': key, 'value': value, 'expected_version': 0,
        }, 'owner-question:' + uuid.uuid4().hex)
    except StorageError as error:
        if error.code != 'database_conflict':
            raise
    return value


def question_answered(owner, question, *, turn_get=None, turn_lister=None):
    """Whether the owner answered this question (durable evidence).

    Blocking: the card's round entry carries a response or was superseded.
    Non-blocking: a human Turn after the question's Turn in the same conversation.
    """
    if turn_get is None:
        from lib.turn_lifecycle import get_turn as turn_get
    if turn_lister is None:
        from lib.turn_lifecycle import list_turns as turn_lister
    try:
        turn = turn_get(question['coordinatorId'], question['turnId'], user_id=owner)
    except Exception as error:
        logger.debug('[Questions] answered-turn read failed for %s: %s', question['turnId'], error)
        return False
    if turn is None:
        return False
    for round_entry in (turn.get('projection') or {}).get('toolRounds', []):
        if not isinstance(round_entry, dict):
            continue
        if round_entry.get('guidanceId') != question['guidanceId']:
            continue
        if round_entry.get('guidanceResponse') or round_entry.get('guidanceSuperseded'):
            return True
    if question.get('blocking'):
        return False
    # Non-blocking: a later human Turn in the same conversation is the answer.
    try:
        ordinal = turn.get('ordinal')
        if ordinal is None:
            return False
        later = turn_lister(question['coordinatorId'], user_id=owner,
                            after_ordinal=ordinal, light=True)
        for item in (later.get('turns') or []):
            if item.get('actor') == 'human':
                return True
    except Exception:
        logger.debug('[OwnerQuestions] answer check failed for %s', question['guidanceId'],
                     exc_info=True)
    return False


def open_questions(owner, *, client=None, turn_get=None, turn_lister=None):
    """All unanswered questions of this owner's leads, oldest first."""
    owner = require_user_id(owner, context='owner questions')
    client = _client(client)
    prefix = f'{owner}:'
    rows = []
    after = ''
    while True:
        page = client().query('record.list', {
            'namespace': NAMESPACE, 'prefix': prefix, 'after_key': after, 'limit': 1000})
        if not isinstance(page, list):
            break
        for row in page:
            if not isinstance(row, dict) or not str(row.get('key', '')).startswith(prefix):
                continue
            value = row.get('value')
            if not isinstance(value, dict):
                continue
            if not question_answered(owner, value, turn_get=turn_get, turn_lister=turn_lister):
                rows.append(value)
        if len(page) < 1000:
            break
        after = page[-1]['key']
    rows.sort(key=lambda row: row.get('createdAt', 0))
    return rows


__all__ = ['NAMESPACE', 'record_question', 'question_answered', 'open_questions']

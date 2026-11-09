"""Coordinator task board: the Coordinator's own summary of the human's tasks.

Entry points: ``CoordinatorTaskProgress.publish`` (the bound Coordinator's
``update_task_progress`` tool) and ``read`` (HTTP projection). One owner/project
head holds the latest complete board of the current Coordinator binding; a
replaced binding starts without a board. The board is model-authored by design
(docs/PROJECT_COORDINATION.md, "Session-free coordination"): a summary for the
human, not evidence, and it grants no authority. Questions for the human are
ask_human cards in the conversation, never board entries. No model, task,
worker or polling is started here.

One progress source: the board is also the Coordinator's checklist.
``publish_task_board`` (update_task_progress) and ``plan_publication``
(todo_write) write this one head under the task's progress lock and leave the
task's checklist state as the board's plan (``plan_from_board``); each write's
round carries ``progress_view`` so the browser refreshes both views live from
the conversation stream.

Display translation: the head may also carry translations of the board's own
texts (``board_texts``) into the language the human reads the Coordinator in.
They are reconstructible display data, derived by
``coordinator_progress_translation`` and merged here by ``untranslated_texts``
and ``attach_translation``; a stored translation always has at least one entry.

Commitments: a task carrying ``due`` is a promise to the owner
(``coordinator_commitments``; docs/PROJECT_COORDINATION.md "Commitments on the
task board"). Every board write validates the commitment rules and merges the
platform's ``commitments`` receipts into the same head CAS, then arms the
scheduler's one-shot due wake. A receipt is the record that one due time already
woke the lead, so it never wakes twice.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import time
import uuid

from lib.conversations.coordinator_reports import CONTRACT
from lib.conversations.project_coordination import ProjectCoordinationRepository
from lib.conversations.project_identity import normalize_project_path, project_channel_key
from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

POLICY = CONTRACT['taskProgress']
_TRANSLATION_MAX_CHARS = (CONTRACT['$defs']['progressTranslation']['properties']['texts']
                          ['items']['properties']['text']['maxLength'])
_TITLE_MAX_CHARS = CONTRACT['$defs']['progressTask']['properties']['title']['maxLength']
_SUMMARY_MAX_CHARS = CONTRACT['$defs']['progressRequest']['properties']['summary']['maxLength']
# Checklist statuses a board task shares; a board has no `blocked`, a plan no `cancelled`.
_PLAN_STATUSES = frozenset({'pending', 'in_progress', 'completed'})


@lru_cache(maxsize=8)
def _validator(definition):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({'$schema': CONTRACT['$schema'], '$defs': CONTRACT['$defs'],
                                 '$ref': '#/$defs/' + definition})


def validate_progress(value, definition):
    error = next(_validator(definition).iter_errors(value), None)
    if error is not None:
        where = '/'.join(str(part) for part in error.absolute_path)
        raise ValueError('Invalid task board' + (' at ' + where if where else '') + ': ' + error.message)
    # Character limits alone must not admit invalid Unicode transport strings.
    json.dumps(value, ensure_ascii=False).encode('utf-8')
    return value


def board_texts(board):
    """The board's own texts in order, each once: the summary, then each task's title and note."""
    texts = [board['summary']]
    for task in board['tasks']:
        texts.append(task['title'])
        if 'progress' in task:
            texts.append(task['progress'])
    return list(dict.fromkeys(texts))


def _stored_translations(value, target):
    """``{source: text}`` of a head value's translation when it is in ``target``."""
    stored = value.get('translation')
    if not stored or stored['target'] != target:
        return {}
    return {entry['source']: entry['text'] for entry in stored['texts']}


def _translation(target, translations, board):
    """``translations`` of ``board``'s texts in board order; None when there is none."""
    texts = [{'source': text, 'text': translations[text]}
             for text in board_texts(board) if text in translations]
    return {'target': target, 'texts': texts} if texts else None


def _head_key(owner, project):
    digest = hashlib.sha256(json.dumps(project, ensure_ascii=False).encode()).hexdigest()
    return str(owner) + ':' + digest


def plan_from_board(board):
    """The board as a checklist: its tasks in order without dropped ones; the summary explains it."""
    return {'todos': [{'content': task['title'], 'status': task['status']}
                      for task in board['tasks'] if task['status'] != 'cancelled'],
            'explanation': board['summary']}


def board_from_plan(previous, todos, explanation):
    """The board request a checklist write leaves; None when there is neither a board nor a plan.

    Each item becomes a task, and ValueError names the first one a board task
    cannot be. A task keeps its note while its title and status stay the same,
    and its ``due`` while its title stays: a commitment survives a checklist
    rewrite (docs/PROJECT_COORDINATION.md "Commitments on the task board").
    The summary stays; a first board takes ``explanation`` when it fits on one
    line, else the title of its first item in progress, else of its first item.
    """
    notes = {(task['title'], task['status']): task['progress']
             for task in (previous or {}).get('tasks', ()) if 'progress' in task}
    dues = {task['title']: task['due'] for task in (previous or {}).get('tasks', ()) if 'due' in task}
    tasks = []
    for number, item in enumerate(todos, 1):
        title, status = item['content'], item['status']
        if status not in _PLAN_STATUSES:
            raise ValueError(f'item {number} is {status}, which no task-board status shows: keep it '
                             'in_progress and say what it waits for in its note with update_task_progress')
        if '\n' in title or '\r' in title:
            raise ValueError(f'item {number} spans several lines; a task-board task is one line')
        if len(title) > _TITLE_MAX_CHARS:
            raise ValueError(f'item {number} has {len(title)} characters; '
                             f'a task-board task has at most {_TITLE_MAX_CHARS}')
        task = {'title': title, 'status': status}
        if (title, status) in notes:
            task['progress'] = notes[(title, status)]
        if title in dues:
            task['due'] = dues[title]
        tasks.append(task)
    if previous is not None:
        return {'summary': previous['summary'], 'tasks': tasks}
    if not tasks:
        return None
    line = ' '.join(explanation.split())
    summary = line if line and len(line) <= _SUMMARY_MAX_CHARS else next(
        (task['title'] for task in tasks if task['status'] == 'in_progress'), tasks[0]['title'])
    return {'summary': summary, 'tasks': tasks}


class CoordinatorTaskProgress:
    def __init__(self, owner, project, *, client=None, bindings=None, notify=None, clock=time.time,
                 arm=None):
        self.owner = require_user_id(owner, context='Coordinator task board')
        if not isinstance(project, str) or not project.strip() or len(project) > 4096:
            raise ValueError('Project path is required')
        self.project = normalize_project_path(project.strip())
        self.key = _head_key(self.owner, self.project)
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.bindings = bindings if bindings is not None else ProjectCoordinationRepository(self.owner, client=client)
        self.notify = notify if notify is not None else self._notify
        self.clock = clock
        self.arm = arm if arm is not None else self._arm

    def _scope(self):
        binding = self.bindings.read(self.project)
        if binding['availability'] != 'available':
            return None, binding
        return {'owner': self.owner, 'projectPath': self.project,
                'coordinatorId': binding['conversationId'], 'bindingRevision': binding['revision']}, binding

    def _head(self):
        row = self.client().query('record.get', {'namespace': POLICY['namespace'], 'key': self.key})
        if row is None:
            return None
        try:
            validate_progress(row['value'], 'progressHead')
            scope = row['value']['scope']
            if scope['owner'] != self.owner or scope['projectPath'] != self.project:
                raise ValueError('Wrong task board owner/project')
            if type(row['version']) is not int or row['version'] < 1:
                raise ValueError('Invalid record version')
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError('database_integrity', 'Invalid Coordinator task board record') from exc
        return row

    def publish(self, scope, request, *, translation_target=None):
        """Replace the board of exactly ``scope``'s binding; unchanged content is a no-op.

        ``translation_target`` is the language the human reads this Coordinator
        in, None without auto-translate. A changed board keeps the stored
        translations of the texts it still shows when they are in that language,
        so unchanged strings never flash back to the written language.
        """
        validate_progress(scope, 'reportScope')
        validate_progress(request, 'progressRequest')
        return self._publish(scope, lambda previous: request, translation_target)

    def publish_plan(self, scope, todos, explanation, *, translation_target=None):
        """``publish`` the board a checklist write leaves (``board_from_plan``).

        Without a board, an empty checklist writes nothing and returns no board.
        """
        validate_progress(scope, 'reportScope')
        return self._publish(scope, lambda previous: board_from_plan(previous, todos, explanation),
                             translation_target)

    def _publish(self, scope, build, translation_target):
        for _ in range(POLICY['casAttempts']):
            # A board racing a binding replacement must never reach the new team.
            if self._scope()[0] != scope:
                raise PermissionError('Coordinator binding changed')
            row = self._head()
            original = row['value'] if row else None
            if original and original['scope']['bindingRevision'] > scope['bindingRevision']:
                raise PermissionError('Coordinator binding changed')
            previous = original['board'] if original and original['scope'] == scope else None
            # Built from the head this attempt read, so a checklist keeps the notes it may keep.
            request = build(deepcopy(previous) if previous else None)
            if request is None:
                return {'board': None, 'changed': False}
            validate_progress(request, 'progressRequest')
            if previous and previous['summary'] == request['summary'] and previous['tasks'] == request['tasks']:
                return {'board': deepcopy(previous), 'changed': False}
            from lib.conversations.coordinator_commitments import (
                check_commitments, merge_commitments, next_commitments)
            now_seconds = self.clock()
            now = int(now_seconds * 1000)
            check_commitments(previous, request, now=now_seconds)
            commitments = merge_commitments(previous, request,
                                            (original or {}).get('commitments'), now=now)
            board = {'revision': previous['revision'] + 1 if previous else 1,
                     'updatedAt': now,
                     'summary': request['summary'], 'tasks': deepcopy(request['tasks'])}
            head = {'scope': scope, 'board': board}
            if commitments:
                head['commitments'] = commitments
            kept = (_translation(translation_target, _stored_translations(original, translation_target), board)
                    if previous else None)
            if kept:
                head['translation'] = kept
            head = validate_progress(head, 'progressHead')
            try:
                self.client(write=True).command('record.put', {
                    'namespace': POLICY['namespace'], 'key': self.key, 'value': head,
                    'expected_version': row['version'] if row else 0,
                }, 'coordinator-progress-cas:' + uuid.uuid4().hex)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
                # A lost acknowledgement re-reads its own board as unchanged.
                continue
            self._hint()
            moment = next_commitments(board, commitments, now=now)
            if moment is not None:
                self.arm(scope['owner'], scope, moment)
            return {'board': deepcopy(board), 'changed': True}
        raise StorageError('database_conflict', 'Task board changed concurrently; send the complete board again')

    def mark_commitments_woken(self, scope, commitments, *, via, command_id, disposition):
        """Receipt one due wake in the same head, so that due time never wakes again.

        Only the (title, dueAt) pairs actually woken are receipted. The board is
        unchanged, and a commitment re-dated afterwards carries no receipt for
        its new due time.
        """
        validate_progress(scope, 'reportScope')
        now = int(self.clock() * 1000)
        for _ in range(POLICY['casAttempts']):
            row = self._head()
            if row is None or row['value']['scope'] != scope:
                return False
            head = deepcopy(row['value'])
            receipts = {item['title']: item for item in head.get('commitments', ())}
            for due in commitments:
                receipt = receipts.get(due['title'])
                if receipt is None or receipt['state'] != 'open' or receipt['dueAt'] != due['dueAt']:
                    continue
                if receipt.get('wake', {}).get('dueAt') == due['dueAt']:
                    continue
                receipt['wake'] = {'dueAt': due['dueAt'], 'at': now, 'via': via,
                                   'commandId': command_id, 'disposition': disposition}
            if head == row['value']:
                return False
            validate_progress(head, 'progressHead')
            try:
                self.client(write=True).command('record.put', {
                    'namespace': POLICY['namespace'], 'key': self.key, 'value': head,
                    'expected_version': row['version'],
                }, 'coordinator-commitment-wake:' + uuid.uuid4().hex)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
                continue
            return True
        raise StorageError('database_conflict', 'Task board changed concurrently; wake not receipted')

    def _arm(self, owner, scope, moment):
        if moment is None:
            return
        from lib.conversations.coordinator_commitments import arm_due_commitments
        try:
            arm_due_commitments(owner, scope, moment)
        except Exception:
            # The board is committed; the next board write arms the same row.
            logger.warning('[Commitments] due wake of %s not armed now; the next board write retries',
                           scope['coordinatorId'], exc_info=True)

    def untranslated_texts(self, scope, target):
        """Texts of exactly ``scope``'s board without a ``target`` translation, in one storage read."""
        row = self._head()
        if row is None or row['value']['scope'] != scope:
            return []
        known = _stored_translations(row['value'], target)
        return [text for text in board_texts(row['value']['board']) if text not in known]

    def attach_translation(self, scope, target, texts):
        """Merge ``{board text: translation}`` into exactly ``scope``'s current board.

        Returns False without writing when the head belongs to another binding or
        nothing would change. Another ``target`` replaces the stored translations;
        a text the board no longer shows is dropped. Whitespace runs collapse to
        one space; empty or over-long translations are skipped.
        """
        for _ in range(POLICY['casAttempts']):
            row = self._head()
            if row is None or row['value']['scope'] != scope:
                return False
            board = row['value']['board']
            merged = _stored_translations(row['value'], target)
            for source, text in texts.items():
                normalized = ' '.join(text.split())
                if normalized and len(normalized) <= _TRANSLATION_MAX_CHARS:
                    merged[source] = normalized
            translation = _translation(target, merged, board)
            if translation == row['value'].get('translation'):
                return False
            head = {'scope': scope, 'board': board}
            if translation:
                head['translation'] = translation
            if row['value'].get('commitments'):
                head['commitments'] = deepcopy(row['value']['commitments'])
            validate_progress(head, 'progressHead')
            try:
                self.client(write=True).command('record.put', {
                    'namespace': POLICY['namespace'], 'key': self.key, 'value': head,
                    'expected_version': row['version'],
                }, 'coordinator-progress-translation-cas:' + uuid.uuid4().hex)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
                # A board replaced meanwhile keeps only translations of texts it still shows.
                continue
            self._hint()
            return True
        raise StorageError('database_conflict', 'Task board changed concurrently; translation not attached')

    def read(self):
        scope, binding = self._scope()
        result = {'projectPath': self.project, 'conversationId': binding['conversationId'],
                  'bindingRevision': binding['revision'], 'board': None}
        if scope is not None:
            row = self._head()
            if row and row['value']['scope'] == scope:
                result['board'] = deepcopy(row['value']['board'])
                if 'translation' in row['value']:
                    result['translation'] = deepcopy(row['value']['translation'])
                if row['value'].get('commitments'):
                    result['commitments'] = deepcopy(row['value']['commitments'])
        return validate_progress(result, 'progressState')

    def _hint(self):
        try:
            self.notify()
        except Exception:
            # Push is a refresh hint; the committed head is the board.
            logger.debug('Coordinator task board push unavailable', exc_info=True)

    def _notify(self):
        # Reaches only sockets of the writing process: a write by the resident
        # execution host refreshes browsers through its round (``progress_view``).
        from lib.agent_core.push import push_event
        push_event('project', project_channel_key(self.project),
                   {'projectKey': self.project, 'kind': 'coordinator_progress'}, user_id=self.owner)


class ProgressReceipt(str):
    """Model text carrying the browser's ``progress_view`` of the same write out of band."""

    def __new__(cls, text, view):
        result = super().__new__(cls, text)
        result.progress_view = view
        return result


def _board_hint(scope, board):
    return {'projectPath': scope['projectPath'], 'conversationId': scope['coordinatorId'],
            'bindingRevision': scope['bindingRevision'], 'revision': board['revision']}


def progress_view(scope, board, state):
    """Round metadata of one progress write: the checklist and the board revision it shows.

    The round reaches the open Coordinator chat through the conversation stream,
    which every process's commits wake, so both views refresh without the push.
    """
    from lib.tools.todo import public_todo_state
    state = public_todo_state(state)
    return {'todos': deepcopy(state['todos']), 'todoState': state,
            'coordinatorProgress': _board_hint(scope, board)}


def coordinator_board_scope(task):
    """``reportScope`` of ``task`` when it is the bound Coordinator keeping a task board, else None.

    The Router Watchdog is bound but keeps no board. A Coordinator's role is
    pinned for its turn before any tool runs, so a failed lookup is another
    task's and keeps its own checklist; ended authority still stops the task.
    """
    from lib.llm.llm_errors import AbortedError
    from lib.tasks_pkg.coordinator_policy import coordinator_scope, coordinator_tool_ceiling
    try:
        scope = coordinator_scope(task)
        if not scope or 'update_task_progress' not in coordinator_tool_ceiling(scope, task):
            return None
    except AbortedError:
        raise
    except Exception:
        logger.warning('[Coordinator] task board role lookup failed; task=%s keeps its own checklist',
                       task.get('id'), exc_info=True)
        return None
    return {'owner': scope['user_id'], 'projectPath': scope['project_path'],
            'coordinatorId': scope['conversation_id'], 'bindingRevision': scope['binding_revision']}


def _translate(task, scope, publish):
    """Run ``publish(target)`` for the human's reading language, then queue the board's translation.

    Also after an unchanged board: that retries a translation that failed before.
    """
    from lib.conversations.coordinator_progress_translation import (
        progress_translation_target, schedule_progress_translation)
    target = progress_translation_target(task, scope)
    published = publish(target)
    if target is not None and published['board'] is not None:
        schedule_progress_translation(scope['owner'], scope['projectPath'], scope, target)
    return published


def publish_task_board(task, scope, request):
    """update_task_progress: publish the board, then hold its plan as the task's checklist."""
    from lib.tools.todo import adopt_todo_projection, todo_lock
    with todo_lock(task):
        published = _translate(task, scope, lambda target: CoordinatorTaskProgress(
            scope['owner'], scope['projectPath']).publish(scope, request, translation_target=target))
        plan = plan_from_board(published['board'])
        state = adopt_todo_projection(task, plan['todos'], plan['explanation'])
    return published, state


def plan_publication(task):
    """``publish`` for ``apply_todo_write_to_task`` when the checklist is a task board, else None.

    It writes each accepted checklist as the board; a write the board refuses
    raises ValueError naming the cause, so the checklist and board stay one.
    """
    scope = coordinator_board_scope(task)
    if scope is None:
        return None

    def publish(state):
        try:
            published = _translate(task, scope, lambda target: CoordinatorTaskProgress(
                scope['owner'], scope['projectPath']).publish_plan(
                    scope, state['todos'], state['explanation'], translation_target=target))
        except PermissionError as error:
            raise ValueError(f'{error}; start a new authorized turn') from error
        except StorageError as error:
            raise ValueError(f'the task board was not saved ({error}); send the checklist again') from error
        except ValueError as error:
            raise ValueError(f'this checklist is the task board shown to the user, and {error}') from error
        if published['board'] is None:
            return {}
        return {'note': 'The user sees it as the task board above this chat.',
                'coordinatorProgress': _board_hint(scope, published['board'])}
    return publish

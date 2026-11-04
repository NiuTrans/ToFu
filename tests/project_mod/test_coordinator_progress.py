"""The Coordinator task board is one validated, binding-scoped, idempotent head.

Its display translation rides on that head: carried forward for texts a changed
board still shows, merged per text into the board the human currently sees, and
derived by an optional job that never fails or blocks the board.
"""
from copy import deepcopy
import json
import logging
from types import SimpleNamespace
import threading

import pytest

from lib.conversations.coordinator_progress import (
    CoordinatorTaskProgress, POLICY, board_texts, validate_progress)
from lib.conversations.coordinator_progress_translation import schedule_progress_translation
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


class Store:
    def __init__(self):
        self.rows = {}; self.lock = threading.Lock(); self.lost_ack = False; self.puts = 0; self.reads = 0
        # Runs once before the next CAS check, like a writer racing that write.
        self.before_put = None

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        assert operation == 'record.get'
        self.reads += 1
        return deepcopy(self.rows.get((payload['namespace'], payload['key'])))

    def command(self, operation, payload, command_id):
        assert operation == 'record.put'
        if self.before_put is not None:
            race, self.before_put = self.before_put, None
            race()
        identity = (payload['namespace'], payload['key'])
        with self.lock:
            old = self.rows.get(identity)
            if (old['version'] if old else 0) != payload['expected_version']:
                raise StorageError('database_conflict', 'CAS conflict')
            self.rows[identity] = {'value': deepcopy(payload['value']), 'version': payload['expected_version'] + 1}
            self.puts += 1
            if self.lost_ack:
                self.lost_ack = False
                raise StorageError('database_unavailable', 'ACK lost', retryable=True)
            return {'version': payload['expected_version'] + 1}


@pytest.fixture
def board():
    store = Store(); notices = []
    binding = {'availability': 'available', 'conversationId': 'coordinator', 'revision': 1}
    bindings = SimpleNamespace(read=lambda path: deepcopy(binding))
    clock = iter(range(1000, 10_000, 10))

    def factory(owner=7, path='/project'):
        return CoordinatorTaskProgress(owner, path, client=store, bindings=bindings,
                                       notify=lambda: notices.append('push'), clock=lambda: next(clock))
    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    request = {'summary': 'The folder fix is verified; the trip research is underway.', 'tasks': [
        {'title': 'Fix folder creation in the sidebar', 'status': 'completed', 'progress': 'Merged and verified.'},
        {'title': 'Research New Year homestays', 'status': 'in_progress', 'progress': 'Comparing four platforms.'},
        {'title': 'Summarize the WeChat article', 'status': 'pending'},
    ]}
    return SimpleNamespace(store=store, binding=binding, factory=factory, scope=scope,
                           request=request, notices=notices,
                           head=lambda: deepcopy(next(iter(store.rows.values()))['value']))


def _translated(texts):
    return {text: '译:' + text for text in texts}


def test_board_round_trips_for_the_current_binding_only(board):
    b = board
    assert b.factory().read() == {'projectPath': '/project', 'conversationId': 'coordinator',
                                  'bindingRevision': 1, 'board': None}
    published = b.factory().publish(b.scope, b.request)
    assert published['changed'] and published['board']['revision'] == 1
    state = b.factory().read()
    assert state['board']['summary'] == b.request['summary']
    assert state['board']['tasks'] == b.request['tasks'] and state['board']['updatedAt'] > 0
    assert b.notices == ['push']
    # A new Coordinator binding starts with an empty board; the old one never leaks.
    b.binding['revision'] = 2
    assert b.factory().read()['board'] is None
    with pytest.raises(PermissionError):
        b.factory().publish(b.scope, b.request)
    newer = dict(b.scope, bindingRevision=2)
    assert b.factory().publish(newer, {'summary': 'Fresh start.', 'tasks': []})['board']['revision'] == 1
    # An older binding cannot overwrite the newer team's board even if it is current again.
    b.binding['revision'] = 1
    with pytest.raises(PermissionError):
        b.factory().publish(b.scope, b.request)


def test_unchanged_board_is_a_quiet_noop_and_revisions_advance(board):
    b = board
    b.factory().publish(b.scope, b.request)
    again = b.factory().publish(b.scope, deepcopy(b.request))
    assert not again['changed'] and again['board']['revision'] == 1
    assert b.store.puts == 1 and b.notices == ['push']
    changed = deepcopy(b.request); changed['tasks'][1]['status'] = 'completed'
    assert b.factory().publish(b.scope, changed)['board']['revision'] == 2
    assert b.factory().read()['board']['tasks'][1]['status'] == 'completed'


def test_lost_acknowledgement_replays_without_a_second_revision(board):
    b = board
    b.store.lost_ack = True
    published = b.factory().publish(b.scope, b.request)
    assert published['board']['revision'] == 1 and b.store.puts == 1


@pytest.mark.parametrize('request_change', [
    {'summary': ''},
    {'summary': 'two\nlines'},
    {'tasks': [{'title': 'x', 'status': 'blocked'}]},
    {'tasks': [{'title': 'x', 'status': 'pending', 'owner': 'worker-1'}]},
    {'tasks': [{'title': 'x' * 121, 'status': 'pending'}]},
    {'tasks': [{'title': 'x', 'status': 'pending', 'progress': ' '}]},
    {'tasks': [{'title': str(index), 'status': 'pending'} for index in range(25)]},
])
def test_invalid_boards_are_refused_with_the_failing_field(board, request_change):
    b = board
    with pytest.raises(ValueError, match='Invalid task board'):
        b.factory().publish(b.scope, {**b.request, **request_change})
    assert b.store.puts == 0


def test_unbound_project_reads_empty_and_corrupt_heads_fail_closed(board):
    b = board
    b.factory().publish(b.scope, b.request)
    key = next(iter(b.store.rows))
    assert key[0] == POLICY['namespace']
    b.store.rows[key]['value']['scope']['owner'] = 8
    with pytest.raises(StorageError):
        b.factory().read()
    b.binding.update(availability='unbound', conversationId='', revision=3)
    assert b.factory().read() == {'projectPath': '/project', 'conversationId': '',
                                  'bindingRevision': 3, 'board': None}


def _bind_default_progress(monkeypatch, b, notify):
    """Production's port-less construction uses the fake head; explicit ports still win."""
    original = CoordinatorTaskProgress.__init__
    defaults = {'client': b.store, 'bindings': SimpleNamespace(read=lambda _: deepcopy(b.binding)),
                'notify': notify}
    monkeypatch.setattr(CoordinatorTaskProgress, '__init__', lambda self, owner, path, **ports: original(
        self, owner, path, **{**defaults, **ports}))


@pytest.fixture
def coordinator_tool(board, monkeypatch):
    """The real tool branch over the fake head, stored Coordinator settings and a recorded scheduler."""
    from lib.conversations import coordinator_progress_translation, coordinator_tools
    from lib.conversations.repository import ConversationRepository
    b = board
    monkeypatch.setattr(coordinator_tools, '_scope', lambda task: {
        'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator', 'binding_revision': 1})
    _bind_default_progress(monkeypatch, b, lambda: b.notices.append('push'))
    targets, scheduled, settings = [], [], {}
    original_publish = CoordinatorTaskProgress.publish

    def publish(self, scope, request, *, translation_target=None):
        targets.append(translation_target)
        return original_publish(self, scope, request, translation_target=translation_target)

    def stored(self, conversation_id, *, user_id, include_messages=True, **_):
        assert (conversation_id, user_id, include_messages) == ('coordinator', 7, False)
        return SimpleNamespace(metadata={'title': 'Coordinator', 'settings': deepcopy(settings)})

    monkeypatch.setattr(CoordinatorTaskProgress, 'publish', publish)
    monkeypatch.setattr(ConversationRepository, 'get', stored)
    monkeypatch.setattr(coordinator_progress_translation, 'schedule_progress_translation',
                        lambda *args: scheduled.append(args) or True)

    def run(task, arguments=None):
        return json.loads(coordinator_tools.execute_coordinator_tool(
            task, 'update_task_progress', b.request if arguments is None else arguments))
    return SimpleNamespace(run=run, targets=targets, scheduled=scheduled, settings=settings)


def test_coordinator_tool_publishes_through_the_bound_scope(coordinator_tool):
    result = coordinator_tool.run({})
    assert result['revision'] == 1 and result['task_count'] == 3 and result['changed'] is True
    refused = coordinator_tool.run({}, {'summary': 'ok', 'tasks': [{'title': 'x', 'status': 'waiting_on_user'}]})
    assert 'Invalid task board at tasks/0/status' in refused['error'] and refused['execution_started'] is False


def test_board_texts_are_the_summary_then_each_title_and_note_once(board):
    assert board_texts(board.request) == [
        'The folder fix is verified; the trip research is underway.',
        'Fix folder creation in the sidebar', 'Merged and verified.',
        'Research New Year homestays', 'Comparing four platforms.', 'Summarize the WeChat article']
    repeated = {'summary': 'Waiting on you.', 'tasks': [
        {'title': 'Book a homestay', 'status': 'pending', 'progress': 'Waiting on you.'},
        {'title': 'Book a homestay', 'status': 'cancelled'}]}
    assert board_texts(repeated) == ['Waiting on you.', 'Book a homestay']


def test_a_changed_board_keeps_translations_of_the_texts_it_still_shows(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert 'translation' not in b.head()
    assert b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    changed = deepcopy(b.request)
    changed['tasks'][1].update(status='completed', progress='Booked the quietest one.')
    del changed['tasks'][2]
    assert b.factory().publish(b.scope, changed, translation_target='Chinese')['changed']
    head = b.head()
    kept = [text for text in board_texts(changed) if text != 'Booked the quietest one.']
    assert head['board']['revision'] == 2 and len(kept) == 4
    assert head['translation'] == {'target': 'Chinese', 'texts': [
        {'source': text, 'text': '译:' + text} for text in kept]}
    validate_progress(head, 'progressHead')


@pytest.mark.parametrize('target', ['Japanese', None])
def test_translations_are_not_carried_into_another_language_or_without_auto_translate(board, target):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    changed = dict(b.request, summary='The trip research is underway.')
    assert b.factory().publish(b.scope, changed, translation_target=target)['changed']
    assert 'translation' not in b.head() and 'translation' not in b.factory().read()


def test_a_replaced_binding_never_inherits_translations(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    b.binding['revision'] = 2
    b.factory().publish(dict(b.scope, bindingRevision=2), b.request, translation_target='Chinese')
    assert 'translation' not in b.head()


def test_attach_merges_normalized_translations_of_the_current_board_texts(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    summary = b.request['summary']
    assert b.factory().attach_translation(b.scope, 'Chinese', {
        summary: '  文件夹修复已验证；\n行程调研\t 进行中。 ',
        'Fix folder creation in the sidebar': '修复侧边栏的文件夹创建',
        'Research New Year homestays': '长' * 560,
        'Comparing four platforms.': '长' * 561,
        'Merged and verified.': ' \n ',
        'A text this board never showed': '不应保存',
    })
    assert b.head()['translation'] == {'target': 'Chinese', 'texts': [
        {'source': summary, 'text': '文件夹修复已验证； 行程调研 进行中。'},
        {'source': 'Fix folder creation in the sidebar', 'text': '修复侧边栏的文件夹创建'},
        {'source': 'Research New Year homestays', 'text': '长' * 560}]}
    assert b.notices == ['push', 'push']
    # A later pass adds to the stored entries, kept in board order.
    assert b.factory().attach_translation(b.scope, 'Chinese', {'Merged and verified.': '已合并并验证。'})
    assert [entry['source'] for entry in b.head()['translation']['texts']] == [
        summary, 'Fix folder creation in the sidebar', 'Merged and verified.', 'Research New Year homestays']
    # What is already stored is neither written nor pushed again.
    puts = b.store.puts
    assert not b.factory().attach_translation(b.scope, 'Chinese', {'Merged and verified.': ' 已合并并验证。\n'})
    assert not b.factory().attach_translation(b.scope, 'Chinese', {})
    assert b.store.puts == puts and len(b.notices) == 3
    validate_progress(b.head(), 'progressHead')


def test_attach_refuses_a_missing_head_and_another_bindings_board(board):
    b = board
    summary = b.request['summary']
    assert not b.factory().attach_translation(b.scope, 'Chinese', {summary: '摘要'})
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    b.binding['revision'] = 2
    b.factory().publish(dict(b.scope, bindingRevision=2), b.request, translation_target='Chinese')
    # A job of the replaced binding cannot translate the newer team's board.
    assert not b.factory().attach_translation(b.scope, 'Chinese', {summary: '摘要'})
    assert 'translation' not in b.head() and b.store.puts == 2 and b.notices == ['push', 'push']


def test_another_target_replaces_the_stored_translations(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    summary = b.request['summary']
    assert b.factory().attach_translation(b.scope, 'Japanese', {summary: 'フォルダの修正を確認しました。'})
    assert b.head()['translation'] == {'target': 'Japanese', 'texts': [
        {'source': summary, 'text': 'フォルダの修正を確認しました。'}]}
    assert b.factory().untranslated_texts(b.scope, 'Japanese') == board_texts(b.request)[1:]
    assert b.factory().untranslated_texts(b.scope, 'Chinese') == board_texts(b.request)


def test_read_projects_the_translation_of_the_current_board_only(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert 'translation' not in b.factory().read()
    b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    state = b.factory().read()
    assert state['board'] == b.head()['board'] and state['translation'] == b.head()['translation']
    b.binding['revision'] = 2
    assert b.factory().read() == {'projectPath': '/project', 'conversationId': 'coordinator',
                                  'bindingRevision': 2, 'board': None}


@pytest.mark.parametrize('corrupt', [
    lambda translation: translation['texts'][0].update(text='two\nlines'),
    lambda translation: translation['texts'][0].update(text='长' * 561),
    lambda translation: translation.update(texts=translation['texts'] * 9),
    lambda translation: translation.update(owner='worker-1'),
])
def test_stored_translations_are_schema_checked_and_fail_closed(board, corrupt):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    validate_progress(b.head(), 'progressHead')
    corrupt(next(iter(b.store.rows.values()))['value']['translation'])
    with pytest.raises(StorageError):
        b.factory().read()


def test_a_translation_racing_a_board_change_lands_only_on_texts_still_shown(board):
    b = board
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    changed = dict(b.request, summary='Everything is done.')
    b.store.before_put = lambda: b.factory().publish(b.scope, changed, translation_target='Chinese')
    assert b.factory().attach_translation(b.scope, 'Chinese', _translated(board_texts(b.request)))
    head = b.head()
    assert head['board']['summary'] == 'Everything is done.'
    assert [entry['source'] for entry in head['translation']['texts']] == board_texts(changed)[1:]


_PROGRESS_SCOPE = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}


def test_auto_translating_coordinator_publishes_for_its_reading_language(coordinator_tool):
    t = coordinator_tool
    task = {'config': {'autoTranslate': True, 'uiLang': 'zh'}}
    result = t.run(task)
    assert sorted(result) == ['changed', 'next_step', 'revision', 'task_count'] and result['changed']
    assert t.targets == ['Chinese'] and t.scheduled == [(7, '/project', _PROGRESS_SCOPE, 'Chinese')]
    # An unchanged board schedules again: that retries a translation that failed before.
    assert t.run(task)['changed'] is False
    assert t.targets == ['Chinese', 'Chinese'] and len(t.scheduled) == 2


def test_coordinator_without_auto_translate_publishes_untranslated(coordinator_tool):
    t = coordinator_tool
    # The turn's explicit choice wins over the stored settings.
    t.settings.update(autoTranslate=True, uiLang='ja')
    t.run({'config': {'autoTranslate': False, 'uiLang': 'zh'}})
    t.settings.clear()
    t.run({'config': {}})
    assert t.targets == [None, None] and t.scheduled == []


def test_automatic_turns_follow_the_conversations_reading_language(coordinator_tool):
    """A dispatch continuation's config pins autoTranslate off for its synthetic input."""
    from lib.project_dispatch.admission import KEY
    t = coordinator_tool
    t.settings.update(autoTranslate=True, uiLang='ja')
    t.run({'config': {KEY: {'assignmentId': 'continuation'}, 'autoTranslate': False}})
    t.run({'config': {'model': 'without-translation-keys'}})
    assert t.targets == ['Japanese', 'Japanese']
    assert [call[3] for call in t.scheduled] == ['Japanese', 'Japanese']


@pytest.fixture
def board_job(board, monkeypatch):
    """Scheduled board jobs run inline on the fake head with a recorded translator."""
    from lib.conversations import coordinator_progress_translation
    from lib.translate import display_texts, execution
    b = board
    _bind_default_progress(monkeypatch, b, lambda: b.notices.append('push'))
    jobs, calls, warnings = [], [], []

    def submit(job_id, *, owner_user_id, function):
        jobs.append((job_id, owner_user_id))
        function()

    def translate(texts, target, *, log_tag, abort_check=None):
        calls.append((list(texts), target))
        return _translated(texts)

    class Warnings(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())

    monkeypatch.setattr(execution, 'submit_reconstructible_translation', submit)
    monkeypatch.setattr(display_texts, 'translate_display_texts', translate)
    handler = Warnings(logging.WARNING)
    coordinator_progress_translation.logger.addHandler(handler)
    yield SimpleNamespace(jobs=jobs, calls=calls, warnings=warnings)
    coordinator_progress_translation.logger.removeHandler(handler)


def test_board_job_translates_only_missing_texts_and_hints(board, board_job):
    b, j = board, board_job
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert schedule_progress_translation(7, '/project', b.scope, 'Chinese') is True
    assert j.calls == [(board_texts(b.request), 'Chinese')]
    assert b.factory().read()['translation'] == {'target': 'Chinese', 'texts': [
        {'source': text, 'text': '译:' + text} for text in board_texts(b.request)]}
    assert b.notices == ['push', 'push'] and not j.warnings
    # Fully translated: an unchanged re-publish costs one storage read and no provider work.
    reads = b.store.reads
    assert schedule_progress_translation(7, '/project', b.scope, 'Chinese')
    assert b.store.reads == reads + 1 and len(j.calls) == 1
    # After a change only the new text is sent; the others were carried forward.
    changed = deepcopy(b.request)
    changed['tasks'][2].update(status='in_progress', progress='Reading it now.')
    b.factory().publish(b.scope, changed, translation_target='Chinese')
    schedule_progress_translation(7, '/project', b.scope, 'Chinese')
    assert j.calls[-1] == (['Reading it now.'], 'Chinese')
    assert len(b.factory().read()['translation']['texts']) == len(board_texts(changed))
    # Each schedule is its own lane job, so a queued or running one never swallows a later publish.
    assert len({job_id for job_id, _ in j.jobs}) == len(j.jobs) == 3
    assert {owner for _, owner in j.jobs} == {7}


def test_board_job_of_a_replaced_binding_writes_nothing(board, board_job):
    b, j = board, board_job
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert schedule_progress_translation(7, '/project', dict(b.scope, coordinatorId='replaced'), 'Chinese')
    assert j.calls == [] and b.store.puts == 1 and b.notices == ['push'] and not j.warnings


def test_board_job_failure_is_logged_and_leaves_the_board_as_written(board, board_job, monkeypatch):
    from lib.translate import display_texts
    b, j = board, board_job

    def unavailable(texts, target, *, log_tag, abort_check=None):
        raise RuntimeError('translation provider unavailable')

    monkeypatch.setattr(display_texts, 'translate_display_texts', unavailable)
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert schedule_progress_translation(7, '/project', b.scope, 'Chinese') is True
    assert 'translation' not in b.factory().read() and b.store.puts == 1
    assert len(j.warnings) == 1 and all(part in j.warnings[0] for part in (
        'owner=7', 'project=/project', 'coordinator=coordinator', 'target=Chinese'))


def test_refused_lane_admission_is_logged_not_raised(board, board_job, monkeypatch):
    from lib.translate import execution
    b, j = board, board_job

    def refuse(job_id, *, owner_user_id, function):
        raise RuntimeError('cannot submit after lane shutdown')

    monkeypatch.setattr(execution, 'submit_reconstructible_translation', refuse)
    assert schedule_progress_translation(7, '/project', b.scope, 'Chinese') is False
    assert len(j.warnings) == 1 and 'coordinator=coordinator' in j.warnings[0]


def test_board_job_runs_on_the_shared_translation_lane(board, monkeypatch):
    from lib.translate import display_texts
    b = board
    attached = threading.Event()
    _bind_default_progress(monkeypatch, b, attached.set)
    monkeypatch.setattr(display_texts, 'translate_display_texts',
                        lambda texts, target, *, log_tag, abort_check=None: _translated(texts))
    b.factory().publish(b.scope, b.request, translation_target='Chinese')
    assert schedule_progress_translation(7, '/project', b.scope, 'Chinese') is True
    assert attached.wait(30)
    assert b.factory().read()['translation']['texts'][0]['text'] == '译:' + b.request['summary']

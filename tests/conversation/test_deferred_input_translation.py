"""Accepted human input is durable at once; its attempt owns send translation.

The turn command never waits on a translator. The accepted attempt's start
translates the authored text while the executor starts and never writes. The
worker joins the result, records the input Turn's verdict as its only writer
and substitutes it before any runner reads its model view, so authority pinned
to the input Turn's revision afterwards observes the final Turn.
"""

from __future__ import annotations

from concurrent.futures import Future
import logging
import threading
import time

import pytest

CONV = 'conv-deferred-input-translation'
_SEPARATOR = '\n\n' + '═' * 50


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


# ── HTTP acceptance ───────────────────────────────────────────────────

@pytest.fixture()
def conversation_authority(flask_client):
    from tests._seed import delete_conversation, seed_conversation

    delete_conversation(CONV, user_id=1)
    seed_conversation(CONV, user_id=1, title='deferred input translation')
    try:
        yield
    finally:
        from lib.runtime.runtime_state_store import reset_for_test
        delete_conversation(CONV, user_id=1)
        reset_for_test()


def _capture_admission(monkeypatch):
    from tests.support.conversation_workers import observe_admissions
    return observe_admissions(monkeypatch)


@pytest.mark.api
@pytest.mark.auth_mode('open')
def test_turn_command_persists_authored_text_without_waiting_on_translation(
        flask_client, conversation_authority, monkeypatch):
    import lib.chat.turn_builder as turn_builder
    from lib.turn_lifecycle import list_turns

    monkeypatch.setattr(
        turn_builder, 'auto_translate_user',
        lambda *args, **kwargs: pytest.fail('the turn command waited on a translator'))
    captured = _capture_admission(monkeypatch)

    response = flask_client.post(f'/api/v4/conversations/{CONV}/turns', json={
        'commandId': 'cmd-deferred',
        'message': {'text': '请帮我修复这个问题'},
        'config': {'model': 'gpt-4o', 'autoTranslate': True},
    })

    assert response.status_code == 200, response.get_json()
    assert captured[0]['config']['_deferredInputTranslation'] is True
    authored = [turn for turn in list_turns(CONV, user_id=1)['turns']
                if turn['actor'] == 'human']
    assert authored[0]['projection']['content'] == '请帮我修复这个问题'
    assert 'originalContent' not in authored[0]['projection']


@pytest.mark.api
@pytest.mark.auth_mode('open')
def test_client_cannot_assign_a_translation_obligation(
        flask_client, conversation_authority, monkeypatch):
    captured = _capture_admission(monkeypatch)

    response = flask_client.post(f'/api/v4/conversations/{CONV}/turns', json={
        'commandId': 'cmd-forged',
        'message': {'text': 'hello'},
        'config': {'model': 'gpt-4o', '_deferredInputTranslation': True},
    })

    assert response.status_code == 200, response.get_json()
    assert '_deferredInputTranslation' not in captured[0]['config']


# ── Input verdict writer (real storage) ──────────────────────────────

@pytest.fixture()
def authored_input(flask_client):
    from tests._seed import delete_conversation, seed_conversation
    from lib.turn_lifecycle import list_turns

    delete_conversation(CONV, user_id=1)
    seed_conversation(CONV, user_id=1, messages=[{'role': 'user', 'content': '原文'}])
    try:
        yield list_turns(CONV, user_id=1)['turns'][0]
    finally:
        from lib.runtime.runtime_state_store import reset_for_test
        delete_conversation(CONV, user_id=1)
        reset_for_test()


@pytest.mark.api
def test_first_input_verdict_wins_and_later_verdicts_never_rewrite(authored_input):
    from lib.translate.commit import record_input_translation_verdict
    from lib.turn_lifecycle import get_turn

    turn_id = authored_input['turnId']
    first = record_input_translation_verdict(
        CONV, turn_id, authored_text='原文', translated_text='Translated',
        model='model-x', user_id=1)

    assert first['projectionRevision'] == authored_input['projectionRevision'] + 1
    projection = first['projection']
    assert (projection['content'], projection['originalContent']) == ('Translated', '原文')
    assert projection['translation'] == {'status': 'completed', 'model': 'model-x'}
    for late in ({'translated_text': 'Other'}, {'failure_reason': 'timed_out'}):
        assert record_input_translation_verdict(
            CONV, turn_id, authored_text='原文', user_id=1, **late) == first
    assert get_turn(CONV, turn_id, user_id=1)['projectionRevision'] == first['projectionRevision']


@pytest.mark.api
def test_input_verdict_never_overwrites_an_edited_input(authored_input):
    from lib.translate.commit import record_input_translation_verdict
    from lib.turn_lifecycle import get_turn, update_turn_projection

    turn_id = authored_input['turnId']
    edited = update_turn_projection(
        CONV, turn_id, projection={**authored_input['projection'], 'content': '改过的请求'},
        expected_projection_revision=authored_input['projectionRevision'], user_id=1)['turn']

    for verdict in ({'translated_text': 'Translated'}, {'failure_reason': 'timed_out'}):
        assert record_input_translation_verdict(
            CONV, turn_id, authored_text='原文', user_id=1, **verdict) == edited
    assert get_turn(CONV, turn_id, user_id=1)['projection']['content'] == '改过的请求'


# ── Attempt-start translation job ─────────────────────────────────────

def _turns(monkeypatch, projection):
    import lib.turn_lifecycle as lifecycle
    # lib.translate.commit binds get_turn when first imported; a first import
    # under this stub would keep it after monkeypatch restores lifecycle.
    import lib.translate.commit  # noqa: F401

    rows = {
        'out': {'turnId': 'out', 'parentTurnId': 'in', 'projection': {}},
        'in': {'turnId': 'in', 'projectionRevision': 3, 'projection': projection},
    }
    monkeypatch.setattr(lifecycle, 'get_turn',
                        lambda conv, turn_id, *, user_id: rows[turn_id])


def _forbid_writes(monkeypatch):
    import lib.translate.commit as commit

    def write(*args, **kwargs):
        pytest.fail('the attempt-start job wrote the input Turn')

    for name in ('record_input_translation_verdict', 'commit_translation_to_turn',
                 'mark_turn_translation_failed'):
        monkeypatch.setattr(commit, name, write)


@pytest.mark.unit
def test_job_translates_without_writing_the_input_turn(monkeypatch):
    from lib.chat import turn_builder

    _turns(monkeypatch, {'content': '原文'})
    _forbid_writes(monkeypatch)
    monkeypatch.setattr(
        turn_builder, 'auto_translate_user',
        lambda text, config, *, user_id, conv_id: ('Translated', text, 'model-x', None))

    future = turn_builder.start_deferred_input_translation(
        'conv-1', 'out', {'autoTranslate': True}, user_id=7)

    assert future.result(timeout=5) == turn_builder.DeferredInputTranslation(
        'in', 3, '原文', 'Translated', 'model-x')


@pytest.mark.unit
@pytest.mark.parametrize('verdict', [
    {'translation': {'status': 'completed', 'model': 'model-x'}},
    {'_translateDone': True, '_translateModel': 'model-x'},
])
def test_job_reuses_a_committed_verdict_without_translating_again(monkeypatch, verdict):
    from lib.chat import turn_builder

    _turns(monkeypatch, {'content': 'Translated', 'originalContent': '原文', **verdict})
    _forbid_writes(monkeypatch)
    monkeypatch.setattr(
        turn_builder, 'auto_translate_user',
        lambda *args, **kwargs: pytest.fail('a committed verdict was translated again'))

    future = turn_builder.start_deferred_input_translation(
        'conv-1', 'out', {'autoTranslate': True}, user_id=7)

    assert future.result(timeout=5) == turn_builder.DeferredInputTranslation(
        'in', 3, '原文', 'Translated', recorded=True)


@pytest.mark.unit
@pytest.mark.parametrize('fail, expected', [
    ('timed_out', {'failure': 'timed_out'}),
    (None, {'recorded': True}),
])
def test_job_reports_an_untranslated_input_without_writing(monkeypatch, fail, expected):
    from lib.chat import turn_builder

    _turns(monkeypatch, {'content': '原文'})
    _forbid_writes(monkeypatch)
    monkeypatch.setattr(
        turn_builder, 'auto_translate_user',
        lambda text, config, *, user_id, conv_id: (text, None, None, fail))

    future = turn_builder.start_deferred_input_translation(
        'conv-1', 'out', {'autoTranslate': True}, user_id=7)

    assert future.result(timeout=5) == turn_builder.DeferredInputTranslation(
        'in', 3, '原文', None, **expected)


@pytest.mark.unit
def test_attempt_start_begins_translation_before_model_context(monkeypatch):
    import lib.chat.turn_builder as turn_builder
    import lib.conversation_sync.task_start as task_start

    order = []
    pending = Future()
    monkeypatch.setattr(
        turn_builder, 'start_deferred_input_translation',
        lambda conv, turn, config, *, user_id: order.append(
            ('translate', conv, turn, user_id)) or pending)
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(
        'lib.turn_lifecycle.build_api_messages',
        lambda *args, **kwargs: order.append(('context',)) or [
            {'role': 'user', 'content': '原文'}])
    task = {'id': 'task-t', 'convId': 'conv-t', 'config': {}, 'status': 'pending'}
    monkeypatch.setattr(task_start, 'create_task', lambda *args, **kwargs: task)

    def stop_before_dispatch(*args):
        raise RuntimeError('stop before dispatch')

    monkeypatch.setattr(task_start, '_prepare_unbound_task_dispatch', stop_before_dispatch)
    monkeypatch.setattr('lib.tasks_pkg.manager.discard_task',
                        lambda task_id, conv_id=None: None)
    config = {'model': 'm', '_turnOwnerUserId': 1, '_turnId': 'turn-t',
              '_attemptId': 'attempt-t', '_deferredInputTranslation': True}

    with pytest.raises(RuntimeError, match='stop before dispatch'):
        task_start.start_conversation_attempt_executor('conv-t', config)

    assert order == [('translate', 'conv-t', 'turn-t', 1), ('context',)]
    assert task['_deferredInputTranslation'] is pending


# ── Worker join ───────────────────────────────────────────────────────

def _task(content, future):
    return {
        'id': 'task-x',
        'messages': [{'role': 'system', 'content': 'rules'},
                     {'role': 'user', 'content': content}],
        '_deferredInputTranslation': future,
        'lastUserQuery': '',
    }


def _resolved(original='原文', translated='Translated', **fields):
    """A joined outcome; ``recorded`` (default) means the Turn already holds it."""
    from lib.chat.turn_builder import DeferredInputTranslation

    future = Future()
    future.set_result(DeferredInputTranslation(
        'in', 3, original, translated, 'm', **{'recorded': True, **fields}))
    return future


def _record_into(monkeypatch, turn, log):
    import lib.translate.commit as commit

    def record(conversation_id, turn_id, **kwargs):
        log.append(('record', conversation_id, turn_id, kwargs))
        return turn

    monkeypatch.setattr(commit, 'record_input_translation_verdict', record)


def _verdict_turn(revision=4, **projection):
    return {'turnId': 'in', 'projectionRevision': revision, 'projection': projection or {
        'content': 'Translated', 'originalContent': '原文', '_translateDone': True}}


@pytest.mark.unit
def test_worker_substitutes_only_the_authored_text():
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    quote = '[引用]\n原文\n[/引用]\n\n'
    document = _SEPARATOR + '\nPDF Document: a.pdf\n原文'
    task = _task(quote + '原文' + document, _resolved())

    apply_deferred_input_translation(task)

    expected = quote + 'Translated' + document
    assert task['messages'][-1]['content'] == expected
    assert task['lastUserQuery'] == expected[:500]
    assert '_deferredInputTranslation' not in task


@pytest.mark.unit
def test_worker_substitutes_the_text_block_of_a_multimodal_message():
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA'}}
    # The model view strips notranslate tags from the authored text.
    task = _task([image, {'type': 'text', 'text': '原文'}],
                 _resolved(original='<nt>原文</nt>', translated='<nt>原文</nt> kept'))

    apply_deferred_input_translation(task)

    assert task['messages'][-1]['content'] == [image, {'type': 'text', 'text': '原文 kept'}]


@pytest.mark.unit
def test_worker_accepts_a_model_view_that_already_reads_the_translation(caplog):
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    task = _task('Translated', _resolved())
    with caplog.at_level(logging.WARNING):
        apply_deferred_input_translation(task)

    assert task['messages'][-1]['content'] == 'Translated'
    assert 'not found' not in caplog.text


@pytest.mark.unit
def test_worker_keeps_a_rewritten_input_and_reports_it(caplog):
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    task = _task('[REDACTED BY HOOK]', _resolved())
    with caplog.at_level(logging.WARNING):
        apply_deferred_input_translation(task)

    assert task['messages'][-1]['content'] == '[REDACTED BY HOOK]'
    assert 'not found in the model view' in caplog.text


@pytest.mark.unit
def test_worker_stop_never_waits_for_the_translator():
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    task = _task('原文', Future())
    task['aborted'] = True
    started = time.monotonic()

    assert apply_deferred_input_translation(task) == (None, None)

    assert time.monotonic() - started < 1.0
    assert task['messages'][-1]['content'] == '原文'


@pytest.mark.unit
def test_worker_survives_a_failed_translator():
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    failed = Future()
    failed.set_exception(RuntimeError('provider unavailable'))
    task = _task('原文', failed)

    apply_deferred_input_translation(task)

    assert task['messages'][-1]['content'] == '原文'


@pytest.mark.unit
def test_worker_shows_translation_progress_while_it_waits(monkeypatch):
    from lib.agent_core import events
    from lib.chat.turn_builder import DeferredInputTranslation
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    phases = []
    monkeypatch.setattr(events, 'emit_phase',
                        lambda task, phase, **kwargs: phases.append(kwargs))
    pending = Future()
    task = _task('原文', pending)
    threading.Timer(0.1, lambda: pending.set_result(DeferredInputTranslation(
        'in', 3, '原文', 'Translated', 'm', recorded=True))).start()

    apply_deferred_input_translation(task)

    assert phases and phases[0]['detailKey'] == 'translationWait.turn'
    assert task['messages'][-1]['content'] == 'Translated'


@pytest.mark.unit
def test_runner_reads_the_translated_view_after_worker_entry(monkeypatch):
    from lib.tasks_pkg import spawn

    monkeypatch.setattr(spawn, '_mark_worker_started',
                        lambda task, started, lock: True)
    monkeypatch.setattr(spawn, '_settle_unscheduled_workspace', lambda task: None)
    seen = []
    task = _task('原文', _resolved())
    run = spawn._owned_task_callable(
        lambda current: seen.append(current['messages'][-1]['content']),
        task, threading.Event(), threading.Lock())

    run()

    assert seen == ['Translated']


def _owned_run(monkeypatch, task, order):
    from lib.tasks_pkg import spawn

    monkeypatch.setattr(spawn, '_mark_worker_started',
                        lambda task, started, lock: True)
    monkeypatch.setattr(spawn, '_settle_unscheduled_workspace', lambda task: None)
    monkeypatch.setattr(
        'lib.project_dispatch.request_budget.bind_source_account',
        lambda task, *, expected_input: order.append(('bind', tuple(expected_input))))
    monkeypatch.setattr(
        'lib.tasks_pkg.model_replay.rebind_history_proof',
        lambda task, settled_input: order.append(('rebind', tuple(settled_input))))
    return spawn._owned_task_callable(
        lambda current: order.append(('runner', current['messages'][-1]['content'])),
        task, threading.Event(), threading.Lock())


@pytest.mark.unit
def test_worker_records_the_verdict_and_pins_authority_before_the_runner(monkeypatch):
    """Regression for coordinator-1a390889: the verdict landed after the pin."""
    order = []
    _record_into(monkeypatch, _verdict_turn(revision=4), order)
    task = _task('原文', _resolved(recorded=False))
    task.update(convId='conv-x', _userId=7)

    _owned_run(monkeypatch, task, order)()

    assert order == [
        ('record', 'conv-x', 'in', {'authored_text': '原文', 'translated_text': 'Translated',
                                    'model': 'm', 'failure_reason': None, 'user_id': 7}),
        ('bind', ('in', 4)),
        ('rebind', ('in', 4)),
        ('runner', 'Translated'),
    ]


@pytest.mark.unit
def test_only_an_attempt_owing_a_deferred_translation_binds_at_worker_entry(monkeypatch):
    order = []
    task = _task('hello', None)
    task.pop('_deferredInputTranslation')

    _owned_run(monkeypatch, task, order)()

    assert order == [('runner', 'hello')]


@pytest.mark.unit
def test_worker_records_a_translator_failure_and_keeps_the_authored_text(monkeypatch):
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    log = []
    _record_into(monkeypatch, _verdict_turn(
        revision=4, content='原文', _translateFailed={'reason': 'timed_out'}), log)
    task = _task('原文', _resolved(translated=None, failure='timed_out', recorded=False))

    assert apply_deferred_input_translation(task) == ('in', 4)
    assert [(entry[3]['translated_text'], entry[3]['failure_reason']) for entry in log] == [
        (None, 'timed_out')]
    assert task['messages'][-1]['content'] == '原文'


@pytest.mark.unit
def test_input_edited_before_its_verdict_settles_the_translated_revision(monkeypatch):
    """The pin then compares against revision 3 and refuses the edited Turn."""
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    log = []
    _record_into(monkeypatch, _verdict_turn(revision=5, content='改过的请求'), log)
    task = _task('原文', _resolved(recorded=False))

    assert apply_deferred_input_translation(task) == ('in', 3)
    assert len(log) == 1


@pytest.mark.unit
def test_worker_that_stops_waiting_records_no_verdict(monkeypatch):
    from lib.chat.turn_builder import DeferredInputTranslation
    from lib.tasks_pkg import input_translation

    monkeypatch.setattr(input_translation, '_JOIN_TIMEOUT_SECONDS', 0.05)
    log = []
    _record_into(monkeypatch, _verdict_turn(), log)
    stalled = Future()
    task = _task('原文', stalled)

    assert input_translation.apply_deferred_input_translation(task) == (None, None)
    stalled.set_result(DeferredInputTranslation('in', 3, '原文', 'Translated', 'm'))

    assert log == []
    assert task['messages'][-1]['content'] == '原文'


@pytest.mark.unit
def test_unconfirmed_verdict_write_keeps_the_translation_but_settles_no_revision(monkeypatch):
    import lib.translate.commit as commit
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    def unavailable(*args, **kwargs):
        raise RuntimeError('storage unavailable')

    monkeypatch.setattr(commit, 'record_input_translation_verdict', unavailable)
    task = _task('原文', _resolved(recorded=False))

    assert apply_deferred_input_translation(task) == (None, None)
    assert task['messages'][-1]['content'] == 'Translated'


@pytest.mark.unit
def test_worker_join_leaves_a_paired_startup_anchor(monkeypatch):
    import json

    from lib import observability
    from lib.tasks_pkg.input_translation import apply_deferred_input_translation

    records = []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda fmt, value: records.append(json.loads(value)))
    task = _task('原文', _resolved())
    task.update(convId='conv-x', _turnId='turn-x', _attemptId='attempt-x', _userId=7)

    apply_deferred_input_translation(task)

    anchors = [record for record in records if record['stage'] == 'input_translation']
    assert [record['boundary'] for record in anchors] == ['start', 'complete']
    assert anchors[1]['task_id'] == 'task-x' and anchors[1]['attempt_id'] == 'attempt-x'


@pytest.mark.api
@pytest.mark.auth_mode('open')
def test_turn_command_window_has_paired_startup_anchors(
        flask_client, conversation_authority, monkeypatch):
    import json

    from lib import observability

    records = []
    real_info = observability.logger.info

    def capture(fmt, *args):
        if fmt == '[TaskStartupTiming] %s':
            records.append(json.loads(args[0]))
        else:
            real_info(fmt, *args)

    monkeypatch.setattr(observability.logger, 'info', capture)
    _capture_admission(monkeypatch)

    response = flask_client.post(f'/api/v4/conversations/{CONV}/turns', json={
        'commandId': 'cmd-anchors',
        'message': {'text': 'hello'},
        'config': {'model': 'gpt-4o'},
    })

    assert response.status_code == 200, response.get_json()
    completed = {record['stage']: record for record in records
                 if record['boundary'] == 'complete'}
    assert {'input_build', 'turn_persist', 'executor_start',
            'acknowledgement'} <= set(completed)
    assert all(completed[stage]['conversation_id'] == CONV for stage in completed)
    attempt_id = response.get_json()['attempt']['attemptId']
    assert completed['executor_start']['attempt_id'] == attempt_id
    assert completed['acknowledgement']['attempt_id'] == attempt_id


def _busy_lane(flask_client, monkeypatch):
    _capture_admission(monkeypatch)
    first = flask_client.post(f'/api/v4/conversations/{CONV}/turns', json={
        'commandId': 'cmd-live', 'message': {'text': 'first'},
        'config': {'model': 'gpt-4o'}})
    assert first.status_code == 200, first.get_json()


@pytest.mark.api
@pytest.mark.auth_mode('open')
def test_queued_input_keeps_translation_for_its_own_attempt(
        flask_client, conversation_authority, monkeypatch):
    import lib.chat.turn_builder as turn_builder
    from lib.turn_lifecycle import get_attempt

    _busy_lane(flask_client, monkeypatch)
    monkeypatch.setattr(
        turn_builder, 'auto_translate_user',
        lambda *args, **kwargs: pytest.fail('a queued input was translated at acceptance'))

    response = flask_client.post(f'/api/v4/conversations/{CONV}/turns', json={
        'commandId': 'cmd-queued', 'injectMode': 'queue',
        'message': {'text': '排队的消息'},
        'config': {'model': 'gpt-4o', 'autoTranslate': True}})

    body = response.get_json()
    assert response.status_code == 200, body
    assert body.get('queued') is True, body
    accepted = get_attempt(body['attempt']['attemptId'], user_id=1,
                           include_execution_config=True)
    assert accepted['config']['_deferredInputTranslation'] is True


@pytest.mark.api
@pytest.mark.auth_mode('open')
def test_steer_translates_before_entering_the_live_model(
        flask_client, conversation_authority, monkeypatch):
    import lib.chat.turn_builder as turn_builder

    _busy_lane(flask_client, monkeypatch)
    translated = []

    def translate(text, config, *, user_id, conv_id):
        translated.append(text)
        return 'Steered in English', text, 'model-x', None

    monkeypatch.setattr(turn_builder, 'auto_translate_user', translate)

    response = flask_client.post(f'/api/v4/conversations/{CONV}/turns', json={
        'commandId': 'cmd-steer', 'injectMode': 'steer',
        'message': {'text': '插入的消息'},
        'config': {'model': 'gpt-4o', 'autoTranslate': True}})

    assert response.status_code == 200, response.get_json()
    assert translated == ['插入的消息']


# ── Replay proof after the verdict ────────────────────────────────────

def _proof_context(*, input_revision=3, other_revision=7, content='原文'):
    """A command-time model-context read: answer ``a2``, input ``in``, target ``out``."""
    def row(turn_id, parent, revision, projection, status='completed'):
        return {'turnId': turn_id, 'parentTurnId': parent, 'status': status,
                'currentAttemptId': None, 'projectionRevision': revision,
                'modelContextRevision': revision, 'projection': projection}
    return {
        'supported': True, 'covered': 2, 'proofEligible': True,
        'continuation': {'historyProof': {'version': 2, 'count': 1, 'digest': 'c' * 64},
                         'turns': [['a1', 'w' * 64]]},
        'historyProof': {'version': 2, 'count': 4,
                         'digest': f'{input_revision}{other_revision}' * 32},
        'turns': [row('a2', 'u2', other_revision, {'content': 'answer'}),
                  row('in', 'a2', input_revision, {'content': content}),
                  row('out', 'in', 1, {}, status='running')],
    }


def _pinned_task(pinned, view):
    from lib.tasks_pkg import model_replay

    config = {}
    model_replay.restore_prefix(pinned['turns'], config, conversation_id='conv',
                                user_id=7, validated_context=pinned)
    return {'config': config, 'convId': 'conv', '_userId': 7, '_turnId': 'out',
            'messages': [{'role': 'system', 'content': 'rules'},
                         {'role': 'user', 'content': view}]}


@pytest.mark.unit
@pytest.mark.parametrize('current, view, status', [
    (_proof_context(input_revision=4, content='Translated'), 'Translated', 'rebound'),
    (_proof_context(input_revision=4, content='Translated', other_revision=8),
     'Translated', 'history_changed'),
    (_proof_context(input_revision=5, content='edited later'), 'Translated', 'input_changed'),
    (_proof_context(input_revision=4, content='Translated'), '原文', 'view_mismatch'),
])
def test_replay_proof_rebinds_only_to_exactly_the_settled_input(
        monkeypatch, caplog, current, view, status):
    """The verdict lands after the command pinned the proof; only that change re-pins it."""
    from lib.tasks_pkg import model_replay
    from lib.tasks_pkg.input_translation import SettledInput

    pinned = _proof_context()
    task = _pinned_task(pinned, view)
    reads = []

    class Store:
        def read_context(self, **kwargs):
            reads.append(kwargs)
            return current

    monkeypatch.setattr(model_replay, 'ModelContinuationStore', Store)
    with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.model_replay'):
        model_replay.rebind_history_proof(task, SettledInput('in', 4))

    expected = current if status == 'rebound' else pinned
    assert task['config']['_modelReplayHistoryProof'] == expected['historyProof']
    assert '_modelReplayProofPin' not in task['config']
    assert reads == [{'user_id': 7, 'conversation_id': 'conv', 'turn_id': 'out'}]
    assert f'proof={status} input=in' in caplog.text


@pytest.mark.unit
@pytest.mark.parametrize('settled', [('in', 3), (None, None), ('other', 4)])
def test_replay_proof_is_kept_without_a_reread_when_the_verdict_changed_nothing_pinned(
        monkeypatch, settled):
    from lib.tasks_pkg import model_replay
    from lib.tasks_pkg.input_translation import SettledInput

    pinned = _proof_context()
    task = _pinned_task(pinned, '原文')
    monkeypatch.setattr(model_replay, 'ModelContinuationStore',
                        lambda: pytest.fail('no history re-read is needed'))

    model_replay.rebind_history_proof(task, SettledInput(*settled))

    assert task['config']['_modelReplayHistoryProof'] == pinned['historyProof']

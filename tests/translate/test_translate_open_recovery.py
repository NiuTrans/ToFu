"""Open-path translation recovery: a failed turn heals when it is reopened.

Problem this pins: a whole-turn translation that failed wrote a durable
``_translateFailed`` verdict and stopped there. The only driver of
``sweep_conversation_translations`` was the boot sweep, bounded to the 32 most
recent conversations within 7 days. So the user reopened the very conversation
that had failed, saw English, and nothing retried it — the failure was durable
but unrecoverable in practice (conv mvhiznrwv669ue, turn f85740dd, 2026-10-21).

Contract under test: opening a conversation admits ONE debounced, detached,
fail-soft recovery pass; the pass is idempotent (a turn with a live translation
or a committed one is never re-spawned), so overlapping triggers cannot pay
twice for the same text.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _task(conv='conv', turn='turn', user=7):
    return {
        'id': 'task-1',
        'convId': conv,
        '_turnId': turn,
        '_userId': user,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }


@pytest.fixture(autouse=True)
def _clear_recovery_state():
    import lib.translate.terminal as terminal

    with terminal._recovery_guard:
        terminal._recovery_last_attempt.clear()
    yield
    with terminal._recovery_guard:
        terminal._recovery_last_attempt.clear()


@pytest.fixture
def joined_threads(monkeypatch):
    """Run the detached recovery body inline so assertions are deterministic."""
    import lib.translate.terminal as terminal

    started = []

    class _InlineThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            self._target = target
            self._args = args
            self.name = name
            started.append(name)

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(terminal.threading, 'Thread', _InlineThread)
    return started


def test_open_path_admits_one_recovery_pass(monkeypatch, joined_threads):
    import lib.translate.terminal as terminal

    swept = []
    monkeypatch.setattr(
        terminal, 'sweep_conversation_translations',
        lambda conv, **kwargs: swept.append((conv, kwargs)) or 1)

    assert terminal.schedule_translation_recovery(
        'conv-open', user_id=7) is True

    assert swept == [('conv-open', {'user_id': 7})]
    assert joined_threads and joined_threads[0].startswith('translate-recovery')


def test_repeated_opens_are_debounced(monkeypatch, joined_threads):
    import lib.translate.terminal as terminal

    swept = []
    monkeypatch.setattr(
        terminal, 'sweep_conversation_translations',
        lambda conv, **kwargs: swept.append(conv) or 0)

    assert terminal.schedule_translation_recovery('conv-a', user_id=7) is True
    # The snapshot endpoint is polled continuously by an open conversation;
    # a failed turn must not be retried once per poll.
    for _ in range(5):
        assert terminal.schedule_translation_recovery(
            'conv-a', user_id=7) is False
    assert swept == ['conv-a']


def test_debounce_is_per_conversation(monkeypatch, joined_threads):
    import lib.translate.terminal as terminal

    swept = []
    monkeypatch.setattr(
        terminal, 'sweep_conversation_translations',
        lambda conv, **kwargs: swept.append(conv) or 0)

    assert terminal.schedule_translation_recovery('conv-a', user_id=7) is True
    assert terminal.schedule_translation_recovery('conv-b', user_id=7) is True
    assert swept == ['conv-a', 'conv-b']


def test_recovery_is_fail_soft(monkeypatch, joined_threads):
    import lib.translate.terminal as terminal

    def _boom(*_args, **_kwargs):
        raise RuntimeError('storage down')

    monkeypatch.setattr(
        terminal, 'sweep_conversation_translations', _boom)

    # A repair pass must never propagate into the read path it was admitted
    # from.
    assert terminal.schedule_translation_recovery(
        'conv-open', user_id=7) is True


def test_missing_identity_is_refused():
    import lib.translate.terminal as terminal

    assert terminal.schedule_translation_recovery('', user_id=7) is False
    assert terminal.schedule_translation_recovery(
        'conv', user_id=None) is False


def test_tracked_conversations_are_bounded(monkeypatch, joined_threads):
    import lib.translate.terminal as terminal

    monkeypatch.setattr(
        terminal, 'sweep_conversation_translations',
        lambda conv, **kwargs: 0)

    for index in range(terminal._RECOVERY_TRACKED_CONVERSATIONS + 5):
        terminal.schedule_translation_recovery(
            f'conv-{index}', user_id=7)

    assert len(terminal._recovery_last_attempt) <= (
        terminal._RECOVERY_TRACKED_CONVERSATIONS)


def test_sweep_never_respawns_a_turn_that_is_already_translating(monkeypatch):
    """Liveness dedupe: an in-flight task carries no durable marker yet."""
    import lib.translate.terminal as terminal
    import lib.turn_lifecycle as lifecycle
    import lib.translate.skip_policy as skip_policy

    monkeypatch.setattr(
        skip_policy, 'should_skip_automatic_translation',
        lambda *a, **k: False)
    monkeypatch.setattr(
        lifecycle, 'list_turns',
        lambda conv, user_id=None, light=False: {'turns': [
            {'turnId': 'live', 'status': 'completed',
             'projection': {'content': 'hello world'}},
            {'turnId': 'missing', 'status': 'completed',
             'projection': {'content': 'hello world'}},
        ]})
    monkeypatch.setattr(
        terminal, '_active_whole_turn_keys',
        lambda: {('conv', 'live')})

    spawned = []
    monkeypatch.setattr(
        terminal, '_spawn_whole_turn_translation',
        lambda **kw: spawned.append(kw['turn_id']) is None)

    count = terminal.sweep_conversation_translations(
        'conv', user_id=1,
        config={'autoTranslate': True, 'uiLang': 'zh'})

    assert spawned == ['missing']
    assert count == 1


def test_spawn_refuses_a_duplicate_live_translation(monkeypatch):
    import lib.translate.terminal as terminal

    monkeypatch.setattr(
        terminal, '_active_whole_turn_keys',
        lambda: {('conv-x', 'turn-x')})
    monkeypatch.setattr(
        'lib.translate.execution.submit_translation_task',
        lambda *a, **k: pytest.fail('duplicate spawn must not be admitted'))

    admitted = terminal._spawn_whole_turn_translation(
        conversation_id='conv-x',
        turn_id='turn-x',
        content='hello',
        target='Chinese',
        user_id=1,
        message_id='',
    )

    assert admitted is False

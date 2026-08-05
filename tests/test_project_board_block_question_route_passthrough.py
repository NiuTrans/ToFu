"""The ``/api/v1/project/board/block`` ROUTE must forward ``question`` +
``options`` to ``block_task``.

Why this suite exists (measured 2026-07-31, epic pt_d689f2016ecf4311 follow-up)
-------------------------------------------------------------------------------
``block_task`` grew a consistency gate: a ``reason`` whose prose claims a
pending question card while no ``question=`` was passed is REFUSED
(``error='question_required'``), because that state parks an epic behind a
control that does not exist — invisible to the human ("Needs you" is built from
the ``block_question`` column) *and* still dispatchable (``select_dispatchable``
honours that same column).

The gate landed in ``9e2a0481`` and is correct at the library boundary. But the
REST route called::

    result = block_task(project_path, conv_id, task_id, reason)

— four positional args, ``question``/``options`` never read from the body and
never forwarded. So for every caller arriving over HTTP the structured channel
was UNREACHABLE: the only block such a caller could construct was a
question-less one. Measured consequence on the live board: ``pt_3879f00e``
accumulated two further blocks AFTER the gate shipped (block_count 4 → 6),
still with ``block_question = None`` and a reason truncated at exactly 2000
chars whose prose says "STILL AWAITING owner one-click on the 4-option question
card".

That is the same defect class the gate was written to kill, surviving one layer
up: **a gate is only as reachable as the thinnest caller that can reach it.**
A library-level guard with no route-level counterpart is a guard that the
product's actual entry point cannot trigger.

These tests drive the ROUTE (not ``block_task``) so the passthrough is pinned
where it broke.
"""

import json

import pytest

pytestmark = pytest.mark.unit


def _find_block_route():
    """Return the board/block handler with its decorators peeled off.

    The registered view is wrapped in ``@require_auth`` / ``@rate_limit``,
    which need a live Quart app+request context. Those layers are not what
    this suite is about — it pins the handler BODY's parameter passthrough —
    so unwrap to the undecorated function via ``__wrapped__`` and drive it
    directly.
    """
    import routes.api_v1.project as proj
    fn = proj.project_board_block
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


def test_route_forwards_question_and_options_to_block_task(monkeypatch):
    """The route must pass the body's ``question``/``options`` through.

    Fails before the fix: the route called block_task with 4 positional args,
    so ``question`` arrived as its default ``''`` no matter what was posted.
    """
    import routes.api_v1.project as proj

    captured = {}

    def _fake_block_task(project_path, conv_id, task_id, reason, **kwargs):
        captured['args'] = (project_path, conv_id, task_id, reason)
        captured['kwargs'] = kwargs
        return {'ok': True, 'blocked_until': 1, 'block_count': 1}

    monkeypatch.setattr(
        'lib.conversations.project_board.block_task', _fake_block_task)
    monkeypatch.setattr(proj, 'parse_body', lambda: {
        'path': '/tmp/proj',
        'taskId': 'pt_x',
        'convId': 'c1',
        'reason': '[human-gated] awaiting your answer on the question card',
        'question': 'Continue slicing, or stop?',
        'options': [{'label': 'Continue'}, {'label': 'Stop'}],
    })
    monkeypatch.setattr(proj, 'api_ok', lambda r: ('OK', r))
    monkeypatch.setattr(proj, '_board_conv_id', lambda d: d.get('convId') or '')

    _find_block_route()()

    assert 'kwargs' in captured, 'block_task was never called'
    kw = captured['kwargs']
    assert kw.get('question') == 'Continue slicing, or stop?', (
        'the route dropped `question` — the structured human gate is '
        f'unreachable over HTTP. Forwarded kwargs: {kw!r}')
    opts = kw.get('options') or []
    assert [o.get('label') for o in opts] == ['Continue', 'Stop'], (
        f'the route dropped `options`. Forwarded kwargs: {kw!r}')


def test_route_surfaces_question_required_refusal(monkeypatch):
    """A refused block must reach the caller as an error, not a silent success.

    Complement to the passthrough test: forwarding is only useful if the
    refusal it enables is actually propagated rather than swallowed.
    """
    import routes.api_v1.project as proj

    def _refusing_block_task(project_path, conv_id, task_id, reason, **kwargs):
        return {'ok': False, 'error': 'question_required'}

    monkeypatch.setattr(
        'lib.conversations.project_board.block_task', _refusing_block_task)
    monkeypatch.setattr(proj, 'parse_body', lambda: {
        'path': '/tmp/proj',
        'taskId': 'pt_x',
        'convId': 'c1',
        'reason': 'STILL AWAITING owner one-click on the 4-option question card',
    })
    monkeypatch.setattr(proj, '_board_conv_id', lambda d: d.get('convId') or '')

    seen = {}

    # routes.api_v1.project no longer imports jsonify (envelope migration);
    # the refusal surfaces through api_payload(result, 400).
    def _fake_api_payload(payload, status=200):
        seen['payload'] = payload
        return payload, status

    monkeypatch.setattr(proj, 'api_payload', _fake_api_payload)
    monkeypatch.setattr(proj, 'api_ok', lambda r: pytest.fail(
        'a refused block must NOT be reported as success'))

    result = _find_block_route()()

    assert isinstance(result, tuple) and result[1] == 400, (
        f'refusal must surface as HTTP 400, got {result!r}')
    assert seen['payload'].get('error') == 'question_required'


def test_no_question_stays_legal_over_the_route(monkeypatch):
    """OVER-FIRING complement: a plain block with no question must still work.

    Without this, "forward the question" could be satisfied by making every
    question-less block fail — which would break the ordinary
    ``[sibling] path=…`` and infra-sign-off blocks that legitimately carry no
    human question.
    """
    import routes.api_v1.project as proj

    captured = {}

    def _fake_block_task(project_path, conv_id, task_id, reason, **kwargs):
        captured['kwargs'] = kwargs
        return {'ok': True, 'blocked_until': 1, 'block_count': 1}

    monkeypatch.setattr(
        'lib.conversations.project_board.block_task', _fake_block_task)
    monkeypatch.setattr(proj, 'parse_body', lambda: {
        'path': '/tmp/proj',
        'taskId': 'pt_x',
        'convId': 'c1',
        'reason': '[sibling] path=lib/x.py waiting on a peer commit',
    })
    monkeypatch.setattr(proj, 'api_ok', lambda r: ('OK', r))
    monkeypatch.setattr(proj, '_board_conv_id', lambda d: d.get('convId') or '')

    _find_block_route()()

    kw = captured['kwargs']
    assert (kw.get('question') or '') == '', (
        f'a question-less block must forward an empty question, got {kw!r}')


def test_route_source_reads_question_from_the_body():
    """Static complement: the handler must actually READ question/options.

    The monkeypatched tests above drive behaviour; this one pins the source so
    a future edit cannot regress to the 4-positional-arg call while keeping the
    behavioural tests green through some other path.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / 'routes' / 'api_v1' / 'project.py'
    text = src.read_text(encoding='utf-8')
    start = text.index('def project_board_block()')
    body = text[start:start + 1800]

    assert "data.get('question'" in body, (
        'the board/block handler never reads `question` from the request body '
        '— the structured human gate is unreachable over HTTP')
    assert "data.get('options'" in body, (
        'the board/block handler never reads `options` from the request body')
    assert 'question=' in body, (
        'the handler must forward question= to block_task as a keyword')

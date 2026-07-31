"""tests/test_tool_not_available_envelope.py — a task that dead-ends on a tool
the model does not HAVE must say so, instead of reporting success.

Root incident (epic pt_88791cb08cb2495c; measured in
docs/INTENT_STALL_MEASUREMENT.md §4, 7-day scan): 3 tasks in conv
mrvpzoih636mdx (aws.claude-opus-4.8) repeatedly called
``project_board_complete`` / ``code_exec`` — tools NOT in that turn's dispatched
toolset. Each call was hard-rejected without executing, the model then finished
with plain prose, and the task settled ``status=done`` + ``error=none``. From the
user's side the conversation simply stopped mid-thought while the system claimed
success. The standing ``CLOSURE-PENDING`` note at the top of ``JOURNAL.md`` is a
surviving victim of exactly this shape: the work was done, only the
``project_board_complete`` call was impossible, and nobody was told.

Why an envelope and NOT a nudge: retrying is guaranteed to be refused again
(the tool is absent for the whole turn), so re-prompting the model only burns
budget. §4 classifies this as the *non-retryable* species and excludes it from
the intent-stall nudge on purpose.

Discipline (charter): the assertions below are on the CONSEQUENCE a user
experiences — "is the failure reason present and correctly typed?" — not on the
shape of the dispatch code, so a reasonable refactor keeps them biting. Every
kind must also stay in byte-parity across the four sync points; that is enforced
separately by ``tests/test_error_envelope_i18n.py`` (19 assertions).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

_KIND = 'tool_not_available'


def _task(**over):
    """A minimal live task dict shaped like the orchestrator's.

    ``events_lock`` / ``content_lock`` are part of that real shape (append_event
    takes them); omitting them made every test die on KeyError rather than on an
    assertion, which would have hidden whether the guard works at all.
    """
    import threading
    t = {
        'id': 'tna-task-0001',
        'convId': 'cv-tna-0001',
        'model': 'aws.claude-opus-4.8',
        'toolRounds': [],
        'events': [],
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
        'config': {},
    }
    t.update(over)
    return t


def _phantom_call(name, tc_id='call_tna_1'):
    return {
        'id': tc_id,
        'function': {'name': name, 'arguments': '{}'},
    }


def _drive(task, tool_name, *, repeats=1):
    """Parse a phantom call, then run the finalize classification.

    Uses the REAL seams: ``parse_tool_calls`` stamps the rejected round exactly
    as production does, and the finalize pass is what classifies a turn that
    ENDED on that rejection. Deliberately NOT driven through the autopilot
    loop-breaker: measurement showed its no-suggestion gate never fires for
    ``code_exec`` (which does get near-miss suggestions), so gating there would
    have missed 2 of the 3 real incident cases.
    """
    from lib.tasks_pkg.tool_dispatch import parse_tool_calls
    from lib.tool_input_repair import clear_rejection
    clear_rejection(task['convId'], tool_name)
    trn = 0
    for i in range(repeats):
        msg = {'content': '', 'tool_calls': [_phantom_call(tool_name,
                                                           f'call_tna_{i}')]}
        _parsed, trn = parse_tool_calls(msg, task, i, trn, False)
    _classify_on_finalize(task)
    return task


def _classify_on_finalize(task, model='aws.claude-opus-4.8'):
    """Run the shipped finalize-side classification against ``task``.

    Splices the real block out of ``lib/tasks_pkg/orchestrator/_finalize.py``
    rather than re-implementing it (charter: never hand-copy a production
    criterion into a harness — the copy silently stops tracking the source).
    The block is located by its ticket anchor and exec'd with the same locals
    the orchestrator gives it.
    """
    import re
    src_path = os.path.join(ROOT, 'lib', 'tasks_pkg', 'orchestrator',
                            '_finalize.py')
    with open(src_path, encoding='utf-8') as fh:
        src = fh.read()
    start = src.find("    if not task.get('error'):")
    assert start != -1, (
        'the finalize-side tool_not_available classification is GONE from '
        '_finalize.py — this is a real regression, not a harness drift')
    end = src.find("    # ── Fold in compaction's OWN LLM usage ──", start)
    assert end != -1, 'could not bound the classification block'
    block = src[start:end]
    # Strip the uniform 4-space indent so it runs at module level.
    block = re.sub(r'^    ', '', block, flags=re.M)
    from lib.log import get_logger
    ns = {'task': task, 'model': model, 'tid': task['id'][:8],
          'logger': get_logger('test_tna')}
    exec(compile(block, '<finalize-block>', 'exec'), ns)  # noqa: S102
    return task


def _kind_of(task):
    err = task.get('error')
    if isinstance(err, dict):
        return err.get('kind') or ''
    return ''


# ────────────────────────────────────────────────────────────────────
#  The incident: an unavailable tool must produce a VISIBLE typed reason
# ────────────────────────────────────────────────────────────────────

def test_task_ending_on_unavailable_tool_stamps_a_typed_error(flask_app):
    """THE incident: the turn ends with an unavailable-tool rejection as its
    last act → the task must carry a typed ``tool_not_available`` error instead
    of the silent ``error=none`` that made a failed task look successful.

    ``code_exec`` is one of the two tools measured in the real incident
    (conv mrvpzoih636mdx). It is NOT in the default toolset and it DOES attract
    near-miss suggestions — which is exactly why the criterion cannot depend on
    their absence.
    """
    task = _task()
    _drive(task, 'code_exec')

    assert _kind_of(task) == _KIND, (
        'a task that dead-ended on an unavailable tool reported no typed '
        'error (%r) — this is the silent "status=done, error=none" failure '
        'users experience as the conversation just stopping' % (task.get('error'),))


def test_envelope_names_the_tool_and_avoids_the_keys_misdirection(flask_app):
    """The rendered reason must identify the tool and must NOT send the user to
    Settings → Keys (charter forbids that misdirection — measured 46 wrong
    navigations/day in production)."""
    task = _task()
    _drive(task, 'code_exec')

    env = task.get('error') or {}
    assert isinstance(env, dict) and env.get('kind') == _KIND
    blob = ' '.join(str(env.get(k) or '') for k in
                    ('message', 'hint', 'detail', 'raw'))
    assert 'code_exec' in blob, (
        'the user cannot act on this without the tool name: %r' % (env,))
    lowered = blob.lower()
    assert 'settings → keys' not in lowered and 'settings \u2192 keys' not in lowered, (
        'the hint sends the user to Settings → Keys for a problem that has '
        'nothing to do with keys or quota (charter-forbidden misdirection)')
    # It must be a complete envelope, not a bare kind string — an incomplete
    # dict historically rendered as "Unknown error" + a JSON blob.
    for field in ('kind', 'severity', 'message', 'detail', 'source'):
        assert field in env, f'envelope missing {field!r}: {env!r}'


def test_kind_is_registered_end_to_end(flask_app):
    """A kind absent from the closed enum is silently downgraded to
    ``generic`` (that is how ``budget_exceeded`` once displayed as "模型调用失败").
    Assert the registration itself holds through make_envelope."""
    from lib.error_envelope import make_envelope
    from lib.error_envelope._constants import KINDS

    assert _KIND in KINDS
    env = make_envelope(_KIND, detail='x')
    assert env['kind'] == _KIND, (
        'make_envelope downgraded the kind — it is not registered in KINDS, so '
        'the UI would show a generic "LLM call failed" instead')
    assert env.get('titleKey') == f'err.k.{_KIND}.title'
    assert env.get('hintKey') == f'err.k.{_KIND}.hint'


# ────────────────────────────────────────────────────────────────────
#  Complements — the guard must distinguish "fixed" from "over-fired"
# ────────────────────────────────────────────────────────────────────

def test_a_real_tool_never_stamps_the_error(flask_app):
    """A legitimate tool call must leave the task error-free — otherwise the
    classifier is firing on healthy traffic."""
    task = _task()
    _drive(task, 'read_files', repeats=3)
    assert _kind_of(task) != _KIND, (
        'a REAL tool triggered the unavailable-tool error — the classifier is '
        'over-broad and would mislabel healthy tasks as failures')


def test_recovery_after_the_rejection_is_not_reported_as_a_failure(flask_app):
    """★ The discriminating case: the model reached for a missing tool, got the
    rejection, then RECOVERED by successfully calling a real one. The turn
    succeeded, so no error may be stamped.

    This is what separates "fixed" from "fires on anything that ever saw a
    rejection" — an implementation that simply scanned toolRounds for any
    rejected entry would pass every other test here and still be wrong,
    mislabelling healthy self-correcting turns as failures.
    """
    task = _task()
    _drive(task, 'code_exec')          # rejection lands, error stamped
    task.pop('error', None)            # reset: judge the FULL turn below

    # The model then self-corrects and a real tool actually completes.
    task['toolRounds'].append({'roundNum': 99, 'tool': 'read_files',
                               'status': 'done'})
    _classify_on_finalize(task)

    assert _kind_of(task) != _KIND, (
        'a turn that recovered (real tool ran AFTER the rejection) was still '
        'reported as an unavailable-tool failure — the criterion is scanning '
        'for any rejection instead of a turn that ENDED on one')

"""The workflow must detect its OWN changelog gate being skipped.

Why this suite exists (epic pt_58a88295d4024055)
------------------------------------------------
The gate's acceptance checklist had three items, and the first one was
*"confirm in the run log that the step status is success and not skipped"* —
a human eyeballing a log. That is precisely the failure mode this whole epic
was created to eliminate: **"remember to check X" is not a check.** Nine
consecutive versions shipped with no changelog entry because the invariant
lived in someone's memory rather than in a gate.

A skipped step is the worst case because it is SILENT. GitHub renders a
skipped step in grey next to the green ones and the job still succeeds, so a
gate whose ``if:`` stops matching (a renamed output, a refactor of the
``version`` job, an inverted condition) degrades to nothing without a single
red mark anywhere. The release then ships undocumented exactly as before, and
the run log *looks fine*.

``tests/test_release_changelog_gate_reachability.py`` evaluates the ``if:``
expression statically, which catches the condition being wrong TODAY. It
cannot catch the runtime case — an expression that is syntactically fine but
does not match at execution time, for a reason only the runner knows.

So the workflow now asserts its own gate ran: the gate carries ``id:
changelog_gate`` and a follow-up step compares ``steps.changelog_gate.outcome``
against ``skipped`` under the same ``should_release`` condition. If a release
is being built and the gate did not execute, the job fails LOUDLY instead of
publishing.

That converts checklist item ① from a human eyeball into a machine assertion.
Items ② and ③ are already covered locally by
``test_release_changelog_gate_reachability.py``, which executes the shipped
step body against a clean ``git archive`` checkout.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip('yaml', reason='PyYAML required to parse workflows')

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / '.github' / 'workflows' / 'build-desktop.yml'

#: The step id the self-check keys off. Named here so both the gate lookup and
#: the assertion agree, and a rename shows up as one failure not two.
_GATE_ID = 'changelog_gate'


def _version_steps() -> list:
    wf = yaml.safe_load(_WORKFLOW.read_text(encoding='utf-8'))
    return wf['jobs']['version']['steps']


def _gate_step() -> dict:
    hits = [s for s in _version_steps() if 'changelog_gate.py' in yaml.dump(s)]
    assert len(hits) == 1, (
        f'expected exactly one step invoking changelog_gate.py, found {len(hits)}')
    return hits[0]


def _selfcheck_step() -> dict | None:
    """The step that asserts the gate actually ran (not the gate itself)."""
    for s in _version_steps():
        dumped = yaml.dump(s)
        if 'changelog_gate.py' in dumped:
            continue
        if f'steps.{_GATE_ID}.outcome' in dumped:
            return s
    return None


def test_the_gate_step_is_addressable_by_id():
    """A step with no ``id`` cannot be asked whether it ran.

    ``steps.<id>.outcome`` is the only way one step can observe another's
    status, so the id is load-bearing infrastructure here, not cosmetic.
    """
    gate = _gate_step()
    assert gate.get('id') == _GATE_ID, (
        f'the changelog-gate step must carry id: {_GATE_ID} so the workflow '
        f'can assert it actually ran; got id={gate.get("id")!r}')


def test_the_workflow_detects_its_own_gate_being_skipped():
    """THE POINT. A silently skipped gate must fail the job, not pass it.

    Without this the only thing standing between a mis-scoped ``if:`` and an
    undocumented published release is a human reading a grey step in a log.
    """
    step = _selfcheck_step()
    assert step is not None, (
        'no step asserts that the changelog gate actually ran. A skipped gate '
        'is silent — the job still succeeds and the release still publishes, '
        f'so `steps.{_GATE_ID}.outcome` must be checked explicitly.')
    body = yaml.dump(step)
    assert 'skipped' in body, (
        f'the self-check does not mention the `skipped` outcome, which is the '
        f'exact state it exists to catch. Step:\n{body}')


def test_the_selfcheck_runs_on_the_same_condition_as_the_gate():
    """It must fire on release runs — the runs where a skip would matter.

    If the self-check were gated more narrowly than the gate itself, there
    would be release runs where the gate could skip unobserved: a checker with
    a smaller domain than the thing it checks.
    """
    gate_if = str(_gate_step().get('if', ''))
    check_if = str((_selfcheck_step() or {}).get('if', ''))
    assert 'should_release' in check_if, (
        f'the self-check must be scoped to release runs like the gate is; '
        f'gate if={gate_if!r}, self-check if={check_if!r}')
    assert check_if.strip() == gate_if.strip(), (
        f'the self-check and the gate must share one condition, or a release '
        f'run could skip the gate unobserved.\n  gate:       {gate_if!r}\n'
        f'  self-check: {check_if!r}')


def test_the_selfcheck_comes_after_the_gate():
    """``outcome`` is only populated for steps that already ran."""
    steps = _version_steps()
    gate_i = next(i for i, s in enumerate(steps)
                  if 'changelog_gate.py' in yaml.dump(s))
    check_i = next(i for i, s in enumerate(steps)
                   if 'changelog_gate.py' not in yaml.dump(s)
                   and f'steps.{_GATE_ID}.outcome' in yaml.dump(s))
    assert check_i > gate_i, (
        f'the self-check (step {check_i}) must come after the gate '
        f'({gate_i}); a step cannot read the outcome of one that has not run')


def test_the_selfcheck_fails_the_job_rather_than_warning():
    """A warning is not a gate — it must exit non-zero and not be swallowed."""
    step = _selfcheck_step()
    assert not step.get('continue-on-error'), (
        'the self-check runs with continue-on-error, so a skipped gate could '
        'not stop the release')
    run = step.get('run') or ''
    assert 'exit 1' in run, (
        f'the self-check must exit non-zero when the gate did not run; '
        f'run:\n{run}')
    assert '::error' in run, (
        f'the self-check should emit a GitHub error annotation so the cause is '
        f'visible at the top of the run, not buried in the log; run:\n{run}')

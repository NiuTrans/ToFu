"""The CHANGELOG gate must RUN when a release is about to be built.

Why this suite exists
---------------------
`tests/test_release_changelog_gate.py` proves the gate's *rule* (what counts as
documented) and that the workflow *contains* a step invoking it. Neither
answers the question that decides whether the gate exists at all on the day it
matters: **does that step actually execute, or does its ``if:`` skip it?**

A gate guarded by a wrong ``if:`` is indistinguishable from no gate — the
workflow still mentions it, the YAML still parses, every text-level assertion
still passes, and the release ships undocumented anyway. That is the same
shape as the defect this project already hit twice: an instrument that is
present but carries no information.

The step cannot be dispatched to a real GitHub runner from here (that needs
credentials this environment does not have), so this suite closes every part
of the gap that IS decidable locally:

  * the ``if:`` expression is EVALUATED against the real dispatch contexts,
    rather than eyeballed;
  * the step is pinned to run BEFORE any build job could consume the version;
  * the shipped step body is EXECUTED verbatim against a clean ``git archive``
    checkout — which is what proves the script and ``CHANGELOG.md`` are really
    committed (not just present locally), that the relative paths resolve at
    the checkout root, and that a failure propagates through ``set -e``.

What remains genuinely unverified: the runner-side execution itself. That is
recorded on the board rather than asserted here, because a test that claimed
it would be the very thing this file exists to prevent.
"""

import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip('yaml', reason='PyYAML required to parse workflows')

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / '.github' / 'workflows' / 'build-desktop.yml'


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding='utf-8'))


def _gate_step() -> dict:
    steps = _workflow()['jobs']['version']['steps']
    hits = [s for s in steps if 'changelog_gate.py' in yaml.dump(s)]
    assert len(hits) == 1, (
        f'expected exactly one changelog-gate step in the version job, found '
        f'{len(hits)}')
    return hits[0]


def _eval_if(expr: str, ctx: dict) -> bool:
    """Evaluate the subset of GitHub expression syntax this workflow uses.

    Supports ``a.b.c == 'literal'`` (the only form present). Anything else
    raises rather than silently returning a constant — a resolver that quietly
    answered True for every input would make the assertions below pass no
    matter what the condition said, which is exactly the hollow-guard failure
    this file is about.
    """
    expr = (expr or '').strip()
    if expr.startswith('${{') and expr.endswith('}}'):
        expr = expr[3:-2].strip()
    m = re.fullmatch(r"([\w.]+)\s*==\s*'([^']*)'", expr)
    if not m:
        raise AssertionError(
            f'cannot evaluate if-expression {expr!r}. Extend _eval_if — and '
            'check whether the new form still gates on should_release.')
    path, want = m.group(1), m.group(2)
    cur = ctx
    for part in path.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            raise AssertionError(
                f'if-expression references {path!r}, which is not in the test '
                f'context. If the gate now keys off something else, verify it '
                f'still means "a release is about to be built".')
        cur = cur[part]
    return str(cur) == want


def _ctx(should_release: str) -> dict:
    return {'steps': {'ver': {'outputs': {
        'should_release': should_release, 'version': '0.16.0'}}}}


def test_gate_runs_when_a_release_will_be_built():
    """THE POINT. should_release=true ⇒ the step must EXECUTE.

    This is the case a real `workflow_dispatch` produces: dispatch sets
    should_release=true without probing the Releases API, so the gate is
    reached. If this evaluates False the gate is decorative — present in the
    YAML, skipped in every run that matters.
    """
    cond = _gate_step().get('if')
    assert cond, (
        'the changelog gate has no `if:` at all. That is not automatically '
        'wrong, but it must then run unconditionally — assert that '
        'deliberately rather than leaving it implicit.')
    assert _eval_if(cond, _ctx('true')) is True, (
        f'the gate would be SKIPPED on a run that is about to build and '
        f'publish. if: {cond!r}')


def test_gate_is_skipped_when_nothing_will_be_released():
    """COMPLEMENT: an already-released version must not re-litigate the docs.

    Without this, "make the gate run" is satisfiable by dropping the `if:`
    entirely — which would fail every ordinary push to main whose VERSION was
    already published, turning a release guard into a branch-wide outage.
    """
    cond = _gate_step().get('if')
    assert _eval_if(cond, _ctx('false')) is False, (
        f'the gate runs even when should_release=false, so every push to main '
        f'on an already-released VERSION would be gated. if: {cond!r}')


def test_gate_keys_off_the_same_signal_the_build_jobs_do():
    """The gate and the builds must agree on "is this a release run?".

    If the gate keyed off a different condition than the build jobs, the two
    could disagree — builds running while the gate sat out is precisely an
    undocumented release.
    """
    wf = _workflow()
    gate_cond = str(_gate_step().get('if'))
    assert 'should_release' in gate_cond, (
        f'the gate does not key off should_release; it must use the same '
        f'signal the build jobs gate on. if: {gate_cond!r}')
    for name, job in wf['jobs'].items():
        if name == 'version':
            continue
        assert 'should_release' in str(job.get('if', '')), (
            f'job {name} is not gated on should_release, so it could build '
            f'while the changelog gate sits out')


def test_gate_runs_before_any_build_job_can_start():
    """Ordering is the whole value: a gate that fires after the build is a report.

    Every build job `needs: version`, so a failure inside the version job
    prevents them from starting at all. Pin that, plus the step's position
    after the `ver` step whose output it consumes.
    """
    wf = _workflow()
    for name, job in wf['jobs'].items():
        if name == 'version':
            continue
        needs = job.get('needs')
        needs = [needs] if isinstance(needs, str) else (needs or [])
        assert 'version' in needs, (
            f'job {name} does not depend on the version job, so a failing '
            f'changelog gate would not stop it. needs={needs!r}')

    steps = wf['jobs']['version']['steps']
    ver_i = next(i for i, s in enumerate(steps) if s.get('id') == 'ver')
    gate_i = next(i for i, s in enumerate(steps)
                  if 'changelog_gate.py' in yaml.dump(s))
    assert gate_i > ver_i, (
        f'the gate (step {gate_i}) must come after the `ver` step '
        f'({ver_i}) whose version output it consumes')


def test_gate_invocation_matches_the_shape_proven_on_real_runners():
    """Interpreter + path shape must match the sibling gate in the SAME job.

    `release_assets.py` is invoked from this same job, on the same
    ubuntu-latest runner, as `python3 scripts/<file>` relative to the checkout
    root — and that step has run on real runners. Matching it is what makes
    "python3 is missing" / "wrong working directory" already-answered
    questions rather than open risks for the new step.
    """
    ver_step = next(s for s in _workflow()['jobs']['version']['steps']
                    if s.get('id') == 'ver')
    sibling = [ln.strip() for ln in (ver_step.get('run') or '').splitlines()
               if 'release_assets.py' in ln]
    assert sibling, (
        'the version job no longer invokes release_assets.py — the precedent '
        'this test leans on is gone; re-establish how python3 is reached')
    assert any(ln.startswith('python3 scripts/') for ln in sibling), (
        f'the proven sibling invocation changed shape: {sibling!r}')

    gate_run = _gate_step().get('run') or ''
    gate_lines = [ln.strip() for ln in gate_run.splitlines()
                  if 'changelog_gate.py' in ln]
    assert gate_lines and gate_lines[0].startswith('python3 scripts/'), (
        f'the changelog gate must be invoked the same way as the sibling gate '
        f'already proven on real runners; got {gate_lines!r}')
    assert 'set -e' in gate_run, (
        f'without set -e the step could report success on a failed gate; '
        f'run:\n{gate_run}')


def _clean_checkout(tmp_path: Path) -> Path:
    """Materialise HEAD into a temp dir — what `actions/checkout@v4` produces.

    Crucially this holds ONLY committed content, so a file that exists in the
    working tree but was never committed is ABSENT here exactly as it would be
    on a runner. That is a failure this project already hit once:
    ``/scripts/*`` is deny-by-default in `.gitignore`, so a new gate script is
    invisible to git until an explicit ``!`` exception is added — and the
    workflow would then call a file that does not exist in CI while working
    perfectly on the author's machine.
    """
    dest = tmp_path / 'ci-sim'
    dest.mkdir()
    archive = subprocess.run(['git', 'archive', 'HEAD'], cwd=_ROOT,
                             capture_output=True, check=True).stdout
    subprocess.run(['tar', '-x', '-C', str(dest)], input=archive, check=True)
    return dest


def _gate_body(version: str) -> str:
    """The shipped step body with the workflow expression resolved."""
    return _gate_step()['run'].replace(
        '${{ steps.ver.outputs.version }}', version)


def test_the_gate_and_its_inputs_survive_a_clean_checkout(tmp_path):
    """Everything the step touches must be COMMITTED, not merely present locally.

    The sibling-precedent argument (``release_assets.py`` proves python3 is
    reachable in this job) covers the interpreter but NOT this: the changelog
    gate carries a file dependency the sibling does not have — it reads
    ``CHANGELOG.md`` on every release run, whereas ``release_assets.py`` runs
    only inside the HTTP-200 branch and is handed its payload.
    """
    root = _clean_checkout(tmp_path)
    for rel in ('scripts/changelog_gate.py', 'CHANGELOG.md', 'VERSION'):
        assert (root / rel).is_file(), (
            f'{rel} is missing from a clean checkout of HEAD, so the release '
            f'workflow would fail on a runner while working fine locally')


def test_the_gate_passes_when_run_exactly_as_the_workflow_runs_it(tmp_path):
    """Execute the shipped shell verbatim, from the checkout root.

    Asserting on the workflow's TEXT cannot distinguish a step that works from
    one that dies on a missing file or a bad relative path. This runs the real
    step body in a real clean checkout and requires exit 0 for the version the
    repo is actually about to release.
    """
    root = _clean_checkout(tmp_path)
    version = (root / 'VERSION').read_text(encoding='utf-8').strip()
    proc = subprocess.run(['bash', '-c', _gate_body(version)], cwd=str(root),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f'the workflow step FAILS on a clean checkout for VERSION {version}.\n'
        f'stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}')
    assert 'DOCUMENTED' in proc.stdout


def test_an_undocumented_version_aborts_the_step(tmp_path):
    """THE ONE THAT MATTERS: proving it PASSES is not proving it is a gate.

    Mirrors `workflow_dispatch` with `version_override` set to a version the
    CHANGELOG does not mention. The step must exit non-zero — that is what
    makes GitHub mark it failed, fail the ``version`` job, and (via
    ``needs: version``) keep every build job from ever starting.
    """
    root = _clean_checkout(tmp_path)
    proc = subprocess.run(['bash', '-c', _gate_body('0.99.0')], cwd=str(root),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, (
        'an undocumented version did NOT abort the step, so the release would '
        f'proceed and publish it as Latest.\nstdout:\n{proc.stdout}')
    assert 'UNDOCUMENTED' in proc.stdout


def test_a_wrong_working_directory_blocks_rather_than_passing(tmp_path):
    """If the gate ever loses its cwd, it must fail CLOSED.

    The step relies on GitHub's default working directory being the checkout
    root. Should that ever change, the honest outcome is a blocked release
    (UNDETERMINED), never a silent pass — the asymmetry this gate is designed
    around, and deliberately the opposite of ``release_assets.py``'s
    fail-open rule.
    """
    root = _clean_checkout(tmp_path)
    proc = subprocess.run(
        ['bash', '-c',
         'set -euo pipefail\npython3 changelog_gate.py --version 0.16.0'],
        cwd=str(root / 'scripts'), capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, (
        'run from the wrong directory the gate PASSED, which would let an '
        f'undocumented release ship on any cwd change.\nstdout:\n{proc.stdout}')


def test_the_version_job_declares_the_runner_that_provides_python3():
    """python3 comes from the ubuntu-latest image, not from a setup step.

    The version job has no `actions/setup-python`, so the interpreter is the
    image's. Pin the runner label: moving this job to a bare/self-hosted
    runner would silently remove python3 from both gates at once.
    """
    vj = _workflow()['jobs']['version']
    assert vj['runs-on'] == 'ubuntu-latest', (
        f"the version job runs on {vj['runs-on']!r}; both its gates rely on "
        f'the GitHub-hosted image providing python3 with no setup step')
    assert not any('setup-python' in str(s.get('uses', ''))
                   for s in vj['steps']), (
        'a setup-python step appeared in the version job — if python3 now '
        'needs setting up, this test\'s reasoning (and the sibling gate\'s) '
        'must be revisited')

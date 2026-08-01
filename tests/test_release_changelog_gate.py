"""A release must not ship a VERSION the CHANGELOG never mentions.

Why this suite exists (measured 2026-07-31)
-------------------------------------------
``VERSION`` said 0.15.2 while ``CHANGELOG.md``'s newest heading was
``## [0.10.0]`` — nine consecutive releases with no entry, 91 lines stranded
under ``[Unreleased]``. ``grep -rl CHANGELOG tests/ scripts/`` returned NOTHING,
so no check anywhere in the repo could observe it.

That matters more than ordinary doc drift because releasing here is a TRIGGER,
not a deliberate act: ``build-desktop.yml`` builds on any push to main whose
VERSION has no complete release, and publishes with ``make_latest: "true"``.
With four sibling sessions live on this shared tree, the next push by anyone
ships 0.15.2 and pins it as the release users see.

The guards below pin BOTH halves, because either alone is satisfiable the
wrong way:

  * the DATA — the repo's current VERSION is documented (fails today);
  * the MECHANISM — the workflow actually runs the gate before building, so
    the invariant survives the next drift instead of being a one-time repair.
"""

import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip('yaml', reason='PyYAML required to parse workflows')

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / '.github' / 'workflows' / 'build-desktop.yml'
_GATE = _ROOT / 'scripts' / 'changelog_gate.py'


def _version() -> str:
    return (_ROOT / 'VERSION').read_text(encoding='utf-8').strip()


def _run_gate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GATE), *args],
        capture_output=True, text=True, cwd=str(_ROOT))


# ── The DATA half ────────────────────────────────────────────────

def test_current_version_is_documented_in_the_changelog():
    """The repo's VERSION must have its own ``## [x.y.z]`` section.

    Fails until the 0.11.0–0.15.2 entries are written. This is the guard that
    would have caught nine versions of drift at the first one.
    """
    proc = _run_gate()
    assert proc.returncode == 0, (
        f'VERSION {_version()} is not documented in CHANGELOG.md.\n'
        f'{proc.stdout}{proc.stderr}')


# ── The MECHANISM half ───────────────────────────────────────────

def test_workflow_runs_the_changelog_gate_before_building():
    """``build-desktop.yml`` must invoke the gate, in the ``version`` job.

    Without this the data test above is a one-time repair: the next VERSION
    bump that forgets the changelog ships exactly as the last nine did.
    """
    wf = yaml.safe_load(_WORKFLOW.read_text(encoding='utf-8'))
    version_job = wf['jobs']['version']
    body = yaml.dump(version_job)
    assert 'changelog_gate.py' in body, (
        'the `version` job never runs scripts/changelog_gate.py — an '
        'undocumented VERSION would build and publish unnoticed, which is how '
        '0.11.0-0.15.2 shipped with no changelog entries. Job body:\n'
        f'{body[:2000]}')


def test_changelog_gate_failure_blocks_the_release():
    """The gate's non-zero exit must actually stop the run.

    A gate whose failure is swallowed is decoration. The step must run under
    a shell that propagates the error — `set -euo pipefail` for bash, or an
    explicit exit-code branch — and must NOT be `continue-on-error`.
    """
    wf = yaml.safe_load(_WORKFLOW.read_text(encoding='utf-8'))
    steps = wf['jobs']['version']['steps']
    gate_steps = [s for s in steps
                  if 'changelog_gate.py' in yaml.dump(s)]
    assert gate_steps, 'no step in the `version` job runs the changelog gate'
    for s in gate_steps:
        assert not s.get('continue-on-error'), (
            f'the changelog gate runs with continue-on-error — its failure '
            f'cannot block a release. Step: {s.get("name") or s}')
        run = s.get('run') or ''
        assert 'set -e' in run or 'exit 1' in run, (
            'the changelog-gate step neither sets -e nor exits non-zero, so a '
            f'failed gate would not stop the build. Step run:\n{run}')


# ── The RULE itself ──────────────────────────────────────────────

def test_unreleased_heading_does_not_count_as_documentation(tmp_path):
    """``## [Unreleased]`` must NOT satisfy the gate.

    This is the precise state the repo was in: content existed, under the
    staging heading, for nine versions. If Unreleased counted, the gate would
    have passed throughout the entire drift.
    """
    cl = tmp_path / 'CHANGELOG.md'
    cl.write_text('# Changelog\n\n## [Unreleased]\n\n- some work\n',
                  encoding='utf-8')
    proc = _run_gate('--version', '0.15.2', '--changelog', str(cl))
    assert proc.returncode == 1, (
        f'[Unreleased] was accepted as documentation for 0.15.2:\n'
        f'{proc.stdout}{proc.stderr}')


def test_a_bare_prose_mention_does_not_count(tmp_path):
    """Naming the version in prose must not satisfy a structural assertion.

    The project has been bitten before by prose satisfying a guard that meant
    to assert structure, so the check is anchored to the heading grammar.
    """
    cl = tmp_path / 'CHANGELOG.md'
    cl.write_text(
        '# Changelog\n\n## [Unreleased]\n\n'
        '- Backported a fix that also shipped in 0.15.2 downstream.\n',
        encoding='utf-8')
    proc = _run_gate('--version', '0.15.2', '--changelog', str(cl))
    assert proc.returncode == 1, (
        'a prose mention of the version satisfied the gate:\n'
        f'{proc.stdout}{proc.stderr}')


def test_a_heading_for_a_different_version_does_not_count(tmp_path):
    """The common near-miss: VERSION bumped, changelog left on the old one."""
    cl = tmp_path / 'CHANGELOG.md'
    cl.write_text('# Changelog\n\n## [0.15.1] - 2026-07-01\n\n- older\n',
                  encoding='utf-8')
    proc = _run_gate('--version', '0.15.2', '--changelog', str(cl))
    assert proc.returncode == 1, (
        f'a heading for 0.15.1 satisfied a 0.15.2 check:\n{proc.stdout}')


def test_a_real_heading_passes(tmp_path):
    """OVER-FIRING complement: a correct changelog must PASS.

    Without this, "block undocumented versions" could be satisfied by a gate
    that refuses everything — which would make the release pipeline
    permanently unusable rather than correct.
    """
    cl = tmp_path / 'CHANGELOG.md'
    cl.write_text(
        '# Changelog\n\n## [Unreleased]\n\n## [0.15.2] - 2026-07-31\n\n'
        '### Added\n- a thing\n',
        encoding='utf-8')
    proc = _run_gate('--version', '0.15.2', '--changelog', str(cl))
    assert proc.returncode == 0, (
        f'a correctly documented version was rejected:\n'
        f'{proc.stdout}{proc.stderr}')


def test_unreadable_changelog_blocks_rather_than_fails_open(tmp_path):
    """UNDETERMINED must block, inverting release_assets.py's asymmetry.

    The asset gate fails OPEN (a redundant build is cheap, a missed release is
    not). Here the expensive mistake is the opposite one: publishing an
    undocumented release pins it as Latest and is hard to retract, while the
    correction is one commit. So an unreadable changelog must not ship.
    """
    proc = _run_gate('--version', '0.15.2',
                     '--changelog', str(tmp_path / 'nope.md'))
    assert proc.returncode == 2, (
        f'expected UNDETERMINED(2), got {proc.returncode}:\n{proc.stdout}')
    assert proc.returncode != 0, 'an unreadable changelog must never pass'


def test_gate_rule_lives_in_exactly_one_place():
    """The heading grammar must not be duplicated into the workflow.

    Same argument as scripts/release_assets.py: two copies of a release rule
    drift, and both keep passing on the cases they still know about.
    """
    wf_text = _WORKFLOW.read_text(encoding='utf-8')
    assert '## [' not in wf_text.replace('changelog_gate.py', ''), (
        'the workflow appears to inline the changelog heading grammar; it '
        'must shell out to scripts/changelog_gate.py instead')

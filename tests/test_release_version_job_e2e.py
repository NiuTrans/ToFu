"""End-to-end simulation of the release ``version`` job — including the
negative probe that proves the CHANGELOG gate can BLOCK.

Why this suite exists (epic pt_58a88295d4024055)
------------------------------------------------
The owner's acceptance criterion was: *"proving it PASSES is not proving it is
a gate"* — dispatch the workflow with ``version_override=0.99.0`` and confirm
the step goes red while no build job starts.

That dispatch needs GitHub credentials this environment does not have
(``gh`` absent, ``POST /actions/.../dispatches`` → 401, no local ``act`` or
container runtime). But almost all of the criterion is decidable here, and
leaving it entirely to a human checklist would repeat the defect this whole
epic exists to kill: an invariant that lives only in someone's memory.

So this module RUNS the shipped step bodies — extracted from the workflow YAML,
never re-typed — against a clean ``git archive`` checkout, with the runner
contract emulated:

  * ``ver`` writes ``version`` / ``should_release`` to ``$GITHUB_OUTPUT``;
  * each subsequent step is executed only when its ``if:`` evaluates true,
    which is how a real skip happens;
  * a step's ``outcome`` (``success`` / ``failure`` / ``skipped``) is fed to
    later steps exactly as ``steps.<id>.outcome`` would be;
  * ``set -e`` failure propagation decides the job verdict, and because every
    build job declares ``needs: version``, a failed job means no build starts.

★ The specific gap this closes beyond the earlier suites: the SELF-CHECK step's
own shell had never been executed anywhere — it was only asserted statically.
An instrument that has never been measured is exactly what this epic is about,
so its body is driven here for real, in both the fires-and-passes and the
fires-and-fails direction.

What remains genuinely unverifiable locally: whether GitHub's runner honours a
non-zero exit by failing the step. That is a platform contract, not our code.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

yaml = pytest.importorskip('yaml', reason='PyYAML required to parse workflows')

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / '.github' / 'workflows' / 'build-desktop.yml'


def _version_job() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding='utf-8'))['jobs']['version']


def _clean_checkout(tmp_path: Path) -> Path:
    """HEAD materialised as `actions/checkout@v4` would leave it."""
    dest = tmp_path / 'co'
    dest.mkdir()
    blob = subprocess.run(['git', 'archive', 'HEAD'], cwd=_ROOT,
                          capture_output=True, check=True).stdout
    subprocess.run(['tar', '-x', '-C', str(dest)], input=blob, check=True)
    return dest


def _render(expr: str, ctx: dict) -> str:
    """Substitute every ``${{ … }}`` reference this job uses.

    An UNRESOLVED reference raises rather than being left in the text. Bash
    would otherwise report ``bad substitution`` and the step would fail for a
    harness reason while looking like a product failure — which is exactly how
    a simulation starts certifying the wrong thing.
    """
    import re

    def sub(m):
        key = m.group(1).strip()
        if key in ctx:
            return str(ctx[key])
        raise AssertionError(
            f'the harness cannot resolve ${{{{ {key} }}}}. Extend the context '
            f'— leaving it unrendered makes bash fail for a reason unrelated '
            f'to the gate. Known keys: {sorted(ctx)}')

    return re.sub(r'\$\{\{([^}]*)\}\}', sub, expr)


def _eval_if(cond, ctx: dict) -> bool:
    """Evaluate a step ``if:``; absent means always-run."""
    if cond is None:
        return True
    import re
    txt = str(cond).strip()
    if txt.startswith('${{') and txt.endswith('}}'):
        txt = txt[3:-2].strip()
    m = re.fullmatch(r"([\w.]+)\s*==\s*'([^']*)'", txt)
    if not m:
        raise AssertionError(f'cannot evaluate if-expression {txt!r}')
    key, want = m.group(1), m.group(2)
    if key not in ctx:
        # An unknown reference is FALSE in GitHub expressions, which is how a
        # renamed output silently turns a gate into a skip — the case the
        # self-check exists to catch, so model it faithfully.
        return False
    return str(ctx[key]) == want


def run_version_job(checkout: Path, *, event: str, version_override: str = '',
                    http_code: str = '404') -> dict:
    """Execute the real ``version`` job end to end. Returns per-step outcomes."""
    job = _version_job()
    bindir = checkout / '_bin'
    bindir.mkdir(exist_ok=True)
    (bindir / 'curl').write_text(f'#!/bin/sh\nprintf %s {http_code}\n', encoding='utf-8')
    (bindir / 'curl').chmod(0o755)
    (bindir / 'git').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    (bindir / 'git').chmod(0o755)

    gh_out = checkout / '_gh_output'
    gh_out.write_text('', encoding='utf-8')
    env = {
        **os.environ,
        'PATH': f'{bindir}{os.pathsep}' + os.environ.get('PATH', ''),
        'GITHUB_OUTPUT': str(gh_out),
        'GITHUB_EVENT_NAME': event,
        'GITHUB_REPOSITORY': 'rangehow/ToFu',
        'GITHUB_API_URL': 'https://api.github.com',
        'GH_TOKEN': 'stub',
        'VERSION_OVERRIDE': version_override,
    }

    ctx: dict = {'github.event_name': event}
    outcomes: dict = {}
    job_failed = False
    logs: list = []

    for step in job['steps']:
        run = step.get('run')
        sid = step.get('id') or (step.get('name') or step.get('uses') or '?')
        if run is None:
            continue  # checkout action — emulated by _clean_checkout
        if job_failed:
            outcomes[sid] = 'skipped'
            continue
        if not _eval_if(step.get('if'), ctx):
            outcomes[sid] = 'skipped'
            if step.get('id'):
                ctx[f'steps.{step["id"]}.outcome'] = 'skipped'
            continue
        body = _render(run, ctx)
        proc = subprocess.run(['bash', '-c', body], cwd=str(checkout), env=env,
                              capture_output=True, text=True, timeout=120)
        ok = proc.returncode == 0
        outcomes[sid] = 'success' if ok else 'failure'
        logs.append((sid, proc.returncode, proc.stdout + proc.stderr))
        if step.get('id'):
            ctx[f'steps.{step["id"]}.outcome'] = outcomes[sid]
        # publish $GITHUB_OUTPUT back into the expression context
        for line in gh_out.read_text(encoding='utf-8').splitlines():
            if '=' in line and step.get('id'):
                k, _, v = line.partition('=')
                ctx[f'steps.{step["id"]}.outputs.{k}'] = v
        if not ok and not step.get('continue-on-error'):
            job_failed = True

    return {'outcomes': outcomes, 'job_failed': job_failed, 'logs': logs,
            'ctx': ctx}


def _log_of(res: dict, needle: str) -> str:
    return '\n'.join(t for sid, rc, t in res['logs'] if needle in sid or needle in t)


# ── the negative probe the owner asked for ────────────────────────

def test_negative_probe_an_undocumented_version_fails_the_job(tmp_path):
    """``workflow_dispatch`` + ``version_override=0.99.0`` must FAIL the job.

    This is the criterion in full: not that the gate passes, but that it
    BLOCKS. workflow_dispatch sets should_release=true without probing, so the
    gate is reached with a version the CHANGELOG does not document.
    """
    co = _clean_checkout(tmp_path)
    res = run_version_job(co, event='workflow_dispatch', version_override='0.99.0')
    assert res['outcomes'].get('changelog_gate') == 'failure', (
        f"the gate did not fail on an undocumented version: {res['outcomes']}\n"
        f"{_log_of(res, 'changelog_gate')}")
    assert res['job_failed'], (
        'the version job did not fail, so build jobs (needs: version) would '
        f"still start: {res['outcomes']}")
    assert 'UNDOCUMENTED' in _log_of(res, 'changelog_gate')


def test_negative_probe_stops_every_build_job(tmp_path):
    """A failed version job must gate all three platform builds.

    The build jobs cannot be executed here, so the invariant is asserted on
    the dependency edge that makes them unreachable.
    """
    co = _clean_checkout(tmp_path)
    res = run_version_job(co, event='workflow_dispatch', version_override='0.99.0')
    assert res['job_failed']
    wf = yaml.safe_load(_WORKFLOW.read_text(encoding='utf-8'))
    builds = [n for n in wf['jobs'] if n.startswith('build-')]
    assert builds, 'no build jobs found — the workflow shape changed'
    for name in builds:
        needs = wf['jobs'][name].get('needs')
        needs = [needs] if isinstance(needs, str) else (needs or [])
        assert 'version' in needs, (
            f'{name} does not need: version, so it would start even though '
            f'the changelog gate failed')


# ── the positive complement ───────────────────────────────────────

def test_the_real_version_passes_the_whole_job(tmp_path):
    """OVER-FIRING complement: today's VERSION must sail through.

    Without this, "block undocumented versions" is satisfiable by a gate that
    refuses everything — which would make releases impossible rather than
    correct.
    """
    co = _clean_checkout(tmp_path)
    version = (co / 'VERSION').read_text(encoding='utf-8').strip()
    res = run_version_job(co, event='workflow_dispatch', version_override=version)
    assert not res['job_failed'], (
        f"the job failed for the real VERSION {version}: {res['outcomes']}\n"
        f"{_log_of(res, 'changelog_gate')}")
    assert res['outcomes'].get('changelog_gate') == 'success'
    assert 'DOCUMENTED' in _log_of(res, 'changelog_gate')


# ── the self-check step, executed rather than merely asserted ─────

def test_the_selfcheck_runs_and_passes_when_the_gate_ran(tmp_path):
    """The self-check's own shell had never been executed anywhere.

    Earlier suites only asserted its TEXT. Here its body runs for real and
    must not false-positive on a healthy release run.
    """
    co = _clean_checkout(tmp_path)
    version = (co / 'VERSION').read_text(encoding='utf-8').strip()
    res = run_version_job(co, event='workflow_dispatch', version_override=version)
    names = [k for k in res['outcomes'] if 'Assert' in str(k)]
    assert names, f'the self-check step did not run: {res["outcomes"]}'
    assert res['outcomes'][names[0]] == 'success', (
        f'the self-check failed on a healthy run: {res["outcomes"]}')


def test_the_selfcheck_catches_a_skipped_gate(tmp_path):
    """THE POINT OF THE SELF-CHECK, driven for real.

    Emulates the silent-decay case: the gate's ``if:`` no longer matches, so
    it is skipped. The self-check must turn that grey step into a red job.
    """
    co = _clean_checkout(tmp_path)
    wf_path = co / '.github' / 'workflows' / 'build-desktop.yml'
    text = wf_path.read_text(encoding='utf-8')
    # Break ONLY the gate's condition, leaving the self-check's intact — the
    # exact drift the self-check exists to notice.
    text = text.replace(
        "      - name: Require a CHANGELOG entry for this version\n"
        "        id: changelog_gate\n"
        "        if: steps.ver.outputs.should_release == 'true'",
        "      - name: Require a CHANGELOG entry for this version\n"
        "        id: changelog_gate\n"
        "        if: steps.ver.outputs.should_release == 'nope'")
    wf_path.write_text(text, encoding='utf-8')

    # Re-read the job from the MUTATED checkout, not the repo.
    global _WORKFLOW
    original = _WORKFLOW
    try:
        _WORKFLOW = wf_path
        version = (co / 'VERSION').read_text(encoding='utf-8').strip()
        res = run_version_job(co, event='workflow_dispatch',
                              version_override=version)
    finally:
        _WORKFLOW = original

    assert res['outcomes'].get('changelog_gate') == 'skipped', (
        f'expected the gate to skip under the broken condition: '
        f'{res["outcomes"]}')
    assert res['job_failed'], (
        'the gate was SKIPPED and the job still succeeded — a silently absent '
        f'gate would publish an undocumented release: {res["outcomes"]}')
    joined = '\n'.join(t for _s, _rc, t in res['logs'])
    assert 'did NOT run' in joined or '::error' in joined, (
        f'the self-check failed without explaining why:\n{joined}')

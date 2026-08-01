"""tests/test_workflow_shell_safety.py — a multi-command build step must FAIL
when one of its commands fails.

THE DEFECT (measured, run 30601806258)
--------------------------------------
The Windows ``Install dependencies`` step reported **success** while
``pip install -r requirements.txt`` had **failed**.

GitHub's default shell on ``windows-latest`` is ``pwsh``. For a multi-line
``run:`` block, pwsh does not abort when a NATIVE command exits non-zero, and
the step's result is taken from the LAST command's exit code. The step was::

    pip install -r requirements.txt          # FAILED (unsatisfiable pin)
    pip install pyinstaller pystray ...      # succeeded -> step went GREEN

The build then continued with the project's own dependencies absent and still
produced an installer. Measured artifact sizes:

    this run (deps missing)   windows-installer   48,960,018 bytes
    last good build v0.14.2   windows-installer  115,342,167 bytes

A 49 MB "Tofu installer" containing essentially none of Tofu was uploaded as a
release artifact. Had the other three legs succeeded, the completeness gate
would have counted four assets and published it as Latest.

Linux and macOS default to ``bash``, which the runner invokes with ``-e``, so
they failed honestly in 2 seconds. The SAME unsatisfiable pin was therefore
loud on three platforms and silently corrupting on the fourth. That asymmetry —
not the missing package — is what this module guards.

WHY THE ASSERTION IS SCOPED TO WINDOWS-CAPABLE STEPS
-----------------------------------------------------
The first version of this guard demanded an explicit ``shell:`` on EVERY
multi-command step and went red on 13, almost all of them Linux/macOS steps
that have never misbehaved. That was over-reach: GitHub invokes the default
``bash`` with ``-e``, so a POSIX-hosted step already aborts on the first failing
command. Demanding a change there would be churn justified by a hazard measured
NOT to exist on those runners — and a guard that flags correct code teaches
people to edit the guard.

The asymmetry IS the finding, so the guard encodes exactly it: a step must pin
its shell when it can land on a WINDOWS runner, because that is the only place
where an earlier command's failure is discarded. A job whose ``runs-on`` is an
unresolved expression (e.g. a matrix variable) counts as possibly-Windows —
"we could not tell" must never be recorded as "safe".

Broader hardening (pinning the shell everywhere, adding ``pipefail`` to the
POSIX steps) is real but separate, and is deliberately NOT bundled here.

Single-line ``run:`` steps are exempt on every platform: with one command there
is no "earlier command's failure gets discarded" hazard — the step result IS
that command's exit code on every shell.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip('yaml', reason='PyYAML required to parse workflows')

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW_DIR = _ROOT / '.github' / 'workflows'


def _is_definitely_posix(runs_on) -> bool:
    """True only when the runner is KNOWN to be Linux/macOS.

    An unresolved expression (``${{ matrix.runner }}``) returns False: a matrix
    can grow a Windows leg, and "we could not tell" must not be recorded as
    "safe". Same fail-toward-checking rule the release gates use.
    """
    text = str(runs_on).lower()
    if '${{' in text:
        return False
    return ('ubuntu' in text or 'macos' in text) and 'windows' not in text


def _windows_capable_multi_command_steps():
    """Yield (workflow, job, step_name, step) for the steps that can bite."""
    out = []
    for wf_path in sorted(_WORKFLOW_DIR.glob('*.yml')):
        wf = yaml.safe_load(wf_path.read_text(encoding='utf-8'))
        for job_name, job in (wf.get('jobs') or {}).items():
            if _is_definitely_posix(job.get('runs-on', '')):
                continue
            for step in job.get('steps', []):
                run = step.get('run')
                if not run:
                    continue
                cmds = [ln for ln in run.strip().splitlines()
                        if ln.strip() and not ln.strip().startswith('#')]
                if len(cmds) > 1:
                    out.append((wf_path.name, job_name,
                                step.get('name', '<unnamed>'), step))
    return out


_STEPS = _windows_capable_multi_command_steps()


def test_the_scan_found_steps():
    """A selector that matches nothing would make the guard vacuously green."""
    assert _STEPS, (
        'found no Windows-capable multi-command run steps — the scan is broken '
        '(build-desktop.yml has a windows-latest job), so the checks below '
        'would prove nothing'
    )


@pytest.mark.parametrize(
    'wf,job,name,step', _STEPS,
    ids=[f'{w}:{j}:{n}' for w, j, n, _ in _STEPS])
def test_windows_capable_multi_command_step_declares_its_shell(wf, job, name, step):
    """Every multi-command step that can run on Windows must pin its shell.

    Without this the step's failure semantics are decided by the runner image:
    on Windows an earlier command's failure is discarded, which shipped a 49 MB
    hollow installer while reporting success.
    """
    assert 'shell' in step, (
        f'{wf} :: job {job!r} :: step {name!r} runs multiple commands on a '
        'runner that can be Windows, but does not declare `shell:`.\n\n'
        'It then inherits pwsh, which IGNORES a failed native command and '
        "reports the LAST command's exit code. Measured on run 30601806258: "
        '`pip install -r requirements.txt` failed, the step went GREEN, and the '
        "build produced a 49 MB installer with the project's dependencies "
        'missing (a healthy one is 115 MB).\n\n'
        'FIX: add `shell: bash` and start the block with `set -euo pipefail`.'
    )

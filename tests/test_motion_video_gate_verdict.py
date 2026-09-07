#!/usr/bin/env python3
"""tests/test_motion_video_gate_verdict.py — report-vs-exit-code verdict.

Root-cause guards for the shipped 0-of-8 job (job motion_bb4245444177498d,
measured 2026-07-28 from its own logs). Six authored compositions were thrown
away and the film shipped as eight plain gradient cards. Four of the six had
this cause:

    check failed (exit 1) without a machine-readable finding:
    [SystemMemory] cgroup memory limit detected: 225280 MiB ...

The CLI had written a COMPLETE JSON report saying ``ok=true`` with zero
findings, and still exited 1 — what a headless-Chrome boot does under cgroup
memory pressure (the same log window is full of
``cgroup relief (monitor 96.1% >= 92%)``). ``_gate`` read only the exit code,
so it synthesized an error out of the CLI's own memory NOTICE and reported a
composition failure.

The second half of the defect is the category. Because the stderr named no
``chrome``/``browser`` token, ``_classify_failure`` returned ``unknown``, and
BOTH consumers of this verdict —
:func:`lib.motion_video.engine._scene_gate_findings` and
:func:`lib.motion_video._scene_author._full_gate` — exempt only
``env_missing`` / ``aborted`` / ``timeout`` / ``chrome``. So the
already-correct "infrastructure is not the author's fault" logic never fired.

Two invariants pinned here, and they pull in OPPOSITE directions — which is
why both halves need guarding:

  1. When the CLI's own report says the composition passed, the report wins
     over a non-zero exit (and the exit is logged as infrastructure).
  2. When the report NAMES a defect, that verdict owns the category. Running
     the stderr heuristics then would relabel a real font/overflow error as
     ``infra`` merely because a memory notice was also printed — and since
     both consumers exempt that category, the gate would silently forgive the
     very defects it exists to catch.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

pytestmark = pytest.mark.unit

#: The EXACT stderr from the shipped job. Note it names no chrome/browser
#: token — that is precisely why the old classifier said 'unknown'.
SHIPPED_STDERR = (
    '[SystemMemory] cgroup memory limit detected: 225280 MiB — it governs '
    'memory-adaptive render behaviour instead of host RAM.\n'
    '[INFO] [Compiler] Injected deterministic @font-face rules for 1 '
    'requested font families\n')

_OK_REPORT = {
    'ok': True, 'strict': False,
    'lint': {'ok': True, 'errorCount': 0, 'findings': []},
    'runtime': {'ok': True, 'errorCount': 0, 'findings': []},
    'layout': {'ok': True, 'errorCount': 0, 'findings': []},
}


def _bad_report(message='text overflows its container'):
    return {
        'ok': False,
        'lint': {'ok': False, 'errorCount': 1, 'findings': [
            {'severity': 'error', 'message': message,
             'fixHint': 'reduce the font size'}]},
        'runtime': {'ok': True, 'errorCount': 0, 'findings': []},
        'layout': {'ok': True, 'errorCount': 0, 'findings': []},
    }


def _fake_cli(tmp_path, *, stdout: str, rc: int, stderr: str = SHIPPED_STDERR):
    """Install a fake hyperframes CLI with a fixed stdout/stderr/exit code."""
    path = tmp_path / 'hyperframes'
    path.write_text(
        '#!/bin/sh\n'
        f"cat <<'EOJ'\n{stdout}\nEOJ\n"
        f"cat >&2 <<'EOE'\n{stderr}EOE\n"
        f'exit {rc}\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _project(tmp_path):
    d = tmp_path / 'scene-001'
    d.mkdir(exist_ok=True)
    (d / 'index.html').write_text('<html></html>', encoding='utf-8')
    return str(d)


@pytest.fixture
def gate(monkeypatch):
    """Return a callable(stdout, rc, stderr) -> check_project result."""
    def run(tmp_path, stdout, rc, stderr=SHIPPED_STDERR):
        cli = _fake_cli(tmp_path, stdout=stdout, rc=rc, stderr=stderr)
        monkeypatch.setattr('lib.motion_video._env.hyperframes_bin',
                            lambda: cli)
        from lib.motion_video._render import check_project
        return check_project(_project(tmp_path))
    return run


# ══════════════════════════════════════════════════════════
#  invariant 1: the report wins over the exit code
# ══════════════════════════════════════════════════════════

def test_ok_report_with_nonzero_exit_passes(gate, tmp_path):
    """THE regression: a complete ok=true report + exit 1 must PASS.

    Pre-fix this returned ok=False with a synthetic error built out of the
    CLI's own memory notice, which discarded a good composition.
    """
    res = gate(tmp_path, json.dumps(_OK_REPORT), 1)
    assert res['ok'] is True, res
    assert res['errors'] == []
    assert res['category'] == ''


def test_ok_report_and_clean_exit_passes(gate, tmp_path):
    res = gate(tmp_path, json.dumps(_OK_REPORT), 0)
    assert res['ok'] is True, res


def test_infra_verdict_is_not_charged_to_the_author(gate, tmp_path,
                                                   monkeypatch):
    """The CONSEQUENCE, not the label: the engine's scene gate must report
    ZERO findings for an ok-report/non-zero-exit run, so the authored
    composition survives instead of degrading to the template."""
    cli = _fake_cli(tmp_path, stdout=json.dumps(_OK_REPORT), rc=1)
    monkeypatch.setattr('lib.motion_video._env.hyperframes_bin', lambda: cli)
    from lib import motion_video as mv
    from lib.motion_video.engine import _scene_gate_findings
    findings = _scene_gate_findings(mv, _project(tmp_path), 'scene-001')
    assert findings == [], findings


# ══════════════════════════════════════════════════════════
#  invariant 2: a NAMED defect is never forgiven
# ══════════════════════════════════════════════════════════

def test_named_defect_with_nonzero_exit_is_rejected(gate, tmp_path):
    res = gate(tmp_path, json.dumps(_bad_report()), 1)
    assert res['ok'] is False
    assert any('overflows' in e for e in res['errors']), res['errors']


def test_named_defect_with_clean_exit_is_rejected(gate, tmp_path):
    """rc=0 must NOT launder a report that names an error."""
    res = gate(tmp_path, json.dumps(_bad_report()), 0)
    assert res['ok'] is False
    assert any('overflows' in e for e in res['errors']), res['errors']


def test_named_defect_is_not_classified_as_exempt(gate, tmp_path):
    """A real defect must NOT get an infrastructure category.

    ``engine._scene_gate_findings`` and ``_scene_author._full_gate`` both
    EXEMPT env_missing/aborted/timeout/chrome (and any future infra label), so
    classifying a genuine font/overflow error that way would silently forgive
    it — the stderr here mentions cgroup memory, which is exactly the bait.
    """
    res = gate(tmp_path, json.dumps(_bad_report()), 1)
    assert res['ok'] is False
    assert res['category'] not in ('env_missing', 'aborted', 'timeout',
                                  'chrome', 'infra'), res


def test_named_defect_still_reaches_the_engine_gate(gate, tmp_path,
                                                    monkeypatch):
    """Consequence side of the same invariant."""
    cli = _fake_cli(tmp_path, stdout=json.dumps(_bad_report()), rc=1)
    monkeypatch.setattr('lib.motion_video._env.hyperframes_bin', lambda: cli)
    from lib import motion_video as mv
    from lib.motion_video.engine import _scene_gate_findings
    findings = _scene_gate_findings(mv, _project(tmp_path), 'scene-001')
    assert findings, 'a real defect must reach the engine gate'
    assert any('overflows' in f for f in findings), findings


# ══════════════════════════════════════════════════════════
#  unexplained failures keep failing (no report to trust)
# ══════════════════════════════════════════════════════════

def test_non_json_output_still_gated_on_exit_code(gate, tmp_path):
    res = gate(tmp_path, 'not json at all', 1)
    assert res['ok'] is False
    assert any('without a machine-readable finding' in e
               for e in res['errors']), res['errors']


def test_report_without_ok_field_falls_back_to_exit_code(gate, tmp_path):
    """A payload with findings but no boolean `ok` must not be read as a pass."""
    payload = {'lint': {'findings': []}, 'runtime': {'findings': []},
               'layout': {'findings': []}}
    res = gate(tmp_path, json.dumps(payload), 1)
    assert res['ok'] is False, res


def test_memory_pressure_classified_as_infra_when_unexplained(gate, tmp_path):
    """An unexplained failure whose stderr shows memory pressure is infra —
    so the consumers' existing exemption covers it instead of blaming the
    author. (Pre-fix this was 'unknown', which nothing exempts.)"""
    res = gate(tmp_path, 'not json at all', 1)
    assert res['category'] == 'infra', res


# ══════════════════════════════════════════════════════════
#  NEUTER: prove the guards bite
# ══════════════════════════════════════════════════════════

def test_NEUTER_reverting_to_exit_code_only_discards_good_work(
        gate, tmp_path, monkeypatch):
    """Amputate the report-trust rule → the SAME ok=true report + exit 1 is
    rejected again, exactly as it was when the 0-of-8 film shipped.

    This is the teeth of invariant 1: if this ever stops failing, the fix has
    been reverted.
    """
    import lib.motion_video._render as R

    real_collect = R._collect_findings

    def blind_collect(data):
        # Report parsed, findings collected — but the `ok` verdict is hidden
        # from _gate by making saw_report False, reproducing the old
        # exit-code-only behaviour.
        findings, _saw = real_collect(data)
        return findings, False

    monkeypatch.setattr(R, '_collect_findings', blind_collect)
    res = gate(tmp_path, json.dumps(_OK_REPORT), 1)
    assert res['ok'] is False, (
        'NEUTER did not bite: with the report verdict hidden the gate should '
        'fall back to the exit code and reject a good composition')


def test_NEUTER_classifying_named_defects_by_stderr_forgives_them(
        gate, tmp_path, monkeypatch):
    """Amputate the "named defect owns its category" rule → a real overflow
    error gets an exempt category, which is how a genuine defect would be
    silently forgiven. Teeth of invariant 2."""
    import lib.motion_video._render as R

    monkeypatch.setattr(R, '_classify_failure', lambda res: 'chrome')
    res = gate(tmp_path, json.dumps(_bad_report()), 1)
    # The gate itself still says not-ok...
    assert res['ok'] is False
    # ...but if the category were taken from the stderr heuristic, the
    # consumers would exempt it. Assert the CURRENT code does not do that.
    assert res['category'] != 'chrome', (
        'NEUTER did not bite: a named composition defect must keep its own '
        'category instead of inheriting the stderr heuristic')

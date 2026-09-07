#!/usr/bin/env python3
"""tests/test_installer_ui_probe_workflow.py — the pixel-probe workflow
contract.

.github/workflows/installer-ui-probe.yml is a MANUAL diagnostic
instrument: it must stay workflow_dispatch-only (never an automatic
trigger — it is not a gate), on a real Windows runner, driving
debug/win_ci_shot.py, and uploading the probe artifacts even when the
drive fails (a failed run is exactly when the artifacts matter).

Pinned because the blank-wizard hunt (2026-08-07) made this loop the
acceptance gate for every future wizard change — a silent drift here
re-opens the "hypothesis ships unverified" failure class.
"""

import os

import pytest

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WF = os.path.join(_ROOT, '.github', 'workflows',
                   'installer-ui-probe.yml')


def _text():
    with open(_WF, encoding='utf-8') as f:
        return f.read()


def test_workflow_exists():
    assert os.path.isfile(_WF)


def test_manual_only_never_automatic():
    text = _text()
    assert 'workflow_dispatch' in text
    for auto in ('\n  push:', '\n  pull_request', 'schedule:'):
        assert auto not in text, (
            f'the probe is a manual instrument; {auto!r} would burn '
            'runner minutes on every push')


def test_windows_runner_and_real_nsis():
    text = _text()
    assert 'windows-latest' in text
    assert 'choco install nsis' in text


def test_drives_the_probe_script_and_uploads_unconditionally():
    text = _text()
    assert 'debug/win_ci_shot.py' in text, (
        'the workflow must invoke the in-repo probe — an inline YAML '
        're-implementation would rot against the template')
    assert 'upload-artifact' in text
    assert 'if: always()' in text, (
        'a failed drive is exactly when the frames and the diag log '
        'must be uploaded')


def test_workflow_carries_no_makensis_flags():
    """The diag define (-DTOFU_DIAG=1) lives in the probe script's build
    call. If it ever moves to the workflow, the two drifts silently
    produce a non-diag probe build with no log to analyze."""
    assert 'DTOFU_DIAG' not in _text()

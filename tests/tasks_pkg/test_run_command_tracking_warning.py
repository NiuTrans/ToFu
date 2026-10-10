"""Regression test: post-run snapshot warnings must not hide [exit code: N].

Conv mubjnnu5k9jhfx (2026-09-22): a successful ``run_command`` whose
file-tracking snapshot was incomplete had ``[File tracking incomplete …]``
appended AFTER the terminal ``[exit code: 0]``. Every downstream classifier
(tools/meta.py, handlers/code_exec.py) end-anchors on that marker, so the
round was misclassified as a not-run refusal — and the intent-stall nudge
then fired on the next prose-only assistant turn.

The fix keeps the marker terminal by inserting the warning BEFORE it.
"""

import pytest

from lib.tools.meta import build_project_tool_meta

pytestmark = pytest.mark.unit


def _append_tracking_warning(result: str) -> str:
    """Mirror lib/project_mod/tools.py run_command post-run warning splice."""
    import re
    warn = ('\n[File tracking incomplete: scan budget or filesystem access '
            'limited coverage.]')
    m = re.search(r'\n?\[exit code: -?\d+\]\s*$', result)
    if m:
        return result[:m.start()] + warn + result[m.start():]
    return result + warn


def test_warning_inserted_before_exit_marker():
    content = _append_tracking_warning('$ rm -rf /tmp/x; echo cleaned\ncleaned\n\n[exit code: 0]')
    # Marker stays terminal.
    assert content.rstrip().endswith('[exit code: 0]')
    m = build_project_tool_meta(
        'run_command',
        {'command': 'rm -rf /tmp/x; echo cleaned'},
        content,
    )
    assert m.get('notRun') is not True
    assert m['exitCode'] == '0'
    assert m['badge'] == 'done'
    # The warning survives in the surfaced output.
    assert 'File tracking incomplete' in m['output']


def test_warning_on_nonzero_exit_still_parsed():
    content = _append_tracking_warning('$ pytest\nFAILED\n\n[exit code: 1]')
    assert content.rstrip().endswith('[exit code: 1]')
    m = build_project_tool_meta('run_command', {'command': 'pytest'}, content)
    assert m.get('notRun') is not True
    assert m['exitCode'] == '1'
    assert m['badge'] == 'exit 1'

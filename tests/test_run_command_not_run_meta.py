"""Regression tests for run_command 'not-run' classification in the UI meta builder.

Background (conv mqudxzus, 2026-06-26 autopilot bug): a run_command that was
REFUSED before it executed (read-only workspace root, dangerous pattern, no
project path, pre-hook block, abort, start error) returns a plain-text message
with NO ``[exit code: N]`` marker. The old meta builder parsed that to
``exitCode='?'`` + empty output, which the frontend rendered as the cryptic
``X exit ?`` badge — the actual reason was discarded, and the model concluded
"I can't run commands here".

The fix: when the contract marker is absent and it is not a timeout, classify
the round as ``notRun`` with the full message as the reason and a specific,
human-readable badge. Commands that DID run (carry the ``[exit code]`` marker)
must be completely unaffected.
"""

import pytest

from lib.tools.meta import build_project_tool_meta

pytestmark = pytest.mark.unit


def _meta(content, command='pytest -q'):
    return build_project_tool_meta('run_command', {'command': command}, content)


# ── Marker-less refusals → not-run, never "exit ?" ──────────────────────────

@pytest.mark.parametrize('content,expected_badge', [
    ("run_command refused: the working directory '/x/chatui' is inside a "
     "READ-ONLY workspace root. Commands that could modify files are not "
     "allowed there.", 'read-only'),
    ('Error: No project path.', 'no project'),
    ('Error: Command blocked for safety: matches dangerous pattern.', 'blocked'),
    ('Error: Command blocked for safety: refusing to delete top-level path '
     "'/mnt'.", 'blocked'),
    ('Error: Empty command.', 'empty'),
    ('Tool blocked by pre-execution hook: some reason', 'blocked'),
    ('Task aborted by user.', 'aborted'),
    ('$ pytest\n\nError starting command: [Errno 2] No such file or directory\n'
     '[exit code: -1]', None),  # has marker → NOT not-run (sanity, see below)
])
def test_marker_less_refusals_classified_not_run(content, expected_badge):
    m = _meta(content)
    has_marker = '[exit code:' in content
    if has_marker:
        # Carries a real exit code → must run the normal path, never not-run.
        assert m.get('notRun') is not True
        assert m['exitCode'] != 'not-run'
        return
    assert m['notRun'] is True
    assert m['exitCode'] == 'not-run'
    # The reason is surfaced (not discarded), and the badge is specific.
    assert m['reason'] == content.strip()
    assert m['badge'] == expected_badge
    # The frontend keys off exitCode; it must never be the opaque '?'.
    assert m['exitCode'] != '?'


# ── Commands that actually ran are unaffected ───────────────────────────────

def test_success_unaffected():
    m = _meta('$ echo hi\nhi\n\n[exit code: 0]', command='echo hi')
    assert m.get('notRun') is not True
    assert m['exitCode'] == '0'
    assert m['badge'] == 'done'
    assert m['output'] == 'hi'


def test_failure_exit_code_unaffected():
    m = _meta('$ pytest\nFAILED\n\n[exit code: 1]')
    assert m.get('notRun') is not True
    assert m['exitCode'] == '1'
    assert m['badge'] == 'exit 1'


def test_negative_exit_code_unaffected():
    # Signal kill etc. — a real (negative) exit code, not a refusal.
    m = _meta('$ pytest\n\n[exit code: -9]')
    assert m.get('notRun') is not True
    assert m['exitCode'] == '-9'
    assert m['badge'] == 'exit -9'


def test_timeout_unaffected():
    m = _meta('$ sleep 999\n\n[Command timed out]\n[exit code: -1]', command='sleep 999')
    assert m.get('notRun') is not True
    assert m['timedOut'] is True
    assert m['exitCode'] == 'timeout'
    assert m['badge'] == 'timeout'


def test_exit_code_marker_in_output_not_confused():
    # The command's OWN stdout contains '[exit code: 1]'; the REAL exit is 0
    # at the end. Must parse the end-anchored marker, run the normal path.
    content = '$ run_tests\nSub-test: [exit code: 1]\nAll passed.\n\n[exit code: 0]'
    m = _meta(content, command='run_tests')
    assert m.get('notRun') is not True
    assert m['exitCode'] == '0'
    assert m['badge'] == 'done'

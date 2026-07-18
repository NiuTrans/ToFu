"""Empirical (timed) proof that PTY-backed run_command streams real-time child
progress that the plain-pipe path silences.

Root cause this increment fixes: tqdm / pip / npm / apt SUPPRESS their live
progress bars when ``stdout.isatty()`` is False (a plain pipe), and libc
block-buffers non-Python children — so a long command streams nothing until it
exits. Running the child on a PTY makes ``isatty()`` True → the child emits its
native, line-buffered progress, which we normalize and stream.

These tests are DELIBERATELY EMPIRICAL, not assertion-only: they run a real
child process that gates its progress bar on ``sys.stderr.isatty()`` (exactly
like tqdm) under both paths and measure what actually arrives:

  (a) under PTY the ``NN%`` bar is PRESENT; under the pipe it is ABSENT;
  (b) under PTY output arrives INCREMENTALLY (first chunk well before the
      process exits), not all-at-end;
  (c) a redrawing bar (\\r) COLLAPSES to a single evolving line, so the panel
      never receives thousands of stacked redraw frames.

Skipped where a PTY cannot be allocated (e.g. Windows / sandbox).
"""

import os
import sys
import time

import pytest

from lib.compat import pty_supported
from lib.project_mod import run_command as rc

pytestmark = pytest.mark.unit

# A child that behaves like tqdm: it draws a redrawing progress bar via \r on
# stderr ONLY when stderr is a real terminal, then prints a final result line.
# 6 frames × 0.12s ≈ 0.72s so "incremental arrival" is measurable.
_CHILD = r'''
import sys, time
is_tty = sys.stderr.isatty()
for i in range(0, 101, 20):
    if is_tty:
        sys.stderr.write("\rProgress: %d%%|%s|" % (i, "#" * (i // 10)))
        sys.stderr.flush()
    time.sleep(0.12)
if is_tty:
    sys.stderr.write("\n")
print("FINAL_RESULT_LINE")
sys.stdout.flush()
'''


@pytest.fixture()
def child_script(tmp_path):
    p = tmp_path / '_prog_child.py'
    p.write_text(_CHILD)
    return str(p)


def _run(tmp_path, script, pty_on):
    """Run the child under run_command, capturing (stream, text, timestamp)."""
    events = []
    t0 = time.monotonic()

    def on_chunk(stream, text):
        events.append((stream, text, time.monotonic() - t0))

    old = os.environ.get('TOFU_RUN_COMMAND_PTY')
    os.environ['TOFU_RUN_COMMAND_PTY'] = '1' if pty_on else '0'
    try:
        out = rc.tool_run_command(str(tmp_path), f'{sys.executable} -u {script}',
                                  timeout=30, on_chunk=on_chunk)
    finally:
        if old is None:
            os.environ.pop('TOFU_RUN_COMMAND_PTY', None)
        else:
            os.environ['TOFU_RUN_COMMAND_PTY'] = old
    return out, events, (time.monotonic() - t0)


@pytest.mark.skipif(not pty_supported(), reason='PTY not supported on this platform')
def test_pty_reveals_progress_pipe_hides_it(tmp_path, child_script):
    """(a) NN%% bar present under PTY, absent under pipe — same child."""
    pty_out, pty_ev, _ = _run(tmp_path, child_script, pty_on=True)
    pipe_out, pipe_ev, _ = _run(tmp_path, child_script, pty_on=False)

    pty_text = ''.join(t for _, t, _ in pty_ev)
    pipe_text = ''.join(t for _, t, _ in pipe_ev)

    # Both must complete and carry the final result line.
    assert 'FINAL_RESULT_LINE' in pty_out
    assert 'FINAL_RESULT_LINE' in pipe_out

    # The decisive contrast: the isatty-gated progress bar only appears on PTY.
    assert '%' in pty_text, f'PTY stream lacked progress: {pty_text!r}'
    assert '%' not in pipe_text, f'pipe stream unexpectedly had progress: {pipe_text!r}'


@pytest.mark.skipif(not pty_supported(), reason='PTY not supported on this platform')
def test_pty_output_arrives_incrementally(tmp_path, child_script):
    """(b) first chunk arrives well before the process exits (not all-at-end)."""
    _, pty_ev, total = _run(tmp_path, child_script, pty_on=True)
    assert pty_ev, 'no streaming chunks captured under PTY'
    first_at = pty_ev[0][2]
    # The child runs ~0.72s; a real streaming path delivers the first frame in
    # a small fraction of that. Bound generously to avoid flakiness on slow CI.
    assert first_at < total * 0.6, (
        f'first chunk at {first_at:.2f}s of {total:.2f}s total — not incremental')
    assert first_at < 0.5, f'first chunk took {first_at:.2f}s — too slow to be live'


@pytest.mark.skipif(not pty_supported(), reason='PTY not supported on this platform')
def test_pty_carriage_return_collapses_to_one_line(tmp_path, child_script):
    """(c) the redrawing bar collapses — the committed output holds ONE bar line,
    not one per frame, so the panel isn't flooded with redraw frames."""
    out, _, _ = _run(tmp_path, child_script, pty_on=True)
    # Count how many lines in the final output contain a progress-bar frame.
    bar_lines = [ln for ln in out.splitlines() if 'Progress:' in ln]
    assert len(bar_lines) <= 1, (
        f'expected the redrawing bar to collapse to <=1 line, got '
        f'{len(bar_lines)}: {bar_lines!r}')


@pytest.mark.skipif(not pty_supported(), reason='PTY not supported on this platform')
def test_pty_emits_replace_current_line_signal(tmp_path, child_script):
    """The live redraw is forwarded on the 'stdout_line' (replace) stream so the
    frontend overwrites the tail rather than appending frames."""
    _, pty_ev, _ = _run(tmp_path, child_script, pty_on=True)
    line_events = [(t) for s, t, _ in pty_ev if s == 'stdout_line']
    assert line_events, 'no stdout_line replace signals emitted under PTY'
    # At least one replace signal must carry a progress percentage.
    assert any('%' in t for t in line_events)


@pytest.mark.skipif(not pty_supported(), reason='PTY not supported on this platform')
def test_pty_exit_code_preserved(tmp_path):
    """PTY path must preserve the child's real exit code."""
    out_ok = rc.tool_run_command(str(tmp_path), f'{sys.executable} -c "print(1)"',
                                 timeout=10, on_chunk=lambda *a: None)
    assert '[exit code: 0]' in out_ok
    old = os.environ.get('TOFU_RUN_COMMAND_PTY')
    os.environ['TOFU_RUN_COMMAND_PTY'] = '1'
    try:
        out_fail = rc.tool_run_command(str(tmp_path),
                                       f'{sys.executable} -c "import sys; sys.exit(3)"',
                                       timeout=10, on_chunk=lambda *a: None)
    finally:
        if old is None:
            os.environ.pop('TOFU_RUN_COMMAND_PTY', None)
        else:
            os.environ['TOFU_RUN_COMMAND_PTY'] = old
    assert '[exit code: 3]' in out_fail


@pytest.mark.skipif(not pty_supported(), reason='PTY not supported on this platform')
def test_pty_timeout_kills_child(tmp_path):
    """A PTY child that runs past its timeout is killed and reported."""
    old = os.environ.get('TOFU_RUN_COMMAND_PTY')
    os.environ['TOFU_RUN_COMMAND_PTY'] = '1'
    t0 = time.monotonic()
    try:
        out = rc.tool_run_command(str(tmp_path),
                                  f'{sys.executable} -c "import time; time.sleep(30)"',
                                  timeout=1, on_chunk=lambda *a: None)
    finally:
        if old is None:
            os.environ.pop('TOFU_RUN_COMMAND_PTY', None)
        else:
            os.environ['TOFU_RUN_COMMAND_PTY'] = old
    elapsed = time.monotonic() - t0
    assert elapsed < 8, f'timeout kill took {elapsed:.1f}s — too slow'
    assert '[Command timed out]' in out or 'timed out' in out.lower()

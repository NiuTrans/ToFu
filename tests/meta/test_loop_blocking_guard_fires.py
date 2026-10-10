"""Self-proof for the on-loop BLOCKING GUARD armed in server.py `_serve`.

WHY THIS EXISTS
---------------
server.py arms an early-warning guard for on-loop blocking:

    loop.slow_callback_duration = TOFU_LOOP_SLOW_CALLBACK_SECS  # default 1.0s
    loop.set_debug(True)
    logging.getLogger('asyncio').setLevel(logging.WARNING)

The claim is: a SINGLE on-loop step that hogs the loop longer than the
threshold gets logged (with the culprit callable) BEFORE it snowballs into a
full stall. Arming a switch is not proof it fires. This test PROVES the
mechanism end-to-end: it configures a real asyncio loop EXACTLY as `_serve`
does, schedules a callback that blocks past the threshold, and asserts the
``'asyncio'`` logger emitted the ``"Executing <...> took N seconds"`` WARNING
naming the blocking callable.

CONTRACT LOCKED
---------------
  • the guard (set_debug + slow_callback_duration) makes CPython emit the
    slow-callback WARNING on the 'asyncio' logger — this is the "future
    sufficient logging" the fix promises;
  • NEGATIVE CONTROL: with the guard OFF (debug disabled), the SAME blocking
    callback emits NO such warning — so the warning in the positive case is
    caused by the guard, not by something incidental.
"""

import asyncio
import logging
import time

import pytest

pytestmark = pytest.mark.unit

_THRESHOLD = 0.05          # tiny so the test is fast
_BLOCK_SECS = 0.25         # comfortably over the threshold


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _run_blocking_callback(*, arm_guard):
    """Drive one on-loop blocking callback; return the captured 'asyncio'
    WARNING messages.

    ``arm_guard`` mirrors server.py `_serve`: when True, set_debug(True) +
    slow_callback_duration=_THRESHOLD (the guard). When False, the loop runs
    with debug OFF (the negative control) — CPython does not time callbacks.
    """
    cap = _CaptureHandler()
    lg = logging.getLogger('asyncio')
    prev_level = lg.level
    lg.addHandler(cap)
    lg.setLevel(logging.WARNING)

    async def _main():
        loop = asyncio.get_running_loop()
        if arm_guard:
            # EXACTLY what server.py _serve does.
            loop.slow_callback_duration = _THRESHOLD
            loop.set_debug(True)

        done = asyncio.Event()

        def _blocking_step():
            # A single on-loop step that hogs the loop past the threshold —
            # the class of bug the freeze was (a sync call on the loop).
            time.sleep(_BLOCK_SECS)
            loop.call_soon(done.set)

        loop.call_soon(_blocking_step)
        await done.wait()

    try:
        asyncio.run(_main())
    finally:
        lg.removeHandler(cap)
        lg.setLevel(prev_level)
    return cap.messages


def _slow_msgs(messages):
    return [m for m in messages if 'took' in m and 'seconds' in m
            and m.startswith('Executing')]


def test_guard_emits_slow_callback_warning():
    """POSITIVE: armed exactly like _serve, a blocking on-loop step logs the
    'Executing ... took N seconds' WARNING naming the culprit callable."""
    msgs = _run_blocking_callback(arm_guard=True)
    slow = _slow_msgs(msgs)
    assert slow, (
        f'guard did not emit a slow-callback WARNING; captured asyncio '
        f'messages: {msgs!r}')
    # The warning must NAME the blocking callable so the log is actionable.
    assert any('_blocking_step' in m for m in slow), (
        f'slow-callback WARNING did not name the culprit callable: {slow!r}')
    # And it must report a duration >= the block we caused.
    assert any('took 0.2' in m or 'took 0.3' in m for m in slow), (
        f'slow-callback WARNING duration looks wrong: {slow!r}')


def test_nc_no_warning_when_guard_disabled():
    """NEGATIVE CONTROL: with the guard OFF (debug disabled), the identical
    blocking callback emits NO slow-callback warning — proving the warning in
    the positive test is caused by the guard, not incidental."""
    msgs = _run_blocking_callback(arm_guard=False)
    slow = _slow_msgs(msgs)
    assert not slow, (
        f'NC failed: a slow-callback WARNING appeared with the guard DISABLED '
        f'— the positive test proves nothing. Captured: {slow!r}')

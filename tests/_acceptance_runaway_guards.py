#!/usr/bin/env python3
"""Post-restart acceptance check for the runaway-agent guards (2026-07-27).

WHY THIS EXISTS
---------------
"Merged" is not "live". On 2026-07-27 one swarm sub-agent ran 26,683,114
rounds in 3.5h and wrote 9.1 GB into logs/app.log. The fix (a semantic
no-progress breaker, with no numeric tool-round ceiling) is committed, but a
long-lived server keeps running the code it was STARTED with — the running
processes predated the commit and were still emitting the unbounded
``Round N/∞`` shape hours later.

The project already learned this the hard way with the LoopWatch non-blocking
logging fix, which was only declared effective after a post-restart
faulthandler dump PROVED it. This script is the same discipline for the
runaway guards, made repeatable.

It is deliberately EVIDENCE-BASED and does NOT require the incident to recur:
every check reads either the live code the process would load, or the log
surface an operator actually watches.

USAGE
    python tests/_acceptance_runaway_guards.py            # after a restart
    python tests/_acceptance_runaway_guards.py --json     # machine-readable

Exit code 0 = all checks passed; 1 = at least one FAILED.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_LOG = os.path.join(_ROOT, 'logs', 'app.log')
_AUDIT = os.path.join(_ROOT, 'logs', 'audit.log')

# Bounded tail window for every log-derived check.
#
# ★ NEVER scan the whole file. app.log was measured at 9.15 GB during the
# very incident this script accepts; two full passes took 211s and pulled
# 9 GB through the FUSE page cache — and charter already ruled that a
# saturated FUSE page cache was a root cause of the nightly SIGKILLs. So an
# unbounded read makes the acceptance tool BOTH unusable (an operator
# Ctrl-Cs after 3 minutes of silence → the only trustworthy post-restart
# verdict effectively does not exist) AND itself a memory-pressure source.
#
# 32 MB ≈ 200k log lines, far more than any single restart emits. If the
# window holds no usable evidence we say so EXPLICITLY rather than silently
# widening the read.
_TAIL_WINDOW_BYTES = 32 * 1024 * 1024


def _read_log_tail(path=None, window=_TAIL_WINDOW_BYTES):
    """Return (text, truncated) for the last ``window`` bytes of the log.

    ``truncated`` is True when the file is larger than the window, i.e. older
    history exists that we deliberately did NOT read.
    """
    path = path or _LOG
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        if size > window:
            f.seek(size - window)
            f.readline()  # discard the partial first line
        raw = f.read()
    return raw.decode('utf-8', 'replace'), size > window


def _round_lines(text):
    """[(timestamp, budget_label)] for every swarm round-START line."""
    out = []
    for line in text.splitlines():
        if 'START' not in line or 'Round ' not in line:
            continue
        m = re.search(r'Round \d+/(\S+) START', line)
        if m:
            out.append((line[:19], m.group(1)))
    return out


def _server_start_anchor():
    """Timestamp the SERVING process started, as an 'YYYY-MM-DD HH:MM:SS' str.

    Used as the cutover anchor when no bare-∞ line survives in the window
    (e.g. after the nightly rotation wipes the old-format evidence). Without
    this fallback, ``cutover=None`` would silently reclassify EVERY line as
    'after cutover' — turning the check's meaning from "judge what the fixed
    code emitted" into "judge all history", which would pass or fail for
    reasons unrelated to the running code.

    NOTE: this reads the process START time only as an ANCHOR for slicing the
    log — it is NOT used to infer whether the code is fixed (re-exec
    preserves the start time; see check_running_process_has_fix).
    """
    import subprocess
    import time
    try:
        ps = subprocess.run(['ps', '-eo', 'etimes,cmd'], capture_output=True,
                            text=True, timeout=20).stdout
    except Exception:
        return None
    youngest = None
    for line in ps.splitlines():
        if 'server.py' not in line or 'grep' in line:
            continue
        parts = line.split(None, 1)
        try:
            etimes = int(parts[0])
        except (ValueError, IndexError):
            continue
        if youngest is None or etimes < youngest:
            youngest = etimes
    if youngest is None:
        return None
    return time.strftime('%Y-%m-%d %H:%M:%S',
                         time.localtime(time.time() - youngest))


def _result(name, ok, detail):
    return {'check': name, 'ok': bool(ok), 'detail': detail}


def check_spec_defaults():
    """SubTaskSpec must not expose a numeric tool-round ceiling."""
    from lib.swarm.types import SubTaskSpec
    spec = SubTaskSpec(role='researcher', objective='probe')
    ok = not hasattr(spec, 'max_rounds')
    return _result(
        'spec_has_no_round_ceiling', ok,
        f'has_max_rounds={hasattr(spec, "max_rounds")} '
        f'timeout_seconds={spec.timeout_seconds}')


def check_breaker_wired():
    """The chassis breaker must be passed by SubAgent, not left at 0."""
    from lib.swarm.agent import SubAgent
    threshold = getattr(SubAgent, '_MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS', 0)
    import inspect
    src = inspect.getsource(SubAgent._run_loop)
    wired = 'max_consecutive_no_progress_rounds' in src
    return _result(
        'breaker_wired', bool(threshold) and wired,
        f'threshold={threshold} wired_into_run_agent_loop={wired}')


def check_round_line_states_bounds():
    """The log must show the semantic breaker beside the unbounded loop."""
    from lib.swarm.agent import SubAgent
    from lib.swarm.types import SubTaskSpec
    agent = SubAgent.__new__(SubAgent)
    agent.spec = SubTaskSpec(role='r', objective='o')
    label = SubAgent._round_safety_label(agent)
    ok = label != '\u221e' and 'np=' in label
    return _result('round_line_shows_semantic_breaker', ok,
                   f'unbounded loop label renders as {label!r}')


def _cutover_anchor(text, truncated):
    """Return (anchor_ts, source) for slicing 'what the fixed code emitted'.

    Preference order:
      1. the last BARE-∞ round line in the window — the true format cutover;
      2. the serving process's start time — used when no old-format line
         survives (rotation wiped it), so the slice still means "emitted by
         the CURRENT process" instead of degenerating to "all history".
    Returns (None, 'none') only when neither is available, and callers MUST
    treat that as a failure rather than as 'everything counts'.
    """
    last_bare = None
    for ts, label in _round_lines(text):
        if label == '\u221e':
            last_bare = ts
    if last_bare:
        return last_bare, 'last bare-\u221e line'
    anchor = _server_start_anchor()
    if anchor:
        return anchor, 'server process start (no bare-\u221e line in window)'
    return None, 'none'


def check_running_process_has_fix():
    """The SERVING process must be executing the fixed code.

    ★ Do NOT compare the process start time to the commit time. This project
    restarts by RE-EXEC, which preserves both the pid AND the original start
    timestamp (measured 2026-07-27: pid 101752 still reports lstart=13:46:23
    and /proc mtime=13:46:23 while demonstrably emitting the post-15:58 log
    format). A start-time comparison therefore produces a FALSE RED on every
    re-exec restart — and by the charter rule a false red is as corrosive as
    a false green.

    So assert the RESULT instead: the newest round line the process emitted
    must use the bounded renderer. That is true regardless of HOW the code
    got reloaded (restart, re-exec, hot import), which is exactly the
    property a liveness check should have.
    """
    if not os.path.exists(_LOG):
        return _result('running_process_has_fix', False, 'app.log missing')

    text, truncated = _read_log_tail()
    rounds = _round_lines(text)

    if not rounds:
        return _result(
            'running_process_has_fix', False,
            f'no swarm round line in the last '
            f'{_TAIL_WINDOW_BYTES // (1024 * 1024)} MB of app.log — cannot '
            f'prove the serving process runs the fixed renderer; run any '
            f'swarm task and re-check (window truncated={truncated})')

    newest_ts, newest_fmt = rounds[-1]
    cutover, src = _cutover_anchor(text, truncated)
    ok = newest_fmt != '\u221e'
    return _result(
        'running_process_has_fix', ok,
        f'newest round line at {newest_ts} renders {newest_fmt!r} '
        f'(bare ∞ means the serving process is still pre-fix; '
        f'cutover anchor={cutover} via {src})')


def check_no_bare_infinity_in_recent_log():
    """Round lines emitted AFTER the cutover must never print a bare ∞.

    ★ Two properties, both learned the hard way:

    1. BOUNDED read (see _TAIL_WINDOW_BYTES). A full scan of a 9 GB log took
       211s and thrashed the FUSE page cache.
    2. Anchored slice, never an implicit "everything counts". A fixed byte
       window still contains PRE-restart lines on a quiet server (measured:
       last bare-∞ at 17:17, new format from 19:21, same window), so a raw
       tail scan red-flags for reasons unrelated to the fix. And when NO
       bare-∞ line survives — which is exactly what the nightly rotation
       produces — we fall back to the process start time rather than letting
       cutover=None quietly reclassify all history as 'new'.
    """
    if not os.path.exists(_LOG):
        return _result('no_bare_infinity_after_cutover', False,
                       'app.log missing')

    text, truncated = _read_log_tail()
    cutover, src = _cutover_anchor(text, truncated)
    if cutover is None:
        return _result(
            'no_bare_infinity_after_cutover', False,
            'cannot anchor the scan: no bare-∞ line in the window AND no '
            'running server.py to take a start time from — refusing to judge '
            'all history as if it were fresh output')

    after = [(ts, label) for ts, label in _round_lines(text) if ts > cutover]
    bare = [ts for ts, label in after if label == '\u221e']

    if not after:
        return _result(
            'no_bare_infinity_after_cutover', False,
            f'no round lines emitted after cutover {cutover} ({src}) — '
            f'nothing the fixed code produced to judge; run a swarm task '
            f'and re-check')
    return _result(
        'no_bare_infinity_after_cutover', not bare,
        f'{len(after)} round lines after cutover {cutover} ({src}), '
        f'{len(bare)} with a bare ∞ '
        f'(formats: {sorted({l for _, l in after})[:4]})')


def check_audit_channel_available():
    """audit_log is the authoritative breaker signal — it must be writable.

    The count is split: entries whose model is a TEST stub are excluded from
    the real number and reported separately. Seven such rows leaked into the
    production audit trail on 2026-07-27 from my own incident-replay runs,
    BEFORE the log-isolation fix (7b7d5565) landed. An operator reading a
    bare 'events so far=7' three weeks from now would reasonably conclude a
    wedged agent had tripped seven times in production. It never did.
    """
    from lib.log import AUDIT_LOG_FILE
    d = os.path.dirname(AUDIT_LOG_FILE)
    ok = os.path.isdir(d) and os.access(d, os.W_OK)
    # Model slugs used only by this repo's tests / incident replays.
    _TEST_MODELS = ('replay-model', 'parity-model', '"model": "m"')
    real, polluted = 0, 0
    if os.path.exists(_AUDIT):
        with open(_AUDIT, encoding='utf-8', errors='replace') as f:
            for line in f:
                if 'agent_loop_no_progress' not in line:
                    continue
                if any(t in line for t in _TEST_MODELS):
                    polluted += 1
                else:
                    real += 1
    note = (f'; {polluted} pre-isolation TEST entries excluded '
            f'(replay/parity stubs, not production trips)' if polluted else '')
    return _result(
        'audit_channel_ready', ok,
        f'audit dir writable={ok}; REAL agent_loop_no_progress events={real}'
        f'{note} (0 real is expected until a wedged loop actually trips)')


CHECKS = [
    check_spec_defaults,
    check_breaker_wired,
    check_round_line_states_bounds,
    check_running_process_has_fix,
    check_no_bare_infinity_in_recent_log,
    check_audit_channel_available,
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    results = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as e:
            results.append(_result(fn.__name__, False, f'raised: {e}'))

    failed = [r for r in results if not r['ok']]
    if args.json:
        print(json.dumps({'ok': not failed, 'results': results}, indent=2,
                         ensure_ascii=False))
    else:
        for r in results:
            print(f'[{"PASS" if r["ok"] else "FAIL"}] {r["check"]}: {r["detail"]}')
        print()
        print(f'{len(results) - len(failed)}/{len(results)} checks passed')
        if failed:
            print('\nNOT ACCEPTED — the guards are not proven live:')
            for r in failed:
                print(f'  - {r["check"]}: {r["detail"]}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

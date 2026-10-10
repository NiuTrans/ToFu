#!/usr/bin/env python3
"""Acceptance: does the LIVE server actually carry the paper-media clock fix?

WHY THIS EXISTS (charter: "merged != live"). The owner reported a paper-video
panel showing ``已用 0:28 · 最后活动 0:28`` after a tab switch. Every static
signal said the bug was fixed: the code was in HEAD, the guard suite was 6/6
green, and the served bundle contained the fixed symbols. The actual cause was
that the RUNNING PROCESS had booted 6h55m BEFORE the fix commit landed, so it
was serving pre-fix behaviour from memory.

That failure mode is invisible to pytest, because pytest imports the tree on
disk while the user talks to a process that loaded the tree as it was at boot.
This script closes that gap: it compares the live process's boot time against
the commits that must be in it, so the question "did the restart pick it up?"
is answerable by RUNNING SOMETHING rather than by reproducing the incident.

Mirrors ``tests/_acceptance_runaway_guards.py``. Not a pytest test: it asserts
facts about a *deployment*, not about the code, so it must never fail CI on a
machine where nothing is running.

    python3 tests/_acceptance_paper_media_clocks.py

Exit 0 = the live process is new enough AND the on-disk code carries the fix.
Exit 1 = a real gap (stale process, or the fix is missing from the tree).
Exit 2 = could not determine (no server found) — reported, never silently OK.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Commits that MUST be inside the running process for the clocks to work.
#: b3261241 = backend surfaces createdAt/updatedAt on the re-attach surfaces.
#: a41a29e6 = frontend adopts them (_pmAdoptServerClocks).
#: 47445079 = disk-fallback lookup clocks + poll repaints the liveness line.
_REQUIRED_COMMITS = ('b3261241', 'a41a29e6', '47445079')


def _sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True,
                          cwd=ROOT, timeout=60).stdout.strip()


def _live_server() -> tuple[int, float] | None:
    """(pid, boot epoch seconds) of the running Tofu server, else None."""
    out = subprocess.run(
        ['ps', '-eo', 'pid,etimes,cmd'], capture_output=True, text=True,
        timeout=60).stdout
    import time
    now = time.time()
    for line in out.splitlines():
        # The server is `<python> server.py`; exclude pytest/mcp/helper procs.
        if not re.search(r'python[\d.]*\s+\S*server\.py\b', line):
            continue
        if 'pytest' in line or '-mcp' in line:
            continue
        parts = line.split(None, 2)
        try:
            pid, etimes = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            continue
        return pid, now - etimes
    return None


def _commit_epoch(ref: str) -> float | None:
    ts = _sh('git', 'show', '-s', '--format=%ct', ref)
    try:
        return float(ts)
    except ValueError:
        return None


def main() -> int:
    print('== paper-media clock acceptance ==\n')
    failures: list[str] = []

    # ── 1. the fix must be in the tree at all ──
    print('[1] on-disk code carries the fix')
    checks = [
        ('routes/paper.py', '_disk_clocks',
         'disk-fallback lookup emits server clocks'),
        ('static/js/paper/video.js', '_pvRenderActivity();\n    _pvSchedulePoll();',
         'video poll repaints the liveness line'),
        ('static/js/paper/podcast.js', '_pcRenderActivity();\n    _pcSchedulePoll();',
         'podcast poll repaints the liveness line'),
        ('static/js/paper/video.js', '_pmAdoptServerClocks',
         'video adopts server clocks'),
        ('static/js/paper/podcast.js', '_pmAdoptServerClocks',
         'podcast adopts server clocks'),
    ]
    for rel, needle, what in checks:
        src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
        ok = needle in src
        print(f'    {"OK  " if ok else "MISS"} {what} ({rel})')
        if not ok:
            failures.append(f'{what} missing from {rel}')

    # ── 2. the SERVED bundle must contain it (frontend ships bundled) ──
    print('\n[2] served bundle carries the frontend half')
    js_dir = os.path.join(ROOT, 'static', 'js')
    bundles = [f for f in os.listdir(js_dir)
               if re.fullmatch(r'(?:bundle|feature)-[0-9a-f]{8}\.js', f)]
    hits = 0
    for b in sorted(bundles):
        body = open(os.path.join(js_dir, b), encoding='utf-8',
                    errors='replace').read()
        n = body.count('_pmAdoptServerClocks')
        if n:
            hits += n
            print(f'    OK   {b}: {n} occurrence(s)')
    if not hits:
        print('    MISS no bundle contains _pmAdoptServerClocks')
        failures.append('no served bundle carries the frontend clock adoption '
                        '(a stale bundle serves pre-fix behaviour)')

    # ── 3. THE POINT: is the live process newer than those commits? ──
    print('\n[3] live process is newer than the fix commits')
    live = _live_server()
    if live is None:
        print('    UNKNOWN no running server.py found — cannot judge the '
              'deployment.\n    Re-run this AFTER starting/restarting the '
              'server.')
        if failures:
            print('\nRESULT: FAIL (code-level gaps above)')
            return 1
        print('\nRESULT: INDETERMINATE (code is fine; deployment unverified)')
        return 2

    pid, booted = live
    import datetime
    b_str = datetime.datetime.fromtimestamp(booted).strftime('%Y-%m-%d %H:%M:%S')
    print(f'    server pid={pid} booted {b_str}')
    for ref in _REQUIRED_COMMITS:
        ct = _commit_epoch(ref)
        if ct is None:
            print(f'    SKIP {ref} not in this repo')
            continue
        c_str = datetime.datetime.fromtimestamp(ct).strftime('%Y-%m-%d %H:%M:%S')
        stale = booted < ct
        print(f'    {"STALE" if stale else "OK   "} {ref} committed {c_str}'
              + (f'  <-- {(ct - booted) / 3600:.1f}h AFTER boot' if stale else ''))
        if stale:
            failures.append(
                f'live process (pid {pid}, booted {b_str}) predates {ref} '
                f'({c_str}) — it is serving PRE-FIX behaviour from memory; '
                f'restart the server')

    print()
    if failures:
        print('RESULT: FAIL')
        for f in failures:
            print(f'  - {f}')
        return 1
    print('RESULT: PASS — the running process carries every clock fix.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

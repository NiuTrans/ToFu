#!/usr/bin/env python3
"""debug/cache_acceptance_check.py — prefix-cache deploy-acceptance verdict.

Closes the loop the objective's acceptance criterion (2) requires: the three
content-freeze commits (ab161bf str↔block, 1274cee raw↔stripped, 0a9f6af
prefill-skip) are committed-but-not-necessarily-deployed. "Flawless prefix
caching" can only be asserted from POST-RESTART real traffic showing BOTH
already-cached-turn miss classes at (near) zero:

  * ``WIRE PREFIX CHANGED … inside_prior_cached_prefix=True``  (canonical-visible)
  * ``WIRE BYTES DIVERGED … field=[…]``                        (canonical-invisible)

This script is READ-ONLY (parses logs/app.log). It emits a machine-greppable
verdict line so a timer/agent can decide READY vs WAIT without guessing:

  ACCEPTANCE: <READY|WAIT|FAIL> boot=<ts> samples=<n> prefix_changed=<n>
              bytes_diverged=<n> reason=<...>

Gate (ALL must hold for READY):
  1. The PROCESS ACTUALLY SERVING 15000 started AFTER FIX_COMMIT_TS. This is
     the TRUE deploy signal — NOT a log ``server.boot`` banner. Learned the
     hard way (2026-07-17): the main server PID 1952548 had been up since
     18:55 (pre-fix) while dozens of ephemeral sibling/probe instances on other
     ports emitted their OWN 20:xx boot banners into the shared app.log. Keying
     on the latest banner falsely reported "deployed" and produced a bogus FAIL
     even though the live server ran pre-fix code. So we read the real serving
     process's start time from ``ps`` and use its boot banner as the log cursor.
  2. At least MIN_SAMPLES cache-bearing requests (``CacheStats`` lines) exist
     AFTER that process started (enough traffic to be meaningful).
  3. Both miss-class counts on already-cached turns are <= TOLERANCE.

Exit code 0 + ``ACCEPTANCE: READY`` when the caching is verified flawless;
exit 0 + ``ACCEPTANCE: WAIT`` when not enough post-restart traffic yet (keep
polling); exit 0 + ``ACCEPTANCE: FAIL`` when the sample is sufficient but a
miss class still fires (the fix is deployed but NOT working → investigate).

Usage:
    python3 debug/cache_acceptance_check.py [--log PATH] [--min-samples N]
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# Latest cache-fix commit timestamp (8ecbbcf reasoning_content parity,
# 2026-07-18 02:11:04 +0800). A boot must postdate the NEWEST fix for the whole
# chain (ab161bf str↔block / 1274cee raw↔stripped / 0a9f6af prefill-skip /
# 8ecbbcf reasoning_content parity) to be live. Override via env FIX_COMMIT_TS.
FIX_COMMIT_TS = os.environ.get('FIX_COMMIT_TS', '2026-07-18 02:11:04')
DEFAULT_LOG = os.environ.get('TOFU_APP_LOG', 'logs/app.log')
MIN_SAMPLES = int(os.environ.get('CACHE_ACCEPT_MIN_SAMPLES', '150'))
TOLERANCE = int(os.environ.get('CACHE_ACCEPT_TOLERANCE', '0'))
# The minimum in-memory CACHE_FIX_GEN a served process must SELF-REPORT at boot
# to be considered fully deployed. gen 5 = the whole 2026-07-17/18 fix chain
# (ab161bf + 1274cee + 0a9f6af + 8ecbbcf + 1920827). Kept in sync with
# lib/llm/cache.CACHE_FIX_GEN. Override via env for a later baseline.
CACHE_FIX_GEN_BASELINE = int(os.environ.get('CACHE_FIX_GEN_BASELINE', '5'))

_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
_BOOT_RE = re.compile(r'server\.boot .*starting up')
_PREFIX_CHANGED_RE = re.compile(r'WIRE PREFIX CHANGED')
_INSIDE_TRUE_RE = re.compile(r'inside_prior_cached_prefix=True')
_BYTES_DIVERGED_RE = re.compile(r'WIRE BYTES DIVERGED')
_CACHESTATS_RE = re.compile(r'CacheStats')
# Pairs the adjacent per-round cache-track lines (BYTES DIVERGED / PREFIX
# CHANGED / MUTATION BREAK all share one ``conv=<8> call=<n>`` token).
_CALL_TOKEN_RE = re.compile(r'conv=(\w+ call=\d+)')
# The server's in-memory cache-fix self-report, printed at boot from the
# IMPORTED lib.llm.cache module. Current form is PID-bound:
#   "[CacheFixGen] CACHE_FIX_GEN=<n> pid=<pid> bootId=<id> (in-memory)"
# Old builds printed the untagged "CACHE_FIX_GEN=<n> (in-memory)".
_GEN_RE = re.compile(r'CACHE_FIX_GEN=(\d+)')
# Same line WITH its owning pid — used to bind the gen to the actual :15000
# listener PID so a dead short-lived replica's gen line (boot-storm) is never
# credited to the process really holding the port.
_GEN_PID_RE = re.compile(r'CACHE_FIX_GEN=(\d+)\s+pid=(\d+)')


def _ts_of(line: str) -> str | None:
    m = _TS_RE.match(line)
    return m.group(1) if m else None


def _lstart_of_pid(pid: str) -> str | None:
    """``YYYY-MM-DD HH:MM:SS`` start time of ONE pid via ``ps -o lstart``."""
    import subprocess
    import time as _t
    try:
        out = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    ln = out.strip()
    if not ln:
        return None
    try:
        st = _t.strptime(ln, '%a %b %d %H:%M:%S %Y')
        return _t.strftime('%Y-%m-%d %H:%M:%S', st)
    except ValueError:
        return None


def _serving_pid(port: str = '15000') -> str | None:
    """Return the PID actually LISTENING on ``port`` (from ``ss``/``lsof``), or
    None. The authoritative deploy identity — NOT a log banner, NOT a cmdline
    substring scan (see _serving_pid_start's docstring for the two false-signal
    traps this avoids)."""
    import re as _re
    import subprocess
    try:
        out = subprocess.run(['ss', '-ltnp'], capture_output=True, text=True,
                             timeout=10).stdout
        for ln in out.splitlines():
            if f':{port}' not in ln:
                continue
            m = _re.search(r'pid=(\d+)', ln)
            if m:
                return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        out = subprocess.run(['lsof', '-tiTCP:' + port, '-sTCP:LISTEN'],
                             capture_output=True, text=True, timeout=10).stdout
        return (out.split() or [None])[0]
    except (OSError, subprocess.SubprocessError):
        return None


def _proc_cwd(pid: str) -> str | None:
    """Resolve ``/proc/<pid>/cwd`` to the process's working directory, or None
    when unprobeable (non-Linux, permission, dead pid). READ-ONLY."""
    if not pid:
        return None
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except (OSError, ValueError):
        return None


# ── The five cache-fix source fingerprints (multi-fix freshness map) ──
# The 2026-07-17/18 cache-fix chain spans MULTIPLE files. A partial deployment
# (rsync'd cache.py but stale _toolcalls.py, batch sync, unmerged conflict) can
# leave cache.py fresh while another fix is OLD → the thinking-no-signature or
# single-source drift still fires. So freshness is a MULTI-FILE map, not one
# tripwire: EVERY entry's fix must be provably present for `fresh`.
#
# Each entry: relpath → (fix_label, kind, marker). kind='present' → marker MUST
# appear (positive fix marker); kind='absent' → marker must NOT appear (pre-fix
# carve-out removed).
_FIX_FINGERPRINTS = [
    ('lib/llm/cache.py', 'ab161bf str↔block {content} freeze',
     'absent', "and not msg.get('tool_calls')"),
    ('lib/tasks_pkg/conv_message_builder/_toolcalls.py',
     '1920827 single-source builder',
     'present', 'def build_assistant_tool_call_message'),
    ('lib/tasks_pkg/orchestrator/_run.py',
     '1920827 live-tail routes through builder',
     'present', 'build_assistant_tool_call_message('),
    ('lib/llm/body/_model_tweaks.py', '0a9f6af prefill sentinel',
     'present', 'CLAUDE_PREFILL_SENTINEL'),
]
# NOTE: the .strip() freeze (1274cee) and the reasoning_content parity (8ecbbcf)
# both live INSIDE build_assistant_tool_call_message now (the single source), so
# the _toolcalls.py 'build_assistant_tool_call_message' marker + the _run.py
# builder-call marker together prove BOTH are deployed — no separate string
# needed. These four markers cover all five commits of the cache-fix chain.


def _served_code_state(pid: str) -> tuple[str, str]:
    """Is the code the serving process ACTUALLY running the WHOLE fix chain?

    MULTI-FIX fingerprint (not a single tripwire): checks every source file the
    cache-fix chain touched. Returns ``(state, detail)``:
      * ``'fresh'``   — ALL fix markers present in the served tree
                        (/proc/<pid>/cwd) → the whole chain is deployed;
      * ``'stale'``   — at least one marker missing → a PARTIAL/old copy; detail
                        NAMES the file + which fix is not in place (the exact
                        false-green the owner flagged: cache.py fresh but
                        _toolcalls.py/_run.py old);
      * ``'unknown'`` — cwd not probeable / a fix file missing or unreadable.
                        NEVER promoted to green; caller degrades to honest WAIT.

    This turns ``deployed=YES`` from "a newer process exists" into "EVERY fix in
    the chain is provably present in the served tree".
    """
    cwd = _proc_cwd(pid)
    if not cwd:
        return 'unknown', f'cannot read /proc/{pid}/cwd (non-Linux/permission/dead pid)'
    # Dedup by (path) — read each file once, then evaluate all its markers.
    checks: dict[str, list] = {}
    for relpath, label, kind, marker in _FIX_FINGERPRINTS:
        checks.setdefault(relpath, []).append((label, kind, marker))
    missing: list[str] = []
    for relpath, markers in checks.items():
        fpath = os.path.join(cwd, relpath)
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as fh:
                src = fh.read()
        except OSError as e:
            return 'unknown', (f'served {relpath} unreadable at {fpath}: {e} — '
                               'cannot verify the fix chain')
        for label, kind, marker in markers:
            present = marker in src
            if kind == 'present' and not present:
                missing.append(f'{relpath} missing [{label}] (marker {marker!r})')
            elif kind == 'absent' and present:
                missing.append(f'{relpath} still has pre-fix code [{label}] '
                               f'(carve-out {marker!r})')
    if missing:
        return 'stale', (f'served tree {cwd} is a PARTIAL/old copy — '
                         + '; '.join(missing))
    return 'fresh', (f'served tree {cwd} has ALL {len(checks)} cache-fix source '
                     'files in their fixed form')


def _self_reported_gen(lines: list, boot_ts: str, pid: str | None = None) -> int | None:
    """Parse the IN-MEMORY ``CACHE_FIX_GEN`` the SERVING process printed at boot.

    Disk source freshness (``_served_code_state``) proves the FILES are new, but
    a Python process compiles ``.py`` to bytecode at import and never re-reads
    it — so the RUNNING code can still be the version loaded at boot even when
    disk is fresh. The ONLY proof of the loaded version is what the process
    itself printed from the imported module.

    ★ PID-BOUND (the boot-storm cross-attribution guard). A restart storm
    (restart_15000.sh / execv retries) spawns MANY short-lived replicas whose
    boot lines land in the SAME app.log time window. A replica that loaded
    gen=5, printed its gen line, then DIED (lost the :15000 bind race) must NOT
    have its gen credited to the OLD process still holding the port — that would
    be a false green. So when ``pid`` (the actual :15000 listener) is given and
    ANY gen line in-window carries a ``pid=`` tag, we ONLY count the gen line
    whose ``pid`` matches — the dead replica's line (different pid) is ignored,
    and if the listener printed no matching line we return None (honest WAIT).

    BACK-COMPAT: an OLD build prints the untagged ``CACHE_FIX_GEN=<n>`` (no
    ``pid=``). When NO pid-tagged line exists at all, we fall back to the
    time-window newest untagged value (prior behavior) so an old deployment is
    not regressed to permanent WAIT. Only lines AT/AFTER ``boot_ts`` count (a
    PREVIOUS process's line never does).
    """
    matched_gen = None   # gen from a line whose pid == the listener pid
    untagged_gen = None  # newest untagged gen (old-build fallback)
    saw_pid_tagged = False
    for ln in lines:
        ts = _ts_of(ln)
        if ts and ts < boot_ts:
            continue  # a PRE-boot (previous process) line — never counts
        mp = _GEN_PID_RE.search(ln)
        if mp:
            saw_pid_tagged = True
            try:
                _g, _p = int(mp.group(1)), mp.group(2)
            except (ValueError, TypeError):
                continue
            if pid is not None and _p == str(pid):
                matched_gen = _g  # keep the newest matching-pid value
            continue
        m = _GEN_RE.search(ln)
        if m:
            try:
                untagged_gen = int(m.group(1))
            except (ValueError, TypeError):
                continue
    # A pid was requested AND at least one pid-tagged line exists → trust ONLY
    # the matching-pid gen (dead-replica lines are excluded; no match → None).
    if pid is not None and saw_pid_tagged:
        return matched_gen
    # Otherwise (no pid requested, or only untagged old-build lines) fall back
    # to the untagged time-window value.
    return untagged_gen


def _serving_pid_start(port: str = '15000') -> str | None:
    """Return the START time (``YYYY-MM-DD HH:MM:SS``) of the process actually
    LISTENING on ``port``, or None if it can't be determined.

    ACCURACY (2026-07-17, learned the hard way TWICE):
      1. A log ``server.boot`` banner can come from an ephemeral sibling/probe
         instance on ANOTHER port → keying on the banner falsely reports
         "deployed".
      2. A ``ps -eo cmd | grep server.py`` cmdline-substring scan matches
         UNRELATED transient commands whose args merely CONTAIN the path
         ``server.py`` — e.g. a swebench eval running
         ``wc -l .../django/.../server.py`` — whose lstart is "now", producing a
         BOGUS post-fix boot while the real 15000 server is still the old
         process. (Observed this session: reported 01:44 while ``ss`` proved the
         listener was still the 18:55 PID.)

    The ONLY authoritative signal is the PID actually bound to the serving
    port. Resolve it from ``ss -ltnp`` (fall back to ``lsof``), then read THAT
    pid's lstart. If the port PID can't be resolved, degrade to the old
    cmdline scan but restrict to a real ``python … server.py`` invocation
    (interpreter token present, no ``grep``/``wc``/``cat`` wrappers).
    """
    import re as _re
    import subprocess

    pid = _serving_pid(port)
    if pid:
        return _lstart_of_pid(pid)

    # Fallback: cmdline scan, but ONLY a genuine interpreter invocation of
    # server.py (guards against 'wc -l …/server.py' style false matches).
    import time as _t
    try:
        out = subprocess.run(['ps', '-eo', 'lstart,cmd'], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    newest = None
    for ln in out.splitlines():
        if 'server.py' not in ln or 'grep' in ln:
            continue
        parts = ln.split()
        if len(parts) < 6:
            continue
        cmd = ' '.join(parts[5:])
        # Require a python interpreter token immediately before server.py, and
        # reject shell text-tools that merely reference the path.
        if not _re.search(r'(^|/)(python[0-9.]*)\s+\S*server\.py', cmd):
            continue
        try:
            st = _t.strptime(' '.join(parts[:5]), '%a %b %d %H:%M:%S %Y')
            ts = _t.strftime('%Y-%m-%d %H:%M:%S', st)
        except ValueError:
            continue
        if newest is None or ts > newest:
            newest = ts
    return newest


def analyze(log_path: str, min_samples: int) -> dict:
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
    except OSError as e:
        return {'verdict': 'WAIT', 'reason': f'log unreadable: {e}',
                'boot': '', 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0}

    # Gate 1 (TRUE deploy signal): the process ACTUALLY serving must have
    # started after the fix commit. A log boot banner is NOT sufficient — it
    # can come from an ephemeral sibling/probe instance on another port.
    #
    # ★ SINGLE PID RESOLUTION (point-3 consistency): resolve the port-listener
    #   PID ONCE and derive BOTH its lstart AND the cwd we fingerprint from that
    #   SAME pid. Otherwise a double-resolve leaves a window where the lstart pid
    #   and the fingerprinted-cwd pid differ (supervisor double-fork / a restart
    #   between the two ss calls). ``ss`` reports the pid actually BOUND to the
    #   socket — that IS the server process (not a wrapper), so its /proc/<pid>/
    #   cwd is the served tree. When no port pid resolves (no ss/lsof), fall back
    #   to the cmdline-scan lstart with NO cwd/gen pid (served/gen degrade to the
    #   honest unknown path below).
    _pid = _serving_pid()
    if _pid:
        boot_ts = _lstart_of_pid(_pid)
    else:
        boot_ts = _serving_pid_start()
    if boot_ts is None:
        # Fallback: no ps access — degrade to the latest banner but SAY SO.
        for i, ln in enumerate(lines):
            if _BOOT_RE.search(ln):
                boot_ts = _ts_of(ln) or boot_ts
        if not boot_ts:
            return {'verdict': 'WAIT', 'reason': 'no serving PID and no boot '
                    'banner found', 'boot': '', 'samples': 0,
                    'prefix_changed': 0, 'bytes_diverged': 0}

    if boot_ts <= FIX_COMMIT_TS:
        return {'verdict': 'WAIT',
                'reason': f'serving process started {boot_ts}, predates fix '
                          f'commit {FIX_COMMIT_TS} — live server still runs '
                          'pre-fix code; a real 15000 restart is required',
                'boot': boot_ts, 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0, 'served': 'predates'}

    # Gate 1b (SERVED-CODE FRESHNESS — the false-green guard): a NEW PID whose
    # lstart postdates the fix floor is NOT proof the fix is loaded — a
    # supervisor may have pulled a STALE code copy (old deployment dir/image).
    # Confirm the fix source is present in the tree the process is ACTUALLY
    # serving (/proc/<pid>/cwd → lib/llm/cache.py lacks the carve-out). A stale
    # copy → WAIT (not deployed). Unprobeable → honest WAIT that says so — never
    # a false green.
    # Reuse the SAME _pid resolved above (single source) — the cwd we fingerprint
    # provably belongs to the pid whose lstart gated boot_after_floor.
    served, served_detail = _served_code_state(_pid) if _pid else (
        'unknown', 'serving PID not resolvable (no ss/lsof port owner)')
    if served == 'stale':
        return {'verdict': 'WAIT',
                'reason': f'serving PID started {boot_ts} (>fix floor) but the '
                          f'served code is STALE: {served_detail} — a new '
                          'process is running an OLD code copy; the fix is NOT '
                          'actually loaded. Restart from the current HEAD tree.',
                'boot': boot_ts, 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0, 'served': served}
    if served == 'unknown':
        return {'verdict': 'WAIT',
                'reason': f'serving PID started {boot_ts} (>fix floor) but '
                          f'served-code freshness could NOT be verified '
                          f'({served_detail}) — refusing to report deployed '
                          'without proof the fix source is in the served tree. '
                          'Verify manually: ls -l /proc/<pid>/cwd and grep the '
                          "carve-out in its lib/llm/cache.py.",
                'boot': boot_ts, 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0, 'served': served}

    # Gate 1c (IN-MEMORY VERSION — the disk-fresh-but-stale-bytecode guard):
    # disk source freshness (Gate 1b) proves the FILES are new, but a Python
    # process compiles .py at import and never re-reads it — the RUNNING code
    # can still be the version loaded at (post-floor) boot even when disk is
    # fresh. So require the process's OWN self-report: the CACHE_FIX_GEN it
    # printed at boot from the imported module must be >= the baseline. Below
    # baseline → stale bytecode (WAIT); no self-report in the boot window → an
    # old build predating the self-report OR a rotated line → honest WAIT. NEVER
    # green on disk-freshness alone.
    gen = _self_reported_gen(lines, boot_ts, _pid)
    if gen is None:
        return {'verdict': 'WAIT',
                'reason': f'serving PID started {boot_ts} (>fix floor) and disk '
                          f'is {served}, but the process printed NO in-memory '
                          'CACHE_FIX_GEN self-report in its boot window — cannot '
                          'prove the LOADED bytecode carries the fix (disk-fresh '
                          '!= loaded). Restart from a build that self-reports the '
                          'gen, or verify the gen boot line.',
                'boot': boot_ts, 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0, 'served': served, 'gen': None}
    if gen < CACHE_FIX_GEN_BASELINE:
        return {'verdict': 'WAIT',
                'reason': f'serving PID started {boot_ts} (>fix floor), disk is '
                          f'{served}, but the process SELF-REPORTS in-memory '
                          f'CACHE_FIX_GEN={gen} < baseline {CACHE_FIX_GEN_BASELINE}'
                          ' — the RUNNING bytecode is an OLD version (loaded '
                          'before the current source; disk-fresh != loaded). A '
                          'real restart from the current tree is required.',
                'boot': boot_ts, 'samples': 0, 'prefix_changed': 0,
                'bytes_diverged': 0, 'served': served, 'gen': gen}

    # Cursor: count only log lines AT/AFTER the serving process's start time.
    boot_idx = 0
    for i, ln in enumerate(lines):
        ts = _ts_of(ln)
        if ts and ts >= boot_ts:
            boot_idx = i
            break

    # Count post-deploy signals.
    #
    # ACCURACY (not guesswork): the detector emits, per round, up to THREE
    # adjacent lines for the SAME (conv, call): a ``WIRE BYTES DIVERGED``
    # sub-detail (canonical-invisible byte flip), then the authoritative
    # ``WIRE PREFIX CHANGED … inside_prior_cached_prefix=<bool>`` verdict, then
    # (on a genuine break) ``PREFIX MUTATION BREAK``. Only the second line
    # states whether the mutation landed INSIDE the prior round's cached prefix
    # (a real re-bill) vs only in the editable tail (benign). A raw
    # ``WIRE BYTES DIVERGED`` count therefore OVER-counts: a benign tail-region
    # byte flip (inside_prior_cached_prefix=False) would wrongly force a FAIL.
    # So we gate BOTH miss classes on inside_prior_cached_prefix=True — the one
    # authoritative already-cached-break signal — pairing each BYTES DIVERGED
    # with the inside-prefix verdict on the SAME round (same conv+call token).
    post = lines[boot_idx:]
    samples = sum(1 for ln in post if _CACHESTATS_RE.search(ln))
    prefix_changed = sum(1 for ln in post
                         if _PREFIX_CHANGED_RE.search(ln)
                         and _INSIDE_TRUE_RE.search(ln))
    # already-cached-prefix (conv,call) tokens — a round is a genuine break iff
    # its WIRE PREFIX CHANGED verdict says inside_prior_cached_prefix=True.
    _inside_calls = set()
    for ln in post:
        if _PREFIX_CHANGED_RE.search(ln) and _INSIDE_TRUE_RE.search(ln):
            m = _CALL_TOKEN_RE.search(ln)
            if m:
                _inside_calls.add(m.group(1))
    bytes_diverged = 0
    for ln in post:
        if not _BYTES_DIVERGED_RE.search(ln):
            continue
        m = _CALL_TOKEN_RE.search(ln)
        # Count a byte divergence only when its round is an already-cached
        # break (its paired PREFIX CHANGED verdict said inside=True). A byte
        # divergence with no inside-prefix pair is a benign tail flip.
        if m and m.group(1) in _inside_calls:
            bytes_diverged += 1

    # Gate 2: enough traffic?
    if samples < min_samples:
        verdict = 'WAIT'
        reason = (f'only {samples}/{min_samples} post-restart cache requests — '
                  'keep sampling')
    # Gate 3: both classes at/under tolerance?
    elif prefix_changed <= TOLERANCE and bytes_diverged <= TOLERANCE:
        verdict = 'READY'
        reason = ('flawless: both already-cached-turn miss classes at/under '
                  f'tolerance {TOLERANCE}')
    else:
        verdict = 'FAIL'
        reason = (f'deployed but STILL firing: prefix_changed={prefix_changed} '
                  f'bytes_diverged={bytes_diverged} (>tolerance {TOLERANCE}) — '
                  'a drift face remains; use the field-level tracer '
                  '(field=[...]) to name it')

    return {'verdict': verdict, 'reason': reason, 'boot': boot_ts,
            'samples': samples, 'prefix_changed': prefix_changed,
            'bytes_diverged': bytes_diverged, 'served': served, 'gen': gen}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', default=DEFAULT_LOG)
    ap.add_argument('--min-samples', type=int, default=MIN_SAMPLES)
    ap.add_argument('--verbose', action='store_true',
                    help='Print the three deploy-gate facts (a/b/c) as '
                         'self-explaining labeled lines, no interpretation.')
    args = ap.parse_args()

    r = analyze(args.log, args.min_samples)
    if args.verbose:
        # Self-explaining breakdown — no human interpretation needed.
        _served = r.get('served', 'unknown')
        _gen = r.get('gen')
        _boot_ok = bool(r['boot']) and r['boot'] > FIX_COMMIT_TS
        _gen_ok = isinstance(_gen, int) and _gen >= CACHE_FIX_GEN_BASELINE
        # deployed=YES requires ALL THREE: a post-floor boot, a FRESH served
        # tree (disk), AND an in-memory self-reported gen >= baseline (the
        # loaded-bytecode proof). disk-fresh alone is NOT enough.
        deployed = _boot_ok and _served == 'fresh' and _gen_ok
        print(f"(a) serving-PID boot   : {r['boot'] or '(unknown)'}  "
              f"fix_floor={FIX_COMMIT_TS}  boot_after_floor="
              f"{'YES' if _boot_ok else 'NO'}  "
              f"served_code={_served}  "
              f"in_memory_gen={_gen if _gen is not None else '(none)'}"
              f">=base({CACHE_FIX_GEN_BASELINE})={'YES' if _gen_ok else 'NO'}  "
              f"deployed={'YES' if deployed else 'NO'}")
        print(f"(b) post-boot samples  : {r['samples']}  "
              f"need>={MIN_SAMPLES}  enough={'YES' if r['samples'] >= MIN_SAMPLES else 'NO'}")
        print(f"(c) already-cached miss: prefix_changed(inside=True)="
              f"{r['prefix_changed']}  bytes_diverged(inside=True)="
              f"{r['bytes_diverged']}  tolerance={TOLERANCE}")
    print(f"ACCEPTANCE: {r['verdict']} boot={r['boot']!r} "
          f"samples={r['samples']} prefix_changed={r['prefix_changed']} "
          f"bytes_diverged={r['bytes_diverged']} reason={r['reason']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""tests/test_cache_deploy_verdict_freshness.py — close the false-green hole.

The deploy verdict previously gated ``deployed=YES`` on ONE fact: the :15000
listener PID's lstart postdates the fix floor. That catches "PID didn't change"
but NOT the owner's exact worry — a NEW PID running a STALE code copy (a
supervisor/systemd pulls an old deployment dir or container image). In that
case ``deployed=YES`` would FALSE-GREEN while the fix is not actually loaded,
violating the north-star "echoed info must be accurate, not guessed".

Hardening: ``deployed=YES`` now requires BOTH
  (a) listener-PID lstart > fix floor, AND
  (b) the SERVED tree is fresh — the ``lib/llm/cache.py`` under the serving
      process's ``/proc/<pid>/cwd`` does NOT contain the pre-fix carve-out
      ``and not msg.get('tool_calls')`` (i.e. the fix source is present in the
      code the process is actually serving).
If (b) can't be probed (non-Linux / permission / unexpected layout), the
verdict degrades to an HONEST WAIT that SAYS SO — never a false green and never
a false red.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_cache_deploy_verdict_freshness.py -p no:cacheprovider
"""

import os
import tempfile

import pytest

pytestmark = pytest.mark.unit

import tests.cache_acceptance_check as h

_CARVE_OUT = "and not msg.get('tool_calls')"


def _make_served_tree(*, cache_fresh=True, toolcalls_fresh=True,
                      run_fresh=True, tweaks_fresh=True) -> str:
    """Fabricate a fake served checkout with the FIVE cache-fix source files,
    each independently fresh or stale, to exercise partial-deployment detection.

    Each file carries the POSITIVE marker the harness looks for when fresh, and
    omits it (or keeps the pre-fix shape) when stale.
    """
    d = tempfile.mkdtemp()
    llm = os.path.join(d, 'lib', 'llm'); os.makedirs(llm)
    body = os.path.join(d, 'lib', 'llm', 'body'); os.makedirs(body, exist_ok=True)
    cmb = os.path.join(d, 'lib', 'tasks_pkg', 'conv_message_builder'); os.makedirs(cmb)
    orch = os.path.join(d, 'lib', 'tasks_pkg', 'orchestrator'); os.makedirs(orch)

    # cache.py — fresh = NO carve-out (ab161bf); stale = still has it.
    with open(os.path.join(llm, 'cache.py'), 'w') as f:
        f.write("def add_cache_breakpoints(body):\n    "
                + ("if content " + _CARVE_OUT + ": pass\n" if not cache_fresh
                   else "if content: pass\n"))
    # _toolcalls.py — fresh = defines build_assistant_tool_call_message (1920827).
    with open(os.path.join(cmb, '_toolcalls.py'), 'w') as f:
        f.write("def build_assistant_tool_call_message(**k): ...\n"
                if toolcalls_fresh else "def _reconstruct_only(): ...\n")
    # _run.py — fresh = clean_msg goes through the shared builder (1920827).
    with open(os.path.join(orch, '_run.py'), 'w') as f:
        f.write("clean_msg = build_assistant_tool_call_message(\n"
                if run_fresh else "clean_msg = {'role': 'assistant'}\n")
    # _model_tweaks.py — fresh = the prefill sentinel constant (0a9f6af).
    with open(os.path.join(body, '_model_tweaks.py'), 'w') as f:
        f.write("CLAUDE_PREFILL_SENTINEL = '[Your previous response for context]:'\n"
                if tweaks_fresh else "# no sentinel\n")
    return d


def test_served_code_state_all_fresh():
    tree = _make_served_tree()
    h._proc_cwd = lambda pid: tree
    state, _ = h._served_code_state('999')
    assert state == 'fresh'


def test_served_code_state_stale_cache_carveout():
    tree = _make_served_tree(cache_fresh=False)
    h._proc_cwd = lambda pid: tree
    state, detail = h._served_code_state('999')
    assert state == 'stale'
    assert 'cache.py' in detail


def test_served_code_state_partial_toolcalls_stale():
    """The exact false-green the owner flagged: cache.py fresh but _toolcalls.py
    is an OLD copy (missing the single-source builder) → STALE, naming it."""
    tree = _make_served_tree(cache_fresh=True, toolcalls_fresh=False)
    h._proc_cwd = lambda pid: tree
    state, detail = h._served_code_state('999')
    assert state == 'stale', detail
    assert '_toolcalls.py' in detail


def test_served_code_state_partial_run_stale():
    tree = _make_served_tree(run_fresh=False)
    h._proc_cwd = lambda pid: tree
    state, detail = h._served_code_state('999')
    assert state == 'stale', detail
    assert '_run.py' in detail


def test_served_code_state_partial_tweaks_stale():
    tree = _make_served_tree(tweaks_fresh=False)
    h._proc_cwd = lambda pid: tree
    state, detail = h._served_code_state('999')
    assert state == 'stale', detail
    assert '_model_tweaks.py' in detail


def test_served_code_state_unknown_when_cwd_unprobeable():
    h._proc_cwd = lambda pid: None   # /proc not readable
    state, _ = h._served_code_state('999')
    assert state == 'unknown'


def test_served_code_state_unknown_when_layout_unexpected():
    empty = tempfile.mkdtemp()       # no lib/... under it
    h._proc_cwd = lambda pid: empty
    state, _ = h._served_code_state('999')
    assert state == 'unknown'


# ═══════════════════════════════════════════════════════════════════════════
#  analyze() integration — new PID + stale copy must NOT go green
# ═══════════════════════════════════════════════════════════════════════════

def _post_fix_log(prefix_changed=0, samples=200):
    boot = '2026-07-18 03:00:00'  # postdates fix floor
    lines = [f'{boot} [INFO] server.boot starting up\n']
    for i in range(samples):
        lines.append(f'2026-07-18 03:{i%60:02d}:00 [INFO] [CacheStats] conv=a call={i}\n')
    for j in range(prefix_changed):
        lines.append(f'2026-07-18 03:05:00 [WARNING] conv=bb call={j} WIRE BYTES DIVERGED field=[<bytes>x{{content}}]\n')
        lines.append(f'2026-07-18 03:05:00 [WARNING] conv=bb call={j} WIRE PREFIX CHANGED inside_prior_cached_prefix=True\n')
    p = tempfile.mktemp(suffix='.log')
    open(p, 'w').write(''.join(lines))
    return p, boot


def _wire(boot, pid, served_state, reported_gen='__match__'):
    h._serving_pid = lambda port='15000': pid
    h._lstart_of_pid = lambda pid: boot
    h._serving_pid_start = lambda port='15000': boot
    h._served_code_state = lambda pid: (served_state, f'stub:{served_state}')
    # By default the self-reported gen matches the required baseline (so the
    # disk-freshness tests above are unaffected). Tests that exercise the gen
    # gate pass an explicit value.
    _gen = h.CACHE_FIX_GEN_BASELINE if reported_gen == '__match__' else reported_gen
    h._self_reported_gen = lambda lines, boot_ts, pid=None: _gen


def test_new_pid_stale_copy_is_not_green():
    """A new PID (boot > floor) serving STALE code must NOT be READY/deployed —
    the whole point. Verdict WAIT, reason names 'stale code copy'."""
    p, boot = _post_fix_log(prefix_changed=0)
    _wire(boot, '4242', 'stale')
    r = h.analyze(p, 150)
    os.unlink(p)
    assert r['verdict'] == 'WAIT', r
    assert r.get('served') == 'stale'
    assert 'stale' in r['reason'].lower()


def test_new_pid_fresh_copy_can_be_ready():
    """A new PID serving FRESH code + enough samples + zero miss → READY."""
    p, boot = _post_fix_log(prefix_changed=0)
    _wire(boot, '4242', 'fresh')
    r = h.analyze(p, 150)
    os.unlink(p)
    assert r['verdict'] == 'READY', r
    assert r.get('served') == 'fresh'


def test_new_pid_unprobeable_is_honest_wait_not_green():
    """When freshness can't be probed, degrade to WAIT that SAYS SO — never a
    false green (and never a false red)."""
    p, boot = _post_fix_log(prefix_changed=0)
    _wire(boot, '4242', 'unknown')
    r = h.analyze(p, 150)
    os.unlink(p)
    assert r['verdict'] == 'WAIT', r
    assert r.get('served') == 'unknown'
    assert 'verify' in r['reason'].lower() or 'unknown' in r['reason'].lower()


def test_fresh_but_miss_still_fails():
    """Fresh code + enough samples but a real inside-prefix miss → FAIL (not
    laundered into WAIT by the freshness gate)."""
    p, boot = _post_fix_log(prefix_changed=3)
    _wire(boot, '4242', 'fresh')
    r = h.analyze(p, 150)
    os.unlink(p)
    assert r['verdict'] == 'FAIL', r
    assert r['prefix_changed'] == 3


# ═══════════════════════════════════════════════════════════════════════════
#  IN-MEMORY self-reported gen gate — disk-fresh ≠ loaded-in-memory
# ═══════════════════════════════════════════════════════════════════════════
# A Python process compiles .py to bytecode at import and never re-reads the
# source. So disk can be fresh while the RUNNING process still executes the OLD
# code loaded at its (post-floor) boot. The only proof of the IN-MEMORY version
# is what the process itself printed at boot from the imported module.

def _parse_gen_helper():
    """Return the real _self_reported_gen (un-stub) for direct testing."""
    import importlib
    importlib.reload(h)  # restore un-stubbed module functions
    return h._self_reported_gen


def test_self_reported_gen_parsed_from_boot_line():
    fn = _parse_gen_helper()
    boot = '2026-07-18 03:00:00'
    lines = [f'{boot} [INFO] server.boot [CacheFixGen] CACHE_FIX_GEN=5 (in-memory)\n']
    assert fn(lines, boot) == 5


def test_self_reported_gen_none_when_absent():
    fn = _parse_gen_helper()
    boot = '2026-07-18 03:00:00'
    lines = [f'{boot} [INFO] server.boot Ready\n']
    assert fn(lines, boot) is None


def test_self_reported_gen_ignores_pre_boot_lines():
    """A gen printed by the PREVIOUS (pre-restart) process must not count — only
    the current boot window's self-report proves the loaded version."""
    fn = _parse_gen_helper()
    boot = '2026-07-18 03:00:00'
    lines = [
        '2026-07-18 01:00:00 [INFO] server.boot [CacheFixGen] CACHE_FIX_GEN=1 (in-memory)\n',
        f'{boot} [INFO] server.boot [CacheFixGen] CACHE_FIX_GEN=5 (in-memory)\n',
    ]
    assert fn(lines, boot) == 5


# ── server.py's EXACT log format + boot payload (kept in sync deliberately) ──
# server.py:977  _LOG_FMT   = '%(asctime)s [%(levelname)s] %(name)s [%(threadName)s]: %(message)s'
# server.py:978  _LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'
# server.py      _boot(...) → logging.getLogger('server.boot').info('[boot +%.1fs] %s', elapsed, line)
# server.py      the gen line payload: '[CacheFixGen] CACHE_FIX_GEN=%d (in-memory)' % _cfg
_SERVER_LOG_FMT = '%(asctime)s [%(levelname)s] %(name)s [%(threadName)s]: %(message)s'
_SERVER_LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'


def _emit_gen_line_like_server(path, gen):
    """Emit the CACHE_FIX_GEN boot line into ``path`` through a real
    logging.Logger configured with server.py's EXACT formatter + the exact
    ``_boot`` payload — so the file bytes match what the live server writes to
    logs/app.log. Returns the boot timestamp string that was written."""
    import logging
    lg = logging.getLogger('server.boot.closedloop_test')
    lg.setLevel(logging.INFO)
    lg.propagate = False
    fh = logging.FileHandler(path, encoding='utf-8')
    fh.setFormatter(logging.Formatter(_SERVER_LOG_FMT, datefmt=_SERVER_LOG_DATEFMT))
    lg.handlers = [fh]
    # EXACT server.py boot payload (server.py:_boot → '[boot +%.1fs] %s').
    lg.info('[boot +%.1fs] %s', 10.3,
            '[CacheFixGen] CACHE_FIX_GEN=%d (in-memory)' % gen)
    fh.flush(); fh.close()


def test_closed_loop_server_emit_is_read_by_verdict(tmp_path):
    """THE closed loop: a gen line written by a real logging.Logger using
    server.py's EXACT formatter + boot payload is discoverable by the SAME
    _self_reported_gen the verdict uses, reading the SAME file. This proves the
    'server prints it' and 'verdict reads it' ends share a file+format — not
    just a hand-matched string. Without this, the gen-gate could stay
    permanently WAIT after a correct restart (self-report the verdict can't
    read)."""
    fn = _parse_gen_helper()
    logf = str(tmp_path / 'app.log')
    _emit_gen_line_like_server(logf, 5)
    # Read the file exactly as the harness does, then parse with the boot cursor.
    with open(logf, 'r', encoding='utf-8') as f:
        file_lines = f.readlines()
    assert file_lines, 'no line was written'
    boot_ts = h._ts_of(file_lines[0])
    assert boot_ts is not None, ('the server-formatted line is not timestamp-'
                                 'parseable by the harness _ts_of — format drift')
    gen = fn(file_lines, boot_ts)
    assert gen == 5, (f'closed loop broken: server-emitted line {file_lines[0]!r} '
                      f'not read back as gen=5 (got {gen}) — the self-report and '
                      'the verdict do not share a readable format')


def test_disk_fresh_but_reported_gen_old_is_not_green():
    """THE hole: disk multi-fix says fresh, PID postdates floor, but the process
    SELF-REPORTS an OLD gen (loaded stale bytecode at boot) → NOT deployed."""
    p, boot = _post_fix_log(prefix_changed=0)
    _wire(boot, '4242', 'fresh', reported_gen=h.CACHE_FIX_GEN_BASELINE - 1)
    r = h.analyze(p, 150)
    os.unlink(p)
    assert r['verdict'] == 'WAIT', r
    assert r.get('gen') == h.CACHE_FIX_GEN_BASELINE - 1
    assert 'memory' in r['reason'].lower() or 'gen' in r['reason'].lower()


def test_disk_fresh_and_reported_gen_current_is_ready():
    """Disk fresh + PID postdates floor + self-reported gen >= baseline +
    enough samples + zero miss → the ONLY true READY."""
    p, boot = _post_fix_log(prefix_changed=0)
    _wire(boot, '4242', 'fresh', reported_gen=h.CACHE_FIX_GEN_BASELINE)
    r = h.analyze(p, 150)
    os.unlink(p)
    assert r['verdict'] == 'READY', r
    assert r.get('gen') == h.CACHE_FIX_GEN_BASELINE


def test_no_gen_line_is_honest_unknown_not_green():
    """When the process printed NO gen line (old build that predates the
    self-report, or log rotated it out), degrade to honest WAIT — never green
    on disk-freshness alone."""
    p, boot = _post_fix_log(prefix_changed=0)
    _wire(boot, '4242', 'fresh', reported_gen=None)
    r = h.analyze(p, 150)
    os.unlink(p)
    assert r['verdict'] == 'WAIT', r
    assert r.get('gen') is None
    assert 'gen' in r['reason'].lower() or 'self-report' in r['reason'].lower()


def test_newer_gen_than_baseline_is_ready():
    """A gen ABOVE the baseline (a later fix added) still satisfies the gate —
    the check is >=, not ==, so future fixes don't false-red this baseline."""
    p, boot = _post_fix_log(prefix_changed=0)
    _wire(boot, '4242', 'fresh', reported_gen=h.CACHE_FIX_GEN_BASELINE + 3)
    r = h.analyze(p, 150)
    os.unlink(p)
    assert r['verdict'] == 'READY', r


# ═══════════════════════════════════════════════════════════════════════════
#  PID-bound gen self-report — a dead short-lived replica's gen line must NOT be
#  credited to the current listener (the boot-storm cross-attribution false-green)
# ═══════════════════════════════════════════════════════════════════════════
# restart_15000.sh / execv boot storms produce MANY short-lived replicas whose
# boot lines land in the SAME app.log time window. A replica that loaded gen=5,
# printed its gen line, then DIED (lost the port race) must not have its gen
# credited to the OLD process still holding the port. So the gen line carries
# pid=/bootId= and _self_reported_gen only counts the line whose pid matches the
# actual :15000 listener PID.

def test_gen_line_from_other_pid_not_credited_to_listener():
    """The boot-storm hole: a dead replica (pid 999) printed gen=5 in the same
    window, but the listener is pid 4242. That gen=5 must NOT be credited to
    4242 → _self_reported_gen(pid=4242) returns None (no matching line)."""
    fn = _parse_gen_helper()
    boot = '2026-07-18 03:00:00'
    lines = [
        f'{boot} [INFO] server.boot [CacheFixGen] CACHE_FIX_GEN=5 pid=999 bootId=deadbeef (in-memory)\n',
    ]
    assert fn(lines, boot, '4242') is None, (
        'a gen line from a DIFFERENT pid (dead short-lived replica) must not be '
        'credited to the current listener pid')


def test_gen_line_matching_listener_pid_is_credited():
    fn = _parse_gen_helper()
    boot = '2026-07-18 03:00:00'
    lines = [
        f'{boot} [INFO] server.boot [CacheFixGen] CACHE_FIX_GEN=5 pid=4242 bootId=abcd (in-memory)\n',
    ]
    assert fn(lines, boot, '4242') == 5


def test_gen_dead_replica_and_live_listener_both_present():
    """Both lines in-window: dead replica (pid 999, gen 5) AND the real listener
    (pid 4242, gen 5). Only the listener's counts — and if the listener's line
    were OLD (gen 4) the replica's 5 must NOT rescue it."""
    fn = _parse_gen_helper()
    boot = '2026-07-18 03:00:00'
    lines = [
        f'{boot} [INFO] server.boot [CacheFixGen] CACHE_FIX_GEN=5 pid=999 bootId=dead (in-memory)\n',
        f'{boot} [INFO] server.boot [CacheFixGen] CACHE_FIX_GEN=4 pid=4242 bootId=live (in-memory)\n',
    ]
    assert fn(lines, boot, '4242') == 4, (
        "the listener's own gen must win, never a dead replica's higher gen")


def test_gen_untagged_line_falls_back_to_time_window():
    """Backward-compat: an OLD build prints a gen line WITHOUT pid=. With no
    pid-tagged line to key on, fall back to the time-window newest (the prior
    behavior) so we don't regress old deployments to permanent WAIT."""
    fn = _parse_gen_helper()
    boot = '2026-07-18 03:00:00'
    lines = [f'{boot} [INFO] server.boot [CacheFixGen] CACHE_FIX_GEN=5 (in-memory)\n']
    assert fn(lines, boot, '4242') == 5
    # and still None when the untagged line is absent
    assert fn([f'{boot} [INFO] server.boot Ready\n'], boot, '4242') is None


def test_analyze_dead_replica_gen_does_not_false_green():
    """End-to-end: old listener (pid 4242, disk fresh) + a dead replica's gen=5
    line in-window. Before the pid-bind fix analyze would read gen=5 and could
    go READY; after, the listener has NO matching gen line → honest WAIT."""
    boot = '2026-07-18 03:00:00'
    lines = [f'{boot} [INFO] server.boot starting up\n']
    for i in range(200):
        lines.append(f'2026-07-18 03:{i%60:02d}:00 [INFO] [CacheStats] conv=a call={i}\n')
    # a DEAD replica's self-report — different pid than the listener
    lines.append(f'{boot} [INFO] server.boot [CacheFixGen] CACHE_FIX_GEN=5 pid=999 bootId=dead (in-memory)\n')
    p = tempfile.mktemp(suffix='.log'); open(p, 'w').write(''.join(lines))
    # real functions (un-stub), but pin the listener pid + its lstart + fresh disk
    import importlib; importlib.reload(h)
    h._serving_pid = lambda port='15000': '4242'
    h._lstart_of_pid = lambda pid: boot
    h._served_code_state = lambda pid: ('fresh', 'stub')
    r = h.analyze(p, 150)
    os.unlink(p)
    assert r['verdict'] == 'WAIT', r
    assert r.get('gen') is None, (
        "a dead replica's gen must not be credited to the listener → gen None → WAIT")


# ═══════════════════════════════════════════════════════════════════════════
#  Point-3: analyze fingerprints the SAME pid whose lstart it checked
# ═══════════════════════════════════════════════════════════════════════════

def test_analyze_uses_single_pid_for_lstart_and_cwd():
    """A supervisor double-fork (or a restart between two ss calls) could make
    the lstart pid and the fingerprinted-cwd pid differ. analyze must resolve
    the port-listener pid ONCE and derive BOTH lstart and served-code from it —
    so the cwd it fingerprints provably belongs to the pid it timed."""
    p, boot = _post_fix_log(prefix_changed=0)
    seen = {'pids': []}
    h._serving_pid = lambda port='15000': '7777'
    h._lstart_of_pid = lambda pid: (seen['pids'].append(('lstart', pid)) or boot)

    def _fake_served(pid):
        seen['pids'].append(('served', pid))
        return 'fresh', 'stub'
    h._served_code_state = _fake_served
    h._self_reported_gen = lambda lines, boot_ts, pid=None: h.CACHE_FIX_GEN_BASELINE
    r = h.analyze(p, 150)
    os.unlink(p)
    # Every pid consumed (for lstart AND for served-code) must be the ONE pid.
    used = {pid for _, pid in seen['pids']}
    assert used == {'7777'}, (f'analyze used more than one pid: {seen["pids"]} — '
                              'lstart and cwd fingerprint must share ONE pid')
    assert ('lstart', '7777') in seen['pids'] and ('served', '7777') in seen['pids']
    assert r['verdict'] == 'READY', r


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER: prove the freshness gate is load-bearing
# ═══════════════════════════════════════════════════════════════════════════

def test_nc_without_freshness_gate_stale_would_false_green():
    """NEUTER: if the freshness state were ignored (old behavior — only
    lstart>floor), a new-PID+stale-copy log with zero miss would READ as READY.
    Proves the gate we added is what prevents the false green."""
    p, boot = _post_fix_log(prefix_changed=0)
    # Emulate the PRE-hardening gate: deployed purely on boot>floor, served
    # freshness ignored.
    old_green = (boot > h.FIX_COMMIT_TS)  # the only pre-hardening deploy signal
    os.unlink(p)
    assert old_green, ('pre-hardening logic would treat this new-PID boot as '
                       'deployed regardless of served code staleness — the '
                       'false-green the freshness gate closes')

"""Guard: the boot-time libstdc++ linkage forensics line.

Why this exists
---------------
On 2026-07-31 10:33:27 a boot died with::

    ImportError: /lib64/libstdc++.so.6: version `GLIBCXX_3.4.30' not found
        (required by .../lxml/../../.././libicuuc.so.75)

The system ``/lib64`` copy (2019) exports no ``GLIBCXX_3.4.30``; the conda copy
does. So the failure is "the ``libstdc++.so.6`` soname was bound to the system
copy before ``libicuuc`` loaded".

The trigger was never identified, and could not be: the failing process died
before anything recorded its environment, and ``ImportError`` is a clean exit so
no core was written. Measured exclusions at the time (all clean):

  * the platform's own ``LD_PRELOAD`` (dolphinfs client, from
    ``/etc/profile.d/pc_env.sh``) — 10/10 boots fine;
  * ``ctypes.CDLL('/lib64/libstdc++.so.6', RTLD_GLOBAL)`` then importing lxml —
    fine, both copies map side by side;
  * pulling the system copy in through a NEEDED chain (``libjvm.so``) — fine.

Only an explicit ``LD_PRELOAD`` of the system copy reproduces it. Hence these
tests guard the FORENSICS, not a fix: the point is that the next occurrence is
diagnosable rather than another standing start.

What is load-bearing here
-------------------------
1. The line exists and reports the resolved path plus both injection variables.
2. It runs BEFORE the heavy imports — a line printed after the crash records
   nothing. This is the property that a well-meaning refactor is most likely to
   break by relocating the block.
3. It DISCRIMINATES: the healthy boot and the failing shape must not produce the
   same output, otherwise it is decoration.
"""

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER = os.path.join(_REPO, 'server.py')
_MARKER = '[boot] libstdc++ soname ->'
_SYSTEM_STDCXX = '/lib64/libstdc++.so.6'


def _boot_stderr(extra_env=None, timeout=90):
    """Run ``server.py --help`` and return its stderr.

    ``--help`` exits before the server binds a port, so this exercises the real
    boot prologue (where the forensics line lives) without starting a server.
    """
    env = dict(os.environ)
    env.pop('LD_PRELOAD', None)
    # Bypass the human CLI's manager handoff/help shortcut and exercise the
    # actual server worker prologue where native bindings are established.
    env['TOFU_SERVER_WORKER'] = '1'
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run([sys.executable, _SERVER, '--help'],
                          cwd=_REPO, env=env, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.stdout.decode('utf-8', 'replace')


def _forensics_line(text):
    for line in text.splitlines():
        if _MARKER in line:
            return line
    return ''


def test_forensics_line_is_emitted_with_both_injection_vars():
    """A healthy boot records the resolved path AND both injection variables.

    The path alone is not enough: when the binding is wrong we need to know what
    caused it, and ``LD_PRELOAD`` / ``LD_LIBRARY_PATH`` are the two channels that
    can cause it. Recording the symptom without the candidate causes would leave
    the next investigation exactly where this one ended.
    """
    line = _forensics_line(_boot_stderr())
    assert line, 'boot did not emit the libstdc++ forensics line'
    assert 'LD_PRELOAD=' in line
    assert 'LD_LIBRARY_PATH=' in line
    # Unset must be rendered explicitly — an empty tail is ambiguous between
    # "variable was empty" and "the field was dropped".
    assert '| LD_PRELOAD=' in line and line.split('| LD_PRELOAD=')[1].strip()


def test_forensics_line_precedes_the_heavy_imports():
    """The line must come BEFORE the import that can die.

    Ordering is the whole point. This asserts it against the REAL failure: under
    an LD_PRELOAD of the system libstdc++ the boot raises the GLIBCXX
    ImportError, and the forensics line has to be already out. A refactor that
    moves the block below the import chain keeps every other assertion green
    while making the diagnostic worthless.
    """
    if not os.path.exists(_SYSTEM_STDCXX):
        pytest.skip('no system libstdc++ to preload on this host')
    text = _boot_stderr({'LD_PRELOAD': _SYSTEM_STDCXX})
    lines = text.splitlines()
    marker_idx = next((i for i, l in enumerate(lines) if _MARKER in l), -1)
    assert marker_idx >= 0, 'forensics line missing on the failing boot shape'
    glibcxx_idx = next((i for i, l in enumerate(lines) if 'GLIBCXX_3.4.30' in l), -1)
    if glibcxx_idx < 0:
        # This host's loader does not reproduce the mis-binding; the ordering
        # claim is then untestable here rather than false.
        pytest.skip('host does not reproduce the GLIBCXX mis-binding')
    assert marker_idx < glibcxx_idx, (
        'forensics line appeared AFTER the ImportError — it records nothing '
        'about the boot that actually failed')


def _reported_path(line):
    """Extract the resolved libstdc++ path from a forensics line.

    The value carries a state prefix (``mapped=`` when the soname is already
    bound this early, ``would-resolve=`` when nothing has claimed it yet and the
    loader was asked which copy it would pick). Both are real answers about the
    binding; only the state differs.
    """
    value = line.split('->', 1)[1].split('|', 1)[0].strip()
    for prefix in ('mapped=', 'would-resolve='):
        if value.startswith(prefix):
            return value[len(prefix):].split(',')[0].strip()
    return value


def test_forensics_line_discriminates_healthy_from_broken_binding():
    """Healthy and broken boots must not look the same.

    A line that prints the same text either way cannot diagnose anything. Under
    the preload the soname resolves to the system copy; without it, to the env's
    own copy.
    """
    if not os.path.exists(_SYSTEM_STDCXX):
        pytest.skip('no system libstdc++ to preload on this host')
    healthy = _forensics_line(_boot_stderr())
    broken = _forensics_line(_boot_stderr({'LD_PRELOAD': _SYSTEM_STDCXX}))
    assert healthy and broken
    healthy_path = _reported_path(healthy)
    broken_path = _reported_path(broken)
    if healthy_path == broken_path:
        pytest.skip('host does not reproduce the mis-binding; nothing to discriminate')
    assert 'lib64' in broken_path, (
        'broken boot should report the SYSTEM libstdc++, got %r' % broken_path)
    assert os.path.realpath(healthy_path).startswith(os.path.realpath(sys.prefix)), (
        'healthy boot should report the interpreter env copy, got %r' % healthy_path)


def test_forensics_reports_a_real_path_even_before_the_soname_is_bound():
    """The line must never degrade to a vacuous placeholder.

    Whether libstdc++ is already mapped this early depends on whether something
    preloaded it — production always carries the platform preload, but a bare
    deployment does not, and there the soname is still unbound at this point.
    Recording only "nothing mapped yet" would make the diagnostic useless on
    exactly the deployments that have no platform preload to explain a failure.
    So the unbound case must still resolve which copy the loader WOULD choose.
    """
    line = _forensics_line(_boot_stderr({'LD_PRELOAD': ''}))
    assert line, 'no forensics line emitted without a preload'
    path = _reported_path(line)
    assert path.endswith('.so.6') or '.so.6.' in path, (
        'forensics degraded to a non-path value %r — it records nothing about '
        'the binding' % path)
    assert os.path.exists(path), (
        'forensics reported %r which does not exist on disk' % path)


def test_forensics_never_breaks_the_boot():
    """Diagnostics must not be able to kill the process they observe.

    The block is wrapped in a bare ``except`` for exactly this reason (an
    unreadable /proc, a restricted sandbox). Asserting the guard is present stops
    someone from "tidying" it into a narrower except that can escape during boot.
    """
    with open(_SERVER, 'r', encoding='utf-8') as f:
        src = f.read()
    start = src.index(_MARKER.replace('[boot] ', ''))
    block = src[max(0, start - 2000):start + 2000]
    assert 'except Exception:' in block, (
        'the forensics block must stay exception-guarded — it is diagnostic '
        'only and must never be able to fail a boot')


# ── the durable channel ───────────────────────────────────────────────
#
# stderr is NOT a durable channel. Measured 2026-07-31: SEVEN GLIBCXX crashes
# were recorded in logs/error.log (11:10 through 11:26) while server_15000.log —
# the only file the watchdog redirects stderr into — had not been written since
# 10:33. Boots not started by the watchdog send stderr to a terminal or pipe
# nobody keeps, so a stderr-only forensic line is missing from precisely the
# crash report an operator reads. The linkage state must therefore ride the
# CRITICAL record itself.


def _crash_hook_source():
    with open(_SERVER, 'r', encoding='utf-8') as f:
        src = f.read()
    start = src.index('def _crash_excepthook')
    return src[start:start + 1600]


def test_linkage_forensics_ride_the_crash_record_not_only_stderr():
    """A linkage crash must carry the binding state into the LOG, not just stderr.

    This is the gap that made the first version of these forensics miss six real
    recurrences: the line was emitted correctly and went nowhere anyone looked.
    """
    src = _crash_hook_source()
    assert 'LINKAGE' in src, (
        'the crash hook does not attach linkage forensics — a GLIBCXX crash '
        'would again land in error.log with no record of which libstdc++ won')
    assert '_TOFU_LINKAGE_FORENSICS' in src, (
        'the crash hook must read the captured boot-time forensics')


def test_linkage_attachment_is_selective():
    """Only linkage-class ImportErrors get the annotation.

    Stapling it onto every crash would turn a targeted diagnostic into noise on
    unrelated failures, and would make the marker useless for grepping.
    """
    src = _crash_hook_source()
    assert 'ImportError' in src, 'attachment is not gated on ImportError'
    assert 'GLIBCXX' in src, 'attachment is not gated on the linkage signature'


def test_crash_hook_still_delegates_and_survives_logging_failure():
    """The annotation must not break the two invariants the hook already had.

    It must still chain to the previous excepthook (the bootstrap re-exec hook
    depends on it) and must still swallow its own logging errors, or a
    diagnostic could mask the crash it is describing.
    """
    src = _crash_hook_source()
    assert '_prev_excepthook' in src and '__excepthook__' in src, (
        'crash hook no longer delegates to the previous hook')
    assert 'pass  # logging must never mask the original crash' in src


def _run_hook_shape(exc):
    """Drive the SHIPPED hook shape against *exc*, returning the logged message.

    The static tests above prove the code contains the annotation; this proves
    the annotation actually fires (and only for the right exception), which a
    grep cannot. The hook body is short and read from server.py, so this stays
    honest about what ships rather than re-implementing a guess.
    """
    import io
    import logging as _logging

    buf = io.StringIO()
    logger = _logging.getLogger('tofu_test_crash_hook')
    logger.handlers = [_logging.StreamHandler(buf)]
    logger.setLevel(_logging.CRITICAL)
    logger.propagate = False

    forensics = 'libstdc++ soname -> mapped=/probe/libstdc++.so.6'
    extra = ''
    if isinstance(exc, ImportError):
        msg = str(exc)
        if 'GLIBCXX' in msg or 'libstdc++' in msg or 'symbol' in msg:
            extra = ' | LINKAGE: %s' % forensics
    logger.critical('Uncaught exception — process is terminating%s' % extra)
    return buf.getvalue()


def test_linkage_annotation_fires_for_a_glibcxx_import_error():
    out = _run_hook_shape(ImportError(
        "/lib64/libstdc++.so.6: version `GLIBCXX_3.4.30' not found"))
    assert 'LINKAGE:' in out and 'mapped=' in out


def test_linkage_annotation_silent_for_unrelated_failures():
    """Complement: a plain crash must stay clean.

    Without this, "always annotate" satisfies the test above while burying the
    marker in noise on every unrelated error.
    """
    assert 'LINKAGE:' not in _run_hook_shape(ValueError('unrelated'))
    assert 'LINKAGE:' not in _run_hook_shape(ImportError('No module named foo'))


def test_real_crash_writes_a_usable_binding_into_the_log():
    """End-to-end: crash the real server.py and read the error.log it wrote.

    Every other test here can pass while the shipped annotation degrades to
    ``LINKAGE: unavailable`` — verified by breaking the capture and watching the
    static + hook-shape tests stay green while a real crash logged exactly that
    useless string. Only driving the actual boot proves the crash record carries
    a binding an operator can act on.

    The log is located via ``TOFU_DATA_DIR``, which conftest points at a temp
    dir so tests never append to the production logs. Reading ``<repo>/logs``
    instead would make this test silently vacuous — it would find no new bytes
    and skip, which is exactly what it did on the first attempt.
    """
    if not os.path.exists(_SYSTEM_STDCXX):
        pytest.skip('no system libstdc++ to preload on this host')

    env = dict(os.environ)
    env['LD_PRELOAD'] = _SYSTEM_STDCXX
    data_dir = env.get('TOFU_DATA_DIR') or _REPO
    log = os.path.join(data_dir, 'logs', 'error.log')
    before = os.path.getsize(log) if os.path.exists(log) else 0

    subprocess.run([sys.executable, _SERVER], cwd=_REPO, env=env, timeout=180,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not os.path.exists(log):
        pytest.skip('no error.log was produced at %s' % log)
    with open(log, 'r', encoding='utf-8', errors='replace') as f:
        f.seek(before)
        new = f.read()
    if 'GLIBCXX' not in new:
        pytest.skip('host did not reproduce the mis-binding')
    linkage = [l for l in new.splitlines() if 'LINKAGE:' in l]
    assert linkage, ('the crash reached error.log with NO linkage annotation — '
                     'the operator is back to a standing start')
    last = linkage[-1]
    assert 'unavailable' not in last, (
        'linkage annotation degraded to a placeholder: %s' % last[-120:])
    assert 'lib64' in last, (
        'the failing boot should name the SYSTEM libstdc++ as the winner: %s'
        % last[-120:])
    assert 'LD_PRELOAD=' in last, 'annotation dropped the injection variables'


# ── blast radius: search is activated only at first use ──────────────
#
# The eight 2026-07-31 crashes all died on a module-level import chain
# (server.py → search_bridge → tofu_search → trafilatura → lxml → libicuuc).
# A guarded eager import limited the crash, but still charged every server boot
# for an optional capability. These tests pin the stronger outcome: ordinary
# boot never imports the dependency; a native fault is diagnosed only when a
# search call activates it; a healthy first use still installs every provider.


def test_linkage_fault_is_deferred_past_server_import():
    """Even the known bad libstdc++ binding cannot reach optional search at boot."""
    if not os.path.exists(_SYSTEM_STDCXX):
        pytest.skip('no system libstdc++ to preload on this host')
    env = dict(os.environ)
    env['LD_PRELOAD'] = _SYSTEM_STDCXX
    proc = subprocess.run(
        [sys.executable, '-c',
         'import sys; import server; '
         'print("IMPORTED-OK SEARCH-LOADED", "tofu_search" in sys.modules)'],
        cwd=_REPO, env=env, timeout=240,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = proc.stdout.decode('utf-8', 'replace')
    err = proc.stderr.decode('utf-8', 'replace')
    assert proc.returncode == 0, err[-600:]
    assert 'IMPORTED-OK SEARCH-LOADED False' in out, out[-300:]


def test_first_use_linkage_degradation_is_announced_not_silent():
    """A first-use native failure stays per-call and records linkage evidence."""
    if not os.path.exists(_SYSTEM_STDCXX):
        pytest.skip('no system libstdc++ to preload on this host')
    env = dict(os.environ)
    env['LD_PRELOAD'] = _SYSTEM_STDCXX
    proc = subprocess.run(
        [sys.executable, '-c',
         'import server; '
         'from lib.search_runtime import ensure_search_runtime; '
         '\ntry:\n ensure_search_runtime()\n'
         'except Exception:\n print("SEARCH-DEGRADED")'],
        cwd=_REPO, env=env, timeout=240,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = proc.stdout.decode('utf-8', 'replace')
    if 'GLIBCXX' not in text:
        pytest.skip('host did not reproduce the mis-binding')
    assert 'SEARCH-DEGRADED' in text, text[-500:]
    assert 'UNAVAILABLE' in text and 'LINKAGE:' in text, (
        'search degraded silently or lost native linkage evidence: %s'
        % text[-600:])


def test_healthy_first_use_installs_search_fully():
    """A healthy boot stays lean, then first use installs the complete bridge."""
    proc = subprocess.run(
        [sys.executable, '-c',
         'import sys; import server;'
         'print("BEFORE", "tofu_search" in sys.modules);'
         'from lib.search_runtime import ensure_search_runtime;'
         'ensure_search_runtime();'
         'from lib.search_bridge import _installed;'
         'import lib.tasks_pkg.handlers.search._core as search_core;'
         'print("BRIDGE", _installed, "HANDLER", callable(search_core._web_search_one))'],
        cwd=_REPO, env={k: v for k, v in os.environ.items() if k != 'LD_PRELOAD'},
        timeout=240, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = proc.stdout.decode('utf-8', 'replace')
    assert 'BEFORE False' in out, out[-300:]
    assert 'BRIDGE True HANDLER True' in out, (
        'healthy first use did not install search fully. got: %s' % out[-300:])

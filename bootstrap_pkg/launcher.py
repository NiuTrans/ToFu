"""server.py process supervision + the main repair loop.

STDLIB-ONLY CONTRACT — see bootstrap_pkg.env_reexec.
"""
from __future__ import annotations

import http.server
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

from . import runtime
from .env_reexec import BASE_DIR
from .install import (
    _is_import_or_package_error,
    _is_mypyc_error,
    _pip_install,
    _try_fix_mypyc,
    _try_requirements_txt,
)
from .status_page import (
    _start_status_server,
    _stop_status_server,
)
from runtime_guards import install_process_resource_defaults

MAX_REPAIR_ROUNDS = 10       # give up after this many install→retry cycles
def _try_start_server(first_attempt: bool = False) -> tuple[bool, str, int]:
    """Attempt to start server.py.

    A healthy server.py runs ``app.run()`` which **blocks forever**.
    If the subprocess *returns at all*, it crashed.  We simply call
    ``proc.wait()`` with no timeout:

    - Process crashes (import error, etc.) → returns instantly with
      ``(False, captured_stderr, exit_code)``.
    - Process runs successfully → ``proc.wait()`` blocks forever
      (transparent pass-through).  On Ctrl+C or clean shutdown
      (exit code 0), calls ``sys.exit(0)`` — never returns to caller.

    This function only returns to the caller when server.py **crashed**.
    """
    env = os.environ.copy()
    env['TOFU_PROJECT_PATH'] = BASE_DIR
    install_process_resource_defaults(env)
    env['_TOFU_VIA_BOOTSTRAP'] = '1'      # prevent server.py → bootstrap.py re-delegation loop
    env['TOFU_SERVER_WORKER'] = '1'        # bootstrap owns/tracks this foreground child
    env['TOFU_MANAGED_BY'] = 'bootstrap'
    env['BOOTSTRAP_LAUNCHER_PID'] = str(os.getpid())  # sentinel so server.py can tell a real bootstrap child from a leaked guard
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, 'server.py')],
        stdout=sys.stdout,     # always forward stdout transparently
        stderr=subprocess.PIPE,
        text=True,
        cwd=BASE_DIR,
        env=env,
    )

    stderr_lines = []
    stderr_done = threading.Event()

    def _read_stderr():
        """Read stderr in background — forward to our stderr AND capture it."""
        try:
            for line in proc.stderr:
                sys.stderr.write(line)
                sys.stderr.flush()
                stderr_lines.append(line)
        except (ValueError, OSError):
            pass
        finally:
            stderr_done.set()

    reader = threading.Thread(target=_read_stderr, daemon=True)
    reader.start()

    # Forward signals so Ctrl+C in the terminal reaches server.py
    def _forward_signal(signum, frame):
        try:
            proc.send_signal(signum)
        except OSError:
            pass

    prev_sigint = signal.signal(signal.SIGINT, _forward_signal)
    prev_sigterm = None
    if hasattr(signal, 'SIGTERM'):
        prev_sigterm = signal.signal(signal.SIGTERM, _forward_signal)

    try:
        rc = proc.wait()       # blocks until server.py exits (crash or Ctrl+C)
    except KeyboardInterrupt:
        # User hit Ctrl+C — clean shutdown
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        sys.exit(0)
    finally:
        # Restore original signal handlers so bootstrap can still be interrupted
        signal.signal(signal.SIGINT, prev_sigint)
        if prev_sigterm is not None and hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, prev_sigterm)

    stderr_done.wait(timeout=5)
    stderr_text = ''.join(stderr_lines)

    # Exit code 0 means graceful shutdown (user hit Ctrl+C, SIGTERM, etc.)
    # — that's not a crash, it's intentional. 130 (SIGINT / a second-Ctrl+C
    # force-quit) and 143 (SIGTERM) are likewise deliberate stops, not crashes:
    # do NOT feed them into the LLM dependency-repair loop.
    if rc in (0, 130, 143):
        sys.exit(0)

    # Non-zero exit → crash.
    return False, stderr_text, rc
def _is_external_kill(rc: int) -> bool:
    """True when server.py died from SIGKILL (rc -9, or shell-style 137).

    SIGKILL is untrappable and leaves no traceback: feeding the empty stderr
    into the LLM dependency-repair loop would 'diagnose' nothing. The cause
    is almost always the container OOM killer (shared cgroup — see
    lib/cgroup_guard.py) or an external reaper. The right response is to
    record evidence and RESTART, not to repair dependencies.
    """
    return rc in (-9, 137)
def _log_external_kill(rc: int) -> None:
    """Durable evidence of a SIGKILL death — stderr + logs/watchdog.log."""
    import datetime
    line = ('%s [bootstrap] server.py SIGKILLed (exit %s) — almost always the '
            'container OOM killer (shared cgroup, zero swap). See '
            'logs/cgroup_pressure.log for the pressure curve; restarting.'
            % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), rc))
    print(line, file=sys.stderr)
    try:
        from lib.log_retention import (
            append_bytes_locked, ensure_private_log_directory,
        )
        log_dir = os.path.join(BASE_DIR, 'logs')
        ensure_private_log_directory(log_dir)
        append_bytes_locked(
            os.path.join(log_dir, 'watchdog.log'),
            (line + '\n').encode('utf-8'))
    except OSError as e:
        print(f'[bootstrap] could not write watchdog.log: {e}', file=sys.stderr)
def _restart_after_external_kill(first_rc: int, max_relaunches: int = 5) -> None:
    """Relaunch server.py after SIGKILL deaths, with linear backoff.

    A SIGKILLed server is healthy code killed by the environment — restarting
    is the whole fix (same contract as supervisord autorestart=true, for
    users who launch via bootstrap). Gives up after max_relaunches
    consecutive kills so a pathological kill-loop cannot spin forever.
    Never returns on success (server runs forever / clean exit sys.exit()s
    inside _try_start_server). When the relaunched server dies of something
    OTHER than SIGKILL, RETURNS that death's ``(stderr_text, rc)`` so the
    caller can enter the repair flow with the real error.
    """
    rc = first_rc
    for attempt in range(1, max_relaunches + 1):
        _log_external_kill(rc)
        backoff = min(5 * attempt, 30)
        print(f'[bootstrap] 🔄 relaunching after SIGKILL '
              f'(attempt {attempt}/{max_relaunches}, backoff {backoff}s)…',
              file=sys.stderr)
        time.sleep(backoff)
        _, stderr_text, rc = _try_start_server()
        # On success / clean exit _try_start_server never returns.
        if _is_external_kill(rc):
            continue
        print(f'[bootstrap] ⚠ relaunched server crashed differently '
              f'(exit {rc}) — entering repair mode.', file=sys.stderr)
        return stderr_text, rc
    print(f'[bootstrap] ❌ server.py SIGKILLed {max_relaunches}× in a row — '
          f'giving up. The container is under sustained memory pressure; '
          f'see logs/cgroup_pressure.log and lib/cgroup_guard.py.',
          file=sys.stderr)
    sys.exit(137)
def main():
    try:
        cfg = runtime._get_config()
    except ValueError as exc:
        print(f'[bootstrap] ❌ Invalid startup configuration: {exc}',
              file=sys.stderr)
        print('[bootstrap] Fix PORT in the project .env, then run '
              '`python serverctl.py doctor`.', file=sys.stderr)
        raise SystemExit(2) from exc
    host = cfg['host']
    port = cfg['port']
    has_llm = bool(cfg['api_keys'])

    # The configured endpoint is authority. Silently shifting to the next port
    # hides a foreign listener, creates manager/config drift, and can turn a
    # repeated launch into a second worker against the same data directory.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            _s.bind((host, port))
    except OSError as exc:
        doctor = shlex.join([
            sys.executable, os.path.join(BASE_DIR, 'serverctl.py'), 'doctor',
        ])
        print(
            f'[bootstrap] ❌ Cannot bind configured endpoint {host}:{port}: '
            f'{exc}',
            file=sys.stderr,
        )
        print(
            '[bootstrap] Refusing to switch ports implicitly. A running Tofu '
            'instance or another process may own this endpoint.',
            file=sys.stderr,
        )
        print(
            f'[bootstrap] Diagnose ownership with: {doctor}\n'
            '[bootstrap] To choose another endpoint, change PORT explicitly '
            'in the project .env.',
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    print(f'[bootstrap] 🚀 Starting Tofu (host={host}, port={port})…',
          file=sys.stderr)

    # ── First attempt (fast path — no status page) ──
    # A healthy server.py blocks forever (app.run).  If _try_start_server
    # returns at all, the process crashed.  On clean shutdown (rc=0, e.g.
    # Ctrl+C) it calls sys.exit(0) internally — so reaching here means crash.
    _, stderr_text, rc = _try_start_server(first_attempt=True)

    # ── SIGKILL (OOM killer / external reaper): record + auto-relaunch ──
    # The server ran fine and was killed by the environment — dependencies
    # are not the problem, so skip the LLM repair flow entirely. If the
    # relaunched server then crashes with a REAL error, the function hands
    # us its (stderr, rc) and we enter repair mode with the real data.
    if _is_external_kill(rc):
        stderr_text, rc = _restart_after_external_kill(rc)

    # ── Enter repair mode ──
    print(f'[bootstrap] ⚠ server.py crashed (exit code {rc}). '
          f'Entering dependency repair mode…', file=sys.stderr)

    status_server = _start_status_server(host, port)

    runtime._bus.emit('phase', json.dumps({
        'id': 'crash-0', 'label': '💥 Server crashed on startup',
        'status': 'error',
        'detail': f'Exit code {rc}',
    }))
    runtime._bus.emit('error_text', stderr_text[-3000:])

    # ── Fast path: mypyc broken extensions (no LLM needed) ──
    # Packages like charset-normalizer ship mypyc-compiled .so files that
    # are platform/Python-version specific.  When they don't match, every
    # import that touches requests/urllib3 fails.  Fix: force-reinstall.
    if _is_mypyc_error(stderr_text) and _try_fix_mypyc(stderr_text):
        runtime._bus.emit('phase', json.dumps({
            'id': 'mypyc-retry',
            'label': '🔄 Retrying server.py after mypyc fix…',
            'status': 'active',
        }))
        runtime._bus.emit('log', 'Restarting server.py…')
        runtime._bus.emit('phase', json.dumps({
            'id': 'handoff-mypyc',
            'label': '🔄 Handing off to server.py — this may take a moment…',
            'status': 'active',
            'detail': 'The server is starting up (database init, migrations, etc.).',
        }))
        time.sleep(0.5)

        _stop_status_server(status_server)
        status_server = None

        _, stderr_text, rc = _try_start_server()
        # If _try_start_server returns, the server crashed again.
        # Re-open status page and fall through to normal repair flow.
        runtime._bus = runtime.EventBus()
        status_server = _start_status_server(host, port)
        runtime._bus.emit('phase', json.dumps({
            'id': 'mypyc-retry',
            'label': '🔄 Still failing after mypyc fix',
            'status': 'error',
            'detail': f'Exit code {rc}',
        }))
        runtime._bus.emit('error_text', stderr_text[-3000:])
        # Fall through to requirements.txt / LLM repair below

    # ── Fast path: requirements.txt (no LLM needed) ──
    # For import / package errors, try installing from requirements.txt
    # first.  This is essential for freshly-exported projects where the
    # LLM API hasn't been configured yet.
    if _is_import_or_package_error(stderr_text) and _try_requirements_txt():
        runtime._bus.emit('phase', json.dumps({
            'id': 'reqtxt-retry',
            'label': '🔄 Retrying server.py after requirements.txt install…',
            'status': 'active',
        }))
        runtime._bus.emit('log', 'Restarting server.py…')

        # ── Notify browser, then free the port for server.py ──
        # Do NOT emit a 'done' event here — server.py hasn't started yet.
        # Instead, emit a 'handoff' phase so the user knows what's happening.
        # When the status server shuts down, the browser's SSE connection drops,
        # es.onerror fires (with _finished=false), and the reconnect polling
        # begins.  The poll will find server.py once it's ready to serve HTTP.
        runtime._bus.emit('phase', json.dumps({
            'id': 'handoff',
            'label': '🔄 Handing off to server.py — this may take a moment…',
            'status': 'active',
            'detail': 'The server is starting up (database init, migrations, etc.).',
        }))
        time.sleep(0.5)  # give browsers time to receive the phase event

        _stop_status_server(status_server)
        status_server = None

        _, stderr_text, rc = _try_start_server()
        # If _try_start_server returns, the server crashed again.

        if _is_import_or_package_error(stderr_text):
            # Still import errors after requirements.txt — fall through to LLM
            print('[bootstrap] ⚠ Still failing after requirements.txt install.',
                  file=sys.stderr)
            # Reset event bus so new browsers get a clean history
            runtime._bus = runtime.EventBus()
            status_server = _start_status_server(host, port)
            runtime._bus.emit('phase', json.dumps({
                'id': 'reqtxt-retry',
                'label': '🔄 Still failing after requirements.txt — trying LLM diagnosis…',
                'status': 'error',
            }))
            runtime._bus.emit('error_text', stderr_text[-3000:])
        else:
            # Non-import error or still crashing for a different reason
            # Reset event bus so new browsers get a clean history
            runtime._bus = runtime.EventBus()
            status_server = _start_status_server(host, port)
            runtime._bus.emit('phase', json.dumps({
                'id': 'reqtxt-retry',
                'label': '🔄 Still failing (non-dependency error)',
                'status': 'error',
                'detail': f'Exit code {rc}',
            }))
            runtime._bus.emit('error_text', stderr_text[-3000:])

            if not has_llm:
                runtime._bus.emit('done', json.dumps({
                    'success': False,
                    'reason': 'Server still crashing after installing requirements.txt. '
                              'The error does not look like a missing-package issue.',
                    'hint': 'Check the error log above. You may also need to configure '
                            'LLM API credentials in .env (LLM_API_KEY, LLM_BASE_URL) '
                            'for smarter auto-diagnosis.',
                }))
                print('[bootstrap] ❌ Non-dependency error and no LLM API configured.',
                      file=sys.stderr)
                _keep_alive_until_interrupt(status_server)
                return

    # ── Check if LLM is available for diagnosis ──
    if not has_llm:
        hint_lines = [
            'No LLM API key configured — cannot auto-diagnose.',
            '',
            'To fix manually:',
            '  1. pip install -r requirements.txt',
            '  2. Configure LLM credentials in .env:',
            '     LLM_API_KEY=sk-your-key-here',
            '     LLM_BASE_URL=https://api.openai.com/v1',
            '  3. Re-run: python server.py',
        ]
        hint = '\n'.join(hint_lines)
        runtime._bus.emit('log', hint)
        runtime._bus.emit('done', json.dumps({
            'success': False,
            'reason': 'No LLM API key configured. Cannot auto-diagnose the error.',
            'hint': 'Set LLM_API_KEY and LLM_BASE_URL in .env, then run '
                    '"pip install -r requirements.txt" and "python server.py".',
        }))
        print('[bootstrap] ❌ No LLM API key configured. '
              'Set LLM_API_KEY in .env and retry.', file=sys.stderr)
        _keep_alive_until_interrupt(status_server)
        return

    # ── LLM-guided repair loop ──
    runtime._bus.emit('round', json.dumps({'current': 1, 'max': MAX_REPAIR_ROUNDS}))

    installed_so_far: list[str] = []
    prev_error = ''

    for round_num in range(1, MAX_REPAIR_ROUNDS + 1):
        runtime._bus.emit('round', json.dumps({'current': round_num, 'max': MAX_REPAIR_ROUNDS}))

        # ── Phase 1: Analyse with LLM ──
        runtime._bus.emit('phase', json.dumps({
            'id': f'llm-{round_num}', 'label': f'🤖 Round {round_num}: Asking LLM to diagnose…',
            'status': 'active',
        }))
        runtime._bus.emit('log', f'── Round {round_num}/{MAX_REPAIR_ROUNDS} ──')

        # Add context about previous installs so LLM doesn't suggest the same thing
        context = stderr_text
        if installed_so_far:
            context += f'\n\n[CONTEXT] Already installed in previous rounds: {", ".join(installed_so_far)}'

        result = runtime._call_llm(context, cfg)
        diagnosis = result.get('diagnosis', 'No diagnosis available.')
        packages = result.get('packages', [])
        unresolvable = result.get('unresolvable', False)

        runtime._bus.emit('diagnosis', json.dumps({
            'diagnosis': diagnosis,
            'packages': packages,
            'unresolvable': unresolvable,
        }))
        runtime._bus.emit('phase', json.dumps({
            'id': f'llm-{round_num}', 'label': f'🤖 Round {round_num}: Diagnosis complete',
            'status': 'done',
            'detail': diagnosis[:200],
        }))

        if unresolvable:
            runtime._bus.emit('log', f'LLM says this error is not fixable via pip: {diagnosis}')
            runtime._bus.emit('done', json.dumps({
                'success': False,
                'reason': diagnosis,
            }))
            print(f'[bootstrap] ❌ Unresolvable error: {diagnosis}', file=sys.stderr)
            # Keep status server alive so user can read the page
            _keep_alive_until_interrupt(status_server)
            return

        if not packages:
            runtime._bus.emit('log', 'LLM did not suggest any packages. Retrying with raw error…')
            # One more attempt: maybe the LLM response was malformed
            if round_num >= 3:
                runtime._bus.emit('done', json.dumps({
                    'success': False,
                    'reason': 'LLM could not determine which packages to install.',
                }))
                _keep_alive_until_interrupt(status_server)
                return
            continue

        # ── Phase 2: Install packages ──
        new_pkgs = [p for p in packages if p not in installed_so_far]
        if not new_pkgs:
            runtime._bus.emit('log', f'All suggested packages already installed: {packages}')
            # Same packages suggested again → likely not a pip issue
            runtime._bus.emit('done', json.dumps({
                'success': False,
                'reason': f'Already installed {packages} but error persists. Manual intervention needed.',
            }))
            _keep_alive_until_interrupt(status_server)
            return

        runtime._bus.emit('phase', json.dumps({
            'id': f'pip-{round_num}',
            'label': f'📦 Round {round_num}: Installing {", ".join(new_pkgs)}',
            'status': 'active',
        }))

        pip_ok, pip_output = _pip_install(new_pkgs)

        if pip_ok:
            installed_so_far.extend(new_pkgs)
            runtime._bus.emit('phase', json.dumps({
                'id': f'pip-{round_num}',
                'label': f'📦 Round {round_num}: Installed {", ".join(new_pkgs)}',
                'status': 'done',
            }))
        else:
            runtime._bus.emit('phase', json.dumps({
                'id': f'pip-{round_num}',
                'label': f'📦 Round {round_num}: pip install failed',
                'status': 'error',
                'detail': 'See log output for details.',
            }))
            # pip failure is potentially unresolvable
            runtime._bus.emit('done', json.dumps({
                'success': False,
                'reason': f'pip install failed for: {", ".join(new_pkgs)}',
            }))
            _keep_alive_until_interrupt(status_server)
            return

        # ── Phase 3: Retry server.py ──
        runtime._bus.emit('phase', json.dumps({
            'id': f'retry-{round_num}',
            'label': f'🔄 Round {round_num}: Retrying server.py…',
            'status': 'active',
        }))
        runtime._bus.emit('log', 'Restarting server.py…')

        # Notify browsers: "we're about to restart — reconnect shortly"
        # Do NOT emit a 'done' event here — server.py hasn't started yet.
        # Let the SSE drop naturally so the browser's reconnect polling kicks in.
        runtime._bus.emit('phase', json.dumps({
            'id': f'handoff-{round_num}',
            'label': f'🔄 Round {round_num}: Handing off to server.py — this may take a moment…',
            'status': 'active',
            'detail': 'The server is starting up (database init, migrations, etc.).',
        }))
        time.sleep(0.5)  # give browsers time to receive the phase event

        # Stop status server to free the port before retrying
        _stop_status_server(status_server)
        status_server = None

        _, stderr_text, rc = _try_start_server()
        # If _try_start_server returns, the server crashed again.
        # (On success it blocks forever; on clean exit it calls sys.exit.)

        # Still crashing — re-start status page for next round
        # Reset event bus so new browsers get a clean history
        runtime._bus = runtime.EventBus()
        runtime._bus.emit('phase', json.dumps({
            'id': f'retry-{round_num}',
            'label': f'🔄 Round {round_num}: Still failing (exit code {rc})',
            'status': 'error',
        }))
        runtime._bus.emit('error_text', stderr_text[-3000:])

        # Check if this is the same error repeating
        if stderr_text.strip() == prev_error.strip() and prev_error:
            runtime._bus.emit('log', '⚠ Same error as last round — the installed packages did not help.')
        prev_error = stderr_text

        # Re-bind status server for next round
        status_server = _start_status_server(host, port)
        if status_server is None:
            # Port stuck — wait a moment and retry
            time.sleep(2)
            status_server = _start_status_server(host, port)

    # Exhausted all rounds
    runtime._bus.emit('done', json.dumps({
        'success': False,
        'reason': f'Exhausted {MAX_REPAIR_ROUNDS} repair rounds. Manual intervention needed.',
    }))
    print(f'[bootstrap] ❌ Gave up after {MAX_REPAIR_ROUNDS} rounds.', file=sys.stderr)
    _keep_alive_until_interrupt(status_server)
def _keep_alive_until_interrupt(server: http.server.HTTPServer | None):
    """Block until Ctrl+C or restart request from the API config form."""
    if server is None:
        return
    print('[bootstrap] Status page still running. Press Ctrl+C to exit.', file=sys.stderr)
    try:
        while True:
            if runtime.consume_restart_request():
                print('[bootstrap] \U0001f504 Restart requested via API config form.',
                      file=sys.stderr)
                _stop_status_server(server)
                # Re-load .env so _get_config() picks up the new keys
                runtime._load_dotenv()
                # Reset the event bus so the next status page is clean
                runtime._bus = runtime.EventBus()
                # Re-enter the main bootstrap flow
                main()
                return
            time.sleep(1)
    except KeyboardInterrupt:
        _stop_status_server(server)

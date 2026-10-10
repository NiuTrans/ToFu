"""Discover the installed Codex login through its native app-server protocol.

Entries: command_context, renew_command_login, clear_command_context. A fixed
command uses the OS user's shell setup; initialize returns the authoritative
codexHome. Native Codex alone handles refresh. No login/logout, model turn,
MCP tool or refresh-token export is created. One bounded metadata child is owned.
Dependencies: stdlib only; the same adapter ships in the paired desktop agent.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
import time

_DEADLINE_SECONDS = 4.0
_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_LINE_BYTES = 65536
_CONTEXT_TTL_SECONDS = 30.0
# Absolute age from a successful native policy read; failures never renew it.
_CONTEXT_MAX_AGE_SECONDS = 120.0
_TRANSIENT_ISSUES = frozenset({
    "codex_command_timeout", "codex_command_unavailable",
})
_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_cached: tuple[tuple, float, dict] | None = None
_verified: tuple[tuple, float, dict, tuple] | None = None
_metadata_probe: threading.Thread | None = None

# One OS-credential metadata transport, serialized by _lock. Renewable account
# operations deliberately use a separate one-shot transport.
_SESSION_IDLE_SECONDS = 60.0
_SESSION_ROTATE_SECONDS = 90.0
_session = None
_session_reaper: threading.Thread | None = None
_session_wake = threading.Event()


def _child_environment(environment: dict, home: Path) -> dict:
    child = {key: value for key, value in environment.items()
             if key not in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN')
             and not key.startswith('OPENAI_IDENTITY_')}
    if environment.get('CODEX_HOME'):
        child['TOFU_CODEX_NATIVE_HOME'] = environment['CODEX_HOME']
    child['HOME'] = str(home)
    child['PWD'] = str(home)
    return child


def _environment_revision(environment: dict, home: Path) -> str:
    """Compare every native launcher input without retaining environment values."""
    payload = json.dumps(_child_environment(environment, home), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


class _CommandFailure(Exception):
    """Non-secret protocol failure; subprocess/parser text never escapes."""


def _launch(environment: dict, home: Path) -> list[str] | None:
    # POSIX shells provide automatic interactive configuration discovery.
    # Windows retains the existing file adapter until a native launcher is
    # integrated with its process-tree supervisor.
    if os.name == 'nt':
        return None
    if os.name != 'nt':
        shell = environment.get('SHELL', '')
        if not shell:
            import pwd
            shell = pwd.getpwuid(os.getuid()).pw_shell
        # User shell startup is trusted local configuration, never bridge input.
        if Path(shell).name in ('bash', 'zsh', 'fish', 'sh', 'dash') and Path(shell).is_file():
            fixed = 'exec codex app-server --listen stdio://'
            if environment.get('CODEX_HOME'):
                # An explicit operator/test override remains authoritative;
                # shell startup still discovers the installed command's PATH.
                fixed = ('exec env CODEX_HOME="$TOFU_CODEX_NATIVE_HOME" '
                         'codex app-server --listen stdio://')
            return [shell, '-ic', fixed]
    executable = shutil.which('codex', path=environment.get('PATH', os.defpath))
    return [executable, 'app-server', '--listen', 'stdio://'] if executable else None


def _stop(process: subprocess.Popen, reader: threading.Thread | None) -> None:
    def terminate_group(sig):
        try:
            if os.name != 'nt':
                os.killpg(process.pid, sig)
            elif process.poll() is None:
                process.kill() if sig == signal.SIGKILL else process.terminate()
        except ProcessLookupError:
            pass  # The owned process group has already exited.
    terminate_group(signal.SIGTERM)
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        terminate_group(signal.SIGKILL)
        process.wait()  # Reap the killed child; never abandon an owned process.
    if reader is not None:
        reader.join(timeout=0.2)
    if reader is not None and reader.is_alive():
        # An exited launcher can still have children holding stdout open.
        terminate_group(signal.SIGKILL)
        reader.join(timeout=1)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()
    if reader is not None and reader.is_alive():
        raise _CommandFailure('codex_command_cleanup_failed')


def _protocol(environment: dict, home: Path, *, renew_root: Path | None = None,
              timeout_seconds: float | None = None):
    """Initialize once; yield metadata; send the next bounded exchange budget.

    Closing this generator reaps the child and joins its bounded reader.
    Only the serialized owner may resume/close it.
    """
    started = time.monotonic()
    deadline = time.monotonic() + (_DEADLINE_SECONDS if timeout_seconds is None else timeout_seconds)
    command = _launch(environment, home)
    if command is None:
        raise _CommandFailure('codex_command_missing')
    inbox: queue.Queue = queue.Queue(maxsize=16)
    failed = threading.Event()
    # Do not select an unrelated API-key/access-token/WIF identity from Tofu's
    # environment. Codex resolves its managed login and policy itself.
    child_env = _child_environment(environment, home)
    _logger.debug('Codex native metadata phase=spawn')
    process = subprocess.Popen(command, cwd=home, env=child_env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt')

    def receive():
        total = 0
        try:
            while True:
                line = process.stdout.readline(_MAX_LINE_BYTES + 1)
                if not line:
                    return
                total += len(line)
                if len(line) > _MAX_LINE_BYTES or total > _MAX_OUTPUT_BYTES:
                    failed.set()
                    return
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue  # Shell startup may print bounded non-protocol text.
                if isinstance(message, dict):
                    try:
                        inbox.put_nowait(message)
                    except queue.Full:
                        failed.set()
                        return
        except (OSError, ValueError):
            failed.set()  # Owned pipe closed during teardown.

    try:
        reader = threading.Thread(target=receive, name='codex-login-protocol', daemon=True)
        reader.start()
    except BaseException:
        # Thread admission can fail after the process already exists. Roll back
        # the owned group even for cancellation before the protocol loop starts.
        _stop(process, None)
        raise
    wire_sequence = 0
    wire_ids = {}

    def send(message):
        nonlocal wire_sequence
        if 'id' in message:
            wire_sequence += 1
            logical_id = message['id']
            wire_ids[logical_id] = wire_sequence
            message = {**message, 'id': wire_sequence}
        process.stdin.write(json.dumps(message).encode() + b'\n')
        process.stdin.flush()

    def result(request_id):
        while time.monotonic() < deadline:
            if failed.is_set():
                raise _CommandFailure('codex_command_invalid_response')
            try:
                message = inbox.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    raise _CommandFailure('codex_command_missing' if process.returncode == 127
                                          else 'codex_command_unavailable')
                continue
            if message.get('id') != wire_ids[request_id]:
                continue
            if 'error' in message or not isinstance(message.get('result'), dict):
                raise _CommandFailure('codex_command_unavailable')
            return message['result']
        _logger.warning('Codex native credential probe failed phase=%s issue=codex_command_timeout elapsed_ms=%d',
                        {1: 'initialize', 2: 'config', 3: 'requirements', 4: 'renewal'}[request_id],
                        round((time.monotonic() - started) * 1000))
        raise _CommandFailure('codex_command_timeout')

    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'tofu_codex_login', 'version': '1.0.0'}}})
        _logger.debug('Codex native metadata phase=initialize_sent')
        hello = result(1)
        _logger.debug('Codex native metadata phase=initialize_received elapsed_ms=%d',
                      round((time.monotonic() - started) * 1000))
        root = hello.get('codexHome')
        if not isinstance(root, str) or len(root) > 4096 or not Path(root).is_absolute():
            raise _CommandFailure('codex_command_unsupported')
        send({'method': 'initialized'})
        while True:
            policy_revision = _source_revision(environment, home, {'codex_home': root})
            send({'id': 2, 'method': 'config/read', 'params': {'includeLayers': False}})
            config = result(2).get('config')
            if not isinstance(config, dict):
                raise _CommandFailure('codex_command_invalid_response')
            send({'id': 3, 'method': 'configRequirements/read', 'params': {}})
            requirements = result(3).get('requirements') or {}
            if not isinstance(requirements, dict):
                raise _CommandFailure('codex_command_invalid_response')
            settings = {key: config[key] for key in ('forced_login_method', 'forced_chatgpt_workspace_id')
                        if key in config}
            store = requirements.get('cliAuthCredentialsStore')
            if store is not None:
                settings['cli_auth_credentials_store'] = store
            methods = requirements.get('allowedLoginMethods')
            if methods is not None:
                if not isinstance(methods, list) or len(methods) > 16 or not all(isinstance(v, str) for v in methods):
                    raise _CommandFailure('codex_command_invalid_response')
                if 'chatgpt' not in methods:
                    raise _CommandFailure('login_method_restricted')
            if requirements.get('chatgptBaseUrl') not in (None, 'https://chatgpt.com'):
                raise _CommandFailure('login_method_restricted')
            if _source_revision(environment, home, {'codex_home': root}) != policy_revision:
                raise _CommandFailure('codex_source_changed')
            if renew_root is not None:
                if Path(root) != renew_root:
                    raise _CommandFailure('codex_source_changed')
                send({'id': 4, 'method': 'account/read', 'params': {'refreshToken': True}})
                account = result(4).get('account')
                if not isinstance(account, dict) or account.get('type') != 'chatgpt':
                    raise _CommandFailure('not_chatgpt_login')
            budget = yield {'codex_home': root, 'settings': settings, 'shared_login_issue': ''}
            started = time.monotonic()
            deadline = started + budget
    finally:
        _stop(process, reader)


def _close_session() -> None:
    """Caller holds _lock; detach before teardown even if cleanup fails."""
    global _session
    owned, _session = _session, None
    _session_wake.set()
    if owned is not None:
        owned['protocol'].close()


def _reap_idle_session(owner: threading.Thread, wake: threading.Event) -> None:
    global _session_reaper
    try:
        while True:
            with _lock:
                if _session_reaper is not owner:
                    return  # Clear detached this reaper; do not touch a new session.
                if _session is None:
                    _session_reaper = None
                    return
                remaining = min(_session['used'] + _SESSION_IDLE_SECONDS,
                                _session['born'] + _CONTEXT_MAX_AGE_SECONDS) - time.monotonic()
                if remaining <= 0:
                    _close_session()
                    _session_reaper = None
                    return
                wake.clear()
            wake.wait(remaining)
    except BaseException:
        # Exceptions cannot leave an unowned native transport behind.
        with _lock:
            if _session_reaper is owner:
                _close_session()
                _session_reaper = None
        raise


def _exchange(environment: dict, home: Path, *, renew_root: Path | None = None,
              timeout_seconds: float | None = None) -> dict:
    """One lifecycle-owned metadata session; renewal remains one-shot.

    Caller holds _lock. Full sanitized child-environment provenance is compared
    in memory (never logged), not just a PATH/home subset. Unknown shell/remote
    policy dependencies require cold rotation before the original 120s bound.
    """
    global _session, _session_reaper
    budget = _DEADLINE_SECONDS if timeout_seconds is None else max(0, timeout_seconds)
    deadline = time.monotonic() + budget
    if renew_root is not None:
        protocol = _protocol(environment, home, renew_root=renew_root, timeout_seconds=budget)
        try:
            return next(protocol)
        finally:
            protocol.close()
    provenance = _environment_revision(environment, home)
    owned = _session
    try:
        if owned is not None and (owned['environment'] != provenance
                or time.monotonic() >= owned['born'] + _SESSION_ROTATE_SECONDS
                or _source_revision(environment, home, owned['context']) != owned['revision']):
            _close_session()
            owned = None
        if owned is not None:
            _logger.debug('Codex native metadata phase=session_reuse')
            context = owned['protocol'].send(max(0, deadline - time.monotonic()))
        else:
            born = time.monotonic()
            protocol = _protocol(environment, home, timeout_seconds=max(0, deadline - born))
            try:
                context = next(protocol)
                revision = _source_revision(environment, home, context)
            except BaseException:
                protocol.close()
                raise
            owned = {'protocol': protocol, 'environment': provenance, 'born': born,
                     'context': dict(context), 'revision': revision, 'used': time.monotonic()}
            _session = owned
        owned['used'] = time.monotonic()
        _session_wake.set()
        # A keyring/API-only policy is unusable for access-file reuse; retain no
        # optional resident process for a login this adapter cannot borrow.
        if context['settings'].get('cli_auth_credentials_store') not in (None, 'file'):
            _close_session()
        elif _session_reaper is None:
            wake = _session_wake
            reaper = threading.Thread(target=lambda: _reap_idle_session(reaper, wake),
                                      name='codex-metadata-reaper', daemon=True)
            _session_reaper = reaper
            try:
                reaper.start()
            except BaseException:
                _session_reaper = None
                raise
        return context
    except BaseException:
        _close_session()
        raise


def _source_revision(environment: dict, home: Path, context: dict) -> tuple:
    """Bounded non-secret stamps; token files deliberately are not metadata.

    Shell hooks can source arbitrary files and managed policies can be remote.
    Stamps invalidate known local changes immediately; the absolute native
    verification age bounds everything that cannot be observed locally.
    """
    codex_root = Path(context['codex_home'])
    shell_root = Path(environment.get('ZDOTDIR') or home)
    xdg_root = Path(environment.get('XDG_CONFIG_HOME') or home / '.config')
    paths = [home / name for name in ('.profile', '.bashrc', '.bash_profile', '.bash_login')]
    paths += [shell_root / name for name in ('.zshenv', '.zprofile', '.zshrc', '.zlogin')]
    paths += [xdg_root / 'fish' / 'config.fish', codex_root / 'config.toml',
              codex_root / 'requirements.toml', Path('/etc/codex/requirements.toml'),
              Path('/etc/codex/managed_config.toml'), Path('/etc/codex/config.toml'),
              Path('/etc/profile'),
              Path('/etc/bash.bashrc'),
              Path('/Library/Managed Preferences/com.openai.codex.plist'),
              home / 'Library/Preferences/com.openai.codex.plist']
    executable = shutil.which('codex', path=environment.get('PATH', os.defpath))
    if executable:
        paths.append(Path(executable))
    stamps = []
    for path in paths:
        try:
            link = path.lstat()
            target = path.stat()
        except FileNotFoundError:
            stamps.append((str(path), None))
            continue
        # Other stat failures reject reuse, including unreadable symlink targets.
        stamps.append((str(path), *((v.st_dev, v.st_ino, v.st_mtime_ns,
                                    v.st_ctime_ns, v.st_size, v.st_mode, v.st_uid)
                                   for v in (link, target))))
    return tuple(stamps)


def command_context(*, environment=None, home: Path | None = None,
                    retry_failed: bool = False) -> dict:
    """One bounded metadata cache and one flight; never cache borrowed tokens."""
    global _cached, _verified, _metadata_probe
    deadline = time.monotonic() + _DEADLINE_SECONDS
    environment = dict(os.environ if environment is None else environment)
    home = home or Path(environment.get('HOME') or Path.home())
    signature = (_environment_revision(environment, home),)
    # A valid immutable metadata snapshot needs no native-auth lock. Renewal
    # can hold that lock for network I/O; healthy file reads must not queue
    # behind it. Account, expiry and file permissions are still checked below
    # by codex_local_login on every read, never served from this cache.
    observed = _cached

    def verified_context():
        verified = _verified
        if not verified or verified[0] != signature or time.monotonic() >= verified[1]:
            return None
        try:
            if _source_revision(environment, home, verified[2]) != verified[3]:
                return None
        except (OSError, ValueError):
            return None  # Local source cannot be verified; fail closed.
        return dict(verified[2])

    def select(context):
        if context.get('shared_login_issue') in _TRANSIENT_ISSUES:
            return verified_context() or dict(context)
        return dict(context)

    def usable(cached, *, bypass_failure):
        if not cached or cached[0] != signature or time.monotonic() >= cached[1]:
            return False
        if not cached[2].get('shared_login_issue') and verified_context() is None:
            return False
        return not (bypass_failure and cached[2].get('shared_login_issue')
                    in {'codex_command_timeout', 'codex_command_unavailable'})

    def run_probe(probe_deadline):
        global _cached, _verified
        now = time.monotonic()
        try:
            context = _exchange(environment, home, timeout_seconds=max(0, probe_deadline - now))
        except _CommandFailure as error:
            context = {'shared_login_issue': str(error)}
        except (OSError, ValueError, TypeError, RuntimeError):
            context = {'shared_login_issue': 'codex_command_unavailable'}
        if (not context.get('shared_login_issue') and _verified
                and _verified[0] == signature
                and context['codex_home'] != _verified[2]['codex_home']):
            context = {'shared_login_issue': 'codex_source_changed'}
        issue = context.get('shared_login_issue')
        if not issue:
            try:
                revision = _source_revision(environment, home, context)
            except (OSError, ValueError):
                _verified = None
                context = {'shared_login_issue': 'codex_command_unavailable'}
            else:
                # Warm reads cannot extend policy loaded at native startup.
                limit = (_session['born'] if _session is not None else time.monotonic()) + _CONTEXT_MAX_AGE_SECONDS
                _verified = (signature, limit, dict(context), revision)
        elif issue not in _TRANSIENT_ISSUES:
            _verified = None  # Native policy/source errors revoke prior metadata.
            _close_session()
        lifetime = 5.0 if context.get('shared_login_issue') else _CONTEXT_TTL_SECONDS
        _cached = (signature, time.monotonic() + lifetime, context)
        selected = select(context)
        if context.get('shared_login_issue') and not selected.get('shared_login_issue'):
            _logger.warning('Codex native credential probe using bounded verified metadata issue=%s',
                            context['shared_login_issue'])
        return selected

    if usable(observed, bypass_failure=retry_failed):
        return select(observed[2])

    # Demand-driven revalidation keeps native startup off the healthy request
    # path. One owned, finite revalidation worker; no periodic probing/token cache.
    verified = verified_context()
    if verified is not None and not retry_failed:
        if _lock.acquire(blocking=False):
            cached = _cached
            if usable(cached, bypass_failure=False):
                _lock.release()
                return select(cached[2])
            def revalidate():
                try:
                    run_probe(time.monotonic() + _DEADLINE_SECONDS)
                finally:
                    _lock.release()
            try:
                worker = threading.Thread(target=revalidate, name='codex-metadata-revalidation', daemon=True)
                _metadata_probe = worker
                worker.start()
            except (OSError, RuntimeError):
                _metadata_probe = None
                _cached = (signature, time.monotonic() + 5.0,
                           {'shared_login_issue': 'codex_command_unavailable'})
                _lock.release()  # Optional worker admission must not kill a healthy read.
            except BaseException:
                _metadata_probe = None
                _lock.release()  # Roll back admission, including cancellation.
                raise
        return verified

    if not _lock.acquire(timeout=max(0, deadline - time.monotonic())):
        _logger.warning('Codex native credential probe failed phase=lock issue=codex_command_timeout')
        return select({'shared_login_issue': 'codex_command_timeout'})
    try:
        # Concurrent explicit recovery joins the result published while waiting.
        cached = _cached
        if usable(cached, bypass_failure=retry_failed and cached is observed):
            return select(cached[2])
        return run_probe(deadline)
    finally:
        _lock.release()


def renew_command_login(root: Path, *, expected_account_id: str, settings: dict,
                        environment=None, home: Path | None = None,
                        timeout_seconds: float | None = None) -> dict:
    """Renew only a previously checked account; no Tofu OAuth endpoint call."""
    budget = _DEADLINE_SECONDS if timeout_seconds is None else max(0, timeout_seconds)
    deadline = time.monotonic() + budget
    environment = dict(os.environ if environment is None else environment)
    home = home or Path(environment.get('HOME') or Path.home())
    if not _lock.acquire(timeout=budget):
        return {'shared_login_issue': 'codex_command_timeout'}
    try:
        # Another reader may have renewed while this caller waited. Re-read
        # under the same native-auth flight before requesting another rotation.
        from lib.oauth.codex_local_login import _read_file_login
        current = _read_file_login(expected_account_id=expected_account_id,
            environment={'CODEX_HOME': str(root)}, command_settings=settings,
            command_verified=True)
        if current.get('access_token'):
            return {'codex_home': str(root), 'settings': settings, 'shared_login_issue': ''}
        if current.get('shared_login_issue') != 'codex_access_expired':
            return {'shared_login_issue': current.get('shared_login_issue', 'codex_credentials_unreadable')}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {'shared_login_issue': 'codex_command_timeout'}
        try:
            return _exchange(environment, home, renew_root=root, timeout_seconds=remaining)
        except _CommandFailure as error:
            return {'shared_login_issue': str(error)}
        except (OSError, ValueError, TypeError, RuntimeError):
            return {'shared_login_issue': 'codex_command_unavailable'}
    finally:
        _lock.release()


def clear_command_context() -> None:
    """Drain demand-driven work before forgetting native source metadata."""
    global _cached, _verified, _metadata_probe, _session_reaper, _session_wake
    with _lock:
        _close_session()
        reaper = _session_reaper
        _session_reaper = None
        _session_wake = threading.Event()
        _cached = None
        _verified = None
        worker = _metadata_probe
        _metadata_probe = None
    if worker is not None:
        worker.join()  # Native exchange/teardown are bounded; never abandon it.

    if reaper is not None:
        reaper.join()


atexit.register(clear_command_context)

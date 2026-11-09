"""Discover the installed Codex login through its native app-server protocol.

Entries: command_context, renew_command_login, clear_command_context. A fixed
command uses the OS user's shell setup; initialize returns the authoritative
codexHome. Native Codex alone handles refresh. No login/logout, model turn,
MCP tool or refresh-token export is created. One bounded metadata child is owned.
Warm exchanges keep a fixed budget; a new app-server's spawn+initialize uses a
budget learned per launcher provenance, clamped to a validated ceiling. A
start that times out at the ceiling refuses that launcher's cold starts for a
bounded window; clear/exit cancel in-flight native waits.
Dependencies: stdlib only; the same adapter ships in the paired desktop agent.
"""
from __future__ import annotations

import atexit
from collections import OrderedDict
import errno
import hashlib
import json
from lib.log import get_logger
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import stat
import subprocess
import threading
import time

# Budget for one exchange on an initialized app-server. It is also the cold
# startup budget of a launcher environment without learned timing.
_EXCHANGE_SECONDS = 4.0
# Spawn+initialize of a new app-server (new session, rotation, renewal) can be
# legitimately slow, e.g. a Codex home on network storage. Its budget is
# learned per launcher provenance: doubled after an initialize timeout, twice
# the observed startup after success but decayed at most by a quarter per
# faster start, clamped to [exchange budget, ceiling].
_STARTUP_CEILING_SECONDS = 30.0
_STARTUP_CEILING_LIMIT_SECONDS = 300.0  # Largest accepted operator override.
_STARTUP_BUDGET_SIGNATURES = 16
# A faster start lowers a learned budget by at most a quarter, so one
# warm-cache start cannot erase a slow-storage observation: 2t decays to
# 1.5t, which still fits t. Sustained fast starts return to the default.
_STARTUP_BUDGET_DECAY = 0.75
# A cold start that used its whole budget at the ceiling and still timed out
# cannot converge by retrying: further cold starts of that launcher fail fast
# for this long, and discovery caches that failure as long. Clear or a
# changed ceiling ends the window early.
_STARTUP_EXHAUSTED_SECONDS = 60.0
_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_LINE_BYTES = 65536
_CONTEXT_TTL_SECONDS = 30.0
# Absolute age from a successful native policy read; failures never renew it.
_CONTEXT_MAX_AGE_SECONDS = 120.0
# A full Codex home is transient (freeing space recovers) but must never be
# reported as a generic command failure: re-login cannot repair a full disk.
_HOME_STORAGE_FULL_ISSUE = 'codex_home_storage_full'
# A migrated explicit home must be restored, never initialized or substituted.
_HOME_UNAVAILABLE_ISSUE = 'codex_home_unavailable'
_TRANSIENT_ISSUES = frozenset({
    "codex_command_timeout", "codex_command_unavailable",
    _HOME_STORAGE_FULL_ISSUE, _HOME_UNAVAILABLE_ISSUE,
})
# Admission floor for the Codex home filesystem. The native probe spawns one
# child and Codex itself maintains sqlite/thread state under the same home;
# refusing distinctly just before the last inodes run out keeps the failure
# actionable instead of a mysterious mid-protocol child exit.
_MIN_HOME_FREE_INODES = 1024
_MIN_HOME_FREE_BYTES = 64 * 1024 * 1024
_logger = get_logger(__name__)
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

# Learned cold startup seconds by launcher provenance hash; absence means the
# default. Timing hints only, never source authority, so clear keeps them.
_startup_budgets: OrderedDict[str, float] = OrderedDict()
_startup_budget_lock = threading.Lock()
_startup_ceiling_rejected = ''
# Refused cold starts by launcher provenance hash: (monotonic time they
# resume, ceiling that was exhausted). A failure window, so clear resets it.
_startup_exhausted: OrderedDict[str, tuple[float, float]] = OrderedDict()

# Clear/exit cancel in-flight native waits so the flight lock frees within
# child teardown instead of a cold startup budget. A count, not an Event: a
# finishing clear must not re-arm flights while another clear still waits.
_cancel_lock = threading.Lock()
_cancel_requests = 0


def _native_shell(environment: dict) -> str | None:
    """Resolve the account's shell, not a cron/service launcher's SHELL."""
    if os.name == 'nt':
        return None
    import pwd
    try:
        account_shell = pwd.getpwuid(os.getuid()).pw_shell
    except (KeyError, OSError):
        account_shell = ''
    for shell in (account_shell, environment.get('SHELL', '')):
        if (shell and Path(shell).name in ('bash', 'zsh', 'fish', 'sh', 'dash')
                and Path(shell).is_file()):
            return shell
    return None


def _child_environment(environment: dict, home: Path) -> dict:
    child = {key: value for key, value in environment.items()
             if key not in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN')
             and not key.startswith('OPENAI_IDENTITY_')}
    if environment.get('CODEX_HOME'):
        child['TOFU_CODEX_NATIVE_HOME'] = environment['CODEX_HOME']
    child['HOME'] = str(home)
    child['PWD'] = str(home)
    shell = _native_shell(environment)
    if shell:
        # Also includes account-shell changes in the hashed launcher revision.
        child['SHELL'] = shell
    return child


def _environment_revision(environment: dict, home: Path) -> str:
    """Compare every native launcher input without retaining environment values."""
    payload = json.dumps(_child_environment(environment, home), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _startup_ceiling() -> float:
    """Hard bound for one cold spawn+initialize; bounds a hung native command.

    An override must be finite, at least the exchange budget and at most
    _STARTUP_CEILING_LIMIT_SECONDS; anything else warns once and is ignored.
    """
    global _startup_ceiling_rejected
    default = max(_EXCHANGE_SECONDS, _STARTUP_CEILING_SECONDS)
    raw = os.environ.get('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS', '').strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if math.isfinite(value) and _EXCHANGE_SECONDS <= value <= _STARTUP_CEILING_LIMIT_SECONDS:
        return value
    if raw != _startup_ceiling_rejected:
        _startup_ceiling_rejected = raw
        _logger.warning('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS ignored: expected seconds '
                        'from %g to %g; using %g', _EXCHANGE_SECONDS,
                        _STARTUP_CEILING_LIMIT_SECONDS, default)
    return default


def _startup_seconds(provenance: str) -> float:
    """Cold spawn+initialize budget for one launcher environment."""
    ceiling = _startup_ceiling()
    with _startup_budget_lock:
        learned = _startup_budgets.get(provenance, _EXCHANGE_SECONDS)
    return min(ceiling, max(_EXCHANGE_SECONDS, learned))


def _native_flight_seconds(provenance: str) -> float:
    """One native flight that may start an app-server: startup plus one exchange.

    Also bounds lock waits, so a caller outlasts a same-provenance flight that
    is legitimately starting native Codex instead of reporting a timeout.
    """
    return _startup_seconds(provenance) + _EXCHANGE_SECONDS


def _learn_startup_seconds(provenance: str, observed: float, *, reason: str) -> None:
    """Next cold budget is twice a completed startup or an exhausted budget.

    A faster start decays the previous budget instead of replacing it.
    Bounded LRU; a budget at the default is stored as absence.
    """
    ceiling = _startup_ceiling()
    with _startup_budget_lock:
        previous = min(ceiling, max(_EXCHANGE_SECONDS,
                                    _startup_budgets.get(provenance, _EXCHANGE_SECONDS)))
        learned = round(min(ceiling, max(_EXCHANGE_SECONDS, 2 * observed,
                                         _STARTUP_BUDGET_DECAY * previous)), 3)
        if learned <= _EXCHANGE_SECONDS:
            _startup_budgets.pop(provenance, None)
        else:
            _startup_budgets[provenance] = learned
            _startup_budgets.move_to_end(provenance)
            while len(_startup_budgets) > _STARTUP_BUDGET_SIGNATURES:
                _startup_budgets.popitem(last=False)
    if learned != previous:
        _logger.info('Codex native startup budget learned reason=%s observed_ms=%d '
                     'budget_ms=%d previous_ms=%d ceiling_ms=%d', reason,
                     round(observed * 1000), round(learned * 1000),
                     round(previous * 1000), round(ceiling * 1000))


def _startup_exhausted_until(provenance: str) -> float:
    """When refused cold starts of this launcher resume; 0.0 if allowed now."""
    ceiling = _startup_ceiling()
    with _startup_budget_lock:
        record = _startup_exhausted.get(provenance)
        if record is not None and (time.monotonic() >= record[0] or record[1] != ceiling):
            del _startup_exhausted[provenance]  # Expired, or the operator changed the ceiling.
            record = None
    return 0.0 if record is None else record[0]


def _refuse_cold_starts(provenance: str, ceiling: float) -> None:
    """Open the refusal window after a startup exhausted the ceiling; bounded LRU."""
    with _startup_budget_lock:
        _startup_exhausted[provenance] = (time.monotonic() + _STARTUP_EXHAUSTED_SECONDS, ceiling)
        _startup_exhausted.move_to_end(provenance)
        while len(_startup_exhausted) > _STARTUP_BUDGET_SIGNATURES:
            _startup_exhausted.popitem(last=False)
    _logger.warning('Codex native startup exhausted its ceiling ceiling_ms=%d; cold starts '
                    'refused for %d s (TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS raises the ceiling)',
                    round(ceiling * 1000), _STARTUP_EXHAUSTED_SECONDS)


class _CommandFailure(Exception):
    """Non-secret protocol failure; subprocess/parser text never escapes."""


class _CommandCancelled(_CommandFailure):
    """Clear/exit stopped this native wait; callers see a transient outage."""

    def __init__(self):
        super().__init__('codex_command_unavailable')


class _StartupCeilingTimeout(_CommandFailure):
    """A cold start timed out at the ceiling, or is refused after one did."""

    def __init__(self):
        super().__init__('codex_command_timeout')


def _explicit_home_issue(environment: dict, home: Path) -> str:
    """Stat only the authoritative override; absence is not a permission failure.

    No mkdir, credential read or alternative-home search. Relative overrides
    follow the launch cwd, not the serving process's unrelated directory.
    Windows retains the existing strict file-only adapter.
    """
    codex_home = environment.get('CODEX_HOME')
    if not codex_home or os.name == 'nt':
        return ''
    try:
        root = Path(codex_home)
        info = (root if root.is_absolute() else home / root).stat()
    except (FileNotFoundError, NotADirectoryError):
        return _HOME_UNAVAILABLE_ISSUE
    except OSError as error:
        return (_HOME_STORAGE_FULL_ISSUE if error.errno == errno.ENOSPC
                else 'codex_command_unavailable')  # Unknown is not evidence of absence.
    except (ValueError, TypeError):
        return 'codex_command_unavailable'
    return '' if stat.S_ISDIR(info.st_mode) else _HOME_UNAVAILABLE_ISSUE


def explicit_home_is_unavailable(*, environment=None, home: Path | None = None) -> bool:
    """Confirmed local source absence fences discovery, independent of cause.

    A positive command/storage failure stays attributed to that failure; it
    cannot authorize borrowing another login when the explicit source vanished.
    This read-only check never searches homes or observes credential contents.
    """
    environment = os.environ if environment is None else environment
    home = home or Path(environment.get('HOME') or Path.home())
    return _explicit_home_issue(environment, home) == _HOME_UNAVAILABLE_ISSUE


def _command_failure_issue(error: Exception) -> str:
    """Preserve fixed native failures and positive ENOSPC; never parser text."""
    if isinstance(error, _CommandFailure):
        return str(error)
    return (_HOME_STORAGE_FULL_ISSUE if isinstance(error, OSError)
            and error.errno == errno.ENOSPC else 'codex_command_unavailable')


def _recheck_home_failure(issue: str, home_issue: str) -> str:
    """Fresh source evidence refines only success or unqualified outages.

    Positive storage, source and policy failures are not replaced by a second
    failure. Their home witness separately fences verified grace and discovery.
    """
    if home_issue and issue in {'', 'codex_command_unavailable',
                               'codex_command_timeout', 'codex_command_missing',
                               'codex_not_signed_in', 'codex_credentials_unreadable',
                               'codex_access_expired'}:
        return home_issue
    return issue


def _home_storage_full(environment: dict, home: Path) -> bool:
    """True when the Codex home filesystem cannot accept new files.

    Side-effect free: one statvfs per candidate, no writes. Filesystems that
    do not report inode capacity (``f_files == 0``, e.g. some FUSE mounts)
    are treated as unknown, never as full. Absence of statvfs (Windows)
    also reports unknown: the file adapter owns that platform.
    """
    statvfs = getattr(os, 'statvfs', None)
    if statvfs is None:
        return False
    candidates = [home]
    codex_home = environment.get('CODEX_HOME')
    if codex_home:
        candidates.append(Path(codex_home))
    else:
        candidates.append(home / '.codex')
    for candidate in candidates:
        try:
            probe = candidate if candidate.is_dir() else candidate.parent
            info = statvfs(probe)
        except (OSError, ValueError):
            continue
        if getattr(info, 'f_files', 0) and info.f_favail < _MIN_HOME_FREE_INODES:
            return True
        if not getattr(info, 'f_blocks', 0):
            continue
        try:
            free_bytes = info.f_bavail * info.f_frsize
        except (AttributeError, OverflowError):
            continue
        if free_bytes < _MIN_HOME_FREE_BYTES:
            return True
    return False


def _launch(environment: dict, home: Path) -> list[str] | None:
    # POSIX shells provide automatic interactive configuration discovery.
    # Windows retains the existing file adapter until a native launcher is
    # integrated with its process-tree supervisor.
    if os.name == 'nt':
        return None
    shell = _native_shell(environment)
    # User shell startup is trusted local configuration, never bridge input.
    if shell:
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
            try:
                stream.close()
            except BrokenPipeError as error:
                # The child is reaped; a failed buffered flush must not replace
                # the protocol failure or prevent closing the other pipe.
                _logger.debug('Codex native pipe closed during cleanup: %s', error)
    if reader is not None and reader.is_alive():
        raise _CommandFailure('codex_command_cleanup_failed')


def _protocol(environment: dict, home: Path, *, provenance: str,
              renew_root: Path | None = None, cap_deadline: float | None = None):
    """Initialize once; yield metadata; send the next bounded exchange budget.

    Spawn+initialize uses the learned cold startup budget for this launcher
    provenance and teaches it; each later exchange uses the exchange budget.
    cap_deadline bounds both for a caller that already spent time waiting.
    Closing this generator reaps the child and joins its bounded reader.
    Only the serialized owner may resume/close it.
    """
    started = time.monotonic()
    learned_startup = _startup_seconds(provenance)
    budget = (learned_startup if cap_deadline is None
              else max(0, min(learned_startup, cap_deadline - started)))
    deadline = started + budget
    home_issue = _explicit_home_issue(environment, home)
    if home_issue:
        raise _CommandFailure(home_issue)
    command = _launch(environment, home)
    if command is None:
        raise _CommandFailure('codex_command_missing')
    if _home_storage_full(environment, home):
        # Fail before spawning: neither the probe child nor Codex's own
        # sqlite/thread state under this home can create files. The caller
        # reports this distinctly so the user frees space instead of
        # re-logging in to no effect.
        raise _CommandFailure(_HOME_STORAGE_FULL_ISSUE)
    if _cancel_requests:
        _logger.debug('Codex native metadata phase=cancelled_before_spawn')
        raise _CommandCancelled()
    if _startup_exhausted_until(provenance):
        _logger.debug('Codex native metadata phase=cold_start_refused')
        raise _StartupCeilingTimeout()
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
        phase = {1: 'initialize', 2: 'config', 3: 'requirements', 4: 'renewal'}[request_id]
        while time.monotonic() < deadline:
            if _cancel_requests:
                _logger.info('Codex native credential probe cancelled phase=%s elapsed_ms=%d',
                             phase, round((time.monotonic() - started) * 1000))
                raise _CommandCancelled()
            if failed.is_set():
                raise _CommandFailure('codex_command_invalid_response')
            try:
                message = inbox.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    if process.returncode == 127:
                        raise _CommandFailure('codex_command_missing')
                    # Codex itself keeps sqlite/thread state under its home;
                    # a disk-full death there must not read as a broken
                    # command (re-login cannot repair it).
                    if _home_storage_full(environment, home):
                        raise _CommandFailure(_HOME_STORAGE_FULL_ISSUE)
                    raise _CommandFailure('codex_command_unavailable')
                continue
            if message.get('id') != wire_ids[request_id]:
                continue
            if 'error' in message or not isinstance(message.get('result'), dict):
                raise _CommandFailure('codex_command_unavailable')
            return message['result']
        _logger.warning('Codex native credential probe failed phase=%s issue=codex_command_timeout '
                        'elapsed_ms=%d budget_ms=%d', phase,
                        round((time.monotonic() - started) * 1000), round(budget * 1000))
        raise _CommandFailure('codex_command_timeout')

    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'tofu_codex_login', 'version': '1.0.0'}}})
        _logger.debug('Codex native metadata phase=initialize_sent')
        try:
            hello = result(1)
        except _CommandFailure as error:
            # A budget truncated by the caller's deadline is no startup evidence.
            if str(error) == 'codex_command_timeout' and budget >= learned_startup:
                _learn_startup_seconds(provenance, budget, reason='initialize_timeout')
                ceiling = _startup_ceiling()
                if learned_startup >= ceiling:
                    _refuse_cold_starts(provenance, ceiling)
                    raise _StartupCeilingTimeout() from None
            raise
        initialized = time.monotonic()
        _logger.debug('Codex native metadata phase=initialize_received elapsed_ms=%d',
                      round((initialized - started) * 1000))
        _learn_startup_seconds(provenance, initialized - started, reason='initialize_observed')
        started = initialized
        budget = (_EXCHANGE_SECONDS if cap_deadline is None
                  else max(0, min(_EXCHANGE_SECONDS, cap_deadline - initialized)))
        deadline = initialized + budget
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
    except BrokenPipeError as error:
        if _home_storage_full(environment, home):
            raise _CommandFailure(_HOME_STORAGE_FULL_ISSUE) from error
        raise
    finally:
        _stop(process, reader)


def _close_session() -> None:
    """Caller holds _lock; detach before teardown even if cleanup fails."""
    global _session
    owned, _session = _session, None
    _session_wake.set()
    if owned is not None:
        owned['protocol'].close()


def _publish_home_checked_context(context: dict, *, environment: dict, home: Path,
                                  deadline: float, lock_held: bool = False) -> dict:
    """Final source check for warm/cold/grace and renewal return boundaries.

    Healthy reads retain the lock-free path. Unverifiable explicit sources
    refuse immediately and revoke only their environment-matching metadata
    under the existing bounded auth flight; a busy flight cannot grant grace.
    Positive storage/policy/account failures retain their original cause.
    """
    global _cached, _verified
    fresh_issue = _explicit_home_issue(environment, home)
    if fresh_issue not in {_HOME_UNAVAILABLE_ISSUE, 'codex_command_unavailable'}:
        return context  # Positive ENOSPC alone keeps existing bounded grace.
    context = {'shared_login_issue': _recheck_home_failure(
        context.get('shared_login_issue') or '', fresh_issue)}
    signature = (_environment_revision(environment, home),)
    acquired = lock_held or _lock.acquire(timeout=max(0, deadline - time.monotonic()))
    if acquired:
        try:
            if _verified is not None and _verified[0] == signature:
                _verified = None
            if _cached is not None and _cached[0] == signature:
                _cached = (signature, 0.0, context)
            if _session is not None and _session['environment'] == signature[0]:
                _close_session()
        finally:
            if not lock_held:
                _lock.release()
    return context


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
    A reused session gets one exchange budget; a new app-server first gets the
    learned cold startup budget. timeout_seconds caps the whole exchange.
    """
    global _session, _session_reaper
    cap_deadline = None if timeout_seconds is None else time.monotonic() + max(0, timeout_seconds)
    provenance = _environment_revision(environment, home)
    if renew_root is not None:
        protocol = _protocol(environment, home, provenance=provenance,
                             renew_root=renew_root, cap_deadline=cap_deadline)
        try:
            return next(protocol)
        finally:
            protocol.close()
    owned = _session
    try:
        home_issue = _explicit_home_issue(environment, home)
        if home_issue:
            raise _CommandFailure(home_issue)
        if owned is not None and (owned['environment'] != provenance
                or time.monotonic() >= owned['born'] + _SESSION_ROTATE_SECONDS
                or _source_revision(environment, home, owned['context']) != owned['revision']):
            _close_session()
            owned = None
        if owned is not None:
            _logger.debug('Codex native metadata phase=session_reuse')
            budget = (_EXCHANGE_SECONDS if cap_deadline is None
                      else min(_EXCHANGE_SECONDS, cap_deadline - time.monotonic()))
            context = owned['protocol'].send(max(0, budget))
        else:
            born = time.monotonic()
            protocol = _protocol(environment, home, provenance=provenance, cap_deadline=cap_deadline)
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
    shell = _native_shell(environment)
    if shell:
        paths.append(Path(shell))
    executable = shutil.which('codex', path=environment.get('PATH', os.defpath))
    if executable:
        paths.append(Path(executable))
    stamps = []
    for path in paths:
        try:
            link = path.lstat()
            # Only a symlink has a distinct target; otherwise lstat is stat.
            target = path.stat() if stat.S_ISLNK(link.st_mode) else link
        except FileNotFoundError:
            stamps.append((str(path), None))
            continue
        # Other stat failures reject reuse, including unreadable symlink targets.
        stamps.append((str(path), *((v.st_dev, v.st_ino, v.st_mtime_ns,
                                    v.st_ctime_ns, v.st_size, v.st_mode, v.st_uid)
                                   for v in (link, target))))
    return tuple(stamps)


def command_context(*, environment=None, home: Path | None = None,
                    retry_failed: bool = False, flight_started: float | None = None) -> dict:
    """One bounded metadata cache and one flight; never cache borrowed tokens.

    flight_started (time.monotonic()) anchors a caller's single native flight
    so a following renewal receives only what this discovery left of it.
    """
    global _cached, _verified, _metadata_probe
    deadline = time.monotonic() + _EXCHANGE_SECONDS
    environment = dict(os.environ if environment is None else environment)
    home = home or Path(environment.get('HOME') or Path.home())
    signature = (_environment_revision(environment, home),)
    home_issue = _explicit_home_issue(environment, home)
    home_unverifiable = home_issue in {_HOME_UNAVAILABLE_ISSUE, 'codex_command_unavailable'}
    # A valid immutable metadata snapshot needs no native-auth lock. Renewal
    # can hold that lock for network I/O; healthy file reads must not queue
    # behind it. Account, expiry and file permissions are still checked below
    # by codex_local_login on every read, never served from this cache.
    observed = _cached

    def verified_context():
        verified = _verified
        if home_unverifiable or not verified or verified[0] != signature or time.monotonic() >= verified[1]:
            return None
        try:
            matches = _source_revision(environment, home, verified[2]) == verified[3]
        except (OSError, ValueError):
            matches = False
        # A matching stamp can be read just before an unmount. Validate the
        # explicit source after that read, not only on command_context entry.
        fresh_issue = _explicit_home_issue(environment, home)
        if not matches or fresh_issue in {_HOME_UNAVAILABLE_ISSUE, 'codex_command_unavailable'}:
            return None
        return dict(verified[2])

    def select(context):
        if (context.get('shared_login_issue') in _TRANSIENT_ISSUES
                and context.get('shared_login_issue') != _HOME_UNAVAILABLE_ISSUE):
            return verified_context() or dict(context)
        return dict(context)

    def usable(cached, *, bypass_failure):
        if not cached or cached[0] != signature or time.monotonic() >= cached[1]:
            return False
        # Recheck home failures every read; restoring the same directory must
        # recover without waiting for the failure TTL or clearing shared state.
        if home_unverifiable or cached[2].get('shared_login_issue') == _HOME_UNAVAILABLE_ISSUE:
            return False
        if not cached[2].get('shared_login_issue') and verified_context() is None:
            return False
        return not (bypass_failure and cached[2].get('shared_login_issue') in _TRANSIENT_ISSUES)

    def run_probe(probe_deadline):
        global _cached, _verified
        now = time.monotonic()
        startup_exhausted = False
        try:
            context = _exchange(environment, home, timeout_seconds=max(0, probe_deadline - now))
        except _CommandCancelled as error:
            # Clear owns the metadata now; a cancelled flight publishes nothing.
            return {'shared_login_issue': _command_failure_issue(error)}
        except (_CommandFailure, OSError, ValueError, TypeError, RuntimeError) as error:
            startup_exhausted = isinstance(error, _StartupCeilingTimeout)
            context = {'shared_login_issue': _command_failure_issue(error)}
        if (not context.get('shared_login_issue') and _verified
                and _verified[0] == signature
                and context['codex_home'] != _verified[2]['codex_home']):
            context = {'shared_login_issue': 'codex_source_changed'}
        if not context.get('shared_login_issue'):
            try:
                revision = _source_revision(environment, home, context)
            except (OSError, ValueError) as error:
                _verified = None
                context = {'shared_login_issue': _command_failure_issue(error)}
        # Startup/pipe/stamp operations can outlive their preflight. Recheck
        # immediately before publication, including successful native replies.
        # A stale entry-time boolean cannot authorize grace after source loss.
        fresh_home_issue = _explicit_home_issue(environment, home)
        issue = _recheck_home_failure(context.get('shared_login_issue') or '', fresh_home_issue)
        if issue:
            context = {'shared_login_issue': issue}
        fresh_home_unverifiable = fresh_home_issue in {
            _HOME_UNAVAILABLE_ISSUE, 'codex_command_unavailable'}
        if not issue:
            # Warm reads cannot extend policy loaded at native startup.
            limit = (_session['born'] if _session is not None else time.monotonic()) + _CONTEXT_MAX_AGE_SECONDS
            _verified = (signature, limit, dict(context), revision)
        elif (home_unverifiable or fresh_home_unverifiable
              or issue == _HOME_UNAVAILABLE_ISSUE or issue not in _TRANSIENT_ISSUES):
            _verified = None  # An unavailable home cannot authorize metadata grace.
            _close_session()
        if not issue:
            lifetime = _CONTEXT_TTL_SECONDS
        elif fresh_home_unverifiable:
            lifetime = 0.0
        elif startup_exhausted and issue == 'codex_command_timeout':
            # Passive reads join the refusal window instead of re-probing.
            lifetime = max(5.0, _startup_exhausted_until(signature[0]) - time.monotonic())
        else:
            lifetime = 5.0
        _cached = (signature, time.monotonic() + lifetime, context)
        selected = select(context)
        if context.get('shared_login_issue') and not selected.get('shared_login_issue'):
            _logger.warning('Codex native credential probe using bounded verified metadata issue=%s',
                            context['shared_login_issue'])
        return _publish_home_checked_context(selected, environment=environment,
            home=home, deadline=probe_deadline, lock_held=True)

    if usable(observed, bypass_failure=retry_failed):
        return _publish_home_checked_context(select(observed[2]), environment=environment,
            home=home, deadline=deadline)

    # Demand-driven revalidation keeps native startup off the healthy request
    # path. One owned, finite revalidation worker; no periodic probing/token cache.
    verified = verified_context()
    if verified is not None and not retry_failed:
        if _lock.acquire(blocking=False):
            cached = _cached
            if usable(cached, bypass_failure=False):
                _lock.release()
                return _publish_home_checked_context(select(cached[2]),
                    environment=environment, home=home, deadline=deadline)
            def revalidate():
                try:
                    # Rotation or a lost session restarts native Codex.
                    run_probe(time.monotonic() + _native_flight_seconds(signature[0]))
                finally:
                    _lock.release()
            try:
                worker = threading.Thread(target=revalidate, name='codex-metadata-revalidation', daemon=True)
                _metadata_probe = worker
                worker.start()
            except (OSError, RuntimeError) as error:
                _metadata_probe = None
                issue = (_HOME_STORAGE_FULL_ISSUE
                         if getattr(error, 'errno', None) == errno.ENOSPC
                         else 'codex_command_unavailable')
                _cached = (signature, time.monotonic() + 5.0,
                           {'shared_login_issue': issue})
                _lock.release()  # Optional worker admission must not kill a healthy read.
            except BaseException:
                _metadata_probe = None
                _lock.release()  # Roll back admission, including cancellation.
                raise
        return _publish_home_checked_context(verified, environment=environment,
            home=home, deadline=deadline)

    if not home_issue:
        # This probe, or the flight it queues behind, may start native Codex.
        # A source issue fails before any spawn and keeps the exchange bound.
        started = time.monotonic() if flight_started is None else flight_started
        deadline = started + _native_flight_seconds(signature[0])
    if not _lock.acquire(timeout=max(0, deadline - time.monotonic())):
        if home_issue:
            return _publish_home_checked_context({'shared_login_issue': home_issue},
                environment=environment, home=home, deadline=deadline)
        _logger.warning('Codex native credential probe failed phase=lock issue=codex_command_timeout')
        return _publish_home_checked_context(select({'shared_login_issue': 'codex_command_timeout'}),
            environment=environment, home=home, deadline=deadline)
    try:
        # Concurrent explicit recovery joins the result published while waiting.
        cached = _cached
        if usable(cached, bypass_failure=retry_failed and cached is observed):
            return _publish_home_checked_context(select(cached[2]), environment=environment,
                home=home, deadline=deadline, lock_held=True)
        return run_probe(deadline)
    finally:
        _lock.release()


def renew_command_login(root: Path, *, expected_account_id: str, settings: dict,
                        environment=None, home: Path | None = None,
                        flight_started: float | None = None) -> dict:
    """Renew only a previously checked account; no Tofu OAuth endpoint call.

    Renewal always starts a one-shot app-server: its lock wait and exchange
    share the learned cold startup budget plus one exchange budget, measured
    from flight_started when discovery already spent part of that flight.
    """
    environment = dict(os.environ if environment is None else environment)
    home = home or Path(environment.get('HOME') or Path.home())
    started = time.monotonic() if flight_started is None else flight_started
    deadline = started + _native_flight_seconds(_environment_revision(environment, home))
    if not _lock.acquire(timeout=max(0, deadline - time.monotonic())):
        return _publish_home_checked_context({'shared_login_issue': 'codex_command_timeout'},
            environment=environment, home=home, deadline=deadline)
    try:
        home_issue = _explicit_home_issue(environment, home)
        if home_issue in {_HOME_UNAVAILABLE_ISSUE, 'codex_command_unavailable'}:
            return _publish_home_checked_context({'shared_login_issue': home_issue},
                environment=environment, home=home, deadline=deadline, lock_held=True)
        # Another reader may have renewed while this caller waited. Re-read
        # under the same native-auth flight before requesting another rotation.
        from lib.oauth.codex_local_login import _read_file_login
        current = _read_file_login(expected_account_id=expected_account_id,
            environment={'CODEX_HOME': str(root)}, command_settings=settings,
            command_verified=True)
        current_context = ({'codex_home': str(root), 'settings': settings, 'shared_login_issue': ''}
            if current.get('access_token') else
            {'shared_login_issue': current.get('shared_login_issue', 'codex_credentials_unreadable')})
        current_context = _publish_home_checked_context(current_context, environment=environment,
            home=home, deadline=deadline, lock_held=True)
        if current_context.get('shared_login_issue') != 'codex_access_expired':
            return current_context
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _publish_home_checked_context({'shared_login_issue': 'codex_command_timeout'},
                environment=environment, home=home, deadline=deadline, lock_held=True)
        try:
            context = _exchange(environment, home, renew_root=root, timeout_seconds=remaining)
        except (_CommandFailure, OSError, ValueError, TypeError, RuntimeError) as error:
            context = {'shared_login_issue': _command_failure_issue(error)}
        issue = _recheck_home_failure(context.get('shared_login_issue') or '',
                                      _explicit_home_issue(environment, home))
        return _publish_home_checked_context({'shared_login_issue': issue} if issue else context,
            environment=environment, home=home, deadline=deadline, lock_held=True)
    finally:
        _lock.release()


def clear_command_context() -> None:
    """Cancel and drain native work before forgetting native source metadata.

    An in-flight native wait observes the cancellation within one poll, then
    terminates and reaps its own child and publishes nothing, so the flight
    lock frees within child teardown, never a cold startup budget. Flights
    admitted before this returns are refused before spawn.
    """
    global _cached, _verified, _metadata_probe, _session_reaper, _session_wake, _cancel_requests
    with _cancel_lock:
        _cancel_requests += 1
    try:
        with _lock:
            _close_session()
            reaper = _session_reaper
            _session_reaper = None
            _session_wake = threading.Event()
            _cached = None
            _verified = None
            worker = _metadata_probe
            _metadata_probe = None
            with _startup_budget_lock:
                _startup_exhausted.clear()  # Explicit reconnect retries native startup at once.
        if worker is not None:
            worker.join()  # Native exchange/teardown are bounded; never abandon it.

        if reaper is not None:
            # Still cancelling: a new flight must not hold the lock this reaper needs.
            reaper.join()
    finally:
        with _cancel_lock:
            _cancel_requests -= 1


atexit.register(clear_command_context)

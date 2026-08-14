#!/usr/bin/env python3
"""Human-facing lifecycle CLI for the project-local Tofu manager."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from server_manager import listener_pids, port_accepts, read_lock_status


PROJECT = os.path.realpath(os.environ.get('TOFU_PROJECT_PATH') or
                           os.path.dirname(os.path.abspath(__file__)))
MANAGER_HOST = os.environ.get('TOFU_SUPERVISOR_HOST', '127.0.0.1')
try:
    MANAGER_PORT = int(os.environ.get('TOFU_SUPERVISOR_PORT', '15001'))
except ValueError:
    MANAGER_PORT = 15001
BASE_URL = f'http://{MANAGER_HOST}:{MANAGER_PORT}'
# supervisor.sh intentionally waits up to 20 seconds for a detached watchdog
# on a saturated host. The caller must outlive that whole budget plus shell
# cleanup; a shorter timeout leaves a healthy manager behind while reporting
# launch failure.
_MANAGER_LAUNCH_TIMEOUT_FLOOR = 25.0

_CGROUP_V2_USAGE = '/sys/fs/cgroup/memory.current'
_CGROUP_V2_LIMIT = '/sys/fs/cgroup/memory.max'
_CGROUP_V2_SWAP_LIMIT = '/sys/fs/cgroup/memory.swap.max'
_CGROUP_V2_EVENTS = '/sys/fs/cgroup/memory.events'
_CGROUP_V1_USAGE = '/sys/fs/cgroup/memory/memory.usage_in_bytes'
_CGROUP_V1_LIMIT = '/sys/fs/cgroup/memory/memory.limit_in_bytes'
_CGROUP_V1_MEMSW_LIMIT = '/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes'
_CGROUP_V1_OOM = '/sys/fs/cgroup/memory/memory.oom_control'
_CGROUP_V1_FAILCNT = '/sys/fs/cgroup/memory/memory.failcnt'


class ManagerUnavailable(RuntimeError):
    pass


def _request(path: str, body: dict | None = None, timeout: float = 5.0) -> dict:
    data = json.dumps(body).encode('utf-8') if body is not None else None
    request = urllib.request.Request(
        BASE_URL + path, data=data, method='POST' if body is not None else 'GET')
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode('utf-8') or '{}')
        except (ValueError, TypeError):
            payload = {'ok': False, 'message': str(exc)}
        payload.setdefault('httpStatus', exc.code)
        return payload
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ManagerUnavailable(str(exc)) from exc


def _manager_health() -> dict | None:
    try:
        payload = _request('/health', timeout=1.0)
        if payload.get('ok') and PROJECT in payload.get('projects', []):
            return payload
    except ManagerUnavailable:
        pass
    return None


def ensure_manager(timeout: float = 8.0) -> dict:
    health = _manager_health()
    if health:
        return health
    script = os.path.join(PROJECT, 'supervisor.sh')
    env = os.environ.copy()
    env['TOFU_SUPERVISOR_PROJECTS'] = PROJECT
    env['TOFU_SUPERVISOR_PYTHON'] = sys.executable
    try:
        result = subprocess.run(
            ['bash', script, 'daemon'], cwd=PROJECT, env=env,
            capture_output=True, text=True,
            timeout=max(_MANAGER_LAUNCH_TIMEOUT_FLOOR, timeout + 2.0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagerUnavailable(f'cannot launch manager: {exc}') from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = _manager_health()
        if health:
            return health
        time.sleep(0.2)
    detail = ((result.stdout or '') + (result.stderr or '')).strip()
    log = os.path.join(PROJECT, 'logs', 'server-manager.log')
    raise ManagerUnavailable(
        f'manager did not become ready on {BASE_URL}; {detail or "no launcher output"}; '
        f'log: {log}')


def _remote_status(*, probe: bool = False) -> dict | None:
    query = urllib.parse.urlencode({'projectPath': PROJECT, 'probe': '1' if probe else '0'})
    try:
        return _request('/status?' + query, timeout=2.0)
    except ManagerUnavailable:
        return None


def _post(action: str, **extra) -> dict:
    ensure_manager()
    return _request('/' + action, {
        'projectPath': PROJECT,
        'source': extra.pop('source', 'serverctl'),
        **extra,
    }, timeout=35.0 if action in ('stop', 'restart') else 5.0)


def _forwarded_server_env() -> dict[str, str]:
    keys = ('PORT', 'BIND_HOST', 'TOFU_TLS', 'TLS_CERTFILE', 'TLS_KEYFILE',
            'TOFU_PROCESS_RSS_RECYCLE_MB')
    return {key: os.environ[key] for key in keys if os.environ.get(key)}


def _wait_ready(timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = _remote_status() or {}
        if last.get('running') and port_accepts(int(last.get('port') or 15000)):
            return last
        if last.get('observed') in ('conflict', 'crashloop'):
            return last
        time.sleep(0.5)
    return last


def _print_status(status: dict, *, manager_online: bool = True) -> None:
    print(f"Manager : {'running' if manager_online else 'not running'}"
          + (f" (PID {status.get('managerPid')})" if status.get('managerPid') else ''))
    print(f"Desired : {status.get('desired', 'unknown')}")
    print(f"Server  : {status.get('observed', 'unknown')}")
    if status.get('pid'):
        print(f"PID     : {status['pid']}")
    if status.get('processStartedAt'):
        print('Started : ' + time.strftime(
            '%Y-%m-%d %H:%M:%S', time.localtime(float(status['processStartedAt']))))
    print(f"Port    : {status.get('port', os.environ.get('PORT', '15000'))}")
    if status.get('launchSource'):
        print(f"Source  : {status['launchSource']}")
    if status.get('restartCount') is not None:
        recent = status.get('recentFailureCount')
        window = status.get('failureWindowSeconds')
        detail = (f'; recent={recent}/{status.get("maxFailures")} in '
                  f'{float(window):.0f}s'
                  if recent is not None and window is not None else '')
        print(f"Restarts: total={status.get('restartCount', 0)}{detail}")
    if status.get('lastRecoverySeconds') is not None:
        print(f"Last RTO: {float(status['lastRecoverySeconds']):.3f}s")
    if status.get('workerRssGuardEnabled'):
        rss = status.get('workerRssBytes')
        limit = status.get('workerRssRecycleBytes')
        print('RSS guard: '
              f"{float(rss or 0) / (1024 ** 3):.2f} GiB / "
              f"{float(limit or 0) / (1024 ** 3):.2f} GiB hard ceiling")
    if status.get('lastExitCause'):
        print(f"Last exit: {status['lastExitCause']}"
              + (f" ({status.get('lastFailureReason')})"
                 if status.get('lastFailureReason') else ''))
    if status.get('lastError'):
        print(f"Problem : {status['lastError']}")
    if status.get('workerLog'):
        print(f"Log     : {status['workerLog']}")


def cmd_start(args: argparse.Namespace) -> int:
    return managed_start(args.server_args, wait=args.wait, source=args.source)


def managed_start(server_args: list[str] | None = None, *, wait: float = 180.0,
                  source: str = 'python-server.py') -> int:
    """Start through the manager; used by both the CLI and ``server.py``."""
    forwarded = list(server_args or [])
    if forwarded[:1] == ['--']:
        forwarded = forwarded[1:]
    result = _post('start', source=source, serverArgs=forwarded,
                   serverEnv=_forwarded_server_env())
    if not result.get('ok'):
        print(f"Tofu failed to start: {result.get('message') or result.get('error')}",
              file=sys.stderr)
        _print_status(result)
        return 1
    status = _wait_ready(wait)
    if status.get('running') and port_accepts(int(status.get('port') or 15000)):
        label = 'already running' if result.get('alreadyRunning') else 'started'
        print(f"Tofu {label} (PID {status.get('pid')}, port {status.get('port')}).")
        print(f"Status: {sys.executable} serverctl.py status")
        print(f"Logs  : {sys.executable} serverctl.py logs -f")
        return 0
    print('Tofu was launched but did not become ready before the timeout.', file=sys.stderr)
    _print_status(status or result)
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    result = _post('stop', source=args.source)
    print(result.get('message') or ('stopped' if result.get('ok') else 'stop failed'))
    return 0 if result.get('ok') else 1


def _approve_restart_if_needed(args: argparse.Namespace) -> bool:
    # Interactive terminal invocation is itself the human action. Automated
    # callers must present/consume the existing one-time approval mechanism.
    gate_passed = os.environ.get('TOFU_RESTART_GATE_PASSED') == '1'
    if sys.stdin.isatty() or gate_passed:
        if args.yes or gate_passed:
            return True
        try:
            return input('Restart the running Tofu server? [y/N] ').strip().lower() in ('y', 'yes')
        except EOFError:
            return False
    try:
        from lib.lifecycle_approval import consume_any
        ok, why, _ = consume_any('restart')
    except Exception as exc:
        print(f'Restart approval check failed: {exc}', file=sys.stderr)
        return False
    if not ok:
        print(f'Restart requires human approval ({why}).', file=sys.stderr)
    return ok


def cmd_restart(args: argparse.Namespace) -> int:
    current = _remote_status()
    live = bool(current and current.get('running'))
    if current is None:
        live = bool(read_lock_status(PROJECT).get('running'))
    if live and not _approve_restart_if_needed(args):
        return 3
    forwarded = list(args.server_args or [])
    if forwarded[:1] == ['--']:
        forwarded = forwarded[1:]
    result = _post('restart', source=args.source, serverArgs=forwarded or None,
                   serverEnv=_forwarded_server_env())
    if not result.get('ok'):
        print(f"Restart failed: {result.get('message')}", file=sys.stderr)
        return 1
    status = _wait_ready(args.wait)
    if status.get('running') and port_accepts(int(status.get('port') or 15000)):
        print(f"Tofu restarted (PID {status.get('pid')}, port {status.get('port')}).")
        return 0
    print('Restart launched, but the server is not ready.', file=sys.stderr)
    _print_status(status or result)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    status = _remote_status(probe=True)
    online = status is not None
    if status is None:
        low = read_lock_status(PROJECT)
        pids = listener_pids(int(os.environ.get('PORT', '15000')))
        status = {
            **low,
            'desired': 'unknown',
            'observed': ('running-unmanaged' if low.get('running') else
                         ('conflict' if pids else 'stopped')),
            'port': int(os.environ.get('PORT', '15000')),
            'foreignListenerPids': [pid for pid in pids if pid != low.get('pid')],
            'lastError': 'manager is not running',
        }
    if args.json:
        print(json.dumps({'ok': True, 'managerOnline': online, **status},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_status(status, manager_online=online)
    unhealthy = status.get('observed') in ('conflict', 'crashloop', 'degraded')
    return 0 if online and not unhealthy else 1


def cmd_logs(args: argparse.Namespace) -> int:
    status = _remote_status() or {}
    path = status.get('managerLog' if args.manager else 'workerLog')
    if not path:
        path = os.path.join(PROJECT, 'logs',
                            'server-manager.log' if args.manager else 'server-console.log')
    if args.follow:
        try:
            return subprocess.call(['tail', '-n', str(args.lines), '-f', path])
        except OSError as exc:
            print(f'Cannot follow {path}: {exc}', file=sys.stderr)
            return 1
    try:
        return subprocess.call(['tail', '-n', str(args.lines), path])
    except OSError as exc:
        print(f'Cannot read {path}: {exc}', file=sys.stderr)
        return 1


def _proc_lines(pattern: str) -> list[str]:
    try:
        out = subprocess.run(['ps', '-eo', 'pid,ppid,sid,lstart,stat,args'],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines() if pattern in line and
            'serverctl.py doctor' not in line]


def _read_first_text(paths: tuple[str, ...]) -> str | None:
    for path in paths:
        try:
            return Path(path).read_text(encoding='utf-8').strip()
        except OSError:
            continue
    return None


def _parse_counter(text: str | None, name: str) -> int | None:
    for line in (text or '').splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == name:
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


def _finite_bytes(text: str | None) -> int | None:
    try:
        value = int(text or '')
    except (TypeError, ValueError):
        return None
    # cgroup v1 represents "unlimited" with a huge page-aligned integer.
    return value if 0 <= value < (1 << 60) else None


def cgroup_memory_snapshot(worker_pid: int | None = None) -> dict:
    """Best-effort cgroup/OOM evidence for the read-only doctor command."""
    usage = _finite_bytes(_read_first_text((_CGROUP_V2_USAGE, _CGROUP_V1_USAGE)))
    limit = _finite_bytes(_read_first_text((_CGROUP_V2_LIMIT, _CGROUP_V1_LIMIT)))
    v2_swap = _finite_bytes(_read_first_text((_CGROUP_V2_SWAP_LIMIT,)))
    memsw_limit = _finite_bytes(_read_first_text((_CGROUP_V1_MEMSW_LIMIT,)))
    if v2_swap is not None:
        swap_limit = v2_swap
    elif memsw_limit is not None and limit is not None:
        swap_limit = max(0, memsw_limit - limit)
    else:
        swap_limit = None
    events = _read_first_text((_CGROUP_V2_EVENTS, _CGROUP_V1_OOM))
    oom_kills = _parse_counter(events, 'oom_kill')
    failcnt = _finite_bytes(_read_first_text((_CGROUP_V1_FAILCNT,)))
    worker_rss = None
    if isinstance(worker_pid, int) and worker_pid > 0:
        try:
            fields = Path(f'/proc/{worker_pid}/statm').read_text().split()
            worker_rss = int(fields[1]) * os.sysconf('SC_PAGE_SIZE')
        except (OSError, ValueError, IndexError):
            pass
    return {
        'usageBytes': usage,
        'limitBytes': limit,
        'usagePct': (round(usage * 100.0 / limit, 1)
                     if usage is not None and limit else None),
        'swapLimitBytes': swap_limit,
        'oomKills': oom_kills,
        'failCount': failcnt,
        'workerRssBytes': worker_rss,
    }


def _project_setting(project_path: str, name: str, default: str = '') -> str:
    names = [name]
    if name.startswith('TOFU_'):
        names.append('CHATUI_' + name[len('TOFU_'):])
    for candidate in names:
        if candidate in os.environ:
            return os.environ[candidate].strip()
    try:
        lines = (Path(project_path) / '.env').read_text(
            encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return default
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        values[key.strip()] = value.strip().strip('"').strip("'")
    for candidate in names:
        if values.get(candidate):
            return values[candidate]
    return default


def sqlite_snapshot_status(project_path: str, now: float | None = None) -> dict:
    """Return cheap backup freshness evidence without opening the live DB."""
    project = Path(project_path).resolve()
    backend = _project_setting(str(project), 'TOFU_DB_BACKEND', 'sqlite').lower()
    raw_db = _project_setting(str(project), 'TOFU_DB_PATH', 'data/tofu.db')
    db_path = Path(os.path.expandvars(os.path.expanduser(raw_db)))
    if not db_path.is_absolute():
        db_path = project / db_path
    raw_dir = _project_setting(str(project), 'TOFU_SQLITE_SNAPSHOT_DIR', '')
    snapshot_dir = Path(os.path.expandvars(os.path.expanduser(raw_dir))) \
        if raw_dir else db_path.parent / 'db_snapshots'
    if not snapshot_dir.is_absolute():
        snapshot_dir = project / snapshot_dir
    try:
        max_age_hours = float(_project_setting(
            str(project), 'TOFU_SQLITE_SNAPSHOT_MAX_AGE_HOURS', '26'))
    except (TypeError, ValueError):
        max_age_hours = 26.0
    max_age_hours = max(1.0, min(24.0 * 30, max_age_hours))
    stamp = time.time() if now is None else float(now)
    candidates: list[tuple[float, int, Path]] = []
    try:
        for path in snapshot_dir.glob('tofu-*.sqlite3'):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                candidates.append((stat.st_mtime, stat.st_size, path))
    except OSError:
        pass
    latest = max(candidates, default=None, key=lambda item: (item[0], str(item[2])))
    age_hours = (max(0.0, stamp - latest[0]) / 3600.0) if latest else None
    required = backend not in ('pg', 'postgres', 'postgresql') and db_path.is_file()
    return {
        'required': required,
        'destinationConfigured': bool(raw_dir),
        'databasePath': str(db_path),
        'snapshotDir': str(snapshot_dir),
        'snapshotCount': len(candidates),
        'latestPath': str(latest[2]) if latest else None,
        'latestSizeBytes': latest[1] if latest else None,
        'latestMtime': latest[0] if latest else None,
        'latestAgeHours': round(age_hours, 2) if age_hours is not None else None,
        'maxAgeHours': max_age_hours,
        'fresh': (age_hours <= max_age_hours) if age_hours is not None else False,
    }


def _gib_text(value: int | None) -> str:
    return 'unknown' if value is None else f'{value / (1 << 30):.1f} GiB'


def _classify_recovery_cron(text: str) -> tuple[list[str], list[str]]:
    lines = [line for line in text.splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    legacy = [line for line in lines if 'tofu_guard' in line]
    manager = [line for line in lines
               if '# tofu_manager' in line and 'serverctl.py ensure' in line]
    return legacy, manager


def cmd_doctor(args: argparse.Namespace) -> int:
    status = _remote_status(probe=True)
    low = read_lock_status(PROJECT)
    port = int((status or {}).get('port') or os.environ.get('PORT', '15000'))
    pids = listener_pids(port)
    guard_flag = Path(PROJECT) / 'data' / '.tofu_guard_disabled'
    guard_loops = [line for line in _proc_lines('tofu_guard.sh --loop')
                   if PROJECT in line]
    supervisors = _proc_lines('supervisor.py')
    memory = cgroup_memory_snapshot(low.get('pid'))
    snapshot = sqlite_snapshot_status(PROJECT)
    cron_lines: list[str] = []
    manager_cron_lines: list[str] = []
    try:
        cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True,
                              timeout=3).stdout
        cron_lines, manager_cron_lines = _classify_recovery_cron(cron)
    except (OSError, subprocess.SubprocessError):
        pass
    report = {
        'projectPath': PROJECT,
        'managerOnline': status is not None,
        'managerStatus': status,
        'lock': low,
        'port': port,
        'listenerPids': pids,
        'legacyGuardDisabled': guard_flag.exists(),
        'legacyGuardLoops': guard_loops,
        'legacyGuardCron': cron_lines,
        'managerRecoveryCron': manager_cron_lines,
        'managerBootRecoveryInstalled': bool(manager_cron_lines),
        'supervisorProcesses': supervisors,
        'memory': memory,
        'snapshot': snapshot,
    }
    problems: list[str] = []
    fixes: list[str] = []
    observed = (status or {}).get('observed')
    if not status:
        problems.append('project lifecycle manager is not running')
        fixes.append(f'{sys.executable} serverctl.py start')
    elif observed in ('conflict', 'crashloop', 'degraded'):
        problems.append((status.get('lastError') or str(observed)).strip())
    recent_failures = int((status or {}).get('recentFailureCount') or 0)
    max_failures = int((status or {}).get('maxFailures') or 5)
    if status and recent_failures >= max(3, max_failures - 2) and observed != 'crashloop':
        problems.append(
            f'worker is unstable: {recent_failures}/{max_failures} recent failures')
    if guard_loops or cron_lines:
        problems.append('legacy tofu_guard still competes with the lifecycle manager')
        fixes.append(f'{sys.executable} serverctl.py install')
    if status and not manager_cron_lines:
        problems.append('manager will not recover after host/session restart')
        fixes.append(f'{sys.executable} serverctl.py install')
    if status and status.get('workerRssGuardEnabled') is False:
        problems.append('manager-side worker RSS recycle guard is disabled')
        fixes.append('set TOFU_PROCESS_RSS_RECYCLE_MB to a safe non-zero ceiling')
    if memory['usagePct'] is not None and memory['usagePct'] >= 90.0:
        problems.append('cgroup memory pressure is critical (>=90%)')
        fixes.append('isolate Tofu in a dedicated cgroup/container and add headroom')
    if snapshot['required'] and not snapshot['fresh']:
        if snapshot['latestPath']:
            problems.append(
                f"SQLite snapshot is stale ({snapshot['latestAgeHours']:.1f}h > "
                f"{snapshot['maxAgeHours']:.1f}h)")
        else:
            problems.append('no verified SQLite snapshot was found')
        fixes.append('run/repair the Database Backup schedule and verify its destination')
    if snapshot['required'] and not snapshot['destinationConfigured']:
        problems.append(
            'SQLite snapshots still share the authority data directory/failure domain')
        fixes.append(
            'set TOFU_SQLITE_SNAPSHOT_DIR to a separately mounted backup target')
    report['healthy'] = not problems
    report['problems'] = problems
    report['fixes'] = list(dict.fromkeys(fixes))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report['healthy'] else 1
    print(f'Project : {PROJECT}')
    print(f"Manager : {'OK' if status is not None else 'MISSING'}")
    print(f"Lock    : {'live pid ' + str(low.get('pid')) if low.get('running') else 'no live worker'}")
    print(f"Port    : {port} -> {pids or 'free/no PID visibility'}")
    print(f"Guard   : {'disabled' if guard_flag.exists() else 'enabled'}; "
          f'{len(guard_loops)} loop(s), {len(cron_lines)} cron line(s)')
    print(f"Recovery: {'installed' if manager_cron_lines else 'NOT installed'}"
          f'; {len(manager_cron_lines)} manager cron line(s)')
    print(f"Memory  : {_gib_text(memory['usageBytes'])} / "
          f"{_gib_text(memory['limitBytes'])}"
          + (f" ({memory['usagePct']:.1f}%)" if memory['usagePct'] is not None else '')
          + f"; swap limit {_gib_text(memory['swapLimitBytes'])}")
    print(f"OOM     : kernel kills={memory['oomKills'] if memory['oomKills'] is not None else 'unknown'}; "
          f"worker RSS={_gib_text(memory['workerRssBytes'])}")
    if status and status.get('workerRssGuardEnabled'):
        print(f"RSS cap : {_gib_text(status.get('workerRssRecycleBytes'))} "
              '(manager-enforced)')
    if snapshot['latestPath']:
        print(f"Snapshot: {snapshot['latestAgeHours']:.1f}h old; "
              f"{_gib_text(snapshot['latestSizeBytes'])}; "
              f"{snapshot['latestPath']}")
    elif snapshot['required']:
        print(f"Snapshot: MISSING under {snapshot['snapshotDir']}")
    for problem in report['problems']:
        print(f'Problem : {problem}')
    for fix in report['fixes']:
        print(f'Fix     : {fix}')
    if report['healthy']:
        print('Result  : lifecycle ownership is consistent')
    return 0 if report['healthy'] else 1


def _replace_guard_cron() -> tuple[bool, str]:
    try:
        old = subprocess.run(['crontab', '-l'], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    kept = [line for line in old.splitlines()
            if '# tofu_guard' not in line and '# tofu_manager' not in line]
    qproject = shlex.quote(PROJECT)
    qpython = shlex.quote(sys.executable)
    command = (f'@reboot cd {qproject} && {qpython} serverctl.py ensure '
               f'>/dev/null 2>&1 # tofu_manager')
    minute = (f'* * * * * cd {qproject} && {qpython} serverctl.py ensure '
              f'>/dev/null 2>&1 # tofu_manager')
    content = '\n'.join([*kept, command, minute]) + '\n'
    try:
        applied = subprocess.run(['crontab', '-'], input=content, text=True,
                                 capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if applied.returncode:
        return False, applied.stderr.strip()
    return True, ''


def cmd_install(_args: argparse.Namespace) -> int:
    health = ensure_manager()
    status = _remote_status(probe=True) or {}
    if status.get('observed') == 'conflict':
        print('Manager cannot take ownership safely: ' +
              (status.get('lastError') or 'external owner detected'), file=sys.stderr)
        print(f'Diagnose: {sys.executable} serverctl.py doctor', file=sys.stderr)
        return 1
    # Manager adoption happens before legacy recovery is disabled/removed.
    try:
        subprocess.run(['bash', os.path.join(PROJECT, 'deploy', 'tofu_guard.sh'), '--stop'],
                       cwd=PROJECT, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
    for line in _proc_lines('tofu_guard.sh --loop'):
        if PROJECT not in line:
            continue
        raw = line.split(None, 1)[0]
        if raw.isdigit():
            try:
                os.kill(int(raw), signal.SIGTERM)
            except OSError:
                pass
    ok, error = _replace_guard_cron()
    if not ok:
        print(f'Manager is running, but cron migration failed: {error}', file=sys.stderr)
        return 1
    print(f"Tofu manager installed (PID {health.get('managerPid')}).")
    print('Legacy tofu_guard cron entries were replaced; the current server was not restarted.')
    return 0


def cmd_ensure(_args: argparse.Namespace) -> int:
    try:
        ensure_manager()
        return 0
    except ManagerUnavailable as exc:
        print(exc, file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='serverctl.py',
                                     description='Manage the project-local Tofu server')
    sub = parser.add_subparsers(dest='command', required=True)

    start = sub.add_parser('start', help='start Tofu idempotently')
    start.add_argument('--wait', type=float, default=180.0)
    start.add_argument('--source', default='serverctl')
    start.add_argument('server_args', nargs=argparse.REMAINDER)
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser('stop', help='stop Tofu and keep it stopped')
    stop.add_argument('--source', default='serverctl')
    stop.set_defaults(func=cmd_stop)

    restart = sub.add_parser('restart', help='restart Tofu through the manager')
    restart.add_argument('-y', '--yes', action='store_true')
    restart.add_argument('--wait', type=float, default=180.0)
    restart.add_argument('--source', default='serverctl')
    restart.add_argument('server_args', nargs=argparse.REMAINDER)
    restart.set_defaults(func=cmd_restart)

    status = sub.add_parser('status', help='show owner, desired and observed state')
    status.add_argument('--json', action='store_true')
    status.set_defaults(func=cmd_status)

    logs = sub.add_parser('logs', help='show worker or manager logs')
    logs.add_argument('-f', '--follow', action='store_true')
    logs.add_argument('--manager', action='store_true')
    logs.add_argument('-n', '--lines', type=int, default=100)
    logs.set_defaults(func=cmd_logs)

    doctor = sub.add_parser('doctor', help='read-only lifecycle diagnostics')
    doctor.add_argument('--json', action='store_true')
    doctor.set_defaults(func=cmd_doctor)

    sub.add_parser('install', help='install manager recovery and migrate legacy guard').set_defaults(func=cmd_install)
    sub.add_parser('ensure', help=argparse.SUPPRESS).set_defaults(func=cmd_ensure)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ManagerUnavailable as exc:
        print(f'Tofu manager unavailable: {exc}', file=sys.stderr)
        print(f'Diagnose: {sys.executable} serverctl.py doctor', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

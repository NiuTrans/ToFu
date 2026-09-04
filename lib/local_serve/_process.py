"""lib/local_serve/_process.py — Managed server lifecycle.

One managed instance = one child process (``start_new_session=True`` so the
whole tree is killable) writing to ONE bounded log under
``data/local_serve/logs/``. The supervisor here is deliberately simple and
fully synchronous: the agent tool calls :func:`start_instance` and blocks
(with progress callbacks the tool relays into the conversation) until the
server answers on its OpenAI shim, an OOM ladder runs out, or a timeout
fires. No resident threads, no daemon — Tofu restarts simply report the
instance as ``stopped`` on next status check (the pid no longer exists).

OOM degradation ladder
----------------------
``_plan`` attaches an ordered ``degrade`` list to every plan. When the
server exits before readiness OR the log shows an OOM signature
(``OOM_PATTERNS``), the supervisor applies the next step — ``replace``
(swap the value of an existing flag), ``append`` (add flags), or
``env_replace`` (patch env) — and respawns. A step marked ``terminal`` is
never executed: it is the final user-facing advice (e.g. "pick a smaller
quant"). The ladder position persists in the ledger so a later restart
resumes at the working tier instead of rediscovering the OOM.

Readiness
---------
Default probe: ``GET <base_url>/models`` until HTTP 200 (vLLM / SGLang /
llama.cpp all serve it). Ollama needs its ``setup_steps`` first — wait for
the native API, write the Modelfile, ``ollama create``, then confirm the
served name appears in ``/v1/models``.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import urllib.request

from lib.log import get_logger
from lib.proxy import register_no_proxy_url

from . import _env as env_mod
from . import _store as store
from ._plan import MANAGED_PORT_BASE, MANAGED_PORT_END

logger = get_logger(__name__)

__all__ = ['OOM_PATTERNS', 'allocate_port', 'start_instance', 'stop_instance',
           'status_instance', 'log_tail']

OOM_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r'CUDA out of memory',
    r'OutOfMemoryError',
    r'insufficient (?:GPU )?memory',
    r'failed to allocate.*(?:kv|buffer|vram)',
    r'no available memory for the block pools',
    r'llama_kv_cache_init.*failed',
    r'ggml_backend_alloc_ctx_tensors_from_buft failed',
))

_READY_TIMEOUT = float(os.environ.get('TOFU_LOCAL_SERVE_READY_TIMEOUT_SEC',
                                      '1200'))
_SETUP_STEP_TIMEOUT = 600
_STOP_GRACE_SEC = 10.0
_LOG_MAX_BYTES = 8 * 1024 * 1024   # rotated once at spawn time, not streamed


def _logs_dir() -> str:
    d = os.path.join(env_mod.serve_root(), 'logs')
    os.makedirs(d, exist_ok=True)
    return d


def _log_path(instance_id: str) -> str:
    return os.path.join(_logs_dir(), '%s.log' % instance_id)


def log_tail(instance_id: str, *, max_bytes: int = 4000) -> str:
    path = _log_path(instance_id)
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def _rotate_log(path: str) -> None:
    try:
        if os.path.getsize(path) > _LOG_MAX_BYTES:
            os.replace(path, path + '.old')
    except OSError:
        pass


def _http_get(url: str, timeout: float = 2.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(65536).decode('utf-8', 'replace')
    except Exception as exc:
        # Readiness failures are expected while a model boots, so keep them at
        # debug level while retaining a traceback for diagnosing TLS/proxy or
        # malformed-response failures that otherwise look like a timeout.
        logger.debug('[LocalServe] readiness request failed: %s', exc,
                     exc_info=True)
        return 0, str(exc)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def allocate_port(*, ledger_rows=None, bind_test=True) -> int | None:
    """First free port in the managed band; None when the band is exhausted."""
    import socket
    used = {r.get('port') for r in (ledger_rows if ledger_rows is not None
                                    else store.list_instances())
            if isinstance(r.get('port'), int)}
    for port in range(MANAGED_PORT_BASE, MANAGED_PORT_END + 1):
        if port in used:
            continue
        if bind_test:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(('127.0.0.1', port))
            except OSError:
                continue
        return port
    return None


# ─────────────────────────── degrade ladder ───────────────────────────

def _apply_degrade(argv: list, env: dict, step: dict) -> tuple[list, dict]:
    argv = list(argv)
    env = dict(env)
    for flag, value in (step.get('replace') or {}).items():
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                argv[i + 1] = str(value)
        else:
            argv += [flag, str(value)]
    for extra in step.get('append') or []:
        if extra not in argv:
            argv.append(extra)
    env.update(step.get('env_replace') or {})
    return argv, env


def _log_shows_oom(text: str) -> bool:
    return any(p.search(text) for p in OOM_PATTERNS)


# ─────────────────────────── setup steps (Ollama) ───────────────────────────

def _run_setup_steps(record: dict, log, http_get, runner) -> dict:
    ctx = {}
    tmp_dir = os.path.join(env_mod.serve_root(), 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    for step in record.get('setup_steps') or []:
        kind = step.get('kind')
        if kind == 'write_file':
            path = os.path.join(tmp_dir, '%s.Modelfile' % record['id'])
            with open(path, 'w', encoding='utf-8') as f:
                f.write(step.get('content') or '')
            ctx[step.get('name') or 'file'] = path
        elif kind == 'run':
            argv = [a.format(**ctx) for a in step.get('argv') or []]
            if log:
                log('初始化: %s' % ' '.join(argv))
            proc = runner(argv, capture_output=True, text=True,
                          timeout=step.get('timeout', _SETUP_STEP_TIMEOUT),
                          env={**os.environ, **(record.get('env') or {})})
            if proc.returncode != 0:
                return {'ok': False,
                        'error': '初始化命令失败: %s'
                                 % ((proc.stderr or proc.stdout or '')[-400:])}
    return {'ok': True}


def _ollama_ready(record: dict, http_get) -> bool:
    status, _body = http_get('http://%s/api/version'
                             % record['env']['OLLAMA_HOST'], 2.0)
    return status == 200


def _served_listed(record: dict, http_get) -> bool:
    status, body = http_get(record['base_url'] + '/models', 2.0)
    if status != 200:
        return False
    name = record.get('served_name') or ''
    return not name or name in (body or '')


# ─────────────────────────── lifecycle ───────────────────────────

def start_instance(instance_id: str, *, log=None, popen=None, http_get=None,
                   runner=None, sleep=time.sleep, clock=time.monotonic,
                   ready_timeout: float | None = None) -> dict:
    """Install (if needed), spawn, await readiness, walk the OOM ladder.

    ``log`` receives user-facing progress lines. Returns the final ledger
    row with ``status`` in {running, failed}.
    """
    record = store.get_instance(instance_id)
    if record is None:
        return {'ok': False, 'error': '未知实例: %s' % instance_id}
    popen = popen or subprocess.Popen
    http_get = http_get or _http_get
    runner = runner or subprocess.run
    ready_timeout = ready_timeout if ready_timeout is not None else _READY_TIMEOUT

    def _log(msg):
        if log:
            log(msg)
        logger.info('[LocalServe] %s: %s', instance_id, msg)

    engine = record['engine']
    store.update_fields(instance_id, status='installing', last_error=None)
    _log('检查 %s 运行环境…' % engine)
    install = env_mod.ensure_engine(engine, log=_log)
    if not install.get('ok'):
        store.update_fields(instance_id, status='failed',
                            last_error=install.get('error'))
        return {'ok': False, **store.get_instance(instance_id)}

    argv = env_mod.resolve_launcher(engine, list(record['argv']))
    env = env_mod.launcher_env(engine, dict(record.get('env') or {}))
    register_no_proxy_url(record['base_url'])

    degrade = record.get('degrade') or []
    idx = int(record.get('degrade_index') or 0)
    attempt_argv, attempt_env = argv, env
    for done in degrade[:idx]:
        if done.get('terminal'):
            break
        attempt_argv, attempt_env = _apply_degrade(attempt_argv, attempt_env,
                                                   done)
    log_path = _log_path(record['id'])

    while True:
        store.update_fields(instance_id, status='starting', pid=None)
        _rotate_log(log_path)
        _log('启动: %s' % ' '.join(attempt_argv))
        try:
            out = open(log_path, 'ab', buffering=0)
        except OSError as e:
            store.update_fields(instance_id, status='failed',
                                last_error='无法打开日志文件: %s' % e)
            return {'ok': False, **store.get_instance(instance_id)}
        try:
            proc = popen(attempt_argv, stdout=out, stderr=subprocess.STDOUT,
                         env={**os.environ, **attempt_env},
                         cwd=env_mod.serve_root(), start_new_session=True)
        except (OSError, subprocess.SubprocessError) as e:
            out.close()
            store.update_fields(instance_id, status='failed',
                                last_error='进程启动失败: %s' % e)
            return {'ok': False, **store.get_instance(instance_id)}
        store.update_fields(instance_id, pid=proc.pid)

        is_ollama = engine == 'ollama'
        setup_done = False
        deadline = clock() + ready_timeout
        outcome = None  # None | 'ready' | 'dead' | 'timeout'
        while clock() < deadline:
            rc = proc.poll()
            if rc is not None:
                outcome = 'dead'
                break
            if is_ollama and not setup_done:
                if _ollama_ready({**record, 'env': attempt_env}, http_get):
                    step_result = _run_setup_steps(record, _log, http_get,
                                                   runner)
                    if not step_result['ok']:
                        outcome = 'dead'
                        _log(step_result['error'])
                        break
                    setup_done = True
                else:
                    sleep(2.0)
                    continue
            if _served_listed(record, http_get):
                outcome = 'ready'
                break
            sleep(2.0)

        tail = log_tail(record['id'])
        if outcome == 'ready':
            store.update_fields(instance_id, status='running',
                                argv=list(attempt_argv),
                                degrade_index=idx, last_error=None)
            _log('服务就绪: %s （模型 %s）'
                 % (record['base_url'], record.get('served_name')))
            return {'ok': True, **store.get_instance(instance_id)}

        # Failure path — decide whether the ladder has another rung.
        oom = _log_shows_oom(tail)
        reason = ('进程提前退出 (rc=%s)' % proc.poll() if outcome == 'dead'
                  else '等待就绪超时 (%.0fs)' % ready_timeout)
        try:
            if proc.poll() is None:
                _terminate(proc)
        except Exception as exc:
            # The original startup failure remains authoritative, but losing
            # cleanup evidence can hide an orphan process that still consumes
            # the user's GPU/RAM and port budget.
            logger.debug(
                '[LocalServe] failed to terminate unsuccessful attempt: %s',
                exc,
                exc_info=True,
            )
        next_step = None
        for j in range(idx, len(degrade)):
            if degrade[j].get('terminal'):
                break
            next_step = (j, degrade[j])
            break
        if oom and next_step is not None:
            j, step = next_step
            idx = j + 1
            attempt_argv, attempt_env = _apply_degrade(attempt_argv,
                                                       attempt_env, step)
            _log('检测到显存不足，执行降级: %s' % step.get('note', ''))
            store.update_fields(instance_id, degrade_index=idx)
            continue
        terminal_note = next(
            (s.get('note') for s in degrade if s.get('terminal')), '')
        error = '%s%s%s' % (
            reason, '；日志显示显存不足' if oom else '',
            ('。%s' % terminal_note) if terminal_note and oom else '')
        store.update_fields(instance_id, status='failed', pid=None,
                            last_error=error)
        _log('启动失败: %s' % error)
        return {'ok': False, **store.get_instance(instance_id)}


def _terminate(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    deadline = time.monotonic() + _STOP_GRACE_SEC
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def stop_instance(instance_id: str, *, sleep=time.sleep) -> dict:
    record = store.get_instance(instance_id)
    if record is None:
        return {'ok': False, 'error': '未知实例: %s' % instance_id}
    pid = record.get('pid')
    stopped = False
    if _pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            deadline = time.monotonic() + _STOP_GRACE_SEC
            while time.monotonic() < deadline and _pid_alive(pid):
                sleep(0.2)
            if _pid_alive(pid):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            stopped = True
        except (OSError, ProcessLookupError) as e:
            logger.debug('[LocalServe] stop %s: %s', instance_id, e)
    store.update_fields(instance_id, status='stopped', pid=None)
    return {'ok': True, 'stopped': stopped,
            **(store.get_instance(instance_id) or {})}


def status_instance(instance_id: str, *, http_get=None) -> dict:
    record = store.get_instance(instance_id)
    if record is None:
        return {'ok': False, 'error': '未知实例: %s' % instance_id}
    http_get = http_get or _http_get
    pid = record.get('pid')
    alive = _pid_alive(pid)
    serving = False
    if alive and record.get('status') == 'running':
        serving = _served_listed(record, http_get)
    effective = record.get('status')
    if effective == 'running' and not alive:
        # Tofu itself restarted (or the server crashed) while the ledger was
        # unattended — reconcile the durable row with reality.
        effective = 'stopped'
        store.update_fields(instance_id, status='stopped', pid=None)
    return {'ok': True, **(store.get_instance(instance_id) or {}),
            'pid_alive': alive, 'serving': serving,
            'log_tail': log_tail(instance_id)}

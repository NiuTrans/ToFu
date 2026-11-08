"""Local kernel: the complete Tofu server for the app that launches it.

The Android app (docs/ANDROID_LOCAL_KERNEL.md) starts the normal server here
instead of through ``python server.py``. The launch policy is fixed, not
configurable:

* Listen on 127.0.0.1 only. ``port=0`` asks the OS for a free port. The port
  is bound, and therefore known to the lifespan owners, before the lifespan
  starts. The launching process is told only once the listener accepts
  connections: ``start()`` returns the port, and the CLI prints one JSON line.
* Run in auth mode ``private`` (the existing ``TOFU_AUTH_MODE`` setting).
  Every app on the device can reach loopback, so every request must pass
  ``routes/api_v1/auth.py`` with ``Authorization: Bearer <secret>``,
  ``x-api-key: <secret>`` or the ``tofu_session=<secret>`` cookie, and must
  name this kernel's own authority (``Host: 127.0.0.1:<port>``).
* The launcher supplies a per-install secret: ``tofu_admin_`` followed by 64
  lowercase hex digits (256 random bits). The first boot registers it as the
  personal owner's admin key, and every later boot must present the same
  secret.
* Code updates and restarts belong to the launcher. Self-update and in-place
  restart are refused, and with a loopback bind the LAN discovery responder
  stays off.

Python API (one kernel per process lifetime)::

    port = local_kernel.start(data_dir, secret, port=0)
    local_kernel.stop()

CLI. The secret is the first line on stdin, never argv or the environment::

    python local_kernel.py --data-dir DIR [--port N]
    stdout: {"event": "listening", "host": "127.0.0.1", "port": N, "pid": P}

Configuration goes into ``os.environ`` before the first ``lib`` import,
because importing ``lib`` fixes the data root (``lib/__init__.py``).
"""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import Future
import json
import logging
import os
import signal
import sys
import threading

LOOPBACK_HOST = '127.0.0.1'

_kernel_lock = threading.Lock()
_kernel = None


class _Kernel:
    """The one kernel of this process: its thread, stop event and port."""

    def __init__(self) -> None:
        self.stop_requested = threading.Event()
        self.listening: Future = Future()
        self.thread: threading.Thread | None = None


def bind_loopback(port: int):
    """Bind the loopback listener now; return ``(config, sockets, bound_port)``.

    ``port=0`` resolves to the port the OS chose. The sockets accept no
    connection until ``lib.hypercorn_runtime.serve`` listens on them.
    """
    if not 0 <= int(port) <= 65535:
        raise ValueError(f'port must be an integer from 0 to 65535 (got {port!r})')
    from lib.hypercorn_runtime import build_hypercorn_config
    from lib.server.server_shutdown import http_keep_alive_timeout_seconds

    config = build_hypercorn_config(
        LOOPBACK_HOST, int(port),
        keep_alive_timeout=http_keep_alive_timeout_seconds(),
    )
    sockets = config.create_sockets()
    (listener,) = (*sockets.secure_sockets, *sockets.insecure_sockets)
    return config, sockets, listener.getsockname()[1]


def listening_record(port: int) -> str:
    """The single JSON line that tells a launching process where to connect."""
    return json.dumps({
        'event': 'listening',
        'host': LOOPBACK_HOST,
        'port': int(port),
        'pid': os.getpid(),
    })


def _configure_process(data_dir: str, secret: str) -> None:
    """Apply the launch policy before ``lib`` is imported; validate the secret."""
    data_dir = os.path.abspath(data_dir)
    if 'lib' in sys.modules and os.environ.get('TOFU_DATA_DIR') != data_dir:
        raise RuntimeError(
            'lib was imported before the local kernel was configured, so its '
            'data root is already fixed elsewhere')
    os.environ['TOFU_DATA_DIR'] = data_dir
    os.environ['TOFU_AUTH_MODE'] = 'private'
    from runtime_guards import install_runtime_resource_defaults

    install_runtime_resource_defaults(os.environ)
    from lib.api_keys import require_personal_key_secret
    from lib.auth_mode import MODE_PRIVATE, get_state

    require_personal_key_secret(secret)
    if get_state(refresh=True).mode != MODE_PRIVATE:
        raise RuntimeError('the local kernel could not enter private auth mode')


async def _serve(secret: str, port: int, *, shutdown_requested, on_listening) -> None:
    """The one serve path of the CLI and the in-process API."""
    import server

    config, sockets, bound_port = bind_loopback(port)
    # Published before the lifespan: port-dependent owners see the bound port,
    # and the LAN discovery responder stays off for a loopback-only bind.
    os.environ['_TOFU_RUNTIME_HOST'] = LOOPBACK_HOST
    os.environ['_TOFU_RUNTIME_PORT'] = str(bound_port)
    server.app.extensions['tofu_local_kernel'] = {
        'host': f'{LOOPBACK_HOST}:{bound_port}'}
    await server.serve_until_shutdown(
        server.app, config,
        host=LOOPBACK_HOST,
        port=bound_port,
        shutdown_requested=shutdown_requested,
        sockets=sockets,
        on_listening=lambda _sockets: on_listening(bound_port),
        personal_key=secret,
    )


def _run(kernel: _Kernel, secret: str, port: int) -> None:
    def listening(bound_port: int) -> None:
        kernel.listening.set_result(bound_port)

    try:
        import server  # noqa: F401 -- assembles the app before the loop starts
        asyncio.run(_serve(secret, port, shutdown_requested=kernel.stop_requested,
                           on_listening=listening))
    except BaseException as error:
        if not kernel.listening.done():
            kernel.listening.set_exception(error)
        raise


def start(data_dir: str, secret: str, port: int = 0) -> int:
    """Start the kernel on its own thread and event loop; return the bound port.

    Blocks until the listener accepts connections, or raises the startup
    error. Readiness follows: ``GET /api/v4/meta`` answers 200 once the boot
    completes. The app and its lifespan owners are process singletons, so a
    process runs at most one kernel; a second ``start`` raises RuntimeError.
    """
    global _kernel
    with _kernel_lock:
        if _kernel is not None:
            raise RuntimeError(
                'a local kernel already ran in this process; start a new process')
        _configure_process(data_dir, secret)
        kernel = _Kernel()
        kernel.thread = threading.Thread(
            target=_run, args=(kernel, secret, port), name='tofu-local-kernel',
            daemon=True)
        _kernel = kernel
    kernel.thread.start()
    return kernel.listening.result()


def stop(timeout: float | None = None) -> bool:
    """Drain and stop the kernel; True once its thread has exited.

    The host owns this process, so no hard-deadline ``os._exit`` backstop is
    armed (the CLI arms it). Returns False if the drain outlives ``timeout``.
    """
    kernel = _kernel
    if kernel is None or kernel.thread is None:
        return True
    from lib.server.server_shutdown import request_graceful_shutdown

    request_graceful_shutdown(
        kernel.stop_requested, timer_starter=lambda *_args, **_kwargs: None)
    kernel.thread.join(timeout)
    return not kernel.thread.is_alive()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python local_kernel.py',
        description='Serve the complete Tofu server on 127.0.0.1 for the '
                    'launching app. The install secret is read from the first '
                    'line of stdin.')
    parser.add_argument('--data-dir', required=True,
                        help='private Tofu data directory (TOFU_DATA_DIR)')
    parser.add_argument('--port', type=int, default=0,
                        help='TCP port on 127.0.0.1; 0 (default) lets the OS choose')
    args = parser.parse_args(argv)
    secret = sys.stdin.readline().strip()

    # stdout carries exactly one line, the listening record. Everything else
    # this process or its children print goes to stderr.
    sys.stdout.flush()
    channel = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8')
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        _configure_process(args.data_dir, secret)
    except ValueError as error:
        parser.error(str(error))
    # The server's single-instance lock, as `python server.py` and asgi.py
    # take it: a second server on this data directory exits here instead of
    # failing deep in boot on the storage lease. Held while this kernel serves.
    from lib.runtime.runtime_paths import data_root
    from lib.server_boot.lock import _acquire_instance_lock

    os.makedirs(data_root(), exist_ok=True)
    held, _instance_lock = _acquire_instance_lock(
        os.path.join(data_root(), '.server.lock'), logging.getLogger('server'))
    if not held:
        parser.exit(2, f'{parser.prog}: error: another Tofu server already serves '
                       f'this data directory ({data_root()})\n')

    def report(bound_port: int) -> None:
        channel.write(listening_record(bound_port) + '\n')
        channel.flush()

    import server  # noqa: F401 -- assembles the app before the loop starts
    from lib.server.server_shutdown import (
        graceful_shutdown_signals, request_graceful_shutdown)

    stop_requested = threading.Event()
    for signum in graceful_shutdown_signals():
        signal.signal(signum, lambda _signum, _frame: request_graceful_shutdown(
            stop_requested))
    asyncio.run(_serve(secret, args.port, shutdown_requested=stop_requested,
                       on_listening=report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

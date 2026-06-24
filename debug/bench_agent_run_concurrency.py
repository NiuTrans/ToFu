"""debug/bench_agent_run_concurrency.py — admission + pool saturation harness.

Measures the headless agent-API concurrency behaviour so the default caps
(``TOFU_MAX_INFLIGHT_TASKS``, ``TOFU_AGENT_WORKERS``, ``TOFU_SYNC_WORKERS``)
can be tuned from DATA instead of guessed. See the 2026-06 concurrency fix
(``lib/agent_core/admission.py``).

What it exercises
-----------------
The REAL request path for ``POST /api/v1/agent/run``:
  inbound request → admission gate (``controller.try_acquire``) → ``spawn_task``
  → dedicated agent ThreadPoolExecutor → event-driven ``await_terminal``.

Only the LLM call is faked: ``lib.tasks_pkg.orchestrator.run_task`` is
monkey-patched to a "sleeper" that holds the worker for ``--work`` seconds
(simulating model+tool latency) then finishes the task via ``append_event``
exactly like the orchestrator does. Everything else — the async handler,
the semaphore, the executors, the waiter wakeup — is the production code.

Two modes
---------
* **in-process (default)** — builds a minimal Quart app with the agent_run
  blueprint and drives it with ``app.test_client()`` concurrently. Lets us
  sweep the admission cap in one run and read ``controller.stats()`` directly
  for peak-in-flight. Replicates server.py's executor setup so the pool
  interplay is representative.
* **HTTP (``--url``)** — fires N concurrent real HTTP requests at a running
  server (uses its real caps + real models unless you point it at a stub).

Usage
-----
    # In-process, sweep several caps with 50 concurrent 2s "turns":
    python3 debug/bench_agent_run_concurrency.py --n 50 --work 2 \
        --caps 8,16,32,64

    # Hit a live server over HTTP:
    python3 debug/bench_agent_run_concurrency.py --url http://127.0.0.1:15000 \
        --token tofu_live_xxx --n 100 --work 0 --model my-stub-model

Read the output table: the ``503`` column should be ~``max(0, n-cap)`` and
``peak_inflight`` should track ``min(n, cap)``. Pick the cap where p95
latency stays flat (workers not queueing) and memory is acceptable.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Project root on sys.path so `python3 debug/bench_*.py` can import lib.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Env defaults MUST be set before importing app code (the admission
#    controller reads TOFU_MAX_INFLIGHT_TASKS at construction; the
#    ephemeral pre-flight probe would otherwise make real network calls). ──
os.environ.setdefault('TUNNEL_TOKEN', 'bench-no-real')
os.environ.setdefault('TOFU_EPHEMERAL_PREFLIGHT', '0')

from lib.log import get_logger  # noqa: E402

logger = get_logger(__name__)


# ── Result helpers ──────────────────────────────────────────────────


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _summarise(label: str, n: int, work_s: float, cap, results: list,
               peak_inflight: int, wall_s: float) -> dict:
    """results: list of (status_code, latency_s)."""
    ok = [lat for code, lat in results if code == 200]
    refused = sum(1 for code, _ in results if code == 503)
    other = sum(1 for code, _ in results if code not in (200, 503))
    return {
        'cap': cap, 'n': n, 'work_s': work_s,
        '200': len(ok), '503': refused, 'other': other,
        'p50_ms': round(_pct(ok, 50) * 1000, 1),
        'p95_ms': round(_pct(ok, 95) * 1000, 1),
        'max_ms': round((max(ok) if ok else 0) * 1000, 1),
        'peak_inflight': peak_inflight,
        'throughput_rps': round(len(ok) / wall_s, 1) if wall_s > 0 else 0.0,
        'wall_s': round(wall_s, 2),
    }


def _print_table(rows: list[dict]) -> None:
    cols = ['cap', 'n', 'work_s', '200', '503', 'other',
            'peak_inflight', 'p50_ms', 'p95_ms', 'max_ms',
            'throughput_rps', 'wall_s']
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    header = '  '.join(c.rjust(widths[c]) for c in cols)
    print(header)
    print('  '.join('-' * widths[c] for c in cols))
    for r in rows:
        print('  '.join(str(r[c]).rjust(widths[c]) for c in cols))


# ── In-process mode ─────────────────────────────────────────────────


def _install_flask_shim():
    """Make ``from flask import X`` resolve to Quart (mirrors server.py)."""
    import inspect

    import quart
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        qs = f'quart.{attr}'
        if qs in sys.modules:
            sys.modules[f'flask.{attr}'] = sys.modules[qs]
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    from quart.wrappers import Request as _QR
    if inspect.iscoroutinefunction(_QR.get_json):
        _orig = _QR.get_json

        def _sync_get_json(self, *a, **kw):
            import asyncio as _a
            return _a.run(_orig(self, *a, **kw))
        # Async handlers (async_parse_body) recover + await this original
        # instead of hitting the sync asyncio.run shim on the loop.
        _sync_get_json._genuine_async_get_json = _orig
        _QR.get_json = _sync_get_json


def _make_sleeper_run_task(work_s: float):
    """Return a run_task replacement that holds the worker for work_s then
    finishes the task exactly like the orchestrator (status + done event)."""
    from lib.tasks_pkg.manager import append_event

    def _run_task(task):
        try:
            if work_s > 0:
                time.sleep(work_s)
            task['content'] = 'bench-ok'
            task['status'] = 'done'
            task['finishReason'] = 'stop'
            task['usage'] = {'input_tokens': 1, 'output_tokens': 1,
                             'total_tokens': 2}
            append_event(task, {'type': 'done', 'finishReason': 'stop',
                                'usage': task['usage']})
        except Exception as e:  # pragma: no cover — surfaces stub bugs
            logger.error('[bench] sleeper run_task failed: %s', e, exc_info=True)
            task['status'] = 'error'
            task['error'] = {'detail': str(e)}
            append_event(task, {'type': 'done', 'finishReason': 'error'})

    return _run_task


def _run_in_process(args) -> list[dict]:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    _install_flask_shim()

    from quart import Quart

    # Replicate server.py executor setup so pool interplay is faithful.
    try:
        sync_workers = int(os.environ.get('TOFU_SYNC_WORKERS', '') or '0')
    except (ValueError, TypeError):
        sync_workers = 0
    if sync_workers <= 0:
        sync_workers = min(128, (os.cpu_count() or 4) * 8)
    try:
        agent_workers = int(os.environ.get('TOFU_AGENT_WORKERS', '') or '0')
    except (ValueError, TypeError):
        agent_workers = 0
    if agent_workers <= 0:
        agent_workers = min(256, (os.cpu_count() or 4) * 16)

    app = Quart(__name__)
    app.config['TESTING'] = True

    from routes.api_v1.auth import (
        attach_rate_headers, bearer_auth_before_request,
    )
    app.before_request(bearer_auth_before_request)
    app.after_request(attach_rate_headers)

    import routes.api_v1.agent_run as ar
    app.register_blueprint(ar.api_v1_agent_run_bp)

    from lib.api_keys import create_key
    _row, token = create_key(name='bench', scopes=['agents:run'])

    # Patch the LLM call to a sleeper (real spawn_task / executor path stays).
    import lib.tasks_pkg as pkg
    import lib.tasks_pkg.orchestrator as orch
    from lib.agent_core.admission import AdmissionController
    orch.run_task = _make_sleeper_run_task(args.work)

    print(f'[bench] in-process: sync_workers={sync_workers} '
          f'agent_workers={agent_workers} n={args.n} work={args.work}s '
          f'caps={args.caps}')

    rows: list[dict] = []

    async def _drive_one_cap(cap: int) -> dict:
        # Install the dedicated agent executor + sized default executor on
        # THIS loop (mirrors server.py::_serve), fresh per cap so a prior
        # run's queue can't bleed into the next.
        loop = asyncio.get_running_loop()
        default_exec = ThreadPoolExecutor(max_workers=sync_workers,
                                          thread_name_prefix='tofu-sync')
        loop.set_default_executor(default_exec)
        agent_exec = ThreadPoolExecutor(max_workers=agent_workers,
                                        thread_name_prefix='tofu-agent')
        pkg.set_agent_executor(agent_exec)

        # Swap in a controller with the cap under test (agent_run imported
        # `controller` by name, so reassign the module binding).
        ar.controller = AdmissionController(max_inflight=cap)

        peak = {'v': 0}
        stop = asyncio.Event()

        async def _sampler():
            while not stop.is_set():
                peak['v'] = max(peak['v'], ar.controller.in_flight)
                await asyncio.sleep(0.005)

        sampler = asyncio.ensure_future(_sampler())
        cli = app.test_client()

        async def _one():
            t0 = time.time()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {token}'},
                json={'model': 'bench-model',
                      'messages': [{'role': 'user', 'content': 'hi'}],
                      'timeout_s': args.timeout})
            return r.status_code, time.time() - t0

        wall0 = time.time()
        results = await asyncio.gather(*[_one() for _ in range(args.n)])
        wall = time.time() - wall0
        stop.set()
        await sampler

        agent_exec.shutdown(wait=True)
        default_exec.shutdown(wait=False)
        return _summarise('in-process', args.n, args.work, cap,
                          list(results), peak['v'], wall)

    async def _go():
        for cap in args.caps:
            rows.append(await _drive_one_cap(cap))

    asyncio.new_event_loop().run_until_complete(_go())
    return rows


# ── HTTP mode ───────────────────────────────────────────────────────


def _run_http(args) -> list[dict]:
    import json
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    url = args.url.rstrip('/') + '/api/v1/agent/run'
    body = json.dumps({
        'model': args.model,
        'messages': [{'role': 'user', 'content': 'hi'}],
        'timeout_s': args.timeout,
    }).encode()
    headers = {'Content-Type': 'application/json'}
    if args.token:
        headers['Authorization'] = f'Bearer {args.token}'

    print(f'[bench] HTTP: url={url} n={args.n} model={args.model} '
          f'(server uses its OWN caps)')

    def _one(_i) -> tuple:
        t0 = time.time()
        req = Request(url, data=body, headers=headers, method='POST')
        try:
            with urlopen(req, timeout=args.timeout + 30) as resp:
                resp.read()
                return resp.status, time.time() - t0
        except HTTPError as e:
            return e.code, time.time() - t0
        except URLError as e:
            logger.warning('[bench] request failed: %s', e)
            return 0, time.time() - t0

    wall0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=args.n) as ex:
        futs = [ex.submit(_one, i) for i in range(args.n)]
        for f in as_completed(futs):
            results.append(f.result())
    wall = time.time() - wall0
    return [_summarise('http', args.n, args.work, 'server', results, -1, wall)]


# ── CLI ─────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--n', type=int, default=50,
                    help='Concurrent requests to fire (default 50).')
    ap.add_argument('--work', type=float, default=1.0,
                    help='Simulated per-turn latency in seconds for the stub '
                         'run_task (in-process mode only; default 1.0).')
    ap.add_argument('--caps', type=str, default='8,16,32,64',
                    help='Comma-separated admission caps to sweep '
                         '(in-process only; default 8,16,32,64).')
    ap.add_argument('--timeout', type=float, default=600,
                    help='Per-request timeout_s sent in the body (default 600).')
    ap.add_argument('--url', type=str, default='',
                    help='Hit a LIVE server over HTTP at this base URL '
                         'instead of in-process. Sweeping caps is N/A '
                         '(server uses its own).')
    ap.add_argument('--token', type=str, default=os.environ.get('TOFU_API_KEY', ''),
                    help='Bearer token for HTTP mode (or $TOFU_API_KEY).')
    ap.add_argument('--model', type=str, default='bench-model',
                    help='Model name for HTTP mode (default bench-model).')
    args = ap.parse_args()

    args.caps = [int(c) for c in str(args.caps).split(',') if c.strip()]

    if args.url:
        rows = _run_http(args)
    else:
        rows = _run_in_process(args)

    print()
    _print_table(rows)
    print('\n[bench] Interpretation:')
    print('  * 503 count ≈ max(0, n - cap)  → admission gate working.')
    print('  * peak_inflight ≈ min(n, cap)  → semaphore bounding correctly.')
    print('  * pick the cap where p95_ms stops tracking work_s*ceil(n/workers)')
    print('    (i.e. workers no longer queueing) and memory is acceptable.')


if __name__ == '__main__':
    main()

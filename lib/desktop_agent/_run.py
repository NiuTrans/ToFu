"""
Desktop Agent — polling loop (``run_agent``) and CLI entry point.

``run_agent`` polls the Tofu server for queued commands, executes them
locally via ``dispatch_command`` and posts back results.  ``main`` parses
CLI args and is invoked from the package ``__main__`` guard — importing
this module (or the package) never triggers the CLI.
"""

import argparse
import itertools
import json
import os
import socket
import sys
import threading
import time
import uuid

import requests

from lib.desktop_agent._dispatch import COMMANDS, dispatch_command
from lib.desktop_wire import limit, protocol_version

from lib.desktop_agent._runtime import ProtocolMismatch, RuntimeState, retry_delay, validate_poll_response
from lib.desktop_agent.config import load_config, update_config
from lib.log import get_logger

logger = get_logger(__name__)


def _start_project_run_streamed(cmd_id, cmd_params, permissions,
                                result_queue, stream_outbox, io_lock):
    """RWA P2: run project_run_command OFF the poll loop, streaming frames.

    Chunks land in ``stream_outbox`` as ``{cmd_id, seq, stream, data, done}``
    (seq dense + unique per command, done frame last) and the final capped
    outcome lands in ``result_queue`` — both are uploaded by the next poll.
    """
    if not permissions.get('allow_exec'):
        with io_lock:
            result_queue.append({
                'id': cmd_id, 'result': None,
                'error': 'Command project_run_command requires --allow-exec flag',
            })
        return
    from lib.desktop_agent._project import start_project_run
    seq = itertools.count(1)

    def on_chunk(stream, data):
        with io_lock:
            stream_outbox.append({'cmd_id': cmd_id, 'seq': next(seq),
                                  'stream': stream, 'data': data,
                                  'done': False})

    def on_exit(outcome):
        try:
            stopped = getattr(result_queue, 'stop', None)
            if stopped is None or not stopped.is_set():
                with io_lock:
                    stream_outbox.append({'cmd_id': cmd_id, 'seq': next(seq),
                                          'stream': 'meta', 'data': '', 'done': True})
        finally:
            result_queue.append({'id': cmd_id, 'result': outcome, 'error': None})
        logger.info('     ✅ project_run_command done (exit=%s%s)',
                    outcome.get('exit_code'),
                    ', timed out' if outcome.get('timed_out') else '')

    err = start_project_run(cmd_id, cmd_params, on_chunk, on_exit,
                            cancel=getattr(result_queue, 'stop', None))
    if err:
        logger.warning('     ❌ project_run_command refused: %s', err)
        with io_lock:
            result_queue.append({'id': cmd_id, 'result': None, 'error': err})


def _start_egress_stream_streamed(cmd_id, cmd_params, permissions,
                                    result_queue, stream_outbox, io_lock):
    """S3: run egress_http_stream OFF the poll loop, streaming frames.

    Mirrors _start_project_run_streamed: the stream executor emits
    {cmd_id, seq, stream, data, done} frames into stream_outbox (meta →
    body → done), and the final stats land in result_queue — while the
    poll loop keeps heartbeating (a 30-min LLM stream must not make the
    server declare this agent dead).
    """
    if not permissions.get('allow_egress'):
        with io_lock:
            result_queue.append({
                'id': cmd_id, 'result': None,
                'error': 'Command egress_http_stream requires --allow-egress flag',
            })
        return
    from lib.desktop_agent._egress import start_egress_stream
    seq = itertools.count(1)

    def on_chunk(frame_seq, stream, data):
        with io_lock:
            stream_outbox.append({'cmd_id': cmd_id, 'seq': next(seq),
                                  'stream': stream, 'data': data,
                                  'done': False})

    def on_exit(outcome):
        with io_lock:
            stream_outbox.append({'cmd_id': cmd_id, 'seq': next(seq),
                                  'stream': 'meta', 'data': '', 'done': True})
            result_queue.append({'id': cmd_id, 'result': outcome,
                                 'error': outcome.get('error')
                                 if isinstance(outcome, dict) else None})
        logger.info('     ✅ egress_http_stream done (status=%s)',
                    outcome.get('status') if isinstance(outcome, dict) else '?')

    err = None
    try:
        start_egress_stream(cmd_params, on_chunk, on_exit)
    except Exception as e:
        logger.warning('[Agent] egress stream failed: %s', e)
        err = f'{type(e).__name__}: {e}'
    if err:
        logger.warning('     ❌ egress_http_stream failed to start: %s', err)
        with io_lock:
            result_queue.append({'id': cmd_id, 'result': None, 'error': err})


def _ensure_agent_id():
    """Return this machine's stable agent_id, generating + persisting it
    on first run.

    The id lives in the agent config file (TOFU_DESKTOP_CONFIG or
    ~/.tofu/desktop_agent.json) so restarts keep the same identity — the
    server-side registry and command addressing key on it (RWA P0).
    """
    candidate = uuid.uuid4().hex
    outcome = {'agent_id': candidate}

    def _mutate(cfg):
        existing = (cfg.get('agent_id') or '').strip()
        if existing:
            outcome['agent_id'] = existing
            return None
        cfg['agent_id'] = candidate
        return cfg

    try:
        update_config(_mutate)
    except Exception as e:
        logger.warning('[Agent] could not persist agent_id (a new one will '
                       'be generated on next start): %s', e)
    return outcome['agent_id']


def _agent_version() -> str:
    """The agent's own version string ('' when unreadable).

    Carried in every poll's registration frame so the server can see
    agent↔server drift (the command protocol evolves WITH the server —
    a release-line agent against a HEAD server can silently
    mis-dispatch). Never raises: a version read failure must not stop
    the agent from polling.
    """
    try:
        from lib.version import __version__ as v
        return (v or '').strip()
    except Exception as e:
        logger.debug('[Agent] version unreadable: %s', e)
        return ''


def _build_agent_frame(agent_id, permissions, share_roots=None):
    """Build the v2 registration frame sent with every poll."""
    return {
        'agent_id': agent_id,
        'protocol_version': protocol_version(),
        'name': socket.gethostname(),
        'platform': sys.platform,
        'version': _agent_version(),
        'capabilities': {
            'write': bool(permissions.get('allow_write')),
            'exec': bool(permissions.get('allow_exec')),
            'gui': bool(permissions.get('allow_gui')),
            'notification': bool(permissions.get('allow_notification')),
            'egress': bool(permissions.get('allow_egress')),
            'codex_login': 1 if permissions.get('allow_codex_login') else 0,
            'paper_build': 1 if permissions.get('allow_exec') else 0,
        },
        'share_roots': list(share_roots or []),
    }


# ══════════════════════════════════════════════════════════
#  Polling Loop (runs on your local machine)
# ══════════════════════════════════════════════════════════

# Route self-repair tuning (owner incident 2026-08-06): a saved route can
# die under the agent (proxy URL, a tunnel child reaped at exit, a server
# that moved). After this many CONSECUTIVE route-dead polls the loop calls
# the route_repair callback (the launcher wires resume_attachment into it),
# which re-walks the persisted attach candidates + the discovery ladder;
# the cooldown bounds how often that (potentially ~30s) walk may run.
_ROUTE_REPAIR_THRESHOLD = 6
_ROUTE_REPAIR_COOLDOWN_S = 300.0


def run_agent(server_url, permissions, poll_interval=1.0, bridge_secret='',
              stop_event=None, on_status=None, route_repair=None,
              browser_relay=None):
    """Main agent loop — polls server for commands, executes locally, returns results.

    Args:
        server_url: Tofu server base URL.
        permissions: dict with allow_write / allow_exec / allow_gui flags.
        poll_interval: seconds between polls.
        bridge_secret: owner-scoped ``agents:bridge`` credential. A standalone
            agent always requires one; the packaged in-process agent receives
            its process-memory capability from the launcher.
        stop_event: optional ``threading.Event``; when set, the loop exits
            cleanly at the next poll boundary. Lets the desktop tray toggle the
            agent off without killing the process.
        on_status: optional callable receiving a small dict on every LINK
            transition — ``{'state': 'ok'}`` / ``'auth'`` (Tofu refused the
            secret) / ``'proxy'`` (a gateway, NOT Tofu, answered — the URL is
            wrong) / ``'http'`` (+code) / ``'unreachable'`` / ``'error'``.
            Fires ONLY on transitions, never per-poll, so a tray menu can
            refresh from it without a 1 Hz storm. The desktop tray renders
            this as its link line: a silently-failing poll was exactly how a
            proxy-URL attachment hid for hours (owner incident 2026-08-03).
        route_repair: optional zero-arg callable invoked after
            ``_ROUTE_REPAIR_THRESHOLD`` consecutive route-dead polls
            (``proxy`` / ``unreachable`` — 'auth' and 'ok' mean the address
            REACHED Tofu and reset the streak). It must return ``(url,
            secret)`` for a live replacement route, or None. A changed URL
            rebinds this loop's endpoint + credential IN PLACE — the link
            line's「正在自动重找通路」promise is kept by this hook, not by
            hoping the dead address recovers (2026-08-06: without it a
            gateway-intercepted attachment polled the same wall forever
            while the tray text claimed re-discovery was running).
        browser_relay: optional ``BrowserRelay`` started by the packaged
            launcher.  When an authenticated Tofu page has discovered the
            loopback broker, desktop polls ride that page's gateway cookies.
            This is the viable transport for web-only Codelab / VS Code
            ``/proxy/<port>`` deployments where the standalone process is
            rejected by the SSO edge before it can reach Tofu.
    """

    endpoint = f'{server_url.rstrip("/")}/api/desktop/poll'
    stop_event = stop_event if stop_event is not None else threading.Event()
    runtime = RuntimeState(stop_event)
    result_queue, stream_outbox, io_lock = runtime.results, runtime.streams, runtime.lock
    paper_worker = None
    last_contact = [time.monotonic()]

    def paper_build_worker():
        nonlocal paper_worker
        if paper_worker is None:
            from lib.desktop_agent._paper import PaperBuildWorker
            from lib.paper.builds.contract import limit
            paper_worker = PaperBuildWorker(stop_event,
                lambda: time.monotonic() - last_contact[0] <= limit('desktop_connection_seconds'))
        return paper_worker

    class CompletionQueue:
        stop = stop_event
        def append(self, result):
            try:
                if not stop_event.is_set():
                    result_queue.append(result)
            finally:
                runtime.finish(result['id'])

    def execute(cmd):
        cmd_id, cmd_type = cmd['id'], cmd['type']
        params = cmd.get('params', {})
        completion = CompletionQueue()
        try:
            if cmd_type == 'paper_build':
                if not permissions.get('allow_exec'):
                    completion.append({'id': cmd_id, 'result': None,
                                       'error': 'Command paper_build requires --allow-exec flag'})
                    return
                sequence = itertools.count(1)
                def emit(stream, data, cancel):
                    stream_outbox.append({'cmd_id': cmd_id, 'seq': next(sequence),
                                          'stream': stream, 'data': data,
                                          'done': stream == 'paper-end'}, cancel=cancel)
                result = paper_build_worker().execute(cmd_id, params, emit)
                completion.append({'id': cmd_id, 'result': result, 'error': None})
                return
            if cmd_type == 'project_run_command':
                _start_project_run_streamed(cmd_id, params, permissions,
                                            completion, stream_outbox, io_lock)
                return
            if cmd_type == 'egress_http_stream':
                _start_egress_stream_streamed(cmd_id, params, permissions,
                                             completion, stream_outbox, io_lock)
                return
            result = dispatch_command(cmd_type, params, permissions)
            encoded = json.dumps(result, ensure_ascii=False, default=str)
            if len(encoded.encode('utf-8')) > 500_000:
                result = {'error': 'Result too large for transport', 'partial': encoded[:100_000]}
            completion.append({'id': cmd_id, 'result': result,
                               'error': result.get('error') if isinstance(result, dict) else None})
        except Exception as exc:
            completion.append({'id': cmd_id, 'result': None,
                               'error': f'{type(exc).__name__}: {str(exc)[:1000]}'})
    headers = {}
    if bridge_secret:
        headers['X-Bridge-Secret'] = bridge_secret

    _last_status = {'state': None, 'transport': None}

    def _emit(state, **extra):
        transport = extra.get('transport')
        if (state == _last_status['state'] and
                transport == _last_status['transport']):
            return
        _last_status['state'] = state
        _last_status['transport'] = transport
        if on_status is None:
            return
        try:
            on_status(dict({'state': state}, **extra))
        except Exception as e:
            logger.debug('[Agent] on_status callback failed: %s', e)

    # ── Route self-repair ──
    # 'proxy'/'unreachable' mean the ADDRESS is dead; 'ok'/'auth' mean it
    # reached Tofu (a wrong credential never re-points the route). The
    # streak gates the walk so a brief blip never triggers it.
    route = {'url': server_url.rstrip('/'), 'secret': bridge_secret}
    route_dead = {'streak': 0, 'last_walk': None}

    def _route_alive():
        route_dead['streak'] = 0

    def execute_owned(cmd):
        try:
            execute(cmd)
        except BaseException:
            runtime.finish(cmd['id'])
            raise
        finally:
            # Project execution delegates to an asynchronous process callback.
            if cmd['type'] != 'project_run_command':
                runtime.finish(cmd['id'])

    def _route_dead_hit():
        """One more poll against a dead route; maybe run the repair walk."""
        if route_repair is None:
            return
        route_dead['streak'] += 1
        if route_dead['streak'] < _ROUTE_REPAIR_THRESHOLD:
            return
        now = time.monotonic()
        if route_dead['last_walk'] is not None and now - route_dead['last_walk'] < _ROUTE_REPAIR_COOLDOWN_S:
            return
        route_dead['last_walk'] = now
        nonlocal endpoint, headers
        try:
            repaired = route_repair()
        except Exception as e:
            logger.warning('[Agent] route repair walk failed: %s', e)
            return
        if not repaired:
            return
        new_url, new_secret = repaired
        new_url = str(new_url or '').strip().rstrip('/')
        if not new_url or (new_url, str(new_secret or '').strip()) == (route['url'], route['secret']):
            return
        route['url'] = new_url
        route['secret'] = str(new_secret or '').strip()
        endpoint = '%s/api/desktop/poll' % new_url
        headers = ({'X-Bridge-Secret': route['secret']}
                   if route['secret'] else {})
        route_dead['streak'] = 0
        logger.info('[Agent] route re-pointed by the repair walk: %s',
                    new_url)

    agent_id = _ensure_agent_id()
    agent_frame = _build_agent_frame(
        agent_id, permissions, load_config().get('share_roots'))

    if permissions.get('allow_egress'):
        # E4: a previously-ensured CLIProxyAPI sidecar resumes with the
        # agent — the subscription path must survive an agent reboot.
        from lib.desktop_agent._adapter import maybe_resume_adapter
        maybe_resume_adapter()

    logger.info('Desktop Agent starting...')
    logger.info('   Server: %s', server_url)
    logger.info('   Agent: %s (%s, %s)', agent_frame['name'],
                agent_id[:8], agent_frame['platform'])
    logger.info('   Permissions: %s', json.dumps(permissions))
    logger.info('   Device credential: %s',
                'configured' if bridge_secret else 'missing')
    available_cmds = ', '.join(sorted(COMMANDS.keys()))
    logger.info('   Available commands: %s', available_cmds)
    logger.info('   Poll interval: %ss', poll_interval)
    logger.info('   Press Ctrl+C to stop\n')

    consecutive_errors = 0
    negotiated_v3 = False

    with requests.Session() as poll_session:
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info('[Agent] stop_event set — shutting down cleanly')
                break
            try:
                # Send results + stream frames + get new commands (single endpoint)
                with io_lock:
                    out_results = result_queue.prefix(limit('result_items_floor'), limit('result_bytes_floor'))
                    agent_frame['execution_slots'] = runtime.available_slots()
                    agent_frame['active_command_ids'] = sorted(runtime.running)
                    out_streams = stream_outbox.prefix(limit('stream_items_floor'), limit('stream_bytes_floor'))
                poll_payload = {'results': out_results,
                                'streams': out_streams,
                                'agent': dict(agent_frame)}
                poll_started = time.monotonic()
                resp = None
                via_browser = False
                # A live page is strictly preferred over retrying a URL already
                # known to sit behind an SSO wall.  BrowserRelay returns a
                # requests.Response-shaped object, so everything below remains
                # the one command/result implementation.
                if browser_relay is not None:
                    try:
                        if browser_relay.browser_available():
                            resp = browser_relay.request(
                                endpoint, poll_payload, headers, timeout=20)
                            via_browser = resp is not None
                    except Exception as e:
                        logger.warning('[Agent] browser relay poll failed: %s', e)
                        resp = None
                if resp is None:
                    resp = poll_session.post(
                        endpoint,
                        json=poll_payload,
                        headers=headers,
                        timeout=15,
                        allow_redirects=False,
                        auth=lambda request: request
                    )

                if resp.status_code in (401, 403):
                    # Two utterly different refusals share this status. Tofu's
                    # own 401 (api_error envelope) means the bridge secret is
                    # wrong — a fresh connect line fixes it. A GATEWAY's 401
                    # (SSO proxy edge answering before Tofu ever sees the
                    # request) means the URL is wrong — no secret will ever
                    # pass, and the old log line sent the owner hunting the
                    # wrong half of the line (measured 2026-08-03).
                    from lib.desktop_agent._probe import is_tofu_error_envelope
                    try:
                        tofu_refusal = is_tofu_error_envelope(resp.json())
                    except ValueError as e:
                        logger.debug('[Agent] poll response not JSON: %s', e)
                        tofu_refusal = False
                    if tofu_refusal:
                        _emit('auth')
                        _route_alive()  # Tofu answered — the address is fine
                        logger.error('Server returned 401 — the device credential '
                                     'is missing, revoked, or belongs to another '
                                     'owner. Pair the device again.')
                    else:
                        _emit('proxy')
                        _route_dead_hit()
                        logger.error(
                            '401 answered by a GATEWAY, not Tofu — the server '
                            'address is intercepted (SSO proxy?). Re-check the '
                            'connect line\'s URL half (ssh tunnel: '
                            'http://127.0.0.1:<port>).')
                    consecutive_errors += 1
                    stop_event.wait(retry_delay(poll_interval, consecutive_errors))
                    continue
                if resp.status_code != 200:
                    _emit('http', code=resp.status_code)
                    logger.info('Server returned %s', resp.status_code)
                    if resp.status_code in (301, 302, 303, 307, 308, 408, 502, 503, 504):
                        _route_dead_hit()
                    consecutive_errors += 1
                    stop_event.wait(retry_delay(poll_interval, consecutive_errors))
                    continue

                try:
                    data = resp.json()
                    commands = validate_poll_response(data, agent_id=agent_id, require_v3=negotiated_v3)
                    if data.get('protocol_version') == protocol_version():
                        negotiated_v3 = True
                        runtime.observe_server_time(data['server_time'], request_elapsed=time.monotonic() - poll_started)
                except ProtocolMismatch:
                    _emit('http', code=426)
                    _route_alive()
                    consecutive_errors += 1
                    stop_event.wait(retry_delay(poll_interval, consecutive_errors))
                    continue
                except (ValueError, TypeError):
                    _emit('proxy')
                    _route_dead_hit()
                    consecutive_errors += 1
                    stop_event.wait(retry_delay(poll_interval, consecutive_errors))
                    continue

                # A request rejected by an SSO edge / server must NOT consume
                # queued command results.  The old unconditional delete happened
                # before status inspection, silently losing results on every 401.
                # A 200 is the only acknowledgement that the server accepted this
                # exact prefix.  Frames appended while in flight remain queued.
                with io_lock:
                    del result_queue[:len(out_results)]
                    del stream_outbox[:len(out_streams)]
                consecutive_errors = 0
                _emit('ok', transport='browser' if via_browser else 'direct')
                _route_alive()
                last_contact[0] = time.monotonic()


                if commands:
                    logger.info('Received %d command(s)', len(commands))

                for cmd in commands:
                    if cmd['type'] in ('egress_cancel', 'paper_build_cancel') and runtime.expired(cmd.get('expires_at')):
                        continue
                    # Cancellation is a nonblocking control-plane signal, not work.
                    if cmd['type'] == 'egress_cancel':
                        from lib.desktop_agent._egress import cancel_inflight
                        cancel_inflight(str(cmd.get('params', {}).get('cmd_id') or ''))
                        continue
                    if cmd['type'] == 'paper_build_cancel':
                        if paper_worker is not None:
                            paper_worker.cancel(str(cmd.get('params', {}).get('cmd_id') or ''))
                        continue
                    if not stop_event.is_set() and runtime.admit(cmd['id'], expires_at=cmd.get('expires_at')):
                        if cmd['type'] == 'paper_build' and permissions.get('allow_exec'):
                            if not paper_build_worker().admit(cmd['id']):
                                CompletionQueue().append({'id': cmd['id'], 'result': None,
                                                          'error': 'Paper build is stopping or already owned'})
                                runtime.finish(cmd['id'])
                                continue
                        try:
                            threading.Thread(target=execute_owned, args=(cmd,), daemon=True,
                                             name='desktop-command').start()
                        except BaseException as exc:
                            if cmd['type'] == 'paper_build' and paper_worker is not None:
                                paper_worker.release(cmd['id'])
                            runtime.finish(cmd['id'])
                            if not isinstance(exc, Exception):
                                raise
                            CompletionQueue().append({'id': cmd['id'], 'result': None,
                                                      'error': str(exc)})

            except requests.RequestException:
                consecutive_errors += 1
                _emit('unreachable')
                _route_dead_hit()
                if consecutive_errors == 1:
                    logger.info('Cannot reach server at %s, retrying...', server_url, exc_info=True)
                stop_event.wait(retry_delay(poll_interval, consecutive_errors))
                continue

            except KeyboardInterrupt:
                logger.info('\n[Agent] Shutting down...')
                break

            except Exception as e:
                _emit('error', detail=str(e)[:120])
                logger.error('Error: %s', e, exc_info=True)
                consecutive_errors += 1
                stop_event.wait(retry_delay(poll_interval, consecutive_errors))
                continue

            with io_lock:
                draining = bool(result_queue.bytes or stream_outbox.bytes)
            if not draining:
                stop_event.wait(max(0.01, poll_interval))

    stop_event.set()
    if paper_worker is not None:
        paper_worker.close()
    runtime.close()


# ══════════════════════════════════════════════════════════
#  CLI Entry Point
# ══════════════════════════════════════════════════════════

def main(argv=None):
    """Parse CLI args and start the agent loop."""
    parser = argparse.ArgumentParser(
        description='Tofu Desktop Agent — control your computer from AI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Read-only mode (safest — can browse files, take screenshots, read clipboard)
  python -m lib.desktop_agent --server http://localhost:5000

  # Allow file writes
  python -m lib.desktop_agent --server http://localhost:5000 --allow-write

  # Allow running commands + GUI automation (most powerful)
  python -m lib.desktop_agent --server http://localhost:5000 --allow-write --allow-exec --allow-gui

  # Full access
  python -m lib.desktop_agent --server http://localhost:5000 --allow-all

  # Declare project share roots for Studio remote-worktree editing (repeatable,
  # persisted to the agent config so they survive restarts)
  python -m lib.desktop_agent --server http://localhost:5000 --allow-write \
      --root myapp=~/code/myapp --root docs=~/Documents/notes
"""
    )
    parser.add_argument('--server', required=True, help='Tofu server URL')
    parser.add_argument('--allow-write', action='store_true', help='Allow file write/move operations')
    parser.add_argument('--allow-exec', action='store_true', help='Allow running commands and opening apps')
    parser.add_argument('--allow-gui', action='store_true', help='Allow GUI automation (mouse, keyboard, screenshot)')
    parser.add_argument('--allow-egress', action='store_true',
                        help='Allow relaying whitelisted subscription API requests '
                             '(anthropic.com/openai.com/chatgpt.com) through this '
                             'machine\'s network')
    parser.add_argument('--allow-codex-login', action='store_true',
                        help='Let the paired Tofu server borrow the current local Codex access token (no refresh token)')
    parser.add_argument('--allow-all', action='store_true', help='Enable all permissions')
    parser.add_argument('--poll-interval', type=float, default=1.0, help='Polling interval in seconds')
    parser.add_argument(
        '--bridge-token', required=True,
        help='Owner-scoped agents:bridge credential returned by device pairing.')
    parser.add_argument('--root', action='append', default=[], metavar='NAME=PATH',
                        help='Declare a project share root (repeatable). Merged into '
                             'the agent config and persisted — a same-named existing '
                             'root is updated. project_* commands are confined to '
                             'these roots.')

    args = parser.parse_args(argv)

    from lib.desktop_agent._permissions import build_permissions
    permissions = build_permissions(
        allow_write=args.allow_write,
        allow_exec=args.allow_exec,
        allow_gui=args.allow_gui,
        allow_egress=args.allow_egress,
        allow_codex_login=args.allow_codex_login,
        allow_all=args.allow_all,
    )

    bridge_secret = args.bridge_token.strip()

    if args.root:
        from lib.desktop_agent.config import merge_cli_roots

        def _merge_roots(cfg):
            cfg['share_roots'] = merge_cli_roots(
                cfg.get('share_roots'), args.root)
            return cfg

        try:
            cfg = update_config(_merge_roots)
        except ValueError as e:
            parser.error(str(e))
        for r in cfg['share_roots']:
            if not os.path.isdir(os.path.expanduser(str(r.get('path', '')))):
                logger.warning('[Agent] share root %r path does not exist: %s',
                               r.get('name'), r.get('path'))
        logger.info('[Agent] share roots: %s',
                    ', '.join(f"{r.get('name')}={r.get('path')}"
                              for r in cfg['share_roots']))

    run_agent(args.server, permissions, args.poll_interval, bridge_secret=bridge_secret)

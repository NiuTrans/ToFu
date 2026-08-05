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
from lib.desktop_agent.config import load_config, save_config
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
        with io_lock:
            stream_outbox.append({'cmd_id': cmd_id, 'seq': next(seq),
                                  'stream': 'meta', 'data': '', 'done': True})
            result_queue.append({'id': cmd_id, 'result': outcome,
                                 'error': None})
        logger.info('     ✅ project_run_command done (exit=%s%s)',
                    outcome.get('exit_code'),
                    ', timed out' if outcome.get('timed_out') else '')

    err = start_project_run(cmd_id, cmd_params, on_chunk, on_exit)
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
    cfg = load_config()
    agent_id = (cfg.get('agent_id') or '').strip()
    if agent_id:
        return agent_id
    agent_id = uuid.uuid4().hex
    cfg['agent_id'] = agent_id
    try:
        save_config(cfg)
    except Exception as e:
        logger.warning('[Agent] could not persist agent_id (a new one will '
                       'be generated on next start): %s', e)
    return agent_id


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
        'name': socket.gethostname(),
        'platform': sys.platform,
        'version': _agent_version(),
        'capabilities': {
            'write': bool(permissions.get('allow_write')),
            'exec': bool(permissions.get('allow_exec')),
            'gui': bool(permissions.get('allow_gui')),
            'notification': bool(permissions.get('allow_notification')),
            'egress': bool(permissions.get('allow_egress')),
        },
        'share_roots': list(share_roots or []),
    }


# ══════════════════════════════════════════════════════════
#  Polling Loop (runs on your local machine)
# ══════════════════════════════════════════════════════════

def run_agent(server_url, permissions, poll_interval=1.0, bridge_secret='',
              stop_event=None, on_status=None):
    """Main agent loop — polls server for commands, executes locally, returns results.

    Args:
        server_url: Tofu server base URL.
        permissions: dict with allow_write / allow_exec / allow_gui flags.
        poll_interval: seconds between polls.
        bridge_secret: optional X-Bridge-Secret value. Required when the
            server has TOFU_BRIDGE_SECRET configured. Pass empty string to
            disable (LAN-only deployments).
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
    """

    endpoint = f'{server_url.rstrip("/")}/api/desktop/poll'
    result_queue = []
    stream_outbox = []  # RWA P2: streamed-command frames for the next poll(s)
    io_lock = threading.Lock()  # guards both outboxes (runner threads append)
    headers = {}
    if bridge_secret:
        headers['X-Bridge-Secret'] = bridge_secret

    _last_status = {'state': None}

    def _emit(state, **extra):
        if state == _last_status['state']:
            return
        _last_status['state'] = state
        if on_status is None:
            return
        try:
            on_status(dict({'state': state}, **extra))
        except Exception as e:
            logger.debug('[Agent] on_status callback failed: %s', e)

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
    logger.info('   Bridge secret: %s', 'configured' if bridge_secret else 'none (LAN-only)')
    available_cmds = ', '.join(sorted(COMMANDS.keys()))
    logger.info('   Available commands: %s', available_cmds)
    logger.info('   Poll interval: %ss', poll_interval)
    logger.info('   Press Ctrl+C to stop\n')

    consecutive_errors = 0

    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info('[Agent] stop_event set — shutting down cleanly')
            break
        try:
            # Send results + stream frames + get new commands (single endpoint)
            with io_lock:
                out_results = list(result_queue)
                out_streams = list(stream_outbox)
            resp = requests.post(
                endpoint,
                json={'results': out_results, 'streams': out_streams,
                      'agent': agent_frame},
                headers=headers,
                timeout=15,
                proxies={'no_proxy': '*'}  # localhost — always bypass env proxy
            )
            with io_lock:
                # Prefix-delete only what was actually sent — frames appended
                # by runner threads while the POST was in flight must survive.
                del result_queue[:len(out_results)]
                del stream_outbox[:len(out_streams)]
            consecutive_errors = 0

            if resp.status_code == 401:
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
                    logger.error('Server returned 401 — bridge auth failed. '
                                 'Set --bridge-secret (or TOFU_BRIDGE_SECRET env var) '
                                 'to match the server.')
                else:
                    _emit('proxy')
                    logger.error(
                        '401 answered by a GATEWAY, not Tofu — the server '
                        'address is intercepted (SSO proxy?). Re-check the '
                        'connect line\'s URL half (ssh tunnel: '
                        'http://127.0.0.1:<port>).')
                time.sleep(poll_interval * 10)
                continue
            if resp.status_code != 200:
                _emit('http', code=resp.status_code)
                logger.info('Server returned %s', resp.status_code)
                time.sleep(poll_interval * 3)
                continue

            _emit('ok')
            data = resp.json()
            commands = data.get('commands', [])

            if commands:
                logger.info('Received %d command(s)', len(commands))

            for cmd in commands:
                cmd_id = cmd.get('id', '')
                cmd_type = cmd.get('type', '')
                cmd_params = cmd.get('params', {})

                logger.info('  → Executing: %s (id=%s...)', cmd_type, cmd_id[:8])

                if cmd_type == 'project_run_command':
                    # RWA P2: streamed, off the poll loop (blocking here would
                    # stall heartbeats past the 15s connected window).
                    _start_project_run_streamed(cmd_id, cmd_params, permissions,
                                                result_queue, stream_outbox,
                                                io_lock)
                    continue

                if cmd_type == 'egress_http_stream':
                    # S3: streamed, off the poll loop — a 30-min LLM stream
                    # must not stall heartbeats past the 15s window.
                    _start_egress_stream_streamed(cmd_id, cmd_params, permissions,
                                                  result_queue, stream_outbox,
                                                  io_lock)
                    continue

                if cmd_type == 'egress_cancel':
                    # S3: fire-and-forget abort of an in-flight stream.
                    from lib.desktop_agent._egress import cancel_inflight
                    cancel_inflight(str(cmd_params.get('cmd_id') or ''))
                    continue

                result = dispatch_command(cmd_type, cmd_params, permissions)

                # Truncate large results for transport
                result_str = json.dumps(result, ensure_ascii=False, default=str)
                if len(result_str) > 500_000:
                    result = {'error': f'Result too large ({len(result_str):,} bytes), truncated',
                              'partial': result_str[:100_000]}

                with io_lock:
                    result_queue.append({
                        'id': cmd_id,
                        'result': result,
                        'error': result.get('error') if isinstance(result, dict) else None,
                    })

                status = '✅' if not (isinstance(result, dict) and result.get('error')) else '❌'
                logger.info('     %s %s done', status, cmd_type)

        except requests.ConnectionError:
            consecutive_errors += 1
            _emit('unreachable')
            if consecutive_errors == 1:
                logger.info('Cannot reach server at %s, retrying...', server_url, exc_info=True)
            wait = min(poll_interval * (2 ** min(consecutive_errors, 5)), 60)
            time.sleep(wait)
            continue

        except KeyboardInterrupt:
            logger.info('\n[Agent] Shutting down...')
            break

        except Exception as e:
            _emit('error', detail=str(e)[:120])
            logger.error('Error: %s', e, exc_info=True)
            time.sleep(poll_interval * 2)

        time.sleep(poll_interval)


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
    parser.add_argument('--allow-all', action='store_true', help='Enable all permissions')
    parser.add_argument('--poll-interval', type=float, default=1.0, help='Polling interval in seconds')
    parser.add_argument('--bridge-secret', default='',
                        help='X-Bridge-Secret value matching server TOFU_BRIDGE_SECRET '
                             '(required when the server enforces bridge auth). '
                             'Falls back to TOFU_BRIDGE_SECRET env var.')
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
        allow_all=args.allow_all,
    )

    bridge_secret = (args.bridge_secret
                     or os.environ.get('TOFU_BRIDGE_SECRET')
                     or '').strip()

    if args.root:
        from lib.desktop_agent.config import merge_cli_roots
        cfg = load_config()
        try:
            cfg['share_roots'] = merge_cli_roots(cfg.get('share_roots'), args.root)
        except ValueError as e:
            parser.error(str(e))
        save_config(cfg)
        for r in cfg['share_roots']:
            if not os.path.isdir(os.path.expanduser(str(r.get('path', '')))):
                logger.warning('[Agent] share root %r path does not exist: %s',
                               r.get('name'), r.get('path'))
        logger.info('[Agent] share roots: %s',
                    ', '.join(f"{r.get('name')}={r.get('path')}"
                              for r in cfg['share_roots']))

    run_agent(args.server, permissions, args.poll_interval, bridge_secret=bridge_secret)

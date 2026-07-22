"""
Desktop Agent — polling loop (``run_agent``) and CLI entry point.

``run_agent`` polls the Tofu server for queued commands, executes them
locally via ``dispatch_command`` and posts back results.  ``main`` parses
CLI args and is invoked from the package ``__main__`` guard — importing
this module (or the package) never triggers the CLI.
"""

import argparse
import json
import os
import time

import requests

from lib.desktop_agent._dispatch import COMMANDS, dispatch_command
from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Polling Loop (runs on your local machine)
# ══════════════════════════════════════════════════════════

def run_agent(server_url, permissions, poll_interval=1.0, bridge_secret='',
              stop_event=None):
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
    """

    endpoint = f'{server_url.rstrip("/")}/api/desktop/poll'
    result_queue = []
    headers = {}
    if bridge_secret:
        headers['X-Bridge-Secret'] = bridge_secret

    logger.info('Desktop Agent starting...')
    logger.info('   Server: %s', server_url)
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
            # Send results + get new commands (single endpoint, like browser extension)
            resp = requests.post(
                endpoint,
                json={'results': result_queue},
                headers=headers,
                timeout=15,
                proxies={'no_proxy': '*'}  # localhost — always bypass env proxy
            )
            result_queue = []  # clear sent results
            consecutive_errors = 0

            if resp.status_code == 401:
                logger.error('Server returned 401 — bridge auth failed. '
                             'Set --bridge-secret (or TOFU_BRIDGE_SECRET env var) '
                             'to match the server.')
                time.sleep(poll_interval * 10)
                continue
            if resp.status_code != 200:
                logger.info('Server returned %s', resp.status_code)
                time.sleep(poll_interval * 3)
                continue

            data = resp.json()
            commands = data.get('commands', [])

            if commands:
                logger.info('Received %d command(s)', len(commands))

            for cmd in commands:
                cmd_id = cmd.get('id', '')
                cmd_type = cmd.get('type', '')
                cmd_params = cmd.get('params', {})

                logger.info('  → Executing: %s (id=%s...)', cmd_type, cmd_id[:8])

                result = dispatch_command(cmd_type, cmd_params, permissions)

                # Truncate large results for transport
                result_str = json.dumps(result, ensure_ascii=False, default=str)
                if len(result_str) > 500_000:
                    result = {'error': f'Result too large ({len(result_str):,} bytes), truncated',
                              'partial': result_str[:100_000]}

                result_queue.append({
                    'id': cmd_id,
                    'result': result,
                    'error': result.get('error') if isinstance(result, dict) else None,
                })

                status = '✅' if not (isinstance(result, dict) and result.get('error')) else '❌'
                logger.info('     %s %s done', status, cmd_type)

        except requests.ConnectionError:
            consecutive_errors += 1
            if consecutive_errors == 1:
                logger.info('Cannot reach server at %s, retrying...', server_url, exc_info=True)
            wait = min(poll_interval * (2 ** min(consecutive_errors, 5)), 60)
            time.sleep(wait)
            continue

        except KeyboardInterrupt:
            logger.info('\n[Agent] Shutting down...')
            break

        except Exception as e:
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
"""
    )
    parser.add_argument('--server', required=True, help='Tofu server URL')
    parser.add_argument('--allow-write', action='store_true', help='Allow file write/move operations')
    parser.add_argument('--allow-exec', action='store_true', help='Allow running commands and opening apps')
    parser.add_argument('--allow-gui', action='store_true', help='Allow GUI automation (mouse, keyboard, screenshot)')
    parser.add_argument('--allow-all', action='store_true', help='Enable all permissions')
    parser.add_argument('--poll-interval', type=float, default=1.0, help='Polling interval in seconds')
    parser.add_argument('--bridge-secret', default='',
                        help='X-Bridge-Secret value matching server TOFU_BRIDGE_SECRET '
                             '(required when the server enforces bridge auth). '
                             'Falls back to TOFU_BRIDGE_SECRET env var.')

    args = parser.parse_args(argv)

    from lib.desktop_agent._permissions import build_permissions
    permissions = build_permissions(
        allow_write=args.allow_write,
        allow_exec=args.allow_exec,
        allow_gui=args.allow_gui,
        allow_all=args.allow_all,
    )

    bridge_secret = (args.bridge_secret
                     or os.environ.get('TOFU_BRIDGE_SECRET')
                     or '').strip()

    run_agent(args.server, permissions, args.poll_interval, bridge_secret=bridge_secret)

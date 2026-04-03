"""
Desktop Agent — Local machine control bridge for ChatUI.

Runs on the user's local machine (not the server), connects back to ChatUI
and exposes system-level tools that Chrome Extension cannot provide:

  ✅ File system operations (read/write/move/copy local files)
  ✅ Run local applications (open files in default app, launch programs)
  ✅ Clipboard read/write (richer than browser clipboard)
  ✅ Screenshot entire desktop (not just browser tabs)
  ✅ System info (processes, disk usage, battery, network)
  ✅ GUI automation via pyautogui (click anywhere on screen, type anywhere)
  ✅ Manage local services (start/stop processes)

Architecture:
  Desktop Agent (your PC)  ←→  ChatUI Server  ←→  LLM

  The agent polls /api/desktop/poll just like the browser extension polls
  /api/browser/poll. The server queues commands and returns results.

Usage:
  pip install pyautogui pillow psutil
  python lib/desktop_agent.py --server http://your-server:5000

Security:
  The agent only accepts commands from YOUR ChatUI server.
  All dangerous operations require --allow-write / --allow-exec flags.
"""

import argparse
import base64
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
import traceback
from pathlib import Path

import psutil
import pyautogui
import pyperclip
import requests

from lib.log import get_logger

logger = get_logger(__name__)


def _get_root_path():
    """Return the root filesystem path for disk usage queries.

    - Unix: '/'
    - Windows: the drive where Python is running (usually 'C:\\\\')
    """
    if os.name == 'nt':
        return os.path.splitdrive(os.getcwd())[0] + '\\\\'
    return '/'


# ══════════════════════════════════════════════════════════
#  Command Handlers
# ══════════════════════════════════════════════════════════

def cmd_list_files(params):
    """List files in a directory."""
    path = os.path.expanduser(params.get('path', '~'))
    if not os.path.isdir(path):
        return {'error': f'Not a directory: {path}'}

    entries = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            stat = entry.stat(follow_symlinks=False)
            entries.append({
                'name': entry.name,
                'type': 'dir' if entry.is_dir() else 'file',
                'size': stat.st_size if entry.is_file() else None,
                'modified': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)),
            })
    except PermissionError as e:
        logger.warning('Permission denied listing directory %s: %s', path, e, exc_info=True)
        return {'error': f'Permission denied: {e}'}

    return {'path': path, 'entries': entries[:500], 'total': len(entries)}


def cmd_read_file(params):
    """Read a local file."""
    path = os.path.expanduser(params.get('path', ''))
    max_size = params.get('maxSize', 500_000)  # 500KB default

    if not os.path.isfile(path):
        return {'error': f'File not found: {path}'}

    size = os.path.getsize(path)
    if size > max_size:
        return {'error': f'File too large ({size:,} bytes > {max_size:,} limit). Use maxSize param to override.'}

    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            content = f.read()
        return {'path': path, 'size': size, 'content': content}
    except Exception as e:
        logger.warning('cmd_read_file failed for path=%s: %s', path, e, exc_info=True)
        return {'error': str(e)}


def cmd_write_file(params):
    """Write content to a local file."""
    path = os.path.expanduser(params.get('path', ''))
    content = params.get('content', '')
    mkdir = params.get('createDirs', False)

    if mkdir:
        os.makedirs(os.path.dirname(path), exist_ok=True)

    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return {'path': path, 'written': len(content), 'success': True}
    except Exception as e:
        logger.warning('cmd_write_file failed for path=%s: %s', path, e, exc_info=True)
        return {'error': str(e)}


def cmd_move_file(params):
    """Move or rename a file/directory."""
    src = os.path.expanduser(params.get('src', ''))
    dst = os.path.expanduser(params.get('dst', ''))

    try:
        shutil.move(src, dst)
        return {'src': src, 'dst': dst, 'success': True}
    except Exception as e:
        logger.warning('cmd_move_file failed src=%s dst=%s: %s', src, dst, e, exc_info=True)
        return {'error': str(e)}


def cmd_open_file(params):
    """Open a file with the default application (like double-clicking)."""
    path = os.path.expanduser(params.get('path', ''))

    system = platform.system()
    try:
        if system == 'Darwin':      # macOS
            subprocess.Popen(['open', path])
        elif system == 'Windows':
            os.startfile(path)
        else:                        # Linux
            subprocess.Popen(['xdg-open', path])
        return {'opened': path, 'success': True}
    except Exception as e:
        logger.warning('cmd_open_file failed for path=%s: %s', path, e, exc_info=True)
        return {'error': str(e)}


def cmd_open_app(params):
    """Launch an application by name or path."""
    app = params.get('app', '')
    args = params.get('args', [])

    try:
        subprocess.Popen([app] + args)
        return {'launched': app, 'args': args, 'success': True}
    except Exception as e:
        logger.warning('cmd_open_app failed for app=%s: %s', app, e, exc_info=True)
        return {'error': str(e)}


# Shell metacharacters that require shell=True for correct behaviour
_SHELL_META_RE = re.compile(r'[|&;<>()$`\\"\'\ \n*?\[#~]')

def cmd_run_local(params):
    """Run a shell command on the local machine.

    Security: avoids shell=True when the command is a simple executable
    invocation (no pipes, redirects, globs, etc.).  When shell features
    *are* needed, the command is passed as a single argument to an
    explicit ``['/bin/sh', '-c', ...]`` invocation so that the
    argument vector is never ambiguously parsed.
    """
    command = params.get('command', '')
    if not isinstance(command, str) or not command.strip():
        return {'error': 'Empty or invalid command'}
    cwd = params.get('cwd')
    timeout = params.get('timeout', 30)

    resolved_cwd = os.path.expanduser(cwd) if cwd else None

    try:
        needs_shell = bool(_SHELL_META_RE.search(command))
        if needs_shell:
            # Use explicit shell invocation instead of shell=True so
            # that *command* is a single, unambiguous argument to sh.
            from lib.compat import get_shell_args
            args = get_shell_args(command)
        else:
            # Simple command — split into argv list, no shell involved.
            # On Windows, use posix=False so that backslash paths and
            # double-quote quoting are handled correctly.
            from lib.compat import IS_WINDOWS
            args = shlex.split(command, posix=not IS_WINDOWS)

        result = subprocess.run(
            args, shell=False,
            capture_output=True, text=True,
            timeout=timeout,
            cwd=resolved_cwd,
        )
        return {
            'stdout': result.stdout[:100_000],
            'stderr': result.stderr[:20_000],
            'exit_code': result.returncode,
        }
    except subprocess.TimeoutExpired:
        logger.warning('cmd_run_local timed out: cmd=%s timeout=%ds', command[:120], timeout, exc_info=True)
        return {'error': f'Command timed out after {timeout}s'}
    except Exception as e:
        logger.warning('cmd_run_local failed for cmd=%s: %s', command[:120], e, exc_info=True)
        return {'error': str(e)}


def cmd_screenshot_desktop(params):
    """Take a screenshot of the entire desktop (or a region)."""
    region = params.get('region')  # (x, y, w, h) or None for full screen
    try:
        img = pyautogui.screenshot(region=tuple(region) if region else None)

        # Resize if too large
        max_dim = params.get('maxDimension', 1920)
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size)

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=75)
        b64 = base64.b64encode(buf.getvalue()).decode()

        return {
            'width': img.width,
            'height': img.height,
            'format': 'jpeg',
            'base64': b64,
            'size_bytes': len(buf.getvalue()),
        }
    except Exception as e:
        logger.warning('cmd_screenshot_desktop failed: %s', e, exc_info=True)
        return {'error': str(e)}


def cmd_gui_action(params):
    """Perform GUI automation: click, type, hotkey, scroll at screen coordinates."""
    pyautogui.FAILSAFE = True   # Move mouse to corner to abort
    pyautogui.PAUSE = 0.1
    action = params.get('action', '')
    try:
        if action == 'click':
            x, y = params.get('x', 0), params.get('y', 0)
            button = params.get('button', 'left')
            clicks = params.get('clicks', 1)
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
            return {'action': 'click', 'x': x, 'y': y, 'success': True}

        elif action == 'doubleclick':
            x, y = params.get('x', 0), params.get('y', 0)
            pyautogui.doubleClick(x=x, y=y)
            return {'action': 'doubleclick', 'x': x, 'y': y, 'success': True}

        elif action == 'type':
            text = params.get('text', '')
            interval = params.get('interval', 0.02)
            pyautogui.typewrite(text, interval=interval) if text.isascii() else pyautogui.write(text)
            return {'action': 'type', 'chars': len(text), 'success': True}

        elif action == 'hotkey':
            keys = params.get('keys', [])
            pyautogui.hotkey(*keys)
            return {'action': 'hotkey', 'keys': keys, 'success': True}

        elif action == 'moveto':
            x, y = params.get('x', 0), params.get('y', 0)
            duration = params.get('duration', 0.3)
            pyautogui.moveTo(x, y, duration=duration)
            return {'action': 'moveto', 'x': x, 'y': y, 'success': True}

        elif action == 'scroll':
            amount = params.get('amount', -3)
            x, y = params.get('x'), params.get('y')
            pyautogui.scroll(amount, x=x, y=y)
            return {'action': 'scroll', 'amount': amount, 'success': True}

        elif action == 'drag':
            x1, y1 = params.get('x1', 0), params.get('y1', 0)
            x2, y2 = params.get('x2', 0), params.get('y2', 0)
            duration = params.get('duration', 0.5)
            pyautogui.moveTo(x1, y1)
            pyautogui.drag(x2 - x1, y2 - y1, duration=duration)
            return {'action': 'drag', 'from': [x1, y1], 'to': [x2, y2], 'success': True}

        elif action == 'locate':
            # Find an image on screen (template matching)
            image_b64 = params.get('image')
            if not image_b64:
                return {'error': 'image (base64) required for locate action'}
            from PIL import Image
            img_bytes = base64.b64decode(image_b64)
            img = Image.open(io.BytesIO(img_bytes))
            # Save temp file for pyautogui
            import tempfile as _tempfile
            tmp_path = os.path.join(_tempfile.gettempdir(), '_chatui_locate.png')
            img.save(tmp_path)
            loc = pyautogui.locateOnScreen(tmp_path, confidence=params.get('confidence', 0.8))
            if loc:
                center = pyautogui.center(loc)
                return {'found': True, 'x': center.x, 'y': center.y, 'region': list(loc)}
            return {'found': False}

        else:
            return {'error': f'Unknown GUI action: {action}'}

    except Exception as e:
        logger.warning('cmd_gui_action failed for action=%s: %s', action, e, exc_info=True)
        return {'error': str(e)}


def cmd_clipboard(params):
    """Read or write the system clipboard."""
    action = params.get('action', 'read')

    if action == 'read':
        return {'content': pyperclip.paste()}
    elif action == 'write':
        pyperclip.copy(params.get('content', ''))
        return {'success': True, 'written': len(params.get('content', ''))}
    return {'error': f'Unknown clipboard action: {action}'}


def cmd_system_info(params):
    """Get system information."""
    info_type = params.get('type', 'overview')

    if info_type == 'overview':
        return {
            'platform': platform.platform(),
            'python': platform.python_version(),
            'cpu_count': psutil.cpu_count(),
            'cpu_percent': psutil.cpu_percent(interval=0.5),
            'memory': {
                'total_gb': round(psutil.virtual_memory().total / 1e9, 1),
                'used_gb': round(psutil.virtual_memory().used / 1e9, 1),
                'percent': psutil.virtual_memory().percent,
            },
            'disk': {
                'total_gb': round(psutil.disk_usage(_get_root_path()).total / 1e9, 1),
                'used_gb': round(psutil.disk_usage(_get_root_path()).used / 1e9, 1),
                'percent': psutil.disk_usage(_get_root_path()).percent,
            },
            'user': os.getenv('USER') or os.getenv('USERNAME', 'unknown'),
            'home': str(Path.home()),
            'cwd': os.getcwd(),
        }

    elif info_type == 'processes':
        top_n = params.get('top', 15)
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info', 'status']):
            try:
                info = p.info
                procs.append({
                    'pid': info['pid'],
                    'name': info['name'],
                    'cpu': info['cpu_percent'],
                    'memory_mb': round(info['memory_info'].rss / 1e6, 1) if info['memory_info'] else 0,
                    'status': info['status'],
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                logger.debug('Skipping inaccessible process during enumeration', exc_info=True)
        procs.sort(key=lambda p: p['cpu'], reverse=True)
        return {'processes': procs[:top_n]}

    elif info_type == 'kill':
        pid = params.get('pid')
        if pid:
            try:
                p = psutil.Process(pid)
                p.terminate()
                return {'killed': pid, 'name': p.name(), 'success': True}
            except Exception as e:
                logger.warning('cmd_system_info process kill failed pid=%s: %s', pid, e, exc_info=True)
                return {'error': str(e)}

    return {'error': f'Unknown info type: {info_type}'}


# ══════════════════════════════════════════════════════════
#  Command Dispatcher
# ══════════════════════════════════════════════════════════

COMMANDS = {
    # File system
    'desktop_list_files':    cmd_list_files,
    'desktop_read_file':     cmd_read_file,
    'desktop_write_file':    cmd_write_file,
    'desktop_move_file':     cmd_move_file,

    # Applications
    'desktop_open_file':     cmd_open_file,
    'desktop_open_app':      cmd_open_app,
    'desktop_run_command':   cmd_run_local,

    # GUI automation
    'desktop_screenshot':    cmd_screenshot_desktop,
    'desktop_gui_action':    cmd_gui_action,
    'desktop_clipboard':     cmd_clipboard,

    # System
    'desktop_system_info':   cmd_system_info,
}

# Permission levels
WRITE_COMMANDS = {'desktop_write_file', 'desktop_move_file'}
EXEC_COMMANDS = {'desktop_run_command', 'desktop_open_file', 'desktop_open_app'}
GUI_COMMANDS = {'desktop_gui_action', 'desktop_screenshot'}


def dispatch_command(cmd_type, params, permissions):
    """Execute a command if permitted."""
    if cmd_type not in COMMANDS:
        return {'error': f'Unknown command: {cmd_type}'}

    # Permission checks
    if cmd_type in WRITE_COMMANDS and not permissions.get('allow_write'):
        return {'error': f'Command {cmd_type} requires --allow-write flag'}
    if cmd_type in EXEC_COMMANDS and not permissions.get('allow_exec'):
        return {'error': f'Command {cmd_type} requires --allow-exec flag'}
    if cmd_type in GUI_COMMANDS and not permissions.get('allow_gui'):
        return {'error': f'Command {cmd_type} requires --allow-gui flag'}

    try:
        return COMMANDS[cmd_type](params)
    except Exception as e:
        logger.error('dispatch_command %s failed', cmd_type, exc_info=True)
        return {'error': f'{type(e).__name__}: {e}', 'traceback': traceback.format_exc()[-500:]}


# ══════════════════════════════════════════════════════════
#  Polling Loop (runs on your local machine)
# ══════════════════════════════════════════════════════════

def run_agent(server_url, permissions, poll_interval=1.0):
    """Main agent loop — polls server for commands, executes locally, returns results."""

    endpoint = f'{server_url.rstrip("/")}/api/desktop/poll'
    result_queue = []
    session = None  # Don't use session — Flask dev server keep-alive bug

    logger.info('Desktop Agent starting...')
    logger.info('   Server: %s', server_url)
    logger.info('   Permissions: %s', json.dumps(permissions))
    available_cmds = ', '.join(sorted(COMMANDS.keys()))
    logger.info('   Available commands: %s', available_cmds)
    logger.info('   Poll interval: %ss', poll_interval)
    logger.info('   Press Ctrl+C to stop\n')

    consecutive_errors = 0

    while True:
        try:
            # Send results + get new commands (single endpoint, like browser extension)
            resp = requests.post(
                endpoint,
                json={'results': result_queue},
                timeout=15,
                proxies={'no_proxy': '*'}  # localhost — always bypass env proxy
            )
            result_queue = []  # clear sent results
            consecutive_errors = 0

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

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='ChatUI Desktop Agent — control your computer from AI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Read-only mode (safest — can browse files, take screenshots, read clipboard)
  python desktop_agent.py --server http://localhost:5000

  # Allow file writes
  python desktop_agent.py --server http://localhost:5000 --allow-write

  # Allow running commands + GUI automation (most powerful)
  python desktop_agent.py --server http://localhost:5000 --allow-write --allow-exec --allow-gui

  # Full access
  python desktop_agent.py --server http://localhost:5000 --allow-all
"""
    )
    parser.add_argument('--server', required=True, help='ChatUI server URL')
    parser.add_argument('--allow-write', action='store_true', help='Allow file write/move operations')
    parser.add_argument('--allow-exec', action='store_true', help='Allow running commands and opening apps')
    parser.add_argument('--allow-gui', action='store_true', help='Allow GUI automation (mouse, keyboard, screenshot)')
    parser.add_argument('--allow-all', action='store_true', help='Enable all permissions')
    parser.add_argument('--poll-interval', type=float, default=1.0, help='Polling interval in seconds')

    args = parser.parse_args()

    permissions = {
        'allow_write': args.allow_write or args.allow_all,
        'allow_exec': args.allow_exec or args.allow_all,
        'allow_gui': args.allow_gui or args.allow_all,
    }

    run_agent(args.server, permissions, args.poll_interval)

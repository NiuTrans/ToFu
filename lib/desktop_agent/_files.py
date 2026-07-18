"""
Desktop Agent — file-system & application command handlers.

Contains ``_get_root_path`` (disk-usage root helper) plus the file/app
command handlers: list/read/write/move files, open files, launch apps.
"""

import os
import platform
import shutil
import subprocess
import time

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

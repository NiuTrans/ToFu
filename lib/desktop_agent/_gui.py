"""
Desktop Agent — GUI automation, screenshot, clipboard & system-info handlers.

Contains ``cmd_screenshot_desktop``, ``cmd_gui_action``, ``cmd_clipboard``,
and ``cmd_system_info``.
"""

import base64
import io
import os
import platform
from pathlib import Path

import psutil
import pyautogui
import pyperclip

from lib.desktop_agent._files import _get_root_path
from lib.log import get_logger

logger = get_logger(__name__)


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
            # Save temp file for pyautogui (use project-local data/ dir,
            # as /tmp may not be accessible on all machines)
            _data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data')
            os.makedirs(_data_dir, exist_ok=True)
            tmp_path = os.path.join(_data_dir, '_tofu_locate.png')
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

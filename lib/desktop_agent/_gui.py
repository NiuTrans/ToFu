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

from lib.desktop_agent._files import _get_root_path
from lib.desktop_agent._scaling import (
    api_to_real,
    real_to_api,
    scaled_dimensions,
)
from lib.log import get_logger

logger = get_logger(__name__)

# ── Optional GUI/system dependencies ──
# pyautogui (screenshot + mouse/keyboard), pyperclip (clipboard) and psutil
# (process/system info) are only needed when the desktop-control agent actually
# runs on a user's machine. They are NOT hard requirements of the Tofu server
# package, so importing this module must never crash when they are absent
# (e.g. a server-only checkout, or a desktop build where the deps were not
# bundled). We import lazily and record the failure; each handler surfaces a
# clear "not enabled" hint instead of the whole package raising ImportError.
_MISSING_DEP_HINT = (
    'Desktop computer-control is not enabled: the "{mod}" package is not '
    'installed. Install the agent deps (pip install pyautogui pyperclip psutil) '
    'or enable computer control from the Tofu tray menu, then retry.'
)

try:
    import pyautogui
except Exception as e:  # ImportError, or DISPLAY/Xlib errors on headless boxes
    pyautogui = None
    logger.warning('[Desktop] pyautogui unavailable — GUI/screenshot tools disabled: %s', e)

try:
    import pyperclip
except Exception as e:
    pyperclip = None
    logger.warning('[Desktop] pyperclip unavailable — clipboard tool disabled: %s', e)

try:
    import psutil
except Exception as e:
    psutil = None
    logger.warning('[Desktop] psutil unavailable — system-info tool disabled: %s', e)


def _dep_error(mod: str) -> dict:
    """Uniform 'dependency missing' result for a desktop handler."""
    return {'error': _MISSING_DEP_HINT.format(mod=mod)}


def cmd_screenshot_desktop(params):
    """Take a screenshot of the entire desktop (or a region)."""
    if pyautogui is None:
        return _dep_error('pyautogui')
    region = params.get('region')  # (x, y, w, h) or None for full screen
    try:
        img = pyautogui.screenshot(region=tuple(region) if region else None)
        real_w, real_h = img.width, img.height

        # Downscale to XGA (default 1024x768) per Anthropic's Computer Use
        # grounding guidance: vision models pick coordinates far more reliably
        # on a small image, and we map the model's click back to real pixels in
        # cmd_gui_action via compute_scale(real_w, real_h). A legacy
        # 'maxDimension' param still caps the long edge if the caller insists on
        # a larger image, but XGA is the default.
        scaled_w, scaled_h, scale = scaled_dimensions(real_w, real_h)
        max_dim = params.get('maxDimension')
        if max_dim and max(scaled_w, scaled_h) > max_dim:
            ratio = max_dim / max(scaled_w, scaled_h)
            scaled_w, scaled_h = max(1, round(scaled_w * ratio)), max(1, round(scaled_h * ratio))
            scale *= ratio
        if (scaled_w, scaled_h) != (real_w, real_h):
            img = img.resize((scaled_w, scaled_h))

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=75)
        b64 = base64.b64encode(buf.getvalue()).decode()

        return {
            'width': img.width,
            'height': img.height,
            'real_width': real_w,
            'real_height': real_h,
            'scale': round(scale, 6),
            'format': 'jpeg',
            'base64': b64,
            'size_bytes': len(buf.getvalue()),
        }
    except Exception as e:
        logger.warning('cmd_screenshot_desktop failed: %s', e, exc_info=True)
        return {'error': str(e)}


def cmd_gui_action(params):
    """Perform GUI automation: click, type, hotkey, scroll at screen coordinates."""
    if pyautogui is None:
        return _dep_error('pyautogui')
    pyautogui.FAILSAFE = True   # Move mouse to corner to abort
    pyautogui.PAUSE = 0.1
    action = params.get('action', '')

    # The model produced its coordinates against the downscaled (XGA) screenshot
    # from cmd_screenshot_desktop, so translate them back to real screen pixels
    # before driving pyautogui. The scale is derived deterministically from the
    # real screen size (no shared state with the screenshot call). If a caller
    # already passes real coordinates it can set scale=1 to opt out.
    try:
        real_w, real_h = pyautogui.size()
    except Exception as e:
        logger.debug('[Desktop] pyautogui.size() unavailable, assuming 1:1 coords: %s', e)
        real_w = real_h = 0
    _, _, _scale = scaled_dimensions(real_w, real_h)
    if 'scale' in params:  # explicit override (e.g. caller sends real coords)
        try:
            _scale = float(params['scale']) or 1.0
        except (ValueError, TypeError) as _e:
            logger.debug('cmd gui action: unparseable/unexpected type (%s)', _e)
            _scale = 1.0

    def _pt(x, y):
        return api_to_real(x, y, _scale)

    try:
        if action == 'click':
            x, y = _pt(params.get('x', 0), params.get('y', 0))
            button = params.get('button', 'left')
            clicks = params.get('clicks', 1)
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
            return {'action': 'click', 'x': x, 'y': y, 'success': True}

        elif action == 'doubleclick':
            x, y = _pt(params.get('x', 0), params.get('y', 0))
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
            x, y = _pt(params.get('x', 0), params.get('y', 0))
            duration = params.get('duration', 0.3)
            pyautogui.moveTo(x, y, duration=duration)
            return {'action': 'moveto', 'x': x, 'y': y, 'success': True}

        elif action == 'scroll':
            amount = params.get('amount', -3)
            x, y = params.get('x'), params.get('y')
            if x is not None and y is not None:
                x, y = _pt(x, y)
            pyautogui.scroll(amount, x=x, y=y)
            return {'action': 'scroll', 'amount': amount, 'success': True}

        elif action == 'drag':
            x1, y1 = _pt(params.get('x1', 0), params.get('y1', 0))
            x2, y2 = _pt(params.get('x2', 0), params.get('y2', 0))
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
                # Report coordinates in the same scaled space the model sees, so
                # a follow-up click(x,y) round-trips correctly.
                api_x, api_y = real_to_api(center.x, center.y, _scale)
                return {'found': True, 'x': api_x, 'y': api_y,
                        'real_x': center.x, 'real_y': center.y, 'region': list(loc)}
            return {'found': False}

        else:
            return {'error': f'Unknown GUI action: {action}'}

    except Exception as e:
        logger.warning('cmd_gui_action failed for action=%s: %s', action, e, exc_info=True)
        return {'error': str(e)}


def cmd_clipboard(params):
    """Read or write the system clipboard."""
    if pyperclip is None:
        return _dep_error('pyperclip')
    action = params.get('action', 'read')

    if action == 'read':
        return {'content': pyperclip.paste()}
    elif action == 'write':
        pyperclip.copy(params.get('content', ''))
        return {'success': True, 'written': len(params.get('content', ''))}
    return {'error': f'Unknown clipboard action: {action}'}


def cmd_system_info(params):
    """Get system information."""
    if psutil is None:
        return _dep_error('psutil')
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

#!/usr/bin/env python3
"""Tofu Desktop Launcher — system tray + Tofu server + auto-open browser.

This is the entry point for the packaged desktop app (.exe / .app).

It spawns the real Tofu server (Quart + Hypercorn) as a child process — the
SAME code path as ``python server.py`` — so the desktop build gets the full,
correctly-initialized startup (DB bootstrap, background workers, executor
sizing, push hub). It then opens the default browser and shows a system tray
icon with controls.

Three things this launcher is careful about, because they bit us before:

1. **Windowed builds have no console.** Under PyInstaller ``console=False``,
   ``sys.stdout`` / ``sys.stderr`` are ``None``. Writing to them raises
   ``AttributeError: 'NoneType' object has no attribute 'write'``. All
   diagnostics here go through :func:`_log`, which is null-safe and tees to
   a log file next to the executable.

2. **The app is Quart, not Flask.** ``app.run(threaded=...)`` is a Flask-only
   call and raises ``TypeError`` on a Quart app. We never call it. The server
   is launched via server.py's own ``__main__`` (Hypercorn), as a subprocess.

3. **HiDPI blur.** The process must declare per-monitor DPI awareness *before*
   any window (tray icon / tk dialog) is created, or Windows bitmap-stretches
   everything.

When frozen (PyInstaller), paths resolve relative to the bundle directory.
When run from source, paths resolve relative to the project root.
"""

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

# ── Resolve base directory (frozen vs source) ──
if getattr(sys, 'frozen', False):
    # PyInstaller --onedir: the executable lives at dist/Tofu/Tofu.exe and
    # the app files are at dist/Tofu/_internal/
    BASE_DIR = os.path.join(os.path.dirname(sys.executable), '_internal')
    if not os.path.isdir(BASE_DIR):
        BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ensure the app directory is on sys.path so `import server` / runpy works.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# ── Portable data directory (next to the executable, not inside _internal) ──
_EXE_DIR = (os.path.dirname(sys.executable)
            if getattr(sys, 'frozen', False) else BASE_DIR)
DATA_DIR = os.path.join(_EXE_DIR, 'data')
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass

os.environ.setdefault('TOFU_DATA_DIR', DATA_DIR)

DEFAULT_PORT = 15000

# ── Update check ──────────────────────────────────────────────────────
# The desktop app has no built-in updater, so a user on an old version never
# learns a new one shipped. On startup we do ONE best-effort query of the
# repo's latest published release and, if it is newer than the bundled
# VERSION, surface a non-blocking "download update" affordance in the tray.
# Anything that goes wrong (offline, rate-limited, parse error) is swallowed
# and logged at diagnostic level — the check must never delay or break launch.
_RELEASES_API = 'https://api.github.com/repos/rangehow/ToFu/releases/latest'
_RELEASES_PAGE = 'https://github.com/rangehow/ToFu/releases/latest'

# ── Null-safe diagnostics ─────────────────────────────────────────────
# Opened lazily; in windowed builds sys.stderr is None so the file is the
# only place these messages can land.
_LOG_PATH = os.path.join(DATA_DIR, 'desktop.log')
_log_fh = None


def _log(msg: str) -> None:
    """Write a diagnostic line without ever touching a None stream."""
    line = '[Tofu] %s\n' % msg
    global _log_fh
    if _log_fh is None:
        try:
            _log_fh = open(_LOG_PATH, 'a', encoding='utf-8', buffering=1)
        except OSError:
            _log_fh = False  # give up on the file, still try stderr
    try:
        if _log_fh:
            _log_fh.write(line)
    except OSError:
        pass
    # sys.stderr may be None (windowed) — guard explicitly.
    try:
        if sys.stderr is not None:
            sys.stderr.write(line)
    except Exception:
        pass


def _enable_dpi_awareness() -> None:
    """Mark the process per-monitor DPI-aware so HiDPI rendering is crisp.

    Must run before any window is created. No-op off Windows. Tries the
    newest API first and degrades gracefully on older Windows versions.
    """
    if not sys.platform.startswith('win'):
        return
    try:
        import ctypes
    except Exception as e:  # pragma: no cover - ctypes always present on win
        _log('DPI awareness unavailable: %s' % e)
        return
    # Per-Monitor v2 (Windows 10 1703+): DPI_AWARENESS_CONTEXT = -4
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Windows 8.1+)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    # System-DPI aware (Vista+) — last resort, still far better than none.
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        _log('Could not set DPI awareness: %s' % e)


def _find_free_port(preferred: int = DEFAULT_PORT) -> int:
    """Return the preferred port if available, else an OS-assigned free one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _wait_for_server(host: str, port: int, proc: subprocess.Popen,
                     timeout: float = 90.0) -> bool:
    """Block until the server accepts connections, the child dies, or timeout.

    Cold starts (FUSE, DB bootstrap, cert reuse) can take a while, so the
    timeout is generous. Returns early with False if the child process exits.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            _log('Server process exited early with code %s' % proc.returncode)
            return False
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _server_command(port: int):
    """Build the (argv, env) for the server child process.

    Frozen: re-launch THIS executable with TOFU_RUN_SERVER=1; the guard at
    the top of :func:`main` turns that invocation into the server.
    Source:  run server.py directly with the current interpreter.
    """
    env = os.environ.copy()
    env['PORT'] = str(port)
    env['BIND_HOST'] = '127.0.0.1'
    # Plain HTTP on loopback — no self-signed cert warnings for a local app.
    env['TOFU_TLS'] = '0'

    if getattr(sys, 'frozen', False):
        env['TOFU_RUN_SERVER'] = '1'
        return [sys.executable], env
    return [sys.executable, os.path.join(BASE_DIR, 'server.py')], env


def _spawn_server(port: int) -> subprocess.Popen:
    """Start the Tofu server as a child process and return the handle."""
    cmd, env = _server_command(port)

    # Tee child stdout/stderr into the desktop log (its own stderr may be a
    # dead handle in windowed mode, so give it a real file).
    try:
        out = open(_LOG_PATH, 'a', encoding='utf-8', buffering=1)
    except OSError:
        out = subprocess.DEVNULL

    kwargs = dict(cwd=BASE_DIR, env=env, stdout=out, stderr=out,
                  stdin=subprocess.DEVNULL)
    if sys.platform.startswith('win'):
        # Don't flash a console window for the child.
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    _log('Starting server: %s (port %d)' % (cmd, port))
    return subprocess.Popen(cmd, **kwargs)


def _load_icon():
    """Load the tray icon image (falls back to a generated square)."""
    from PIL import Image

    for name in ('tofu.ico', 'tofu.icns', 'logo.png'):
        icon_path = os.path.join(BASE_DIR, 'static', 'icons', name)
        if os.path.isfile(icon_path):
            try:
                return Image.open(icon_path)
            except Exception as e:
                _log('Icon load failed for %s: %s' % (name, e))
                continue

    img = Image.new('RGBA', (64, 64), (255, 215, 0, 255))
    return img


def _local_version() -> str:
    """Return the bundled app version (from the VERSION file), or '' if unknown."""
    try:
        from lib.version import __version__ as v
        return (v or '').strip()
    except Exception as e:
        _log('Could not read local version: %s' % e)
        return ''


def _parse_version(v: str):
    """Parse a version string into a comparable tuple of ints.

    Strips a leading 'v' and any pre-release suffix (e.g. '0.14.1-beta' →
    (0, 14, 1)). Returns None when nothing numeric can be parsed.
    """
    if not v:
        return None
    core = v.lstrip('vV').split('-', 1)[0].split('+', 1)[0]
    parts = []
    for chunk in core.split('.'):
        digits = ''.join(ch for ch in chunk if ch.isdigit())
        if digits == '':
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _fetch_latest_version(timeout: float = 6.0):
    """Best-effort fetch of the latest published release tag from GitHub.

    Returns the tag string (e.g. 'v0.14.1') or None on any failure. Never
    raises. Drafts are excluded by the /releases/latest endpoint itself.
    """
    import json
    import urllib.request
    req = urllib.request.Request(
        _RELEASES_API,
        headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'Tofu-Desktop'},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace'))
        tag = (data.get('tag_name') or data.get('name') or '').strip()
        return tag or None
    except Exception as e:
        _log('Update check failed (non-fatal): %s' % e)
        return None


def _check_for_update():
    """Compare local vs latest release. Returns the newer tag string or None.

    None means "no known newer version" (up to date, offline, or unparseable)
    — the caller treats that as "show nothing", so a failed check is silent.
    """
    local = _parse_version(_local_version())
    latest_tag = _fetch_latest_version()
    latest = _parse_version(latest_tag or '')
    if local is None or latest is None:
        return None
    if latest > local:
        _log('Update available: local=%s latest=%s' % (_local_version(), latest_tag))
        return latest_tag
    return None


def _run_tray(port: int, proc: subprocess.Popen):
    """Run the system tray icon (blocks on the main thread)."""
    url = f'http://127.0.0.1:{port}'

    def _shutdown():
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    try:
        import pystray
        from pystray import MenuItem
    except ImportError:
        _log('pystray not available — running without system tray. '
             'Server is at %s' % url)
        try:
            while proc.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        _shutdown()
        return

    icon_image = _load_icon()

    # Holder mutated by the background update check; the tray reads it lazily.
    _update = {'tag': None}

    def on_open(icon, item):
        webbrowser.open(url)

    def on_update(icon, item):
        webbrowser.open(_RELEASES_PAGE)

    def on_components(icon, item):
        """Launch the component installer dialog."""
        try:
            from desktop.post_install import (OPTIONAL_COMPONENTS, _prompt_gui,
                                              _install_components)
        except Exception as e:
            _log('Component installer unavailable: %s' % e)
            return
        not_installed = [c for c in OPTIONAL_COMPONENTS if not c.is_installed()]
        if not not_installed:
            return
        selected = _prompt_gui(not_installed)
        if selected:
            def _bg():
                for name, success, msg in _install_components(selected):
                    _log('%s %s: %s' % ('OK' if success else 'FAIL', name, msg))
            threading.Thread(target=_bg, daemon=True).start()

    def on_quit(icon, item):
        icon.stop()
        _shutdown()
        os._exit(0)

    # Dynamic "update available" item: its text is computed at menu-open time
    # and it is hidden entirely until the background check finds a newer tag.
    menu = pystray.Menu(
        MenuItem('Open Tofu', on_open, default=True),
        MenuItem(lambda item: f'Download update ({_update["tag"]})',
                 on_update,
                 visible=lambda item: bool(_update['tag'])),
        pystray.Menu.SEPARATOR,
        MenuItem('Install Components...', on_components),
        MenuItem(f'Port: {port}', None, enabled=False),
        pystray.Menu.SEPARATOR,
        MenuItem('Quit', on_quit),
    )

    icon = pystray.Icon('tofu', icon_image, 'Tofu', menu)

    # Kick off the update check off the main thread so it never delays the
    # tray appearing. When it finds a newer version it flips the holder and
    # asks pystray to re-render the menu (the item then becomes visible).
    def _bg_update_check():
        tag = _check_for_update()
        if tag:
            _update['tag'] = tag
            try:
                icon.update_menu()
            except Exception as e:
                _log('Could not refresh tray menu after update check: %s' % e)

    threading.Thread(target=_bg_update_check, daemon=True,
                     name='tofu-update-check').start()

    icon.run()
    # Tray stopped (e.g. Quit) — make sure the server goes down too.
    _shutdown()


def main():
    # ── Server mode (frozen self-relaunch) ──
    # When this executable is started with TOFU_RUN_SERVER=1 it IS the server:
    # execute server.py under __main__ so we reuse its full Hypercorn startup.
    if os.environ.get('TOFU_RUN_SERVER') == '1':
        import runpy
        runpy.run_module('server', run_name='__main__')
        return

    # ── Playwright-install mode (frozen self-relaunch) ──
    # A PyInstaller --onedir bundle has NO standalone python.exe — the only
    # executable is THIS one. So `sys.executable -m playwright install` cannot
    # work (main() would just boot a second app). Instead post_install.py
    # relaunches us with TOFU_PLAYWRIGHT_INSTALL=1, and here we drive the
    # bundled playwright package in-process to fetch its Chromium binary.
    if os.environ.get('TOFU_PLAYWRIGHT_INSTALL') == '1':
        try:
            from playwright.__main__ import main as _pw_main
            sys.argv = ['playwright', 'install', 'chromium']
            _pw_main()
        except SystemExit:
            raise
        except Exception as e:
            _log('Playwright install failed: %s' % e)
            sys.exit(1)
        return

    # ── GUI process: crisp rendering before any window is created ──
    _enable_dpi_awareness()

    port = _find_free_port()

    # Start the real server as a child process (full Hypercorn startup).
    try:
        proc = _spawn_server(port)
    except Exception as e:
        _log('FATAL: could not start server process: %s' % e)
        raise

    # Open the browser as soon as the server is reachable — in a background
    # thread so the optional first-launch prompt can run in parallel.
    url = f'http://127.0.0.1:{port}'

    def _open_when_ready():
        if _wait_for_server('127.0.0.1', port, proc):
            _log('Server ready at %s' % url)
            webbrowser.open(url)
        else:
            _log('WARNING: server did not become ready at %s' % url)

    threading.Thread(target=_open_when_ready, daemon=True,
                     name='tofu-open-browser').start()

    # ── First-launch: offer optional component downloads ──
    try:
        from desktop.post_install import is_first_launch, run_first_launch_prompt
        if is_first_launch():
            run_first_launch_prompt()
    except Exception as e:
        _log('Component prompt skipped: %s' % e)

    # Run tray on the main thread (required by macOS/Windows).
    _run_tray(port, proc)


if __name__ == '__main__':
    main()

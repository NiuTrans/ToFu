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


def _start_computer_control(port: int, state: dict) -> None:
    """Start the in-process desktop-control agent against the local server.

    Safety posture is **deny by default**: enabling computer control grants
    only READ-ONLY tools (list/read files, system overview). The write / exec /
    GUI tiers each stay OFF until the user ticks them in the tray. The agent
    reads the SAME ``permissions`` dict every poll, so toggling a tier live
    takes effect on the next command without restarting the agent.

    A ``threading.Event`` in *state* lets the tray toggle it back off cleanly.
    Replaces the old "install a second program and run python -m
    lib.desktop_agent" flow.
    """
    if state.get('thread') and state['thread'].is_alive():
        return
    try:
        from lib.desktop_agent import run_agent
        from lib.desktop_agent._permissions import safe_default
    except Exception as e:
        _log('Computer control unavailable (import failed): %s' % e)
        state['error'] = str(e)
        return

    stop_event = threading.Event()
    state['stop'] = stop_event
    state['error'] = None
    # Shared, live-mutable permissions dict. Created deny-all; the tray tier
    # toggles mutate it in place and run_agent picks up the change each poll.
    if not isinstance(state.get('perms'), dict):
        state['perms'] = safe_default()
    permissions = state['perms']

    # Where to poll. A remote attachment (tray → "Connect to remote Tofu…")
    # wins; with none configured we poll the server this app just started,
    # which is the packaged-app default and must stay untouched.
    server_url = f'http://127.0.0.1:{port}'
    bridge_secret = (os.environ.get('TOFU_BRIDGE_SECRET') or '').strip()
    try:
        from lib.desktop_agent.config import remote_server
        _rurl, _rsecret = remote_server()
        if _rurl:
            server_url, bridge_secret = _rurl, _rsecret
    except Exception as e:
        _log('Could not read remote attachment, using local server: %s' % e)
    state['server_url'] = server_url

    def _loop():
        try:
            run_agent(server_url, permissions, poll_interval=1.0,
                      bridge_secret=bridge_secret, stop_event=stop_event)
        except Exception as e:
            _log('Computer-control agent crashed: %s' % e)
            state['error'] = str(e)
        finally:
            state['enabled'] = False

    t = threading.Thread(target=_loop, daemon=True, name='tofu-desktop-agent')
    state['thread'] = t
    state['enabled'] = True
    t.start()
    _log('Computer control ENABLED (read-only; perms=%s, agent polling %s)'
         % (permissions, server_url))


def _prompt_connect_line(current_url: str = ''):
    """Ask for ONE pasted connect line; return (url, secret) or None.

    The web UI (Local Control → "This computer", remote case) renders a single
    click-to-copy line carrying BOTH the server address and the token. This
    dialog therefore takes ONE field: the user pastes what they copied and is
    done. Two separate fields would make them split the string by hand, which
    is the cognitive load the merged surface exists to remove.

    Parsing is delegated to lib.desktop_agent.config.parse_connect_line — the
    single owner of the format — so this dialog can never drift from what the
    web side emits. Returns None when the user cancels.
    """
    from lib.desktop_agent.config import parse_connect_line
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as e:
        _log('Connect dialog unavailable (no tkinter): %s' % e)
        return None

    result = {'value': None}
    root = tk.Tk()
    root.title('Connect to remote Tofu')
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=16)
    frame.grid(sticky='nsew')

    ttk.Label(frame, text='Connect this computer to a remote Tofu',
              font=('', 11, 'bold')).grid(row=0, column=0, columnspan=2,
                                          sticky='w')
    ttk.Label(frame, wraplength=430, justify='left',
              text=('In Tofu, open Local Control \u2192 This computer and press '
                    '"Generate connect line". Paste the whole line here.')
              ).grid(row=1, column=0, columnspan=2, sticky='w', pady=(6, 10))

    entry = ttk.Entry(frame, width=58)
    entry.grid(row=2, column=0, columnspan=2, sticky='we')
    if current_url:
        ttk.Label(frame, foreground='#666',
                  text='Currently attached to: %s' % current_url
                  ).grid(row=3, column=0, columnspan=2, sticky='w', pady=(6, 0))
    err = ttk.Label(frame, foreground='#b00', wraplength=430, justify='left')
    err.grid(row=4, column=0, columnspan=2, sticky='w', pady=(6, 0))

    def _ok(*_a):
        try:
            result['value'] = parse_connect_line(entry.get())
        except ValueError as ve:
            # Keep the dialog open with a specific reason — silently closing
            # would leave the user unable to tell what was wrong.
            err.config(text=str(ve))
            return
        root.destroy()

    def _cancel(*_a):
        result['value'] = None
        root.destroy()

    btns = ttk.Frame(frame)
    btns.grid(row=5, column=0, columnspan=2, sticky='e', pady=(12, 0))
    ttk.Button(btns, text='Cancel', command=_cancel).grid(row=0, column=0,
                                                          padx=(0, 8))
    ttk.Button(btns, text='Connect', command=_ok).grid(row=0, column=1)
    entry.bind('<Return>', _ok)
    root.bind('<Escape>', _cancel)
    entry.focus_set()

    try:
        root.mainloop()
    except Exception as e:
        _log('Connect dialog failed: %s' % e)
        return None
    return result['value']


def _stop_computer_control(state: dict) -> None:
    """Signal the in-process desktop-control agent to stop at the next poll."""
    ev = state.get('stop')
    if ev is not None:
        ev.set()
    state['enabled'] = False
    _log('Computer control DISABLED')


def _run_tray(port: int, proc: subprocess.Popen):
    """Run the system tray icon (blocks on the main thread)."""
    url = f'http://127.0.0.1:{port}'
    # Desktop-control agent state (started/stopped via the tray toggle below).
    # 'perms' is the live, deny-all-by-default permissions dict shared with the
    # agent loop; the per-tier toggles mutate it in place.
    _cc_state: dict = {'enabled': False, 'thread': None, 'stop': None,
                       'error': None, 'perms': None}

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

    def _attached_url() -> str:
        """The remote server this app is attached to, or '' when local-only."""
        try:
            from lib.desktop_agent.config import remote_server
            return remote_server()[0]
        except Exception as e:
            _log('Could not read remote attachment: %s' % e)
            return ''

    def on_connect_remote(icon, item):
        """Paste a connect line to attach this computer to a remote Tofu."""
        parsed = _prompt_connect_line(_attached_url())
        if parsed is None:
            return
        url, secret = parsed
        try:
            from lib.desktop_agent.config import save_remote_server
            save_remote_server(url, secret)
        except Exception as e:
            _log('Could not save remote attachment: %s' % e)
            return
        _log('Attached to remote Tofu at %s' % url)
        # Re-point a RUNNING agent: it captured the old address when it
        # started, so without this the user would have to toggle it off and on
        # (and would get no hint that they must).
        if _cc_state.get('enabled'):
            _stop_computer_control(_cc_state)
            _start_computer_control(port, _cc_state)
        try:
            icon.update_menu()
        except Exception as e:
            _log('Could not refresh tray menu after connect: %s' % e)

    def on_toggle_computer_control(icon, item):
        """Enable/disable the in-process desktop-control agent."""
        if _cc_state.get('enabled'):
            _stop_computer_control(_cc_state)
        else:
            _start_computer_control(port, _cc_state)
        try:
            icon.update_menu()
        except Exception as e:
            _log('Could not refresh tray menu after CC toggle: %s' % e)

    def _toggle_perm(key: str):
        """Flip one permission tier on the live shared perms dict."""
        def _handler(icon, item):
            perms = _cc_state.get('perms')
            if not isinstance(perms, dict):
                # Not enabled yet — ticking a tier has nothing to mutate.
                return
            perms[key] = not perms.get(key)
            _log('Computer control tier %s -> %s' % (key, perms[key]))
            try:
                icon.update_menu()
            except Exception as e:
                _log('Could not refresh tray menu after perm toggle: %s' % e)
        return _handler

    def _perm_checked(key: str):
        return lambda item: bool((_cc_state.get('perms') or {}).get(key))

    def _perm_enabled(item):
        # Tier toggles are only meaningful while the agent is running.
        return bool(_cc_state.get('enabled'))

    def on_quit(icon, item):
        _stop_computer_control(_cc_state)
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
        MenuItem('Enable Computer Control', on_toggle_computer_control,
                 checked=lambda item: bool(_cc_state.get('enabled'))),
        MenuItem('Permissions', pystray.Menu(
            MenuItem('Allow file writes', _toggle_perm('allow_write'),
                     checked=_perm_checked('allow_write'), enabled=_perm_enabled),
            MenuItem('Allow run commands / open apps', _toggle_perm('allow_exec'),
                     checked=_perm_checked('allow_exec'), enabled=_perm_enabled),
            MenuItem('Allow mouse / keyboard / screenshot', _toggle_perm('allow_gui'),
                     checked=_perm_checked('allow_gui'), enabled=_perm_enabled),
        )),
        MenuItem('Connect to remote Tofu…', on_connect_remote),
        MenuItem('Install Components...', on_components),
        # Which server the agent talks to. Silence here was a real gap: after
        # pasting a connect line the user had no way to tell it took effect.
        MenuItem(lambda item: ('Server: %s' % (_attached_url() or
                                               f'this computer (port {port})')),
                 None, enabled=False),
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
    #
    # --only-shell matches install.sh + post_install.py: a default install
    # also fetches the 175 MB full build, which no call site here launches
    # (every one is headless).
    if os.environ.get('TOFU_PLAYWRIGHT_INSTALL') == '1':
        try:
            from playwright.__main__ import main as _pw_main
            sys.argv = ['playwright', 'install', '--only-shell', 'chromium']
            _pw_main()
        except SystemExit:
            raise
        except Exception as e:
            _log('Playwright install failed: %s' % e)
            sys.exit(1)
        return

    # ── Smoke mode (CI: prove the frozen bundle can actually START) ──
    # The release gates check that an artifact EXISTS and is of a plausible
    # SIZE. Neither can see a missing hidden import: tofu.spec declares 48 of
    # them, and dropping one changes no byte count and no PyInstaller exit
    # code — it fails at the moment the user double-clicks, with a
    # ModuleNotFoundError nobody is around to read.
    #
    # So CI runs the built binary once with TOFU_SMOKE=1. This imports
    # `server`, which at MODULE level constructs the Quart app and calls
    # routes.register_all(app) — so a single import exercises the whole
    # blueprint tree and the transitive dependency graph the spec's
    # hiddenimports exist to preserve. Then it exits.
    #
    # ── The verdict is the EXIT CODE, deliberately, and this is the trap ──
    # "the process stayed alive for N seconds" is NOT a usable signal for this
    # build: `console=False` means a windowed binary detaches and lingers
    # regardless of whether anything imported, so a liveness check is green by
    # construction — a guard measuring the wrong thing. An exit code cannot be
    # faked that way: 0 only happens if every import resolved.
    #
    # No socket is bound and no server is served: binding would make the check
    # sensitive to port contention on shared CI runners, which is noise about
    # the environment rather than evidence about the bundle.
    if os.environ.get('TOFU_SMOKE') == '1':
        try:
            import server as _server
            app = getattr(_server, 'app', None)
            if app is None:
                raise RuntimeError('server module exposes no `app` object')
            # register_all() ran at import; an empty blueprint map would mean
            # the app booted hollow, which an import-only check would
            # otherwise call success.
            n = len(getattr(app, 'blueprints', {}) or {})
            if n == 0:
                raise RuntimeError('no blueprints registered on the app')
            sys.stdout.write('TOFU_SMOKE_OK version=%s blueprints=%d\n'
                             % (_local_version() or 'unknown', n))
            sys.stdout.flush()
        except BaseException:
            # Print the real traceback to stderr and fail loudly. CI asserts
            # BOTH exit==0 and an empty stderr, so a degraded-but-surviving
            # import cannot pass as healthy.
            import traceback
            traceback.print_exc()
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

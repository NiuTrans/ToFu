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

from runtime_guards import install_process_resource_defaults  # noqa: E402
from tofu_dotenv import load_dotenv_file  # noqa: E402

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
            from lib.log_retention import register_external_log
            register_external_log(_LOG_PATH, 'desktop_console')
            _log_fh = open(_LOG_PATH, 'a', encoding='utf-8', buffering=1)
        except OSError:
            _log_fh = False  # give up on the file, still try stderr
        except Exception:
            # A retention helper failure must not make a windowed launcher
            # silent; the open below remains its last-resort diagnostic sink.
            try:
                _log_fh = open(_LOG_PATH, 'a', encoding='utf-8', buffering=1)
            except OSError:
                _log_fh = False
    try:
        from lib.log_redaction import redact_text
        line = redact_text(line, max_chars=4096)
    except Exception:
        pass
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

    Must run before any window is created. No-op off Windows. The 3-level
    fallback lives in desktop._tk_theme (single theme source) — this used to
    be one of TWO private copies and the post_install one lacked the v2 level.
    """
    from desktop import _tk_theme as theme
    theme.ensure_dpi_awareness(_log)


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
    load_dotenv_file(os.path.join(BASE_DIR, '.env'), env)
    env['TOFU_PROJECT_PATH'] = _EXE_DIR
    install_process_resource_defaults(env)
    env['PORT'] = str(port)
    env['BIND_HOST'] = '127.0.0.1'
    # Plain HTTP on loopback — no self-signed cert warnings for a local app.
    env['TOFU_TLS'] = '0'
    # The desktop launcher deliberately owns and monitors this child. Bypass
    # the project-local manager handoff so the tray retains a real process
    # handle and Quit can stop exactly the server it launched.
    env['TOFU_SERVER_WORKER'] = '1'
    env['TOFU_MANAGED_BY'] = 'desktop'
    env['TOFU_EXTERNAL_CONSOLE_LOG'] = _LOG_PATH
    env['TOFU_EXTERNAL_CONSOLE_STREAM'] = 'desktop_console'

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
        try:
            from lib.log_retention import register_external_log
            register_external_log(_LOG_PATH, 'desktop_console')
        except Exception:
            pass
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

    # Where to poll. An installer-provisioned remote attachment wins; with
    # none configured we poll the server this app just started, which is the
    # packaged-app default and must stay untouched.
    server_url = f'http://127.0.0.1:{port}'
    from lib.bridge_auth import process_agent_token
    bridge_secret = process_agent_token()
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


def _stop_computer_control(state: dict) -> None:
    """Signal the in-process desktop-control agent to stop at the next poll."""
    ev = state.get('stop')
    if ev is not None:
        ev.set()
    state['enabled'] = False
    _log('Computer control DISABLED')


def _persist_cc_state(state: dict) -> None:
    """Write the tray's computer-control state to the agent config.

    Called ONLY from explicit user clicks (the enable toggle, a permission
    tier) — never from quit/startup paths, so a crash or a normal Quit
    cannot erase what the user chose. Failures are logged, never fatal:
    a read-only config dir must not break the toggle itself.
    """
    try:
        from lib.desktop_agent.config import save_computer_control
        save_computer_control(bool(state.get('enabled')),
                              state.get('perms') or {})
    except Exception as e:
        _log('Could not persist computer-control state: %s' % e)


def _restore_cc_state(state: dict) -> bool:
    """Load the persisted computer-control state into *state*.

    Returns whether the agent should be auto-started. A fresh install (or
    a malformed blob) yields False and leaves *state* untouched —
    deny-by-default is the floor this restore never lowers. Saved perms
    are merged over the deny-all baseline so a tier added by a future
    version still defaults OFF for old config files.
    """
    try:
        from lib.desktop_agent.config import load_computer_control
        enabled, saved_perms = load_computer_control()
    except Exception as e:
        _log('Could not read computer-control state: %s' % e)
        return False
    if saved_perms:
        try:
            from lib.desktop_agent._permissions import safe_default
            perms = safe_default()
            perms.update(saved_perms)
            state['perms'] = perms
        except Exception as e:
            _log('Could not merge saved permissions: %s' % e)
    return enabled


def _run_tray(port: int, proc: subprocess.Popen):
    """Run the system tray icon (blocks on the main thread)."""
    url = f'http://127.0.0.1:{port}'
    # Desktop-control agent state (started/stopped via the tray toggle below).
    # 'perms' is the live, deny-all-by-default permissions dict shared with the
    # agent loop; the per-tier toggles mutate it in place.
    _cc_state: dict = {'enabled': False, 'thread': None, 'stop': None,
                       'error': None, 'perms': None}

    # Restore the user's persisted choice: an agent they explicitly enabled
    # comes back up on every launch; a fresh install stays OFF.
    if _restore_cc_state(_cc_state):
        _log('Computer control was enabled at last exit — restoring')
        _start_computer_control(port, _cc_state)

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
        """Launch the component manager (select + progress-visible install)."""
        try:
            from desktop.post_install import OPTIONAL_COMPONENTS, _prompt_gui
        except Exception as e:
            _log('Component installer unavailable: %s' % e)
            return
        not_installed = [c for c in OPTIONAL_COMPONENTS if not c.is_installed()]
        if not not_installed:
            return
        # The dialog hosts the install itself (progress rows + bar) and
        # returns the results; nothing downloads invisibly anymore. The
        # DIALOG rides the tk host thread (tray-first topology); the rest
        # of this handler stays on the tray thread.
        from desktop import _tk_host
        results = _tk_host.call(lambda: _prompt_gui(not_installed))
        for name, success, msg in results:
            _log('%s %s: %s' % ('OK' if success else 'FAIL', name, msg))

    def _attached_url() -> str:
        """The remote server this app is attached to, or '' when local-only."""
        try:
            from lib.desktop_agent.config import remote_server
            return remote_server()[0]
        except Exception as e:
            _log('Could not read remote attachment: %s' % e)
            return ''

    def on_toggle_computer_control(icon, item):
        """Enable/disable the in-process desktop-control agent."""
        if _cc_state.get('enabled'):
            _stop_computer_control(_cc_state)
        else:
            _start_computer_control(port, _cc_state)
        _persist_cc_state(_cc_state)
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
            _persist_cc_state(_cc_state)
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

    # Tray strings are bilingual (desktop.tray.* in _tk_theme) — the AST
    # ratchet in tests/test_desktop_tray_i18n.py refuses a hardcoded literal.
    from desktop import _tk_theme as theme
    _lang = theme.detect_lang()

    def _tt(key: str, **fill) -> str:
        text = theme.t(key, _lang)
        for ph, val in fill.items():
            text = text.replace('{%s}' % ph, str(val))
        return text

    # ── Role window / control panel (desktop/role_window.py) ──
    # The window delegates every mutation to the SAME handlers the tray
    # uses (a second VIEW over _cc_state, not a new state path), so the
    # two surfaces can never disagree. Tray callbacks expect an icon
    # with update_menu(); window-driven actions have no tray to
    # refresh, hence the null shim.
    class _NullIcon:
        @staticmethod
        def update_menu():
            pass

    _NULL_ICON = _NullIcon()

    def _role_state_fn():
        from desktop import role_window
        return role_window.role_state_full(
            port, _cc_state, _attached_url(),
            show_flag=role_window.should_show_at_startup())

    _role_actions = {
        'open': lambda: webbrowser.open(url),
        'toggle_cc': lambda: on_toggle_computer_control(_NULL_ICON, None),
        'toggle_perm': lambda key: _toggle_perm(key)(_NULL_ICON, None),
    }

    def on_control_panel(icon, item):
        from desktop import _tk_host, role_window
        _tk_host.post_or_call(lambda: role_window.show_role_window(
            'full', _role_state_fn, _role_actions, log=_log))

    # Dynamic "update available" item: its text is computed at menu-open time
    # and it is hidden entirely until the background check finds a newer tag.
    menu = pystray.Menu(
        MenuItem(_tt('desktop.tray.open'), on_open, default=True),
        MenuItem(_tt('desktop.tray.controlPanel'), on_control_panel),
        MenuItem(lambda item: _tt('desktop.tray.downloadUpdate',
                                  tag=_update['tag']),
                 on_update,
                 visible=lambda item: bool(_update['tag'])),
        pystray.Menu.SEPARATOR,
        MenuItem(_tt('desktop.tray.enableCC'), on_toggle_computer_control,
                 checked=lambda item: bool(_cc_state.get('enabled'))),
        MenuItem(_tt('desktop.tray.permissions'), pystray.Menu(
            MenuItem(_tt('desktop.tray.permWrite'), _toggle_perm('allow_write'),
                     checked=_perm_checked('allow_write'), enabled=_perm_enabled),
            MenuItem(_tt('desktop.tray.permExec'), _toggle_perm('allow_exec'),
                     checked=_perm_checked('allow_exec'), enabled=_perm_enabled),
            MenuItem(_tt('desktop.tray.permGui'), _toggle_perm('allow_gui'),
                     checked=_perm_checked('allow_gui'), enabled=_perm_enabled),
        )),
        MenuItem(_tt('desktop.tray.installComponents'), on_components),
        # Which server the agent talks to. Keep attachment state observable
        # even though the installer configures it without user input.
        MenuItem(lambda item: _tt('desktop.tray.serverLabel',
                                  url=_attached_url() or
                                  _tt('desktop.tray.serverLocal', port=port)),
                 None, enabled=False),
        pystray.Menu.SEPARATOR,
        MenuItem(_tt('desktop.tray.quit'), on_quit),
    )

    icon = pystray.Icon('tofu', icon_image, 'Tofu', menu)

    # Startup role declaration (owner directive 2026-08-03), now TRAY-FIRST
    # (owner report 2026-08-04): the tray must exist BEFORE the window can
    # ever hide, or "minimize to tray" strands the app with no surface. The
    # tk host thread owns the window; the main thread reaches icon.run()
    # immediately. Off win32 host.start() returns False and post_or_call
    # inlines — the legacy window-then-tray sequence stands there.
    from desktop import _tk_host, role_window
    _tk_host.start(log=_log)
    if role_window.should_show_at_startup():
        _tk_host.post_or_call(lambda: role_window.show_role_window(
            'full', _role_state_fn, _role_actions, log=_log))

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


def _import_preseed() -> None:
    """Import a server-baked ``preseed_server.json`` into the attachment.

    The implementation lives in desktop/connect_ui.py (shared with the
    agent-only build). ``_EXE_DIR`` is read at CALL time — the preseed
    suite monkeypatches it to an isolated dir.
    """
    from desktop.connect_ui import import_preseed
    import_preseed(_EXE_DIR, _log)


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

    # ── First-launch: import a server-baked preseed (if any), then offer
    #    optional component downloads. Preseed first: it decides WHICH
    #    server this machine attaches to, and the component prompt is
    #    unrelated to it.
    try:
        _import_preseed()
    except Exception as e:
        _log('Preseed import skipped: %s' % e)
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

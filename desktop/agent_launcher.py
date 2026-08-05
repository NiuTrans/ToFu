#!/usr/bin/env python3
"""Tofu Agent Launcher — the controlled-machine component (TofuAgent.exe).

The agent-only packaged app: NO Quart server, NO browser auto-open, NO
database, NO component manager, NO GitHub update check. The machine's only
role is to be controlled by a Tofu server — the agent loop IS this process.

Four acts (docs/DESKTOP_AGENT_DIST_DESIGN.md §4.1):

  1. import the installer's preseed (server address, one-shot, non-secret);
  2. ensure an attachment — the shared connect-line dialog, once, then
     persisted (cancel ⇒ exit 0: an agent with nothing to poll is nothing);
  3. rebuild the permission floor — persisted tiers over deny-all;
  4. run the agent in a thread + a minimal tray on the main thread.

The tray is the whole "configuration capability": Server label, Connect…,
the four permission tiers, Start with Windows, Quit.

Two disciplines copied from desktop/launcher.py because they bit us there:

  * windowed builds have no console — sys.stdout/sys.stderr may be None,
    so ALL diagnostics go through the null-safe _log file tee;
  * DPI awareness must be declared BEFORE any window (tray / tk dialog).
"""

import os
import sys
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

# ── Resolve base directory (frozen vs source) — same contract as launcher.py ──
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.join(os.path.dirname(sys.executable), '_internal')
    if not os.path.isdir(BASE_DIR):
        BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

_EXE_DIR = (os.path.dirname(sys.executable)
            if getattr(sys, 'frozen', False) else BASE_DIR)
DATA_DIR = os.path.join(_EXE_DIR, 'data')
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass

os.environ.setdefault('TOFU_DATA_DIR', DATA_DIR)

# ── Null-safe diagnostics (windowed builds have no console) ──
_LOG_PATH = os.path.join(DATA_DIR, 'desktop-agent.log')
_log_fh = None


def _log(msg: str) -> None:
    """Write a diagnostic line without ever touching a None stream."""
    line = '[TofuAgent] %s\n' % msg
    global _log_fh
    if _log_fh is None:
        try:
            _log_fh = open(_LOG_PATH, 'a', encoding='utf-8', buffering=1)
        except OSError:
            _log_fh = False
    try:
        if _log_fh:
            _log_fh.write(line)
    except OSError:
        pass
    try:
        if sys.stderr is not None:
            sys.stderr.write(line)
    except Exception:
        pass


def _agent_version() -> str:
    try:
        from lib.version import __version__ as v
        return (v or '').strip()
    except Exception as e:
        _log('Could not read version: %s' % e)
        return ''


def _enable_dpi_awareness() -> None:
    try:
        from desktop import _tk_theme as theme
        theme.ensure_dpi_awareness(_log)
    except Exception as e:
        _log('DPI awareness setup failed: %s' % e)


def _load_icon():
    """Tray icon from the bundled static/icons (falls back to a square)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    for name in ('tofu.ico', 'tofu.icns', 'logo.png'):
        icon_path = os.path.join(BASE_DIR, 'static', 'icons', name)
        if os.path.isfile(icon_path):
            try:
                return Image.open(icon_path)
            except Exception as e:
                _log('Icon load failed for %s: %s' % (name, e))
                continue
    try:
        return Image.new('RGBA', (64, 64), (255, 215, 0, 255))
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
#  Boot autostart (owner amendment ① — a relay machine must survive reboots)
#  Per-user HKCU Run value: UAC-free, matches the per-user install.
#  Windows-only in v1 (the honest boundary: §6 of the design doc).
# ═══════════════════════════════════════════════════════════════════
_RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
_RUN_VALUE = 'TofuAgent'


def _autostart_supported() -> bool:
    return sys.platform.startswith('win')


def _autostart_get() -> bool:
    """Whether the HKCU Run value for this app currently exists."""
    if not _autostart_supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _RUN_VALUE)
        return True
    except OSError:
        return False
    except Exception as e:
        _log('Autostart probe failed: %s' % e)
        return False


def _autostart_apply(enabled: bool) -> None:
    """Write/delete the HKCU Run value. Failures are logged, never fatal."""
    if not _autostart_supported():
        return
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            if enabled:
                winreg.SetValueEx(k, _RUN_VALUE, 0, winreg.REG_SZ,
                                  '"%s"' % sys.executable)
            else:
                try:
                    winreg.DeleteValue(k, _RUN_VALUE)
                except OSError:
                    pass
        _log('Autostart %s' % ('ENABLED' if enabled else 'DISABLED'))
    except OSError as e:
        _log('Autostart registry write failed: %s' % e)
        logger.warning('Autostart registry write failed: %s', e)


def _reconcile_autostart() -> None:
    """Reconcile the persisted choice with the registry, both directions.

    config carries 'autostart' → apply it (the choice survives reinstall:
    a fresh installer's default-ON task must not override a user who
    turned it off). No config key yet (first run) → persist what the
    installer wrote, so the tray checkbox shows the truth.
    """
    try:
        from lib.desktop_agent.config import load_config, save_config
        cfg = load_config()
        if 'autostart' in cfg:
            _autostart_apply(bool(cfg.get('autostart')))
        else:
            cfg['autostart'] = _autostart_get()
            save_config(cfg)
    except Exception as e:
        _log('Autostart reconcile failed: %s' % e)
        logger.warning('Autostart reconcile failed: %s', e)


def _persist_autostart(enabled: bool) -> None:
    try:
        from lib.desktop_agent.config import load_config, save_config
        cfg = load_config()
        cfg['autostart'] = bool(enabled)
        save_config(cfg)
    except Exception as e:
        _log('Autostart persist failed: %s' % e)
        logger.warning('Autostart persist failed: %s', e)


# ═══════════════════════════════════════════════════════════════════
#  Smoke gate (CI / server build): exit code is the verdict.
#  Mirrors launcher's TOFU_SMOKE discipline — a windowed binary lingers
#  whether or not anything imported, so liveness proves nothing.
# ═══════════════════════════════════════════════════════════════════
def _smoke_main() -> None:
    try:
        import lib.desktop_agent as agent_pkg
        n = len(getattr(agent_pkg, 'COMMANDS', {}) or {})
        if n == 0:
            raise RuntimeError('empty dispatch table — the agent would '
                               'poll but execute nothing')
        # The size claim of this build is an import-graph fact: the server
        # stack must be ABSENT from both the loaded modules and the bundle
        # tree. If a future change drags it back in, the BUILD goes red —
        # not the user's machine.
        _BANNED = ('quart', 'flask', 'hypercorn', 'psycopg2', 'playwright',
                   'trafilatura')
        for banned in _BANNED:
            if banned in sys.modules:
                raise RuntimeError(
                    'server-stack module leaked into the agent closure: %s'
                    % banned)
        bundle_root = getattr(sys, '_MEIPASS', None)
        if bundle_root:
            for banned in _BANNED + ('fitz', 'server.py'):
                if os.path.exists(os.path.join(bundle_root, banned)):
                    raise RuntimeError(
                        'server-stack payload in the agent bundle: %s'
                        % banned)
        import tkinter  # noqa: F401 — the connect dialog's toolkit must ship
        import desktop.role_window  # noqa: F401 — the control panel must ship
        sys.stdout.write('TOFU_AGENT_SMOKE_OK version=%s commands=%d\n'
                         % (_agent_version() or 'unknown', n))
        sys.stdout.flush()
    except BaseException:
        import traceback
        traceback.print_exc()
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
#  Agent lifecycle + tray
# ═══════════════════════════════════════════════════════════════════
def _start_agent(state: dict, perms: dict) -> None:
    """Start the polling agent in a daemon thread (the tray stays main)."""
    from lib.desktop_agent import run_agent
    stop = threading.Event()
    state['stop'] = stop
    url, secret = state['url'], state['secret']

    def _on_status(st: dict) -> None:
        state['last_status'] = st
        _log('Link status: %s' % st)

    def _loop():
        try:
            run_agent(url, perms, poll_interval=1.0,
                      bridge_secret=secret, stop_event=stop,
                      on_status=_on_status)
        except Exception as e:
            _log('Agent loop crashed: %s' % e)
            logger.error('Agent loop crashed: %s', e, exc_info=True)

    t = threading.Thread(target=_loop, daemon=True, name='tofu-agent')
    state['thread'] = t
    t.start()
    _log('Agent started (polling %s, perms=%s)' % (url, perms))


def _restart_agent(state: dict, perms: dict) -> None:
    """Re-point the agent at a new attachment (stop → join → start)."""
    ev = state.get('stop')
    if ev is not None:
        ev.set()
    t = state.get('thread')
    if t and t.is_alive():
        t.join(timeout=5)
    _start_agent(state, perms)


def _link_status_text(state: dict) -> str:
    """The tray link line's ``{status}`` fill — one readable phrase per state.

    States come from run_agent's on_status transitions; anything unknown
    (including 'error', which carries a free-form detail) renders as the
    detail itself so the menu never shows a raw code.
    """
    from desktop import _tk_theme as theme
    lang = theme.detect_lang()
    st = state.get('last_status') or {}
    code = st.get('state')
    if not code:
        return theme.t('desktop.tray.stStarting', lang)
    if code == 'ok':
        return theme.t('desktop.tray.stOk', lang)
    if code == 'auth':
        return theme.t('desktop.tray.stAuth', lang)
    if code == 'proxy':
        return theme.t('desktop.tray.stProxy', lang)
    if code == 'unreachable':
        return theme.t('desktop.tray.stUnreachable', lang)
    if code == 'http':
        return theme.t('desktop.tray.stHttp', lang).replace(
            '{code}', str(st.get('code') or '?'))
    if code == 'error':
        # The detail is a raw exception string (dynamic): it stays verbatim
        # behind a localized prefix — the sweep's rule for dynamic details.
        return theme.t('desktop.tray.stError', lang).replace(
            '{detail}', str(st.get('detail') or '?')[:80])
    return str(st.get('detail') or code)[:80]


def _run_tray(state: dict, perms: dict) -> None:
    """The minimal tray: the whole configuration surface of this component."""
    from desktop.connect_ui import prompt_attachment_flow
    from lib.desktop_agent.config import save_remote_server, \
        save_computer_control

    try:
        import pystray
        from pystray import MenuItem
    except ImportError:
        # Headless fallback: the agent thread is already running — just wait.
        _log('pystray unavailable — running headless (no tray)')
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            ev = state.get('stop')
            if ev is not None:
                ev.set()
        return

    def on_connect(icon, item):
        # Only the DIALOG rides the tk host thread; the save/restart/menu
        # refresh below stay on the tray thread (icon.update_menu is not
        # thread-safe to call from the host). host.call inlines when the
        # host is down or the caller is already on it (role-window path).
        from desktop import _tk_host
        parsed = _tk_host.call(
            lambda: prompt_attachment_flow(state.get('url') or '', log=_log))
        if parsed is None:
            return
        url, secret = parsed
        try:
            save_remote_server(url, secret)
        except Exception as e:
            _log('Could not save attachment: %s' % e)
            logger.warning('Could not save attachment: %s', e)
            return
        state['url'], state['secret'] = url, secret
        _log('Attached to %s' % url)
        _restart_agent(state, perms)
        try:
            icon.update_menu()
        except Exception as e:
            _log('Menu refresh failed after connect: %s' % e)

    def _toggle_perm(key: str):
        def _handler(icon, item):
            perms[key] = not perms.get(key)
            _log('Permission tier %s -> %s' % (key, perms[key]))
            try:
                save_computer_control(True, perms)
            except Exception as e:
                _log('Could not persist permissions: %s' % e)
                logger.warning('Could not persist permissions: %s', e)
            try:
                icon.update_menu()
            except Exception as e:
                _log('Menu refresh failed after perm toggle: %s' % e)
        return _handler

    def _perm_checked(key: str):
        return lambda item: bool(perms.get(key))

    def on_toggle_autostart(icon, item):
        enabled = not _autostart_get()
        _autostart_apply(enabled)
        _persist_autostart(enabled)
        try:
            icon.update_menu()
        except Exception as e:
            _log('Menu refresh failed after autostart toggle: %s' % e)

    def on_quit(icon, item):
        ev = state.get('stop')
        if ev is not None:
            ev.set()
        icon.stop()
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
    # uses, so the two surfaces can never disagree about what a click
    # does. Tray callbacks expect an icon with update_menu(); window-
    # driven actions have no tray to refresh, hence the null shim.
    class _NullIcon:
        @staticmethod
        def update_menu():
            pass

    _NULL_ICON = _NullIcon()

    def _role_state_fn():
        from desktop import role_window
        autostart = (_autostart_get() if _autostart_supported() else None)
        return role_window.role_state_agent(
            state.get('url') or '', perms, autostart,
            show_flag=role_window.should_show_at_startup())

    _role_actions = {
        'toggle_perm': lambda key: _toggle_perm(key)(_NULL_ICON, None),
        'connect': lambda: on_connect(_NULL_ICON, None),
        'toggle_autostart': lambda: on_toggle_autostart(_NULL_ICON, None),
    }

    def on_control_panel(icon, item):
        from desktop import _tk_host, role_window
        _tk_host.post_or_call(lambda: role_window.show_role_window(
            'agent', _role_state_fn, _role_actions, log=_log))

    menu = pystray.Menu(
        MenuItem(_tt('desktop.tray.controlPanel'), on_control_panel,
                 default=True),
        pystray.Menu.SEPARATOR,
        # Which server this machine answers to — the silence gap the full
        # app's tray already fixed; never leave it unverifiable.
        MenuItem(lambda item: _tt('desktop.tray.serverLabel',
                                  url=state.get('url') or
                                  _tt('desktop.tray.notAttached')),
                 None, enabled=False),
        # The link's live verdict — the line that would have told the owner
        # "the proxy is eating your polls" in five seconds instead of after
        # a server-side log dig (owner incident 2026-08-03). Transitions
        # are pushed by run_agent's on_status; the label re-reads state on
        # every menu open, so it is never more than a poll behind.
        MenuItem(lambda item: _tt('desktop.tray.linkState',
                                  status=_link_status_text(state)),
                 None, enabled=False),
        MenuItem(_tt('desktop.tray.connectDifferent'), on_connect),
        pystray.Menu.SEPARATOR,
        MenuItem(_tt('desktop.tray.permissions'), pystray.Menu(
            MenuItem(_tt('desktop.tray.permWrite'), _toggle_perm('allow_write'),
                     checked=_perm_checked('allow_write')),
            MenuItem(_tt('desktop.tray.permExec'), _toggle_perm('allow_exec'),
                     checked=_perm_checked('allow_exec')),
            MenuItem(_tt('desktop.tray.permGui'), _toggle_perm('allow_gui'),
                     checked=_perm_checked('allow_gui')),
            MenuItem(_tt('desktop.tray.permEgress'),
                     _toggle_perm('allow_egress'),
                     checked=_perm_checked('allow_egress')),
        )),
        MenuItem(_tt('desktop.tray.autostart'), on_toggle_autostart,
                 checked=lambda item: _autostart_get(),
                 visible=lambda item: _autostart_supported()),
        pystray.Menu.SEPARATOR,
        MenuItem(_tt('desktop.tray.quit'), on_quit),
    )

    icon = pystray.Icon('tofu-agent', _load_icon(), 'Tofu Agent', menu)

    # Startup role declaration (owner directive 2026-08-03), now TRAY-FIRST
    # (owner report 2026-08-04: the window vanished with no tray anywhere):
    # the old order — blocking window mainloop, THEN icon.run() — meant the
    # entire first window session had no tray at all, so "minimize to tray"
    # was structurally impossible. On Windows the tk host thread owns every
    # window and the main thread reaches icon.run() immediately; off win32
    # (macOS demands the main thread for both frameworks) host.start()
    # returns False and post_or_call inlines — the legacy sequence stands.
    from desktop import _tk_host, role_window
    _tk_host.start(log=_log)
    if role_window.should_show_at_startup():
        _tk_host.post_or_call(lambda: role_window.show_role_window(
            'agent', _role_state_fn, _role_actions, log=_log))
    icon.run()


def main():
    if os.environ.get('TOFU_AGENT_SMOKE') == '1':
        _smoke_main()
        return

    _enable_dpi_awareness()

    # ── 1. Preseed (installer-baked server address; one-shot, non-secret) ──
    from desktop.connect_ui import import_preseed, prompt_attachment_flow
    try:
        import_preseed(_EXE_DIR, _log)
    except Exception as e:
        _log('Preseed import skipped: %s' % e)

    # ── 2. Attachment (discovery ladder, then asked ONCE, then persisted) ──
    from lib.desktop_agent.config import remote_server, save_remote_server
    try:
        url, secret = remote_server()
    except Exception as e:
        _log('Could not read attachment: %s' % e)
        url, secret = '', ''
    if url:
        # Resume path (owner review 2026-08-03): a saved address may only be
        # reachable through an SSH tunnel that died with the last process
        # (atexit _reap_tunnels) — trusting it blindly polled a dead port on
        # every boot after the first, and autostart-on-install made that the
        # NORMAL case. Probe first, trust second: the ladder re-runs only
        # when the saved address is dead, and the token rides along (bearer,
        # address-independent).
        try:
            from lib.desktop_agent._pair import resume_attachment
            url, secret = resume_attachment(url, secret, log=_log)
        except Exception as e:
            _log('Attachment resume probe skipped: %s' % e)
    if not url:
        # Zero-question ladder (§11.2.1): loopback → LAN broadcast →
        # ~/.ssh/config self-tunnels. Whatever it finds pre-fills the
        # pairing dialog's address field; a miss just leaves it editable.
        discovered = ''
        try:
            from lib.desktop_agent._pair import discover
            discovered = discover(log=_log)
        except Exception as e:
            _log('Server discovery skipped: %s' % e)
        parsed = prompt_attachment_flow(discovered, log=_log)
        if parsed is None:
            _log('No server attachment configured — nothing to poll, exiting')
            return
        url, secret = parsed
        try:
            save_remote_server(url, secret)
        except Exception as e:
            _log('Could not save attachment: %s' % e)
            logger.warning('Could not save attachment: %s', e)

    # ── 3. Permission floor: persisted tiers over deny-all ──
    from lib.desktop_agent._permissions import safe_default
    perms = safe_default()
    try:
        from lib.desktop_agent.config import load_computer_control
        _enabled, saved_perms = load_computer_control()
        if saved_perms:
            perms.update(saved_perms)
    except Exception as e:
        _log('Could not read permission tiers, deny-all floor: %s' % e)

    # ── 4. Autostart reconcile (config ↔ HKCU Run value) ──
    _reconcile_autostart()

    state = {'url': url, 'secret': secret, 'stop': None, 'thread': None}
    _start_agent(state, perms)
    _run_tray(state, perms)


if __name__ == '__main__':
    main()

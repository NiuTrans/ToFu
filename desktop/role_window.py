#!/usr/bin/env python3
"""desktop/role_window.py — the startup role window + control panel.

Owner directive 2026-08-03 (docs/DESKTOP_STARTUP_ROLE_UX_DESIGN.md): the
desktop app must SAY at startup whether this machine is the server or a
controlled endpoint, and the client controls must not live ONLY in the
system tray. This module is the ONE authoring consumed by BOTH launchers
(the connect_ui.py pattern — two copies of a role sentence would drift):

* **Full app** declares 「这台电脑是 Tofu 服务器」 and hosts the computer-
  control panel (enable toggle and permission tiers).
* **Agent app** declares 「这台电脑是 Tofu 受控端」 and hosts its four
  tiers and autostart. Its server attachment is supplied by the installer.

Deliberately split so headless CI can test every fact the window shows:

* **Pure builders** — :func:`role_state_full` / :func:`role_state_agent`
  return a plain dict of localized display facts. The tests assert the
  SENTENCES, never pixels.
* **The gate** — :func:`should_show_at_startup` reads ``show_role_window``
  from the agent config blob. Absent key (= fresh install AND upgrades
  from pre-window builds) means SHOW: nobody unchecks a window they have
  never seen.
* **The renderer** — :func:`show_role_window`, tkinter imported lazily
  (headless rule), themed by ``_tk_theme.apply_theme``, one re-entrant
  instance per process: a second call lifts the existing window instead
  of stacking duplicates.

Window ownership (owner report 2026-08-04 — "minimize to tray" stranded
the app with NO tray): the renderer has two modes picked at call time.

* **Host-backed** (``_tk_host.parent_or_none()`` non-None — the Windows
  tray-first topology): the window is a Toplevel of the host root, this
  function returns immediately, and BOTH the title-bar minimize button
  and the in-window button hide it to the ALREADY-RUNNING tray.
* **Standalone** (legacy sequence / first-run / host failed): its own Tk
  + blocking mainloop, exactly the old behaviour — minimizing to a tray
  that does not exist yet would strand the app, so the interception stays
  off.

The renderer never owns state: it pulls fresh facts from ``state_fn`` on
every refresh and delegates every mutation to ``actions`` — the same
seams the tray menus already call, so window and tray can never disagree
about what a click does.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def _noop_log(_msg: str) -> None:
    pass


# ═══════════════════════════════════════════════════════════════
#  The startup gate
# ═══════════════════════════════════════════════════════════════

def should_show_at_startup() -> bool:
    """Whether the role window appears at launch.

    ABSENT key → True. That covers fresh installs AND every upgrade from
    a build predating this window — both are users who have never seen
    the panel, which is exactly the audience the window exists for. Any
    read failure also means True: a hidden panel is the failure mode this
    module was written to kill.
    """
    try:
        from lib.desktop_agent.config import load_config
        value = load_config().get('show_role_window')
        return True if value is None else bool(value)
    except Exception as e:
        logger.warning('Could not read show_role_window, defaulting to '
                       'show: %s', e)
        return True


def persist_show_at_startup(flag: bool) -> None:
    """Persist the window's "show at startup" checkbox.

    Merges into the existing config blob (agent_id, remote_server, …) —
    a clobbering write would erase the attachment the window itself just
    displayed. Failures are logged, never fatal.
    """
    try:
        from lib.desktop_agent.config import update_config

        def _mutate(cfg):
            cfg['show_role_window'] = bool(flag)
            return cfg

        update_config(_mutate)
    except Exception as e:
        logger.warning('Could not persist show_role_window: %s', e)


# ═══════════════════════════════════════════════════════════════
#  Pure builders — every fact the window shows, headless-testable
# ═══════════════════════════════════════════════════════════════

def role_state_full(port, cc_state, attached_url, show_flag=True,
                    lang=None) -> dict:
    """Display facts for the FULL app's role window (the server side).

    ``dual_role`` is the tunnel-incident fact: a full app that is ALSO
    attached to a remote Tofu is both server and controlled endpoint, and
    the window must say so — that state was completely invisible when it
    last bit us.
    """
    from desktop import _tk_theme as theme
    lang = lang or theme.detect_lang()
    perms = (cc_state or {}).get('perms') or {}
    return {
        'kind': 'full',
        'lang': lang,
        'role': theme.t('desktop.role.serverTitle', lang),
        'server_url': 'http://127.0.0.1:%d' % port,
        'attached_url': attached_url or '',
        'dual_role': bool(attached_url),
        'cc_enabled': bool((cc_state or {}).get('enabled')),
        'perms': {k: bool(v) for k, v in perms.items()},
        'tiers': ['allow_write', 'allow_exec', 'allow_gui'],
        'show_at_startup': bool(show_flag),
    }


def role_state_agent(url, perms, autostart, show_flag=True,
                     lang=None, link_text='', link_state='') -> dict:
    """Display facts for the AGENT app's role window (the client side).

    ``autostart=None`` means the platform does not support the toggle
    (non-Windows v1) and the renderer hides the row — three-state, never
    a silent False. ``link_text`` is the tray's live link verdict (owner
    2026-08-05: a window that says "controlled by…" while the poll never
    reaches the server is a lie — the same verdict the tray carries now
    shows here too, refreshed live).
    """
    from desktop import _tk_theme as theme
    lang = lang or theme.detect_lang()
    return {
        'kind': 'agent',
        'lang': lang,
        'role': theme.t('desktop.role.agentTitle', lang),
        'server_url': url or '',
        'attached': bool(url),
        'link_text': str(link_text or ''),
        'link_state': str(link_state or ''),
        'perms': {k: bool(v) for k, v in (perms or {}).items()},
        'tiers': ['allow_write', 'allow_exec', 'allow_gui', 'allow_egress'],
        'autostart': autostart,
        'show_at_startup': bool(show_flag),
    }


# ═══════════════════════════════════════════════════════════════
#  The renderer — lazy tkinter, one re-entrant instance
# ═══════════════════════════════════════════════════════════════

_OPEN = {'root': None, 'refresh': None}

_TIER_KEYS = {
    'allow_write': 'permWrite', 'allow_exec': 'permExec',
    'allow_gui': 'permGui', 'allow_egress': 'permEgress',
}

_TIER_DESC_KEYS = {
    'allow_write': 'desktop.role.tierWriteDesc',
    'allow_exec': 'desktop.role.tierExecDesc',
    'allow_gui': 'desktop.role.tierGuiDesc',
    'allow_egress': 'desktop.role.tierEgressDesc',
}


def show_role_window(kind, state_fn, actions, log=_noop_log) -> None:
    """Show the role window; host-backed mode returns immediately.

    Args:
        kind: 'full' or 'agent' — selects the layout sections.
        state_fn: zero-arg callable returning a fresh builder dict on every
            refresh (the window never caches facts).
        actions: mutation callbacks. Shared keys: 'toggle_perm'(key),
            'connect'(). Full adds 'open'() and 'toggle_cc'(); agent adds
            'toggle_autostart'(). Every action is followed by a refresh.
        log: launcher-style diagnostic sink.

    Re-entrant: a second call while open lifts and refreshes the existing
    window — the tray's "Control panel…" item relies on this.
    """
    from desktop import _tk_theme as theme
    from desktop import _tk_host as host
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as e:
        log('Role window unavailable (no tkinter): %s' % e)
        logger.warning('Role window unavailable (no tkinter): %s', e)
        return

    existing = _OPEN.get('root')
    if existing is not None:
        try:
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            refresh = _OPEN.get('refresh')
            if refresh is not None:
                refresh()
            return
        except tk.TclError:
            _OPEN['root'] = None
            _OPEN['refresh'] = None

    parent = host.parent_or_none()

    state = state_fn()
    lang = state.get('lang') or theme.detect_lang()

    if parent is not None:
        root = tk.Toplevel(parent)
    else:
        root = tk.Tk()
    _OPEN['root'] = root
    palette = theme.apply_theme(root)
    root.title(state['role'])
    root.resizable(False, False)
    theme.set_window_icon(root)
    root.protocol('WM_DELETE_WINDOW', lambda: _close())

    frame = ttk.Frame(root, style='Tofu.TFrame', padding=(28, 24))
    frame.grid(sticky='nsew')

    # ── Header: brand + role sentence + plain-language subtitle ──
    header = ttk.Frame(frame, style='Tofu.TFrame')
    header.grid(row=0, column=0, sticky='we')
    photo = theme.load_logo_photo(root, size=48)
    if photo is not None:
        ttk.Label(header, image=photo, style='Tofu.TLabel').grid(
            row=0, column=0, rowspan=2, padx=(0, 12), sticky='n')
    ttk.Label(header, text=state['role'],
              style='Tofu.Title.TLabel').grid(row=0, column=1, sticky='w')
    sub_key = ('desktop.role.serverSub' if state['kind'] == 'full'
               else 'desktop.role.agentSub')
    ttk.Label(header, text=theme.t(sub_key, lang), wraplength=560,
              justify='left', style='Tofu.Sub.TLabel').grid(
        row=1, column=1, sticky='w', pady=(3, 0))

    body = ttk.Frame(frame, style='Tofu.TFrame')
    body.grid(row=1, column=0, sticky='we', pady=(16, 0))

    show_var = tk.BooleanVar(value=bool(state.get('show_at_startup', True)))
    tier_vars = {}

    def _act(name, *args):
        fn = actions.get(name)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            log('Role-window action %s failed: %s' % (name, e))
            logger.warning('Role-window action %s failed: %s', name, e)
        _refresh()

    def _persist():
        persist_show_at_startup(show_var.get())

    def _close():
        _persist()
        _OPEN['root'] = None
        _OPEN['refresh'] = None
        try:
            root.destroy()
        except tk.TclError:
            pass

    def _on_unmap(event):
        """Title-bar minimize = minimize to tray (host-backed mode only).

        The tray icon exists before this window ever could hide (tray-
        first topology), so converting the taskbar-iconify into a withdraw
        keeps the design-doc promise「minimizing sends it to the tray」.
        Standalone mode has no tray yet — the interception stays OFF and
        the button keeps its stock taskbar meaning.
        """
        if event.widget is not root:
            return
        try:
            if root.state() == 'iconic':
                _persist()
                root.withdraw()
        except tk.TclError:
            pass

    def _tier_tile(grid, key, st, index):
        """One compact permission tile in the two-column capability grid."""
        var = tk.BooleanVar(value=bool(st['perms'].get(key)))
        tier_vars[key] = var
        tile = tk.Frame(grid, bg=palette['bg3'],
                        highlightthickness=1,
                        highlightbackground=palette['border'],
                        highlightcolor=palette['border'])
        tile.grid(row=index // 2, column=index % 2, sticky='nsew',
                  padx=(0, 6) if index % 2 == 0 else (6, 0),
                  pady=(0, 8))
        cb = ttk.Checkbutton(
            tile, text=theme.t('desktop.tray.' + _TIER_KEYS[key], st['lang']),
            style='Tile.TCheckbutton', variable=var,
            command=lambda k=key: _act('toggle_perm', k))
        # The full app gates tiers behind the CC enable toggle (mirroring
        # the tray's _perm_enabled); the agent's tiers are always live.
        if st['kind'] == 'full' and not st['cc_enabled']:
            cb.state(['disabled'])
        cb.grid(row=0, column=0, sticky='w', padx=10, pady=(8, 1))
        ttk.Label(tile, text=theme.t(_TIER_DESC_KEYS[key], st['lang']),
                  style='TileSub.TLabel', wraplength=260,
                  justify='left').grid(row=1, column=0, sticky='w',
                                       padx=(30, 10), pady=(0, 8))

    def _hairline(parent_widget, row):
        line = tk.Frame(parent_widget, bg=palette['border'], height=1)
        line.grid(row=row, column=0, sticky='we', padx=12, pady=(4, 0))
        return row + 1

    def _refresh():
        st = state_fn()
        for child in body.winfo_children():
            child.destroy()
        tier_vars.clear()
        row = 0

        # ── Server card ──
        card = theme.card_frame(body, palette)
        card.grid(row=row, column=0, sticky='we', pady=(0, 10))
        row += 1
        ttk.Label(card, text=theme.t('desktop.role.serverCardLabel',
                                     st['lang']),
                  style='CardHead.TLabel').grid(row=0, column=0, sticky='w',
                                                padx=12, pady=(10, 0))
        url_text = st['server_url'] or theme.t('desktop.tray.notAttached',
                                               st['lang'])
        ttk.Label(card, text=url_text, style='CardName.TLabel',
                  wraplength=590, justify='left').grid(
            row=1, column=0, sticky='w', padx=12, pady=(2, 6))
        card_row = 2
        if st['kind'] == 'full' and st.get('dual_role'):
            ttk.Label(card, wraplength=580, justify='left',
                      style='CardSub.TLabel',
                      text=theme.t('desktop.role.alsoClient', st['lang'])
                      .replace('{url}', st['attached_url'])).grid(
                row=card_row, column=0, sticky='w', padx=12, pady=(0, 4))
            card_row += 1
        btns = ttk.Frame(card, style='Card.TFrame')
        btns.grid(row=card_row, column=0, sticky='w', padx=12, pady=(0, 10))
        if st['kind'] == 'full':
            ttk.Button(btns, text=theme.t('desktop.tray.open', st['lang']),
                       style='Tofu.Accent.TButton',
                       command=lambda: _act('open')).grid(row=0, column=0,
                                                          padx=(0, 8))
        elif actions.get('browser_relay'):
            ttk.Button(btns,
                       text=theme.t('desktop.tray.browserRelay', st['lang']),
                       style='Tofu.Accent.TButton',
                       command=lambda: _act('browser_relay')).grid(
                row=0, column=0, padx=(0, 8))
        # ── Computer-control card ──
        cc = theme.card_frame(body, palette)
        cc.grid(row=row, column=0, sticky='we')
        row += 1
        head = ttk.Frame(cc, style='Card.TFrame')
        head.grid(row=0, column=0, sticky='we', padx=12, pady=(10, 4))
        ttk.Label(head, text=theme.t('desktop.role.ccTitle', st['lang']),
                  style='CardName.TLabel').grid(row=0, column=0, sticky='w')
        if st['kind'] == 'full':
            ttk.Button(head,
                       text=theme.t('desktop.role.disable'
                                    if st['cc_enabled']
                                    else 'desktop.role.enable', st['lang']),
                       style='Tofu.TButton',
                       command=lambda: _act('toggle_cc')).grid(
                row=0, column=1, sticky='e', padx=(20, 0))
            head.columnconfigure(0, weight=1)
        tier_row = 1
        if st['kind'] == 'full':
            status_key = ('desktop.role.ccOn' if st['cc_enabled']
                          else 'desktop.role.ccOff')
            style = ('Status.Ok.TLabel' if st['cc_enabled']
                     else 'CardSub.TLabel')
            ttk.Label(cc, wraplength=580, justify='left', style=style,
                      text=theme.t(status_key, st['lang'])).grid(
                row=tier_row, column=0, sticky='w', padx=12, pady=(0, 4))
            tier_row += 1
        perm_grid = ttk.Frame(cc, style='Card.TFrame')
        perm_grid.grid(row=tier_row, column=0, sticky='we', padx=12,
                       pady=(4, 2))
        perm_grid.columnconfigure(0, weight=1, uniform='perm')
        perm_grid.columnconfigure(1, weight=1, uniform='perm')
        for index, key in enumerate(st['tiers']):
            _tier_tile(perm_grid, key, st, index)
        tier_row += 1
        if st['kind'] == 'agent' and st.get('autostart') is not None:
            var = tk.BooleanVar(value=bool(st['autostart']))
            ttk.Checkbutton(
                cc, text=theme.t('desktop.tray.autostart', st['lang']),
                style='Tier.TCheckbutton', variable=var,
                command=lambda: _act('toggle_autostart')).grid(
                row=tier_row, column=0, sticky='w', padx=(10, 12),
                pady=(2, 0))
            ttk.Label(cc, text=theme.t('desktop.role.autostartDesc',
                                       st['lang']),
                      style='CardSub.TLabel', wraplength=580,
                      justify='left').grid(row=tier_row + 1, column=0,
                                           sticky='w', padx=(34, 12),
                                           pady=(0, 4))
            tier_row += 2
        tier_row = _hairline(cc, tier_row)
        ttk.Label(cc, wraplength=580, justify='left', style='CardSub.TLabel',
                  text=theme.t('desktop.role.permHint', st['lang'])).grid(
            row=tier_row, column=0, sticky='w', padx=12, pady=(4, 10))

        # Sync the tier checkboxes with fresh state (a toggle may have
        # failed or been overridden by the config restore).
        for key, var in tier_vars.items():
            var.set(bool(st['perms'].get(key)))

    # ── Live link verdict (agent only) — the tray's on_status truth, on
    # the window. The 2026-08-05 incident: this window said "controlled by
    # a Tofu server" while the poll NEVER reached one (dead proxy route).
    # The row lives OUTSIDE `body` (which _refresh rebuilds on every
    # action), and a 3s tick re-pulls state_fn so a connecting/dropping
    # link shows within one beat.
    link_lbl = None
    link_dot = None
    if state['kind'] == 'agent':
        link_box = tk.Frame(frame, bg=palette['bg2'],
                            highlightthickness=1,
                            highlightbackground=palette['border'],
                            highlightcolor=palette['border'])
        link_box.grid(row=2, column=0, sticky='we', pady=(12, 0))
        link_dot = tk.Canvas(link_box, width=12, height=12,
                             bg=palette['bg2'], highlightthickness=0)
        link_dot.grid(row=0, column=0, padx=(12, 8), pady=11)
        link_lbl = ttk.Label(link_box, style='CardName.TLabel',
                             wraplength=570, justify='left')
        link_lbl.grid(row=0, column=1, sticky='w', padx=(0, 12), pady=9)
        link_box.columnconfigure(1, weight=1)

    def _tick_link():
        if link_lbl is None or _OPEN.get('root') is not root:
            return
        try:
            st = state_fn()
            code = st.get('link_state') or ''
            link_lbl.config(
                text=theme.t('desktop.tray.linkState',
                             st.get('lang') or lang)
                .replace('{status}', st.get('link_text') or '…'))
            if link_dot is not None:
                color = (palette['success'] if code == 'ok'
                         else palette['error'] if code in
                         ('auth', 'proxy', 'unreachable', 'error')
                         else palette['text3'])
                link_dot.delete('all')
                link_dot.create_oval(2, 2, 10, 10, fill=color,
                                     outline=color)
        except tk.TclError:
            return  # window already gone
        except Exception as e:
            log('Link tick failed: %s' % e)
        try:
            root.after(3000, _tick_link)
        except tk.TclError:
            pass

    # ── Bottom bar: startup gate + the dismiss action ──
    bottom = ttk.Frame(frame, style='Tofu.TFrame')
    bottom.grid(row=3, column=0, sticky='we', pady=(16, 0))
    ttk.Checkbutton(bottom,
                    text=theme.t('desktop.role.showAtStartup', lang),
                    style='Bg.TCheckbutton',
                    variable=show_var).grid(row=0, column=0, sticky='w')
    next_col = 1
    # Agent only: one click copies the dead-link evidence pack (saved
    # route / candidates / link verdict / log tail) so the user can paste
    # it back — debugging a remote controlled machine without shell
    # access was the 2026-08-06 incident's blind spot. The action only
    # BUILDS the text; the clipboard write stays here on the tk thread.
    if state['kind'] == 'agent' and actions.get('copy_diag'):
        diag_btn = ttk.Button(bottom,
                              text=theme.t('desktop.role.copyDiag', lang),
                              style='Tofu.TButton')

        def _copy_diag():
            try:
                text = actions['copy_diag']()
            except Exception as e:
                log('Diagnostics report failed: %s' % e)
                return
            try:
                root.clipboard_clear()
                root.clipboard_append(text)
            except tk.TclError as e:
                log('Clipboard set failed: %s' % e)
                return
            diag_btn.config(text=theme.t('desktop.role.copyDiagDone', lang))
            try:
                root.after(2000, lambda: diag_btn.config(
                    text=theme.t('desktop.role.copyDiag', lang)))
            except tk.TclError:
                pass

        diag_btn.config(command=_copy_diag)
        diag_btn.grid(row=0, column=next_col, sticky='e', padx=(0, 8))
        next_col += 1
    ttk.Button(bottom, text=theme.t('desktop.role.minimize', lang),
               style='Tofu.Accent.TButton',
               command=_close).grid(row=0, column=next_col, sticky='e')
    bottom.columnconfigure(0, weight=1)

    _OPEN['refresh'] = _refresh
    _refresh()
    if link_lbl is not None:
        _tick_link()
    theme.center_on_screen(root, width=680)
    if parent is not None:
        # Host-backed: the tray is already running — the title-bar
        # minimize also sends the window to it, and we never block.
        root.bind('<Unmap>', _on_unmap)
        return
    try:
        root.mainloop()
    except Exception as e:
        log('Role window failed: %s' % e)
        logger.warning('Role window failed: %s', e)
        _OPEN['root'] = None
        _OPEN['refresh'] = None

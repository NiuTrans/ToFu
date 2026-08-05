#!/usr/bin/env python3
"""desktop/connect_ui.py — the shared connect-line dialog + preseed import.

BOTH packaged components (the full desktop app and the agent-only build)
attach a machine to a Tofu server the SAME way: the user pastes one
connect line, or the installer baked a ``preseed_server.json`` next to
the exe. These two functions lived in ``desktop/launcher.py`` until the
agent build needed them too; they moved here so there is ONE authoring —
two copies of the dialog or the preseed contract would drift, and the
dialog's parser (``lib.desktop_agent.config.parse_connect_line``) is the
single owner of the wire format.

``desktop/launcher.py`` keeps thin delegating wrappers under its old
names — its call sites and test patch points are byte-identical.
"""

import os

from lib.log import get_logger

logger = get_logger(__name__)


def _noop_log(_msg: str) -> None:
    pass


def prompt_connect_line(current_url: str = '', log=_noop_log):
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
        log('Connect dialog unavailable (no tkinter): %s' % e)
        logger.warning('Connect dialog unavailable (no tkinter): %s', e)
        return None
    from desktop import _tk_theme as theme
    from desktop import _tk_host as host

    lang = theme.detect_lang()
    result = {'value': None}
    # Ride the tk host when it owns the process's windows (tray-first
    # topology): a Toplevel on the ONE root, modal via wait_window.
    # Otherwise the standalone Tk + mainloop of old.
    parent = host.parent_or_none()
    root = tk.Toplevel(parent) if parent is not None else tk.Tk()
    theme.apply_theme(root)
    root.title(theme.t('desktop.connect.title', lang))
    root.resizable(False, False)
    theme.set_window_icon(root)
    frame = ttk.Frame(root, style='Tofu.TFrame', padding=20)
    frame.grid(sticky='nsew')

    header = ttk.Frame(frame, style='Tofu.TFrame')
    header.grid(row=0, column=0, columnspan=2, sticky='w')
    photo = theme.load_logo_photo(root, size=40)
    if photo is not None:
        ttk.Label(header, image=photo, style='Tofu.TLabel').grid(
            row=0, column=0, padx=(0, 10))
    ttk.Label(header, text=theme.t('desktop.connect.heading', lang),
              style='Tofu.Title.TLabel').grid(row=0, column=1, sticky='w')
    ttk.Label(frame, wraplength=430, justify='left', style='Tofu.Sub.TLabel',
              text=theme.t('desktop.connect.instructions', lang)
              ).grid(row=1, column=0, columnspan=2, sticky='w', pady=(8, 12))

    entry = ttk.Entry(frame, width=58, style='Tofu.TEntry')
    entry.grid(row=2, column=0, columnspan=2, sticky='we')
    if current_url:
        ttk.Label(frame, style='Tofu.Sub.TLabel',
                  text=theme.t('desktop.connect.current', lang)
                  .replace('{url}', current_url)
                  ).grid(row=3, column=0, columnspan=2, sticky='w', pady=(8, 0))
    err = ttk.Label(frame, style='Tofu.Err.TLabel', wraplength=430,
                    justify='left')
    err.grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 0))

    # A connect line that cannot connect is worse than none — the agent
    # would poll a wall forever and the panel would sit on "not running"
    # with no explanation (owner incident 2026-08-03: a proxy URL whose
    # SSO edge 401s every request). Probe before saving; on failure keep
    # the dialog open with the precise reason and let a SECOND click
    # force-save (the server may legitimately be mid-restart).
    probe_state = {'armed_for': None}

    def _ok(*_a):
        try:
            parsed = parse_connect_line(entry.get())
        except ValueError as ve:
            # Keep the dialog open with a specific reason — silently closing
            # would leave the user unable to tell what was wrong. The parser
            # throws CODED refusals; the prose lives in the theme (bilingual).
            err.config(text=theme.connect_error_text(ve, lang))
            return
        raw = entry.get().strip()
        if probe_state['armed_for'] != raw:
            from lib.desktop_agent._probe import probe_server
            err.config(text=theme.t('desktop.connect.verifying', lang))
            root.update()
            ok, reason = probe_server(parsed[0])
            if not ok:
                err.config(
                    text=theme.t('desktop.connect.verifyFailed', lang)
                    .replace('{reason}', theme.reason_text(reason, lang)))
                probe_state['armed_for'] = raw
                return
        result['value'] = parsed
        root.destroy()

    def _cancel(*_a):
        result['value'] = None
        root.destroy()

    btns = ttk.Frame(frame, style='Tofu.TFrame')
    btns.grid(row=5, column=0, columnspan=2, sticky='e', pady=(14, 0))
    ttk.Button(btns, text=theme.t('desktop.connect.cancel', lang),
               style='Tofu.TButton', command=_cancel).grid(row=0, column=0,
                                                           padx=(0, 8))
    ttk.Button(btns, text=theme.t('desktop.connect.connect', lang),
               style='Tofu.Accent.TButton', command=_ok).grid(row=0, column=1)
    entry.bind('<Return>', _ok)
    root.bind('<Escape>', _cancel)
    entry.focus_set()
    theme.center_on_screen(root, width=520)

    if parent is not None:
        root.transient(parent)
        root.grab_set()
        try:
            parent.wait_window(root)
        except tk.TclError:
            pass
        return result['value']
    try:
        root.mainloop()
    except Exception as e:
        log('Connect dialog failed: %s' % e)
        logger.warning('Connect dialog failed: %s', e)
        return None
    return result['value']


# Sentinel returned by prompt_attach when the user picks the advanced
# connect-line path — the caller then opens prompt_connect_line.
PREFER_CONNECT_LINE = '__prefer_connect_line__'


def prompt_attach(server_url: str = '', log=_noop_log):
    """The pairing-first attach dialog: server address + 6-digit code.

    This is the agent's half of the pairing-code UX (§11): the panel
    mints the code, this dialog collects it (plus the address, pre-filled
    by the discovery ladder and still editable) and exchanges it for a
    bridge token — no connect line, no SSH command the user has to run
    themselves. Returns:

      * ``(url, secret)`` — paired and verified;
      * ``PREFER_CONNECT_LINE`` — the user picked the advanced path
        (paste a minted connect line instead);
      * ``None`` — cancelled.

    A failed exchange keeps the dialog open with the precise reason
    (invalid/expired code vs rate-limited vs unreachable address — three
    different fixes, so they are three different messages).
    """
    from lib.desktop_agent._pair import exchange_pair_code
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as e:
        log('Pair dialog unavailable (no tkinter): %s' % e)
        logger.warning('Pair dialog unavailable (no tkinter): %s', e)
        return None
    from desktop import _tk_theme as theme
    from desktop import _tk_host as host

    lang = theme.detect_lang()
    result = {'value': None}
    parent = host.parent_or_none()
    root = tk.Toplevel(parent) if parent is not None else tk.Tk()
    theme.apply_theme(root)
    root.title(theme.t('desktop.pair.title', lang))
    root.resizable(False, False)
    theme.set_window_icon(root)
    frame = ttk.Frame(root, style='Tofu.TFrame', padding=20)
    frame.grid(sticky='nsew')

    header = ttk.Frame(frame, style='Tofu.TFrame')
    header.grid(row=0, column=0, columnspan=2, sticky='w')
    photo = theme.load_logo_photo(root, size=40)
    if photo is not None:
        ttk.Label(header, image=photo, style='Tofu.TLabel').grid(
            row=0, column=0, padx=(0, 10))
    ttk.Label(header, text=theme.t('desktop.pair.heading', lang),
              style='Tofu.Title.TLabel').grid(row=0, column=1, sticky='w')
    ttk.Label(frame, wraplength=430, justify='left', style='Tofu.Sub.TLabel',
              text=theme.t('desktop.pair.instructions', lang)
              ).grid(row=1, column=0, columnspan=2, sticky='w', pady=(8, 12))

    ttk.Label(frame, style='Tofu.TLabel',
              text=theme.t('desktop.pair.serverLabel', lang)
              ).grid(row=2, column=0, sticky='w')
    addr = ttk.Entry(frame, width=40, style='Tofu.TEntry')
    addr.grid(row=2, column=1, sticky='we', pady=(0, 6))
    if server_url:
        addr.insert(0, server_url)

    ttk.Label(frame, style='Tofu.TLabel',
              text=theme.t('desktop.pair.codeLabel', lang)
              ).grid(row=3, column=0, sticky='w')
    code_entry = ttk.Entry(frame, width=14, style='Tofu.TEntry')
    code_entry.grid(row=3, column=1, sticky='w')

    err = ttk.Label(frame, style='Tofu.Err.TLabel', wraplength=430,
                    justify='left')
    err.grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 0))

    def _ok(*_a):
        url = addr.get().strip().rstrip('/')
        code = code_entry.get().strip()
        if not url.startswith(('http://', 'https://')):
            err.config(text=theme.t('desktop.pair.badAddress', lang))
            return
        if not (code.isdigit() and 4 <= len(code) <= 8):
            err.config(text=theme.t('desktop.pair.badCode', lang))
            return
        err.config(text=theme.t('desktop.pair.verifying', lang))
        root.update()
        ok, val = exchange_pair_code(url, code)
        if ok:
            result['value'] = (url, val)
            root.destroy()
            return
        if val == 'invalid_code':
            err.config(text=theme.t('desktop.pair.invalidCode', lang))
        elif val == 'rate_limited':
            err.config(text=theme.t('desktop.pair.rateLimited', lang))
        else:
            err.config(text=theme.t('desktop.pair.failed', lang)
                       .replace('{reason}', theme.reason_text(val, lang)))

    def _use_line(*_a):
        result['value'] = PREFER_CONNECT_LINE
        root.destroy()

    def _cancel(*_a):
        result['value'] = None
        root.destroy()

    btns = ttk.Frame(frame, style='Tofu.TFrame')
    btns.grid(row=5, column=0, columnspan=2, sticky='we', pady=(14, 0))
    ttk.Button(btns, text=theme.t('desktop.pair.useLine', lang),
               style='Tofu.TButton', command=_use_line).grid(
        row=0, column=0, sticky='w')
    ttk.Button(btns, text=theme.t('desktop.pair.cancel', lang),
               style='Tofu.TButton', command=_cancel).grid(
        row=0, column=1, sticky='e', padx=(0, 8))
    ttk.Button(btns, text=theme.t('desktop.pair.connect', lang),
               style='Tofu.Accent.TButton', command=_ok).grid(
        row=0, column=2, sticky='e')
    btns.columnconfigure(0, weight=1)
    code_entry.bind('<Return>', _ok)
    root.bind('<Escape>', _cancel)
    (code_entry if server_url else addr).focus_set()
    theme.center_on_screen(root, width=520)

    if parent is not None:
        root.transient(parent)
        root.grab_set()
        try:
            parent.wait_window(root)
        except tk.TclError:
            pass
        return result['value']
    try:
        root.mainloop()
    except Exception as e:
        log('Pair dialog failed: %s' % e)
        logger.warning('Pair dialog failed: %s', e)
        return None
    return result['value']


def prompt_attachment_flow(current_url: str = '', log=_noop_log):
    """Pairing-first, connect-line fallback — the ONE attach flow.

    Both first run (agent_launcher.main) and the tray's "Connect to a
    different Tofu…" call this so the two surfaces can never drift:
    pairing dialog first; if the user picks the advanced path, the
    legacy connect-line dialog opens instead. Returns ``(url, secret)``
    or ``None``.
    """
    parsed = prompt_attach(current_url, log=log)
    if parsed == PREFER_CONNECT_LINE:
        parsed = prompt_connect_line(current_url, log=log)
    return parsed


def import_preseed(exe_dir: str, log=_noop_log) -> None:
    """Import a server-baked ``preseed_server.json`` into the attachment.

    A server-built installer (lib/desktop_dist/winbuilder.py) bakes the
    address of the server it was built FROM next to the exe, so the first
    run attaches without the user pasting anything. Rules:

      * ONE-SHOT — the file is deleted after any attempt, so a stale
        preseed never overrides an attachment the user has since made.
      * NEVER overrides an existing attachment — the user's own connect
        wins over the install-time default.
      * NON-SECRET (the URL only) — the token still comes from the minted
        connect line or the tray dialog.
      * Any failure is logged and the file removed — a bad preseed must
        never wedge first run.
    """
    path = os.path.join(exe_dir, 'preseed_server.json')
    if not os.path.isfile(path):
        return
    try:
        import json
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        url = str(data.get('url') or '').strip()
        if not url.startswith(('http://', 'https://')):
            raise ValueError('preseed has no http(s) url')
        from lib.desktop_agent.config import remote_server, \
            save_remote_server
        existing, _secret = remote_server()
        if existing:
            log('Preseed ignored (already attached to %s)' % existing)
        else:
            save_remote_server(url, '')
            log('Preseeded remote attachment from installer: %s' % url)
    except Exception as e:
        log('Preseed import failed (ignored): %s' % e)
        logger.warning('Preseed import failed (ignored): %s', e)
    try:
        os.remove(path)
    except OSError as e:
        log('Could not remove preseed file: %s' % e)
        logger.debug('Could not remove preseed file %s: %s', path, e)

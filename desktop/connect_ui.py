#!/usr/bin/env python3
"""desktop/connect_ui.py — the shared connect-line dialog + attach imports.

BOTH packaged components (the full desktop app and the agent-only build)
attach a machine to a Tofu server the SAME way: a download-baked
``tofu-agent-attach.json`` (zero-config: route candidates + a fresh
bridge token), or the installer-baked ``preseed_server.json`` next to
the exe, or — as the always-available manual repair path — ONE pasted
connect line. These functions lived in ``desktop/launcher.py`` until the
agent build needed them too; they moved here so there is ONE authoring —
two copies of the dialog or the preseed contract would drift, and the
dialog's parser (``lib.desktop_agent.config.parse_connect_line``) is the
single owner of the wire format.

The 6-digit PAIRING-CODE dialog was removed 2026-08-05 (owner decree:
zero configuration burden — the credential rides the download, never the
user's keyboard). The server-side pair endpoints stay for
shipped-installer compat; no UI may mint or collect codes again.

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


def import_attach_bundle(exe_dir: str, log=_noop_log) -> bool:
    """Import a download-baked ``tofu-agent-attach.json`` (zero-config).

    The per-download ZIP (``/api/v1/desktop/agent-bundle``) carries this
    file NEXT TO the installer; the NSIS script adopts it next to the
    installed exe. It carries EVERYTHING the agent needs — an ordered
    route-candidate list plus the bridge token minted at download time —
    so first run attaches with zero user input (owner decree 2026-08-05:
    no pairing codes, no pasted lines).

    Probe order: the bundle's direct candidates first (a LAN address has
    no SSO edge in between), then the discovery ladder (loopback → LAN
    broadcast → ssh self-tunnel), the browser-reachable fallback LAST —
    a cloud-IDE proxy URL is a measured dead end for a cookieless agent
    (it 401s every /api/* at the edge; access.log showed zero agent
    requests, 2026-08-05). When NOTHING answers, the first candidate is
    still saved: the server may simply be off, the poll loop retries by
    itself, and the tray link line says 'unreachable' honestly.

    Discipline (mirrors import_preseed):
      * ONE-SHOT — the file carries a bearer token, so it is deleted
        after ANY attempt, success or failure;
      * NEVER overrides an existing attachment;
      * the whole route set persists as ``attach_candidates`` so
        resume_attachment can re-point a dead saved route by itself.

    Returns True when an attachment (probed or optimistic) was written.
    """
    path = os.path.join(exe_dir, 'tofu-agent-attach.json')
    if not os.path.isfile(path):
        return False
    try:
        import json
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('attach bundle is not an object')
        token = str(data.get('token') or '').strip()

        def _urls(key):
            out = []
            for u in data.get(key) or []:
                u = str(u or '').strip().rstrip('/')
                if u.startswith(('http://', 'https://')) and u not in out:
                    out.append(u)
            return out

        candidates = _urls('candidates')
        fallbacks = [u for u in _urls('fallback_candidates')
                     if u not in candidates]
        from lib.desktop_agent.config import (load_config, remote_server,
                                              save_config,
                                              save_remote_server)
        existing, _secret = remote_server()
        if existing:
            log('Attach bundle ignored (already attached to %s)' % existing)
            return False
        if not candidates and not fallbacks:
            log('Attach bundle carried no addresses — ignored')
            return False
        from lib.desktop_agent._probe import probe_server
        winner = ''
        for url in candidates:
            ok, reason = probe_server(url, timeout=2.5)
            if ok:
                winner = url
                break
            log('Attach candidate %s not reachable: %s' % (url, reason))
        if not winner:
            from lib.desktop_agent._pair import discover
            winner = discover(log=log)
        if not winner:
            for url in fallbacks:
                ok, reason = probe_server(url, timeout=2.5)
                if ok:
                    winner = url
                    break
                log('Attach fallback %s not reachable: %s' % (url, reason))
        chosen = winner or (candidates[0] if candidates else fallbacks[0])
        save_remote_server(chosen, token)
        try:
            cfg = load_config()
            cfg['attach_candidates'] = candidates + fallbacks
            save_config(cfg)
        except Exception as e:
            log('Could not persist attach candidates: %s' % e)
            logger.warning('Could not persist attach candidates: %s', e)
        if winner:
            log('Attach bundle imported: polling %s (probed alive)' % chosen)
        else:
            log('Attach bundle imported: no address answered yet — polling '
                '%s optimistically; the poll loop retries by itself' % chosen)
        return True
    except Exception as e:
        log('Attach bundle import failed (ignored): %s' % e)
        logger.warning('Attach bundle import failed (ignored): %s', e)
        return False
    finally:
        # One-shot ALWAYS: the file carries a bearer token and must not
        # linger next to the exe (same discipline as import_preseed).
        try:
            os.remove(path)
        except OSError as e:
            log('Could not remove attach bundle %s: %s' % (path, e))
            logger.debug('Could not remove attach bundle %s: %s', path, e)


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

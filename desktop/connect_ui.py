#!/usr/bin/env python3
"""desktop/connect_ui.py — installer attachment imports and legacy repair UI.

BOTH packaged components attach a machine to a Tofu server from installer
data: ``tofu-agent-attach.json`` carries route candidates and a fresh bridge
token, while ``preseed_server.json`` is the older URL-only fallback. The
connect-line parser/dialog remains callable only for compatibility with
already-shipped builds; current launchers expose no token, pairing-code, or
paste workflow to the user.

The 6-digit PAIRING-CODE dialog was removed 2026-08-05 (owner decree:
zero configuration burden — the credential rides the download, never the
user's keyboard). The server-side pair endpoints stay for
shipped-installer compat; no UI may mint or collect codes again.

"""

import os

from lib.log import get_logger

logger = get_logger(__name__)


def _noop_log(_msg: str) -> None:
    pass


def prompt_connect_line(current_url: str = '', log=_noop_log):
    """Legacy repair dialog retained for already-shipped callers.

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
    """Import installer-baked ``tofu-agent-attach.json`` (zero-config).

    ``/api/v1/desktop/agent-installer`` embeds this record inside the EXE;
    the NSIS script extracts it into the installation directory. It carries
    EVERYTHING the agent needs — an ordered
    route-candidate list plus the bridge token minted at download time —
    so first run attaches with zero user input (owner decree 2026-08-05:
    no pairing codes, no pasted lines).

    Probe order: the bundle's direct candidates first (a LAN address has
    no SSO edge in between), then the discovery ladder (loopback → LAN
    broadcast → ssh self-tunnel), the browser-reachable fallback LAST.
    A cloud-IDE proxy URL 401s a cookieless direct probe at the edge, but
    the saved URL remains useful to the browser-assisted transport: the
    signed-in Tofu tab carries polls without exporting its cookies. When
    NOTHING answers directly, the first candidate is still saved so that
    browser handoff and ordinary retry both retain the intended server.

    Discipline (mirrors import_preseed):
      * ONE-SHOT — the file carries a bearer token, so it is deleted
        after ANY attempt, success or failure;
      * NEVER overrides a LIVE existing attachment — but a DEAD one does
        not veto the bundle: re-downloading the installer IS the repair
        path (owner incident 2026-08-06 — an old dead proxy URL silently
        vetoed the fresh bundle's working direct-LAN candidate, and the
        one-shot delete destroyed the repair material unused), so a dead
        saved route is re-pointed from the bundle, the dead address kept
        as a trailing candidate in case the outage was transient;
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
        from lib.desktop_agent.config import remote_server, save_attachment
        from lib.desktop_agent._probe import probe_server
        existing, existing_secret = remote_server()
        if existing:
            alive, dead_reason = probe_server(existing, timeout=2.5)
            if alive:
                log('Attach bundle ignored (already attached to %s)'
                    % existing)
                return False
            log('Saved attachment %s is dead (%s) — treating the fresh '
                'bundle as a re-point, not an override'
                % (existing, dead_reason))
        if not candidates and not fallbacks:
            log('Attach bundle carried no addresses — ignored')
            return False
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
        # The bundle's token is the freshest credential; when it is absent
        # (open-bridge download) keep whatever secret the attachment had.
        try:
            route_set = list(candidates) + list(fallbacks)
            if (existing and existing != chosen
                    and existing not in route_set):
                route_set.append(existing)  # may recover — keep as backup
            save_attachment(
                chosen, token or existing_secret,
                attach_candidates=route_set)
        except Exception as e:
            log('Could not persist attachment: %s' % e)
            logger.warning('Could not persist attachment: %s', e)
            return False
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
      * NEVER overrides an existing attachment — personalized installer data
        wins over the install-time default.
      * NON-SECRET (the URL only) — current controlled-end downloads use the
        stronger token-bearing attachment instead.
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

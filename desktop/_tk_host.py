#!/usr/bin/env python3
"""desktop/_tk_host.py — the tk UI host thread (tray-first window ownership).

Root fix for owner report 2026-08-04: clicking "Minimize to tray" made the
role window vanish with NO tray icon anywhere. The old orchestration ran
``show_role_window`` (blocking tk mainloop) BEFORE ``icon.run()`` — so for
the window's entire first lifetime the process had no tray at all, and the
title-bar minimize button iconified to the taskbar with a generic tk icon
nobody recognizes. "Minimize to tray" was a promise the structure could
not keep.

The fix is a thread topology, not a patch:

* **Main thread → pystray ``icon.run()`` from second zero** (unchanged
  from today — pystray's documented home). The tray icon exists BEFORE
  any window can hide, so hiding to the tray is always possible.
* **A dedicated tk host thread owns ONE hidden root** (created here,
  mainloop here). Every window/dialog rides it as a Toplevel — tk's
  one-thread rule is then trivially satisfied for the process's whole
  lifetime, no matter which thread asks for a window.
* **Tray callbacks marshal window work to the host** — ``post`` (fire and
  forget: the role window) and ``call`` (blocking: dialogs that return a
  value, e.g. the pair flow). The tray thread blocking inside ``call`` is
  exactly today's behaviour (a modal dialog suspends the pystray loop).
* **Host-thread callers short-circuit** — a role-window button already
  runs ON the host thread; ``call``/``post`` from there execute inline,
  so no self-deadlock is possible.

Windows-only by design: on macOS BOTH pystray (AppKit) and tk (Cocoa)
demand the main thread, so the legacy window-then-tray sequence is the
only safe one; the agent build ships Windows-only in v1. When the host
cannot start (headless, no tk, no display) every entry point degrades to
calling the function directly — byte-identical to the old behaviour.

Headless rule: tkinter is imported only inside the host thread. The queue
mechanics (``_drain_once``) take a root duck-type so CI can exercise them
without a display.
"""

import queue
import sys
import threading

from lib.log import get_logger

logger = get_logger(__name__)

_POLL_MS = 120

_host = {
    'thread': None,
    'queue': None,
    'root': None,
    'tid': None,
    'ready': threading.Event(),
}


def _noop_log(_msg: str) -> None:
    pass


def supported() -> bool:
    """Whether the tray-first host topology is safe on this platform.

    win32: pystray's backend runs its GetMessage loop happily on the main
    thread while tk lives on a worker — the split this module exists for.
    macOS: both frameworks demand the main thread — the split is
    impossible, so the host refuses to start and callers keep the legacy
    sequence. Linux: unproven, same refusal.
    """
    return sys.platform.startswith('win')


def available() -> bool:
    """True when the host root is live and marshalling works."""
    return _host['root'] is not None


def on_host_thread() -> bool:
    """True when the CALLER is already the host thread (inline, never queue)."""
    tid = _host['tid']
    return tid is not None and tid == threading.get_ident()


def start(log=_noop_log) -> bool:
    """Spawn the host thread and wait for its root. Idempotent.

    Returns False (callers then use the legacy inline path) when the
    platform is unsupported, tkinter is missing, or root creation fails
    (no display). Never raises — a window system that cannot start must
    not take the tray down with it.
    """
    if not supported():
        return False
    if _host['thread'] is not None:
        return available()
    q = queue.Queue()
    _host['queue'] = q
    _host['ready'].clear()
    t = threading.Thread(target=_thread_main, args=(q, log),
                         daemon=True, name='tofu-tk-host')
    _host['thread'] = t
    t.start()
    if not _host['ready'].wait(timeout=10):
        log('tk host did not become ready in time — falling back to inline '
            'windows')
        logger.warning('tk host readiness timeout — falling back to inline '
                       'windows')
        return False
    return available()


def _thread_main(q, log) -> None:
    """Host thread body: create the hidden root, then drain forever."""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
    except Exception as e:
        log('tk host unavailable (%s) — windows fall back to inline' % e)
        logger.warning('tk host unavailable: %s', e)
        _host['ready'].set()
        return
    _host['root'] = root
    _host['tid'] = threading.get_ident()
    _host['ready'].set()
    root.after(_POLL_MS, _drain_once, root, q, log)
    try:
        root.mainloop()
    except Exception as e:
        log('tk host mainloop died: %s' % e)
        logger.error('tk host mainloop died: %s', e, exc_info=True)
    finally:
        _host['root'] = None
        _host['tid'] = None


def _drain_once(root, q, log) -> None:
    """Run every queued request on the host thread, then re-arm the poll.

    ``root`` is a duck type (anything with ``.after``) so headless tests
    can drive this with a stub. Each item is ``(fn, done, box)``: ``fn()``
    executes here; its return value (or exception) lands in ``box``;
    ``done`` is set when the caller is blocked in :func:`call`.
    """
    while True:
        try:
            fn, done, box = q.get_nowait()
        except queue.Empty:
            break
        try:
            box['result'] = fn()
        except Exception as e:
            box['error'] = e
            log('tk host request failed: %s' % e)
            logger.warning('tk host request failed: %s', e)
        finally:
            if done is not None:
                done.set()
    try:
        root.after(_POLL_MS, _drain_once, root, q, log)
    except Exception as e:
        # The root is being torn down — the process is exiting; nothing
        # sensible to re-arm onto.
        log('tk host drain could not re-arm: %s' % e)
        logger.debug('tk host drain re-arm failed (shutdown?): %s', e)


def post(fn) -> bool:
    """Queue ``fn`` for the host thread. False → caller must run it inline."""
    if not available() or on_host_thread():
        return False
    _host['queue'].put((fn, None, {}))
    return True


def call(fn, timeout=None):
    """Run ``fn`` on the host thread and return its result (blocking).

    Direct execution when the host is down or the caller IS the host
    thread — the two cases where queuing would deadlock or be pointless.
    An exception inside ``fn`` is re-raised on the CALLING thread so the
    tray handler's own error handling still sees it.
    """
    if not available() or on_host_thread():
        return fn()
    done = threading.Event()
    box = {}
    _host['queue'].put((fn, done, box))
    if not done.wait(timeout=timeout):
        logger.error('tk host call timed out after %ss', timeout)
        raise TimeoutError('tk host call did not complete')
    if 'error' in box:
        raise box['error']
    return box.get('result')


def post_or_call(fn) -> None:
    """Fire-and-forget marshalling: host up → queue; host down → inline."""
    if not post(fn):
        fn()


def parent_or_none():
    """The host root when called ON the host thread, else None.

    Dialogs use this to decide their shape: Toplevel(host_root) +
    wait_window when riding the host, standalone Tk() + mainloop when not
    (legacy sequence, first-run prompts before the tray exists, or a
    failed host). Returning None off the host thread is deliberate — a
    Toplevel parent must be created on its own thread.
    """
    return _host['root'] if on_host_thread() else None

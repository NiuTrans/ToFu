#!/usr/bin/env python3
"""Post-install optional component manager.

Handles secondary downloads of large optional components (Playwright/Chromium,
etc.) that are too big to bundle in the main installer. Provides:

1. A first-launch prompt asking which components to install
2. A system-tray menu item "Install Components..." for later installation
3. Background download + progress reporting

Components are defined in OPTIONAL_COMPONENTS below. Each knows how to
check if it's installed, how to install itself, and its approximate size.
"""

import os
import queue
import subprocess
import sys
import threading

from desktop import _tk_theme as theme

# Resolve base directory
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.join(os.path.dirname(sys.executable), '_internal')
    if not os.path.isdir(BASE_DIR):
        BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve the WRITABLE data dir. Prefer the shared resolver (lib/runtime_paths)
# so the first-launch marker + component checks land in the SAME place the
# server writes to (honours $TOFU_DATA_DIR, falls back to a per-user dir when
# the exe sibling is read-only, e.g. a Program Files install).
try:
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    from lib.runtime_paths import data_root as _data_root
    DATA_DIR = _data_root()
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False)
                            else BASE_DIR, 'data')

# Marker file to track first-launch prompt
_FIRST_LAUNCH_MARKER = os.path.join(DATA_DIR, '.components_prompted')


def _diag(msg: str) -> None:
    """Null-safe diagnostic write — sys.stderr is None in windowed builds."""
    try:
        if sys.stderr is not None:
            sys.stderr.write(f'[Tofu] {msg}\n')
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  Component Definitions
# ═══════════════════════════════════════════════════════════════

class Component:
    """Base class for an optional downloadable component.

    All user-facing text is keyed (desktop.comp.<key>.name/.desc/.size in
    _tk_theme.STRINGS); install() + progress_callback return/emit MACHINE
    TOKENS ('chromium_timeout' …) or 'detail:<raw>' — never prose — which
    the UI boundary localises via theme.component_msg (2026-08-04 sweep).
    """

    key: str = ''        # i18n lookup key: desktop.comp.<key>.name/.desc/.size
    name: str = ''       # English fallback (logs)
    description: str = ''
    recommended: bool = False

    def is_installed(self) -> bool:
        raise NotImplementedError

    def install(self, progress_callback=None) -> tuple[bool, str]:
        """Install the component. Returns (success, message)."""
        raise NotImplementedError


class PlaywrightChromium(Component):
    key = 'chromium'
    name = 'Browser Engine (Chromium)'
    description = (
        'Enables advanced web page fetching, JavaScript rendering, '
        'and browser automation. Required for fetch_url on JS-heavy sites.'
    )
    recommended = True

    def is_installed(self) -> bool:
        """True when a usable Chromium binary is on disk.

        Deliberately NOT ``playwright.chromium.executable_path``: that property
        names the FULL build (``chromium-<rev>/chrome-linux64/chrome``) even
        when only the headless shell is installed. install.sh installs
        ``--only-shell`` on purpose (-60% download), so that path is one this
        product never creates — checking it reported "not installed" for a
        browser that launches fine, and the app kept offering the ~150 MB
        download forever.
        """
        try:
            if BASE_DIR not in sys.path:
                sys.path.insert(0, BASE_DIR)
            from chromium_env import chromium_executable
            return bool(chromium_executable())
        except Exception as e:
            _diag(f'chromium detection failed: {e}')
            return False

    def install(self, progress_callback=None) -> tuple[bool, str]:
        """Download and install Playwright Chromium browser.

        A PyInstaller --onedir bundle contains NO standalone ``python.exe`` —
        ``sys.executable`` is ``Tofu.exe`` and ``Tofu.exe -m playwright …`` would
        just boot a second app. So when frozen we relaunch ``Tofu.exe`` with
        ``TOFU_PLAYWRIGHT_INSTALL=1``, which the launcher recognises and turns
        into an in-process ``playwright install chromium``. From source we use
        the interpreter's ``-m playwright`` directly.
        """
        try:
            if progress_callback:
                progress_callback(self.name, 'chromium_downloading')

            env = os.environ.copy()
            if getattr(sys, 'frozen', False):
                cmd = [sys.executable]
                env['TOFU_PLAYWRIGHT_INSTALL'] = '1'
            else:
                # --only-shell matches install.sh: the full build is 175 MB
                # nobody launches here (every call site is headless).
                cmd = [sys.executable, '-m', 'playwright', 'install',
                       '--only-shell', 'chromium']

            result = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=600
            )

            if result.returncode == 0:
                return True, 'chromium_ok'
            return False, 'detail:%s' % (result.stderr or result.stdout)

        except subprocess.TimeoutExpired:
            return False, 'chromium_timeout'
        except FileNotFoundError:
            return False, 'chromium_no_module'
        except Exception as e:
            return False, 'detail:%s' % e


# Registry of all optional components
OPTIONAL_COMPONENTS: list[Component] = [
    PlaywrightChromium(),
]


# ═══════════════════════════════════════════════════════════════
#  UI — First-launch prompt (cross-platform)
# ═══════════════════════════════════════════════════════════════

def _prompt_gui(components: list[Component]) -> list[tuple]:
    """Show the component manager: select → install WITH VISIBLE PROGRESS → results.

    Returns a list of (name, success, message) tuples for the components the
    user chose to install (empty when they skipped). Falls back to the
    terminal prompt when tkinter is unavailable.

    ── Why the dialog stays OPEN during install ──
    The old flow closed the window on "Install Selected" and ran ~165 MB of
    downloads on a daemon thread whose only output was a log file. The user
    got no progress, no failure notice, nothing. That pipe (progress_callback)
    existed but was never wired to UI. Here the SAME window swaps to a
    progress view: one status row per component, an overall progress bar,
    and per-component failure messages — all driven by a worker thread whose
    events are marshalled onto the tk thread via a queue polled by after()
    (tk is not thread-safe; worker threads must never touch widgets).
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return _prompt_terminal_gui_fallback(components)

    theme.ensure_dpi_awareness(_diag)
    lang = theme.detect_lang()

    holder: dict = {'results': []}
    # Ride the tk host when it owns the process's windows (the tray-first
    # topology): a Toplevel on the ONE root, modal via wait_window.
    # Standalone Tk + mainloop otherwise (first launch, no host yet).
    from desktop import _tk_host as host
    parent = host.parent_or_none()
    root = tk.Toplevel(parent) if parent is not None else tk.Tk()
    p = theme.apply_theme(root)
    root.title('Tofu — %s' % theme.t('desktop.components.title', lang))
    root.resizable(False, False)
    theme.set_window_icon(root)

    outer = ttk.Frame(root, style='Tofu.TFrame', padding=22)
    outer.pack(fill='both', expand=True)

    # ── Header: brand logo + title + subtitle ──
    header = ttk.Frame(outer, style='Tofu.TFrame')
    header.pack(fill='x')
    photo = theme.load_logo_photo(root, size=52)
    if photo is not None:
        ttk.Label(header, image=photo, style='Tofu.TLabel').pack(
            side='left', padx=(0, 14))
    head_text = ttk.Frame(header, style='Tofu.TFrame')
    head_text.pack(side='left', fill='x', expand=True)
    ttk.Label(head_text, text=theme.t('desktop.components.title', lang),
              style='Tofu.Title.TLabel').pack(anchor='w')
    ttk.Label(head_text, text=theme.t('desktop.components.subtitle', lang),
              style='Tofu.Sub.TLabel', wraplength=400,
              justify='left').pack(anchor='w', pady=(4, 0))

    body = ttk.Frame(outer, style='Tofu.TFrame')
    body.pack(fill='both', expand=True, pady=(18, 0))

    # ── Selection view: one card per component ──
    select_frame = ttk.Frame(body, style='Tofu.TFrame')
    select_frame.pack(fill='both', expand=True)

    rows: list[tuple] = []  # (comp, var)
    for comp in components:
        installed = comp.is_installed()
        var = tk.BooleanVar(value=comp.recommended and not installed)
        card = theme.card_frame(select_frame, p)
        card.pack(fill='x', pady=4)
        inner = ttk.Frame(card, style='Card.TFrame', padding=(12, 10))
        inner.pack(fill='x')
        name = theme.t('desktop.comp.%s.name' % comp.key, lang)
        status = (' · %s' % theme.t('desktop.components.installed', lang)
                  if installed else
                  ' · %s' % theme.t('desktop.comp.%s.size' % comp.key, lang))
        cb = ttk.Checkbutton(inner, text='%s%s' % (name, status),
                             variable=var, style='Tofu.TCheckbutton',
                             state='disabled' if installed else 'normal')
        cb.pack(anchor='w')
        desc = theme.t('desktop.comp.%s.desc' % comp.key, lang)
        ttk.Label(inner, text=desc, style='CardSub.TLabel', wraplength=450,
                  justify='left').pack(anchor='w', padx=(22, 0), pady=(2, 0))
        rows.append((comp, var))

    # ── Progress view (hidden until Install) ──
    progress_frame = ttk.Frame(body, style='Tofu.TFrame')
    prog_status: list = []   # per-component status StringVar
    prog_style: list = []    # per-component status LABEL (color flips ok/err)
    prog_msg: list = []      # per-component failure-message StringVar
    bar = ttk.Progressbar(progress_frame, style='Tofu.Horizontal.TProgressbar',
                          mode='determinate')
    summary_var = tk.StringVar(value='')
    summary = ttk.Label(progress_frame, textvariable=summary_var,
                        style='Tofu.Sub.TLabel', wraplength=480,
                        justify='left')

    # ── Buttons ──
    btns = ttk.Frame(outer, style='Tofu.TFrame')
    btns.pack(fill='x', pady=(18, 0))

    def on_skip():
        root.destroy()

    skip_btn = ttk.Button(btns, text=theme.t('desktop.components.skip', lang),
                          style='Tofu.TButton', command=on_skip)
    skip_btn.pack(side='left')

    close_btn = ttk.Button(btns, text=theme.t('desktop.components.close', lang),
                           style='Tofu.Accent.TButton', command=root.destroy,
                           state='disabled')

    events: queue.Queue = queue.Queue()

    def _drain():
        """Pump worker events onto the tk thread (the only thread allowed
        to touch widgets). Stops polling once 'finished' arrives."""
        try:
            while True:
                kind, idx, payload = events.get_nowait()
                if kind == 'start':
                    prog_status[idx].set(
                        theme.t('desktop.components.installing', lang))
                elif kind == 'status':
                    prog_status[idx].set(theme.component_msg(payload, lang))
                elif kind == 'done':
                    success, msg = payload
                    bar['value'] = bar['value'] + 1
                    if success:
                        prog_status[idx].set(
                            theme.t('desktop.components.installed', lang))
                        prog_style[idx].configure(style='Status.Ok.TLabel')
                    else:
                        prog_status[idx].set(
                            theme.t('desktop.components.failed', lang))
                        prog_style[idx].configure(style='Status.Err.TLabel')
                        prog_msg[idx].set(
                            theme.component_msg(msg, lang)[:240])
                elif kind == 'finished':
                    results = payload
                    holder['results'] = results
                    failed = sum(1 for _n, ok, _m in results if not ok)
                    summary_var.set(
                        theme.t('desktop.components.summaryOk', lang) if not failed
                        else theme.t('desktop.components.summaryFail', lang)
                        .replace('{n}', str(failed)))
                    close_btn.configure(state='normal')
                    return  # stop polling
        except queue.Empty:
            pass
        try:
            root.after(80, _drain)
        except Exception:
            pass  # window closed mid-install; worker is a daemon, results logged nowhere

    def on_install():
        selected = [c for c, v in rows if v.get() and not c.is_installed()]
        if not selected:
            root.destroy()
            return
        # Swap selection → progress view.
        select_frame.pack_forget()
        skip_btn.pack_forget()
        install_btn.pack_forget()
        close_btn.pack(side='right')

        for comp in selected:
            card = theme.card_frame(progress_frame, p)
            card.pack(fill='x', pady=4)
            inner = ttk.Frame(card, style='Card.TFrame', padding=(12, 8))
            inner.pack(fill='x')
            head = ttk.Frame(inner, style='Card.TFrame')
            head.pack(fill='x')
            name = theme.t('desktop.comp.%s.name' % comp.key, lang)
            ttk.Label(head, text=name, style='CardName.TLabel').pack(side='left')
            sv = tk.StringVar(value=theme.t('desktop.components.pending', lang))
            sl = ttk.Label(head, textvariable=sv, style='CardSub.TLabel')
            sl.pack(side='right')
            mv = tk.StringVar(value='')
            ml = ttk.Label(inner, textvariable=mv, style='Status.Err.TLabel',
                           wraplength=450, justify='left')
            ml.pack(anchor='w', pady=(2, 0))
            prog_status.append(sv)
            prog_style.append(sl)
            prog_msg.append(mv)

        bar.configure(maximum=len(selected), value=0)
        bar.pack(fill='x', pady=(12, 0))
        summary.pack(anchor='w', pady=(8, 0))
        progress_frame.pack(fill='both', expand=True)

        def _worker():
            results = []
            for i, comp in enumerate(selected):
                events.put(('start', i, None))
                ok, msg = comp.install(
                    progress_callback=lambda _n, txt, i=i:
                        events.put(('status', i, txt)))
                results.append((comp.name, ok, msg))
                events.put(('done', i, (ok, msg)))
            events.put(('finished', -1, results))

        threading.Thread(target=_worker, daemon=True,
                         name='component-installer').start()
        root.after(80, _drain)

    install_btn = ttk.Button(
        btns, text=theme.t('desktop.components.install', lang),
        style='Tofu.Accent.TButton', command=on_install)
    install_btn.pack(side='right')

    theme.center_on_screen(root, width=560)
    if parent is not None:
        root.transient(parent)
        root.grab_set()
        try:
            parent.wait_window(root)
        except tk.TclError:
            pass
        return holder['results']
    root.mainloop()
    return holder['results']


def _prompt_terminal_gui_fallback(components: list[Component]) -> list[tuple]:
    """No-tkinter path: terminal selection, then install with plain prints.

    Kept separate from _prompt_terminal (which only SELECTS, for __main__'s
    manual flow) so the GUI entry point's contract — returns install RESULTS,
    not selections — holds on headless machines too. Interactive text is
    bilingual like every other surface (the 2026-08-04 sweep) — only log
    lines stay English.
    """
    lang = theme.detect_lang()
    selected = _prompt_terminal(components)
    results = []
    for comp in selected:
        ok, msg = comp.install(
            lambda n, txt: print('  … %s' % theme.component_msg(txt, lang)))
        results.append((comp.name, ok, msg))
        print('  [%s] %s: %s' % ('OK' if ok else 'FAIL',
                                 theme.t('desktop.comp.%s.name' % comp.key,
                                         lang),
                                 theme.component_msg(msg, lang)))
    return results


def _prompt_terminal(components: list[Component]) -> list[Component]:
    """Fallback: terminal-based prompt for headless environments."""
    lang = theme.detect_lang()
    print('\n' + '=' * 56)
    print('  Tofu — %s' % theme.t('desktop.components.title', lang))
    print('=' * 56 + '\n')

    selected = []
    for comp in components:
        name = theme.t('desktop.comp.%s.name' % comp.key, lang)
        if comp.is_installed():
            print(theme.t('desktop.terminal.alreadyInstalled', lang)
                  .replace('{name}', name))
            continue

        default = 'Y' if comp.recommended else 'N'
        prompt = theme.t('desktop.terminal.installPrompt', lang) \
            .replace('{name}', name) \
            .replace('{size}', theme.t('desktop.comp.%s.size' % comp.key,
                                       lang)) \
            .replace('{default}', default) \
            .replace('{other}', 'n' if comp.recommended else 'y')
        try:
            answer = input(prompt).strip().lower()
            if not answer:
                answer = default.lower()
            if answer in ('y', 'yes'):
                selected.append(comp)
        except (EOFError, KeyboardInterrupt):
            break

    return selected


def _install_components(components: list[Component], progress_callback=None):
    """Install a list of components sequentially."""
    results = []
    for comp in components:
        if comp.is_installed():
            results.append((comp.name, True, 'Already installed.'))
            continue
        success, msg = comp.install(progress_callback)
        results.append((comp.name, success, msg))
    return results


# ═══════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════

def is_first_launch() -> bool:
    """Check if this is the first launch (components not yet prompted)."""
    return not os.path.isfile(_FIRST_LAUNCH_MARKER)


def mark_prompted():
    """Mark that the user has been prompted about components."""
    os.makedirs(os.path.dirname(_FIRST_LAUNCH_MARKER), exist_ok=True)
    with open(_FIRST_LAUNCH_MARKER, 'w') as f:
        f.write('1')


def run_first_launch_prompt():
    """Show the first-launch component selection dialog.

    Called by the launcher on first run. Non-blocking if user clicks Skip.
    """
    not_installed = [c for c in OPTIONAL_COMPONENTS if not c.is_installed()]
    if not not_installed:
        mark_prompted()
        return

    # The dialog itself now hosts the install with visible progress and
    # returns the RESULTS; nothing installs on an invisible thread anymore.
    results = _prompt_gui(not_installed)
    mark_prompted()
    for name, success, msg in results:
        status = '[OK]' if success else '[FAIL]'
        _diag(f'{status} {name}: {msg}')


def get_uninstalled_components() -> list[Component]:
    """Get list of components that are not yet installed."""
    return [c for c in OPTIONAL_COMPONENTS if not c.is_installed()]


if __name__ == '__main__':
    # Manual invocation: show prompt regardless of first-launch state
    not_installed = [c for c in OPTIONAL_COMPONENTS if not c.is_installed()]
    if not not_installed:
        print('All optional components are already installed.')
    else:
        results = _prompt_gui(not_installed)
        if results:
            for name, success, msg in results:
                status = '[OK]' if success else '[FAIL]'
                print(f'  {status} {name}: {msg}')
        else:
            print('No components selected.')

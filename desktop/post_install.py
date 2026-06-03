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
import subprocess
import sys
import threading

# Resolve base directory
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.join(os.path.dirname(sys.executable), '_internal')
    if not os.path.isdir(BASE_DIR):
        BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    """Base class for an optional downloadable component."""

    name: str = ''
    description: str = ''
    size_hint: str = ''  # e.g. "~150 MB"
    recommended: bool = False

    def is_installed(self) -> bool:
        raise NotImplementedError

    def install(self, progress_callback=None) -> tuple[bool, str]:
        """Install the component. Returns (success, message)."""
        raise NotImplementedError


class PlaywrightChromium(Component):
    name = 'Browser Engine (Chromium)'
    description = (
        'Enables advanced web page fetching, JavaScript rendering, '
        'and browser automation. Required for fetch_url on JS-heavy sites.'
    )
    size_hint = '~150 MB download'
    recommended = True

    def is_installed(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright
            # Check if chromium binary exists
            with sync_playwright() as p:
                path = p.chromium.executable_path
                return os.path.isfile(path)
        except Exception:
            return False

    def install(self, progress_callback=None) -> tuple[bool, str]:
        """Download and install Playwright Chromium browser."""
        try:
            if progress_callback:
                progress_callback(self.name, 'Downloading Chromium...')

            # Find the python executable in our frozen bundle or env
            python_exe = sys.executable

            # Run playwright install chromium
            result = subprocess.run(
                [python_exe, '-m', 'playwright', 'install', 'chromium'],
                capture_output=True, text=True, timeout=600
            )

            if result.returncode == 0:
                return True, 'Chromium browser installed successfully.'
            else:
                # Try alternative: direct playwright CLI
                result2 = subprocess.run(
                    [python_exe, '-m', 'playwright', 'install', '--with-deps', 'chromium'],
                    capture_output=True, text=True, timeout=600
                )
                if result2.returncode == 0:
                    return True, 'Chromium browser installed successfully (with system deps).'
                return False, f'Installation failed: {result.stderr or result2.stderr}'

        except subprocess.TimeoutExpired:
            return False, 'Download timed out (10 min). Check your network connection.'
        except FileNotFoundError:
            return False, 'Playwright module not found in bundle.'
        except Exception as e:
            return False, f'Unexpected error: {e}'


class PostgreSQL(Component):
    name = 'PostgreSQL Database'
    description = (
        'High-performance database for multi-user deployments. '
        'Provides better concurrency, JSONB support, and full-text search. '
        'Without this, the app uses SQLite (single-user, still fully functional).'
    )
    size_hint = '~50 MB download'
    recommended = True

    def is_installed(self) -> bool:
        """Check if PG binaries are available."""
        import shutil
        # Check in bundled location first
        pg_dir = os.path.join(DATA_DIR, 'pgsql', 'bin')
        if os.path.isdir(pg_dir):
            return True
        # Check system PATH
        return shutil.which('initdb') is not None

    def install(self, progress_callback=None) -> tuple[bool, str]:
        """Bootstrap PostgreSQL via the app's existing mechanism."""
        try:
            if progress_callback:
                progress_callback(self.name, 'Setting up PostgreSQL...')

            # The app's lib/database/_bootstrap.py handles PG setup.
            # Trigger it by importing the database module.
            sys.path.insert(0, BASE_DIR)
            os.chdir(BASE_DIR)

            from lib.database._bootstrap import ensure_pg_available
            success = ensure_pg_available()

            if success:
                return True, 'PostgreSQL configured successfully.'
            else:
                return False, (
                    'PostgreSQL bootstrap failed. The app will use SQLite instead. '
                    'You can install PostgreSQL manually later.'
                )
        except ImportError:
            return False, (
                'Database bootstrap module not available. '
                'PostgreSQL can be installed manually.'
            )
        except Exception as e:
            return False, f'PostgreSQL setup error: {e}'


# Registry of all optional components
OPTIONAL_COMPONENTS: list[Component] = [
    PostgreSQL(),
    PlaywrightChromium(),
]


# ═══════════════════════════════════════════════════════════════
#  UI — First-launch prompt (cross-platform)
# ═══════════════════════════════════════════════════════════════

def _prompt_gui(components: list[Component]) -> list[Component]:
    """Show a GUI dialog asking which components to install.

    Returns the list of components the user selected.
    Falls back to terminal prompt if no GUI available.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return _prompt_terminal(components)

    # Per-monitor DPI awareness so the dialog isn't bitmap-stretched (blurry)
    # on HiDPI Windows displays. No-op elsewhere / on older Windows.
    if sys.platform.startswith('win'):
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    selected = []
    root = tk.Tk()
    root.title('Tofu — Optional Components')
    root.geometry('520x400')
    root.resizable(False, False)

    # Try to set icon
    try:
        icon_path = os.path.join(BASE_DIR, 'static', 'icons', 'tofu.ico')
        if os.path.isfile(icon_path):
            root.iconbitmap(icon_path)
    except Exception:
        pass

    frame = ttk.Frame(root, padding=20)
    frame.pack(fill='both', expand=True)

    ttk.Label(frame, text='Optional Components', font=('', 14, 'bold')).pack(anchor='w')
    ttk.Label(frame, text=(
        'The following components can be downloaded now or later.\n'
        'Recommended components are pre-selected.'
    ), wraplength=480).pack(anchor='w', pady=(5, 15))

    vars_map: dict[int, tk.BooleanVar] = {}
    for i, comp in enumerate(components):
        var = tk.BooleanVar(value=comp.recommended and not comp.is_installed())
        vars_map[i] = var

        comp_frame = ttk.Frame(frame)
        comp_frame.pack(fill='x', pady=3)

        status = ' ✓ installed' if comp.is_installed() else f' ({comp.size_hint})'
        cb = ttk.Checkbutton(
            comp_frame,
            text=f'{comp.name}{status}',
            variable=var,
            state='disabled' if comp.is_installed() else 'normal'
        )
        cb.pack(anchor='w')

        ttk.Label(comp_frame, text=f'    {comp.description}',
                  wraplength=460, foreground='gray').pack(anchor='w')

    def on_install():
        for i, comp in enumerate(components):
            if vars_map[i].get() and not comp.is_installed():
                selected.append(comp)
        root.destroy()

    def on_skip():
        root.destroy()

    btn_frame = ttk.Frame(frame)
    btn_frame.pack(fill='x', pady=(20, 0))
    ttk.Button(btn_frame, text='Skip for now', command=on_skip).pack(side='left')
    ttk.Button(btn_frame, text='Install Selected', command=on_install).pack(side='right')

    root.mainloop()
    return selected


def _prompt_terminal(components: list[Component]) -> list[Component]:
    """Fallback: terminal-based prompt for headless environments."""
    print('\n╔══════════════════════════════════════════════════════╗')
    print('║       Tofu — Optional Components Setup              ║')
    print('╚══════════════════════════════════════════════════════╝\n')

    selected = []
    for comp in components:
        if comp.is_installed():
            print(f'  ✓ {comp.name} — already installed')
            continue

        default = 'Y' if comp.recommended else 'N'
        prompt = f'  Install {comp.name} ({comp.size_hint})? [{default}/{"n" if comp.recommended else "y"}]: '
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

    selected = _prompt_gui(not_installed)
    mark_prompted()

    if selected:
        def _bg_install():
            results = _install_components(selected)
            for name, success, msg in results:
                status = '✓' if success else '✗'
                _diag(f'{status} {name}: {msg}')

        thread = threading.Thread(target=_bg_install, daemon=True,
                                  name='component-installer')
        thread.start()


def get_uninstalled_components() -> list[Component]:
    """Get list of components that are not yet installed."""
    return [c for c in OPTIONAL_COMPONENTS if not c.is_installed()]


if __name__ == '__main__':
    # Manual invocation: show prompt regardless of first-launch state
    not_installed = [c for c in OPTIONAL_COMPONENTS if not c.is_installed()]
    if not not_installed:
        print('All optional components are already installed.')
    else:
        selected = _prompt_gui(not_installed)
        if selected:
            results = _install_components(selected)
            for name, success, msg in results:
                status = '✓' if success else '✗'
                print(f'  {status} {name}: {msg}')
        else:
            print('No components selected.')

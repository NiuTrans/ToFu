"""Per-user Agent login startup for Windows, Linux and macOS.

No GUI/server imports or service process ownership. Entries launch the existing
agent composition, which restores its connection and explicit permission choice.
"""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
# Stable registry identity preserves existing startup settings across the rename.
RUN_VALUE = 'TofuAgent'
LABEL = 'com.tofu.agent'


def supported():
    return sys.platform.startswith(('win', 'linux')) or sys.platform == 'darwin'


def _entry_path():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    if not config.is_absolute():
        raise ValueError('XDG_CONFIG_HOME must be absolute')
    return config / 'autostart/tofu-agent.desktop'


def _argv():
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    return [sys.executable, '-m', 'desktop.agent_launcher']


def enabled():
    if not supported():
        return False
    if sys.platform.startswith('win'):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_VALUE)
            return True
        except FileNotFoundError:
            return False
    return _entry_path().is_file()


def _desktop_quote(value):
    if '\n' in value or '\r' in value:
        raise ValueError('Autostart path contains a newline')
    value = value.replace('\\', '\\\\\\\\').replace('"', '\\\\"')
    value = value.replace('`', '\\\\`').replace('$', '\\\\$').replace('%', '%%')
    return '"' + value + '"'


def apply(choice):
    """Publish atomically; failures preserve the previous entry and propagate."""
    if not supported():
        raise ValueError('Login startup is unsupported on this platform')
    if sys.platform.startswith('win'):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if choice:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ,
                                  subprocess.list2cmdline(_argv()))
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    return  # An absent owned value already satisfies disable.
        return
    target = _entry_path()
    if target.is_symlink():
        raise ValueError('Autostart entry must not be a symlink')
    if not choice:
        target.unlink(missing_ok=True)
        return
    cwd = str(Path(__file__).resolve().parents[1])
    if sys.platform == 'darwin':
        data = plistlib.dumps({'Label': LABEL, 'ProgramArguments': _argv(),
                              'RunAtLoad': True, 'WorkingDirectory': cwd})
    else:
        command = ' '.join(_desktop_quote(arg) for arg in _argv())
        if '\n' in cwd or '\r' in cwd:
            raise ValueError('Autostart working directory contains a newline')
        cwd = cwd.replace('\\', '\\\\')
        data = ('[Desktop Entry]\nType=Application\nName=Tofu Remote\n'
                'Exec=' + command + '\nPath=' + cwd + '\nTerminal=false\n'
                'X-GNOME-Autostart-enabled=true\n').encode('utf-8')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, candidate = tempfile.mkstemp(prefix='.tofu-agent-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(candidate, target)
    finally:
        Path(candidate).unlink(missing_ok=True)

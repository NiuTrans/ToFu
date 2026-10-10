"""Per-user startup entries preserve arguments, preferences and old artifacts."""
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from desktop import autostart

pytestmark = pytest.mark.unit


@pytest.fixture
def desktop_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setattr(autostart, 'sys', SimpleNamespace(
        platform='linux', executable='/opt/Tofu space/TofuAgent', frozen=True))
    return tmp_path


def test_linux_publish_reinstall_and_disable(desktop_home):
    assert autostart.supported() and not autostart.enabled()
    autostart.apply(True)
    path = desktop_home / 'config/autostart/tofu-agent.desktop'
    content = path.read_text()
    assert 'Exec="/opt/Tofu space/TofuAgent"' in content
    assert 'Terminal=false' in content
    assert path.stat().st_mode & 0o777 == 0o600
    autostart.apply(True)
    assert path.read_text() == content
    assert autostart.enabled()
    autostart.apply(False)
    autostart.apply(False)
    assert not autostart.enabled()


def test_source_startup_launches_agent_module(desktop_home, monkeypatch):
    monkeypatch.setattr(autostart.sys, 'frozen', False)
    autostart.apply(True)
    assert '"-m" "desktop.agent_launcher"' in autostart._entry_path().read_text()


def test_macos_launchagent_has_exact_argv(desktop_home, monkeypatch):
    monkeypatch.setattr(autostart.sys, 'platform', 'darwin')
    autostart.apply(True)
    data = plistlib.loads(autostart._entry_path().read_bytes())
    assert data['ProgramArguments'] == ['/opt/Tofu space/TofuAgent']
    assert data['Label'] == autostart.LABEL and data['RunAtLoad'] is True
    autostart.apply(False)
    assert not autostart.enabled()


def test_failed_publication_preserves_previous_entry(desktop_home, monkeypatch):
    autostart.apply(True)
    path = autostart._entry_path()
    old = path.read_bytes()
    def fail(*args):
        raise OSError('synthetic replace failure')
    monkeypatch.setattr(autostart.os, 'replace', fail)
    with pytest.raises(OSError):
        autostart.apply(True)
    assert path.read_bytes() == old
    assert list(path.parent.iterdir()) == [path]


def test_symlink_and_relative_config_refused(desktop_home, monkeypatch):
    target = autostart._entry_path()
    target.parent.mkdir(parents=True)
    other = desktop_home / 'unrelated'
    other.write_text('keep')
    target.symlink_to(other)
    with pytest.raises(ValueError, match='symlink'):
        autostart.apply(True)
    with pytest.raises(ValueError, match='symlink'):
        autostart.apply(False)
    assert other.read_text() == 'keep'
    monkeypatch.setenv('XDG_CONFIG_HOME', 'relative')
    with pytest.raises(ValueError, match='absolute'):
        autostart.apply(True)


def test_desktop_exec_escapes_both_layers(desktop_home, monkeypatch):
    monkeypatch.setattr(autostart.sys, 'executable', '/opt/a$`"%\\b/TofuAgent')
    autostart.apply(True)
    content = autostart._entry_path().read_text()
    assert '\\\\$' in content and '\\\\`' in content and '\\\\"' in content
    assert '%%' in content and '\\\\\\\\b' in content
    monkeypatch.setattr(autostart.sys, 'executable', '/opt/invalid\npath')
    old = content
    with pytest.raises(ValueError, match='newline'):
        autostart.apply(True)
    assert autostart._entry_path().read_text() == old


def test_windows_registry_startup_preserves_quoted_executable(desktop_home, monkeypatch):
    import sys
    values = {}
    class Key:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    def query(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1
    def delete(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]
    registry = SimpleNamespace(HKEY_CURRENT_USER=1, REG_SZ=1,
        OpenKey=lambda *args: Key(), CreateKey=lambda *args: Key(), QueryValueEx=query,
        SetValueEx=lambda key, name, reserved, kind, value: values.update({name: value}),
        DeleteValue=delete)
    monkeypatch.setitem(sys.modules, 'winreg', registry)
    monkeypatch.setattr(autostart.sys, 'platform', 'win32')
    monkeypatch.setattr(autostart.sys, 'executable', r'C:\Program Files\Tofu\TofuAgent.exe')
    assert not autostart.enabled()
    autostart.apply(True)
    assert values['TofuAgent'] == r'"C:\Program Files\Tofu\TofuAgent.exe"'
    assert autostart.enabled()
    values['other-application'] = 'keep'
    autostart.apply(False)
    autostart.apply(False)
    assert values == {'other-application': 'keep'}

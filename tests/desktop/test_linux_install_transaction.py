"""Linux desktop publication, login startup and rollback in private fake homes."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / 'Tofu space'
    (path / '_internal/static/icons').mkdir(parents=True)
    (path / 'Tofu').write_text('#!/bin/sh\nexit 0\n')
    (path / 'Tofu').chmod(0o755)
    (path / '_internal/static/icons/logo.png').write_bytes(b'synthetic-icon')
    shutil.copy(ROOT / 'desktop/tofu.desktop', path / 'tofu.desktop')
    shutil.copy(ROOT / 'desktop/install-linux.sh', path / 'install.sh')
    home = tmp_path / 'home'
    env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / 'data'),
               XDG_CONFIG_HOME=str(home / 'config'))
    env.pop('LD_LIBRARY_PATH', None)
    env.pop('LD_PRELOAD', None)
    return path, home, env


def run(bundle, *args):
    path, _, env = bundle
    return subprocess.run(['bash', str(path / 'install.sh'), *args], env=env,
                          capture_output=True, text=True, timeout=10)


def test_install_startup_rerun_disable_uninstall_preserve_data(bundle):
    path, home, env = bundle
    assert run(bundle, '--autostart').returncode == 0
    entry = home / 'data/applications/tofu.desktop'
    startup = home / 'config/autostart/tofu.desktop'
    icon = home / 'data/icons/hicolor/512x512/apps/tofu.png'
    assert startup.read_bytes() == entry.read_bytes()
    assert run(bundle).returncode == 0
    assert startup.is_file()  # Default rerun preserves the explicit choice.
    assert run(bundle, '--no-autostart').returncode == 0
    assert not startup.exists() and entry.exists()
    (path / 'durable-user-data').write_text('keep')
    assert run(bundle, '--uninstall').returncode == 0
    assert run(bundle, '--uninstall').returncode == 0
    assert not entry.exists() and not icon.exists()
    assert (path / 'durable-user-data').read_text() == 'keep'
    assert (path / 'Tofu').exists()


def test_bad_upgrade_template_preserves_old_icon_and_entry(bundle):
    path, home, _ = bundle
    assert run(bundle).returncode == 0
    entry = home / 'data/applications/tofu.desktop'
    icon = home / 'data/icons/hicolor/512x512/apps/tofu.png'
    old_entry, old_icon = entry.read_bytes(), icon.read_bytes()
    (path / 'tofu.desktop').write_text('[Desktop Entry]\nExec=broken\n')
    (path / '_internal/static/icons/logo.png').write_bytes(b'new-icon')
    assert run(bundle).returncode != 0
    assert entry.read_bytes() == old_entry and icon.read_bytes() == old_icon


def test_mid_publication_failure_rolls_back_all_entries(bundle, tmp_path):
    path, home, env = bundle
    assert run(bundle, '--autostart').returncode == 0
    targets = [home / 'data/applications/tofu.desktop',
               home / 'data/icons/hicolor/512x512/apps/tofu.png',
               home / 'config/autostart/tofu.desktop']
    previous = [p.read_bytes() for p in targets]
    (path / '_internal/static/icons/logo.png').write_bytes(b'new-icon')
    fake_bin = tmp_path / 'fake-bin'
    fake_bin.mkdir()
    mv = fake_bin / 'mv'
    # Fail the new menu publication after the icon was replaced, but allow rollback.
    mv.write_text('#!/bin/bash\ncase "$3" in */.tofu.desktop.*) exit 1;; esac\nexec /bin/mv "$@"\n')
    mv.chmod(0o755)
    env['PATH'] = str(fake_bin) + os.pathsep + env['PATH']
    assert run(bundle, '--autostart').returncode != 0
    assert [p.read_bytes() for p in targets] == previous
    assert not (home / 'data/applications/.tofu-install.lock').exists()


def test_uninstall_from_old_bundle_refuses_other_bundle_entries(bundle, tmp_path):
    assert run(bundle, '--autostart').returncode == 0
    path, home, env = bundle
    other = tmp_path / 'new-bundle'
    shutil.copytree(path, other)
    second = (other, home, env)
    assert run(second).returncode == 0
    assert str(other) in (home / 'config/autostart/tofu.desktop').read_text()
    before = (home / 'data/applications/tofu.desktop').read_bytes()
    assert run(bundle, '--uninstall').returncode != 0
    assert (home / 'data/applications/tofu.desktop').read_bytes() == before
    assert run(second, '--uninstall').returncode == 0


def test_lock_and_symlink_do_not_overwrite_existing_state(bundle):
    path, home, _ = bundle
    applications = home / 'data/applications'
    applications.mkdir(parents=True)
    lock = applications / '.tofu-install.lock'
    lock.mkdir()
    assert run(bundle).returncode != 0
    assert lock.exists()
    lock.rmdir()
    unrelated = home / 'other'
    unrelated.write_text('keep')
    (applications / 'tofu.desktop').symlink_to(unrelated)
    assert run(bundle).returncode != 0
    assert unrelated.read_text() == 'keep'

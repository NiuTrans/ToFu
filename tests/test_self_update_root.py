"""Production wiring guards for the self-update install root."""

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


def test_self_update_root_is_the_tofu_install_root():
    import lib.self_update as updater

    root = Path(updater._ROOT)
    assert (root / 'server.py').is_file()
    assert (root / 'VERSION').is_file()
    assert (root / 'lib' / 'self_update').is_dir()


def test_real_git_checkout_uses_git_update_mode():
    import lib.self_update as updater

    root = Path(updater._ROOT)
    if not (root / '.git').is_dir():
        pytest.skip('release/export tree has no Git metadata')
    assert updater.git_available() is True

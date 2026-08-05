"""tests/test_bind_lan_default.py — the LAN-by-default invariant (owner 2026-08-04).

The desktop-agent attach flow (LAN discovery + pairing code) only works when
the server is reachable off-loopback. Historically the default was split:
``bootstrap.py`` / Docker / install.sh already defaulted to ``0.0.0.0`` while
direct ``python server.py`` and the deploy scripts defaulted to ``127.0.0.1``
— the outlier that stranded the agent flow on this deployment. Owner ruling
2026-08-04: all-interfaces is the default everywhere; loopback becomes the
explicit opt-in (``--host 127.0.0.1`` / ``BIND_HOST=127.0.0.1``), which the
packaged desktop app already pins for itself in ``desktop/launcher.py``.

Pinned:
  1. ``server.py``'s argparse ``--host`` default is ``0.0.0.0``.
  2. All three production launchers (restart_15000.sh, deploy/tofu_guard.sh,
     deploy/supervisor/tofu.conf) default BIND_HOST to ``0.0.0.0`` — an
     OOM-respawned server must not silently narrow the bind.
  3. The packaged DESKTOP app keeps its explicit loopback pin (a laptop app
     must not start serving the LAN just because the server default moved).
  4. The open-auth + non-loopback loud banner still exists in server.py —
     the security tripwire that makes the wider default acceptable.

Run:  pytest tests/test_bind_lan_default.py -q -p no:napari -o addopts=
"""

import os
import re

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


class TestBindLanDefault:
    def test_server_py_argparse_default_is_all_interfaces(self):
        src = _src('server.py')
        m = re.search(
            r"add_argument\('--host',\s*default=os\.environ\.get\('BIND_HOST',\s*'([^']+)'\)\)",
            src)
        assert m, 'server.py --host argparse default not found (drift?)'
        assert m.group(1) == '0.0.0.0', (
            f"server.py --host default drifted back to {m.group(1)!r} — "
            "the agent LAN flow needs all-interfaces by default; loopback "
            "is the explicit opt-in now")

    def test_restart_script_defaults_to_all_interfaces(self):
        src = _src('restart_15000.sh')
        assert 'BIND_HOST="${BIND_HOST:-0.0.0.0}"' in src, (
            'restart_15000.sh must default BIND_HOST to 0.0.0.0')

    def test_guard_relaunch_does_not_narrow_the_bind(self):
        src = _src('deploy/tofu_guard.sh')
        assert 'BIND_HOST="${BIND_HOST:-0.0.0.0}"' in src, (
            'tofu_guard.sh must default BIND_HOST to 0.0.0.0 — an '
            'OOM-respawned server must not come back loopback-only')

    def test_supervisor_conf_binds_all_interfaces(self):
        src = _src('deploy/supervisor/tofu.conf')
        assert 'BIND_HOST="0.0.0.0"' in src, (
            'deploy/supervisor/tofu.conf must bind 0.0.0.0')

    def test_packaged_desktop_app_keeps_its_loopback_pin(self):
        src = _src('desktop/launcher.py')
        assert "env['BIND_HOST'] = '127.0.0.1'" in src, (
            'the packaged desktop app MUST keep binding loopback — a '
            'laptop app must not inherit the LAN default')

    def test_open_auth_non_loopback_banner_survives(self):
        src = _src('server.py')
        assert 'API is reachable on the LAN' in src, (
            'the loud open-auth + non-loopback boot banner is the security '
            'tripwire that makes the wider default acceptable — do not '
            'remove it')

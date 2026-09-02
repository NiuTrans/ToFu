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
     and the rendered supervisor program) default BIND_HOST to ``0.0.0.0`` — an
     OOM-respawned server must not silently narrow the bind.
  3. The packaged DESKTOP app keeps its explicit loopback pin (a laptop app
     must not start serving the LAN just because the server default moved).
  4. The open-auth + non-loopback warning is exercised behaviorally by
     ``tests/test_server_boot_report.py``.

Run:  pytest tests/test_bind_lan_default.py -q -p no:napari -o addopts=
"""

import os
import re
from pathlib import Path
import sys

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

    @pytest.mark.skipif(os.name == 'nt', reason='system supervisord is Unix-only')
    def test_supervisor_conf_binds_all_interfaces(self, tmp_path):
        import importlib.util
        import pwd

        renderer_path = Path(REPO) / 'deploy' / 'supervisor' / 'render_config.py'
        spec = importlib.util.spec_from_file_location(
            'tofu_bind_default_renderer', renderer_path)
        assert spec is not None and spec.loader is not None
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        home = tmp_path / 'home'
        home.mkdir()
        rendered = renderer.render_config(
            project_root=Path(REPO),
            python_executable=Path(sys.executable),
            user_name=pwd.getpwuid(os.getuid()).pw_name,
            home_directory=home,
            port=15000,
        )
        assert 'BIND_HOST="0.0.0.0"' in rendered

    def test_packaged_desktop_app_keeps_its_loopback_pin(self):
        src = _src('desktop/launcher.py')
        assert "env['BIND_HOST'] = '127.0.0.1'" in src, (
            'the packaged desktop app MUST keep binding loopback — a '
            'laptop app must not inherit the LAN default')

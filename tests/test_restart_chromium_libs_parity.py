# Incident anchor: born in commit 4551701e — chromium-libs 双 launcher 不变量:supervisord conf 补 CHROMIUM_EXTRA_LIB_DI...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""tests/test_restart_chromium_libs_parity.py — the two-launcher invariant.

Measured hole (owner review, 2026-08-03): the FUSE root-cause fix for
headless-Chromium launch storms wires ``CHROMIUM_EXTRA_LIB_DIRS`` into
``restart_15000.sh`` — but ``deploy/supervisor/tofu.conf`` is a SECOND,
mutex-locked launcher (the restart script REFUSES to run while supervisord
owns tofu). Its ``environment=`` line carried only PORT/BIND_HOST/HOME/LANG,
so the moment the box flips to supervisor management (the documented durable
direction) the variable has NO path into the server process and the FUSE fix
silently evaporates — with every sign pointing at "already fixed".

The invariant: BOTH launchers must carry the override (and the fontconfig
half — env/etc/fonts rides the same FUSE mount, a bad window there renders
every glyph as nothing). This suite pins that structurally so a future edit
to one launcher can't strand the other.
"""

import os
import re

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


class TestTwoLauncherChromiumLibsParity:
    def test_restart_script_exports_override(self):
        src = _src('restart_15000.sh')
        assert 'CHROMIUM_EXTRA_LIB_DIRS' in src
        assert 'export CHROMIUM_EXTRA_LIB_DIRS' in src
        # Discovery follows the TOFU_BROWSER_LIBS_DIR convention with the
        # $HOME/tofu-browser-libs default.
        assert 'TOFU_BROWSER_LIBS_DIR' in src
        assert 'tofu-browser-libs' in src
        # Fontconfig half rides along (same FUSE mount, blank-glyph window).
        assert 'FONTCONFIG_FILE' in src

    def test_supervisor_conf_environment_carries_override(self):
        src = _src('deploy/supervisor/tofu.conf')
        env_lines = [l for l in src.splitlines()
                     if l.strip().startswith('environment=')]
        assert env_lines, 'tofu.conf must have an environment= line'
        env = env_lines[0]
        assert 'CHROMIUM_EXTRA_LIB_DIRS=' in env, (
            'the supervisord launcher must carry CHROMIUM_EXTRA_LIB_DIRS — '
            'restart_15000.sh refuses to run under supervisor ownership, so '
            'this line is the only path the variable has into the server')
        assert 'FONTCONFIG_FILE=' in env
        assert 'FONTCONFIG_PATH=' in env

    def test_both_launchers_agree_on_the_libs_path(self):
        """The two launchers must point at the SAME libs directory."""
        conf = _src('deploy/supervisor/tofu.conf')
        m = re.search(r'CHROMIUM_EXTRA_LIB_DIRS="([^"]+)"', conf)
        assert m, 'conf must pin an absolute CHROMIUM_EXTRA_LIB_DIRS'
        conf_dir = m.group(1)
        script = _src('restart_15000.sh')
        # The script derives <base>/lib from TOFU_BROWSER_LIBS_DIR /
        # $HOME/tofu-browser-libs; the conf's pinned path must be that same
        # default (<HOME>/tofu-browser-libs/lib).
        assert conf_dir.endswith('tofu-browser-libs/lib'), (
            f'conf pins {conf_dir!r} which drifts from the script convention '
            '($HOME/tofu-browser-libs/lib)')
        # Compare against the conf's OWN HOME pin, not the test host's ~: the
        # conf is a deployment artefact (the opensource export sanitizes it to
        # /home/your-user), so expanduser('~') made this guard red on every
        # machine but the one box the conf was written for. The invariant that
        # actually matters is INTERNAL consistency — the libs dir must sit
        # under the same HOME the supervised server will run with.
        hm = re.search(r'(?:^|[,\s])HOME="([^"]+)"', conf)
        assert hm, 'conf must pin HOME in its environment line'
        assert conf_dir == os.path.join(hm.group(1), 'tofu-browser-libs', 'lib'), (
            f'conf libs path {conf_dir!r} != conf HOME convention '
            f'{os.path.join(hm.group(1), "tofu-browser-libs", "lib")!r}')

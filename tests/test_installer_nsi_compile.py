"""tests/test_installer_nsi_compile.py — the makensis compile gate.

The template's REAL syntax gate: render both components (full / agent)
with genuine wrap-time art and a stub payload, then let makensis judge.
A rendered template that does not compile is a broken release pipeline —
and 32-bit Windows apps cannot run on this box's wine (SIGSYS), so the
compiler verdict is the strongest runtime-free gate available.

Skipped (not failed) when makensis is not provisioned — e.g. a minimal
CI runner without the desktop toolchain.

Run:  pytest tests/test_installer_nsi_compile.py -q
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.desktop_dist import installer_art, winbuilder as wb

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent


def _makensis() -> str | None:
    override = os.environ.get('TOFU_MAKENSIS', '').strip()
    if override and os.path.isfile(override):
        return override
    for cand in (
        shutil.which('makensis'),
        str(_ROOT / 'data' / 'desktop_toolchain' / 'cache' / 'tools'
            / 'bin' / 'makensis'),
    ):
        if cand and os.path.isfile(cand):
            return cand
    return None


@pytest.fixture(scope='module')
def makensis():
    exe = _makensis()
    if exe is None:
        pytest.skip('makensis not provisioned on this box')
    return exe


@pytest.mark.parametrize('target', ('full', 'agent'))
def test_rendered_template_compiles(makensis, tmp_path, target):
    tgt = wb._TARGETS[target]
    # Stub payload: the File /r glob and the exe-existence checks only
    # need the shape, not the 87-309 MB real thing.
    payload = tmp_path / 'payload'
    (payload / '_internal').mkdir(parents=True)
    (payload / tgt['exe']).write_bytes(b'MZ stub')
    (payload / '_internal' / 'x.txt').write_text('x')
    art_dir = tmp_path / 'art'
    installer_art.render(str(art_dir),
                         wb._NSI_TARGETS[target]['app_name'], '0.16.0',
                         autostart=bool(
                             wb._NSI_TARGETS[target]['autostart_value']))
    out_exe = tmp_path / 'setup.exe'
    nsi = wb._render_nsi('0.16.0', str(payload), str(out_exe), target,
                         art_dir=str(art_dir))
    script = tmp_path / 'installer.nsi'
    script.write_text(nsi, encoding='utf-8')
    proc = subprocess.run([makensis, '-V2', str(script)],
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, (
        f'{target} rendering failed to compile:\n'
        f'{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}')
    assert out_exe.is_file() and out_exe.stat().st_size > 0



def test_diag_variant_compiles(makensis, tmp_path):
    """The TOFU_DIAG seam (2026-08-06 measurement build) is dead text to
    the production compile gate above — without compiling WITH the
    define, a syntax error inside the ifdef branches would only surface
    when someone builds the diagnostic installer mid-incident. The agent
    target is the fullest path (AUTOSTART_PAGE injection)."""
    tgt = wb._TARGETS['agent']
    payload = tmp_path / 'payload'
    (payload / '_internal').mkdir(parents=True)
    (payload / tgt['exe']).write_bytes(b'MZ stub')
    (payload / '_internal' / 'x.txt').write_text('x')
    art_dir = tmp_path / 'art'
    installer_art.render(str(art_dir),
                         wb._NSI_TARGETS['agent']['app_name'], '0.16.0',
                         autostart=True)
    out_exe = tmp_path / 'setup-diag.exe'
    nsi = wb._render_nsi('0.16.0', str(payload), str(out_exe), 'agent',
                         art_dir=str(art_dir))
    script = tmp_path / 'installer.nsi'
    script.write_text(nsi, encoding='utf-8')
    proc = subprocess.run(
        [makensis, '-V2', '-DTOFU_DIAG=1', str(script)],
        capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, (
        'agent DIAG rendering failed to compile:\n'
        f'{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}')
    assert out_exe.is_file() and out_exe.stat().st_size > 0

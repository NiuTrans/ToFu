"""install.sh must install every requirements.txt dependency at its declared spec.

install.sh has an in-script drift guard, but it only runs ON THE TARGET
HOST and compares names only — a missing entry ships to users and kills the
install there (sh02, 2026-09-10: 10 uncovered deps, install died at the guard),
and a looser installer floor silently admits releases requirements.txt rejects
(CONDA_PKGS kept pillow>=10.0 under the Pillow>=13.0.0 security floor). This
test runs both comparisons in CI so the drift fails before it ships.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# Installed by dedicated steps, not the three arrays: tofu-search (own step),
# docling (--with-docling only). Keep aligned with install.sh's _EXEMPT.
_EXEMPT = {'tofu-search', 'docling'}

# The conda solve runs first, then one pip stage installs the PDF trio and the
# pip-only packages, so a later array's entry is the spec finally installed
# (conda's pymupdf floor only bootstraps old-glibc hosts).
_INSTALL_ORDER = ('CONDA_PKGS', 'PDF_STACK_PKGS', 'PIP_ONLY_PKGS')

#: Deliberate divergences where install.sh is STRICTER than requirements.txt.
#: The ratchet below requires each entry to still drift and its installer floor
#: to satisfy the declared spec; a looser installer spec is never exempt.
_SPEC_DRIFT_EXEMPT = {
    # conda-forge lxml>=6 keeps the solver off libxml2<2.14 / icu<76 (see the
    # CONDA_PKGS comment); requirements.txt keeps the bootstrap repair list's
    # >=5.3 baseline, and trafilatura>=2.3.1 already requires lxml>=6.1.3.
    'lxml',
}


def _norm(name: str) -> str:
    """Mirror install.sh's _norm: lowercase, strip [extras], strip the
    version spec, underscores → dashes, drop whitespace."""
    name = name.lower()
    name = re.sub(r'\[[^]]*\]', '', name)
    name = re.split(r'[<>=!~; ]', name, maxsplit=1)[0]
    return name.replace('_', '-').strip()


def _array_specs(installer: str, name: str) -> list[str]:
    block = re.search(rf'^{name}=\(\n(.*?)\n\)', installer, flags=re.M | re.S)
    assert block, f'{name} array not found in install.sh'
    # Bash ignores comments; quoted prose in them ("No module named pip") is
    # not an array entry. Package specs never contain '#'.
    code = '\n'.join(line.split('#', 1)[0] for line in block.group(1).splitlines())
    return re.findall(r'"([^"]+)"', code)


def _requirement_lines() -> list[str]:
    return [
        line.strip()
        for line in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ]


def _spec_comparison():
    """Return (shared names, {name: (installed spec, declared spec)} drift)."""
    installer = (ROOT / 'install.sh').read_text(encoding='utf-8')
    installed = {}
    for array in _INSTALL_ORDER:
        for entry in _array_specs(installer, array):
            installed[_norm(entry)] = Requirement(entry).specifier
    declared = {_norm(line): Requirement(line).specifier for line in _requirement_lines()}
    shared = set(installed) & set(declared)
    return shared, {name: (installed[name], declared[name])
                    for name in shared if installed[name] != declared[name]}


def test_install_sh_covers_requirements_txt():
    installer = (ROOT / 'install.sh').read_text(encoding='utf-8')
    covered = set().union(
        *({_norm(entry) for entry in _array_specs(installer, n)} for n in _INSTALL_ORDER),
        _EXEMPT,
    )
    declared = {_norm(line) for line in _requirement_lines()}
    missing = declared - covered
    assert not missing, (
        f'requirements.txt declares packages not installed by install.sh: '
        f'{sorted(missing)} — add them to CONDA_PKGS / PDF_STACK_PKGS / '
        f'PIP_ONLY_PKGS (or the exempt list) in install.sh')


def test_install_sh_installs_the_declared_version_specs():
    shared, drift = _spec_comparison()
    assert len(shared) >= 30, (
        'spec comparison found almost nothing to compare — the parser broke, '
        'so a green result here would be vacuous')
    unexpected = {name: f'install.sh {installed or "(any)"} vs requirements.txt {declared or "(any)"}'
                  for name, (installed, declared) in sorted(drift.items())
                  if name not in _SPEC_DRIFT_EXEMPT}
    assert not unexpected, (
        f'install.sh would install different versions than requirements.txt '
        f'declares: {unexpected}. Copy the requirements.txt spec into install.sh '
        f'rather than adding an exemption.')


def test_spec_drift_exemptions_still_drift_and_stay_stricter():
    _, drift = _spec_comparison()
    for name in sorted(_SPEC_DRIFT_EXEMPT):
        assert name in drift, (
            f'{name!r} no longer drifts — delete it from _SPEC_DRIFT_EXEMPT so '
            f'the spec comparison covers it again')
        installed, declared = drift[name]
        floors = [spec.version for spec in installed if spec.operator == '>=']
        assert floors and all(declared.contains(floor) for floor in floors), (
            f'{name!r}: install.sh {installed} is not stricter than '
            f'requirements.txt {declared}; only stricter specs may be exempt')


def test_in_script_guard_normalization_matches_this_test():
    """If install.sh's _norm drifts from _norm above, the two guards disagree
    and one of them goes blind. Pin the script-side normalization contract."""
    installer = (ROOT / 'install.sh').read_text(encoding='utf-8')
    assert r"s/\[[^]]*\]//g" in installer, (
        'install.sh _norm must strip [extras] — psycopg[binary,pool] is '
        'satisfied by conda psycopg + psycopg-pool')
    assert '"${PDF_STACK_PKGS[@]}"' in installer.split('_covered=')[1].split('\n', 1)[0], (
        'the drift guard must count PDF_STACK_PKGS as covered — pymupdf4llm '
        'and pymupdf_layout are installed from that array, not CONDA/PIP_ONLY')

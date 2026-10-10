"""install.sh's package lists must cover every dep in requirements.txt.

install.sh has an in-script drift guard, but it only runs ON THE TARGET
HOST — a missing entry ships to users and kills the install there (sh02,
2026-09-10: 10 uncovered deps, install died at the guard). This test runs
the same comparison in CI so the drift fails before it ships.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# Installed by dedicated steps, not the three arrays: tofu-search (own step),
# docling (--with-docling only). Keep aligned with install.sh's _EXEMPT.
_EXEMPT = {'tofu-search', 'docling'}


def _norm(name: str) -> str:
    """Mirror install.sh's _norm: lowercase, strip [extras], strip the
    version spec, underscores → dashes, drop whitespace."""
    name = name.lower()
    name = re.sub(r'\[[^]]*\]', '', name)
    name = re.split(r'[<>=!~; ]', name, maxsplit=1)[0]
    return name.replace('_', '-').strip()


def _array_entries(installer: str, name: str) -> set[str]:
    block = re.search(rf'^{name}=\(\n(.*?)\n\)', installer, flags=re.M | re.S)
    assert block, f'{name} array not found in install.sh'
    return {_norm(entry) for entry in re.findall(r'"([^"]+)"', block.group(1))}


def test_install_sh_covers_requirements_txt():
    installer = (ROOT / 'install.sh').read_text(encoding='utf-8')
    covered = set().union(
        *(_array_entries(installer, n)
          for n in ('CONDA_PKGS', 'PDF_STACK_PKGS', 'PIP_ONLY_PKGS')),
        _EXEMPT,
    )
    declared = {
        _norm(line)
        for line in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    }
    missing = declared - covered
    assert not missing, (
        f'requirements.txt declares packages not installed by install.sh: '
        f'{sorted(missing)} — add them to CONDA_PKGS / PDF_STACK_PKGS / '
        f'PIP_ONLY_PKGS (or the exempt list) in install.sh')


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

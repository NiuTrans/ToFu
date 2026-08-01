"""tests/test_clean_checkout_imports.py — this package must import from a CLEAN
CHECKOUT, not merely from the machine that wrote it.

WHY THIS EXISTS
---------------
Measured 2026-07-31: ``HEAD`` had been unimportable for THREE DAYS and nobody
noticed. ``tofu_search/search/vertical/__init__.py`` imports seven names from
``registry``; three of them — ``DOMAIN_META``, ``available_types``,
``describe_domains`` — existed only in an uncommitted working-tree change::

    ImportError: cannot import name 'DOMAIN_META' from
    tofu_search.search.vertical.registry

Introduced by ``ddbd504`` ("tighten the public surface", 2026-07-28 11:02) and
carried by all eight commits after it, INCLUDING the 0.5.3 feature commit. So
every version this repo could have published in that window was dead on
arrival.

It survived because every check ran somewhere the defect is invisible:

  * the author's shell resolves the package from the WORKING TREE, where the
    uncommitted edit supplies the missing names;
  * ``site-packages`` on that machine holds an installed ``tofu-search 0.5.2``,
    so even ``import tofu_search`` from another directory can succeed against a
    DIFFERENT copy than the one under test.

Both are the same failure: the thing being validated was not the thing that
would ship. Downstream, ``chatui`` declares this package a hard dependency and
its server calls ``install_search_bridge()`` at boot with no guard — so a
published broken build takes the whole app down at startup, not at first search.

WHAT MAKES THIS TEST HONEST
---------------------------
It refuses to trust the ambient environment:

  1. the package is exported with ``git archive HEAD`` into a temp dir, so the
     working tree cannot supply a missing symbol;
  2. the import runs in a SUBPROCESS with ``-S`` and a ``PYTHONPATH`` naming
     only that temp dir, and with ``sys.path`` scrubbed of any directory
     containing an installed ``tofu_search`` — otherwise a green result could
     come from the 0.5.2 in site-packages;
  3. it asserts every name in each package ``__all__`` actually resolves, not
     merely that the top-level import statement returned.

(3) matters because ``__all__`` is exactly where this defect lived: the module
imported and re-exported names that did not exist.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent

# Packages whose public surface must resolve from a clean checkout. The root
# package plus every subpackage that declares an __all__ — those are the
# re-export facades, which is where an unresolvable name hides.
_PACKAGES = (
    'tofu_search',
    'tofu_search.search',
    'tofu_search.search.vertical',
    'tofu_search.fetch',
    'tofu_search.verify',
)

_PROBE = r'''
import importlib, sys
failures = []
for name in {packages!r}:
    try:
        mod = importlib.import_module(name)
    except ImportError as e:
        failures.append(f"{{name}}: import failed -> {{type(e).__name__}}: {{e}}")
        continue
    except Exception as e:
        failures.append(f"{{name}}: import raised {{type(e).__name__}}: {{e}}")
        continue
    # The module object existing is not enough: __all__ is exactly where a
    # dangling re-export hides, because `from x import Y` only fails at the
    # moment Y is looked up.
    for sym in getattr(mod, "__all__", ()):
        if not hasattr(mod, sym):
            failures.append(f"{{name}}.__all__ advertises {{sym!r}} but it does not exist")
    # Prove we imported the CHECKOUT, not an installed copy.
    origin = getattr(mod, "__file__", "") or ""
    if not origin.startswith({root!r}):
        failures.append(f"{{name}} resolved to {{origin}} — NOT the clean checkout")
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("OK")
'''


def _tofu_search_root() -> Path | None:
    """Locate the sibling tofu-search checkout, or None when absent."""
    cand = _ROOT.parent / 'tofu-search'
    return cand if (cand / 'pyproject.toml').is_file() else None


@pytest.fixture(scope='module')
def clean_export():
    """`git archive HEAD` of tofu-search into a temp dir.

    Uses HEAD rather than the working tree ON PURPOSE — the working tree is
    precisely where the missing symbols came from, so testing it would repeat
    the mistake that let this ship.
    """
    src = _tofu_search_root()
    if src is None:
        pytest.skip('sibling tofu-search checkout not present')
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(['git', 'archive', 'HEAD'], cwd=src,
                              capture_output=True, timeout=120)
        if proc.returncode != 0:
            pytest.skip(f'git archive failed: {proc.stderr[:200]!r}')
        tar = subprocess.run(['tar', '-x', '-C', td], input=proc.stdout,
                             capture_output=True, timeout=120)
        if tar.returncode != 0:
            pytest.skip(f'tar failed: {tar.stderr[:200]!r}')
        yield td


def test_clean_checkout_imports_and_every_public_name_resolves(clean_export):
    """HEAD must import, and every ``__all__`` entry must actually exist."""
    code = _PROBE.format(packages=list(_PACKAGES), root=clean_export)
    env = dict(os.environ)
    # Only the export. Not the working tree, not site-packages' 0.5.2.
    env['PYTHONPATH'] = clean_export
    env['PYTHONNOUSERSITE'] = '1'
    proc = subprocess.run([sys.executable, '-c', code], cwd=clean_export,
                          capture_output=True, text=True, timeout=300, env=env)
    if proc.returncode != 0:
        pytest.fail(
            'tofu-search HEAD does not import from a clean checkout — it '
            'cannot be published:\n\n'
            f'{proc.stdout.strip()}\n{proc.stderr.strip()[-1500:]}\n\n'
            'This is invisible from the author machine, where the working '
            'tree (and an installed copy in site-packages) supply names HEAD '
            'lacks. chatui declares this package a hard dependency and calls '
            'install_search_bridge() at boot with no guard, so a published '
            'broken build kills the server at startup.'
        )


def test_the_probe_would_notice_a_missing_symbol(clean_export):
    """NEUTER-in-place: the check must actually be able to fail.

    A guard whose probe silently succeeds on a broken tree is worse than none.
    Here we delete a name the facade re-exports and require the probe to catch
    it — proving the __all__ sweep, not just the bare import, is load-bearing.
    """
    reg = (Path(clean_export) / 'tofu_search' / 'search' / 'vertical'
           / 'registry.py')
    if not reg.is_file():
        pytest.skip('registry.py not present in the export')
    original = reg.read_text(encoding='utf-8')
    if 'DOMAIN_META' not in original:
        pytest.skip('DOMAIN_META absent — sibling test already covers this')
    try:
        reg.write_text(original.replace('DOMAIN_META', '_NEUTERED_DOMAIN_META'),
                       encoding='utf-8')
        code = _PROBE.format(packages=list(_PACKAGES), root=clean_export)
        env = dict(os.environ)
        env['PYTHONPATH'] = clean_export
        env['PYTHONNOUSERSITE'] = '1'
        proc = subprocess.run([sys.executable, '-c', code], cwd=clean_export,
                              capture_output=True, text=True, timeout=300,
                              env=env)
        assert proc.returncode != 0, (
            'the probe reported OK after DOMAIN_META was removed — it is not '
            'actually checking the public surface'
        )
    finally:
        reg.write_text(original, encoding='utf-8')

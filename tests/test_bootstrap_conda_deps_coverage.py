#!/usr/bin/env python3
"""Drift guard for bootstrap.py's conda-forge dependency-repair list.

Tofu has historically maintained THREE parallel dependency lists — install.sh,
bootstrap.py's ``_CONDA_PYTHON_DEPS``, and requirements.txt — and they drift.
install.sh has its own drift guard; bootstrap.py's did not, and its conda list
had already fallen behind: it shipped only transitive ``flask`` (never the
actual ``quart``/``hypercorn`` ASGI stack the server runs on), and lacked
``orjson`` (REQUIRED for the SSE snapshot). On a CentOS-7-class host where the conda
repair path is the ONLY one that works (pip manylinux wheels crash on glibc
2.17), that meant the server could never boot after a dep repair.

This test locks two invariants:
  1. Every boot-critical package (``_CRITICAL_BOOT_PACKAGES``) is present in
     ``_CONDA_PYTHON_DEPS`` — so the conda repair path installs what boot needs.
  2. Every boot-critical package is also DECLARED in requirements.txt — so the
     guard tracks the real requirement, not an invented name, and the two lists
     stay coherent.

Pure text/parse over the repo files; no DB, no network, no conda.
"""
import os
import re
import sys
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The launcher facade intentionally performs environment re-exec at import
# time. Dependency declarations are owned by this side-effect-free leaf.
from bootstrap_pkg import install as bootstrap  # noqa: E402

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bare(spec: str) -> str:
    """Strip version specifiers / extras → bare lower-cased package name."""
    return re.split(r'[<>=!~\[ ]', spec, 1)[0].strip().lower()


def _spec(spec: str) -> str:
    """Return just the version-specifier tail of a dependency string.

    ``'mcp>=1.0,<2'`` → ``'>=1.0,<2'``; a bare ``'psutil'`` → ``''``. Extras are
    part of the name, not the spec, so ``'pkg[x]>=1'`` yields ``'>=1'`` only if
    the bare-name split already consumed the bracket.
    """
    bare = _bare(spec)
    tail = spec.strip()[len(bare):] if spec.strip().lower().startswith(bare) else ''
    # Drop an extras group so 'pkg[compile]>=1' compares against 'pkg>=1'.
    return re.sub(r'^\[[^\]]*\]', '', tail).strip()


def _requirements_specs() -> dict:
    """{bare name: version spec} for every live requirements.txt line."""
    out = {}
    with open(os.path.join(_ROOT, 'requirements.txt'), encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            b = _bare(line)
            if b:
                out[b] = _spec(line)
    return out


def _requirements_names() -> set[str]:
    names = set()
    with open(os.path.join(_ROOT, 'requirements.txt'), encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            b = _bare(line)
            if b:
                names.add(b)
    return names


class BootstrapCondaDepsCoverageTest(unittest.TestCase):

    def test_conda_list_covers_boot_critical_packages(self):
        conda_bare = {_bare(s) for s in bootstrap._CONDA_PYTHON_DEPS}
        for pkg in bootstrap._CRITICAL_BOOT_PACKAGES:
            self.assertIn(
                pkg.lower(), conda_bare,
                f'boot-critical package {pkg!r} missing from _CONDA_PYTHON_DEPS '
                f'— the conda repair path would install a non-bootable env')

    def test_boot_critical_packages_are_declared_in_requirements(self):
        req = _requirements_names()
        for pkg in bootstrap._CRITICAL_BOOT_PACKAGES:
            self.assertIn(
                pkg.lower(), req,
                f'boot-critical package {pkg!r} not declared in requirements.txt '
                f'— the drift guard must track a real requirement')

    def test_the_asgi_stack_is_present(self):
        """Explicit regression assertion for the exact drift that was found:
        quart + hypercorn (NOT just transitive flask) must be in the conda list."""
        conda_bare = {_bare(s) for s in bootstrap._CONDA_PYTHON_DEPS}
        self.assertIn('quart', conda_bare)
        self.assertIn('hypercorn', conda_bare)

    # ── Version-spec agreement between the two lists ──────────────────
    #
    # The coverage assertions above only check PRESENCE. That left a second
    # drift axis wide open, and 2026-07-29 showed what it costs: both lists
    # carried an UNBOUNDED ``mcp>=1.0`` while the SDK shipped a breaking 2.0.0.
    # ``_CONDA_PYTHON_DEPS`` feeds the PRE-BOOT installer, so an unbounded spec
    # there installs the breaking major into Tofu's own interpreter before the
    # app starts — and no existing guard looked at bounds at all.
    #
    # Note the division of labour: tests/test_mcp_sdk_pin_bounded.py owns "is
    # every MCP spec bounded"; THIS test owns "do the two lists say the SAME
    # thing", which is the drift that makes one installer disagree with the
    # other regardless of which package it happens to.

    #: Packages whose specs legitimately differ between the two lists.
    #: Each entry is a KNOWN, PRE-EXISTING divergence that predates this guard
    #: and is tracked separately — not a licence to add more. The ratchet below
    #: asserts each exemption is STILL drifting, so a fixed one must be deleted
    #: from this set rather than lingering as dead cover for a future drift.
    #:
    #: Currently EMPTY, and that is the goal state: the only entry it ever had
    #: (``lxml``: conda ``>=5.3`` vs requirements ``>=4.9``) was fixed by
    #: aligning requirements.txt to ``>=5.3``, which is the floor our own
    #: declared dependencies already impose (trafilatura 2.0.0 requires
    #: ``lxml>=5.3.0``), so the old floor was dead text that only disagreed
    #: with the other list. Per the ratchet below, the entry HAD to be deleted
    #: the moment it stopped drifting.
    _SPEC_DRIFT_EXEMPT: set[str] = set()

    def _shared_specs(self):
        """{pkg: (conda_spec, req_spec)} for packages present in BOTH lists."""
        conda = {_bare(s): _spec(s) for s in bootstrap._CONDA_PYTHON_DEPS}
        req = _requirements_specs()
        return {p: (conda[p], req[p]) for p in (set(conda) & set(req))}

    def test_shared_packages_declare_identical_specs(self):
        """A package in both lists must carry the SAME version spec.

        Two installers that disagree about a version produce an environment
        whose behaviour depends on which path ran — the exact class of bug the
        conda/pip split keeps reproducing.
        """
        shared = self._shared_specs()
        self.assertGreaterEqual(
            len(shared), 10,
            'spec comparison found almost nothing to compare — the parser '
            'broke, so a green result here would be vacuous')
        for pkg, (conda_spec, req_spec) in sorted(shared.items()):
            if pkg in self._SPEC_DRIFT_EXEMPT:
                continue
            self.assertEqual(
                conda_spec, req_spec,
                f'{pkg!r} spec drift: _CONDA_PYTHON_DEPS says {conda_spec!r} '
                f'but requirements.txt says {req_spec!r}. The two installers '
                f'would build different environments. Fix the mismatch rather '
                f'than adding an exemption.')

    def test_drift_exemptions_are_still_drifting(self):
        """Ratchet: an exemption that no longer drifts MUST be deleted.

        Without this, the exemption set silently becomes permanent cover — a
        package could be fixed, then drift again later, and the guard would
        never notice because its name was still on the list.
        """
        shared = self._shared_specs()
        for pkg in sorted(self._SPEC_DRIFT_EXEMPT):
            self.assertIn(
                pkg, shared,
                f'{pkg!r} is exempted from spec-drift checking but is no '
                f'longer in both lists — delete the exemption')
            conda_spec, req_spec = shared[pkg]
            self.assertNotEqual(
                conda_spec, req_spec,
                f'{pkg!r} no longer drifts (both say {conda_spec!r}) — remove '
                f'it from _SPEC_DRIFT_EXEMPT so the guard covers it again')

    def test_mcp_spec_is_bounded_in_the_preboot_installer(self):
        """``mcp`` in ``_CONDA_PYTHON_DEPS`` carries an upper bound.

        Called out by name because this list is the PRE-BOOT installer and the
        MCP SDK's 2.0.0 is a breaking rework (renamed transport factory, changed
        tuple arity, snake_case fields). ``mcp`` is deliberately NOT added to
        ``_CRITICAL_BOOT_PACKAGES``: the server boots fine without MCP, and
        claiming otherwise would make that list dishonest.
        """
        specs = {_bare(s): _spec(s) for s in bootstrap._CONDA_PYTHON_DEPS}
        self.assertIn('mcp', specs,
                      'mcp vanished from _CONDA_PYTHON_DEPS — if that is '
                      'deliberate, delete this assertion on purpose')
        self.assertRegex(
            specs['mcp'], r'(?:<|==|~=)',
            f'pre-boot installer declares an UNBOUNDED mcp spec '
            f'({specs["mcp"]!r}); `pip install mcp` resolves to the breaking '
            f'2.x. Use ">=1.0,<2".')


if __name__ == '__main__':
    unittest.main()

"""Rejection coverage for the documentation catalog gate (item N).

``scripts/check_documentation.py`` is the only gate script with no direct
rejection test: a regression that makes it silently accept an uncataloged
``docs/*.md`` would land green. These tests pin the two failure behaviours
that carry the gate's value — an on-disk doc absent from ``docs/catalog.json``
is rejected with a precise diagnostic, and the catalog-staleness diagnostic
names the offending path — plus a static meta-test asserting every
``scripts/check_*.py`` gate has at least one rejection test referencing it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import check_documentation as documentation

# Intentional references to paths that do not (and must not)
# exist in the real repo — negative-case fixture anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = {'docs/EXISTING.md', 'docs/GHOST.md', 'docs/NEW_UNCATALOGED.md'}


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"


def _isolate_catalog(monkeypatch, tmp_path, present: set[str], cataloged: set[str]) -> None:
    """Point the gate at a synthetic catalog file + repository doc set.

    The gate reads every doc in ``present ∩ cataloged`` from ``ROOT / path``,
    so a patched ``ROOT`` under ``tmp_path`` must actually contain those files.
    """
    import json

    for relative in present & cataloged:
        document = tmp_path / relative
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text("# Title\n", encoding="utf-8")

    catalog = {
        "version": 1,
        "policy": {"maximum_lines_by_group": {"current": 1000}},
        "current": sorted(cataloged),
        "contracts": [],
    }
    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(documentation, "ROOT", tmp_path)
    monkeypatch.setattr(documentation, "CATALOG_PATH", catalog_file)
    monkeypatch.setattr(
        documentation, "_repository_markdown", lambda: set(present)
    )
    monkeypatch.setattr(documentation, "_repository_contracts", lambda: set())


def test_uncataloged_document_is_rejected(monkeypatch, tmp_path, capsys):
    """A doc present on disk but absent from the catalog must fail the gate."""
    _isolate_catalog(
        monkeypatch,
        tmp_path,
        present={"docs/EXISTING.md", "docs/NEW_UNCATALOGED.md"},
        cataloged={"docs/EXISTING.md"},
    )

    assert documentation.main() == 1
    err = capsys.readouterr().err
    assert "documentation-check: FAILED" in err
    assert "uncataloged document: docs/NEW_UNCATALOGED.md" in err


def test_missing_cataloged_document_is_rejected(monkeypatch, tmp_path, capsys):
    """A catalog row with no on-disk doc must fail the gate with the path."""
    _isolate_catalog(
        monkeypatch,
        tmp_path,
        present=set(),
        cataloged={"docs/GHOST.md"},
    )

    assert documentation.main() == 1
    err = capsys.readouterr().err
    assert "cataloged document is missing: docs/GHOST.md" in err


def test_every_gate_script_has_a_rejection_test():
    """Meta-test: every scripts/check_*.py gate is referenced by a rejection test.

    A gate with no test that exercises its failure path can silently regress to
    always-pass. For each gate we require a test file that both references the
    gate module and asserts a rejection signal (a ``== 1`` main return or a
    ``FAILED`` diagnostic). This is a static structural check — it reads test
    sources, not their behaviour.
    """
    # Durable content gates whose silent always-pass regression loses user
    # value (catalog staleness, architecture rule, helm/runtime contract). The
    # remaining check_*.py scripts (function-size ratchet, monorepo contract)
    # are pass/fail but outside item N's scope; they are listed here so the
    # meta-test stays honest rather than blanket-green.
    required_gates = {
        "check_documentation.py",
        "check_architecture.py",
        "check_helm_render.py",
        "check_developer_runtime_artifacts.py",
    }
    gates = sorted(
        path for path in SCRIPTS.glob("check_*.py") if path.name in required_gates
    )
    assert {p.name for p in gates} == required_gates

    test_sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in TESTS.glob("**/test_*.py")
    }
    rejection_signal = re.compile(r"(== 1|FAILED|validate_)")

    uncovered: list[str] = []
    for gate in gates:
        stem = gate.stem  # e.g. check_documentation
        covered = any(
            (stem in source or gate.name in source) and rejection_signal.search(source)
            for source in test_sources.values()
        )
        if not covered:
            uncovered.append(gate.name)

    assert not uncovered, (
        "gate script(s) with no rejection test: " + ", ".join(uncovered)
    )

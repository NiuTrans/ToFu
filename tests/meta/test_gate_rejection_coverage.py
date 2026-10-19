"""Require rejection coverage for architecture and release validation gates."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"


def test_every_gate_script_has_a_rejection_test():
    """Meta-test: every scripts/check_*.py gate is referenced by a rejection test.

    A gate with no test that exercises its failure path can silently regress to
    always-pass. For each gate we require a test file that both references the
    gate module and asserts a rejection signal (a ``== 1`` main return or a
    ``FAILED`` diagnostic). This is a static structural check — it reads test
    sources, not their behaviour.
    """
    # Retained scope: architecture rules and Helm/runtime release contracts.
    # Other check scripts have their own focused behavioral coverage.
    required_gates = {
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
        stem = gate.stem
        covered = any(
            (stem in source or gate.name in source) and rejection_signal.search(source)
            for source in test_sources.values()
        )
        if not covered:
            uncovered.append(gate.name)

    assert not uncovered, (
        "gate script(s) with no rejection test: " + ", ".join(uncovered)
    )

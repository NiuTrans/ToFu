"""Concept-index drift guard + resolver controls.

``docs/CONCEPT_INDEX.md`` joins each change concept from ``docs/README.md``'s
documentation map to its owning code paths, cataloged contracts, contract
guard tests, and the focused tests named by the linked domain/authority
documents. It exists so a contributor (human or model) touching one layer can
see the semantic neighbourhood of the change in one row instead of
discovering it through a distant test failure. An index that silently goes
stale is worse than none, so this file pins three things:

1. **Drift guard** — the committed index matches what
   ``scripts/gen_concept_index.py`` derives from the live sources.
2. **Coverage** — every change-table row in ``docs/README.md`` is indexed,
   and every concept with a contract also carries that contract's catalog
   guard test (a guard cell that silently stayed empty would defeat the join).
3. **Resolver controls** — the strict test-reference resolver provably fails
   on an unresolvable reference (positive control), so an empty focused-tests
   cell means "the owning document names no tests", never "the resolver
   stopped working".
"""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

GEN = "scripts/gen_concept_index.py"


def _repo_root():
    import os
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _generator():
    sys.path.insert(0, _repo_root())
    from scripts import gen_concept_index
    return gen_concept_index


class TestIndexNotStale:
    def test_committed_index_matches_sources(self):
        """`--check` must pass: the committed index reflects the live docs."""
        proc = subprocess.run(
            [sys.executable, GEN, "--check"],
            cwd=_repo_root(), capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, (
            f"concept index is stale or its sources are broken — run "
            f"`python3 {GEN}` and commit docs/CONCEPT_INDEX.md.\n"
            f"stderr: {proc.stderr[-2000:]}"
        )


class TestConceptCoverage:
    def test_every_readme_change_row_is_indexed_in_order(self):
        """The README change table is the concept source; nothing may drop."""
        gen = _generator()
        readme = (_repo_root() + "/docs/README.md")
        with open(readme, encoding="utf-8") as handle:
            expected = [concept for concept, _, _
                        in gen.parse_change_table(handle.read())]
        data = gen.collect()
        assert not data["failures"], (
            f"concept sources have stale references: {data['failures']}"
        )
        actual = [row["concept"] for row in data["concepts"]]
        assert actual == expected, (
            f"index rows diverge from the README change table: "
            f"missing={sorted(set(expected) - set(actual))} "
            f"extra={sorted(set(actual) - set(expected))}"
        )

    def test_every_contract_carries_its_catalog_guard(self):
        """A concept with a contract must also name the contract's guard.

        The guard join is the point of the index: a contract without its
        guard test in the same row is a wire change waiting to break a
        distant suite. ``docs/catalog.json`` requires a guard per contract,
        so a non-empty contract cell must imply a non-empty guard cell.
        """
        gen = _generator()
        data = gen.collect()
        unguarded = [row["concept"] for row in data["concepts"]
                     if row["contracts"] and not row["guards"]]
        assert unguarded == [], (
            f"concepts whose contracts lost their catalog guard: {unguarded}"
        )

    def test_every_concept_names_at_least_one_owner_path(self):
        """A row with no owner path can never be reached from code."""
        gen = _generator()
        data = gen.collect()
        orphan = [row["concept"] for row in data["concepts"]
                  if not row["owners"]]
        assert orphan == [], (
            f"concepts with no runtime owner path: {orphan}"
        )


class TestResolverControls:
    def test_positive_control_unresolvable_reference_fails(self):
        """A document naming a nonexistent test MUST record a failure.

        Without this control, an empty focused-tests cell is
        indistinguishable from a resolver that stopped resolving — the same
        reason the tool inventory keeps a positive control for its detector.
        """
        gen = _generator()
        _, failures = gen.document_tests(
            "Specs: `test_no_such_file_anywhere_zzz.py`", {})
        assert failures, (
            "the strict resolver failed its POSITIVE CONTROL: a nonexistent "
            "test reference produced no failure, so empty cells mean nothing"
        )

    def test_negative_control_real_basename_resolves(self):
        """A genuine bare test name resolves to exactly one repo path."""
        gen = _generator()
        resolved, failures = gen.document_tests(
            "Specs: `test_concept_index_generated.py`",
            gen._test_basename_index())
        assert failures == []
        assert resolved == ["tests/meta/test_concept_index_generated.py"]

    def test_pytest_node_ids_and_globs_resolve(self):
        """`path::node` and `glob` references resolve to their files."""
        gen = _generator()
        resolved, failures = gen.document_tests(
            "Specs: `tests/meta/test_concept_index_generated.py::TestIndexNotStale` "
            "and `tests/meta/test_concept_index_generate*.py`",
            gen._test_basename_index())
        assert failures == []
        assert resolved == ["tests/meta/test_concept_index_generated.py"]

"""Offline harness contracts; production matcher/preflight runs are never mocked."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from evaluations.edit_file.comparison import ARMS, REPOSITORY, _validate_cases, run_comparison
from evaluations.edit_file.dataset import CASES_BY_ID
from evaluations.edit_file.paired_dataset import CODEX_REFERENCES, PAIRED_CASES, PAIRED_CASES_BY_ID


pytestmark = pytest.mark.unit

@pytest.fixture(scope="module")
def comparison(tmp_path_factory):
    scratch = tmp_path_factory.mktemp("edit-file-comparison")
    report = run_comparison(scratch)
    assert list(scratch.iterdir()) == []
    return report


def _rows(report):
    return {case["id"]: case for case in report["cases"]}


def test_complete_evidence_separates_execution_from_static_review(comparison):
    assert comparison["case_count"] == len(PAIRED_CASES)
    assert comparison["model_calls"] == comparison["native_codex_calls"] == 0
    assert comparison["native_codex"]["executed"] is False
    assert comparison["native_codex"]["source_verification"] == "not_available_in_this_run"
    assert comparison["forbidden_effect_attempts"] == []
    assert comparison["atomic_default"] is True
    assert len(comparison["source"]["sha256"]) > 0
    diagnostics = comparison["diagnostics"]
    assert diagnostics["observed_trials"] == len(PAIRED_CASES) * len(ARMS)
    assert diagnostics["missing_trials"] == diagnostics["excess_trials"] == []
    assert diagnostics["exception_counts"] == diagnostics["evidence_issue_counts"] == {}
    assert diagnostics["formal_score"] is None
    for case in comparison["cases"]:
        assert set(case["measured"]) == set(ARMS)
        assert case["native_codex"]["evidence_kind"] == "static_source_review_not_executed"
        assert set(case["native_codex"]["references"]) <= set(CODEX_REFERENCES)
        for result in case["measured"].values():
            assert result["evidence_kind"] == "executed_production_python"


@pytest.mark.parametrize("case_id", list(CASES_BY_ID))
def test_existing_corpus_uses_actual_tool_and_exact_postimages(comparison, case_id):
    case = _rows(comparison)[case_id]
    assert case["files"] == CASES_BY_ID[case_id]["files"]
    for result in case["measured"].values():
        assert result["status"] == "applied"
        assert result["files"] == CASES_BY_ID[case_id]["expected"]
        assert result["reference_postimage_matches"]


@pytest.mark.parametrize("case_id", ["nonexistent_guard", "nonexistent_call", "nonexistent_identifier",
                                    "significant_string_space", "changed_relative_indentation", "repeated_anchor"])
def test_missing_or_unsafe_anchor_never_mutates(comparison, case_id):
    case = _rows(comparison)[case_id]
    for result in case["measured"].values():
        assert result["status"] == "rejected"
        assert result["files"] == case["files"]
        assert result["changed_paths"] == []


@pytest.mark.parametrize("case_id", ["partial_same_file", "partial_multiple_files"])
def test_preflight_rejection_differs_from_explicit_partial_policy(comparison, case_id):
    case = _rows(comparison)[case_id]
    for arm, result in case["measured"].items():
        if ARMS[arm][1]:
            assert result["status"] == "rejected"
            assert result["files"] == case["files"]
        else:
            assert result["status"] == "partial"
            assert result["files"] == case["intended_files"]
        assert result["failed_edits"] >= 1
    if case_id == "partial_multiple_files":
        static = case["native_codex"]
        assert static["standalone_expected"]["status"] == "partial"
        assert static["standalone_expected"]["files"]["c.py"] == case["files"]["c.py"]
        assert static["verified_expected"]["files"] == case["files"]


@pytest.mark.parametrize("case_id", ["evolving_hunk_dependency", "reverse_original_hunks", "duplicate_file_headers"])
def test_actual_evolving_state_is_not_mislabeled_native_codex(comparison, case_id):
    case = _rows(comparison)[case_id]
    for result in case["measured"].values():
        assert result["status"] == "applied"
        assert result["files"] == case["intended_files"]
    assert case["native_codex"]["verified_expected"]["status"] == "rejected"


def test_existing_converter_limitations_remain_visible(comparison):
    rows = _rows(comparison)
    for case_id in ("repeated_anchor_explicit_all", "landmark_selects_second"):
        results = rows[case_id]["measured"]
        assert results["tofu_anchor_atomic"]["status"] == "applied"
        assert results["tofu_converted_patch_atomic"]["status"] == "rejected"
        assert results["tofu_converted_patch_atomic"]["reference_postimage_matches"] is False
    truncated = rows["truncated_patch"]
    assert truncated["measured"]["tofu_converted_patch_atomic"]["status"] == "applied"
    assert truncated["native_codex"]["standalone_expected"]["status"] == "rejected"


def test_byte_postimage_differences_are_preserved_not_normalized_away(comparison):
    rows = _rows(comparison)
    whitespace = rows["trailing_whitespace"]["measured"]
    assert whitespace["tofu_anchor_atomic"]["files"] == {"sample.py": "value = 2\n"}
    assert whitespace["tofu_converted_patch_atomic"]["files"] == {"sample.py": "value = 2  \n"}
    offset = rows["uniform_indentation_offset"]
    assert offset["measured"]["tofu_anchor_atomic"]["files"] == {
        "sample.py": "def run():\n      begin()\n      report()\n"}
    assert offset["measured"]["tofu_anchor_atomic"]["reference_postimage_matches"] is False
    assert offset["native_codex"]["standalone_expected"]["files"] == offset["intended_files"]


def test_small_run_is_repeatable_and_cleanup_is_owned(tmp_path):
    sentinel = tmp_path / "caller-owned.txt"
    sentinel.write_text("untouched")
    ids = ["insert_between_blocks", "nonexistent_guard", "partial_multiple_files"]
    first = run_comparison(tmp_path, case_ids=ids)
    second = run_comparison(tmp_path, case_ids=ids)
    assert first == second
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_text() == "untouched"


@pytest.mark.parametrize("mutation", ["path_escape", "patch_escape", "duplicate", "oversize"])
def test_fixture_admission_fails_before_execution(mutation):
    cases = [copy.deepcopy(PAIRED_CASES_BY_ID["replace_value"])]
    if mutation == "path_escape":
        cases[0]["files"] = {"../escape.py": "bad"}
    elif mutation == "patch_escape":
        cases[0]["patch"] = "*** Begin Patch\n*** Update File: ../escape.py\n@@\n-a\n+b\n*** End Patch\n"
    elif mutation == "duplicate":
        cases.append(copy.deepcopy(cases[0]))
    else:
        cases[0]["files"]["config.py"] = "x" * 20_001
    with pytest.raises(ValueError):
        _validate_cases(cases)


def test_child_audit_guard_denies_external_effects(tmp_path):
    root = tmp_path / "guarded"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged")
    (root / "escape.txt").symlink_to(outside)
    script = """
import json, pathlib, socket, subprocess, sys
from evaluations.edit_file.comparison import _install_scratch_guard
root = pathlib.Path(sys.argv[1])
blocked = _install_scratch_guard(root)
for attempt in (
    lambda: (root.parent / 'outside.txt').write_text('bad'),
    lambda: (root / 'escape.txt').write_text('bad'),
    lambda: socket.socket(),
    lambda: subprocess.run([sys.executable, '-c', 'print(1)']),
):
    try:
        attempt()
    except PermissionError:
        pass
    else:
        raise AssertionError('forbidden effect was admitted')
(root / 'allowed.txt').write_text('allowed')
print(json.dumps(blocked))
"""
    env = {"PATH": os.defpath, "PYTHONPATH": str(REPOSITORY), "HOME": str(root),
           "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-B", "-c", script, str(root)], cwd=root,
                          env=env, capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(proc.stdout) == ["open", "open", "socket.__new__", "subprocess.Popen"]
    assert outside.read_text() == "unchanged"
    assert (root / "allowed.txt").read_text() == "allowed"

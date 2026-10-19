"""Run deterministic edit semantics, never a model or native-Codex approximation.

Usage: python -B -m evaluations.edit_file.comparison --scratch-root .eval-runs/edit-file
       [--codex-source /verified/read-only/codex] > comparison.json

Runs the actual unwrapped tool_edit_file implementation and the existing
parse_codex_patch converter. The workspace lease decorator is deliberately not
entered: its global/Git lock writes are outside a hermetic scratch directory.
Matching, preflight and temp-file commits are NOT replaced. This is not an
end-to-end authorization, lease, journal, race, rollback or model evaluation.
Native Codex fixtures are static source review only, with no guessed matcher.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from evaluations.evidence import TrialObservation, summarize_trials
from evaluations.source_identity import safe_relative
from .paired_dataset import CODEX_REFERENCES, CODEX_REVISION, PAIRED_CASES, PAIRED_CASES_BY_ID

REPOSITORY = Path(__file__).resolve().parents[2]
ARMS = {
    "tofu_anchor_atomic": (False, True),
    "tofu_anchor_partial": (False, False),
    "tofu_converted_patch_atomic": (True, True),
    "tofu_converted_patch_partial": (True, False),
}
TOFU_SOURCES = (
    "evaluations/edit_file/dataset.py", "evaluations/edit_file/paired_dataset.py",
    "evaluations/edit_file/comparison.py", "evaluations/evidence.py",
    "lib/project_mod/write_tools/_ops.py", "lib/project_mod/write_tools/_post_edit_checks.py",
    "lib/project_mod/write_tools/_text.py", "lib/project_mod/write_tools/_paths.py",
    "lib/tool_input_repair/_codex_patch.py",
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _validate_cases(cases):
    if not 1 <= len(cases) <= 32 or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("comparison requires 1..32 unique frozen cases")
    for case in cases:
        safe_relative(case["id"])
        if len(case["files"]) > 8 or not 1 <= len(case["edits"]) <= 30:
            raise ValueError("fixture file/edit budget exceeded")
        for path, text in case["files"].items():
            safe_relative(path)
            if len(text.encode()) > 20_000:
                raise ValueError("fixture byte budget exceeded")
        for edit in case["edits"]:
            if edit["path"] not in case["files"]:
                raise ValueError("edit must target an admitted fixture file")
        for line in case["patch"].splitlines():
            if line.startswith("*** Update File: ") and line[17:] not in case["files"]:
                raise ValueError("patch must target an admitted fixture file")
        if len(case["patch"].encode()) > 40_000:
            raise ValueError("patch byte budget exceeded")


def _install_scratch_guard(root):
    """Fail closed on accidental Python-level external side effects in the child.

    Not an OS sandbox for hostile native code. The only executed code is the
    reviewed local Python implementation. Child process execution is forbidden.
    """
    root = Path(root).resolve(strict=True)
    blocked = []

    def deny(event):
        blocked.append(event)
        raise PermissionError("offline comparison forbids " + event)

    def check_path(value, event):
        if isinstance(value, int):
            return
        path = Path(os.fsdecode(value))
        if not path.resolve().is_relative_to(root):
            deny(event)

    def audit(event, args):
        if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork"}:
            deny(event)
        if event == "open":
            path, mode, flags = args
            if (mode and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                check_path(path, event)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.chown", "os.truncate", "os.utime"}:
            check_path(args[0], event)
        elif event in {"os.rename", "os.link", "os.symlink"}:
            check_path(args[0], event)
            check_path(args[1], event)

    sys.addaudithook(audit)
    return blocked


def _outcome(before, after, output):
    failures = len(re.findall(r"^\[\d+\] FAIL\b", output, re.MULTILINE))
    failed = bool(failures or output.startswith("Error") or "preflight failed" in output.lower())
    changed = sorted(path for path in before if before[path] != after[path])
    status = ("partial" if changed else "rejected") if failed else ("applied" if changed else "unchanged")
    return {"status": status, "changed_paths": changed, "failed_edits": failures,
            "error_kinds": sorted(set(re.findall(r"\[([A-Z][A-Z_]+)\]", output))),
            "files": after, "tool_output": output}


def _run_worker(root, case_ids):
    root = Path(root).resolve(strict=True)
    blocked = _install_scratch_guard(root)
    from lib.project_mod.write_tools import tool_edit_file
    from lib.tool_input_repair._codex_patch import parse_codex_patch

    # This is the existing function, not copied code or a stand-in matcher.
    edit_file = inspect.unwrap(tool_edit_file)
    if "atomic" not in inspect.signature(edit_file).parameters:
        raise RuntimeError("production edit_file lacks the required atomic preflight policy")
    cases = [PAIRED_CASES_BY_ID[case_id] for case_id in case_ids]
    _validate_cases(cases)
    rows = []
    for case in cases:
        measured = {}
        for arm, (converted, atomic) in ARMS.items():
            project = root / "tmp" / case["id"] / arm
            project.mkdir(parents=True)
            for path, text in case["files"].items():
                destination = project / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text, encoding="utf-8")
            arguments = {"edits": copy.deepcopy(case["edits"])}
            conversion_error = ""
            if converted:
                arguments, conversion_error = parse_codex_patch(case["patch"])
            if arguments is None:
                output = "Error [PATCH_CONVERSION_REJECTED]: " + conversion_error
            else:
                if set(arguments) - {"edits", "description"}:
                    raise RuntimeError("fixture conversion unexpectedly requested another tool")
                for edit in arguments["edits"]:
                    if edit["path"] not in case["files"]:
                        raise RuntimeError("converter emitted a non-fixture path")
                output = edit_file(str(project), arguments["edits"], atomic=atomic)
            after = {path: (project / path).read_text(encoding="utf-8") for path in case["files"]}
            outcome = _outcome(case["files"], after, output.replace(str(project), "<case-root>"))
            # Partial-policy fixtures deliberately have a different preflight postimage.
            reference = (case["files"] if atomic and case["category"] == "partial_preflight"
                         else case["intended_files"])
            measured[arm] = {"evidence_kind": "executed_production_python", "atomic": atomic,
                             "reference_postimage_matches": after == reference,
                             "converted_arguments": arguments if converted else None, **outcome}
        rows.append({**case, "measured": measured})
    if blocked:
        raise RuntimeError("production execution attempted forbidden effects: " + ", ".join(blocked))
    return {"cases": rows, "atomic_default": inspect.signature(edit_file).parameters["atomic"].default,
            "forbidden_effect_attempts": blocked}


def _source_fingerprint(root, paths):
    root = Path(root).resolve(strict=True)
    def git(*args):
        proc = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=10, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None
    files = {}
    for path in paths:
        safe_relative(path)
        source = root / path
        if source.is_symlink() or not source.is_file():
            raise ValueError("missing or symlinked source: " + path)
        files[path] = hashlib.sha256(source.read_bytes()).hexdigest()
    return {"revision": git("rev-parse", "HEAD"), "sha256": files,
            "dirty_paths": git("status", "--porcelain=v1", "--untracked-files=all", "--", *paths)}


def _native_evidence(codex_source):
    evidence = {"executed": False, "kind": "static_source_review_not_executed",
                "reviewed_revision": CODEX_REVISION, "references": CODEX_REFERENCES,
                "reason": "No native Codex executable is invoked or approximated by this harness."}
    if codex_source is None:
        evidence["source_verification"] = "not_available_in_this_run"
        return evidence
    paths = sorted({r["path"] for r in CODEX_REFERENCES.values()})
    paths.extend(["codex-rs/apply-patch/Cargo.toml", "codex-rs/apply-patch/src/standalone_executable.rs"])
    source = _source_fingerprint(codex_source, paths)
    evidence["source"] = source
    evidence["source_verification"] = ("matches_reviewed_revision" if source["revision"] == CODEX_REVISION
                                       and source["dirty_paths"] == "" else "differs_from_reviewed_revision")
    evidence["bounded_build_probe"] = {name: (Path(codex_source) / name).exists() for name in (
        "codex-rs/target", "target", "bazel-bin", "codex-rs/target/debug/apply_patch",
        "codex-rs/target/release/apply_patch")}

    evidence["execution_limitation"] = (
        "Only the listed build locations were probed, not every host binary/cache. No vendored build was run; "
        "Cargo.toml depends on workspace crates including codex-exec-server, Tokio and tree-sitter. "
        "An unrelated installed Codex binary is not evidence of this reviewed source's behavior.")
    return evidence


def run_comparison(scratch_root, *, codex_source=None, case_ids=None):
    """Measure frozen cases in a bounded child. Returns JSON-compatible evidence.

    scratch_root must be a caller-owned test/evaluation directory. All mutable
    worker data is temporary and removed on success or failure. The optional
    Codex source is read-only provenance; it never becomes a runtime dependency.
    """
    case_ids = list(case_ids) if case_ids is not None else [c["id"] for c in PAIRED_CASES]
    cases = [PAIRED_CASES_BY_ID[case_id] for case_id in case_ids]
    _validate_cases(cases)
    scratch_root = Path(scratch_root).resolve()
    scratch_root.mkdir(parents=True, exist_ok=True)
    source_before = _source_fingerprint(REPOSITORY, TOFU_SOURCES)
    native = _native_evidence(codex_source)
    with tempfile.TemporaryDirectory(prefix="paired-edit-", dir=scratch_root) as directory:
        root = Path(directory)
        for name in ("tmp", "home", "runtime", "cache"):
            (root / name).mkdir()
        # Do not inherit credentials, application settings, or provider routing.
        env = {"PATH": os.defpath, "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
               "TOFU_DATA_DIR": str(root / "runtime"), "XDG_CACHE_HOME": str(root / "cache"),
               "XDG_DATA_HOME": str(root / "runtime"), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONHASHSEED": "0", "PYTHONPATH": str(REPOSITORY), "LANG": "C.UTF-8"}
        proc = subprocess.run([sys.executable, "-B", "-m", "evaluations.edit_file.comparison",
                               "--worker", str(root), "--case-ids", json.dumps(case_ids)],
                              cwd=root, env=env, text=True, capture_output=True, timeout=90, check=False)
        if proc.returncode:
            raise RuntimeError("offline comparison worker failed: " + proc.stderr[-8000:])
        measured = json.loads(proc.stdout)
    source_after = _source_fingerprint(REPOSITORY, TOFU_SOURCES)
    if source_before != source_after:
        raise RuntimeError("source changed during comparison; rerun on a stable candidate")
    trials = [TrialObservation(identity=f"{case['id']}:{arm}", task=case["id"], model=arm,
                              observations=(result["status"],))
              for case in measured["cases"] for arm, result in case["measured"].items()]
    return {"schema": "edit-file.paired-semantics/v1", "dataset_sha256": _digest(cases),
            "seed": 0, "model_calls": 0, "native_codex_calls": 0, "case_count": len(cases),
            "source": source_before, "native_codex": native,
            "execution_boundary": "inspect.unwrap(lib.project_mod.write_tools.tool_edit_file); matching/preflight/temp commits retained",
            "limitations": ["Deterministic fixtures are not model reliability or accuracy estimates.",
                            "Converted patch input uses Tofu semantics, not native Codex semantics.",
                            "Native standalone/verification expectations are static, not executed results.",
                            "Workspace lease decorator, authorization, undo journal and UI are outside this measurement.",
                            "Atomic means preflight admission, not rollback on later write errors.",
                            "LF synthetic files only; no encoding, filesystem race, crash, or write-fault comparison.",
                            "Diagnostic model labels below identify tool arms; no model is invoked."],
            "summary": {arm: dict(sorted(Counter(row["measured"][arm]["status"] for row in measured["cases"]).items())) for arm in ARMS},
            "diagnostics": summarize_trials(trials, expected={(case["id"], arm): 1 for case in cases for arm in ARMS}),
            **measured}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-root", type=Path)
    parser.add_argument("--codex-source", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--case-ids", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        result = _run_worker(args.worker, json.loads(args.case_ids))
    else:
        if args.scratch_root is None:
            parser.error("--scratch-root is required; use a caller-owned ignored evaluation directory")
        result = run_comparison(args.scratch_root, codex_source=args.codex_source)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

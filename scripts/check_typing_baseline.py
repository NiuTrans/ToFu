#!/usr/bin/env python3
"""Typing-debt ratchet: pyright diagnostics may only shrink, per scope.

Runs pyright over the committed ``pyrightconfig.json`` include set and
compares scoped diagnostic counts against ``tests/typing_baseline.json``.
New diagnostics fail; a count that dropped below the baseline also fails and
must be locked in with ``--write-baseline`` (the same one-way-ratchet
contract as ``tests/audit_baseline.json`` — debt can be paid down, it cannot
silently re-accumulate, and the baseline may not stay loose).

Scopes are the top-level include areas so a regression is attributed to the
layer that introduced it. Only files inside the include scopes are counted;
pyright also analyzes their import closure, which is reported for
information but not gated.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
BASELINE = os.path.join(ROOT, "tests", "typing_baseline.json")
CONFIG = os.path.join(ROOT, "pyrightconfig.json")

# scope name -> path prefix (repository-relative). First match wins.
SCOPES = (
    ("routes", ("routes/",)),
    ("lib/storage_authority_sql", ("lib/storage_authority_sql/",)),
    ("lib/agent_core", ("lib/agent_core/",)),
    (
        "lib-strict-files",
        (
            "lib/api_v4.py",
            "lib/identity.py",
            "lib/process_roles.py",
            "lib/storage/connection_probe.py",
        ),
    ),
)


def run_pyright() -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson"],
        cwd=ROOT, capture_output=True, text=True, timeout=900,
    )
    payload = (result.stdout or "").strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        print(
            f"pyright did not emit JSON (rc={result.returncode}):\n"
            f"{(result.stderr or payload)[-2000:]}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def scoped_counts(document: dict) -> dict[str, int]:
    counts = {name: 0 for name, _ in SCOPES}
    for diagnostic in document.get("generalDiagnostics", []):
        if diagnostic.get("severity") not in ("error", "warning"):
            continue
        path = os.path.relpath(diagnostic["file"], ROOT)
        for name, prefixes in SCOPES:
            if any(
                path == prefix or path.startswith(prefix)
                for prefix in prefixes
            ):
                counts[name] += 1
                break
    return counts


def compare(counts: dict[str, int]) -> list[str]:
    with open(BASELINE, encoding="utf-8") as f:
        baseline = json.load(f)["counts"]
    problems = []
    for scope, base in sorted(baseline.items()):
        now = counts.get(scope, 0)
        if now > base:
            problems.append(
                f"[{scope}] typing diagnostics grew {base} -> {now} "
                f"(+{now - base}); fix the new diagnostics — do NOT raise "
                "the baseline to make the gate green"
            )
        elif now < base:
            problems.append(
                f"[{scope}] typing diagnostics SHRANK {base} -> {now}; lock "
                "in the improvement: python3 "
                "scripts/check_typing_baseline.py --write-baseline"
            )
    return problems


def write_baseline(counts: dict[str, int]) -> None:
    document = {
        "policy": (
            "One-way typing ratchet for the pyright include scopes. Counts "
            "may only move down; regenerate deliberately with --write-baseline "
            "and name the fixed diagnostics in the commit."
        ),
        "counts": counts,
    }
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2)
        f.write("\n")
    print(f"wrote {os.path.relpath(BASELINE, ROOT)}: {counts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()

    if not os.path.isfile(CONFIG):
        print("pyrightconfig.json is missing", file=sys.stderr)
        return 2
    document = run_pyright()
    counts = scoped_counts(document)
    if args.write_baseline:
        write_baseline(counts)
        return 0
    problems = compare(counts)
    if problems:
        print("typing-baseline: FAILED")
        print("\n".join(f"- {problem}" for problem in problems))
        return 1
    print(f"typing-baseline: OK ({counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

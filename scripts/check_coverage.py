#!/usr/bin/env python3
"""Coverage floor ratchet: total line coverage may only rise.

Combines the coverage data produced by the test lanes, then compares the
total percentage against ``tests/coverage_baseline.json``. Dropping below the
floor fails; rising more than ``LOOSE_MARGIN`` above the floor also fails —
the floor must track reality upward, exactly like the suite-health ratchet,
otherwise regressions up to the slack would go unnoticed.

Usage:
    python3 scripts/check_coverage.py [coverage-data-file ...]
    python3 scripts/check_coverage.py --write-baseline [coverage-data-file ...]

With no data-file arguments the combined ``.coverage`` data in the repository
root is used. Data-file arguments are forwarded to ``coverage combine``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
BASELINE = os.path.join(ROOT, "tests", "coverage_baseline.json")
LOOSE_MARGIN = 1.0


def _coverage(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "coverage", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )


def measure(data_files: list[str]) -> float:
    if data_files:
        combined = _coverage("combine", *data_files)
        if combined.returncode != 0:
            print(f"coverage combine failed:\n{combined.stderr}", file=sys.stderr)
            raise SystemExit(2)
    report = _coverage("json", "-o", "-")
    if report.returncode != 0:
        print(f"coverage json failed:\n{report.stderr}", file=sys.stderr)
        raise SystemExit(2)
    document = json.loads(report.stdout)
    return float(document["totals"]["percent_covered"])


def load_floor() -> float:
    with open(BASELINE, encoding="utf-8") as f:
        return float(json.load(f)["floor_percent"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("data_files", nargs="*")
    args = parser.parse_args()

    measured = measure(args.data_files)
    if args.write_baseline:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "policy": (
                        "One-way coverage ratchet over the unit+api lanes. "
                        "The floor may only move up; regenerate deliberately "
                        "with --write-baseline and say which suites earned it."
                    ),
                    "floor_percent": round(measured, 2),
                },
                f,
                indent=2,
            )
            f.write("\n")
        print(f"wrote {os.path.relpath(BASELINE, ROOT)}: floor {measured:.2f}%")
        return 0

    floor = load_floor()
    if measured < floor:
        print(
            f"coverage-floor: FAILED — unit+api coverage fell to "
            f"{measured:.2f}% (floor {floor:.2f}%). New code needs tests; "
            "do NOT lower the floor to make the gate green."
        )
        return 1
    if measured > floor + LOOSE_MARGIN:
        print(
            f"coverage-floor: FAILED — coverage rose to {measured:.2f}% but "
            f"the floor still says {floor:.2f}%. Lock in the improvement: "
            "python3 scripts/check_coverage.py --write-baseline"
        )
        return 1
    print(f"coverage-floor: OK ({measured:.2f}% >= floor {floor:.2f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

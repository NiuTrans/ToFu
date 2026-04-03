#!/usr/bin/env python3
"""Master test runner for ChatUI.

Usage:
    python tests/run_all.py                  # Run all tests
    python tests/run_all.py --unit           # Only unit tests
    python tests/run_all.py --api            # Only API integration tests
    python tests/run_all.py --visual         # Only visual E2E tests
    python tests/run_all.py --no-visual      # Skip visual tests (faster)
    python tests/run_all.py --vlm            # Enable VLM screenshot analysis
    python tests/run_all.py -v               # Verbose output
    python tests/run_all.py -k "test_send"   # Filter by test name pattern
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)


def _banner(title: str):
    width = 60
    print()
    print("═" * width)
    print(f"  {title}")
    print("═" * width)


def _run_pytest(markers: list[str], extra_args: list[str] = None) -> int:
    """Run pytest with given markers and return exit code."""
    cmd = [sys.executable, "-m", "pytest"]

    if markers:
        marker_expr = " or ".join(markers)
        cmd.extend(["-m", marker_expr])

    cmd.extend(extra_args or [])
    cmd.extend(["--tb=short", "-v"])

    print(f"\n  $ {' '.join(cmd)}\n")
    return subprocess.call(cmd)


def main():
    parser = argparse.ArgumentParser(description="ChatUI Test Runner")
    parser.add_argument("--unit", action="store_true", help="Run only unit tests")
    parser.add_argument("--api", action="store_true", help="Run only API tests")
    parser.add_argument("--visual", action="store_true", help="Run only visual E2E tests")
    parser.add_argument("--no-visual", action="store_true", help="Skip visual tests")
    parser.add_argument("--vlm", action="store_true", help="Enable VLM screenshot analysis")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("-k", "--filter", default="", help="pytest -k filter expression")
    parser.add_argument("-x", "--exitfirst", action="store_true", help="Stop on first failure")
    args = parser.parse_args()

    _banner("ChatUI Test Suite")

    start = time.time()
    exit_code = 0
    extra = []
    if args.verbose:
        extra.append("-vv")
    if args.filter:
        extra.extend(["-k", args.filter])
    if args.exitfirst:
        extra.append("-x")
    if args.vlm:
        os.environ["VLM_ENABLED"] = "1"

    # Determine which test categories to run
    if args.unit:
        _banner("Unit Tests")
        exit_code = _run_pytest(["unit"], extra)
    elif args.api:
        _banner("API Integration Tests")
        exit_code = _run_pytest(["api"], extra)
    elif args.visual:
        _banner("Visual E2E Tests")
        exit_code = _run_pytest(["visual"], extra)
    else:
        # Run all (optionally skip visual)
        markers = ["unit"]
        _banner("Phase 1: Unit Tests")
        rc = _run_pytest(["unit"], extra)
        if rc != 0:
            exit_code = rc
            if args.exitfirst:
                _banner(f"STOPPED — Unit tests failed (exit code {rc})")
                return rc

        _banner("Phase 2: API Integration Tests")
        rc = _run_pytest(["api"], extra)
        if rc != 0:
            exit_code = rc
            if args.exitfirst:
                _banner(f"STOPPED — API tests failed (exit code {rc})")
                return rc

        if not args.no_visual:
            _banner("Phase 3: Visual E2E Tests")
            rc = _run_pytest(["visual"], extra)
            if rc != 0:
                exit_code = rc
        else:
            print("\n  ⏭️  Skipping visual tests (--no-visual)")

    elapsed = time.time() - start
    _banner(f"Results: {'PASS ✅' if exit_code == 0 else 'FAIL ❌'}  ({elapsed:.1f}s)")

    # Show screenshot summary if visual tests were run
    ss_dir = os.path.join(PROJECT_ROOT, "tests", "screenshots")
    if os.path.isdir(ss_dir):
        screenshots = [f for f in os.listdir(ss_dir) if f.endswith(".png")]
        if screenshots:
            print(f"\n  📸 {len(screenshots)} screenshots in {ss_dir}/")
            for s in sorted(screenshots):
                size = os.path.getsize(os.path.join(ss_dir, s))
                print(f"     {s} ({size/1024:.1f}KB)")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

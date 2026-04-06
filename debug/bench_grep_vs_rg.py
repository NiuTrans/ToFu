#!/usr/bin/env python3
"""A/B benchmark: grep vs ripgrep for the grep_search tool.

Tests real patterns the tool handles:
  1. Simple literal (common case) — e.g. 'logger'
  2. Short literal (function name) — e.g. 'def tool_grep'
  3. Include filter (*.py) — e.g. 'import json' in *.py
  4. Regex pattern — e.g. 'def _.*cache'
  5. Rare pattern (few matches) — e.g. 'WHOLE_FILE_THRESHOLD'
  6. Context lines (-C 3) — e.g. 'log_context'
  7. Wide pattern (many matches) — e.g. 'self'
  8. Binary-safe (-I) — full project scan
  9. Non-existent pattern — e.g. 'xyzzy_not_found_42'
 10. Large pattern with include — e.g. 'def ' in *.js

Each test is run N_ITERATIONS times for both grep and rg.
Cache is warmed before timing (drop_caches not available).
"""

import subprocess
import time
import os
import json
import statistics
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IGNORE_DIRS = [
    'node_modules', '.git', '__pycache__', '.venv', 'venv',
    '.tox', '.mypy_cache', '.pytest_cache', 'dist', 'build',
    '.next', '.nuxt', 'coverage', '.eggs', '*.egg-info',
    'data', 'logs', '.chatui', 'uploads',
]

N_ITERATIONS = 20  # per test per tool
TIMEOUT = 30
MAX_MATCHES = 15  # mirrors tool_grep's -m 15

# ─── Test cases ───────────────────────────────────────

TEST_CASES = [
    {
        "name": "1. Simple literal (common)",
        "pattern": "logger",
        "include": None,
        "context": 0,
    },
    {
        "name": "2. Function name lookup",
        "pattern": "def tool_grep",
        "include": None,
        "context": 0,
    },
    {
        "name": "3. Include filter (*.py)",
        "pattern": "import json",
        "include": "*.py",
        "context": 0,
    },
    {
        "name": "4. Regex pattern",
        "pattern": "def _.*cache",
        "include": "*.py",
        "context": 0,
    },
    {
        "name": "5. Rare pattern (few matches)",
        "pattern": "WHOLE_FILE_THRESHOLD",
        "include": None,
        "context": 0,
    },
    {
        "name": "6. Context lines (-C 3)",
        "pattern": "log_context",
        "include": "*.py",
        "context": 3,
    },
    {
        "name": "7. Wide pattern (many matches)",
        "pattern": "self",
        "include": "*.py",
        "context": 0,
    },
    {
        "name": "8. Full project scan (binary-safe)",
        "pattern": "Flask",
        "include": None,
        "context": 0,
    },
    {
        "name": "9. Non-existent pattern",
        "pattern": "xyzzy_not_found_42_benchmark",
        "include": None,
        "context": 0,
    },
    {
        "name": "10. JS file search",
        "pattern": "function",
        "include": "*.js",
        "context": 0,
    },
]


def build_grep_cmd(pattern, include=None, context=0):
    """Build GNU grep command matching tool_grep() exactly."""
    cmd = ['grep', '-rni', '--color=never', '-I']
    for d in IGNORE_DIRS[:20]:
        cmd.extend(['--exclude-dir', d])
    if include:
        cmd.extend(['--include', include])
    if context > 0:
        cmd.extend(['-C', str(context)])
    cmd.extend(['-m', str(MAX_MATCHES), '--', pattern, PROJECT_ROOT])
    return cmd


def build_rg_cmd(pattern, include=None, context=0):
    """Build ripgrep command with equivalent behavior."""
    cmd = ['rg', '-ni', '--color=never', '--no-heading']
    for d in IGNORE_DIRS[:20]:
        cmd.extend(['-g', f'!{d}/'])
    if include:
        cmd.extend(['-g', include])
    if context > 0:
        cmd.extend(['-C', str(context)])
    cmd.extend(['-m', str(MAX_MATCHES), '--', pattern, PROJECT_ROOT])
    return cmd


def run_once(cmd):
    """Run a command once, return (elapsed_ms, match_count, returncode)."""
    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=TIMEOUT, cwd=PROJECT_ROOT, errors='replace'
        )
        elapsed = (time.perf_counter() - t0) * 1000
        lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
        return elapsed, len(lines), result.returncode
    except subprocess.TimeoutExpired:
        elapsed = (time.perf_counter() - t0) * 1000
        return elapsed, -1, -1


def warm_cache():
    """Read project files into OS page cache."""
    subprocess.run(
        ['find', PROJECT_ROOT, '-type', 'f', '-name', '*.py', '-exec', 'cat', '{}', '+'],
        capture_output=True, timeout=30
    )
    subprocess.run(
        ['find', PROJECT_ROOT, '-type', 'f', '-name', '*.js', '-exec', 'cat', '{}', '+'],
        capture_output=True, timeout=30
    )


def run_benchmark():
    print("=" * 70)
    print("GREP vs RIPGREP — A/B Benchmark for ChatUI grep_search tool")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Iterations per test: {N_ITERATIONS}")
    print("=" * 70)

    # Warm up OS cache
    print("\nWarming OS page cache...")
    warm_cache()
    time.sleep(0.5)

    # Also warm each tool's binary
    subprocess.run(['grep', '--version'], capture_output=True)
    subprocess.run(['rg', '--version'], capture_output=True)

    all_results = []

    for tc in TEST_CASES:
        name = tc["name"]
        grep_cmd = build_grep_cmd(tc["pattern"], tc["include"], tc["context"])
        rg_cmd = build_rg_cmd(tc["pattern"], tc["include"], tc["context"])

        print(f"\n{'─' * 60}")
        print(f"Test: {name}")
        print(f"  Pattern: '{tc['pattern']}'  Include: {tc['include']}  Context: {tc['context']}")

        # Interleave runs to avoid ordering bias
        grep_times = []
        rg_times = []
        grep_matches = 0
        rg_matches = 0

        for i in range(N_ITERATIONS):
            # Alternate which goes first
            if i % 2 == 0:
                gt, gm, _ = run_once(grep_cmd)
                rt, rm, _ = run_once(rg_cmd)
            else:
                rt, rm, _ = run_once(rg_cmd)
                gt, gm, _ = run_once(grep_cmd)
            grep_times.append(gt)
            rg_times.append(rt)
            grep_matches = gm
            rg_matches = rm

        grep_median = statistics.median(grep_times)
        rg_median = statistics.median(rg_times)
        grep_mean = statistics.mean(grep_times)
        rg_mean = statistics.mean(rg_times)
        grep_p95 = sorted(grep_times)[int(N_ITERATIONS * 0.95)]
        rg_p95 = sorted(rg_times)[int(N_ITERATIONS * 0.95)]
        grep_stdev = statistics.stdev(grep_times) if len(grep_times) > 1 else 0
        rg_stdev = statistics.stdev(rg_times) if len(rg_times) > 1 else 0

        speedup = grep_median / rg_median if rg_median > 0 else float('inf')
        winner = "ripgrep" if rg_median < grep_median else "grep"

        result = {
            "test": name,
            "pattern": tc["pattern"],
            "grep_median_ms": round(grep_median, 2),
            "rg_median_ms": round(rg_median, 2),
            "grep_mean_ms": round(grep_mean, 2),
            "rg_mean_ms": round(rg_mean, 2),
            "grep_p95_ms": round(grep_p95, 2),
            "rg_p95_ms": round(rg_p95, 2),
            "grep_stdev_ms": round(grep_stdev, 2),
            "rg_stdev_ms": round(rg_stdev, 2),
            "grep_matches": grep_matches,
            "rg_matches": rg_matches,
            "speedup_x": round(speedup, 2),
            "winner": winner,
        }
        all_results.append(result)

        print(f"  {'':>15} {'Median':>10} {'Mean':>10} {'P95':>10} {'StDev':>10} {'Matches':>8}")
        print(f"  {'grep':>15} {grep_median:>9.2f}ms {grep_mean:>9.2f}ms {grep_p95:>9.2f}ms {grep_stdev:>9.2f}ms {grep_matches:>8}")
        print(f"  {'ripgrep':>15} {rg_median:>9.2f}ms {rg_mean:>9.2f}ms {rg_p95:>9.2f}ms {rg_stdev:>9.2f}ms {rg_matches:>8}")
        print(f"  → Winner: {winner} ({speedup:.2f}x {'faster' if winner == 'ripgrep' else 'slower for rg'})")

    # ─── Summary ──────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    print(f"{'Test':<35} {'grep (ms)':>10} {'rg (ms)':>10} {'Speedup':>8} {'Winner':>8}")
    print(f"{'─' * 35} {'─' * 10} {'─' * 10} {'─' * 8} {'─' * 8}")

    grep_wins = 0
    rg_wins = 0
    total_grep = 0
    total_rg = 0

    for r in all_results:
        print(f"{r['test']:<35} {r['grep_median_ms']:>9.2f} {r['rg_median_ms']:>9.2f} {r['speedup_x']:>7.2f}x {r['winner']:>8}")
        total_grep += r['grep_median_ms']
        total_rg += r['rg_median_ms']
        if r['winner'] == 'grep':
            grep_wins += 1
        else:
            rg_wins += 1

    print(f"{'─' * 35} {'─' * 10} {'─' * 10} {'─' * 8} {'─' * 8}")
    overall_speedup = total_grep / total_rg if total_rg > 0 else 0
    print(f"{'TOTAL (sum of medians)':<35} {total_grep:>9.2f} {total_rg:>9.2f} {overall_speedup:>7.2f}x")
    print(f"\nScore: ripgrep wins {rg_wins}/{len(all_results)}, grep wins {grep_wins}/{len(all_results)}")
    print(f"Overall speedup: ripgrep is {overall_speedup:.2f}x {'faster' if overall_speedup > 1 else 'slower'}")

    # ─── Match correctness check ──────────────────────
    print(f"\n{'=' * 70}")
    print("CORRECTNESS CHECK (match counts)")
    print(f"{'=' * 70}")
    all_correct = True
    for r in all_results:
        gm = r['grep_matches']
        rm = r['rg_matches']
        status = "✓" if gm == rm else "⚠ MISMATCH"
        if gm != rm:
            all_correct = False
        print(f"  {r['test']:<35} grep={gm:<5} rg={rm:<5} {status}")

    if all_correct:
        print("\n  ✅ All match counts are identical — drop-in compatible.")
    else:
        print("\n  ⚠️  Some match count differences detected — investigate before switching.")

    # Save raw data
    out_path = os.path.join(PROJECT_ROOT, 'debug', 'bench_results.json')
    with open(out_path, 'w') as f:
        json.dump(all_results, f, indent=2)
    print(f"\nRaw results saved to: {out_path}")


if __name__ == '__main__':
    run_benchmark()

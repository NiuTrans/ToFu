#!/usr/bin/env python3
"""A/B benchmark: fd vs GNU find vs Python os.walk for file finding.

Tests realistic patterns from our tool_find_files usage on the NFS-backed codebase.
"""

import fnmatch
import os
import shutil
import subprocess
import statistics
import time

# ── Config ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IGNORE_DIRS = {
    '__pycache__', 'node_modules', '.git', '.venv', 'venv', 'env',
    '.tox', '.mypy_cache', '.pytest_cache', '.eggs', '*.egg-info',
    'dist', 'build', '.idea', '.vscode', '.project_sessions',
    '.project_indexes', 'data', 'logs',
}
ROUNDS = 10
HAS_FD = shutil.which('fd') is not None or shutil.which(os.path.expanduser('~/.local/bin/fd')) is not None
FD_BIN = shutil.which('fd') or os.path.expanduser('~/.local/bin/fd')

# ── Test cases: (description, glob_pattern, search_path) ──
TEST_CASES = [
    ("Find all Python files",           "*.py",        "."),
    ("Find all JS files",               "*.js",        "."),
    ("Find test files",                 "test_*.py",   "."),
    ("Find Dockerfile",                 "Dockerfile",  "."),
    ("Find all markdown files",         "*.md",        "."),
    ("Find config files",               "*.json",      "."),
    ("Find CSS files",                  "*.css",       "."),
    ("Find HTML files",                 "*.html",      "."),
    ("Find all log-related Python",     "*log*.py",    "."),
    ("Find requirements files",         "requirements*","." ),
    ("Find files in lib/ only",         "*.py",        "lib"),
    ("Find files in static/ only",      "*.js",        "static"),
]


def python_walk_find(base, pattern, search_path='.'):
    """Current implementation — Python os.walk + fnmatch."""
    target = os.path.join(base, search_path)
    matches = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in sorted(dirs)
                   if d not in IGNORE_DIRS and not d.startswith('.')]
        for fname in sorted(files):
            if fnmatch.fnmatch(fname.lower(), pattern.lower()):
                rel = os.path.relpath(os.path.join(root, fname), base)
                matches.append(rel)
    return matches


def gnu_find(base, pattern, search_path='.'):
    """GNU find with -name glob."""
    target = os.path.join(base, search_path)
    # Build prune expressions for ignored dirs
    prune_parts = []
    for d in sorted(IGNORE_DIRS):
        prune_parts.extend(['-name', d, '-o'])
    # Remove trailing -o
    if prune_parts:
        prune_parts.pop()

    cmd = ['find', target, '('] + prune_parts + [')', '-prune', '-o',
           '-name', pattern, '-print']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
        # Convert to relative paths
        return [os.path.relpath(l, base) for l in lines]
    except Exception as e:
        return [f'ERROR: {e}']


def fd_find(base, pattern, search_path='.'):
    """fd-find with glob pattern."""
    target = os.path.join(base, search_path)
    # fd uses regex by default, use -g for glob mode
    cmd = [FD_BIN, '-g', pattern, target,
           '--type', 'f',
           '--hidden',  # search hidden files (like find does)
           ]
    # Add exclude patterns
    for d in sorted(IGNORE_DIRS):
        cmd.extend(['--exclude', d])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
        return [os.path.relpath(l, base) for l in lines]
    except Exception as e:
        return [f'ERROR: {e}']


def benchmark_one(func, base, pattern, search_path, rounds=ROUNDS):
    """Run a function multiple times and return timing stats."""
    times = []
    result = None
    for _ in range(rounds):
        t0 = time.perf_counter()
        result = func(base, pattern, search_path)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)  # ms
    return {
        'times': times,
        'median': statistics.median(times),
        'mean': statistics.mean(times),
        'stdev': statistics.stdev(times) if len(times) > 1 else 0,
        'min': min(times),
        'max': max(times),
        'count': len(result) if result else 0,
    }


def main():
    print("=" * 80)
    print("A/B BENCHMARK: fd vs GNU find vs Python os.walk")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Rounds per test: {ROUNDS}")
    print(f"fd available: {HAS_FD} ({FD_BIN})")
    print("=" * 80)

    # Count project files first
    total_files = sum(len(files) for _, _, files in os.walk(PROJECT_ROOT))
    print(f"Total files in project tree: {total_files}")
    print()

    tools = [("Python os.walk", python_walk_find), ("GNU find", gnu_find)]
    if HAS_FD:
        tools.append(("fd-find", fd_find))

    all_results = []

    for i, (desc, pattern, search_path) in enumerate(TEST_CASES, 1):
        print(f"─── Test {i}: {desc} ───")
        print(f"    Pattern: {pattern}  |  Path: {search_path}")
        
        test_results = {}
        for tool_name, func in tools:
            stats = benchmark_one(func, PROJECT_ROOT, pattern, search_path)
            test_results[tool_name] = stats
            print(f"  {tool_name:16s}: {stats['median']:8.2f}ms median "
                  f"(±{stats['stdev']:5.2f}ms)  "
                  f"[{stats['min']:7.2f} – {stats['max']:7.2f}]  "
                  f"matches={stats['count']}")

        # Determine winner
        medians = {k: v['median'] for k, v in test_results.items()}
        winner = min(medians, key=medians.get)
        worst = max(medians, key=medians.get)
        speedup = medians[worst] / medians[winner] if medians[winner] > 0 else float('inf')
        print(f"  🏆 Winner: {winner} ({speedup:.1f}x faster than {worst})")
        
        # Check result consistency
        counts = {k: v['count'] for k, v in test_results.items()}
        if len(set(counts.values())) > 1:
            print(f"  ⚠️  Match count mismatch: {counts}")
        
        all_results.append({
            'test': desc,
            'pattern': pattern,
            'results': test_results,
            'winner': winner,
        })
        print()

    # ── Summary ──
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    
    win_count = {}
    for tool_name, _ in tools:
        win_count[tool_name] = 0
    
    for r in all_results:
        win_count[r['winner']] += 1
    
    print(f"\nWins by tool (out of {len(TEST_CASES)} tests):")
    for tool_name in sorted(win_count, key=win_count.get, reverse=True):
        print(f"  {tool_name:16s}: {win_count[tool_name]} wins")
    
    # Overall median comparison
    print("\nOverall median latency (ms):")
    for tool_name, _ in tools:
        all_medians = [r['results'][tool_name]['median'] for r in all_results]
        overall = statistics.mean(all_medians)
        print(f"  {tool_name:16s}: {overall:.2f}ms average median")
    
    # fd vs Python speedup
    if HAS_FD:
        python_total = sum(r['results']['Python os.walk']['median'] for r in all_results)
        fd_total = sum(r['results']['fd-find']['median'] for r in all_results)
        find_total = sum(r['results']['GNU find']['median'] for r in all_results)
        print(f"\nOverall speedup:")
        print(f"  fd vs Python os.walk: {python_total/fd_total:.2f}x")
        print(f"  fd vs GNU find:       {find_total/fd_total:.2f}x")
        print(f"  Python vs GNU find:   {find_total/python_total:.2f}x")

    # ── Match count verification ──
    print("\n─── Match Count Verification ───")
    mismatches = 0
    for r in all_results:
        counts = {k: v['count'] for k, v in r['results'].items()}
        if len(set(counts.values())) > 1:
            print(f"  ⚠️  {r['test']}: {counts}")
            mismatches += 1
    if mismatches == 0:
        print("  ✅ All tools found identical results across all tests")
    else:
        print(f"  {mismatches} test(s) had mismatched counts (investigate!)")


if __name__ == '__main__':
    main()

"""Dependency-free weighted CSR PageRank: the always-available ranking kernel.

Entry: solve(offsets, targets, weights, personal), the call contract of the
optional native `tofu-repo-kernel` (packages/tofu-repo-kernel/rust/kernel.rs).
Like that kernel it follows NetworkX's SciPy implementation: row-normalized
weights, uniform start, personalized dangling redistribution, damping 0.85 and
the node-scaled L1 stopping rule, so backend selection changes scores only by
floating-point rounding. Callers build the graph through
`_native_rank.score_with_kernel`. Standard library only; no import-time work,
retained state or cache.
"""
from __future__ import annotations

DAMPING = 0.85


def solve(offsets, targets, weights, personal, *, tolerance=1e-6,
          maximum_iterations=100):
    """Return per-node scores; raise RuntimeError when iteration does not converge."""
    nodes = len(personal)
    personal_total = sum(personal)
    teleport = [value / personal_total for value in personal]
    rows, dangling = [], []
    for row in range(nodes):
        start, end = offsets[row], offsets[row + 1]
        total = sum(weights[start:end])
        if total == 0:
            dangling.append(row)
            rows.append(())
            continue
        reciprocal = 1.0 / total
        rows.append(tuple((targets[edge], weights[edge] * reciprocal)
                          for edge in range(start, end)))
    previous = [1.0 / nodes] * nodes
    for _ in range(maximum_iterations):
        following = [0.0] * nodes
        for row, edges in enumerate(rows):
            mass = previous[row]
            for target, share in edges:
                following[target] += mass * share
        dangling_mass = sum(previous[row] for row in dangling)
        following = [DAMPING * (value + dangling_mass * share) + (1.0 - DAMPING) * share
                     for value, share in zip(following, teleport)]
        if sum(abs(new - old) for new, old in zip(following, previous)) < nodes * tolerance:
            return following
        previous = following
    raise RuntimeError('PageRank failed to converge within iteration limit')

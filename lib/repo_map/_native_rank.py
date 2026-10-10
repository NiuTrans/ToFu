"""Lazy optional native graph ranking; canonical graph and failure policy.

Entry: available_kernel, score_with_kernel. One process-lifetime immutable C ABI
binding; each graph/buffer is call-local. No storage, cache, pool or subprocess.
Dependency: standalone tofu_repo_kernel when installed, canonical path penalty.
"""
from functools import lru_cache
from .outline import _is_test_path


@lru_cache(maxsize=1)
def available_kernel():
    """Freeze optional availability until restart; never compile during a call."""
    try:
        from tofu_repo_kernel import ABI_VERSION, load_kernel
        if ABI_VERSION != 1:
            return None
        return load_kernel()
    except (ImportError, OSError, ValueError, KeyError, AttributeError, TypeError):
        return None


def score_with_kernel(repo, chat_rel_paths, kernel):
    indices, adjacency = {}, []
    def node(name):
        if name not in indices:
            indices[name] = len(indices)
            adjacency.append([])
        return indices[name]
    for ident in set(repo.definitions):
        definers = repo.definitions.get(ident, set())
        referencers = repo.references.get(ident, set()) - definers
        if not definers or not referencers:
            continue
        for path in referencers:
            source, target = node('file:' + path), node('ident:' + ident)
            adjacency[source].append((target, len(definers)))
        for path in definers:
            source, target = node('ident:' + ident), node('file:' + path)
            adjacency[source].append((target, len(referencers)))
    if not indices:
        return {}
    offsets, targets, weights = [0], [], []
    for row in adjacency:
        for target, weight in sorted(row):
            targets.append(target); weights.append(weight)
        offsets.append(len(targets))
    personal = {indices['file:' + path]: 50.0 for path in chat_rel_paths or ()
                if 'file:' + path in indices}
    vector = [personal.get(i, 0.0) for i in range(len(indices))] if personal else [1.0] * len(indices)
    try:
        values = kernel.solve(offsets, targets, weights, vector)
    except Exception as exc:
        from .rank import logger, _fallback_scores
        logger.debug('[repo_map] native pagerank failed, using fallback scores: %s', exc)
        return _fallback_scores(repo, set(repo.definitions), repo.references, repo.definitions)
    return {name[5:]: values[index] * (.2 if _is_test_path(name[5:]) else 1.)
            for name, index in indices.items() if name.startswith('file:')}

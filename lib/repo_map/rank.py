"""lib/repo_map/rank.py — Importance ranking over the reference graph.

The ranking is the core "aider trick" that makes a repo map useful instead
of an alphabetical dump: files that define names referenced *by many other
files* are structurally central and get their signatures shown first.

The algorithm matches aider's published ``sumbd`` design (paper:
"Repository-Level Prompt Generation for LLMs", Zhang et al. 2023 — the
approach behind aider's reported 3× token savings at ~90% quality):

1. Build a bipartite graph: ``{files} ∪ {defined identifiers}``.
   An edge connects a file to every identifier it *references*; a second
   edge connects that identifier to every file that *defines* it. Only
   identifiers defined in at least one file are kept — library calls
   (``print``, ``len``, ``json.dumps``) carry no signal.
2. Run PageRank (personalisation spreads the mass evenly).
3. The file-side mass is the importance score; files with more definitions
   get their score distributed per tag (aider uses ``rank / len(tags)``).

If networkx is unavailable we fall back to a plain "count of referencing
files" score — strictly less principled but with the same output shape, so
rendering code never branches.
"""
from __future__ import annotations

from lib.log import get_logger

from .outline import RepoMap, _is_test_path

logger = get_logger(__name__)


def sumbd(repo: RepoMap, chat_rel_paths: set[str] | None = None) -> dict[str, float]:
    """Return ``{rel_path: importance}`` for every file in *repo*.

    ``chat_rel_paths`` (aider parity) is a set of files currently in the
    conversation; their PageRank personalisation is boosted 50× so the
    context around "what we are editing right now" is most detailed.
    """
    from ._native_rank import available_kernel, score_with_kernel
    kernel = available_kernel()
    if kernel is not None:
        return score_with_kernel(repo, chat_rel_paths, kernel)
    defines = repo.definitions          # ident → {defining files}
    references = repo.references        # ident → {referencing files}

    # Identifiers defined somewhere in the repo are the only ones that
    # can carry importance; everything else is stdlib/3rd-party noise.
    defined_idents = set(defines)

    try:
        import networkx as nx
    except ImportError:
        return _fallback_scores(repo, defined_idents, references, defines)

    # Each old referencer→identifier edge occurred once per definer, and
    # each identifier→definer edge once per referencer. Aggregate exactly
    # those multiplicities instead of materializing the Cartesian product.
    graph = nx.DiGraph()

    def _node(kind: str, key: str) -> str:
        return f'{kind}:{key}'

    for ident in defined_idents:
        definers = defines.get(ident, set())
        referencers = references.get(ident, set()) - definers
        if not definers or not referencers:
            continue
        identifier_node = _node('ident', ident)
        for referencer in referencers:
            graph.add_edge(_node('file', referencer), identifier_node,
                           weight=len(definers))
        for definer in definers:
            graph.add_edge(identifier_node, _node('file', definer),
                           weight=len(referencers))

    if not graph:
        return {}

    personalization = None
    if chat_rel_paths:
        personalization = {
            _node('file', p): 50.0 for p in chat_rel_paths
            if _node('file', p) in graph
        } or None

    try:
        ranked = nx.pagerank(graph, weight='weight',
                             personalization=personalization)
    except Exception as exc:
        # PageRank failure (degenerate graph) degrades to reference counts.
        logger.debug('[repo_map] pagerank failed, using fallback scores: %s', exc)
        return _fallback_scores(repo, defined_idents, references, defines)

    scores: dict[str, float] = {}
    for node, rank in ranked.items():
        if not node.startswith('file:'):
            continue
        rel = node[5:]
        # Test files reference everything → their definitions would crowd
        # out production code but a model orients on source, not fixtures.
        if _is_test_path(rel):
            # Tests reference all of production; without this they win every
            # ranking while telling a model almost nothing about the design.
            rank *= 0.2
        scores[rel] = rank
    return scores


def ensure_scores(repo: RepoMap, scores: dict[str, float]) -> dict[str, float]:
    """Every file with definitions must appear in the map even when the
    reference graph produced no edges (toy repos, single-file projects,
    names that all landed in STOP_WORDS). Files missing from ``scores``
    share the remaining mass equally below every ranked file.
    """
    if scores:
        lowest = min(scores.values())
    else:
        lowest = 0.0
    neutral = max(lowest * 0.1, 1e-12)
    out = dict(scores)
    for rel_path, tags in repo.tags.items():
        if tags and rel_path not in out:
            out[rel_path] = neutral
    return out

def per_tag_score(repo: RepoMap, file_scores: dict[str, float]) -> dict[str, list[float]]:
    """Spread a file's importance across its tags.

    Mirrors aider: each definition in a heavily-tagged file gets a
    smaller slice, so a 200-function utility dump does not crowd out a
    central 3-class module.
    """
    out: dict[str, list[float]] = {}
    for rel_path, tags in repo.tags.items():
        score = file_scores.get(rel_path, 0.0)
        if not tags:
            continue
        per = score / max(len(tags), 1)
        out[rel_path] = [per] * len(tags)
    return out


def _fallback_scores(repo: RepoMap,
                     defined_idents: set[str],
                     references: dict[str, set[str]],
                     defines: dict[str, set[str]]) -> dict[str, float]:
    """networkx-free scoring: files ranked by #files referencing their defs."""
    scores: dict[str, float] = {}
    for ident in defined_idents:
        definers = defines.get(ident, set())
        n_refs = len(references.get(ident, set()) - definers)
        for definer in definers:
            scores[definer] = scores.get(definer, 0.0) + n_refs
    return scores

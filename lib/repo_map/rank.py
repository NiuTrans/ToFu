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

``_native_rank.score_with_kernel`` owns the canonical graph and runs it on the
optional native ``tofu-repo-kernel`` when installed, otherwise on the
dependency-free ``_python_rank`` kernel with the same semantics. Only a kernel
computation failure (non-convergence, contained native panic, refused input)
degrades to the plain "count of referencing files" score below, with one
warning per process; the output shape is identical, so rendering never branches.
"""
from __future__ import annotations

from lib.log import get_logger

from .outline import RepoMap

logger = get_logger(__name__)


def sumbd(repo: RepoMap, chat_rel_paths: set[str] | None = None) -> dict[str, float]:
    """Return ``{rel_path: importance}`` for every file in *repo*.

    ``chat_rel_paths`` (aider parity) is a set of files currently in the
    conversation; their PageRank personalisation is boosted 50× so the
    context around "what we are editing right now" is most detailed.
    """
    from . import _python_rank
    from ._native_rank import available_kernel, score_with_kernel
    return score_with_kernel(repo, chat_rel_paths, available_kernel() or _python_rank)


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
    """Degraded scoring: files ranked by #files referencing their defs."""
    scores: dict[str, float] = {}
    for ident in defined_idents:
        definers = defines.get(ident, set())
        n_refs = len(references.get(ident, set()) - definers)
        for definer in definers:
            scores[definer] = scores.get(definer, 0.0) + n_refs
    return scores

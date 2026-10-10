"""Repository scan/rank/render composition for CLI and server-owned tool callers.

Entry points: generate_repo_map, RepositoryScanner. Dependencies: outline,
rank, render. Callers own scanner authorization and lifecycle; no registry/cache.
"""
from __future__ import annotations

import os
from typing import Protocol

from .outline import RepoMap, scan
from .rank import ensure_scores, sumbd
from .render import render_map


class RepositoryScanner(Protocol):
    """Borrowed extraction port; receives the normalized authorized root."""
    def __call__(self, root: str, *, path: str, focus: str,
                 include_refs: bool, include_tests: bool) -> RepoMap: ...


def generate_repo_map(root: str,
                      path: str = '',
                      focus: str = '',
                      max_tokens: int = 2048,
                      chat_rel_paths: set[str] | None = None,
                      with_rank: bool = True,
                      include_tests: bool = False, *,
                      scanner: RepositoryScanner | None = None) -> tuple[str, dict]:
    """One-shot: scan → rank → render. Returns (map_text, stats).

    ``root`` may be relative; it is resolved once here. ``with_rank=False``
    skips PageRank (unit tests and _ablations only). ``include_tests`` gates
    whether test files appear at all — tests reference everything, so by
    default they would dominate the ranking while telling a model almost
    nothing about the production design.
    ``scanner`` is an optional server-owned borrowed port, never a model/wire
    option. The caller authorizes and releases it; this function neither closes
    nor stores it. Omission uses the canonical scanner. Ranking/render stay here.
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    scan_repository = scan if scanner is None else scanner
    repo = scan_repository(root, path=path, focus=focus, include_refs=with_rank,
                           include_tests=include_tests)
    if with_rank:
        scores = sumbd(repo, chat_rel_paths=chat_rel_paths)
    else:
        # Alphabetical deterministic order for tests.
        scores = {p: float(len(repo.tags.get(p, ())))
                  for p in repo.files if repo.tags.get(p)}
    scores = ensure_scores(repo, scores)
    return render_map(repo, scores, max_tokens=max_tokens,
                      chat_rel_paths=chat_rel_paths)

"""lib/repo_map — Compact repository structure outline for language models.

Generates an aider-style *repo map*: for the most important source files in
a project, list only the definition lines (functions / classes / methods),
omit all bodies, and return every accepted definition in the selected scope. The
goal is to let a model orient "what lives where" in one cheap call instead of
a dozen ``ls``/``rg`` rounds.

Pipeline (pure, no I/O besides the caller's filesystem):

    list_files()        → git ls-files / rg --files / pruned walk
    collect_tags()      → per-file (name, line, kind, signature) via
                          tree-sitter (grep_ast) with a Python-ast fast path
    rank_files()        → PageRank over the reference graph (networkx)
                          using the sumbd filter (aider)
    render_map()        → complete aider-style indented table;
                          shared L0 owns model-context overflow

Everything degrades gently:
  - unknown extensions are skipped silently
  - files that fail to parse contribute no definitions but still participate
    in the reference graph (they can still *reference* names)
  - non-git trees fall back to a bounded os.walk with directory pruning
"""
from __future__ import annotations

from .outline import RepoMap, Tag
from .rank import sumbd

__all__ = ["RepoMap", "Tag", "sumbd"]

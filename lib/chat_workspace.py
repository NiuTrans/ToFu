"""Landing zone for archives dropped straight onto the chat composer.

A dropped ``.zip`` / ``.tar(.gz)`` is a *folder*, not a file: its value is the
tree (``\\input{chapters/…}``, figure paths), which only survives extraction to
a real directory. Unlike the project-browser archive drop — which requires an
already-attached workspace root — a chat drop commonly lands with NO project
attached, so this module owns the managed workspace root the extracted tree
lives under until the conversation mounts it as a project root.

The workspace is mutable USER STATE (an extracted user upload referenced by the
conversation's project mount), so it co-locates with the DB via
``runtime_paths.uploads_root()`` exactly like ``uploads/papers`` and
``uploads/videos``. It is intentionally NOT per-user: ``uploads/`` is already
the single-owner state tree and the project mount / ``_roots`` registry is the
single-user primary-root seam today (see the TODO(enterprise) notes in
``routes/api_v1/project.py``); the per-user keying seam is added there, not
duplicated here.

The extractor itself is the hardened, budgeted ``lib.paper.archive`` (zip-slip
/link defense, 512 files / 64 MiB total / 32 MiB per file, 128 MiB compressed,
atomic fresh-dir extraction). Nothing here re-implements archive handling.
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['chat_workspaces_root']


def chat_workspaces_root() -> str:
    """Absolute path to the managed chat-drop workspace root (created on demand).

    Extraction lands in ``<root>/<archive-stem>/`` (fresh, auto-suffixed — the
    extractor never merges or overwrites). Kept separate from the active project
    tree so a chat drop never writes into a root the user attached for editing.
    """
    try:
        from lib.runtime.runtime_paths import uploads_root
        path = os.path.join(uploads_root(), 'workspaces')
    except Exception as exc:  # pragma: no cover — defensive fallback
        logger.debug('[ChatWorkspace] uploads_root unavailable, in-tree: %s', exc)
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, 'uploads', 'workspaces')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.warning('[ChatWorkspace] could not create workspace root %s: %s',
                       path, exc)
    return path

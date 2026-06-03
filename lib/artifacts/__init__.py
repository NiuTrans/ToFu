"""lib/artifacts — Renderable chat artifacts (Markdown / HTML / SVG).

A chat artifact is a "report-shaped" output that we want to surface in the
right-side panel of the chat UI rather than scrolling past as plain prose.
Sources of artifacts:

  * write_file of .md / .markdown / .html / .htm / .svg (Producer A,
    wired in lib/tasks_pkg/handlers/project.py)
  * Inline fenced ```html / ```markdown / ```svg blocks above a size
    threshold, or a self-contained <!doctype html> / <html> region in
    assistant prose (Producer B, lib/artifacts/scanner.py — Step 4)

This module is the storage + lifecycle layer.  It does NOT know about
the LLM; producers call ``create_artifact`` with already-extracted bytes
and we persist + emit a metadata-only SSE event.

Step 1 (this commit) ships the core: schema, CRUD, dedupe, and the SSE
event.  Detection wiring is in subsequent steps.

Public API (re-exported here for the "from lib.artifacts import …" form):

    create_artifact(...)        — persist + emit SSE event
    get_artifact(id)            — fetch one row by id (raw content + meta)
    get_artifact_meta(id)       — fetch metadata only (no content)
    list_artifacts(conv_id)     — list metadata rows for a conv
    delete_artifact(id, ...)    — soft-delete (deleted_at = ts_ms)
    set_pinned(id, pinned)      — toggle pin flag
    is_renderable_path(path)    — predicate used by Producer A
    detect_format(path)         — extension → format string
"""

from __future__ import annotations

from lib.artifacts.core import (
    ArtifactNotFoundError,
    ArtifactSizeError,
    create_artifact,
    delete_artifact,
    detect_format,
    get_artifact,
    get_artifact_meta,
    is_renderable_path,
    list_artifacts,
    list_pinned_or_recent,
    list_versions,
    set_pinned,
)
from lib.artifacts.events import emit_artifact_event
from lib.artifacts.scanner import scan_message

__all__ = [
    'ArtifactNotFoundError',
    'ArtifactSizeError',
    'create_artifact',
    'delete_artifact',
    'detect_format',
    'emit_artifact_event',
    'get_artifact',
    'get_artifact_meta',
    'is_renderable_path',
    'list_artifacts',
    'list_pinned_or_recent',
    'list_versions',
    'scan_message',
    'set_pinned',
]

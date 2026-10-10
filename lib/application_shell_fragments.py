"""Assemble frontend-owned application-shell HTML fragments.

Responsibility:
  * map ``APPLICATION_SHELL_FRAGMENT:<name>`` markers in ``index.html`` to
    authored HTML under ``frontend/src/application-shell/fragments``;
  * expose one cache signature covering every fragment;
  * fail closed when marker/file parity drifts.

Entry points are :func:`inject_fragments` and :func:`fragments_signature`.
This module knows nothing about feature behavior: ``routes.common`` owns the
HTTP response, while frontend source files own all rendered markup.
"""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS_DIR = (
    PROJECT_ROOT / "frontend" / "src" / "application-shell" / "fragments"
)
_MARKER_RE = re.compile(
    r"<!--\s*APPLICATION_SHELL_FRAGMENT:([a-z][a-z0-9_]*)\s*-->"
)


def marker_for(name: str) -> str:
    """Return the canonical marker for an application-shell fragment."""
    if re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
        raise ValueError(f"invalid application-shell fragment name: {name!r}")
    return f"<!-- APPLICATION_SHELL_FRAGMENT:{name} -->"


def fragment_path(name: str) -> Path:
    """Return the authored path for ``name`` after validating the name."""
    marker_for(name)
    return FRAGMENTS_DIR / f"{name}.html"


def find_markers(html: str) -> list[str]:
    """Return application-shell marker names in document order."""
    return _MARKER_RE.findall(html or "")


def list_fragment_names() -> set[str]:
    """Return the complete set of authored application-shell fragments."""
    try:
        return {path.stem for path in FRAGMENTS_DIR.glob("*.html")}
    except OSError:
        return set()


def fragments_signature() -> str:
    """Return a stable cache signature for the fragment directory."""
    parts: list[str] = []
    try:
        paths = sorted(FRAGMENTS_DIR.glob("*.html"))
    except OSError:
        return ""
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            return "unreadable"
        parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(parts)


def inject_fragments(html: str) -> str:
    """Replace all application-shell markers, rejecting an open fragment set."""
    marker_names = find_markers(html)
    duplicate_names = sorted({
        name for name in marker_names if marker_names.count(name) > 1
    })
    if duplicate_names:
        raise ValueError(
            "duplicate application-shell fragment markers: "
            + ", ".join(duplicate_names)
        )

    marker_set = set(marker_names)
    fragment_set = list_fragment_names()
    if marker_set != fragment_set:
        missing_files = sorted(marker_set - fragment_set)
        unused_files = sorted(fragment_set - marker_set)
        raise ValueError(
            "application-shell fragment parity drift: "
            f"missing_files={missing_files}, unused_files={unused_files}"
        )

    def _replace(match: re.Match[str]) -> str:
        path = fragment_path(match.group(1))
        with path.open(encoding="utf-8") as handle:
            return handle.read().rstrip("\n")

    return _MARKER_RE.sub(_replace, html)

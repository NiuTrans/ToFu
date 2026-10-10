"""Bounded GNU-order directory walk for the run_command ``find`` fast path.

One walker serves the translated safe subset of GNU find: a relative root,
``-name``/``-iname`` basename globs (including the ``-name a -o -name b``
union), ``-type f``/``-type d``/no type, ``-maxdepth``/``-mindepth``, and an
optional trailing ``-print``. Output order and path rendering match GNU
find's pre-order, print-root-as-typed contract, so piping the result into
``head``/``sort``/``wc -l`` is observably identical to the shell pipeline.

Every walk is bounded twice: an entry-scan budget and a wall-clock deadline.
A caller asking for an exact count (``| wc -l``) or a full sort receives
``complete=False`` on budget exhaustion and MUST fall back to the native
find segment (which carries its own coreutils-timeout budget); a capped
listing (``| head -n K`` or the default 500-row page) simply stops early,
which is exactly what downstream ``head`` would have done via SIGPIPE.
"""

from __future__ import annotations

import fnmatch
import os
import time


def _env_budget(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_SCAN_BUDGET = _env_budget(
    'TOFU_FIND_FASTPATH_SCAN_ENTRIES', 250_000, 100, 2_000_000)
_SCAN_DEADLINE_S = _env_budget(
    'TOFU_FIND_FASTPATH_DEADLINE_S', 30, 1, 120)


def _render_path(root_display, parts):
    """``find <root>`` path rendering: root exactly as typed, then components."""
    if not parts:
        return root_display
    return root_display.rstrip('/') + '/' + '/'.join(parts)


def shell_find(base, root, patterns, *, case_sensitive, entry_type,
               max_depth, min_depth, limit):
    """Walk ``base/root`` collecting matches in GNU find pre-order.

    ``patterns`` is a non-empty union of basename globs; ``entry_type`` is
    'f', 'd', or 'any'; ``limit`` caps collected matches (None = collect
    all, still budget-bounded). Returns::

        {'entries': [...], 'complete': bool, 'exhausted': bool}

    ``complete=False`` means absence/count evidence is not trustworthy
    (budget hit): callers needing exact totals must fall back to native.
    """
    root_abs = os.path.join(base, root) if not os.path.isabs(root) else root
    if not os.path.isdir(root_abs):
        return None
    comparable = patterns if case_sensitive else [p.lower() for p in patterns]
    deadline = time.monotonic() + _SCAN_DEADLINE_S
    budget = _SCAN_BUDGET
    entries = []
    exhausted = False

    def name_matches(name):
        probe = name if case_sensitive else name.lower()
        return any(fnmatch.fnmatchcase(probe, pat) for pat in comparable)

    def type_matches(is_dir, is_file):
        if entry_type == 'f':
            return is_file
        if entry_type == 'd':
            return is_dir
        return True                       # symlinks/specials match 'any'

    def emit(parts, is_dir, is_file):
        if limit is not None and len(entries) >= limit:
            return False
        entries.append(_render_path(root, parts))
        return True

    # Root itself (depth 0). GNU matches the pattern against the root's
    # basename; a plain directory root only surfaces with -type d / no type.
    if min_depth == 0:
        base_name = root.rstrip('/').rsplit('/', 1)[-1] or root
        if name_matches(base_name) and type_matches(True, False):
            if not emit([], True, False):
                return {'entries': entries, 'complete': True,
                        'exhausted': False}
    if max_depth == 0:
        return {'entries': entries, 'complete': True, 'exhausted': False}

    # Iterative pre-order: (dir_abs, parts, scandir iterator). Each level is
    # fully consumed (matches emitted in readdir order) before the next
    # sibling, mirroring fts pre-order traversal.
    stack = []

    def push(dir_abs, parts, depth):
        try:
            it = os.scandir(dir_abs)
        except OSError:
            return False
        stack.append([it, parts, depth, None])
        return True

    if not push(root_abs, [], 0):
        return {'entries': entries, 'complete': False, 'exhausted': True}

    while stack:
        if budget <= 0 or time.monotonic() >= deadline:
            exhausted = True
            break
        frame = stack[-1]
        it, parts, depth, _pending = frame
        try:
            entry = next(it)
        except StopIteration:
            it.close()
            stack.pop()
            continue
        except OSError:
            it.close()
            stack.pop()
            continue
        budget -= 1
        child_parts = parts + [entry.name]
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError:
            continue
        entry_depth = depth + 1
        if entry_depth >= min_depth and name_matches(entry.name) \
                and type_matches(is_dir, is_file):
            if not emit(child_parts, is_dir, is_file):
                while stack:
                    try:
                        stack.pop()[0].close()
                    except OSError:
                        pass
                return {'entries': entries, 'complete': True,
                        'exhausted': False}
        if is_dir and entry_depth < (max_depth if max_depth is not None
                                     else 1 << 30):
            push(entry.path, child_parts, entry_depth)

    while stack:
        try:
            stack.pop()[0].close()
        except OSError:
            pass
    return {'entries': entries, 'complete': not exhausted,
            'exhausted': exhausted}


__all__ = ['shell_find']

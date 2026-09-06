"""Multi-root-aware project path resolution.

Extracted from ``tools.py`` (2026-09-03): ``_resolve_base`` resolves
``base_path + rel_path`` honouring ``rootname:path`` namespacing, cross-root
auto-routing, and a conv-scoped self-heal.  It lives outside the dispatch
façade so ``write_tools`` / ``tasks_pkg`` consumers import it directly —
the façade imports ``write_tools`` at module top, so ``write_tools`` cannot
import the façade back (hence the old lazy function-level imports).
``tools.py`` re-exports ``_resolve_base`` for backward compat.
"""

import os

from lib.log import get_logger
from lib.project_mod.config import (
    UnknownWorkspaceRootError,
    _lock as _cfg_lock,
    get_conv_roots,
    resolve_namespaced_path,
)

logger = get_logger(__name__)

# Multi-root cross-check result cache for _resolve_base.  Keyed on the
# conversation + root-set signature, so a project/root switch (which changes
# the signature) naturally invalidates.  Bounded; cleared wholesale when it
# grows past the cap (the common case is a small set of hot rel_paths).
_RESOLVE_BASE_CACHE = {}
_RESOLVE_BASE_CACHE_MAX = 4096


def _resolve_base(base_path, rel_path, conv_id=None):
    """Resolve base_path + rel_path, supporting multi-root 'name:path' syntax.

    If rel_path contains ':', treat the part before ':' as a root name.
    Otherwise fall back to the provided base_path.

    Cross-root safety: when multiple roots are configured, checks if
    the requested relative path exists under the primary root.  If it
    does NOT exist there but DOES exist under exactly one other root,
    auto-routes to that root and logs a warning.  This prevents the
    common model mistake of writing files intended for root B into root A.

    conv_id scoping (2026-05-05): when the caller knows which
    conversation's root registry should authoritatively answer this
    resolution, pass the full conv_id.  resolve_namespaced_path will
    check that conv's registry first so concurrent tasks cannot
    clobber each other's root namespaces.  Falls back to the shared
    global _roots when no conv-specific match is found.

    Self-healing fallback: if no conv-specific registry answers AND
    ``base_path`` is provided AND its basename matches the root name
    used in ``rel_path``, resolve to ``base_path`` + rel.  This covers
    the concurrent-clobber case where a task's global _roots entry was
    overwritten by another task after the system prompt was built but
    before the tool call executed.

    Returns (effective_base, effective_rel).
    """
    if rel_path and ':' in rel_path and not os.path.isabs(rel_path):
        # Check it's not a Windows drive letter like C:\...
        colon_idx = rel_path.index(':')
        # Reject prefixes that can't be a workspace root name: JSON/array
        # punctuation or whitespace before the colon means rel_path is a
        # serialized blob (e.g. a stringified reads array '[{"path": ...]'),
        # not 'rootname:path'.  Treating it as a root produced misleading
        # "Unknown workspace root '[{\"path\"'" errors.
        _looks_like_root = not any(c in rel_path[:colon_idx] for c in '[]{}"\'\t\n ')
        if colon_idx > 0 and colon_idx < 40 and _looks_like_root:  # reasonable name length
            try:
                return resolve_namespaced_path(rel_path, conv_id=conv_id)
            except ValueError as _ve:
                _name, _, _rest = rel_path.partition(':')
                # ── Self-heal: base_path's basename matches the requested
                #    root name → this is almost certainly the concurrent-
                #    clobber case (we *are* in the task whose root that is,
                #    but some other task wiped the global registry).  Resolve
                #    to the provided base_path.  Safe because the name and
                #    path agree by construction.
                if base_path:
                    bp_basename = os.path.basename(os.path.abspath(base_path))
                    if bp_basename == _name or bp_basename.lower() == _name.lower():
                        logger.info('[Tools] Self-heal namespaced path %r: '
                                    'base_path basename matches unknown root — '
                                    'resolving to base_path (conv-state race workaround). '
                                    'conv_id=%s',
                                    rel_path, conv_id[:12] if conv_id else '?')
                        return base_path, (_rest or '.')
                # DO NOT silently strip the 'name:' prefix. Stripping
                #   it converts a model typo ('CDP:foo' when meant 'cdp:foo',
                #   or a stale root that was cleared by set_project) into a
                #   DATA-LOSS bug: the write tools fall back to the primary
                #   root and silently overwrite whatever file with the same
                #   relative name exists there.  See the
                #   create_project_frontend_sync_bug memo.
                #
                #   Instead, raise a sentinel that path-taking tools surface
                #   as an explicit error to the model.  The only legitimate
                #   case for a colon in a path is a Windows drive letter
                #   ('C:\...'), which is already excluded by isabs() above.
                # Log ONCE here with full context. Task-executor layers
                # that re-raise should NOT re-log this as WARNING — they
                # check isinstance(e, UnknownWorkspaceRootError) and log
                # at INFO (recoverable, LLM-facing error).
                logger.warning('[Tools] namespaced path %r: unknown root %r — '
                               'refusing to fall through to primary '
                               '(would risk silent clobber). %s',
                               rel_path, _name, _ve)
                raise UnknownWorkspaceRootError(
                    f'Unknown workspace root "{_name}" in path "{rel_path}". '
                    f'Use a known root name (see the multi-root table shown at '
                    f'session start), a plain relative path without any colon '
                    f'prefix (resolves under the primary root), or an absolute '
                    f'path under a writable location (its containing directory '
                    f'registers as a new root on first write — requires '
                    f'allow_outside_workspace=true after the user confirms).'
                ) from _ve

    # ── Multi-root cross-check for path-misrouting ──
    # When the model forgets the 'rootname:' prefix in a multi-root
    # workspace, the path silently resolves under the primary root.
    # If the file/dir does NOT exist under primary but DOES exist under
    # exactly one other root, auto-route there.  This is a safety net,
    # not a substitute for proper 'rootname:' prefix usage.
    if base_path and rel_path and rel_path not in ('.', '', '/'):
        with _cfg_lock:
            # Source roots from the SAME conv-scoped registry the namespaced
            #   resolver uses (get_conv_roots falls back to global _roots when
            #   the conv has none).  Reading the global _roots here would let a
            #   concurrent conversation's root leak in and misroute a write —
            #   the same clobber-risk class as the prompt root-table leak.
            roots_view = get_conv_roots(conv_id)
            if len(roots_view) > 1:
                # Memoize the filesystem probe: on FUSE each os.path.exists is a
                # stat, and this loop re-probes every extra root on every call.
                # Keyed by conv + root-set signature so a project/root switch
                # invalidates; filesystem-level staleness is bounded by the same
                # root-set lifetime the resolver itself uses.
                sig = tuple(sorted((rn, rs['path']) for rn, rs in roots_view.items()))
                cache_key = (conv_id, sig, base_path, rel_path)
                cached = _RESOLVE_BASE_CACHE.get(cache_key)
                if cached is not None:
                    return cached

                result = (base_path, rel_path)  # default: primary fallback
                primary_target = os.path.join(base_path, rel_path)
                if not os.path.exists(primary_target):
                    # File doesn't exist under primary — check other roots
                    candidate_roots = []
                    for rn, rs in roots_view.items():
                        if rs['path'] == base_path:
                            continue
                        other_target = os.path.join(rs['path'], rel_path)
                        if os.path.exists(other_target):
                            candidate_roots.append((rn, rs['path']))
                    if len(candidate_roots) == 1:
                        rn, rp = candidate_roots[0]
                        # Successful single-candidate resolution is NOT an error
                        # — log at INFO so it stays out of error.log.
                        logger.info(
                            '[Tools] Cross-root auto-route: %s not found under primary %s '
                            'but exists under [%s] %s — routing there. '
                            'Model should use \'%s:%s\' prefix to be explicit.',
                            rel_path, base_path, rn, rp, rn, rel_path)
                        result = (rp, rel_path)
                    elif len(candidate_roots) > 1:
                        names = ', '.join(f'{rn}' for rn, _ in candidate_roots)
                        logger.warning(
                            '[Tools] Ambiguous multi-root path: %s not found under primary '
                            'but exists in multiple roots (%s). Using primary as fallback. '
                            'Model should use explicit root prefix.',
                            rel_path, names)

                _RESOLVE_BASE_CACHE[cache_key] = result
                if len(_RESOLVE_BASE_CACHE) > _RESOLVE_BASE_CACHE_MAX:
                    _RESOLVE_BASE_CACHE.clear()
                return result

    return base_path, rel_path

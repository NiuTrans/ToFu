"""Multi-root-aware project path resolution.

Extracted from ``tools.py`` (2026-09-03): ``_resolve_base`` resolves
``base_path + rel_path`` honouring explicit ``rootname:path`` namespacing.  It lives outside the dispatch
façade so ``write_tools`` / ``tasks_pkg`` consumers import it directly —
the façade imports ``write_tools`` at module top, so ``write_tools`` cannot
import the façade back (hence the old lazy function-level imports).
``tools.py`` re-exports ``_resolve_base`` for backward compat.
"""

import os

from lib.log import get_logger
from lib.project_mod.config import (
    UnknownWorkspaceRootError,
    resolve_namespaced_path,
)

logger = get_logger(__name__)

def _resolve_base(base_path, rel_path, conv_id=None):
    """Resolve base_path + rel_path, supporting multi-root 'name:path' syntax.

    If rel_path contains ':', treat the part before ':' as a root name.
    Otherwise fall back to the provided base_path.

    Bare relative paths always stay under the supplied primary base. File
    existence never changes repository identity; use rootname:path explicitly.

    conv_id scoping (2026-05-05): when the caller knows which
    conversation's root registry should authoritatively answer this
    resolution, pass the full conv_id.  resolve_namespaced_path will
    check that conv's registry first so concurrent tasks cannot
    clobber each other's root namespaces.  Falls back to the shared
    global _roots when no conv-specific match is found.

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
                    f'Registry detail: {_ve}. '
                    f'Relative paths resolve under the primary root {base_path!r}; '
                    f'use a registered rootname:path to select another root. '
                    f'For read_files, an absolute path can read the intended local '
                    f'file without registering a new root. Writes outside registered '
                    f'roots still require the normal write authorization.'
                ) from _ve

    return base_path, rel_path

"""Project write tools — write_file, edit_file, apply_diff(s), insert_content(s).

Was a single 1565-line ``write_tools.py``; decomposed into function-seam
submodules behind this facade so every existing import — ``from
lib.project_mod.write_tools import tool_write_file`` /
``_resolve_write_path`` / ``_apply_one_diff`` / … and ``import
lib.project_mod.write_tools as wt`` + ``wt._temp_roots._cache = …`` — keeps
working byte-for-byte:

  * ``_text``  — pure fuzzy-match / unicode-escape / duplicate-describe / vscode-touch
  * ``_paths`` — the write-path CORE (temp/root-signal/resolve/attribution)
  * ``_ops``   — the write/edit operations (write_file / upload / apply_diff(s) / insert_content(s))

Re-exported via ``lib/project_mod/tools.py`` for the historic flat-module
import surface too.
"""

from ._paths import (
    _FORBIDDEN_CREATE_ROOTS,
    _enforce_not_readonly,
    _is_forbidden_create_path,
    _is_temp_path,
    _mod_attribution,
    _nearest_existing_dir,
    _resolve_write_path,
    _root_signal,
    _should_record_modification,
    _signal_root_added,
    _temp_roots,
    drain_root_added_signals,
)
from ._text import (
    _UNICODE_ESCAPE_RE,
    _decode_unicode_escapes,
    _describe_duplicate_matches,
    _find_closest_match,
    _touch_for_vscode,
)
from ._ops import (
    _apply_one_diff,
    _dedupe_target,
    _insert_one,
    _invalid_edit_entry_msg,
    save_uploaded_file,
    tool_apply_diff,
    tool_apply_diffs,
    tool_edit_file,
    tool_insert_content,
    tool_insert_contents,
    tool_write_file,
)

__all__ = [
    'tool_write_file',
    'save_uploaded_file',
    'tool_apply_diff',
    'tool_apply_diffs',
    'tool_edit_file',
    'tool_insert_content',
    'tool_insert_contents',
    'drain_root_added_signals',
]

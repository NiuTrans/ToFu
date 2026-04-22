"""
Project Co-Pilot package.

Decomposed from monolithic lib/project.py into:
  - config.py         — Constants, state, ignore lists
  - modifications.py  — Session/undo/redo system
  - scanner.py        — File scanning and tree building
  - tools.py          — Tool implementations and dispatch
  - indexer.py        — Context generation for prompt injection
"""

__all__ = [
    # Config & State
    '_lock', '_state',
    'get_state', 'get_project_path',
    'get_recent_projects', 'save_recent_project', 'clear_recent_projects',
    'IGNORE_DIRS',
    'IGNORE_FILES', 'BINARY_EXTENSIONS',
    'MAX_FILE_SIZE', 'MAX_SCAN_FILES', 'MAX_TREE_ENTRIES', 'MAX_READ_CHARS',
    'MAX_GREP_RESULTS', 'LINE_COUNT_LIMIT',
    'SESSIONS_DIR',
    'MAX_COMMAND_TIMEOUT', 'MAX_COMMAND_OUTPUT', 'SHELL_PREFIX',
    'DANGEROUS_PATTERNS', 'CODE_EXTENSIONS', 'DATA_EXTENSIONS',
    'MAX_DATA_FILE_PREVIEW',
    # Modifications / Undo
    'get_modifications', 'get_conv_ids_with_modifications',
    'undo_conv_modifications', 'undo_task_modifications', 'undo_all_modifications',
    '_record_modification',
    # Scanner
    'set_project', 'set_project_paths', 'ensure_project_state', 'clear_project', 'rescan',
    'add_project_root', 'remove_project_root', 'list_roots',
    '_should_ignore', '_is_data_file', '_is_likely_data_content',
    '_fmt_size', '_safe_path',
    # Multi-Root Config
    '_roots', '_make_root_state', 'resolve_namespaced_path',
    'get_roots', 'get_root_path',
    # Tools
    'tool_list_dir', 'tool_read_files', 'tool_grep', 'tool_grep_batch',
    'tool_find_files', 'tool_find_files_batch',
    'tool_write_file', 'tool_apply_diff', 'tool_insert_content',
    'tool_create_project', 'tool_run_command',
    'execute_tool', 'execute_standalone_command',
    'project_tool_display', 'browse_directory',
    # Context
    'get_context_for_prompt',
]

# ── Config & State ──
# ── Multi-Root Config ──
from lib.project_mod.config import (
    BINARY_EXTENSIONS,
    CODE_EXTENSIONS,
    DANGEROUS_PATTERNS,
    DATA_EXTENSIONS,
    IGNORE_DIRS,
    IGNORE_FILES,
    LINE_COUNT_LIMIT,
    MAX_COMMAND_OUTPUT,
    MAX_COMMAND_TIMEOUT,
    MAX_DATA_FILE_PREVIEW,
    MAX_FILE_SIZE,
    MAX_GREP_RESULTS,
    MAX_READ_CHARS,
    MAX_SCAN_FILES,
    MAX_TREE_ENTRIES,
    SESSIONS_DIR,
    SHELL_PREFIX,
    _lock,
    _make_root_state,
    _roots,
    _state,
    clear_recent_projects,
    get_project_path,
    get_recent_projects,
    get_root_path,
    get_roots,
    get_state,
    resolve_namespaced_path,
    save_recent_project,
)

# ── Context ──
from lib.project_mod.indexer import (
    get_context_for_prompt,
)

# ── Modifications / Undo ──
from lib.project_mod.modifications import (
    _record_modification,
    get_conv_ids_with_modifications,
    get_modifications,
    undo_all_modifications,
    undo_conv_modifications,
    undo_task_modifications,
)

# ── Scanner (path registration, no background scan) ──
from lib.project_mod.scanner import (
    _fmt_size,
    _is_data_file,
    _is_likely_data_content,
    _safe_path,
    _should_ignore,
    add_project_root,
    clear_project,
    ensure_project_state,
    list_roots,
    remove_project_root,
    rescan,
    set_project,
    set_project_paths,
)

# ── Tools ──
from lib.project_mod.tools import (
    browse_directory,
    execute_standalone_command,
    execute_tool,
    project_tool_display,
    tool_apply_diff,
    tool_create_project,
    tool_find_files,
    tool_find_files_batch,
    tool_grep,
    tool_grep_batch,
    tool_insert_content,
    tool_list_dir,
    tool_read_files,
    tool_run_command,
    tool_write_file,
)

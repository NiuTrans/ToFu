"""
Project Co-Pilot package.

Decomposed from monolithic lib/project.py into:
  - config.py         — Constants, state, ignore lists
  - modifications.py  — Session/undo/redo system
  - scanner.py        — File scanning and tree building
  - tools.py          — Tool implementations and dispatch
  - indexer.py        — AI-powered file indexing
"""

__all__ = [
    # Config & State
    '_lock', '_state', '_ScanAborted',
    'get_state', 'get_project_path',
    'get_recent_projects', 'save_recent_project', 'clear_recent_projects',
    'IGNORE_DIRS', 'IGNORE_FILES', 'BINARY_EXTENSIONS',
    'MAX_FILE_SIZE', 'MAX_SCAN_FILES', 'MAX_TREE_ENTRIES', 'MAX_READ_CHARS',
    'MAX_GREP_RESULTS', 'LINE_COUNT_LIMIT',
    'INDEX_MODEL', 'PARALLEL_INDEX_THRESHOLD', 'LARGE_FILE_THRESHOLD',
    'INDEX_DIR', 'SESSIONS_DIR', 'SKIP_INDEX_THRESHOLD',
    'rate_limiter',
    'MAX_COMMAND_TIMEOUT', 'MAX_COMMAND_OUTPUT', 'SHELL_PREFIX',
    'DANGEROUS_PATTERNS', 'CODE_EXTENSIONS', 'DATA_EXTENSIONS',
    'MAX_INDEX_FILE_SIZE', 'MAX_DATA_FILE_PREVIEW',
    # Modifications / Undo
    'get_modifications', 'get_conv_ids_with_modifications',
    'undo_conv_modifications', 'undo_task_modifications', 'undo_all_modifications',
    '_record_modification', '_schedule_index_update',
    # Scanner
    'set_project', 'set_project_paths', 'clear_project', 'rescan',
    'add_project_root', 'remove_project_root', 'list_roots',
    '_scan_worker', '_scan_and_build_tree',
    '_should_ignore', '_is_data_file', '_is_likely_data_content',
    '_fmt_size', '_safe_path',
    # Multi-Root Config
    '_roots', '_make_root_state', 'resolve_namespaced_path',
    'get_roots', 'get_root_path',
    # Tools
    'tool_list_dir', 'tool_read_file', 'tool_grep', 'tool_find_files',
    'tool_write_file', 'tool_apply_diff', 'tool_run_command',
    'execute_tool', 'execute_standalone_command',
    'project_tool_display', 'browse_directory',
    # Indexer
    'start_indexing', 'get_context_for_prompt',
    '_load_cached_index', '_save_index',
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
    INDEX_DIR,
    INDEX_MODEL,
    LARGE_FILE_THRESHOLD,
    LINE_COUNT_LIMIT,
    MAX_COMMAND_OUTPUT,
    MAX_COMMAND_TIMEOUT,
    MAX_DATA_FILE_PREVIEW,
    MAX_FILE_SIZE,
    MAX_GREP_RESULTS,
    MAX_INDEX_FILE_SIZE,
    MAX_READ_CHARS,
    MAX_SCAN_FILES,
    MAX_TREE_ENTRIES,
    PARALLEL_INDEX_THRESHOLD,
    SESSIONS_DIR,
    SHELL_PREFIX,
    SKIP_INDEX_THRESHOLD,
    _lock,
    _make_root_state,
    _roots,
    _ScanAborted,
    _state,
    clear_recent_projects,
    get_project_path,
    get_recent_projects,
    get_root_path,
    get_roots,
    get_state,
    rate_limiter,
    resolve_namespaced_path,
    save_recent_project,
)

# ── Indexer ──
from lib.project_mod.indexer import (
    _load_cached_index,
    _save_index,
    get_context_for_prompt,
    start_indexing,
)

# ── Modifications / Undo ──
from lib.project_mod.modifications import (
    _record_modification,
    _schedule_index_update,
    get_conv_ids_with_modifications,
    get_modifications,
    undo_all_modifications,
    undo_conv_modifications,
    undo_task_modifications,
)

# ── Scanner ──
from lib.project_mod.scanner import (
    _fmt_size,
    _is_data_file,
    _is_likely_data_content,
    _safe_path,
    _scan_and_build_tree,
    _scan_worker,
    _should_ignore,
    add_project_root,
    clear_project,
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
    tool_find_files,
    tool_grep,
    tool_list_dir,
    tool_read_file,
    tool_run_command,
    tool_write_file,
)

"""Context generation for project co-pilot.

The LLM relies entirely on tool-based exploration (grep_search, find_files,
list_dir, read_files) to understand project structure at runtime.

This module provides ``get_context_for_prompt()`` which assembles
the system-prompt context block (project path, CLAUDE.md, multi-root
instructions, tool docs).  No file tree is injected — the model
discovers the project structure on demand via tools.
"""
import os

from lib.log import get_logger
from lib.project_mod.config import (
    _lock,
    _roots,
    _state,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  Context for Chat
# ═══════════════════════════════════════════════════════

def get_context_for_prompt(base_path=None):
    """Build the system-prompt context block for a project co-pilot session.

    Includes: project path, CLAUDE.md, multi-root workspace instructions,
    and tool documentation.  No file tree is injected.
    """
    with _lock:
        path = base_path or _state['path']
        # Collect extra roots for multi-root workspace
        extra_roots = {}
        _roots_snapshot = {}
        for rn, rs in _roots.items():
            _roots_snapshot[rn] = rs.copy()
            if rs['path'] != path:
                extra_roots[rn] = rs.copy()
    if not path:
        return None

    logger.debug('[Context] Building prompt for path=%s, extra_roots=%s',
                 path, list(extra_roots.keys()) if extra_roots else '[]')

    tools_section = """
Tools for code exploration:
- list_dir(path) — List directory contents
- read_files(reads) — Read one or more files/ranges in a single call (up to 20). Each entry: {path, start_line?, end_line?}
- grep_search(pattern, path?, include?) — Search patterns (regex) across files
- find_files(pattern, path?) — Find files by name glob

Tools for code modification:
- write_file(path, content, description?) — Write/create a file (overwrites entirely)
- apply_diff(path, search, replace, description?, replace_all?) — Apply targeted search-and-replace edit
  Set replace_all=true to replace ALL occurrences (default: errors on multiple matches for safety).
  For MULTIPLE edits, pass an 'edits' array: apply_diff(edits=[{path, search, replace, replace_all?, description?}, ...])
  Edits are applied sequentially so later edits see earlier changes. Much faster than separate calls.
- insert_content(path, anchor, content, position?, description?) — Insert new content before or after an anchor string without replacing it.
  The 'anchor' must match exactly once (errors on 0 or multiple matches, like apply_diff's search).
  position='before' or 'after' (default: 'after'). For MULTIPLE insertions, pass an 'edits' array.
- create_project(path, name?, overwrite?) — Create a NEW project directory at the given absolute/~-prefixed path and
  register it as an EXTRA workspace root. REQUIRED before writing files that live OUTSIDE the currently-open project
  (e.g. "generate a complete new repo at /some/path while referencing this one"). After calling this, address the new
  project via '<rootName>:<rel/path>' (preferred) or an absolute path under it. The currently-open project is NOT
  replaced — it remains available for reading as reference. System paths like /etc, /usr, $HOME are rejected.
- run_command(command, timeout?, working_dir?) — Execute shell command. In multi-root workspaces, use working_dir='rootname:' to run in a specific root.

Token-saving tools (use these to avoid re-generating existing content):
- emit_to_user(comment) — TERMINAL: end your turn by pointing the user to the most recent tool result they can already see, instead of re-outputting it.
  Use when a tool's raw output fully answers the question (e.g. command output, file contents, search results).
  The user sees all tool results in expandable panels. Just add a brief comment — do NOT repeat the output.
  This is a TERMINAL tool — calling it ends your turn immediately. Do NOT call other tools after this.
- write_file supports content_ref={tool_round, start?, end?} INSTEAD of content — to write a previous tool result to a file without regenerating it.
  Example: write_file(path="output.txt", content_ref={"tool_round": 3}) writes round 3's output to the file.
  Use content_ref whenever you need to save/copy content that already exists as a tool result from an earlier round.

Strategy:
1. Start with list_dir('.') to understand project structure, then use find_files() for specific files
2. Use grep_search to locate relevant code
3. Use read_files to examine files — batch multiple paths/ranges into ONE call to minimize round-trips
4. Provide answers with specific file paths and line numbers
5. When suggesting changes, show exact code with file path
6. Use apply_diff for small targeted edits, write_file for new files or major rewrites
7. When making multiple edits, prefer batch apply_diff(edits=[...]) over separate calls — this dramatically reduces round trips
8. **Prefer insert_content over apply_diff when the change is purely additive** (adding new lines without modifying existing ones). Examples: adding an import, appending to end of file, inserting a new function/method/block before or after existing code. insert_content is simpler (no need to repeat the anchor in both search and replace) and less error-prone.

⚠️ IMPORTANT — read WIDE, not narrow:
- When reading a function or class, read 200+ lines in one shot — don't read 50-line fragments and come back for more
- Prefer reading the WHOLE file (omit start_line/end_line) for files under 500 lines
- The server auto-expands to whole-file for files under ~40KB regardless of range, so don't worry about requesting too much

"""

    ctx = (f"[PROJECT CO-PILOT MODE]\n"
           f"Project: {path}\n\n")

    # ★ Cross-DC warning — let the LLM know about latency constraints
    try:
        from lib.cross_dc import get_latency_class, get_timeout_multiplier
        lat_class = get_latency_class(path)
        if lat_class in ('slow', 'very_slow'):
            multiplier = get_timeout_multiplier(path)
            ctx += (
                f"⚠️ CROSS-DATACENTER PROJECT — This project is on a remote DolphinFS cluster.\n"
                f"File I/O latency is {lat_class.replace('_', ' ')} (~{multiplier:.0f}x normal).\n"
                f"Timeouts are auto-adjusted but operations may still be slow.\n"
                f"Optimize by: batching reads, using targeted grep paths, avoiding deep tree walks.\n\n"
            )
    except Exception as e:
        logger.debug('[Indexer] cross_dc info unavailable: %s', e)

    # ═══════════════════════════════════════════════════════
    #  Multi-Root: append extra workspace roots
    # ═══════════════════════════════════════════════════════
    if extra_roots:
        primary_name = None
        for _rn, _rs in _roots_snapshot.items():
            if _rs.get('path') == path:
                primary_name = _rn
                break
        primary_name = primary_name or os.path.basename(path)

        ctx += f"\n{'='*50}\n"
        ctx += f"⚠️ MULTI-ROOT WORKSPACE — {1 + len(extra_roots)} roots active\n"
        ctx += f"{'='*50}\n"
        ctx += (
            f"MANDATORY: You MUST use 'rootname:path' prefix for ALL file operations\n"
            f"targeting non-primary roots. This applies to BOTH reading AND writing\n"
            f"(including creating new files). Without the prefix, paths resolve under\n"
            f"the PRIMARY root ({primary_name}).\n\n"
            f"⚠️ CREATING NEW FILES: When creating a new file in a non-primary root,\n"
            f"you MUST use the rootname prefix. There is no auto-detection for new files\n"
            f"(the file doesn't exist yet to check). Forgetting the prefix will create\n"
            f"the file in the wrong project.\n\n"
            f"Root prefix table:\n"
            f"  {primary_name}: → {path} (PRIMARY — default when no prefix)\n"
        )
        for rn, rs in extra_roots.items():
            ctx += f"  {rn}: → {rs['path']}\n"
        ctx += (
            f"\nExamples:\n"
            f"  read_files([{{path: '{primary_name}:src/main.py'}}])   — read from primary\n"
        )
        first_extra = next(iter(extra_roots))
        ctx += (
            f"  read_files([{{path: '{first_extra}:src/main.py'}}])   — read from extra root\n"
            f"  write_file(path='{first_extra}:config.yaml', ...)     — write to extra root\n"
            f"  write_file(path='{first_extra}:src/new_file.py', ...) — CREATE in extra root\n"
            f"  run_command(command='npm test', working_dir='{first_extra}:')  — run in extra root\n"
            f"  grep_search(pattern='TODO', path='{first_extra}:src') — search in extra root\n"
            f"  apply_diff(edits=[{{path: '{first_extra}:file.py', ...}}])  — batch edit in extra root\n"
        )
        ctx += "\n"
        for rn, rs in extra_roots.items():
            ctx += f"[{rn}] {rs['path']}\n\n"

    # ═══════════════════════════════════════════════════════
    #  CLAUDE.md / Project Intelligence auto-detection
    # ═══════════════════════════════════════════════════════
    _INTELLIGENCE_FILES = ['CLAUDE.md', '.cursorrules', 'AGENTS.md', 'COPILOT.md']
    for intel_name in _INTELLIGENCE_FILES:
        intel_path = os.path.join(path, intel_name)
        if os.path.isfile(intel_path):
            try:
                with open(intel_path, encoding='utf-8', errors='replace') as f:
                    intel_content = f.read(32_000)
                if intel_content.strip():
                    ctx += (f"\n{'='*50}\n"
                            f"📋 Project Intelligence — {intel_name}\n"
                            f"{'='*50}\n"
                            f"(Auto-detected from {intel_path})\n"
                            f"⚠️ MANDATORY: All code changes in this project MUST comply with the rules below.\n\n"
                            f"{intel_content.strip()}\n")
                    logger.info('[Context] Injected project intelligence file: %s (%d chars)',
                                intel_path, len(intel_content))
            except OSError as e:
                logger.warning('[Context] Failed to read project intelligence file %s: %s',
                               intel_path, e)

    ctx += tools_section
    return ctx

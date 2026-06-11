"""Context generation for project co-pilot.

The LLM relies entirely on tool-based exploration (grep_search, find_files,
list_dir, read_files) to understand project structure at runtime.

This module provides ``get_context_for_prompt()`` which assembles the
SYSTEM-LEVEL context block: the project header, multi-root workspace topology,
and any project intelligence file (CLAUDE.md / .cursorrules / AGENTS.md /
COPILOT.md) that lives in the workspace.

It does NOT enumerate per-tool descriptions — each tool's own usage prose now
lives in its API-level ``description`` field (see ``lib/tools/*.py``), which
the model receives as part of the standard ``tools: [...]`` parameter on every
request.  Cross-cutting routing meta lives in
``lib.tasks_pkg.system_prompt_cc.section_using_tools``.

This split mirrors Claude Code's architecture (per-tool ``prompt()`` methods +
small ``getUsingYourToolsSection`` cross-cutting policy) and avoids duplicating
tool docs in the cache-sensitive system prefix.
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

def get_context_for_prompt(base_path=None, conv_id=None):
    """Build the system-prompt project-context block for a session.

    Contains only *system-level* context — the project header, multi-root
    topology, and auto-detected project intelligence files.  Per-tool usage
    prose lives in each tool's own ``description`` field (see
    ``lib/tools/*.py``); cross-cutting routing meta lives in
    ``lib.tasks_pkg.system_prompt_cc.section_using_tools``.

    ★ ``conv_id`` (2026-06-03): when provided, the advertised multi-root
    table is sourced from this conversation's per-conv registry
    (``get_conv_roots``) instead of the global ``_roots``.  This MUST match
    the registry that ``resolve_namespaced_path`` consults at tool-call
    time — otherwise a concurrent task's ``set_project`` can leak a foreign
    root name (e.g. ``chatui``) into this conv's prompt, the model dutifully
    emits ``chatui:...`` paths, and resolution then rejects them as
    ``Unknown workspace root`` (the conv's own registry never had that
    root).  Without ``conv_id`` we keep reading the global registry for the
    single-user UI / legacy path.
    """
    with _lock:
        path = base_path or _state['path']
    # Source the root set from the per-conv registry when we know the conv,
    # so the prompt's root table agrees with the resolver's strict isolation.
    if conv_id:
        from lib.project_mod.config import get_conv_roots
        _roots_snapshot = get_conv_roots(conv_id)
    else:
        with _lock:
            _roots_snapshot = {rn: rs.copy() for rn, rs in _roots.items()}
    extra_roots = {rn: rs for rn, rs in _roots_snapshot.items()
                   if rs.get('path') != path}
    if not path:
        return None

    logger.debug('[Context] Building prompt for path=%s, extra_roots=%s',
                 path, list(extra_roots.keys()) if extra_roots else '[]')

    ctx = (f"[PROJECT CO-PILOT MODE]\n"
           f"Project: {path}\n\n")

    # ★ Cross-DC warning — let the LLM know about latency constraints
    try:
        from lib.cross_dc import get_latency_class, get_timeout_multiplier
        lat_class = get_latency_class(path)
        if lat_class in ('slow', 'very_slow'):
            multiplier = get_timeout_multiplier(path)
            ctx += (
                f"CROSS-DATACENTER PROJECT — This project is on a remote DolphinFS cluster.\n"
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

        # Per-root access flags (read-only = reference-only attachment).
        primary_ro = False
        for _rn, _rs in _roots_snapshot.items():
            if _rs.get('path') == path:
                primary_ro = _rs.get('access') == 'ro'
                break
        any_ro = primary_ro or any(rs.get('access') == 'ro'
                                   for rs in extra_roots.values())

        first_extra_path = next(iter(extra_roots.values()))['path']
        ctx += f"\n{'='*50}\n"
        ctx += f"MULTI-ROOT WORKSPACE — {1 + len(extra_roots)} roots active\n"
        ctx += f"{'='*50}\n"
        ctx += (
            f"This session spans several project roots:\n"
            f"  {path}  (PRIMARY{', READ-ONLY' if primary_ro else ''})\n"
        )
        for rn, rs in extra_roots.items():
            _ro = '  (READ-ONLY)' if rs.get('access') == 'ro' else ''
            ctx += f"  {rs['path']}{_ro}\n"
        if any_ro:
            ctx += (
                "\nREAD-ONLY roots are attached for reference only: you may "
                "read / grep / list them, but write_file, apply_diff, "
                "insert_content, create_project, and file-modifying "
                "run_command targeting a read-only root will be REFUSED. "
                "Make your edits in a writable root.\n"
            )
        ctx += (
            f"\nHow to address a file in any root — two equivalent ways:\n\n"
            f"  1. ABSOLUTE path (simplest, most reliable — works for reads AND writes,\n"
            f"     including creating new files):\n"
            f"       read_files([{{path: '{first_extra_path}/src/main.py'}}])\n"
            f"       write_file(path='{first_extra_path}/src/new_file.py', ...)\n\n"
            f"  2. 'rootname:rel' shorthand (optional convenience — the names below):\n"
            f"       Root names:  {primary_name}: → {path} (PRIMARY)\n"
        )
        for rn, rs in extra_roots.items():
            ctx += f"                    {rn}: → {rs['path']}\n"
        first_extra = next(iter(extra_roots))
        ctx += (
            f"       write_file(path='{first_extra}:src/new_file.py', ...)\n"
            f"       run_command(command='npm test', working_dir='{first_extra}:')\n\n"
            f"A BARE relative path (no '/' prefix, no 'rootname:') resolves under the\n"
            f"PRIMARY root ({primary_name}). To create or edit a file in another root,\n"
            f"give its absolute path or the 'rootname:' prefix — a bare relative path\n"
            f"will land in the PRIMARY root.\n\n"
            f"Tip: if you read a file by its absolute path, write it back with the SAME\n"
            f"absolute path — no need to translate it into a 'rootname:' prefix.\n"
        )

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
                            f"Project Intelligence — {intel_name}\n"
                            f"{'='*50}\n"
                            f"(Auto-detected from {intel_path})\n"
                            f"MANDATORY: All code changes in this project MUST comply with the rules below.\n\n"
                            f"{intel_content.strip()}\n")
                    logger.info('[Context] Injected project intelligence file: %s (%d chars)',
                                intel_path, len(intel_content))
            except OSError as e:
                logger.warning('[Context] Failed to read project intelligence file %s: %s',
                               intel_path, e)

    return ctx

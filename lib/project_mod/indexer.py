"""Context generation for project co-pilot.

The LLM relies entirely on tool-based exploration (grep_search, find_files,
list_dir, read_files) to understand project structure at runtime.

This module provides ``get_context_for_prompt()`` which assembles the
SYSTEM-LEVEL context block: the project header, multi-root workspace topology,
any project intelligence file (CLAUDE.md / .cursorrules / AGENTS.md /
COPILOT.md) that lives in the workspace, and the project evolution journal
(``JOURNAL.md`` — auto-seeded on a writable primary root that lacks one, then
injected so the model reads and maintains it).

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

# Name of the project iteration journal — a living lab-notebook the agent
# reads AND writes (distinct from the immutable CLAUDE.md rules file). Named
# ``JOURNAL.md`` rather than ``CHANGE.md``/``CHANGELOG.md`` so the model does
# not fall into the "Keep a Changelog" prior (terse versioned release bullets)
# — a free-form dev journal is what we want.
_JOURNAL_FILE = 'JOURNAL.md'


def _ensure_journal_gitignored(root: str) -> None:
    """Add ``JOURNAL.md`` to ``root``'s ``.gitignore`` so it isn't committed.

    The journal is a per-developer working artifact, not source — auto-creating
    it must not cause accidental commits. We only touch ``.gitignore`` when the
    root is a git repo (has a ``.git`` entry) or already has a ``.gitignore``;
    we never create a stray ``.gitignore`` in a non-git directory. Idempotent:
    if the file is already ignored (as a bare or rooted entry) we do nothing.
    Best-effort — any failure is logged, never raised into prompt build.
    """
    gitignore = os.path.join(root, '.gitignore')
    has_gitignore = os.path.isfile(gitignore)
    if not has_gitignore and not os.path.exists(os.path.join(root, '.git')):
        return  # not a git repo and no .gitignore — leave the dir untouched
    try:
        existing = ''
        if has_gitignore:
            with open(gitignore, encoding='utf-8', errors='replace') as f:
                existing = f.read()
            ignored = {ln.strip() for ln in existing.splitlines()}
            if _JOURNAL_FILE in ignored or f'/{_JOURNAL_FILE}' in ignored:
                return  # already ignored
        block = (f"# AI assistant evolution journal (per-developer, not source)\n"
                 f"{_JOURNAL_FILE}\n")
        if existing and not existing.endswith('\n'):
            block = '\n' + block
        with open(gitignore, 'a', encoding='utf-8') as f:
            f.write(block)
        logger.info('[Context] Added %s to %s', _JOURNAL_FILE, gitignore)
    except OSError as e:
        logger.warning('[Context] Failed to update .gitignore at %s: %s', gitignore, e)


def _journal_seed() -> str:
    """Return the seed content written when a project has no ``JOURNAL.md``.

    The seed explains what the journal is for, lays out the basic rules for
    tracking large-scale project evolution, and explicitly instructs the
    agent to keep updating it and to reference it at the start of each
    session. It is intentionally rule-text only (no fabricated history) — the
    agent fills in real entries as the project evolves.
    """
    return """# Project Journal

> This file is the project's **evolution journal** — a living record the AI
> assistant maintains across sessions. It is NOT a rules file (project rules
> live in CLAUDE.md / AGENTS.md if present) and NOT a versioned changelog.
> It is a free-form dev log of *how and why* this project changes over time.

## How to use this file

- **Read it first.** At the start of a session, read this journal to understand
  how the project reached its current state before making changes.
- **Keep it current — on your own initiative.** After any meaningful change,
  append a dated entry yourself. This is a standing, pre-authorized action: you
  do NOT need to ask the user before adding an entry, and you should not pause
  to request permission to update the journal. Do not rewrite or delete past
  entries — the history is the point; only ever append.
- **Record the *why*, not just the *what*.** A diff shows what changed; this
  journal explains the reasoning a future reader (human or model) could not
  reconstruct from the code alone.

## What to record

- **Experimental projects:** methods/approaches tried, why each was adopted or
  abandoned, hyperparameter or design changes, and experiment results
  (metrics, observations, dead ends).
- **Engineering projects:** technology-selection changes and their rationale,
  refactoring steps and their motivation, architectural decisions, and the
  current status / known issues / next steps.

## Entries

<!-- Append newest entries at the top. Suggested format:

### YYYY-MM-DD — short title
- **Change:** what changed
- **Why:** the reasoning / problem being solved
- **Result / status:** outcome, metrics, or current state
-->
"""


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

    # Is the PRIMARY root writable?  Used below to decide whether to nudge
    # the model to create the iteration journal (never nudge inside a
    # read-only root — writes there are refused anyway).
    primary_is_ro = any(rs.get('access') == 'ro'
                        for rs in _roots_snapshot.values()
                        if rs.get('path') == path)

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

    # ═══════════════════════════════════════════════════════
    #  JOURNAL.md — the project evolution journal (auto-created)
    # ═══════════════════════════════════════════════════════
    # Unlike CLAUDE.md (immutable rules the agent OBEYS), JOURNAL.md is a
    # living dev log the agent READS and WRITES: methods/tech tried and why
    # they changed, experiment results, refactoring decisions and current
    # status. Option A: when the primary root is WRITABLE and has no journal,
    # we lazily SEED one here (at context-build time, not at path-selection
    # time) with the evolution-tracking rules + the update/reference
    # instruction, so the model both knows the file exists and is told to keep
    # it current. We NEVER clobber an existing journal, and read-only primaries
    # are skipped entirely (a write there would be refused).
    journal_path = os.path.join(path, _JOURNAL_FILE)
    journal_content = ''
    journal_exists = os.path.isfile(journal_path)
    if not journal_exists and not primary_is_ro:
        # Double-check the resolver agrees this path is writable before
        # touching disk — a root may be flagged read-only via the conv-scoped
        # registry even when the snapshot's primary entry isn't.
        try:
            from lib.project_mod.config import is_readonly_path
            blocked = is_readonly_path(journal_path, conv_id)
        except Exception as e:
            logger.debug('[Context] is_readonly_path check failed for %s: %s',
                         journal_path, e)
            blocked = False
        if not blocked:
            try:
                from lib.json_store import write_text_atomic
                write_text_atomic(journal_path, _journal_seed())
                journal_exists = True
                logger.info('[Context] Auto-created project journal: %s', journal_path)
                _ensure_journal_gitignored(path)
                try:
                    from lib.log import audit_log
                    audit_log('journal_autocreate', path=journal_path)
                except Exception as _e:
                    logger.debug('[Context] journal audit_log failed: %s', _e)
            except OSError as e:
                logger.warning('[Context] Failed to auto-create journal %s: %s',
                               journal_path, e)

    if journal_exists:
        try:
            with open(journal_path, encoding='utf-8', errors='replace') as f:
                journal_content = f.read(32_000)
        except OSError as e:
            logger.warning('[Context] Failed to read journal file %s: %s',
                           journal_path, e)
            journal_content = ''

    if journal_content.strip():
        ctx += (f"\n{'='*50}\n"
                f"Project Journal — {_JOURNAL_FILE}\n"
                f"{'='*50}\n"
                f"(Auto-detected from {journal_path})\n"
                f"This is the project's evolution journal — a record of exploration "
                f"and decisions, NOT a rules file. Read it to understand how the "
                f"project reached its current state (method/technology changes and "
                f"why, experiment results, refactoring history, current status). As "
                f"you make meaningful changes, APPEND dated entries on your own "
                f"initiative so the next session can pick up where you left off — "
                f"updating this journal is a standing, pre-authorized action that "
                f"needs NO user confirmation; never pause to ask before adding an "
                f"entry.\n\n"
                f"{journal_content.strip()}\n")
        logger.info('[Context] Injected project journal: %s (%d chars)',
                    journal_path, len(journal_content))

    return ctx

"""Context generation for project co-pilot.

The LLM relies entirely on tool-based exploration (grep_search, find_files,
read_files, and bounded ``ls`` routing through run_command) to understand
project structure at runtime.

This module provides ``get_context_for_prompt()`` which assembles the
SYSTEM-LEVEL context block: the project header, multi-root workspace topology,
any project intelligence file (CLAUDE.md / .cursorrules / AGENTS.md /
COPILOT.md) that lives in the workspace, and a short project evolution journal
locator. Context acquisition is read-only: it does not seed, rotate, or inject
journal history, or update .gitignore.

It does NOT enumerate per-tool descriptions — each tool's own usage prose now
lives in its API-level ``description`` field (see ``lib/tools/*.py``), which
the model receives as part of the standard ``tools: [...]`` parameter on every
request.  Cross-cutting routing meta lives in
``lib.tasks_pkg.system_prompt_base.section_using_tools``.

This split (per-tool usage prose in the tool's own schema + a small
cross-cutting routing section in the built-in prompt) avoids duplicating
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

# Hidden runtime-state artifacts the assistant writes INTO the selected project
# directory (file-history + memories under .tofu/, .tofu_trash/ recoverable
# deletes, .tofu_sandbox/ shims, .tofu_env.json marker).  None are source —
# they are per-developer / per-host working state, so we keep them out of git.
# A SINGLE glob (.tofu*) from the central registry covers every current AND
# future ``.tofu``-prefixed artifact, so this never needs editing when a new
# one is introduced — see lib/agent_loop/agent_artifacts.py for the naming convention.
from lib.agent_loop.agent_artifacts import GITIGNORE_PATTERN as _TOFU_ARTIFACT_GLOB

_TOFU_ARTIFACT_IGNORES = (_TOFU_ARTIFACT_GLOB,)


def _within_git_tree(path: str) -> bool:
    """Return True if ``path`` is itself a git repo or lives inside one.

    Walks ``path`` and its ancestors looking for a ``.git`` entry (capped at a
    sane depth so a stray FUSE/NFS mount can't make this loop forever).  This
    is broader than a self-only ``.git`` check because the selected project is
    often a SUB-DIRECTORY of a repo — in which case our hidden artifacts would
    still show up in the parent repo's ``git status``, so the ``.gitignore``
    matters there too.
    """
    try:
        cur = os.path.abspath(path)
    except OSError as e:
        logger.debug('[Context] _within_git_tree abspath failed for %s: %s', path, e)
        return False
    for _ in range(40):
        if os.path.exists(os.path.join(cur, '.git')):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return False


def _gitignore_has_entry(existing_lines: set, entry: str) -> bool:
    """True if ``entry`` is already covered by one of ``existing_lines``.

    Matches the bare / rooted / trailing-slash variants (``.tofu``,
    ``.tofu/``, ``/.tofu``, ``/.tofu/``) so we never append a duplicate that
    differs only in slash decoration.
    """
    core = entry.strip().strip('/').lstrip('/')
    if not core:
        return True
    variants = {core, f'{core}/', f'/{core}', f'/{core}/'}
    return bool(existing_lines & variants)


def _ensure_gitignored(root: str, entries, header: str) -> None:
    """Ensure each of ``entries`` is listed in ``root``'s ``.gitignore``.

    Creates ``.gitignore`` when missing AND the root is part of a git working
    tree (``.git`` in the root or an ancestor); we never conjure a stray
    ``.gitignore`` in a directory that has nothing to do with git.  When a
    ``.gitignore`` already exists we always append (whatever it's for).
    Idempotent — entries already present (in any slash form) are skipped, and
    only the genuinely-missing ones are appended under a single ``header``
    comment.  Best-effort: any failure is logged, never raised into the
    prompt build.
    """
    gitignore = os.path.join(root, '.gitignore')
    has_gitignore = os.path.isfile(gitignore)
    if not has_gitignore and not _within_git_tree(root):
        return  # not under git and no .gitignore — leave the dir untouched
    try:
        existing = ''
        existing_lines = set()
        if has_gitignore:
            with open(gitignore, encoding='utf-8', errors='replace') as f:
                existing = f.read()
            existing_lines = {ln.strip() for ln in existing.splitlines()}
        missing = [e for e in entries
                   if not _gitignore_has_entry(existing_lines, e)]
        if not missing:
            return  # everything already ignored
        block = f"# {header}\n" + '\n'.join(missing) + '\n'
        if existing and not existing.endswith('\n'):
            block = '\n' + block
        with open(gitignore, 'a', encoding='utf-8') as f:
            f.write(block)
        logger.info('[Context] Added %s to %s', missing, gitignore)
    except OSError as e:
        logger.warning('[Context] Failed to update .gitignore at %s: %s', gitignore, e)


def _ensure_journal_gitignored(root: str) -> None:
    """Add ``JOURNAL.md`` to ``root``'s ``.gitignore`` so it isn't committed.

    The journal is a per-developer working artifact, not source — auto-creating
    it must not cause accidental commits.  Thin wrapper over
    :func:`_ensure_gitignored` (same create-only-under-git + idempotent policy).
    """
    _ensure_gitignored(
        root, [_JOURNAL_FILE],
        'AI assistant evolution journal (per-developer, not source)')


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

def get_context_for_prompt(base_path=None, conv_id=None, *, include_guidance=True):
    """Build the system-prompt project-context block for a session.

    Contains only *system-level* context — the project header, multi-root
    topology, and auto-detected project intelligence files.  Per-tool usage
    prose lives in each tool's own ``description`` field (see
    ``lib/tools/*.py``); cross-cutting routing meta lives in
    ``lib.tasks_pkg.system_prompt_base.section_using_tools``.

    ``conv_id`` (2026-06-03): when provided, the advertised multi-root
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
        path = base_path or (None if conv_id else _state['path'])
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

    # Scope the journal maintenance reminder to writable primary roots.
    primary_is_ro = any(rs.get('access') == 'ro'
                        for rs in _roots_snapshot.values()
                        if rs.get('path') == path)

    logger.debug('[Context] Building prompt for path=%s, extra_roots=%s',
                 path, list(extra_roots.keys()) if extra_roots else '[]')

    ctx = (f"[PROJECT CO-PILOT MODE]\n"
           f"Project: {path}\n\n")

    # Cross-DC warning — let the LLM know about latency constraints
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
                "read / search / list them, but file-writing tools and "
                "file-modifying commands targeting a read-only root will be REFUSED. "
                "Make your edits in a writable root.\n"
            )
        ctx += (
            f"\nHow to address a file in any root — two equivalent ways:\n\n"
            f"  1. ABSOLUTE path (simplest, most reliable — works for reads AND writes,\n"
            f"     including creating new files):\n"
            f"       {first_extra_path}/src/main.py\n"
            f"     Use the live tool schema for names, arguments, and write authorization.\n\n"
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
    from lib.project_mod.instructions import read_root_guidance, render_guidance
    if include_guidance:
        rule_rows = []
        for root in dict.fromkeys([path, *(rs['path'] for rs in extra_roots.values())]):
            rule_rows.extend(read_root_guidance(root))
        # Standalone callers retain complete rules. Task execution delivers
        # them once through the raw-witness scoped guidance owner instead.
        ctx += render_guidance(rule_rows)

    # Context reads advertise the journal location, never its history or a
    # maintenance lifecycle. Historical evidence is retrieved only as needed.
    journal_path = os.path.join(path, _JOURNAL_FILE)
    ctx += (f"\nProject Journal — {_JOURNAL_FILE}\n"
            f"Location: {journal_path}\n"
            "Historical evidence, NOT current project rules. Read relevant entries "
            "only when needed for this task; verify claims against current code/tests.\n")
    if primary_is_ro:
        ctx += "READ-ONLY primary root: do not create or update its journal.\n"

    return ctx

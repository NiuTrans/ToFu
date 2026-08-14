"""lib/memory/injection.py — System prompt injection and memory context building.

Memory injection strategy (tool-based, on-demand):
  • NO listing of memories is injected into the prompt.
  • The system message tells the model it has `search_memories` tool and
    how many memories are available.
  • The model calls `search_memories(query)` when it needs past experience,
    using its own keywords for BM25 matching (including body content).
  • This saves ~2K tokens/turn that were previously used for the index,
    and lets the model search with better keywords than the raw user message.
"""


from lib.memory.storage import get_eligible_memories

__all__ = [
    'MEMORY_ACCUMULATION_INSTRUCTIONS',
    'MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT',
    'build_memory_context',
]


MEMORY_ACCUMULATION_INSTRUCTIONS = """<memory_accumulation>
You have memory management tools to maintain a reusable knowledge base across sessions:
- `search_memories` — Search your memories by keyword (BM25). Call this when you suspect a project-specific convention or past lesson applies.
- `create_memory` — Save a NEW memory (accumulated experience).
- `update_memory` — Update an EXISTING memory's content, description, or tags.
- `delete_memory` — Remove an outdated or incorrect memory.
- `merge_memories` — Combine multiple overlapping/related memories into one consolidated memory.

Installed skill packages (if any) are listed in the `<available_skills>` block —
they are instruction GUIDES, a different thing from memories. Load one with
`load_skill` when the task matches its description. Memory tools cannot
modify skill packages (they are user-managed).

**When to SEARCH memories (narrow triggers):**
- You suspect this exact project has an established convention you don't remember (style, naming, architecture).
- You're about to redo something the user has previously corrected you on in this project.
- You're hitting a tricky bug AND it feels like one you might have logged before.

**When NOT to search memories (use other tools instead):**
- The user mentions a local file path or directory → use `read_files` / `list_dir` directly. Don't search memory for it.
- The user asks about an external project, library, product, or service (e.g. claude-code, openclaw, citadel) → use `web_search` and/or read the local copy with `read_files` / `list_dir`. Memory is unlikely to have it.
- General coding / API knowledge questions → answer from training, or `web_search` for fresh info.
- A relevant `<relevant_memories>` block was already prefetched and injected this turn → it's already done; don't re-search the same topic.

**When to CREATE a memory:**
1. A project convention was verified in repository evidence.
2. A recurring failure pattern has a confirmed root cause and fix.
3. A tool/API quirk was reproduced or documented and will matter again.

**Never store as memory:** user identity, user preferences, a one-off task,
conversation summaries, speculative reasoning, or a transcript of how you solved
the current problem. Durable user-specific facts belong to My Context; only the
post-turn context learner manages that store.

Guidelines:
- Keep each memory focused on ONE topic.
- Use `scope='project'` for project-specific, `scope='global'` for general patterns.
- Don't duplicate — search first, then create/update/merge as needed.
- Don't ask permission — manage memories quietly when relevant.

**Description quality is critical** — both `search_memories` and the per-turn
  prefetch rank with BM25 over name+description+body, and the description is shown
  verbatim to the relevance filter and in the injected block. Write ONE dense, specific
  sentence (~120 chars) that front-loads the concrete trigger words someone would search
  for: the symptom, the symbol / file name, and the fix or rule. Vague summaries
  ("fixes a bug") are useless for retrieval. Don't pad to a fixed length — pack signal.

**Body structure** — write the body as skimmable Markdown, not a conversation recap.
  A reliable skeleton (adapt section names as the topic needs):
    ## Symptom / Why   — what goes wrong and how to recognise it
    ## Fix / What      — the concrete change, with `file:line` and code / commands
    ## Guardrail       — the rule to follow next time, plus any test that covers it
</memory_accumulation>"""


# Compact version — stays in the system message for cache stability.
# Includes search_memories as the primary discovery mechanism.
MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT = """<memory_accumulation>
You have memory tools: search_memories (keyword search), create/update/delete/merge_memories.
Use search_memories when you suspect this project has an established convention or past lesson that applies.
Don't use it as a generic discovery step: if the user mentions a local path use read_files/list_dir;
if they ask about an external project/library use web_search. A `<relevant_memories>` block, when present,
already surfaces likely-relevant memories — don't re-search the same topic.
Proactively save a memory only for verified, reusable project conventions,
confirmed failure patterns, or reproduced/documented tool/API quirks. Never save
user identity/preferences, one-off requests, chat summaries, speculative model
reasoning, or a solution transcript; user-specific facts belong to My Context.
Keep each memory focused on ONE topic; write a dense ~120-char description that
front-loads trigger words (symptom, symbol/file, fix) — it's the primary search signal —
and a skimmable Markdown body (e.g. Symptom/Why → Fix/What → Guardrail).
Use scope='project' or 'global'.
</memory_accumulation>"""



def build_memory_context(project_path=None, extra_paths=None):
    """Build a minimal memory hint for injection.

    Since memories are now discovered via the `search_memories` tool,
    this function only returns a short count hint (NOT a full listing).
    The model uses search_memories(query) to find relevant memories on demand.

    Args:
        project_path: Path to project for project-scoped memories.
        extra_paths: Additional workspace roots (multi-root session) whose
            memories are unioned in alongside the primary root's.

    Returns None if no eligible memories exist, otherwise a short hint string.
    """
    memories = get_eligible_memories(project_path, extra_paths=extra_paths)
    if not memories:
        return None

    # ★ CACHE-CRITICAL: this hint is appended to the system message (messages[0])
    #   as a cache-stable block. The exact memory COUNT must NOT appear here —
    #   it changes the moment the model calls create/delete/merge_memories
    #   during a turn, which rewrites bytes inside the cached prompt prefix and
    #   invalidates the ENTIRE messages-array cache from messages[0] onward
    #   (every following message reads from a now-changed prefix → full body
    #   re-write, cache_read pinned at the static tools/system floor). The
    #   precise N has marginal value to the model (it has the search_memories
    #   tool + the per-turn <relevant_memories> prefetch block), so we keep the
    #   wording byte-stable across memory-CRUD turns. See
    #   .tofu/memories/prefix-mutation-cache-miss-truncate-old-tool-results.md.
    return (
        'You have accumulated memories available. '
        'A `<relevant_memories>` block is auto-injected when prefetch finds matches; '
        'call search_memories(query) only when you specifically suspect a past project '
        'convention or logged lesson applies (not as a generic discovery step).'
    )

"""System context injection — append/prepend helpers and context layering.

Extracted from orchestrator.py to isolate the system-message manipulation
logic (project context, skills, swarm prompt, search addendum).

Includes delta attachment tracking (inspired by Claude Code): context strings
are hashed, and when the content is unchanged between successive tasks in the
same conversation, we **skip the expensive load** (FUSE I/O) but still inject
the text.  This is necessary because each task receives a *fresh* message list
from the frontend — the system message does NOT carry over project/skills
context from the previous task.

Includes Claude Code-inspired prompt sections:
  - Function Result Clearing notification (tells model old results are auto-cleared)
  - Tool result summarization guidance (tells model to write down important info)
  - Tool usage guidance (parallel calls, prefer dedicated tools)
  - Output efficiency guidance (concise, direct output)
"""

import hashlib

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.compaction import MICRO_HOT_TAIL
from lib.tasks_pkg.model_config import _build_search_addendum

# ═══════════════════════════════════════════════════════════════════════════════
#  Claude Code-inspired system prompt sections
# ═══════════════════════════════════════════════════════════════════════════════

_FUNCTION_RESULT_CLEARING_SECTION = f"""\
# Function Result Clearing

Old tool results will be automatically cleared from context to free up space. \
The {MICRO_HOT_TAIL} most recent results are always kept."""

_SUMMARIZE_TOOL_RESULTS_SECTION = """\
When working with tool results, write down any important information you \
might need later in your response, as the original tool result may be \
cleared later."""

_TOOL_USAGE_GUIDANCE = """\
# Using your tools
 - In general, do not propose changes to code you haven't read. If a user asks about or wants you to modify a file, read it first. Understand existing code before suggesting modifications.
 - You can call multiple tools in a single response. If you intend to call multiple tools and there are no dependencies between them, make all independent tool calls in parallel. Maximize use of parallel tool calls where possible to increase efficiency. However, if some tool calls depend on previous calls to inform dependent values, do NOT call these tools in parallel and instead call them sequentially.
 - When using web_search and fetch_url, review search result summaries first before deciding what to fetch. Use fetch_url on the 1-2 most promising URLs to read full content.
 - Prefer grep_search for finding code patterns (built-in fuzzy hints, context lines, case-insensitive). Prefer read_files for understanding code (returns with line numbers, supports batch reads). Prefer run_command for shell operations (counting, testing, building).
 - Use apply_diff for small targeted edits, write_file for new files or major rewrites. When making multiple edits, prefer batch apply_diff(edits=[...]) over separate calls — this dramatically reduces round trips.
 - If an approach fails, diagnose why before switching tactics — read the error, check your assumptions, try a focused fix. Don't retry the identical action blindly, but don't abandon a viable approach after a single failure either."""

_OUTPUT_EFFICIENCY_GUIDANCE = """\
# Output efficiency

Go straight to the point. Try the simplest approach first without going in circles. Do not overdo it. Be extra concise.

Keep your text output brief and direct. Lead with the answer or action, not the reasoning. Skip filler words, preamble, and unnecessary transitions. Do not restate what the user said — just do it.

Focus text output on:
- Decisions that need the user's input
- High-level status updates at natural milestones
- Errors or blockers that change the plan

If you can say it in one sentence, don't use three. Prefer short, direct sentences over long explanations. This does not apply to code or tool calls."""

# ── Delta attachment tracking ──
# Cache of (hash, text) per category per conv_id.
# Purpose: skip the expensive FUSE load (get_context_for_prompt /
# build_skills_context) when the content hasn't changed.
# IMPORTANT: we ALWAYS inject into the system message — we only skip
# the *computation*.  Each task gets fresh messages from the frontend,
# so the text is NOT already present.
_last_context_cache: dict[tuple[str, str], tuple[str, str]] = {}


def _context_hash(text: str) -> str:
    """Compute a fast hash for a context string."""
    return hashlib.md5(text.encode('utf-8', errors='replace')).hexdigest()[:16]


def _get_cached_or_compute(conv_id: str, category: str,
                            compute_fn) -> str:
    """Return cached context text if hash matches, else re-compute.

    Unlike the previous ``_should_inject`` (which skipped injection),
    this function always returns text — it only skips the *computation*.

    Args:
        conv_id:    Conversation ID for cache scoping.
        category:   'project' or 'skills'.
        compute_fn: Zero-arg callable that produces the context string.

    Returns:
        The context string (either from cache or freshly computed).
        Empty string if compute_fn returns falsy.
    """
    key = (conv_id, category)
    text = compute_fn()
    if not text:
        return ''
    h = _context_hash(text)
    prev = _last_context_cache.get(key)
    if prev and prev[0] == h:
        logger.debug('[DeltaCtx] Reusing cached %s context (hash=%s) conv=%s',
                     category, h[:8], conv_id[:8])
        return prev[1]  # cached text (should be identical)
    _last_context_cache[key] = (h, text)
    return text


_TIMESTAMP_PREFIX = 'Current date and time: '


def inject_search_addendum_to_user(messages: list, search_enabled: bool):
    """Inject a minimal timestamp into the last user message.

    Moved OUT of the system message to avoid breaking the prompt cache every
    minute.  Only the timestamp is injected — static search guidance lives in
    _TOOL_USAGE_GUIDANCE (system prompt, cached).

    Dedup: strips any previous timestamp from the same message before
    injecting, so it doesn't accumulate across rounds.
    """
    if not search_enabled:
        return
    addendum = _build_search_addendum()
    if not addendum:
        return
    # Find last user message and append (with dedup)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            if isinstance(content, str):
                # Strip any previously injected timestamp
                content = _strip_old_timestamp(content)
                messages[i]['content'] = content + '\n\n' + addendum
            elif isinstance(content, list):
                # Remove old timestamp text blocks
                messages[i]['content'] = [
                    b for b in messages[i]['content']
                    if not (isinstance(b, dict) and b.get('type') == 'text'
                            and b.get('text', '').strip().startswith(_TIMESTAMP_PREFIX))
                ]
                messages[i]['content'].append({
                    'type': 'text',
                    'text': '\n\n' + addendum,
                })
            return
    logger.debug('[SearchAddendum] No user message found to inject into')


def _strip_old_timestamp(text: str) -> str:
    """Remove a previously injected timestamp line from user message text."""
    lines = text.split('\n')
    cleaned = [ln for ln in lines if not ln.strip().startswith(_TIMESTAMP_PREFIX)]
    # Also strip trailing blank lines left behind
    result = '\n'.join(cleaned).rstrip()
    return result


def _wrap_system_reminder(text: str) -> str:
    """Wrap text in <system-reminder> tags.

    Claude Code wraps all mid-conversation system-level injections in these
    tags to distinguish them from user-authored content.  The model is trained
    to treat <system-reminder> content as authoritative system instructions.

    We use the same convention for dynamic injected context (project, skills,
    search addendum, swarm) so that:
      1. The model clearly distinguishes system instructions from user text.
      2. Compaction can identify and preserve system-reminder blocks.
      3. Context is consistent with Claude Code's convention.
    """
    return f'<system-reminder>\n{text}\n</system-reminder>'


def _append_to_system_message(messages, text, *, as_separate_block=False):
    """Append text to the first system message, or create one if absent.

    Helper used by _inject_system_contexts to avoid repeating the
    str-vs-list content detection pattern.

    Args:
        messages: The messages list (mutated in-place).
        text: The text to append.
        as_separate_block: If True and content is already a list,
            append as a separate text block (for cache segmentation).
            If content is a string, convert to list-of-blocks first.
    """
    if messages and messages[0].get('role') == 'system':
        sc = messages[0].get('content', '')
        if as_separate_block:
            # Force list-of-blocks format for cache segmentation
            if isinstance(sc, str):
                messages[0]['content'] = [
                    {'type': 'text', 'text': sc},
                    {'type': 'text', 'text': text},
                ]
            elif isinstance(sc, list):
                messages[0]['content'].append({'type': 'text', 'text': text})
            else:
                messages[0]['content'] = [{'type': 'text', 'text': text}]
        else:
            if isinstance(sc, str):
                messages[0]['content'] = sc + '\n\n' + text
            elif isinstance(sc, list):
                # Merge into last text block to avoid block proliferation
                if sc and isinstance(sc[-1], dict) and sc[-1].get('type') == 'text':
                    sc[-1] = {**sc[-1], 'text': sc[-1]['text'] + '\n\n' + text}
                else:
                    messages[0]['content'].append({'type': 'text', 'text': text})
    else:
        messages.insert(0, {'role': 'system', 'content': text.strip()})


def _prepend_to_system_message(messages, text):
    """Prepend text before the first system message content, or create one.

    Used for project context which should appear *before* the user's system prompt.
    """
    if messages and messages[0].get('role') == 'system':
        sc = messages[0].get('content', '')
        if isinstance(sc, str):
            messages[0]['content'] = text + '\n\n' + sc
        elif isinstance(sc, list):
            messages[0]['content'] = [{'type': 'text', 'text': text}] + sc
    else:
        messages.insert(0, {'role': 'system', 'content': text})


def _inject_system_contexts(messages, project_path, project_enabled,
                             skills_enabled, search_enabled, swarm_enabled,
                             has_real_tools, conv_id: str = '',
                             task: dict = None):
    """Inject project, swarm, and static contexts into the system message.

    Modifies the messages list directly. Contexts are layered onto the system
    message in a consistent order optimized for **cache stability**:

      1. Project context (CLAUDE.md, file tree) — prepended, changes on file edits
      2. Static guidance (FRC, tool usage, output) — SEPARATE BLOCK, never changes
      3. Compact skill instructions — static, ~400 chars
      4. Swarm prompt — static when swarm is enabled
      5. Session memory — changes across turns (least cacheable)

    **Skills listing** (the `<available_skills>` XML index) is NO LONGER injected
    here.  It is injected into the **last user message** by
    ``inject_skills_to_user()`` in the orchestrator's main loop.  This prevents
    skill CRUD operations from breaking the prompt cache.

    IMPORTANT: Context is ALWAYS injected into the system message.  Each task
    receives fresh messages from the frontend (which only has the user's custom
    system prompt — no project/skills context).  Delta tracking is used solely
    to skip expensive FUSE I/O when the context hasn't changed since the last
    task in this conversation — the *text* is still injected from cache.

    Memory prefetch support: if ``task`` is provided and contains
    ``_prefetch_project`` / ``_prefetch_skills`` futures, their already-
    completed results are consumed instead of re-computing (saving FUSE I/O
    latency).  Inspired by Claude Code's ``startRelevantMemoryPrefetch()``.
    """
    _cid = conv_id or ''

    # ── Helper: try to get prefetched result, else compute synchronously ──
    def _get_prefetched(key, fallback_fn):
        """Get result from prefetch future if available, else call fallback."""
        if task and task.get(key):
            future = task[key]
            if future.done():
                try:
                    result = future.result(timeout=0)
                    logger.debug('[MemPrefetch] Using prefetched %s', key)
                    return result
                except Exception as e:
                    logger.debug('[MemPrefetch] %s failed, falling back: %s',
                                 key, e)
            else:
                logger.debug('[MemPrefetch] %s not done yet, falling back', key)
        return fallback_fn()

    # ★ 1. Project context injection (prepended — appears first after user system prompt)
    if project_enabled:
        def _load_project():
            from lib.project_mod import get_context_for_prompt
            return get_context_for_prompt(project_path)

        if _cid:
            # Delta path: use cache to skip FUSE I/O when content unchanged
            proj_ctx = _get_cached_or_compute(
                _cid, 'project',
                lambda: _get_prefetched('_prefetch_project', _load_project),
            )
        else:
            proj_ctx = _get_prefetched('_prefetch_project', _load_project)

        if proj_ctx:
            _prepend_to_system_message(messages, _wrap_system_reminder(proj_ctx))

    # ★ 2. Static guidance sections (SEPARATE BLOCK — never changes, maximizes cache prefix)
    #   Injected BEFORE any dynamic content so the cache-stable prefix is as
    #   long as possible: [user system prompt] → [project CLAUDE.md] → [static guidance]
    if has_real_tools:
        _static_guidance = '\n\n'.join([
            _FUNCTION_RESULT_CLEARING_SECTION,
            _SUMMARIZE_TOOL_RESULTS_SECTION,
            _TOOL_USAGE_GUIDANCE,
            _OUTPUT_EFFICIENCY_GUIDANCE,
        ])
        _append_to_system_message(messages, _static_guidance,
                                  as_separate_block=True)

    # ★ 3. Compact skill accumulation instructions (static, ~400 chars)
    #   Only the HOW-TO-USE instructions stay in the system message.
    #   The dynamic skills listing (<available_skills> index) is injected
    #   into the user message by inject_skills_to_user() — see orchestrator.
    if has_real_tools:
        from lib.skills import SKILL_ACCUMULATION_INSTRUCTIONS_COMPACT
        _append_to_system_message(
            messages,
            _wrap_system_reminder(SKILL_ACCUMULATION_INSTRUCTIONS_COMPACT))

    # ★ 4. Swarm system prompt injection
    if swarm_enabled and project_enabled:
        swarm_prompt = """
<parallel_execution>
You have a **parallel execution** system via `spawn_agents`. It dramatically speeds up complex tasks by running multiple sub-tasks simultaneously.

**ALWAYS use `spawn_agents` when:**
- A task has 2+ parts that can be worked on independently
- You need to research/analyze/modify multiple files or topics
- Any decomposition would speed things up vs doing everything sequentially

**Do NOT use it for:**
- Trivial single-step questions
- Tasks that are inherently sequential with no parallelizable parts

**How:**
Call `spawn_agents` with a list of sub-tasks. Each sub-task only needs an `objective` (what to do) and optional `context`. Don't overthink it — just split and ship.

```json
{"agents": [
  {"objective": "Find all usages of deprecated API X in lib/ and routes/", "context": "Looking for function calls like X.do_thing()"},
  {"objective": "Research the new API Y replacement patterns from the docs", "context": "See https://docs.example.com/migration"},
  {"objective": "Write unit tests for the migration in tests/test_migration.py", "context": "Test both old→new conversion and edge cases"}
]}
```

Sub-tasks run in parallel with full tool access. Results come back together. You then synthesize a final answer.
Use `depends_on: [0]` only when a task truly needs another's output (rare — prefer maximum parallelism).
</parallel_execution>
"""
        _append_to_system_message(messages, _wrap_system_reminder(swarm_prompt))

    # ★ Search addendum: timestamp + guidance now injected into the LAST
    #   user message (not system) to avoid cache-breaking every minute.
    #   See inject_search_addendum_to_user() called from orchestrator.

    # ★ 5. Session memory injection (most dynamic — injected LAST in system msg)
    #   Persistent session notes extracted by background thread (session_memory.py).
    #   Injected every turn so the model retains key decisions and working state
    #   across compactions.  Inspired by Claude Code's SessionMemory system.
    if _cid:
        try:
            from lib.tasks_pkg.session_memory import get_session_memory_for_prompt
            _session_mem = get_session_memory_for_prompt(_cid)
            if _session_mem:
                _append_to_system_message(
                    messages, _wrap_system_reminder(_session_mem))
        except Exception as e:
            logger.debug('[SystemCtx] Session memory injection failed: %s', e)


# ═══════════════════════════════════════════════════════════════════════════════
#  Skills-to-user-message injection
# ═══════════════════════════════════════════════════════════════════════════════

_SKILLS_MARKER = '<available_skills>'


def _extract_last_user_text(messages: list) -> str:
    """Extract the text content of the last user message for BM25 query."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        parts.append(block.get('text', ''))
                return ' '.join(parts)
    return ''


def _strip_old_skills_listing(text: str) -> str:
    """Remove a previously injected skills listing from text.

    Prevents accumulation across rounds — strips everything from
    <available_skills> to </available_skills> inclusive.
    """
    if _SKILLS_MARKER not in text:
        return text
    import re
    cleaned = re.sub(
        r'\n*<available_skills>.*?</available_skills>\n*',
        '\n',
        text,
        flags=re.DOTALL,
    )
    return cleaned.rstrip()


def inject_skills_to_user(messages: list, project_path: str = None,
                           project_enabled: bool = False,
                           skills_enabled: bool = False,
                           has_real_tools: bool = False,
                           conv_id: str = '',
                           task: dict = None):
    """Inject the skills listing into the last user message.

    Called AFTER all other user message modifications (planner replacement,
    critic messages, attachments, search addendum) so it always operates
    on the final user message content.

    Uses BM25 relevance filtering: extracts the user message text as a query,
    scores skills against it, and includes only the top-K most relevant ones.

    Dedup: strips any previously injected skills listing before re-injecting
    to prevent accumulation across tool rounds.

    Args:
        messages: The messages list (mutated in-place).
        project_path: Path to project for project-scoped skills.
        project_enabled: Whether project mode is active.
        skills_enabled: Whether skills are enabled in settings.
        has_real_tools: Whether the task has real tools (skills CRUD needs tools).
        conv_id: Conversation ID for cache scoping.
        task: Task dict (may contain prefetch futures).
    """
    if not skills_enabled and not has_real_tools:
        return

    _pp = project_path if project_enabled else None

    # Extract user message text as BM25 query BEFORE injecting skills
    query_text = _extract_last_user_text(messages)

    # ── Helper: try to get prefetched result, else compute synchronously ──
    def _get_prefetched_skills():
        if task and task.get('_prefetch_skills'):
            future = task['_prefetch_skills']
            if future.done():
                try:
                    return future.result(timeout=0)
                except Exception as e:
                    logger.debug('[SkillsToUser] Prefetch failed, computing: %s', e)
        return None

    # Build skills context with BM25 filtering
    def _load_skills():
        from lib.skills import build_skills_context
        return build_skills_context(project_path=_pp, query=query_text)

    # Try prefetch first (but prefetch doesn't have BM25 filtering)
    # If prefetch available, use it directly (it covers all skills);
    # BM25 filtering happens inside build_skills_context when query is set.
    _cid = conv_id or ''
    if _cid:
        skills_ctx = _get_cached_or_compute(
            _cid, 'skills_user', _load_skills)
    else:
        skills_ctx = _load_skills() or ''

    if not skills_ctx:
        return

    # Find last user message and inject (with dedup)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            if isinstance(content, str):
                content = _strip_old_skills_listing(content)
                messages[i]['content'] = content + '\n\n' + skills_ctx
            elif isinstance(content, list):
                # Remove old skills listing text blocks
                messages[i]['content'] = [
                    b for b in messages[i]['content']
                    if not (isinstance(b, dict) and b.get('type') == 'text'
                            and _SKILLS_MARKER in b.get('text', ''))
                ]
                messages[i]['content'].append({
                    'type': 'text',
                    'text': '\n\n' + skills_ctx,
                })
            return
    logger.debug('[SkillsToUser] No user message found to inject into')

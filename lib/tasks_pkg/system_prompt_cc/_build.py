"""Static-prompt assembler + user-context reminder builder.

``build_static_blocks`` is the single source of block order/identity;
``build_static_prompt`` joins the (optionally filtered) result into one
cache-stable text block. ``build_user_context_reminder`` builds the
prepended CLAUDE.md user-message body (ports prependUserContext).
"""
from __future__ import annotations

from lib.log import get_logger

from lib.tasks_pkg.system_prompt_cc._sections import (
    section_actions,
    section_doing_tasks,
    section_function_result_clearing,
    section_intro,
    section_output_efficiency,
    section_summarize_tool_results,
    section_system,
    section_system_reminders,
    section_tone_and_style,
    section_using_tools,
)
from lib.tasks_pkg.system_prompt_cc._environment import (
    section_current_date,
    section_environment,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Assembler — returns the full static block, joined with "\n\n"
# ═══════════════════════════════════════════════════════════════════════════════

# ── Block registry ──
# Stable IDs for each toggleable static-prompt block. The editor stores
# which IDs the user has switched OFF (keyed on ID, never on rendered text —
# text changes between releases, IDs don't). ``intro`` is deliberately NOT
# listed as user-toggleable in the UI because it carries the
# ``_CC_STATIC_MARKER`` idempotency probe, but the backend will honour a
# disable for any ID if asked.
#
# ``BLOCK_META`` drives the editor: id → (human title, whether the block's
# text is dynamic/read-only). Dynamic blocks (environment, current_date) are
# generated per-request and can only be toggled, not edited.
BLOCK_META: dict[str, dict] = {
    'intro': {'title': 'Intro & identity', 'dynamic': False, 'lockable': True},
    'system': {'title': '# System', 'dynamic': False, 'lockable': False},
    'doing_tasks': {'title': '# Doing tasks', 'dynamic': False, 'lockable': False},
    'actions': {'title': '# Executing actions with care',
                'dynamic': False, 'lockable': False},
    'using_tools': {'title': '# Using your tools',
                    'dynamic': False, 'lockable': False},
    'tone_and_style': {'title': '# Tone and style',
                       'dynamic': False, 'lockable': False},
    'output_efficiency': {'title': '# Output efficiency',
                          'dynamic': False, 'lockable': False},
    'function_result_clearing': {'title': '# Function Result Clearing',
                                 'dynamic': False, 'lockable': False},
    'system_reminders': {'title': 'System-reminder semantics',
                         'dynamic': False, 'lockable': False},
    'environment': {'title': '# Environment',
                    'dynamic': True, 'lockable': False},
    'current_date': {'title': 'Current date',
                     'dynamic': True, 'lockable': False},
}


def build_static_blocks(*, cwd: str, is_git: bool, model: str,
                         extra_roots: list[str] | None = None,
                         has_real_tools: bool = True,
                         is_code_context: bool = True,
                         include_date: bool = True,
                         tool_names: set[str] | None = None,
                         ) -> list[dict]:
    """Build the static prompt as an ordered list of identified blocks.

    Each block is ``{'id': str, 'title': str, 'text': str, 'dynamic': bool}``.
    A block whose ``text`` is empty (because a mode gate suppressed it) is
    omitted from the returned list entirely — callers never see ghost blocks.

    This is the single source of block order/identity; ``build_static_prompt``
    joins the (optionally filtered) result. The editor renders this list with
    a per-block keep/drop toggle.

    Args mirror ``build_static_prompt``.
    """
    raw: list[tuple[str, str]] = [
        ('intro', section_intro(is_code_context=is_code_context)),
        ('system', section_system()),
        ('doing_tasks', section_doing_tasks(is_code_context=is_code_context)),
        ('actions', section_actions(is_code_context=is_code_context)),
    ]
    if has_real_tools:
        raw.append(('using_tools', section_using_tools(tool_names=tool_names)))
    _web_tools = bool(tool_names) and bool(
        {'web_search', 'fetch_url'} & set(tool_names))
    raw.append(('tone_and_style',
                section_tone_and_style(is_code_context=is_code_context,
                                       web_tools=_web_tools)))
    raw.append(('output_efficiency', section_output_efficiency()))
    if has_real_tools:
        raw.append(('function_result_clearing',
                    section_function_result_clearing()))
        raw.append(('system_reminders', section_system_reminders()))
        # NOTE: summarize-tool-results is folded into function_result_clearing
        # for toggling purposes — they're one conceptual unit.
        raw[-2] = ('function_result_clearing',
                   section_function_result_clearing() + "\n\n"
                   + section_summarize_tool_results())
    else:
        raw.append(('system_reminders', section_system_reminders()))
    raw.append(('environment',
                section_environment(cwd=cwd, is_git=is_git, model=model,
                                    extra_roots=extra_roots,
                                    has_real_tools=has_real_tools)))
    if include_date:
        raw.append(('current_date', section_current_date()))

    blocks: list[dict] = []
    for bid, text in raw:
        if not text:
            continue
        meta = BLOCK_META.get(bid, {})
        blocks.append({
            'id': bid,
            'title': meta.get('title', bid),
            'text': text,
            'dynamic': bool(meta.get('dynamic', False)),
        })
    return blocks


def build_static_prompt(*, cwd: str, is_git: bool, model: str,
                         extra_roots: list[str] | None = None,
                         has_real_tools: bool = True,
                         is_code_context: bool = True,
                         include_date: bool = True,
                         tool_names: set[str] | None = None,
                         disabled_blocks: set[str] | None = None) -> str:
    """Assemble the full Claude Code-style static prompt block.

    Sections are concatenated with blank lines between, matching Claude
    Code's ``getSystemPrompt`` return value.  The block is intended to
    live as a single text block in the system message so it can be
    annotated with a single ``cache_control`` breakpoint.

    Args:
        cwd:             Primary working directory (Tofu project path).
        is_git:          Whether ``cwd`` is inside a git repository.
        model:           Kept for caller back-compat; not rendered.
        extra_roots:     Multi-root workspace extras, or None.
        has_real_tools:  When False, skip the tool-related sections
                         (``# Using your tools``, FRC, summarize) and
                         omit Shell/OS-Version bullets from Environment.
        is_code_context: When False, drop the SWE-bench-shaped material
                         (code-hygiene bullets in ``# Doing tasks``,
                         git/CI examples in ``# Executing actions``,
                         file_path:line_number guidance). Default True
                         for back-compat with callers that don't pass it.
        include_date:    When False, omit the trailing ``Current date:``
                         line. Used by the Settings default-prompt preview
                         so the editor text doesn't bake in a stale date,
                         and by replace-mode injection which appends the
                         date as its own dynamic block.
        tool_names:      The set of tool names actually registered for this
                         turn. Passed to ``section_using_tools`` so the
                         "prefer the dedicated tool" bullets only name tools
                         that exist (e.g. ``write_file`` / ``grep_search``
                         are project-mode-only). ``None`` ships all bullets
                         (back-compat).
        disabled_blocks: Block IDs (see ``BLOCK_META``) the user has switched
                         OFF in the per-block editor. Those blocks are dropped
                         from the assembled prompt. ``None`` keeps every block.
    """
    disabled = disabled_blocks or set()
    blocks = build_static_blocks(
        cwd=cwd, is_git=is_git, model=model, extra_roots=extra_roots,
        has_real_tools=has_real_tools, is_code_context=is_code_context,
        include_date=include_date, tool_names=tool_names,
    )
    return "\n\n".join(b['text'] for b in blocks if b['id'] not in disabled)


# ═══════════════════════════════════════════════════════════════════════════════
#  User-context injection (ports prependUserContext)
# ═══════════════════════════════════════════════════════════════════════════════

def build_user_context_reminder(claude_md: str | None,
                                 current_date: str | None = None) -> str | None:
    """Build the Claude-Code-style <system-reminder> user-message body.

    Claude Code places CLAUDE.md in a prepended user message rather than
    the system prompt (see ``utils/api.ts:prependUserContext``).  A/B
    testing on Tofu confirmed this saves 18% cost / +49% cache hit
    (see ``.tofu/skills/claudemd-placement-ab-test-results.md``).

    Args:
        claude_md:     Rendered project-intelligence text (or None).
        current_date:  ISO date string, or None to skip.

    Returns:
        The reminder body (without role wrapper), or None if nothing to inject.
    """
    ctx = {}
    if claude_md:
        ctx['Project context'] = claude_md.strip()
    if current_date:
        ctx['Current date'] = f"Today's date is {current_date}."

    if not ctx:
        return None

    parts = ["<system-reminder>",
             "As you answer the user's questions, you can use the following context:"]
    for key, value in ctx.items():
        parts.append(f"# {key}\n{value}")
    parts.append("")
    parts.append(
        "IMPORTANT: this context may or may not be relevant to your tasks. "
        "You should not respond to this context unless it is highly "
        "relevant to your task."
    )
    parts.append("</system-reminder>")
    return "\n".join(parts)

"""LLM analysis prompt + UI-decoration constants.

Kept separate from the LLM-call logic so the prompt can be evolved
without churning the engine module.
"""

_ANALYSIS_SYSTEM = """\
You are a work journal assistant. Analyse the user's AI conversations and
produce a concise daily summary. Output a JSON OBJECT (not array) with THREE keys:

{
  "streams": [ ... work stream objects ... ],
  "tomorrow": [ ... todo objects ... ],
  "yesterday_done": [ "exact text of completed yesterday TODO 1", ... ]
}

═══ STREAMS ═══
Group related conversations into coherent work areas (5-15 clusters).

Each stream:
{
  "title": "specific title, max 20 chars",
  "summary": "ONE sentence: the key outcome or what happened today",
  "status": "done" | "in_progress" | "blocked",
  "conv_ids": ["exact conversation IDs from input"]
}

Status classification rules (BE PRECISE):
- "done" — the task was COMPLETED: code was deployed/merged, bug was fixed and verified,
  question was fully answered, investigation reached a clear conclusion, implementation
  was finished. A task counts as done if the conversation shows a successful outcome
  (e.g., "fixed", "deployed", "works now", "merged", test passing, problem solved).
- "in_progress" — work is actively ongoing but not yet finished: partial implementation,
  debugging still in progress, waiting for test results, iterating on a solution.
- "blocked" — cannot proceed due to an external dependency: waiting for someone else's
  input, API/service is down, permission needed, unclear requirements needing clarification.

Summary rules:
- ONE sentence only. Concise. Not "discussed X" but "fixed X" / "identified root cause of X".
- If blocked, the summary should say WHY it's blocked.
- Trivial quick Q&A can merge into one "零碎问答" stream.
- Default to "done" for short Q&A conversations that got a clear answer.
- Default to "done" if a fix/implementation was applied AND confirmed working.

═══ TOMORROW ═══
Synthesize 3-8 TODO items from ALL unfinished work across streams.
Each item is a JSON OBJECT with three keys:
{
  "text": "short actionable title (max 30 chars, specific)",
  "detail": "A concrete, actionable prompt (1-3 sentences) that can be sent directly to an AI assistant to start this task. Include specific file names, function names, error messages, module paths, or other context from today's conversations so the assistant can immediately understand the task.",
  "tools": ["search"]  // subset of: search, code, browser, fetch, project
}

Available tool names:
- "search"  — web search (for research, looking up docs/APIs/errors)
- "code"    — code execution (for running scripts, testing, data analysis)
- "browser" — browser automation (for web interaction, testing UIs)
- "fetch"   — URL fetching (for reading specific web pages / docs)
- "project" — code project co-pilot (for editing files, reading code, debugging)

Title rules:
Bad: "继续图像相关工作" → Good: "修复多轮图片回显"
Bad: "处理评测问题" → Good: "适配M17评测脚本"

Detail rules:
- Write as if you're giving a brief to an AI coding assistant
- Reference specific files, functions, error messages, or URLs from today's work
- Be concrete: "Fix the _build_transcript function in routes/daily_report.py that truncates CJK text" not "Fix the truncation bug"

Yesterday's unfinished items are automatically categorized as "未完成".
If an unfinished item is still relevant and worth continuing tomorrow,
you SHOULD re-add it to tomorrow.  But don't blindly copy all items —
only include ones that are genuinely actionable for the next day.
If everything is done today, return an empty array.

═══ YESTERDAY_DONE ═══
If the input includes a "YESTERDAY'S TODO STATUS" section:
- Review each ✗ item and check if today's work (visible in streams) addressed it.
- If a TODO was clearly worked on today, include its EXACT original text in
  the "yesterday_done" array (copy-paste the text after the ✗ marker).
- Be GENEROUS in matching: if any of today's streams relate to the same topic
  as a yesterday TODO (even partially), mark it as done. The goal is to
  automatically close resolved tasks rather than leave them lingering.
- A TODO counts as "done" if:
  • Today's conversation explicitly fixed / completed the thing described
  • Today's conversation investigated or made progress on the same issue
  • Today's conversation shows the feature/bug described in the TODO was addressed
- Only omit items where today had ZERO relevant activity.
- If no yesterday TODOs exist, return an empty array.

═══ RULES ═══
- All text in the SAME language as the user (Chinese → Chinese)
- Return ONLY the raw JSON object. No markdown fences, no explanation.
"""


# ── Tool mapping for quick_action generation ──────────────
_TODO_TOOL_DEFAULTS = {
    'searchMode': 'off',
    'fetchEnabled': True,  # always on
    'codeExecEnabled': False,
    'browserEnabled': False,
    'projectEnabled': False,
}
_TODO_TOOL_MAP = {
    'search':  {'searchMode': 'multi'},
    'code':    {'codeExecEnabled': True},
    'browser': {'browserEnabled': True},
    'fetch':   {},  # fetchEnabled always on, no override needed
    'project': {'projectEnabled': True},
}


_QUOTES = [
    "人生苦短，我用 AI。",
    "今天的你比昨天更会提问了 ✨",
    "每一次对话都是一次思维的升级。",
    "AI 是工具，你才是灵魂。",
    "效率 × 创造力 = 你 + Tofu 🧈",
    "你和 AI 的默契度又提升了 1 级。",
    "Knowledge is power, and you're charging up. ⚡",
    "Done is better than perfect.",
    "Ship it. 🚀",
]

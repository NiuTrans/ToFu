"""Tool-NAME repair (alias resolution) and hallucinated-tool classification.

Open models routinely emit a tool name borrowed from a *different* harness
(Claude Code, OpenAI function-calling demos, plain Unix) instead of Tofu's
canonical name. :func:`resolve_tool_name` maps a well-known wrong name to the
canonical tool the model obviously intended. When that fails, the call is a
*hallucination* — :func:`classify_tool_call` / :func:`suggest_tool_names`
build the actionable descriptor the dispatcher rejects on.
"""

from __future__ import annotations

import re
from typing import Any

from lib.log import get_logger

from lib.tool_input_repair._schema import _schemas

logger = get_logger(__name__)


# ══════════════════════════════════════════
#  Tool-NAME repair (alias resolution)
# ══════════════════════════════════════════
#
# Each such call previously hit a hard "Unknown tool" wall and wasted a full
# round (top offender in the log audit: ``read_file`` 73×/day, plus ``bash``,
# ``read_text``, ``write_files``, ``grep_file``, ``cat`` …).
#
# This map rewrites the well-known wrong names to the canonical Tofu tool the
# model obviously intended. Only 1:1, unambiguous synonyms belong here — never
# map to a tool whose argument schema differs in a way the model can't satisfy.
# Keys are matched case-insensitively (see :func:`resolve_tool_name`), so the
# Claude-Code CamelCase variants (``Read``/``Grep``/``Edit`` …) are covered by
# the same lowercase entries.
_TOOL_NAME_ALIASES: dict[str, str] = {
    # ── file reading ──
    'read_file': 'read_files',
    'read': 'read_files',
    'read_text': 'read_files',
    'read_text_file': 'read_files',
    'readfile': 'read_files',
    'cat': 'read_files',
    'open_file': 'read_files',
    'view_file': 'read_files',
    'view': 'read_files',
    # ── directory listing ──
    'ls': 'list_dir',
    'list_directory': 'list_dir',
    'list_files': 'list_dir',
    'listdir': 'list_dir',
    'dir': 'list_dir',
    # ── content search ──
    'grep': 'grep_search',
    'grep_file': 'grep_search',
    'search': 'grep_search',
    'search_text': 'grep_search',
    'ripgrep': 'grep_search',
    'rg': 'grep_search',
    # ── filename search ──
    'find': 'find_files',
    'find_file': 'find_files',
    'glob': 'find_files',
    'search_files': 'find_files',
    # ── writing ──
    'write': 'write_file',
    'writefile': 'write_file',
    'write_files': 'write_file',
    'create_file': 'write_file',
    'save_file': 'write_file',
    # ── editing ──
    'edit': 'apply_diff',
    'edit_file': 'apply_diff',
    'str_replace': 'apply_diff',
    'str_replace_editor': 'apply_diff',
    'search_replace': 'apply_diff',
    'replace': 'apply_diff',
    'edits': 'apply_diffs',
    'multiedit': 'apply_diffs',  # Claude Code's batch-edit tool
    'insert': 'insert_content',
    # ── shell ──
    'bash': 'run_command',
    'shell': 'run_command',
    'sh': 'run_command',
    'exec': 'run_command',
    'execute': 'run_command',
    'execute_command': 'run_command',
    'terminal': 'run_command',
    'command': 'run_command',
    # ── fetch / search ──
    'fetch': 'fetch_url',
    'fetch_page': 'fetch_url',
    'webfetch': 'fetch_url',  # Claude Code's web-fetch tool
    'browse': 'fetch_url',
    'open_url': 'fetch_url',
    'websearch': 'web_search',
    'google': 'web_search',
    # Pre-2026-08 browser namespace migration. The argument contract is
    # compatible and the target must exist in the current task catalog.
    'download_url_to_server': 'browser_download_url_to_server',
    # ── large tool-result continuation ──
    # ``read_artifact`` is also a real swarm artifact tool. Exact live names
    # always win above; this alias applies only in a catalog where that tool is
    # absent and the unambiguous continuation reader is available.
    'read_artifact': 'read_tool_artifact',
    # ── ask the user ──
    # Claude Code's native tool is ``AskUserQuestion`` (matched
    # case-insensitively). These only resolve when ``ask_human`` is in the
    # session's tool set (human-guidance enabled) — never invented.
    'askuserquestion': 'ask_human',
    'ask_user': 'ask_human',
    'ask_user_question': 'ask_human',
    'ask': 'ask_human',
}


def resolve_tool_name(name: str, known: set[str] | None = None) -> tuple[str, str | None]:
    """Map a possibly-wrong tool name to a canonical Tofu tool name.

    Resolution order (first match wins):

    1. **Exact** — ``name`` is already a real tool → returned untouched
       (``alias_kind=None``). This is the overwhelmingly common path and
       must stay byte-cheap.
    2. **Static alias** — ``name.lower()`` is in :data:`_TOOL_NAME_ALIASES`
       *and* the target is a known tool → rewrite (``alias_kind='alias'``).
    3. **Case-insensitive** — a single known tool equals ``name`` ignoring
       case (catches Claude-Code ``Read``/``Grep`` and stray capitalisation)
       → rewrite (``alias_kind='casefold'``).

    Args:
        name: The tool name the model emitted.
        known: Set of valid tool names for this session (exact + MCP +
            swarm + memory tools). When ``None``, falls back to the
            schema-indexed built-in tools. Passing the live registry set
            lets dynamically-registered tools (MCP, swarm) win the exact
            check so we never alias over a real tool.

    Returns:
        ``(resolved_name, alias_kind)``. ``alias_kind`` is ``None`` when no
        rewrite happened (exact match or no confident mapping), else the
        kind of rewrite applied (for telemetry / UI badge).
    """
    if not name or not isinstance(name, str):
        return name, None

    valid = known if known is not None else set(_schemas().keys())

    # 1. Already valid — do nothing (hot path).
    if name in valid:
        return name, None

    # Unified-edit migration: old Tofu names and familiar foreign-harness
    # names all converge on edit_file when that is the session's visible edit
    # surface. The structural repair pass then converts their legacy argument
    # shapes into ``edits=[{operation, anchor, content, ...}]``. In rollback
    # sessions edit_file is absent, so the ordinary legacy aliases below keep
    # resolving to apply_diff/apply_diffs exactly as before.
    if 'edit_file' in valid and name.lower() in {
        'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents',
        'edit', 'str_replace', 'str_replace_editor', 'search_replace',
        'replace', 'edits', 'multiedit', 'insert',
    }:
        return 'edit_file', 'alias'

    # 2. Static alias table (case-insensitive key), but only if the target
    #    actually exists in this session — never invent a tool.
    target = _TOOL_NAME_ALIASES.get(name.lower())
    if target and target in valid:
        return target, 'alias'

    # 3. Case-insensitive match against a real tool (e.g. 'Grep' → no static
    #    entry needed if 'grep_search' weren't aliased; 'Read_Files' → ...).
    low = name.lower()
    ci_matches = [t for t in valid if t.lower() == low]
    if len(ci_matches) == 1:
        return ci_matches[0], 'casefold'

    # No confident mapping — leave untouched so the caller surfaces an
    # honest "unknown tool" error the model can correct.
    return name, None


# ══════════════════════════════════════════
#  Hallucinated-tool classification (unified rejection)
# ══════════════════════════════════════════
#
# After alias resolution (:func:`resolve_tool_name`) fails to map a name to a
# real session tool, the call is a *hallucination*: the model invented a tool
# that does not exist in this session (e.g. ``search_web`` when only
# ``web_search`` is registered, or a tool from a different harness with no
# alias). This is the single classifier so the dispatcher can reject uniformly
# and the UI can style it distinctly.


def _name_similarity(a: str, b: str) -> float:
    """Cheap similarity in [0, 1] between two tool names (no deps).

    Combines a substring-containment boost with difflib's ratio so that
    ``search_web`` scores highly against ``web_search`` (shared tokens) and
    ``read`` against ``read_files`` (prefix). Pure-stdlib, total, never raises.
    """
    a_l, b_l = a.lower(), b.lower()
    if not a_l or not b_l:
        return 0.0
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, a_l, b_l).ratio()
    # Token-overlap boost: split on non-alphanumerics, compare the sets.
    a_tok = set(re.split(r'[^a-z0-9]+', a_l)) - {''}
    b_tok = set(re.split(r'[^a-z0-9]+', b_l)) - {''}
    if a_tok and b_tok:
        overlap = len(a_tok & b_tok) / len(a_tok | b_tok)
        ratio = max(ratio, 0.5 * ratio + 0.5 * overlap)
    if a_l in b_l or b_l in a_l:
        ratio = max(ratio, 0.85)
    return ratio


def suggest_tool_names(name: str, known: set[str], *, limit: int = 3,
                       threshold: float = 0.45) -> list[str]:
    """Return up to ``limit`` real tool names most similar to ``name``.

    Used to make a hallucinated-tool rejection actionable ("did you mean
    web_search?"). Only names scoring above ``threshold`` are returned, so a
    name with no plausible match yields ``[]`` rather than noise.
    """
    if not name or not known:
        return []
    scored = sorted(
        ((t, _name_similarity(name, t)) for t in known),
        key=lambda kv: kv[1], reverse=True,
    )
    return [t for t, s in scored[:limit] if s >= threshold]


def classify_tool_call(name: str, known: set[str]) -> dict[str, Any] | None:
    """Classify a tool name against the live session tool set.

    Call this AFTER :func:`resolve_tool_name` has already failed to alias the
    name to a real tool. ``known`` MUST be the live set of tools shipped to
    the model this turn (built-ins + MCP + swarm + memory + custom-env), so a
    legitimate dynamically-registered tool is never flagged.

    Returns:
        ``None`` when ``name`` is a real tool (no rejection). Otherwise a
        descriptor ``{kind:'hallucinated', attempted, suggestions}`` the
        dispatcher stamps onto the tool round and the frontend renders as a
        distinct "not a real tool" state.
    """
    if not name or not isinstance(name, str):
        return {'kind': 'hallucinated', 'attempted': str(name), 'suggestions': []}
    if name in known:
        return None
    return {
        'kind': 'hallucinated',
        'attempted': name,
        'suggestions': suggest_tool_names(name, known),
    }

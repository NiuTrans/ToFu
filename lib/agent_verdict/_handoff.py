"""lib/agent_verdict/_handoff.py — Sentinels, tool sets, and structured-token
parsers for the agent-loop decision logic.

Carries the small pieces that classify_verdict and the autopilot loop consume:

  * the state-changing ("deliverable") tool sets;
  * the virtual-user completion sentinel (``VU_DONE_SENTINEL``);
  * the replan kill-switch (``replan_enabled``);
  * the state-changing tool-round counter (``count_state_changing_rounds``);
  * the structured ``[PROGRESS: resolved=X remaining=Y]`` parser
    (``_PROGRESS_RE`` / ``parse_progress``) — used both by classify_verdict's
    backend-authoritative gate and by the diminishing-returns ledger.

Each regex lives WITH the function that uses it. Pure logic — imports only
the standard library and ``lib.log``.
"""

from __future__ import annotations

import os
import re

from lib.goal_runs.contract import GOAL_POLICY_DIRECTIVE
from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  State-changing ("deliverable") tools
# ══════════════════════════════════════════════════════════

# Calls to these tools are what we count as real work; everything else
# (list_dir, read_files, grep_search, find_files, web_search, fetch_url, …)
# is exploration.
#
# ``code_exec`` is deliberately NOT a member here: the Flow round counter
# special-cases it (a code_exec round carries a different toolName), so the
# membership test must NOT match it.  Callers that count from a flat list of
# tool names — and therefore have no special-casing — should use
# :data:`STATE_CHANGING_TOOLS_WITH_CODE_EXEC` instead.
STATE_CHANGING_TOOLS = frozenset({
    'write_file',
    'edit_file',
    'apply_diff',
    'apply_diffs',
    'insert_content',
    'insert_contents',
    'run_command',
    'generate_image',
})

# Same set plus ``code_exec`` — for callers (e.g. the orchestration engine's
# flat-tool-name snapshot) that do not special-case code_exec separately.
STATE_CHANGING_TOOLS_WITH_CODE_EXEC = STATE_CHANGING_TOOLS | {'code_exec'}


# ══════════════════════════════════════════════════════════
#  Autopilot virtual-user completion sentinel
# ══════════════════════════════════════════════════════════

# A virtual_user emits this verbatim when it judges the task finished.
# Used by autopilot's role prompt + done check, and by the engine's
# virtual_user verdict inversion.
VU_DONE_SENTINEL = '[VU: TASK_DONE]'


# ══════════════════════════════════════════════════════════
#  Virtual-user (project-owner driver) role prompt — SINGLE SOURCE
# ══════════════════════════════════════════════════════════
#
# THE one definition of the virtual-user persona.  Both consumers import it
# from here so they can never re-diverge:
#   * lib/tasks_pkg/autopilot._VU_ROLE_PROMPT — the LIVE standalone autopilot
#     loop (production path) embeds it in the VU directive turn.
#   * lib/swarm/registry.AGENT_ROLES['virtual_user']['system_prompt_suffix'] —
#     the FlowExecutor engine path injects it as the VU sub-agent's system
#     prompt.
# Before this constant lived here, the registry carried a hand-copied
# 3-sentence paraphrase (comment: "Mirrors ... _VU_ROLE_PROMPT") that had
# already drifted from the ~2000-char original — the exact "hand-copied,
# began to diverge" anti-pattern lib/agent_verdict exists to kill.  It
# interpolates VU_DONE_SENTINEL so the sentinel is defined in exactly one
# place too.  tests/test_vu_prompt_single_source.py pins that both consumers
# ARE this object (identity), so a future paraphrase cannot silently re-fork.
VU_ROLE_PROMPT = (
    'You are the PROJECT OWNER driving this task to completion. The '
    'assistant reports to YOU. Your job is not to answer the assistant '
    'or to be agreeable — it is to keep the work moving toward the '
    'objective and to refuse to declare victory until the objective is '
    'actually met.\n\n'
    'Trust nothing you have not checked. The assistant\'s self-report '
    '("done", "tests pass", "I created X") is a claim, not evidence.\n\n'
    f'Governing GoalRun policy: {GOAL_POLICY_DIRECTIVE}\n\n'
    'Before you reply, do this:\n'
    '1. VERIFY the assistant\'s most consequential claim using your '
    'tools. If it said tests pass, run or inspect them; if it said it '
    'created/edited a file, read_files it; if it claimed a behavior '
    'works, check it. You MUST verify any checkable claim that the '
    'objective depends on — do not skip this.\n'
    '2. ASSESS the gap between the real current state and the objective '
    'stated at the top of this turn.\n'
    '3. DECIDE the genuine next step toward that objective — NOT merely '
    'a response to whatever the assistant last said. If the assistant '
    'asked you a decision question, answer it from the objective\'s '
    'perspective. If it declared the task finished, hold it to the '
    'objective\'s acceptance criteria.\n'
    '4. THINK CREATIVELY. A good owner does more than grade the '
    'assistant\'s homework. Use what you learned while investigating to '
    'surface things the assistant has NOT considered: an edge case or '
    'failure mode it missed, a simpler or more robust approach, a '
    'hidden assumption worth challenging, a related part of the '
    'objective it has not touched yet, or a concrete improvement. When '
    'you have such an insight, lead with it — it is more valuable than '
    'a verification report.\n\n'
    '=== PROVENANCE (read carefully) ===\n'
    'Your tool calls and your private reasoning are NOT sent to the '
    'assistant — they are shown to the human watching, but the assistant '
    'only ever receives your final REPLY TEXT as its next user message. '
    'So investigate as deeply as you need (the cost is yours to spend), '
    'but distil the result: your reply must be a clean, self-contained '
    'instruction that stands on its own without the investigation behind '
    'it. Do not say "as I found above" or reference your tool output — '
    'state the conclusion and the next step directly.\n'
    '=== END PROVENANCE ===\n\n'
    'Decision rules:\n'
    '- For code / engineering tasks: demand the most robust long-term '
    'solution. Do not accept shortcuts that optimize for cost, '
    'implementation speed, or backward compatibility. Prefer fixing '
    'root causes over patches.\n'
    '- For open-ended discussion: use your own judgment, stay concrete, '
    'pick a direction instead of asking more questions.\n'
    '- If the objective is a SUBJECTIVE or one-shot question (advice, an '
    'explanation, an opinion, a recommendation) with NOTHING to verify '
    'with tools and NO further acceptance criteria, and the assistant '
    'has already answered it substantively and correctly, then the '
    'objective IS met: reply EXACTLY '
    f'{VU_DONE_SENTINEL} — do NOT invent a follow-up, do NOT ask the '
    'assistant what it meant, and do NOT role-swap into answering as an '
    'assistant would. A genuinely complete one-shot answer concludes the '
    'run; manufacturing more turns is the failure mode to avoid.\n'
    '- Stop ONLY when you have VERIFIED that the objective\'s acceptance '
    'criteria are genuinely met (the check actually ran, the file is '
    'actually correct, the behavior actually works) — not when the '
    'assistant says so. When (and only when) that is true, reply '
    f'EXACTLY: {VU_DONE_SENTINEL}\n'
    '- The runtime audits your completion claim before accepting it: '
    f'{VU_DONE_SENTINEL} with remaining>0 is rejected outright, and the '
    'first completion claim is CHALLENGED and sent back when you never '
    'used your own tools this run while the assistant changed real '
    'state, or when nothing was resolved and nothing was built anywhere '
    'in the run. A challenge is not a formality: re-issue the claim only '
    'with the verification evidence or the no-work justification the '
    'challenge demanded.\n'
    '- If the objective is NOT yet met, give the assistant the specific '
    'unmet criterion or the next concrete step. Do not emit '
    f'{VU_DONE_SENTINEL} while anything remains unresolved.\n'
    '- HUMAN GATE: when EVERY remaining item is something only the human '
    'can do — their credentials or one-key approval, a publish / deploy / '
    'restart you were explicitly told to leave to them, or an external '
    'system you cannot reach — then the agent-reachable objective is '
    'COMPLETE. First verify every agent-reachable claim (rule 1); if '
    'nothing agent-reachable remains, reply '
    f'{VU_DONE_SENTINEL} with one line naming the human-owned remainder '
    'instead of manufacturing assistant work (verification loops, '
    'redundant checks, "one more polish" turns). Dispatching the '
    'assistant into work only the human can finish burns compute and can '
    'wedge the whole run for hours (measured 2026-07-31: a VU that kept '
    'verifying after the wrap-up listed only human-gated items hung the '
    'run 2.5h inside one command). "The human COULD also do this" is NOT '
    'a human gate — they delegated the objective to you; the gate exists '
    'only when the human MUST be the one to act.\n'
    '- Never invent product requirements beyond the stated objective.\n'
    '- Reply in the first person as the owner, in the same language the '
    'assistant used. Be concise but cite the specific evidence you '
    'verified or the criterion you are holding the assistant to.\n'
    '- Output ONLY the reply text — no quotation marks, no role labels, '
    f'no preamble. The {VU_DONE_SENTINEL} sentinel must appear on its '
    'own when used.\n'
    '- END your reply with EXACTLY ONE progress line, on its own final '
    'line, in this exact form:\n'
    '  [PROGRESS: resolved=X remaining=Y]\n'
    '  where X = the number of the objective\'s acceptance criteria you '
    'have now VERIFIED as genuinely done (cumulative, counting from the '
    'start of the run), and Y = the number that remain unmet. Base X on '
    'what you actually checked this turn, not on the assistant\'s claims. '
    'This line is a machine signal that lets the run detect when it is '
    'churning without real progress — it must be present and accurate on '
    'every reply (including the one carrying '
    f'{VU_DONE_SENTINEL}, where Y should be 0).'
)


# ══════════════════════════════════════════════════════════
#  Replan kill-switch
# ══════════════════════════════════════════════════════════

def replan_enabled() -> bool:
    """Replan kill-switch: ``TOFU_FLOW_REPLAN=0`` disables CONTINUE_PLANNER.

    When disabled, a ``planner`` phase is downgraded to ``worker`` so the
    redesign can be hot-disabled without a code rollback.  Defaults to
    enabled (``'1'``).
    """
    return os.environ.get('TOFU_FLOW_REPLAN', '1').strip() != '0'


# ══════════════════════════════════════════════════════════
#  State-changing tool round counter
# ══════════════════════════════════════════════════════════

def count_state_changing_rounds(tool_rounds) -> tuple:
    """Count state-changing vs exploratory tool rounds in a single worker turn.

    Parameters
    ----------
    tool_rounds : list[dict] | None
        ``task['toolRounds']`` snapshot — each entry has ``toolName``.

    Returns
    -------
    (int, int, list[str])
        ``(state_changing_count, exploratory_count, state_changing_tool_names)``.
        ``state_changing_tool_names`` preserves order + duplicates so the
        deliverables snapshot can show "apply_diff×2, write_file".

    ``code_exec`` rounds (whose ``toolName`` differs — see executor.py) are
    treated as state-changing.
    """
    if not tool_rounds:
        return 0, 0, []

    state_changing_names: list[str] = []
    exploratory_count = 0

    for entry in tool_rounds:
        if not isinstance(entry, dict):
            continue
        name = entry.get('toolName') or entry.get('tool_name') or ''
        if name == 'code_exec':
            state_changing_names.append('code_exec')
            continue
        if name in STATE_CHANGING_TOOLS:
            state_changing_names.append(name)
        else:
            exploratory_count += 1

    return len(state_changing_names), exploratory_count, state_changing_names


# ══════════════════════════════════════════════════════════
#  Structured [PROGRESS: resolved=X remaining=Y] parser
# ══════════════════════════════════════════════════════════

_PROGRESS_RE = re.compile(
    r'\[PROGRESS:\s*resolved\s*=\s*(\d+)\s*(?:,|;|\s)\s*remaining\s*=\s*(\d+)\s*\]',
    re.IGNORECASE,
)


# ══════════════════════════════════════════════════════════
#  Machine-control token strip — SINGLE ENTRY POINT
# ══════════════════════════════════════════════════════════

# Every synthetic machine-control token a model's reply may carry MUST be
# registered here, and ``strip_machine_tokens`` is the ONLY predicate that
# removes such tokens before a reply is persisted into conversation history
# or fed back into model context.
#
# Why a registry instead of per-call-site stripping: the VU protocol had
# exactly one hardcoded strip (``[VU: TASK_DONE]`` in autopilot) and missed
# the second token (``[PROGRESS: ...]``) — 90 leaked lines across 52 convs,
# after which the model started authoring the signal itself ().
# A third control token added to the protocol but NOT to this list is that
# bug's next instance; tests/test_vu_machine_token_strip.py pins the
# registry contents so the omission fails loudly instead.
#
# The PROGRESS pattern is the ``_PROGRESS_RE`` object ITSELF (not a copy):
# the strip must match exactly what ``parse_progress`` matches, by
# construction.
_MACHINE_TOKEN_STRIP_PATTERNS = (
    ('vu_done_sentinel', re.compile(re.escape(VU_DONE_SENTINEL))),
    ('progress_line', _PROGRESS_RE),
)


def parse_progress(text: str):
    """Extract the structured ``[PROGRESS: resolved=X remaining=Y]`` line.

    Returns ``(resolved, remaining)`` as ints, or ``(None, None)`` when no
    parseable line is present (the guard then fails open — it cannot conclude
    no-progress without the hard signal).  Uses the LAST match if the VU
    emitted more than one.
    """
    last = None
    for m in _PROGRESS_RE.finditer(text or ''):
        last = m
    if last is None:
        return None, None
    try:
        return int(last.group(1)), int(last.group(2))
    except (ValueError, TypeError) as e:
        logger.debug('[Verdict] parse_progress: non-int PROGRESS values (%s) — '
                     'failing open', e)
        return None, None


def strip_machine_tokens(text: str, *, keep=()) -> str:
    """Remove every registered machine-control token from ``text``.

    Args:
        text: The model reply to clean.  Returned unchanged (byte-identical)
            when it carries no registered token, aside from outer-whitespace
            normalisation when a strip DID occur.
        keep: Optional iterable of registry labels to NOT strip on this call.
            The one legitimate user is the VU budget guard path:
            ``run_virtual_user`` strips the DONE sentinel early but KEEPS
            ``'progress_line'`` so ``_record_vu_turn_and_check_budget`` can
            still parse it; the persistence path strips everything.  Unknown
            labels raise ``ValueError`` — a typo'd keep must not silently
            strip less than the caller assumed.

    Returns the cleaned text (blank husk lines left by a stripped own-line
    token collapsed, outer whitespace stripped).  Safe on empty/None input.
    Idempotent.
    """
    if not text:
        return text
    keep_set = frozenset(keep)
    known = {label for label, _ in _MACHINE_TOKEN_STRIP_PATTERNS}
    unknown = keep_set - known
    if unknown:
        raise ValueError(
            f'unknown machine-token label(s) in keep=: {sorted(unknown)}')
    out = text
    changed = False
    for label, rx in _MACHINE_TOKEN_STRIP_PATTERNS:
        if label in keep_set:
            continue
        out, n = rx.subn('', out)
        changed = changed or n > 0
    if not changed:
        return text
    # A stripped own-line token leaves a blank husk — collapse 3+ newlines.
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()

"""CORPUS + GUARD TEMPLATE — NOT WIRED TO ANY PRODUCTION CODE.

⚠️  OWNERSHIP: the nudge criterion itself belongs to board epic
    ``pt_33ba079f5cea4841`` (held by conversation ms3ao89ctbsrbc, design in
    docs/INTENT_STALL_MEASUREMENT.md). A detector was prototyped here and
    then DELIBERATELY REVERTED — ``lib/agent_verdict/_announced.py``, its
    facade export and the ``_analyse.py`` call site are all gone. Nothing in
    this file imports production code today, so it is CURRENTLY INERT: it
    exists so whoever lands the real gate inherits the production fixtures
    and the both-directions NEUTER pattern instead of re-deriving them.

    Do NOT read a passing run of this file as "the gate is protected".
    Until the epic lands an implementation, there is no gate.

⚠️  KNOWN MISSING CASE — must be added before any gate ships. ``rejected``
    is at least TWO species and only one is retriable:
      (a) the hook refused a SPECIFIC FORM of an available tool (the R18
          sample below: `rm -rf "$w"` blocked, "re-issue with a narrower
          target" → rewriting works, a nudge is productive);
      (b) the tool is NOT IN THIS TURN'S TOOLSET at all ("X is not a real
          tool ... not in the list of tools available to you this turn",
          3 production samples) → retrying is guaranteed to be refused
          again, so every nudge in the budget burns on a dead end.
    The structural shape below matches (b) as well as (a). A must-not-fire
    case for (b) — suggested discriminator: refused tool name ∈ this turn's
    dispatched toolset — is NOT yet in this file. Species (b) is routed to
    the error-envelope epic ``pt_88791cb08cb2495c`` instead.

Guard intent (for whoever lands it): the refused-tool-then-silence gate must
fire on the real incident and NEVER on a legitimate stop.

Fixtures use the REAL production timing behind this gate, not values chosen
to pass. From ``ms34yw0k74o2lq`` (2026-07-27 21:25:27):

  * R17 — ``run_command`` REFUSED by a pre-execution hook (``status=rejected``)
  * R18 — prose only, ``finish_reason=stop``, ZERO tool calls
  * task settles ``done``; the conversation stops mid-work

MUST fire on exactly that. MUST NOT fire on any of the five other populations
that superficially resemble it.

Why the trigger is structural
-----------------------------
A phrasing trigger ("ends on an action intent") was measured over 7 days and
rejected: 26 of 53 hits were the model handing control BACK to the human
("Give me the go … and I'll execute", "你拍板范围，我就开做"). Firing there
overrides human-in-the-loop, which is worse than the bug. So the test below
pins the ASYMMETRY: prose alone never fires no matter how action-like, while
a refused prior tool fires regardless of how the sentence is worded.

Assertions are on the OUTCOME ("is this round allowed to be terminal?"), so
the guard survives a reasonable reimplementation.

NEUTER (both directions, patching the SHIPPED objects — never a hand-copied
duplicate):
  * ``test_neuter_structural_trigger`` — widen the trigger set to accept a
    SUCCESSFUL prior tool → the ordinary "prose after a good tool round"
    ending starts firing. Catches "gate too wide".
  * ``test_neuter_veto_list``          — drop the veto list → a hand-back
    after a refused tool starts firing. Catches "overrides the human".
"""

import pytest

# The detector does not exist yet — see the OWNERSHIP note above. Skip the
# whole module rather than erroring at collection: a red file on a shared
# tree is indistinguishable from a real regression, and this session's own
# JOURNAL entry warns against leaving misleading test signal behind.
# When pt_33ba079f5cea4841 lands ``detect_announced_inaction`` in
# lib/agent_verdict, these tests activate automatically with no edit here.
_announced = pytest.importorskip(
    'lib.agent_verdict._announced',
    reason='announced-inaction detector not implemented yet '
           '(board epic pt_33ba079f5cea4841)',
)
detect_announced_inaction = _announced.detect_announced_inaction

pytestmark = pytest.mark.unit


# ── Real samples ──

R18_PROSE = (
    'The hook blocked my `rm -rf "$w"` fallback (unexpanded variable). '
    'Let me use explicit paths only.'
)
HANDBACK = (
    "Give me the go (and answers to the 3 questions) and I'll execute the "
    'export + push + tag-retract in order, verifying each step.'
)
TRUNCATED_MID_WORD = "Now I'll rel"


def _incident(**over):
    """The real R17-rejected → R18-prose-stop timing."""
    kw = dict(content=R18_PROSE, finish_reason='stop', has_tool_calls=False,
              prior_tool_status='rejected', state_changing_count=0,
              aborted=False, task_error=None, stream_anomaly=False,
              nudge_count=0)
    kw.update(over)
    return kw


# ══════════════════════════════════════════════════════════
#  MUST fire
# ══════════════════════════════════════════════════════════

def test_refused_tool_then_silence_is_not_allowed_to_be_terminal():
    verdict = detect_announced_inaction(**_incident())
    assert verdict['fire'] is True, (
        'the round that produced the reported half-finished turn was treated '
        f'as a legitimate finish (reason={verdict["reason"]})')


@pytest.mark.parametrize('status', ['rejected', 'error', 'blocked', 'denied',
                                    'timeout', 'REJECTED', ' Error '])
def test_every_unfinished_prior_status_fires(status):
    """Refusal is reported under several spellings; all mean unfinished."""
    assert detect_announced_inaction(**_incident(
        prior_tool_status=status))['fire'] is True


def test_fires_regardless_of_wording():
    """The trigger is structural — a plain factual sentence still fires.

    Pins that we did NOT silently keep a phrasing gate: this text carries no
    action intent at all.
    """
    verdict = detect_announced_inaction(**_incident(
        content='The command was not permitted in this environment.'))
    assert verdict['fire'] is True
    assert verdict['intent_hint'] is False, (
        'fixture should carry no action wording, else it cannot prove the '
        'trigger is structural')


# ══════════════════════════════════════════════════════════
#  MUST NOT fire — each a DIFFERENT reason to stop
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize('label,over', [
    # THE regression this replaces: prose after a SUCCESSFUL tool round is
    # the normal way a task ends. 24 of 29 mid-work endings looked like this.
    ('prior_tool_done', {'prior_tool_status': 'done'}),
    ('no_prior_tool', {'prior_tool_status': None}),
    # Waiting on the human, even though a tool was refused first.
    ('handback', {'content': HANDBACK}),
    ('aborted', {'aborted': True}),
    ('task_error', {'finish_reason': 'error',
                    'task_error': {'kind': 'upstream_unavailable'}}),
    ('truncation', {'content': TRUNCATED_MID_WORD, 'stream_anomaly': True}),
    ('budget', {'nudge_count': 2}),
    ('did_work', {'has_tool_calls': True}),
    ('state_changed', {'state_changing_count': 1}),
    ('negated', {'content': "I'll stop here rather than retry without your ok."}),
    ('conditional', {'content': "If you approve the scope, I'll retry it."}),
])
def test_legitimate_stops_are_left_alone(label, over):
    verdict = detect_announced_inaction(**_incident(**over))
    assert verdict['fire'] is False, (
        f'{label}: gate fired on a legitimate stop — it would override the '
        'reason the turn ended')
    assert verdict['reason'], 'no-fire must still report WHY (log attribution)'


def test_action_wording_alone_never_fires():
    """The 49%-false-positive population: action-sounding prose, clean prior
    tool. Must stay silent — this is the whole reason the trigger moved."""
    verdict = detect_announced_inaction(**_incident(
        prior_tool_status='done',
        content='Let me verify the tree is clean and run the tests.'))
    assert verdict['fire'] is False
    assert verdict['intent_hint'] is True, (
        'wording should still be REPORTED for telemetry even when it cannot '
        'trigger')


def test_disabled_budget_turns_the_gate_off():
    assert detect_announced_inaction(**_incident(max_nudges=0))['fire'] is False


# ══════════════════════════════════════════════════════════
#  NEUTER — both directions must bite
# ══════════════════════════════════════════════════════════

def test_neuter_structural_trigger(monkeypatch):
    """Widen the SHIPPED trigger set to accept a successful prior tool →
    ordinary endings start firing (gate too wide)."""
    monkeypatch.setattr(_announced, 'PRIOR_ROUND_UNFINISHED_STATUSES',
                        frozenset({'rejected', 'error', 'blocked', 'denied',
                                   'timeout', 'done'}))
    assert detect_announced_inaction(**_incident(
        prior_tool_status='done',
        content='Let me verify the tree is clean.'))['fire'] is True, (
        'NEUTER did not bite: the structural trigger set is not what keeps '
        'the gate off ordinary endings')


def test_neuter_veto_list(monkeypatch):
    """Drop the SHIPPED veto list → a hand-back after a refused tool starts
    firing (gate overrides the human)."""
    monkeypatch.setattr(_announced, '_NEGATIVES', ())
    assert detect_announced_inaction(**_incident(content=HANDBACK))['fire'] is True, (
        'NEUTER did not bite: removing the veto list changed nothing, so the '
        'list is not what keeps the gate off hand-backs')


# ══════════════════════════════════════════════════════════
#  Sentence window
# ══════════════════════════════════════════════════════════

def test_veto_reads_the_final_sentence_not_a_char_window():
    """An earlier draft matched the trailing 160 CHARS and swallowed clauses
    from the previous sentence, turning hand-backs into fires."""
    verdict = detect_announced_inaction(**_incident(
        content=('The hook refused it. Tell me which scope you want and I '
                 'will re-issue it.')))
    assert verdict['fire'] is False, (
        'the closing sentence hands back to the human and must veto')

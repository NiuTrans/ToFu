"""tests/test_project_board_autonomy_rule.py — the autonomy-first block policy.

The Project Brain redesign has a UI half (surface everything awaiting the
human in one place) and a POLICY half: the count on that surface is only
low if agents stop parking work on humans in the first place. A rule in
CLAUDE.md cannot do that job — it is not in context at the moment
``project_board_block`` is being considered. The TOOL DESCRIPTION is.

So these tests pin the decision rule into the tool schema itself:

  • ask a human ONLY when the decision is irreversible, a matter of
    taste/policy, or unverifiable from inside the repo;
  • otherwise decide, take the more robust long-term option, and record it
    with ``project_charter_commit`` (which agents may self-commit);
  • uncertainty alone is explicitly NOT a reason to block.

They are deliberately assertions about MEANING-BEARING PHRASES, not exact
prose: a reworded description still passes as long as the rule survives.
What they catch is the rule being deleted or watered back down to "ask the
human when unsure" — which is the failure mode that produced the loop of
epics blocked for days on questions an agent could have answered.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _block_desc() -> str:
    from lib.tools.conversation import BOARD_BLOCK_TOOL
    return BOARD_BLOCK_TOOL['function']['description']


def test_block_tool_states_the_three_ask_a_human_categories():
    """The rule must enumerate WHEN asking is legitimate. Without the three
    categories the guidance degrades to a vibe, and 'I wasn't sure' creeps
    back in as sufficient grounds."""
    d = _block_desc().lower()
    assert 'irreversible' in d, 'category (a) missing'
    assert 'taste' in d and 'policy' in d, 'category (b) missing'
    assert 'unverifiable' in d, 'category (c) missing'


def test_block_tool_tells_the_agent_to_decide_otherwise():
    """The positive half of the rule: everything outside those categories is
    the agent's call. This is the part that actually reduces the count."""
    d = _block_desc().lower()
    assert 'decide yourself' in d, \
        'the tool must tell the agent to decide non-gated questions itself'
    assert 'project_charter_commit' in d, \
        'the tool must point at the durable, reversible way to RECORD that decision'
    assert 'robust' in d or 'long term' in d or 'long-term' in d, \
        'the tool must say WHICH option to take when deciding alone'


def test_block_tool_rejects_uncertainty_as_grounds():
    """The specific failure mode: an agent blocking because it felt unsure,
    rather than because a human is genuinely required."""
    d = _block_desc().lower()
    assert "wasn't sure" in d or 'not sure' in d or 'uncertain' in d, \
        'the tool must explicitly refuse uncertainty as a reason to block'


def test_block_tool_names_the_cost_of_blocking():
    """The rule only lands if the model knows blocking is EXPENSIVE — a
    workstream parked on someone who may not look for hours."""
    d = _block_desc().lower()
    assert 'expensive' in d or 'hours or days' in d, \
        'the tool must state the cost of parking work on a human'


def test_block_tool_still_documents_the_structured_question_path():
    """The autonomy rule must not have crowded out the mechanism: when a human
    IS required, the structured question is still the right closure (it is what
    the Needs-you surface renders answer controls for)."""
    d = _block_desc()
    assert 'question' in d and 'options' in d
    assert 'auto-retry' in d.lower() or 'auto-retrying' in d.lower(), \
        'the question path must still explain that it pauses auto-retry'


def test_block_tool_asks_for_one_word_answerable_questions():
    """A question that needs an essay is another way of stalling — the human
    defers it, and the epic is parked anyway."""
    d = _block_desc().lower()
    assert 'one word' in d or 'enumerated options' in d, \
        'the tool must require the question be cheap for a human to answer'


def test_propose_tool_no_longer_claims_a_human_must_commit():
    """Agents have self-committed charter decisions since the 2026-07-12
    de-gating. The propose description still said it 'records your proposal for
    a human to review and commit' — which actively taught the model to route
    decisions through a human queue that nothing waits on. That stale framing
    is exactly what made pending proposals the loudest signal on the collab
    bar despite blocking nothing."""
    from lib.tools.conversation import CHARTER_PROPOSE_TOOL
    d = CHARTER_PROPOSE_TOOL['function']['description']
    assert 'for a human to review and commit' not in d, \
        'stale human-gate framing must be gone'
    assert 'project_charter_commit' in d, \
        'propose must point at commit as the preferred path for real decisions'
    assert 'nothing waits on it' in d.lower() or 'work continues' in d.lower(), \
        'propose must say it does not block progress'


def test_block_result_text_suggests_continuing_other_work():
    """A blocked epic must not read as 'you are done for now'. The tool RESULT
    (what the model sees right after blocking) should redirect it to other
    work — otherwise one gated epic idles a whole conversation."""
    import inspect

    from lib.conversations import project_board
    src = inspect.getsource(project_board.execute_board_tool)
    assert 'this epic is parked, but YOU are not' in src or \
           'Pick up another open epic' in src, \
        'the block result must redirect the agent to other work'
    assert 'project_charter_commit instead' in src, \
        ('the bare-cooldown result must offer deciding-and-recording as the '
         'alternative to leaving the epic parked')

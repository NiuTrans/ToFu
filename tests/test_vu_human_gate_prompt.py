"""tests/test_vu_human_gate_prompt.py — the VU has a HUMAN GATE decision rule.

Incident (2026-07-31, conv ms8bx7089s3268, epic pt_841eb73c): the
assistant's wrap-up ended with exactly two items, BOTH human-gated
("twine upload needs your credential + one key", "server restart needs
you"). The turn was fr=stop. Six minutes later the VU spawned a
continuation anyway — its own thinking (persisted on the synthetic VU
message) shows WHY: the persona's rules said "Do not emit [VU: TASK_DONE]
while anything remains unresolved", with NO concept of a human gate.
Given unresolvable-by-the-assistant remainders and no rule for them, the
most obedient VU does the only legal thing: keep driving — verification
loops, redundant checks — and in this case it hung the run 2.5h inside a
single verification command (the zombie grep of the parent incident).

The fix adds a HUMAN GATE clause to the single-source persona
(``lib.agent_verdict.VU_ROLE_PROMPT``, consumed identically by the live
autopilot loop and the swarm VU): when every remaining item requires the
HUMAN to act, the agent-reachable objective is complete — verify the
agent-reachable claims first (anti-dodge), then DONE with one line naming
the human-owned remainder.

Pinned here: the clause's presence, the anti-dodge guard, the single-
source identity (both consumers), and an in-test NEUTER.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_vu_human_gate_prompt.py -v
"""

import pytest

pytestmark = pytest.mark.unit


def _prompt():
    from lib.agent_verdict import VU_ROLE_PROMPT
    return VU_ROLE_PROMPT


def test_human_gate_clause_present():
    p = _prompt().lower()
    assert 'human gate' in p, (
        'the persona must name the human-gate concept — without it the '
        '"never stop while anything remains" rule is totalitarian')
    assert 'agent-reachable' in p, (
        'the clause must be scoped to agent-reachable completion')
    assert 'must be the one to act' in p, (
        'the anti-dodge guard ("the human COULD also do this" is not a '
        'gate) must be present, or a lazy VU can offload its work')


def test_done_with_remainder_line_contract():
    from lib.agent_verdict import VU_DONE_SENTINEL
    p = _prompt()
    # The clause routes through the SAME sentinel (no new verdict class) and
    # demands the human-owned remainder be named in the reply.
    assert VU_DONE_SENTINEL in p
    assert 'human-owned remainder' in p.lower()


def test_both_consumers_get_the_clause():
    """Single-source: the live autopilot loop AND the swarm registry resolve
    to the SAME object — the clause reaches both VU paths by construction."""
    from lib.agent_verdict import VU_ROLE_PROMPT
    from lib.tasks_pkg.autopilot import _VU_ROLE_PROMPT
    from lib.swarm.registry import get_role_system_suffix
    assert _VU_ROLE_PROMPT is VU_ROLE_PROMPT
    assert get_role_system_suffix('virtual_user') is VU_ROLE_PROMPT
    assert 'human gate' in _VU_ROLE_PROMPT.lower()
    assert 'human gate' in get_role_system_suffix('virtual_user').lower()


def test_neuter_strip_clause_breaks_guards():
    """NEUTER: remove the clause BLOCK from a copy — every guard above must
    flip red on it, proving they are keyed on the clause, not ambient text."""
    p = _prompt()
    low = p.lower()
    guards = ('human gate', 'agent-reachable', 'must be the one to act')
    for g in guards:
        assert g in low, f'precondition: guard {g!r} missing from the prompt'
    # Slice the clause block out (from its heading to the next rule).
    start = low.index('- human gate')
    end = low.index('- never invent', start)
    neutered = (p[:start] + p[end:]).lower()
    for g in guards:
        assert g not in neutered, (
            f'NEUTER applied but guard {g!r} survives — the tests above are '
            'not keyed on the clause')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3 — roundNum unification guard (L7 drift).

TESTS-FIRST: RED on HEAD by design.

The stream-event contract in ``lib/agent_core/events.py`` labels a round index
with TWO different field names depending on the event family:

  * ``roundNum`` — TOOL_START / TOOL_PROGRESS / TOOL_RESULT / TOOL_DONE /
    CONTEXT_COMPACTED / timer-poll.
  * ``round``    — PHASE / DELTA_RESET / ROUND_USAGE / ROUND_COMMITTED /
    MESSAGES_SNAPSHOT / peer_inbox_inject / user_steer_inject.

The client then re-derives the index locally in every handler (and a tool round
is located by FOUR names: roundNum / round / llmRound / synthetic 9000000+len),
with NO single normalization point. Phase 3 unifies the wire contract to ONE
key (``roundNum``) so the reducer's ``locateRound`` has a single field to read.

This guard scans the EventSpec ``fields=`` dicts and asserts no round-bearing
spec still advertises a bare ``round`` key. RED now (the drift is real); GREEN
after §5 of docs/RENDER_CONTRACT_PHASE3_PLAN.md renames them to ``roundNum``.

  NEUTER (in-test): re-introducing a ``round`` field into the scanned set must
  re-flag → proves the scan is load-bearing, not vacuously green.

Pure static AST/text scan — no DB, no server. Standalone + pytest.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

EVENTS_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'lib', 'agent_core', 'events.py')

# The canonical round-index key Phase 3 converges on.
CANONICAL_KEY = 'roundNum'


def _fields_blocks(src: str):
    """Yield each ``fields={...}`` literal block text found in events.py.

    Crude but sufficient: EventSpec fields are single-line-ish dict literals in
    this file. We capture from ``fields={`` to the matching close brace.
    """
    out = []
    for m in re.finditer(r'fields=\{', src):
        i = m.end() - 1  # at the '{'
        depth = 0
        j = i
        while j < len(src):
            if src[j] == '{':
                depth += 1
            elif src[j] == '}':
                depth -= 1
                if depth == 0:
                    out.append(src[i:j + 1])
                    break
            j += 1
    return out


def _blocks_declaring_bare_round(blocks):
    """Return the fields-blocks that declare a bare ``'round':`` key (the drift)."""
    bad = []
    for b in blocks:
        # a dict key 'round' (not 'roundNum', not 'round_usage', etc.)
        if re.search(r"['\"]round['\"]\s*:", b):
            bad.append(b)
    return bad


def test_events_use_single_round_key():
    """No round-bearing EventSpec may advertise a bare ``round`` field — the
    contract must use ``roundNum`` everywhere. RED on HEAD (drift is real)."""
    with open(EVENTS_PY, encoding='utf-8') as f:
        src = f.read()
    blocks = _fields_blocks(src)
    assert blocks, 'scanner found no fields={} blocks — events.py shape changed, scan broken'

    bad = _blocks_declaring_bare_round(blocks)
    # Extract a readable label (the field dict's first ~60 chars) for the message.
    labels = [re.sub(r'\s+', ' ', b)[:80] for b in bad]
    assert not bad, (
        f'ROUND-KEY DRIFT: {len(bad)} EventSpec(s) still advertise a bare '
        f"'round' field instead of the canonical '{CANONICAL_KEY}'. Phase 3 §5 "
        f'must rename these to {CANONICAL_KEY} so the client reducer has ONE '
        f'index to normalize. Offending field blocks: {labels}')


def test_NC_reintroduced_round_field_is_flagged():
    """NEUTER: a synthetic fields-block carrying a bare ``round`` key MUST be
    caught by the scanner — proves the guard is load-bearing."""
    synthetic = ["{'round': 'round number', 'detail': 'x'}"]
    bad = _blocks_declaring_bare_round(synthetic)
    assert bad == synthetic, (
        'NEUTER FAILED: the scanner did not flag a bare round field — it would '
        'not catch a future re-introduction of the drift')
    # And the canonical form must NOT be flagged.
    ok = _blocks_declaring_bare_round(["{'roundNum': 'round index', 'toolName': 'x'}"])
    assert ok == [], 'the canonical roundNum form must not be flagged'


def test_round_boundary_specs_exist_and_use_roundNum():
    """RENDER_CONTRACT Phase 3 (last criterion): the orchestrator must emit
    EXPLICIT round boundaries — ROUND_START / ROUND_END — so the client keys
    round attribution off real boundaries instead of inferring from the first
    tool_start / llmRound grouping. Both must be registered EventSpecs whose
    `fields` declare the canonical `roundNum` (consistent with the §5 wire
    unification). TESTS-FIRST: RED until the specs land.
    """
    from lib.agent_core.events import EventType, get_event_spec
    for const in ('ROUND_START', 'ROUND_END'):
        assert hasattr(EventType, const), (
            f'EventType.{const} is missing — the round-boundary events are the '
            'last Phase 3 criterion (explicit round_start/round_end so the '
            'reducer stops inferring boundaries from the first tool_start).')
        wire = getattr(EventType, const)
        spec = get_event_spec(wire)
        assert spec is not None, f'{wire} has no registered EventSpec'
        assert 'roundNum' in spec.fields, (
            f'{wire} EventSpec must declare the canonical `roundNum` field '
            f'(got fields={sorted(spec.fields)})')
        # Must NOT reintroduce the retired bare `round` alias.
        assert 'round' not in spec.fields, (
            f'{wire} must use `roundNum`, not the retired bare `round` alias')


def _run(fn):
    try:
        fn(); print('  \033[32m✓\033[0m', fn.__name__); return True
    except AssertionError as e:
        print('  \033[31m✗\033[0m', f'{fn.__name__}: {e}'); return False


def main():
    print('\n\033[36m═══ Phase-3 roundNum unification guard (tests-first) ═══\033[0m\n')
    pos = _run(test_events_use_single_round_key)
    neu = _run(test_NC_reintroduced_round_field_is_flagged)
    print()
    print('\033[33m(test_events_use_single_round_key is RED on HEAD by design — '
          'the round/roundNum drift is real; GREEN after Phase 3 §5)\033[0m')
    print(f'\nNEUTER load-bearing: {"PASS" if neu else "FAIL"}; '
          f'unification guard currently: {"GREEN" if pos else "RED (expected)"}\n')


if __name__ == '__main__':
    main()

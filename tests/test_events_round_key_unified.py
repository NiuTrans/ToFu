#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3 — roundNum unification guard (L7 drift).

The stream-event contract in ``lib/agent_core/events.py`` once labelled a round
index with TWO different field names depending on the event family:

  * ``roundNum`` — TOOL_START / TOOL_PROGRESS / TOOL_RESULT / TOOL_DONE /
    CONTEXT_COMPACTED / timer-poll.
  * ``round``    — PHASE / DELTA_RESET / ROUND_USAGE / ROUND_COMMITTED /
    MESSAGES_SNAPSHOT / peer_inbox_inject / user_steer_inject.

The client then re-derived the index locally in every handler, with NO single
normalization point. Phase 3 unified the wire contract to ONE key
(``roundNum``) so the reducer's ``locateRound`` has a single field to read.
This guard keeps it unified.

★ WHY THIS GUARD WAS REWRITTEN (pt_174f89ef93ac41be)
----------------------------------------------------
The original scan was a REGEX over the raw ``fields={...}`` block text::

    if re.search(r"['\\"]round['\\"]\\s*:", b):   # ← matches keys AND values

A dict literal's text contains both its KEYS and its VALUE strings, and the
VALUES here are human documentation. So the guard flagged PHASE for this
perfectly correct entry::

    'detailArgs': '(optional) interpolation args for `detailKey` '
                  '(e.g. {"round": 3, "model": "claude-4"})',

— an EXAMPLE inside a description, not a wire field. Measured at the time of
the rewrite: across 54 EventSpecs there were ZERO real bare ``round`` keys,
while the regex reported exactly 1 offender, and that one offender was the
prose. The guard had been RED on clean HEAD, indicting a contract that was
already correct.

That is the same failure this project logged as charter #24 ("a negative
assertion must not be satisfiable by prose"), pointing the other way: prose
must not be able to FAIL a structural assertion either. A structural claim
about field NAMES must be evaluated against field NAMES.

So the scan now reads the KEYS:
  * primary source = the RUNTIME registry (``all_event_specs()``), because it
    is the only view where a ``**_TOOL_CLOCK_FIELDS`` spread is already
    resolved — 4 specs use one, and a naive AST key-walk would silently skip
    exactly those keys, i.e. go green by not looking;
  * plus an AST cross-check over the literal keys, so a spec that is somehow
    absent from the registry still cannot smuggle a bare ``round`` in.

TESTS-FIRST heritage: this file was originally RED by design while the drift
existed. The drift is gone; the guard now protects the unified state.

Pure static AST + registry scan — no DB, no server. Standalone + pytest.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

EVENTS_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'lib', 'agent_core', 'events.py')

# The canonical round-index key Phase 3 converges on.
CANONICAL_KEY = 'roundNum'

# The retired alias. A field NAMED this is drift; the word appearing inside a
# description string is documentation and must never fail this guard.
RETIRED_KEY = 'round'


def _spec_field_keys_from_ast(src: str):
    """Yield ``(event_type_label, [declared field keys])`` per EventSpec call.

    Reads only the KEYS of each ``fields={...}`` dict literal. A ``**spread``
    entry has no literal key, so it is reported as the sentinel ``'**'`` —
    callers must treat such a spec as "not fully covered here" and rely on the
    runtime registry for it, rather than silently assuming it is clean.
    """
    out = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, 'id', '') == 'EventSpec'):
            continue
        label = ast.unparse(node.args[0]) if node.args else '<unknown>'
        for kw in node.keywords:
            if kw.arg != 'fields' or not isinstance(kw.value, ast.Dict):
                continue
            keys = []
            for k in kw.value.keys:
                if k is None:
                    keys.append('**')          # spread — resolved at runtime
                elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.append(k.value)
            out.append((label, keys))
    return out


def test_events_use_single_round_key():
    """No EventSpec may declare a field literally NAMED ``round``.

    Asserted against the runtime registry (where ``**`` spreads are already
    resolved), so this cannot be satisfied by a spec whose keys live behind a
    spread — nor failed by a description that merely mentions the word.
    """
    from lib.agent_core.events import all_event_specs

    specs = all_event_specs()
    assert specs, 'the event registry is empty — the scan would be vacuous'

    bad = [s.type for s in specs if RETIRED_KEY in s.fields]
    assert not bad, (
        f'ROUND-KEY DRIFT: {len(bad)} EventSpec(s) declare a field named '
        f"'{RETIRED_KEY}' instead of the canonical '{CANONICAL_KEY}'. The "
        f'client reducer normalizes on ONE index key; a second name puts it '
        f'back to re-deriving the index per handler. Offenders: {bad}')


def test_ast_cross_check_agrees_with_the_registry():
    """Second, independent read of the same claim.

    The registry is the authority, but it only contains specs that were
    actually registered. This walks the SOURCE so a spec that is defined yet
    somehow absent from ``_SPECS`` still cannot introduce the retired key.
    Specs whose keys hide behind a ``**spread`` are explicitly deferred to the
    registry check rather than being counted as clean.
    """
    with open(EVENTS_PY, encoding='utf-8') as f:
        src = f.read()

    per_spec = _spec_field_keys_from_ast(src)
    assert per_spec, 'AST scan found no EventSpec fields= dicts — scan broken'

    offenders = [label for label, keys in per_spec if RETIRED_KEY in keys]
    assert not offenders, (
        f"AST cross-check: these EventSpec literals declare a bare "
        f"'{RETIRED_KEY}' field key: {offenders}")

    # Sanity: the scan must actually be seeing the canonical key somewhere,
    # otherwise a broken extractor would pass by finding nothing at all.
    assert any(CANONICAL_KEY in keys for _, keys in per_spec), (
        f"the AST scan found no '{CANONICAL_KEY}' key anywhere — the "
        'extractor is broken and this guard is vacuous')


def test_NC_reintroduced_round_field_is_flagged():
    """NEUTER: a spec that really declares ``round`` MUST be caught.

    Drives the SAME predicate the guard uses (membership in the resolved
    ``fields`` mapping) against a synthetic spec, so the guard is proven
    load-bearing without editing the shipped registry.
    """
    from lib.agent_core.events import EventCategory, EventSpec

    drifted = EventSpec('synthetic_drift', EventCategory.LIFECYCLE,
                        'synthetic', fields={'round': 'round number',
                                             'detail': 'x'})
    assert RETIRED_KEY in drifted.fields, (
        'NEUTER FAILED: the predicate did not flag a real bare round field — '
        'it would not catch a future re-introduction of the drift')

    canonical = EventSpec('synthetic_ok', EventCategory.LIFECYCLE, 'synthetic',
                          fields={CANONICAL_KEY: 'round index',
                                  'toolName': 'x'})
    assert RETIRED_KEY not in canonical.fields, (
        'the canonical roundNum form must not be flagged')


def test_NC_prose_mentioning_round_is_NOT_flagged():
    """★ REVERSE NEUTER — the defect this rewrite fixes.

    A field DESCRIPTION may legitimately contain the word ``round`` (PHASE's
    ``detailArgs`` documents its shape with the example
    ``{"round": 3, "model": "claude-4"}``). Documentation must never fail a
    structural guard about field NAMES.

    The retired text-regex flagged exactly this and left the suite RED on a
    contract that was already correct. Without this face, a future "fix" that
    reverts to scanning raw block text would look perfectly green.
    """
    from lib.agent_core.events import EventCategory, EventSpec

    documented = EventSpec(
        'synthetic_documented', EventCategory.LIFECYCLE, 'synthetic',
        fields={
            CANONICAL_KEY: 'round number',
            'detailArgs': '(optional) interpolation args for `detailKey` '
                          '(e.g. {"round": 3, "model": "claude-4"})',
        })
    assert RETIRED_KEY not in documented.fields, (
        'a description that MENTIONS "round" must not be read as declaring a '
        'field named "round" — that false positive is exactly what this '
        'rewrite removed')

    # And the real registry must contain such a documented spec, so this face
    # is anchored to production rather than to a fixture only.
    from lib.agent_core.events import get_event_spec
    phase = get_event_spec('phase')
    assert phase is not None, 'the PHASE spec vanished'
    assert RETIRED_KEY not in phase.fields, (
        'PHASE must not declare a bare round field')
    assert any('round' in str(v) for v in phase.fields.values()), (
        'PHASE no longer documents "round" anywhere in its descriptions — if '
        'that text was removed, this reverse guard has lost its anchor and '
        'should be re-pointed at whichever spec still carries such prose')


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
    print('\n\033[36m═══ Phase-3 roundNum unification guard ═══\033[0m\n')
    results = [
        _run(test_events_use_single_round_key),
        _run(test_ast_cross_check_agrees_with_the_registry),
        _run(test_NC_reintroduced_round_field_is_flagged),
        _run(test_NC_prose_mentioning_round_is_NOT_flagged),
        _run(test_round_boundary_specs_exist_and_use_roundNum),
    ]
    print()
    ok = all(results)
    print(f'\n{sum(results)}/{len(results)} '
          f'{"GREEN" if ok else "— RED"}\n')
    return 0 if ok else 1


if __name__ == '__main__':
    main()

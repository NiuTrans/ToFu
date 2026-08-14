"""Regression guards for the any-model tool_call wire-shape healer
(``lib.llm_sanitize._toolcalls._fix_tool_call_wire_shape``).

Root incident (2026-08-07, task 9a8196f3 round 4, conv msebjymx5b4a25):
kimi-k3 emitted a malformed tool call with ``id=''`` and
``function.name=''``. The parse layer skipped execution, minted an id and
fed back an error receipt — but left ``name=''`` on the wire dict. The
next round replayed it verbatim and Kimi hard-400'd the WHOLE request
with the misleading ``Invalid request: tokenization failed`` on BOTH
gateway keys (deterministic); the fallback chain rescued the task onto
qwen3.5-plus.

Live probe matrix against kimi-k3 (2026-08-07, max_tokens=1) — the
ground truth every rule below cites:

    B   name=''                      → 400 tokenization failed
    H   type missing                 → 400 tokenization failed
    K   arguments = dict             → 400 expected type string
    L   name key missing             → 400 name can't be blank
    M   tool_call_id=''              → 400 tool_call_id is not found
    N   name='unnamed_tool_call'     → 200 (the fix, pre-verified)
    E   arguments=''                 → 200 (untouched)
    F   arguments='123'              → 200 (untouched)
    G   arguments=invalid-json str   → 200 (untouched)
    I   name='antml:thinking'        → 200 (kimi tolerates; Anthropic's
                                        ^[a-zA-Z0-9_-]{1,64}$ does not →
                                        normalised)
    J   id=''                        → 200 (minted anyway — the orphan
                                        fixer pairs BY id)
    C   lone surrogate in content    → 200 (untouched)
"""

from __future__ import annotations

import json
import logging
import pathlib
import re

import pytest

from lib.llm_sanitize import (
    _UNNAMED_TOOL_NAME,
    _fix_orphaned_tool_calls,
    _fix_tool_call_wire_shape,
)
from lib.llm.body import build_body


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_PY = ROOT / 'lib' / 'llm' / 'body' / '_build.py'
PARSE_PY = ROOT / 'lib' / 'tasks_pkg' / 'tool_dispatch' / '_parse.py'

_STRICT_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def _assistant_tc(tc_id, name, arguments='{}', tc_type='function'):
    tc = {'type': tc_type, 'function': {'name': name, 'arguments': arguments}}
    if tc_id is not None:
        tc['id'] = tc_id
    return {'role': 'assistant', 'content': '', 'tool_calls': [tc]}


def _tool_msg(tc_id, content='result'):
    return {'role': 'tool', 'tool_call_id': tc_id, 'content': content}


# ────────────────────────────────────────────────────────────
#  name healing (probes B / L / I / N)
# ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_empty_name_becomes_placeholder():
    """Probe B: name='' is a Kimi hard-400 ("tokenization failed")."""
    msgs = [_assistant_tc('call_a', '')]
    out = _fix_tool_call_wire_shape(msgs)
    assert out[0]['tool_calls'][0]['function']['name'] == _UNNAMED_TOOL_NAME


@pytest.mark.unit
def test_missing_name_key_becomes_placeholder():
    """Probe L: name key missing → "name can't be blank"."""
    msg = {'role': 'assistant', 'content': '',
           'tool_calls': [{'id': 'call_a', 'type': 'function',
                           'function': {'arguments': '{}'}}]}
    out = _fix_tool_call_wire_shape([msg])
    assert out[0]['tool_calls'][0]['function']['name'] == _UNNAMED_TOOL_NAME


@pytest.mark.unit
def test_none_and_whitespace_names_become_placeholder():
    for bad in (None, '   '):
        msgs = [_assistant_tc('call_a', bad)]
        out = _fix_tool_call_wire_shape(msgs)
        assert out[0]['tool_calls'][0]['function']['name'] == _UNNAMED_TOOL_NAME


@pytest.mark.unit
def test_non_string_name_becomes_placeholder():
    msgs = [_assistant_tc('call_a', 42)]
    out = _fix_tool_call_wire_shape(msgs)
    assert out[0]['tool_calls'][0]['function']['name'] == _UNNAMED_TOOL_NAME


@pytest.mark.unit
def test_placeholder_matches_strictest_vendor_pattern():
    """Anthropic's ^[a-zA-Z0-9_-]{1,64}$ is the tightest contract — the
    placeholder itself must satisfy it (probe N verified kimi accepts it)."""
    assert _STRICT_NAME_RE.match(_UNNAMED_TOOL_NAME)


@pytest.mark.unit
def test_invalid_char_name_normalised_for_anthropic():
    """Probe I: kimi tolerates 'antml:thinking' but Anthropic rejects
    non [a-zA-Z0-9_-] names — normalise at the chokepoint."""
    msgs = [_assistant_tc('call_a', 'antml:thinking')]
    out = _fix_tool_call_wire_shape(msgs)
    name = out[0]['tool_calls'][0]['function']['name']
    assert name == 'antml_thinking'
    assert _STRICT_NAME_RE.match(name)


@pytest.mark.unit
def test_overlong_name_clamped_to_64():
    msgs = [_assistant_tc('call_a', 'a' * 80)]
    out = _fix_tool_call_wire_shape(msgs)
    name = out[0]['tool_calls'][0]['function']['name']
    assert len(name) == 64
    assert _STRICT_NAME_RE.match(name)


@pytest.mark.unit
def test_valid_name_untouched():
    msgs = [_assistant_tc('call_a', 'mcp__xuecheng__read_doc')]
    out = _fix_tool_call_wire_shape(msgs)
    assert (out[0]['tool_calls'][0]['function']['name']
            == 'mcp__xuecheng__read_doc')


# ────────────────────────────────────────────────────────────
#  type field (probe H)
# ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_missing_type_gets_function():
    """Probe H: missing ``type`` is a Kimi hard-400 ("tokenization failed")."""
    msg = {'role': 'assistant', 'content': '',
           'tool_calls': [{'id': 'call_a',
                           'function': {'name': 'run_command',
                                        'arguments': '{}'}}]}
    out = _fix_tool_call_wire_shape([msg])
    assert out[0]['tool_calls'][0]['type'] == 'function'


# ────────────────────────────────────────────────────────────
#  id minting / coercion (probe J + pairing contract)
# ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_empty_id_minted():
    msgs = [_assistant_tc('', 'run_command')]
    out = _fix_tool_call_wire_shape(msgs)
    new_id = out[0]['tool_calls'][0]['id']
    assert isinstance(new_id, str) and new_id.startswith('call_')
    assert len(new_id) == len('call_') + 12


@pytest.mark.unit
def test_missing_id_minted():
    msg = {'role': 'assistant', 'content': '',
           'tool_calls': [{'type': 'function',
                           'function': {'name': 'run_command',
                                        'arguments': '{}'}}]}
    out = _fix_tool_call_wire_shape([msg])
    assert out[0]['tool_calls'][0]['id'].startswith('call_')


@pytest.mark.unit
def test_mint_is_deterministic_across_build_body_runs():
    """Cache-parity pin (owner review 2026-08-07): _strip_non_api_fields
    deep-copies, so the heal never reaches the source messages — a random
    mint would give the same persisted id-less call a DIFFERENT wire id
    every round and break the prompt-cache prefix. Two build_body runs
    over the same id-less history must produce BYTE-IDENTICAL wires."""
    legacy = [
        {'role': 'user', 'content': 'hi'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': '', 'type': 'function',
             'function': {'name': '', 'arguments': '{}'}}]},
        _tool_msg('', 'x'),
    ]
    import copy as _copy
    b1 = build_body('kimi-k3', _copy.deepcopy(legacy))
    b2 = build_body('kimi-k3', _copy.deepcopy(legacy))
    assert (json.dumps(b1['messages'], sort_keys=True, ensure_ascii=False)
            == json.dumps(b2['messages'], sort_keys=True, ensure_ascii=False))


@pytest.mark.unit
def test_mint_derives_from_content_and_position():
    """Same id-less call at the same position → same id; at a different
    position → different id (position is in the hash input, so identical
    id-less twins in one message never collide)."""
    def minted_at(msg_idx):
        msgs = [{'role': 'user', 'content': 'hi'}] * msg_idx + [
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'type': 'function',
                 'function': {'name': 'run_command', 'arguments': '{}'}}]}]
        out = _fix_tool_call_wire_shape(msgs)
        return out[msg_idx]['tool_calls'][0]['id']

    assert minted_at(1) == minted_at(1)
    assert minted_at(1) != minted_at(2)


@pytest.mark.unit
def test_identical_idless_twins_get_distinct_ids():
    msg = {'role': 'assistant', 'content': '', 'tool_calls': [
        {'type': 'function', 'function': {'name': '', 'arguments': '{}'}},
        {'type': 'function', 'function': {'name': '', 'arguments': '{}'}},
    ]}
    out = _fix_tool_call_wire_shape([msg])
    ids = [tc['id'] for tc in out[0]['tool_calls']]
    assert ids[0] != ids[1]
    assert all(re.fullmatch(r'call_[0-9a-f]{12}', i) for i in ids)


@pytest.mark.unit
def test_non_string_id_coerced_on_both_sides():
    """int id on the assistant call + int tool_call_id on the receipt —
    coercing both to str keeps the pair matchable downstream."""
    msgs = [_assistant_tc(123, 'run_command'), _tool_msg(123)]
    out = _fix_tool_call_wire_shape(msgs)
    assert out[0]['tool_calls'][0]['id'] == '123'
    assert out[1]['tool_call_id'] == '123'


# ────────────────────────────────────────────────────────────
#  arguments (probes E / F / G / K)
# ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_dict_arguments_become_json_string():
    """Probe K: non-string arguments are a hard-400 (expected type string)."""
    msgs = [_assistant_tc('call_a', 'run_command', {'command': 'ls'})]
    out = _fix_tool_call_wire_shape(msgs)
    args = out[0]['tool_calls'][0]['function']['arguments']
    assert isinstance(args, str)
    assert json.loads(args) == {'command': 'ls'}


@pytest.mark.unit
def test_none_and_scalar_arguments_become_empty_object():
    for bad in (None, 123):
        msgs = [_assistant_tc('call_a', 'run_command', bad)]
        out = _fix_tool_call_wire_shape(msgs)
        assert out[0]['tool_calls'][0]['function']['arguments'] == '{}'


@pytest.mark.unit
def test_string_arguments_pass_through_untouched():
    """Probes E/F/G: '', scalar-json and even invalid-json STRINGS are
    accepted by the vendor — the healer must NOT rewrite them (the
    round-level sanitize_malformed_tool_call_args owns fresh-round
    evidence)."""
    for args in ('', '123', '{"city":', '{"command":"ls"}'):
        msgs = [_assistant_tc('call_a', 'run_command', args)]
        out = _fix_tool_call_wire_shape(msgs)
        assert out[0]['tool_calls'][0]['function']['arguments'] == args


# ────────────────────────────────────────────────────────────
#  structural garbage
# ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_non_dict_tool_call_entry_dropped():
    msg = {'role': 'assistant', 'content': '',
           'tool_calls': ['garbage',
                          {'id': 'call_a', 'type': 'function',
                           'function': {'name': 'run_command',
                                        'arguments': '{}'}}]}
    out = _fix_tool_call_wire_shape([msg])
    tcs = out[0]['tool_calls']
    assert len(tcs) == 1 and tcs[0]['id'] == 'call_a'


@pytest.mark.unit
def test_non_list_tool_calls_key_removed():
    msg = {'role': 'assistant', 'content': 'hi', 'tool_calls': 'garbage'}
    out = _fix_tool_call_wire_shape([msg])
    assert 'tool_calls' not in out[0]


@pytest.mark.unit
def test_all_garbage_entries_pop_the_key():
    msg = {'role': 'assistant', 'content': 'hi', 'tool_calls': ['x', None]}
    out = _fix_tool_call_wire_shape([msg])
    assert 'tool_calls' not in out[0]


# ────────────────────────────────────────────────────────────
#  tool message tool_call_id (probe M)
# ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_empty_tool_call_id_paired_positionally():
    """Receipts follow tool_calls in original order (pipeline contract) —
    an id-less receipt pairs with the next unclaimed call id."""
    msg = {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call_1', 'type': 'function',
         'function': {'name': 'a', 'arguments': '{}'}},
        {'id': 'call_2', 'type': 'function',
         'function': {'name': 'b', 'arguments': '{}'}},
    ]}
    msgs = [msg, _tool_msg('call_1', 'r1'), _tool_msg('', 'r2')]
    out = _fix_tool_call_wire_shape(msgs)
    assert out[1]['tool_call_id'] == 'call_1'
    assert out[2]['tool_call_id'] == 'call_2'


@pytest.mark.unit
def test_unpairable_tool_message_dropped():
    """Probe M: an id-less receipt with no preceding tool_call is
    protocol-dead — drop it (the orphan fixer only drops truthy ids)."""
    msgs = [{'role': 'user', 'content': 'hi'}, _tool_msg('', 'orphan')]
    out = _fix_tool_call_wire_shape(msgs)
    assert len(out) == 1
    assert out[0]['role'] == 'user'


@pytest.mark.unit
def test_valid_tool_call_id_untouched_and_claim_consumed():
    """A valid receipt id consumes its claim order-agnostically, so a
    later id-less receipt pairs with the REMAINING call."""
    msg = {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call_1', 'type': 'function',
         'function': {'name': 'a', 'arguments': '{}'}},
        {'id': 'call_2', 'type': 'function',
         'function': {'name': 'b', 'arguments': '{}'}},
    ]}
    msgs = [msg, _tool_msg('call_2', 'r2'), _tool_msg('', 'r1')]
    out = _fix_tool_call_wire_shape(msgs)
    assert out[1]['tool_call_id'] == 'call_2'
    assert out[2]['tool_call_id'] == 'call_1'


# ────────────────────────────────────────────────────────────
#  chain interplay: healer runs BEFORE the orphan fixer
# ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_minted_id_keeps_receipt_paired_through_orphan_fixer():
    """The exact incident shape: a call with empty id AND empty name plus
    its error receipt. After healer + orphan fixer the pair must SURVIVE
    (a minted-but-unpaired id would make the orphan fixer strip the call
    and drop the receipt — the model would lose the feedback)."""
    msgs = [
        {'role': 'user', 'content': 'hi'},
        _assistant_tc('', ''),
        _tool_msg('', '[SYSTEM: TOOL CALL DID NOT RUN]'),
    ]
    healed = _fix_tool_call_wire_shape(msgs)
    out = _fix_orphaned_tool_calls(healed)
    assert len(out) == 3
    tc = out[1]['tool_calls'][0]
    assert tc['function']['name'] == _UNNAMED_TOOL_NAME
    assert out[2]['tool_call_id'] == tc['id']


# ────────────────────────────────────────────────────────────
#  logging discipline (§2: every heal leaves a trace)
# ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_heal_is_logged_with_counts(caplog):
    msgs = [_assistant_tc('', '')]
    caplog.set_level(logging.WARNING)
    _fix_tool_call_wire_shape(msgs)
    text = ' '.join(rec.getMessage() for rec in caplog.records)
    assert 'Healed tool_call wire shape' in text
    assert 'name=1' in text and 'id=1' in text


@pytest.mark.unit
def test_clean_wire_is_silent(caplog):
    msgs = [
        _assistant_tc('call_a', 'run_command', '{"command":"ls"}'),
        _tool_msg('call_a', 'ok'),
    ]
    caplog.set_level(logging.WARNING)
    out = _fix_tool_call_wire_shape(msgs)
    assert not [r for r in caplog.records
                if 'Healed tool_call wire shape' in r.getMessage()]
    assert len(out) == 2


# ────────────────────────────────────────────────────────────
#  build_body integration — NEUTER-biting: deleting the call site
#  flips these RED
# ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_build_body_heals_the_incident_shape():
    """Byte-for-byte the R4 failure: assistant carrying one valid call +
    one empty-name call, both with receipts. After build_body the wire
    must satisfy every probe-verified rule."""
    msgs = [
        {'role': 'user', 'content': 'hi'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'call_ok', 'type': 'function',
             'function': {'name': 'run_command',
                          'arguments': '{"command":"ls"}'}},
            {'id': '', 'type': 'function',
             'function': {'name': '', 'arguments': '{}'}},
        ]},
        _tool_msg('call_ok', 'file1\nfile2'),
        _tool_msg('', '[SYSTEM: TOOL CALL DID NOT RUN]'),
    ]
    body = build_body('kimi-k3', msgs)
    wire = body['messages']
    assistant = next(m for m in wire if m.get('tool_calls'))
    for tc in assistant['tool_calls']:
        assert tc.get('id'), 'every tool_call must carry an id'
        assert tc.get('type') == 'function'
        name = tc['function']['name']
        assert _STRICT_NAME_RE.match(name), f'invalid wire name: {name!r}'
        assert isinstance(tc['function']['arguments'], str)
    receipts = [m for m in wire if m.get('role') == 'tool']
    assert len(receipts) == 2
    assert all(r.get('tool_call_id') for r in receipts)


@pytest.mark.unit
def test_build_py_call_order_guard():
    """The healer must run BEFORE _fix_orphaned_tool_calls (pairing is
    BY id — healing after would orphan freshly-minted calls). A stealth
    NEUTER that deletes or reorders the call flips this RED."""
    src = BUILD_PY.read_text()
    i_heal = src.index('_fix_tool_call_wire_shape(clean_messages)')
    i_orphan = src.index('_fix_orphaned_tool_calls(clean_messages)')
    assert i_heal < i_orphan, (
        '_fix_tool_call_wire_shape must run before _fix_orphaned_tool_calls')


@pytest.mark.unit
def test_parse_py_writes_back_placeholder_name():
    """Producer-side fix: the missing-name drop branch in parse_tool_calls
    must write the placeholder onto the shared wire dict (the same way it
    already writes back the minted id)."""
    src = PARSE_PY.read_text()
    code = '\n'.join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith('#'))
    assert "_fn_wire['name'] = _UNNAMED_TOOL_NAME" in code, (
        'parse_tool_calls must write the placeholder name back onto the '
        'wire dict when minting an id for a name-less call')
    assert 'from lib.llm_sanitize import _UNNAMED_TOOL_NAME' in src

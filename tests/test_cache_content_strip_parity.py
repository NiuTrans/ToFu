"""tests/test_cache_content_strip_parity.py — live-tail vs replay content .strip() parity.

The SECOND live→replay content asymmetry (the first was the str↔block wrap,
commit ab161bf). This one is a canonical-VISIBLE ``WIRE PREFIX CHANGED`` miss,
not a byte-only one — and WIRE PREFIX CHANGED is also a prefix-cache break
(post-restart it fired MORE often than WIRE BYTES DIVERGED).

The asymmetry (proven end-to-end):
  * ``parse_tool_calls`` (_parse.py) snapshots the pre-tool prose STRIPPED:
    ``_assistant_content = (assistant_msg.get('content') or '').strip()`` →
    persisted as ``round['assistantContent']`` → replayed as the assistant
    turn's ``content`` by ``_reconstruct_tool_call_messages`` (the
    frontend-summary rebuild path, used when the 2h/200-entry server-store
    cache misses).
  * the LIVE TAIL turn (orchestrator ``clean_msg``) sent the RAW, UN-stripped
    ``assistant_msg['content']`` (the server-store rebuild replays THIS verbatim).

So the SAME already-cached ``assistant/tool_call`` turn is sent with RAW content
one round (store path / live) and STRIPPED content another (reconstruct path
after store expiry) → the tokenized content prefix genuinely differs → the
cached prefix cannot extend past it → a full re-bill (``WIRE PREFIX CHANGED``).

FIX (freeze to ONE canonical form = stripped): the live tail ``clean_msg`` now
stamps ``content.strip()`` too, so every path — live tail, server-store replay,
and ``_reconstruct`` — emits byte-identical stripped content for the same turn.
Stripped is chosen because ``assistantContent`` is ALREADY persisted stripped
everywhere (_parse.py / segments / tool_history), and the content here is
inter-round narration where leading/trailing whitespace is not meaningful.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_cache_content_strip_parity.py -p no:cacheprovider
"""

import copy

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.conv_message_builder._toolcalls import (
    _reconstruct_tool_call_messages)
from lib.tasks_pkg.wire_fingerprint import canonical_messages, diff_canonical


def _live_tail_turn(assistant_msg):
    """Mirror the orchestrator's clean_msg construction at _run.py:1349-1351.

    Imported behaviourally (not the function) because it is inline in the
    round loop. Kept in lockstep with the source: role + tool_calls +
    content(.strip()) + reasoning + signature.
    """
    clean = {'role': 'assistant', 'tool_calls': assistant_msg['tool_calls']}
    # THE FIX under test: content is stripped to match the snapshot form.
    _c = (assistant_msg.get('content') or '').strip()
    if _c:
        clean['content'] = _c
    if assistant_msg.get('reasoning_content'):
        clean['reasoning_content'] = assistant_msg['reasoning_content']
    if assistant_msg.get('thinking_signature'):
        clean['thinking_signature'] = assistant_msg['thinking_signature']
    return clean


def _snapshot_assistant_content(assistant_msg):
    """Mirror _parse.py:240 — the persisted assistantContent form."""
    return (assistant_msg.get('content') or '').strip()


def _reconstruct_turn(assistant_msg):
    """The _reconstruct_tool_call_messages replay path for a one-round batch."""
    tc = assistant_msg['tool_calls'][0]
    rounds = [{'toolCallId': tc['id'], 'toolName': tc['function']['name'],
               'status': 'done', 'toolContent': 'ok',
               'toolArgs': tc['function']['arguments'],
               'assistantContent': _snapshot_assistant_content(assistant_msg),
               'llmRound': 0}]
    return _reconstruct_tool_call_messages(rounds)[0]


def _run_command_msg(prose):
    return {'role': 'assistant', 'content': prose,
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'run_command',
                                         'arguments': '{"command": "ls"}'}}]}


# ═══════════════════════════════════════════════════════════════════════════
#  THE FIX: live-tail content == reconstruct-replay content (both stripped)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('prose', [
    'Let me check the logs.\n\n',      # trailing newlines (most common)
    '   Reading the file now.',        # leading whitespace
    '\n\nChecking.\n',                 # both ends
    'No surrounding whitespace.',      # already-clean (must stay stable)
])
def test_live_tail_matches_reconstruct_content(prose):
    """The live-tail turn and the _reconstruct replay must emit byte-identical
    ``content`` for the same turn, whatever the surrounding whitespace — so an
    already-cached turn never flips content when the server-store expires and
    the conversation falls back to the reconstruct path."""
    src = _run_command_msg(prose)
    live = _live_tail_turn(copy.deepcopy(src))
    recon = _reconstruct_turn(copy.deepcopy(src))
    assert live.get('content') == recon.get('content'), (
        f'live-tail content {live.get("content")!r} != reconstruct content '
        f'{recon.get("content")!r} for prose {prose!r} — raw↔stripped flip '
        'breaks the cached prefix on server-store expiry.')
    # And canonical (which SEES whitespace) reports NO change — no WIRE PREFIX
    # CHANGED on this turn.
    assert diff_canonical(canonical_messages([live]),
                          canonical_messages([recon])) == [], (
        'canonical diff non-empty — the turn would fire WIRE PREFIX CHANGED')


def test_store_replay_matches_reconstruct_after_fix():
    """End-to-end: the server-store replay (raw clean_msg, now stripped) and
    the reconstruct replay (stripped assistantContent) agree, so BOTH
    production replay paths hit the same cached prefix."""
    src = _run_command_msg('Investigating the failure.\n')
    store_replay = _live_tail_turn(copy.deepcopy(src))   # what save/get_messages holds
    recon_replay = _reconstruct_turn(copy.deepcopy(src))
    assert store_replay.get('content') == recon_replay.get('content')


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER: prove the flip is real without the freeze (raw live tail)
# ═══════════════════════════════════════════════════════════════════════════

def test_nc_raw_live_tail_flips_against_stripped_reconstruct():
    """NEUTER / negative control: the PRE-FIX live tail (RAW content, no strip)
    diverges from the stripped reconstruct replay for the same turn, and
    canonical SEES it (WIRE PREFIX CHANGED). This proves the .strip() asymmetry
    is a real prefix break, and that the freeze (stripping the live tail) is
    load-bearing — not cosmetic."""
    src = _run_command_msg('Let me check the logs.\n\n')

    # PRE-FIX live tail: raw content, NO strip.
    raw_live = {'role': 'assistant', 'tool_calls': src['tool_calls']}
    if src.get('content'):
        raw_live['content'] = src['content']   # ← raw, the old behaviour

    recon = _reconstruct_turn(copy.deepcopy(src))

    assert raw_live.get('content') != recon.get('content'), (
        'expected raw-vs-stripped content divergence but found none — the '
        'NEUTER premise is gone')
    culprits = diff_canonical(canonical_messages([raw_live]),
                              canonical_messages([recon]))
    assert any(c.endswith('.content') for c in culprits), (
        f'expected a canonical .content culprit (WIRE PREFIX CHANGED) for the '
        f'raw↔stripped flip, got {culprits}')

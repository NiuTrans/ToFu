"""The intent-stall nudge's USER-VISIBLE contract (epic pt_5303eb3c7afb44a8).

The nudge machinery has three surfaces that face a human or the wire, and each
one is a place where a MACHINE signal can leak into something a person reads or
a model replays:

1. **``[END_TURN: …]`` must never reach the delivered answer.** ``NUDGE_TEXT``
   *teaches* the token, so the moment the nudge can fire, models start emitting
   it. It is the classifier's input, not prose.
2. **The nudge row must never reach the wire.** The nudge is appended as a
   ``role='user'`` message with no human author. Rendering it as a chip is
   right; replaying it as if the user said it is not.
3. **The phase label must be translatable.** Every other retry bucket ships an
   i18n key; a hardcoded string would render Chinese to an English user.

Each test below asserts the RESULT (what a reader/model would receive), never
the shape of the implementation that produces it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lib.tasks_pkg.segments import is_synthetic_inbox_round
from lib.tasks_pkg.stream_handler._intent_stall import (
    END_TURN_MARKER,
    NUDGE_TEXT,
    parse_end_turn_reason,
    strip_end_turn_marker,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent


# ── 1. The control token must not reach the reader ──

@pytest.mark.parametrize('body,expect', [
    ('Done — the migration is applied.\n\n[END_TURN: done]',
     'Done — the migration is applied.'),
    ('I need your call on this.\n[END_TURN: awaiting_human]',
     'I need your call on this.'),
    ('Blocked on credentials.\n\n\n[END_TURN: blocked]',
     'Blocked on credentials.'),
    # Mid-text token: the prose on BOTH sides has to survive.
    ('First part. [END_TURN: done] Second part.',
     'First part.  Second part.'),
])
def test_the_control_token_is_removed_from_the_answer(body, expect):
    assert strip_end_turn_marker(body) == expect


def test_an_invented_reason_is_also_stripped():
    """Display hygiene is TOTAL even though semantic trust is strict.

    ``parse_end_turn_reason`` deliberately refuses to trust a reason outside the
    closed set (so a model cannot invent one and silently suppress a nudge). The
    stripper must NOT inherit that strictness — otherwise a model that writes
    ``[END_TURN: banana]`` gets its typo published verbatim to the user.
    """
    text = 'All set.\n\n[END_TURN: banana]'
    assert parse_end_turn_reason(text) is None, 'invented reason must not be trusted'
    assert strip_end_turn_marker(text) == 'All set.', 'but it MUST still be hidden'


def test_ordinary_prose_is_returned_untouched():
    """The overwhelming majority of turns carry no marker at all.

    Asserted as object identity: the fast path must not rebuild the string, so a
    regression that starts running regexes over every answer shows up here.
    """
    body = 'Here is the summary you asked for.\n\nNo control tokens in sight.'
    assert strip_end_turn_marker(body) is body


def test_stripping_is_idempotent():
    once = strip_end_turn_marker('Done.\n\n[END_TURN: done]')
    assert strip_end_turn_marker(once) == once


def test_bracketed_prose_that_is_not_the_token_survives():
    """Only the control token goes — not anything that merely looks bracketed."""
    body = 'See [END_TURN_MARKER] in the docs, and [note: keep this].'
    assert strip_end_turn_marker(body) == body


def test_the_nudge_text_teaches_exactly_the_token_the_stripper_removes():
    """The teach-side and the hide-side must reference the SAME token.

    If ``NUDGE_TEXT`` ever taught a different spelling, models would emit a
    token the stripper does not recognise and it would reach users. This pins
    the pair together rather than trusting two hand-written literals.
    """
    taught = re.findall(r'\[END_TURN:[^\]]*\]', NUDGE_TEXT)
    assert taught, 'the nudge must teach the marker'
    for token in taught:
        assert strip_end_turn_marker(f'Answer.\n\n{token}') == 'Answer.', (
            f'nudge teaches {token!r} but the stripper leaves it in place'
        )


# ── 2. The nudge row must not reach the wire ──

def test_the_nudge_row_is_wire_excluded():
    """A stall-nudge row is display-only, like the other three inbox lanes.

    Driven through the REAL producer + the REAL frontend rehydrator (see
    ``tests/test_stall_nudge_lane_e2e.py``) rather than a hand-built dict: a
    fixture literal would assert only that the marker string appears in a
    constant, which stays green even when the lane has no producer at all —
    exactly how this marker shipped dead the first time.
    """
    from tests.test_stall_nudge_lane_e2e import (
        _rehydrate_via_shipped_js,
        _sidecar_from_real_producer,
    )
    row = _rehydrate_via_shipped_js(_sidecar_from_real_producer())[0]
    assert is_synthetic_inbox_round(row)


def test_a_real_tool_round_is_not_wire_excluded():
    """Complement: the exclusion must not swallow genuine tool rounds.

    Without this, marking EVERY round synthetic would satisfy the test above
    while silently emptying the replay projection.
    """
    assert not is_synthetic_inbox_round({
        'roundNum': 3, 'toolName': 'read_files',
        'toolCallId': 'call_abc', 'toolContent': 'File: x.py',
    })


# ── 3. The phase label must be translatable ──

def _i18n_has_key(key: str) -> dict | None:
    """Return the {zh, en} pair for *key* from the shipped dictionary."""
    out = {}
    for lang in ('zh', 'en'):
        catalog = json.loads(
            (_ROOT / f'frontend/src/i18n/locales/{lang}.json').read_text(
                encoding='utf-8'))
        if key in catalog:
            out[lang] = catalog[key]
    return out or None


def test_the_nudge_phase_label_is_translatable():
    """The backend emits `detailKey`, and the dictionary defines it in both langs.

    Both halves are asserted: a key with no dictionary entry renders as the raw
    key string, and a dictionary entry no backend emits is dead weight.
    """
    key = 'stream.phase.intentStallNudge'

    analyse = (_ROOT / 'lib/tasks_pkg/stream_handler/_analyse.py').read_text(
        encoding='utf-8')
    # Emitted via emit_phase (the typed chokepoint) → kwarg spelling.
    assert f"detailKey='{key}'" in analyse, (
        'the intent_stall_nudge phase event must carry the i18n detailKey'
    )

    pair = _i18n_has_key(key)
    assert pair is not None, f'{key} is emitted but missing from locale chunks'
    assert pair.get('zh'), f'{key} has no zh translation'
    assert pair.get('en'), f'{key} has no en translation'


def test_the_nudge_phase_detail_fallback_is_not_chinese_only():
    """`detail` is the headless/non-i18n fallback — it must be readable in English.

    A client that does not resolve `detailKey` renders `detail` verbatim, so a
    Chinese-only fallback is a broken label for every such consumer.
    """
    analyse = (_ROOT / 'lib/tasks_pkg/stream_handler/_analyse.py').read_text(
        encoding='utf-8')
    # Anchored on the typed Phase reference (emit_phase(Phase.INTENT_STALL_NUDGE,
    # …)) — the string literal no longer appears at the construction site.
    block = analyse.split('Phase.INTENT_STALL_NUDGE', 1)[1][:600]
    detail = re.search(r"detail\s*=\s*\(?\s*'((?:[^'\\]|\\.)*)'", block)
    assert detail, 'the nudge phase event must carry a `detail` fallback'
    assert not re.search(r'[\u4e00-\u9fff]', detail.group(1)), (
        'the `detail` fallback is rendered verbatim by non-i18n clients, so it '
        'must not be Chinese-only; put the localized copy in locale chunks instead'
    )


def test_the_marker_constant_is_the_single_spelling():
    """Nothing hand-writes the token; everything derives from END_TURN_MARKER."""
    assert END_TURN_MARKER == '[END_TURN:'
    assert END_TURN_MARKER in NUDGE_TEXT

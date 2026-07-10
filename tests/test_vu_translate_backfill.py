"""Tests for the untranslated-autopilot-VU-turn backfill collector.

``lib.conversations.vu_translate_backfill.collect_untranslated_vu_turns`` is the
shared pure predicate that the one-shot backfill
(``tests/_migrate_backfill_vu_translations.py``) uses to pre-filter which VU
turns still need auto-translating. The ACTUAL translation is delegated to the
production ``_maybe_auto_translate_vu`` (so the safety net's own gates apply);
this collector is only the "which rows qualify" selection.

A VU turn is stored ``role='user'`` + ``_isVirtualUser=True`` and is
DISPLAY-translated — it needs translating when it has non-empty ``content`` but
no ``translatedContent``.
"""

import os

import pytest

from lib.conversations.vu_translate_backfill import collect_untranslated_vu_turns
from tests._nc_harness import neutered_source

pytestmark = pytest.mark.unit

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'lib', 'conversations', 'vu_translate_backfill.py',
)


def _msgs():
    """A mixed conversation: normal user, assistant, a translated VU turn, and
    an UNtranslated VU turn (the one that must be collected)."""
    return [
        {'role': 'user', 'content': 'hello', '_msgId': 'u1'},
        {'role': 'assistant', 'content': 'hi there', '_msgId': 'a1'},
        {'role': 'user', '_isVirtualUser': True, 'content': 'VU one done',
         'translatedContent': '译文一', '_msgId': 'vu1'},           # already translated
        {'role': 'user', '_isVirtualUser': True, 'content': 'VU two pending',
         '_msgId': 'vu2'},                                          # ← the target
    ]


def test_collects_only_untranslated_vu_turns():
    hits = collect_untranslated_vu_turns(_msgs())
    assert len(hits) == 1, 'exactly the one untranslated VU turn qualifies'
    h = hits[0]
    assert h['idx'] == 3
    assert h['msgId'] == 'vu2'
    assert h['content'] == 'VU two pending'


def test_skips_translated_vu_and_non_vu():
    hits = collect_untranslated_vu_turns(_msgs())
    got_ids = {h['msgId'] for h in hits}
    assert 'vu1' not in got_ids, 'a VU turn WITH translatedContent must be skipped (idempotent)'
    assert 'u1' not in got_ids and 'a1' not in got_ids, 'normal user + assistant are not VU turns'


def test_empty_content_vu_ignored():
    msgs = [{'role': 'user', '_isVirtualUser': True, 'content': '', '_msgId': 'vuE'}]
    assert collect_untranslated_vu_turns(msgs) == []


def test_non_list_input_safe():
    assert collect_untranslated_vu_turns(None) == []
    assert collect_untranslated_vu_turns({'not': 'a list'}) == []


def test_migration_reuses_the_shared_collector():
    """The migration must bind the shared collector, not re-implement selection
    (the conv-OOM reuse invariant)."""
    mig = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       '_migrate_backfill_vu_translations.py')
    src = open(mig, encoding='utf-8').read()
    assert 'from lib.conversations.vu_translate_backfill import' in src
    assert 'collect_untranslated_vu_turns' in src
    # And it must delegate the actual translation to the live safety-net wire.
    assert '_maybe_auto_translate_vu' in src


def test_neuter_translated_guard_makes_it_reflag(monkeypatch):
    """NEUTER (in-memory, read-only): drop the ``translatedContent`` skip and an
    ALREADY-translated VU turn is wrongly re-collected — proving the skip is the
    load-bearing idempotency guard, not incidental."""
    # Baseline: the translated VU turn (vu1) is NOT collected.
    assert {h['msgId'] for h in collect_untranslated_vu_turns(_msgs())} == {'vu2'}

    with neutered_source(
        _SRC,
        "        tc = m.get('translatedContent') or ''\n        if tc:\n            continue  # already display-translated",
        "        tc = m.get('translatedContent') or ''\n        if False:\n            continue  # already display-translated",
    ) as mod:
        hits = mod.collect_untranslated_vu_turns(_msgs())

    got = {h['msgId'] for h in hits}
    assert 'vu1' in got, 'NEUTER: without the translatedContent skip, a translated VU turn re-flags (bites)'

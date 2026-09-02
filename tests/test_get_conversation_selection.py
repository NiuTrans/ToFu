"""get_conversation output shaping — honest selection, honest recovery.

Three defects this pins, all measured on real rows before the fix:

1. **Head-only truncation.** ``result[:MAX_CHARS]`` kept the OPENING of a
   conversation and dropped the end. On a 20-message row the model's view
   ended mid-word inside a tool round while ``build_conversation_digest`` —
   the HUMAN card, reading the same row — returned all 20 messages with the
   conclusion intact. The human view strictly dominated the model view, which
   is backwards: the reason to open a past conversation is usually to learn
   how it ENDED.

2. **A false recovery path.** The chain is get_conversation → L0 budgeting.
   conv_ref capped at MAX_CHARS=80 000 first, then L0 persisted THAT
   already-truncated text to disk and told the model "Full output saved
   to: <path>". Measured on one row: true raw record 15 700 103 chars →
   file on disk 84 210 bytes = **0.54%**. The model is handed a recovery
   instruction that cannot recover, and no way to detect it.

3. **raw=true emitted invalid JSON.** The dump was cut mid-token inside the
   ```json fence, so ``json.loads`` failed on every conversation tested —
   while the tool description promised "nothing summarized or truncated away".

The fix: select at the MESSAGE level (head + tail, reusing the digest's
existing anchoring), state the omission explicitly, keep raw parseable by
windowing BEFORE serialization, and never claim a fuller copy exists than
the one actually written.
"""

import json

import pytest

pytestmark = pytest.mark.unit


def _mk_messages(n, body='x'):
    """n alternating messages, each ~1 KB so a few hundred blow any budget.

    Sized deliberately: at n=400 the rendered transcript is ~400 KB, well past
    MAX_CHARS, so the truncation tests exercise the real path instead of
    passing vacuously on a conversation that never needed trimming.
    """
    out = []
    for i in range(n):
        role = 'user' if i % 2 == 0 else 'assistant'
        out.append({'role': role,
                    'content': f'MSG{i:04d} ' + (body * 1000),
                    '_msgId': f'm{i}'})
    return out


class _FakeRow(dict):
    """dict that also supports row['col'] access like the DB wrapper."""


def _install_fake_row(monkeypatch, messages, title='T'):
    from lib.conv_ref import _detail
    row = _FakeRow({
        'id': 'c1', 'user_id': 1, 'title': title,
        'messages': messages, 'created_at': 1, 'updated_at': 2,
        'settings': {}, 'msg_count': len(messages), 'rev': 3,
    })
    monkeypatch.setattr(
        _detail,
        '_read_conversation_snapshot',
        lambda conversation_id, *, user_id: row,
    )
    return row


class TestSelectionKeepsTheEnding:
    def test_long_conversation_keeps_the_last_message(self, monkeypatch):
        """The single most important property: the CONCLUSION must survive."""
        from lib.conv_ref._detail import get_conversation
        msgs = _mk_messages(400)
        _install_fake_row(monkeypatch, msgs)
        out = get_conversation('c1', user_id=1)
        assert 'MSG0399' in out, (
            'the final message was dropped — head-only truncation again')

    def test_long_conversation_also_keeps_the_opening(self, monkeypatch):
        from lib.conv_ref._detail import get_conversation
        _install_fake_row(monkeypatch, _mk_messages(400))
        out = get_conversation('c1', user_id=1)
        assert 'MSG0000' in out

    def test_omission_is_stated_not_silent(self, monkeypatch):
        """A gap the reader can't see is worse than a smaller window."""
        from lib.conv_ref._detail import get_conversation
        _install_fake_row(monkeypatch, _mk_messages(400))
        out = get_conversation('c1', user_id=1)
        low = out.lower()
        assert 'omitted' in low or 'skipped' in low

    def test_short_conversation_is_untouched(self, monkeypatch):
        from lib.conv_ref._detail import get_conversation
        _install_fake_row(monkeypatch, _mk_messages(6))
        out = get_conversation('c1', user_id=1)
        for i in range(6):
            assert f'MSG{i:04d}' in out
        assert 'omitted' not in out.lower()

    def test_selection_helper_is_shared_with_the_digest(self):
        """One anchoring implementation, not two that can drift apart."""
        from lib.conv_ref import _detail
        assert hasattr(_detail, '_select_message_window')


class TestNoFalseRecoveryPath:
    def test_no_claim_of_a_fuller_copy_that_does_not_exist(self, monkeypatch):
        """conv_ref must not hand off text it already truncated.

        Either it stays within its budget (so L0 never fires), or the text it
        emits is the complete record. What it must never do is emit a
        truncated blob that L0 then advertises as 'Full output saved'.
        """
        from lib.conv_ref._detail import get_conversation
        from lib.tasks_pkg.compaction.api import budget_tool_result
        _install_fake_row(monkeypatch, _mk_messages(400))
        out = get_conversation('c1', user_id=1)
        after_l0 = budget_tool_result('get_conversation', out)
        if 'Full output saved' in after_l0:
            import re
            m = re.search(r'Full output saved to: (\S+)', after_l0)
            assert m
            with open(m.group(1), encoding='utf-8') as f:
                on_disk = f.read()
            assert on_disk == out, (
                'the persisted file is not what get_conversation produced')
            assert not out.rstrip().endswith(
                'conversation has more content]'), (
                'L0 promises the full output while conv_ref already truncated '
                'it — the recovery path is a lie')

    def test_get_conversation_has_its_own_budget_entry(self):
        """One owner for the cap, so 80k/60k can't silently double-clip."""
        from lib.tasks_pkg.compaction._constants import TOOL_RESULT_MAX_CHARS
        assert 'get_conversation' in TOOL_RESULT_MAX_CHARS

    def test_char_level_fallback_is_head_and_tail(self, monkeypatch):
        """If a char clamp still fires, it must not be head-only."""
        from lib.conv_ref._detail import get_conversation
        # One message whose body alone blows any budget — message-level
        # selection cannot help, so the char path is what runs.
        huge = [{'role': 'user', 'content': 'HEADMARK ' + ('z' * 300000) + ' TAILMARK'}]
        _install_fake_row(monkeypatch, huge)
        out = get_conversation('c1', user_id=1)
        assert 'HEADMARK' in out
        assert 'TAILMARK' in out, (
            'char-level clamp dropped the tail — same head-only bug, one '
            'level down')


class TestRawStaysParseable:
    def test_raw_is_valid_json(self, monkeypatch):
        from lib.conv_ref._detail import get_conversation
        _install_fake_row(monkeypatch, _mk_messages(400))
        out = get_conversation('c1', raw=True, user_id=1)
        assert '```json' in out
        body = out.split('```json', 1)[1].rsplit('```', 1)[0]
        json.loads(body)  # must not raise

    def test_raw_small_conversation_is_complete(self, monkeypatch):
        from lib.conv_ref._detail import get_conversation
        msgs = _mk_messages(4)
        _install_fake_row(monkeypatch, msgs)
        out = get_conversation('c1', raw=True, user_id=1)
        body = out.split('```json', 1)[1].rsplit('```', 1)[0]
        rec = json.loads(body)
        assert len(rec['messages']) == 4
        assert rec.get('truncated') in (False, None)

    def test_raw_windowed_reports_what_it_dropped(self, monkeypatch):
        """A windowed raw read must SAY it is windowed, in-band."""
        from lib.conv_ref._detail import get_conversation
        _install_fake_row(monkeypatch, _mk_messages(400))
        out = get_conversation('c1', raw=True, user_id=1)
        body = out.split('```json', 1)[1].rsplit('```', 1)[0]
        rec = json.loads(body)
        assert rec.get('truncated') is True
        assert rec.get('messageCount') == 400
        assert len(rec['messages']) < 400

    def test_raw_is_bounded_even_for_one_giant_message(self, monkeypatch):
        """Dropping whole messages cannot shrink a single enormous one.

        A conversation of ONE 800 KB message would otherwise serialize to a
        800 KB raw payload — the context-flood the cap exists to prevent. The
        bound must hold while the JSON stays parseable.
        """
        from lib.conv_ref._detail import MAX_CHARS, get_conversation
        _install_fake_row(monkeypatch,
                          [{'role': 'user', 'content': 'q' * 800000}])
        out = get_conversation('c1', raw=True, user_id=1)
        assert len(out) <= MAX_CHARS * 1.1, (
            f'raw payload is {len(out):,} chars — unbounded')
        body = out.split('```json', 1)[1].rsplit('```', 1)[0]
        json.loads(body)  # still valid

    def test_raw_field_clamp_is_marked(self, monkeypatch):
        """A clamped field must say so, not silently look complete."""
        from lib.conv_ref._detail import get_conversation
        _install_fake_row(monkeypatch,
                          [{'role': 'user', 'content': 'q' * 800000}])
        out = get_conversation('c1', raw=True, user_id=1)
        body = out.split('```json', 1)[1].rsplit('```', 1)[0]
        rec = json.loads(body)
        assert rec.get('truncated') is True
        blob = json.dumps(rec, ensure_ascii=False)
        assert 'clamped' in blob or 'elided' in blob or 'truncated' in blob


class TestPaging:
    def test_accepts_a_window_and_a_cursor(self):
        import inspect
        from lib.conv_ref._detail import get_conversation
        p = inspect.signature(get_conversation).parameters
        assert 'limit' in p and 'before' in p

    def test_cursor_walks_backwards(self, monkeypatch):
        """Paging up must reach content the default window omitted.

        ``before`` is an EXCLUSIVE 1-based message number, so before=200 ends
        on message #199 — which carries the 0-based token MSG0198.
        """
        from lib.conv_ref._detail import get_conversation
        _install_fake_row(monkeypatch, _mk_messages(400))
        out = get_conversation(
            'c1', limit=10, before=200, user_id=1)
        assert 'MSG0198' in out, 'cursor did not land on the message before it'
        assert 'MSG0399' not in out, 'cursor window still shows the tail'

    def test_footer_tells_the_model_how_to_continue(self, monkeypatch):
        """Truncation without a next step is a dead end."""
        from lib.conv_ref._detail import get_conversation
        _install_fake_row(monkeypatch, _mk_messages(400))
        out = get_conversation('c1', user_id=1)
        assert 'before=' in out

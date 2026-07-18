"""Unit tests for lib.project_mod.ansi_normalize.AnsiNormalizer.

The normalizer turns a raw PTY byte stream (ANSI colors, cursor moves, and
carriage-return progress-bar redraws) into clean incremental text via a LINE
model: ``feed()`` returns ``(committed_delta, current_line)`` where the current
line is the tail being redrawn (``\r`` rewinds to col 0). This is the layer
that makes a tqdm/pip bar collapse to ONE evolving line instead of thousands of
stacked redraw frames in the output panel.
"""

import pytest

from lib.project_mod.ansi_normalize import AnsiNormalizer

pytestmark = pytest.mark.unit


def _full(norm, text):
    """Feed text and return the concatenation of committed + current."""
    committed, current = norm.feed(text)
    return committed, current


def test_plain_text_passthrough():
    n = AnsiNormalizer()
    committed, current = n.feed('hello world\n')
    assert committed == 'hello world\n'
    assert current == ''


def test_partial_line_no_newline_is_current():
    n = AnsiNormalizer()
    committed, current = n.feed('in progress')
    assert committed == ''
    assert current == 'in progress'
    # flush commits the dangling line
    assert n.flush() == 'in progress\n'


def test_carriage_return_collapses_progress_bar():
    """A redrawing bar (\\r rewind) must stay ONE current line, never stack."""
    n = AnsiNormalizer()
    _, cur = n.feed('Epoch: 12%|##        |')
    assert cur == 'Epoch: 12%|##        |'
    committed, cur = n.feed('\rEpoch: 84%|########  |')
    # No new committed lines — the bar overwrote itself in place.
    assert committed == ''
    assert cur == 'Epoch: 84%|########  |'
    # Final redraw with a newline commits exactly ONE clean line.
    committed, cur = n.feed('\rEpoch: 100%|##########|\n')
    assert committed == 'Epoch: 100%|##########|\n'
    assert cur == ''


def test_ansi_color_codes_stripped():
    n = AnsiNormalizer()
    committed, _ = n.feed('\x1b[31mred\x1b[0m text\n')
    assert committed == 'red text\n'


def test_cursor_move_sequences_stripped():
    n = AnsiNormalizer()
    # CSI cursor-forward / cursor-up should be dropped, text preserved.
    committed, _ = n.feed('a\x1b[2Cb\x1b[1Ac\n')
    assert 'a' in committed and 'b' in committed and 'c' in committed
    assert '\x1b' not in committed


def test_erase_line_from_cursor():
    """CSI K erases from the cursor to end of the current line."""
    n = AnsiNormalizer()
    n.feed('long stale text')
    # rewind, then erase-to-end, then write short text
    _, cur = n.feed('\r\x1b[Kok')
    assert cur == 'ok'


def test_split_escape_across_chunks():
    """An escape truncated at a chunk boundary must be reassembled, not shown."""
    n = AnsiNormalizer()
    committed, cur = n.feed('val=\x1b[3')   # incomplete CSI
    # the incomplete escape is stashed, not emitted as garbage
    assert '\x1b' not in cur
    assert cur == 'val='
    committed, cur = n.feed('1mGREEN\x1b[0m\n')  # completes ESC[31m
    assert committed == 'val=GREEN\n'


def test_backspace_edits_current_line():
    n = AnsiNormalizer()
    _, cur = n.feed('abcX')
    _, cur = n.feed('\b\bY')   # back over X and c, write Y → 'abY' ... wait: cursor after X is col4; \b\b→col2, write Y overwrites 'c'
    # after 'abcX' cursor col=4; two backspaces → col2; writing 'Y' overwrites index2 ('c') → 'abYX'
    assert cur == 'abYX'


def test_tab_expands_to_8col_stops():
    n = AnsiNormalizer()
    _, cur = n.feed('ab\tc')
    # 'ab' → col2; tab → col8 (6 spaces); 'c' at col8
    assert cur == 'ab' + ' ' * 6 + 'c'


def test_multiple_committed_lines_in_one_feed():
    n = AnsiNormalizer()
    committed, cur = n.feed('line1\nline2\nline3')
    assert committed == 'line1\nline2\n'
    assert cur == 'line3'


def test_flush_idempotent_when_empty():
    n = AnsiNormalizer()
    n.feed('x\n')
    assert n.flush() == ''   # nothing dangling
    assert n.flush() == ''

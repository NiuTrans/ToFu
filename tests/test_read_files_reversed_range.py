"""read_files must repair a REVERSED line range, and only that.

Motivation (measured 2026-07-29): ``read_files(start_line=6171,
end_line=6162)`` errored out. An inverted interval is unambiguous — there
is exactly one range the caller can mean — so rejecting it only burns a
round trip.

The lone-spec error was the VISIBLE half. The worse half is silent: a
reversed spec batched with another range for the SAME file was absorbed by
``_merge_same_file_ranges`` (it sorts by ``(start, end)`` then coalesces
within GAP_THRESHOLD), so the model received a clean result for lines it
never requested, with no error at all. Measured before the fix:

    [{100→50}, {60,70}]  →  [{60,70}]      # the 50-100 intent vanished

Guardrails here:
  L1 — reversed ranges are repaired at the funnel, BEFORE merging (kills
       both the error case and the silent-absorption case).
  L2 — the repair is NOTED in the result, so a malformed call stays visible.
  L3 — COMPLEMENT: a genuinely out-of-bounds range is NOT "repaired".
       Swapping cannot rescue it and silently accepting it would mask a
       wrong request.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.project_mod.read_tools import (  # noqa: E402
    _merge_same_file_ranges,
    _normalize_line_range,
    tool_read_files,
)

pytestmark = pytest.mark.unit

TOTAL_LINES = 9000


@pytest.fixture
def proj(tmp_path):
    """A 9000-line file — big enough to defeat the whole-file auto-expand."""
    body = '\n'.join(f'line{i}' for i in range(1, TOTAL_LINES + 1))
    (tmp_path / 'big.txt').write_text(body)
    return str(tmp_path)


# ── L0: the helper itself ────────────────────────────────────────────

class TestNormalizeHelper:
    def test_reversed_is_swapped_and_flagged(self):
        assert _normalize_line_range(6171, 6162) == (6162, 6171, True)

    @pytest.mark.parametrize('s,e', [(10, 20), (500, 500), (1, 2)])
    def test_ordered_range_untouched(self, s, e):
        assert _normalize_line_range(s, e) == (s, e, False)

    @pytest.mark.parametrize('s,e', [(100, None), (None, 100), (None, None)])
    def test_single_sided_cannot_be_reversed(self, s, e):
        """One bound missing means 'open ended', never inverted."""
        assert _normalize_line_range(s, e) == (s, e, False)

    def test_idempotent(self):
        """Re-normalising an already-normal range is a no-op (the display
        path and the exec path both call this on the same spec)."""
        once = _normalize_line_range(6171, 6162)
        twice = _normalize_line_range(once[0], once[1])
        assert twice == (6162, 6171, False)


# ── L1: the reported case now returns content ────────────────────────

class TestReversedRangeIsRead:
    def test_reversed_range_returns_the_lines(self, proj):
        out = tool_read_files(proj, [{'path': 'big.txt',
                                      'start_line': 6171, 'end_line': 6162}])
        assert not out.lstrip().startswith('Error'), out[:200]
        # Reads exactly what 6162-6171 would have read.
        assert 'line6162' in out
        assert 'line6171' in out
        assert 'line6161' not in out
        assert 'line6172' not in out

    def test_matches_the_equivalent_forward_range(self, proj):
        rev = tool_read_files(proj, [{'path': 'big.txt',
                                      'start_line': 6171, 'end_line': 6162}])
        fwd = tool_read_files(proj, [{'path': 'big.txt',
                                      'start_line': 6162, 'end_line': 6171}])
        # Identical once the advisory note (asserted separately) is stripped.
        body = rev.split('\n\n', 1)[1] if rev.startswith('[Note]') else rev
        assert body == fwd, 'repaired read must equal the forward read'

    def test_string_reversed_range_also_repaired(self, proj):
        """Coercion (str→int) runs first, so string bounds reach the swap."""
        out = tool_read_files(proj, [{'path': 'big.txt',
                                      'start_line': '6171', 'end_line': '6162'}])
        assert 'line6162' in out and 'line6171' in out


# ── L1b: the SILENT half — reversed spec inside a batch ──────────────

class TestReversedRangeNotSwallowedByMerge:
    def test_merge_no_longer_loses_the_reversed_interval(self):
        """Pre-fix this returned only [{60,70}] — the 50-100 intent was
        absorbed and the model silently got the wrong lines."""
        merged = _merge_same_file_ranges([
            {'path': 'a.py', 'start_line': 50, 'end_line': 100},
            {'path': 'a.py', 'start_line': 60, 'end_line': 70},
        ])
        assert len(merged) == 1
        assert merged[0]['start_line'] == 50
        assert merged[0]['end_line'] == 100

    def test_batched_reversed_spec_still_yields_its_lines(self, proj):
        """End-to-end: the reversed spec's lines must survive the merge."""
        out = tool_read_files(proj, [
            {'path': 'big.txt', 'start_line': 6171, 'end_line': 6162},
            {'path': 'big.txt', 'start_line': 6180, 'end_line': 6185},
        ])
        assert 'line6162' in out, 'reversed spec was swallowed by the merge'
        assert 'line6171' in out
        assert 'line6185' in out


# ── L2: the repair stays visible ─────────────────────────────────────

class TestRepairIsAnnounced:
    def test_note_names_the_correction(self, proj):
        out = tool_read_files(proj, [{'path': 'big.txt',
                                      'start_line': 6171, 'end_line': 6162}])
        assert 'auto-corrected' in out
        assert '6162' in out.split('\n')[0] and '6171' in out.split('\n')[0]

    def test_no_note_when_nothing_was_repaired(self, proj):
        out = tool_read_files(proj, [{'path': 'big.txt',
                                      'start_line': 6162, 'end_line': 6171}])
        assert 'auto-corrected' not in out


# ── L3: COMPLEMENT — genuine errors must NOT be "repaired" ───────────

class TestGenuineOutOfBoundsStillErrors:
    def test_out_of_bounds_forward_range_still_errors(self, proj):
        """20000-20100 of a 9000-line file is a real error; swapping cannot
        rescue it and accepting it would mask a wrong request."""
        out = tool_read_files(proj, [{'path': 'big.txt',
                                      'start_line': 20000, 'end_line': 20100}])
        assert 'Error' in out
        assert 'out of bounds' in out
        assert 'auto-corrected' not in out

    def test_out_of_bounds_reversed_range_errors_after_swap(self, proj):
        """Reversed AND out of bounds: swap, then still report the bound
        error — the repair must not paper over it."""
        out = tool_read_files(proj, [{'path': 'big.txt',
                                      'start_line': 20100, 'end_line': 20000}])
        assert 'Error' in out
        assert 'out of bounds' in out

    def test_missing_file_unaffected(self, proj):
        out = tool_read_files(proj, [{'path': 'nope.txt',
                                      'start_line': 20, 'end_line': 10}])
        assert 'File not found' in out


# ── L4: the display path agrees with what was read ───────────────────

class TestDisplayShowsCorrectedRange:
    def test_chip_shows_forward_range(self):
        from lib.project_mod.tools import project_tool_display
        disp = project_tool_display('read_files', {
            'reads': [{'path': 'big.txt', 'start_line': 6171, 'end_line': 6162}]})
        assert 'L6162-6171' in disp, disp
        assert 'L6171-6162' not in disp

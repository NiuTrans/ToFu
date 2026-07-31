"""Vertical-search block must survive the oversized-web_search disk offload.

Root cause (see JOURNAL 2026-07-06): when a batch ``web_search`` result exceeds
its Layer-0 budget, ``_persist_web_search_split`` writes one file per hit and
returns an INDEX as the new LLM-facing ``toolContent``. The handler had prepended
a ``═══ Vertical Search … ═══`` block (paper/citation metadata), but the split
boundary is the 20-char ``════`` per-result separator, NOT the 3-char ``═══``
header rules — so the block got glued onto result-1's split file and dropped out
of the model's immediate context. It stayed in ``round.verticals`` (the purple
card rendered fine) but vanished from the debug-panel wire message.

The fix relocates every vertical block out of the split body and prepends it to
the index verbatim.

Test plan (project double-neuter convention):
  1. Large batch web_search content WITH a vertical header exceeds budget →
     the returned index CONTAINS the vertical block(s), and the split file for
     result-1 does NOT.
  2. Offload of content with NO vertical header is byte-for-byte the same before
     and after the fix (no collateral change).
  3. End-to-end through the real ``budget_tool_result`` +
     ``_reconstruct_tool_call_messages`` → the reconstructed debug-panel wire
     ``role:tool`` message surfaces the vertical block.
  4. NEUTER: strip the header-relocation lines from the on-disk source → the
     index LOSES the block (proving it is load-bearing); restore byte-identical.

Run:  pytest tests/test_persist_vertical_block_relocate.py -v
"""
from __future__ import annotations

import glob
import os
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PERSIST_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'lib', 'tasks_pkg', 'compaction', '_persist', '_splitters.py',
)

# The exact marker strings the handler (_vertical_header_for_llm) emits.
_VERT_HDR = '═══ Vertical Search'
_VERT_CLOSE = '═══ Web Search Results ═══'
_SEP = '════════════════════'


# ── Fixtures: synthetic oversized web_search content ──────────────────────

def _one_result(n: int, title: str, fill_kb: int = 40) -> str:
    """One structured web_search result block (over budget when several)."""
    body = ('lorem ipsum dolor sit amet ' * 40 + '\n') * (fill_kb * 25)
    return (
        f'[{n}] {title}\n'
        f'URL: https://example.com/paper/{n}\n'
        f'──── Full Page Content ────\n'
        f'{body}'
    )


def _vert_block_single() -> str:
    return (
        f'{_VERT_HDR} (academic: Semantic Scholar + Hugging Face Papers) ═══\n\n'
        f'## Hugging Face Papers\n\n'
        f'- **Rubric Is All You Need** (arXiv:2512.24618, ▲155)\n'
        f'  A paper about agentic rubrics for code evaluation.\n\n'
        f'{_VERT_CLOSE}\n\n'
    )


def _vert_block_for_query(q: str) -> str:
    return (
        f'{_VERT_HDR} (academic: Hugging Face Papers) ═══\n\n'
        f'## Papers for "{q}"\n\n'
        f'- **Agentic Rubrics as Contextual Verifiers** (arXiv:2607.02032)\n\n'
        f'{_VERT_CLOSE}\n\n'
    )


def _make_single_search_content() -> str:
    """Single-query oversized web_search content with ONE leading vertical block."""
    results = _SEP.join(_one_result(i, f'Result number {i}') for i in range(1, 5))
    return _vert_block_single() + results


def _make_batch_search_content() -> str:
    """Batch oversized web_search content with a vertical block per query section."""
    q1 = 'agentic rubrics generation LLM evaluation'
    q2 = 'rubric-based reward model SWE-bench judge'
    sec1_results = _SEP.join(_one_result(i, f'Q1 result {i}') for i in range(1, 4))
    sec2_results = _SEP.join(_one_result(i, f'Q2 result {i}') for i in range(1, 4))
    sec1 = f'=== Search: {q1} ===\n{_vert_block_for_query(q1)}{sec1_results}'
    sec2 = f'=== Search: {q2} ===\n{_vert_block_for_query(q2)}{sec2_results}'
    return f'{sec1}\n\n{sec2}'


# ─────────────────────────────────────────────────────────────────────────
#  1 + 2. Direct behavior of the splitter
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestVerticalBlockRelocate:

    def _run_split(self, content: str):
        from lib.tasks_pkg.compaction._persist import _persist_web_search_split
        with tempfile.TemporaryDirectory() as d:
            safe_id = uuid.uuid4().hex[:8]
            index = _persist_web_search_split(content, d, safe_id)
            files = {os.path.basename(p): open(p, encoding='utf-8').read()
                     for p in glob.glob(os.path.join(d, 'search_*.txt'))}
            return index, files

    def test_single_search_block_in_index_not_in_split_files(self):
        content = _make_single_search_content()
        # sanity: content is genuinely a split candidate
        assert content.count(_SEP) >= 1
        index, files = self._run_split(content)
        assert index is not None, 'should have split into per-result files'
        # The vertical block is in the LLM-facing index
        assert _VERT_HDR in index
        assert _VERT_CLOSE in index
        assert 'Rubric Is All You Need' in index
        # ...and NOT glued onto any split file (esp. result-1)
        assert files, 'expected split files on disk'
        for name, body in files.items():
            assert _VERT_HDR not in body, f'stray vertical header leaked into {name}'
            assert 'Rubric Is All You Need' not in body, f'vertical body leaked into {name}'

    def test_batch_search_every_block_relocated(self):
        content = _make_batch_search_content()
        index, files = self._run_split(content)
        assert index is not None
        # Both per-query vertical blocks land in the index
        assert index.count(_VERT_HDR) == 2, 'both batch vertical blocks should be in the index'
        assert index.count(_VERT_CLOSE) == 2
        assert 'Agentic Rubrics as Contextual Verifiers' in index
        for name, body in files.items():
            assert _VERT_HDR not in body, f'stray vertical header leaked into {name}'

    def test_no_vertical_header_unchanged(self):
        """Content with NO vertical block must offload exactly as before."""
        from lib.tasks_pkg.compaction._persist import _persist_web_search_split
        content = _SEP.join(_one_result(i, f'Plain {i}') for i in range(1, 5))
        assert _VERT_HDR not in content
        with tempfile.TemporaryDirectory() as d:
            index = _persist_web_search_split(content, d, 'plainid00')
        assert index is not None
        assert _VERT_HDR not in index
        # Index still starts with the normal "Search returned N results" banner
        assert index.lstrip().startswith('Search returned')

    def test_missing_close_marker_no_result_text_lost(self):
        """DATA-LOSS GUARD: a vertical open marker with NO matching close must
        NOT let the lazy DOTALL regex swallow real result bodies.

        Pathological content: the leading vertical block is missing its
        ``═══ Web Search Results ═══`` close (e.g. truncated by an earlier
        budget pass). A later result body happens to literally contain that
        close string, so an unguarded ``.*?`` would match from the open marker
        all the way THROUGH results 1..k and sub them out — deleting result
        text. The guard must detect the over-reach (the span contains ``════``
        / ``[N]`` markers) and abandon relocation, preserving every result.
        """
        # Result-2's body contains the close marker at LINE START downstream
        # (the ^-anchored close pattern only over-reaches to a line-start
        # occurrence).
        r1 = _one_result(1, 'First result')
        poisoned = (
            f'[2] Second result\n'
            f'URL: https://example.com/paper/2\n'
            f'──── Full Page Content ────\n'
            f'some body text\n'
            f'{_VERT_CLOSE}\n'
            f'more body text\n'
            + ('filler line\n' * 2000)
        )
        r3 = _one_result(3, 'Third result')
        # Leading vertical header with NO close of its own.
        broken_hdr = f'{_VERT_HDR} (academic: HF) ═══\n\n## Papers\n- **UniquePaperTitleXYZ**\n\n'
        content = broken_hdr + _SEP.join([r1, poisoned, r3])

        index, files = self._run_split(content)
        assert index is not None, 'should still split into per-result files'

        # Every result body must survive SOMEWHERE (index preview + files).
        haystack = index + '\n' + '\n'.join(files.values())
        assert 'First result' in haystack, 'result 1 text lost'
        assert 'Second result' in haystack, 'result 2 text lost (over-greedy swallow)'
        assert 'Third result' in haystack, 'result 3 text lost'
        # The distinctive result-2 body marker must not have been deleted.
        assert 'more body text' in haystack, 'result-2 body deleted by over-greedy match'
        # No result-body chars should have gone missing: the concatenated split
        # files must carry all three [N] result markers.
        files_blob = '\n'.join(files.values())
        for marker in ('[1]', '[2]', '[3]'):
            assert marker in files_blob, f'result marker {marker} missing from split files'


# ─────────────────────────────────────────────────────────────────────────
#  3. End-to-end: real budget_tool_result → debug-panel wire reconstruction
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestEndToEndWireMessage:

    def test_offload_then_reconstruct_surfaces_vertical(self):
        from lib.tasks_pkg.compaction import budget_tool_result
        from lib.tasks_pkg.conv_message_builder import _reconstruct_tool_call_messages

        content = _make_batch_search_content()
        # Must genuinely exceed the web_search budget so the offload path fires.
        from lib.tasks_pkg.compaction._constants import TOOL_RESULT_MAX_CHARS
        assert len(content) > TOOL_RESULT_MAX_CHARS['web_search']

        tc_id = 'toolu_' + uuid.uuid4().hex[:24]
        persisted = budget_tool_result('web_search', content, tool_use_id=tc_id,
                                       conv_id='testconv_vert')
        # Offload happened (index, not raw content)
        assert 'saved to separate files' in persisted
        # The vertical block survived into the LLM-facing content
        assert _VERT_HDR in persisted
        assert _VERT_CLOSE in persisted

        # This is exactly what the /debug-messages panel reconstructs from the
        # stored round: a role:tool message whose content == the persisted
        # toolContent.
        rounds = [{
            'toolCallId': tc_id,
            'toolName': 'web_search',
            'status': 'done',
            'llmRound': 0,
            'toolArgs': '{"queries": []}',
            'toolContent': persisted,
        }]
        msgs = _reconstruct_tool_call_messages(rounds)
        assert msgs is not None
        tool_msgs = [m for m in msgs if m.get('role') == 'tool']
        assert len(tool_msgs) == 1
        assert _VERT_HDR in tool_msgs[0]['content'], \
            'debug-panel wire tool message must surface the vertical block'


# ─────────────────────────────────────────────────────────────────────────
#  4. NEUTER — prove the relocation lines are load-bearing
# ─────────────────────────────────────────────────────────────────────────

# The line that DISCOVERS the blocks to relocate inside
# _persist_web_search_split. Zeroing it out disables the whole relocation
# (nothing gets prepended, the header stays glued onto result-1 — the bug).
_NEUTER_FIND = "    vert_blocks = _VERT_BLOCK_RE.findall(content)\n"
_NEUTER_REPLACE = "    vert_blocks = []  # NEUTERED: relocation disabled\n"


@pytest.mark.unit
def test_neuter_relocation_is_load_bearing():
    """In-memory neuter (xdist-safe): disable the relocation → the index loses
    the vertical block. Uses the shared NC harness so the SHIPPED source is
    opened read-only and never written — a crash can't poison the tree."""
    from tests._nc_harness import neutered_source

    with neutered_source(_PERSIST_SRC, _NEUTER_FIND, _NEUTER_REPLACE) as _p:
        # The swapped module's own _persist_web_search_split is the neutered one.
        content = _make_single_search_content()
        with tempfile.TemporaryDirectory() as d:
            idx = _p._persist_web_search_split(content, d, 'neut' + uuid.uuid4().hex[:6])
            files = [open(p, encoding='utf-8').read()
                     for p in glob.glob(os.path.join(d, 'search_*.txt'))]
        # With the fix neutered: the header is NOT in the index (bug reproduced),
        # and IS glued into a split file.
        assert idx is not None
        assert _VERT_HDR not in idx, \
            'neuter did not drop the block from the index'
        assert any(_VERT_HDR in b for b in files), \
            'neuter should leave the block glued in a split file'


@pytest.mark.unit
def test_source_restored_and_reimport_still_works():
    """After the in-memory neuter context exits, the canonical module still
    relocates (it was never mutated/reloaded)."""
    import lib.tasks_pkg.compaction._persist as _p
    content = _make_single_search_content()
    with tempfile.TemporaryDirectory() as d:
        index = _p._persist_web_search_split(content, d, 'restorechk')
    assert index is not None and _VERT_HDR in index

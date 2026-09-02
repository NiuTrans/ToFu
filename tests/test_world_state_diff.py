"""World-state differential annotation — Codex world_state.rs port (adapted).

Pins the turn-over-turn change trailers the composer appends to the volatile
tail blocks (project_board / project_goals / related_conversations):

  * first sight  → no trailer, baseline learned;
  * unchanged    → "[World-state: unchanged since your last turn]";
  * changed      → "[World-state Δ … +A −B line(s)]" + the added/removed
    lines (capped), so the model sees exactly what a sibling moved.

Baseline is learned from the RAW render (trailer excluded) so an annotated
turn never poisons the next diff.
"""

import pytest

from lib.tasks_pkg.context_composer._models import ComposeRequest, ContextBlock
from lib.tasks_pkg.context_composer._world_diff import (
    _MAX_DELTA_LINES,
    _reset_for_tests,
    annotate_turn_blocks,
)

pytestmark = pytest.mark.unit

CONV = 'c' * 32


@pytest.fixture(autouse=True)
def _clean_store():
    _reset_for_tests()
    yield
    _reset_for_tests()


def _block(block_id: str, content: str, *, suppressed: str = '') -> ContextBlock:
    return ContextBlock(
        id=block_id, source='test', content=content, authority='ambient',
        placement='tail', stability='turn', lifecycle='task', priority=40,
        suppressed_reason=suppressed,
    )


def _annotate(conv_id: str, *blocks: ContextBlock) -> list[ContextBlock]:
    """annotate_turn_blocks swaps frozen-dataclass entries in place — return
    the list so tests read back the REPLACED copies."""
    lst = list(blocks)
    annotate_turn_blocks(conv_id, lst)
    return lst


BOARD_V1 = '[BOARD]\n  • [pt_aaaa] epic one — claimed by x\n  • [pt_bbbb] epic two — open'
BOARD_V2 = '[BOARD]\n  • [pt_aaaa] epic one — claimed by x\n  • [pt_cccc] epic three — claimed by y'


def test_first_sight_learns_baseline_without_trailer():
    (b,) = _annotate(CONV, _block('project_board', BOARD_V1))
    assert b.content == BOARD_V1
    assert b.provenance['worldState'] == 'baseline'


def test_unchanged_block_gets_skim_trailer():
    _annotate(CONV, _block('project_board', BOARD_V1))
    (b2,) = _annotate(CONV, _block('project_board', BOARD_V1))
    assert '[World-state: unchanged since your last turn]' in b2.content
    assert b2.provenance['worldState'] == 'unchanged'


def test_changed_block_shows_line_delta():
    _annotate(CONV, _block('project_board', BOARD_V1))
    (b2,) = _annotate(CONV, _block('project_board', BOARD_V2))
    assert '[World-state Δ since your last turn: +1 −1 line(s)]' in b2.content
    assert '-  • [pt_bbbb] epic two — open' in b2.content
    assert '+  • [pt_cccc] epic three — claimed by y' in b2.content
    assert b2.provenance['worldState'] == 'changed:+1-1'


def test_baseline_excludes_trailer_so_annotated_turn_stays_diffable():
    # Turn 2 (unchanged) carries the skim trailer; turn 3 (still unchanged)
    # must STILL diff as unchanged — the baseline stores the raw render.
    _annotate(CONV, _block('project_board', BOARD_V1))
    _annotate(CONV, _block('project_board', BOARD_V1))
    (b3,) = _annotate(CONV, _block('project_board', BOARD_V1))
    assert b3.provenance['worldState'] == 'unchanged'


def test_delta_excerpt_is_capped():
    big_old = '\n'.join(f'line {i} old' for i in range(30))
    big_new = '\n'.join(f'line {i} new' for i in range(30))
    _annotate(CONV, _block('project_board', big_old))
    (b2,) = _annotate(CONV, _block('project_board', big_new))
    delta_lines = [ln for ln in b2.content.splitlines()
                   if ln.startswith(('+', '-'))]
    assert len(delta_lines) <= _MAX_DELTA_LINES
    assert 'more changed line(s)' in b2.content


def test_non_whitelisted_block_is_never_annotated():
    _annotate(CONV, _block('relevant_memories', 'some memory block'))
    _annotate(CONV, _block('relevant_memories', 'some memory block'))
    (b3,) = _annotate(CONV, _block('relevant_memories', 'some memory block'))
    assert b3.content == 'some memory block'
    assert 'worldState' not in b3.provenance


def test_empty_conv_id_is_a_noop():
    (b,) = _annotate('', _block('project_board', BOARD_V1))
    assert b.content == BOARD_V1
    assert 'worldState' not in b.provenance


def test_suppressed_or_empty_block_is_skipped():
    (b,) = _annotate(CONV, _block('project_board', '',
                                  suppressed='no_active_claims'))
    assert 'worldState' not in b.provenance


def test_convs_have_independent_baselines():
    _annotate(CONV, _block('project_board', BOARD_V1))
    (other,) = _annotate('d' * 32, _block('project_board', BOARD_V2))
    # A different conv's first sight is a baseline, not a diff vs CONV's.
    assert other.provenance['worldState'] == 'baseline'


def test_compose_context_integration_annotates_whitelisted_blocks(
        monkeypatch):
    """End-to-end through the single composition boundary."""
    import lib.tasks_pkg.context_composer as cc

    monkeypatch.setattr(cc, 'collect_context_blocks',
                        lambda messages, request: [
                            _block('project_board', BOARD_V1)])

    def _req():
        return ComposeRequest(
            project_path='', project_enabled=False, memory_enabled=False,
            search_enabled=False, has_real_tools=False, conv_id=CONV,
            model='', system_prompt_mode='append', tool_names=frozenset(),
            disabled_blocks=frozenset(), task=None)

    r1 = cc.compose_context([{'role': 'user', 'content': 'hi'}], _req())
    tail1 = r1.messages[-1]['content'][0]['text']
    assert '[World-state:' not in tail1  # baseline turn

    r2 = cc.compose_context([{'role': 'user', 'content': 'hi'}], _req())
    tail2 = r2.messages[-1]['content'][0]['text']
    assert '[World-state: unchanged since your last turn]' in tail2

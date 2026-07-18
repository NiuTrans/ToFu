"""Golden byte-identity gate for the segment-timeline model (step 1).

Board epic pt_cb8f98b0cb9b47fb / docs/EPIC_SEGMENT_TIMELINE_DESIGN.md §5-6.

Step 1 ships DARK: ``task['segments']`` is populated alongside the three
existing channels (``content`` / ``thinking`` / ``toolRounds``). This suite is
the acceptance gate that de-risks the whole chain — it proves the three
channels are *loss-less projections* of the segment list over real multi-round
transcript shapes, so none of the ~40 measured backend readers can drift when
later steps flip them onto segments.

  • GOLDEN: for each transcript, ``derive_content`` / ``derive_thinking`` /
    ``derive_tool_rounds`` are byte-identical to what the current pipeline
    exposes (terminal ``task['content']`` / ``task['thinking']`` and
    ``_merge_tool_rounds(task)``).
  • NC-1: corrupt the derivation (misclassify the deliverable segment) → the
    golden diverges → the test FAILS. Proves the assertions are load-bearing.
  • Independence: the ``deliverable`` classification does NOT depend on
    ``_discard_pretool_prose`` having zeroed the accumulators — assembly reads
    the pre-tool ``assistantContent`` snapshot and the terminal string
    separately. Pinned by ``test_deliverable_rule_is_position_based``.

Pure-unit: no Flask, no DB, no LLM.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.tasks_pkg.manager import _merge_tool_rounds
from lib.tasks_pkg.segments import (
    SEG_TEXT,
    SEG_THINKING,
    SEG_TOOL_USE,
    assemble_segments,
    derive_content,
    derive_thinking,
    derive_tool_rounds,
    rehydrate_segments,
    segments_to_json,
)


# ═══════════════════════════════════════════════════════════
#  Fixtures — realistic finished-turn task dicts
# ═══════════════════════════════════════════════════════════

def _round(round_num, llm_round, tc_id, name, args, content, *,
           assistant_content='', thinking='', thinking_signature='',
           extra_content=None, status='done'):
    """A tool round entry in the exact live shape (tool_dispatch stamps these)."""
    r = {
        'roundNum': round_num,
        'llmRound': llm_round,
        'toolCallId': tc_id,
        'toolName': name,
        'toolArgs': args,
        'toolContent': content,
        'status': status,
    }
    if assistant_content:
        r['assistantContent'] = assistant_content
    if thinking:
        r['thinking'] = thinking
    if thinking_signature:
        r['thinkingSignature'] = thinking_signature
    if extra_content:
        r['extraContent'] = extra_content
    return r


def _task_single_round():
    """One assistant turn: prose → 1 tool call → final answer."""
    return {
        'id': 'a' * 32, 'convId': 'c' * 32,
        'content': 'The file defines two functions.',
        'thinking': '',
        'toolRounds': [
            _round(1, 0, 'tc_1', 'read_files', '{"path":"a.py"}', 'def f(): ...',
                   assistant_content='Let me read the file.'),
        ],
    }


def _task_multi_round():
    """Three assistant turns with interleaved thinking + narration + batches."""
    return {
        'id': 'b' * 32, 'convId': 'c' * 32,
        'content': 'Fixed the bug on line 42. Here is the patch:\n\n```py\n...\n```',
        'thinking': 'The terminal answer needed a code block.',
        'toolRounds': [
            # Batch 0: two tool calls from ONE assistant turn (same llmRound).
            _round(1, 0, 'tc_1', 'grep_search', '{"pattern":"bug"}', 'line 42 hit',
                   assistant_content='Let me search for the bug.',
                   thinking='I should grep first.',
                   thinking_signature='opaque-sig-0'),
            _round(2, 0, 'tc_2', 'read_files', '{"path":"b.py"}', 'def g(): ...'),
            # Batch 1: one tool call, its own narration.
            _round(3, 1, 'tc_3', 'apply_diff', '{"path":"b.py"}', 'ok, 1 change',
                   assistant_content='Now let me apply the fix.'),
        ],
    }


def _task_thinking_only_terminal():
    """Terminal round with thinking but the model called a tool and answered."""
    return {
        'id': 'd' * 32, 'convId': 'c' * 32,
        'content': 'Done.',
        'thinking': 'Confirmed the change compiles.',
        'toolRounds': [
            _round(1, 0, 'tc_1', 'run_command', '{"command":"pytest"}', 'PASS',
                   assistant_content='Running the tests.'),
        ],
    }


def _task_no_tools():
    """A plain answer turn — no tool calls at all."""
    return {
        'id': 'e' * 32, 'convId': 'c' * 32,
        'content': 'The capital of France is Paris.',
        'thinking': 'Simple factual question.',
        'toolRounds': [],
    }


def _task_continue_checkpoint():
    """Continue flow: checkpoint rounds + current-turn rounds merge in order."""
    t = {
        'id': 'f' * 32, 'convId': 'c' * 32,
        'content': 'Final synthesis after resuming.',
        'thinking': '',
        '_checkpointToolRounds': [
            _round(1, 0, 'tc_a', 'web_search', '{"query":"x"}', 'result a',
                   assistant_content='Searching (pre-checkpoint).'),
        ],
        'toolRounds': [
            _round(2, 1, 'tc_b', 'fetch_url', '{"url":"https://x"}', 'page body',
                   assistant_content='Fetching the top hit.'),
        ],
    }
    return t


def _round_prefetch(round_num, url, content):
    """Prefetch fetch_url round — the EXACT shape executor.py:532 emits:
    no llmRound, no toolCallId, no toolArgs, no toolContent (results/query only)."""
    return {
        'roundNum': round_num,
        'query': f'📄 {url}',
        'results': [{'title': f'Page: {url}', 'snippet': f'{len(content)} chars',
                     'url': url, 'source': 'Direct Fetch', 'fetched': True}],
        'status': 'done',
        'toolName': 'fetch_url',
    }


def _round_image_gen(round_num, llm_round, tc_id, prompt):
    """generate_image round — carries query/results (a data-uri meta), NOT
    toolContent (executor_image.py). toolContent is None on the round dict."""
    return {
        'roundNum': round_num,
        'llmRound': llm_round,
        'toolCallId': tc_id,
        'toolName': 'generate_image',
        'toolArgs': f'{{"prompt":"{prompt}"}}',
        'query': f'🎨 {prompt}',
        'results': [{'toolName': 'generate_image', 'imagePrompt': prompt,
                     'imageDataUri': 'data:image/png;base64,AAAA', 'badge': '✓'}],
        'status': 'done',
        'assistantContent': 'Generating the image.',
    }


def _round_rejected(round_num, llm_round, fake_name):
    """Hallucinated/rejected round — status='rejected', _rejected set, never
    executed (toolContent None). Shape from tool_dispatch hallucination branch."""
    return {
        'roundNum': round_num,
        'llmRound': llm_round,
        'toolCallId': f'tc_rej_{round_num}',
        'toolName': fake_name,
        'toolArgs': '{}',
        'status': 'rejected',
        '_rejected': {'attempted': fake_name, 'suggestions': []},
        'toolContent': None,
        'assistantContent': 'Let me use a tool.',
    }


def _task_prefetch_interleaved():
    """#1 — prefetch fetch_url rounds (no llmRound) interleaved with a normal
    tool batch. The None-llmRound rounds must NOT collapse into one phantom
    batch nor lose the normal round's prose."""
    return {
        'id': 'p' * 32, 'convId': 'c' * 32,
        'content': 'Both pages confirm the API changed.',
        'thinking': '',
        'toolRounds': [
            _round_prefetch(1, 'https://a.example', 'body a'),
            _round_prefetch(2, 'https://b.example', 'body b'),
            _round(3, 0, 'tc_1', 'grep_search', '{"pattern":"api"}', 'hit',
                   assistant_content='Now let me grep the code.'),
        ],
    }


def _task_image_gen():
    """#2 — image-gen round (query/results, no toolContent)."""
    return {
        'id': 'i' * 32, 'convId': 'c' * 32,
        'content': 'Here is the generated logo.',
        'thinking': '',
        'toolRounds': [
            _round_image_gen(1, 0, 'tc_img', 'a red fox logo'),
        ],
    }


def _task_rejected_round():
    """#3 — a hallucinated/rejected round that never executed, followed by a
    real recovery round + answer. Rejected rounds must round-trip and emit NO
    spurious deliverable."""
    return {
        'id': 'r' * 32, 'convId': 'c' * 32,
        'content': 'Recovered and answered.',
        'thinking': '',
        'toolRounds': [
            _round_rejected(1, 0, 'search_web'),
            _round(2, 1, 'tc_ok', 'web_search', '{"query":"x"}', 'real result',
                   assistant_content='Using the correct tool name.'),
        ],
    }


def _task_nonelement_terminal():
    """#4 — non-content terminal exit (tool_rounds_exhausted / budget /
    content_filter): task['content'] is EMPTY, an error envelope is set, and
    the last round has a tool call. Must yield NO phantom deliverable segment
    and derive_content must equal the (empty) channel."""
    return {
        'id': 'x' * 32, 'convId': 'c' * 32,
        'content': '',
        'thinking': '',
        'error': {'code': 'tool_rounds_exhausted', 'detail': 'limit reached'},
        'toolRounds': [
            _round(1, 0, 'tc_1', 'read_files', '{"path":"a.py"}', 'body',
                   assistant_content='Reading first.'),
            _round(2, 1, 'tc_2', 'read_files', '{"path":"b.py"}', 'body2',
                   assistant_content='And the next file.'),
        ],
    }


ALL_TASKS = [
    _task_single_round,
    _task_multi_round,
    _task_thinking_only_terminal,
    _task_no_tools,
    _task_continue_checkpoint,
    _task_prefetch_interleaved,
    _task_image_gen,
    _task_rejected_round,
    _task_nonelement_terminal,
]


# ═══════════════════════════════════════════════════════════
#  GOLDEN — derived projections are byte-identical to the pipeline
# ═══════════════════════════════════════════════════════════

class TestGoldenByteIdentity:
    @pytest.mark.parametrize('make', ALL_TASKS, ids=[f.__name__ for f in ALL_TASKS])
    def test_derived_content_matches_channel(self, make):
        task = make()
        segs = assemble_segments(task)
        assert derive_content(segs) == task['content']

    @pytest.mark.parametrize('make', ALL_TASKS, ids=[f.__name__ for f in ALL_TASKS])
    def test_derived_thinking_matches_channel(self, make):
        task = make()
        segs = assemble_segments(task)
        assert derive_thinking(segs) == task['thinking']

    @pytest.mark.parametrize('make', ALL_TASKS, ids=[f.__name__ for f in ALL_TASKS])
    def test_derived_tool_rounds_match_merge(self, make):
        task = make()
        segs = assemble_segments(task)
        # Byte-identity against the SAME _merge_tool_rounds the persist path uses.
        assert derive_tool_rounds(segs) == _merge_tool_rounds(task)

    def test_segment_order_is_interleaved(self):
        """The multi-round turn produces segments in true chronological order."""
        segs = assemble_segments(_task_multi_round())
        shape = [(s['type'], s.get('deliverable'), s.get('name'))
                 for s in segs]
        assert shape == [
            (SEG_THINKING, False, None),           # batch-0 thinking
            (SEG_TEXT, False, None),               # batch-0 narration
            (SEG_TOOL_USE, None, 'grep_search'),   # batch-0 call 1
            (SEG_TOOL_USE, None, 'read_files'),    # batch-0 call 2 (same batch, no repeat prose)
            (SEG_TEXT, False, None),               # batch-1 narration
            (SEG_TOOL_USE, None, 'apply_diff'),    # batch-1 call
            (SEG_THINKING, False, None),           # terminal thinking
            (SEG_TEXT, True, None),                # terminal deliverable answer
        ]

    def test_batch_prose_emitted_once_per_llmround(self):
        """Two tool calls in one llmRound → narration segment appears ONCE."""
        segs = assemble_segments(_task_multi_round())
        batch0_text = [s for s in segs
                       if s.get('type') == SEG_TEXT and s.get('llmRound') == 0]
        assert len(batch0_text) == 1
        assert batch0_text[0]['text'] == 'Let me search for the bug.'

    def test_thinking_signature_preserved_on_segment(self):
        segs = assemble_segments(_task_multi_round())
        think0 = next(s for s in segs
                      if s.get('type') == SEG_THINKING and s.get('llmRound') == 0)
        assert think0['signature'] == 'opaque-sig-0'


# ═══════════════════════════════════════════════════════════
#  Production round-shape edge cases (the four fidelity gaps)
# ═══════════════════════════════════════════════════════════

class TestProductionRoundShapes:
    def test_1_prefetch_rounds_not_collapsed_and_prose_preserved(self):
        """#1 — two None-llmRound prefetch rounds must NOT collapse into one
        phantom batch, and the following real round's narration survives."""
        task = _task_prefetch_interleaved()
        segs = assemble_segments(task)
        tool_uses = [s for s in segs if s['type'] == SEG_TOOL_USE]
        assert [s['name'] for s in tool_uses] == ['fetch_url', 'fetch_url', 'grep_search']
        # The real round's narration is present exactly once.
        narration = [s for s in segs if s['type'] == SEG_TEXT and not s.get('deliverable')]
        assert [s['text'] for s in narration] == ['Now let me grep the code.']
        # Deliverable + tool-round golden already covered by the parametrized
        # suite; assert them here too for locality.
        assert derive_content(segs) == task['content']
        assert derive_tool_rounds(segs) == _merge_tool_rounds(task)

    def test_2_image_gen_round_round_trips_without_toolcontent(self):
        """#2 — image-gen round has results/query but toolContent=None; it must
        round-trip through derive_tool_rounds and carry result.status."""
        task = _task_image_gen()
        segs = assemble_segments(task)
        img = next(s for s in segs if s['type'] == SEG_TOOL_USE and s['name'] == 'generate_image')
        assert img['result']['content'] is None      # no toolContent — preserved as None
        assert img['result']['status'] == 'done'
        assert derive_tool_rounds(segs) == _merge_tool_rounds(task)

    def test_3_rejected_round_round_trips_no_spurious_deliverable(self):
        """#3 — a rejected round survives derive_tool_rounds AND emits no
        deliverable text segment of its own (only the terminal answer is
        deliverable)."""
        task = _task_rejected_round()
        segs = assemble_segments(task)
        rej = next(s for s in segs if s['type'] == SEG_TOOL_USE and s['name'] == 'search_web')
        assert rej['result']['status'] == 'rejected'
        deliverables = [s for s in segs if s['type'] == SEG_TEXT and s.get('deliverable')]
        assert [s['text'] for s in deliverables] == ['Recovered and answered.']
        assert derive_tool_rounds(segs) == _merge_tool_rounds(task)

    def test_4_nonelement_terminal_yields_no_phantom_deliverable(self):
        """#4 — an exhausted/budget/filter terminal has empty content; there
        must be ZERO deliverable segments and derive_content == '' (the channel)."""
        task = _task_nonelement_terminal()
        segs = assemble_segments(task)
        deliverables = [s for s in segs if s['type'] == SEG_TEXT and s.get('deliverable')]
        assert deliverables == []            # no phantom answer
        assert derive_content(segs) == ''    # byte-identical to the empty channel
        assert derive_content(segs) == task['content']
        # The tool rounds still round-trip so history is intact for replay.
        assert derive_tool_rounds(segs) == _merge_tool_rounds(task)


# ═══════════════════════════════════════════════════════════
#  deliverable rule independence
# ═══════════════════════════════════════════════════════════

class TestDeliverableRule:
    def test_deliverable_rule_is_position_based(self):
        """Classification must NOT depend on _discard_pretool_prose having run.

        Simulate the WORST case: the reset was 'skipped' so a round's
        assistantContent equals a stale copy of the terminal content. The
        narration segment (from assistantContent) must STILL be deliverable=False
        and the terminal segment deliverable=True — because assembly reads the
        two sources by position, not by whether they were zeroed.
        """
        task = {
            'id': 'g' * 32, 'convId': 'c' * 32,
            'content': 'THE ANSWER',
            'thinking': '',
            'toolRounds': [
                _round(1, 0, 'tc_1', 'read_files', '{}', 'body',
                       assistant_content='scaffolding narration'),
            ],
        }
        segs = assemble_segments(task)
        narration = [s for s in segs
                     if s['type'] == SEG_TEXT and not s.get('deliverable')]
        answer = [s for s in segs
                  if s['type'] == SEG_TEXT and s.get('deliverable')]
        assert [s['text'] for s in narration] == ['scaffolding narration']
        assert [s['text'] for s in answer] == ['THE ANSWER']

    def test_narration_excluded_from_deliverable_projection(self):
        """The headless-narrator fix (step 3) keys on this: derive_content
        must contain ZERO inter-round narration."""
        task = _task_multi_round()
        content = derive_content(assemble_segments(task))
        assert 'Let me search' not in content
        assert 'Now let me apply' not in content
        assert content == task['content']


# ═══════════════════════════════════════════════════════════
#  NC-1 — corrupt the derivation, prove the golden diverges
# ═══════════════════════════════════════════════════════════

class TestNeuterGuardsGolden:
    def test_NC1_misclassified_deliverable_breaks_content_golden(self):
        """If the terminal answer were flagged deliverable=False (the classic
        'discard ate the answer' bug), derive_content would drop it and the
        golden would FAIL. Prove the byte-identity assertion is load-bearing."""
        task = _task_multi_round()
        segs = assemble_segments(task)
        # Neuter: strip the deliverable flag off the terminal answer segment.
        for s in segs:
            if s.get('type') == SEG_TEXT and s.get('terminal'):
                s['deliverable'] = False
        broken = derive_content(segs)
        assert broken != task['content']
        assert broken == ''  # the answer vanished — exactly the regression we guard

    def test_NC1_narration_marked_deliverable_leaks_into_content(self):
        """The inverse corruption: flag narration deliverable=True → it leaks
        into the answer projection (the narrator bug at root). Golden diverges."""
        task = _task_multi_round()
        segs = assemble_segments(task)
        for s in segs:
            if s.get('type') == SEG_TEXT and s.get('llmRound') == 0:
                s['deliverable'] = True
        broken = derive_content(segs)
        assert broken != task['content']
        assert 'Let me search for the bug.' in broken

    def test_NC1_dropped_tool_round_breaks_merge_golden(self):
        """If assembly dropped a tool_use segment, derive_tool_rounds would no
        longer equal _merge_tool_rounds. Prove that assertion bites."""
        task = _task_multi_round()
        segs = [s for s in assemble_segments(task)
                if not (s.get('type') == SEG_TOOL_USE and s.get('name') == 'read_files')]
        assert derive_tool_rounds(segs) != _merge_tool_rounds(task)


# ═══════════════════════════════════════════════════════════
#  GROUND TRUTH — assert identity against an ACTUALLY-PRODUCED task dict
# ═══════════════════════════════════════════════════════════
#
# The fixtures above encode shapes I authored. This class kills that class of
# risk permanently: it drives the REAL orchestrator.run_task through a stubbed
# stream_llm_response (no LLM, no network) so the golden asserts byte-identity
# against a task dict the PRODUCTION pipeline built — including whatever fields
# tool_dispatch / _discard_pretool_prose / the persist path actually stamp.
# Mirrors the stub seam + real-conv recipe from tests/test_e2e_smoke.py and
# tests/test_abort_dangling_tool_round.py.

import json as _json

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/segment_model_unittest.db')


def _seed_conv(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    import time as _time
    messages = [
        {'role': 'user', 'content': 'search then answer', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},
    ]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(_time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'segment-groundtruth',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _cleanup_conv(conv_id):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    except Exception:
        pass


class TestGroundTruthRealRunTask:
    """Assert the derivation over a task dict PRODUCED by real run_task."""

    def _install_stub(self, monkeypatch):
        """Stub stream_llm_response at ALL binding sites (mgr/orch/llm_fb) so a
        multi-round turn runs: round 0 emits a web_search tool_call with
        pre-tool narration; round 1 streams the deliverable answer."""
        import lib.tasks_pkg.manager as mgr
        import lib.tasks_pkg.orchestrator as orch
        import lib.tasks_pkg.llm_fallback as llm_fb
        import lib.tasks_pkg.handlers.search as search_h
        import tofu_search

        def _stub(task, body, tag='', on_tool_call_ready=None):
            if not task.get('_gt_tool_done'):
                task['_gt_tool_done'] = True
                # Pre-tool narration streams as a DELTA (real _on_content path).
                narration = 'Let me search for that.'
                with task['content_lock']:
                    task['content'] += narration
                mgr.append_event(task, mgr.build_event(mgr.EventType.DELTA, content=narration))
                tc = {'id': 'call_gt_1', 'index': 0, 'type': 'function',
                      'function': {'name': 'web_search',
                                   'arguments': _json.dumps({'query': 'gt query'})}}
                if on_tool_call_ready:
                    try:
                        on_tool_call_ready(tc)
                    except Exception:
                        pass
                return ({'role': 'assistant', 'content': narration, 'tool_calls': [tc]},
                        'tool_calls',
                        {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})
            # Round 1: the deliverable answer, streamed word-by-word.
            answer = 'The answer is 42, per the search results.'
            for i, w in enumerate(answer.split(' ')):
                cd = w + (' ' if i < len(answer.split(' ')) - 1 else '')
                with task['content_lock']:
                    task['content'] += cd
                mgr.append_event(task, mgr.build_event(mgr.EventType.DELTA, content=cd))
            return ({'role': 'assistant', 'content': answer, 'tool_calls': []},
                    'stop',
                    {'prompt_tokens': 20, 'completion_tokens': 9, 'total_tokens': 29})

        def _stub_search(query, user_question='', freshness='', **kwargs):
            return [{'title': 'GT stub', 'snippet': 'deterministic', 'url': 'https://x.invalid', 'source': 'stub'}]

        for mod in (mgr, orch, llm_fb):
            if hasattr(mod, 'stream_llm_response'):
                monkeypatch.setattr(mod, 'stream_llm_response', _stub)
        monkeypatch.setattr(tofu_search, 'perform_web_search', _stub_search)
        monkeypatch.setattr(search_h, 'perform_web_search', _stub_search)

    def test_derivation_matches_produced_task_dict(self, monkeypatch):
        from lib.database import init_db
        from lib.tasks_pkg.manager import create_task, _merge_tool_rounds
        from lib.tasks_pkg.orchestrator import run_task

        # DB bootstrap is required for the real create_task/run_task/persist
        # path. Skip with a concrete reason (NOT a silent pass) when the local
        # environment can't bootstrap it — e.g. SQLAlchemy < 2.0 lacks
        # sa.Double used by _core_schema.py. Same discipline as the e2e
        # browser fixture: degrade to skip only on a genuine env gap.
        try:
            init_db()
        except Exception as e:
            pytest.skip(f'DB bootstrap unavailable in this env ({type(e).__name__}: {e}); '
                        'ground-truth run_task path needs a working DB')
        conv_id = 'cv-seg-gt-' + str(id(self))
        _cleanup_conv(conv_id)
        _seed_conv(conv_id)
        self._install_stub(monkeypatch)

        try:
            task = create_task(
                conv_id,
                [{'role': 'user', 'content': 'search then answer'}],
                {'model': 'test-model', 'projectEnabled': False,
                 'webSearchEnabled': True},
            )
            run_task(task)

            # run_task → persist_task_result populated task['segments'] (dark).
            segs = task.get('segments')
            assert segs is not None, 'run_task did not populate task["segments"]'

            # GROUND-TRUTH GOLDEN: derivations byte-identical to the produced dict.
            assert derive_content(segs) == task['content']
            assert derive_thinking(segs) == task['thinking']
            assert derive_tool_rounds(segs) == _merge_tool_rounds(task)

            # The produced turn actually ran a tool round + a deliverable answer.
            # NOTE: the real run_task legitimately POST-PROCESSES task['content']
            # (here it appends a Sources footer at orchestrator.py:456 because
            # the stubbed answer cited none of the pages it opened). So we assert
            # the streamed answer is PRESENT — not that content equals it exactly.
            # This is the ground-truth test earning its keep: a hand fixture
            # would never have surfaced the footer post-processing.
            assert 'The answer is 42, per the search results.' in task['content']
            tool_uses = [s for s in segs if s['type'] == SEG_TOOL_USE]
            assert any(s['name'] == 'web_search' for s in tool_uses), \
                f'expected a web_search tool_use segment, got {[s["name"] for s in tool_uses]}'
            # The pre-tool narration is a NON-deliverable segment (not leaked
            # into the answer) — the real _discard_pretool_prose ran.
            assert 'Let me search for that.' not in derive_content(segs)
            # Exactly ONE deliverable segment, and it captures the FULL terminal
            # content verbatim (answer + whatever the pipeline appended) — i.e.
            # derive_content is byte-identical to task['content'] (already asserted
            # above) and the deliverable carries the streamed answer.
            deliverables = [s for s in segs if s['type'] == SEG_TEXT and s.get('deliverable')]
            assert len(deliverables) == 1
            assert deliverables[0]['text'] == task['content']
            assert 'The answer is 42, per the search results.' in deliverables[0]['text']
        finally:
            _cleanup_conv(conv_id)


# ═══════════════════════════════════════════════════════════
#  STEP 4 — compaction preserves segment identity fields (invariant #2)
# ═══════════════════════════════════════════════════════════

class TestCompactionSegmentIdentity:
    """Invariant #2: when compaction truncates a tool RESULT, the tool_use
    segment's id/name/input + result.status must survive; only result.content
    shrinks. Segments are re-assembled from toolRounds at each persist, so a
    segment built from an already-truncated round reflects the shrunk content
    while identity fields are copied verbatim — by construction."""

    def test_truncated_result_keeps_identity_shrinks_content(self):
        full = 'X' * 5000
        # Round AFTER compaction truncated its result content (the durable
        # placeholder compaction writes — see _layer1.py).
        truncated = '[compacted — was 5000 chars]'
        task = {
            'id': 'k' * 32, 'convId': 'c' * 32,
            'content': 'done', 'thinking': '',
            'toolRounds': [
                _round(1, 0, 'tc_keep', 'read_files', '{"path":"big.py"}',
                       truncated, assistant_content='Reading the big file.'),
            ],
        }
        segs = assemble_segments(task)
        tu = next(s for s in segs if s['type'] == SEG_TOOL_USE)
        # Identity fields preserved verbatim.
        assert tu['id'] == 'tc_keep'
        assert tu['name'] == 'read_files'
        assert tu['input'] == '{"path":"big.py"}'
        assert tu['result']['status'] == 'done'
        # Content is the truncated placeholder, not the full 5000 chars.
        assert tu['result']['content'] == truncated
        assert full not in _json.dumps(segs)
        # And the reconstructor still rebuilds a valid tool_call from it.
        from lib.tasks_pkg.segments import reconstruct_tool_messages_from_segments
        msgs = reconstruct_tool_messages_from_segments(segs)
        assert msgs is not None
        assert msgs[0]['tool_calls'][0]['id'] == 'tc_keep'
        assert msgs[1]['content'] == truncated


# ═══════════════════════════════════════════════════════════
#  STEP 2 — thin/rehydrate round-trip (pure, no DB)
# ═══════════════════════════════════════════════════════════

class TestThinRehydrateRoundTrip:
    """segments_to_json (strip _round) → rehydrate_segments (re-zip) is lossless
    GIVEN the co-persisted tool_rounds. Pure — no DB, just the JSON boundary."""

    @pytest.mark.parametrize('make', ALL_TASKS, ids=[f.__name__ for f in ALL_TASKS])
    def test_thin_form_carries_no_round_mirror(self, make):
        segs = assemble_segments(make())
        thin = segments_to_json(segs)
        assert all('_round' not in s for s in thin), \
            'thin form must not embed the _round mirror (it duplicates tool_rounds)'

    @pytest.mark.parametrize('make', ALL_TASKS, ids=[f.__name__ for f in ALL_TASKS])
    def test_rehydrate_restores_byte_identical_tool_rounds(self, make):
        task = make()
        segs = assemble_segments(task)
        # Simulate the persist boundary: JSON-encode the thin form, decode,
        # rehydrate against the co-persisted tool_rounds.
        thin = segments_to_json(segs)
        wire = _json.dumps(thin, ensure_ascii=False)
        restored_thin = _json.loads(wire)
        rehydrated = rehydrate_segments(restored_thin, _merge_tool_rounds(task))
        # derive_tool_rounds over the rehydrated list == the pipeline's merge.
        assert derive_tool_rounds(rehydrated) == _merge_tool_rounds(task)
        # The non-tool projections survive the JSON round-trip too.
        assert derive_content(rehydrated) == task['content']
        assert derive_thinking(rehydrated) == task['thinking']

    def test_json_survives_cjk_and_nested_result(self):
        """Unicode/CJK in prose + nested result dict survive JSON encode/decode."""
        task = {
            'id': 'z' * 32, 'convId': 'c' * 32,
            'content': '答案是四十二。\n\n参考资料：https://例え.jp',
            'thinking': '需要先检索',
            'toolRounds': [
                _round(1, 0, 'tc_cjk', 'web_search', '{"query":"你好"}',
                       '检索结果：CAD 环形采样', assistant_content='让我搜索一下。'),
            ],
        }
        segs = assemble_segments(task)
        wire = _json.dumps(segments_to_json(segs), ensure_ascii=False)
        rehydrated = rehydrate_segments(_json.loads(wire), _merge_tool_rounds(task))
        assert derive_content(rehydrated) == task['content']
        assert '答案是四十二' in derive_content(rehydrated)
        assert derive_tool_rounds(rehydrated) == _merge_tool_rounds(task)

    def test_empty_content_terminal_survives_round_trip(self):
        """#4 shape through the wire: no phantom deliverable after rehydrate."""
        task = _task_nonelement_terminal()
        segs = assemble_segments(task)
        wire = _json.dumps(segments_to_json(segs), ensure_ascii=False)
        rehydrated = rehydrate_segments(_json.loads(wire), _merge_tool_rounds(task))
        assert derive_content(rehydrated) == ''
        assert [s for s in rehydrated if s.get('type') == SEG_TEXT and s.get('deliverable')] == []
        assert derive_tool_rounds(rehydrated) == _merge_tool_rounds(task)


# ═══════════════════════════════════════════════════════════
#  STEP 2 — DB round-trip: re-READ segments from the persisted row
# ═══════════════════════════════════════════════════════════

class TestSegmentsDBRoundTrip:
    """Drive real run_task, then re-READ the persisted segments from BOTH the
    task_results.segments column AND the conversation messages dict — proving
    JSON serialization survives the DB boundary and rehydrates byte-identically
    to what was assembled in memory. A pure in-memory assert would miss a
    serialization bug; this is the step-2 acceptance gate held to the
    ground-truth bar."""

    def test_persisted_segments_reread_and_rehydrate(self, monkeypatch):
        from lib.database import init_db, DOMAIN_CHAT, get_thread_db
        from lib.tasks_pkg.manager import create_task, _merge_tool_rounds
        from lib.tasks_pkg.orchestrator import run_task

        try:
            init_db()
        except Exception as e:
            pytest.skip(f'DB bootstrap unavailable in this env ({type(e).__name__}: {e}); '
                        'segments DB round-trip needs a working DB')

        conv_id = 'cv-seg-rt-' + str(id(self))
        _cleanup_conv(conv_id)
        _seed_conv(conv_id)
        # Reuse the ground-truth stub (multi-round: web_search → answer).
        TestGroundTruthRealRunTask._install_stub(self, monkeypatch)

        try:
            task = create_task(
                conv_id,
                [{'role': 'user', 'content': 'search then answer'}],
                {'model': 'test-model', 'projectEnabled': False,
                 'webSearchEnabled': True},
            )
            run_task(task)

            in_memory = task.get('segments')
            assert in_memory, 'run_task did not populate task["segments"]'
            expected_tr = _merge_tool_rounds(task)

            db = get_thread_db(DOMAIN_CHAT)

            # ── (A) task_results.segments column ──
            row = db.execute(
                'SELECT segments, tool_rounds FROM task_results WHERE task_id=?',
                (task['id'],)
            ).fetchone()
            assert row is not None, 'task_results row not written'
            col_segments = row['segments'] if not isinstance(row, tuple) else row[0]
            assert col_segments, 'segments column is empty — persist did not write it'
            thin_from_db = _json.loads(col_segments)
            # The persisted form is THIN (no _round mirror — it would duplicate
            # the tool_rounds column / conversation toolRounds).
            assert all('_round' not in s for s in thin_from_db)
            # Rehydrate against the co-persisted tool_rounds → byte-identical.
            rehydrated = rehydrate_segments(thin_from_db, expected_tr)
            assert derive_content(rehydrated) == task['content']
            assert derive_thinking(rehydrated) == task['thinking']
            assert derive_tool_rounds(rehydrated) == expected_tr

            # ── (B) conversation messages dict ──
            crow = db.execute(
                'SELECT messages FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()
            messages = _json.loads((crow['messages'] if not isinstance(crow, tuple) else crow[0]) or '[]')
            asst = [m for m in messages if m.get('role') == 'assistant']
            assert asst, 'no assistant message persisted'
            msg_segments = asst[-1].get('segments')
            assert msg_segments, 'segments not written onto the conversation message'
            assert all('_round' not in s for s in msg_segments)
            msg_tr = asst[-1].get('toolRounds') or []
            rehydrated_msg = rehydrate_segments(msg_segments, msg_tr)
            # The message's derive_content matches the persisted message content.
            assert derive_content(rehydrated_msg) == asst[-1].get('content')
            assert derive_tool_rounds(rehydrated_msg) == msg_tr
        finally:
            _cleanup_conv(conv_id)


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])

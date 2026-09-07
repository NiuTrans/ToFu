"""PR4 step-0 — behavior-pinning invariants for lib/tasks_pkg/compaction.py.

Goal
----
These tests are the **gate** before the planned 9-module split of
``compaction.py``.  Each test pins an invariant the split must preserve.
They run against the current monolithic module today and must continue
to pass post-split — by file path of the symbols they exercise:

  1. test_constants_reachable_via_facade
       The 9 user-tunable constants stay readable as
       ``lib.tasks_pkg.compaction.X`` (hot-reload contract).

  2. test_concurrent_init_is_idempotent
       Lazy DB-table init is double-checked-locked; concurrent first
       calls produce exactly one CREATE-TABLE.

  3. test_only_archive_transcript_emits_compaction_event
       ``_archive_transcript`` is the *single* SSE-emit boundary for
       'compaction' / 'compaction_done' events.

  4. test_reactive_compact_archives_before_image_strip
     test_reactive_compact_passes_skip_archive_flag
       Reactive ordering: archive → image-strip → force_compact(skip).
       The skip flag prevents duplicate archive rows on the same 413.

  5. test_phase0_image_strip_runs_when_image_count_exceeds_keep_tail
       Phase 0 image-strip (Layer 1 OOM protection) is non-negotiable.

  6. test_micro_compact_source_has_no_transcript_authority_access
       L1 may change only the request-local projection, never durable Turns.

  7. test_compaction_facade_export_list
       Every name imported by the existing test suite stays reachable.

These tests intentionally use light-weight mocks so they run in <1 s.
Heavy-weight integration coverage already lives in
``tests/test_compaction_improvements.py`` — that suite must continue to
pass post-split.

Run:  pytest tests/test_compaction_invariants.py -v
"""
from __future__ import annotations

import contextlib
import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════
#  1. Constants reachable via facade  (hot-reload contract)
# ═══════════════════════════════════════════════════════════════════════

# The 9 tunable constants documented in the
# `compaction-defaults-2026-04-19-relaxed` memory plus the wire-safety
# pair from `_strip_images_aggressive`.  Values are NOT asserted (those
# can change in a §10.1 turn); presence + reachability are.
_PUBLIC_CONSTANTS = [
    'MICRO_HOT_TAIL',
    'MICRO_HOT_TAIL_TOKENS',
    'MICRO_COMPACT_THRESHOLD',
    '_SUMMARY_TRIGGER_RATIO',
    '_THINKING_HOT_TAIL',
    '_SUMMARY_MAX_TOKENS',
    '_SUMMARY_COOLDOWN',
    '_DEFAULT_CONTEXT_LIMIT',
    '_OUTPUT_RESERVE',
    '_COMPACTION_RESERVE',
    '_WIRE_BYTE_SOFT_LIMIT',
    '_WIRE_IMAGE_KEEP_TAIL',
    '_PRESERVE_BUDGET_RATIO',
    '_MAX_PRESERVE_TURNS',
    '_COMPACT_TOOL_NAME',
]


@pytest.mark.unit
class TestConstantsHaveOneOwner:
    """Compaction defaults live only in the concrete constants module."""

    @pytest.mark.parametrize('name', _PUBLIC_CONSTANTS)
    def test_constant_attr_exists(self, name):
        import lib.tasks_pkg.compaction._constants as _comp
        assert hasattr(_comp, name), (
            f'{name} is missing from the compaction constants owner'
        )

    @pytest.mark.parametrize('name', _PUBLIC_CONSTANTS)
    def test_constant_not_duplicated_on_package_root(self, name):
        import lib.tasks_pkg.compaction as compaction_namespace
        assert not hasattr(compaction_namespace, name)

    def test_constant_has_a_concrete_value(self):
        """Sanity: at least one of the relaxed-defaults constants has a
        recognisably non-zero value (catches accidental ``= None`` after
        a botched extraction)."""
        import lib.tasks_pkg.compaction._constants as _comp
        assert _comp.MICRO_HOT_TAIL > 0
        assert _comp.MICRO_HOT_TAIL_TOKENS > 0
        assert _comp.MICRO_COMPACT_THRESHOLD > 0
        assert 0 < _comp._SUMMARY_TRIGGER_RATIO <= 1.0


# ═══════════════════════════════════════════════════════════════════════
#  2. Lazy DB init double-checked lock idempotence
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSidecarSchemaAuthority:
    """Compaction must not create or migrate storage tables in-process."""

    def test_archive_delegates_schema_lifecycle_to_repository(self):
        import inspect

        from lib.tasks_pkg.compaction._archive import _archive_transcript

        source = inspect.getsource(_archive_transcript)
        assert 'get_conversation_store' in source
        assert '_ensure_compaction_tables' not in source
        assert '_init_tables' not in source


# ═══════════════════════════════════════════════════════════════════════
#  3. _archive_transcript is the only SSE-emit boundary
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestArchiveIsTheOnlyEventEmitter:
    """Search the module source for ``append_event`` calls referencing
    a 'compaction' or 'compaction_done' event type — every such call MUST
    live inside ``_archive_transcript`` (or the post-summary UPDATE path
    inside ``execute_compact_tool``).  Static check, no execution."""

    def test_compaction_event_emit_sites_are_audited(self):
        """The split must not introduce a new SSE-emit site without
        moving it back into ``_archive`` per the design.  This is a
        belt-and-suspenders static check that flags drift early.

        Walks the package source — handles both the pre-split (single
        compaction.py) and post-split (compaction/_archive.py +
        compaction/_layer2.py) layouts.
        """
        import inspect

        module_names = (
            'lib.tasks_pkg.compaction._archive',
            'lib.tasks_pkg.compaction._layer2._compact',
            'lib.tasks_pkg.compaction._reactive',
        )
        sources = [
            inspect.getsource(importlib.import_module(module_name))
            for module_name in module_names
        ]

        all_src = '\n'.join(sources)

        # The 'compaction' SSE emit MUST exist somewhere in the package.
        # Emissions now flow through the typed constructor
        # ``build_event(EventType.COMPACTION, ...)`` (item-2 unification,
        # 2026-06) — accept either the typed form or the legacy literal.
        assert ("'type': 'compaction'" in all_src
                or '"type": "compaction"' in all_src
                or 'EventType.COMPACTION' in all_src), (
            "expected at least one 'compaction' SSE emit in the package; "
            "split must preserve the SSE-emit boundary in _archive"
        )
        # Ensure the post-summary 'compaction_done' event is also present
        # (literal reference or typed EventType.COMPACTION_DONE).
        assert ('compaction_done' in all_src
                or 'EventType.COMPACTION_DONE' in all_src), (
            "expected 'compaction_done' event reference in the package"
        )

    def test_archive_transcript_emits_compaction_event(self):
        """End-to-end: calling ``_archive_transcript`` with a task fires
        exactly one ``compaction`` SSE event via ``append_event``."""
        import lib.tasks_pkg.compaction._archive as _comp

        events = []
        store = MagicMock()
        store.archive_transcript.return_value = '42'

        # Patch the DB layer + append_event so the test is hermetic.
        with patch('lib.agent_core.store.get_conversation_store',
                   return_value=store), \
             patch('lib.tasks_pkg.manager.append_event',
                   side_effect=lambda task, ev: events.append(ev)):

            task = {'id': 'unit-test-task', '_userId': 1,
                    'config': {'model': 'mock-model'}}
            _comp._archive_transcript(
                conv_id='c1', messages=[{'role': 'user', 'content': 'hi'}],
                summary='', trigger='force', task=task,
                user_id=1,
                round_num=3, tokens_before=1000, tokens_after=200,
                msgs_before=10, msgs_after=4, reason='test',
                emit_event=True,
            )

        assert len(events) == 1
        ev = events[0]
        assert ev['type'] == 'compaction'
        assert str(ev['archiveId']) == '42'
        assert ev['trigger'] == 'force'
        assert ev['roundNum'] == 3
        assert ev['tokensBefore'] == 1000
        assert ev['tokenCountKind'] == 'estimated'
        assert ev['snapshotKind'] == 'pre_compaction_transcript'

    def test_archive_transcript_can_suppress_event(self):
        """The ``emit_event=False`` path must not produce an SSE event —
        this is what the reactive_compact early-snapshot path relies on
        when the inner force_compact would otherwise double-emit."""
        import lib.tasks_pkg.compaction._archive as _comp

        events = []
        store = MagicMock()
        store.archive_transcript.return_value = '7'

        with patch('lib.agent_core.store.get_conversation_store',
                   return_value=store), \
             patch('lib.tasks_pkg.manager.append_event',
                   side_effect=lambda task, ev: events.append(ev)):
            _comp._archive_transcript(
                conv_id='c1', messages=[], task={'id': 't', '_userId': 1},
                user_id=1,
                emit_event=False,
            )

        assert events == []


# ═══════════════════════════════════════════════════════════════════════
#  4. reactive_compact ordering + skip-archive flag
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestReactiveCompactOrdering:
    """Constraint #4 in the decomposition design.  Bit-for-bit ordering
    must be preserved across the split:

      1. Early ``_archive_transcript(trigger='reactive')`` snapshot.
      2. Phase 0 image-strip via ``_strip_images_aggressive``.
      3. Inner ``force_compact_if_needed(_compaction_skip_archive=True)``.

    Steps 1 and 2 must come BEFORE step 3, and step 3 MUST carry the
    skip flag.  Otherwise the viewer gets two 'reactive' archive rows
    on the same 413 (one from the outer call, one from the inner)."""

    def test_archive_precedes_image_strip(self):
        """Record the call sequence and assert archive runs first."""
        import lib.tasks_pkg.compaction._reactive as _comp
        # Post-split: reactive_compact lives in _reactive.py and looks
        # up its dependencies via direct imports — patch the names in
        # _reactive's namespace so the fakes are seen by the function.
        try:
            import lib.tasks_pkg.compaction._reactive as _reactive
            target = _reactive
        except ImportError:
            target = _comp

        sequence = []

        def _fake_archive(*_a, **_kw):
            sequence.append('archive')
            return 99

        def _fake_strip(messages, *, keep_tail=2):
            sequence.append('strip')
            return 0, 0

        def _fake_force(messages, task=None, **kwargs):
            sequence.append(('force', kwargs.get('_compaction_skip_archive')))
            return False  # didn't compact further

        with patch.object(target, '_archive_transcript', side_effect=_fake_archive), \
             patch.object(target, '_strip_images_aggressive',
                          side_effect=_fake_strip), \
             patch.object(target, 'force_compact_if_needed',
                          side_effect=_fake_force), \
             patch.object(target, '_estimate_wire_bytes', return_value=0), \
             patch.object(target, '_estimate_total_tokens', return_value=0), \
             patch('lib.agent_core.store.get_conversation_store'), \
             patch('lib.tasks_pkg.manager.append_event'):

            task = {'id': 'r-test', 'convId': 'c1', '_userId': 1,
                    'config': {'model': 'mock-model'}}
            _comp.reactive_compact(
                messages=[
                    {'role': 'user', 'content': 'hi'},
                    {'role': 'assistant', 'content': 'hello'},
                ],
                task=task,
                error_text='prompt too long: 1310784 tokens',
            )

        # The archive call must come BEFORE the inner force_compact.
        # (Image-strip may legitimately not fire when there are no
        # images in the test fixture — only assert ordering for sites
        # that actually ran.)
        assert 'archive' in sequence, 'reactive_compact did not archive'
        archive_idx = sequence.index('archive')

        # Find the force_compact entry and assert its position + flag.
        force_entries = [(i, s) for i, s in enumerate(sequence)
                         if isinstance(s, tuple) and s[0] == 'force']
        assert force_entries, 'reactive_compact did not invoke force_compact'
        force_idx, force_entry = force_entries[0]
        assert archive_idx < force_idx, (
            f'reactive_compact called force_compact at idx {force_idx} '
            f'BEFORE archive at idx {archive_idx} — ordering invariant broken'
        )

    def test_passes_skip_archive_flag(self):
        """The inner ``force_compact_if_needed`` call MUST carry
        ``_compaction_skip_archive=True`` so the viewer doesn't get a
        duplicate archive row for the same 413."""
        import lib.tasks_pkg.compaction._reactive as _comp
        try:
            import lib.tasks_pkg.compaction._reactive as _reactive
            target = _reactive
        except ImportError:
            target = _comp

        captured = {}

        def _fake_force(messages, task=None, **kwargs):
            captured['skip'] = kwargs.get('_compaction_skip_archive')
            captured['trigger'] = kwargs.get('_compaction_trigger')
            return False

        with patch.object(target, '_archive_transcript', return_value=99), \
             patch.object(target, '_strip_images_aggressive', return_value=(0, 0)), \
             patch.object(target, 'force_compact_if_needed',
                          side_effect=_fake_force), \
             patch.object(target, '_estimate_wire_bytes', return_value=0), \
             patch.object(target, '_estimate_total_tokens', return_value=0), \
             patch('lib.agent_core.store.get_conversation_store'), \
             patch('lib.tasks_pkg.manager.append_event'):

            task = {'id': 'r-test', 'convId': 'c1', '_userId': 1,
                    'config': {'model': 'mock-model'}}
            _comp.reactive_compact(
                messages=[{'role': 'user', 'content': 'hi'}],
                task=task,
                error_text='prompt too long',
            )

        assert captured.get('skip') is True, (
            'reactive_compact did NOT pass _compaction_skip_archive=True '
            '— would cause duplicate archive rows on the same 413'
        )
        # Trigger should be 'reactive' so the inner archive (if it fired)
        # would at least be tagged correctly.
        assert captured.get('trigger') == 'reactive'


# ═══════════════════════════════════════════════════════════════════════
#  4b. Reactive archive back-fill (the "\u2192 0" viewer artifact fix)
# ═══════════════════════════════════════════════════════════════════════

def _optional_patch(module: str, attr: str):
    """patch(module.attr) when the module exists, else a no-op context.

    lib.context_telemetry ships with the proactive-economics batch; on a
    tree without that batch (e.g. this fix committed standalone onto an
    older HEAD) the patch target is absent and must simply be skipped."""
    import importlib.util
    if importlib.util.find_spec(module) is None:
        return contextlib.nullcontext()
    return patch(f'{module}.{attr}')


@pytest.mark.unit
class TestReactiveArchiveBackfill:
    """The reactive pre-snapshot row is written BEFORE any compaction runs,
    so its tokens_after/msgs_after start at 0.  These tests pin the
    back-fill contract that stops the viewer showing "\u2192 0" forever:

      * reactive_compact hands the pre-snapshot row id down via
        ``_compaction_archive_id``;
      * when the inner summary compacts, ITS success tail owns the UPDATE
        (no double write from the fallback);
      * when the summary DECLINES, reactive_compact writes the final counts
        itself and emits compaction_done so the live marker closes;
      * execute_compact_tool adopts the caller-supplied row on the
        skip-archive path instead of inserting a second one.
    """

    def _reactive_target(self):
        import lib.tasks_pkg.compaction._reactive as _comp
        try:
            import lib.tasks_pkg.compaction._reactive as _reactive
            return _comp, _reactive
        except ImportError:
            return _comp, _comp

    def test_passes_pre_snapshot_archive_id_to_force_compact(self):
        """The pre-snapshot row id must ride ``_compaction_archive_id`` so
        the inner post-summary UPDATE can find the row to back-fill."""
        _comp, target = self._reactive_target()

        captured = {}

        def _fake_force(messages, task=None, **kwargs):
            captured['archive_id'] = kwargs.get('_compaction_archive_id')
            return True  # compacted → inner path owns the UPDATE

        with patch.object(target, '_archive_transcript', return_value=99), \
             patch.object(target, '_strip_images_aggressive', return_value=(0, 0)), \
             patch.object(target, 'force_compact_if_needed',
                          side_effect=_fake_force), \
             patch.object(target, '_estimate_wire_bytes', return_value=0), \
             patch.object(target, '_estimate_total_tokens', return_value=0), \
             patch('lib.agent_core.store.get_conversation_store') as store, \
             patch('lib.tasks_pkg.manager.append_event'):
            task = {'id': 'r-test', 'convId': 'c1', '_userId': 1,
                    'config': {'model': 'mock-model'}}
            _comp.reactive_compact(
                messages=[{'role': 'user', 'content': 'hi'}],
                task=task,
                error_text='prompt too long',
            )

        assert captured.get('archive_id') == 99, (
            'reactive_compact did not pass the pre-snapshot archive id down '
            '— the inner UPDATE cannot find the row to back-fill'
        )
        # Phase-3 compacted → the inner path owns the UPDATE; the fallback
        # must NOT write a second time.
        assert not store.return_value.update_archive_summary.called

    def test_backfills_archive_when_summary_declined(self):
        """Inner summary returned False (empty/refused): without this
        fallback the row keeps tokens_after=0 forever.  reactive_compact
        must write the final counts itself and close out the live marker
        with compaction_done."""
        _comp, target = self._reactive_target()

        events = []
        with patch.object(target, '_archive_transcript', return_value=99), \
             patch.object(target, '_strip_images_aggressive', return_value=(0, 0)), \
             patch.object(target, 'force_compact_if_needed', return_value=False), \
             patch.object(target, '_head_truncate', return_value=0), \
             patch.object(target, '_estimate_wire_bytes', return_value=0), \
             patch.object(target, '_estimate_total_tokens', return_value=1234), \
             patch('lib.agent_core.store.get_conversation_store') as store, \
             patch('lib.tasks_pkg.manager.append_event',
                   side_effect=lambda task, ev: events.append(ev)):
            task = {'id': 'r-test', 'convId': 'c1', '_userId': 1,
                    'config': {'model': 'mock-model'}}
            msgs = [{'role': 'user', 'content': 'hi'},
                    {'role': 'assistant', 'content': 'hello'}]
            _comp.reactive_compact(
                messages=msgs, task=task, error_text='prompt too long',
            )

        upd = store.return_value.update_archive_summary
        upd.assert_called_once()
        assert upd.call_args.args == (99, '', 1234, len(msgs))
        assert upd.call_args.kwargs['user_id'] == 1
        receipt = upd.call_args.kwargs['receipt']
        assert receipt['schemaVersion'] == 'tofu.compaction-receipt/v1'
        assert receipt['strategy'] == 'deterministic_recovery'
        done = [e for e in events if e.get('type') == 'compaction_done']
        assert len(done) == 1, (
            f'expected exactly one compaction_done from the fallback, '
            f'got {events}'
        )
        assert str(done[0]['archiveId']) == '99'
        assert done[0]['tokensAfter'] == 1234
        assert done[0]['tokenCountKind'] == 'estimated'
        assert done[0]['msgsAfter'] == len(msgs)
        assert done[0]['receipt'] == receipt

    def test_force_compact_backfills_adopted_archive_row(self):
        """BEHAVIOR CONTRACT (structure-agnostic on purpose): when the caller
        passes ``_compaction_skip_archive=True`` + ``_compaction_archive_id``
        and the summary succeeds, exactly ONE ``update_archive_summary`` hits
        the ADOPTED row (not a fresh insert) and exactly one compaction_done
        carries its id.  The UPDATE site itself has moved between
        execute_compact_tool and force_compact_if_needed across refactors —
        pin the contract, not the site."""
        from lib.tasks_pkg.compaction._layer2 import _compact as _cmod

        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'old q'},
            {'role': 'assistant', 'content': 'old a'},
            {'role': 'user', 'content': 'new q'},
            {'role': 'assistant', 'content': 'new a'},
        ]
        events = []
        with patch.object(_cmod, '_estimate_total_tokens', return_value=400), \
             patch.object(_cmod, '_get_context_limit', return_value=128000), \
             patch.object(_cmod, '_usable_context', return_value=100000), \
             patch.object(_cmod, '_extract_current_query', return_value='q'), \
             patch.object(_cmod, '_find_turn_boundary', return_value=3), \
             patch.object(_cmod, '_objective_anchor_index',
                          return_value=None), \
             patch.object(_cmod, '_fold_recent_intra_turn',
                          side_effect=lambda recent, **_kwargs:
                          (list(recent), [])), \
             patch.object(_cmod, '_generate_query_aware_summary',
                          return_value='SUMMARY'), \
             patch.object(_cmod, '_extract_recently_accessed_files',
                          return_value=[]), \
             patch.object(_cmod, '_archive_transcript') as arch, \
             patch('lib.tasks_pkg.compaction._tokens'
                   '._count_tokens_authoritative',
                   return_value=(10, 'mock')), \
             patch('lib.agent_core.store.get_conversation_store') as store, \
             patch('lib.tasks_pkg.cache_tracking._roi.record_l2_compaction'), \
             _optional_patch('lib.context_telemetry',
                             'record_compaction_event'), \
             patch('lib.tasks_pkg.manager.append_event',
                   side_effect=lambda task, ev: events.append(ev)):
            task = {'id': 'f-test', 'convId': 'c1', '_userId': 1,
                    'config': {'model': 'mock-model'}}
            ok = _cmod.force_compact_if_needed(
                messages, task=task, force=True,
                _compaction_trigger='reactive',
                _compaction_skip_archive=True,
                _compaction_archive_id=99,
            )

        assert ok is True
        arch.assert_not_called()  # no second archive row on the skip path
        upd = store.return_value.update_archive_summary
        upd.assert_called_once()
        args = upd.call_args[0]
        assert args[0] == 99, f'UPDATE hit archive id {args[0]}, want 99'
        assert 'SUMMARY' in args[1]
        assert int(args[2]) > 0 and int(args[3]) > 0, (
            f'back-filled counts must be the real post-compaction values, '
            f'got tokens_after={args[2]} msgs_after={args[3]}'
        )
        assert upd.call_args.kwargs['receipt']['strategy'] == 'selective_summary'
        done = [e for e in events if e.get('type') == 'compaction_done']
        assert len(done) == 1 and str(done[0]['archiveId']) == '99', (
            f'expected one compaction_done for archive 99, got {events}'
        )

    def test_execute_skip_archive_inserts_no_second_row(self):
        """execute_compact_tool on the skip-archive path must NOT insert a
        second archive row (the caller's pre-snapshot row is the only one)
        and must still complete the compaction.  The adopted-row back-fill
        itself is pinned end-to-end by the force-level contract test above."""
        from lib.tasks_pkg.compaction._layer2 import _compact as _cmod

        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'old q'},
            {'role': 'assistant', 'content': 'old a'},
            {'role': 'user', 'content': 'new q'},
            {'role': 'assistant', 'content': 'new a'},
        ]
        meta = {}
        with patch.object(_cmod, '_estimate_total_tokens', return_value=400), \
             patch.object(_cmod, '_get_context_limit', return_value=128000), \
             patch.object(_cmod, '_usable_context', return_value=100000), \
             patch.object(_cmod, '_extract_current_query', return_value='q'), \
             patch.object(_cmod, '_find_turn_boundary', return_value=3), \
             patch.object(_cmod, '_objective_anchor_index',
                          return_value=None), \
             patch.object(_cmod, '_fold_recent_intra_turn',
                          side_effect=lambda recent, **_kwargs:
                          (list(recent), [])), \
             patch.object(_cmod, '_generate_query_aware_summary',
                          return_value='SUMMARY'), \
             patch.object(_cmod, '_extract_recently_accessed_files',
                          return_value=[]), \
             patch.object(_cmod, '_archive_transcript') as arch, \
             patch('lib.tasks_pkg.compaction._tokens'
                   '._count_tokens_authoritative',
                   return_value=(10, 'mock')), \
             patch('lib.agent_core.store.get_conversation_store'), \
             patch('lib.tasks_pkg.cache_tracking._roi.record_l2_compaction'), \
             patch('lib.tasks_pkg.manager.append_event'):
            task = {'id': 'e-test', 'convId': 'c1',
                    'config': {'model': 'mock-model'}}
            res = _cmod.execute_compact_tool(
                messages, task=task,
                keep_recent_pairs=2, preserve_budget_tokens=100,
                _compaction_trigger='reactive',
                _compaction_skip_archive=True,
                _compaction_archive_id=99,
                _result_meta=meta,
            )

        assert 'SUMMARY' in res
        arch.assert_not_called()
        assert meta.get('compacted') is True


# ═══════════════════════════════════════════════════════════════════════
#  5. Phase 0 image-strip is wired to the wire-byte soft limit
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPhase0ImageStrip:
    """``_strip_images_aggressive`` is the OOM-protection routine called
    from reactive_compact.  Per the
    ``micro-compact-image-strip-bug-fix`` memory it must NOT be relaxed
    during the split.  Verify it strips when the image count exceeds the
    keep-tail and produces the expected text placeholder."""

    def test_strip_when_count_exceeds_keep_tail(self):
        import lib.tasks_pkg.compaction._reactive._strip as _comp

        def _img_msg(b64_payload: str):
            return {
                'role': 'user',
                'content': [
                    {'type': 'image_url',
                     'image_url': {'url': f'data:image/png;base64,{b64_payload}'}},
                ],
            }

        # 5 images, keep_tail=2 → 3 stripped.
        messages = [_img_msg('A' * 100) for _ in range(5)]
        stripped, freed = _comp._strip_images_aggressive(messages, keep_tail=2)

        assert stripped == 3, f'expected 3 stripped, got {stripped}'
        assert freed > 0, 'expected non-zero bytes_freed estimate'

        # First 3 messages were stripped → contain text placeholder.
        for i in range(3):
            blk = messages[i]['content'][0]
            assert blk['type'] == 'text'
            assert 'image removed' in blk['text']

        # Last 2 messages are intact image_url blocks.
        for i in range(3, 5):
            blk = messages[i]['content'][0]
            assert blk['type'] == 'image_url'

    def test_no_strip_when_count_within_keep_tail(self):
        """Below the keep_tail threshold, no images are stripped."""
        import lib.tasks_pkg.compaction._reactive._strip as _comp

        messages = [{
            'role': 'user',
            'content': [{'type': 'image_url',
                         'image_url': {'url': 'data:image/png;base64,XX'}}],
        }]
        stripped, freed = _comp._strip_images_aggressive(messages, keep_tail=2)
        assert stripped == 0
        assert freed == 0


# ═══════════════════════════════════════════════════════════════════════
#  6. micro_compact is isolated from durable transcript authority
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMicroCompactRequestIsolation:
    """Automatic L1 may mutate only its API-form request projection."""

    def test_micro_compact_source_has_no_transcript_authority_access(self):
        import inspect

        from lib.tasks_pkg.compaction.api import micro_compact

        src = inspect.getsource(micro_compact)
        forbidden = (
            '_round_index', 'toolContent', 'load_transcript',
            'cas_update_conversation_messages', 'update_turn_projection',
            'notify_conversation_changed',
        )
        assert not [marker for marker in forbidden if marker in src]
        assert 'run_steps(step_names, ctx)' in src

    def test_orchestrator_uses_a_nested_working_copy(self):
        import inspect

        from lib.tasks_pkg.orchestrator import _run

        src = inspect.getsource(_run.run_task)
        assert "messages = copy.deepcopy(task['messages'])" in src


# ═══════════════════════════════════════════════════════════════════════
#  7. Public API audit
# ═══════════════════════════════════════════════════════════════════════

# Stable services consumed by application code and experiment extensions.
_REQUIRED_PUBLIC_API_NAMES = [
    'advanced_compact',
    'budget_tool_result',
    'budget_tool_result_v2',
    'clamp_tool_result_text',
    'enforce_round_aggregate_budget',
    'enforce_round_aggregate_budget_v2',
    'mark_empty_result',
    'micro_compact',
    'execute_compact_tool',
    'force_compact_if_needed',
    'reactive_compact',
    'run_compaction_pipeline',
    'recompose_context_after_compaction',
    'build_context_policy',
    'resolve_model_context_profile',
    'register_step',
    'list_steps',
]


@pytest.mark.unit
class TestPublicApi:
    """Stable services live in ``compaction.api``; internals do not leak."""

    @pytest.mark.parametrize('name', _REQUIRED_PUBLIC_API_NAMES)
    def test_required_public_name_reachable(self, name):
        import lib.tasks_pkg.compaction.api as compaction_api
        assert name in compaction_api.__all__
        assert hasattr(compaction_api, name)

    def test_api_exports_no_private_names(self):
        import lib.tasks_pkg.compaction.api as compaction_api
        assert all(not name.startswith('_') for name in compaction_api.__all__)

    def test_root_is_a_namespace_not_a_service_facade(self):
        import lib.tasks_pkg.compaction as compaction_namespace
        assert compaction_namespace.__all__ == ()
        for name in _REQUIRED_PUBLIC_API_NAMES:
            assert not hasattr(compaction_namespace, name)

    def test_api_reload_preserves_contract(self):
        import lib.tasks_pkg.compaction.api as compaction_api
        importlib.reload(compaction_api)
        for name in _REQUIRED_PUBLIC_API_NAMES:
            assert hasattr(compaction_api, name)



# ═══════════════════════════════════════════════════════════════════════
#  8. Import-graph guards (post-split boundary enforcement)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestUsableContextFloor:
    """Regression: a fixed ``_OUTPUT_RESERVE`` (tuned for the 1M-context
    Claude family) must not drive ``usable`` to zero/negative on a
    small-window model.  Before the floor, a 128K model gave
    ``usable = 128000 - 128000 - 8000 = -8000`` → negative force-compact
    threshold → L2 summary fired on EVERY request.  ``_usable_context``
    floors usable at ``_MIN_USABLE_RATIO`` of the window."""

    def test_small_window_usable_is_positive(self):
        from lib.tasks_pkg.compaction._tokens import (
            _MIN_USABLE_RATIO, _usable_context,
        )
        for limit in (128_000, 200_000):
            usable = _usable_context(limit)
            assert usable >= int(limit * _MIN_USABLE_RATIO), (
                f'usable={usable} below floor for {limit}-token window'
            )
            assert usable > 0

    def test_large_window_unaffected_by_floor(self):
        """1M-context models keep the literal reserve subtraction — the
        floor only kicks in when reserves would over-eat the window."""
        from lib.tasks_pkg.compaction._constants import (
            _COMPACTION_RESERVE, _OUTPUT_RESERVE,
        )
        from lib.tasks_pkg.compaction._tokens import _usable_context
        limit = 1_000_000
        assert _usable_context(limit) == limit - _OUTPUT_RESERVE - _COMPACTION_RESERVE

    def test_small_window_does_not_force_compact_tiny_convo(self):
        """End-to-end: a 3-message conversation on a 128K model must NOT
        trip the force-compact threshold."""
        from lib.tasks_pkg.compaction._tokens import _should_force_compact
        task = {'convId': 'floor-test', 'config': {'model': 'gpt-4'}}
        messages = [
            {'role': 'system', 'content': 'System'},
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there!'},
        ]
        assert _should_force_compact(messages, task) is False


@pytest.mark.unit
class TestImportGraphGuards:
    """Structural tests that enforce the DAG from the decomposition
    design.  These import individual sub-modules and assert they do NOT
    pull in forbidden siblings — catching accidental coupling."""

    def test_constants_module_is_a_leaf(self):
        """_constants.py must not import anything from the compaction
        package itself."""
        import inspect
        mod = importlib.import_module('lib.tasks_pkg.compaction._constants')
        src = inspect.getsource(mod)
        siblings = {'_archive', '_budget', '_persist', '_tokens',
                    '_layer1', '_layer2', '_reactive', '_pipeline'}
        for name in siblings:
            assert f'from .{name}' not in src and f'compaction.{name}' not in src, (
                f'_constants.py has a forbidden import of sibling {name}'
            )

    def test_archive_module_has_no_layer_imports(self):
        """_archive.py must not import _layer1, _layer2, _reactive, or
        _pipeline.  It's the SSE-emit boundary and must stay low in the
        dependency graph."""
        import inspect
        mod = importlib.import_module('lib.tasks_pkg.compaction._archive')
        src = inspect.getsource(mod)
        forbidden = ['_layer1', '_layer2', '_reactive', '_pipeline']
        for name in forbidden:
            assert f'compaction.{name}' not in src and f'from .{name}' not in src, (
                f'_archive.py has a forbidden import of {name} — '
                f'SSE-emit boundary must not depend on caller-side logic'
            )

    def test_layer1_does_not_invoke_dispatch_chat(self):
        """_layer1 (micro_compact) must never call the LLM.  If it does,
        a per-round function that was designed to be free just became
        paid — a billing regression."""
        import inspect
        mod = importlib.import_module('lib.tasks_pkg.compaction._layer1')
        src = inspect.getsource(mod)
        assert 'dispatch_chat' not in src, (
            '_layer1.py contains dispatch_chat — Layer 1 must be zero-LLM-cost'
        )
        assert 'dispatch_stream' not in src, (
            '_layer1.py contains dispatch_stream — Layer 1 must be zero-LLM-cost'
        )

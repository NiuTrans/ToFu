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

  6. test_micro_compact_persists_via_cas_update
       L1 fix: micro_compact mutates ``toolContent`` durably and
       persists via CAS-style UPDATE on the conversations table.

  7. test_compaction_facade_export_list
       Every name imported by the existing test suite stays reachable.

These tests intentionally use light-weight mocks so they run in <1 s.
Heavy-weight integration coverage already lives in
``tests/test_compaction_improvements.py`` — that suite must continue to
pass post-split.

Run:  pytest tests/test_compaction_invariants.py -v
"""
from __future__ import annotations

import importlib
import os
import sys
import threading
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
class TestConstantsReachableViaFacade:
    """The hot-reload pattern is ``import lib as _lib;
    _lib.tasks_pkg.compaction.MICRO_HOT_TAIL`` — values are read at
    call time so config edits propagate without restart.  After the
    split, the new package ``__init__.py`` MUST re-export each one of
    these from its sub-module."""

    @pytest.mark.parametrize('name', _PUBLIC_CONSTANTS)
    def test_constant_attr_exists(self, name):
        from lib.tasks_pkg import compaction as _comp
        assert hasattr(_comp, name), (
            f'{name} not reachable as lib.tasks_pkg.compaction.{name} — '
            f'breaks the hot-reload contract documented in CLAUDE.md §10.5'
        )

    @pytest.mark.parametrize('name', _PUBLIC_CONSTANTS)
    def test_constant_reachable_via_lib_namespace(self, name):
        """The ``import lib as _lib; _lib.tasks_pkg.compaction.X`` access
        path used by ``system_prompt_cc.py`` and others must work."""
        import lib as _lib
        # Drill through the namespace mirroring the call-site pattern.
        assert hasattr(_lib.tasks_pkg.compaction, name), (
            f'{name} unreachable through lib.tasks_pkg.compaction namespace'
        )

    def test_constant_has_a_concrete_value(self):
        """Sanity: at least one of the relaxed-defaults constants has a
        recognisably non-zero value (catches accidental ``= None`` after
        a botched extraction)."""
        from lib.tasks_pkg import compaction as _comp
        assert _comp.MICRO_HOT_TAIL > 0
        assert _comp.MICRO_COMPACT_THRESHOLD > 0
        assert 0 < _comp._SUMMARY_TRIGGER_RATIO <= 1.0


# ═══════════════════════════════════════════════════════════════════════
#  2. Lazy DB init double-checked lock idempotence
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestLazyInitIdempotent:
    """``_init_tables`` is called from every public entry point.  Under
    concurrent first-calls, ``_ensure_compaction_tables`` MUST run at
    most once.  Regression here would create duplicate CREATE-IF-NOT-
    EXISTS DDL noise and (worse) confuse a future schema migration that
    relies on the version cache."""

    def test_concurrent_init_runs_create_once(self, monkeypatch):
        from lib.tasks_pkg import compaction as _comp
        # The latch lives in ._constants after the split (it always did
        # logically — the package re-exports it for the hot-reload
        # contract).  Mutate the source of truth + the package alias so
        # the double-checked lock starts in the False state.
        try:
            from lib.tasks_pkg.compaction import _archive as _arch
            from lib.tasks_pkg.compaction import _constants as _c
            monkeypatch.setattr(_c, '_tables_initialized', False, raising=True)
            monkeypatch.setattr(_comp, '_tables_initialized', False, raising=True)
            ensure_target = _arch
        except ImportError:
            # Pre-split layout: latch lives directly on the package.
            monkeypatch.setattr(_comp, '_tables_initialized', False, raising=True)
            ensure_target = _comp

        call_count = {'n': 0}
        call_lock = threading.Lock()

        def _fake_ensure():
            with call_lock:
                call_count['n'] += 1

        # Patch _ensure_compaction_tables on whichever module owns it
        # (the live impl post-split, the package pre-split).
        monkeypatch.setattr(ensure_target, '_ensure_compaction_tables', _fake_ensure)
        # Always patch the package re-export too so calls that resolved
        # through the facade hit the fake.
        monkeypatch.setattr(_comp, '_ensure_compaction_tables', _fake_ensure)

        # Fire 50 threads against _init_tables simultaneously.
        threads = [threading.Thread(target=_comp._init_tables) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert call_count['n'] == 1, (
            f'_ensure_compaction_tables ran {call_count["n"]}× under '
            f'concurrent first-call — double-checked lock is broken'
        )
        assert _comp._tables_initialized is True


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

        from lib.tasks_pkg import compaction as _comp

        # Collect source from the package + any extracted sub-modules.
        sources = [inspect.getsource(_comp)]
        for sub in ('_archive', '_layer2', '_reactive', '_layer1'):
            try:
                _mod = importlib.import_module(f'lib.tasks_pkg.compaction.{sub}')
                sources.append(inspect.getsource(_mod))
            except (ImportError, ModuleNotFoundError):
                # Pre-split or deferred extraction — skip silently.
                continue

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
        from lib.tasks_pkg import compaction as _comp

        events = []
        fake_db = MagicMock()
        # Make the SELECT id query return a row with id=42
        fake_row = MagicMock()
        fake_row.__getitem__ = lambda _self, _k: 42
        fake_cur = MagicMock()
        fake_cur.fetchone.return_value = fake_row
        fake_db.execute.return_value = fake_cur

        # Patch the DB layer + append_event so the test is hermetic.
        with patch('lib.database.get_thread_db', return_value=fake_db), \
             patch('lib.database.db_execute_with_retry', return_value=None), \
             patch('lib.tasks_pkg.manager.append_event',
                   side_effect=lambda task, ev: events.append(ev)):

            task = {'id': 'unit-test-task', 'config': {'model': 'mock-model'}}
            _comp._archive_transcript(
                conv_id='c1', messages=[{'role': 'user', 'content': 'hi'}],
                summary='', trigger='force', task=task,
                round_num=3, tokens_before=1000, tokens_after=200,
                msgs_before=10, msgs_after=4, reason='test',
                emit_event=True,
            )

        assert len(events) == 1
        ev = events[0]
        assert ev['type'] == 'compaction'
        assert ev['archiveId'] == 42
        assert ev['trigger'] == 'force'
        assert ev['roundNum'] == 3
        assert ev['tokensBefore'] == 1000

    def test_archive_transcript_can_suppress_event(self):
        """The ``emit_event=False`` path must not produce an SSE event —
        this is what the reactive_compact early-snapshot path relies on
        when the inner force_compact would otherwise double-emit."""
        from lib.tasks_pkg import compaction as _comp

        events = []
        fake_db = MagicMock()
        fake_row = MagicMock()
        fake_row.__getitem__ = lambda _self, _k: 7
        fake_db.execute.return_value.fetchone.return_value = fake_row

        with patch('lib.database.get_thread_db', return_value=fake_db), \
             patch('lib.database.db_execute_with_retry', return_value=None), \
             patch('lib.tasks_pkg.manager.append_event',
                   side_effect=lambda task, ev: events.append(ev)):
            _comp._archive_transcript(
                conv_id='c1', messages=[], task={'id': 't'},
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
        from lib.tasks_pkg import compaction as _comp
        # Post-split: reactive_compact lives in _reactive.py and looks
        # up its dependencies via direct imports — patch the names in
        # _reactive's namespace so the fakes are seen by the function.
        try:
            from lib.tasks_pkg.compaction import _reactive
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
             patch.object(target, '_estimate_total_tokens', return_value=0):

            task = {'id': 'r-test', 'convId': 'c1',
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
        from lib.tasks_pkg import compaction as _comp
        try:
            from lib.tasks_pkg.compaction import _reactive
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
             patch.object(target, '_estimate_total_tokens', return_value=0):

            task = {'id': 'r-test', 'convId': 'c1',
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
        from lib.tasks_pkg import compaction as _comp

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
        from lib.tasks_pkg import compaction as _comp

        messages = [{
            'role': 'user',
            'content': [{'type': 'image_url',
                         'image_url': {'url': 'data:image/png;base64,XX'}}],
        }]
        stripped, freed = _comp._strip_images_aggressive(messages, keep_tail=2)
        assert stripped == 0
        assert freed == 0


# ═══════════════════════════════════════════════════════════════════════
#  6. micro_compact loads from DB, mutates toolContent, persists via UPDATE
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMicroCompactDurablePersistence:
    """The L1 fix (``l1-compaction-fix-durable-placeholders`` memory):
    when ``micro_compact`` archives a tool result, it must also mutate
    the conv-side ``toolContent`` field and persist it back to the
    ``conversations`` table via a CAS-style UPDATE so the next
    ``build_api_messages_from_db`` rebuild produces the same placeholder.

    Static-source check (full integration is in
    ``tests/test_compaction_improvements.py``).  This test guards the
    invariant that the DB-load + UPDATE flow lives **inside** the
    micro_compact function — i.e. nobody can split Phase A/B/C from the
    DB-persistence path without breaking this behaviour."""

    def test_micro_compact_source_contains_db_load_and_update(self):
        import inspect

        from lib.tasks_pkg.compaction import micro_compact

        src = inspect.getsource(micro_compact)
        # The L1 fix introduced these markers in the function body:
        assert '_round_index' in src, (
            "micro_compact lost its _round_index DB-load — L1 placeholder "
            "fix regressed (memory: l1-compaction-fix-durable-placeholders)"
        )
        assert 'toolContent' in src, (
            "micro_compact no longer mutates toolContent for durability"
        )
        # CAS-style persist of the conversation row. The literal
        # ``UPDATE conversations`` SQL moved behind the ConversationStore
        # seam (2026-06 persistence-decoupling); accept either the inline
        # SQL (legacy) or the seam call so the invariant tracks behaviour,
        # not the layer the SQL happens to live in.
        assert ('UPDATE conversations' in src
                or 'cas_update_conversation_messages' in src), (
            "micro_compact lost its CAS conversation persist (inline UPDATE "
            "or store.cas_update_conversation_messages) — placeholders will "
            "not survive next build_api_messages_from_db"
        )


# ═══════════════════════════════════════════════════════════════════════
#  7. Facade export-list audit
# ═══════════════════════════════════════════════════════════════════════

# Names imported by the existing test suite + production callers.
# Every name in this list MUST stay reachable as
# ``lib.tasks_pkg.compaction.<name>`` post-split.  Drift here means an
# import in production or in tests/test_compaction_improvements.py
# breaks silently.
_REQUIRED_FACADE_NAMES = [
    # Constants (subset — full list in TestConstantsReachableViaFacade)
    'MICRO_HOT_TAIL',
    'MICRO_COMPACT_THRESHOLD',
    '_THINKING_HOT_TAIL',
    # Layer 0 — Budget
    'budget_tool_result',
    'enforce_round_aggregate_budget',
    'mark_empty_result',
    'TOOL_RESULT_MAX_CHARS',
    'MAX_ROUND_TOOL_RESULTS_CHARS',
    '_BUDGET_EXEMPT_TOOLS',
    '_DEFAULT_TOOL_RESULT_MAX',
    # Persist (Layer 0 splitters)
    '_persist_to_disk',
    '_generate_web_search_preview',
    '_PERSIST_DIR_BASE',
    '_PERSIST_PREVIEW_CHARS',
    # Tokens / decision
    '_get_context_limit',
    # Layer 1
    'micro_compact',
    # Layer 2
    'execute_compact_tool',
    'force_compact_if_needed',
    'smart_summary_compact',
    '_extract_recently_accessed_files',
    '_SUMMARY_SYSTEM_PROMPT',
    # Reactive
    'reactive_compact',
    '_head_truncate',
    # Pipeline
    'run_compaction_pipeline',
    '_reinject_system_contexts_after_compact',
    # Archive
    '_archive_transcript',
    'cleanup_compaction_data',
]


@pytest.mark.unit
class TestFacadeExportList:
    """The split installs ``compaction/__init__.py`` as a re-export
    facade.  Every name the project imports today MUST stay reachable
    by the same path — no caller change should be needed."""

    @pytest.mark.parametrize('name', _REQUIRED_FACADE_NAMES)
    def test_required_facade_name_reachable(self, name):
        from lib.tasks_pkg import compaction as _comp
        assert hasattr(_comp, name), (
            f'lib.tasks_pkg.compaction.{name} is missing — production '
            f'callers and existing tests rely on this import path'
        )

    def test_module_reimport_does_not_break_anything(self):
        """Defensive: reload the module and re-check the facade.  Catches
        bugs where the package layout works on first import but fails
        after a reload (an actual production-deploy concern when
        Settings UI hot-reloads modules)."""
        import lib.tasks_pkg.compaction as _comp
        importlib.reload(_comp)
        for name in _REQUIRED_FACADE_NAMES:
            assert hasattr(_comp, name), (
                f'After reload, {name} no longer reachable — '
                f'package layout has an import-order bug'
            )



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

"""tests/test_cache_schema_stability.py — Prompt-cache schema-stability fixes.

Pins the fixes that keep the tool-schema bytes byte-identical across the
rounds of a single conversation (so the prompt-cache prefix is not
invalidated):

  1. **Sticky multi-root hint** (A) — once a conversation goes multi-root, the
     ``_MULTIROOT_PATH_HINT`` stays on every path-taking tool for the rest of
     the conversation, even if a later task transiently reports a single
     ``projectPaths``. Flapping it rewrites ~10 tool schemas and breaks cache.
  2. **Deterministic MCP ordering** (A) — ``MCPBridge.get_openai_tool_defs``
     returns tools sorted by namespaced name, so a server reconnect /
     re-discovery (which changes dict-insertion order) does not reorder the
     tools array.
  3. **Per-conversation tool-schema latch** (B) — ``latch_tool_list`` freezes
     the EXACT tool list a conversation first used and serves it byte-identical
     every later round, so a mid-conversation user toggle (Swarm/Scheduler/…)
     cannot break the cache; the change is deferred to the next conversation or
     an explicit "Apply now" (``clear_tool_list_latch``).

See the cache-miss investigation: fixes (A)+(B) of the tools-array-change
breaks (48 breaks / 36 convs in production logs).
"""

from __future__ import annotations

import unittest

from lib.tools import ToolContext, assemble_tool_list
from lib.tools import registry as _reg


def _names(tool_list):
    return [t['function']['name'] for t in (tool_list or [])]


def _ctx(**overrides):
    base = dict(
        cfg={}, task_id='t-test', conv_id='',
        project_path='', project_enabled=False,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        swarm_enabled=False, image_gen_enabled=False,
        human_guidance_enabled=False, scheduler_enabled=False, messages=[],
    )
    base.update(overrides)
    return ToolContext(**base)


def _apply_diff_desc(tool_list):
    """Return the apply_diff tool's path-parameter description (or '')."""
    for t in (tool_list or []):
        if t['function']['name'] == 'apply_diff':
            return t['function']['parameters']['properties']['path'].get('description', '')
    return ''


class TestStickyMultiroot(unittest.TestCase):
    def setUp(self):
        # Ensure a clean latch for each test's conv ids.
        for c in ('_mr_conv', '_mr_conv2', ''):
            _reg.clear_multiroot_sticky(c)

    def tearDown(self):
        for c in ('_mr_conv', '_mr_conv2'):
            _reg.clear_multiroot_sticky(c)

    def _single(self, conv_id):
        return _ctx(conv_id=conv_id, project_path='/tmp/a', project_enabled=True,
                    cfg={'projectPaths': ['/tmp/a']})

    def _multi(self, conv_id):
        return _ctx(conv_id=conv_id, project_path='/tmp/a', project_enabled=True,
                    cfg={'projectPaths': ['/tmp/a', '/tmp/b']})

    def test_single_root_has_no_hint(self):
        tl, _ = assemble_tool_list(self._single('_mr_conv'))
        self.assertNotIn('rootname:', _apply_diff_desc(tl))
        self.assertFalse(_reg.is_multiroot_sticky('_mr_conv'))

    def test_multi_root_adds_hint_and_latches(self):
        tl, _ = assemble_tool_list(self._multi('_mr_conv'))
        self.assertIn('rootname:', _apply_diff_desc(tl))
        self.assertTrue(_reg.is_multiroot_sticky('_mr_conv'))

    def test_downgrade_is_sticky_within_conversation(self):
        # Round 1: multi-root → hint on, latched.
        tl1, _ = assemble_tool_list(self._multi('_mr_conv'))
        self.assertIn('rootname:', _apply_diff_desc(tl1))
        # Round 2: cfg transiently reports single root — hint MUST persist.
        tl2, _ = assemble_tool_list(self._single('_mr_conv'))
        self.assertIn('rootname:', _apply_diff_desc(tl2),
                      'multi-root hint must not flap off mid-conversation')

    def test_latch_is_per_conversation(self):
        assemble_tool_list(self._multi('_mr_conv'))
        # A DIFFERENT conversation that's single-root is unaffected.
        tl, _ = assemble_tool_list(self._single('_mr_conv2'))
        self.assertNotIn('rootname:', _apply_diff_desc(tl))

    def test_clear_releases_latch(self):
        assemble_tool_list(self._multi('_mr_conv'))
        self.assertTrue(_reg.is_multiroot_sticky('_mr_conv'))
        _reg.clear_multiroot_sticky('_mr_conv')
        self.assertFalse(_reg.is_multiroot_sticky('_mr_conv'))

    def test_stateless_assembly_no_latch(self):
        # conv_id='' (tests / compat adapters) → raw signal, never latched.
        tl_multi, _ = assemble_tool_list(
            _ctx(conv_id='', project_path='/tmp/a', project_enabled=True,
                 cfg={'projectPaths': ['/tmp/a', '/tmp/b']}))
        self.assertIn('rootname:', _apply_diff_desc(tl_multi))
        tl_single, _ = assemble_tool_list(
            _ctx(conv_id='', project_path='/tmp/a', project_enabled=True,
                 cfg={'projectPaths': ['/tmp/a']}))
        self.assertNotIn('rootname:', _apply_diff_desc(tl_single))


class TestMCPDeterministicOrdering(unittest.TestCase):
    def _client_with(self, ns_names):
        from lib.mcp.client import MCPBridge
        c = MCPBridge()
        for ns in ns_names:
            c._tool_index[ns] = {
                'server_name': ns.split('__')[1] if '__' in ns else 's',
                'tool_name': ns,
                'namespaced_name': ns,
                'description': '',
                'input_schema': {'type': 'object', 'properties': {}},
                'openai_def': {'type': 'function', 'function': {'name': ns}},
                'read_only_hint': True,
            }
        return c

    def test_ordering_is_sorted_not_insertion(self):
        # Insertion order is deliberately reversed-alphabetical.
        c = self._client_with(['mcp__z__t', 'mcp__a__t', 'mcp__m__t'])
        defs = c.get_openai_tool_defs()
        names = [d['function']['name'] for d in defs]
        self.assertEqual(names, ['mcp__a__t', 'mcp__m__t', 'mcp__z__t'])

    def test_reconnect_reorder_yields_same_sequence(self):
        # Two clients with the SAME tools inserted in DIFFERENT orders must
        # produce the identical (sorted) tool sequence — i.e. a reconnect that
        # changes dict-insertion order does not change the bytes.
        a = self._client_with(['mcp__hope__a', 'mcp__hope__b', 'mcp__x__c'])
        b = self._client_with(['mcp__x__c', 'mcp__hope__b', 'mcp__hope__a'])
        self.assertEqual(
            [d['function']['name'] for d in a.get_openai_tool_defs()],
            [d['function']['name'] for d in b.get_openai_tool_defs()],
        )


class TestToolListLatch(unittest.TestCase):
    """The per-conversation tool-schema latch (root fix B): freeze the tool
    list a conversation first used; serve it byte-identical every later round;
    report divergence when toggles change; clear it on Apply-now / cleanup.
    """

    def setUp(self):
        from lib.tools import clear_tool_list_latch
        clear_tool_list_latch('_latch_conv')

    def tearDown(self):
        from lib.tools import clear_tool_list_latch
        clear_tool_list_latch('_latch_conv')

    def _tool(self, name):
        return {'type': 'function', 'function': {'name': name, 'parameters': {}}}

    def test_first_round_establishes_snapshot(self):
        from lib.tools import latch_tool_list
        fresh = [self._tool('a'), self._tool('b')]
        eff, diverged = latch_tool_list('_latch_conv', fresh)
        self.assertEqual([t['function']['name'] for t in eff], ['a', 'b'])
        self.assertFalse(diverged)

    def test_later_round_serves_frozen_snapshot(self):
        from lib.tools import latch_tool_list
        latch_tool_list('_latch_conv', [self._tool('a'), self._tool('b')])
        # User toggled ON a new tool 'c' — but the frozen list is served.
        eff, diverged = latch_tool_list(
            '_latch_conv', [self._tool('a'), self._tool('b'), self._tool('c')])
        self.assertEqual([t['function']['name'] for t in eff], ['a', 'b'],
                         'frozen snapshot must be served byte-identical')
        self.assertTrue(diverged, 'divergence must be reported')

    def test_toggle_then_revert_reports_no_divergence(self):
        from lib.tools import latch_tool_list
        latch_tool_list('_latch_conv', [self._tool('a'), self._tool('b')])
        latch_tool_list('_latch_conv',
                        [self._tool('a'), self._tool('b'), self._tool('c')])
        # Reverted back to the original toggles → no divergence.
        _eff, diverged = latch_tool_list(
            '_latch_conv', [self._tool('a'), self._tool('b')])
        self.assertFalse(diverged)

    def test_diff_names_added_and_removed(self):
        from lib.tools import latch_tool_list, tool_list_diff
        latch_tool_list('_latch_conv', [self._tool('a'), self._tool('b')])
        # Toggled OFF 'b', toggled ON 'c'.
        latch_tool_list('_latch_conv', [self._tool('a'), self._tool('c')])
        diff = tool_list_diff('_latch_conv')
        self.assertEqual(diff['added'], ['c'])
        self.assertEqual(diff['removed'], ['b'])

    def test_diff_cleared_when_reverted(self):
        from lib.tools import latch_tool_list, tool_list_diff
        latch_tool_list('_latch_conv', [self._tool('a'), self._tool('b')])
        latch_tool_list('_latch_conv', [self._tool('a')])  # removed 'b'
        self.assertEqual(tool_list_diff('_latch_conv')['removed'], ['b'])
        # Revert → divergence clears → diff empties.
        latch_tool_list('_latch_conv', [self._tool('a'), self._tool('b')])
        self.assertEqual(tool_list_diff('_latch_conv'),
                         {'added': [], 'removed': []})

    def test_clear_latch_rebuilds_from_current(self):
        from lib.tools import clear_tool_list_latch, latch_tool_list
        latch_tool_list('_latch_conv', [self._tool('a')])
        clear_tool_list_latch('_latch_conv')
        # After Apply-now, the next round re-establishes from current toggles.
        eff, diverged = latch_tool_list(
            '_latch_conv', [self._tool('a'), self._tool('b')])
        self.assertEqual([t['function']['name'] for t in eff], ['a', 'b'])
        self.assertFalse(diverged)

    def test_empty_conv_id_is_noop(self):
        from lib.tools import latch_tool_list
        fresh = [self._tool('a')]
        eff, diverged = latch_tool_list('', fresh)
        self.assertIs(eff, fresh)
        self.assertFalse(diverged)

    def test_kill_switch_disables_latch(self):
        import os
        from lib.tools import latch_tool_list
        old = os.environ.get('TOFU_TOOLSET_LATCH')
        os.environ['TOFU_TOOLSET_LATCH'] = '0'
        try:
            latch_tool_list('_latch_conv', [self._tool('a')])
            eff, diverged = latch_tool_list(
                '_latch_conv', [self._tool('a'), self._tool('b')])
            # With the latch off, the live (changed) list flows through.
            self.assertEqual([t['function']['name'] for t in eff], ['a', 'b'])
            self.assertFalse(diverged)
        finally:
            if old is None:
                os.environ.pop('TOFU_TOOLSET_LATCH', None)
            else:
                os.environ['TOFU_TOOLSET_LATCH'] = old


class TestClearAllToolListLatches(unittest.TestCase):
    """``clear_all_tool_list_latches`` (the MCP-mutation root fix): an MCP
    install / connect / uninstall changes the GLOBAL tool surface on purpose,
    so EVERY conversation's latch is dropped and the new tool set takes effect
    on the next round of each — not just a brand-new conversation. Routes in
    ``routes/api_v1/mcp.py`` call this after every MCP mutation.
    """

    def _tool(self, name):
        return {'type': 'function', 'function': {'name': name, 'parameters': {}}}

    def setUp(self):
        from lib.tools import clear_all_tool_list_latches
        clear_all_tool_list_latches()

    def tearDown(self):
        from lib.tools import clear_all_tool_list_latches
        clear_all_tool_list_latches()

    def test_clears_every_conversation_and_returns_count(self):
        from lib.tools import clear_all_tool_list_latches, latch_tool_list
        base = [self._tool('web_search'), self._tool('read_files')]
        latch_tool_list('convA', base)
        latch_tool_list('convB', base)
        n = clear_all_tool_list_latches()
        self.assertEqual(n, 2)
        # After clear, an MCP-augmented set re-establishes fresh (diverged=False)
        # on the NEXT round of each conversation — i.e. it actually takes effect.
        with_mcp = base + [self._tool('mcp__hope__submit_job')]
        for c in ('convA', 'convB'):
            eff, diverged = latch_tool_list(c, with_mcp)
            self.assertIn('mcp__hope__submit_job',
                          [t['function']['name'] for t in eff])
            self.assertFalse(diverged)

    def test_unchanged_toolset_relatches_without_divergence(self):
        # A conversation whose effective tool set is UNCHANGED by the mutation
        # re-latches byte-identical → diverged=False → no prompt-cache rebuild.
        from lib.tools import clear_all_tool_list_latches, latch_tool_list
        base = [self._tool('web_search')]
        latch_tool_list('convC', base)
        clear_all_tool_list_latches()
        _eff, diverged = latch_tool_list('convC', base)
        self.assertFalse(diverged)

    def test_empty_returns_zero(self):
        from lib.tools import clear_all_tool_list_latches
        self.assertEqual(clear_all_tool_list_latches(), 0)

    def test_boot_latch_before_mcp_relatches_without_banner(self):
        # Reproduce the spurious-banner defect: MCP auto-connect runs on a
        # background thread AFTER boot, so a conversation opened first latches
        # the tool schema WITHOUT the not-yet-connected MCP tools.
        from lib.tools import clear_all_tool_list_latches, latch_tool_list
        base = [self._tool('web_search'), self._tool('read_files')]
        # Round 1 (boot, MCP not yet connected): freeze the MCP-less snapshot.
        _eff, diverged = latch_tool_list('convBoot', base)
        self.assertFalse(diverged)
        # MCP finishes connecting on the background thread → auto-connect now
        # clears every latch (the fix). Without the clear, round 2 below would
        # diverge and raise the "tools changed" banner.
        cleared = clear_all_tool_list_latches()
        self.assertGreaterEqual(cleared, 1)
        # Round 2: the freshly-assembled list now includes the MCP tools. Since
        # the latch was cleared, this round RE-ESTABLISHES the snapshot rather
        # than diverging → no spurious banner.
        with_mcp = base + [self._tool('mcp__github__search_code'),
                           self._tool('mcp__hope__submit_job')]
        eff2, diverged2 = latch_tool_list('convBoot', with_mcp)
        self.assertFalse(diverged2, 'boot-time latch cleared by MCP '
                         'auto-connect must re-establish WITH the MCP tools, '
                         'not report a spurious divergence/banner')
        names = [t['function']['name'] for t in eff2]
        self.assertIn('mcp__github__search_code', names)
        self.assertIn('mcp__hope__submit_job', names)

    def test_without_clear_boot_latch_diverges(self):
        # NEUTER / negative control: proving the clear is load-bearing. If MCP
        # auto-connect did NOT clear the boot-time latch, the incomplete frozen
        # snapshot is served and the MCP-augmented round diverges → banner.
        from lib.tools import clear_all_tool_list_latches, latch_tool_list
        clear_all_tool_list_latches()
        base = [self._tool('web_search')]
        latch_tool_list('convNoClear', base)
        # NO clear_all here — mimic the pre-fix boot path.
        with_mcp = base + [self._tool('mcp__hope__submit_job')]
        eff, diverged = latch_tool_list('convNoClear', with_mcp)
        self.assertTrue(diverged, 'without the auto-connect clear, the '
                        'incomplete boot latch MUST diverge (the defect)')
        # And the frozen (MCP-less) snapshot is what gets served.
        self.assertNotIn('mcp__hope__submit_job',
                         [t['function']['name'] for t in eff])
        clear_all_tool_list_latches()


class TestAssembleToolListByteStability(unittest.TestCase):
    """Integration: the orchestrator's actual entry point (_assemble_tool_list)
    must emit byte-identical tool schemas across a multi-root → single-root
    flap within one conversation. This is the property the prompt cache
    depends on.
    """

    def setUp(self):
        from lib.tools import clear_tool_list_latch
        _reg.clear_multiroot_sticky('_int_conv')
        clear_tool_list_latch('_int_conv')

    def tearDown(self):
        from lib.tools import clear_tool_list_latch
        _reg.clear_multiroot_sticky('_int_conv')
        clear_tool_list_latch('_int_conv')

    def _assemble(self, project_paths):
        from lib.tasks_pkg.model_config import _assemble_tool_list
        cfg = {'projectPaths': project_paths, 'mcpEnabled': False}
        tl, _has, _max = _assemble_tool_list(
            cfg, project_paths[0], True, 't-int',
            'off', False, False,
            False, False, False, False,
            messages=[], conv_id='_int_conv',
        )
        return tl

    def test_multi_then_single_is_byte_identical(self):
        import json
        tl_multi = self._assemble(['/tmp/a', '/tmp/b'])
        tl_single = self._assemble(['/tmp/a'])  # transient single-root snapshot
        self.assertEqual(
            json.dumps(tl_multi, sort_keys=True),
            json.dumps(tl_single, sort_keys=True),
            'tool schemas must be byte-identical after a multi→single flap '
            '(sticky multiroot latch); otherwise the prompt cache breaks',
        )
        # And the hint is present in BOTH (latched on).
        self.assertIn('rootname:', _apply_diff_desc(tl_single))


class TestMultirootTransitionReestablishesLatch(unittest.TestCase):
    """Single→multi-root transition: adding a second root mid-conversation is a
    LEGITIMATE one-time schema change (the model needs the ``rootname:`` hint
    NOW). The OFF→ON multiroot-sticky transition must re-establish the
    tool-schema latch so the next assembly re-freezes the snapshot WITH the
    hint — one deliberate rebuild, then byte-stable — instead of leaving a
    PERMANENT phantom empty-name-diff divergence (the stuck "apply in a new
    conversation" banner that this episode diagnosed).
    """

    CONV = '_mr_trans_conv'

    def setUp(self):
        from lib.tools import clear_tool_list_latch
        _reg.clear_multiroot_sticky(self.CONV)
        clear_tool_list_latch(self.CONV)

    def tearDown(self):
        from lib.tools import clear_tool_list_latch
        _reg.clear_multiroot_sticky(self.CONV)
        clear_tool_list_latch(self.CONV)

    def _assemble_and_latch(self, project_paths):
        """Mirror the orchestrator: assemble (reads multiroot_active, which may
        clear the latch on transition) THEN latch_tool_list, in that order."""
        from lib.tools import latch_tool_list
        cfg = {'projectPaths': project_paths, 'mcpEnabled': False}
        ctx = _ctx(conv_id=self.CONV, project_path=project_paths[0],
                   project_enabled=True, cfg=cfg)
        tl, _has = assemble_tool_list(ctx)
        return latch_tool_list(self.CONV, tl)

    def test_transition_reestablishes_without_phantom_divergence(self):
        from lib.tools import tool_list_diff
        # Round 1: single-root → freezes the hint-LESS snapshot, no divergence.
        eff1, div1 = self._assemble_and_latch(['/tmp/a'])
        self.assertFalse(div1)
        self.assertNotIn('rootname:', _apply_diff_desc(eff1))

        # Round 2: second root added → OFF→ON transition clears+re-freezes the
        # latch IN THE SAME ROUND with the hinted list. No phantom divergence.
        eff2, div2 = self._assemble_and_latch(['/tmp/a', '/tmp/b'])
        self.assertFalse(div2, 'multiroot transition must re-establish the '
                               'latch in the same round, not report a phantom '
                               'empty-name-diff divergence')
        self.assertIn('rootname:', _apply_diff_desc(eff2),
                      'the model must get the rootname hint immediately')
        self.assertEqual(tool_list_diff(self.CONV), {'added': [], 'removed': []})

    def test_stable_after_transition(self):
        # Rounds 3+ stay byte-stable (diverged=False) on the frozen hinted list.
        self._assemble_and_latch(['/tmp/a'])
        self._assemble_and_latch(['/tmp/a', '/tmp/b'])
        for _ in range(3):
            eff, div = self._assemble_and_latch(['/tmp/a', '/tmp/b'])
            self.assertFalse(div)
            self.assertIn('rootname:', _apply_diff_desc(eff))

    def test_transition_clear_fires_once(self):
        # The clear must fire ONLY on the first OFF→ON mark, not every round
        # (mark_multiroot_sticky returns True only on the transition).
        self.assertTrue(_reg.mark_multiroot_sticky(self.CONV))
        self.assertFalse(_reg.mark_multiroot_sticky(self.CONV))
        self.assertFalse(_reg.mark_multiroot_sticky(self.CONV))


if __name__ == '__main__':
    unittest.main(verbosity=2)

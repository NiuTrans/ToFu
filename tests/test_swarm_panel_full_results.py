"""Swarm panel must show COMPLETE sub-agent results — no silent truncation.

The panel is a DEBUGGING surface. Four independent caps compounded to make it
lie about what a sub-agent actually produced:

  * ``agent.py`` ``preview[:300]`` / ``error[:300]``  — per-tool-call SSE frame
  * ``master.py`` ``[:_PREVIEW_CHARS]`` (1200)        — the DURABLE snapshot
  * ``streaming_swarm_panel.js`` ``.slice(0, 1200)``  — the rendered card
  * ``streaming_swarm_panel.js`` ``.slice(0, 200)``   — the error card

The 300-char cut is the one visible in the reported screenshot: a fetch_url
result stops mid-path at ``/mnt/your-fs``.

Acceptance (owner-stated): a 5,000-char sub-agent answer and a 2,000-char
tool-result preview both render in full, with no ellipsis.

The shape asserted here is deliberately NOT "raise every constant":
  * the DURABLE snapshot carries the agent's full final answer (it is the
    authoritative terminal record the reloaded panel renders from);
  * the per-tool-call preview is persisted into ``tool_log`` so the durable
    path keeps it too, rather than it existing only on a transient frame;
  * the live SSE frame stays bounded for wire economy, but says so
    (``previewTruncated``) instead of silently clipping.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

from lib.swarm import master as sw_master  # noqa: E402


class TestSnapshotCarriesFullAnswer(unittest.TestCase):
    """The durable snapshot must not clip the agent's final answer."""

    def test_five_thousand_char_answer_survives_snapshot(self):
        import inspect
        src = inspect.getsource(
            sw_master.MasterOrchestrator._build_agent_snapshot)
        self.assertNotIn(
            '[:_PREVIEW_CHARS]', src,
            'the durable snapshot must carry the FULL final answer — the '
            'reloaded panel renders from it, so a slice here permanently '
            'destroys the tail of every sub-agent result')
        self.assertIn(
            "(result.final_answer or '')", src,
            'the snapshot must persist final_answer verbatim')

    def test_preview_chars_cap_is_retired(self):
        """The 1200-char snapshot cap must be gone entirely, not just unused."""
        from lib.swarm import snapshot as sw_snapshot
        self.assertFalse(
            hasattr(sw_snapshot, '_PREVIEW_CHARS'),
            'a live 1200-char cap on the durable snapshot contradicts the '
            'full-results contract — remove the constant so it cannot be '
            'silently reintroduced')


class TestToolCallPreviewNotClippedAt300(unittest.TestCase):
    """The 300-char cut visible in the screenshot must be gone."""

    def test_sse_preview_bound_is_not_300(self):
        import inspect
        from lib.swarm import agent as sw_agent
        agent_src = inspect.getsource(sw_agent)
        self.assertNotIn(
            "preview=preview[:300] if preview else ''", agent_src,
            'the 300-char tool-preview cut is the exact truncation reported '
            'in the panel screenshot (fetch_url note stops mid-path)')

    def test_two_thousand_char_tool_preview_fits_the_bound(self):
        from lib.swarm.agent import _SSE_TOOL_PREVIEW_CHARS
        self.assertGreaterEqual(
            _SSE_TOOL_PREVIEW_CHARS, 2000,
            'a 2,000-char tool-result preview must reach the panel intact')

    def test_tool_log_entries_can_carry_preview(self):
        """Durability: the preview must survive on the persisted tool_log.

        Otherwise the full text exists only on a transient SSE frame and a
        reloaded panel can never show it.
        """
        import inspect
        from lib.swarm import agent as sw_agent
        src = inspect.getsource(sw_agent)
        self.assertIn(
            "'preview'", src,
            'tool_log rows must carry the preview so the durable path keeps it')


class TestSnapshotTimelineKeepsPreview(unittest.TestCase):
    """The rebuilt timeline must forward preview/error, not drop them."""

    def test_timeline_forwards_preview_and_error(self):
        tools, calls = sw_master._snapshot_tool_timeline([
            {'round': 1, 'tool': 'fetch_url', 'args_brief': '3 URLs',
             'preview': 'P' * 2000, 'error': ''},
        ])
        self.assertEqual(tools, ['fetch_url'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            len(calls[0].get('preview') or ''), 2000,
            'the durable timeline row must carry the full tool preview — '
            'this is what a reloaded panel renders')

    def test_timeline_tolerates_rows_without_preview(self):
        """Legacy tool_log rows predate the preview field."""
        tools, calls = sw_master._snapshot_tool_timeline([
            {'round': 1, 'tool': 'grep_search', 'args_brief': 'x'},
        ])
        self.assertEqual(tools, ['grep_search'])
        self.assertEqual(calls[0]['preview'], '')


class TestFrontendSlicesRemoved(unittest.TestCase):
    """The two JS slices that clip the rendered card."""

    def _panel_src(self):
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.normpath(os.path.join(here, '..'))
        path = os.path.join(root, 'static', 'js', 'ui',
                            'streaming_swarm_panel.js')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_agent_preview_slice_removed(self):
        self.assertNotIn(
            '(a.preview || "").slice(0, 1200)', self._panel_src(),
            'the panel must render the complete agent answer')

    def test_error_preview_slice_removed(self):
        self.assertNotIn(
            'preview.slice(0, 200)', self._panel_src(),
            'a failed agent\'s error text is exactly what needs reading in '
            'full — 200 chars truncates the stack/cause')


if __name__ == '__main__':
    unittest.main(verbosity=2)

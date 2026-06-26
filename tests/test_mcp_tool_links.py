"""Tests for clickable MCP tool-call links.

The MCP tool-call line shows an opaque resource id (e.g. an Overleaf
``6a1e7…a668`` short-id) that users can't read or navigate. The backend
now attaches ``_mcpLinks`` = {label → href} so the frontend can wrap that
exact label in a hyperlink. These tests cover the URL cache layer
(``lib.mcp.project_names``) and the display layer
(``lib.tasks_pkg.tool_display._tool_display_mcp``).
"""

import unittest

from lib.mcp.project_names import (
    clear_cache,
    get_doc_url,
    get_project_url,
    ingest_tool_result,
)
from lib.tasks_pkg.tool_display import _tool_display_mcp


class OverleafLinkTest(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def test_project_url_synthesized_from_default_base(self):
        # Even with an empty cache, an overleaf project_id yields a link
        # synthesized from the public Overleaf base.
        pid = '6a1e782e9ba0ae3d7727a668'
        self.assertEqual(
            get_project_url(pid),
            f'https://www.overleaf.com/project/{pid}',
        )

    def test_non_hex_project_id_no_url(self):
        self.assertEqual(get_project_url('not-a-real-id'), '')
        self.assertEqual(get_project_url(''), '')

    def test_display_attaches_link_for_short_id(self):
        disp, extra = _tool_display_mcp(
            'mcp__overleaf__edit_file',
            {'project_id': '6a1e782e9ba0ae3d7727a668', 'file_path': 'acl_latex.tex'},
            't', '{}',
        )
        links = extra.get('_mcpLinks')
        self.assertTrue(links)
        # The link is keyed by the EXACT label rendered on the line.
        self.assertIn('@ ' + list(links)[0], disp)
        self.assertEqual(
            list(links.values())[0],
            'https://www.overleaf.com/project/6a1e782e9ba0ae3d7727a668',
        )

    def test_link_label_matches_cached_name(self):
        # After a create_project, both the displayed label AND the link key
        # become the human-readable name — they must stay consistent so the
        # frontend can find the substring to wrap.
        ingest_tool_result(
            'mcp__overleaf__create_project',
            {'name': 'My Paper'},
            'Created Overleaf project [My Paper]\n'
            '   project_id: 6a1e782e9ba0ae3d7727a668  (short: 6a1e7…a668)\n'
            '   Open: https://www.overleaf.com/project/6a1e782e9ba0ae3d7727a668',
        )
        disp, extra = _tool_display_mcp(
            'mcp__overleaf__edit_file',
            {'project_id': '6a1e782e9ba0ae3d7727a668', 'file_path': 'acl_latex.tex'},
            't', '{}',
        )
        links = extra['_mcpLinks']
        label = list(links)[0]
        self.assertIn(label, disp)  # label is a substring of the displayed line

    def test_self_hosted_base_learned_from_url(self):
        pid = 'aaaaaaaaaaaaaaaaaaaaaaaa'
        ingest_tool_result(
            'mcp__overleaf__edit_file',
            {'project_id': pid, 'file_path': 'x.tex'},
            f'Edited x.tex in project (aaaaa…aaaa) '
            f'https://overleaf.mycorp.com/project/{pid}',
        )
        self.assertEqual(
            get_project_url(pid),
            f'https://overleaf.mycorp.com/project/{pid}',
        )

    def test_write_tool_result_carries_harvestable_url(self):
        # Contract with overleaf-mcp: EVERY write tool's return message now
        # embeds the canonical project URL (via _project_tag), so the link
        # works on the FIRST edit — not only after a create_project. A
        # self-hosted base in that URL is learned for sibling projects.
        pid = '6a1e782e9ba0ae3d7727a668'
        self_hosted = f'https://overleaf.corp.example.com/project/{pid}'
        ingest_tool_result(
            'mcp__overleaf__edit_file',
            {'project_id': pid, 'file_path': 'acl_latex.tex'},
            f"✅ Edited 'acl_latex.tex' in project [Tofu] (6a1e7…a668) {self_hosted}",
        )
        self.assertEqual(get_project_url(pid), self_hosted)
        # Sibling project on the same deployment inherits the learned base
        # rather than the public default.
        sibling = 'bbbbbbbbbbbbbbbbbbbbbbbb'
        self.assertEqual(
            get_project_url(sibling),
            f'https://overleaf.corp.example.com/project/{sibling}',
        )
        _disp, extra = _tool_display_mcp(
            'mcp__overleaf__edit_file',
            {'project_id': pid, 'file_path': 'acl_latex.tex'}, 't', '{}',
        )
        self.assertEqual(list(extra['_mcpLinks'].values())[0], self_hosted)


class XuechengLinkTest(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def test_no_link_until_url_harvested(self):
        # No canonical Xuecheng base is assumed — a doc has no link until a
        # full URL is seen in a tool result.
        self.assertEqual(get_doc_url('2761323464'), '')
        _disp, extra = _tool_display_mcp(
            'mcp__xuecheng__read_doc', {'doc': '2761323464'}, 't', '{}',
        )
        self.assertFalse(extra.get('_mcpLinks'))

    def test_link_after_harvest(self):
        ingest_tool_result(
            'mcp__xuecheng__read_doc',
            {'doc': 'https://km.internal.example.com/collabpage/2761323464'},
            '{"ok": true, "title": "My Doc", '
            '"url": "https://km.internal.example.com/collabpage/2761323464"}',
        )
        self.assertEqual(
            get_doc_url('2761323464'),
            'https://km.internal.example.com/collabpage/2761323464',
        )
        disp, extra = _tool_display_mcp(
            'mcp__xuecheng__read_doc', {'doc': '2761323464'}, 't', '{}',
        )
        links = extra['_mcpLinks']
        self.assertEqual(
            links.get('My Doc'),
            'https://km.internal.example.com/collabpage/2761323464',
        )
        self.assertIn('My Doc', disp)


class NonLinkableToolTest(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def test_github_tool_no_mcp_links(self):
        # github tools have no resolvable URL via this mechanism.
        _disp, extra = _tool_display_mcp(
            'mcp__github__create_issue',
            {'owner': 'torvalds', 'repo': 'linux', 'title': 'bug'},
            't', '{}',
        )
        self.assertFalse(extra.get('_mcpLinks'))


if __name__ == '__main__':
    unittest.main()

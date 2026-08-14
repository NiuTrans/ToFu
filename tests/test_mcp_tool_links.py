"""Tests for clickable MCP tool-call links.

The MCP tool-call line shows an opaque resource id (e.g. an Overleaf
``6a1e7…a668`` short-id) that users can't read or navigate. The backend
now attaches ``_mcpLinks`` = {label → href} so the frontend can wrap that
exact label in a hyperlink. These tests cover the URL cache layer
(``lib.mcp.project_names``) and the display layer
(``lib.tasks_pkg.tool_display._tool_display_mcp``).
"""

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/tasks_pkg/tool_display.py', 'lib/x.py', 'static/y.js',
    'static/js/ui/tool_rounds.js',
}

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


class BatchCommitPathDisplayTest(unittest.TestCase):
    """batch_commit / batch_delete / push_files carry paths inside a
    ``files``/``paths`` list, invisible to the flat arg scan — so the title
    line used to degrade to ``main @ owner/repo`` with no paths shown."""

    def test_batch_commit_lists_every_path(self):
        args = {
            'owner': 'rangehow', 'repo': 'ToFu', 'branch': 'main',
            'message': 'x',
            'files': [
                {'path': 'lib/tasks_pkg/tool_display.py', 'content': '...'},
                {'path': 'static/js/ui/tool_rounds.js', 'content': '...'},
            ],
            'delete_paths': ['old/legacy.py'],
        }
        disp, extra = _tool_display_mcp(
            'mcp__github-batch__batch_commit', args, 't', '{}',
        )
        # Every path appears, one per line; deletes get a − marker.
        self.assertIn('lib/tasks_pkg/tool_display.py', disp)
        self.assertIn('static/js/ui/tool_rounds.js', disp)
        self.assertIn('− old/legacy.py', disp)
        self.assertIn('3 files', disp)
        self.assertIn('@ rangehow/ToFu', disp)
        # The multiline form is exposed as _display_query so it survives into
        # the SSE tool_start event (which prefers _display_query).
        self.assertEqual(extra.get('_display_query'), disp)
        # NC: the OLD behaviour showed the branch as the resource and no path.
        self.assertNotIn('main @ rangehow/ToFu', disp.split('\n')[0])

    def test_batch_delete_lists_paths(self):
        disp, _extra = _tool_display_mcp(
            'mcp__github-batch__batch_delete',
            {'owner': 'a', 'repo': 'b', 'branch': 'main', 'message': 'm',
             'paths': ['x.py', 'y.py']},
            't', '{}',
        )
        self.assertIn('− x.py', disp)
        self.assertIn('− y.py', disp)
        self.assertIn('@ a/b', disp)

    def test_official_push_files_lists_paths(self):
        disp, _extra = _tool_display_mcp(
            'mcp__github__push_files',
            {'owner': 'a', 'repo': 'b', 'branch': 'main', 'message': 'm',
             'files': [{'path': 'README.md', 'content': '# hi'}]},
            't', '{}',
        )
        self.assertIn('README.md', disp)
        self.assertIn('1 file', disp)

    def test_non_batch_mcp_call_unaffected(self):
        # A flat-arg call still routes through _mcp_arg_suffix unchanged.
        disp, _extra = _tool_display_mcp(
            'mcp__github__create_issue',
            {'owner': 'a', 'repo': 'b', 'title': 'Bug', 'issue_number': 5},
            't', '{}',
        )
        self.assertNotIn('\n', disp)
        self.assertIn('Bug', disp)


class PostBuildTitleTest(unittest.TestCase):
    """The persisted results-row title (handlers/mcp.py::_post_build) must use
    the SAME compose_mcp_display helper as the live tool-line — otherwise a
    batch_commit title regresses to the branch-only ``main @ owner/repo`` once
    the commit completes (dual-source drift)."""

    def _run_post_build(self, fn_name, fn_args, tool_content='ok'):
        """Drive the REAL _post_build closure without a live MCP bridge.

        Monkeypatches ``get_bridge`` (so handle_mcp_tool can build the
        closure) and stubs ``simple_call`` to capture the ``post_build``
        callback, then invokes it against a fresh ``meta`` dict. Returns the
        resulting ``meta``.
        """
        import lib.mcp as mcp_pkg
        import lib.tasks_pkg.handlers.mcp as mcp_handler

        class _FakeBridge:
            def get_tool_info(self, name):
                server, tool = name.replace('mcp__', '', 1).split('__', 1)
                return {'server_name': server, 'tool_name': tool}

        captured = {}

        def _fake_simple_call(task, fn, args, rn, round_entry, tc_id,
                              *, executor, source, module_tag, extra,
                              post_build=None, **_kw):
            captured['post_build'] = post_build
            return tc_id, 'ok', False

        orig_get_bridge = mcp_pkg.get_bridge
        orig_simple_call = mcp_handler.simple_call
        mcp_pkg.get_bridge = lambda: _FakeBridge()
        mcp_handler.simple_call = _fake_simple_call
        try:
            mcp_handler.handle_mcp_tool(
                {}, {}, fn_name, 't', fn_args, 1, {}, {}, None, False,
            )
            meta = {}
            captured['post_build'](meta, tool_content, fn_args)
            return meta
        finally:
            mcp_pkg.get_bridge = orig_get_bridge
            mcp_handler.simple_call = orig_simple_call

    def test_batch_commit_title_has_paths(self):
        meta = self._run_post_build(
            'mcp__github-batch__batch_commit',
            {'owner': 'rangehow', 'repo': 'ToFu', 'branch': 'main',
             'message': 'm',
             'files': [{'path': 'lib/x.py', 'content': '...'},
                       {'path': 'static/y.js', 'content': '...'}],
             'delete_paths': ['old/z.py']},
        )
        title = meta['title']
        self.assertIn('lib/x.py', title)
        self.assertIn('static/y.js', title)
        self.assertIn('− old/z.py', title)
        self.assertIn('3 files', title)
        # NC: the OLD (drifted) _post_build produced this branch-only title.
        self.assertNotEqual(title, 'github-batch/batch_commit — main @ rangehow/ToFu')

    def test_flat_arg_title_unchanged(self):
        meta = self._run_post_build(
            'mcp__github__create_issue',
            {'owner': 'a', 'repo': 'b', 'title': 'Bug', 'issue_number': 5},
        )
        self.assertEqual(meta['title'], 'github/create_issue — Bug @ a/b')
        self.assertEqual(meta['badge'], 'github')


if __name__ == '__main__':
    unittest.main()

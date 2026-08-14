"""tests/test_tool_changes.py — port-parity tests for lib.tool_changes.

Each test exercises a JS branch with a fixture matching the JS impl's
output. If a test fails, either the Python port diverged from JS or
the JS itself changed and the port needs to track it.
"""

from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {'scripts/build.sh', 'tests/test_x.py'}

import json
import unittest

from lib.tool_changes import (
    FileChange, _split_root, extract_file_changes,
    extract_file_changes_dicts,
)


class SplitRootTest(unittest.TestCase):

    def test_no_colon(self):
        self.assertEqual(_split_root('foo.py'), ('', 'foo.py'))

    def test_simple_root_prefix(self):
        self.assertEqual(_split_root('myproj:src/foo.py'),
                          ('myproj', 'src/foo.py'))

    def test_root_only(self):
        # "myproj:" → root, path defaults to "."
        self.assertEqual(_split_root('myproj:'),
                          ('myproj', '.'))

    def test_absolute_path_no_root(self):
        self.assertEqual(_split_root('/usr/local/bin/foo'),
                          ('', '/usr/local/bin/foo'))

    def test_home_relative_no_root(self):
        self.assertEqual(_split_root('~/foo.py'), ('', '~/foo.py'))

    def test_url_like_no_root(self):
        # Slash before colon → not a root prefix.
        self.assertEqual(_split_root('foo/bar:baz'),
                          ('', 'foo/bar:baz'))

    def test_empty_string(self):
        self.assertEqual(_split_root(''), ('', ''))

    def test_none(self):
        self.assertEqual(_split_root(None), ('', ''))

    def test_long_prefix_rejected(self):
        # 40+ chars before colon → not treated as root.
        long = 'a' * 50 + ':rest'
        self.assertEqual(_split_root(long), ('', long))

    def test_windows_drive_letter_treated_as_root(self):
        # JS uses the same logic — "C:" matches the prefix rules
        # because no slash precedes the colon. We mirror that.
        # (JS comment says "Don't match Windows drive letters" but the
        # regex doesn't actually exclude them; output matches JS.)
        out = _split_root('C:\\foo\\bar')
        self.assertEqual(out, ('C', '\\foo\\bar'))


class ExtractFileChangesTest(unittest.TestCase):

    def test_empty_rounds(self):
        self.assertEqual(extract_file_changes([]), [])
        self.assertEqual(extract_file_changes(None), [])

    def test_non_write_tool_ignored(self):
        rounds = [
            {'toolName': 'web_search', 'toolArgs': '{"q":"x"}',
             'results': [{'title': 'something'}]},
        ]
        self.assertEqual(extract_file_changes(rounds), [])

    def test_write_file_created(self):
        rounds = [{
            'toolName': 'write_file',
            'toolArgs': '{"path": "src/new.py", "content": "x"}',
            'results': [{'badge': 'Created', 'writeOk': True}],
        }]
        out = extract_file_changes(rounds)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].path, 'src/new.py')
        self.assertEqual(out[0].action, 'created')
        self.assertTrue(out[0].ok)
        self.assertEqual(out[0].root, '')

    def test_write_file_written(self):
        rounds = [{
            'toolName': 'write_file',
            'toolArgs': '{"path": "foo.py"}',
            'results': [{'badge': 'Modified', 'writeOk': True}],
        }]
        out = extract_file_changes(rounds)
        self.assertEqual(out[0].action, 'written')

    def test_write_file_failed(self):
        rounds = [{
            'toolName': 'write_file',
            'toolArgs': '{"path": "foo.py"}',
            'results': [{'badge': 'Modified', 'writeOk': False}],
        }]
        out = extract_file_changes(rounds)
        self.assertFalse(out[0].ok)

    def test_apply_diff_with_edits_array(self):
        rounds = [{
            'toolName': 'apply_diffs',
            'toolArgs': json.dumps({
                'edits': [
                    {'path': 'a.py', 'search': 's', 'replace': 'r'},
                    {'path': 'b.py', 'search': 's', 'replace': 'r'},
                ],
            }),
            'results': [{'writeOk': True}],
        }]
        out = extract_file_changes(rounds)
        self.assertEqual(len(out), 2)
        self.assertEqual({fc.path for fc in out}, {'a.py', 'b.py'})
        for fc in out:
            self.assertEqual(fc.action, 'patched')

    def test_apply_diff_single_path(self):
        rounds = [{
            'toolName': 'apply_diff',
            'toolArgs': '{"path": "foo.py", "search": "s", "replace": "r"}',
            'results': [{'writeOk': True}],
        }]
        out = extract_file_changes(rounds)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].path, 'foo.py')
        self.assertEqual(out[0].action, 'patched')

    def test_insert_contents(self):
        rounds = [{
            'toolName': 'insert_contents',
            'toolArgs': json.dumps({
                'edits': [{'path': 'foo.py', 'anchor': 'x', 'content': 'y'}],
            }),
            'results': [{'writeOk': True}],
        }]
        out = extract_file_changes(rounds)
        self.assertEqual(out[0].action, 'inserted')

    def test_run_command_with_file_changes(self):
        rounds = [{
            'toolName': 'run_command',
            'results': [{
                'fileChanges': [
                    {'path': 'src/foo.py', 'action': 'modified'},
                    {'path': 'src/bar.py', 'action': 'created'},
                ],
            }],
        }]
        out = extract_file_changes(rounds)
        self.assertEqual({fc.path for fc in out},
                          {'src/foo.py', 'src/bar.py'})

    def test_pending_writes_during_streaming(self):
        rounds = [{
            'toolName': 'write_file',
            'toolArgs': '{"path": "wip.py", "content": "x"}',
            'status': 'searching',
            # no 'results' yet
        }]
        out = extract_file_changes(rounds)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].pending)
        self.assertEqual(out[0].action, 'writing…')
        self.assertEqual(out[0].count, 0)

    def test_pending_apply_diff(self):
        rounds = [{
            'toolName': 'apply_diff',
            'toolArgs': '{"path": "wip.py"}',
            'status': 'searching',
        }]
        out = extract_file_changes(rounds)
        self.assertEqual(out[0].action, 'patching…')

    def test_dedup_same_root_path(self):
        # Two writes to same file → one entry, count=2, last action wins
        # except 'created' beats 'written'.
        rounds = [
            {'toolName': 'write_file',
             'toolArgs': '{"path": "foo.py"}',
             'results': [{'badge': 'Created', 'writeOk': True}]},
            {'toolName': 'write_file',
             'toolArgs': '{"path": "foo.py"}',
             'results': [{'badge': 'Modified', 'writeOk': True}]},
        ]
        out = extract_file_changes(rounds)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].count, 2)
        # 'created' must persist even after a subsequent 'written'.
        self.assertEqual(out[0].action, 'created')

    def test_dedup_failure_propagates(self):
        rounds = [
            {'toolName': 'write_file',
             'toolArgs': '{"path": "foo.py"}',
             'results': [{'badge': 'Modified', 'writeOk': True}]},
            {'toolName': 'write_file',
             'toolArgs': '{"path": "foo.py"}',
             'results': [{'badge': 'Modified', 'writeOk': False}]},
        ]
        out = extract_file_changes(rounds)
        # Once any write fails, ok is False.
        self.assertFalse(out[0].ok)

    def test_multi_root_keys_dont_collide(self):
        rounds = [
            {'toolName': 'write_file',
             'toolArgs': '{"path": "proj-a:src/foo.py"}',
             'results': [{'badge': 'Modified', 'writeOk': True}]},
            {'toolName': 'write_file',
             'toolArgs': '{"path": "proj-b:src/foo.py"}',
             'results': [{'badge': 'Modified', 'writeOk': True}]},
        ]
        out = extract_file_changes(rounds)
        self.assertEqual(len(out), 2)
        roots = sorted(fc.root for fc in out)
        self.assertEqual(roots, ['proj-a', 'proj-b'])
        for fc in out:
            self.assertEqual(fc.path, 'src/foo.py')

    def test_fallback_title_with_emoji_prefix(self):
        rounds = [{
            'toolName': 'write_file',
            # No toolArgs → falls through to title
            'results': [{'title': '✅ scripts/build.sh', 'writeOk': True}],
        }]
        out = extract_file_changes(rounds)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].path, 'scripts/build.sh')
        self.assertEqual(out[0].action, 'written')

    def test_malformed_args_falls_back_to_title(self):
        rounds = [{
            'toolName': 'write_file',
            'toolArgs': '{not valid json',
            'results': [{'title': '✅ recovered/path.py', 'writeOk': True}],
        }]
        out = extract_file_changes(rounds)
        self.assertEqual(out[0].path, 'recovered/path.py')

    def test_title_equal_to_tool_name_is_not_a_file(self):
        # build_project_tool_meta DEFAULTS meta['title'] to the bare tool name
        # when no builder overwrites it with a filename. A write round whose
        # toolArgs carries no usable path (e.g. a malformed / auto-repaired
        # apply_diff) must NOT surface a bogus file-change entry literally
        # named after the tool ("apply_diff"). Regression for the file-edit
        # display showing "apply_diff" as the modified file.
        rounds = [{
            'toolName': 'apply_diff',
            'toolArgs': '{"search": "x", "replace": "y"}',  # no path
            'results': [{'title': 'apply_diff', 'writeOk': True}],
        }]
        self.assertEqual(extract_file_changes(rounds), [])

        # Same guard for the other write tools.
        for tn in ('write_file', 'apply_diffs', 'insert_content',
                   'insert_contents'):
            rounds = [{
                'toolName': tn,
                'toolArgs': '{}',
                'results': [{'title': tn, 'writeOk': True}],
            }]
            self.assertEqual(extract_file_changes(rounds), [],
                             f'{tn}: title==tool name must not emit a file')

    def test_dict_args_supported(self):
        # Args may already be a dict (not a string) when the round dict
        # comes from the live task state rather than from JSON.
        rounds = [{
            'toolName': 'write_file',
            'toolArgs': {'path': 'foo.py'},
            'results': [{'badge': 'Created', 'writeOk': True}],
        }]
        out = extract_file_changes(rounds)
        self.assertEqual(out[0].action, 'created')

    def test_serialisable_dict_output(self):
        rounds = [{
            'toolName': 'write_file',
            'toolArgs': '{"path": "myproj:foo.py"}',
            'results': [{'badge': 'Created', 'writeOk': True}],
        }]
        out = extract_file_changes_dicts(rounds)
        self.assertEqual(len(out), 1)
        self.assertEqual(set(out[0].keys()),
                          {'path', 'action', 'ok', 'count',
                           'pending', 'root'})
        # Round-trip through json
        s = json.dumps(out)
        decoded = json.loads(s)
        self.assertEqual(decoded[0]['path'], 'foo.py')
        self.assertEqual(decoded[0]['root'], 'myproj')

    def test_complex_real_world(self):
        """A realistic mixed sequence: write + apply_diff + run_command +
        a streaming write that's still pending."""
        rounds = [
            {'toolName': 'write_file',
             'toolArgs': '{"path": "myapp:src/main.py"}',
             'results': [{'badge': 'Created', 'writeOk': True}]},
            {'toolName': 'apply_diffs',
             'toolArgs': json.dumps({
                 'edits': [
                     {'path': 'myapp:src/main.py'},
                     {'path': 'myapp:src/util.py'},
                 ],
             }),
             'results': [{'writeOk': True}]},
            {'toolName': 'run_command',
             'results': [{
                 'fileChanges': [
                     {'path': 'myapp:tests/test_x.py', 'action': 'modified'},
                 ],
             }]},
            {'toolName': 'write_file',
             'toolArgs': '{"path": "myapp:src/wip.py"}',
             'status': 'searching'},
        ]
        out = extract_file_changes(rounds)
        # Expect 4 distinct entries.
        self.assertEqual(len(out), 4)
        by_path = {fc.path: fc for fc in out}
        # main.py: created then patched. JS parity: 'created' is only
        # preserved against subsequent 'written' (in the write_file
        # branch), NOT against 'patched' from apply_diff. So the final
        # action becomes 'patched'.
        self.assertEqual(by_path['src/main.py'].action, 'patched')
        self.assertEqual(by_path['src/main.py'].count, 2)
        # util.py: patched
        self.assertEqual(by_path['src/util.py'].action, 'patched')
        # tests/test_x.py: modified
        self.assertEqual(by_path['tests/test_x.py'].action, 'modified')
        # wip.py: pending
        self.assertTrue(by_path['src/wip.py'].pending)
        self.assertEqual(by_path['src/wip.py'].action, 'writing…')
        # All in 'myapp' root
        for fc in out:
            self.assertEqual(fc.root, 'myapp')


if __name__ == '__main__':
    unittest.main()

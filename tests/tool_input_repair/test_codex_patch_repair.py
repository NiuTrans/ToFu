"""Codex freeform apply_patch → edit_file conversion (Proposal B1)."""

from __future__ import annotations

import pytest

from lib.tool_input_repair._codex_patch import (
    looks_like_codex_patch,
    parse_codex_patch,
)
from lib.tool_input_repair._classify import resolve_tool_name
from lib.tool_input_repair._ingest import ingest_tool_call


pytestmark = pytest.mark.unit


_SIMPLE_PATCH = """*** Begin Patch
*** Update File: src/app.py
@@
 def main():
-    old_call()
+    new_call()
     return 0
*** End Patch
"""

_MULTI_HUNK_PATCH = """*** Begin Patch
*** Update File: src/app.py
@@
-import os
+import sys
@@
 def main():
-    old_call()
+    new_call()
*** End Patch
"""

_MULTI_FILE_PATCH = """*** Begin Patch
*** Update File: a.py
@@
-x = 1
+x = 2
*** Update File: b.py
@@
-y = 1
+y = 2
*** End Patch
"""


class TestParser:
    def test_gate(self):
        assert looks_like_codex_patch(_SIMPLE_PATCH)
        assert looks_like_codex_patch('prefix text\n*** Begin Patch\n...')
        assert not looks_like_codex_patch('{"edits": []}')
        assert not looks_like_codex_patch(None)
        assert not looks_like_codex_patch('*** Begin Patch'.join(['x' * 500, '']))

    def test_simple_update(self):
        args, error = parse_codex_patch(_SIMPLE_PATCH)
        assert not error
        assert args['description'] == 'Update src/app.py'
        assert args['edits'] == [{
            'path': 'src/app.py',
            'operation': 'replace',
            'anchor': 'def main():\n    old_call()\n    return 0',
            'content': 'def main():\n    new_call()\n    return 0',
        }]

    def test_multiple_hunks_keep_order(self):
        args, error = parse_codex_patch(_MULTI_HUNK_PATCH)
        assert not error
        assert [edit['anchor'] for edit in args['edits']] == [
            'import os', 'def main():\n    old_call()']

    def test_multiple_files(self):
        args, error = parse_codex_patch(_MULTI_FILE_PATCH)
        assert not error
        assert args['description'] == 'Update 2 files via patch'
        assert [edit['path'] for edit in args['edits']] == ['a.py', 'b.py']

    def test_add_file_fails_with_write_file_guidance(self):
        patch = ('*** Begin Patch\n*** Add File: new.py\n+hello\n'
                 '*** End Patch\n')
        args, error = parse_codex_patch(patch)
        assert args is None
        assert 'write_file' in error

    def test_delete_file_fails_closed(self):
        patch = '*** Begin Patch\n*** Delete File: old.py\n*** End Patch\n'
        args, error = parse_codex_patch(patch)
        assert args is None
        assert 'not supported' in error

    def test_move_to_fails_closed(self):
        patch = ('*** Begin Patch\n*** Update File: a.py\n'
                 '*** Move to: b.py\n@@\n-x\n+y\n*** End Patch\n')
        args, error = parse_codex_patch(patch)
        assert args is None
        assert 'rename' in error

    def test_anchorless_insertion_fails_with_insert_guidance(self):
        patch = ('*** Begin Patch\n*** Update File: a.py\n@@\n'
                 '+brand_new_line\n*** End Patch\n')
        args, error = parse_codex_patch(patch)
        assert args is None
        assert 'insert_after' in error

    def test_truncated_patch_still_converts_complete_ops(self):
        patch = ('*** Begin Patch\n*** Update File: a.py\n@@\n'
                 '-x = 1\n+x = 2\n')
        args, error = parse_codex_patch(patch)
        assert not error
        assert args['edits'][0]['anchor'] == 'x = 1'

    def test_empty_patch_fails(self):
        args, error = parse_codex_patch('*** Begin Patch\n*** End Patch\n')
        assert args is None
        assert 'no convertible' in error

    def test_bare_lines_treated_as_context(self):
        patch = ('*** Begin Patch\n*** Update File: a.py\n@@\n'
                 'keep = True\n-x = 1\n+x = 2\n*** End Patch\n')
        args, error = parse_codex_patch(patch)
        assert not error
        assert args['edits'][0]['anchor'] == 'keep = True\nx = 1'


class TestNameRouting:
    def test_apply_patch_aliases_to_edit_file_when_visible(self):
        resolved, kind = resolve_tool_name(
            'apply_patch', known={'edit_file', 'read_files'})
        assert (resolved, kind) == ('edit_file', 'alias')

    def test_apply_patch_stays_unmapped_without_edit_file(self):
        resolved, kind = resolve_tool_name(
            'apply_patch', known={'apply_diff', 'read_files'})
        assert (resolved, kind) == ('apply_patch', None)


class TestIngestIntegration:
    def _call(self, arguments):
        return {'function': {'name': 'apply_patch', 'arguments': arguments}}

    def test_freeform_string_payload_converts(self):
        ingested = ingest_tool_call(
            self._call(_SIMPLE_PATCH),
            known_tools={'edit_file', 'read_files'}, emit_audit=False)
        assert ingested.ok, ingested.parse_error
        assert ingested.fn_name == 'edit_file'
        assert ingested.fn_args['edits'][0]['path'] == 'src/app.py'
        assert ('$', 'codex_apply_patch') in ingested.repair_log

    def test_wrapped_patch_key_converts(self):
        ingested = ingest_tool_call(
            self._call({'patch': _SIMPLE_PATCH}),
            known_tools={'edit_file', 'read_files'}, emit_audit=False)
        assert ingested.ok, ingested.parse_error
        assert ingested.fn_args['edits']

    def test_add_file_returns_actionable_error(self):
        patch = ('*** Begin Patch\n*** Add File: new.py\n+x\n'
                 '*** End Patch\n')
        ingested = ingest_tool_call(
            self._call(patch),
            known_tools={'edit_file', 'read_files'}, emit_audit=False)
        assert not ingested.ok
        assert 'write_file' in (ingested.parse_error or '')

    def test_json_edit_file_payload_untouched(self):
        payload = ('{"description": "d", "edits": [{"path": "a", '
                   '"operation": "replace", "anchor": "x", '
                   '"content": "y"}]}')
        ingested = ingest_tool_call(
            {'function': {'name': 'edit_file', 'arguments': payload}},
            known_tools={'edit_file'}, emit_audit=False)
        assert ingested.ok, ingested.parse_error
        assert all(pattern != 'codex_apply_patch'
                   for _path, pattern in ingested.repair_log)


class TestGatewayIntegration:
    def test_normalize_gateway_call_converts_patch(self):
        from lib.tools.gateway import normalize_gateway_call

        catalog = [{
            'name': 'edit_file',
            'parameters': {
                'type': 'object',
                'properties': {
                    'description': {'type': 'string'},
                    'edits': {'type': 'array'},
                },
            },
        }]
        call, error = normalize_gateway_call(
            {'name': 'apply_patch', 'arguments': _SIMPLE_PATCH},
            catalog=catalog, namespace_by_name={},
            gateway_call_id='gw1', index=0, source='execute_calls')
        assert error is None
        assert call['function']['name'] == 'edit_file'
        kinds = [repair['kind']
                 for repair in call.get('_normalization_repairs') or ()]
        assert 'codex_apply_patch' in kinds

    def test_normalize_gateway_call_guides_add_file(self):
        from lib.tools.gateway import normalize_gateway_call

        catalog = [{'name': 'edit_file',
                    'parameters': {'type': 'object', 'properties': {}}}]
        patch = ('*** Begin Patch\n*** Add File: new.py\n+x\n'
                 '*** End Patch\n')
        call, error = normalize_gateway_call(
            {'name': 'apply_patch', 'arguments': patch},
            catalog=catalog, namespace_by_name={},
            gateway_call_id='gw1', index=0, source='execute_calls')
        assert call is None
        assert error['code'] == 'invalid_arguments'
        assert 'write_file' in error['message']

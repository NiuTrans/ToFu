"""Common syntax at an insertion boundary is new content, not copied context."""

import pytest

from lib.project_mod.write_tools import tool_edit_file


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('operation', ['insert_after', 'insert_before'])
@pytest.mark.parametrize('boundary', ['}', '};', '});', ']', ');', '', '    '])
def test_shared_closing_syntax_is_inserted_verbatim(tmp_path, operation, boundary):
    anchor = f'function existing() {{\n  existing();\n{boundary}'
    addition = f'function added() {{\n  added();\n{boundary}'
    target = tmp_path / 'build_frontend.mjs'
    target.write_text(anchor + '\n// tail\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': target.name, 'operation': operation,
        'anchor': anchor, 'content': addition,
    }])

    # A supplied final newline already terminates the inserted line; the
    # line-oriented tool only supplies a missing terminator.
    inserted = addition if addition.endswith('\n') else addition + '\n'
    expected = (anchor + '\n' + inserted + '// tail\n'
                if operation == 'insert_after'
                else inserted + anchor + '\n// tail\n')
    assert result.startswith('Applied 1/1 edits'), result
    assert 'auto-repaired' not in result
    assert target.read_text(encoding='utf-8') == expected


@pytest.mark.parametrize('operation', ['insert_after', 'insert_before'])
def test_shared_opening_syntax_is_not_stripped(tmp_path, operation):
    anchor = '{\n  "existing": true\n},'
    addition = '{\n  "added": true\n},'
    target = tmp_path / 'data.txt'
    target.write_text(anchor + '\nEND\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': target.name, 'operation': operation,
        'anchor': anchor, 'content': addition,
    }])

    blocks = [anchor, addition] if operation == 'insert_after' else [addition, anchor]
    assert result.startswith('Applied 1/1 edits'), result
    assert 'auto-repaired' not in result
    assert target.read_text(encoding='utf-8') == '\n'.join(blocks) + '\nEND\n'


@pytest.mark.parametrize('operation', ['insert_after', 'insert_before'])
def test_multiline_structural_run_is_not_context(tmp_path, operation):
    anchor = 'function existing() {\n  work(() => {\n  });\n}\n'
    addition = 'function added() {\n  other(() => {\n  });\n}\n'
    target = tmp_path / 'sample.js'
    target.write_text(anchor + '// tail\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': target.name, 'operation': operation,
        'anchor': anchor.rstrip('\n'), 'content': addition,
    }])

    blocks = [anchor, addition] if operation == 'insert_after' else [addition, anchor]
    assert result.startswith('Applied 1/1 edits'), result
    assert 'auto-repaired' not in result
    assert target.read_text(encoding='utf-8') == ''.join(blocks) + '// tail\n'

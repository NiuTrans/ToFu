"""Byte/control filenames have reversible display without changing scan identity."""
import json
import pytest
from lib.repo_map.outline import RepoMap, Tag
from lib.repo_map.render import render_map

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('path', 'header'), [
    ('src/普通.py', 'src/普通.py'),
    ('invalid-\udcff.py', '"invalid-\\udcff.py"'),
    ('line\nname.py', '"line\\nname.py"'),
    ('tab\tname.py', '"tab\\tname.py"'),
    ('quote"name.py', '"quote\\"name.py"'),
    ('slash\\name.py', '"slash\\\\name.py"'),
])
def test_path_header_is_utf8_single_line_and_reversible(path, header):
    repo = RepoMap(root='/project', files=[path])
    repo.add_tag(Tag(path, 'symbol', 1, 'def', 'def symbol(): pass'))
    text, stats = render_map(repo, {path:1})
    assert text.splitlines() == [header+':', '  1: def symbol(): pass']
    assert text.encode('utf8').decode('utf8') == text
    if header.startswith('"'):
        assert json.loads(header) == path
    assert repo.files == [path] and repo.tags[path][0].rel_path == path
    assert stats['est_tokens'] == (len(text)+2)//3
    assert stats['files_shown'] == stats['tags_shown'] == 1

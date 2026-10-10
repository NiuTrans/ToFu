"""File-local Python derivations release without a process-wide GC intervention."""
import gc
import weakref

import pytest
from lib.repo_map.outline import _extract_python_tags

pytestmark = pytest.mark.unit


def test_result_tags_release_without_cyclic_collection():
    enabled = gc.isenabled()
    gc.disable()
    try:
        tags = _extract_python_tags('fixture.py',
            'PUBLIC = 1\nclass Outer:\n    def method(self):\n        def nested(): return 1\n')
        references = [weakref.ref(tag) for tag in tags]
        del tags
        assert all(reference() is None for reference in references)
    finally:
        if enabled: gc.enable()


def test_preorder_indentation_and_body_admission_stay_canonical():
    source = ('PUBLIC = 1\n_PRIVATE = 2\nclass Outer:\n'
              '    VALUE = 3\n    async def method(self):\n'
              '        def _tmp_nested(): return 1\n'
              'if True:\n    def conditional(): return 2\n'
              'def last(): return 3\n')
    tags = _extract_python_tags('fixture.py', source)
    assert [(tag.name, tag.kind, tag.indent) for tag in tags] == [
        ('PUBLIC', 'var', 0), ('Outer', 'class', 0), ('method', 'def', 1),
        ('_tmp_nested', 'def', 2), ('last', 'def', 0)]

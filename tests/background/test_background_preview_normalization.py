"""Display preview budgets must not corrupt history or relax execution evidence."""
from copy import deepcopy

import pytest

from lib.conversation_sync.validation import ContractViolation, decode
from lib.turn_projection_patch import normalize_projection_document

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('text', ['x' * 2001, '😀' * 2001, '😀' * 2000])
def test_background_previews_are_bounded_without_mutating_history(text):
    completion = {'commandId': 'bg_test', 'status': 'completed', 'command': text,
                  'resultPreview': text, 'resultTruncated': False}
    raw = {'content': text, 'backgroundCommand': completion,
           '_bgCommandInjects': [{'round': 1, 'count': 1, 'previews': [
               {'commandId': 'bg_test', 'text': 'model wrapper',
                'backgroundCommand': completion}]}]}
    before = deepcopy(raw)
    result = normalize_projection_document(raw)
    assert raw == before
    assert result['content'] == text
    nested = result['_bgCommandInjects'][0]['previews'][0]
    assert nested['text'] == 'model wrapper'
    for preview in (result['backgroundCommand'], nested['backgroundCommand']):
        assert preview['resultPreview'] == text[:2000]
        assert preview['command'] == text[:2000]
        assert preview['resultTruncated'] is (len(text) > 2000)
        decode('BackgroundCommandCompletion', preview)
    assert normalize_projection_document(result) == result


def test_preview_normalization_preserves_truncation_and_rejects_bad_authority():
    valid = {'commandId': 'bg_test', 'status': 'completed', 'command': 'cmd',
             'resultPreview': 'short', 'resultTruncated': True}
    result = normalize_projection_document({'backgroundCommand': valid})
    assert result['backgroundCommand'] == valid
    for mutation in ({'status': 'invented'}, {'commandId': 'x' * 129},
                     {'resultPreview': 42}):
        result = normalize_projection_document({'backgroundCommand': {**valid, **mutation}})
        with pytest.raises(ContractViolation):
            decode('BackgroundCommandCompletion', result['backgroundCommand'])

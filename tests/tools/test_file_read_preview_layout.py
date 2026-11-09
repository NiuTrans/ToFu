"""Compact file identities still leave usable space for acquired previews."""
import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import split_tool_result_delivery, tool_result_observation

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('limit', [800, 1100])
@pytest.mark.parametrize('stage', ['single', 'aggregate'])
def test_long_paths_keep_each_available_preview_within_the_fitted_layout(monkeypatch, limit, stage):
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', limit if stage == 'single' else 2400)
    writes = []
    def store(content, **kwargs):
        writes.append(content)
        return 'tool-result:' + 'a' * 64
    monkeypatch.setattr(_budget, '_store_tool_result_artifact', store)
    items = [{'type': 'file_read/v1', 'path': f'src/{i}/' + 'nested/' * 90 + f'file-{i}.py',
              'status': 'ok', 'preview': f'FILE-{i}: original evidence.\n' * 120}
             for i in range(2)]
    body = ''.join(item['preview'] for item in items)
    result = _budget.budget_tool_result_v2('read_files', body, user_id=17,
                                          projection_items=items, recovery_policy='artifact')
    original = result.canonical
    if stage == 'aggregate':
        result = _budget.enforce_round_aggregate_budget_v2(
            {'read': (result, 'read_files', 'read')}, user_id=17, token_budget=limit)['read'][0]
    delivery = split_tool_result_delivery(result)
    view = tool_result_observation(delivery.model_text, delivery.evidence)
    assert len(view['items']) == len(items)
    for index, item in enumerate(view['items']):
        assert item['path'].startswith(f'src/{index}/') and item['path'].endswith(f'file-{index}.py')
        assert item['status'] == 'ok' and item['previewTruncated']
        assert item.get('preview', '').startswith(f'FILE-{index}:')
        assert items[index]['preview'].startswith(item['preview'])
    assert len(delivery.model_text.encode()) <= limit
    assert result.canonical is original and original.text() == body
    assert len(writes) == 1

"""Complete real project batches, bounded previews and owner-scoped retention."""
import json

import pytest

pytest_plugins = ['tests._artifact_sidecar']
pytestmark = pytest.mark.unit


def test_real_grep_batch_keeps_every_search_past_count_and_output_quotas(tmp_path):
    from lib.project_mod import read_tools, tree_index
    source=tmp_path/'batch.py'
    source.write_text(''.join(
        f'# GROUP_{group:03d} line {line:02d} ' + 'q'*200 + '\n'
        for group in range(32) for line in range(20)))
    tree_index._build_sync(str(tmp_path))
    searches=[{'pattern':f'GROUP_{group:03d}', 'max_results':20} for group in range(32)]
    result=read_tools.tool_grep_batch(str(tmp_path), searches)
    assert len(result)>100000
    for group in range(32):
        assert f'GROUP_{group:03d}' in result
    assert 'more searches skipped' not in result
    assert 'batch budget exceeded' not in result


def test_real_find_batch_keeps_every_search_past_count_and_output_quotas(tmp_path):
    from lib.project_mod import read_tools, tree_index
    for number in range(32):
        (tmp_path/(f'file-{number:03d}-'+'q'*180+'.py')).write_text('source')
    (tmp_path/'last-batch-target.py').write_text('last source')
    tree_index._build_sync(str(tmp_path))
    searches=[{'pattern':'*.py'} for _ in range(31)]+[{'pattern':'last-batch-target.py'}]
    result=read_tools.tool_find_files_batch(str(tmp_path), searches)
    assert len(result)>100000
    assert result.endswith(read_tools.tool_find_files(str(tmp_path), 'last-batch-target.py'))
    assert '1 found' in result
    assert 'more finds skipped' not in result
    assert 'batch budget exceeded' not in result


@pytest.mark.parametrize('absolute', [False, True])
def test_real_ranged_read_batch_preserves_more_than_fifty_mib_and_late_receipts(tmp_path, absolute):
    from lib.project_mod.read_tools import tool_read_files
    padding='Z'*(4*1024*1024)
    reads=[]
    for number in range(13):
        path=tmp_path/f'file-{number:02d}.py'
        path.write_text(padding+f'\nFILE_BOUNDARY_{number}\n')
        reads.append({'path':str(path) if absolute else path.name, 'start_line':1,'end_line':2})
    (tmp_path/'late.py').write_text('LATE_REAL_READ_EVIDENCE\n')
    reads.extend([{'bad':'malformed'}, {'path':str(tmp_path/'late.py') if absolute else 'late.py'}])
    items=[]
    result=tool_read_files(str(tmp_path), reads, result_items=items)
    assert len(result)>50*1024*1024
    assert result.count('Z')==13*len(padding)
    for number in range(13):
        assert f'FILE_BOUNDARY_{number}' in result
    assert 'LATE_REAL_READ_EVIDENCE' in result
    assert len(items)==15
    assert items[-2]['status']=='error'
    assert items[-1]['status']=='ok'
    assert items[-1]['path'].endswith('late.py')
    assert not any(item['status']=='skipped' for item in items)


def test_all_real_read_receipts_retain_tail_with_bounded_model_preview(tmp_path):
    from lib.project_mod.read_tools import tool_read_files
    from lib.tasks_pkg.compaction import _budget
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.tools.result_projection import normalize_file_read_projection_items
    reads=[]
    for number in range(120):
        path=tmp_path/f'file-{number:03d}.py'
        path.write_text(f'# LAST_BATCH_EVIDENCE_{number:03d}\n' + '# source detail\n'*220)
        reads.append({'path':path.name})
    items=[]
    raw=tool_read_files(str(tmp_path), reads, result_items=items)
    assert len(items)==120
    assert len(normalize_file_read_projection_items(items, {'reads':reads}))==120
    assert 'LAST_BATCH_EVIDENCE_119' in raw
    delivery=split_tool_result_delivery(_budget.budget_tool_result_v2(
        'read_files', raw, user_id=17, tool_arguments={'reads':reads}, projection_items=items))
    visible=json.loads(delivery.model_text)
    assert len(visible['items'])<=64
    assert _budget._model_result_tokens(delivery.model_text, '')<=8000
    assert '120 file results' in visible['summary']
    assert 'preview window' in visible['summary']
    reference=delivery.evidence['artifactRef']
    assert reference
    repository=ToolResultArtifactRepository()
    hit=repository.search(user_id=17, artifact_ref=reference, query='LAST_BATCH_EVIDENCE_119')
    assert hit['items'] and 'LAST_BATCH_EVIDENCE_119' in str(hit['items'])
    assert repository.read_range(user_id=18, artifact_ref=reference) is None
    source_delivery=split_tool_result_delivery(_budget.budget_tool_result_v2(
        'read_files', raw, user_id=17, tool_arguments={'reads':reads},
        projection_items=items, recovery_policy='source'))
    source_visible=json.loads(source_delivery.model_text)
    assert len(source_visible['items'])<=64
    assert _budget._model_result_tokens(source_delivery.model_text, '')<=_budget.SOURCE_TOOL_RESULT_V2_MAX_TOKENS
    assert '120 file results' in source_visible['summary']
    assert 'preview window' in source_visible['summary']


def test_late_unauthorized_read_stays_an_error_and_does_not_skip_following_source(tmp_path):
    from lib.project_mod.read_tools import tool_read_files
    root=tmp_path/'project';root.mkdir()
    (tmp_path/'foreign.py').write_text('FOREIGN_SECRET_MUST_STAY_OUT')
    reads=[]
    for number in range(31):
        path=root/f'file-{number:02d}.py';path.write_text(f'# own source {number}')
        reads.append({'path':path.name})
    (root/'last.py').write_text('FINAL_AUTHORIZED_SOURCE')
    reads.extend([{'path':'../foreign.py'}, {'path':'last.py'}])
    items=[]
    text=tool_read_files(str(root), reads, result_items=items)
    assert 'FOREIGN_SECRET_MUST_STAY_OUT' not in text
    assert 'FINAL_AUTHORIZED_SOURCE' in text
    assert len(items)==33
    assert items[31]['status']=='error'
    assert items[32]['status']=='ok'

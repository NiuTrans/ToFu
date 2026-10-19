"""Real complete text sources across project, absolute and fallback readers."""
import hashlib
import json

import pytest

pytest_plugins = ['tests._artifact_sidecar']
pytestmark = pytest.mark.unit


def test_real_fifty_one_mib_text_is_complete_through_both_read_paths(tmp_path):
    from lib.project_mod.read_tools import _read_project_file, _read_absolute_file
    path=tmp_path/'large.txt'
    digest=hashlib.sha256()
    chunk=b'q'*(1024*1024)
    footer=b'\nFULL_SOURCE_AFTER_FIFTY_ONE_MIB\n'
    with path.open('wb') as output:
        for _ in range(51):
            output.write(chunk);digest.update(chunk)
        output.write(footer);digest.update(footer)
    expected=digest.digest()
    for reader,separator in ((lambda:_read_project_file(str(tmp_path),path.name),'\n'),
                             (lambda:_read_absolute_file(str(path)),'\n\n')):
        result=reader()
        assert 'File too large' not in result
        body=result.split(separator,1)[1]
        assert hashlib.sha256(body.encode()).digest()==expected
        assert body.endswith(footer.decode())


def test_complete_large_json_reaches_result_retention_and_late_evidence(tmp_path):
    from lib.project_mod.read_tools import tool_read_files
    from lib.tasks_pkg.compaction import _budget
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    path=tmp_path/'large.json'
    body=json.dumps({'padding':'q'*1200000,'tail':'TAIL_COMPLETE_JSON_SOURCE'})
    path.write_text(body)
    items=[]
    raw=tool_read_files(str(tmp_path), [{'path':path.name}],result_items=items)
    returned=raw.split('\n',1)[1]
    assert returned==body
    assert json.loads(returned)['tail']=='TAIL_COMPLETE_JSON_SOURCE'
    assert items[0]['status']=='ok'
    delivery=split_tool_result_delivery(_budget.budget_tool_result_v2(
        'read_files',raw,user_id=17,tool_arguments={'reads':[{'path':path.name}]},projection_items=items))
    reference=delivery.evidence['artifactRef']
    assert reference
    hit=ToolResultArtifactRepository().search(user_id=17,artifact_ref=reference,query='TAIL_COMPLETE_JSON_SOURCE')
    assert hit['items'] and 'TAIL_COMPLETE_JSON_SOURCE' in str(hit['items'])


def test_csv_is_complete_and_explicit_ranges_remain_exact(tmp_path):
    from lib.project_mod.read_tools import _read_project_file
    path=tmp_path/'data.csv'
    lines=['id,value\n']+[f'{number},entry-{number:06d}\n' for number in range(10000)]
    body=''.join(lines);path.write_text(body)
    full=_read_project_file(str(tmp_path),path.name)
    assert full.split('\n',1)[1]==body
    assert 'DATA FILE — showing first' not in full
    sliced=_read_project_file(str(tmp_path),path.name,5000,5001)
    assert sliced.split('\n',1)[1]==''.join(lines[4999:5001])


def test_python_grep_fallback_searches_beyond_retired_file_size_limit(tmp_path):
    from lib.project_mod.read_tools import _python_grep
    path=tmp_path/'large.txt'
    path.write_text('unrelated line\n'*70000+'FALLBACK_LATE_EVIDENCE\n')
    assert path.stat().st_size>512*1024
    result=_python_grep(str(tmp_path),str(tmp_path),'FALLBACK_LATE_EVIDENCE')
    assert 'FALLBACK_LATE_EVIDENCE' in result
    assert path.name in result


def test_binary_source_is_still_rejected_as_text(tmp_path):
    from lib.project_mod.read_tools import _read_project_file, _read_absolute_file
    path=tmp_path/'binary.txt';path.write_bytes(b'\0'*8192+b'opaque')
    assert 'Binary file:' in _read_project_file(str(tmp_path),path.name)
    assert 'appears to be binary' in _read_absolute_file(str(path))


def test_encoding_fallback_reads_past_retired_character_ceiling(tmp_path):
    from lib.file_reader import read_local_file
    path=tmp_path/'encoded.txt'
    prefix=b'q'*(1024*1024)
    tail='远端完整文本'
    with path.open('wb') as output:
        for _ in range(51):
            output.write(prefix)
        output.write(tail.encode('gbk'))
    result=read_local_file(str(path))
    body=result.split('\n\n',1)[1]
    assert len(body)==51*1024*1024+len(tail)
    assert body.endswith(tail)

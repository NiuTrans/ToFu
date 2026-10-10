"""Offline benchmark reports preserve evidence and expose incomplete coverage."""
import hashlib
import json
import pytest
from benchmarks.research_quality_compare import build_report,main
from tests.research.test_research_quality_comparison import pair,versioned_pair

pytestmark=pytest.mark.unit


def test_complete_report_has_exact_input_provenance_and_version_identity(tmp_path):
    raw=json.dumps([versioned_pair()]).encode();source=tmp_path/'input.json';source.write_bytes(raw)
    output=tmp_path/'report.json'
    assert main([str(source),'--output',str(output)])==0
    report=json.loads(output.read_text())
    assert report['input_sha256']==hashlib.sha256(raw).hexdigest()
    assert report['actual_model_calls']==0 and report['scope']=='declared_external_audit_receipts_only'
    assert report['comparison']['system_generations']['treatment']=='b'*64
    assert source.read_bytes()==raw and not list(tmp_path.glob('.research-quality-*.tmp'))


def test_incomplete_comparison_still_saves_diagnostics_with_failure_exit(tmp_path):
    source=tmp_path/'input.json';source.write_text(json.dumps([versioned_pair('one'),versioned_pair('two','c'*64)]))
    output=tmp_path/'report.json'
    assert main([str(source),'--output',str(output)])==3
    report=json.loads(output.read_text())
    assert report['comparison']['excluded_pair_count']==2
    assert report['comparison']['paired_metric_summary']=={}


@pytest.mark.parametrize('raw',[b'{"x":1,"x":2}',b'[NaN]',b'[1e400]',b'not JSON',b'{}'])
def test_invalid_json_never_becomes_a_successful_report(raw):
    with pytest.raises((ValueError,TypeError)):build_report(raw)


def test_output_cannot_overwrite_input_or_its_alias(tmp_path):
    source=tmp_path/'input.json';raw=json.dumps([pair()]).encode();source.write_bytes(raw)
    alias=tmp_path/'alias.json';alias.symlink_to(source)
    assert main([str(source),'--output',str(source)])==1
    assert main([str(source),'--output',str(alias)])==1
    assert source.read_bytes()==raw


def test_rejected_input_preserves_existing_report(tmp_path):
    source=tmp_path/'input.json';source.write_text('invalid JSON')
    output=tmp_path/'report.json';output.write_text('previous report')
    assert main([str(source),'--output',str(output)])==1
    assert output.read_text()=='previous report'


def test_extreme_integer_budget_stays_visible_as_excluded_evidence():
    row=pair();row['treatment']['actual_cost']=10**400
    report=build_report(json.dumps([row]).encode())
    assert report['comparison']['valid_pair_count']==0
    assert 'treatment_invalid_budget' in report['comparison']['excluded_pairs'][0]['reasons']


def test_input_bound_refuses_before_json_decode(monkeypatch):
    from benchmarks import research_quality_compare as runner
    monkeypatch.setattr(runner,'MAX_INPUT_BYTES',4)
    with pytest.raises(ValueError,match='at most'):runner.build_report(b'12345')


def test_failed_atomic_publication_preserves_old_report_and_cleans_temporary(tmp_path,monkeypatch):
    from benchmarks import research_quality_compare as runner
    source=tmp_path/'input.json';source.write_text(json.dumps([pair()]))
    output=tmp_path/'report.json';output.write_text('previous report')
    def refuse(*args):raise OSError('publication fault')
    monkeypatch.setattr(runner.os,'replace',refuse)
    assert main([str(source),'--output',str(output)])==1
    assert output.read_text()=='previous report' and not list(tmp_path.glob('.research-quality-*.tmp'))


def manifest(rows):
    fields=('task_digest','corpus_digest','model','budget_digest')
    return json.dumps({'schema_version':1,'pairs':[{'pair_id':row['pair_id'],
        **{key:row['control'][key] for key in fields}} for row in rows]}).encode()


def test_manifest_detects_omitted_tasks_with_exact_provenance(tmp_path):
    rows=[versioned_pair('one'),versioned_pair('two')];plan=manifest(rows)
    report=build_report(json.dumps(rows[:1]).encode(),manifest_raw=plan)
    assert report['manifest_sha256']==hashlib.sha256(plan).hexdigest()
    assert report['manifest_coverage']['missing_pair_ids']==['two']
    assert not report['manifest_coverage']['complete']
    source=tmp_path/'input.json';source.write_text(json.dumps(rows[:1]))
    planned=tmp_path/'manifest.json';planned.write_bytes(plan)
    output=tmp_path/'report.json'
    assert main([str(source),'--manifest',str(planned),'--output',str(output)])==3
    assert json.loads(output.read_text())['manifest_coverage']['missing_pair_ids']==['two']


@pytest.mark.parametrize('field',['task_digest','corpus_digest','model','budget_digest'])
def test_manifest_refuses_relabeling_different_work_as_a_planned_pair(field):
    row=versioned_pair();plan=manifest([row])
    row['control'][field]=row['treatment'][field]='substituted'
    coverage=build_report(json.dumps([row]).encode(),manifest_raw=plan)['manifest_coverage']
    assert coverage['mismatched_pair_ids']==['p1'] and not coverage['complete']


def test_manifest_reports_extra_duplicate_and_unidentified_inputs():
    expected=versioned_pair('planned');extra=versioned_pair('extra')
    coverage=build_report(json.dumps([expected,expected,extra,None]).encode(),
        manifest_raw=manifest([expected]))['manifest_coverage']
    assert coverage['unexpected_pair_ids']==['extra'] and coverage['duplicate_pair_ids']==['planned']
    assert coverage['unidentified_pair_count']==1 and not coverage['complete']


def test_complete_manifest_and_no_manifest_are_explicitly_distinct():
    rows=[versioned_pair()];raw=json.dumps(rows).encode()
    assert build_report(raw,manifest_raw=manifest(rows))['manifest_coverage']['complete']
    assert build_report(raw)['manifest_coverage'] is None


def test_report_cannot_overwrite_manifest(tmp_path):
    rows=[versioned_pair()];source=tmp_path/'input.json';source.write_text(json.dumps(rows))
    planned=tmp_path/'manifest.json';original=manifest(rows);planned.write_bytes(original)
    assert main([str(source),'--manifest',str(planned),'--output',str(planned)])==1
    assert planned.read_bytes()==original


@pytest.mark.parametrize('plan',[{}, {'schema_version':True,'pairs':[]},
    {'schema_version':1,'pairs':[]},{'schema_version':1,'pairs':[{'pair_id':'x'}]}])
def test_malformed_manifest_is_not_a_coverage_certificate(plan):
    with pytest.raises(ValueError):build_report(json.dumps([pair()]).encode(),manifest_raw=json.dumps(plan).encode())


def version_manifest(rows):
    plan=json.loads(manifest(rows))
    for planned,row in zip(plan['pairs'],rows):
        planned['system_digests']={arm:row[arm]['system_digest'] for arm in ('control','treatment')}
    return json.dumps(plan).encode()


@pytest.mark.parametrize('arm',['control','treatment'])
@pytest.mark.parametrize('mutation',['changed','missing'])
def test_manifest_binds_expected_generations_not_merely_consistent_submissions(arm,mutation):
    row=versioned_pair();plan=version_manifest([row])
    if mutation=='missing':row[arm].pop('system_digest')
    else:row[arm]['system_digest']='c'*64
    report=build_report(json.dumps([row]).encode(),manifest_raw=plan)
    coverage=report['manifest_coverage']
    assert coverage['generation_bound_pair_count']==1
    assert coverage['generation_mismatched_pair_ids']==['p1'] and not coverage['complete']


def test_consistent_but_unplanned_versions_return_incomplete_exit(tmp_path):
    rows=[versioned_pair('one'),versioned_pair('two')];plan=version_manifest(rows)
    for row in rows:row['treatment']['system_digest']='c'*64
    source=tmp_path/'input.json';source.write_text(json.dumps(rows))
    planned=tmp_path/'manifest.json';planned.write_bytes(plan);output=tmp_path/'report.json'
    assert main([str(source),'--manifest',str(planned),'--output',str(output)])==3
    report=json.loads(output.read_text())
    assert report['comparison']['valid_pair_count']==2
    assert report['manifest_coverage']['generation_mismatched_pair_ids']==['one','two']


@pytest.mark.parametrize('versions',[None,{}, {'control':'a'*64},{'control':'a'*64,'treatment':'invalid'}])
def test_partial_or_invalid_planned_versions_cannot_certify_coverage(versions):
    rows=[versioned_pair()];plan=json.loads(manifest(rows));plan['pairs'][0]['system_digests']=versions
    with pytest.raises(ValueError,match='freeze both'):
        build_report(json.dumps(rows).encode(),manifest_raw=json.dumps(plan).encode())


def test_matching_version_manifest_preserves_complete_coverage_and_legacy_scope():
    rows=[versioned_pair()];raw=json.dumps(rows).encode()
    assert build_report(raw,manifest_raw=version_manifest(rows))['manifest_coverage']['complete']
    coverage=build_report(raw,manifest_raw=manifest(rows))['manifest_coverage']
    assert coverage['complete'] and coverage['generation_bound_pair_count']==0


def test_offline_report_excludes_every_conflicting_duplicate_record():
    first = pair()
    second = pair()
    second['treatment']['metrics']['counterevidence_recall'] = 1
    result = build_report(json.dumps([first, second]).encode())
    comparison = result['comparison']
    assert comparison['status'] == 'insufficient_evidence'
    assert comparison['valid_pair_count'] == 0
    assert comparison['excluded_pair_count'] == 2
    assert comparison['paired_metric_summary'] == {}

"""Evidence identity/discovery cannot manufacture system workload admission."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'benchmarks'))
from system_evidence_inventory import inventory

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('filename,domain', [
    ('system_repository_fixture.json', 'repository'),
    ('system_context_final_admission_20261008.json', 'context'),
    ('system_context_native_producer_20261008.json', 'context'),
    ('system_ingest_domain_candidate_20261008.json', 'ingest'),
])
def test_positive_kernel_does_not_become_domain_coverage(tmp_path, filename, domain):
    raw=tmp_path/'raw.json';raw.write_text('{"fixture":true}')
    report=tmp_path/filename
    report.write_text(json.dumps({'scope':'kernel','production_promoted':True,
        'raw_artifact':str(raw),'raw_artifact_sha256':hashlib.sha256(raw.read_bytes()).hexdigest()}))
    result=inventory([report],artifact_directory=tmp_path)
    assert result['reports'][0]['raw_artifact']['status']=='verified'
    assert result['domain_report_counts'][domain]==1
    assert result['standard_report_gate']['verdict']=='incomplete'
    assert result['standard_report_gate']['coverage']=={'measured':0,'required':30}
    assert result['whole_goal_completion']=='not_proven_by_inventory'


def test_raw_mismatch_and_outside_directory_remain_visible(tmp_path):
    raw=tmp_path/'raw.json';raw.write_text('{}')
    report=tmp_path/'system_event_fixture.json'
    report.write_text(json.dumps({'raw_artifact':str(raw),'raw_artifact_sha256':'0'*64}))
    assert inventory([report],artifact_directory=tmp_path)['reports'][0]['raw_artifact']['status']=='digest_mismatch'
    assert inventory([report],artifact_directory=tmp_path/'other')['reports'][0]['raw_artifact']['status']=='outside_supplied_artifact_directory'


def test_standard_gate_and_unknown_reports_are_not_hidden(tmp_path):
    report=tmp_path/'system_unknown.json';report.write_text('{}')
    result=inventory([report],reference={'format':'bad'},candidate={'format':'bad'})
    assert result['standard_report_gate']['verdict']=='invalid'
    assert result['unclassified_reports']==['system_unknown.json']
    with pytest.raises(ValueError):inventory([],reference={})


def test_digest_lookup_uses_content_not_filename_or_source_digest(tmp_path):
    raw=tmp_path/'unexpected-name.json';raw.write_text('{"raw":true}')
    digest=hashlib.sha256(raw.read_bytes()).hexdigest()
    report=tmp_path/'system_context.json';report.write_text(json.dumps({
        'experiments':[{'raw_sha256':digest,'source_sha256':digest}]}))
    result=inventory([report],artifact_directory=tmp_path,resolve_digests=True)
    refs=result['reports'][0]['raw_digest_references']
    assert len(refs)==1 and refs[0]['report_field']=='/experiments/0/raw_sha256'
    assert refs[0]['status']=='content_match' and refs[0]['matching_artifacts']==[str(raw)]
    assert result['standard_report_gate']['verdict']=='incomplete'


def test_digest_match_does_not_hide_wrong_explicit_reference(tmp_path):
    raw=tmp_path/'correct.json';raw.write_text('{"correct":true}')
    wrong=tmp_path/'wrong.json';wrong.write_text('{}')
    report=tmp_path/'system_event_fixture.json';report.write_text(json.dumps({
        'raw_artifact':str(wrong),'raw_artifact_sha256':hashlib.sha256(raw.read_bytes()).hexdigest()}))
    row=inventory([report],artifact_directory=tmp_path,resolve_digests=True)['reports'][0]
    assert row['raw_artifact']['status']=='digest_mismatch'
    assert row['raw_digest_references'][0]['status']=='content_match'


def test_digest_lookup_excludes_symlink_escape(tmp_path):
    inside=tmp_path/'inside';inside.mkdir()
    raw=tmp_path/'outside.json';raw.write_text('{"outside":true}')
    (inside/'alias.json').symlink_to(raw)
    report=inside/'system_unknown.json';report.write_text(json.dumps({
        'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest()}))
    row=inventory([report],artifact_directory=inside,resolve_digests=True)['reports'][0]
    assert row['raw_digest_references'][0]['status']=='unresolved'

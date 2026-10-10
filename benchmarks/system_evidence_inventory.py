"""Read-only six-domain evidence inventory, separate from admission authority.

Hash retained reports and explicitly referenced raw artifacts. Optional standard
reports enter the existing system_performance gate; kernel reports never become
domain integration by filename or favorable ratios. Output stays external.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from system_performance import CONTRACT, compare

ROOT=Path(__file__).resolve().parents[1]
RAW_DIGEST_KEYS=frozenset(('raw_artifact_sha256','raw_sha256','memory_artifact_sha256'))
PREFIXES={
    'events':('system_event_','system_native_event_'),
    'context':('system_context_','system_incremental_context_','system_request_copy_','system_conversion_'),
    'markdown':('system_markdown_','system_streaming_renderer_'),
    'repository':('system_repository_',),
    'http':('system_http_','system_native_http_','system_sealed_transport_'),
    'ingest':('system_ingest_','system_knowledge_html_','system_pdf_','system_publication_metadata_','system_search_startup_'),
}


def artifact_check(report,artifact_directory):
    path=report.get('raw_artifact');expected=report.get('raw_artifact_sha256')
    if not path or not expected:
        return {'status':'no_explicit_path_and_digest'}
    if not isinstance(path,str) or not isinstance(expected,str):
        return {'status':'invalid_reference'}
    raw=Path(path).resolve()
    if artifact_directory is None or raw.parent!=artifact_directory.resolve():
        return {'status':'outside_supplied_artifact_directory','path':str(raw)}
    if not raw.is_file():return {'status':'missing','path':str(raw)}
    actual=hashlib.sha256(raw.read_bytes()).hexdigest()
    return {'status':'verified' if actual==expected else 'digest_mismatch',
            'path':str(raw),'expected_sha256':expected,'actual_sha256':actual}


def artifact_index(directory):
    """Hash stable direct JSON files only; exclude symlink escapes/live writes."""
    result={}
    if directory is None:return result
    directory=directory.resolve()
    for path in sorted(directory.iterdir()):
        if path.suffix!='.json' or not path.is_file() or path.resolve().parent!=directory:
            continue
        before=path.stat();content=path.read_bytes();after=path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            continue
        digest=hashlib.sha256(content).hexdigest()
        result.setdefault(digest,[]).append(str(path.resolve()))
    return result


def digest_references(value,path=''):
    """Find explicitly named raw digest fields, never source/fixture digests."""
    def leaves(item,pointer):
        if isinstance(item,str) and len(item)==64 and all(c in '0123456789abcdef' for c in item):
            yield pointer,item
        elif isinstance(item,dict):
            for key,child in item.items():yield from leaves(child,pointer+'/'+str(key))
        elif isinstance(item,list):
            for index,child in enumerate(item):yield from leaves(child,pointer+'/'+str(index))
    if isinstance(value,dict):
        for key,child in value.items():
            pointer=path+'/'+str(key)
            if key in RAW_DIGEST_KEYS:yield from leaves(child,pointer)
            else:yield from digest_references(child,pointer)
    elif isinstance(value,list):
        for index,child in enumerate(value):yield from digest_references(child,path+'/'+str(index))


def inventory(paths,*,artifact_directory=None,reference=None,candidate=None,resolve_digests=False):
    indexed=artifact_index(artifact_directory) if resolve_digests else {}
    records=[]
    for path in sorted(paths):
        content=path.read_bytes();report=json.loads(content)
        domains=[domain for domain,prefixes in PREFIXES.items() if path.name.startswith(prefixes)]
        records.append({'report':path.name,'sha256':hashlib.sha256(content).hexdigest(),
            'domains':domains,'scope':report.get('scope'),
            'declared':{key:report[key] for key in ('whole_system_verdict','candidate_verdict',
                        'verdict','production_promoted','production_promotion') if key in report},
            'raw_artifact':artifact_check(report,artifact_directory),
            'raw_digest_references':[{'report_field':pointer,'expected_sha256':digest,
                'status':'content_match' if digest in indexed else 'unresolved',
                'matching_artifacts':indexed.get(digest,[])} for pointer,digest in digest_references(report)]})
    if (reference is None)!=(candidate is None):
        raise ValueError('reference and candidate standard reports must be supplied together')
    gate=compare(reference,candidate) if reference is not None else {
        'verdict':'incomplete','coverage':{'measured':0,'required':sum(map(len,CONTRACT['scope'].values()))},
        'issues':[{'kind':'incomplete','reason':'standard domain-integration comparison reports not supplied'}]}
    return {'format':'tofu.system-evidence-inventory/v1',
        'contract_sha256':hashlib.sha256((ROOT/'benchmarks/system_performance_contract.json').read_bytes()).hexdigest(),
        'required_cases':CONTRACT['scope'],'report_count':len(records),'reports':records,
        'domain_report_counts':{domain:sum(domain in row['domains'] for row in records) for domain in CONTRACT['scope']},
        'unclassified_reports':[row['report'] for row in records if not row['domains']],
        'standard_report_gate':gate,'whole_goal_completion':'not_proven_by_inventory',
        'limits':['Prefix classification is discoverability, not workload coverage or correctness proof.',
                  'A raw digest verifies artifact identity, not sample semantics or admission.',
                  'Historical source hashes are retained; current source is not substituted for historical identity.',
                  'Standard gate acceptance still requires direct integration/failure/recovery evidence audit.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--artifacts',type=Path)
    parser.add_argument('--resolve-digests',action='store_true')
    parser.add_argument('--reference',type=Path);parser.add_argument('--candidate',type=Path)
    args=parser.parse_args()
    names=subprocess.check_output(['/usr/bin/git','ls-files','-z','--','docs/evidence/system_*.json'],cwd=ROOT).decode().split('\0')
    report=inventory([ROOT/name for name in names if name],artifact_directory=args.artifacts,
        resolve_digests=args.resolve_digests,
        reference=json.loads(args.reference.read_text()) if args.reference else None,
        candidate=json.loads(args.candidate.read_text()) if args.candidate else None)
    args.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'reports':report['report_count'],'domains':report['domain_report_counts'],
                     'gate':report['standard_report_gate']['verdict']},sort_keys=True))


if __name__=='__main__':main()

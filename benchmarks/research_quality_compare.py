"""Offline comparison of declared, externally audited research-quality pairs.

Entry: main / build_report. Inputs and statistics belong to quality_comparison;
this runner owns bounded JSON intake, provenance and atomic report publication.
No models, network calls, application storage or experimental execution occur.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lib.research import quality_comparison

MAX_INPUT_BYTES=8*1024*1024


def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate JSON object field: '+key)
        result[key]=value
    return result


def _constant(value):
    raise ValueError('non-finite JSON number: '+value)


def _float(value):
    parsed=float(value)
    if not math.isfinite(parsed):raise ValueError('JSON number exceeds finite precision')
    return parsed


def _decode(raw):
    if not isinstance(raw,bytes) or len(raw)>MAX_INPUT_BYTES:
        raise ValueError('comparison input must be UTF-8 JSON of at most 8 MiB')
    return json.loads(raw.decode('utf-8-sig'),object_pairs_hook=_object,
        parse_constant=_constant,parse_float=_float)


def _manifest_coverage(manifest,pairs):
    fields=quality_comparison.MATCHED_IDENTITIES
    if (not isinstance(manifest,dict) or set(manifest)!={'schema_version','pairs'}
            or type(manifest['schema_version']) is not int or manifest['schema_version']!=1
            or not isinstance(manifest['pairs'],list) or not 1<=len(manifest['pairs'])<=quality_comparison.MAX_PAIRS):
        raise ValueError('manifest requires schema_version 1 and 1..100 planned pairs')
    planned={}
    for row in manifest['pairs']:
        if (not isinstance(row,dict) or set(row) not in ({'pair_id',*fields},{'pair_id',*fields,'system_digests'})
                or any(not isinstance(row[key],str) or not row[key].strip() for key in ('pair_id',*fields))
                or row['pair_id'] in planned):
            raise ValueError('manifest pair identities must be complete, nonblank and unique')
        if 'system_digests' in row:
            versions=row['system_digests']
            if (not isinstance(versions,dict) or set(versions)!={'control','treatment'}
                    or any(not quality_comparison.system_digest_valid(value) for value in versions.values())):
                raise ValueError('manifest system_digests must freeze both valid control and treatment versions')
        planned[row['pair_id']]=row
    observed=[];mismatched=[];generation_mismatches=[];unidentified=0
    for row in pairs:
        identity=row.get('pair_id') if isinstance(row,dict) else None
        if not isinstance(identity,str) or not identity.strip():unidentified+=1;continue
        observed.append(identity)
        if identity in planned and any(not isinstance(row.get(arm),dict)
                or any(row[arm].get(key)!=planned[identity][key] for key in fields)
                for arm in ('control','treatment')):
            mismatched.append(identity)
        if identity in planned and 'system_digests' in planned[identity] and any(
                not isinstance(row.get(arm),dict) or row[arm].get('system_digest')!=planned[identity]['system_digests'][arm]
                for arm in ('control','treatment')):
            generation_mismatches.append(identity)
    missing=[identity for identity in planned if identity not in observed]
    unexpected=list(dict.fromkeys(identity for identity in observed if identity not in planned))
    duplicates=list(dict.fromkeys(identity for identity in observed if observed.count(identity)>1))
    return {'basis':'declared_manifest_identity_coverage','planned_pair_count':len(planned),
        'missing_pair_ids':missing,'unexpected_pair_ids':unexpected,'duplicate_pair_ids':duplicates,
        'mismatched_pair_ids':list(dict.fromkeys(mismatched)),'unidentified_pair_count':unidentified,
        'generation_bound_pair_count':sum('system_digests' in row for row in planned.values()),
        'generation_mismatched_pair_ids':list(dict.fromkeys(generation_mismatches)),
        'complete':not (missing or unexpected or duplicates or mismatched or generation_mismatches or unidentified),
        'limitations':['A manifest digest does not establish pre-result preregistration or authentic execution.']}


def build_report(raw,*,manifest_raw=None):
    pairs=_decode(raw)
    comparison=quality_comparison.compare_quality(pairs)
    coverage=_manifest_coverage(_decode(manifest_raw),pairs) if manifest_raw is not None else None
    return {'schema_version':1,'scope':'declared_external_audit_receipts_only',
        'manifest_sha256':hashlib.sha256(manifest_raw).hexdigest() if manifest_raw is not None else None,
        'manifest_coverage':coverage,
        'input_sha256':hashlib.sha256(raw).hexdigest(),
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'evaluator_sha256':hashlib.sha256(Path(quality_comparison.__file__).read_bytes()).hexdigest(),
        'python_version':'.'.join(map(str,sys.version_info[:3])),
        'actual_model_calls':0,'comparison':comparison}


def _publish(path,text):
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
                prefix='.research-quality-',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name);stream.write(text);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path);temporary=None
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path,help='JSON array of at most 100 paired audit records')
    parser.add_argument('--output',type=Path,help='Atomically save a report; input is never overwritten')
    parser.add_argument('--manifest',type=Path,help='Declared planned pair IDs and frozen task/corpus/model/budget identities')
    args=parser.parse_args(argv)
    try:
        for source in (args.input,args.manifest):
            if source is not None and args.output and (source.resolve()==args.output.resolve()
                    or (args.output.exists() and os.path.samefile(source,args.output))):
                raise ValueError('report output cannot replace input evidence or the task manifest')
        with args.input.open('rb') as stream:raw=stream.read(MAX_INPUT_BYTES+1)
        manifest_raw=None
        if args.manifest:
            with args.manifest.open('rb') as stream:manifest_raw=stream.read(MAX_INPUT_BYTES+1)
        report=build_report(raw,manifest_raw=manifest_raw)
        rendered=json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)+'\n'
        if args.output:_publish(args.output,rendered)
        else:sys.stdout.write(rendered)
        comparison=report['comparison']
        return 0 if (comparison['valid_pair_count'] and not comparison['excluded_pair_count']
            and (report['manifest_coverage'] is None or report['manifest_coverage']['complete'])) else 3
    except (OSError,ValueError,TypeError,OverflowError) as exc:
        print('Research quality comparison failed: '+str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())

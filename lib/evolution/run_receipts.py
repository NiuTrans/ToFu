"""Immutable owner-bound stage receipt pages outside the mutable run header.

Entry: compact / expand. Runs own the only publication CAS. Pages acknowledged
before that CAS remain candidates until referenced. Reads reconstruct exact
history for existing consumers; lazy runtime projections are still separate work.
Dependencies: records, canonical run contract and stage-input immutable writes.
"""
import hashlib
import json
from . import records
from .contracts import EvolutionStateError,contract_validator
from .stage_inputs import _put_immutable

NAMESPACE='evolution.run_receipt_pages.v1'
PAGE_SIZE=32
RECENT_RECEIPTS=64
SCHEMA='evolution_run_v1.schema.json'


def _json(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()



def _key(run_id,sha,user_id):
    return records.entity_key(hashlib.sha256(run_id.encode()).hexdigest()+'-'+sha,user_id,kind='run receipt page')


def _validate(value,definition):
    if not contract_validator(SCHEMA,definition).is_valid(value):
        raise EvolutionStateError('run receipt archive contract changed')


def compact(run,*,user_id):
    from lib.identity import require_user_id
    owner=require_user_id(user_id,context='run receipt archive')
    pointer=run.get('receipt_archive');rows=run['receipts'];archived=0;prefix=hashlib.sha256()
    if pointer:
        _validate(pointer,'receipt_archive_ref');archived=pointer['count']
        for row in rows[:archived]:prefix.update(_json(row)+b'\n')
        if archived>len(rows) or prefix.hexdigest()!=pointer['prefix_sha256']:
            raise EvolutionStateError('archived run receipts changed')
    while len(rows)-archived>RECENT_RECEIPTS:
        page={'schema_version':1,'run_id':run['run_id'],'user_id':owner,
              'previous':pointer,'receipts':rows[archived:archived+PAGE_SIZE]}
        _validate(page,'receipt_archive_page')
        sha=hashlib.sha256(_json(page)).hexdigest()
        _put_immutable(NAMESPACE,_key(run['run_id'],sha,owner),page,kind='run receipt page')
        for row in page['receipts']:prefix.update(_json(row)+b'\n')
        archived+=PAGE_SIZE
        pointer={'sha256':sha,'count':archived,'prefix_sha256':prefix.hexdigest()}
    return {**run,'receipts':rows[archived:],**({'receipt_archive':pointer} if pointer else {})}


def expand(run,*,user_id):
    if not run or not run.get('receipt_archive'):return run
    from lib.identity import require_user_id
    owner=require_user_id(user_id,context='run receipt archive')
    head=run['receipt_archive'];pointer=head;rows=[];prefixes={}
    _validate(head,'receipt_archive_ref')
    while pointer:
        page=records.get_record(NAMESPACE,_key(run['run_id'],pointer['sha256'],owner))
        if page is None:raise EvolutionStateError('run receipt archive is unavailable')
        from .receipt_validation import validate_page
        try:
            page_digest=hashlib.sha256(_json(page))
        except (ValueError,TypeError) as exc:
            raise EvolutionStateError('run receipt archive contract changed') from exc
        validate_page(page,page_digest.digest(),contract_validator(SCHEMA,'receipt_archive_page'))
        previous=page['previous']
        if (page['run_id']!=run['run_id'] or page['user_id']!=owner
                or page_digest.hexdigest()!=pointer['sha256']
                or (previous['count'] if previous else 0)+PAGE_SIZE!=pointer['count']):
            raise EvolutionStateError('run receipt archive identity changed')
        prefixes[pointer['count']]=pointer['prefix_sha256']
        rows.extend(reversed(page['receipts']));pointer=previous
    # One collector preserves row identities without retaining page containers or
    # making a second full-history list. Never allocate from an untrusted count.
    rows.reverse();digest=hashlib.sha256()
    for index,row in enumerate(rows,1):
        digest.update(_json(row)+b'\n')
        if index in prefixes and digest.hexdigest()!=prefixes[index]:
            raise EvolutionStateError('run receipt archive prefix changed')
    rows.extend(run['receipts'])
    return {**run,'receipts':rows}

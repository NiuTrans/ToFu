"""CAS-reserved owner/kind version numbers before mutation publication.

Entry: reserve. One durable head prevents number reuse after failed publication.
It grants no mutation approval. Healthy contention retries only after observed
head advancement; unchanged/invalid authority refuses instead of busy retrying.
"""
import hashlib
import json
from lib.identity import require_user_id
from lib.storage.errors import StorageError
from .contracts import contract_validator,validate_mutation
from .records import entity_key,get_versioned_record,put_record

NAMESPACE='evolution.mutation_versions.v1'


def _read(key, owner, kind):
    value,physical=get_versioned_record(NAMESPACE,key)
    if value is None:
        return None,0
    try:
        contract_validator('evolution_mutation_version_v1.schema.json').validate(value)
        if (value['user_id']!=owner or value['kind']!=kind or physical<1
                or value['allocated_version']<physical):
            raise ValueError('reservation scope/version differs')
    except Exception as exc:
        raise StorageError('database_integrity','Invalid mutation version head') from exc
    return value,physical


def reserve(mutation, *, user_id, historical_maximum):
    validate_mutation(mutation)
    if type(historical_maximum) is not int or historical_maximum<0:
        raise ValueError('invalid historical mutation version maximum')
    owner=require_user_id(user_id,context='mutation version owner')
    kind=mutation['kind']
    key=entity_key(kind,owner,kind='mutation version kind')
    identity={name:mutation[name] for name in ('mutation_id','kind','payload','evidence_episode_ids','parent_version')}
    fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    while True:
        before,physical=_read(key,owner,kind)
        maximum=max(historical_maximum,before['allocated_version'] if before else 0)
        proposed={'schema_version':1,'user_id':owner,'kind':kind,'allocated_version':maximum+1,
            'mutation_id':mutation['mutation_id'],'request_digest':fingerprint}
        try:
            acknowledged=put_record(NAMESPACE,key,proposed,kind='mutation version',expected_version=physical)
            if acknowledged==physical+1:
                return proposed['allocated_version']
            failure=StorageError('database_internal','Mutation version reservation ACK differs')
        except StorageError as exc:
            failure=exc
        observed,current_physical=_read(key,owner,kind)
        if observed==proposed and current_physical==physical+1:
            return proposed['allocated_version']
        if failure.code in ('database_conflict','database_timeout','database_internal'):
            if (observed is not None and current_physical>physical
                    and observed['allocated_version']>=proposed['allocated_version']):
                # Earlier ownership is unproved after a later reservation. Skip
                # its possible gap and acquire a new number rather than reuse it.
                continue
        raise failure

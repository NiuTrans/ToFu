"""Exact selected replay with shared prefixes, honest corruption and deep chains."""
from copy import deepcopy
import hashlib
import random

import pytest
from lib.tasks_pkg.snapshot_delta import SnapshotProjector, SnapshotReplayIndex, _canon, content_hash, prefix_hash

pytestmark = pytest.mark.unit


def row(payload, sequence=0):
    return {'event_id': sequence, 'type': 'messages_snapshot', 'payload': payload, 'ts_ms': sequence}


def test_index_preserves_originals_across_divergence_turns_kinds_tools_and_reemission():
    generator=random.Random(73);projector=SnapshotProjector();originals=[];stored=[];state={}
    for position in range(180):
        turn=generator.choice(['plan','work','review']);kind=generator.choice(['request','state'])
        messages=list(state.get(turn,[]))
        if position%13==0 and messages:messages=messages[:len(messages)//2]
        if position%7:messages.append({'role':'user','content':f'Exact 🧪 {position} "\\\n','nested':{'value':position/7}})
        state[turn]=messages
        tools=[{'type':'function','function':{'name':f'read_{position//29}'}}]
        original={'type':'messages_snapshot','kind':kind,'turn':turn,'roundNum':position+1,'messages':deepcopy(messages),'tools':tools}
        originals.append(original);stored.append(row(projector.project('controlled',original),position))
    frozen=deepcopy(stored);index=SnapshotReplayIndex(stored)
    for position,original in enumerate(originals):
        rebuilt=index.payload(position)
        assert rebuilt['messages']==original['messages'] and rebuilt['tools']==original['tools']
        assert not rebuilt.get('degraded')
    assert stored==frozen
    first=index.payload(30);first['messages'].append({'role':'assistant','content':'response-local'})
    assert index.payload(30)['messages']==originals[30]['messages']


@pytest.mark.parametrize('version', [None,2])
def test_legacy_migration_and_kind_scopes_keep_exact_prefix_authority(version):
    first={'type':'messages_snapshot','kind':'request','roundNum':1,'messages':[{'role':'user','content':'Legacy exact baseline'}],'tools':[{'name':'read'}]}
    second={'type':'messages_snapshot','kind':'state','roundNum':2,'prefixLen':1,'prefixHash':prefix_hash(first['messages'],1),'newMessages':[{'role':'tool','content':'Result'}],'messageCount':2,'toolsHash':content_hash(first['tools'])}
    if version is not None:second['snapshotDeltaVersion']=version
    index=SnapshotReplayIndex([row(first),row(second)])
    payload=index.payload(1)
    if version==2:
        assert payload['messages']==first['messages']+second['newMessages'] and not payload.get('degraded')
    else:
        assert payload['degraded'] is True and 'baseline' in payload['degradedReason']


@pytest.mark.parametrize('fault', ['missing_baseline','hash','message_count','tools','version'])
def test_corruption_remains_explicit_and_never_silently_becomes_exact(fault):
    projector=SnapshotProjector();first={'type':'messages_snapshot','kind':'request','roundNum':1,'messages':[{'role':'user','content':'Original'}],'tools':[{'name':'read'}]}
    second={**first,'roundNum':2,'messages':first['messages']+[{'role':'tool','content':'Tail'}]}
    stored=[row(projector.project('controlled',first)),row(projector.project('controlled',second))]
    if fault=='missing_baseline':stored=stored[1:]
    elif fault=='hash':stored[-1]['payload']['prefixHash']='0'*16
    elif fault=='message_count':stored[-1]['payload']['messageCount']=999
    elif fault=='tools':stored[-1]['payload']['toolsHash']='unavailable'
    else:stored[-1]['payload']['snapshotDeltaVersion']=999
    index=SnapshotReplayIndex(stored);payload=index.payload(len(stored)-1)
    assert payload['degraded'] is True and payload['degradedReason']


def test_deep_append_only_chain_materializes_selected_payload_without_recursion():
    rows=[];hasher=hashlib.sha256(b'[')
    for position in range(2200):
        prefix=hasher.copy();prefix.update(b']');tail={'role':'user','content':f'Exact {position}'}
        rows.append(row({'type':'messages_snapshot','kind':'request','roundNum':position+1,'snapshotDeltaVersion':2,'prefixLen':position,'prefixHash':prefix.hexdigest()[:16],'messageCount':position+1,'newMessages':[tail]}))
        if position:hasher.update(b',')
        hasher.update(_canon(tail).encode())
    index=SnapshotReplayIndex(rows)
    assert len(index.payload(2199)['messages'])==2200
    assert index.payload(13)['messages'][-1]['content']=='Exact 13'
    assert sum(len(history.tail) for _,history in index.records.values())==2200


def test_legacy_payload_without_a_delta_stays_readable_without_hashing_its_text():
    original={'type':'messages_snapshot','messages':[{'role':'user','content':'legacy lone surrogate \ud800'}]}
    assert SnapshotReplayIndex([row(original)]).payload(0)==original


def test_replaced_suffix_reuses_exact_prefix_without_deepening_old_suffix_chains():
    projector=SnapshotProjector();prefix=[{'role':'user','content':f'Persistent {n}'} for n in range(400)];stored=[]
    for number in range(800):
        messages=prefix+[{'role':'assistant','content':f'Replaced tail {number}'}]
        stored.append(row(projector.project('controlled',{'type':'messages_snapshot','kind':'request','roundNum':number+1,'messages':messages,'tools':[]})))
    index=SnapshotReplayIndex(stored)
    for number in (0,7,300,799):
        assert index.payload(number)['messages']==prefix+[{'role':'assistant','content':f'Replaced tail {number}'}]
        assert not index.payload(number).get('degraded')


@pytest.mark.parametrize('fields', [{}, {'messages': None}, {'messages': 'legacy text'}, {'messages': []}])
def test_legacy_fields_pass_through_without_normalizing_the_original_payload(fields):
    original = {'type': 'messages_snapshot', **fields}
    assert SnapshotReplayIndex([row(original)]).payload(0) == original


def test_unhashed_legacy_delta_tail_stays_readable_until_prefix_verification_requires_it():
    tail = [{'role': 'user', 'content': 'legacy lone surrogate \ud800'}]
    first = {'type': 'messages_snapshot', 'prefixLen': 0, 'newMessages': tail}
    second = {'type': 'messages_snapshot', 'prefixLen': 1}
    assert SnapshotReplayIndex([row(first), row(second)]).payload(1)['messages'] == tail
    with pytest.raises(UnicodeEncodeError):
        SnapshotReplayIndex([row(first), row({**second, 'prefixHash': '0' * 16})])

"""Owned complete-exchange archival, exact restoration and capacity refusal."""
from copy import deepcopy
import hashlib
import json
import pytest
from lib.research import history_context as history

pytestmark = pytest.mark.unit
TOOLS = [{'type':'function','function':{'name':'read_tool_artifact'}}]


@pytest.fixture
def cache(monkeypatch):
    class Cache:
        def __init__(self): self.rows = {}; self.writes = 0
        def put(self, *, user_id, content, media_type):
            self.writes += 1
            ref = 'tool-result:' + hashlib.sha256(content.encode()).hexdigest()
            self.rows[user_id, ref] = content
            return {'artifactRef': ref}
        def read_range(self, *, user_id, artifact_ref, limit):
            return {'content':self.rows[user_id,artifact_ref][:limit]} if (user_id,artifact_ref) in self.rows else None
    cache = Cache()
    monkeypatch.setattr('lib.tool_results.tool_result_artifacts.ToolResultArtifactRepository', lambda: cache)
    monkeypatch.setattr('lib.research.context.action_window', lambda _: 8192)
    monkeypatch.setattr('lib.token_counter.count_text', lambda text, **kwargs: len(text) // 4)
    return cache


def messages():
    return [{'role':'system','content':'Owner instructions'}, {'role':'user','content':'Frozen workspace'},
        {'role':'assistant','tool_calls':[{'id':'old','function':{'name':'run_command','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'old','content':'Original measured evidence 漢字' * 1200},
        {'role':'assistant','tool_calls':[{'id':'recent','function':{'name':'run_command','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'recent','content':'Most recent observed result'}]


def prepare(source, ordinal=1, loader=None):
    return history.prepare_history_messages(source, model='test', tools=TOOLS, max_output=1024,
        user_id=7, task_id='owned-action', checkpoint_ordinal=ordinal,
        load_checkpoint=loader or (lambda _: {'messages':deepcopy(source)}))


def descriptor(prepared):
    return json.loads(prepared[2]['content'])[history.KEY]


def test_archival_preserves_whole_exchanges_and_exact_originals(cache):
    source = messages(); before = deepcopy(source)
    prepared = prepare(source); ref = descriptor(prepared)
    assert source == before and prepared[:2] == source[:2] and prepared[-2:] == source[-2:]
    assert json.loads(cache.rows[7,ref['artifact_ref']])['messages'] == source[2:4]
    assert ref['checkpoint_ordinal'] == 1 and 'do not replay effects' in ref['instruction']
    assert history.admit_messages(prepared, model='test', tools=TOOLS, max_output=1024) > 0


@pytest.mark.parametrize('reader,changed,oversized', [(False,False,False),(True,True,False),(True,False,True)])
def test_refusal_preserves_history_and_does_not_publish_cache(cache, reader, changed, oversized):
    source = messages()
    if oversized: source[0]['content'] = 'large instructions' * 5000
    before = deepcopy(source)
    checkpoint = deepcopy(source)
    if changed: checkpoint[3]['content'] = 'different original'
    with pytest.raises(ValueError):
        history.prepare_history_messages(source, model='test', tools=TOOLS if reader else [], max_output=1024,
            user_id=7, task_id='owned-action', checkpoint_ordinal=1,
            load_checkpoint=lambda _: {'messages':checkpoint})
    assert source == before and cache.writes == 0


@pytest.mark.parametrize('changed', [False,True])
def test_expired_cache_restores_only_digest_matching_checkpoint(cache, changed):
    source = messages(); prepared = prepare(source); ref = descriptor(prepared)['artifact_ref']
    cache.rows.clear()
    checkpoint = deepcopy(source)
    if changed: checkpoint[3]['content'] = 'altered observed result'
    if changed:
        with pytest.raises(ValueError, match='history changed'):
            history.restore_history_reference(prepared, user_id=7, task_id='owned-action', artifact_ref=ref,
                load_checkpoint=lambda _: {'messages':checkpoint})
        assert not cache.rows
    else:
        assert history.restore_history_reference(prepared, user_id=7, task_id='owned-action', artifact_ref=ref,
            load_checkpoint=lambda _: {'messages':checkpoint})
        assert json.loads(cache.rows[7,ref])['messages'] == source[2:4]


@pytest.mark.parametrize('owner,task', [(8,'owned-action'),(7,'foreign-action')])
def test_foreign_scope_does_not_load_originals(cache, owner, task):
    prepared = prepare(messages()); ref = descriptor(prepared)['artifact_ref']; cache.rows.clear()
    assert not history.restore_history_reference(prepared, user_id=owner, task_id=task, artifact_ref=ref,
        load_checkpoint=lambda _: pytest.fail('foreign reference must not read checkpoint'))


def test_nested_expired_reference_restores_without_replaying_history(cache):
    first = messages(); prepared = prepare(first); old_ref = descriptor(prepared)['artifact_ref']
    second = prepared + messages()[2:]
    latest = prepare(second, ordinal=3)
    cache.rows.clear(); originals = {1:first,3:second}; reads = []
    def load(ordinal):
        reads.append(ordinal)
        return {'messages': originals[ordinal]}
    assert history.restore_history_reference(latest, user_id=7, task_id='owned-action',
        artifact_ref=old_ref, load_checkpoint=load)
    assert reads == [3,1] and json.loads(cache.rows[7,old_ref])['messages'] == first[2:4]


def test_small_history_needs_no_checkpoint_read_or_cache_write(cache):
    source = messages(); source[3]['content'] = 'Small observed result'
    prepared = prepare(source, loader=lambda _: pytest.fail('fitting context needs no history I/O'))
    assert prepared is source and cache.writes == 0

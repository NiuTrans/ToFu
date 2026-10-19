"""Public continuation proofs remain exact across long-lived authority changes.

Entry points are real semantic commands against disposable SQLite/native stores.
The independent public-header oracle comes from the context parity suite; no
private native frontier, storage namespace or implementation counter is trusted.
Native runs require a supplied executable and never build or deploy one.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from tests.tofudb.test_context_semantic_revision import (
    _Authority,
    _context,
    _create_pair,
    _seed,
    _settle,
    _turn,
    _update,
    tofudb_binary,  # noqa: F401 -- explicit no-build native fixture
)
from tests.tofudb.test_tofudb_differential import (
    LegacyDriver,
    _running_tofudb_owner,
    integration_owner_daemon,  # noqa: F401 -- disposable authenticated daemon
)

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


@pytest.fixture(params=['sqlite', 'native'])
def frontier_authority(request):
    if request.param == 'sqlite':
        runtime = request.getfixturevalue('chat_sidecar')
        yield _Authority(LegacyDriver(), sql_runtime=runtime)
        return
    authority = _Authority(
        native_factory=request.getfixturevalue('integration_owner_daemon'))
    try:
        yield authority
    finally:
        authority.close()


@pytest.mark.parametrize('forgery', ['digest', 'count', 'empty_prefix'])
def test_supplied_prefix_proof_never_becomes_authoritative(frontier_authority, forgery):
    """A valid terminal witness does not certify an arbitrary earlier prefix."""
    authority = frontier_authority
    scenario = _seed(authority)
    conv = scenario.conversation_id
    candidate = copy.deepcopy(scenario.cached)
    if forgery == 'digest':
        candidate['historyProof']['digest'] = 'f' * 64
    elif forgery == 'count':
        candidate['historyProof']['count'] -= 1
    else:
        candidate['historyProof'] = {
            'version': 2, 'count': 0,
            'digest': hashlib.sha256(b'').hexdigest(),
        }
    # A legacy/import candidate may contain an unverified prefix. Its public
    # storage contract permits retaining it; reuse must independently prove it.
    assert authority.command('model_continuation.delete',
                             conversation_id=conv, lane_id='main') is True
    assert authority.command('model_continuation.put', conversation_id=conv,
                             lane_id='main', projection=candidate) is True
    for reopen in (False, True):
        if reopen:
            authority.restart()
        value = _context(authority, conv, scenario.target_id)
        assert value['covered'] == 0
        assert value['continuation'] is None
        assert [row['turnId'] for row in value['turns']] == scenario.all_ids
    assert authority.command('model_continuation.delete',
                             conversation_id=conv, lane_id='main') is True
    assert authority.command('model_continuation.put', conversation_id=conv,
                             lane_id='main', projection=scenario.cached) is True
    scenario.assert_available()


def test_repeated_settlement_advances_exact_prefix_after_reopen(frontier_authority):
    authority = frontier_authority
    scenario = _seed(authority)
    conv = scenario.conversation_id
    cached, pair = scenario.cached, scenario.pair
    original = _turn(authority, conv, scenario.prefix_id)
    for index in range(8):
        # Presentation enrichment is a real write, but changes no model evidence.
        projection = copy.deepcopy(original['projection'])
        projection['translation'] = {'text': f'display-only-{index}'}
        _update(authority, conv, scenario.prefix_id, projection)
        before = _context(authority, conv, pair['turn']['turnId'])
        assert before['continuationStatus'] == 'available'
        assert before['continuation'] == cached
        assert before['covered'] == cached['historyProof']['count'] + 1
        assert len(before['turns']) == 2
        cached = _settle(authority, conv, pair)
        pair = _create_pair(authority, conv, f'new work {index}: 中文 \\ "')
        if index in (2, 6):
            authority.restart()
        after = _context(authority, conv, pair['turn']['turnId'])
        assert after['continuationStatus'] == 'available'
        assert after['continuation'] == cached
        assert after['covered'] == cached['historyProof']['count'] + 1
        assert after['historyProof']['count'] == before['historyProof']['count'] + 2
        assert len(after['turns']) == 2


def test_edited_prefix_cannot_be_recertified_by_later_settlement(frontier_authority):
    authority = frontier_authority
    scenario = _seed(authority)
    conv = scenario.conversation_id
    # Capture the public context before an editor changes old model evidence.
    old_context = scenario.assert_available()
    original = _turn(authority, conv, scenario.prefix_id)
    edited = copy.deepcopy(original['projection'])
    edited['content'] = 'new evidence committed after model context was read'
    _update(authority, conv, scenario.prefix_id, edited)
    stale_candidate = {
        'messages': [{'role': 'assistant', 'content': 'response to stale context'}],
        'turns': [[scenario.target_id, '0' * 64]],
        'historyProof': old_context['historyProof'],
    }
    settled = authority.command(
        'turn.event.record', attempt_id=scenario.pair['attempt']['attemptId'],
        task_id='frontier-stale-context', terminal=True, status='completed',
        projection={'content': 'response to stale context'},
        settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []},
        model_continuation={'projection': stale_candidate,
                            'content': 'response to stale context'},
    )
    assert settled['applied'] is True
    pair = _create_pair(authority, conv, 'must observe the edit')
    authority.restart()
    value = _context(authority, conv, pair['turn']['turnId'])
    assert value['covered'] == 0 and value['continuation'] is None
    assert next(row for row in value['turns']
                if row['turnId'] == scenario.prefix_id)['projection'] == edited
    # Reverting user content still advances its semantic revision. The old
    # checkpoint cannot come back to life through content equality.
    _update(authority, conv, scenario.prefix_id, original['projection'])
    authority.restart()
    reverted = _context(authority, conv, pair['turn']['turnId'])
    assert reverted['covered'] == 0 and reverted['continuation'] is None


def test_deleted_prefix_cannot_reuse_or_reinstall_old_checkpoint(frontier_authority):
    authority = frontier_authority
    scenario = _seed(authority)
    conv = scenario.conversation_id
    authority.command('turn.delete', conversation_id=conv,
                      turn_ids=[scenario.prefix_id])
    assert _turn(authority, conv, scenario.prefix_id) is None
    authority.command('model_continuation.put', conversation_id=conv,
                      lane_id='main', projection=scenario.cached)
    authority.restart()
    value = _context(authority, conv, scenario.target_id)
    assert value['covered'] == 0 and value['continuation'] is None
    assert scenario.prefix_id not in {row['turnId'] for row in value['turns']}


def test_foreign_conversation_prefix_cannot_certify_local_terminal(frontier_authority):
    authority = frontier_authority
    left, right = _seed(authority), _seed(authority)
    candidate = copy.deepcopy(left.cached)
    candidate['historyProof'] = copy.deepcopy(right.cached['historyProof'])
    assert candidate['historyProof'] != left.cached['historyProof']
    authority.command('model_continuation.delete',
                      conversation_id=left.conversation_id, lane_id='main')
    assert authority.command('model_continuation.put',
                             conversation_id=left.conversation_id, lane_id='main',
                             projection=candidate) is True
    authority.restart()
    value = left.context()
    assert value['covered'] == 0 and value['continuation'] is None
    right.assert_available()


def test_compaction_replacement_invalidates_persisted_prefix(frontier_authority):
    authority = frontier_authority
    scenario = _seed(authority)
    conv = scenario.conversation_id
    cached = _settle(authority, conv, scenario.pair)
    before = authority.query('turn.revision', conversation_id=conv)
    summary_id = conv + '-summary'
    compacted = authority.command(
        'turn.compact', conversation_id=conv,
        expected_conversation_revision=before,
        summary_turn_id=summary_id,
        summary_projection={'content': 'replacement history',
                            'compaction': {'blockId': 'compaction'}},
        delete_turn_ids=scenario.all_ids[:2], projection_updates=[],
        insert_before_turn_id=scenario.all_ids[2],
    )
    assert compacted['applied'] is True
    authority.command('model_continuation.put', conversation_id=conv,
                      lane_id='main', projection=cached)
    pair = _create_pair(authority, conv, 'continue after compaction')
    authority.restart()
    value = _context(authority, conv, pair['turn']['turnId'])
    assert value['covered'] == 0 and value['continuation'] is None
    assert value['turns'][0]['turnId'] == summary_id
    assert value['turns'][0]['projection']['content'] == 'replacement history'
    assert not set(scenario.all_ids[:2]).intersection(
        row['turnId'] for row in value['turns'])


def test_generic_record_alias_cannot_replace_private_context_evidence(frontier_authority):
    authority = frontier_authority
    scenario = _seed(authority)
    before = scenario.assert_available()
    alias = (hashlib.sha256(scenario.conversation_id.encode()).hexdigest()
             + ':' + hashlib.sha256(b'main').hexdigest())
    authority.command(
        'record.put', namespace='model.continuation.v1.lanes', key=alias,
        value={'count': 0, 'digest': 'f' * 64, 'owner': authority.owner_id},
    )
    authority.restart()
    assert scenario.assert_available() == before


def test_old_writer_edit_cannot_reuse_new_writer_prefix(tofudb_binary, tmp_path):
    """An actual earlier writer can mutate history without the new cache hook."""
    supplied = os.environ.get('TOFUDB_PRE_FRONTIER_BIN')
    if supplied is None:
        pytest.skip('supply the frozen pre-frontier binary for a real downgrade test')
    old_binary = Path(supplied).resolve(strict=True)
    assert old_binary != tofudb_binary
    data_dir = tmp_path / 'authority'
    subprocess.run([str(tofudb_binary), 'init', '--data-dir', str(data_dir)],
                   check=True, capture_output=True, text=True, timeout=120)
    selected = [tofudb_binary]
    authority = _Authority(native_factory=lambda owner:
                           _running_tofudb_owner(selected[0], data_dir, owner))
    try:
        scenario = _seed(authority)
        scenario.assert_available()
        conv = scenario.conversation_id
        original = _turn(authority, conv, scenario.prefix_id)
        selected[0] = old_binary
        authority.restart()
        edited = copy.deepcopy(original['projection'])
        edited['content'] = 'old writer committed new model evidence'
        _update(authority, conv, scenario.prefix_id, edited)
        selected[0] = tofudb_binary
        authority.restart()
        value = scenario.context()
        assert value['covered'] == 0 and value['continuation'] is None
        assert next(row for row in value['turns']
                    if row['turnId'] == scenario.prefix_id)['projection'] == edited
    finally:
        authority.close()

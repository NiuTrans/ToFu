"""Public-storage specifications for semantic-revision context continuations.

Both authorities use disposable local stores and the real differential harness.
Native tests use an already-built binary only: TOFUDB_DIFFERENTIAL_BIN
may name a supplied artifact; otherwise the worktree's debug binary is used if
present. This module deliberately overrides the build-triggering fixture.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.tofudb.test_tofudb_differential import (
    LegacyDriver,
    _OWNER_ID,
    integration_owner_daemon,  # noqa: F401 -- reused disposable native fixture
)

from tests.support.postgres_cluster import postgres_cluster  # noqa: F401 -- private fixture

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit

_PROJECT = Path(__file__).resolve().parents[2]
_NATIVE_BINARY_ENV = 'TOFUDB_DIFFERENTIAL_BIN'


@pytest.fixture(scope='module')
def tofudb_binary():
    """Accept a supplied native executable, never invoke Cargo or a build script."""
    supplied = os.environ.get(_NATIVE_BINARY_ENV)
    binary = (Path(supplied).expanduser().resolve() if supplied else
              _PROJECT / 'packages/tofu-db/target/debug/tofu-db')
    if not binary.is_file() or not os.access(binary, os.X_OK):
        if supplied:
            pytest.fail(f'{_NATIVE_BINARY_ENV} is not an executable file: {binary}')
        pytest.skip('native binary not supplied; no native build is allowed here')
    return binary


class _Authority:
    """Real semantic operations with bounded restart and authenticated-owner scopes."""

    def __init__(self, driver=None, *, native_factory=None, sql_runtime=None, driver_factory=LegacyDriver):
        self.driver = driver
        self.sql_runtime = sql_runtime

        self.driver_factory = driver_factory
        self.native_factory = native_factory
        self.owner_id = _OWNER_ID
        self._native_context = None
        if native_factory is not None:
            self.restart()

    def close(self):
        if self._native_context is not None:
            context, self._native_context = self._native_context, None
            context.__exit__(None, None, None)

    def restart(self):
        if self.native_factory is not None:
            self.close()
            self._native_context = self.native_factory(self.owner_id)
            self.driver = self._native_context.__enter__()
        else:
            self.sql_runtime.stop()
            self.sql_runtime.start()
            self.driver = self.driver_factory()

    @contextmanager
    def as_owner(self, owner_id):
        """Native identity is authenticated by a new daemon, never payload spoofing."""
        self.owner_id = owner_id
        if self.native_factory is not None:
            self.restart()
        try:
            yield self
        finally:
            self.owner_id = _OWNER_ID
            if self.native_factory is not None:
                self.restart()

    def outcome(self, operation, *, command=False, **payload):
        return self.driver.execute(
            operation, {'user_id': self.owner_id, **payload},
            'context-semantic-' + uuid.uuid4().hex if command else None,
        )

    def query(self, operation, **payload):
        result = self.outcome(operation, **payload)
        assert result.ok, (operation, result.error_code, result.error_message)
        return result.value

    def command(self, operation, **payload):
        result = self.outcome(operation, command=True, **payload)
        assert result.ok, (operation, result.error_code, result.error_message)
        return result.value


@pytest.fixture(params=['sqlite', 'native', 'postgres'])
def semantic_authority(request):
    if request.param == 'sqlite':
        runtime = request.getfixturevalue('chat_sidecar')
        yield _Authority(LegacyDriver(), sql_runtime=runtime)
        return
    if request.param == 'postgres':
        from lib.storage import StorageSupervisor, StorageError
        from tests.tofudb.test_tofudb_differential import Outcome, _normalize
        root = request.getfixturevalue('tmp_path')
        monkeypatch = request.getfixturevalue('monkeypatch')
        dsn_file = root / 'private-postgres-dsn'
        dsn_file.write_text(str(request.getfixturevalue('postgres_cluster')))
        dsn_file.chmod(0o600)
        monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', str(dsn_file))
        supervisor = StorageSupervisor(project_root=root, backend='postgres', startup_timeout=60)
        supervisor.start()
        class Driver:
            def execute(self, operation, payload, command_id):
                try:
                    value = (supervisor.client.query(operation, payload) if command_id is None
                             else supervisor.client.command(operation, payload, command_id))
                    return Outcome(ok=True, value=_normalize(value))
                except StorageError as exc:
                    return Outcome(ok=False, error_code=exc.code, error_message=str(exc))
        try:
            yield _Authority(Driver(), sql_runtime=supervisor, driver_factory=Driver)
        finally:
            supervisor.stop()
        return
    authority = _Authority(
        native_factory=request.getfixturevalue('integration_owner_daemon'))
    try:
        yield authority
    finally:
        authority.close()


def _projection(content):
    """Small real tool evidence with similarly named genuine argument data."""
    rounds = []
    for index in range(2):
        rounds.append({
            'roundNum': index + 1,
            'toolCallId': f'call-{index}',
            'toolName': 'run_command',
            'toolArgs': {
                'command': 'printf context',

                'zeroFlag': 0,
                'translatedContent': 'tool argument evidence',
                'translatedText': 'tool argument evidence',
                '_translateDone': False,
                'translation': {'text': 'argument translation'},
                'usage': {'value': 1},
                'timingTrace': {'value': 1},
                '_swarmSnapshot': {'value': 1},
                'toolRounds': [{'_swarmSnapshot': {'value': 1}}],
                'segments': [{'_round': {'_swarmSnapshot': {'value': 1}}}],
            },
            'toolContent': f'evidence-{index}',
            'status': 'done',
            '_swarmSnapshot': {'phase': 'display-original'},
        })
    return {
        'content': content,
        'thinking': 'model-visible reasoning',
        'translation': {'text': 'display-original'},
        'usage': {'input': 1, 'output': 2},
        'timingTrace': {'elapsed': 1},
        'toolRounds': rounds,
        'segments': [
            {'kind': 'tool', 'segmentId': f'segment-{index}',
             '_round': copy.deepcopy(round_entry)}
            for index, round_entry in enumerate(rounds)
        ],
    }


def _model_revision(row):
    return row.get('modelContextRevision', row['projectionRevision'])


def _history_proof(rows, target_id, *, version=2):
    """Independent proof oracle over public Turn headers, not production helpers."""
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        if row['turnId'] == target_id:
            return {'version': version, 'count': count, 'digest': digest.hexdigest()}
        revision = (_model_revision(row) if version == 2 else
                    row['projectionRevision'])
        header = [row['turnId'], row.get('parentTurnId'), row['status'],
                  row.get('currentAttemptId'), revision]
        digest.update((json.dumps(header, ensure_ascii=False,
                                  separators=(',', ':')) + '\n').encode('utf-8'))
        count += 1
    raise AssertionError(f'target missing from public Turn order: {target_id}')


def _turn(authority, conversation_id, turn_id):
    return authority.query('turn.get', conversation_id=conversation_id,
                           turn_id=turn_id)


def _context(authority, conversation_id, target_id, *, replay=True):
    result = authority.query('turn.context.read', conversation_id=conversation_id,
                             turn_id=target_id, replay=replay)
    rows = authority.query('turn.list', conversation_id=conversation_id,
                           lane_id='main')
    assert result['supported'] is True
    assert result['nextTurnId'] is None
    assert result['proofEligible'] is True
    assert result['historyProof'] == _history_proof(rows, target_id)
    return result


def _create_pair(authority, conversation_id, content):
    return authority.command(
        'turn.create_pair', conversation_id=conversation_id,
        command_id=uuid.uuid4().hex, input_projection={'content': content},
        config={'model': 'context-semantic-test', 'excludeLast': True},
    )


def _settle(authority, conversation_id, pair, *, version=2):
    """Let public atomic settlement mint the terminal witness and checkpoint."""
    target_id = pair['turn']['turnId']
    context = _context(authority, conversation_id, target_id, replay=False)
    proof = context['historyProof']
    if version == 1:
        proof = _history_proof(context['turns'], target_id, version=1)
    candidate = {
        'messages': [
            {'role': 'user', 'content': 'retained history'},
            {'role': 'assistant', 'content': 'retained context checkpoint'},
        ],
        # Settlement replaces this placeholder with its own authoritative witness.
        'turns': [[target_id, '0' * 64]],
        'historyProof': proof,
    }
    result = authority.command(
        'turn.event.record', attempt_id=pair['attempt']['attemptId'],
        task_id='context-semantic-task', terminal=True, status='completed',
        projection=_projection('settled answer'),
        settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []},
        model_continuation={'projection': candidate, 'content': 'settled answer'},
    )
    assert result['applied'] is True
    assert result['model_continuation_status'] == 'stored'
    cached = authority.query('model_continuation.get',
                             conversation_id=conversation_id, lane_id='main')
    assert cached['historyProof'] == proof
    assert cached['messages'] == candidate['messages']
    assert cached['turns'][0][0] == target_id
    assert cached['turns'][0][1] != '0' * 64
    return cached


@dataclass
class _Scenario:
    authority: _Authority
    conversation_id: str
    prefix_id: str
    terminal_id: str
    pair: dict
    cached: dict
    all_ids: list[str]

    @property
    def target_id(self):
        return self.pair['turn']['turnId']

    def row_id(self, location):
        return self.prefix_id if location == 'prefix' else self.terminal_id

    def context(self):
        return _context(self.authority, self.conversation_id, self.target_id)

    def assert_available(self):
        context = self.context()
        covered = self.cached['historyProof']['count'] + 1
        assert context['continuationStatus'] == 'available'
        assert context['covered'] == covered
        assert context['continuation'] == self.cached
        assert [row['turnId'] for row in context['turns']] == self.all_ids[covered:]
        assert context['turns'][-1]['turnId'] == self.target_id
        return context

    def assert_cold(self, status):
        context = self.context()
        assert context['continuationStatus'] == status
        assert context['covered'] == 0
        assert context['continuation'] is None
        assert [row['turnId'] for row in context['turns']] == self.all_ids
        return context


def _seed(authority, *, version=2):
    conversation_id = 'context-semantic-' + uuid.uuid4().hex
    authority.command('conversation.create', conv_id=conversation_id)
    prefix_id = conversation_id + '-older-assistant'
    for turn_id, actor, projection in [
        (conversation_id + '-first-human', 'human', {'content': 'original question'}),
        (prefix_id, 'assistant', _projection('older settled answer')),
    ]:
        authority.command(
            'turn.append_settled', conversation_id=conversation_id,
            command_id=uuid.uuid4().hex, turn_id=turn_id, actor=actor,
            status='completed', projection=projection, lane_id='main',
            created_at=1_760_000_000_000,
        )
    terminal_pair = _create_pair(authority, conversation_id, 'checkpoint question')
    cached = _settle(authority, conversation_id, terminal_pair, version=version)
    next_pair = _create_pair(authority, conversation_id, 'new question')
    rows = authority.query('turn.list', conversation_id=conversation_id, lane_id='main')
    scenario = _Scenario(
        authority, conversation_id, prefix_id, terminal_pair['turn']['turnId'],
        next_pair, cached, [row['turnId'] for row in rows],
    )
    assert len(rows) == 6
    assert cached['historyProof']['count'] == 3
    if version == 2:
        scenario.assert_available()
    return scenario


@pytest.fixture
def scenario(semantic_authority):
    return _seed(semantic_authority)


def _update(authority, conversation_id, turn_id, projection):
    before = _turn(authority, conversation_id, turn_id)
    result = authority.command(
        'turn.projection.update', conversation_id=conversation_id, turn_id=turn_id,
        expected_projection_revision=before['projectionRevision'],
        projection=projection,
    )
    after = _turn(authority, conversation_id, turn_id)
    assert result['turn'] == after
    assert after['projection'] == projection
    assert after['projectionRevision'] == before['projectionRevision'] + 1
    return before, after


def _change_path(projection, path, *, remove=False):
    changed = copy.deepcopy(projection)
    parent = changed
    for key in path[:-1]:
        parent = parent[key]
    if remove:
        del parent[path[-1]]
    else:
        old = parent[path[-1]]
        parent[path[-1]] = ('genuinely changed model content' if isinstance(old, str)
                           else {'value': 'changed'})
    return changed


_DISPLAY_PATHS = [
    pytest.param(('translation',), id='translation'),
    pytest.param(('usage',), id='usage'),
    pytest.param(('timingTrace',), id='timingTrace'),
    pytest.param(('toolRounds', 0, '_swarmSnapshot'), id='round-0-swarm'),
    pytest.param(('toolRounds', 1, '_swarmSnapshot'), id='round-1-swarm'),
    pytest.param(('segments', 0, '_round', '_swarmSnapshot'), id='segment-0-swarm'),
    pytest.param(('segments', 1, '_round', '_swarmSnapshot'), id='segment-1-swarm'),
]

_TRUE_EDIT_PATHS = [
    pytest.param(('toolRounds', 0, 'toolArgs', 'translatedContent'), id='argument-translated-content'),
    pytest.param(('toolRounds', 0, 'toolArgs', 'translatedText'), id='argument-translated-text'),
    pytest.param(('toolRounds', 0, 'toolArgs', '_translateDone'), id='argument-translate-done'),
    pytest.param(('content',), id='content'),
    pytest.param(('thinking',), id='thinking'),
    pytest.param(('toolRounds', 0, 'toolContent'), id='tool-evidence'),
    pytest.param(('toolRounds', 0, 'toolArgs', 'translation'), id='argument-translation'),
    pytest.param(('toolRounds', 0, 'toolArgs', 'usage'), id='argument-usage'),
    pytest.param(('toolRounds', 0, 'toolArgs', 'timingTrace'), id='argument-timingTrace'),
    pytest.param(('toolRounds', 0, 'toolArgs', '_swarmSnapshot'), id='argument-swarm'),
    pytest.param(('toolRounds', 0, 'toolArgs', 'toolRounds', 0, '_swarmSnapshot'),
                 id='argument-nested-round-swarm'),
    pytest.param(('segments', 0, '_round', 'toolArgs', 'segments', 0,
                  '_round', '_swarmSnapshot'), id='argument-nested-segment-swarm'),
]


@pytest.mark.parametrize('location', ['prefix', 'terminal'])
@pytest.mark.parametrize('path', _DISPLAY_PATHS)
def test_display_enrichment_and_removal_preserve_v2_continuation(scenario, location, path):
    authority, conv = scenario.authority, scenario.conversation_id
    row_id = scenario.row_id(location)
    initial = _turn(authority, conv, row_id)
    original_proof = scenario.context()['historyProof']
    changed = _change_path(initial['projection'], path)
    _, enriched = _update(authority, conv, row_id, changed)
    assert _model_revision(enriched) == _model_revision(initial)
    assert scenario.assert_available()['historyProof'] == original_proof

    # Removing a declared display field is also display-only, not just replacing it.
    removed = _change_path(enriched['projection'], path, remove=True)
    _, stripped = _update(authority, conv, row_id, removed)
    assert _model_revision(stripped) == _model_revision(initial)
    assert stripped['projectionRevision'] == initial['projectionRevision'] + 2
    assert scenario.assert_available()['historyProof'] == original_proof


@pytest.mark.parametrize('location', ['prefix', 'terminal'])
@pytest.mark.parametrize('path', _TRUE_EDIT_PATHS)
def test_model_content_and_tool_argument_edits_reject_cached_context(scenario, location, path):
    authority, conv = scenario.authority, scenario.conversation_id
    row_id = scenario.row_id(location)
    initial = _turn(authority, conv, row_id)
    _, edited = _update(authority, conv, row_id, _change_path(initial['projection'], path))
    assert _model_revision(edited) == _model_revision(initial) + 1
    context = scenario.assert_cold(
        'history_changed' if location == 'prefix' else 'terminal_changed')
    returned = next(row for row in context['turns'] if row['turnId'] == row_id)
    assert returned['projection'] == edited['projection']


@pytest.mark.parametrize('location', ['prefix', 'terminal'])
def test_true_edit_then_revert_cannot_resurrect_stale_checkpoint(scenario, location):
    authority, conv = scenario.authority, scenario.conversation_id
    row_id = scenario.row_id(location)
    original = _turn(authority, conv, row_id)
    status = 'history_changed' if location == 'prefix' else 'terminal_changed'
    _update(authority, conv, row_id,
            _change_path(original['projection'], ('content',)))
    scenario.assert_cold(status)
    _, reverted = _update(authority, conv, row_id, copy.deepcopy(original['projection']))
    assert reverted['projection'] == original['projection']
    assert reverted['projectionRevision'] == original['projectionRevision'] + 2
    assert _model_revision(reverted) == _model_revision(original) + 2
    scenario.assert_cold(status)


def test_verified_display_updates_and_checkpoint_survive_authority_restart(scenario):
    authority, conv = scenario.authority, scenario.conversation_id
    revisions = {}
    for location, path in [('prefix', ('translation',)),
                           ('terminal', ('segments', 1, '_round', '_swarmSnapshot'))]:
        row_id = scenario.row_id(location)
        original = _turn(authority, conv, row_id)
        _, updated = _update(authority, conv, row_id,
                             _change_path(original['projection'], path))
        revisions[row_id] = (updated['projectionRevision'], _model_revision(updated))
    before = scenario.assert_available()
    authority.restart()
    assert authority.query('model_continuation.get', conversation_id=conv,
                           lane_id='main') == scenario.cached
    assert scenario.assert_available() == before
    for row_id, expected in revisions.items():
        row = _turn(authority, conv, row_id)
        assert (row['projectionRevision'], _model_revision(row)) == expected


def test_display_updates_preserve_projection_cas_and_checkpoint_frontier_cas(scenario):
    authority, conv = scenario.authority, scenario.conversation_id
    row_id = scenario.terminal_id
    original = _turn(authority, conv, row_id)
    _, updated = _update(authority, conv, row_id,
                         _change_path(original['projection'], ('usage',)))
    stale = authority.outcome(
        'turn.projection.update', command=True, conversation_id=conv, turn_id=row_id,
        expected_projection_revision=original['projectionRevision'],
        projection={'content': 'stale editor must not win'},
    )
    assert not stale.ok and stale.error_code == 'turn_projection_stale'
    assert _turn(authority, conv, row_id) == updated
    assert authority.command('model_continuation.put', conversation_id=conv,
                             lane_id='main', projection=scenario.cached) is True

    divergent = copy.deepcopy(scenario.cached)
    divergent['messages'][-1]['content'] = 'same-frontier replacement must not win'
    assert authority.command('model_continuation.put', conversation_id=conv,
                             lane_id='main', projection=divergent) is False
    forged = copy.deepcopy(scenario.cached)
    forged['turns'][0][1] = 'f' * 64
    assert authority.command('model_continuation.put', conversation_id=conv,
                             lane_id='main', projection=forged) is False
    scenario.assert_available()

    # A genuinely later settlement advances the frontier; the old checkpoint
    # cannot overwrite it even though its terminal witness still matches.
    advanced = _settle(authority, conv, scenario.pair)
    assert authority.command('model_continuation.put', conversation_id=conv,
                             lane_id='main', projection=scenario.cached) is False
    assert authority.query('model_continuation.get', conversation_id=conv,
                           lane_id='main') == advanced


def test_foreign_owner_cannot_read_replace_or_delete_checkpoint(scenario):
    authority, conv = scenario.authority, scenario.conversation_id
    with authority.as_owner(_OWNER_ID + 1):
        assert authority.query('turn.get', conversation_id=conv,
                               turn_id=scenario.terminal_id) is None
        assert authority.query('turn.context.read', conversation_id=conv,
                               turn_id=scenario.target_id) is None
        assert authority.query('model_continuation.get', conversation_id=conv,
                               lane_id='main') is None
        assert authority.command('model_continuation.put', conversation_id=conv,
                                 lane_id='main', projection=scenario.cached) is False
        assert authority.command('model_continuation.delete', conversation_id=conv,
                                 lane_id='main') is False
    assert authority.query('model_continuation.get', conversation_id=conv,
                           lane_id='main') == scenario.cached
    scenario.assert_available()


@pytest.mark.parametrize('target_location', ['prefix', 'terminal'])
def test_checkpoint_is_not_reused_for_its_terminal_or_an_earlier_target(scenario, target_location):
    target_id = scenario.row_id(target_location)
    context = _context(scenario.authority, scenario.conversation_id, target_id)
    assert context['continuationStatus'] == 'checkpoint_not_before_target'
    assert context['covered'] == 0
    assert context['continuation'] is None
    target_index = scenario.all_ids.index(target_id)
    assert [row['turnId'] for row in context['turns']] == scenario.all_ids[:target_index + 1]


def test_missing_checkpoint_returns_full_history_and_explicit_status(scenario):
    authority, conv = scenario.authority, scenario.conversation_id
    assert authority.command('model_continuation.delete', conversation_id=conv,
                             lane_id='main') is True
    assert authority.query('model_continuation.get', conversation_id=conv,
                           lane_id='main') is None
    scenario.assert_cold('checkpoint_missing')


def test_legacy_v1_checkpoint_is_cold_until_one_v2_refresh_then_reusable(semantic_authority):
    scenario = _seed(semantic_authority, version=1)
    authority, conv = semantic_authority, scenario.conversation_id
    assert scenario.cached['historyProof']['version'] == 1
    for _ in range(2):
        # Repeated reads must not implicitly promote an unverifiable old proof.
        scenario.assert_cold('legacy_proof')
    assert authority.query('model_continuation.get', conversation_id=conv,
                           lane_id='main') == scenario.cached

    refreshed = _settle(authority, conv, scenario.pair)
    assert refreshed['historyProof']['version'] == 2
    next_pair = _create_pair(authority, conv, 'question after v2 refresh')
    authority.restart()
    context = _context(authority, conv, next_pair['turn']['turnId'])
    assert context['continuationStatus'] == 'available'
    assert context['continuation'] == refreshed
    assert context['covered'] == refreshed['historyProof']['count'] + 1
    assert [row['turnId'] for row in context['turns']] == [
        next_pair['submittedTurn']['turnId'], next_pair['turn']['turnId'],
    ]
    assert context['historyProof']['version'] == 2


@pytest.mark.parametrize('location', ['prefix', 'terminal'])
@pytest.mark.parametrize('field,new_value', [('zeroFlag', False), ('usage', True), ('usage', 1.0)])
def test_json_type_changes_are_real_edits_on_both_authorities(scenario, location, field, new_value):
    authority, conv = scenario.authority, scenario.conversation_id
    row_id = scenario.row_id(location)
    original = _turn(authority, conv, row_id)
    changed = copy.deepcopy(original['projection'])
    args = changed['toolRounds'][0]['toolArgs']
    if field == 'usage':
        assert type(args['usage']['value']) is int and args['usage']['value'] == 1
        args['usage']['value'] = new_value
    else:
        assert type(args[field]) is int and args[field] == 0
        args[field] = new_value
    _, updated = _update(authority, conv, row_id, changed)
    assert _model_revision(updated) == _model_revision(original) + 1
    scenario.assert_cold('history_changed' if location == 'prefix' else 'terminal_changed')


def test_offline_sql_turn_projection_preserves_only_valid_nonzero_display_offset():
    from lib.storage_authority_sql.migration.tofudb_migration_import import _turn_document, MigrationImportError
    row = {'turn_id': 'a', 'conversation_id': 'conv', 'presentation_id': 'a',
           'user_id': 11, 'lane_id': 'main', 'parent_turn_id': None, 'ordinal': 1,
           'actor': 'assistant', 'kind': 'real', 'run_id': '', 'status': 'completed',
           'current_attempt_id': None, 'projection_json': '{"content":"same"}',
           'projection_revision': 9, 'settlement_json': '{}', 'created_at': 1, 'updated_at': 2}
    legacy = _turn_document(row)
    assert 'model_context_display_offset' not in legacy
    assert _turn_document(row | {'model_context_display_offset': 0}) == legacy
    with_offset = _turn_document(row | {'model_context_display_offset': 3})
    assert with_offset == legacy | {'model_context_display_offset': 3}
    with pytest.raises(MigrationImportError, match='offset exceeds revision'):
        _turn_document(row | {'model_context_display_offset': 10})


@pytest.mark.parametrize('location', ['prefix', 'terminal'])
@pytest.mark.parametrize('field', ['translatedContent', None, 'content', 'complete', 'failure'])
def test_real_translation_writer_preserves_only_display_context(semantic_authority, monkeypatch, location, field):
    """The real producer CAS-writes overlays through each selected authority."""
    from lib.translate.commit import commit_translation_to_turn
    from lib.storage.errors import StorageError
    from lib.turn_projection_segments import projection_with_stable_segments
    def source_projection(content):
        return projection_with_stable_segments({
            'content': content, 'segments': [
                {'type': 'text', 'blockId': 'text:narration', 'text': 'source narration',
                 'deliverable': False},
                {'type': 'thinking', 'blockId': 'thinking:reason', 'text': 'source reasoning'},
                {'type': 'text', 'blockId': 'text:terminal', 'text': content,
                 'deliverable': True, 'terminal': True},
            ]}, actor='assistant', status='completed')
    monkeypatch.setattr(__name__ + '._projection', source_projection)
    scenario = _seed(semantic_authority)
    authority, conv = scenario.authority, scenario.conversation_id
    class Client:
        def query(self, operation, payload, **kwargs):
            return authority.query(operation, **payload)
        def command(self, operation, payload, command_id=None, **kwargs):
            outcome = authority.outcome(operation, command=True, **payload)
            if not outcome.ok:
                raise StorageError(outcome.error_code, outcome.error_message)
            return outcome.value
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: Client())
    notifications = []
    monkeypatch.setattr('lib.conversations.notify_conv_changed',
                        lambda *args, **kwargs: notifications.append(kwargs))
    row_id = scenario.row_id(location)
    before = _turn(authority, conv, row_id)
    if field == 'complete':
        from lib.translate.commit import mark_turn_translation_complete
        result = mark_turn_translation_complete(conv, row_id, user_id=authority.owner_id)
    elif field == 'failure':
        from lib.translate.commit import mark_turn_translation_failed
        result = mark_turn_translation_failed(conv, row_id, user_id=authority.owner_id,
                                              reason='synthetic', error='synthetic provider failure')
    else:
        result = commit_translation_to_turn(
            conv, row_id, field, 'translated answer', user_id=authority.owner_id,
            model='synthetic translator',
            segment_translations={'text:narration': 'translated narration',
                                  'thinking:reason': 'translated reasoning'},
            segment_sources={'text:narration': 'source narration',
                             'thinking:reason': 'source reasoning'},
        )
    after = _turn(authority, conv, row_id)
    assert after['projectionRevision'] > before['projectionRevision']
    if field not in ('complete', 'failure'):
        assert next(s for s in after['projection']['segments'] if s['blockId']=='text:narration')['translatedText'] == 'translated narration'
        assert next(s for s in after['projection']['segments'] if s['blockId']=='thinking:reason')['translatedText'] == 'translated reasoning'
    assert notifications[-1]['rev'] == result['conversationRevision']
    if field == 'content':
        assert after['modelContextRevision'] > before['modelContextRevision']
        scenario.assert_cold('history_changed' if location == 'prefix' else 'terminal_changed')
    else:
        assert after['modelContextRevision'] == before['modelContextRevision']
        assert after['projection']['content'] == before['projection']['content']
        scenario.assert_available()
        # Removing all producer-owned display overlays is still display-only.
        projection = copy.deepcopy(before['projection'])
        _, removed = _update(authority, conv, row_id, projection)
        assert removed['modelContextRevision'] == before['modelContextRevision']
        scenario.assert_available()


def test_generated_model_context_projection_rules_are_current():
    import subprocess
    import sys
    subprocess.run([sys.executable, str(_PROJECT / 'scripts/gen_model_context_projection.py'),
                    '--check'], check=True, timeout=10)

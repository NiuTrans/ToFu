"""Owner-scoped abort tombstones for registry-lost conversation tasks.\n\nA worker may outlive its in-memory registry entry. Abort routes therefore\npersist an owner-scoped signal that the worker consumes at its next check.\n"""


import pytest

import routes.chat_poll_abort as cpa
import lib.tasks_pkg.manager._registry as reg
import lib.tasks_pkg.manager.runtime as mstate
from tests.support.chat_tasks import (
    chat_task_fixture_guard,
    chat_task_registry,
)


# ── semantic storage fake ────────────────────────────────────────────

class _FakeStorageClient:
    def __init__(self, *, running_ids=(), requested_ids=(), max_ts=None):
        self.running_ids = list(running_ids)
        self.requested_ids = set(requested_ids)
        self.max_ts = max_ts
        self.commands = []

    def query(self, operation, payload, **_kwargs):
        if operation == 'event.latest':
            return ({'created_at_ms': self.max_ts}
                    if self.max_ts is not None else None)
        if operation == 'task_results.abort_requested':
            return {'requested': payload['task_id'] in self.requested_ids}
        if operation == 'task_results.summary_list':
            return {'records': [
                {'key': task_id, 'task_id': task_id}
                for task_id in self.running_ids
            ], 'capped': False}
        raise AssertionError(f'unexpected query: {operation}')

    def command(self, operation, payload, command_id, **_kwargs):
        assert operation == 'task_results.abort'
        self.commands.append((operation, dict(payload), command_id))
        if payload['task_id'] not in self.running_ids:
            return {'signaled': False, 'changed': False}
        changed = payload['task_id'] not in self.requested_ids
        self.requested_ids.add(payload['task_id'])
        return {'signaled': True, 'changed': changed}


def _install_storage(monkeypatch, client):
    import lib.storage
    monkeypatch.setattr(
        lib.storage, 'get_storage_client',
        lambda write=False: client, raising=True)


@pytest.fixture
def clean_tombstones():
    with mstate._abort_tombstones_lock:
        mstate._abort_tombstones.clear()
    yield
    with mstate._abort_tombstones_lock:
        mstate._abort_tombstones.clear()


# ── Abort tombstone channel ─────────────────────────────────────────

@pytest.mark.unit
class TestAbortTombstone:

    def test_plant_requires_running_row(self, monkeypatch, clean_tombstones):
        fake = _FakeStorageClient()
        _install_storage(monkeypatch, fake)
        assert reg.plant_abort_tombstone(
            't-dead', source='test', user_id=1) is False
        assert not reg.has_abort_tombstone('t-dead')

    def test_plant_writes_owner_scoped_signal_and_memory(
            self, monkeypatch, clean_tombstones):
        fake = _FakeStorageClient(running_ids=['t-live'])
        _install_storage(monkeypatch, fake)
        assert reg.plant_abort_tombstone(
            't-live', source='test', user_id=7) is True
        assert reg.has_abort_tombstone('t-live')
        operation, payload, _ = fake.commands[0]
        assert operation == 'task_results.abort'
        assert payload == {
            'task_id': 't-live', 'source': 'test', 'user_id': 7}

    def test_abort_check_consumes_memory_tombstone(self, clean_tombstones):
        task = {'id': 't-ghost', '_userId': 1, 'aborted': False}
        with mstate._abort_tombstones_lock:
            mstate._abort_tombstones.add('t-ghost')
        check = reg.make_task_abort_check(task)
        assert check() is True
        assert check() is True  # hit latched

    def test_abort_check_db_channel(self, monkeypatch, clean_tombstones):
        monkeypatch.setattr(
            reg, '_db_abort_tombstoned',
            lambda tid, *, user_id: user_id == 7)
        task = {'id': 't-ghost2', '_userId': 7, 'aborted': False}
        check = reg.make_task_abort_check(task)
        assert check() is True  # first call reads DB (last_db=0.0)

    def test_abort_check_plain_flag_and_negative(self, monkeypatch,
                                                 clean_tombstones):
        monkeypatch.setattr(
            reg, '_db_abort_tombstoned',
            lambda tid, *, user_id: False)
        assert reg.make_task_abort_check(
            {'id': 'a', '_userId': 1, 'aborted': True})() is True
        assert reg.make_task_abort_check(
            {'id': 'b', '_userId': 1, 'aborted': False})() is False

    def test_end_to_end_vanished_task(self, monkeypatch, clean_tombstones):
        """create → vanish from registry → plant → worker's check sees it."""
        fake = _FakeStorageClient(running_ids=['t-vanished'])
        _install_storage(monkeypatch, fake)
        task = {'id': 't-vanished', 'aborted': False, 'convId': 'c1',
                '_userId': 1, 'status': 'running'}
        with chat_task_fixture_guard:
            chat_task_registry['t-vanished'] = task
        try:
            with chat_task_fixture_guard:
                chat_task_registry.pop('t-vanished', None)  # the evaporation
            assert reg.plant_abort_tombstone('t-vanished',
                                             source='test', user_id=1) is True
            assert reg.make_task_abort_check(task)() is True
        finally:
            with chat_task_fixture_guard:
                chat_task_registry.pop('t-vanished', None)

    def test_conv_sweep_only_tombstones_registry_lost(self, monkeypatch,
                                                      clean_tombstones):
        fake = _FakeStorageClient(running_ids=['a', 'b'])
        _install_storage(monkeypatch, fake)
        monkeypatch.setattr(reg, '_write_abort_tombstone_row',
                            lambda tid, src, *, user_id: True)
        with chat_task_fixture_guard:
            chat_task_registry['a'] = {
                'id': 'a', '_userId': 1, 'status': 'running'}
        try:
            n = reg.plant_abort_tombstones_for_conv(
                'c1', source='test', user_id=1)
            assert n == 1
            assert reg.has_abort_tombstone('b')
            assert not reg.has_abort_tombstone('a')
        finally:
            with chat_task_fixture_guard:
                chat_task_registry.pop('a', None)


# ── Route and stream wiring ─────────────────────────────────────────

@pytest.mark.unit
class TestEndpointWiring:

    def test_abort_by_id_plants_on_miss(self):
        src = open(cpa.__file__, encoding='utf-8').read()
        assert "source='api_chat_abort', user_id=owner_user_id" in src

    def test_abort_conv_sweeps_tombstones(self):
        src = open(cpa.__file__, encoding='utf-8').read()
        assert "source='api_chat_abort_conv', user_id=owner_user_id" in src

    def test_stream_wires_tombstone_abort_check(self):
        import lib.tasks_pkg.manager._stream as stream_mod
        src = open(stream_mod.__file__, encoding='utf-8').read()
        assert 'make_task_abort_check' in src
        assert 'abort_check=_abort_check' in src

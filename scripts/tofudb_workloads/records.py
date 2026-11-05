"""Record and header operation shapes, plus independent Turn append identities.

Entry: RecordWorkloads; dependencies are explicit inputs and execution ports.
"""
from __future__ import annotations

from dataclasses import dataclass

from scripts.tofudb_workloads.inputs import WorkloadInputs, WorkloadPorts


@dataclass(frozen=True)
class RecordWorkloads:
    inputs: WorkloadInputs
    ports: WorkloadPorts

    def record_put_setup(self, engine):
        for i in range(self.inputs.scale['seed_records']):
            engine.execute('record.put', {
                'namespace': self.inputs.namespace, 'key': self.inputs.record_key(i), 'value': self.ports.record_value(i),
            }, command=True)

    def record_put_op(self, i):
        return ('record.put', {
            'namespace': self.inputs.namespace, 'key': self.inputs.record_key(self.inputs.scale['seed_records'] + i),
            'value': self.ports.record_value(i),
        }, True)

    def record_get_op(self, i):
        return ('record.get', {
            'namespace': self.inputs.namespace, 'key': self.inputs.record_key(i % self.inputs.scale['seed_records']),
        }, False)

    def conv_create_op(self, i):
        return ('conversation.create', {
            'conv_id': self.inputs.conversation_id('cb', i), 'user_id': self.inputs.user,
            'title': f'Perf conversation {i}',
            'settings': {'projectPath': f'/perf/{i}'},
            'created_at': self.inputs.conversation_timestamp + i, 'updated_at': self.inputs.conversation_timestamp + i,
        }, True)

    def conv_seed_setup(self, prefix):
        def setup(engine):
            for i in range(self.inputs.scale['seed_convs']):
                engine.execute('conversation.create', {
                    'conv_id': self.inputs.conversation_id(prefix, i), 'user_id': self.inputs.user,
                    'title': f'Perf conversation {i}',
                    'settings': {'projectPath': f'/perf/{i}'},
                    'created_at': self.inputs.conversation_timestamp + i, 'updated_at': self.inputs.conversation_timestamp + i,
                }, command=True)
        return setup

    def conv_list_op(self, i):
        # Production sidebar payload: the catalog always passes
        # include_messages=False (lib/conversations/catalog.py). The engine
        # defaults an omitted flag to full transcript hydration, which is a
        # different (bulk) contract measured by transcript_bulk_hydration.
        return ('conversation.list', {
            'user_id': self.inputs.user, 'limit': 50, 'include_messages': False,
        }, False)

    def conv_get_op(self, i):
        # Production conversation open is a bounded message window
        # (repository.get(message_window=...)), never an unbounded history.
        return ('conversation.get', {
            'conv_id': self.inputs.conversation_id('op', i % self.inputs.scale['seed_convs']),
            'user_id': self.inputs.user, 'message_window': 50,
        }, False)

    def make_turn_append(self, tag: str):
        """One (setup, op) pair with identities private to this workload.

        Every workload that appends turns must own a distinct tag: workloads
        share one authority per engine, and reused conversation/command/turn
        identities collide as database_conflict on the second seeder.
        """
        conv = f'{self.inputs.namespace}_{tag}_turns'

        def setup(engine):
            engine.execute('conversation.create', {
                'conv_id': conv, 'user_id': self.inputs.user,
                'title': 'Turn append target',
                'settings': {},
                'created_at': self.inputs.conversation_timestamp, 'updated_at': self.inputs.conversation_timestamp,
            }, command=True)

        def op(i):
            at = self.inputs.search_timestamp + 60_000 + i
            return ('turn.append_settled', {
                'conversation_id': conv, 'user_id': self.inputs.user,
                'actor': 'human', 'status': 'completed',
                'projection': {
                    'content': (
                        f'agent round {i} '
                        + 'lorem ipsum dolor sit amet ' * 8
                    ),
                },
                'lane_id': 'main', 'command_id': f'{conv}_{i}_command',
                'turn_id': f'{conv}_turn_{i:06d}', 'created_at': at, 'now': at,
                'conversation_defaults': {
                    'allowCreate': False, 'title': 'Turn append target',
                    'createdAt': self.inputs.conversation_timestamp, 'settings': {},
                },
            }, True)

        return setup, op

    def mixed_setup(self, engine):
        self.record_put_setup(engine)
        self.conv_seed_setup('mx')(engine)

    def mixed_op(self, i):
        slot = i % 10
        if slot < 8:
            return self.record_get_op(i)
        if slot == 8:
            return ('record.put', {
                'namespace': self.inputs.namespace, 'key': f'mix/{i:06d}',
                'value': self.ports.record_value(i),
            }, True)
        return self.conv_list_op(i)

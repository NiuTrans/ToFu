"""History and large-payload corpora; preserve recency and hydration distinctions.

Entry: HistoryWorkloads; dependencies are explicit inputs and execution ports.
"""
from __future__ import annotations

from dataclasses import dataclass

from scripts.tofudb_workloads.inputs import WorkloadInputs, WorkloadPorts


@dataclass(frozen=True)
class HistoryWorkloads:
    inputs: WorkloadInputs
    ports: WorkloadPorts

    def large_turn_setup(self, engine):
        engine.execute('conversation.create', {
            'conv_id': f'{self.inputs.namespace}_large_turns', 'user_id': self.inputs.user,
            'title': 'Large turn target',
            'settings': {},
            'created_at': self.inputs.conversation_timestamp, 'updated_at': self.inputs.conversation_timestamp,
        }, command=True)
        for i in range(-self.inputs.scale.get('large_turn_history', 0), 0):
            op, payload, command = self.large_turn_op(i)
            engine.execute(op, payload, command)

    def large_turn_op(self, i):
        """A turn carrying a tool-result-class payload (256 KiB of text).

        Agent harness reality: tool results dominate turn bytes. Large
        payloads exercise carried-block spill, WAL record size, and the
        pack/segment pipeline that small turns never touch.
        """
        at = self.inputs.search_timestamp + 7_200_000 + i
        return ('turn.append_settled', {
            'conversation_id': f'{self.inputs.namespace}_large_turns', 'user_id': self.inputs.user,
            'actor': 'human', 'status': 'completed',
            'projection': {
                'content': self.ports.large_turn_content(i, self.inputs.scale.get('large_turn_bytes', 262_144),
                    self.inputs.scale.get('large_turn_pattern', 'repetitive')),
            },
            'lane_id': 'main', 'command_id': f'{self.inputs.namespace}_large_turn_{i}_command',
            'turn_id': f'{self.inputs.namespace}_large_turn_{i:06d}', 'created_at': at,
            'now': at,
            'conversation_defaults': {
                'allowCreate': False, 'title': 'Large turn target',
                'createdAt': self.inputs.conversation_timestamp, 'settings': {},
            },
        }, True)

    def history_open_setup(self, engine):
        """One conversation carrying a realistic accumulated history.

        The charter's header-only ``conversation_open`` corpus cannot see
        history-scaled read paths; this phase's corpus can. The op reads
        the production open path (windowed main-lane snapshot), so any
        O(history) work behind an O(window) contract shows up linearly.
        """
        engine.execute('conversation.create', {
            'conv_id': f'{self.inputs.namespace}_history_open', 'user_id': self.inputs.user,
            'title': 'Long history target', 'settings': {},
            'created_at': self.inputs.conversation_timestamp, 'updated_at': self.inputs.conversation_timestamp,
        }, command=True)
        for i in range(self.inputs.scale['history_turns']):
            at = self.inputs.search_timestamp + 3_600_000 + i
            engine.execute('turn.append_settled', {
                'conversation_id': f'{self.inputs.namespace}_history_open', 'user_id': self.inputs.user,
                'actor': 'human', 'status': 'completed',
                'projection': {
                    'content': f'history round {i} ' + 'lorem ipsum ' * 8,
                },
                'lane_id': 'main', 'command_id': f'{self.inputs.namespace}_hist_{i}_command',
                'turn_id': f'{self.inputs.namespace}_hist_turn_{i:06d}', 'created_at': at,
                'now': at,
                'conversation_defaults': {
                    'allowCreate': False, 'title': 'Long history target',
                    'createdAt': self.inputs.conversation_timestamp, 'settings': {},
                },
            }, command=True)

    def history_open_op(self, i):
        return ('turn.sync.snapshot', {
            'conversation_id': f'{self.inputs.namespace}_history_open', 'user_id': self.inputs.user,
            'turn_limit': 20,
        }, False)

    def sidebar_history_setup(self, engine):
        """Sidebar conversations that carry real turn history.

        ``sidebar_list_page`` seeds headers only; production sidebars are
        dominated by conversations with accumulated turns, and any
        per-conversation history work inside ``conversation.list`` scales
        with the summed history instead of the page size.
        """
        for c in range(self.inputs.scale['sidebar_hist_convs']):
            cid = f'{self.inputs.namespace}_sbh_{c:05d}'
            engine.execute('conversation.create', {
                'conv_id': cid, 'user_id': self.inputs.user,
                'title': f'History sidebar {c}', 'settings': {},
                'created_at': self.inputs.conversation_timestamp + c, 'updated_at': self.inputs.conversation_timestamp + c,
            }, command=True)
            for t in range(self.inputs.scale['sidebar_hist_turns']):
                # _TS_BASE-based timestamps keep these conversations ahead of
                # the header-only seed corpora (conv_ts + i) in recency
                # order; otherwise a full-suite run pages the header-only
                # conversations and the phase silently re-measures
                # sidebar_list_page instead of history-bearing entries.
                at = self.inputs.conversation_timestamp + 7_900_000 + c * 10_000 + t
                engine.execute('turn.append_settled', {
                    'conversation_id': cid, 'user_id': self.inputs.user,
                    'actor': 'human', 'status': 'completed',
                    'projection': {'content': f'sidebar {c} turn {t}'},
                    'lane_id': 'main',
                    'command_id': f'{self.inputs.namespace}_sbh_{c}_{t}_command',
                    'turn_id': f'{self.inputs.namespace}_sbh_{c}_turn_{t:06d}',
                    'created_at': at, 'now': at,
                    'conversation_defaults': {
                        'allowCreate': False, 'title': f'History sidebar {c}',
                        'createdAt': self.inputs.conversation_timestamp + c, 'settings': {},
                    },
                }, command=True)

    def sidebar_history_op(self, i):
        return ('conversation.list', {
            'user_id': self.inputs.user, 'limit': 50, 'include_messages': False,
        }, False)

    def hydration_setup(self, engine):
        for c in range(self.inputs.scale['sidebar_hist_convs']):
            cid = f'{self.inputs.namespace}_hyd_{c:05d}'
            engine.execute('conversation.create', {
                'conv_id': cid, 'user_id': self.inputs.user,
                'title': f'Hydration sidebar {c}', 'settings': {},
                'created_at': self.inputs.conversation_timestamp + c, 'updated_at': self.inputs.conversation_timestamp + c,
            }, command=True)
            for t in range(self.inputs.scale['sidebar_hist_turns']):
                at = self.inputs.conversation_timestamp + 8_900_000 + c * 10_000 + t
                engine.execute('turn.append_settled', {
                    'conversation_id': cid, 'user_id': self.inputs.user,
                    'actor': 'human', 'status': 'completed',
                    'projection': {'content': f'hydration {c} turn {t}'},
                    'lane_id': 'main',
                    'command_id': f'{self.inputs.namespace}_hyd_{c}_{t}_command',
                    'turn_id': f'{self.inputs.namespace}_hyd_{c}_turn_{t:06d}',
                    'created_at': at, 'now': at,
                    'conversation_defaults': {
                        'allowCreate': False, 'title': f'Hydration sidebar {c}',
                        'createdAt': self.inputs.conversation_timestamp + c, 'settings': {},
                    },
                }, command=True)

    def hydration_op(self, i):
        # The bulk transcript path (sync/export): conversation.list's engine
        # default hydrates every entry's full main lane. Kept as its own
        # workload so the O(history-sum) contract stays visible and is never
        # conflated with the production sidebar path above.
        return ('conversation.list', {
            'user_id': self.inputs.user, 'limit': 50, 'include_messages': True,
        }, False)

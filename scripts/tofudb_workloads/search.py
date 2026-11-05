"""Search corpus preparation and complete asynchronous projection readiness.

Entry: SearchWorkload; dependencies are explicit inputs and execution ports.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

from scripts.tofudb_workloads.inputs import WorkloadInputs, WorkloadPorts


@dataclass(frozen=True)
class SearchWorkload:
    inputs: WorkloadInputs
    ports: WorkloadPorts
    search_word: str

    def search_setup(self, engine):
        for i in range(self.inputs.scale['search_docs']):
            at = self.inputs.search_timestamp + i * 1_000
            engine.execute('turn.append_settled', {
                'conversation_id': f'{self.inputs.namespace}_search_{i:04d}', 'user_id': self.inputs.user,
                'actor': 'human', 'status': 'completed',
                'projection': {
                    'content': (
                        f'document {i} padding text {self.search_word} '
                        'probe body with more words'
                    ),
                },
                'lane_id': 'main', 'command_id': f'{self.inputs.namespace}_search_{i}_command',
                'turn_id': f'{self.inputs.namespace}_search_turn_{i:04d}',
                'created_at': at, 'now': at,
                'conversation_defaults': {
                    'allowCreate': True, 'title': f'Search doc {i}',
                    'createdAt': self.inputs.search_timestamp, 'settings': {},
                },
            }, command=True)
        # Both authorities index through an asynchronous projection worker.
        # The legacy engines only flip their search coverage to 'ready' after
        # an explicit global backfill sweep; without it they report 'indexing'
        # forever even once every seeded document is searchable. Schedule the
        # sweep now, then wait until the seeded documents are all visible and
        # coverage is ready. (tofu-db reports 'ready' as soon as its owner is
        # not actively indexing, so this command is a harmless no-op for it.)
        try:
            engine.execute('turn.search.backfill', {}, command=True)
        except self.ports.error:
            pass
        # Backfill schedules a destructive rebuild, not a resumable nudge.
        # Issue it once: repeating it can reset the cursor before an accumulated
        # corpus finishes. Coverage changes alone are not a progress witness;
        # use the existing bounded total preparation deadline for every engine.
        started = time.monotonic()
        deadline = started + 600.0 + 2.0 * self.inputs.scale['search_docs']
        last_state = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise self.ports.error(
                    f'search projection did not stabilize for {engine.name} '
                    f'within {now - started:.0f}s (last state: {last_state})')
            hits = self.ports.execute(engine, 'conversation.search', {
                'user_id': self.inputs.user, 'query': self.search_word,
                'limit': max(50, self.inputs.scale['search_docs']), 'snippet_radius': 10,
            }, command=False)
            # Observe coverage even before all seed hits become visible.
            coverage = engine.execute('conversation.search', {
                'user_id': self.inputs.user, 'query': '', 'page': True, 'limit': 1,
            }, command=False)
            if isinstance(coverage, dict):
                last_state = (len(hits), coverage.get('coverage'),
                              tuple(coverage.get('coverage_reasons') or ()))
                if len(hits) >= self.inputs.scale['search_docs'] \
                        and coverage.get('coverage') == 'ready' \
                        and coverage.get('coverage_reasons') == []:
                    engine._search_readiness_observation = {
                        'available': True, 'source': 'conversation.search',
                        'phase': 'conversation_search_setup',
                        'coverage': 'ready', 'coverage_reasons': [],
                        'seed_documents_visible': len(hits),
                        'seed_documents_expected': self.inputs.scale['search_docs'],
                        'observed_at_unix_ms': int(time.time() * 1000),
                    }
                    engine._search_stabilization_s = round(
                        time.monotonic() - started, 1)
                    return
            time.sleep(0.1)

    def search_op(self, i):
        return ('conversation.search', {
            'user_id': self.inputs.user, 'query': self.search_word,
            'limit': 20, 'snippet_radius': 10,
        }, False)

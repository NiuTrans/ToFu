"""My Day goal service: the owner's goal views and commands for the HTTP adapter.

Responsibility: compose GoalStore, linked-request evidence and decomposition
into goal views, and bound how much work concurrent requests can start
(DB load measurement 2026-11-08: 249 concurrent My Day calendar scans stalled
the API host):

- One in-flight view load per owner and write generation. Concurrent page
  opens share it; a write bumps the generation, so a load that starts after a
  write never returns data from before it.
- One in-flight decomposition per goal. A reload joins it and a client
  disconnect never cancels it; the view reports ``decomposing`` meanwhile.

Nothing is cached after a flight settles and there is no window, timeout or
capacity to tune. Storage and model calls run in worker threads.
Entry points: MyDayGoals, Flights, FLIGHTS.
Dependencies: lib.myday.{goals,evidence,decompose}.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Hashable
import threading
import weakref

from .goals import GoalStore, goal_view


class Flights:
    """One running task per key on each event loop; nothing is kept once it settles."""

    def __init__(self):
        self._loops: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _table(self) -> dict:
        loop = asyncio.get_running_loop()
        table = self._loops.get(loop)
        if table is None:
            table = self._loops[loop] = {}
        return table

    def keys(self) -> frozenset:
        """Keys running on the current event loop."""
        return frozenset(self._table())

    async def run(self, key: Hashable, load: Callable[[], Coroutine]):
        table = self._table()
        flight = table.get(key)
        if flight is None:
            flight = asyncio.get_running_loop().create_task(load())
            table[key] = flight

            def settle(done, key=key):
                if table.get(key) is done:
                    del table[key]
                if not done.cancelled():
                    done.exception()  # retrieved even when every awaiter went away
            flight.add_done_callback(settle)
        return await asyncio.shield(flight)


FLIGHTS = Flights()
_generations: dict[int, int] = {}
_generations_lock = threading.Lock()


def _generation(owner: int) -> int:
    with _generations_lock:
        return _generations.get(owner, 0)


def _bump(owner: int) -> None:
    with _generations_lock:
        _generations[owner] = _generations.get(owner, 0) + 1


class MyDayGoals:
    """Async goal views and owner commands for one authenticated owner."""

    def __init__(self, owner: int, *, store: GoalStore | None = None, evidence=None, propose=None,
                 requests=None, flights: Flights = FLIGHTS):
        self.store = store or GoalStore(owner)
        self.owner = self.store.owner
        if evidence is None:
            from .evidence import request_evidence as evidence
        self.evidence = evidence
        self.propose = propose
        self.requests = requests
        self.flights = flights

    def _decomposing(self) -> frozenset:
        """IDs of this owner's goals with a running decomposition; read on the event loop."""
        return frozenset(key[2] for key in self.flights.keys() if key[:2] == ('decompose', self.owner))

    def _view(self, goal: dict, decomposing: frozenset) -> dict:
        request = self.evidence(self.owner, goal['link']) if goal['link'] else None
        return goal_view(goal, request=request, decomposing=goal['id'] in decomposing)

    async def view(self) -> dict:
        async def load():
            goals = await asyncio.to_thread(self.store.list)
            decomposing = self._decomposing()
            # Evidence reads run one goal at a time: one cohort read per linked goal.
            views = await asyncio.to_thread(lambda: [self._view(goal, decomposing) for goal in goals])
            return {'goals': views}
        return await self.flights.run(('view', self.owner, _generation(self.owner)), load)

    async def goal(self, goal_id: str) -> dict:
        decomposing = self._decomposing()
        return await asyncio.to_thread(lambda: self._view(self.store.read(goal_id), decomposing))

    async def write(self, command: Callable[[GoalStore], dict]) -> dict:
        """Run one owner command and return the changed goal's fresh view."""
        try:
            goal = await asyncio.to_thread(command, self.store)
        finally:
            _bump(self.owner)
        return await asyncio.to_thread(self._view, goal, self._decomposing())

    async def decompose(self, goal_id: str) -> dict:
        """Run, or join, the goal's one decomposition and return its fresh view."""
        from .decompose import propose_items

        async def run():
            try:
                goal = await asyncio.to_thread(self.store.read, goal_id)
                workers = []
                if goal['link']:
                    request = await asyncio.to_thread(self.evidence, self.owner, goal['link'])
                    workers = request.get('workers', []) if request.get('available') else []
                texts = await asyncio.to_thread(self.propose or propose_items, self.owner, goal,
                                                workers=workers)
                return await asyncio.to_thread(self.store.record_decomposition, goal_id, texts)
            finally:
                _bump(self.owner)
        goal = await self.flights.run(('decompose', self.owner, goal_id), run)
        return await asyncio.to_thread(self._view, goal, self._decomposing())

    async def linkable_requests(self) -> dict:
        from .evidence import linkable_requests
        return {'requests': await asyncio.to_thread(self.requests or linkable_requests, self.owner)}

    async def link(self, goal_id: str, project_path: str, source_attempt_id: str) -> dict:
        """Map the goal to the owner's request whose Workers share ``source_attempt_id``."""
        from .evidence import resolve_request
        return await self.write(lambda store: store.link(
            goal_id, resolve_request(self.owner, project_path, source_attempt_id)))

    async def update_item(self, goal_id: str, item_id: str, body: dict) -> dict:
        """An item command; a first status on a Worker item creates the owner's override."""
        def command(store):
            evidence = None
            if item_id.startswith('w-') and 'status' in body:
                goal = store.read(goal_id)
                if goal['link'] and not any(item['id'] == item_id for item in goal['items']):
                    request = self.evidence(self.owner, goal['link'])
                    from .goals import evidence_item_id
                    worker = next((worker for worker in request.get('workers', [])
                                   if evidence_item_id(worker['assignmentId']) == item_id), None)
                    if worker is None:
                        raise ValueError('That Worker is not part of the linked request')
                    evidence = {'assignmentId': worker['assignmentId'], 'title': worker['title']}
            return store.update_item(goal_id, item_id, status=body.get('status'), text=body.get('text'),
                                     proposal=body.get('proposal'),
                                     follow_evidence=bool(body.get('followEvidence')), evidence=evidence)
        return await self.write(command)


__all__ = ['FLIGHTS', 'Flights', 'MyDayGoals']

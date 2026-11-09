"""My Day goals: owner goals, their items and the progress estimate.

Responsibility: the owner-scoped goal records (one portable record CAS per
goal, ``contracts/myday_goals_v1.schema.json``), the owner commands that change
them, the regeneration merge for agent suggestions, and the goal view with its
progress estimate. Goals are not dated: an unfinished item stays open across
days, so carry-over needs no copy and nothing is reset.

Rules (docs/modules/scheduling_ops.md, "My Day goals"):
- Items keep stable IDs. The owner's status is manual and authoritative;
  regeneration and evidence never change it.
- Agent suggestions are proposals. A pending or dismissed proposal never
  counts; any owner action on it (accept, set status, edit) adopts it.
- Regeneration matches by normalized text and derived ID, touches no adopted,
  dismissed, owner or evidence item, and replaces only untouched proposals.
- progress.percent = floor(100 * completed / counted); null when nothing counts
  or a linked request's Workers cannot be read.

Entry points: GoalStore, merge_suggestions, goal_view, progress, normalized_text.
Dependencies: lib.storage (record.get/put/list), lib.myday.contract. Linked
request evidence arrives as an argument (lib.myday.evidence).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
import hashlib
import re
import time
import uuid

from lib.identity import require_user_id
from lib.storage.errors import StorageError

from .contract import CAS_ATTEMPTS, MAX_ITEMS, NAMESPACE, validate

_GOAL_ID = re.compile(r'^g-[0-9a-f]{16}$')
_ITEM_ID = re.compile(r'^[iaw]-[0-9a-f]{16}$')
# The storage protocol's largest record.list page (lib/storage_authority_sql _record_list).
_LIST_PAGE = 1000


class GoalNotFound(LookupError):
    """No goal with this ID belongs to the owner."""


def normalized_text(text: str) -> str:
    """Identity of an item text: lowercase without whitespace or punctuation."""
    return re.sub(r'[\s\W]+', '', text.strip().lower())


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]


def agent_item_id(goal_id: str, text: str) -> str:
    return 'a-' + _digest(goal_id + '\0' + normalized_text(text))


def evidence_item_id(assignment_id: str) -> str:
    return 'w-' + _digest(assignment_id)


def _goal_text(text) -> str:
    return text.strip() if isinstance(text, str) else ''


def _item_text(text) -> str:
    return ' '.join(text.split()) if isinstance(text, str) else ''


def _new_item(item_id: str, text: str, origin: str, now_ms: int, **fields) -> dict:
    return {'id': item_id, 'text': text, 'origin': origin, **fields, 'status': 'pending',
            'manual': False, 'createdAt': now_ms, 'statusChangedAt': now_ms,
            'startedAt': None, 'completedAt': None}


def _set_status(item: dict, status: str, now_ms: int) -> bool:
    """Apply an owner status; returns whether anything changed."""
    if item['status'] == status and item['manual']:
        return False
    if item['status'] != status:
        item['statusChangedAt'] = now_ms
        if status == 'in_progress' and item['startedAt'] is None:
            item['startedAt'] = now_ms
        item['completedAt'] = now_ms if status == 'completed' else None
        item['status'] = status
    item['manual'] = True
    return True


def _adopt(item: dict) -> None:
    if item['origin'] == 'agent' and item['proposal'] == 'pending':
        item['proposal'] = 'accepted'


def _find(goal: dict, item_id: str) -> dict | None:
    return next((item for item in goal['items'] if item['id'] == item_id), None)


def _room(goal: dict, count: int = 1) -> None:
    if len(goal['items']) + count > MAX_ITEMS:
        raise ValueError(f'A goal holds at most {MAX_ITEMS} items; archive it or remove items first')


def merge_suggestions(goal: dict, texts, *, now_ms: int) -> dict | None:
    """The goal after one regeneration proposed ``texts``; None when nothing changes.

    A proposal matching any kept item (normalized text or derived ID) is not
    added again. Untouched pending proposals the regeneration no longer
    proposes are replaced; every other item and its order are kept as stored.
    Proposals beyond the contract's item bound are not added.
    """
    goal = deepcopy(goal)
    proposed = []
    for text in texts:
        text = _item_text(text)[:200].strip()
        if text and normalized_text(text) and normalized_text(text) not in {
                normalized_text(seen) for seen in proposed}:
            proposed.append(text)
    proposed_keys = {normalized_text(text) for text in proposed}

    def untouched(item):
        return (item['origin'] == 'agent' and item['proposal'] == 'pending' and not item['manual']
                and item['status'] == 'pending')

    kept = [item for item in goal['items']
            if not untouched(item) or normalized_text(item['text']) in proposed_keys]
    known = {normalized_text(item['text']) for item in kept} | {item['id'] for item in kept}
    added = []
    for text in proposed:
        item_id = agent_item_id(goal['id'], text)
        if normalized_text(text) in known or item_id in known:
            continue
        if len(kept) + len(added) >= MAX_ITEMS:
            break
        added.append(_new_item(item_id, text, 'agent', now_ms, proposal='pending'))
        known |= {normalized_text(text), item_id}
    items = kept + added
    if items == goal['items']:
        return None
    goal['items'] = items
    return goal


class GoalStore:
    """Owner-scoped goal records over the portable record CAS."""

    def __init__(self, owner: int, *, client=None, clock: Callable[[], float] = time.time):
        self.owner = require_user_id(owner, context='My Day goals')
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.clock = clock
        self.prefix = f'{self.owner}:'

    def _now(self) -> int:
        return int(self.clock() * 1000)

    def _key(self, goal_id: str) -> str:
        if not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id):
            raise GoalNotFound('Unknown goal')
        return self.prefix + goal_id

    def _checked(self, value) -> dict:
        try:
            validate(value, 'goal')
            if value['owner'] != self.owner:
                raise ValueError('Goal record escaped its owner')
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError('database_integrity', 'Invalid My Day goal record') from error
        return value

    def _row(self, goal_id: str) -> dict:
        row = self.client().query('record.get', {'namespace': NAMESPACE, 'key': self._key(goal_id)})
        if row is None:
            raise GoalNotFound('Unknown goal')
        if type(row.get('version')) is not int or row['version'] < 1:
            raise StorageError('database_integrity', 'Invalid My Day goal record version')
        self._checked(row.get('value'))
        return row

    def _put(self, goal_id: str, value: dict, version: int) -> None:
        self.client(write=True).command('record.put', {
            'namespace': NAMESPACE, 'key': self._key(goal_id), 'value': value,
            'expected_version': version}, 'myday-goal:' + uuid.uuid4().hex)

    def read(self, goal_id: str) -> dict:
        return deepcopy(self._row(goal_id)['value'])

    def list(self) -> list[dict]:
        """Every goal of the owner (active and archived), oldest first."""
        goals, after = [], ''
        while True:
            rows = self.client().query('record.list', {
                'namespace': NAMESPACE, 'prefix': self.prefix, 'after_key': after, 'limit': _LIST_PAGE})
            if not isinstance(rows, list) or len(rows) > _LIST_PAGE:
                raise StorageError('database_integrity', 'Invalid My Day goal page')
            for row in rows:
                key = row.get('key') if isinstance(row, Mapping) else None
                if not isinstance(key, str) or not key.startswith(self.prefix) or key <= after:
                    raise StorageError('database_integrity', 'My Day goal page escaped its owner')
                goals.append(self._checked(deepcopy(row.get('value'))))
                after = key
            if len(rows) < _LIST_PAGE:
                return sorted(goals, key=lambda goal: (goal['createdAt'], goal['id']))

    def create(self, text: str) -> dict:
        now = self._now()
        goal = validate({'version': 1, 'owner': self.owner, 'id': 'g-' + uuid.uuid4().hex[:16],
                         'text': _goal_text(text), 'origin': 'owner', 'state': 'active',
                         'createdAt': now, 'updatedAt': now, 'archivedAt': None,
                         'decomposedAt': None, 'items': [], 'link': None}, 'goal')
        self._put(goal['id'], goal, 0)
        return deepcopy(goal)

    def update(self, goal_id: str, mutate: Callable[[dict], dict | None]) -> dict:
        """CAS one change: ``mutate`` gets a copy of the latest goal and returns the next one.

        Returning None writes nothing and returns the stored goal. ``mutate``
        may run again after a concurrent write, always on the newest record.
        """
        for _ in range(CAS_ATTEMPTS):
            row = self._row(goal_id)
            changed = mutate(deepcopy(row['value']))
            if changed is None:
                return deepcopy(row['value'])
            changed = deepcopy(changed)
            changed['updatedAt'] = max(self._now(), row['value']['updatedAt'])
            validate(changed, 'goal')
            if changed['id'] != goal_id or changed['owner'] != self.owner:
                raise ValueError('A goal command cannot change the goal identity')
            try:
                self._put(goal_id, changed, row['version'])
            except StorageError as error:
                if error.code != 'database_conflict' and not error.retryable:
                    raise
                continue
            return changed
        raise StorageError('database_conflict', 'The goal changed concurrently; send the command again')

    # ── Owner commands ────────────────────────────────────────────

    def edit_goal(self, goal_id: str, *, text: str | None = None, state: str | None = None) -> dict:
        if text is None and state is None:
            raise ValueError('Nothing to change')

        def change(goal):
            before = deepcopy(goal)
            if text is not None:
                goal['text'] = _goal_text(text)
            if state is not None and state != goal['state']:
                goal['state'] = state
                goal['archivedAt'] = self._now() if state == 'archived' else None
            return None if goal == before else goal
        return self.update(goal_id, change)

    def add_item(self, goal_id: str, text: str) -> dict:
        def change(goal):
            _room(goal)
            goal['items'].append(_new_item('i-' + uuid.uuid4().hex[:16], _item_text(text), 'owner',
                                           self._now()))
            return goal
        return self.update(goal_id, change)

    def update_item(self, goal_id: str, item_id: str, *, status: str | None = None,
                    text: str | None = None, proposal: str | None = None,
                    follow_evidence: bool = False, evidence: Mapping | None = None) -> dict:
        """One owner command on one item.

        ``evidence`` ({assignmentId, title}) names the linked Worker behind a
        ``w-`` item that has no stored override yet; the first status creates
        that override.
        """
        if status is not None and status not in ('pending', 'in_progress', 'completed'):
            raise ValueError('status must be pending, in_progress or completed')
        if proposal not in (None, 'accepted'):
            raise ValueError('proposal can only be accepted; remove the item to dismiss it')
        if status is None and text is None and proposal is None and not follow_evidence:
            raise ValueError('Nothing to change')

        def change(goal):
            item = _find(goal, item_id)
            if item is None and evidence is not None and status is not None and goal['link']:
                if item_id != evidence_item_id(evidence['assignmentId']):
                    raise ValueError('The Worker evidence does not match this item')
                _room(goal)
                item = _new_item(item_id, _item_text(evidence['title'])[:200], 'evidence', self._now(),
                                 assignmentId=evidence['assignmentId'])
                goal['items'].append(item)
            if item is None:
                if follow_evidence and item_id.startswith('w-'):
                    return None  # no override exists: the item already follows its evidence
                raise ValueError('Unknown item')
            before = deepcopy(item)
            if follow_evidence:
                if item['origin'] != 'evidence':
                    raise ValueError('Only a linked Worker item follows evidence')
                goal['items'] = [row for row in goal['items'] if row['id'] != item_id]
                return goal
            if item['origin'] == 'agent' and item['proposal'] == 'dismissed':
                raise ValueError('This suggestion was dismissed')
            if text is not None:
                if item['origin'] == 'evidence':
                    raise ValueError('A linked Worker item keeps its Worker title')
                item['text'] = _item_text(text)
                _adopt(item)
            if proposal == 'accepted':
                if item['origin'] != 'agent':
                    raise ValueError('Only a suggestion can be accepted')
                _adopt(item)
            if status is not None:
                _set_status(item, status, self._now())
                _adopt(item)
            return None if item == before else goal
        return self.update(goal_id, change)

    def remove_item(self, goal_id: str, item_id: str) -> dict:
        """Remove an owner item; dismiss a suggestion so it is not proposed again."""
        def change(goal):
            item = _find(goal, item_id)
            if item is None:
                raise ValueError('Unknown item')
            if item['origin'] == 'evidence':
                raise ValueError('A linked Worker cannot be removed; unlink the request instead')
            if item['origin'] == 'agent':
                if item['proposal'] == 'dismissed':
                    return None
                item['proposal'] = 'dismissed'
                return goal
            goal['items'] = [row for row in goal['items'] if row['id'] != item_id]
            return goal
        return self.update(goal_id, change)

    def decide_suggestions(self, goal_id: str, decision: str) -> dict:
        """Accept or dismiss every pending suggestion of the goal."""
        if decision not in ('accept', 'dismiss'):
            raise ValueError('decision must be accept or dismiss')

        def change(goal):
            pending = [item for item in goal['items']
                       if item['origin'] == 'agent' and item['proposal'] == 'pending']
            for item in pending:
                item['proposal'] = 'accepted' if decision == 'accept' else 'dismissed'
            return goal if pending else None
        return self.update(goal_id, change)

    def link(self, goal_id: str, link: Mapping) -> dict:
        """Map the goal to one Coordinator request; a new request drops old Worker overrides."""
        value = validate({**{key: link.get(key) for key in (
            'projectPath', 'coordinatorId', 'sourceAttemptId', 'requestTurnId', 'label')},
            'linkedAt': self._now()}, 'link')

        def change(goal):
            current = goal['link']
            if current and current['sourceAttemptId'] == value['sourceAttemptId'] \
                    and current['projectPath'] == value['projectPath']:
                return None
            goal['link'] = value
            goal['items'] = [item for item in goal['items'] if item['origin'] != 'evidence']
            return goal
        return self.update(goal_id, change)

    def unlink(self, goal_id: str) -> dict:
        def change(goal):
            if goal['link'] is None:
                return None
            goal['link'] = None
            goal['items'] = [item for item in goal['items'] if item['origin'] != 'evidence']
            return goal
        return self.update(goal_id, change)

    def record_decomposition(self, goal_id: str, texts) -> dict:
        """Merge one regeneration's proposals and stamp when it ran."""
        def change(goal):
            merged = merge_suggestions(goal, texts, now_ms=self._now()) or goal
            merged['decomposedAt'] = self._now()
            return merged
        return self.update(goal_id, change)


# ── View ──────────────────────────────────────────────────────────

def _counted(item_view: dict) -> bool:
    if item_view['origin'] == 'agent' and item_view.get('proposal') != 'accepted':
        return False
    return item_view['status'] != 'cancelled'


def progress(items: list[dict], *, complete: bool = True) -> dict:
    """The estimate over item views: every counted item weighs the same.

    ``complete`` is False when a linked request's Workers could not be read: a
    percentage over the visible items alone would overstate progress, so it
    is unknown (None) while the counts still describe what is visible.
    """
    counted = [item for item in items if item['counted']]
    completed = sum(1 for item in counted if item['status'] == 'completed')
    return {'counted': len(counted), 'completed': completed,
            'inProgress': sum(1 for item in counted if item['status'] == 'in_progress'),
            'pending': sum(1 for item in counted if item['status'] == 'pending'),
            'suggested': sum(1 for item in items if item['origin'] == 'agent'
                             and item.get('proposal') == 'pending'),
            'percent': completed * 100 // len(counted) if counted and complete else None}


def _stored_view(item: dict) -> dict:
    view = {key: item[key] for key in ('id', 'text', 'origin', 'status', 'manual', 'createdAt',
                                       'statusChangedAt', 'startedAt', 'completedAt')}
    if item['origin'] == 'agent':
        view['proposal'] = item['proposal']
    return view


def goal_view(goal: dict, *, request: Mapping | None = None, decomposing: bool = False) -> dict:
    """The owner-facing goal: effective item states, evidence and the progress estimate.

    ``request`` is lib.myday.evidence.request_evidence for a linked goal. Each
    Worker becomes an evidence item; a stored owner override keeps its manual
    status beside the Worker evidence. Dismissed suggestions are hidden.
    """
    request = request or {}
    available = bool(request.get('available'))
    workers = list(request.get('workers') or ()) if available else []
    overrides = {item['id']: item for item in goal['items'] if item['origin'] == 'evidence'}
    items = []
    for item in goal['items']:
        if item['origin'] == 'evidence' or (item['origin'] == 'agent' and item['proposal'] == 'dismissed'):
            continue
        view = _stored_view(item)
        view['counted'] = _counted(view)
        items.append(view)
    seen = set()
    for worker in workers:
        item_id = evidence_item_id(worker['assignmentId'])
        seen.add(item_id)
        override = overrides.get(item_id)
        if override:
            view = _stored_view(override)
        else:
            view = {'id': item_id, 'text': worker['title'], 'origin': 'evidence',
                    'status': worker['state'], 'manual': False, 'createdAt': None,
                    'statusChangedAt': None, 'startedAt': None, 'completedAt': None}
        view['evidence'] = {key: worker[key] for key in
                            ('assignmentId', 'conversationId', 'review', 'state', 'reason')}
        view['counted'] = _counted(view)
        items.append(view)
    for item_id, override in overrides.items():
        if item_id not in seen:
            view = _stored_view(override)
            view['counted'] = _counted(view)
            items.append(view)
    request_view = None
    if goal['link'] is not None:
        request_view = {'available': available, 'settled': available and bool(request.get('settled')),
                        'workers': len(workers),
                        'reason': None if available else str(request.get('reason')
                                                             or 'Evidence was not read')[:300]}
    return {'id': goal['id'], 'text': goal['text'], 'origin': goal['origin'], 'state': goal['state'],
            'createdAt': goal['createdAt'], 'updatedAt': goal['updatedAt'], 'archivedAt': goal['archivedAt'],
            'decomposedAt': goal['decomposedAt'], 'decomposing': bool(decomposing), 'items': items,
            'progress': progress(items, complete=goal['link'] is None or available),
            'link': deepcopy(goal['link']), 'request': request_view}


__all__ = ['GoalNotFound', 'GoalStore', 'agent_item_id', 'evidence_item_id', 'goal_view',
           'merge_suggestions', 'normalized_text', 'progress']

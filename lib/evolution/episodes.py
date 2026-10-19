"""Episode lifecycle for the self-evolution module.

Responsibility: open a running episode against a confirmed plan and settle it
with measured actuals, a verdict against the goal's own criteria, and bounded
learning deltas. Entry points: ``open_episode`` / ``settle_episode`` /
``get_episode`` / ``list_episodes``. Dependencies: ``contracts``, ``goals``
(plan/goal lookup), ``records`` (owner-scoped persistence).

Invariants enforced here, not left to callers:

- An episode opens only against a **confirmed** plan whose goal the owner
  admitted; path selection is never autonomous compute authorization.
- Settlement is receipt-shaped: a non-abort terminal status requires a
  verdict whose outcome equals the status.
- Exploratory episodes (goals without machine-checkable criteria) may not
  settle with strategy templates, anti-patterns, or capability gaps — vague
  goals cannot mint reusable memory.
- A terminal episode never re-settles; write failures propagate typed, so a
  failed settle leaves the episode running rather than faking settlement.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import heapq
import time
import uuid

from lib.identity import require_user_id
from lib.storage.errors import StorageError

from .contracts import EvolutionStateError, validate_episode, validate_goal
from .goals import get_goal, get_plan
from .records import (
    MAX_LIST_LIMIT,
    NAMESPACE_EPISODES,
    NAMESPACE_GOALS,
    entity_key,
    get_record,
    get_versioned_record,
    list_records,
    list_record_page,
    is_record_page_resource_refusal,
    put_record,
)

_RUNNING = 'running'
_TERMINAL = ('success', 'partial', 'failure', 'aborted')


def _episode_key(episode_id: str, user_id) -> str:
    return entity_key(episode_id, user_id, kind='episode')


def open_episode(*, goal_id: str, plan_id: str, selected_path_id: str,
                 task_id: str, user_id, now: int | None = None) -> dict:
    goal = get_goal(goal_id, user_id=user_id)
    if goal is None:
        raise EvolutionStateError('episode references an unknown goal for this owner')
    plan = get_plan(plan_id, user_id=user_id)
    if plan is None or plan.get('goal_id') != goal_id:
        raise EvolutionStateError('episode plan does not belong to the goal')
    if plan.get('status') != 'confirmed':
        raise EvolutionStateError(
            'episode requires a confirmed plan; selection is not autonomous authorization')
    candidate_ids = {c.get('path_id') for c in plan.get('candidates', [])}
    if selected_path_id not in candidate_ids:
        raise EvolutionStateError('selected path is not a plan candidate')
    ts = int(now if now is not None else time.time())
    episode = validate_episode({
        'schema_version': 1,
        'episode_id': f'ep-{uuid.uuid4().hex[:16]}',
        'goal_id': goal_id,
        'plan_id': plan_id,
        'selected_path_id': selected_path_id,
        'exploratory': bool(goal.get('exploratory')),
        'task_id': task_id,
        'status': _RUNNING,
        'created_at': ts,
        'updated_at': ts,
        'settled_at': None,
        'actuals': {'tokens': 0, 'wall_clock_seconds': 0, 'tool_calls': 0},
        'verdict': None,
        'learning': {
            'resource_weight_deltas': [],
            'cost_actual_vs_estimate': None,
            'strategy_outcome': None,
            'capability_gap': None,
        },
    })
    put_record(NAMESPACE_EPISODES, _episode_key(episode['episode_id'], user_id),
               episode, kind='episode')
    return episode


def _check_exploratory_rights(episode: dict) -> None:
    if not episode.get('exploratory'):
        return
    learning = episode.get('learning') or {}
    strategy = learning.get('strategy_outcome') or {}
    if (strategy.get('distilled_template_id') is not None
            or strategy.get('anti_pattern') is not None
            or learning.get('capability_gap') is not None):
        raise EvolutionStateError(
            'exploratory episodes may only update resource weights and cost '
            'priors; strategy and capability learning require verifiable criteria')


def _check_verdict_consistency(episode: dict) -> None:
    status = episode['status']
    verdict = episode.get('verdict')
    if status == 'aborted':
        return
    if not isinstance(verdict, dict):
        raise EvolutionStateError(
            f'{status} settlement requires a verdict against the goal criteria')
    if verdict.get('outcome') != status:
        raise EvolutionStateError(
            f"verdict outcome {verdict.get('outcome')!r} contradicts status {status!r}")


def settle_episode(episode_id: str, *, user_id, status: str, actuals: dict,
                   stage_receipts: list | None = None,
                   verdict: dict | None = None,
                   learning: dict | None = None,
                   now: int | None = None, actuals_complete: bool = True,
                   reconcile: bool = False) -> dict:
    """Commit once; opted-in recovery confirms only the exact existing settlement."""
    if type(actuals_complete) is not bool:
        raise EvolutionStateError('actuals completeness must be an explicit boolean')
    if type(reconcile) is not bool:
        raise EvolutionStateError('settlement reconciliation must be an explicit boolean')
    episode, version = get_versioned_record(NAMESPACE_EPISODES, _episode_key(episode_id, user_id))
    if episode is None:
        raise EvolutionStateError('unknown episode for this owner')
    if episode.get('status') != _RUNNING and not reconcile:
        raise EvolutionStateError(
            f"episode is already terminal ({episode.get('status')}); settlement is final")
    if status not in _TERMINAL:
        raise EvolutionStateError(f'invalid terminal status {status!r}')
    ts = int(now if now is not None else time.time())
    settled = deepcopy(episode)
    settled.update({
        'status': status,
        'updated_at': ts,
        'settled_at': ts,
        'actuals': actuals,
        'stage_receipts': stage_receipts or [],
        'verdict': verdict,
        'learning': learning if learning is not None else episode['learning'],
    })
    if actuals_complete is False:
        settled['actuals_complete'] = False
        if (settled['learning'].get('cost_actual_vs_estimate') is not None
                or settled['learning'].get('resource_weight_deltas')):
            raise EvolutionStateError('incomplete actuals cannot authorize cost or efficiency learning')
    else:
        settled.pop('actuals_complete', None)
    validate_episode(settled)
    _check_verdict_consistency(settled)
    _check_exploratory_rights(settled)
    if episode.get('status') != _RUNNING:
        return _confirm_settlement(episode, settled)
    try:
        put_record(NAMESPACE_EPISODES, _episode_key(episode_id, user_id),
                   settled, kind='episode', expected_version=version)
    except StorageError as exc:
        if not reconcile or exc.code != 'database_conflict':
            raise
        current = get_episode(episode_id, user_id=user_id)
        if current is None or current.get('status') not in _TERMINAL:
            raise
        return _confirm_settlement(current, settled)
    return settled


def _confirm_settlement(current: dict, proposed: dict) -> dict:
    # Observation time is not settlement identity. Every other field, including
    # owner-bound episode/task/plan identity and usage completeness, must agree.
    def identity(value):
        result = {key: item for key, item in value.items()
                  if key not in ('updated_at', 'settled_at', 'actuals_complete')}
        result['actuals_complete'] = value.get('actuals_complete', True)
        return result

    if current.get('status') not in _TERMINAL or identity(current) != identity(proposed):
        raise EvolutionStateError('episode settlement conflicts with the committed result')
    return current


def get_episode(episode_id: str, *, user_id) -> dict | None:
    return get_record(NAMESPACE_EPISODES, _episode_key(episode_id, user_id))


def list_episodes(*, user_id, limit: int = 50) -> list[dict]:
    return list_records(NAMESPACE_EPISODES, user_id, kind='episode', limit=limit)


def find_running_episode_for_task(*, task_id: str, user_id) -> dict | None:
    """Find an exact legacy task binding through complete owner-scoped pages."""
    owner = require_user_id(user_id, context='evolution terminal episode owner')
    cursor = ''
    page_size = MAX_LIST_LIMIT
    while True:
        try:
            page = list_record_page(NAMESPACE_EPISODES, owner, kind='episode',
                                    limit=page_size, after_key=cursor)
        except StorageError as exc:
            if not is_record_page_resource_refusal(exc) or page_size == 1:
                raise
            page_size = max(1, page_size // 2)
            continue
        for row in page:
            episode = row['value']
            try:
                validate_episode(episode)
                if _episode_key(episode['episode_id'], owner) != row['key']:
                    raise ValueError('episode identity differs from its key')
            except ValueError as exc:
                raise StorageError('database_integrity', 'Invalid terminal episode binding') from exc
            if episode.get('task_id') == task_id and episode.get('status') == _RUNNING:
                return episode
        if not page:
            return None
        cursor = page[-1]['key']


def recent_settled_episodes(*, user_id, limit=MAX_LIST_LIMIT):
    """Page original records and retain recent settlements in bounded memory.

    The window ranks recorded settlement time and then episode ID, descending.
    Running records never displace settled evidence. No cache-completeness or
    timestamp watermark is assumed; storage failures leave the view unavailable.
    """
    owner = require_user_id(user_id, context='evolution recent episode owner')
    if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError('invalid recent episode window')
    cursor = ''
    page_size = MAX_LIST_LIMIT
    newest = []
    while True:
        try:
            page = list_record_page(NAMESPACE_EPISODES, owner, kind='episode',
                limit=page_size, after_key=cursor)
        except StorageError as exc:
            if not is_record_page_resource_refusal(exc) or page_size == 1:
                raise
            page_size = max(1, page_size // 2)
            continue
        for row in page:
            episode = row['value']
            try:
                validate_episode(episode)
                if _episode_key(episode['episode_id'], owner) != row['key']:
                    raise ValueError('episode identity differs from its key')
                if episode['status'] in _TERMINAL and type(episode.get('settled_at')) is not int:
                    raise ValueError('terminal episode settlement time is unavailable')
            except ValueError as exc:
                raise StorageError('database_integrity', 'Invalid original learning episode') from exc
            if episode['status'] not in _TERMINAL:
                continue
            candidate = (episode['settled_at'], episode['episode_id'], episode)
            if len(newest) < limit:
                heapq.heappush(newest, candidate)
            elif candidate[:2] > newest[0][:2]:
                heapq.heapreplace(newest, candidate)
        if len(page) < page_size:
            break
        cursor = page[-1]['key']
    return [deepcopy(item[2]) for item in sorted(newest, reverse=True)]


def episode_goals(selected, *, user_id, goal_reader=None):
    """Resolve exact parent identities; missing parents grant no learning facts."""
    owner = require_user_id(user_id, context='evolution learning goal owner')
    resolved = {}
    for goal_id in dict.fromkeys(episode['goal_id'] for episode in selected):
        goal = (goal_reader or get_goal)(goal_id, user_id=owner)
        if goal is not None:
            try:
                validate_goal(goal)
            except ValueError as exc:
                raise StorageError('database_integrity', 'Invalid original learning goal') from exc
            if goal.get('goal_id') != goal_id:
                raise StorageError('database_integrity', 'Learning goal identity differs from its reference')
            resolved[goal_id] = goal
    return resolved


class EpisodeReadWindow:
    """One lazy owner-bound maintenance view; no global cache or write authority.

    Consumers receive isolated copies. A failed load is not cached, so a later
    fail-soft maintenance step can retry through the normal repository boundary.
    The bounded window contains recent validated settlements; exact immutable
    parents are cached only for selected references. Ordinary lists keep their
    own limit semantics. Close releases originals even on cancellation.
    """
    def __init__(self, *, user_id):
        self.owner = require_user_id(user_id, context='evolution maintenance owner')
        self.rows = None
        self.closed = False
        self.goals = {}
        self.goal_page_loaded = False

    def __call__(self, *, user_id, limit):
        owner = require_user_id(user_id, context='evolution maintenance reader owner')
        if self.closed or owner != self.owner:
            raise EvolutionStateError('maintenance episode view is closed or belongs to another owner')
        if type(limit) is not int or limit != MAX_LIST_LIMIT:
            raise EvolutionStateError('maintenance episode reader requires its full bounded window')
        if self.rows is None:
            self.rows = recent_settled_episodes(user_id=owner)
        return deepcopy(self.rows)

    def read_goal(self, goal_id, *, user_id):
        owner = require_user_id(user_id, context='evolution maintenance goal owner')
        if self.closed or owner != self.owner or self.rows is None:
            raise EvolutionStateError('maintenance goal read requires its owned episode view')
        if goal_id not in {row['goal_id'] for row in self.rows}:
            raise EvolutionStateError('goal is outside the selected episode view')
        if not self.goal_page_loaded:
            selected_ids = {row['goal_id'] for row in self.rows}
            page_size = MAX_LIST_LIMIT
            while True:
                try:
                    page = list_record_page(NAMESPACE_GOALS, owner, kind='goal', limit=page_size)
                    break
                except StorageError as exc:
                    if not is_record_page_resource_refusal(exc) or page_size == 1:
                        raise
                    page_size = max(1, page_size // 2)
            captured = {}
            for row in page:
                value = row['value']
                try:
                    if entity_key(value.get('goal_id'), owner, kind='goal') != row['key']:
                        raise ValueError('goal identity differs from its key')
                except ValueError as exc:
                    raise StorageError('database_integrity', 'Invalid original learning goal') from exc
                if value['goal_id'] in selected_ids:
                    captured[value['goal_id']] = deepcopy(value)
            self.goals.update(captured)
            self.goal_page_loaded = True
        if goal_id not in self.goals:
            self.goals[goal_id] = get_goal(goal_id, user_id=owner)
        return deepcopy(self.goals[goal_id])

    def close(self):
        self.goals.clear()
        self.goal_page_loaded = False
        self.rows = None
        self.closed = True


@contextmanager
def episode_read_window(*, user_id):
    reader = EpisodeReadWindow(user_id=user_id)
    try:
        yield reader
    finally:
        reader.close()

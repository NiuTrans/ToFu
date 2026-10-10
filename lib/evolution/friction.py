"""Friction miner: deterministic detection of struggle without failure.

Responsibility: mine settled episodes' bound tasks for behavioral friction —
a truncated tool result followed by an identical retry, or an identical-call
loop — and record each signal as an observation, so the frontier can see pain
that never produced a failure verdict. Entry point: ``mine_friction``.
Dependencies: the episodes store, an injectable owner-scoped task loader, the
observation ledger.

Discipline, pinned by tests:

- Signals are deterministic and receipt-bound: same task, same signature,
  same observation — re-mining never duplicates.
- A friction record is an observation: zero learning rights, no verdict,
  strategy or mutation influence. It informs the frontier and the owner.
- One unloadable task never aborts the pass (same posture as crystallize).
"""
from __future__ import annotations

import json
import time

from lib.log import get_logger

from .bridge import default_task_loader
from .contracts import EvolutionContractError, EvolutionStateError
from .episodes import list_episodes
from .observations import observation_id, record_observation

logger = get_logger(__name__)

MAX_FRICTION_PER_TASK = 4
REPEAT_LOOP_THRESHOLD = 3
_TRUNCATION_NEEDLE = 'truncat'  # '… (output truncated', '"truncated": true'


def _iter_calls(task: dict):
    """Yield ``(name, normalized_args, result_text)`` from the message list,
    pairing assistant tool calls with their tool-result messages."""
    messages = (task or {}).get('messages') or []
    results = {}
    for msg in messages:
        if isinstance(msg, dict) and msg.get('role') == 'tool':
            results[str(msg.get('tool_call_id') or '')] = str(
                msg.get('content') or '')
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            continue
        for call in msg.get('tool_calls') or []:
            if not isinstance(call, dict):
                continue
            fn = call.get('function')
            if not isinstance(fn, dict):
                fn = call
            name = str(fn.get('name') or '')
            if not name:
                continue
            raw_args = fn.get('arguments')
            args_text = (raw_args if isinstance(raw_args, str)
                         else json.dumps(raw_args or {}, sort_keys=True,
                                         ensure_ascii=False))
            normalized = ' '.join(args_text.split())
            yield name, normalized, results.get(str(call.get('id') or ''), '')


def _signals(calls: list) -> list:
    """Deterministic struggle signals over one task's call sequence."""
    signals = []
    seen = set()
    by_identity: dict[tuple, list] = {}
    for position, (name, args, _result) in enumerate(calls):
        by_identity.setdefault((name, args), []).append(position)
    for (name, args), positions in by_identity.items():
        if len(positions) >= REPEAT_LOOP_THRESHOLD:
            key = ('repeat_loop', name)
            if key not in seen:
                seen.add(key)
                signals.append({
                    'signal': 'repeat_loop',
                    'tool': name,
                    'statement': (f"tool {name} was called "
                                  f"{len(positions)} times with identical "
                                  'arguments in one task'),
                })
    for position, (name, args, result) in enumerate(calls):
        if _TRUNCATION_NEEDLE not in result.lower():
            continue
        later = [p for p in by_identity.get((name, args), []) if p > position]
        if later:
            key = ('truncation_retry', name)
            if key not in seen:
                seen.add(key)
                signals.append({
                    'signal': 'truncation_retry',
                    'tool': name,
                    'statement': (f"tool {name} returned truncated output "
                                  'and a later call re-issued identical '
                                  'arguments; the retry cannot reach what '
                                  'was withheld'),
                })
    return signals


def mine_friction(*, user_id, loader=None, now: int | None = None, episode_reader=None) -> list:
    """Scan recent settled episodes and record friction observations.

    Returns the deterministic signal list (idempotent: a re-scan finds the
    same signals and the ledger keeps one record each).
    """
    load = loader or default_task_loader
    ts = int(now if now is not None else time.time())
    found = []
    for episode in (episode_reader or list_episodes)(user_id=user_id, limit=200):
        if episode.get('status') not in ('success', 'partial', 'failure'):
            continue
        task_id = str(episode.get('task_id') or '')
        if not task_id:
            continue
        try:
            task = load(task_id, user_id=user_id)
        except Exception as _e:
            logger.debug('mine friction entry failed: %s', _e)
            task = None
        signals = _signals(list(_iter_calls(task or {})))
        for signal in signals[:MAX_FRICTION_PER_TASK]:
            subject = {'type': 'tool', 'id': signal['tool']}
            obs_id = observation_id(
                'friction', subject, signal['statement'],
                salt=f"{signal['signal']}|{episode['episode_id']}")
            try:
                record_observation({
                    'schema_version': 1,
                    'observation_id': obs_id,
                    'kind': 'friction',
                    'statement': signal['statement'],
                    'subject': subject,
                    'lineage': [episode['episode_id'], task_id],
                    'source': {'actor': 'miner'},
                    'status': 'open',
                    'created_at': ts,
                    'updated_at': ts,
                }, user_id=user_id, now=ts)
            except (EvolutionContractError, EvolutionStateError):
                continue
            found.append({**signal, 'episode_id': episode['episode_id'],
                          'observation_id': obs_id})
    return found

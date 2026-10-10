"""Pure paired-evaluation arithmetic and candidate identity.

Entry points: compare_pairs and mutation_fingerprint. Callers own execution,
owner authorization, frozen inputs and checkers; this module owns neither a
model loop nor storage. A measured gain is scoped to these cases, not a claim
of general capability. Unknown costs remain null in the supplied receipts.
"""
from __future__ import annotations

import hashlib
import json
import math

from .contracts import EvolutionContractError, contract_validator


def mutation_fingerprint(mutation: dict) -> str:
    identity = {key: mutation.get(key) for key in
                ('mutation_id', 'kind', 'payload', 'version', 'evidence_episode_ids')}
    return hashlib.sha256(json.dumps(identity, sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()



def paired_context_fingerprint(mutation: dict) -> str:
    identity={key:mutation.get(key) for key in
        ('mutation_id','kind','payload','version','parent_version','evidence_episode_ids')}
    return hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def compare_pairs(pairs: list[dict]) -> dict:
    if not isinstance(pairs, list) or len(pairs) > 32:
        raise EvolutionContractError('paired comparison accepts at most 32 cases')
    validator = contract_validator('evolution_regression_v1.schema.json', 'pair')
    ids = set()
    for pair in pairs:
        if not validator.is_valid(pair):
            raise EvolutionContractError('invalid paired observation')
        if pair['item_id'] in ids:
            raise EvolutionContractError('duplicate paired case')
        ids.add(pair['item_id'])
        for arm in ('baseline', 'candidate'):
            for metric in ('tokens', 'wall_clock_seconds'):
                value = pair[arm][metric]
                if value is not None and not math.isfinite(value):
                    raise EvolutionContractError('non-finite paired cost')
    measured = [p for p in pairs if all(p[a]['outcome'] != 'skip'
                                      for a in ('baseline', 'candidate'))]
    wins = sum(p['baseline']['outcome'] == 'fail' and
               p['candidate']['outcome'] == 'pass' for p in measured)
    losses = sum(p['baseline']['outcome'] == 'pass' and
                 p['candidate']['outcome'] == 'fail' for p in measured)
    discordant = wins + losses
    # Exact one-sided paired sign test. Small samples cannot yield certainty.
    p_value = (sum(math.comb(discordant, k) for k in range(wins, discordant + 1))
               / (2 ** discordant)) if discordant else 1.0
    conclusion = ('regression' if losses else
                  'inconclusive' if len(measured) < 3 or len(measured) != len(pairs)
                  else 'observed_gain' if wins else 'no_gain')
    return {'pairs': pairs, 'measured': len(measured), 'wins': wins,
            'losses': losses, 'ties': len(measured) - wins - losses,
            'conclusion': conclusion, 'sign_test_p': p_value}

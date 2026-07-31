"""tests/_migrate_hybrid_cost_backfill.py — recompute persisted cost stamps
that were written by the pre-``ebfd5464`` hybrid double-count.

WHY
===
The sankuai_anthropic gateway emits a HYBRID usage payload: ``prompt_tokens``
(the cache-INCLUSIVE total) beside ``cache_*_input_tokens`` (Anthropic residual
semantics). Before ``ebfd5464`` the cost engine read the TOTAL as if it were the
uncached RESIDUAL, so the whole prefix was re-priced at the uncached rate and
``totalInputTokens`` double-counted the cache.

Every ``cost`` block stamped on such a round is a snapshot of that wrong
arithmetic and is still what the UI renders for historical messages. This script
recomputes those stamps with the CURRENT engine.

SCOPE (measured, not assumed)
-----------------------------
Two carriers hold persisted cost, BOTH are rewritten:
  * ``messages[i].apiRounds[j].cost``  — the per-round stamps (AUTHORITATIVE)
  * ``messages[i].cost``               — the turn-level roll-up, REBUILT BY
    SUMMING the corrected per-round components.

★ WHY THE TURN TOTAL IS NOT RECOMPUTED FROM ``messages[i].usage``
-----------------------------------------------------------------
The first version of this script did exactly that and it was WRONG. The
turn-level ``usage`` is an AGGREGATE that has drifted from the sum of its
rounds: measured fleet-wide, only **3 of 117** turns satisfy the hybrid
identity exactly, while **115 of 117** satisfy
``sum(round.prompt_tokens) == input + cache_read + cache_write``.

The drift is real and sometimes large — the flagged turn ms5i5ydigs9j9w carries
``prompt_tokens = 5,562,791`` against a round sum of ``5,550,662`` (+12,129,
merged memory-prefetch/rerank usage that ``merge_usage_totals`` folds in), and
one conversation drifts by 53.5M. So a strict identity check on the turn-level
usage EXCLUDES the very turn the owner complained about, and pricing that
inflated aggregate directly would invent cost that no round ever incurred.

The per-round stamps are the reliable carrier: each round's usage is a single
verbatim provider payload. The turn total is therefore DERIVED by summing the
recomputed rounds — which is also what makes it internally consistent with the
per-round numbers the debug panel shows.

Deliberately NOT touched:
  * ``usage`` — the raw provider payload, at BOTH levels. It is the EVIDENCE
    this migration is derived from; rewriting it would destroy the ability to
    re-derive or audit, and the turn-level aggregate drift documented above is
    itself a signal worth preserving.
  * cache hit-rate / telemetry — probed and found to be log-and-in-memory only
    (``_roi.log_round_cache_stats`` emits a log line; ``_detect``'s
    ``total_input_tokens`` lives on the in-process ``CacheState``). Nothing
    hit-rate-shaped is persisted on a message, so there is nothing to backfill
    there; the corrected numbers appear on the next run by construction.

SAFETY
------
  * DRY-RUN BY DEFAULT. ``--apply`` is required to write.
  * IDEMPOTENT. A round is rewritten only when the recomputed cost actually
    differs; re-running after an apply is a no-op.
  * NARROW. Only rounds whose usage satisfies the measured hybrid identity
    ``input_tokens + cache_read + cache_write == prompt_tokens`` are considered.
    A pure-OpenAI or pure-Anthropic round is never touched.
  * CAS. Writes go through ``save_conversation_messages(expected_rev=...)`` so a
    concurrent writer cannot be silently clobbered (charter: the conversations
    blob is read-modify-write and non-CAS writes lose rows).
  * AUDITED. Each conversation rewritten emits an ``audit_log`` entry with the
    before/after totals.

Usage:
    python3 tests/_migrate_hybrid_cost_backfill.py            # dry run
    python3 tests/_migrate_hybrid_cost_backfill.py --verbose  # + per-conv detail
    python3 tests/_migrate_hybrid_cost_backfill.py --apply    # write
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cost import compute_cost  # noqa: E402
from lib.database import get_thread_db  # noqa: E402
from lib.log import audit_log, get_logger  # noqa: E402

logger = get_logger(__name__)


def _is_hybrid(u: dict) -> bool:
    """True when this usage carries BOTH spellings and satisfies the identity.

    That identity — verified on 28/28 rounds of ms5i5ydigs9j9w and 3,289 rounds
    fleet-wide — is what proves ``prompt_tokens`` is the cache-INCLUSIVE total
    rather than a residual. Requiring it keeps the migration off every other
    wire shape.
    """
    if not isinstance(u, dict):
        return False
    pt = u.get('prompt_tokens')
    it = u.get('input_tokens')
    cr = u.get('cache_read_input_tokens') or 0
    cw = u.get('cache_creation_input_tokens') or 0
    if not pt or it is None or not (cr or cw):
        return False
    try:
        return int(it) + int(cr) + int(cw) == int(pt)
    except (TypeError, ValueError):
        return False


def _recompute(u: dict, model: str, provider_id: str | None) -> dict | None:
    return compute_cost(u, model_id=model or '', provider_id=provider_id or None)


def _cny(c) -> float:
    return float((c or {}).get('costCny') or 0.0)


# Component fields summed when rebuilding a turn total from its rounds.
_SUM_FIELDS = (
    'costUsd', 'costCny',
    'inputTokens', 'outputTokens', 'totalInputTokens',
    'cacheWriteTokens', 'cacheReadTokens', 'thinkingTokens',
    'inputCostCny', 'outputCostCny', 'cacheWriteCostCny', 'cacheReadCostCny',
    'inputCostUsd', 'outputCostUsd', 'cacheWriteCostUsd', 'cacheReadCostUsd',
    'cacheSavingsCny', 'cacheSavingsUsd',
)


def _sum_round_costs(rounds: list) -> dict | None:
    """Rebuild a turn-level cost block by summing its per-round blocks.

    Used INSTEAD of pricing ``messages[i].usage`` directly, because that
    aggregate has drifted from the sum of its rounds on 115 of 117 measured
    turns (see the module docstring). Returns None when no round carries a
    cost block, so a turn is never rewritten from nothing.
    """
    costs = [r.get('cost') for r in rounds if isinstance(r.get('cost'), dict)]
    if not costs:
        return None
    out = {}
    for f in _SUM_FIELDS:
        vals = [c.get(f) for c in costs if isinstance(c.get(f), (int, float))]
        if vals:
            out[f] = sum(vals)
    return out or None


def _correct_message(m: dict, default_model: str, default_pid) -> tuple[int, bool, float, float]:
    """Apply the corrected pricing to ONE assistant message, IN PLACE.

    Returns ``(rounds_changed, turn_changed, turn_cost_before, turn_cost_after)``.

    THE single implementation of the mutation — both the dry-run scan and the
    apply path call it, so what the dry run reports is by construction what the
    apply writes. A second hand-written copy in the writer is exactly how a
    migration starts reporting one thing and doing another.
    """
    model = m.get('model') or default_model or ''
    pid = m.get('provider_id') or default_pid
    rounds = m.get('apiRounds') or []

    n_rounds = 0
    old_sum = new_sum = 0.0
    touched = False
    for ar in rounds:
        u = ar.get('usage') or {}
        if not _is_hybrid(u):
            continue
        fresh = _recompute(u, ar.get('model') or model,
                           (u.get('_dispatch') or {}).get('provider_id') or pid)
        if not fresh:
            continue
        old = _cny(ar.get('cost'))
        new = _cny(fresh)
        if abs(old - new) > 1e-9:
            old_sum += old
            new_sum += new
            n_rounds += 1
            touched = True
        ar['cost'] = fresh

    turn_changed = False
    turn_old = turn_new = 0.0
    if touched:
        rebuilt = _sum_round_costs(rounds)
        if rebuilt:
            turn_old = _cny(m.get('cost'))
            turn_new = _cny(rebuilt)
            if abs(turn_old - turn_new) > 1e-9:
                m['cost'] = rebuilt
                turn_changed = True
    return n_rounds, turn_changed, turn_old, turn_new


def _correct_messages(msgs: list) -> tuple[int, int, float, float]:
    """Apply the correction to every assistant message in a transcript, IN PLACE.

    Returns ``(rounds_changed, turns_changed, turn_cost_before, turn_cost_after)``.
    """
    n_rounds = n_turns = 0
    t_old = t_new = 0.0
    for m in msgs:
        if not isinstance(m, dict) or m.get('role') != 'assistant':
            continue
        r, tc, to, tn = _correct_message(m, m.get('model') or '',
                                         m.get('provider_id'))
        n_rounds += r
        if tc:
            n_turns += 1
            t_old += to
            t_new += tn
    return n_rounds, n_turns, t_old, t_new


def scan(verbose: bool = False):
    db = get_thread_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, messages::text AS m FROM conversations "
        "WHERE messages::text LIKE '%cache_creation_input_tokens%'")
    rows = cur.fetchall()

    plan = []
    tot_old = tot_new = 0.0
    n_rounds = n_turns = 0

    for row in rows:
        cid = row['id']
        try:
            msgs = json.loads(row['m'])
        except Exception as e:
            logger.warning('[Backfill] conv=%s unparseable messages: %s', cid, e)
            continue

        conv_rounds, conv_turns, conv_old, conv_new = _correct_messages(msgs)

        if conv_rounds or conv_turns:
            n_rounds += conv_rounds
            n_turns += conv_turns
            tot_old += conv_old
            tot_new += conv_new
            # NOTE: the mutated `msgs` is NOT carried to the writer. The apply
            # path re-reads each conversation and replays the correction on the
            # FRESH transcript (see main()), because this scan takes ~45s and a
            # sibling session can legitimately append during it.
            plan.append(cid)
            if verbose:
                print('  %-18s rounds=%-4d turns=%-3d  turn-cost %9.2f -> %9.2f'
                      % (cid, conv_rounds, conv_turns, conv_old, conv_new))

    return plan, tot_old, tot_new, n_rounds, n_turns


def apply_plan(conv_ids, *, max_attempts: int = 5):
    """Write the correction to each conversation, re-reading it FIRST.

    Returns ``(written, skipped_noop, failed)``.

    ★ WHY THIS RE-READS INSTEAD OF WRITING THE SCANNED COPY
    -------------------------------------------------------
    The scan is a ~45s full-table pass. A sibling session can legitimately
    append to any of these conversations while it runs, so the transcript the
    scan parsed is already potentially stale by the time the writer reaches it —
    and so is any ``rev`` captured alongside it. Writing that copy back would
    erase the sibling's append, which is precisely the incident
    ``DefaultConversationStore.save_conversation_messages`` was built to make
    unspellable (conv ms3sfyrmn31omb: 13 appends logged, 8 rows survived).

    So the plan carries only conversation IDs. For each one the writer does
    ``load_conversation_messages()`` — which returns ``(messages, updated_at,
    rev)`` from ONE statement — replays the correction onto that FRESH
    transcript, and CASes with the rev from that same read. The correction is
    a pure per-message recompute derived from each round's own ``usage``, so
    replaying it on newer data is well-defined: rounds the sibling added are
    corrected too, and rounds already correct are no-ops.

    A lost race is retried (the sibling wrote between our read and our write);
    only a genuinely exhausted retry budget counts as a failure.
    """
    from lib.agent_core.store import get_conversation_store
    from lib.tasks_pkg.persistence_store import ConcurrentWriteConflict

    store = get_conversation_store()
    written = skipped = failed = 0

    for cid in conv_ids:
        for attempt in range(1, int(max_attempts) + 1):
            try:
                loaded = store.load_conversation_messages(cid)
                if loaded is None:
                    logger.warning('[Backfill] conv=%s vanished before write', cid)
                    skipped += 1
                    break
                messages, _updated_at, rev = loaded

                n_rounds, n_turns, c_old, c_new = _correct_messages(messages)
                if not (n_rounds or n_turns):
                    # Already correct — a re-run after a previous apply, or the
                    # sibling's newer rounds were priced by the fixed engine.
                    skipped += 1
                    break

                store.save_conversation_messages(cid, messages, expected_rev=rev)
                audit_log('hybrid_cost_backfill', conv_id=cid[:12],
                          rounds=n_rounds, turns=n_turns,
                          cost_before=round(c_old, 4), cost_after=round(c_new, 4),
                          expected_rev=int(rev), attempt=attempt)
                logger.info('[Backfill] conv=%s rewrote %d round(s) / %d turn(s) '
                            '%.2f -> %.2f CNY (rev=%s)',
                            cid[:8], n_rounds, n_turns, c_old, c_new, rev)
                written += 1
                break

            except ConcurrentWriteConflict as e:
                # A sibling wrote between our read and our write. Re-read and
                # replay — never re-push the stale copy.
                logger.debug('[Backfill] conv=%s lost the rev race '
                             '(attempt %d/%d): %s', cid[:8], attempt,
                             max_attempts, e)
                if attempt == max_attempts:
                    logger.warning('[Backfill] conv=%s gave up after %d CAS '
                                   'attempts', cid[:8], max_attempts)
                    failed += 1
            except Exception as e:
                logger.error('[Backfill] conv=%s write failed: %s', cid, e,
                             exc_info=True)
                failed += 1
                break

    return written, skipped, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='actually write (default: dry run)')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    print('=' * 72)
    print('HYBRID COST BACKFILL — %s' % ('APPLY' if args.apply else 'DRY RUN'))
    print('=' * 72)
    if args.verbose:
        print('\nPer-conversation:')

    plan, tot_old, tot_new, n_rounds, n_turns = scan(verbose=args.verbose)

    print('\nBLAST RADIUS')
    print('  conversations touched : %d' % len(plan))
    print('  per-round stamps      : %d' % n_rounds)
    print('  turn-level stamps     : %d' % n_turns)
    print('\nTURN-LEVEL COST (what the UI renders)')
    print('  currently persisted   : %12.2f CNY' % tot_old)
    print('  recomputed (correct)  : %12.2f CNY' % tot_new)
    print('  overstatement removed : %12.2f CNY  (%.1f%%)'
          % (tot_old - tot_new,
             (100 * (tot_old - tot_new) / tot_old) if tot_old else 0.0))
    print('\nNOT TOUCHED (deliberate)')
    print('  usage (both levels) — raw provider evidence; this migration derives FROM it')
    print('  hit-rate / telemetry — probed: not persisted on messages at all')
    print('                         (log line + in-process CacheState only)')

    if not args.apply:
        print('\nDRY RUN — nothing written. Re-run with --apply to commit.')
        return 0

    ok, skipped, fail = apply_plan(plan)

    print('\nAPPLIED: %d conversations written, %d no-op, %d failed'
          % (ok, skipped, fail))
    if fail:
        print('  (failures are CAS conflicts or write errors — safe to re-run;'
              ' the script is idempotent)')
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())

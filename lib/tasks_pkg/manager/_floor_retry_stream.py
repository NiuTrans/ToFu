"""Floor-collapse resend, adoption, convergence, and billing authority.

The caller owns primary stream callbacks.  This helper retries only a proven
byte-stable floor collapse, adopts one complete resend atomically, preserves
prior-round task content, and returns every billed attempt to accounting."""

from __future__ import annotations

from typing import Any, Callable

from lib.cost import normalize_usage
from lib.llm.stream_result import (
    ProviderStreamResult,
    ensure_provider_stream_result,
)
from lib.log import get_logger


logger = get_logger(__name__)


def apply_floor_retry(
    task: dict[str, Any],
    body: dict[str, Any],
    msg: dict[str, Any],
    finish_reason: str,
    usage: Any,
    *,
    model: str,
    pool_wide: bool,
    pfx: str,
    tag: str,
    dispatch_stream_fn: Callable[..., Any],
    abort_check: Callable[[], bool],
    on_retry: Callable[..., Any],
    avoid_pairs: Any,
    on_waiting: Callable[..., Any],
    round_base_content: str,
    round_base_thinking: str,
    stream_result: ProviderStreamResult | None = None,
) -> ProviderStreamResult:

    current_result = (
        ensure_provider_stream_result(stream_result)
        if stream_result is not None
        else ProviderStreamResult.from_legacy(msg, finish_reason, usage)
    )

    # Floor-collapse identical-resend mitigation (env-gated, default OFF).
    #   A byte-STABLE round whose cache_read pinned at the system+tools floor
    #   is the SERVER-SIDE stochastic cache-write-visibility miss (proven by
    #   4-run identical-byte replay: different rounds collapse each run). A
    #   resend of the IDENTICAL body re-rolls the gateway's dice and usually
    #   hits the now-visible cache write — driving effective floor% toward zero
    #   (harness: mrsfs9d6 20%->0%). Discipline: only on a proven byte-stable
    #   collapse, capped, and STOP on a throttle error (don't pile retries on
    #   an already-throttled gateway). See lib/tasks_pkg/floor_retry.py +
    #   docs/LLM_COST_OPTIMIZATION.md.
    # Tracks whether ANY floor-retry resend's response was adopted into the
    #   returned (msg, finish_reason, usage). Both adoption sites below
    #   (RECOVERED and still-floored-loop-exhausted) stream with
    #   on_content=None / on_thinking=None, so the adopted resend's text NEVER
    #   reached task['content']/task['thinking'] — those still hold ONLY the
    #   FIRST attempt's (floor-collapsed, often partial) deltas. Since _sync
    #   persists from task['content'] (not the returned msg), an adopted resend
    #   would silently persist the first-attempt residue (the live 3411→215
    #   loss). We converge ONCE after the loop, covering both doors.
    _fr_adopted = False
    # HONEST ACCOUNTING: every attempt the gateway processed (whether
    #   ADOPTED or DISCARDED) was BILLED. Collect their usage dicts here
    #   so the outer LLM-fallback loop can append them to api_rounds and
    #   accumulate them — the "reported cost < actual gateway bill" bug
    #   is impossible when every billed request appears once in api_rounds.
    _fr_discarded_billing = []  # list of {'model', 'usage', 'tag'}
    try:
        from lib.tasks_pkg import floor_retry as _fr
        from lib.tasks_pkg.manager._registry import task_user_id
        _conv_for_fr = task.get('convId', '') or ''
        # pool_wide rescue: floor-retry resends the identical body to the
        # SAME model — undefined when the rescue is free to roam models —
        # so the mitigation stays off for rescue dispatches.
        if (_fr.floor_retry_enabled() and _conv_for_fr and not pool_wide
                and current_result.is_verified_complete
                and _fr.is_floor_collapse(usage)
                and _fr.wire_prefix_stable(
                    _conv_for_fr, usage, user_id=task_user_id(task))):
            _fr_max = _fr.floor_retry_max()
            # The primary attempt (whose msg/usage `usage` currently holds) is
            # the FIRST billed request; it is about to be superseded by a resend
            # if one recovers. Preserve its usage now so it survives the
            # `usage = _rusage` reassignments below.
            for _fr_i in range(_fr_max):
                if task.get('aborted', False):
                    break
                _fr_u = normalize_usage(usage)
                logger.warning(
                    '%s conv=%s [FloorRetry] byte-stable floor-collapse '
                    '(read=%s write=%s) — resending identical body (%d/%d)',
                    pfx, _conv_for_fr,
                    _fr_u['cache_read'], _fr_u['cache_write'],
                    _fr_i + 1, _fr_max)
                try:
                    # Layer-1 orphan fix: a FloorRetry resend re-streams the
                    #   IDENTICAL body purely to re-roll the gateway's cache-write
                    #   dice for a cheaper usage — its token/tool deltas are
                    #   THROWAWAY unless it RECOVERS (adopted below). Reusing
                    #   on_tool_call_ready here made every discarded resend
                    #   announce a fresh 'searching' tool round (new tc_id) that
                    #   never survived into the final assistant_msg → an orphan
                    #   swept to status='aborted' with an empty result, which the
                    #   reader then had to defend against (layer 2). Pass None —
                    #   exactly as on_thinking/on_content already are — so the
                    #   resend announces NOTHING. If it RECOVERS, parse_tool_calls
                    #   re-emits the adopted response's tool_start (a few-hundred-ms
                    #   later chip; functionally lossless — owner-approved).
                    retry_result = ensure_provider_stream_result(
                        dispatch_stream_fn(
                        body,
                        on_thinking=None, on_content=None,
                        on_tool_call_ready=None,
                        abort_check=abort_check,
                        prefer_model=model, log_prefix=f'{pfx}[floor-retry{_fr_i+1}]',
                        strict_model=True, on_retry=on_retry,
                        avoid_pairs=avoid_pairs,
                        on_waiting=on_waiting))
                    _rmsg, _rfin, _rusage = retry_result
                except Exception as _rerr:
                    # 503/throttle/transient — do NOT keep piling resends on an
                    # already-throttled gateway; that only deepens the throttle.
                    logger.warning('%s [FloorRetry] resend %d errored, stopping: '
                                   '%s: %s', pfx, _fr_i + 1,
                                   type(_rerr).__name__, str(_rerr)[:120])
                    break
                if not retry_result.is_verified_complete:
                    # Cache shape can say "recovered" while the provider
                    # stream itself is truncated or malformed. The closed
                    # stream state outranks cache economics: keep the current
                    # complete response, bill this processed resend honestly,
                    # and stop instead of adopting corrupt bytes.
                    if isinstance(_rusage, dict):
                        _fr_discarded_billing.append({
                            'model': model,
                            'usage': {
                                key: value for key, value in _rusage.items()
                                if key != '_extra_billing_rounds'
                            },
                            'tag': (
                                f'{tag}-FLOOR-DISCARDED-resend{_fr_i + 1}'
                                if tag else
                                f'FLOOR-DISCARDED-resend{_fr_i + 1}'
                            ),
                        })
                    logger.warning(
                        '%s conv=%s [FloorRetry] resend %d returned unusable '
                        'stream state=%s — retaining the prior verified '
                        'response and stopping floor retries',
                        pfx, _conv_for_fr, _fr_i + 1,
                        retry_result.state.value,
                    )
                    break
                # HONEST ACCOUNTING: the CURRENT `usage` is about to be
                #   superseded. Whatever it points to now (the primary attempt
                #   on iter 0, or the previously-floored resend on iter >0) was
                #   BILLED by the gateway — preserve it before overwriting.
                if isinstance(usage, dict):
                    _disc_tag_suffix = ('primary' if _fr_i == 0
                                        else f'resend{_fr_i}')
                    _fr_discarded_billing.append({
                        'model': model,
                        'usage': {k: v for k, v in usage.items()
                                  if k != '_extra_billing_rounds'},
                        'tag': f'{tag}-FLOOR-DISCARDED-{_disc_tag_suffix}'
                        if tag else f'FLOOR-DISCARDED-{_disc_tag_suffix}',
                    })
                if not _fr.is_floor_collapse(_rusage):
                    # Recovered: the resend hit the now-visible cache write.
                    # Adopt its response + usage (a genuine cache read, cheaper
                    # AND the same conversation content — the body was identical).
                    _ru = normalize_usage(_rusage)
                    logger.warning('%s conv=%s [FloorRetry] RECOVERED on resend %d '
                                   '(read=%s write=%s)', pfx, _conv_for_fr, _fr_i + 1,
                                   _ru['cache_read'], _ru['cache_write'])
                    msg, finish_reason, usage = _rmsg, _rfin, _rusage
                    current_result = retry_result
                    _fr_adopted = True
                    break
                # Still floored — keep the freshest usage and try again.
                msg, finish_reason, usage = _rmsg, _rfin, _rusage
                current_result = retry_result
                _fr_adopted = True
    except Exception as _fre:
        logger.debug('%s [FloorRetry] mitigation skipped (non-fatal): %s', pfx, _fre)

    # FloorRetry content-track convergence (fixes the 3411→215 silent loss).
    #   When a resend was adopted, its full text lives ONLY in the returned
    #   `msg` — the adopted resend streamed with on_content=None/on_thinking=None,
    #   so task['content']/task['thinking'] still hold the FIRST attempt's
    #   (floor-collapsed, partial) deltas. _sync persists from task['content'],
    #   so without this the partial first-attempt text is what lands in the DB.
    #   A resend is a byte-identical-body FRESH generation, so REPLACE (not
    #   append) — the adopted msg is the whole, authoritative answer. We do NOT
    #   emit DELTA_RESET / replay here: the done event and durable turn
    #   projection reconcile the live tab, so no new visual behavior.
    if _fr_adopted:
        with task['content_lock']:
            _discarded_content = task['content']
            _discarded_thinking = task['thinking']
            # Base-preserve (owner audit on ): task['content']/
            #   ['thinking'] ACCUMULATE across ALL rounds of this turn — the
            #   main orchestrator loop has no per-round content reset (only
            #   the one-time contentPrefix seed at _run.py:501). The adopted
            #   msg holds THIS round's text only, so a wholesale replace
            #   would silently drop every prior round's prose from the
            #   persisted answer (the R1+R2 preamble the user already read).
            #   Keep the round base captured at stream entry and replace
            #   only this round's tail. The residue recording below stays
            #   the FULL pre-convergence snapshot — the checkpointed conv
            #   row mirrors that full text and the terminal-guard exemption
            #   byte-matches on it.
            task['content'] = round_base_content + (msg.get('content') or '')
            task['thinking'] = round_base_thinking + (msg.get('reasoning_content') or '')
        # Record the DISCARDED first-attempt text verbatim (bounded). The
        #   ~5s streaming checkpoint mirrors task['content']/['thinking'] into
        #   conversations.messages DURING the attempt — so after this
        #   convergence the conv row can still hold the discarded draft while
        #   the task holds the adopted one. Downstream guards (the terminal
        #   content guard / CAS re-read guard in _sync.py) treat "existing >
        #   new" as "frontend genuinely won"; an EXACT byte-match against this
        #   recorded residue is how they tell our own discarded attempt apart
        #   from a real frontend win and overwrite it with the authoritative
        #   final answer (the live mrxij7q34xm070 "abrupt stop" bug: the
        #   4344-char discarded draft survived with a stop finish-tag).
        if _discarded_content or _discarded_thinking:
            _residue = task.setdefault('_floor_retry_residue', [])
            if len(_residue) < 8:
                _residue.append({'content': _discarded_content,
                                 'thinking': _discarded_thinking})
        # Record the TRUE cause of any orphan tool round this turn produces.
        #   When a FloorRetry resend is adopted, the FIRST attempt's tool calls
        #   (announced live via on_tool_call_ready → 'searching' rounds) are NOT
        #   in the adopted msg (the resend re-minted fresh tc_ids), so
        #   reconcile_announced_rounds settles them as 'superseded' orphans.
        #   This marker lets reconcile log the accurate cause (FloorRetry
        #   adoption) instead of the hardcoded — and, per the app.log evidence,
        #   FALSE — "discarded stream-retry attempt" story: stream transient
        #   retries were 0 while FloorRetry drove 100% of observed orphans.
        task['_floor_retry_adopted'] = True
        logger.info('%s [FloorRetry] converged task content/thinking from adopted '
                    'resend (content=%dchars thinking=%dchars) — prevents first-'
                    'attempt residue from being persisted',
                    pfx, len(task['content']), len(task['thinking']))

    # HONEST ACCOUNTING: expose every discarded-but-billed FloorRetry
    #   attempt on the returned usage dict so the LLM-fallback loop can
    #   append them to api_rounds and accumulated_usage. The gateway billed
    #   each of these; the cost popover / wallet / daily-report MUST see them.
    #   Silent covering-up of billed rounds is what motivated flipping the
    #   floor-retry default OFF — but even opt-in usage must be honest.
    if _fr_discarded_billing and isinstance(usage, dict):
        # dict.setdefault: never clobber a caller-provided list (defensive).
        _bill_list = usage.setdefault('_extra_billing_rounds', [])
        if isinstance(_bill_list, list):
            _bill_list.extend(_fr_discarded_billing)
        else:
            usage['_extra_billing_rounds'] = list(_fr_discarded_billing)
        logger.warning('%s [FloorRetry] preserved %d discarded-but-billed '
                       'attempt(s) for honest cost accounting: tags=%s',
                       pfx, len(_fr_discarded_billing),
                       [b['tag'] for b in _fr_discarded_billing])
    return current_result.with_usage(usage)

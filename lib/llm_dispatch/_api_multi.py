"""Multi-dispatch operations (fastest/parallel) and capability-routed smart_chat."""

from collections import defaultdict
from .factory import get_dispatcher
import threading
import time
from lib.log import get_logger
from lib.llm_dispatch._api_chat import dispatch_chat
from lib.llm_dispatch._api_contention import _note_shared_contention_recovered
from lib.llm_dispatch._api_errors import DispatchNoAdmissibleSlot, DispatchRateLimitBudgetExceeded, DispatchSharedContentionDeferred

logger = get_logger('lib.llm_dispatch.api')


def dispatch_fastest(messages, *, max_tokens=4096, temperature=0,
                     thinking_enabled=False, preset='low', effort=None,
                     capability='text', prefer_model=None,
                     n_race=2, tools=None, extra=None,
                     log_prefix='', owner_user_id: int | None = None):
    """Fire requests to N slots simultaneously, return the first successful result.

    This wastes some API quota but guarantees the fastest possible response.
    Best for latency-critical tasks.

    Args:
        n_race: Number of slots to race (default 2)
        (other args same as dispatch_chat)

    Returns:
        (content_text: str, usage_dict: dict)
    """
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    from lib.llm import chat

    dispatcher = get_dispatcher()
    slots = dispatcher.pick_top_n(n=n_race, capability=capability,
                                   prefer_model=prefer_model)

    if not slots:
        raise RuntimeError(f'No slots available for capability={capability}')

    if len(slots) == 1:
        # Only one slot — just call it directly
        return dispatch_chat(messages, max_tokens=max_tokens, temperature=temperature,
                             thinking_enabled=thinking_enabled, preset=preset,
                             effort=effort, capability=capability,
                             prefer_model=slots[0].model,
                             tools=tools, extra=extra, log_prefix=log_prefix,
                             owner_user_id=owner_user_id)

    cancel_event = threading.Event()

    def _race_worker(slot):
        if cancel_event.is_set():
            return None
        # Note: record_request() was already called atomically in pick_top_n(reserve=True)
        t0 = time.time()
        tag = f'{log_prefix}[Race:{slot.key_name}:{slot.model}]'
        try:
            _extra = dict(extra) if extra else {}
            if tools:
                _extra['tools'] = tools

            content, usage = chat(
                model=slot.model, messages=messages,
                max_tokens=max_tokens, temperature=temperature,
                thinking_enabled=thinking_enabled,
                effort=effort or preset,
                api_key=slot.api_key,
                base_url=slot.base_url or None,
                extra_headers=slot.extra_headers or None,
                extra=_extra or None,
                log_prefix=tag,
                max_retries=0,
                thinking_format=slot.thinking_format or '',
                provider_id=slot.provider_id or '',
                api_protocol=slot.protocol or 'openai',
                responses_feature_profile=(
                    getattr(slot, 'responses_profile', '') or 'compatible'),
                oauth=slot.oauth or '',
                adapter=slot.adapter or None,
                owner_user_id=owner_user_id,
            )
            latency = (time.time() - t0) * 1000
            _out_tokens = 0
            if isinstance(usage, dict):
                _out_tokens = (usage.get('completion_tokens')
                               or usage.get('output_tokens') or 0)
                try:
                    _out_tokens = int(_out_tokens)
                except (ValueError, TypeError) as _e_audit:
                    logger.debug('[api] _race_worker caught %s: %s', type(_e_audit).__name__, _e_audit)
                    _out_tokens = 0
            slot.record_success(latency, output_tokens=_out_tokens)
            _note_shared_contention_recovered(dispatcher, slot, tag)
            return (content, usage, slot)
        except Exception as e:
            latency = (time.time() - t0) * 1000
            err_str = str(e)
            is_429 = '429' in err_str or 'rate' in err_str.lower()
            slot.record_error(is_rate_limit=is_429)
            raise

    with ThreadPoolExecutor(max_workers=n_race) as pool:
        futures = {pool.submit(_race_worker, s): s for s in slots}

        # Wait for the first successful result
        last_err = None
        done, pending = wait(futures, return_when=FIRST_COMPLETED)

        while done:
            for fut in done:
                try:
                    result = fut.result()
                    if result is not None:
                        content, usage, winner = result
                        cancel_event.set()
                        # Cancel pending futures
                        for p in pending:
                            p.cancel()
                        # Inject dispatch metadata
                        if isinstance(usage, dict):
                            usage['_dispatch'] = {
                                'key': winner.key_name,
                                'model': winner.model,
                                'key_tail': (winner.api_key or '')[-4:],
                                'provider_id': winner.provider_id,
                                'protocol': winner.protocol or 'openai',
                                'responses_profile': (
                                    winner.responses_profile or ''),
                                'latency_ms': round(winner.latency_ema),
                                'mode': 'race',
                            }
                        logger.debug('%s[Race] Winner: %s:%s', log_prefix, winner.key_name, winner.model)
                        return content, usage
                except Exception as e:
                    logger.debug('[Dispatch] race candidate failed: %s', e, exc_info=True)
                    last_err = e

            if pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
            else:
                break

        raise last_err or RuntimeError(
            'All %d race participants failed for capability=%s' % (n_race, capability))


def dispatch_parallel(tasks, *, capability='text', max_workers=4, log_prefix='',
                      owner_user_id: int | None = None):
    """Execute multiple LLM tasks in parallel, distributing across slots.

    Args:
        tasks: List of dicts, each with:
            - 'messages': chat messages
            - 'max_tokens': (optional, default 4096)
            - 'temperature': (optional, default 0)
            - 'prefer_model': (optional)
            - 'extra': (optional)
        capability: Required capability for all tasks
        max_workers: Max concurrent requests

    Returns:
        List of (content, usage) tuples in the same order as tasks.
    """
    results = [None] * len(tasks)

    def _do_task(idx, task):
        try:
            task_owner_user_id = (
                task['owner_user_id']
                if 'owner_user_id' in task else owner_user_id
            )
            content, usage = dispatch_chat(
                task['messages'],
                max_tokens=task.get('max_tokens', 4096),
                temperature=task.get('temperature', 0),
                capability=capability,
                prefer_model=task.get('prefer_model'),
                extra=task.get('extra'),
                owner_user_id=task_owner_user_id,
                log_prefix=f'{log_prefix}[P{idx}]',
            )
            return idx, (content, usage)
        except Exception as e:
            logger.debug('[Dispatch] parallel task[%d] failed (model=%s): %s', idx, task.get('prefer_model', '?'), e, exc_info=True)
            return idx, (None, {'error': str(e)})

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_do_task, i, t) for i, t in enumerate(tasks)]
        for fut in as_completed(futures):
            idx, result = fut.result()
            results[idx] = result

    return results


def get_dispatch_status() -> dict:
    """Return current dispatcher status for monitoring/debugging."""
    d = get_dispatcher()
    slots_info = d.get_slots_info()
    return {
        'slots': slots_info,
        'total_slots': len(d.slots),
        'available_slots': sum(1 for s in d.slots if s.is_available),
        'by_capability': _group_by_capability(slots_info),
        'shared_contention': d.get_shared_contention_info(),
    }


def _group_by_capability(slots_info):
    """Group slot info by capability for easy overview."""
    caps = defaultdict(list)
    for s in slots_info:
        for c in s.get('capabilities', []):
            caps[c].append({
                'slot': f"{s['key']}:{s['model']}",
                'score': s['score'],
                'available': s['available'],
                'rpm_headroom_pct': s['rpm_headroom_pct'],
            })
    return dict(caps)


def smart_chat(messages, *, model=None, max_tokens=4096, temperature=0,
               thinking_enabled=False, preset='low', effort=None,
               capability='text', tools=None, extra=None,
               log_prefix='', max_retries=3, timeout=None,
               exclude_models=None, abort_check=None,
               max_429_attempts: int | None = None,
               defer_on_shared_contention: bool = False,
               owner_user_id: int | None = None, **_kw):
    """Drop-in replacement for ``lib.llm.chat()`` with auto dispatch.

    Uses the fastest available (key, model) slot across ALL keys.
    Falls back to direct ``chat()`` if dispatch fails entirely.

    Signature is intentionally close to ``chat()`` so call sites only
    need to change ``from lib.llm import chat`` →
    ``from lib.llm_dispatch import smart_chat as chat``.

    Extra kwargs (api_key, etc.) are silently ignored so callers that
    sometimes pass api_key don't break.
    """
    try:
        return dispatch_chat(
            messages, max_tokens=max_tokens, temperature=temperature,
            thinking_enabled=thinking_enabled, preset=preset,
            effort=effort, capability=capability,
            prefer_model=model, tools=tools, extra=extra,
            max_retries=max_retries, log_prefix=log_prefix,
            timeout=timeout,
            exclude_models=exclude_models,
            abort_check=abort_check,
            max_429_attempts=max_429_attempts,
            defer_on_shared_contention=defer_on_shared_contention,
            owner_user_id=owner_user_id,
        )
    except Exception as e:
        from lib.llm import AbortedError
        if isinstance(e, (
                AbortedError,
                DispatchNoAdmissibleSlot,
                DispatchRateLimitBudgetExceeded,
                DispatchSharedContentionDeferred,
        )):
            # Cancellation is terminal for this caller. Falling through to
            # direct chat would resurrect a timed-out or budget-exhausted
            # background request and keep consuming the shared model pool
            # after its owner returned.
            raise
        from lib.key_stats import is_strict_billing_stop_admission
        if is_strict_billing_stop_admission():
            # Optional background work deliberately admitted only slots with
            # no recorded billing stop. A direct lib.llm fallback chooses its
            # own default key and would bypass that filter, repeating a known
            # 402/quota failure outside the dispatcher's accounting boundary.
            logger.info(
                '%s[Dispatch] Strict billing-stop admission rejected direct '
                'chat fallback after dispatch failure: %s', log_prefix, e)
            raise
        # Ultimate fallback — direct call with default key
        logger.warning('%s[Dispatch] All slots exhausted (%s), '
                    'falling back to direct chat()', log_prefix, e, exc_info=True)
        from lib.llm import chat
        _fb_timeout = timeout
        # For 'cheap' tasks (translate etc.), fall back to a cheap model,
        #   NOT the default LLM_MODEL (which is Opus — way too slow/expensive).
        _fb_model = model
        if not _fb_model and capability == 'cheap':
            from lib import GEMINI_MODEL
            _fb_model = GEMINI_MODEL   # gemini-2.5-flash — fast & cheap
            logger.info('%s Fallback using cheap model: %s', log_prefix, _fb_model)
        # dispatch_chat folds tool definitions into ``extra`` before calling
        # the low-level transport. Preserve that exact request authority on
        # the direct fallback; silently dropping tools here changes an
        # agentic turn into an unrelated plain-chat request.
        _fb_extra = dict(extra) if extra else {}
        if tools:
            _fb_extra['tools'] = tools
        # ── Audit trail: record the model switch on exhaustion fallback ──
        try:
            from lib.log import audit_log as _audit
            _audit('model_switch',
                   old=(model or '(dispatch-auto)'),
                   new=(_fb_model or '(lib.llm default)'),
                   reason='dispatch_exhausted',
                   capability=capability,
                   error=str(e)[:200])
        except Exception as _aerr:
            logger.debug('%s audit_log model_switch failed: %s', log_prefix, _aerr)
        return chat(messages=messages, model=_fb_model,
                    max_tokens=max_tokens, temperature=temperature,
                    thinking_enabled=thinking_enabled, effort=effort or preset,
                    extra=_fb_extra or None,
                    log_prefix=log_prefix, timeout=_fb_timeout,
                    owner_user_id=owner_user_id)


def smart_chat_batch(prompts, *, max_tokens=4096, temperature=0,
                     capability='text', log_prefix='',
                     max_concurrent=8, **kw):
    """Send multiple independent prompts concurrently via dispatch.

    Each prompt is dispatched to a different slot (potentially different
    keys and models), maximising throughput across all available RPM.

    Args:
        prompts: list of str | list of list[dict]  (raw text or messages)
        max_concurrent: max parallel workers (default 8)
        **kw: forwarded to dispatch_chat

    Returns:
        list of (content, usage) tuples — same order as input
    """
    import concurrent.futures

    def _to_messages(p):
        if isinstance(p, str):
            return [{'role': 'user', 'content': p}]
        return p  # already messages list

    results = [None] * len(prompts)

    def _worker(idx, msgs):
        try:
            return idx, dispatch_chat(
                msgs, max_tokens=max_tokens, temperature=temperature,
                capability=capability,
                log_prefix=f'{log_prefix}[batch:{idx}]', **kw)
        except Exception as e:
            logger.warning('%s[batch:%d] Failed: %s', log_prefix, idx, e, exc_info=True)
            return idx, (f'[Error] {e}', {})

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(prompts), max_concurrent)) as pool:
        futures = [pool.submit(_worker, i, _to_messages(p))
                   for i, p in enumerate(prompts)]
        for f in concurrent.futures.as_completed(futures):
            idx, result = f.result()
            results[idx] = result

    return results

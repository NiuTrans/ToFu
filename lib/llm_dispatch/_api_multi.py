"""Multi-dispatch operations (fastest/parallel) and capability-routed smart_chat."""
from lib.llm_dispatch._api_errors import DispatchNoAdmissibleSlot as DispatchNoAdmissibleSlot
from lib.llm_dispatch._api_errors import DispatchRateLimitBudgetExceeded as DispatchRateLimitBudgetExceeded
from lib.llm_dispatch._api_errors import DispatchSharedContentionDeferred as DispatchSharedContentionDeferred

from collections import defaultdict
from .factory import get_dispatcher
import threading
import time
from lib.log import get_logger
from lib.llm_dispatch._api_chat import dispatch_chat
from lib.llm_dispatch._api_contention import _note_shared_contention_recovered

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

    if len(slots) <= 1:
        # Let normal dispatch own temporary capacity waits and its reservation.
        preferred = slots[0].model if slots else prefer_model
        if slots:
            slots[0].release()
        return dispatch_chat(messages, max_tokens=max_tokens, temperature=temperature,
                             thinking_enabled=thinking_enabled, preset=preset,
                             effort=effort, capability=capability,
                             prefer_model=preferred,
                             tools=tools, extra=extra, log_prefix=log_prefix,
                             owner_user_id=owner_user_id)

    cancel_event = threading.Event()

    def _race_worker(slot):
        if cancel_event.is_set():
            slot.release()
            return None
        # Note: record_request() was already called atomically in pick_top_n(reserve=True)
        t0 = time.time()
        tag = f'{log_prefix}[Race:{slot.key_name}:{slot.model}]'
        settled = False
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
            settled = True
            _note_shared_contention_recovered(dispatcher, slot, tag)
            return (content, usage, slot)
        except Exception as e:
            from lib.llm.llm_errors import LocalRequestPreparationError, RateLimitError
            if not settled:
                if isinstance(e, LocalRequestPreparationError):
                    slot.release()
                else:
                    rate_limit = isinstance(e, RateLimitError)
                    slot.record_error(
                        error=e, is_rate_limit=rate_limit,
                        is_quota_exhausted=rate_limit and e.is_quota,
                        is_gateway=rate_limit and e.is_gateway,
                        is_shared_contention=rate_limit and e.is_shared_contention,
                        is_account_quota=rate_limit and e.status_code == 402,
                        cooldown_s=e.retry_after_s if rate_limit else None,
                    )
                settled = True
            raise
        finally:
            if not settled:
                slot.release()

    try:
        pool = ThreadPoolExecutor(max_workers=n_race)
    except BaseException:
        for slot in slots:
            slot.release()
        raise
    with pool:
        futures = {}
        submitted = 0
        try:
            for slot in slots:
                futures[pool.submit(_race_worker, slot)] = slot
                submitted += 1
        except BaseException:
            for slot in slots[submitted:]:
                slot.release()
            cancel_event.set()
            raise

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
                            if p.cancel():
                                futures[p].release()
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
        max_workers: Idle worker reuse hint; accepted requests have no count window

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

    from concurrent.futures import as_completed
    from lib.observability import InstrumentedThreadPoolExecutor

    with InstrumentedThreadPoolExecutor(max_workers=max_workers,
                                       metric_pool='llm-parallel',
                                       thread_name_prefix='llm-parallel') as pool:
        futures = {}
        for index, task in enumerate(tasks):
            try:
                futures[pool.submit(_do_task, index, task)] = index
            except Exception as error:
                # A failed submit is guaranteed unstarted; earlier requests
                # remain owned and later authorized requests still start.
                logger.debug('dispatch_parallel failure handled: %s', type(error).__name__)
                results[index] = (None, {'error': str(error)})
        for future in as_completed(futures):
            index = futures[future]
            try:
                _index, results[index] = future.result()
            except Exception as error:
                logger.debug('dispatch_parallel failure handled: %s', type(error).__name__)
                results[index] = (None, {'error': str(error)})

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
               owner_user_id: int | None = None, model_budget_admit=None, **_kw):
    """Drop-in replacement for ``lib.llm.chat()`` with auto dispatch.

    Uses the fastest available (key, model) slot across ALL keys.
    The dispatcher owns every route, retry and credential selection.
    Exhaustion propagates its original typed failure to the caller.

    Signature is intentionally close to ``chat()`` so call sites only
    need to change ``from lib.llm import chat`` →
    ``from lib.llm_dispatch import smart_chat as chat``.

    Extra kwargs (api_key, etc.) are silently ignored so callers that
    sometimes pass api_key don't break.
    """
    if model_budget_admit is not None and not callable(model_budget_admit):
        raise ValueError('model_budget_admit must be callable')
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
        **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}),
    )


def smart_chat_batch(prompts, *, max_tokens=4096, temperature=0,
                     capability='text', log_prefix='',
                     max_concurrent=8, **kw):
    """Send multiple independent prompts concurrently via dispatch.

    Each prompt is dispatched to a different slot (potentially different
    keys and models), maximising throughput across all available RPM.

    Args:
        prompts: list of str | list of list[dict]  (raw text or messages)
        max_concurrent: idle worker reuse hint (default 8), never an active quota
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

    if not prompts:
        return results
    from lib.observability import InstrumentedThreadPoolExecutor
    with InstrumentedThreadPoolExecutor(max_workers=max_concurrent,
                                       metric_pool='llm-batch',
                                       thread_name_prefix='llm-batch') as pool:
        futures = {}
        for index, prompt in enumerate(prompts):
            try:
                futures[pool.submit(_worker, index, _to_messages(prompt))] = index
            except Exception as error:
                logger.debug('smart_chat_batch failure handled: %s', type(error).__name__)
                results[index] = (f'[Error] {error}', {})
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                _index, results[index] = future.result()
            except Exception as error:
                logger.debug('smart_chat_batch failure handled: %s', type(error).__name__)
                results[index] = (f'[Error] {error}', {})

    return results

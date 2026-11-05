"""Route and retry a translation input, then commit an accepted candidate.

Entry points: _translate_one_chunk and the whole-document _translate_freetext
wrapper. Dependencies: shared dispatcher/billing, translation stores and
_candidate's quality assessment. This module owns deadlines, cancellation,
dispatch error policy and final positive-cache commit; content predicates live
in _quality_checks.

Tries (in order):
    1. Translation cache lookup.
    2. Configured machine-translation provider (NiuTrans etc.).
    3. LLM dispatch (cheap-tier) with an aggressive retry loop that
       penalises slots / excludes models on empty / truncated / no-op
       output.

Returns ``(translated_text, usage_dict)``. The usage dict carries
``_dispatch.model`` and ``_dispatch.key`` so the caller can identify
which (key, model) actually produced the output.
"""

import time

from lib import translate_cache
from lib import translate_refusal
from lib.log import get_logger

from lib.llm.diagnostic_timing import timed_call, timed_stage
from ..observability import observed_engine

from ..dedup import _dedup_repetition_loop
from ..errors import (
    TranslationContentRefused,
    TranslationDeadlineExhausted,
    TranslationNoAdmissibleProvider,
)
from ..prompt import _wrap_for_translation
from ._candidate import assess_translation_candidate
from ._quality_checks import (
    _strip_model_thinking as _strip_model_thinking,
    _targets_chinese,
    overgeneration_verdict,
)

logger = get_logger(__name__)


# ── Over-generation guard thresholds (symmetric to the truncation floors) ──
# A translation should be roughly the same order of magnitude as its input.
# When a cheap model finishes the real translation of a SHORT input then keeps
# going — appending a large block of unrelated training-corpus text (the
# "poetry bleed" contamination: a 45→2373 char, 52x expansion committed as
# verdict=ok because every other guard only defends against SHORT output) —
# reject it. Both floors must be crossed together so a legitimately terse input
# ("你好" → a short sentence) is never killed: the output must be BOTH more than
# _OVERGEN_RATIO× the input AND more than _OVERGEN_ABS chars longer than it.
# Module-level so tests can monkeypatch (a NEUTER control raises them so the
# guard cannot fire, proving it is load-bearing).
_OVERGEN_RATIO = 8.0
_OVERGEN_ABS = 800
# Below this input length the ratio is meaningless (a 3-char input trivially
# expands), so the guard is disabled — matches the truncation guard's own
# length gates.
_OVERGEN_MIN_INPUT = 20

def _is_identity_invariant(text, target):
    """Compatibility hook for the shared whole-document identity policy."""
    from lib.translate.skip_policy import identity_invariant_reason

    return identity_invariant_reason(text, target)


def _build_trace(*, path, model, in_chars, out_chars, attempts=1,
                 content_fails=0, dispatch_fails=0, elapsed=0.0,
                 verdict='ok', target=''):
    """Build a structured provenance record for one translation call.

    Attached to the returned usage dict as ``_translate_trace`` and emitted
    as a single grep-able ``[Translate:trace]`` log line so every
    auto-translate leaves a machine-parseable audit trail (path taken,
    model, attempts, in/out length, ratio, completeness verdict).
    """
    return {
        'path': path, 'model': model, 'in_chars': in_chars,
        'out_chars': out_chars,
        'ratio': round(out_chars / in_chars, 3) if in_chars else 0.0,
        'attempts': attempts, 'content_fails': content_fails,
        'dispatch_fails': dispatch_fails, 'elapsed': round(elapsed, 1),
        'verdict': verdict, 'target': target,
    }


def _translate_freetext(text, system_prompt, chunk_label='',
                        source='', target='', status_cb=None,
                        progress_cb=None, overall_deadline=None,
                        use_cache=True, abort_check=None,
                        max_429_attempts=None,
                        defer_on_shared_contention=False, model_budget_admit=None):
    """Translate free-text in a SINGLE LLM call — no input splitting.

    Public free-text entry point used by every caller (server auto-translate,
    sync ``/api/v1/translate``, the chat send-path, the legacy message queue,
    and the re-translate script).  Forwards the WHOLE text to
    ``_translate_one_chunk``; the trailing ``chunk`` vocabulary is historical.

    Input splitting was tried and removed: cutting a message into pieces
    isolates an already-target-language tail (or severs a ``` fence) into its
    own call, where a weak cheap model "translates" it into the wrong language
    or echoes it verbatim.  A whole-document call lets the prompt's mixed-
    language rule keep already-target text intact while translating the rest,
    and the engine's truncation detector + model-rotation retry cover the
    long-input case the split was meant to solve.
    """
    return _translate_one_chunk(
        text, system_prompt, chunk_label=chunk_label,
        source=source, target=target, status_cb=status_cb,
        progress_cb=progress_cb, overall_deadline=overall_deadline,
        use_cache=use_cache, abort_check=abort_check,
        max_429_attempts=max_429_attempts,
        defer_on_shared_contention=defer_on_shared_contention,
        **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}))


@timed_stage('engine_attempt')
def _dispatch_translation_candidate(
        messages, *, chunk_label, max_tokens, timeout, max_retries,
        excluded_models, progress_cb, deadline_exceeded,
        remaining_deadline_seconds, prefer_model=None, strict_model=False,
        stream=False, capability='cheap', temperature=1,
        max_429_attempts=None, defer_on_shared_contention=False, model_budget_admit=None,
        attempt_timeout=None, first_byte_timeout=None):
    """Run one translation attempt and normalize stream/non-stream results.

    ``attempt_timeout`` / ``first_byte_timeout`` are hard bounds on THIS
    attempt, enforced by the transport watchdog. They exist because a
    translation task owns a fixed total budget: without them one silent slot
    consumes the entire budget and the retry that would have succeeded is
    never dispatched.
    """
    from lib.llm_dispatch import dispatch_chat, dispatch_stream, smart_chat
    from lib.llm_dispatch.conv_affinity import conv_affinity_cleared
    from lib.key_stats import strict_billing_stop_admission
    from lib.translate.model_gate import translation_model_slot
    from lib.translate.policy import translation_max_429_attempts

    if max_429_attempts is None:
        max_429_attempts = translation_max_429_attempts()

    # Translation is stateless: every request carries a fresh payload, so a
    # conversation's sticky key / warm-key hold buys nothing and only stalls
    # 429 failover (up to seconds per dispatch). Dispatch unbound — the
    # picker is purely score-based and a 429 rotates to whatever is healthy.
    if progress_cb is None and not stream:
        with strict_billing_stop_admission():
            with translation_model_slot(abort_check=deadline_exceeded):
                with conv_affinity_cleared():
                    dispatch = dispatch_chat if strict_model else smart_chat
                    model_arguments = (
                        {'prefer_model': prefer_model, 'strict_model': strict_model}
                        if strict_model else {'model': prefer_model}
                    )
                    return dispatch(
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        capability=capability,
                        thinking_enabled=False,
                        log_prefix=f'[Translate{chunk_label}]',
                        timeout=min(float(timeout),
                                    max(0.1, remaining_deadline_seconds())),
                        max_retries=max_retries,
                        exclude_models=excluded_models or None,
                        abort_check=deadline_exceeded,
                        max_429_attempts=max_429_attempts,
                        defer_on_shared_contention=defer_on_shared_contention,
                        **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}),
                        **model_arguments,
                    )

    stream_buffer = []

    def on_content_delta(delta):
        # ``dispatch_stream`` already returns the authoritative assembled
        # message. Keep a second copy only when a caller explicitly needs
        # progressive previews; paper translation streams for transport
        # verification without paying duplicate output memory.
        if not delta or progress_cb is None:
            return
        stream_buffer.append(delta)
        try:
            progress_cb(''.join(stream_buffer))
        except Exception as callback_error:
            logger.debug('[Translate%s] progress_cb failed: %s',
                         chunk_label, callback_error)

    from lib.llm.stream_result import require_verified_provider_stream_result
    with strict_billing_stop_admission():
        with translation_model_slot(abort_check=deadline_exceeded):
            with conv_affinity_cleared():
                stream_result = require_verified_provider_stream_result(dispatch_stream(
                    messages,
                    on_content=on_content_delta,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    capability=capability,
                    # An unset intent emits the model's declared default rung
                    # (max on always-thinking Kimi K3); off degrades to the
                    # lowest rung on models that cannot disable reasoning.
                    thinking_enabled=False,
                    prefer_model=prefer_model,
                    strict_model=strict_model,
                    log_prefix=f'[Translate{chunk_label}]',
                    max_retries=max_retries,
                    exclude_models=excluded_models or None,
                    abort_check=deadline_exceeded,
                    max_429_attempts=max_429_attempts,
                    defer_on_shared_contention=defer_on_shared_contention,
                    attempt_timeout=attempt_timeout,
                    first_byte_timeout=first_byte_timeout,
                    **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}),
                ), context='streaming translation candidate')
    stream_message = stream_result.message
    finish_reason = stream_result.provider_finish_reason
    usage = stream_result.usage
    if isinstance(stream_message, dict):
        content = stream_message.get('content', '') or ''
    else:
        content = stream_message or ''
    normalized_usage = dict(usage or {})
    if finish_reason:
        normalized_usage.setdefault('finish_reason', finish_reason)
    return content, normalized_usage


def _try_translation_fast_paths(
    chunk,
    *,
    source,
    target,
    chunk_label,
    use_cache,
    allow_mt,
    abort_requested,
    notify,
):
    """Return a cache/identity/MT result, or ``None`` for LLM fallback."""
    from lib.llm.llm_errors import AbortedError

    cached = timed_call('cache_lookup', translate_cache.get, chunk, source, target) if use_cache else None
    if cached and cached.get('translated'):
        cached_text = cached['translated']
        cached_model = cached.get('model', '') or 'cache'
        logger.info('[Translate%s] Cache hit: %d→%d chars model=%s',
                    chunk_label, len(chunk), len(cached_text), cached_model)
        return cached_text, {
            'model': cached_model,
            '_dispatch': {'model': cached_model},
            '_cache_hit': True,
            '_translate_trace': _build_trace(
                path='cache', model=cached_model, in_chars=len(chunk),
                out_chars=len(cached_text), verdict='cache', target=target),
        }

    invariant, invariant_reason = timed_call('identity_check', _is_identity_invariant,
        chunk.strip(), target)
    if invariant:
        passthrough = chunk.strip()
        logger.info(
            '[Translate%s] Identity-invariant content accepted verbatim '
            '(%d chars, reason=%s) — skipping translation',
            chunk_label, len(passthrough), invariant_reason)
        if use_cache:
            timed_call('cache_store', translate_cache.put,
                chunk, source, target, passthrough, model='identity')
        return passthrough, {
            'model': 'identity',
            '_dispatch': {'model': 'identity'},
            '_identity_invariant': True,
            '_translate_trace': _build_trace(
                path='identity', model='identity', in_chars=len(chunk),
                out_chars=len(passthrough), verdict='identity', target=target),
        }

    from lib.mt_provider import is_mt_configured, mt_translate_chunked
    if not allow_mt or not is_mt_configured():
        return None
    try:
        from lib.translate.model_gate import translation_model_slot

        started_at = time.time()
        with translation_model_slot(abort_check=abort_requested):
            result = timed_call('machine_translation', mt_translate_chunked,
                chunk, source=source, target=target)
        if abort_requested():
            raise AbortedError('Translation aborted during MT request')
        elapsed = time.time() - started_at
        logger.info('[Translate%s] MT provider: %d→%d chars in %.1fs',
                    chunk_label, len(chunk), len(result), elapsed)
        timed_call('cache_store', translate_cache.put,
            chunk, source, target, result, model='mt:niutrans')
        return result, {
            'model': 'mt:niutrans',
            '_dispatch': {'model': 'mt:niutrans'},
            '_translate_trace': _build_trace(
                path='mt', model='mt:niutrans', in_chars=len(chunk),
                out_chars=len(result), elapsed=elapsed, verdict='mt',
                target=target),
        }
    except AbortedError:
        raise
    except Exception as exc:
        logger.warning(
            '[Translate%s] MT provider failed, falling back to LLM: %s',
            chunk_label, exc)
        notify('mt_fallback', 1, 0, str(exc)[:120])
        return None


class _TranslationDeadline:
    """One abort-aware monotonic deadline shared by dispatch and backoff."""

    __slots__ = ('abort_requested', 'seconds', 'started_at', 'deadline_at')

    def __init__(self, seconds, abort_requested):
        self.abort_requested = abort_requested
        self.seconds = max(0.0, float(seconds))
        self.started_at = time.monotonic()
        self.deadline_at = self.started_at + self.seconds

    def exceeded(self):
        return self.abort_requested() or time.monotonic() >= self.deadline_at

    def remaining(self):
        return max(0.0, self.deadline_at - time.monotonic())

    @timed_stage('engine_backoff')
    def sleep(self, seconds):
        from lib.llm.llm_errors import AbortedError

        until = min(
            self.deadline_at,
            time.monotonic() + max(0.0, float(seconds)),
        )
        while time.monotonic() < until:
            if self.abort_requested():
                raise AbortedError('Translation aborted during retry backoff')
            time.sleep(min(0.1, max(0.0, until - time.monotonic())))


def _raise_cached_translation_refusal(chunk, source, target, *,
                                      chunk_label, max_content_retries):
    """Replay a fresh content-shape refusal without burning the roster again."""
    refusal = translate_refusal.get(chunk, source, target)
    if not refusal:
        return
    logger.info(
        '[Translate%s] Refusal marker hit: verdict=%s model=%s — replaying '
        'cached refusal with zero dispatches',
        chunk_label, refusal.get('verdict'), refusal.get('model'))
    raise TranslationContentRefused(
        refusal['verdict'],
        f"{refusal.get('reason', '')} (cached refusal, 0 dispatches)",
        attempts=0,
        content_fails=refusal.get('content_fails') or max_content_retries,
    )


def _translation_status_notifier(status_cb, chunk_label):
    def _notify(kind, attempt, elapsed, detail=''):
        """Safely invoke status_cb — never let caller's callback break us."""
        if status_cb is None:
            return
        try:
            timed_call('status_delivery', status_cb, {
                'kind': kind,
                'attempt': attempt,
                'elapsed': elapsed,
                'detail': detail,
            })
        except Exception as e:
            logger.debug('[Translate%s] status_cb failed: %s', chunk_label, e)

    return _notify


def _translation_chunk_attempt_limits(clen):
    # Bigger per-attempt budget + more dispatch retries per attempt.
    if clen > 6000:
        _mt, _timeout, _retries = 16000, 180, 12
    elif clen > 3000:
        _mt, _timeout, _retries = 12000, 120, 10
    else:
        _mt, _timeout, _retries = 8000, 60, 8

    return _mt, _timeout, _retries


# A whole-document translation is ONE provider call. Three independent bounds
# are required, because they answer different questions:
#   * ``translation_first_byte_timeout_seconds`` — how long to wait for response
#     headers. This is the cheap liveness proof and the one that actually made
#     the difference.
#   * ``translation_attempt_timeout_seconds`` — how long ONE physical attempt
#     may run once headers arrived. The idle window measures silence only, so
#     an attempt that keeps the socket warm while producing nothing is
#     otherwise unbounded.
#   * ``translation_total_deadline_seconds`` — how long the whole task may run.
#
# A per-attempt bound that consumes the whole task budget is the failure this
# replaces: on 2026-10-21 (conv mvhiznrwv669ue, turn f85740dd) a provider sent
# no response headers at all, the single attempt sat for 90.7s until the total
# budget expired, and the retry that would have delivered the 2783-char answer
# was never dispatched.
#
# The attempt window therefore stays exactly the size table's long-standing
# dispatch timeout (60/120/180s): a healthy but slow model must not be cut off
# mid-generation, and shortening it would trade this bug for truncated output.
# Speed comes from the first-byte bound plus pair rotation, and from the total
# budget always affording a second full attempt.
#
# Headers are the cheapest liveness signal: real first-byte latency is
# single-digit seconds even for long prompts, so waiting minutes for them
# proves the slot is dead, never that the answer is nearly done.
_ATTEMPT_FIRST_BYTE_SECONDS = 20.0
# A fixed 20s header wait is still most of a short caller budget: a silent slot
# could spend 20s of a 30s preview budget and leave the rotated healthy slot
# 10s. A slot that sends no headers may hold at most this share of the remaining
# budget, so the replacement attempt keeps at least as long as the silent one
# had; budgets of 40s or more keep the full bound.
_FIRST_BYTE_REMAINING_BUDGET_SHARE = 0.5
# Room for at least two full attempts: the first may be a silent slot, and the
# retry on a different model is what actually delivers the result.
_TRANSLATION_TOTAL_DEADLINE_FLOOR_SECONDS = 90.0
_TRANSLATION_TOTAL_DEADLINE_ATTEMPT_ALLOWANCE = 2.0
# Content-shape failures (empty / truncated / no-op / wrong-language) and
# transport failures share this retry budget: a different model or slot usually
# fixes both, and past it the input itself is the likelier cause.
_MAX_CONTENT_RETRIES = 5


def translation_attempt_timeout_seconds(chars):
    """Return the wall bound for ONE translation attempt on ``chars``.

    This is the size table's own dispatch timeout, finally enforced on the
    STREAMING transport too. It was always documented as the per-attempt
    window; only the non-streaming read clock ever honored it, so a streaming
    translation had no attempt bound at all.
    """
    _mt, dispatch_timeout, _retries = _translation_chunk_attempt_limits(chars)
    return float(dispatch_timeout)


def translation_first_byte_timeout_seconds(chars, remaining_seconds=None):
    """Return the response-header bound for one attempt (never above it).

    With ``remaining_seconds`` the bound is also capped at the caller's
    remaining-budget share, so a silent slot cannot starve the retry.
    """
    bound = min(
        _ATTEMPT_FIRST_BYTE_SECONDS,
        translation_attempt_timeout_seconds(chars),
    )
    if remaining_seconds is None:
        return bound
    return min(
        bound,
        max(0.0, float(remaining_seconds))
        * _FIRST_BYTE_REMAINING_BUDGET_SHARE,
    )


def translation_total_deadline_seconds(chars):
    """Return the wall budget for translating ``chars`` in one call.

    Two attempts' worth of the per-attempt bound (see
    :func:`translation_attempt_timeout_seconds`) plus the historical 90s floor,
    so the unavoidable first failure — a silent slot, a 429 rotation, an empty
    reply — still leaves a full retry inside the same budget. The engine's own
    600s default remains the ceiling for the largest documents.
    """
    attempt_timeout = translation_attempt_timeout_seconds(chars)
    return max(
        _TRANSLATION_TOTAL_DEADLINE_FLOOR_SECONDS,
        attempt_timeout * _TRANSLATION_TOTAL_DEADLINE_ATTEMPT_ALLOWANCE,
    )


def _translation_overgeneration_verdict(output, clen, model):
    """Keep the established threshold fault-injection seam at the engine."""
    return overgeneration_verdict(output, clen, model,
        ratio=_OVERGEN_RATIO, absolute=_OVERGEN_ABS, minimum_input=_OVERGEN_MIN_INPUT)


def _raise_unaccepted_translation(*, terminal_dispatch_error, abort_requested,
                                  deadline_expired, deadline_seconds, attempts,
                                  dispatched_attempts, dispatch_fails,
                                  content_fails, started_mono, started_ts,
                                  last_err, chunk_label):
    """Raise the error explaining why the retry loop accepted no candidate."""
    from lib.llm.llm_errors import AbortedError

    if terminal_dispatch_error is not None:
        raise terminal_dispatch_error
    if abort_requested():
        raise AbortedError('Translation aborted by owner')
    if deadline_expired:
        # Callers must be able to tell "never dispatched" and "cut by the
        # deadline" from an empty model reply; all three used to share the
        # ValueError below, which also projected as an internal error.
        raise TranslationDeadlineExhausted(
            deadline_seconds=deadline_seconds,
            dispatched_attempts=dispatched_attempts,
            dispatch_fails=dispatch_fails,
            content_fails=content_fails,
            elapsed_seconds=time.monotonic() - started_mono,
            last_err=last_err,
            chunk_label=chunk_label,
        )
    raise ValueError(
        f'Empty translation result for chunk{chunk_label} after '
        f'{attempts} attempts ({dispatch_fails} dispatch fails, '
        f'{content_fails} content fails, elapsed={time.time() - started_ts:.0f}s, '
        f'last_err={last_err})'
    )


@observed_engine
def _translate_one_chunk(chunk, system_prompt, chunk_label='',
                         source='', target='', status_cb=None,
                         progress_cb=None, overall_deadline=None,
                         use_cache=True, abort_check=None, prefer_model=None,
                         strict_model=False, allow_mt=True, stream=False,
                         capability='cheap', temperature=1,
                         accept_truncated=True, max_429_attempts=None,
                         defer_on_shared_contention=False, model_budget_admit=None):
    """Translate one chunk through bounded cache → MT → LLM fallbacks.

    ``status_cb`` receives retry state, ``progress_cb`` receives streamed LLM
    text, and ``abort_check`` is honored before dispatch and during backoff.
    Artifact producers disable ``accept_truncated`` so partial output cannot be
    published; reconstructible previews may additionally bound 429 attempts or
    yield to active shared contention.
    """
    _notify = _translation_status_notifier(status_cb, chunk_label)

    from lib.agent_core.execution_budget import ModelBudgetAdmissionDenied
    if model_budget_admit is not None and not callable(model_budget_admit):
        raise ValueError('model_budget_admit must be callable')
    from lib.llm.llm_errors import AbortedError

    def _abort_requested():
        return bool(abort_check and abort_check())

    if _abort_requested():
        raise AbortedError('Translation aborted before dispatch')

    fast_result = _try_translation_fast_paths(
        chunk,
        source=source,
        target=target,
        chunk_label=chunk_label,
        use_cache=use_cache,
        allow_mt=allow_mt,
        abort_requested=_abort_requested,
        notify=_notify,
    )
    if fast_result is not None:
        return fast_result

    from lib.llm_dispatch import (
        DispatchNoAdmissibleSlot,
        DispatchRateLimitBudgetExceeded,
        DispatchSharedContentionDeferred,
    )
    from lib.llm_dispatch.factory import get_dispatcher
    from lib.llm import BadRequestError, RateLimitError, RequestScopedError

    clen = len(chunk)
    _target_is_chinese = _targets_chinese(target)
    _mt, _timeout, _retries = _translation_chunk_attempt_limits(clen)
    # Bounds on ONE physical attempt. The dispatch timeout above only reaches
    # the non-streaming transport's read clock; the streaming transport had no
    # per-attempt bound at all, which is how one silent provider consumed the
    # whole task budget. Both bounds are always forwarded; the header bound is
    # recomputed per attempt against the remaining budget.
    _attempt_timeout = translation_attempt_timeout_seconds(clen)

    # ── Aggressive retry policy ──
    # Rationale: auto-translate is a background task.  A user waiting for
    # the "译文" toggle expects it to eventually succeed if ANY model/key
    # recovers.  So:
    #   • Rate-limit / dispatch failures → retry indefinitely with backoff
    #     (up to a generous wall-clock budget).
    #   • Empty / truncated output       → retry up to _MAX_CONTENT_RETRIES
    #     times (different model usually fixes it; after that the issue
    #     is more likely with the input itself).
    _OVERALL_DEADLINE_SEC = (
        max(0.0, float(overall_deadline))
        if overall_deadline is not None else 600.0)
    _BACKOFF_MIN = 1.0
    _BACKOFF_MAX = 30.0

    _start_ts = time.time()
    _deadline = _TranslationDeadline(
        _OVERALL_DEADLINE_SEC, _abort_requested)
    _start_mono = _deadline.started_at
    _deadline_exceeded = _deadline.exceeded
    _remaining_deadline_seconds = _deadline.remaining
    _sleep_backoff = _deadline.sleep

    _attempt = 0
    _content_fail_count = 0
    _dispatch_fail_count = 0
    _last_err = None
    _terminal_dispatch_error = None
    _terminal_verdict = 'ok'
    _dispatched_attempts = 0
    _deadline_expired = False
    # ``c`` keeps the last candidate even after a guard REJECTED it, so only
    # this flag — never a non-empty ``c`` — means a translation was accepted.
    _accepted = False
    c, u = '', None
    # ── Models proven bad for THIS chunk ──
    # When a model returns an empty or truncated body, we add it here so the
    # next dispatch_stream / smart_chat call skips it. Without this the slot
    # picker keeps choosing the same low-latency cheap model (e.g. MiniMax-M2.5)
    # and burns the whole _MAX_CONTENT_RETRIES budget on identical failures.
    _excluded_models: set[str] = set()
    _dispatcher = timed_call('route_resolution', get_dispatcher)

    if use_cache:
        _raise_cached_translation_refusal(
            chunk, source, target, chunk_label=chunk_label,
            max_content_retries=_MAX_CONTENT_RETRIES)

    # Emit a 'started' event up front so callers polling the status
    # side-channel see something within the first poll tick — without
    # this, a slow first attempt with no retries leaves the user staring
    # at a static "Translating…" with no indication anything is happening.
    _notify('started', 1, 0, f'starting translation ({len(chunk)} chars)')

    while True:
        _attempt += 1
        _elapsed = time.monotonic() - _start_mono
        if _abort_requested():
            # Owner cancellation, never a translation failure. ``_deadline
            # .exceeded()`` folds both into one True, so checking it first
            # reported a cancelled job as "deadline exhausted" at ERROR.
            _notify('aborted', _attempt, _elapsed,
                    'translation cancelled by owner')
            raise AbortedError('Translation aborted before dispatch')
        if _deadline_exceeded():
            _deadline_expired = True
            if _last_err is None:
                # Nothing was dispatched: the caller's budget was spent
                # before this chunk arrived.
                _last_err = 'overall deadline exhausted before dispatch'
            # With no failed dispatch the caller's shared budget was spent
            # before ours began: nothing here failed, so this is not an
            # incident. Boot sweep used to emit ~125 of these at ERROR.
            _report = logger.error if (
                _content_fail_count or _dispatch_fail_count) else logger.warning
            _report('[Translate%s] Giving up after %.0fs (attempts=%d, '
                    'content_fails=%d, dispatch_fails=%d, last_err=%s)',
                    chunk_label, _elapsed, _dispatched_attempts,
                    _content_fail_count, _dispatch_fail_count, _last_err)
            break

        try:
            _msgs = [{'role': 'system', 'content': system_prompt},
                     {'role': 'user', 'content': _wrap_for_translation(chunk)}]
            _dispatched_attempts += 1
            c, u = _dispatch_translation_candidate(
                _msgs,
                chunk_label=chunk_label,
                max_tokens=_mt,
                timeout=_timeout,
                max_retries=_retries,
                excluded_models=_excluded_models,
                progress_cb=progress_cb,
                deadline_exceeded=_deadline_exceeded,
                remaining_deadline_seconds=_remaining_deadline_seconds,
                prefer_model=prefer_model,
                strict_model=strict_model,
                stream=stream,
                capability=capability,
                temperature=temperature,
                max_429_attempts=max_429_attempts,
                defer_on_shared_contention=defer_on_shared_contention,
                # Never let ONE attempt outlive the remaining task budget:
                # dispatch would otherwise hold the slot past the deadline and
                # report the deadline as the failure instead of retrying.
                attempt_timeout=min(_attempt_timeout,
                                    max(1.0, _remaining_deadline_seconds())),
                first_byte_timeout=translation_first_byte_timeout_seconds(
                    clen, remaining_seconds=_remaining_deadline_seconds()),
                **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}),
            )
        except ModelBudgetAdmissionDenied:
            raise
        except AbortedError as deadline_abort:
            if _abort_requested():
                _notify('aborted', _attempt, _elapsed,
                        'translation cancelled by owner')
                raise
            _deadline_expired = True
            _last_err = f'{type(deadline_abort).__name__}: {deadline_abort}'
            _notify('deadline_exceeded', _attempt, _elapsed,
                    'translation deadline expired during dispatch')
            break
        except DispatchSharedContentionDeferred as contention_deferred:
            _dispatch_fail_count += 1
            _last_err = type(contention_deferred).__name__
            logger.info(
                '[Translate%s] Optional work yielded to active shared '
                'contention before transport',
                chunk_label,
            )
            _notify(
                'shared_contention_deferred',
                _attempt,
                _elapsed,
                'translation preview yielded to active model contention',
            )
            raise
        except DispatchNoAdmissibleSlot as admission_error:
            _dispatch_fail_count += 1
            _last_err = type(admission_error).__name__
            logger.warning(
                '[Translate%s] No billing-healthy slot is admissible; '
                'ending optional translation without outer backoff',
                chunk_label,
            )
            _notify(
                'no_admissible_slot',
                _attempt,
                _elapsed,
                'no billing-healthy translation slot is available',
            )
            raise TranslationNoAdmissibleProvider() from admission_error
        except DispatchRateLimitBudgetExceeded as budget_error:
            _dispatch_fail_count += 1
            _last_err = type(budget_error).__name__
            logger.warning(
                '[Translate%s] Rate-limit API budget exhausted after %d/%d '
                'upstream attempts; ending optional translation',
                chunk_label, budget_error.attempts, budget_error.limit,
            )
            _notify(
                'rate_limit_budget_exhausted',
                _attempt,
                _elapsed,
                f'upstream rate-limit budget exhausted '
                f'({budget_error.attempts}/{budget_error.limit})',
            )
            raise
        except (BadRequestError, RequestScopedError) as deterministic_error:
            # A read timeout is a TRANSPORT failure that the error taxonomy
            # explicitly marks retryable (``ResponseReadTimeoutError`` mixes in
            # ``RetryableAPIError``), and no payload is at fault. Treating it as
            # deterministic ended the translation after the first slow slot: the
            # whole document and every enrichment segment were dropped on a
            # single 14s read timeout. Route it to the bounded transient retry
            # instead, where a different slot can serve the same payload.
            from lib.llm.llm_errors import RetryableAPIError

            if isinstance(deterministic_error, RetryableAPIError):
                _dispatch_fail_count += 1
                _last_err = (
                    f'{type(deterministic_error).__name__}: '
                    f'{deterministic_error}'
                )
                if _deadline_exceeded():
                    _deadline_expired = True
                    _notify('deadline_exceeded', _attempt, _elapsed,
                            'translation deadline expired during dispatch')
                    break
                if _dispatch_fail_count >= _MAX_CONTENT_RETRIES + 1:
                    logger.error(
                        '[Translate%s] Too many transport failures (%d): %s',
                        chunk_label, _dispatch_fail_count, deterministic_error,
                        exc_info=True)
                    _terminal_dispatch_error = deterministic_error
                    _notify('dispatch_failed_final', _attempt, _elapsed,
                            str(deterministic_error)[:160])
                    break
                _sleep = min(
                    _BACKOFF_MAX,
                    _BACKOFF_MIN * (2 ** min(_dispatch_fail_count - 1, 5)),
                )
                logger.warning(
                    '[Translate%s] Retryable transport failure (attempt %d, '
                    'fails=%d): %s — sleeping %.1fs and retrying',
                    chunk_label, _attempt, _dispatch_fail_count,
                    deterministic_error, _sleep)
                _notify('dispatch_error', _attempt, _elapsed,
                        f'{type(deterministic_error).__name__}: '
                        f'{str(deterministic_error)[:120]}')
                _sleep_backoff(min(_sleep, _remaining_deadline_seconds()))
                continue

            # Dispatch already applied its bounded pair/model policy. Replaying
            # the identical payload in this outer loop cannot repair a 400,
            # 404, or 422 and previously multiplied one terminal rejection into
            # six full dispatch rounds plus exponential sleeps.
            _dispatch_fail_count += 1
            _last_err = (
                f'{type(deterministic_error).__name__}: '
                f'{deterministic_error}'
            )
            _terminal_dispatch_error = deterministic_error
            logger.warning(
                '[Translate%s] Deterministic dispatch rejection; ending '
                'translation without outer retry: %s',
                chunk_label,
                deterministic_error,
            )
            _notify(
                'dispatch_failed_final',
                _attempt,
                _elapsed,
                str(deterministic_error)[:160],
            )
            break
        except RateLimitError as re_err:
            # All keys temporarily rate-limited — wait and retry forever
            # (within the overall deadline).  This is the most common transient
            # failure and users expect the translation to eventually land.
            _dispatch_fail_count += 1
            _last_err = f'RateLimitError: {re_err}'
            if _deadline_exceeded():
                _deadline_expired = True
                _notify('deadline_exceeded', _attempt, _elapsed,
                        'translation deadline expired during dispatch')
                break
            _sleep = min(_BACKOFF_MAX, _BACKOFF_MIN * (2 ** min(_dispatch_fail_count - 1, 5)))
            logger.warning('[Translate%s] All keys rate-limited (attempt %d, '
                           'total_rl_fails=%d, elapsed=%.0fs) — sleeping %.1fs and retrying',
                           chunk_label, _attempt, _dispatch_fail_count, _elapsed, _sleep)
            _notify('rate_limited', _attempt, _elapsed,
                    f'all keys busy (fails={_dispatch_fail_count}, retry in {_sleep:.0f}s)')
            _sleep_backoff(min(_sleep, _remaining_deadline_seconds()))
            continue
        except Exception as se:
            # Other dispatch errors (network, timeout, bad payload, etc.) —
            # retry a few times but don't loop forever.
            if _deadline_exceeded():
                # The caller's own abort check stopped this attempt: a cut
                # stream surfaces as an unverified client-aborted result. That
                # is a deadline stop, not a failed dispatch.
                _deadline_expired = True
                _last_err = f'stopped at the caller deadline: {se}'
                logger.warning(
                    '[Translate%s] Dispatch stopped at the %.0fs caller '
                    'deadline: %s', chunk_label, _OVERALL_DEADLINE_SEC, se)
                _notify('deadline_exceeded', _attempt, _elapsed,
                        'translation deadline expired during dispatch')
                break
            _dispatch_fail_count += 1
            _last_err = f'dispatch error: {se}'
            if _dispatch_fail_count >= _MAX_CONTENT_RETRIES + 1:
                logger.error('[Translate%s] Too many dispatch failures (%d): %s',
                             chunk_label, _dispatch_fail_count, se, exc_info=True)
                _notify('dispatch_failed_final', _attempt, _elapsed, str(se)[:160])
                break
            _sleep = min(_BACKOFF_MAX, _BACKOFF_MIN * (2 ** min(_dispatch_fail_count - 1, 5)))
            logger.warning('[Translate%s] smart_chat raised (attempt %d, fails=%d): %s '
                           '— sleeping %.1fs and retrying',
                           chunk_label, _attempt, _dispatch_fail_count, se, _sleep)
            _notify('dispatch_error', _attempt, _elapsed,
                    f'{type(se).__name__}: {str(se)[:120]}')
            _sleep_backoff(min(_sleep, _remaining_deadline_seconds()))
            continue

        decision = assess_translation_candidate(
            c, u, chunk=chunk, source=source, target=target,
            chunk_label=chunk_label, clen=clen,
            target_is_chinese=_target_is_chinese, strict_model=strict_model,
            accept_truncated=accept_truncated, attempt=_attempt, elapsed=_elapsed,
            content_fails=_content_fail_count, last_error=_last_err,
            max_content_retries=_MAX_CONTENT_RETRIES,
            excluded_models=_excluded_models, dispatcher=_dispatcher,
            notify=_notify, logger=logger,
            overgeneration_verdict=_translation_overgeneration_verdict,
        )
        c = decision.text
        _content_fail_count = decision.content_fails
        _last_err = decision.last_error
        if decision.action == 'retry':
            continue
        if decision.action == 'accept':
            _accepted = True
            _terminal_verdict = decision.verdict
        break

    if not _accepted:
        _raise_unaccepted_translation(
            terminal_dispatch_error=_terminal_dispatch_error,
            abort_requested=_abort_requested, deadline_expired=_deadline_expired,
            deadline_seconds=_OVERALL_DEADLINE_SEC, attempts=_attempt,
            dispatched_attempts=_dispatched_attempts,
            dispatch_fails=_dispatch_fail_count, content_fails=_content_fail_count,
            started_mono=_start_mono, started_ts=_start_ts,
            last_err=_last_err, chunk_label=chunk_label)

    # ── Post-processing: detect and truncate repetition loops ──
    c, was_truncated = timed_call('postprocess', _dedup_repetition_loop, c)
    if was_truncated:
        logger.info('[Translate%s] Repetition loop cleaned: %d chars after dedup',
                    chunk_label, len(c))

    final = c.strip()
    _model_for_cache = ''
    if isinstance(u, dict):
        _disp = u.get('_dispatch', {}) or {}
        _model_for_cache = _disp.get('model', u.get('model', '')) or ''
    # Do NOT persist a KNOWN-incomplete translation: caching a truncated body
    # pins the partial forever (every later request served the same cut-off
    # text — the reported "displayed incompletely" bug). A fresh request then
    # re-translates and a healthier model can produce a complete result.
    if _terminal_verdict != 'truncated':
        timed_call('cache_store', translate_cache.put,chunk, source, target, final, model=_model_for_cache)
    else:
        logger.warning('[Translate%s] NOT caching truncated result '
                       '(%d/%d chars) so a later request can re-translate',
                       chunk_label, len(final), clen)

    # ── 溯源: full lifecycle trace attached to the usage dict + one
    #    structured, grep-able line. Flags suspiciously-short output even
    #    when it cleared the truncation floor, so silent partial drops stay
    #    visible after the fact. ──
    _ratio = len(final) / clen if clen else 0.0
    _soft_floor = 0.40 if _target_is_chinese else 0.30
    _suspicious = bool(clen > 500 and _ratio < _soft_floor
                       and _terminal_verdict == 'ok')
    _trace = _build_trace(
        path='llm', model=_model_for_cache or '?', in_chars=clen,
        out_chars=len(final), attempts=_attempt,
        content_fails=_content_fail_count, dispatch_fails=_dispatch_fail_count,
        elapsed=time.time() - _start_ts, verdict=_terminal_verdict,
        target=target)
    _trace['suspicious'] = _suspicious
    logger.info('[Translate:trace%s] verdict=%s%s path=llm model=%s in=%d out=%d '
                'ratio=%.2f attempts=%d cfail=%d dfail=%d elapsed=%.1fs',
                chunk_label, _terminal_verdict,
                ' SUSPICIOUS-SHORT' if _suspicious else '',
                _trace['model'], clen, len(final), _ratio, _attempt,
                _content_fail_count, _dispatch_fail_count, _trace['elapsed'])
    if isinstance(u, dict):
        u['_translate_trace'] = _trace

    return final, u

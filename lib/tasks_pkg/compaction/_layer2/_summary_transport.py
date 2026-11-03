"""Transport, completion proof and usage for one logical L2 summary.

Entry point: SummaryTransport.dispatch, called with the currently authorized
route. The entrypoint retains route selection, credential grant lifetime and
budget settlement. This per-call object only records observed provider work;
it adds no provider loop, queue, cache or durable state. Dispatch and typed
stream completion still use the existing shared owners.
"""
from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable

from lib.log import get_logger

# Preserve observers of the existing summary diagnostic stream.
logger = get_logger('lib.tasks_pkg.compaction._layer2._summary')


class IncompleteSummaryError(ValueError):
    """Observed provider output is not a verified normal completion."""


@dataclass(slots=True)
class SummaryTransport:
    """Retain transport observations across authorized credential re-selection."""
    messages: list
    conv_id: str
    affinity_id: str
    owner_user_id: int | None
    log_prefix: str
    abort_check: Callable | None
    on_delta: Callable | None
    usage_out: dict | None
    dispatch_chat: Callable
    dispatch_stream: Callable
    transport_completed: bool = field(default=False, init=False)
    observed_provider_work: bool = field(default=False, init=False)
    _call_rows: list = field(default_factory=list, init=False)

    def _dispatch_transport(self, call, *args, retry_allowed=None, **kwargs):
        from lib.tasks_pkg.llm_fallback._credential_recovery import call_with_credential_recovery

        def report_retry(attempt, cause):
            from lib.tasks_pkg.compaction._summary_failure import validate_summary_failure
            safe = validate_summary_failure({
                'code': 'summary_credentials_unavailable',
                'authIssue': cause.reason, 'credentialSource': cause.credential_source,
                'providerReached': False}) or {}
            logger.warning('%s Summary credential preparation retry attempt=%d source=%s issue=%s',
                           self.log_prefix, attempt, safe.get('credentialSource', 'unknown'),
                           safe.get('authIssue', 'unavailable'))

        return call_with_credential_recovery(
            lambda: call(*args, **kwargs), abort_check=self.abort_check,
            on_retry=report_retry,
            retry_allowed=lambda: not self.observed_provider_work and not self.transport_completed
                and (retry_allowed is None or retry_allowed()))

    def _record_usage(self, usage, summary_route):
        usage = dict(usage) if isinstance(usage, dict) else {}
        from lib.cost import merge_usage_totals
        from lib.tasks_pkg.compaction._summary_cost import price_summary_call
        metadata = usage.get('_dispatch') or {}
        model_id = (metadata.get('logical_model') or metadata.get('model')
                    or summary_route.get('modelId') or '')
        provider_id = metadata.get('provider_id') or summary_route.get('providerId')
        self._call_rows.append({'model': model_id, 'provider_id': provider_id,
                          'usage': dict(usage), 'cost': price_summary_call(
                              usage, model=model_id, provider_id=provider_id,
                              declared_pricing=summary_route.get('declaredPricing'))})
        if isinstance(self.usage_out, dict):
            merged = merge_usage_totals(self.usage_out, usage)
            self.usage_out.clear()
            self.usage_out.update(merged)
            self.usage_out['_summaryCalls'] = list(self._call_rows)
            self.usage_out['_summaryRoute'] = summary_route
        try:
            from lib.tasks_pkg.compaction._compaction_usage import record_compaction_usage
            record_compaction_usage(self.conv_id, usage, kind='L2')
        except Exception as exc:
            logger.debug('%s record_compaction_usage failed: %s', self.log_prefix, exc)

    def dispatch(
        self, capability: str, *, summary_route: dict, codex_provider: str,
        auxiliary_pin: str, dispatch_output_tokens: int, dispatch_options: dict,
    ):
        """Return ``(content, usage)`` for either path.

        With ``on_delta`` set we STREAM and forward each delta live; otherwise
        we use the non-streaming ``dispatch_chat`` (byte-identical to before).
        The streaming branch adopts content only after the typed provider
        result proves completion; a half-stream can never become the durable
        compaction summary. Both branches then yield ``(content, usage)``."""
        # Codex subscription slots are streaming-only.  A non-streaming
        # dispatch excludes them and may silently buy the summary from another
        # API provider.  Stream internally even when no UI delta callback was
        # requested, and hard-pin the picker to the subscription provider.
        # A live-task summary streams even without a UI delta callback: the
        # streaming transport's watchdog polls abort_check every 0.5s and
        # interrupts generation mid-flight, while non-streaming chat() is a
        # bare blocking http_post that can only honor abort between retries.
        use_stream = (self.on_delta is not None or bool(codex_provider)
                      or self.abort_check is not None or summary_route.get('requestOwned'))

        # Keep this auxiliary request out of the parent conversation's sticky
        # routing + cache-settle namespace.  The context manager restores the
        # worker thread's parent affinity even when dispatch raises.  Streaming
        # also carries the same identity in the body so Codex's session headers
        # (and prompt_cache_key on profiles that emit it) are stable; dispatching
        # a bare message list would otherwise create a random subscription
        # session.
        from lib.llm_dispatch.conv_affinity import conv_affinity
        affinity_scope = conv_affinity(self.affinity_id)
        from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin
        selected_provider = auxiliary_pin or summary_route.get('providerId') or codex_provider
        existing_pin = get_pinned_provider()
        if selected_provider and existing_pin and selected_provider != existing_pin:
            raise RuntimeError('summary_provider_pin_conflict')
        pin = provider_pin(selected_provider) if selected_provider else nullcontext()
        # An owner-wide auxiliary route is optional work: it never challenges a
        # recorded billing stop. The task's own route keeps attended admission.
        from lib.key_stats import strict_billing_stop_admission
        admission = (strict_billing_stop_admission() if summary_route.get('requestOwned')
                     else nullcontext())
        with affinity_scope, pin, admission:
            if not use_stream:
                chat_content, chat_usage = self._dispatch_transport(self.dispatch_chat,
                    self.messages,
                    max_tokens=dispatch_output_tokens, temperature=0,
                    capability=capability, log_prefix=self.log_prefix,
                    owner_user_id=self.owner_user_id, abort_check=self.abort_check,
                    **dispatch_options)
                self.transport_completed = True
                self._record_usage(chat_usage, summary_route)

                finish = str((chat_usage or {}).get('finish_reason') or '').lower()
                if finish not in {'stop', 'end_turn', 'stop_sequence', 'completed'}:
                    raise IncompleteSummaryError('summary_not_complete:' + (finish or 'missing_terminal'))
                return chat_content, chat_usage

            summary_body = {
                'messages': self.messages,
                'max_tokens': dispatch_output_tokens,
                'temperature': 0,
                'stream': True,
            }
            from lib.llm.reasoning import capture_intent
            summary_body['_reasoning_intent'] = capture_intent(
                thinking_enabled=False, temperature=0)
            dispatch_started = time.monotonic()
            first_text_ms = None
            if self.affinity_id:
                summary_body['_conv_id'] = self.affinity_id

            def _on_thinking(chunk):
                if chunk:
                    self.observed_provider_work = True

            def _on_attempt_restart(**_):
                # A discarded transport/dispatcher attempt is not an unsent
                # logical summary, even if it produced no visible content.
                self.observed_provider_work = True

            def _on_provider_attempt(*, kind, **fields):
                if (kind == 'complete'
                        and fields.get('errorKind') != 'LocalRequestPreparationError'):
                    self.observed_provider_work = True

            def _on_content(chunk: str):
                nonlocal first_text_ms
                if not chunk:
                    return
                self.observed_provider_work = True
                if first_text_ms is None:
                    first_text_ms = (time.monotonic() - dispatch_started) * 1000
                if self.on_delta is None:
                    return
                try:
                    self.on_delta(chunk)
                except Exception as _cb_e:
                    # Best-effort live UI — a push failure must never abort the
                    # summary generation (the DB rewrite is the source of truth).
                    logger.debug('%s on_delta callback failed: %s', self.log_prefix, _cb_e)

            from lib.llm.stream_result import (
                ensure_provider_stream_result, require_verified_provider_stream_result,
            )
            raw_result = self._dispatch_transport(self.dispatch_stream,
                summary_body,
                retry_allowed=lambda: first_text_ms is None,
                on_content=_on_content, on_thinking=_on_thinking,
                on_attempt_restart=_on_attempt_restart,
                on_provider_attempt=_on_provider_attempt,
                max_tokens=dispatch_output_tokens, temperature=0,
                capability=capability, log_prefix=self.log_prefix,
                owner_user_id=self.owner_user_id, abort_check=self.abort_check,
                **dispatch_options)
            self.transport_completed = True
            stream_result = ensure_provider_stream_result(raw_result)
            # Account observed usage even when completion cannot be adopted.
            observed_usage = getattr(stream_result, 'usage', {})
            self._record_usage(observed_usage, summary_route)
            observed_message = getattr(stream_result, 'message', {})
            metadata = observed_usage.get('_dispatch', {}) if isinstance(observed_usage, dict) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            thinking = observed_message.get('reasoning_content', '') if isinstance(observed_message, dict) else ''
            logger.info('%s Summary attempt model=%s provider=%s thinking_chars=%d first_text_ms=%s',
                        self.log_prefix, metadata.get('model', ''), metadata.get('provider_id', ''),
                        len(thinking) if isinstance(thinking, str) else 0, first_text_ms)
            stream_result = require_verified_provider_stream_result(
                stream_result, context='L2 compaction summary')

            finish_reason = (stream_result.provider_finish_reason
                             or stream_result.compatibility_finish_reason)
            if finish_reason not in {'stop', 'end_turn', 'stop_sequence', 'completed'}:
                raise IncompleteSummaryError('summary_not_complete:' + str(finish_reason))
            msg = stream_result.message
            usage = stream_result.usage
            # dispatch_stream returns the assistant message as a dict
            # ({'role': 'assistant', 'content': '...'}), NOT a bare string like
            # dispatch_chat — unwrap it so the shared post-processing (re.sub /
            # .strip below) receives a str. Same canonical unwrap as
            # lib/translate/engine/_engine.py.
            content = msg.get('content', '') if isinstance(msg, dict) else msg
            return content, usage

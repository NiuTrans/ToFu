"""Assess one translation candidate and apply its quality feedback.

Entry: assess_translation_candidate. Dependencies: pure quality checks, the
shared dispatcher feedback port and refusal-marker store. This owner never
dispatches or sleeps; the engine retains retries, deadlines and positive-cache
commit. The injected over-generation check preserves engine threshold probes.
"""
from dataclasses import dataclass
import re
from typing import Literal

from lib import translate_refusal
from lib.llm.diagnostic_timing import timed_stage
from lib.translate.errors import TranslationContentRefused
from ._quality_checks import _strip_model_thinking, translation_candidate_verdict


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    action: Literal['retry', 'accept', 'exhausted']
    text: str
    content_fails: int
    last_error: str | None
    verdict: str = 'ok'


def _normalize_content(text, *, chunk_label, logger):
    """Accept provider text blocks; preserve literal non-leading think tags."""
    if isinstance(text, list):
        try:
            text = ''.join(
                b.get('text', '') for b in text
                if isinstance(b, dict) and b.get('type') == 'text'
            )
        except Exception as _ce:
            logger.warning('[Translate%s] Failed to flatten list content: %s',
                           chunk_label, _ce)
            text = ''
    elif isinstance(text, dict):
        text = text.get('text', '') or text.get('content', '') or ''
    if not isinstance(text, str):
        text = str(text) if text is not None else ''

    # Strip leading model-reasoning wrappers only. Literal <think>
    # mentions later in the translation are document content.
    text = _strip_model_thinking(text)
    text = re.sub(r'</?translate>', '', text).strip()

    return text


def _audit_quality(event, *, chunk_label, model, clen, text,
                   content_fails, reason, target, logger):
    """Leave the existing audit trail without replacing the quality result."""
    try:
        from lib.log import audit_log
        audit_log(event,
                  chunk_label=chunk_label, model=model,
                  in_chars=clen, out_chars=len(text),
                  ratio=round(len(text) / max(1, clen), 3),
                  content_fails=content_fails,
                  reason=reason, target=target)
    except Exception as _ae:
        logger.debug('[Translate%s] audit_log failed: %s', chunk_label, _ae)


@timed_stage('quality_validation')
def assess_translation_candidate(
        text, usage, *, chunk, source, target, chunk_label, clen,
        target_is_chinese, strict_model, accept_truncated,
        attempt, elapsed, content_fails, last_error,
        max_content_retries, excluded_models, dispatcher,
        notify, logger, overgeneration_verdict):
    """Assess once; return retry state or raise a typed terminal refusal."""
    text = _normalize_content(text, chunk_label=chunk_label, logger=logger)
    used_key = ''
    model = ''
    if isinstance(usage, dict):
        dispatch = usage.get('_dispatch') or {}
        used_key = dispatch.get('key', '')
        model = dispatch.get('model', usage.get('model', ''))

    if not text:
        verdict, reason = 'empty', f'empty result (len={len(chunk)})'
    else:
        verdict, reason = translation_candidate_verdict(
            text, chunk.strip(), clen=clen, target=target,
            target_is_chinese=target_is_chinese,
            finish=(usage or {}).get('finish_reason', ''), model=model,
            overgeneration_verdict=overgeneration_verdict,
        )
    if verdict == 'ok':
        return CandidateDecision('accept', text, content_fails, last_error)

    # Every content failure consumes the same budget and penalizes the same
    # slot. Explicit model selection prevents rotation, but keeps feedback.
    content_fails += 1
    last_error = reason
    if used_key and model:
        dispatcher.record_truncation(used_key, model,
            error='empty translation output' if verdict == 'empty' else reason)
        if not strict_model:
            excluded_models.add(model)

    if verdict == 'empty':
        if content_fails >= max_content_retries:
            logger.error('[Translate%s] Still empty after %d content retries — giving up',
                         chunk_label, content_fails)
            notify('empty_final', attempt, elapsed,
                    f'empty after {content_fails} retries')
            return CandidateDecision('exhausted', text, content_fails, last_error)
        logger.warning('[Translate%s] Empty translation from %s (attempt %d, '
                       'content_fails=%d) — excluding model and retrying',
                       chunk_label, model or '?', attempt, content_fails)
        notify('empty_output', attempt, elapsed,
                f'empty result, retrying (fails={content_fails})')
        text = ''
        return CandidateDecision('retry', text, content_fails, last_error)

    if verdict == 'truncated':
        if content_fails >= max_content_retries:
            action = 'accepting' if accept_truncated else 'rejecting'
            logger.warning('[Translate%s] Still truncated after %d content retries: %s '
                           '— %s partial result (%d chars)',
                           chunk_label, content_fails, reason,
                           action, len(text))
            notify('truncated_final', attempt, elapsed,
                    f'truncated after {content_fails} retries')
            # 溯源: committing a KNOWN-incomplete translation is a silent
            # failure unless we leave a loud, machine-parseable trail.
            _audit_quality('translate_truncated_accept',
                chunk_label=chunk_label, model=model, clen=clen, text=text,
                content_fails=content_fails, reason=reason,
                target=target, logger=logger)
            if not accept_truncated:
                raise TranslationContentRefused(
                    'truncated', reason,
                    attempts=attempt,
                    content_fails=content_fails,
                )
            return CandidateDecision('accept', text, content_fails, last_error, 'truncated')
        logger.warning('[Translate%s] Truncated translation (attempt %d, content_fails=%d): %s '
                       '— excluding model and retrying',
                       chunk_label, attempt, content_fails, reason)
        notify('truncated', attempt, elapsed,
                f'output truncated, retrying (fails={content_fails})')
        return CandidateDecision('retry', text, content_fails, last_error)

    if verdict == 'noop':
        if content_fails >= max_content_retries:
            logger.error('[Translate%s] Still no-op after %d content retries: %s '
                         '— giving up, will surface error',
                         chunk_label, content_fails, reason)
            notify('noop_final', attempt, elapsed,
                    f'no-op output after {content_fails} retries')
        else:
            logger.warning('[Translate%s] No-op translation (attempt %d, content_fails=%d): %s '
                           '— excluding model and retrying',
                           chunk_label, attempt, content_fails, reason)
            notify('noop_output', attempt, elapsed,
                    f'output identical to input, retrying (fails={content_fails})')

    elif verdict == 'wrong_language':
        if content_fails >= max_content_retries:
            logger.error('[Translate%s] Still wrong-language after %d content retries: %s '
                         '— giving up, will surface error',
                         chunk_label, content_fails, reason)
            notify('wrong_language_final', attempt, elapsed,
                    f'output flipped language after {content_fails} retries')
        else:
            logger.warning('[Translate%s] Wrong-language flip (attempt %d, content_fails=%d): %s '
                           '— excluding model and retrying',
                           chunk_label, attempt, content_fails, reason)
            notify('wrong_language', attempt, elapsed,
                    f'output flipped to wrong language, retrying (fails={content_fails})')

    elif verdict == 'over_generated':
        if content_fails >= max_content_retries:
            logger.error('[Translate%s] Still over-generated after %d content retries: %s '
                         '— giving up, will surface error',
                         chunk_label, content_fails, reason)
            notify('over_generated_final', attempt, elapsed,
                    f'over-generated after {content_fails} retries')
            # 溯源: symmetric to translate_truncated_accept — leave a loud,
            # machine-parseable trail for the rejected contamination.
            _audit_quality('translate_over_generated_reject',
                chunk_label=chunk_label, model=model, clen=clen, text=text,
                content_fails=content_fails, reason=reason,
                target=target, logger=logger)
        else:
            logger.warning('[Translate%s] Over-generated translation (attempt %d, content_fails=%d): %s '
                           '— excluding model and retrying',
                           chunk_label, attempt, content_fails, reason)
            notify('over_generated', attempt, elapsed,
                    f'output over-generated, retrying (fails={content_fails})')

    if content_fails >= max_content_retries:
        # This required marker prevents repeated paid requests for refused
        # content. A store failure must not masquerade as a cached refusal.
        translate_refusal.put(chunk, source, target, verdict=verdict,
                              reason=reason, model=model, content_fails=content_fails)
        raise TranslationContentRefused(verdict, reason,
            attempts=attempt, content_fails=content_fails)
    return CandidateDecision('retry', text, content_fails, last_error)

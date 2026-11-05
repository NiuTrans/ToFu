"""Settle an accepted attempt's deferred input translation at worker entry.

Responsibility: before a conversation task's first model request, join the
attempt-start translation, record its verdict on the input Turn and substitute
the translated authored text into the task's model view. Entry point:
``apply_deferred_input_translation``. The translation itself starts with the
accepted attempt (``lib.chat.turn_builder.start_deferred_input_translation``)
and never writes; this worker is the verdict's only writer, so the input Turn
is final once the call returns. Nothing here raises into the worker lifecycle.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import wait
from typing import Any, NamedTuple

from lib.log import get_logger

logger = get_logger(__name__)

# The producer is bounded by the 45s send-translation budget plus two Turn
# reads; this join bound only protects the worker from a lost producer.
_JOIN_TIMEOUT_SECONDS = 60.0
_JOIN_POLL_SECONDS = 0.25
# Mirrors the model-view composition in conv_message_builder._transform: the
# authored text has its notranslate tags stripped, quotes/references precede
# it and inline document blocks follow this separator.
_NOTRANSLATE_TAGS = re.compile(r'</?(?:notranslate|nt)>', re.IGNORECASE)
_DOCUMENT_SEPARATOR = '\n\n' + '═' * 50


class SettledInput(NamedTuple):
    """The accepted input Turn revision a worker's model view reflects.

    Both fields are ``None`` when the verdict could not establish it (Stop, a
    stalled or failed translator, an unconfirmed write); the attempt writes
    nothing to the input Turn afterwards in any case.
    """

    turn_id: str | None
    projection_revision: int | None


_UNSETTLED = SettledInput(None, None)


def apply_deferred_input_translation(task: dict[str, Any]) -> SettledInput | None:
    """Return ``None`` when the attempt owed no deferred input translation."""
    future = task.pop('_deferredInputTranslation', None)
    if future is None:
        return None
    task_label = str(task.get('id') or '?')[:8]
    try:
        outcome, revision = _join(task, future, task_label)
        if outcome is None:
            return _UNSETTLED
        if outcome.translated is not None and not _substitute(task, outcome):
            logger.warning(
                '[Task %s] translated input not found in the model view; '
                'the model sees the authored text', task_label)
        if revision is None:
            return _UNSETTLED
        return SettledInput(outcome.input_turn_id, revision)
    except Exception as exc:
        logger.warning('[Task %s] deferred input translation unavailable: %s',
                       task_label, exc, exc_info=True)
        return _UNSETTLED


def _aborted(task: dict[str, Any]) -> bool:
    abort_event = task.get('abort_event')
    return bool(task.get('aborted')
                or (abort_event is not None and abort_event.is_set()))


def _join(task, future, task_label):
    from lib.observability import measure_task_startup

    identity = {
        'task_id': task.get('id'), 'conversation_id': task.get('convId'),
        'turn_id': task.get('_turnId'), 'attempt_id': task.get('_attemptId'),
        'user_id': task.get('_userId'),
    }
    with measure_task_startup('input_translation', identity=identity):
        outcome = _wait_for_translation(task, future, task_label)
        if outcome is None:
            return None, None
        return outcome, _record_verdict(task, outcome, task_label)


def _record_verdict(task, outcome, task_label):
    from lib.chat.turn_builder import record_deferred_input_translation

    try:
        return record_deferred_input_translation(
            task.get('convId'), outcome, user_id=task.get('_userId'))
    except Exception as exc:
        logger.warning('[Task %s] input translation verdict not recorded: %s',
                       task_label, exc, exc_info=True)
        return None


def _wait_for_translation(task, future, task_label):
    if not future.done():
        from lib.agent_core.events import Phase, emit_phase
        emit_phase(task, Phase.WORKING, detail='Translating your message…',
                   detailKey='translationWait.turn')
    started = time.monotonic()
    while not future.done():
        if _aborted(task):
            return None
        if time.monotonic() - started >= _JOIN_TIMEOUT_SECONDS:
            logger.warning('[Task %s] deferred input translation still running '
                           'after %.0fs; continuing with the authored text and '
                           'recording no verdict', task_label, _JOIN_TIMEOUT_SECONDS)
            return None
        wait([future], timeout=_JOIN_POLL_SECONDS)
    waited = time.monotonic() - started
    if waited >= 0.05:
        logger.info('[Task %s] joined deferred input translation after %.2fs',
                    task_label, waited)
    return future.result()


def _model_text(text: str) -> str:
    if '<notranslate>' in text or '<nt>' in text:
        return _NOTRANSLATE_TAGS.sub('', text)
    return text


def _authored_span(text: str, authored: str) -> tuple[int, int] | None:
    """Locate authored text after any quote/reference prefix, before documents."""
    cut = text.find(_DOCUMENT_SEPARATOR)
    end = len(text) if cut < 0 else cut
    start = end - len(authored)
    if (not authored or start < 0 or text[start:end] != authored
            or (start and not text[:start].endswith('\n\n'))):
        return None
    return start, end


def _rewrite(text: str, original: str, translated: str) -> str | None:
    span = _authored_span(text, original)
    if span is not None:
        return text[:span[0]] + translated + text[span[1]:]
    if _authored_span(text, translated) is not None:
        # The committed translation was already read into this model view.
        return text
    return None


def _substitute(task: dict[str, Any], outcome) -> bool:
    original = _model_text(outcome.original)
    translated = _model_text(outcome.translated)
    messages = task.get('messages') or []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            replaced = _rewrite(content, original, translated)
            if replaced is None:
                return False
            message['content'] = replaced
            task['lastUserQuery'] = replaced[:500]
            return True
        if isinstance(content, list):
            for block in reversed(content):
                if not isinstance(block, dict) or block.get('type') != 'text':
                    continue
                replaced = _rewrite(
                    str(block.get('text') or ''), original, translated)
                if replaced is not None:
                    block['text'] = replaced
                    task['lastUserQuery'] = ' '.join(
                        str(item.get('text') or '') for item in content
                        if isinstance(item, dict) and item.get('type') == 'text'
                    )[:500]
                    return True
        return False
    return False


def model_view_carries(task: dict[str, Any], text: Any) -> bool:
    """Whether the latest user message of the model view holds ``text`` as its authored span."""
    if not isinstance(text, str) or not text:
        return False
    authored = _model_text(text)
    for message in reversed(task.get('messages') or []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return _authored_span(content, authored) is not None
        if isinstance(content, list):
            return any(
                isinstance(block, dict) and block.get('type') == 'text'
                and _authored_span(str(block.get('text') or ''), authored) is not None
                for block in content)
        return False
    return False


__all__ = ['apply_deferred_input_translation', 'model_view_carries']

"""Streaming SSE generator for the paper Q&A endpoint.

Reuses ``dispatch_stream`` for retry handling and rate-limit rotation.
Yields SSE-formatted lines including a final ``data: [DONE]\\n\\n``.
"""

import json
import queue
import threading

from lib.llm_dispatch.api import dispatch_stream
from lib.log import get_logger

logger = get_logger(__name__)


def _stream_llm_sse(messages, model=None, max_tokens=128000, temperature=0):
    """Streaming SSE generator for paper Q&A / translate.

    Reuses dispatch_stream for retry handling and rate-limit rotation.
    Yields SSE-formatted lines including a final ``data: [DONE]\\n\\n``.

    ``max_tokens`` defaults to a very large ceiling (128k) so responses run
    to completion without artificial truncation.  ``_clamp_max_tokens()`` in
    ``build_body`` automatically reduces this to each model's native API
    limit, so the effective value is "as much as the model allows."
    """
    q = queue.Queue()
    _sentinel = object()

    def _worker():
        try:
            def _on_content(text):
                q.put(text)

            dispatch_stream(
                messages,
                on_content=_on_content,
                max_tokens=max_tokens,
                temperature=temperature,
                prefer_model=model or None,
                strict_model=bool(model),
                log_prefix='[Paper:Chat]',
            )
        except Exception as e:
            logger.error('[Paper:Chat] Stream failed: %s', e, exc_info=True)
            q.put(('__error__', str(e)))
        finally:
            q.put(_sentinel)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    while True:
        item = q.get()
        if item is _sentinel:
            break
        if isinstance(item, tuple) and item[0] == '__error__':
            yield f'data: {json.dumps({"error": item[1]})}\n\n'
            break
        yield f'data: {json.dumps({"choices": [{"delta": {"content": item}}]})}\n\n'

    yield 'data: [DONE]\n\n'

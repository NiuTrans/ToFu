"""Server-side auto-translate for endpoint-mode critic review messages.

``_maybe_auto_translate_critic`` is a thin role-flavoured wrapper around the
core ``_maybe_auto_translate_assistant`` safety net. Endpoint-mode critic
output is stored as ``role='user'`` with ``_isEndpointReview=true`` but the
underlying safety net commits by index regardless of role, so we reuse it
directly and only override the log prefix + source-lang hint for observability.
"""

from lib.log import get_logger

from lib.tasks_pkg.auto_translate._assistant import _maybe_auto_translate_assistant

logger = get_logger(__name__)


def _maybe_auto_translate_critic(conv_id, content, msg_idx, db=None):
    """Server-side auto-translate for endpoint-mode critic review messages.

    Endpoint-mode critic output is authored by the Critic LLM (English by
    default, sometimes mixed) and is stored as ``role='user'`` with
    ``_isEndpointReview=true`` in the conversation's ``messages`` list.  The
    existing ``_maybe_auto_translate_assistant`` safety-net commits to
    ``messages[msg_idx]`` by index regardless of role, so we reuse it
    directly and only override the log prefix + source-lang hint for
    observability.

    This path is only invoked from
    ``endpoint._trigger_endpoint_auto_translate``.  The per-conv
    ``autoTranslate`` gate, dedup against running frontend translate tasks,
    and stale-partial re-translation logic are inherited verbatim.
    """
    pfx = '[AutoTranslate:Critic]'
    if not conv_id or not content:
        logger.debug('%s conv=%s msg=%s — empty conv/content; skipping',
                     pfx, conv_id[:8] if conv_id else '?', msg_idx)
        return
    # Delegate to the shared helper — it is role-agnostic at the commit
    # layer (writes to messages[msg_idx]).  We only log the role flavour
    # here so operators can distinguish critic translations in the log.
    logger.info('%s conv=%s msg=%d content=%dchars — delegating to '
                '_maybe_auto_translate_assistant safety net',
                pfx, conv_id[:8], msg_idx, len(content))
    _maybe_auto_translate_assistant(conv_id, content, msg_idx, db)

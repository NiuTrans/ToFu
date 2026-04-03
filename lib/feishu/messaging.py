"""lib/feishu/messaging.py — Send messages to Feishu via Lark API.

Handles text splitting (Feishu 4KB limit), reply vs direct send,
and Lark client singleton initialization.
"""

import json
import logging

from lib.feishu._state import (
    APP_ID,
    APP_SECRET,
    FEISHU_MSG_LIMIT,
    _lark_client,
    _lark_client_lock,
)

logger = logging.getLogger(__name__)

__all__ = ['split_message', 'send_text']


def _get_lark_client():
    """Lazy-initialize the Lark API client singleton (thread-safe)."""
    global _lark_client
    # Module-level global is in _state, but we write to it here
    import lib.feishu._state as _st
    if _st._lark_client is None:
        with _lark_client_lock:
            if _st._lark_client is None:
                import lark_oapi as lark
                _st._lark_client = lark.Client.builder() \
                    .app_id(APP_ID) \
                    .app_secret(APP_SECRET) \
                    .log_level(lark.LogLevel.WARNING) \
                    .build()
    return _st._lark_client


def split_message(text: str, limit: int = FEISHU_MSG_LIMIT) -> list:
    """Split text into chunks that fit Feishu's message size limit.

    Tries to split on newlines first, then spaces, then hard-cuts.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind('\n', 0, limit)
        if cut <= 0:
            cut = text.rfind(' ', 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip('\n')
    return chunks


def send_text(message_id: str | None, text: str,
              chat_id: str = None, open_id: str = None) -> None:
    """Send a text message to Feishu.

    Priority: reply to message_id > send to chat_id > send to open_id.
    Long messages are automatically split into multiple sends.
    """
    if not text:
        return

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )

    client = _get_lark_client()
    chunks = split_message(text)

    for i, chunk in enumerate(chunks):
        content = json.dumps({'text': chunk})

        try:
            if message_id and i == 0:
                # Reply to the original message (first chunk only)
                req = ReplyMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type('text')
                        .content(content)
                        .build()
                    ).build()
                resp = client.im.v1.message.reply(req)
            elif chat_id:
                req = CreateMessageRequest.builder() \
                    .receive_id_type('chat_id') \
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type('text')
                        .content(content)
                        .build()
                    ).build()
                resp = client.im.v1.message.create(req)
            elif open_id:
                req = CreateMessageRequest.builder() \
                    .receive_id_type('open_id') \
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(open_id)
                        .msg_type('text')
                        .content(content)
                        .build()
                    ).build()
                resp = client.im.v1.message.create(req)
            else:
                logger.warning('[FeishuBot] send_text: no target (message_id, chat_id, open_id)')
                return

            if not resp.success():
                logger.warning(
                    '[FeishuBot] Send failed: code=%s msg=%s',
                    resp.code, resp.msg,
                )
        except Exception as e:
            logger.error(
                '[FeishuBot] Send exception (chunk %d/%d): %s',
                i + 1, len(chunks), e, exc_info=True,
            )

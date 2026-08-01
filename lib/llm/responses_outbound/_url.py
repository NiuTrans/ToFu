"""lib/llm/responses_outbound/_url.py — Responses API endpoint URL."""

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['responses_url']


def responses_url(base_url: str) -> str:
    """Return the Responses API endpoint for a provider base URL.

    ``https://api.deepseek.com/v1`` → ``https://api.deepseek.com/v1/responses``
    (trailing slash tolerant, mirroring ``anthropic_messages_url``).
    """
    return f'{(base_url or "").rstrip("/")}/responses'

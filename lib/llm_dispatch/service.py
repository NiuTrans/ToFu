"""Default :class:`lib.protocols.LLMService` backed by dispatch.

Consumers depend on the protocol and call ``resolve_llm_service(llm)`` instead
of dual-pathing ``llm.chat`` against ``smart_chat``.  This module must not
import dispatcher state, provider discovery, or HTTP transports at import
time; ``chat`` / ``stream`` resolve those lazily.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.protocols import LLMService

__all__ = [
    'DispatchLLMService',
    'resolve_llm_service',
]


class DispatchLLMService:
    """Production :class:`~lib.protocols.LLMService` using dispatch facades."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: float = 0,
        thinking_enabled: bool = False,
        capability: str = 'text',
        prefer_model: str | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 3,
        log_prefix: str = '',
        timeout: float | None = None,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any]]:
        from lib import llm_dispatch

        model = prefer_model if prefer_model is not None else kwargs.pop('model', None)
        return llm_dispatch.smart_chat(
            messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking_enabled=thinking_enabled,
            capability=capability,
            tools=tools,
            max_retries=max_retries,
            log_prefix=log_prefix,
            timeout=timeout,
            **kwargs,
        )

    def stream(
        self,
        body: dict[str, Any] | list[dict[str, Any]],
        *,
        on_content: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        abort_check: Callable[[], bool] | None = None,
        capability: str = 'text',
        prefer_model: str | None = None,
        log_prefix: str = '',
        **kwargs: Any,
    ):
        from lib import llm_dispatch
        from lib.llm.stream_result import ensure_provider_stream_result

        return ensure_provider_stream_result(
            llm_dispatch.dispatch_stream(
                body,
                on_content=on_content,
                on_thinking=on_thinking,
                abort_check=abort_check,
                capability=capability,
                prefer_model=prefer_model,
                log_prefix=log_prefix,
                **kwargs,
            )
        )


_DEFAULT: DispatchLLMService | None = None


def resolve_llm_service(llm: LLMService | None = None) -> LLMService:
    """Return ``llm`` or the process-wide dispatch-backed default.

    Importing this function does not initialize dispatch.  The default adapter
    is constructed on first use and stays stateless.
    """
    if llm is not None:
        return llm
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = DispatchLLMService()
    return _DEFAULT

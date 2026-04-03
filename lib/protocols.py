"""lib/protocols.py — Protocol interfaces for module boundary decoupling.

Defines ``typing.Protocol`` classes at the highest-coupling boundaries
in the codebase so that modules depend on abstract interfaces rather than
concrete implementations.

Key boundaries addressed:
  • **LLMService** — used by trading_autopilot, swarm, tasks_pkg, trading.intel
    instead of importing ``lib.llm_dispatch`` / ``lib.llm_client`` directly.

  • **FetchService** — used by tasks_pkg.executor, trading.intel instead of
    importing ``lib.fetch`` directly.

  • **TradingDataProvider** — used by trading_autopilot instead of importing
    ``lib.trading`` directly.

  • **TaskEventSink** — used by tasks_pkg.executor, tool_dispatch, etc.
    instead of importing ``lib.tasks_pkg.manager`` directly.

  • **ToolHandler** — standard callable protocol for tool dispatch entries.

  • **BodyBuilder** — callable protocol for LLM request body construction.

Usage::

    # In a consumer module:
    from lib.protocols import LLMService

    def my_function(llm: LLMService, prompt: str) -> str:
        content, usage = llm.chat(
            [{'role': 'user', 'content': prompt}],
            max_tokens=1024,
        )
        return content

    # The concrete implementation (lib.llm_dispatch) satisfies this
    # protocol without inheriting from it — structural subtyping.

These protocols are intended for:
  1. Type annotations at function/class boundaries
  2. Enabling testability (mock objects that satisfy the protocol)
  3. Documenting the expected interface without creating hard imports

They are NOT intended to be instantiated directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

__all__ = [
    'LLMService',
    'FetchService',
    'TradingDataProvider',
    'TaskEventSink',
    'ToolHandler',
    'BodyBuilder',
]


# ═══════════════════════════════════════════════════════════
#  LLM Service Protocol
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class LLMService(Protocol):
    """Protocol for LLM chat completion services.

    Satisfied by:
      - ``lib.llm_dispatch`` module-level functions (dispatch_chat, dispatch_stream, smart_chat)
      - Any adapter wrapping a different LLM provider
      - Test mocks

    The two core methods mirror the dispatch API:
      - ``chat()`` for non-streaming (returns content + usage)
      - ``stream()`` for streaming (returns msg + finish_reason + usage)
    """

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
    ) -> tuple[str, dict[str, Any]]:
        """Non-streaming chat completion.

        Returns:
            (content_text, usage_dict) where usage_dict contains at minimum
            ``prompt_tokens``, ``completion_tokens``, ``total_tokens``.
        """
        ...

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
    ) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
        """Streaming chat completion.

        Returns:
            (message_dict, finish_reason, usage_dict)
        """
        ...


# ═══════════════════════════════════════════════════════════
#  Fetch Service Protocol
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class FetchService(Protocol):
    """Protocol for web content fetching.

    Satisfied by:
      - ``lib.fetch`` module-level functions (fetch_page_content, fetch_urls)
      - Any adapter wrapping a custom HTTP/browser fetch layer
      - Test mocks that return pre-canned page content

    Decouples consumers (executor, trading.intel crawlers) from the concrete
    fetch implementation (HTTP + Playwright + PDF extraction).
    """

    def fetch_page_content(
        self,
        url: str,
        max_chars: int = 80000,
        pdf_max_chars: int = 160000,
        timeout: int = 15,
    ) -> str | None:
        """Fetch and extract text content from a URL.

        Handles HTML, PDF, and JS-rendered pages transparently.

        Args:
            url: The URL to fetch.
            max_chars: Maximum character limit for HTML content.
            pdf_max_chars: Maximum character limit for PDF content.
            timeout: Request timeout in seconds.

        Returns:
            Extracted text content, or None if fetch failed.
        """
        ...

    def fetch_urls(
        self,
        urls: list[str],
        max_chars: int = 160000,
        pdf_max_chars: int = 160000,
        timeout: int = 15,
    ) -> dict[str, str]:
        """Fetch multiple URLs in parallel.

        Args:
            urls: List of URLs to fetch.
            max_chars: Maximum character limit per page.
            pdf_max_chars: Maximum character limit for PDF content.
            timeout: Per-request timeout in seconds.

        Returns:
            Dict mapping URL → extracted text for successful fetches.
        """
        ...


# ═══════════════════════════════════════════════════════════
#  Fund Data Provider Protocol
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class TradingDataProvider(Protocol):
    """Protocol for trading data access — NAV, info, intel.

    Satisfied by ``lib.trading`` module-level functions.
    Used by trading_autopilot to decouple the autopilot orchestration
    from the concrete trading data implementation.
    """

    def get_latest_price(self, symbol: str) -> tuple[float | None, str]:
        """Get the latest price for an asset.

        Returns:
            (nav_value_or_None, nav_date_string)
        """
        ...

    def fetch_asset_info(self, symbol: str) -> dict[str, Any] | None:
        """Fetch basic asset information.

        Returns:
            Dict with keys like 'name', 'type', 'nav', 'nav_date', etc.
            or None if not found.
        """
        ...

    def fetch_price_history(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch price history for an asset.

        Returns:
            List of {'date': str, 'nav': float} dicts sorted by date ascending.
        """
        ...

    def build_intel_context(
        self,
        db: Any,
    ) -> tuple[str, int]:
        """Build intelligence context for autopilot.

        Returns:
            (intel_context_string, intel_item_count)
        """
        ...


# ═══════════════════════════════════════════════════════════
#  Task Event Sink Protocol
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class TaskEventSink(Protocol):
    """Protocol for emitting task lifecycle events (SSE stream).

    Satisfied by ``lib.tasks_pkg.manager.append_event`` and friends.
    Decouples tool execution (executor, tool_dispatch) from the
    concrete task-state storage.

    Wired into callsites:
      - ``tool_dispatch.emit_tool_exec_phase()`` accepts ``event_sink: TaskEventSink``
      - ``executor._prefetch_user_urls()`` accepts ``fetch_service: FetchService``
        (same DI pattern for a different protocol)
    """

    def append_event(
        self,
        task: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        """Append an event to the task's event stream.

        Args:
            task: The live task dict (mutated in place).
            event: Event dict with at minimum a ``'type'`` key
                   (e.g. ``{'type': 'tool_start', 'roundNum': 1, ...}``).
                   Stored in ``task['events']`` and used for SSE streaming.
        """
        ...


# ═══════════════════════════════════════════════════════════
#  Tool Handler Protocol
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class ToolHandler(Protocol):
    """Protocol for individual tool execution handlers.

    Each handler in the ToolRegistry must satisfy this signature.
    Used by ``lib.tasks_pkg.executor.ToolRegistry`` dispatch.

    The signature matches the actual handler registration pattern::

        @tool_registry.handler('web_search', category='search', ...)
        def _handle_web_search(task, tc, fn_name, tc_id, fn_args, rn,
                               round_entry, cfg, project_path,
                               project_enabled, all_tools=None):
            ...
            return tc_id, tool_content, is_search
    """

    def __call__(
        self,
        task: dict[str, Any],
        tc: dict[str, Any],
        fn_name: str,
        tc_id: str,
        fn_args: dict[str, Any],
        rn: int,
        round_entry: dict[str, Any],
        cfg: dict[str, Any],
        project_path: str | None,
        project_enabled: bool,
        all_tools: list[dict] | None = None,
    ) -> tuple[str, str, bool]:
        """Execute a tool and return results.

        Args:
            task: The live task dict.
            tc: The tool-call dict from the LLM response.
            fn_name: Tool function name.
            tc_id: Tool call ID from the LLM response.
            fn_args: Parsed tool arguments.
            rn: Round number for the search-round display.
            round_entry: Display round entry dict (mutated with badge/metadata).
            cfg: Task config dict.
            project_path: Active project path or None.
            project_enabled: Whether project-mode is active.
            all_tools: Full tool list (passed to handlers that need it).

        Returns:
            (tc_id, tool_content_str, is_search_result) tuple.
        """
        ...


# ═══════════════════════════════════════════════════════════
#  Body Builder Protocol
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class BodyBuilder(Protocol):
    """Protocol for LLM request body construction.

    Satisfied by ``lib.llm_client.build_body``.
    Decouples swarm agents and orchestrators from the concrete
    model-aware body construction logic.
    """

    def __call__(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 128000,
        temperature: float = 1.0,
        thinking_enabled: bool = False,
        tools: list[dict] | None = None,
        stream: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build a model-aware request body for /chat/completions.

        Returns:
            Request body dict ready for the API.
        """
        ...

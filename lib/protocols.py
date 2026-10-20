"""lib/protocols.py — Protocol interfaces for module boundary decoupling.

Defines ``typing.Protocol`` classes at the highest-coupling boundaries
in the codebase so that modules depend on abstract interfaces rather than
concrete implementations.

Key boundaries addressed:
  • **LLMService** — used by trading_autopilot, swarm, tasks_pkg, trading.intel
    instead of importing ``lib.llm_dispatch`` / ``lib.llm`` directly.

  • **FetchService** — used by tasks_pkg.executor, trading.intel instead of
    importing ``tofu_search.fetch`` directly.

  • **TaskEventSink** — used by tasks_pkg.executor, tool_dispatch, etc.
    instead of importing ``lib.tasks_pkg.manager`` directly.

  • **ToolHandler** — standard callable protocol for tool dispatch entries.

  • **BodyBuilder** — callable protocol for LLM request body construction.

Usage::

    # In a consumer module:
    from lib.protocols import LLMService, resolve_llm_service

    def my_function(llm: LLMService | None, prompt: str) -> str:
        llm = resolve_llm_service(llm)
        content, usage = llm.chat(
            [{'role': 'user', 'content': prompt}],
            max_tokens=1024,
        )
        return content

    # Production default: lib.llm_dispatch.service.DispatchLLMService.
    # Test doubles satisfy the protocol structurally — no inheritance.

These protocols are intended for:
  1. Type annotations at function/class boundaries
  2. Enabling testability (mock objects that satisfy the protocol)
  3. Documenting the expected interface without creating hard imports

They are NOT intended to be instantiated directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from lib.llm.stream_result import ProviderStreamResult

__all__ = [
    'LLMService',
    'resolve_llm_service',
    'FetchService',
    'resolve_fetch_service',
    'TaskEventSink',
    'ConversationStore',
    'ToolHandler',
    'BodyBuilder',
    'resolve_body_builder',
]


# ═══════════════════════════════════════════════════════════
#  LLM Service Protocol
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class LLMService(Protocol):
    """Protocol for LLM chat completion services.

    Satisfied by:
      - ``lib.llm_dispatch.service.DispatchLLMService`` (production default)
      - Any adapter wrapping a different LLM provider
      - Test mocks

    The two core methods mirror the dispatch API:
      - ``chat()`` for non-streaming (returns content + usage)
      - ``stream()`` for streaming (returns a ``ProviderStreamResult`` whose
        state carries the provider's terminal evidence)
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
    ) -> ProviderStreamResult:
        """Streaming chat completion.

        Returns:
            One typed provider attempt.  Legacy tuple-unpacking remains
            available on the result only as a compatibility seam; callers
            must use ``state`` / ``is_verified_complete`` for semantics.
        """
        ...


def resolve_llm_service(llm: LLMService | None = None) -> LLMService:
    """Return ``llm`` or the production dispatch-backed default.

    The default adapter is imported lazily so ``import lib.protocols`` does
    not initialize dispatcher state or HTTP transports.
    """
    if llm is not None:
        return llm
    from lib.llm_dispatch.service import resolve_llm_service as _resolve
    return _resolve(None)


# ═══════════════════════════════════════════════════════════
#  Fetch Service Protocol
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class FetchService(Protocol):
    """Protocol for web content fetching.

    Satisfied by:
      - ``tofu_search.fetch`` module-level functions (fetch_page_content, fetch_urls)
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

    def get_publish_date_from_url(
        self,
        url: str,
        timeout: int = 10,
    ) -> str | None:
        """Return the page's declared publication date, if discoverable.

        Used by crawlers that rank time-sensitive content; returns None when
        no trustworthy date signal exists.
        """
        ...


def resolve_fetch_service(fetch: FetchService | None = None) -> FetchService:
    """Return ``fetch`` or the production default (``tofu_search.fetch``).

    The default adapter is imported lazily so ``import lib.protocols`` does
    not initialize the HTTP/browser fetch stack; it is only available when
    the host installed the ``full`` capability extra.
    """
    if fetch is not None:
        return fetch
    import tofu_search.fetch as _fetch_module
    return _fetch_module  # type: ignore[return-value]


# ═══════════════════════════════════════════════════════════


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
#  Conversation Store Protocol
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class ConversationStore(Protocol):
    """Owner-scoped persistence boundary for the reusable agent core.

    The transcript body is a projection of normalized turns. Implementations
    expose only semantic turn mutations; a whole-transcript overwrite is not
    part of this protocol.
    """

    def call_with_transient_retry(
        self,
        fn,
        *,
        abort_event=None,
        label: str = 'storage operation',
    ):
        """Run one persistence operation with bounded transient-stall retry.

        The host owns the backoff schedule; core callers name the semantic
        operation, never the retry mechanics.
        """
        ...

    def next_task_event_id(self, task_id: str, *, floor: int = 0) -> int:
        """Return an unused durable task-event sequence at or above ``floor``."""
        ...

    def inject_project_messages(self, task: dict[str, Any], messages: list[dict[str, Any]], round_num: int) -> None:
        """Prepare owner-scoped project input; construction does not acknowledge it."""
        ...

    def confirm_project_messages(self, task: dict[str, Any], round_num: int) -> None:
        """Confirm only verified project messages using their existing delivery debt."""
        ...

    def confirm_project_context_consumption(self, task: dict[str, Any]) -> bool:
        """Revalidate task/coordinator binding before confirming consumed narrative."""
        ...

    def confirm_project_context_delivery(self, delivery: dict[str, Any]) -> bool:
        """Advance one owner-scoped Project Context delivery cursor."""
        ...

    def load_transcript(
        self, conv_id: str, *, user_id: Any,
    ) -> tuple[list, int, int] | None:
        """Return ``(derived_messages, updated_at_ms, revision)`` or ``None``."""
        ...

    def manual_compaction_rejection(
        self, conv_id: str, *, user_id: Any,
        messages: Sequence[Mapping[str, Any]] = (),
    ) -> str | None:
        """Return an owned rejection reason, or None to allow preparation.

        Read this store's authority; unknown reads propagate as errors. The
        supplied messages are its loaded projection, not request metadata.
        Missing support refuses manual compaction as turn_protocol_unsupported.
        """
        ...

    def compact_turn_transcript(
        self, conv_id: str, current_messages: list, compacted_messages: list,
        expected_revision: int, *, command_id: str, user_id: Any,
    ) -> int:
        """Recheck admission, then compact atomically; return 1 or 0 on CAS loss.

        Raise ManualCompactionRefused when policy changed during preparation.
        """
        ...

    def archive_transcript(
        self, conv_id: str, messages: list, *, user_id: Any,
        summary: str = '', trigger: str = 'force', task_id: str = '',
        round_num: int = 0, model: str = '', tokens_before: int = 0,
        tokens_after: int = 0, msgs_before: int = 0, msgs_after: int = 0,
        reason: str = '', receipt: dict | None = None, archive_scope: str = '',
    ) -> str | None:
        """Create an owner-scoped pre-compaction archive."""
        ...

    def list_compaction_archives(
        self, conv_id: str, *, user_id: Any, limit: int = 200, archive_scope: str = "",
    ) -> list[dict]:
        """List compact archive metadata without loading transcript payloads."""
        ...

    def get_compaction_archive(
        self, conv_id: str, archive_id: str, *, user_id: Any,
        include_messages: bool = True, include_post_messages: bool = False,
        archive_scope: str = "",
    ) -> dict | None:
        """Load owner-scoped archive metadata and optionally its transcript."""
        ...

    def update_archive_summary(
        self, archive_id: str, summary: str, tokens_after: int,
        msgs_after: int, *, user_id: Any, receipt: dict | None = None,
        post_messages: list | None = None, archive_scope: str = "",
    ) -> Any:
        """Set final summary, result receipt, and post-compaction counts."""
        ...

    def delete_archives(self, conv_id: str, *, user_id: Any, archive_scope: str = "") -> Any:
        """Delete every archive owned by one conversation."""
        ...

    def prune_archives(
        self, conv_id: str, keep: int, *, user_id: Any, archive_scope: str = "",
    ) -> int:
        """Keep the newest ``keep`` archives and return the deletion count."""
        ...

    def notify_conversation_changed(self, conv_id: str, *, user_id: Any) -> None:
        """Emit an owner-scoped wake hint carrying the current revision."""
        ...


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

    Satisfied by ``lib.llm.build_body``.
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
        thinking_enabled: bool | None = None,
        inference_profile: dict[str, Any] | None = None,
        reasoning_intent: dict[str, Any] | None = None,
        defer_reasoning: bool = False,
        tools: list[dict] | None = None,
        stream: bool = True,
        precomputed_input_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build a model-aware request body for /chat/completions.

        Returns:
            Canonical request body; deferred reasoning requires selected-slot
            compilation before the transport boundary.
        """
        ...


def resolve_body_builder(builder: BodyBuilder | None = None) -> BodyBuilder:
    """Return ``builder`` or the production default (``lib.llm.build_body``).

    The default is imported lazily so ``import lib.protocols`` does not
    initialize the model-routing stack.
    """
    if builder is not None:
        return builder
    from lib.llm import build_body as _build_body
    return _build_body

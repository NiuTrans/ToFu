"""lib/agent_backends/protocol.py — Core abstractions for multi-backend agent architecture.

Defines the ``AgentBackend`` abstract base class, ``NormalizedEvent`` dataclass,
and ``BackendCapabilities`` that together form the contract between our Flask
routes and the various coding-agent backends (built-in Tofu, Claude Code CLI,
Codex CLI, etc.).

Design inspired by Craft Agent's BaseAgent / AgentEvent / BACKEND_CAPABILITIES
pattern — but adapted for our subprocess-based Python architecture.

The key flow::

    Frontend  →  /api/chat/start  →  BackendRouter
                                        │
                        ┌───────────────┼───────────────┐
                        ▼               ▼               ▼
                  BuiltinBackend  ClaudeCodeBackend  CodexBackend
                        │               │               │
                   NormalizedEvent  NormalizedEvent  NormalizedEvent
                        │               │               │
                        └───────────────┼───────────────┘
                                        ▼
                                  sse_bridge.py
                                  (→ our SSE protocol)
                                        ▼
                                    Frontend

Each backend:
  1. Receives a user message + project context
  2. Yields ``NormalizedEvent`` instances as work progresses
  3. ``sse_bridge.normalized_to_sse()`` translates each to our frontend SSE format

External backends (Claude Code, Codex) are "pure frontend" — they use the
CLI's own auth, model selection, tool execution, and context management.
We just spawn the subprocess and normalize its JSONL output.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

__all__ = [
    'AgentBackend',
    'BackendCapabilities',
    'NormalizedEvent',
    'NormalizedEventKind',
]


# ═══════════════════════════════════════════════════════════
#  Backend Capabilities
# ═══════════════════════════════════════════════════════════

@dataclass
class BackendCapabilities:
    """Declares what a backend supports — drives UI adaptation.

    The frontend reads these to show/hide/grey-out controls.
    When a capability is False, the corresponding UI element is
    hidden (for config controls) or greyed out (for feature toggles).

    Example: Claude Code backend has ``model_selector=False`` because
    the CLI handles its own model selection.
    """

    # ── Core streaming / session features ──
    streaming: bool = True
    multi_turn: bool = True
    abort: bool = True

    # ── Tool capabilities the backend provides natively ──
    has_web_search: bool = True
    has_file_tools: bool = True
    has_code_exec: bool = True

    # ── Tofu-only features — unavailable in external backends ──
    has_image_gen: bool = False
    has_browser_ext: bool = False
    has_desktop_agent: bool = False
    has_swarm: bool = False
    has_scheduler: bool = False
    has_conv_ref: bool = False
    has_human_guidance: bool = False

    # ── UI configuration controls ──
    model_selector: bool = True       # Can user choose model?
    thinking_depth: bool = True       # Can user set thinking level?
    search_toggle: bool = True        # Can user toggle search?
    project_selector: bool = True     # Can user select project path?
    preset_selector: bool = True      # Can user select presets?
    temperature_control: bool = True  # Can user set temperature?
    endpoint_mode: bool = False       # Supports plan→work→review loop?

    # ── Permission model ──
    approval_system: str = 'none'     # 'none', 'tool-level', 'mode-based'

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON API responses."""
        return {
            'streaming': self.streaming,
            'multiTurn': self.multi_turn,
            'abort': self.abort,
            'hasWebSearch': self.has_web_search,
            'hasFileTools': self.has_file_tools,
            'hasCodeExec': self.has_code_exec,
            'hasImageGen': self.has_image_gen,
            'hasBrowserExt': self.has_browser_ext,
            'hasDesktopAgent': self.has_desktop_agent,
            'hasSwarm': self.has_swarm,
            'hasScheduler': self.has_scheduler,
            'hasConvRef': self.has_conv_ref,
            'hasHumanGuidance': self.has_human_guidance,
            'modelSelector': self.model_selector,
            'thinkingDepth': self.thinking_depth,
            'searchToggle': self.search_toggle,
            'projectSelector': self.project_selector,
            'presetSelector': self.preset_selector,
            'temperatureControl': self.temperature_control,
            'endpointMode': self.endpoint_mode,
            'approvalSystem': self.approval_system,
        }


# ═══════════════════════════════════════════════════════════
#  Normalized Event
# ═══════════════════════════════════════════════════════════

class NormalizedEventKind:
    """Event kind constants for NormalizedEvent.kind."""

    TEXT_DELTA = 'text_delta'
    THINKING_DELTA = 'thinking_delta'
    TOOL_START = 'tool_start'
    TOOL_OUTPUT = 'tool_output'        # Streaming output during execution
    TOOL_COMPLETE = 'tool_complete'    # Final result
    FILE_CHANGE = 'file_change'
    PHASE = 'phase'                    # Status / progress updates
    APPROVAL_REQUEST = 'approval_request'
    DONE = 'done'
    ERROR = 'error'


@dataclass
class NormalizedEvent:
    """Unified event produced by any backend, ready for SSE translation.

    This is the intermediate representation between backend-specific
    event formats (Claude Code stream-json, Codex exec --json, or our
    own internal events) and our frontend SSE protocol.

    Each backend's normalizer produces these; ``sse_bridge.normalized_to_sse()``
    translates them to the frontend event dicts.
    """

    kind: str  # One of NormalizedEventKind constants

    # ── Phase hint (for PHASE events) ──
    phase_type: str = ''  # Frontend phase: 'working', 'retrying', 'tool_exec', etc.

    # ── Text content ──
    text: str = ''

    # ── Tool events ──
    tool_name: str = ''
    tool_id: str = ''
    tool_input: dict = field(default_factory=dict)
    tool_output: str = ''
    tool_is_error: bool = False

    # ── File changes ──
    file_path: str = ''
    file_action: str = ''  # 'create', 'modify', 'delete'

    # ── Completion ──
    finish_reason: str = ''   # 'stop', 'error', 'aborted', 'max_turns'
    error_message: str = ''
    usage: dict = field(default_factory=dict)

    # ── Session (for multi-turn) ──
    session_id: str = ''


# ═══════════════════════════════════════════════════════════
#  Agent Backend ABC
# ═══════════════════════════════════════════════════════════

class AgentBackend(ABC):
    """Abstract base for all agent backends.

    Subclasses:
      - ``BuiltinBackend`` — wraps existing Tofu orchestrator (all features)
      - ``ClaudeCodeBackend`` — spawns ``claude`` subprocess (pure frontend)
      - ``CodexBackend`` — spawns ``codex`` subprocess (pure frontend)

    External backends are "pure frontend" — our system does NOT inject
    system prompts, configure API keys, select models, or manage context
    for them.  The CLI handles everything; we just normalize the output.

    Lifecycle::

        backend = get_backend('claude-code')
        if not backend.is_available():
            return error("CLI not installed")
        if not backend.is_authenticated():
            return error("Not logged in")

        for event in backend.start_turn(task, user_message, project_path=path):
            sse_event = normalized_to_sse(event)
            append_event(task, sse_event)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique backend identifier (e.g. 'builtin', 'claude-code', 'codex')."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for UI display."""
        ...

    @abstractmethod
    def get_capabilities(self) -> BackendCapabilities:
        """Return this backend's capability declaration."""
        ...

    @abstractmethod
    def start_turn(
        self,
        task: dict[str, Any],
        user_message: str,
        *,
        images: list[dict] | None = None,
        project_path: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[NormalizedEvent]:
        """Start a new turn and yield normalized events until completion.

        This is a BLOCKING generator.  For the built-in backend it wraps
        our threaded run_task; for CLI backends it runs a subprocess and
        parses JSONL line-by-line.

        Args:
            task: The live task dict (for abort checking, metadata storage).
            user_message: The user's message text.
            images: Optional list of image dicts (base64/url).
            project_path: Working directory for file operations.
            session_id: Backend's native session ID for multi-turn resume.

        Yields:
            NormalizedEvent instances.
        """
        ...

    @abstractmethod
    def abort(self, task_id: str) -> bool:
        """Abort an active turn.  Returns True if successfully aborted."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is installed and ready to use."""
        ...

    @abstractmethod
    def is_authenticated(self) -> bool:
        """Check if this backend has valid credentials / auth."""
        ...

    def get_version(self) -> str | None:
        """Get backend version string, or None if unknown."""
        return None

    def get_session_id(self, conv_id: str) -> str | None:
        """Get the backend's native session ID for a conversation, if any."""
        return None

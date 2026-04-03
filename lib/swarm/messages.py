"""lib/swarm/messages.py — Inter-agent communication message envelope.

Extracted from protocol.py for modularity.

Contains:
  • AgentMessage — message passed between agents
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════
#  AgentMessage — inter-agent communication
# ═══════════════════════════════════════════════════════════

@dataclass
class AgentMessage:
    """A message passed between agents.

    Used for the master to send instructions/context to sub-agents,
    and for sub-agents to report back.  Also usable as a lightweight
    chat-style message with ``role`` + ``content``.

    Supports both old-style (from_agent/to_agent) and new-style
    (sender_id/receiver_id) field names for backward compatibility.
    """
    content: str = ''
    role: str = ''                               # 'user', 'assistant', 'system'
    sender_id: str = ''
    receiver_id: str = ''
    from_agent: str = ''                         # Alias for sender_id (backward compat)
    to_agent: str = ''                           # Alias for receiver_id (backward compat)
    msg_type: str = 'text'                       # 'text', 'tool_result', 'status', 'result', 'instruction', 'query'
    metadata: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)  # File paths, data blobs, etc.
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """Serialize to a plain dict."""
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

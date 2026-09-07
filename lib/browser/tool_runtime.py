"""Request-scoped authority for one browser tool execution.

Handlers receive this object explicitly. It is the only place that binds a
tool invocation to its authenticated repository owner and extension device;
there is no process-global or thread-local routing fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from lib.log import get_logger

from .queue import send_browser_command

logger = get_logger(__name__)


BrowserCommandSender = Callable[..., tuple[object, object]]


@dataclass(frozen=True)
class BrowserToolRuntime:
    owner_user_id: str
    client_id: str
    sender: BrowserCommandSender = field(
        default=send_browser_command,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        owner = str(self.owner_user_id or '').strip()
        client = str(self.client_id or '').strip()
        if not owner.isdigit() or int(owner) < 1:
            raise ValueError('owner_user_id must be a positive integer')
        if not client:
            raise ValueError('client_id is required')
        object.__setattr__(self, 'owner_user_id', owner)
        object.__setattr__(self, 'client_id', client)

    @property
    def route_key(self) -> tuple[str, str]:
        return self.owner_user_id, self.client_id

    def send(self, command, params=None, timeout=30):
        """Send one command with this runtime's immutable authority route."""
        result, error = self.sender(
            command,
            params,
            timeout=timeout,
            client_id=self.client_id,
            owner_user_id=self.owner_user_id,
        )
        if command == 'list_tabs' and not error and isinstance(result, list):
            # Feed the display-side title cache so later tool-round labels can
            # name the page instead of the opaque tab id. Purely advisory.
            try:
                from .tab_titles import ingest_tab_list
                ingest_tab_list(self.owner_user_id, self.client_id, result)
            except Exception as e:
                logger.debug('tab-title ingest failed: %s', e)
        return result, error


__all__ = ['BrowserCommandSender', 'BrowserToolRuntime']

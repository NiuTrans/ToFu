"""Owner-bound repository for the durable desktop egress-agent preference.

The Storage Sidecar is authoritative.  ``oauth_egress_agents.json`` is read at
most once per owner that has no Sidecar row, then an explicit row (including an
empty selection) permanently marks migration complete.  The compatibility
file is never written or treated as a fallback after that marker exists.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import uuid
from typing import Any

from lib.identity import require_user_id
from lib.json_store import JsonStoreReadError, read_json
from lib.log import get_logger


logger = get_logger(__name__)
_MAX_AGENT_ID_CHARS = 128
_MAX_LEGACY_STORE_BYTES = 1024 * 1024


def _validated_agent_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("agent_id must be a string")
    if len(value) > _MAX_AGENT_ID_CHARS:
        raise ValueError(
            f"agent_id must be at most {_MAX_AGENT_ID_CHARS} characters")
    return value


def _legacy_agent_id(owner_user_id: int) -> str:
    """Read the retired JSON authority without hiding corruption.

    A damaged legacy file is left untouched and treated as an unavailable
    migration source.  The Sidecar marker is still initialized to empty so a
    broken compatibility artifact cannot remain on the request hot path.
    """
    from lib.config_dir import config_path

    path = config_path("oauth_egress_agents.json")
    try:
        document = read_json(
            path,
            default=None,
            strict=True,
            max_bytes=_MAX_LEGACY_STORE_BYTES,
        )
    except JsonStoreReadError as exc:
        logger.warning(
            "[EgressPreference] legacy pin import skipped: %s", exc)
        return ""
    if document is None:
        return ""
    if not isinstance(document, Mapping):
        logger.warning(
            "[EgressPreference] legacy pin store is not an object: %s", path)
        return ""
    candidate = document.get(str(owner_user_id), "")
    try:
        return _validated_agent_id(candidate)
    except ValueError:
        logger.warning(
            "[EgressPreference] ignored invalid legacy pin for owner %s",
            owner_user_id,
        )
        return ""


class EgressAgentPreferenceRepository:
    """Keep owner injection, migration, and Sidecar operation names together."""

    def __init__(
        self,
        owner_user_id: int,
        *,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.owner_user_id = require_user_id(
            owner_user_id, context="desktop egress preference owner")
        self._client_factory = client_factory

    def _client(self, *, write: bool = False):
        if self._client_factory is not None:
            return self._client_factory(write=write)
        from lib.storage import get_storage_client

        return get_storage_client(write=write)

    @staticmethod
    def _result_agent_id(result: object) -> str:
        if not isinstance(result, Mapping):
            raise RuntimeError("desktop egress preference returned an invalid result")
        return _validated_agent_id(result.get("agent_id"))

    def pinned_agent(self) -> str:
        payload = {"owner_user_id": self.owner_user_id}
        result = self._client().query("desktop.egress_agent.get", payload)
        if isinstance(result, Mapping) and result.get("present") is True:
            return self._result_agent_id(result)

        legacy_agent_id = _legacy_agent_id(self.owner_user_id)
        command_id = (
            "desktop.egress_agent.initialize:"
            f"{self.owner_user_id}:{uuid.uuid4().hex}"
        )
        initialized = self._client(write=True).command(
            "desktop.egress_agent.initialize",
            {**payload, "agent_id": legacy_agent_id},
            command_id,
        )
        return self._result_agent_id(initialized)

    def set_pinned_agent(self, agent_id: str) -> str:
        normalized = _validated_agent_id(agent_id)
        result = self._client(write=True).command(
            "desktop.egress_agent.set",
            {
                "owner_user_id": self.owner_user_id,
                "agent_id": normalized,
            },
            (
                "desktop.egress_agent.set:"
                f"{self.owner_user_id}:{uuid.uuid4().hex}"
            ),
        )
        return self._result_agent_id(result)


__all__ = ["EgressAgentPreferenceRepository"]

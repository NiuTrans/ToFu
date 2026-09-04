"""Plugin-owned structural interfaces for optional trading data providers.

The host supplies generic LLM protocols. Trading market-data behavior belongs
to this distribution, so its dependency-injection contract is defined here
instead of extending or importing a private host interface.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ['TradingDataProvider']


@runtime_checkable
class TradingDataProvider(Protocol):
    """Market and intelligence operations used by autopilot orchestration."""

    def get_latest_price(self, code: str) -> Any:
        """Return the latest market price payload for one asset."""
        ...

    def fetch_asset_info(self, code: str) -> dict[str, Any]:
        """Return descriptive and classification metadata for one asset."""
        ...

    def fetch_price_history(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """Return an ordered price history for one asset and date range."""
        ...

    def build_intel_context(self, db: Any) -> tuple[str, int]:
        """Build the intelligence prompt section and return its item count."""
        ...

"""Public Python SDK exports; usable independently of the Tofu server.

Tofu owns a synchronous Requests session; AsyncTofu uses HTTPX. Protocol,
transport, streaming and domain workflows have package-local source owners.
Use ``with Tofu(...)`` or ``async with AsyncTofu(...)`` to retire owned pools.
"""
from ._version import __version__
from ._protocol import TofuError
from ._client import Tofu
from .async_client import AsyncTofu, TofuAsync
from .api_v4_generated import ApiMetaResponse

__all__ = ['ApiMetaResponse', 'AsyncTofu', 'Tofu', 'TofuAsync', 'TofuError', '__version__']

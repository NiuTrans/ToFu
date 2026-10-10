"""Loopback-only passthrough benchmark proxy for the pinned OpenCode arm.

OpenCode speaks OpenAI-compatible chat completions natively, so unlike the
Codex arm this proxy performs no wire translation.  It enforces the pinned
model, requires the per-trial correlation header, attaches the host-owned
upstream credential, and records credential-free usage/timing metadata.
"""

from .server import OpenCodeBenchmarkProxy, ProxyConfig, serve
from .supervisor import OpenCodeProxySupervisor

__all__ = [
    "OpenCodeBenchmarkProxy",
    "OpenCodeProxySupervisor",
    "ProxyConfig",
    "serve",
]

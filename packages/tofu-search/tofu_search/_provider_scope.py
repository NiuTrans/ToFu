"""Provider registration, captured views and their exact operation lifetime.

ProviderSlot owns one optional capability. Public composition captures all
slots before binding any of them; activation restores nested scopes on every
exit. ProviderBinding identity is also a weak-referenceable lifetime key for
reconstructible session state, independent of a provider's equality or slots.
No host identity, transport, cache policy or credentials are inferred here.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock

_UNBOUND = object()


@dataclass(frozen=True, eq=False)
class ProviderBinding:
    provider: object


class ProviderSlot:
    def __init__(self, name: str):
        self._lock = Lock()
        self._registered = None
        self._context = ContextVar(name, default=_UNBOUND)

    def register(self, provider):
        binding = ProviderBinding(provider) if provider is not None else None
        with self._lock:
            self._registered = binding

    def snapshot(self):
        scoped = self._context.get()
        if scoped is not _UNBOUND:
            return scoped, True
        with self._lock:
            return self._registered, False

    def current_binding(self):
        return self.snapshot()[0]

    def get(self):
        binding = self.current_binding()
        return binding.provider if binding is not None else None

    @contextmanager
    def activate(self, snapshot):
        binding, already_bound = snapshot
        if binding is not None and not already_bound:
            # Auth binding is an optional extension for older duck-typed
            # providers. Their existing static view remains the default.
            bind = getattr(binding.provider, 'bind', None)
            provider = bind() if bind is not None else binding.provider
            if provider is not binding.provider:
                binding = ProviderBinding(provider) if provider is not None else None
        token = self._context.set(binding)
        try:
            yield binding
        finally:
            self._context.reset(token)


browser_scope = ProviderSlot('tofu_search_browser_provider')
site_search_scope = ProviderSlot('tofu_search_site_search_provider')
auth_source_scope = ProviderSlot('tofu_search_auth_source_provider')

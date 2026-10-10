"""Public Tofu-DB transport API; independent of the Tofu application."""
__all__ = [
    "StorageV2ConnectionClosedError",
    "StorageV2Error",
    "StorageV2ProtocolError",
    "StorageV2Response",
    "StorageV2Session",
]


def __getattr__(name):
    # Host-local lifecycle imports the stdlib-only _loopback boundary without
    # loading wire schemas or optional accelerators. Public wire API stays lazy.
    if name not in __all__:
        raise AttributeError(name)
    from . import wire
    value = getattr(wire, name)
    globals()[name] = value
    return value

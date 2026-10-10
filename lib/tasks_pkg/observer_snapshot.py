"""Detached built-in containers for read-only observers and context providers.

Strings are shared. Futures/locks keep their explicit runtime identity; mutable
JSON containers never alias live transcript, configuration or result buffers.
"""
from __future__ import annotations


def observer_snapshot(value, memo=None):
    memo = {} if memo is None else memo
    if id(value) in memo:
        return memo[id(value)]
    if isinstance(value, dict):
        result = {}
        memo[id(value)] = result
        result.update((key, observer_snapshot(item, memo)) for key, item in value.items())
        return result
    if isinstance(value, list):
        result = []
        memo[id(value)] = result
        result.extend(observer_snapshot(item, memo) for item in value)
        return result
    if isinstance(value, tuple):
        return tuple(observer_snapshot(item, memo) for item in value)
    if isinstance(value, set):
        return set(value)
    return value

"""Bounded schema-success proofs for canonical receipt-page bytes.

Entry: validate_page, proof_stats, clear_proofs. No original page or row is cached.
Every caller still fetches, hashes and authorizes originals. Validator identity
fences process-contract changes; a proof never establishes owner/chain authority.
The lazy LRU scales with the shared launch memory probe and retains only hashes
and validator references. No storage, worker, timer or persisted state lives here.
"""
from collections import OrderedDict
from threading import Lock

from .contracts import EvolutionStateError


class _Proofs:
    def __init__(self, capacity):
        self.capacity = capacity
        self.entries = OrderedDict()
        self.lock = Lock()
        self.hits = self.misses = 0

    def contains(self, digest, validator):
        with self.lock:
            if self.entries.get(digest) is validator:
                self.entries.move_to_end(digest)
                self.hits += 1
                return True
            self.misses += 1
            return False

    def remember(self, digest, validator):
        with self.lock:
            self.entries[digest] = validator
            self.entries.move_to_end(digest)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)


_cache = None
_init_lock = Lock()


def _capacity():
    from runtime_guards import probe_system_resources
    try:
        snapshot = probe_system_resources()
        values = (snapshot.effective_memory_capacity_mb,
            snapshot.effective_memory_available_mb)
        if any(type(value) is not int or value < 0 for value in values):
            return 1
        # One small proof per MiB of launch capacity/headroom, no eager allocation.
        return max(1, min(values))
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return 1


def _get_cache():
    global _cache
    with _init_lock:
        if _cache is None:
            _cache = _Proofs(_capacity())
        return _cache


def validate_page(page, digest, validator):
    """Reuse only an exact-byte success from this exact compiled contract."""
    cache = _get_cache()
    if cache.contains(digest, validator):
        return
    if not validator.is_valid(page):
        raise EvolutionStateError('run receipt archive contract changed')
    cache.remember(digest, validator)


def proof_stats():
    cache = _get_cache()
    with cache.lock:
        return {'capacity': cache.capacity, 'entries': len(cache.entries),
            'hits': cache.hits, 'misses': cache.misses}


def clear_proofs():
    """Release reconstructible proofs without changing originals or contracts."""
    global _cache
    with _init_lock:
        _cache = None

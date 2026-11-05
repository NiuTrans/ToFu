"""Cross-process report usage and recorded-cost projection, without I/O.

SQL operations validate identity/input and lock the authoritative row before
calling these reducers. Report hooks also use them for their best-effort local
projection after a failed write. Callers own persistence and replay; this module only
updates the supplied metadata. Dependencies must remain standard-library only.
"""

from collections.abc import Mapping


REPORT_TOKEN_FIELDS = {
    'prompt_tokens': 'promptTokens',
    'completion_tokens': 'completionTokens',
    'cache_read_tokens': 'cacheReadTokens',
    'cache_write_tokens': 'cacheWriteTokens',
    'reasoning_tokens': 'reasoningTokens',
}


def recorded_usage_integer(value) -> int:
    """Read stored integer usage without coercing strings, booleans or floats."""
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _recorded_cost(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def recompute_report_totals(metadata: dict) -> None:
    """Sum body and pass records; never look up current model prices."""
    passes = metadata.get('secondPasses')
    entries = tuple(entry for entry in passes.values() if isinstance(entry, Mapping)) \
        if isinstance(passes, Mapping) else ()
    total = {key: recorded_usage_integer(metadata.get(field))
             for key, field in REPORT_TOKEN_FIELDS.items()}
    for entry in entries:
        usage = entry.get('usage')
        if isinstance(usage, Mapping):
            for key in total:
                total[key] += recorded_usage_integer(usage.get(key))
    metadata['totalUsage'] = total
    for suffix in ('Cny', 'Usd'):
        field = f'cost{suffix}'
        amount = _recorded_cost(metadata.get(field)) + sum(
            _recorded_cost(entry.get(field)) for entry in entries)
        total_field = f'totalCost{suffix}'
        if amount or total_field in metadata:
            metadata[total_field] = amount


def merge_report_second_pass(metadata: dict, name: str, entry: dict) -> dict:
    """Replace one pass while retaining sibling metadata and recorded costs."""
    passes = metadata.get('secondPasses')
    if not isinstance(passes, dict):
        passes = {}
        metadata['secondPasses'] = passes
    passes[name] = entry
    recompute_report_totals(metadata)
    return metadata

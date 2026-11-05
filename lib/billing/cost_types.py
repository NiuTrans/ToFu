"""Shared integer cost result for dynamic pricing and frozen native invoices.

CostBreakdown carries the component totals and explainable pricing snapshot.
This value boundary has no price lookup, account admission or storage dependency.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CostBreakdown:
    """Per-component charge from dynamic pricing or a frozen native invoice."""

    micro: int  # total micro-credits to debit
    base_micro: int  # before margin
    margin_micro: int  # added on top of base
    matched_model: str  # model id the cost engine priced
    components: dict  # {'input': µ, 'output': µ, 'cache_read': µ, ...}
    snapshot: dict  # immutable/explainable pricing decision

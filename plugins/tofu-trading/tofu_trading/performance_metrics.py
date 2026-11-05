"""Shared equity arithmetic for simulation, backtest and risk reports.

Entry points compute adjacent returns, sample moments and maximum drawdown.
Callers own history admission, annualization, risk-free rates and output fields.
This leaf depends only on the standard library and performs no I/O or mutation.
"""
from collections.abc import Sequence
from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class Drawdown:
    fraction: float
    peak_date: str
    trough_date: str
    periods_to_trough: int


def adjacent_returns(values: Sequence[float]) -> list[float]:
    """Use a zero return when the preceding value is nonpositive.

    Simulation's sparse series omits those observations and retains its own
    return policy; backtest and advanced risk reports use this convention.
    """
    returns = []
    for i in range(1, len(values)):
        previous = values[i - 1]
        returns.append((values[i] - previous) / previous if previous > 0 else 0)
    return returns


def sample_return_statistics(returns: Sequence[float]) -> tuple[float, float]:
    """Return mean and sample standard deviation for a nonempty return series."""
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / max(len(returns) - 1, 1)
    return mean, math.sqrt(variance)


def maximum_drawdown(values: Sequence[float], dates: Sequence[str]) -> Drawdown:
    """Use the first observed equity as the peak for a nonempty paired series.

    Equal peaks and equal maximum declines retain their first dates. Duration
    counts observations since the selected peak, including equal-peak samples;
    it is neither a calendar duration nor the longest underwater interval.
    """
    peak = values[0]
    peak_index = worst_peak_index = trough_index = 0
    max_drawdown = 0
    for i, value in enumerate(values):
        if value > peak:
            peak = value
            peak_index = i
        drawdown = (peak - value) / peak if peak > 0 else 0
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            worst_peak_index = peak_index
            trough_index = i
    return Drawdown(max_drawdown, dates[worst_peak_index], dates[trough_index],
                    trough_index - worst_peak_index)

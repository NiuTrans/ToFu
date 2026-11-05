# Performance metric ownership

Problem: simulation, backtest and advanced risk reports independently computed
maximum drawdown. Backtest and risk reporting also repeated adjacent returns
and sample variance, despite having different surrounding policies.

Decision: `tofu_trading/performance_metrics.py` owns the identical arithmetic.
This standard-library-only leaf has explicit input series, no I/O, no retained
state and no mutation. Existing report entry points retain admission, rates,
annualization, downstream metrics, rounding and output shapes.

| Policy | Historical simulation | Backtest report | Advanced risk report |
| --- | --- | --- | --- |
| Return origin | Configured capital | First observed equity | First observed equity |
| Annualization | Calendar span and sparse frequency | 252 observations/year | 252 observations/year |
| Nonpositive preceding equity | Omit return observation | Include zero return | Include zero return |
| Variance denominator | Population size | Sample size minus one, at least one | Sample size minus one, at least one |
| Risk-free rate | Zero | Existing 2.5% annual convention | Existing 2.5% annual convention |
| Downside deviation | Not reported | Sample denominator; zero without downside | Population denominator; Sharpe-derived without downside |
| Minimum history | Empty result for empty series | Error for empty series | Error below ten observations |

These are retained compatibility semantics, not new market assumptions. Changing
them is a separate behavior decision. Simulation retains ratio-minus-one
returns; the other owners retain difference-divided-by-previous evaluation.

Drawdown starts at observed equity. Strictly greater peaks and declines win;
ties retain the first peak/trough dates. `periods_to_trough` is the selected
trough index minus peak index. Advanced risk retains its `max_drawdown_days`
output name for this observation count. Its longest underwater interval and
ulcer index stay separate: equal-peak recovery resets the former, whereas the
maximum-drawdown peak date retains the earlier equal peak.

Alternatives considered: duplicate scans invite divergence; one configurable
report engine would hide distinct policies behind unrelated switches. Supplying
configured capital to drawdown would introduce an unobserved peak. Persistent
caching could hide changed histories. None is needed for shared arithmetic.

Verification starts with `tests/test_performance_metric_policies.py`, then the
quantitative parity, fee, simulation and full plugin suites. Frozen-source
comparisons cover full payloads, warnings, errors and unchanged inputs; timings
cover complete report calls, not just the helper.

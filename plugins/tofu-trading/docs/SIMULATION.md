# Historical simulation

`tofu_trading/trading/llm_simulator.py::run_simulation` coordinates one run with
an explicit owner identity and its owner-bound `TradingConnection`. Model output
proposes decisions; deterministic admission, fill resolution and fee rules decide
which virtual trades execute. This engine does not place live orders.

Shared report arithmetic and retained metric policies are mapped in
[`METRICS.md`](METRICS.md).

## Realized outcomes

Problem: a stop-loss or take-profit is triggered by the decision bar's close,
but the next bar can gap through the entry price. Counting every take-profit as
a win and every stop-loss as a loss disagrees with the actual filled position.

Decision: all closed trades count as wins only when the executed exit price is
above the entry price, matching the stored position's gross `pnl_pct`. An equal
price is not a win. Fees remain separately charged to cash and total fees;
`winning_trades` is not a measure of net profit after costs. The session row,
metrics and subsequent model context use the same count.

The trigger still identifies why the exit was attempted. Decision and risk exits
use `_next_bar_fill`; an unavailable next open defers or rejects that exit. Final
liquidation retains its existing last-mark valuation. `FeeBook` owns all fee
schedules, provenance and holding-period rules.

Alternative considered: classify wins by the stop/target label. Rejected because
that label describes an earlier price observation, not the executed outcome.

Verification starts with `tests/test_simulator_accounting.py`, then the existing
execution-timing, adaptive-stop, end-to-end and fee suites. Synthetic fixed bars
and scripted model decisions must exercise real fills and the owner-bound storage
adapter without market or model network requests.

## Execution and projection owners

Problem: each exit path repeated cash, fee and win arithmetic, while live-event
and journal presentation was embedded in the decision loop. A long run also
rescanned its entire trade history for every step summary.

Decision: `trading/simulation_ledger.py::SimulationLedger` owns cash, charged
fees, closed-trade counts and valuation arithmetic for one invocation. Each buy
or sell receives the current `FeeBook` explicitly, preserving configuration
changes between steps. The engine retains position admission/removal and fill
resolution; `simulation_persistence.py` stages the required records and settles
them through the existing owner-bound transaction seam.

`trading/simulation_reporting.py::project_step_summary` owns the shared action,
summary and reasoning projection consumed by both live events and journal
replay. Decision dates are unique in the generated schedule. The engine captures
the trade-log offset before each step and supplies that step's new entries;
the complete log remains in the final result. Rejected model intentions do not
become displayed executions. No extra history cache is introduced.

`project_holdings` renders model context and analysis-event holdings from the
same positions after automatic exits. Problem: text captured inside the price
update loop retained positions already sold by a stop or target, and omitted
held positions when the current quote was unavailable. Decision: project the
surviving holdings once, using the same last known mark (or entry-price fallback)
as portfolio valuation. Formatting during price acquisition is rejected because
it precedes the completed risk decision. This changes context only; fill rules,
fees and the phase's transaction boundary remain with their existing owners.
The complete-engine accounting tests capture actual model requests and analysis
events for automatic exits, retained positions and missing quotes.

Alternatives considered: moving the whole loop behind another facade would
leave the duplicated accounting intact; a mutable global portfolio would mix
independent runs. Both are rejected. The ledger performs arithmetic only and
does not create another persistence, fee, risk or invocation authority.

Differential verification compares complete prompts, results, stored rows,
storage operations and ordered event sequences for unchanged execution scenarios.
The failure and publication semantics below are independently specified through
fault injection and fresh-connection observations.

## Model context and deterministic decisions

Problem: model instructions and buy/sell admission were embedded in the phase
coordinator alongside acquisition, rollback and publication. This obscured the
boundary between proposed intentions and authorized virtual execution.

Decision: `simulation_prompt.py::build_decision_prompt` renders the existing
system/user instructions from explicitly supplied context and live ledger/config
references. It performs no I/O. The coordinator still loads strategy/journal
context, invokes the shared model dispatcher with the existing request policy,
and parses the response within the same failure boundaries.

`simulation_execution.py::SimulationDecisionExecution` handles one step's
ordered model intentions. It owns hold/confidence checks and the buy/sell policy,
using explicit market/fill callbacks, the owner-bound connection, the live ledger
and the same `SimulationSettlement`. It preserves dynamic symbol registration,
current-equity limits, T+1 rules, fee selection and strategy attribution. It
neither commits nor publishes staged results. Automatic risk exits still precede
model context; phase rollback, settlement and final liquidation remain with the
coordinator. Mutable run state is passed by reference, never cached across runs.

Alternatives considered: moving the entire loop would preserve the mixed
responsibilities; copying configuration or adding another invocation/transaction
layer would change live fee updates and failure ownership. Both are rejected.
Fee guards cover the extracted owners, and the position-cap specification checks
actual cash/shares after a purchase against reduced equity instead of a source
expression. Complete-run comparisons include failed writes, failed commits,
partial decision failures and changes to the fee book between steps.

## Settlement and failure

Problem: simulation record writers swallowed storage exceptions. A run could
report a closed trade and completed result while its durable position remained
open. Trade events preceded commit, while the step summary was written afterward.
The broad step exception handler also retained partially mutated in-memory cash
and holdings, allowing a failed decision phase to leak into later results.

Decision: `SimulationSettlement` stages a single phase's trade rows, analysis and
decision journal, strategy feedback, step summary and session progress. Once
decision processing succeeds, it sends those writes through `write_transaction`
on the supplied owner-bound `TradingConnection`. All required writes propagate
errors. Only a successful commit publishes `sim_trade` and `sim_step_done`;
`sim_step`, analysis and acquisition progress still publish immediately. Session
creation precedes `sim_start`; final liquidation and result publication are one
transaction preceding `sim_complete`. Observer failures cannot reverse a commit.

Recoverable decision-processing exceptions discard that phase's staged writes
and events, restore the current ledger/holdings/symbol list, and truncate only
the appended history tails. The checkpoint copies current positions, never the
entire run history. Existing model-call failures remain recoverable and produce
`sim_error`; persistence failures escape to the shared task runtime's error
boundary without being treated as another decision to skip.

On an escaping failure, the engine attempts to mark the session `failed` without
changing the last durable cash, positions, counts or journal. Cleanup and failure
marker errors never replace the original exception, including process-control
exits. If rollback or the marker cannot be saved, the task still receives the
original failure; the stored session may retain its earlier `running` state.

A commit error can mean its acknowledgement was lost after the repository applied
the batch. The engine does not replay it or claim success. The storage adapter
reloads authoritative rows after commit failure, and the failure marker updates
only `running` rows: an already durable `completed` result is never demoted.
History presents running/failed sessions with their saved journal and without
inventing final performance metrics or a success notification.

Alternatives considered: holding a transaction across model/network work would
also let legacy price-cache helpers' implicit commits flush incomplete simulation
records. Retrying an uncertain commit could duplicate execution. Continuing after
a required write failed would recreate contradictory state. Staging until the
short settlement boundary avoids all three without introducing a second storage,
task-state, fee or execution authority.

Verification: `tests/test_simulator_settlement.py` injects SQL, repository commit,
rollback and marker faults, checks the shared task error outcome, and opens fresh
owner-bound connections from event callbacks. The frontend settlement suite
executes the shipped simulator UI against failed/running/completed history.

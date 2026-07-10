# Epic Learning — Close the Feedback Loop: a Utility Ledger + Credit Assignment

> **Status: DESIGN-FIRST / §10-GATED. No ledger/harvester code until owner sign-off.**
> This document exists to be reviewed *before* a line of learning-loop code
> is written. It is the same class of change as the parked scale-out epics
> ([`EPIC_C_RUNTIME_STATE_DESIGN.md`](EPIC_C_RUNTIME_STATE_DESIGN.md) et al.):
> a new cross-cutting substrate + a ratified §0 build order, reviewed first.
>
> The thesis being tested: **Tofu can become measurably smarter the more it
> is used** — not merely *bigger* — but only if we harvest a reward signal
> that already flows through the system unused, and only if credit assignment
> and anti-drift guardrails are load-bearing from day one, not bolted on.

---

## 1. Problem statement — three half-built loops, one missing ingredient

Tofu already has three mechanisms that *accumulate experience* and *change
behaviour*. Each is missing the SAME third piece: a **utility / reward signal**
telling a good change from a bad one. Confirmed by reading the code:

| Loop | Experience capture | Policy update | **Credit assignment (reward)** |
|---|---|---|---|
| **Optimizer** (`lib/optimizer/`) | ✅ `analyzer.gather_evidence` mines logs/audit/DB | ✅ `applier` + TTL-revert | ❌ `outcome_metric` is real **only** for `block_search_domain`; every other action type falls into the `{'note': 'no auto-metric for this action_type'}` branch (`analyzer.py` `_compute_post_apply_metrics`) |
| **Memory prefetch** (`lib/memory/prefetch.py`) | ✅ records which memories it injected (`task['_memoryPrefetch']`, and `audit_log('memory_prefetch', memory_names=…)`) | ✅ ranking = BM25 → cheap-LLM | ❌ nothing records whether an injected memory *helped*; the corpus only grows |
| **User profile** (`lib/memory/profile_consolidate.py`) | ✅ consolidation learns preferences; `applied_profile_items` records which bullets were injected | ✅ auto-applied, editable in Settings | ❌ a wrong inference is corrected only if the human notices and edits it |

Without a reward signal a system that accumulates **drifts** — it gets bigger
(more memories, more preferences, more applied actions), not reliably smarter.

```
Experience  →  Signal (reward)  →  Credit assignment  →  bounded Policy update  →  Experience
                     ▲ the scarce ingredient — and it is already in the logs, unharvested
```

The good news: we do **not** need to invent the reward signal. It already
flows through the system (critic verdicts, regenerate clicks, corrective
re-asks, in-place edits, aborts, tool-error-after-advice). This epic's job is
to **harvest it, attribute it precisely, and let it steer — slowly, reversibly,
and only after we have proven it is real.**

---

## 2. Non-goals / scope boundary (read this before objecting)

- **No model training, no fine-tuning, no RL on weights.** The LLM and the
  prompts stay fixed. Only per-*item*, per-*scope* scalar weights move (a
  memory's utility, a preference's confidence, an optimizer action's measured
  effect). This is bandit-style online *selection*, not gradient learning.
- **No new always-on network dependency.** The ledger is a table in the
  existing SYSTEM DB domain (§4). No Redis requirement (that is the scale-out
  epics' substrate; this epic is single-box-first and composes with them later).
- **No behaviour change on day one.** Everything ships in **shadow mode**
  (§8): we log the re-ranking / suppression the loop *would* do, and change
  nothing the model sees, for weeks — until the signal is proven (§8.3).
- **Not coupled to Epics B/C/D.** Per-`user_id` scoping reuses the profile
  scope seam (§7); when scale-out lands, the ledger is already keyed to survive
  it. But this epic does not wait on them and does not touch them.

---

## 3. The reward signal — a passive-signal taxonomy (and the critic caveat)

### 3.1 The critic verdict is a BONUS signal, not the backbone

`lib/agent_verdict.classify_verdict` (STOP vs CONTINUE_WORKER/PLANNER) is a
high-quality, automatic per-task quality judge. **But it only fires in endpoint
mode (Planner→Worker→Critic) and autopilot** (`endpoint_review.py`,
`autopilot.py`, `orchestration_engine.py` — the three consumers named in the
`agent-verdict-single-source-of-truth` skill). **Ordinary interactive chat
turns never run `classify_verdict`.** Treating it as "the single highest-value
signal" would over-index the loop on a minority of turns and starve the common
case.

**Design rule (load-bearing):** the harvester MUST produce a usable reward on a
plain chat turn *with no critic*. The critic is an **additive bonus** on the
tasks that have one — it raises confidence and weight on those, but its absence
never disables the loop. Concretely: the signal fusion (§6.3) reads the critic
term as *optional* (weight 0 when absent), and the min-sample gate (§6.4) is
sized against the abundant passive signals, not the scarce critic.

### 3.2 Signals available on EVERY turn (the backbone)

All already observable in-process or in the DB; none require nagging the user
(consistent with the profile's "inform, don't ask" ethos):

| Signal | Source (already exists) | Polarity | Notes |
|---|---|---|---|
| **Regenerate / retry same turn** | chat regenerate path; a new task for the same conv+turn | **negative** | strongest passive negative — the user rejected the answer |
| **Immediate corrective re-ask** | next user turn | negative | **RATIFIED: NOT in §0 Step 1.** Step 1 harvests *structural* signals only (regenerate / branch-edit-and-resend / abort — all free and unambiguous, and all subject to the §3.5 gates). A cheap-LLM "was that a correction?" sentiment classifier on the follow-up is deferred — added later ONLY if the shadow charts show the structural negatives are too sparse. Step 1's harvest is **zero-extra-LLM-cost.** |
| **In-place edit of the answer** | user edits the assistant message | negative (mild) | they had to fix it |
| **Task abort** (`task['aborted']`) | `TaskRuntime` / abort route | negative | user gave up mid-run |
| **Clean completion + no rework next turn** | task terminal `success` AND next user turn is a *new* topic (cheap-LLM "is this a continuation-because-broken?" = no) | **positive** | the default good outcome |
| **`detect_stuck`** (`agent_verdict.detect_stuck`) | already computed in loops | negative (efficiency) | Jaccard spiral |
| **Rounds-to-completion & token cost** | `accumulate_usage`, round counter | efficiency reward (secondary) | cheaper good turn > expensive good turn |
| **Tool error *after* a memory advised an approach** | executor `tool_error` audit event, joined to injected memory ids | **negative, targeted** | catches the exact trap-re-trigger memory prefetch was built to prevent |

### 3.3 Signals specific to a unit type

- **Profile:** a preference **edited or deleted in Settings** is the strongest
  possible negative on that learned unit (a human correction). A preference
  restated by the user is a positive. These come from the existing profile
  CRUD routes.
- **Optimizer:** the before/after metric the analyzer already knows how to
  compute for `block_search_domain` — generalized so every action type gets a
  measured `outcome_metric` (fills the `no auto-metric` gap).

### 3.4 Signal → scalar

Each harvested signal maps to a bounded scalar in `[-1, +1]` with a `weight`
(confidence). Regenerate = `-1.0 @ w1.0`; mild edit = `-0.3 @ w0.6`; clean
completion = `+0.5 @ w0.7`; critic STOP-clean = `+1.0 @ w1.0` (bonus, when
present). These starting values are **RATIFIED as the §10-tunable defaults** —
accepted as-is because they are cheap to change once the shadow charts (§8.2)
show which signals actually carry. The table is a tunable constant block (like
`key_stats`'s `MIN_ATTEMPTS` / `MIN_SUCCESS_RATE`), not hardcoded magic sprinkled
through the code; the fusion weights (§6.3) are ratified on the same terms.

### 3.5 The negative-signal confounder — regenerate/edit is NOT always "that was wrong"

> The negative-side twin of the §6.2 attribution discipline. Just as a good
> turn must not blindly credit every injected unit, a regenerate/edit must not
> blindly *penalize* every injected unit — the negative can be spurious, and a
> spurious negative POISONS a good memory just as surely as spurious positive
> credit inflates a useless one.

Two confounders make a raw regenerate/edit a false negative:

1. **Regenerate = "give me a variation," not "that was wrong."** Common on
   creative and image-gen turns — the user liked it and wants another take.
   Scoring that as negative-on-injected-units punishes memories that did nothing
   wrong.
2. **Branch-edit changes the PROMPT.** The injected units were selected for a
   query that no longer exists; penalizing them for an answer to a *superseded*
   prompt is attributing a mismatch they never caused.

**RATIFIED gating for the negative signals:**

- **Exclude creative / image-gen task types from the regenerate-negative
  entirely** (no negative credit is written for a regenerate on those task
  types). Regenerate is expected there and carries no quality signal.
- **Gate regenerate/edit negatives to turns where the prompt is *substantially
  unchanged*** — a plain regenerate (identical prompt) or a branch-edit whose
  edited prompt is near-identical to the original (cheap token/similarity check,
  no LLM). If the prompt changed materially, the injected units were chosen for
  a different query → write **no** negative against them.
- A negative that survives both gates still credits units only under the §6.2
  use-evidence rule (a negative is targeted at units that were actually in play,
  not sprayed across everything injected).

This is a hard prerequisite of §0 Step 1's harvest — the structural negatives it
harvests (see §3.2 ratified note) are only written after passing these gates.

---

## 4. The substrate — one append-only Utility Ledger

One new table in the **SYSTEM** domain, declared in `lib/database/_core_schema.py`
via `define_table` (the SINGLE source of every table — mirrors
`OPTIMIZER_ACTION_LOG`), with CRUD in a new `lib/learning/ledger.py` following
the `lib/optimizer/storage.py` shape verbatim (`db_execute_with_retry`,
`_as_json`, TEXT PK, `get_thread_db(DOMAIN_SYSTEM)`).

```python
# lib/database/_core_schema.py  (schema-version bump, §10.3-gated)
UTILITY_LEDGER = define_table(
    'utility_ledger',
    bigint_autoincrement_pk(),                 # high-churn, like rate_limit_events
    sa.Column('unit_type', sa.Text, nullable=False),   # memory|preference|opt_action|tool_pref|model_route
    sa.Column('unit_id',   sa.Text, nullable=False),   # memory name, pref bullet hash, opt action_log id, …
    sa.Column('scope',     sa.Text, nullable=False, server_default=''),  # user_id ('' = global; see §7)
    sa.Column('turn_id',   sa.Text, nullable=False),   # task_id — the credit-assignment key (§6)
    sa.Column('conv_id',   sa.Text, nullable=False, server_default=''),
    sa.Column('signal',    double_column(), nullable=False, server_default=sa.text('0')),  # [-1,+1]
    sa.Column('weight',    double_column(), nullable=False, server_default=sa.text('0')),  # confidence 0..1
    sa.Column('kind',      sa.Text, nullable=False, server_default=''),  # regenerate|edit|abort|critic_stop|…
    sa.Column('used',      sa.Integer, nullable=False, server_default=sa.text('0')),  # attribution flag (§6.2)
    sa.Column('shadow',    sa.Integer, nullable=False, server_default=sa.text('1')),  # 1 until §8.3 clears it
    sa.Column('ts',        sa.Text, nullable=False),
)
```

Plus a small **derived** rollup, `utility_weights(unit_type, unit_id, scope,
ewma, sample_count, updated_at)` — the EWMA state read on the hot path (§6.5),
so ranking never scans the raw ledger. One writer per unit type (the
harvester), many readers (memory ranking, profile consolidation, optimizer).

**Why append-only + a derived rollup:** the raw ledger is the auditable
evidence (why did this memory get demoted?); the rollup is the fast read. Same
split the optimizer already uses (`optimizer_action_log` rows + computed
`outcome_metric`).

---

## 5. The harvester — where the signal is written

A single `lib/learning/harvest.py` module, called from **task terminal** (one
hook in `lib/tasks_pkg/manager.py` where a task reaches a terminal state — the
same place `_sync_result_to_conversation` already persists `_memoryPrefetch`).
It:

1. Reads the units that were **in context** for this `turn_id` — already
   recorded: injected memory names (`task['_memoryPrefetch'].memories`),
   injected preference bullets (`applied_profile_items`), active optimizer
   actions (`storage.list_applied_actions`).
2. Computes the turn's reward from the passive signals (§3.2), plus the critic
   bonus **iff** this task ran a verdict.
3. Applies the **attribution rule** (§6) to decide *which* units get credited
   and how much.
4. Appends one ledger row per credited `(unit, signal)` and updates the EWMA
   rollup.

Deferred / next-turn signals (regenerate, corrective re-ask, in-place edit)
are harvested on the *following* turn by matching `conv_id + prior turn_id`,
and written against that prior `turn_id`. This is why `turn_id` is the ledger's
spine.

Everything is **best-effort and non-blocking** (mirrors the memory-prefetch
and profile-consolidate discipline): any harvest failure logs a warning and the
turn is unaffected. The reward loop must never be able to break a chat turn.

---

## 6. Credit assignment — the anti-reward-hacking core

> Crediting *every* injected unit for a good turn is how this design would
> quietly fail. If 5 memories are injected and 1 (or none) mattered, uniform
> credit drifts toward reinforcing **whatever gets injected most**, not what
> helps. This section is the guardrail, not a footnote.

### 6.1 The failure mode, named

"Reward hacking / spurious credit": a unit accrues positive reward merely by
being *present* on good turns, regardless of whether it *caused* the good
outcome. Over time the most-injected units dominate — a feedback loop that
reinforces its own past selections (feedback collapse).

### 6.2 Attribution rule — credit only on evidence of USE

A unit is credited for a turn **only** when at least one holds (the `used`
flag, per unit):

1. **Referenced** — the model's output or tool-call args reference the unit
   (a memory's subject/path/symbol appears in the assistant turn or a tool
   argument; a preference's directive is observably followed). Detected by a
   cheap deterministic check first (substring/symbol match), escalated to a
   single cheap-LLM "did this unit influence the answer?" only when the turn is
   otherwise credit-ambiguous (bounded ≤1 extra cheap call/turn). **RATIFIED
   fail-closed rule:** if that cheap-LLM call errors, is rate-limited, or is
   disabled, the escalation falls back to the deterministic path only
   (substring/symbol + sole/dominant) — NEVER to "assume used / credit
   everything." A missing or failed use-check is **"no credit," not "assume
   used."** (Note: §0 Step 1 does not enable this escalation at all — see §3.5
   / §10; the deterministic path is the only attribution in the first loop.)
2. **Sole/dominant** — it was the ONLY unit of its type injected this turn
   (then the turn outcome is unambiguously attributable to it).
3. **Targeted negative** — a tool-error / trap that the unit specifically
   advised against or caused (the §3.2 "tool error after advice" join). Negative
   credit needs *less* evidence than positive credit — a re-triggered trap is a
   strong, specific signal — but still must name the unit.

Units that were injected but show **no** evidence of use get **no** reward
(neither + nor −). "Injected but ignored" is not "helpful". This single rule
is what stops the most-injected-wins collapse.

### 6.3 Signal fusion per turn

```
turn_reward = clamp(
    w_passive · passive_signals            # always present (§3.2)
  + w_critic  · critic_term                # 0 when no verdict ran (§3.1)
  + w_eff     · efficiency_term            # rounds/tokens, secondary
, -1, +1)
```

`w_critic` is 0 for a plain chat turn — the fusion is designed to degrade to
"passive only" gracefully. No single signal may exceed its cap; no single turn
may move a unit's EWMA by more than a fixed step (§6.5).

### 6.4 Minimum sample count before any weight moves behaviour

Mirrors `key_stats.MIN_ATTEMPTS` exactly (the project already trusts this
pattern to avoid flapping on 1–2 events):

- A unit's EWMA is **not read by any policy** until `sample_count >=
  LEARN_MIN_SAMPLES` (**RATIFIED default 12** credited turns — raised from 8:
  per-memory injections are sparse for niche memories, and a memory behaving
  exactly as today for longer is strictly preferable to graduating on a handful
  of confounded turns. Conservatism is free in shadow mode. Sized against the
  abundant passive signals, NOT the scarce critic).
- Below the threshold the unit behaves exactly as today (pure BM25→cheap-LLM
  for memory; pure consolidation confidence for profile).

### 6.5 EWMA, not last-value; bounded step

- Utility is an **exponential moving average** (`ewma = (1-α)·ewma + α·signal`,
  proposed `α = 0.2`), never last-value — one bad turn can't tank a
  consistently-useful memory, and one lucky turn can't crown a useless one.
- A single update is **step-clamped** so no turn moves the EWMA by more than a
  fixed delta — dampens both noise and adversarial/self-reinforcing spikes.

### 6.6 Scope isolation

Credit is written and read **per-scope** (§7). One tenant's signal never moves
another tenant's — and never moves the global default. This is both a privacy
property and an anti-poisoning property.

---

## 7. Per-`user_id` scoping — reuse the profile scope seam

The ledger's `scope` column is populated by the **same** rule the user profile
already uses: `lib/memory/user_profile.resolve_profile_scope(ctx)` →
authenticated `user_id`, `''` for open/private single-operator mode. Captured
onto the task at creation (`task['_profileScope']`, the daemon-thread-safe
snapshot already in place), so the terminal harvester has it without a request
context. Consequences, inherited for free:

- **Open / private mode (single operator):** `scope=''` → one global weight
  set. Byte-identical accumulation semantics to "just me".
- **Multi-user tenant:** per-`user_id` weights. Tenant A's regenerate never
  demotes a memory for tenant B. Matches the `apply_headless_personal_defaults`
  isolation posture (§3.7 CLAUDE.md) — personal signal fails closed across the
  boundary.

No new scoping mechanism, no new privacy surface to reason about.

---

## 8. Shadow mode — prove the signal before it steers anything

> The single most important discipline in this epic. We do NOT let a weight
> change what the model sees until we have weeks of evidence that the weight
> *predicts* good turns. This is the project's "claim positive stats only if
> individually significant" bar applied to a control loop.

### 8.1 What ships first (shadow = the default `shadow=1`)

- The ledger + harvester run for real: every terminal turn writes rows and
  updates EWMA.
- Memory ranking, profile consolidation, and the optimizer **read** the weights
  and **compute** the decision they *would* make (re-rank order, suppress-this-
  preference, graduate-this-action) — then **log it and discard it**. The model
  sees exactly today's behaviour.

### 8.2 Shadow metrics emitted (the proof artifacts)

Per unit type, emitted to `audit_log` + a small `debug/` report so we can chart
them over weeks:

- **Predictive validity — OUT-OF-SAMPLE ONLY (non-negotiable):** "Do high-EWMA
  memories co-occur with positive-reward turns?" measured *in-sample* is
  **circular and proves nothing** — the EWMA is built FROM those turns' rewards,
  so it is positive by construction. The validation MUST be held-out:

  > **Split every unit's history into a disjoint train window and a later test
  > window.** Compute each unit's EWMA on the *train* window ONLY (e.g. weeks
  > 1–2). Then measure whether that frozen weight — computed WITHOUT ever seeing
  > the test turns — predicts the realized reward of turns in the *test* window
  > (e.g. week 3+). A unit's weight was never allowed to see the turns it is
  > being scored against. Only an out-of-sample correlation can falsify "the
  > signal is real"; the in-sample version is unfalsifiable and is explicitly
  > forbidden as a graduation input.

  The reported statistic is a **held-out rank correlation** (Spearman ρ between
  train-window EWMA and test-window realized reward, per unit type) **with its
  p-value**. Flat or non-significant ρ ⇒ the signal is noise ⇒ do NOT graduate,
  keep measuring. (The in-sample co-occurrence chart may still be logged as a
  sanity/plumbing check, but it is NEVER a graduation criterion.)
- **Would-change rate:** how often would the learned ranking differ from the
  BM25→cheap-LLM ranking, and in which direction?
- **Attribution coverage:** fraction of injected units that ever get a `used`
  credit (if ~0, attribution is too strict; if ~100%, too loose — §6.2 needs
  tuning).
- **Stability:** EWMA variance per unit; drift detection (is any unit's weight
  ratcheting monotonically = feedback collapse warning?).
- **Sample sufficiency:** how many units have crossed `LEARN_MIN_SAMPLES`.

### 8.3 Graduation gate (§10-GATED, human decision)

A weight is allowed to influence behaviour (`shadow → 0`) ONLY when, per unit
type, ALL of the following hold (RATIFIED bar):

1. **Held-out predictive validity is significant.** The §8.2 out-of-sample
   statistic — Spearman rank correlation between the train-window EWMA and the
   *test-window* realized reward, per unit type — is positive with **p < 0.05
   on the held-out turns**. In-sample co-occurrence does NOT count. "Significant
   on its own" means this specific held-out test clears its p-value on its own
   window, not a cherry-picked run (the user's stated bar).
2. **Window ≥ 2 weeks** AND enough test-window turns for the correlation to be
   meaningful (report n alongside ρ and p; a significant ρ on a handful of
   turns does not graduate).
3. Attribution coverage (§8.2) is in a sane band, and no drift-collapse alarm
   fired.

Flat or non-significant held-out ρ ⇒ do NOT graduate; keep measuring.
Graduation is a human sign-off recorded as `audit_log('config_change',
change='learning_graduate_<unit_type>', approved_by='user')`. Until then,
`shadow=1` and the loop is a measurement instrument only.

### 8.4 Reversibility after graduation

Every graduated influence is bounded and reversible, reusing existing seams:

- **Env kill-switch** per unit type (`TOFU_LEARN_MEMORY=0`, …), fail-open to
  today's behaviour — the `_resolve_feature_flag` pattern already used by
  prefetch/consolidate.
- Memory influence is a **bounded prior** on the existing cascade (it re-orders
  and can demote a chronically-useless memory below the injection cut, but the
  cheap-LLM precision filter still has final say — the weight is a thumb on the
  scale, not a new gate).
- Optimizer graduation (suggest-only → auto_apply) inherits the **TTL-revert**
  that already exists (`applier.revert_expired_actions`): a graduated action
  that measures negative auto-reverts.
- Profile suppression only ever *withholds re-adding* a repeatedly-corrected
  preference; it never deletes what the user wrote. The human's Settings edit
  is always ground truth.

---

## 9. What each loop becomes (after graduation)

- **Memory prefetch self-prunes toward what helps THIS user.** A memory
  injected often but never *used*-with-positive-reward sinks below the cut and
  eventually becomes a GC candidate (surfaced to the user, never silently
  deleted). This directly fixes "the corpus only grows".
- **The optimizer closes the loop for all 11 suggest-only action types.** The
  ledger gives the before/after `outcome_metric` the analyzer is missing today,
  so an action can *graduate* to auto-apply once its measured utility clears a
  bar — and auto-revert when it doesn't.
- **The profile stops re-proposing corrected preferences.** A preference the
  user keeps editing away gets low confidence; consolidation suppresses
  re-adding it. Fewer wrong inferences over time.

---

## 10. Ratified §0 build order (proposed — awaiting owner sign-off)

Smallest fully-closed loop FIRST; prove the flywheel turns before generalizing.
No later step starts before the earlier is green (mirrors the scale-out epics'
§0 discipline).

1. **Ledger substrate + harvester, MEMORY only, SHADOW only, ZERO-extra-LLM.**
   Schema table + `lib/learning/ledger.py` + `lib/learning/harvest.py` terminal
   hook. Harvest the **structural** §3.2 backbone only — regenerate /
   branch-edit-and-resend / abort / clean-completion / efficiency (+ critic
   bonus when a verdict ran) — against the memory ids already in
   `task['_memoryPrefetch']`, with the §3.5 confounder gates applied to every
   negative. Attribution = the **deterministic** §6.2 path only (substring/symbol
   + sole/dominant); no cheap-LLM use-check and no corrective-sentiment call in
   this loop. EWMA rollup, `LEARN_MIN_SAMPLES=12`. **No read path into ranking
   yet.** Ship. → One closed loop end-to-end on top of existing plumbing, fully
   reversible (a table + a best-effort hook), zero added per-turn LLM cost, and
   it validates the entire thesis.
2. **Shadow read + metrics (memory).** Memory ranking computes the would-re-rank
   and logs the §8.2 metrics. Still changes nothing the model sees. Run for the
   graduation window; chart predictive validity.
3. **Graduation gate (memory)** — §8.3 human sign-off. Only then does memory
   utility become a bounded prior on the cascade, behind a kill-switch.
4. **Generalize the ledger to the optimizer** (fills the `no auto-metric` gap;
   suggest-only → auto-apply graduation on measured utility, auto-revert on
   negative). Shadow → metrics → gate, same three beats.
5. **Generalize to the profile** (correction-suppression). Shadow → metrics →
   gate.

Step 1 is a day of work on existing seams and is the only thing to build once
this doc is signed off. Steps 2–5 each re-run the shadow→prove→gate cadence.

---

## 11. Owner decisions — RATIFIED (were open questions)

These are now commitments, not questions.

1. **Signal→scalar table (§3.4) + fusion weights (§6.3):** proposed starting
   values ACCEPTED as the §10-tunable defaults — cheap to change once the shadow
   charts show which signals carry.
2. **`LEARN_MIN_SAMPLES` (§6.4): 12**, not 8. Per-memory injections are sparse
   for niche memories; behaving as-today for longer beats graduating on a
   handful of confounded turns. Conservatism is free in shadow mode.
3. **Graduation bar (§8.3): ≥2 weeks AND a held-out out-of-sample test** —
   Spearman rank correlation between train-window EWMA and *test-window*
   realized reward, per unit type, with **p < 0.05 on the held-out turns** (n
   reported alongside). In-sample co-occurrence is forbidden as a graduation
   input (circular). Flat/non-significant ⇒ do NOT graduate.
4. **Attribution escalation (§6.2 rule 1):** the ≤1-extra-cheap-LLM-call/turn
   use-check is ALLOWED, but **fail-closed to deterministic-only** — on error /
   rate-limit / disabled it falls back to substring/symbol + sole/dominant,
   NEVER to "credit everything." A missing use-check is "no credit." (And it is
   OFF entirely in §0 Step 1.)
5. **Corrective-re-ask sentiment (§3.2): structural signals ONLY for Step 1**
   (regenerate / branch-edit-and-resend / abort — free and unambiguous). No
   cheap-LLM "was that a correction?" call in the first loop; add it later ONLY
   if the shadow charts show the structural negatives are too sparse. Step 1
   harvest is zero-extra-LLM-cost.
6. **Regenerate/edit confounder (§3.5):** ratified — exclude creative/image-gen
   task types from the regenerate-negative entirely, and gate all
   regenerate/edit negatives to substantially-unchanged prompts (a branch-edit
   that materially changes the prompt writes no negative against units selected
   for the superseded query).

---

*Prepared as the design-first deliverable for the learning-loop epic. Same
governance as the scale-out epics: any schema change is §10.3-gated (bump
`_SCHEMA_VERSION` in both `_schema_pg.py` and `_schema_sqlite.py`, mirror DDL,
`audit_log('config_change', approved_by='user')`); graduating any weight from
shadow to steering is §10-gated with human sign-off. No implementation code
beyond §0 Step 1 until the owner confirms after reading this doc — and Step 1
itself is shadow-only by construction.*

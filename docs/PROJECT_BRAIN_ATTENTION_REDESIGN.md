# Project Brain — Attention-First Redesign

> **Status:** design + phased implementation (2026-07-26).
> **Driver (owner):** *"信息太散了，需要把所有需要人类介入的事情集中起来，并且在
> presence-strip 和 collab-bar 里也要有指示。而且除非必要，尽量减少任务被人类选项
> 阻塞——让 LLM 自己选最长期、最全面、最优雅、不计成本的改进方式。"*
>
> Companion docs: [`PROJECT_BRAIN.md`](PROJECT_BRAIN.md) (the pillars),
> [`PROJECT_BRAIN_STATUS_LANE.md`](PROJECT_BRAIN_STATUS_LANE.md) (Pillar #7).

---

## 1. What is actually wrong today

The panel is not ugly because of its colours. It is hard to read because **the
things a human must ACT on are scattered across four of the five tabs**, and
the one surface that is always visible (the collab bar) leads with the *least*
urgent of them.

### 1.1 The scatter — where "needs you" lives today

| Human-actionable thing | Where it lives now | Visible without clicking? |
|---|---|---|
| Epic blocked with a structured **question** (auto-retry **paused forever** until answered) | Board tab → *Awaiting your answer* lane | **No** — not in `build_brain_summary` at all |
| Charter **pending proposal** (commit / reject) | Charter tab → *Proposed* section | Yes — and it *leads* the collab bar |
| **Conflict** advisory (two convs writing the same files) | Collab bar detail lines + nowhere else | Partially |
| Epic on a **cooldown block** (self-expiring, no action needed) | Board tab → *Blocked* lane | No |
| Stuck / mis-held **claim** (reopen) | Board tab → *Claimed* lane | No |
| Peer **hard-abort** approval | Team tab → per-peer control | No |
| Watch-item open concern | Status & Watch tab | No |

Four tabs, one banner, one bar — to answer one question: *"is anything waiting
on me?"* That is the complaint, stated precisely.

### 1.2 The inversion — the loudest signal is the least urgent

`build_brain_summary` orders its segments by "action value" and puts
`pendingDecisions` **first**, rendered `.collab-seg-decisions` with emphasis
(`presence.js:_segments`). But since **2026-07-12 the owner de-gated charter
decision commits** — agents call `project_charter_commit` themselves
(`project_charter.py` module docstring; `execute_charter_tool`). A proposal is
now explicitly *"only for suggestions you are not yet ready to make binding"*.

So the bar shouts about an **advisory** item, while the item that *genuinely
stops a workstream indefinitely* — `block_question` with no `human_answer`,
which `project_dispatch.py:191` uses to skip the epic on **every** heartbeat —
is not in the summary payload at all.

### 1.3 Redundancy makes each surface longer without adding information

Confirmed by survey (`project-brain-peers.js` / `-status.js` / `renderCharter`):

- peer count renders **4×** (roster head, Team badge, collab bar, Status
  evidence chip);
- board open/claimed/done/blocked counts render **3×** (Board lanes, Status
  evidence chips, collab bar);
- pending-decision count renders **3×** (Charter badge, Status chip, collab bar);
- every Status *history* row repeats the whole evidence chip set.

Nothing here is wrong; it is just the same numbers restated in four dialects.
Density without information is exactly what "hard to read" feels like.

### 1.4 A real correctness gap found while surveying

`project_board.py:586` is the **only** place a block class is parsed:

```python
block_class = 'sibling' if _SIBLING_TAG in reason.lower() else 'human'
```

`'[human-gated]'` is **never matched anywhere in the codebase** — "human" is
merely the `else` branch. And the class affects **only the backoff curve**
(`_block_cooldown_ms`), never lane placement or dispatch eligibility.

**Consequence:** the string prefix is not a trustworthy signal. The redesign
keys "needs a human" on **`block_question` presence**, which is the field
dispatch actually honours, and renders the `[human-gated]` / `[sibling]` prefix
as a *human-readable badge only*.

---

## 2. Design decisions

These were decided rather than escalated, per the owner's standing instruction
("unless necessary, minimise human-blocking options; pick the most long-term,
elegant approach"). Each records its rationale so it can be overturned knowingly.

### D1 — One SSOT for attention, computed backend-side

A new `lib/conversations/project_attention.py` exports
`build_attention_items(project_path, conv_id)` → a **priority-ordered list of
typed items**. Every surface (collab bar, presence strip, the new panel tab,
the influence lens) renders *that one list*.

*Why backend:* the frontend already re-derives "is this awaiting an answer?"
from raw board rows in `renderBoard`. A second client-side partition is exactly
how the Board lane and the collab-bar count drift apart. The same argument
`read_board`'s docstring already makes for its counts.

*Rejected:* a client-side aggregator over the existing four API calls — cheaper
to write, but it makes the panel the source of truth for a semantic the backend
owns, and leaves the bar unable to show the count without opening the panel.

### D2 — Severity, not source, is the ordering key

Two severities only:

| Severity | Meaning | Members |
|---|---|---|
| `blocking` | Work is **stopped** and only a human can restart it | board `block_question` unanswered; peer hard-abort awaiting approval |
| `advisory` | Progress continues; a human *may* improve the outcome | charter proposals |

(Conflict advisories were advisory members at launch; removed 2026-08-01 —
see §D7. Stuck claims and expiring cooldowns were never implemented as items.)

The collab bar's emphasis class is driven by whether any **blocking** item
exists — not by the pending-proposal count. This directly fixes §1.2.

*Why two and not five:* the only decision the operator makes from a glance is
"do I have to get up?". Grades between "urgent" and "very urgent" are noise.

### D3 — A cooldown block is NOT an attention item

`blocked_until` expires at read time and `select_dispatchable` re-picks the
epic with no human involvement (`project_dispatch.py:181-184`). Listing it
under "needs you" would train the operator to ignore the surface. It stays in
the Board tab's Blocked lane, where the *reason* and *retry-in* are the useful
content, and it appears in the attention surface **only** as a muted
"nothing needs you, N waiting on their own gates" reassurance line.

### D4 — Watch items are the human's outbox, not inbox

`project_watch` items are things the *human* asked the brain to keep an eye on.
They are not the brain waiting on the human. They stay in the Status tab.

### D5 — Autonomy-first: raise the bar for asking a human at the source

This is the owner's core instruction, and UI alone cannot deliver it — the
count is only low if agents *stop parking work on humans*. So the redesign also
tightens the two tool surfaces that create blocking items:

- `project_board_block`'s description gains an explicit **decision rule**: ask
  a human **only** when the choice is (a) irreversible or expensive to undo,
  (b) a matter of taste/policy with no technically-best answer, or (c)
  unverifiable from inside the repo. Otherwise **choose the most robust,
  long-term option, proceed, and record the choice** as a charter decision
  (which agents may now self-commit) so it is auditable and reversible.
- The tool text stops presenting "ask the human" as the safe default. Blocking
  a workstream for days on a question the agent could have answered — and
  recorded — is framed as the *more* costly error, because it is.

*Why here and not only in a prompt:* the tool description is the text the model
reads at the moment of the decision. A rule in `CLAUDE.md` is not in context
when `project_board_block` is being considered.

### D6 — Deep-link, don't duplicate

The attention tab renders each item with **its own resolving control inline**
(answer chips + free text; commit/reject) — the *same* controls the owning tab
has, from the same code path, not a reimplementation. Nothing is duplicated;
the sources stay authoritative.

### D7 — A live file conflict is NOT an attention item (2026-08-01 owner directive)

A conflict advisory fails the surface's own admission test on three counts:
it is **notify-only** (the system deliberately never locks), **self-clearing**
(recomputed from the presence registry; vanishes when a peer goes idle), and
has **no resolving control** (the card could only deep-link elsewhere —
*"the operator decides whether to intervene"*). The owner's verdict: *"since
you don't need me to handle it, don't display it here."* So `_conflicts` was
removed from `build_attention_items`, which also drops overlaps from the
`needsYou` count on the collab bar. Detection itself is untouched — the
overlap stays visible as LIVE STATUS in the bar's detail lines
(`summary.conflictMessages`) and the Team tab, both fed by
`lib.presence.conflict.detect_overlaps` directly.

---

## 3. The surfaces

### 3.1 `presence-strip` / `collab-bar` (always visible)

```
🧠 Project · ⚠ 2 need you · 5 in progress · 3 open · 2 online
             └─ new, leads, only when blocking>0 or advisory>0
```

- A single **`needsYou`** segment replaces the `decisions awaiting you`
  segment. Its count is `blocking + advisory`; its *style* is driven by
  `blocking > 0` (`.collab-has-blocking`) so an advisory-only project reads
  calm.
- Clicking the bar opens the panel **on the attention tab** when the count is
  non-zero, otherwise on the previously-selected tab. The bar is the operator's
  question; the tab is the answer.
- The existing `.collab-seg-decisions` class is **kept as an alias** on the new
  segment so the collab-bar test contract (and any muscle memory) survives.

### 3.2 The panel: a new first tab

Tab order becomes:

```
Needs you (N) · Charter · Board · Activity · Team · Status & Watch
```

`Needs you` is first and default-selected **when N > 0** (otherwise Charter
stays the landing tab, so a quiet project doesn't open onto an empty state).

Empty state is a deliberate, positive statement rather than a dashed box:
*"Nothing needs you. 3 epics in progress, 2 waiting on their own gates."*

### 3.3 Readability changes (the "presentability" half)

Applied to the whole panel, not just the new tab:

1. **One card grammar.** Today a board card, a charter proposal, a peer card
   and a watch item are four different layouts. All become the same primitive:
   `severity rail · title · meta row · actions row`, differing only in accent.
2. **Type scale with actual hierarchy.** Currently title/meta/body sit within
   ~1.5px of each other, so nothing recedes. Title 13.5px/600, meta 11.5px
   tertiary, body 12.5px secondary.
3. **Section rhythm.** Lane heads become sticky within the scroller so a long
   board keeps its context.
4. **Kill the duplicate counts.** Status evidence chips drop the counts that
   the tab badges already carry; the roster head drops the count the Team badge
   carries.
5. **Quiet the chrome.** Per-card action buttons render icon-only until hover /
   focus-within on pointer devices (already the pattern for
   `.pb-charter-row-actions`), full labels on coarse pointers.

---

## 4. Phases

| Phase | Deliverable | Files |
|---|---|---|
| **P1** | `project_attention.py` SSOT + `build_brain_summary` carries `needsYou`/`attention`; failing-first tests | `lib/conversations/project_attention.py`, `project_brain_summary.py`, `routes/api_v1/project.py`, `tests/test_project_attention.py` |
| **P2** | Attention tab + collab-bar segment + deep-link | `index.html`, `static/js/project-brain-attention.js`, `project-brain.js`, `presence.js`, `api.js`, `i18n.js`, `lib/js_bundler.py` |
| **P3** | Autonomy-first tool descriptions (D5) | `lib/conversations/project_board.py`, `project_charter.py`, tool schemas |
| **P4** | Readability pass §3.3 | `static/styles.css` |

Each phase commits separately with explicit pathspecs (shared-HEAD discipline).

---

## 5. Contracts a redesign must not break

From the frontend test inventory — these are **load-bearing** and either
preserved or migrated **in the same commit** as their test:

- collab bar: `#presenceStrip`, `[data-testid="collab-bar"]`,
  `.collab-bar-inner`, `.collab-label`, `.collab-seg-*`, `.collab-has-decisions`,
  `.collab-has-conflicts`, `.collab-peer-epic`, `CollabBar._setSummary/_setPeers/_render`.
  Classes asserted **absent** (`.collab-status`, `.collab-cluster-conv`, …)
  must stay absent.
- panel: `.project-brain-col.pb-tab-panel{display:none}` /
  `.pb-tab-panel-active{display:flex}` must stay **compound** selectors
  (`test_project_brain_tab_css_cascade`), `.project-brain-columns` must keep
  `display:block` and **no** `grid-template-columns`.
- `data-pb-src` / `data-pb-tr` attributes are the content-translation contract.
- Every infinite animation must sit under `prefers-reduced-motion`.
- NEUTER tests anchor on **exact source strings** in `project-brain.js`,
  `presence.js`, `project-brain-i18n.js` — reformatting those regions requires
  updating the anchors in the same commit.

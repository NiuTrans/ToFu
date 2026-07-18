# Project Brain — Per-Conversation Worktree Isolation

> **Status:** DESIGN — owner-directed 2026-07-11. Q1/Q2 RATIFIED; FUSE
> validation V2–V4 + the land primitive RUN FOR REAL and CONTENT-verified (§7.1
> — the git worktree model SURVIVES on the beegfs-fuse mount; no fallback to
> lighter isolation needed). The §5.1 land primitive was CORRECTED after the
> owner caught that a `commit-tree <conv-tree>` "merge" silently discarded 7 of
> 8 landers' content (last-tree-wins) while all 8 stayed reachable — the land
> now does a REAL 3-way merge and the gate is CONTENT-verified, not reachability.
> A SECOND owner catch closed a false-green one level up: the acceptance gate
> now re-runs ON THE MERGE-RESULT tree inside the CAS critical section (§5) —
> two branches that each pass their own gate can merge textually-clean into a
> semantically-broken trunk (A drops `foo()`, B adds a call to it), so a
> pre-merge-only gate is a false green; content-preserved ≠ integration-correct.
> **BUILD STARTED 2026-07-12: step 1 LANDED (`e313197`)** — the dark
> `project_worktree.py` module (lifecycle + the CAS-merge land algorithm),
> env-gated `inproc`/OFF (byte-identical single-box), 16 tests green through the
> fresh-HEAD acceptance gate. Steps 2–6 (per-conv `_roots` binding, worktree-
> scoped path resolution + `run_command` cwd, land-verb WIRING, dispatch write-
> set partitioning, legacy-gate retirement) remain and each wire into shared
> route/JS/tool files sibling conversations currently hold. Tier-2
> bleeding-control (class-aware backoff + event-driven wait_paths) landed
> already (`a73f824`) and is NOT a substitute for this.
>
> **Scope:** replaces the entire commit-time contention apparatus
> (`project_commit` byte-identity gate, `project_ready` overlap-hold,
> contamination classifier) with structural authorship via isolated git
> worktrees, plus dispatch-time file-ownership partitioning.

---

## 0. The defect, named precisely

The pain — "autonomous promotion always gets blocked by other conversations" —
is **not** fundamentally "a shared working tree." It is **authorship-by-inference**.

`project_commit` decides "is this file mine to commit?" by *inferring* authorship
from byte-identity: a file is clean iff its on-disk bytes equal *this*
conversation's last recorded post-image (`_build_conv_version_map` →
`_classify`, `lib/conversations/project_commit.py`). That inference **cannot in
principle** distinguish:

- *"this hunk is mine"* — I wrote it, from
- *"this hunk is a sibling's that predated my first touch"* — my recorded
  post-image already *contained* the sibling bytes, so `disk == blob` holds and
  the file is declared CLEAN, and `git add` sweeps the sibling's hunks into my
  commit (the **false-clean trap**, journal 2026-07-11 passim).

No amount of tuning fixes an inference that structurally lacks the information.
Byte-identity proves "matches MY last write," never "contains ONLY my work."

**Worktrees are the right fix because they make authorship STRUCTURAL rather
than inferred.** Your worktree = your branch = your commits, by construction.
There is nothing to infer: the git DAG *is* the authorship record. Once that
holds, the byte-identity gate, the overlap-hold serializer, and the
contamination classifier all collapse into a single well-understood operation —
**merge the branch** — and real conflicts surface as ordinary merge conflicts, a
solved and finite event instead of a permanent land-time jail.

### 0.1 Consequent design rule: RETIRE, do not port

The following mechanisms exist ONLY to survive N writers in one tree. Under
worktree isolation they have no job. They are **retired**, not ported into the
new model:

| Retired | File | Why it dies |
|---|---|---|
| Byte-identity contamination gate | `project_commit.py` `_classify` / `_build_conv_version_map` | Authorship is structural — nothing to classify |
| Overlap-hold serializer | `project_ready.py` `_partition_by_overlap` | Conflicts are merges, not held clusters |
| "false-clean" hunk-audit discipline | (human ritual + memories) | No shared tree to contaminate |
| `wait_paths` as a *land-time* gate | `project_board.py` `_paths_waited_but_held` used at commit | Superseded by dispatch-time ownership (§4) |

**Kept and reused** (they are correct and worktree-orthogonal):
- `run_acceptance_gate` / `detect_orphaned_callers`
  (`project_acceptance.py`) — the fresh-worktree gate is *already* the proven
  `git worktree add --detach` seam on this FUSE tree; it becomes the pre-merge
  CI check.
- The board's soft-lease + `_effective_status` at-read-time expiry model — sound,
  deadlock-free; reused for worktree GC leases.

---

## 1. Why the shared tree is still the mechanism to remove

Even though the *defect* is inference, the *enabler* is the single tree: the
server is ONE process with ONE checkout, and every conversation's file tools
mutate it concurrently. Isolation removes the enabler so the inference is never
needed. The rest of this doc is the concrete plan.

---

## 2. (a) Worktree lifecycle per conversation

### 2.1 Model

- One long-lived **integration branch** (default `tofu/integration`, env
  `TOFU_WORKTREE_INTEGRATION_BRANCH`) is the project's moving trunk that all
  conversations rebase onto and land into. **RATIFIED (Q1, owner 2026-07-11):**
  `tofu/integration` is a DEDICATED branch — autonomous merges NEVER land
  directly on the branch the human builds from. The human periodically
  fast-forwards `tofu/integration` into their working branch at their own
  cadence, giving a review / rollback seam and blast-radius isolation between
  the autonomous fleet and the human's trunk. Landing = CAS-merge into
  `tofu/integration` only (§5).
- Each **active conversation** that does project work gets its own git worktree
  and a per-conversation branch `tofu/conv/<conv_id>` created off the current
  integration HEAD.
- Worktrees live OUTSIDE the primary checkout, under a project-scoped state dir
  (`.tofu_worktrees/<conv_id>/`, prefix per §3.6 artifact convention; it is a
  `.tofu*` name so every existing consumer already ignores/excludes it). They
  share the one `.git` object store via `git worktree` — cheap, no full clone.

### 2.2 Lifecycle operations (new module `lib/conversations/project_worktree.py`)

| Op | Trigger | Behavior |
|---|---|---|
| `ensure_worktree(project, conv_id)` | first project tool call of a task, or dispatch | create off integration HEAD if absent; reuse if present; return abs path. Idempotent. |
| `sync_worktree(conv_id)` | task start / before land | rebase conv branch onto latest integration HEAD (fast-forward when possible; on conflict, surface as a normal conflict for the conv to resolve — NOT a silent park) |
| `land_worktree(conv_id, files, msg)` | replaces `project_commit.do_commit` | pre-flight gate the conv branch, then a REAL 3-way merge into integration under CAS (§5.1), RE-GATING the merge-result tree inside the CAS section before publishing the ref. Success = merge-result GATES GREEN (not just content-present). Conflict / red merge-result → report + resolve in conv worktree, never publish. |
| `release_worktree(conv_id)` | task/conversation end + soft-lease TTL | GC: prune the worktree, delete the conv branch IF fully merged; keep if it has unmerged commits (never lose work). |

### 2.3 GC — reuse the board's proven soft-lease model, do NOT invent a reaper

A worktree is held by a **soft lease** exactly like a path lease
(`DEFAULT_LEASE_TTL_MS`, `_effective_status` at-read-time expiry). A crashed /
abandoned conversation's worktree is reclaimable after one TTL. GC NEVER deletes
a branch with unmerged commits — it downgrades to "orphaned, awaiting human or
re-dispatch." No background thread; prune piggybacks on the existing sweep tick
(`sweep_dispatch`), mirroring `_prune_expired_leases`.

---

## 3. (b) Worktree-scoped file tools — the actual hard part

### 3.1 Why this is the risk, and why it is tractable

The owner's flag is correct: the server is one process, one cwd. **BUT** —
verified in code — the project file tools do NOT depend on `os.getcwd()`.
`project_path` is threaded as an explicit **parameter**:

```
config['projectPath']            (lib/scheduler/_shared.py:52 — per-conversation)
  → _handle_project_tool(..., project_path)        (handlers/project.py:188)
    → execute_tool(fn, args, project_path, ...)    (handlers/project.py:307)
      → _EXEC_HANDLERS[fn](args, base_path, ...)   (project_mod/tools.py:894)
        → _resolve_base(base_path, rel)            (project_mod/tools.py:271)
```

So worktree-scoping is a **path-resolution change, not a `chdir`**: resolve the
task's `project_path` to *that conversation's worktree root* at task start, and
every read/write/grep/apply_diff already flows against it with zero further
change. There is no shared mutable cwd for file I/O to fight over.

### 3.2 The one real subtlety: `run_command`

`run_command` (`lib/project_mod/run_command.py`) DOES spawn a shell with a
`cwd`. That cwd must be the conv's worktree, not the primary checkout. It
already receives `base_path` — route its `cwd=` to the resolved worktree root.
Its process-tree kill / snapshot / destructive-guard logic is worktree-agnostic
and unchanged.

### 3.3 Multi-root `_roots` registry hazard

`_resolve_base` consults a process-global `_roots` registry with a documented
"conv-state race workaround" (`project_mod/tools.py:314-328`) that can resolve an
unknown root to `base_path` anyway — a latent cross-root clobber (this is patch
register item #3). Under worktrees this becomes load-bearing: the registry MUST
be keyed per (conv_id / task), never a shared process global, or conv A's tool
could resolve into conv B's worktree. **This binding is a prerequisite of the
refactor, folded into §3.**

### 3.4 File-history / snapshots

`make_snapshot` records post-images per `conv_id` (`commit_round.py`). Under
worktrees these are still per-conv and still correct; they become a *diagnostic*
record rather than the *authorship source of truth* (which is now the branch).

---

## 4. (d) Dispatch-time file-ownership partitioning — shift coordination LEFT

Today the brain detects collisions at **land time** (the most expensive moment)
and parks. The board already knows epic → paths (`wait_paths`, `dispatch_target`
authorship). Move the coordination to **dispatch time**:

- **RATIFIED (Q2, owner 2026-07-11):** each epic DECLARES its intended
  **write-set** at post/dispatch time — the set of files it expects to touch —
  and the dispatcher partitions on THAT declared write-set, with a coarse
  subsystem tag (e.g. `css`, `routes/api_v1`, `lib/conversations`) as the
  fallback when a precise set can't be given. `wait_paths` alone is explicitly
  REJECTED as the source: it only captures declared *sibling-blocks*, not the
  epic's full write footprint, so it under-counts and lets two epics that will
  both edit `styles.css` co-dispatch. The declared write-set is a new epic
  field (`write_set` JSON), set by `post_task` / `dispatch_epic`.
- `select_dispatchable` prefers epics whose declared write-set is **disjoint**
  from every currently-claimed epic's write-set — so two *live* conversations
  rarely touch the same file by construction. Missing write-set → fall back to
  the subsystem tag; missing both → treat as touching-everything (dispatch
  serially, conservative).
- Overlap becomes a **scheduler constraint** (don't co-dispatch two epics that
  both own `styles.css`), NOT a land-time jail. If ownership sets must overlap
  (unavoidable hot file), the epics are *serialized* at dispatch — one waits for
  the other to land — which is the correct, bounded behavior.

Net: landing conflicts go from the common case to the rare case, and the rare
case is a real, finite merge conflict resolved inside the owning conversation's
worktree — never a permanent stall.

**This is the piece that actually cures the reported symptom.** Isolation makes
conflicts *survivable*; dispatch-time partitioning makes them *rare*.

---

## 5. (c) Integration-branch land flow (replaces the byte-identity gate)

```
land_worktree(conv_id, files, test_paths, message):
  1. sync_worktree(conv_id)        # rebase conv branch onto integration HEAD
       └─ conflict? → report to conv, resolve in-worktree, retry (no park)
  2. run_acceptance_gate(conv_worktree, ...)   # PRE-FLIGHT on the conv branch:
       └─ fail fast if the conv's own work is red. Necessary, NOT sufficient.
  3. not ok? → return why (tests red / orphaned callers). Do NOT merge.
  4. ok? → CAS-serialized REAL 3-way merge into tofu/integration (§5.1), and
           inside that critical section RE-RUN the acceptance gate ON THE
           MERGE-RESULT tree before publishing the ref. Success = the
           merge-result GATES GREEN (not just "files present", not "reachable").
           A red merge-result routes to conflict resolution in the conv's
           worktree — the broken merge is NEVER published to the ref.
  5. STOP. The human fast-forwards tofu/integration → their build branch at
     their own cadence (Q1). Autonomous landing NEVER writes the human's trunk.
```

> ⚠️ **The gate must run on the tree that PUBLISHES (owner catch 2026-07-11).**
> Step 2 gates the conv branch in ISOLATION; that is necessary (fail fast) but
> NOT sufficient. Two branches that each pass their own gate can merge
> textually-clean into a semantically-broken integration — e.g. conv A removes
> `foo()` (green: nothing calls it on A), conv B adds `lib.foo()` in a different
> file (green: `foo` still exists on B), the diffs don't overlap so `git merge`
> succeeds with zero conflict, and `tofu/integration` is now RED with a broken
> reference NO pre-merge gate ever saw. **Content-preserved ≠ integration-correct.**
> The gate on the merge-result (step 4) is the only thing that catches this; it
> runs inside the CAS section so the ref is published ONLY if the merged tree is
> green. On a lost CAS, re-merge AND re-gate (never re-CAS a stale/ungated tree).
> Proven on the real mount (§7.1 Scenario C).

- **Authorship** is the merge commit's parentage — structural, not inferred.
- **No contamination possible**: the conv branch contains only that conv's
  commits by construction.
- **The gate runs TWICE**: pre-merge on the conv branch (fail fast, step 2) and
  — the load-bearing one — on the merge-result inside the CAS section (step 4).
  The same `run_acceptance_gate` (green tests AND `selfConsistent` orphan scan);
  the merge-result run is what makes the published trunk provably correct.
- Agent-authored merges keep `--author 'Tofu Agent'` (the existing
  `_agent_author()` convention); the human remains committer.

### 5.1 Integration-ref land serialization — the primitive the file model needs

**Per-conv worktrees move collision off the FILES but reintroduce it on the
INTEGRATION REF** (owner's catch). N worktrees share one `.git`; two
conversations merging into `tofu/integration` at the same instant race on the
single ref update. Without serialization, "merge the branch" just relocates the
stall to `refs/heads/tofu/integration`.

**This is measured, not assumed** (§7.1): a BLIND concurrent `update-ref new`
loses 7 of 8 updates (last-writer-wins → silent lost merges). The fix is a REAL
3-way merge whose *result tree* is committed under git's native compare-and-swap
plus a bounded retry, NOT a home-grown lock.

> ⚠️ **CORRECTED PRIMITIVE (owner catch 2026-07-11).** The first draft used
> `commit-tree <conv-tree> -p OLD -p conv_branch`. That does NOT merge: it
> fabricates a commit whose TREE is the conv's OWN tree while merely *pointing*
> at `OLD` as a parent. Ancestry links up (so an `--is-ancestor` / reachability
> check passes for ALL landers — a FALSE green), but each lander's tree
> OVERWRITES the previous lander's — measured: 8 distinct-file landers → final
> tree held only `base.txt` + the last winner's file. **Seven of eight landers'
> content silently discarded, reported as success.** That is data loss, worse
> than the stall. The land MUST compute a genuine merged tree.

```
CAS-merge(conv_branch):                    # the land critical section
  for attempt in 1..MAX_LAND_RETRIES (50):
    OLD = rev-parse refs/heads/tofu/integration
    if is-ancestor(OLD, conv_branch):      # trivially fast-forwardable
        NEW = conv_branch tip              # tree already includes OLD's content
    else:                                  # true divergence → REAL 3-way merge
        lw = git worktree add --detach <scratch> OLD    # throwaway land-worktree
        if NOT git -C lw merge --no-edit conv_branch:   # genuine content merge
            git -C lw merge --abort; rm -rf lw; prune
            return CONFLICT → sync_worktree(step 1): resolve in the CONV's
                              worktree, never on integration (no clobber)
        if NOT run_acceptance_gate(lw):     # *** GATE THE MERGE-RESULT ***
            rm -rf lw; git worktree prune
            return CONFLICT → resolve in the CONV's worktree; NEVER publish a
                              red merge-result to the ref
        NEW = git -C lw rev-parse HEAD     # the MERGE-RESULT tree (all content,
                                           # and PROVEN green as an integration)
        rm -rf lw; git worktree prune
    if update-ref refs/heads/tofu/integration NEW OLD:   # CAS: only if unchanged
        return landed
    # lost the CAS race: integration moved under us → re-read OLD, RE-MERGE and
    # RE-GATE against the new tip (never re-CAS a stale/ungated tree)
    jittered-backoff()
  return land_exhausted → post a board block, do not force
```

- **The merge is a REAL `git merge`** in a throwaway detached land-worktree
  checked out at `OLD`, so `NEW` is a true 3-way merge tree containing EVERY
  landed change, and a genuine same-file conflict falls out of `git merge`
  (→ resolved in the conv's own worktree, never silently "last-tree-wins").
  `git merge` (not `merge-tree --write-tree`, absent on git 2.11) is the
  2.11-compatible way to get a real merged tree. The scratch worktree is
  detached + immediately pruned, so it never collides with a live conv edit.
- **CAS is the serialization** — `git update-ref <ref> <new> <old>` fails
  atomically if the ref moved since we read `OLD`; exactly one writer wins per
  round and the losers RE-READ `OLD`, RE-MERGE (against the new tip), and retry.
  This is a much lighter primitive than a board soft-lease on the ref and needs
  no external lock; the board lease is reserved for the *worktree* (GC), not the ref.
- **Re-merge on every retry, not re-CAS the stale tree** — because a lost CAS
  means integration advanced, the losing lander MUST recompute its merge on the
  new `OLD` (the loop re-enters at the top), or it would reintroduce the
  last-tree-wins clobber at the ref layer. This is the subtle invariant the
  broken draft violated.
- **CONTENT-verified, measured (§7.1):** 8 concurrent distinct-file landers →
  8/8 landed, `git ls-tree` shows ALL 8 files + base present (no loss); a
  same-file conflict → exactly one lands, the other REPORTS conflict (not
  clobbered), integration holds one intact winner; ≤8 retries, 0 exhausted,
  fsck clean.
- **The acceptance gate runs TWICE**: a pre-flight on the conv branch (cheap
  fail-fast, outside the critical section) AND — the load-bearing one — on the
  MERGE-RESULT tree inside the CAS section, because content-preserved ≠
  integration-correct (§5, Scenario C). The critical-section work is therefore
  merge + gate; keep the gate scoped to the affected tests so the window stays
  short (emit the per-land duration metric — Q5 — so a slow merge-gate is
  visible). On a lost CAS the loser re-merges AND re-gates against the new tip.
- **Exhaustion is a real (rare) outcome** — 50 failed CAS rounds means
  pathological contention; it posts a board block (class `[sibling]`, retried by
  Tier-2 event-driven wake), never a forced blind write.

---

## 6. (e) Env-gated rollout seam — single-box stays byte-identical

Mirror `lib/rate_limit_store.py` exactly (lazy `get_store()` + `reset_for_test()`,
env-selected backend). New env: **`TOFU_WORKTREE_ISOLATION`**.

- **`inproc` (default, OFF):** byte-identical to today. `project_path` resolves
  to the primary checkout; `project_commit`/`project_ready` behave exactly as
  now. A single-box / desktop install sees ZERO change. This is the fail-open
  default and the release-safety guarantee.
- **`on` (opt-in):** `ensure_worktree` resolves `project_path` per conversation;
  `land_worktree` replaces `do_commit`; dispatch-time partitioning active.

The seam sits at path-resolution + the land verb, so flipping the flag is a
localized behavior swap, not a fork of the tool surface. Both modes run the SAME
acceptance gate.

---

## 7. (f) FUSE / DolphinFS validation plan — validate BEFORE the refactor

This is where it breaks, if it breaks. Validate `git worktree` semantics on the
actual DolphinFS/FUSE mount before committing to the refactor:

| # | Check | Method | Pass criterion |
|---|---|---|---|
| V1 | `worktree add --detach` works on FUSE | Already proven — `run_acceptance_gate` uses it in production today | ✅ pre-confirmed; re-confirm under concurrency |
| V2 | N concurrent worktrees sharing one `.git` | Script: create 8 worktrees, each commits, in parallel | no `.git/worktrees/*` lock corruption; all commits land |
| V3 | `.git/worktrees/<id>/` lock-file behavior on FUSE | Stress `git commit` in two worktrees simultaneously | no `index.lock` deadlock / stale lock; retry-clean |
| V4 | `worktree remove` + `prune` cleanup on FUSE | GC 100 worktrees | no leaked dirs; `git worktree list` clean |
| V5 | rebase/merge across worktrees on FUSE latency | time `sync_worktree` under cross-DC mount | within `cross_dc.py` tolerance; no corruption |
| V6 | worktree path resolution under the multi-root `_roots` fix (§3.3) | unit + integration: conv A tool cannot resolve into conv B worktree | strict per-conv isolation |

**Gate:** V2–V4 RAN FOR REAL on the `beegfs-fuse` mount 2026-07-11 (see §7.1) →
**PASS**. The `git worktree` model survives concurrent FUSE writes + concurrent
integration-ref merges. **No fallback to lighter isolation is needed.** V5–V6
remain to be exercised during the build (they gate their own steps, not the
go/no-go).

### 7.1 Empirical results (2026-07-11, `beegfs-fuse` / git 2.11.0)

Probe: 8 worktrees off one `.git`, 80 concurrent commits, then concurrent
integration-ref land under two strategies. All `.git` on the FUSE mount.

| Check | Result |
|---|---|
| **V2** 8 worktrees / one `.git`, 80 concurrent commits | **PASS** — 8/8 add, 80/80 commit, 0 bad branch tips |
| **V3** lock behavior under concurrency | **PASS** — 0 stale `.lock`, no `index.lock` deadlock |
| **V4** GC | **PASS with constraint** — `worktree remove` is UNSUPPORTED on git 2.11; GC = `rm -rf <wt>` + `git worktree prune` (verified clean) |
| **REF blind** `update-ref new` (no CAS) | **FAILS by design** — only 1 of 8 survives (silent lost merges). NEVER use. |
| **REF CAS** `commit-tree`-fake-merge + `update-ref new old` | **FALSE GREEN — data loss** — 8/8 "landed" + all reachable, but final tree held only base + last winner's file; 7/8 landers' content silently discarded (owner catch) |
| **REF CAS** REAL 3-way merge (`git merge` in scratch land-worktree) + `update-ref new old` + retry (§5.1) | **PASS, CONTENT-verified** — 8 distinct-file landers → 8/8 landed, `ls-tree` shows ALL 8 files + base (no loss); same-file conflict → one lands + one REPORTS conflict (not clobbered); ≤8 retries, 0 exhausted, fsck clean |
| **GATE on merge-result** (Scenario C: A drops `foo()`, B adds a call in another file — textually clean, semantically broken) | **PASS** — both branches GREEN individually; A lands, B's textually-clean merge GATES RED on the merge-result → land REFUSES to publish; `tofu/integration` ref UNCHANGED, trunk stays GREEN. A "files present" check would have passed both files (proves present ≠ correct) |
| "corruption" seen on first pass | **reflog garbling ONLY**, not DAG/object/ref corruption — eliminated by `core.logallrefupdates=false` on the server repo |

**Three hard environment constraints this surfaced (bind the build):**
1. **git is 2.11.0** — NO `merge-tree --write-tree`, NO `worktree remove`. Land
   MUST compute a REAL merged tree via `git merge` in a throwaway detached
   land-worktree, then CAS `update-ref new old` to the merge-result (§5.1) —
   NEVER `commit-tree <conv-tree>` (that fabricates ancestry without merging
   content = the discarded-content data-loss bug). GC MUST use `rm -rf` +
   `worktree prune`. A newer git (`merge-tree --write-tree`, `worktree remove`)
   would simplify this but is not assumed.
2. **The server `.git` MUST set `core.logallrefupdates=false`.** Per-ref reflogs
   are the ONLY artifact that garbles under concurrent FUSE appends; with them
   off, `fsck --connectivity-only` is clean after 80 concurrent commits + 8
   concurrent CAS merges. (A dangling reflog is not DAG corruption, but turning
   it off removes the noise and the (harmless) fsck warnings entirely.)
3. **Integration-ref land is CAS-with-retry, never blind** — the blind path
   loses 7/8 merges silently. §5.1 is not optional; it is the primitive that
   makes the whole "merge the branch" story not relocate the stall to the ref.
4. **The land must do a REAL 3-way merge and the gate must be CONTENT-verified,
   never reachability.** `commit-tree <conv-tree>` links ancestry without
   merging trees → last-tree-wins data loss that a reachability/ancestor check
   reports as SUCCESS. The land runs `git merge` in a scratch detached
   land-worktree and the probe asserts every lander's file is in the final
   `ls-tree`; a lost CAS forces a RE-MERGE against the new tip, not a re-CAS of
   the stale tree.
5. **The acceptance gate MUST run on the MERGE-RESULT tree, inside the CAS
   critical section, before publishing the ref — not only pre-merge on the conv
   branch.** Two branches that each pass their own gate can merge textually-clean
   into a semantically-broken trunk (§5 example / §7.1 Scenario C). A pre-merge
   gate is necessary (fail fast) but a FALSE GREEN on its own; content-preserved
   ≠ integration-correct. Red merge-result → route to conflict resolution, never
   the ref. On a lost CAS, re-merge AND re-gate.

Probe scripts kept at `/tmp/fuse_worktree_probe*.sh` (not committed — throwaway
validation harness; the results + constraints live here). `probe4` is the
gate-on-merge-result Scenario C. Re-runnable.

---

## 8. Build order (proposed — ratify before code)

0. **FUSE validation V2–V4** (§7.1) — DONE 2026-07-11, PASS. (V5 rebase-latency
   / V6 per-conv `_roots` isolation are exercised inside steps 3/2 respectively.)
1. `lib/conversations/project_worktree.py` — lifecycle primitives, dark, behind
   `TOFU_WORKTREE_ISOLATION=inproc` (no behavior change), with the soft-lease GC.
   Sets `core.logallrefupdates=false` on the shared `.git` (§7.1 constraint 2);
   GC via `rm -rf` + `worktree prune` (§7.1 constraint 1, git 2.11).
   **LANDED 2026-07-12 (`e313197`).** The module ships the full lifecycle
   (`ensure_worktree`/`sync_worktree`/`release_worktree`/`gc_worktrees`) AND the
   CAS-with-retry land primitive from step 4 (`land_worktree` + `_merge_and_gate`)
   in one dark unit, so steps 1 and 4's core algorithm are both in-tree behind
   the flag; step 4 remains open for its WIRING (making `land_worktree` the
   `on`-mode land verb in the tool surface). 16 tests green in a fresh-HEAD
   worktree gate (OFF-mode inertness, content-verified two-lander merge,
   same-file conflict reported-not-clobbered, Scenario-C broken-merge-never-
   published + direct merge-result-gate red, GC unmerged-safety). Two git-2.11
   findings folded in that the paper design had not yet pinned: (a)
   `--no-optional-locks` (an OpenCode FUSE-hardening flag) was only ADDED in git
   2.15 → on 2.11 it aborts every call with rc=129 "Unknown option", so it is
   feature-detected once and included ONLY when supported; `-c core.fsmonitor=false`
   (universally accepted) carries the FUSE lock hardening on the 2.11 baseline.
   (b) `git worktree remove` absent on 2.11 → `_prune_worktree_dir` tries it then
   falls back to a retry-on-EBUSY `rm -rf` + `worktree prune` (OpenCode's
   network-FS rm-retry pattern).
2. Per-conv `_roots` binding fix (§3.3) — prerequisite, independently testable
   (this IS V6). **LANDED 2026-07-12 (`daedc34`).** The per-conv registry
   (`_conv_roots`, 2026-05) already gave strict isolation *when a conv had a
   registry*; the residual §3.3 hazard was the process-global `_roots` /
   `_state['path']` FALL-THROUGH that survives when a conv lacks one — under
   worktrees that global tree IS the shared primary checkout / another conv's
   worktree. Gated behind `TOFU_WORKTREE_ISOLATION` (lazy
   `project_worktree.is_isolation_enabled`, fails closed to OFF):
   `resolve_namespaced_path` + `get_conv_roots` no longer fall through to the
   global registry/primary for a conv-scoped call under isolation (raise
   `UnknownWorkspaceRootError` / return `{}` — fail closed), and the `tools.py`
   self-heal only heals to `base_path` when it is a REGISTERED root of THIS conv.
   OFF = byte-identical (legacy fall-through intact). `tests/test_worktree_roots_isolation.py`
   (11) + 26/26 through the fresh-HEAD gate (V6 + readonly-roots + conv-eviction),
   self-consistent, 0 orphans.
3. Worktree-scoped path resolution (§3.1) + `run_command` cwd (§3.2), flag-gated
   (rebase latency here IS V5). **HELPER LANDED 2026-07-12 (`0a553f6`); WIRING
   HELD (sibling-contaminated files).** The core seam is
   `project_worktree.scoped_base_path(project_path, conv_id)`: under isolation
   it resolves the base that FILE TOOLS + `run_command` cwd operate against to
   the conv's own worktree (via `ensure_worktree`), else returns `project_path`
   unchanged (OFF = byte-identical; fail-open to primary on error). Because the
   tools thread `project_path` as an explicit PARAMETER (no `os.getcwd()`
   dependence), this ONE helper covers reads/writes/grep/apply_diff + the
   `run_command` cwd — a path-resolution swap, not a chdir. It scopes ONLY the
   tool base; presence/feed/board/modifications keep the original
   `project_path`. `tests/test_worktree_scoped_base.py` (7) + 34/34 fresh-HEAD
   gate. The WIRING (calling the helper at the two tool-execution seams —
   `lib/tasks_pkg/handlers/project.py` sync handler + `streaming_tool_executor.py`
   pre-exec) is coded + tested on disk but HELD: both files currently carry a
   sibling conversation's uncommitted work, so landing them would sweep foreign
   hunks. It lands the moment the sibling commits those two files.
   **WIRING LANDED 2026-07-12 (`39b81c0`, owner-directed wholesale commit** — no
   live peers, whole tree gate-green 117/117, so the two hot-path files were
   committed WHOLE, knowingly sweeping in the uncommitted sibling hunks they
   carried). Both tool-execution seams now call `scoped_base_path` under
   isolation.
4. `land_worktree` land flow (§5) + the CAS-with-retry integration-ref primitive
   (§5.1, REAL `git merge` in a scratch detached land-worktree → acceptance gate
   ON THE MERGE-RESULT tree inside the CAS critical section → `update-ref new
   old`; a red merge-result routes to conflict resolution, never the ref); the
   pre-merge gate on the conv branch is reused but is NOT sufficient alone.
   `project_commit` becomes the `inproc`-mode path only.
   **LANDED 2026-07-12 (`4280c5b`).** `commit_worktree` (safe `git add -A`
   inside the isolated worktree — nothing to contaminate by construction) +
   `execute_land_tool` (commit-in-worktree → `land_worktree` CAS-merge, gating
   the merge-result; conflict / red-merge / preflight-red REPORTED, never forced
   onto the ref). `project_board.execute_board_tool` routes `project_commit` →
   `execute_land_tool` when `TOFU_WORKTREE_ISOLATION=on`, else the byte-identity
   `project_commit` (OFF default = byte-identical; fail-open to project_commit on
   any routing error). `tests/test_project_worktree.py` +5 (commit/land/conflict).
5. Dispatch-time write-set partitioning (§4) in `select_dispatchable` — add the
   epic `write_set` field to `post_task` / `dispatch_epic`.
   **CORE LANDED 2026-07-12 (`39385a3`).** `project_tasks` gains a `write_set`
   JSON column (schema v39→40, byte-equivalent PG+SQLite, parity-tested, in
   `_CRITICAL_COLUMNS` both backends). `post_task(write_set=)` + a new
   `set_write_set()` helper declare an epic's footprint; `select_dispatchable`
   PREFERS a candidate whose write_set is DISJOINT from every live-claimed
   epic's — a stable disjoint-first reorder. SOFT preference, never a hard filter
   (a conflicting epic still dispatches last → no stall); an empty/undeclared
   write_set is "unknown footprint" → non-conflicting → never demoted
   (fail-open); `_paths_intersect` handles directory-containment overlap in
   either direction. `tests/test_project_write_set.py` (9, incl. NC-WS) + 116/116
   fresh-HEAD gate (write-set + board + dispatch + schema-parity). The board-TOOL
   arg (a `write_set` field on the post_task tool spec in `lib/tools/conversation.py`)
   is HELD — that file carries a sibling's uncommitted work; the backend already
   accepts write_set via `post_task`/`set_write_set`, the tool-surface arg lands
   when the sibling commits. **TOOL-ARG LANDED 2026-07-12 (`39b81c0`, part of the
   owner-directed wholesale commit):** `write_set` is now on the
   `project_board_post` tool spec and routed through `execute_board_tool` →
   `post_task(write_set=)`.
6. Retire (§0.1) the byte-identity gate + overlap-hold once (1)–(5) are green
   under the flag and the flag defaults `on` — behind the strangler-fig
   invariant (legacy path stays until every consumer is migrated).

No step starts before the prior is green. Each step is env-gated OFF by default
until the whole chain is validated.

---

## 9. Relationship to Tier-2 bleeding-control (parallel, NOT a substitute)

Two small corrections land immediately in the CURRENT model to stop the
oscillation while §8 is built. They are explicitly interim:

- **(a) class-aware backoff** — a `[sibling]` block tracks the lease clock (retry
  at ~lease-release, no escalation); the exponential curve
  (`_block_cooldown_ms`) is reserved strictly for `[human-gated]`. A
  collaboration event must never ratchet an epic toward a 24 h sleep.
- **(b) event-driven wait_paths** — an epic waiting on a sibling path re-checks
  when the lease releases, not on a separate exponential sleep, removing the
  "sibling freed the path in 30 min but the epic still sleeps ≥1 h" latency gap.

These reduce pain in the shared-tree model; they do NOT remove the shared tree.
Worktree isolation (§0–§8) remains THE fix.

---

## 10. Open questions

1. ~~**Integration branch identity**~~ — **RESOLVED (Q1):** dedicated
   `tofu/integration`; the human fast-forwards it into their build branch at
   their own cadence. Autonomous landing never writes the human's trunk. (§2.1, §5)
2. ~~**Ownership-set source**~~ — **RESOLVED (Q2):** each epic declares its
   intended write-set at post/dispatch; the dispatcher partitions on it, with a
   coarse subsystem tag as fallback. `wait_paths` alone is rejected (too narrow). (§4)
3. ~~**FUSE fallback threshold**~~ — **RESOLVED empirically (§7.1):** V2–V4 PASS
   on the real mount; no fallback needed. The bounded-retry primitive is CAS on
   the integration ref (§5.1), not a commit retry, and it is required regardless.
4. ~~**Worktree disk budget**~~ — **RATIFIED (Q4, owner 2026-07-11):** soft cap =
   max concurrent dispatched convs; LRU-GC idle worktrees past their lease. The
   shared object store makes the marginal cost mostly working copies —
   acceptable. (§2.3 GC tuning.)
5. ~~**Ref-land contention ceiling**~~ — **RATIFIED (Q5, owner 2026-07-11):**
   `MAX_LAND_RETRIES=50` + jittered backoff is fine at current width. Because
   each retry now re-runs a real `git merge` AND a re-gate (constraint 5), the
   land MUST emit a per-land retry-count + duration metric so an expensive
   large-diff critical section is visible; go adaptive ONLY if the metric says
   so. (Measured ≤8 retries at 8-way, 0 exhausted, CONTENT-verified.)

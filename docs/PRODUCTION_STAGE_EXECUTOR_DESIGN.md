# Stage executors — the seam for external compute (hope MCP, and whatever follows)

**Status:** design only. Nothing here is implemented. Written *before* R5/R6 so
those recipes are authored against a contract instead of reaching for a
subprocess.

**Owner directive this serves:** "when rendering tables and running experiments
later on, we can use hope's mcp … think more about reusing tofu rather than
developing from scratch."

---

## 1. The gap, measured

`lib/production/stages.py` defines the whole contract as:

```python
Stage(name, run, gate, retry, resumable)
```

There is **no place to say "this step needs compute that does not live in this
process."** Measured 2026-07-28: `grep -rn "mcp" lib/research/ lib/production/stages.py`
returns **zero**. The research recipe is three local stages
(`harvest`/`survey`/`ideate`).

So when R5 (render result tables) and R6 (run validation experiments) arrive,
the path of least resistance is to call the MCP bridge — or worse, `hope` via
`subprocess` — from inside a recipe's `run()`. That is the "build rather than
reuse" shape the charter rules out, and it would be the *fourth* private copy
of a capability the platform already owns.

**What already exists and must be reused, not re-created:**

| Capability | Existing single source |
|---|---|
| Call an MCP tool | `lib/mcp/client/_bridge.py::call_tool(namespaced_name, arguments)` |
| Discover connected servers / tools | `lib/mcp/registry.py` |
| Redact credentials in config + errors | `lib/mcp/transport.py::redact_config`, `MCPConnectError._format` |
| Report "produced, but a capability was missing" | `task['artifact_quality']` (`degraded` + `degraded_reason`) |

hope is already an installed MCP server (`hope_*` tools: `submit_job`,
`get_status`, `watch_job`, `get_queue_resource`, …). **No new transport, no new
credential store, no new client is needed** — only a declared seam.

---

## 2. The contract

### 2.1 One new optional field

```python
@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[dict], Any]
    gate: Optional[Callable[[dict, Any], list]] = None
    retry: int = 0
    resumable: bool = True
    executor: Optional[str] = None      # NEW
```

`executor=None` (the default) means **pure local computation** — every stage
that exists today. A non-empty value *declares* that the stage wants external
compute, in the form `"<kind>:<id>"`:

| Value | Meaning |
|---|---|
| `None` | local (default) |
| `"mcp:hope"` | needs the `hope` MCP server's tools |
| `"mcp:<server_id>"` | needs some other MCP server |

The value is a **declaration, not a dispatch mechanism.** The runner does not
call MCP; it resolves *availability* and hands the stage a handle. This keeps
`stages.py` capability-agnostic — a guard already AST-asserts it imports no
motion_video/tts/llm/paper/audio module, and that guard must keep passing.

### 2.2 Resolution happens in the runner, once, before `run()`

```python
# lib/production/executors.py  (new, small)
def resolve_executor(spec: str | None) -> ExecutorHandle:
    """Return a handle describing whether `spec` is usable RIGHT NOW."""
```

```python
@dataclass(frozen=True)
class ExecutorHandle:
    spec: str | None          # what was asked for
    available: bool           # can we actually reach it?
    reason: str               # why not, when available is False
    call: Callable | None     # bound MCP call_tool, or None
```

`run(ctx)` reads the handle from `ctx['executor']`. **A stage must never import
the MCP bridge itself** — that is what makes the seam swappable (a future
`"k8s:"` or `"slurm:"` kind changes one resolver, not N recipes).

---

## 3. Degradation is part of the contract, not a `try/except` at the call site

This is the load-bearing decision, and it is the same shape as the
`artifact_quality` axis already committed for research.

**Rule: an unavailable executor MUST NOT fail the stage.** It must produce a
*complete artifact with one capability missing*, and say so.

| Stage | Executor available | Executor missing |
|---|---|---|
| R5 tables | report **with** rendered result tables | report **without** tables, prose intact, `degraded_reason='tables skipped: hope MCP unavailable'` |
| R6 experiments | claims **with** measured validation | claims marked *unvalidated*, methodology intact, `degraded_reason='validation skipped: …'` |

Rationale — three distinct states that a boolean cannot express:

1. **executor unreachable** → infra/credential problem. Not the work's fault.
   Deliver the rest. `degraded`.
2. **experiment ran and failed** → a real scientific result (the hypothesis did
   not hold). **Not** degraded — that is a finding, and flagging it as a defect
   would be a false attribution of exactly the kind the charter warns about.
3. **stage itself crashed** → `StageFailed`, retry, then a real error.

Collapsing 1 and 2 would tell the user "your experiment failed" when the truth
is "we never ran it," and collapsing 1 and 3 would throw away a usable report
because a queue was busy.

The verdict rides the existing field — **no new status enum member**
(`status` is the lifecycle axis; quality is a separate axis, already decided):

```python
runtime.finish(task_id, result=..., degraded=True,
               degraded_reason='tables skipped: hope MCP unavailable')
```

---

## 4. Why `executor` is declared on the Stage rather than discovered inside `run()`

Because the runner can then answer **before spending any money**:

> "This job wants `mcp:hope`, which is not connected. Everything will still be
> produced, minus tables."

That message belongs in the job's first progress event, not in a surprise at
minute 40 of a harvest. A stage that discovers its own unavailability can only
report it *after* the expensive stages ahead of it have already run.

It also makes the dependency **greppable**: `grep -rn "executor=" lib/*/recipe.py`
lists every external-compute dependency in the codebase. Buried
`bridge.call_tool(...)` calls are not enumerable.

---

## 5. Guards to write **with** the implementation (not before)

```python
def test_unavailable_executor_degrades_rather_than_fails():
    """The artifact is still produced; the job is degraded, not failed."""
    # a Stage declaring executor='mcp:nonexistent'
    # → run() completes, gate passes, result['degraded'] is True,
    #   result['degraded_reason'] names the executor.

def test_available_executor_is_not_marked_degraded():
    """Complement — without this, 'always degrade' passes the test above."""

def test_experiment_that_ran_and_refuted_is_not_degraded():
    """A negative RESULT is a finding, not a pipeline defect."""

def test_stages_module_still_imports_no_capability_module():
    """The existing AST guard must keep passing — resolution lives in
    executors.py, and stages.py stays capability-agnostic."""
```

NEUTER for each: make `resolve_executor` always report available → test 1 red;
make it always report unavailable → test 2 red; mark a refuted experiment
degraded → test 3 red.

---

## 6. Explicitly NOT in this design

- **No implementation.** hope's tool signatures (`submit_job` arguments, result
  shape) must be read from the live server when R5/R6 is built — writing them
  from memory now would bake in a guess, and this project has been burned by
  exactly that (guessed entry points that produced tests passing by doing
  nothing).
- **No retry/backoff policy for external jobs.** hope jobs are minutes-to-hours;
  that is a queue-watching problem (`hope_watch_job`), not the stage `retry`
  count, which exists for gate failures.
- **No new credential path.** hope MCP auth is already the MCP bridge's
  problem and already redacted.

---

## 7. Ticket

Open as `[design, extension-point]`, blocked until R5 or R6 actually starts —
implementing a seam with no caller is how a speculative abstraction gets the
shape wrong. The value delivered *today* is that R5/R6's author starts from
this contract instead of from `subprocess.run(['hope', ...])`.

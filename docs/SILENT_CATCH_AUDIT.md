# Silent-catch audit — evidence for epic `pt_98a4e0c2eece4cad`

Snapshot taken on a clean `git worktree` of HEAD `f672f019` (2026-07-27).

**The count moves.** It was 58 when the epic was filed and 97 a few hours
later, on the same guard with no change to it — siblings keep landing code and
`test_code_quality` is RED, so nothing stops a new unlogged catch from
arriving. Re-run the inventory below before acting on any number here; the
*classification* is the durable part, not the totals.

```bash
# Regenerate the inventory (clean worktree recommended — a dirty tree mixes in
# sibling WIP and you will misattribute it, which has already happened once).
python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location('tcq', 'tests/test_code_quality.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for d in (m.LIB_DIR, m.ROUTES_DIR):
    for rel, tree in m._parsed_trees(d):
        for cls in (m._SilentCatchFinder, m._AssignSilentCatchFinder):
            f = cls(); f.visit(tree)
            allow = (m.TestSilentCatches.ACCEPTABLE_SIGS if cls is m._SilentCatchFinder
                     else m.TestAssignmentSilentCatches.ACCEPTABLE_SIGS)
            for x in f.issues:
                if m._finding_sig(rel, x) not in allow:
                    print('%s:%d\t%s\tin %s()' % (rel, x['lineno'], x['exc'], x['func']))
PY
```

## The findings split in two, and the halves need OPPOSITE treatment

Sorting by the caught type separates them cleanly:

| Class | Share | What it is | Right fix |
|---|---|---|---|
| **Probe-shaped** — catches ONLY `OSError` / `ValueError` / `TypeError` / `IndexError` / `KeyError` / `JSONDecodeError` / `AttributeError` … | ~72% | Reading `/proc`, parsing an env var, `os.getsize` on an optional file. The read failing IS the return value ("this machine has no cgroup"), not an error. | NOT a per-site `logger.debug` — see below. |
| **Broad / domain** — `except Exception`, or a domain error like `ProjectError` / `TTSError` / `_LlmFailed` / `NarrationAborted` | ~28% (27 unique sites) | A real failure being swallowed. | Root-fix each one. Independent of any design decision. |

## Why the probe half must not get per-site logs

`lib/cgroup_guard.py` is the clearest specimen (15 findings, the single
largest file). Its module docstring states the contract outright:

> Everything degrades to a **NO-OP** when the cgroup / /proc is unreadable
> (bare metal, macOS, restricted sandbox)

Those 15 catches fire **100% of the time on any non-Linux host**. A
`logger.debug` on each would emit a dozen lines every 30s monitor tick saying
"still not Linux". And the information is already reported — at the boundary,
once, by the caller:

* `startup_self_check()` → `'[cgroup] self-check: cgroup memory unreadable — no-op'`
* `start_monitor()` → `'[cgroup] monitor not started — cgroup memory unreadable'`

So the "narrow the except + log once at the caller's boundary" shape the epic
proposed as a possible fix is **already implemented here**. The guard simply
cannot see it, because it has no notion of "this function is a probe".

## Root cause: the exemption ladder is missing its middle rung

`tests/test_code_quality.py` offers exactly two ways to say "this catch is
fine":

1. `_EXEMPT_EXC_TYPES` — a global by-exception-type pass (`ImportError`,
   `CancelledError`, …). Too coarse: `OSError` cannot go here, it is a real
   error almost everywhere else.
2. `ACCEPTABLE_SIGS` — naming one `(file, function, exc-tuple)` at a time.
   Too fine for ~70 sites; the epic explicitly forbids inflating it from 40 to
   90 entries because that dilutes the guard's discrimination.

Missing: **"this FUNCTION is an environment probe; a narrow read failure is
its return value, and its caller reports the outcome."** Without that rung the
only options are noise or dilution — which is why this epic is question-blocked
rather than half-fixed.

## The 27 that need root-fixing regardless

These are broad or domain-specific swallows — real signal, no design decision
required. Several look like live defects rather than debt:

* `routes/config.py:299` `get_server_config()` — `except Exception`
* `lib/llm/_sse_core.py:1132` `finalize()` — `except Exception`
* `routes/common.py:147` `_request_user_id()` — `except Exception`
* `lib/netpath.py:174 / :284 / :336` — `except Exception` ×3
* `lib/desktop_agent/_exec.py:126 / :201` — `except Exception` ×2
* `lib/swarm/agent.py:966` `_run_loop()` — `except _LlmFailed`
* `lib/paper/survey.py:196` `_load_paper_inputs()` — `except Exception`
* `lib/tasks_pkg/manager/_stream.py:250` `_on_waiting()` — `except Exception`
* … full list from the inventory command above.

Config loading, SSE finalisation and user-identity resolution failing
*silently* is exactly the "behaves oddly, nothing in the log" class CLAUDE.md
§2 exists to prevent.

**Sibling boundary:** `lib/motion_video/` findings are owned by
`pt_c42462c449124aeb` — leave them to that epic.

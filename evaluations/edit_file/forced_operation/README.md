# editbench-4cbf7e4a: forced-operation edit benchmark (edit_file / write_file)

This directory is the committed copy. Raw per-call records (`raw/*.jsonl`, 9 MB),
rebuildable task files and repository clones stay in the run directory
`/tmp/editbench-4cbf7e4a`; `MANIFEST.sha256` pins every one of them.
Analysis outputs are under `results/` here (`out/` in the run directory).

Re-carry of lane 2c8c6447. Pre-registration: `PREREG.md` (also committed in the
Tofu worktree at `evaluations/edit_file/forced_operation/`, commits 306ab0c06b,
5becb15695, 5d22aff333). Findings log: `FINDINGS.log`.

## Layout

| path | content |
|---|---|
| `scripts/` | task builders, arm builders, executors, runners, analysis |
| `tasks/tasks.jsonl`, `tasks/arms.json`, `tasks/oracle.jsonl` | Exp 1 task set, frozen arms, oracle floor |
| `tasks/write_tasks.jsonl`, `tasks/write_arms.json`, `tasks/oracle_write.jsonl` | Exp 2 |
| `tasks/files/` | original and expected texts (rebuildable) |
| `repos/` | shallow clones of the six public repositories at the pinned commits |
| `raw/pilot.jsonl`, `raw/main.jsonl`, `raw/e2.jsonl` | one JSON line per attempt: every model call (usage, latency, reasoning text up to 20K chars, tool calls with arguments and tool output) |
| `out/` | analysis outputs (`*.json` all numbers, `*.md` tables, `*.rows.jsonl` derived per-attempt rows) |
| `MANIFEST.sha256` | sha256 of every raw, task and script file |
| `evidence.csv` | evidence rows for the 10-tools section |

## Commands (from this directory)

```sh
W=<tofu worktree>   # read-only use: production schemas and executors
# 0. sources and tasks (deterministic)
python3 scripts/build_tasks.py              # -> 60 tasks; prints 8/8/8 and 4/4/4 strata
python3 scripts/build_write_tasks.py        # -> 21 Exp 2 tasks
# hermetic env for schema freezing (no writes outside scratch):
S=scratch/arms; mkdir -p $S/home $S/tmp $S/runtime $S/cache
(cd $S && env -i PATH=/usr/bin:/bin HOME=$PWD/home TMPDIR=$PWD/tmp TOFU_DATA_DIR=$PWD/runtime \
   XDG_CACHE_HOME=$PWD/cache XDG_DATA_HOME=$PWD/runtime PYTHONPATH=$W python3 -B ../../scripts/arms.py)
(cd $S && env -i ... PYTHONPATH=$W python3 -B ../../scripts/run_write.py --build-arms)
# 1. port pinning and oracle (expected: all pass)
EDITBENCH_CODEX_DIR=<vendored codex> python3 scripts/verify_codex_patch.py   # 23 passed, 2 CRLF skipped
python3 scripts/verify_cc_edit.py                                             # all pinned cases pass
EDITBENCH_TOFU_WORKTREE=$W python3 scripts/oracle.py                          # 240 oracle executions, 0 failures
EDITBENCH_TOFU_WORKTREE=$W python3 scripts/oracle_write.py                    # 42 oracle calls, 0 failures
# 2. model runs (Tofu's local OpenAI-compatible route, resumable)
EDITBENCH_TOFU_WORKTREE=$W python3 scripts/run_bench.py --run pilot --models glm-5.3 --tasks A01,K03,F06,T08,V10 --repeats 1
EDITBENCH_TOFU_WORKTREE=$W python3 scripts/run_bench.py --run main --models glm-5.3,deepseek-v4-pro,claude-opus-5 --repeats 3 --concurrency 2
EDITBENCH_TOFU_WORKTREE=$W python3 scripts/run_write.py --run e2 --models glm-5.3,deepseek-v4-pro,claude-opus-5 --repeats 3 --concurrency 2
# 3. analysis
python3 scripts/analyze.py --runs main --out out/main
python3 scripts/analyze_write.py --runs e2 --out out/e2
sha256sum -c MANIFEST.sha256
```

The CC strings dump used by `arms.py` is `EDITBENCH_CC_STRINGS` (Claude Code
2.1.207 linux-x64, binary sha256 85e7e988a392...). Raw records contain no API
keys; requests went through the local open-mode route without credentials.

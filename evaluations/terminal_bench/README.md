# Native Terminal-Bench runner (no Harbor, no agentix)

Runs Terminal-Bench 2.x tasks on CodeLab sandbox VMs (sandboxGateway, e.g.
cluster 857) with our own code end to end: one VM per trial, a stdlib-only
trial executor inside the VM, and a host orchestrator that resumes safely.

## Components

| Module | Role |
| --- | --- |
| `harness.py` | Task loading, protocols, Tofu/OpenCode/Pi/Claude-Code launch specs, trial spec, task bundle (never contains `solution/`) |
| `../claude_code_contract.py` | Claude Code 2.1.207 pins, native-binary runtime tree, guest settings and launch program |
| `anthropic_bridge.py` | Anthropic Messages ↔ OpenAI chat-completions bridge for the claude-code arm, in front of the shared relay |
| `source_guard.py` | Benchmark-source blocklist, one rule projected into each arm's own mechanism, plus the in-container canary |
| `vm_trial.py` | Uploaded into each VM: image pull → container → runtime install → agent under deadline → `tests/test.sh` → `result.json` + bundle; egress filter proxy |
| `gateway.py` | pssdk session start/stop and the ingress native exec channel |
| `runner.py` | Concurrent trials, resumable attempts, `summary.json` |
| `relay.py` | Model relay shared by every arm: same FRIDAY deployment, same 4 keys, same sampling |
| `../pi_contract.py` | Pi version pins, prebuilt runtime tree, guest `models.json` and launch program |
| `audit.py` | Benchmark-answer contamination flags for review |

The VM-side entry point is the ingress native exec channel
(`/native/exec|status|read|kill`) on nlp/harbor branch
`ruanjunhao/tofu-native-exec-20261102` (b35af6f). That branch only adds the
channel; nothing in the trial path runs Harbor.

## Workflow

```bash
PY=<interpreter with pssdk, aiohttp, dirhash>   # e.g. the Tofu app interpreter (.tofu_env.json)
RUN=.eval-runs/<run-dir>
# 1. relay (keys stay in this process; sandboxes see a key-less URL)
env -u PYTHONPATH $PY -m evaluations.terminal_bench.relay --port 18451 --model glm-5.3-flash-m17 \
  --temperature 1.0 --top-p 1.0 --max-tokens 65536 --reasoning-effort max \
  --log $RUN/relay-requests.jsonl --files-dir $RUN/files
# 1b. only for the claude-code arm: Anthropic bridge in front of the relay
env -u PYTHONPATH $PY -m evaluations.terminal_bench.anthropic_bridge --port 18452 \
  --relay-url http://127.0.0.1:18451 --log $RUN/bridge-requests.jsonl
# 2. run (SANDBOX_857_TOKEN injected by the sandbox-direct-access skill env overlay)
env -u PYTHONPATH $PY -m evaluations.terminal_bench run --work-dir $RUN/full --concurrency 178 \
  --tasks-dir <tb2.1 tasks> --arms tofu,opencode[,pi,claude-code] --protocol vendor-6h \
  --relay-url http://<host-ip>:18451 --artifacts-manifest $RUN/artifacts.json \
  --endpoint https://model.internal.example.com/sandboxGateway/system/857 \
  --image rootfs://harbor-pipeline-dind-v5 --proxy http://10.70.33.35:8080
  # add --anthropic-url http://<host-ip>:18452 when --arms includes claude-code
# 3. score: merges fix-up dirs, attaches the audit, applies human contamination verdicts
env -u PYTHONPATH $PY -m evaluations.terminal_bench.report $RUN/full [$RUN/<fixup> ...] \
  --tasks-dir <tb2.1 tasks> [--review $RUN/review.json]
# 3b. compare arms run separately: list every run's dirs and reviews, write elsewhere
env -u PYTHONPATH $PY -m evaluations.terminal_bench.report $RUN/full [$RUN/<fixup> ...] $OTHER/full \
  --tasks-dir <tb2.1 tasks> --review $RUN/review.json --review $OTHER/review.json \
  --output .eval-runs/<comparison>/report.json
```

`report` refuses a dir whose protocol, model, context, image, endpoint, proxy
or dataset commit differs from the first dir's, or that plans a task the first
dir does not; reviews that disagree on a trial are refused too. A trial counts
as a budget exhaustion (`agent_budget_exhausted`, `agent_timeouts`) when the
runner timed it out or its agent ran for its whole budget: Tofu stops itself at
the deadline (exit 3) without the runner's timeout flag.

Rerunning `run` with the same work dir resumes from `run.json`; measured
trials are never recomputed. `summarize` rewrites `summary.json`. A new run
refuses task directories whose Harbor checksum (`dirhash` sha256) differs
from `evaluations/terminal_bench_21_task_checksums.json`.

When a runner or runtime defect exhausts a trial's attempts, rerun just those
trials in a separate work dir with the fixed code/artifact (never a second
runner on a live work dir) and pass it to `report` after the main dir; a
measured trial there replaces the unmeasured one.

Runtime artifacts (`artifacts.json`, served from `--files-dir`):

- Tofu: standalone CPython 3.13 with the `tofu-agent[full]` wheel built per
  `docs/DEVELOPER_RUNTIME.md`. The released wheel cannot run a task headless
  (`lib.storage` excluded but imported by `lib/tasks_pkg`; `languages.json`
  not shipped), so the artifact adds both from the same commit. It also
  installs that commit's `packages/tofu-search` wheel: tofu-search is a
  separate distribution, and the base runtime's release would keep its own
  fetch gate and URL blocklist (the guard canary then fails closed). Verify
  any rebuild with `env -i ... python3 -I -c 'import lib.tasks_pkg.manager'`
  (the host exports `PYTHONPATH=<repo>`, which otherwise hides missing
  modules) and check that the runtime's freeze lists both wheels.
- OpenCode: `opencode_contract.OPENCODE_VERSION` linux-x64-baseline binary plus
  the pinned `@ai-sdk/openai-compatible` seed from `prepare_cache_seed`.
- Claude Code: `python -m evaluations.claude_code_contract build-runtime --work-dir <scratch> --out-dir <files>`
  verifies both npm tarballs against the registry integrity pins, confirms the wrapper pins the
  platform package at the same version, and packs the native `claude` binary (glibc >= 2.17),
  exactly what the wrapper's postinstall would install on linux-x64 glibc.
- Pi: `python -m evaluations.pi_contract build-runtime --work-dir <scratch> --out-dir <files>`
  packs Node `PI_NODE_VERSION` (sha256-checked against nodejs.org) and a
  global `npm --ignore-scripts` install of `PI_PACKAGE@PI_VERSION`, plus
  `pi-runtime.json` listing every installed package. Node is invoked by
  absolute path, so the task's own tools and PATH are unchanged.
- Native Pi contract verification: set `TB_NATIVE_PI_RUNTIME` to an extracted
  `build-runtime` output tree to run the real pinned runtime against the local
  relay stub. With it unset, the offline contract tests still run and only that
  live-runtime test skips.
- glibc floor: TB2.1 includes Debian bullseye images (glibc 2.31). On a newer
  host uv picks `manylinux_2_34` wheels (`cryptography`,
  `tree-sitter-language-pack`), which fail to import there. Build with
  `uv pip install --python-platform x86_64-manylinux_2_28 --no-build` and scan
  every `.so` for `GLIBC_2.x` above 2.28 before packaging. Build interpreters
  from `readlink -f` of the uv-managed path: copying the version symlink makes
  uv install into the shared interpreter.

## Fairness and protocol controls

- Same model, keys, relay, sampling and reasoning effort for every arm; the
  relay overrides `temperature`, `top_p`, `max_tokens` and (when
  `--reasoning-effort` is given) `reasoning_effort` on every request, and logs
  both the harness's original values and what was sent. The claude-code arm reaches that same relay
  through `anthropic_bridge.py`: Claude Code speaks only Anthropic Messages,
  so the bridge translates each request to the relay's OpenAI dialect under
  the same arm label, and the response back. `output_config.effort` maps to
  `reasoning_effort` (low/high/max; medium->high, xhigh->max, the deployment's
  levels), `thinking` enabled/adaptive maps to Z.ai's `thinking: {type:
  enabled, clear_thinking: false}`, and assistant `thinking` blocks go back as
  `reasoning_content`, so thinking is preserved across turns. The bridge logs
  the client's settings, the translated settings and the history's reasoning
  counts per request, so the protocol can be audited on the wire. Anthropic's
  server-side tools (web_search) have no OpenAI counterpart: they are dropped
  with a system note rather than failing the turn. `document` blocks become
  OpenAI `file` parts (the deployment reads PDFs this way); `count_tokens`
  returns 404, which Claude Code handles with its own estimate.
- Claude Code runs as Harbor's upstream adapter does (32164177f009): the
  instruction on stdin, `--verbose --output-format=stream-json
  --permission-mode=bypassPermissions --effort max --print`, every model alias
  pinned to the run model, `CLAUDE_CODE_MAX_OUTPUT_TOKENS` = the protocol limit
  (its default for an unknown model is 32000). Claude Code assumes a 200 000
  token context for a model name it does not know; the recorded 262 144 applies
  to the other arms, and there is no supported override short of disabling
  compaction. Its run ends at the final stream-json `result` record, so a
  process lingering on children it started is not killed and its daemons stay
  up for the verifier.
- Reasoning effort parity: Pi always sends `reasoning_effort` for GLM-5.3
  (its catalog maps levels to low/high/max) and Tofu pins its native
  `--thinking max`; OpenCode has no client-side effort field. TB2.1 runs also
  pin `--reasoning-effort max` at the relay for every arm rather than relying
  on the deployment default. The 2026-11-03 relay probe
  (`.eval-runs/native-harness-artifacts/effort-probe`, 16 samples x 2 prompts)
  found the default indistinguishable from `max` (Mann-Whitney z 0.06 and
  -0.47) while `high`, the Harbor preflight's level, used 2.3-2.6x fewer
  completion tokens; that justified the first baseline's default, not an
  implicit default in later runs. Any arm that changes effort must be re-run
  for all arms.
- Pi's `models.json` replaces only the built-in provider's `baseUrl` and sets
  the same context/output limits every arm is told, so Pi keeps its shipped
  thinking format and tool streaming. Pi's stdout is filtered like Harbor's
  (no `message_update` records); its session files under
  `/logs/agent/pi/sessions` are the audit source.
- Every agent runs once, as its upstream adapter runs it (OpenCode and Pi as
  in Harbor, Tofu as `tofu-agent run`), with its own product defaults. The
  runner never resumes, nudges or retries an agent and sets no harness tuning
  knob: a run that ends early, for example at the output-token limit, is
  scored as it ended. See the decision below.
- Same container, resources (4 CPU / 16 GiB), egress policy, budgets, and
  model facts (context window 262 144, output 65 536) for every arm, each
  through its own configuration surface: OpenCode's provider `limit`, Pi's
  `modelOverrides`, Tofu's routing deployment `max_output_tokens`.
- Tofu stops its own run at the agent budget (`--timeout-s`) and then writes
  its result document, the audit's trajectory source; only that write gets
  `TOFU_FINALIZE_GRACE_SEC` before the runner's kill. In the 2026-11-03 run
  no Tofu trial needed the kill and the largest overrun was 2.0 s.
- `vendor-6h`: 21 600 s agent budget for every task (Z.ai GLM-5.3-Flash card
  footnote); `task-default`: `task.toml` budgets.
- Agents run as `bash -lc` in the image `WORKDIR` like Harbor, as the child of
  a parent shell whose argv carries the trial marker (`MARKED_SHELL`); at the
  deadline that shell's process tree gets SIGTERM, then SIGKILL after
  `KILL_GRACE_SEC`, so a program that `exec`s or ignores SIGTERM stops too.
  Detached daemons keep running for the verifier. Tests are uploaded only
  after the agent finished.
- Benchmark-source blocklist (`source_guard.py`, on for every new run and
  frozen in `run.json` as `source_guard`). One rule: `ANSWER_HOSTS`
  (tbench.ai, marginlab.ai, harborframework.com and their subdomains) refused
  everywhere; `SOURCE_REPOS` (harbor-framework, laude-institute and the
  audit-found mirrors Bruce-lys/LLM-benchmark and Eyan-L/Agent_Bench-NSCC)
  refused wherever they appear in a URL or command; `NAME_MARKERS`
  (terminal-bench and variants, which forks keep) refused inside a URL. Each
  arm enforces it through its own mechanism: the trial egress proxy refuses
  the hosts at CONNECT and the markers in plain-HTTP URLs; Claude Code runs a
  PreToolUse hook on Bash/WebFetch/WebSearch (exit 2 shows the reason to the
  model); OpenCode gets `permission.bash` deny patterns; Pi loads a
  `tool_call` extension; Tofu gets its product `search.skip_domains` setting:
  the answer hosts as host rules and every source repository and name marker
  as a path pattern on every host (`*/**/*terminal-bench*`), enforced at the
  fetch gate, in search listings, in browser navigation and on absolute URLs
  in `run_command` (scheme-less references such as `git clone github.com/...`
  stay audit-only for Tofu).
  HTTPS hides paths from the proxy, so paths on shared hosts are refused only
  where a harness exposes the URL before it runs; other internet access stays
  open. Before the agent starts, the trial runs `guard_check` in the container:
  the canary URLs must be refused and the control URL on the same shared host
  must pass, and a failure is an install error, never a scored trial.
- Egress goes through a per-trial proxy that logs every request; agent-phase
  denials are audit evidence (the setup-phase canary is refused on purpose and
  is not). Only network calls (web tools or URL fetches) give strong evidence:
  task images ship visible tests, canary-bearing sources and whole
  repositories, so local reads of hidden lines are a weak flag. A strongly
  flagged pass is excluded after reviewing the cited tool calls.

- Gateway cache affinity: Tofu's evaluation Connection declares
  `cache_affinity_mt_session_id`, so the product sends its stable per-lineage
  `Mt-Session-Id` header. The relay forwards this header for every arm, including
  retries on another API key. It never generates an identity for a client that
  sends none, and never inserts a marker into the prompt. The request log's
  `cache_affinity` field records the carrier name without the identity. This
  measures each harness's configured transport behavior; comparisons intended
  to isolate agent behavior must separately control affinity for every arm.
  Pre-change runs whose relay stripped client headers use a different transport
  configuration and must be identified as such.

## Claude Opus 5.5 at medium effort (`--protocol claude-6h-medium`)

- Run model `claude-opus-5.5`, wire `kunlunbedrock.claude-opus-5.5-m17`, on
  FRIDAY's Anthropic face. Start ONE relay for both arms:
  `python -m evaluations.terminal_bench.relay --port 18521 --anthropic-model
  kunlunbedrock.claude-opus-5.5-m17 --key-suffixes 6672,0733 --read-timeout
  1800 --retry-budget 900 --log ... --files-dir ...`.
- The protocol keeps vendor-6h's 21 600 s agent budget and resources, sets
  reasoning to `medium` for both arms and uses the model's own facts: no
  temperature/top_p (Opus 5.5's catalog profile omits them), a 1 000 000-token
  context window and a 128 000-token output cap.
- Tofu's routing envelope declares protocol `anthropic`, the FRIDAY wire name
  and FRIDAY's provider id (`example-corp`, which selects its Claude cache
  multipliers and gateway sanitization). Pi uses its built-in
  `anthropic/claude-opus-5-5` definition, whose `models.json` override points
  at the relay. Each trial's base URL is
  `/arm/<arm>-<label>/task/<task>/anthropic`, so the relay attributes every
  request to a trial; Pi sends no affinity header and sandbox peers are NAT'd.
- Reasoning equivalence: Tofu sends `thinking: {type: adaptive, display:
  summarized}` with `output_config.effort = medium`. Pi's built-in Opus 5.5
  entry declares `supportsMidConvoEffort`, so it asks for a constant top-level
  `effort: high` and carries `medium` in empty mid-conversation `system`
  messages. FRIDAY rejects those markers (`system content must contain at
  least one block`), so the relay folds them into the top-level effort their
  last marker asks for and records the fold (`pi_effort_fold`, `sent_effort`).
  Both arms therefore reach the model with `output_config.effort = medium`.
  `--preserve-pi-effort-markers` passes them through instead, to measure that.
- The relay logs, per request: cache breakpoints with their TTL, digests of
  tools/system/each message (excluding `cache_control`), joined tool-name
  bytes, a digest of the Mt-Session-Id value, the request's own settings and
  the provider usage. No prompt text or affinity value is logged.
- `count_tokens` is forwarded too, so Tofu's exact token counter keeps working.

## Decisions

**Problem:** the runner resumed OpenCode and Pi with `--continue` and a
fixed nudge (up to 16 times) when a run ended at the output-token limit,
and gave Tofu evaluation-only upstream-fault budgets
(`TOFU_GATEWAY_OUTAGE_BUDGET_S`, `TOFU_429_SATURATION_SECS`). Both compensate
for, or tune, behavior of the harness under test. In the 2026-11-03 run the
loop resumed OpenCode in 4 trials, 2 of which then passed. **Decision:** the
evaluation layer only provisions, enforces the shared budget, collects
evidence and verifies. Each harness runs once with its own defaults, and how
it handles an output-limit stop is part of what is measured. **Alternatives
rejected:** keeping the loop for every arm (it measures the runner's
recovery policy, not the harness's); extending an equivalent loop to Tofu
(same objection); per-harness retry knobs (tuning the subject of the
measurement).

**Problem:** Harbor + agentix wrapped the agent in layers we could not fix
quickly (Python 3.15 interpreter selection, a 14 400 s wrapper cap below the
protocol budget, CCR translation and a 16 384-token cap for OpenCode, sandbox
apt mirror rewrites). **Decision:** drive the sandbox VM directly with a
generic exec channel and keep all trial semantics in this package.
**Alternatives rejected:** patching Harbor/agentix per finding (each fix
needed a pushed branch and re-sync, and the layers kept diverging from the
product); the rootless local QEMU runner (no KVM capacity for 178 parallel
6 h trials on this host).

**Problem:** installing agents inside task images through package managers
broke on stale mirrors and picked wrong interpreters. **Decision:** prebuilt,
sha256-pinned runtime trees copied in with `docker cp`; Tofu runs with
`python3 -I` so a task's `lib/` package or `PYTHONPATH` cannot shadow it.
**Alternative rejected:** per-trial `uv tool install` (network-dependent,
version drift, Python 3.15 selection).

**Problem:** blocking answer sources at the tool level missed `curl`/`urllib`
fetches. **Decision:** network-level proxy deny list plus an evidence audit
(fingerprints of the task's `solution/`, `tests/` and `README.md` lines —
none of which the agent is given — received before the agent wrote them,
canary GUID in tool results, benchmark-source tool calls). **Alternative rejected:** TLS
interception to see GitHub paths (changes every task's trust store).

**Problem:** the deadline kill found the agent by a marker passed as `$0` of
`bash -lc program`, but bash replaces itself with a lone simple command or the
last command of a list, so the marker vanished and the kill signalled 0
processes (the 2026-11-02 run's `opencode/mailman` agent and
`model-extraction-relu-logits` verifier attempts). **Decision:** run the
program under `MARKED_SHELL`, a parent shell that only waits, and track
victims by pid and start time so processes that outlive their marked parent
still get SIGKILL. **Alternatives rejected:** `docker exec <container> kill`
(trusts tools inside the image); stopping the container (also stops the
daemons the verifier tests).

**Problem:** `start_container` made both log mounts `chmod 0777`. A process the
agent or the verifier's own `test.sh` dropped to a de-privileged uid (e.g.
65534) could then create, rewrite, delete, rename over or symlink-swap
`/logs/verifier/reward.txt` — the reward channel was writable by the very code
being scored, and a `reward.txt` the agent planted before the verifier ran was
read back as a real reward when the test never wrote one. Verified in a real
container on cluster 857 (2026-11-09, one synthetic task, no model): with
0777 a uid-65534 child forged the reward six ways and a delayed grandchild
deleted it, ending in `no_reward`; a uid-1000 agent's planted `1.0` was the
score. **Decision:** `/logs/verifier` is a root:root 0700 directory
(`seal_verifier_logs`), emptied again immediately before the verifier runs so
nothing the agent phase planted survives; every verifier command runs as
uid 0, so the test still writes its reward and logs as root; `/logs/agent`
belongs to the image's user (the agent's uid/gid, read from the container) at
0755, so the agent still writes its own logs and nobody else can; rewards are
read only from regular files, never through a symlink, and a planted FIFO or
directory no longer blocks or crashes the reader. The red-team attacker is
`tests/support/reward_forgery_probe.py` (create, in-place rewrite, truncate,
delete, rename swap, symlink swap, hard-link swap, directory rename/chmod,
read), driven as uid 65534 by
`tests/evaluations/test_tb_reward_forgery.py`; that module is stdlib-only so
the VM smoke uploads and runs it inside the real container.
**Alternatives rejected:** keeping 0777 and trusting the verifier's `test.sh`
not to run untrusted code (the submitted code is the thing under evaluation);
only removing `reward.txt` before verifying, which still allows the swap races
after the legitimate reward is written; running the verifier as the agent's
uid, which shares the reward channel with the agent.

**Problem:** Harbor 0.25.0 does the same thing in its own run mode:
`environments/docker/docker.py` calls
`ensure_dirs(_mount_targets(writable_only=True))` at start, which is
`mkdir -p ... && chmod 777 /logs/verifier /logs/agent` as root
(`environments/base.py::_ensure_dirs_command`), `TrialPaths.chmod_dir` makes
the host-side trial/agent/verifier dirs 0777, and
`trial/trial.py::_run_separate_verifier` empties and re-`chmod 777`s the
verifier mount right before `test.sh` runs. Tofu's rootless QEMU adapter
inherits all of it even though `capabilities.mounted` is False (the trial
passes the log binds as `auto_mounts`). **Decision:**
`rootless_vm/harbor_environment.py` re-seals `/logs/verifier` to root:root
0700 at the end of every directory command it builds, and a verifier exec runs
as root explicitly (a task-level `[verifier] user` is honored by chowning that
directory to the named user, never by re-opening it to every uid). The logs
come back by guest download, so a root-only container directory does not
impede the host reader. **Alternatives rejected:** patching Harbor upstream
(different owner, and its `prepare_logs_for_host` chowns host binds back to
the host user, so a root-only mount would break a non-root Harbor host);
leaving the mount 0777 because the guest is a VM (the uid boundary is inside
the container, exactly where the agent's and the tests' code runs).

## Verification

`pytest -q tests/evaluations/test_terminal_bench_native.py` (offline: bundle
excludes solutions, spec/protocol, launch contracts, proxy allow/deny/CONNECT,
reward parsing, safe bundle extraction, summary, audit rules),
`pytest -q tests/evaluations/test_claude_code_native.py` (Claude Code command
assembly, bridge request/response/stream translation and an end-to-end run
against a fake relay), `pytest -q tests/evaluations/test_tb_source_guard.py`
(the blocklist rule and its bash/JavaScript/OpenCode projections, canary
pass/fail, audit evidence), and
`pytest -q tests/evaluations/test_pi_native_contract.py` (Pi launch spec,
guest program and event filtering against a scripted Pi, audit of Pi session
files),
`pytest -q tests/evaluations/test_tb_reward_forgery.py` (the reward channel is
root-only: directory modes and owners before the agent and before the
verifier, verifier execs as uid 0, agent-planted rewards/links/FIFOs removed,
rewards read only from regular files, and — with real root, or in the VM smoke
— a uid-65534 child unable to create, rewrite, delete or swap `reward.txt`
while a legitimate root reward is written and read back and the agent's own
log still works), and
`pytest -q tests/evaluations/test_harbor_verifier_log_permissions.py`
(rootless Harbor adapter: the verifier mount is re-sealed to root:root 0700
after Harbor's own `chmod 777` preparation, `/logs/agent` keeps its
writability, and a task-named verifier user gets ownership instead of a
world-writable directory). Setting `TB_NATIVE_PI_RUNTIME` to an extracted Pi runtime tree also
runs the real pinned Pi against a local OpenAI-compatible stub, asserting the
wire request and that an output-limit stop ends the run unresumed. Live smoke: two short tasks per arm
on cluster 857.

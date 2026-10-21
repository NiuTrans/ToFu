# Programmatic calling diagnostic pilot

Run an offline preflight with `python -m evaluations.programmatic_pilot`.
It performs no model calls. The frozen pack covers result reduction, dependent
fan-out, semantic judgment and a small lookup. The latter two are controls
where a program may add overhead. No task asks the model to use a program.

Select `--pack adaptive` on either CLI for observed dependency navigation,
semantic judgment joined with numeric aggregation, and the original semantic
and small-result controls. `baseline` stays the default and preserves its frozen
identities. The adaptive world returns real distinct data at each step; no task
asks for code, reuse, a handoff, or a particular call sequence. Both packs require
complete successful reads of every dataset in their respective worlds, even if
a model guesses the correct answer. Pack identity enters preflight and report
validation, so evidence from different packs cannot be combined silently.

Select `--pack declared` for known decoded-result fields, returned dataset names,
actual next-page cursors and explicit `count=records`/`total=sum(amount)` oracles.
Both arms receive identical return contracts. Small pages fit direct delivery;
source proof also requires the manifest and every dataset's terminal cursor.
The original semantic/small controls keep their original generic tools. Per-task
tool schemas enter the pack manifest. This separate pack preserves both existing
pack identities and measures voluntary additive adoption, not code-only exposure.

Problem: unclear returned fields and ambiguous aggregation instructions obscured
known-dependency behavior. Decision: add explicit producer contracts and a separate
frozen pack. Alternatives rejected: revising the old fanout prompt after failure,
forcing a program, assuming cursor termination proves all rows were read, or
crediting a scripted SDK test as real-model adoption.

Problem: fixed tool arguments and parameter-echo stubs cannot establish behavior
when observed values govern later reads or semantic decisions. Decision: add a
separate frozen adaptive pack using the same production runtime, tools and
budgets. Alternatives rejected: changing old tasks after seeing their outcomes,
forcing program use, weakening source coverage, and reporting unit orchestration
as natural adoption. Neither this small synthetic pack nor
[parameter-serialization benchmark results](https://arxiv.org/html/2608.06370v1)
establishes production superiority.

Both arms use the public production `AgentRuntime`, the same access envelope,
thinking, prompts, exclusive read-only custom tools, and execution budgets.
Only `tools.programmaticCalling=off/on` differs. The candidate retains direct
tools and uses additive exposure. Array reads support offset/limit pagination,
so the baseline can obtain complete source despite inline result limits.
Oracles require both exact JSON and complete successful source reads.

Live execution is opt-in and requires a complete existing v2 access envelope;
the runner never invents model facts or reads application credentials:

```bash
python -m evaluations.programmatic_pilot --live \
  --model-routing /private/model-access.json \
  --output "$PWD/.eval-runs/ptc-pilot-new" \
  --trials 2 --thinking medium --timeout-seconds 180
```

Output must be a new directory; evidence files are exclusive-created and
mode 0600 beneath mode 0700 run/task directories. Model-access secrets are
omitted from the manifest. The manifest freezes current retained source bytes,
pack, tools, system prompt, configs, time/token/tool budgets and zero retries.
Source drift stops dispatch/report generation. Native events, leaf-call hashes,
public runtime evidence and terminal records survive failures. No failed task
is silently retried or excluded. Trial order alternates baseline/candidate.

Limits are 1–5 paired trials per task, 10–600 seconds per task, 100,000 reported
tokens per task, 32 admitted tools per task, 4,096 output tokens per model
call and 16 MiB per evidence artifact. A final in-flight call can exceed the
remaining reported-token budget; the production admission contract owns this.
Work executes sequentially, with no host shell/filesystem tools or sub-agents.

An optional `--price-card` freezes explicit USD rates per million tokens:

```json
{"currency":"USD","source":"https://creator.example/pricing",
 "perMillionTokens":{"input":2,"cachedInput":0.2,"output":8}}
```

Those numbers are an illustrative schema, not any model's prices. Pricing
requires complete input/output/cache usage; missing cache evidence or a missing
card leaves cost null. Raw provider usage is retained for subsequent audit.
Latency includes the measured run and evidence handling; no overhead is
subtracted. Program counts mean authored local programs in native evidence,
not exposure, batches or estimated saved rounds.

The report is always diagnostic and never claims population benefit or release
readiness. Quality, adoption, latency and cost must be assessed together, per
family; the small synthetic pack cannot establish production superiority.

Verification: `pytest -q tests/evaluations/test_programmatic_pilot.py
tests/llm/test_programmatic_catalog_activation.py
tests/evaluations/test_declared_programmatic_pilot.py`. The public-runtime tests
substitute only provider transport; they are keyless and prove orchestration,
gateway, client I/O, evidence and settlement, not natural model adoption.

## Configured application transport

When a local application already owns a ready model route, use its public v4
API without exporting provider secrets:

```bash
python -m evaluations.programmatic_pilot.native_api --live \
  --origin http://127.0.0.1:15000 --creator deepseek --model deepseek-v4-flash \
  --output /private/ptc-native-api-new --trials 2 --timeout-seconds 180
```

The model must already be `ready` in that caller's redacted routing aggregate.
An authenticated application can use `--api-token-env NAME`; the value stays
in HTTP headers and never enters artifacts. Only explicit loopback IP origins
are accepted, and redirects are refused before forwarding credentials.

The API mints each request's client tools. The caller selects the same explicit
schema precedence branch as the public SDK, with two executable read tools;
native wire events verify that no host tools enter either arm. Provider identity
is pinned after the probe. Events follow the producer cursor, including terminal
catch-up, and foreign/evicted evidence aborts the owned task. A lost admission
reply is never resubmitted; the caller attempts owner-scoped recovery by its
fresh conversation correlation and refuses later work unless settlement is
verified. Failed and partial runs retain records and cleanup evidence.

A separate, explicitly forced capability probe requires an actual
`local_javascript` / `quickjs-wasm` program and an exact 64-bit integer result.
It gates paired trials and never counts as natural adoption. This also detects
mixed resident modules after source changes. Restart a shared service only
through its human-approved lifecycle boundary; no approval or restart bypass
exists in the runner. A failed probe runs no paired trials.

The capability gate checks the native `program_output` result, completed status
and engine statistics. Final answer formatting is scored separately: wrapping
the exact result in Markdown can fail the strict answer oracle without making
the engine incapable. Paired-task oracles remain unchanged, and `answer.json`
retains each terminal answer (or explicit failure) for independent rescoring.

This transport measures the deployed server's behavior. The source fingerprint
covers the local client checkout, including JavaScript/WASM and dependency
artifacts; it does **not** prove the server's loaded source bytes. Reports retain
`serverSourceBytesVerified=false` and remain diagnostic. The capability probe
warms the kernel/provider before pairs; its evidence and first-use cost remain
separate, and pair deltas describe subsequent executions. HTTP polling,
callbacks and artifact handling remain in measured wall time.

Problem: a configured application can be callable without a private SDK access
file. Decision: use its public owner-bound API and require runtime evidence
before measuring. Alternatives rejected: exporting application secrets,
using another provider hop that changes the agent loop, or treating a mixed
module failure as evidence about the redesigned mechanism.

On 2026-10-03, an existing ready DeepSeek V4 Flash route accepted the native
probe on provider `example-corp`, advertised exactly the two client tools plus the
gateway, and completed normally. Its gateway returned `ImportError` for
`ProgramExecutionError` from a cached old module. The corrected transport
retained the terminal trace, rejected the capability gate (`programCount=0`),
started no paired tasks, and verified zero owned tasks in flight. This proves
route access and probe rejection, not optional adoption or performance gains.

Local candidate requests now offer the first-class `execute_program({code})`
action with named async tool calls. The direct arm remains unchanged. The
forced runtime probe names that action only to verify execution capability;
paired tasks still contain no program instructions. Wire evidence accepts
only the exact two client tools plus the code action (or its explicit batch
recovery surface) for additive exposure, never an expanded executable catalog.
An explicitly preregistered `code_api` candidate instead requires the single
`execute_program` wire action and still requires exactly two executable child
tools and a schema fingerprint. Native additive fallback or a batch-only action
fails this candidate's projection gate; the direct arm keeps both native tools.
Problem: the additive-only evaluation guard rejected valid code API projection
before any model round. Decision: derive the expected wire from the requested
exposure while checking child authority independently. Alternative rejected:
accepting arbitrary subsets would hide missing tools or a failed projection.
Prior reports retain
their original surface fingerprints and strict scores.

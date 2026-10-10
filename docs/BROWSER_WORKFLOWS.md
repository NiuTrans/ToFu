# Browser sessions, authenticated reads and workflows

`browser_site(site=..., action="status")` returns connection/capability readiness,
`authentication: unknown`, and a 120-second owner/device/profile/site routing
descriptor encoded as a bounded string with no authentication material. It is
not a credential or grant. Optional `session_ref` is checked against the live
authenticated route on every use; cookie presence never proves
login. Operations still perform their own content/login checks.

The optional `workflow` executes declared ephemeral read operations in one model
call. Its schema is owned by `workflow_contract.py`: at most 8 steps, 5 foreach
items/step, 120 seconds, 65,536 input characters and 4,096–80,000 output characters.
`foreach` selects one named field from a preceding array result into one declared
operation parameter; it is not an expression evaluator. For example:

```json
{"site":"modelplaza","workflow":{"steps":[
  {"id":"hits","operation":"search","params":{"query":"Claude","limit":3}},
  {"id":"details","operation":"detail","foreach":{"step":"hits","field":"url","parameter":"url","limit":3}}
]}}
```

Sequential steps reuse only their own ephemeral page and skip repeat navigation
to an unchanged exact URL. Reuse never implies cached authorization or content.
Each invocation validates schemas, domain policy, capabilities and pacing again.
Unpaced independent foreach reads may request `parallelism: 2`; each gets its own
page, and only two futures are submitted at a time. Normal pacing waits consume
the same deadline; a cooldown or external contention fails explicitly. Writes,
persistent/current user-tab operations and automatic retries are excluded.
The task abort event interrupts queue waits and pacing and prevents subsequent
steps from starting; cleanup commands remain executable after cancellation.
Partial receipts retain completed step/item indexes and stop reasons; cleanup
runs before returning. Resume only unfinished reads.

Admission permits one workflow/owner, and a process limit derived from the
launch-probed poll-inflight budget divided by four, clamped to 1–4 (lean default
2). Each active workflow retains one sequential page, at most two additional
fan-out pages and two worker threads; pools close at workflow exit. All pages
also consume the existing process lease budget. No durable workflow/cache store
is added. Intermediate records are redacted and bounded by the output budget;
two parallel results can transiently occupy twice that budget before projection.

Adapter-owned `AuthenticatedRead` recipes declare a fixed HTTP(S) origin/path,
query keys and optional localStorage/sessionStorage credential key/header.
Model parameters cannot select credentials, headers, request methods or arbitrary
endpoints. GET runs inside that origin with browser cookies, a 10-second timeout,
no redirect following, 256 KiB JSON body, 4,000 nodes and depth 16. Known token
values and credential fields are scrubbed before crossing the bridge. No token
cache is introduced. ModelPlaza model-info uses this shared path; other adapters
can supply recipes without reproducing credential transport code.

Extension 5.6.0 negotiates optional `read_receipt` and `cookie_metadata` capabilities
on protocol 2. A high-level page read bundles its state checks and content into
one command (three commands on older extensions). Redirects and same-URL document
reloads reject the bundled receipt. Cookie diagnostics require an explicit scope
and return metadata directly from Chrome; older extensions receive an upgrade
message instead of transporting values for diagnostics. Internal verified login
capture remains a separate domain-scoped raw-value path into the authentication
source owner; therefore model redaction does not mean credentials never leave
Chrome. Cookie store/partition selectors are supported by the internal command.

Page auto/text/data reads return a content revision. Supplying `known_revision`
suppresses unchanged content after a fresh authorized read; hashes include owner
and device. This saves model context without a persistent content cache.
Adapter/workflow receipts include at most 128 request-local timing samples,
summarized as command count, navigation/read/interaction/cleanup phase totals,
queue/total/extension-execution and residual
transport/settlement p50/p95/sums. Samples contain no URLs, arguments or bodies;
parallel sums are aggregate work, not wall time. Unknown extension timing stays
unknown. The transport remains asynchronous long polling with immediate result
flush; command-count reduction is measured by the workflow tests.
Active result/command chains reconnect without the idle 100 ms gap; empty
responses restore it. A WebSocket migration requires observed transport latency
evidence, not an assumed
eight-second delay per command.


Verification: `pytest -q tests/browser/test_browser_workflows.py tests/browser/test_browser_authenticated_read.py tests/browser/test_browser_site_tool.py`.

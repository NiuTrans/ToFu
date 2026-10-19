# Repository scan and ranking

`outline.py` owns source admission/extraction; `rank.py` owns weighted ranking
and fallback; `api.py` owns public scan/rank/render composition. The caller
owns scanner authorization and lifecycle; no module owns a repository cache.
Built-in Python admission and explicit exclusions do not initialize optional
grammars; `test_parser_catalog_laziness.py` verifies the first mixed-language
transition and unchanged tags/references.

`_native_rank.py` lazily prefers the optional independently installed
`tofu-repo-kernel` wheel. Package absence/admission failure uses NetworkX;
computation failure keeps reference-count fallback. The process retains only
one immutable native binding; graph arrays are call-local. Algorithm/C ABI
source authority is `packages/tofu-repo-kernel/`, never the experiment wrappers.

Verify with an installed wheel: `tests/repo_map/test_native_rank.py`, neighboring
`tests/repo_map`, `tests/meta/test_repo_map.py`, `tests/tasks_pkg/test_repo_map_handler.py`,
then `benchmarks/repository_index/native_publication_measure.py` pilot/full runs.
The owning decision is `docs/modules/tools_execution.md`.

`native_runtime.py` owns explicit installed-stream task binding. `_native_stream.py`
owns framing/deadlines/child retirement; `_native_owned.py` lends immutable code
under runtime/task lifetimes. `_native_limits.py` validates trusted byte budgets;
`_native_source_admission.py` uses generated `_native_catalog.py`. Benchmarks
reexport these owners. Task scopes bind `repository_scanner` independently of
custom tool environments; terminal release must not take the runtime binding
lock while ExecutionSession holds its lock. Verify native application scanner/SDK
tests plus the stream/ownership suites. Explicit installed grammar bundles are admitted by the optional package; full
performance and catalog coverage remain separate. Default extraction stays reference.
`StreamLimits.from_launch_policy` consumes issued frame capacity without a probe
or reservation; canonical source/reference bounds still limit source growth.
Verify `test_native_repository_capacity.py` and SDK laziness/owned-scan cases.

`test_native_model_loop.py` verifies real Rust SDK/child provider rounds;
`test_native_application_http.py` verifies v4 commands and durable completion
across storage transports. `test_native_application_lifespan.py` covers distinct
application owners and joined cancelled close. Application/child selectors live
in `lib/tasks_pkg/native_application.py` and `native_invocation.py`.

`_native_discovery_fallback.py` negotiates protocol v3 with the installed stream:
Git success is final, then directly owned rg, then a Rust walk. Empty rg remains
final. Path/request budgets and one overall cancellation/deadline cover both
sibling PIDs. Verify plain-directory membership, hidden/configured inputs,
blocked rg join, malformed negotiation, ownership and native SDK child rounds.

Source protocol v2 carries discovery indexes, with a codec byte selecting
Git/rg replacement or walk surrogateescape. Restore Tag paths only from the
selected discovery table; reject row index/tag-field substitutions.

Application task scopes select file-local grammar failures, preserving canonical
Python tags and all admitted references. Owned measurement scopes remain strict
by default; transport/artifact failures still fail the tool. Verify the missing
grammar public-handler and strict-owner tests in `test_native_application_scanner.py`.
`StreamLimits` retains its frozen dataclass API with explicit value methods to
reduce first-use generation. Verify `test_native_limits.py` before timing changes.

A `None` grammar directory selects the installed pinned bundle lazily; explicit
paths preserve trusted-directory selection. `grammar_admission` always fails the
scan, even with file-local missing-grammar policy. Verify bundle/stream tests and
installed SDK ownership/close cases; grammar code belongs only to the joined child.

# Native repository extraction

`src/lib.rs` ports the canonical tag traversal in `lib/repo_map/outline.py`.
`src/main.rs` owns one explicit JSON batch with caller-supplied byte limits.
`src/wire.rs` owns shared bounded JSON preparation; `src/ffi.rs` exposes a coarse
experimental C call into caller-owned buffers. Its unsafe preconditions are
documented on the export; panic containment is not native fault isolation.
Use the exact locked upstream registry with runtime downloads disabled and an
explicit trusted grammar directory; no Python import shim or cache discovery.
Build outside the checkout with `TSLP_OFFLINE=1` and `TSLP_LANGUAGES` unset.
Never disable upstream grammar safety patches. The owning process retires all
grammar libraries. The C experiment keeps those libraries resident until process
exit; no in-process unload, hard cancellation or production selector is admitted.

Run Rust tests, then `benchmarks/repository_index/check_native_extraction.py` against the release binary
and the same installed Python grammar libraries. Exact Tag fields, ordering,
Unicode boundaries, malformed input, per-file failures and joined concurrent
children precede performance measurement. Retain raw source/grammar digests.
For the library, run the same candidate pytest file with
`TOFU_NATIVE_EXTRACTION_LIBRARY` plus the grammar directory. Caller-buffer
atomicity and concurrent calls into one CDLL precede actual-tool comparison.

`generate_catalog.py` pins the upstream grep-ast source hash and generates
`src/catalog_generated.rs`, the Python admission-key set and the POSIX filename
oracle. Never hand-edit these outputs or bypass the source-hash refusal. Keep
the upstream Apache license with the generated mapping. `src/catalog.rs` owns
the lexical resolver; protocol v2 opts in while v1 still requires a language.
Verify `tests/repo_map/test_native_extraction_catalog.py`, including a fresh
process that forbids Python parser imports, before measuring cold-start gains.

`src/stream_main.rs` combines real-Git discovery, source reads and extraction in
one owned child. `benchmarks/repository_index/native_extraction_stream.py` borrows the installed package's
sealed image, environment and kill/join owner. Budgets are explicit; one framed
file at a time does not bound parser or result RSS. Run
`test_native_extraction_stream.py` with `TOFU_NATIVE_STREAM_BINARY` and the exact
grammar directory: text normalization, protocol faults, blocked reads/writes,
repeated interrupts and actual tool publication precede timing.
`stream_discovery.rs` owns fallback negotiation and the Rust directory walk.
`--list-rg` execs rg in place; the parent owns/join both sibling PIDs. Canonical
walk limits/ignore names arrive from the existing Python owner, never duplicate
policy tables. Verify `test_native_plain_directory.py` plus Rust framing/walk tests;
`stream_paths.rs` preserves native walk path bytes; source requests/rows use
discovery indexes and path-free tags. Verify both decoding modes and malformed
index refusal. Product default and full performance admission remain separate.

Keep POSIX byte conversion cfg-gated so unsupported stream hosts can still
build the standalone package. Non-POSIX stream execution remains unadmitted;
invalid byte input refuses rather than substituting a different path.

`sealed_grammars.rs` implements explicit installed bundle admission on Linux.
The shared schema is `../../tofu_repo_kernel/grammar_bundle.schema.json`; its pin
joins the installed build record. Keep upstream alias/symbol behavior, null/ABI
checks and process-lifetime code ownership. Never unload while a Language/Parser
can survive. No temporary grammar directory is created. Run its core faults,
the explicit real-library lifetime test, and `test_native_grammar_stream.py`.
Integrity failures must escape file-local parser degradation and retire the child.

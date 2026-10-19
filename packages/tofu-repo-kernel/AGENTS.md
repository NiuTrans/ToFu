# Native repository kernel

Standalone package outside the shared uv workspace. `rust/kernel.rs` is the
ranking algorithm owner; `tofu_repo_kernel/_kernel.py` owns the C ABI and input
buffers. `setup.py` builds platform wheels and their digest/ABI receipts.
`rust/discovery/` owns the separate gix library, with exact dependencies in
Cargo.lock. `_discovery.py` owns its result-buffer lifetime and lazy binding.
Discovery stays explicitly invoked until application lifecycle/performance
admission. The in-process deadline is cooperative. `main.rs` and
`_process_discovery.py` provide a separately loaded, per-call child whose parent
terminates and joins it on timeout/cancellation/error. The child changes cwd
after exec; parent state is unchanged. On Linux, `_process_discovery.py` seals
the verified bytes in one owned memfd; launch duplicates it under the close
lock. Its declared local Git environment excludes unrelated credentials and
loader hooks. Unsupported hosts refuse process discovery. Verify with
`tests/repo_map/test_native_discovery_process.py`, including blocked Git config,
repeated interrupts and relative Git environment paths. Runtime imports stay standard-library-only, inert and independent of Tofu.

`__init__.py` freezes one validated build manifest under its existing loader
lock; each lazy artifact still verifies its bytes. `_process_discovery.py`
projects environment names before reading values. Verify concurrent manifest
sharing, invalid-record retry and replacement refusal in `test_native_manifest.py`.
`test_native_discovery_indexes.py` compares both transports with real Git for large
compressed/conflicted indexes, submodule states and plain/bare refusal boundaries.

Build/install a wheel into an isolated target before running
`benchmarks/repository_index/check_native_csr.py --library PATH`,
`tests/repo_map/test_native_rank.py`, and real task-publication measurements.
Build outputs stay outside retained source and are never committed.

`python -m tofu_repo_kernel` certifies installed artifact integrity/computation
without a checkout. Docker role builds and immutable platform-image release
gates must fail if this certification or native application selection fails.


`rust/extraction/` owns the packaged stream protocol and pinned native catalog.
`_stream.py` exposes explicitly owned sealed code and raw child startup;
`open_stream_process()` validates the installed manifest without global image
retention. Keep the caller's framing/deadline/child-retirement boundary explicit.
Verify `test_native_stream_package.py`, the extraction/stream/owner suites, and
an sdist-built wheel outside the checkout. The installed probe exercises a real
minimal Git worktree. Explicit directories remain trusted inputs; optional installed
grammars are selected through the pinned bundle below.

Stream protocol v3 negotiates rg/walk after ordinary Git discovery failure.
`start_rg` uses the same sealed executable to chdir after launch and exec rg in
place; keep it a directly owned sibling, with no inherited loader/credential
inputs. Verify plain-directory membership and PID retirement before timing.

`stream_paths.rs` owns byte-preserving discovery-table admission. Source protocol
v2 selects indexes; rows repeat those indexes and tags carry no path. Keep the
Git/rg replacement and walk byte-preserving modes explicit. Verify replacement
twins, invalid-byte roots/directories, malformed indexes and packaged probes.

`tofu_repo_kernel/grammar_bundle.schema.json` owns the installed grammar manifest;
Python packaging and the compiled Rust worker consume its format/version constants.
`_grammars.py` validates/copies pinned build inputs and supplied notices, never loads
code. `_stream.py` carries explicit installed selection to the same owned child.
`rust/extraction/src/sealed_grammars.rs` seals touched code without disk staging and
keeps library handles until process exit. Verify bundle/stream fault tests, actual
installed SDK calls, an sdist-built wheel and observed cleanup before admission.

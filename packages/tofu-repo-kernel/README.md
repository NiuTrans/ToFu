# Tofu repository kernel

Standalone weighted CSR PageRank with the canonical Tofu/NetworkX iteration
defaults and dangling-node behavior. It has no Tofu, database or NumPy dependency.
Import is inert; the first `load_kernel()` verifies the packaged binary digest
and initializes one immutable C ABI binding. Calls release the GIL, own their
buffers and retire Rust allocations on return. No graph/cache/thread pool is
retained. Restart the process after installing or replacing the native package.

**Problem:** separate lazy artifact loads repeat path resolution, file reads and
JSON parsing; environment filtering decodes values it will discard. **Decision:**
the loaders share one validated, process-lifetime build manifest under their
existing lock. Each artifact still verifies its own bytes; later
manifest edits cannot admit a different worker into that running package
snapshot. Invalid base records remain retryable. Discovery environment projection
reads only declared local Git/path/config values and valid indexed Git keys;
unrelated environment values are not read or forwarded. **Alternatives:** eager
artifact loading taxes unused capabilities; skipping binary verification loses
integrity; copying the whole environment retains unnecessary value decoding.

Install a platform wheel, or build from source with a Rust compiler available:

```sh
python -m pip install ./packages/tofu-repo-kernel
python -m build packages/tofu-repo-kernel
python -m tofu_repo_kernel
```

Source builds require Rust 1.88 or newer and Cargo; wheel consumers require only Python. Wheels carry a
platform tag and `py3-none` ABI tag; a build is not automatically manylinux
certified. The package resolves independently of the shared uv workspace.
Tofu automatically prefers this package for repository ranking when installed.
Missing or invalid artifacts retain the canonical NetworkX path. There is no
runtime compiler, download, storage authority or model-visible backend option.
Container builds install the wheel into API, Worker and Headless environments;
Rust stays in the build stage. The module command certifies a digest-verified
installed artifact and computation without a checkout or host application.

`rust/kernel.rs` owns the algorithm. `_kernel.py` owns buffer validation and the
C ABI. The application owns repository graph semantics and fallback policy in
`lib/repo_map/_native_rank.py`; this package imports no application code.


The wheel also carries a separately loaded Git discovery library. Explicit
`load_discovery().list_files(root, timeout=60)` reads the index, expands sparse
entries and walks untracked files with Git ignore rules. It returns sorted raw
path bytes, including an authoritative empty result. Every call owns its
repository and allocations; no process cwd/environment is changed. The library
is built from `rust/discovery/` with Cargo.lock and its source/library digests
are recorded in build.json. Ranking never loads this companion library.

`DiscoveryError.status` distinguishes discovery failure (2), expired cooperative
deadline (3), contained panic (4), and unsupported environment redirection (5).
Relative Git environment paths, GIT_COMMON_DIR and GIT_ALTERNATE_OBJECT_DIRECTORIES
are refused because interpreting them against process cwd could select the
wrong repository. No partial result escapes; result buffers are freed even if
Python cannot copy them. An already-blocked filesystem call is not interruptible
by this deadline. Callers requiring a hard timeout must own and join a process.
The application has **not** selected native discovery; the retained real-tool
experiment is `benchmarks/repository_index/native_discovery_measure.py`.

For blocking-filesystem isolation, explicit `load_discovery_process()` returns
a digest-verified, sealed executable snapshot on Linux. Its `list_files(root, timeout=60,
cancellation=None)` launches one owned Rust child and waits for terminal exit.
An optional `threading.Event` requests cancellation. Deadline/cancellation,
caller exceptions, malformed output and nonzero exits withhold all paths and
close both pipes after joining the child. Repeated KeyboardInterrupt during
cleanup cannot abandon it. A successful empty frame remains authoritative.
The child changes cwd **after exec**, allowing its parent to own filesystem
setup stalls and giving relative Git environment paths their root semantics.
GIT_COMMON_DIR and GIT_ALTERNATE_OBJECT_DIRECTORIES still require Git fallback.
No background worker, pool, queue or repository cache is retained. One sealed image is retained for the descriptor lifetime; explicit `close()`
retires it and refuses new calls. Existing children keep their own descriptor.
Disk replacement cannot alter already admitted bytes; restart after a package
update. Other platforms refuse this process API; ranking and C ABI discovery
remain independently available. Only declared local Git/path/config environment inputs reach the child,
including indexed Git config entries; unrelated credentials and loader hooks
are excluded. The host controls authorization and admission concurrency.

The process API is explicit, not the application's discovery default. The
real-tool experiment accepts `--transport process`. Its separate memory cohorts retain
wait4 child peaks and sealed-image bytes alongside parent RSS. Their sum is a
conservative bound; shared pages can be counted twice. Cold RSS remains parent
only. These observations do not certify physical PSS or a default switch.

Release discovery artifacts use size optimization (`opt-level = "s"`), fat
link-time optimization and one codegen unit. **Problem:** first process discovery hashes and copies the entire worker
into a sealed image, so linked bytes contribute to first-use CPU and retained
memory. **Decision:** optimize code size across crate boundaries during release builds;
artifact digests, sealed execution, panic containment and joined cleanup keep
the same contracts. The measured Linux x86_64 0.3.1 worker is 1,991,408 bytes,
down from 2,592,040 for 0.3.0 with the same source algorithm and locked
dependencies. This size observation is not a latency or default-selection
verdict. **Alternatives:** retain the previous release profile, or avoid the
copy by executing a verified pathname. The latter cannot preserve admitted
bytes when a package file is replaced. Build-time optimization retains that
integrity boundary and adds no runtime worker or cache.

The size profile leaves loop vectorization enabled and preserves panic containment.
The more aggressive `z` profile was also built and tested: its slightly smaller
worker had mixed first-use CPU results across the Python and incremental cases.
These diagnostic cohorts select a candidate for full real-tool verification;
they do not admit a discovery default or waive earlier performance failures.
The available profile settings are defined by the
[Cargo profile contract](https://doc.rust-lang.org/cargo/reference/profiles.html).

The same-runtime release comparison
retains 1,060 workers with equal outputs and joined cleanup. All five measured
fixtures improve warm median latency, but cold CPU, ranking CPU and retained
RSS guards still fail. Memory observations include collector records and
inherited child high-water marks. These limits remain explicit; release size
reduction does not select application discovery or establish whole-system speed.

The 0.3.2 size-profile comparison
retains another 1,060 fresh workers using the same installed ranking on both
arms. The worker is 1,578,352 bytes. All five fixtures pass the observed latency,
warm CPU and separate post-exec memory guards; Python first-use CPU remains
5.85% above Git and fails the 5% guard. Native discovery remains explicit.
Earlier failures remain evidence; these results cover transient Git fixtures,
not all repository layouts or the full application.

The 0.3.3 bootstrap comparison
retains 1,060 fresh workers with matching outputs and joined cleanup. All observed
CPU, warm latency and separate memory guards pass; multilingual cold P95 is
78.604 ms versus Git's 73.707 ms and fails the 5% guard. Both arms use the same
0.3.3 ranking/package, so this does not isolate a manifest-cache speedup over
0.3.2. The private manifest object graph is about 3 KB; actual process memory is
measured separately. Native discovery remains explicit, and all failed cohorts
stay retained.

Both transports also match Git on a 32,768-entry compressed index, deleted
tracked paths, fresh untracked files, conflict stages, skip-worktree entries,
and initialized/modified/deinitialized submodules. Calls preserve index bytes
and observe later edits. Plain directories and bare repositories explicitly
refuse discovery; status 2 remains a general discovery error. Initializing a
previously refused directory is visible on the next call. These semantic oracles
do not establish large-repository performance or select application fallback.


## Explicit installed stream worker

Version 0.5.0 packages the locked Rust source-discovery/extraction stream.
Its source authority is `rust/extraction/`; benchmark adapters contain no copy
of the Rust algorithm. Source distributions include the crate, sibling discovery
crate, generated catalog and upstream license. Builds keep grammar downloads
disabled and grammar safety patches enabled. External grammar binaries are not
bundled, verified or sealed by this release.

`open_stream_process()` returns a new caller-owned sealed executable from the
frozen installed build manifest. Import alone opens no image and starts no
child. `start(root, grammar_directory, timeout_ns, limits)` returns a child with
raw pipes; limits are four positive byte ceilings for discovery, request, file
frames and source reads. The caller owns protocol validation, deadline/cancel
handling and child retirement before closing the image. The package's stream
wrapper has no Tofu, numeric-library or parser-package import dependency.

On Linux, `python -m tofu_repo_kernel` exercises a real disposable Git worktree through
native discovery and Python source transport, without requiring an external Git
executable or grammar library. Other hosts report sealed stream process support
as unavailable instead of claiming an unexecuted probe. This certifies installed protocol and artifact
integrity. Mixed-language semantic checks still require explicit trusted grammar
artifacts. Product defaults, grammar immutability, launch-probed budgets and
whole-application performance admission remain separate requirements.

Stream protocol v3 retains discovery negotiation and uses indexed v2 source
requests. Successful native Git discovery, including empty output, stays
final. Ordinary discovery failure emits the reserved u64-max frame header;
unsupported environment, panic and timeout still refuse. The parent launches
`start_rg(root)` under the same image and overall task deadline. That directly
owned child changes directory after exec and replaces itself with
`rg --files -0 --no-messages`; there is no unowned grandchild. Its environment
includes explicit `RIPGREP_CONFIG_PATH` along with the declared local inputs.
Exit 0/1 (including empty output) supplies the complete path plan; absence or
other exit status requests a Rust walk with the canonical caller's ignore names,
depth and file limit. The ordinary framed source request follows that plan.
Every producer is joined before its pipes/image close. Discovery output and
negotiation bytes use the existing caller path/request ceilings.

The walk preserves hidden-directory/file exclusions, directory-symlink pruning,
file-symlink inclusion, readdir traversal order before the file limit, and
unreadable-directory behavior. Its indexed source requests preserve original
walk path bytes, including names that are not valid UTF-8.
The standalone Git-only discovery APIs retain their original refusal behavior.

The discovery data frame begins with codec byte 0 (Git UTF-8 replacement with deduplication),
1 (walk surrogateescape), or 2 (rg replacement preserving duplicate names), followed by NUL-delimited path bytes. The codec
byte is protocol overhead outside the path-byte budget. A v2 source request
carries `path_indexes`, never path strings. The worker validates every index
and duplicate before any source read; each response carries the matching
`path_index` and path-free tags. The parent restores the selected canonical
path on each tag. This preserves distinct invalid-byte walk names and their
replacement-character twins without sending lone surrogates through JSON.
Older stream manifests/launch flags refuse before use; the package version is
0.6.0. External grammar and full performance admission remain separate.

POSIX byte conversion is cfg-gated so the standalone package keeps compiling
its independent kernel APIs on other targets. The path-table module passes
Windows/MSVC and Darwin metadata checks; this is not full cross-platform build
or runtime certification. Sealed stream execution remains Linux-only.

The `system_repository_stream_portability_20261015.json`
records the limited cross-target checks and unchanged Linux byte-path/probe tests.


Explicit installed grammar selection (package 0.7+) uses a pinned optional bundle.
The build assembler supplies `TOFU_REPO_GRAMMAR_BUNDLE=/absolute/bundle` and
`TOFU_REPO_GRAMMAR_MANIFEST_SHA256=<sha256>` together, using a fresh build directory.
`grammars.json` follows `tofu_repo_kernel/grammar_bundle.schema.json`: it declares
registry version 1.20.0 and each library's filename, exact byte length and SHA256.
The directory also supplies `NOTICES.txt` for those libraries; packaging preserves
and pins the supplied notices, without asserting license-evidence completeness.
The wheel contains only the declared libraries. It never discovers a user cache
or downloads grammars. Bundles may cover a subset of the catalog; qualification
must name that coverage. `open_stream_process().start(..., grammar_directory=None,
...)` selects this installed authority. An explicit directory keeps the existing
trusted-directory behavior. A missing installed bundle refuses selection.

Problem: sealing the scanner left separately loaded grammar bytes mutable.
Decision: the owned Rust worker verifies its pinned manifest and lazily seals only
touched libraries into memfds. The upstream registry still resolves aliases and
C symbol overrides; null pointers and unsupported tree-sitter ABIs refuse loading.
Library handles and sealed descriptors live until worker exit, so parsers cannot
outlive code. Missing unlisted grammars remain file-local; a declared corrupt,
missing or incompatible library fails the entire selected scan. Existing task
cancellation joins that same child. No grammar directory, resident worker, parser
pool or application-level grammar mapping is added. Alternatives rejected:
child-owned symlink directories can survive SIGKILL; static linkage would require
sealing unused grammars with every scanner image. Current performance and memory
admission remain separate from these correctness/lifetime checks.

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.

# Rootless VM sandbox

`rootless_vm` is a harness-neutral isolation core for ordinary users without Docker,
KVM, FUSE, privileged mounts or a cloud sandbox. Outer confinement requires working
unprivileged user/network/PID namespaces. QEMU TCG runs a complete guest kernel; guest root grants no host root.

## Threat model

The protected assets are the host filesystem, host processes, local services,
and long-lived credentials. The guest and every benchmark task are hostile.

The current MVP enforces these defaults:

- TCG is mandatory. There is no implicit KVM or namespace fallback.
- QEMU itself runs in new unprivileged user, network, PID, IPC, and UTS
  namespaces. Before QEMU starts, a launcher enters a per-session minimal
  chroot, brings up only that network namespace's loopback, drops every
  capability, and sets `no_new_privs`. A trusted descriptor inventory opens
  before chroot; every inherited handle above stderr closes before exec, even
  above lowered descriptor limits. Inventory/closure failures refuse launch.
  Range caps were rejected: an inherited directory handle can bypass chroot.
  The project, home directory, host
  `/tmp`, host processes, and host IP network are absent from QEMU's view.
- The jail contains private reflink/copies of only the exact QEMU executable,
  loader/libraries, required firmware, native proxy bridge, and disk inputs.
  Every qcow2 backing layer is cloned and rebased to a jail-local path; each
  run writes only to a new private top overlay.
- Networking is fail-closed: `no-network` attaches no NIC; `public` uses QEMU
  user-mode NIC `restrict=on` and only an authenticated HTTP(S) proxy rejecting
  non-global IPs. Allowlist and arbitrary socket networking are not implemented.
- Secrets are forbidden from QEMU arguments, images, and persistent disks.
- QEMU and qemu-img receive a fixed minimal process environment, so Friday,
  proxy, shell, and provider credentials inherited by Harbor stop at the host
  adapter rather than propagating down the process tree.
- Session deletion requires a private parent, an exact child path, and an
  exact per-session marker.
- The namespace launcher arms a Linux parent-death signal before `execve` and
  fails closed if it was already adopted by host PID 1; launcher crashes do
  not leave an unowned QEMU process behind. PID 0 remains valid for the first
  process inside the private PID namespace.
- Runtime preflight launches a real TCG machine and completes a QMP exchange;
  checking only `qemu --version` is deliberately insufficient.
- QEMU seccomp sandbox support is mandatory for real sessions. The build is
  probed by launching QEMU with `-sandbox on`; a help-text match is not trusted.
- QEMU runs below `prlimit`: core dumps are disabled, file descriptors and
  address space are capped, and writable output files cannot grow without
  bound. The qcow2 ceiling uses the greater of the task's storage hint and the
  inherited backing disk's actual virtual size, plus 2 GiB of metadata
  headroom. A smaller task hint never shrinks the filesystem or causes QEMU to
  hit `RLIMIT_FSIZE` and remount a still-spacious ext4 guest `emergency_ro`.
- A tiny launcher sets Linux `no_new_privs` before `execve(QEMU)`. QEMU's
  sandbox denies obsolete and resource-control syscalls. Offline runs also deny
  process spawn and identity-changing syscalls. Public runs permit the exact
  libslirp guest-forward bridge subprocess; it inherits `no_new_privs`, the
  remaining seccomp filter, zero capabilities, and the secret-free environment.
  The harness never silently retries without seccomp.

This is defense in depth, not a proof that QEMU has no vulnerabilities. QEMU's
own [security documentation](https://www.qemu.org/docs/master/system/security.html)
does not treat TCG system emulation by itself as a supported security boundary;
the namespaces, chroot, capability removal, seccomp, and lack of host mounts
exist specifically so safety does not rely on the guest boundary alone. Every
downloaded artifact must still be pinned by digest or verified signature.

## Host storage capability boundary

Host scratch allocation is explicit through `rootless_vm.host_paths`: owner-root
scratch, `ROOTLESS_VM_TMPDIR`, `TOFU_EVAL_ROOT/.runtime/tmp`, then checkout
`.eval-runs/.runtime/tmp`. Host `/tmp`, `/var/tmp`, `/dev/shm`, symlink components,
and unsafe scratch permissions are rejected. Guest/jail `/tmp` remains private
and is not rewritten. The shared host policy keeps writable roots on DolphinFS
when the checkout resides there. `child_storage_environment` gives launchers,
preparation tools and the bootstrap private HOME/tmp/caches; Harbor freezes
those child paths in its `runtime_storage` manifest.

`rootless_vm.storage_probe` verifies named-file readback, fsync/rename, advisory
locking and a pathname Unix-socket round trip before dispatch/start. Some
DolphinFS deployments return ENOSYS for socket bind and ENOENT for reads of
unlink-while-open temporary files. Named files alone working does not qualify
such a volume for the current QMP/QGA/proxy transport. Fail before a task batch,
never relocate silently to host `/tmp`. An inherited-fd IPC implementation must
be independently validated across QGA, QMP, egress/control relays, credential
checks and teardown before it can replace pathname sockets; exposing QGA on
unauthenticated TCP or a host-global abstract socket is not an acceptable fix.


A diskless QEMU 11.1.0 probe has completed greeting, capabilities, query-status
and clean shutdown over `socket,id=qmp0,fd=0,server=off`, with zero capabilities,
seccomp filter mode 2 and `no_new_privs`. This proves the installed chardev can
accept a connected socketpair; it does not qualify the production QGA transport,
relay multiplexing, FD hand-off through namespaces, chroot or cancellation.
`-mon` still works in that version but is deprecated; production wiring should
verify its `monitor-qmp` object syntax against the pinned executable.
See the [evaluation owner](../evaluations/swebench/README.md) for the neutral
catalog, DolphinFS setup and self-contained offline verification commands.

## Isolated project command port

Problem: generic namespace PID 1 can ignore SIGTERM and leave detached children running after its waiter dies.
Decision: `project_command.run_project_command` takes an immutable private-jail specification with pinned runtime files,
explicit per-process limits and an original absolute deadline. It reuses `qemu_launcher` and `project_mod.run_command_process`;
shell strings are never evaluated on the host and unavailable isolation never falls back.
Stop, interrupt and timeout kill namespace init through a stable pidfd, then let unshare reap it. Parent/owner death uses SIGKILL.
A pinned supervisor proc directory verifies adoption before exec; all bootstrap handles then close.
Alternatives: SIGTERM/process-group-only teardown misses ignoring/detached children; start ticks differ across namespace clocks.
Python pidfd wrappers are preferred; audited 64-bit Linux syscall ABIs support older builds, with real self-probes and refusal on unsupported hosts.
File effects remain in the jail. This port is not a model tool or admitted by exploration programmes. Frozen task authority,
freshness/atomic project publication and aggregate fork-tree memory/PID/disk bounds remain required; per-process limits do not imply them.

## Rootless QEMU bootstrap

An ordinary x86-64 Linux user can build the required runtime into an explicit
private prefix. No system package manager, Docker daemon, `sudo`, setuid
helper, or write outside that prefix is used:

```bash
# Run from the DolphinFS checkout; do not substitute host /tmp.
export TOFU_EVAL_ROOT="$PWD/.eval-runs/benchmarks"
export TOFU_QEMU_ROOT="$TOFU_EVAL_ROOT/rootless-qemu"
scripts/bootstrap_rootless_qemu.sh \
  --prefix "$TOFU_QEMU_ROOT" \
  --jobs 8

export ROOTLESS_VM_QEMU=$TOFU_QEMU_ROOT/runtime/bin/qemu-system-x86_64
export ROOTLESS_VM_QEMU_IMG=$TOFU_QEMU_ROOT/runtime/bin/qemu-img
export ROOTLESS_VM_EGRESS_BRIDGE=$TOFU_QEMU_ROOT/runtime/bin/rootless-egress-bridge
```

The bootstrap pins and SHA-256 verifies QEMU 11.1.0, libseccomp 2.6.0, GNU
gperf 3.3, micromamba 2.3.3, and an exact libslirp package. Micromamba's root,
package cache, compiler
environment, sources, build trees, logs, libraries, and installed binaries all
live below the selected mode-0700 prefix. The build environment comes from an
explicit Conda lock containing an exact URL and SHA-256 for every package; no
dependency solving is performed. The installed binaries receive an explicit
runtime search path for the private libraries, and the script finishes by
running the real QMP/seccomp `doctor` probe. A verified completion marker makes
subsequent invocations skip package relinking and compilation while still
repeating that live probe.

The bootstrap needs network access while acquiring/building the trusted
runtime. A benchmark can then run offline or use the explicit public proxy.
For mirrored or air-gapped acquisition, put the four exactly named archives in
one directory and pass `--source-cache /that/directory`; hashes are checked
identically.
The script refuses a symlink, filesystem root, or a non-empty unmarked prefix,
and interrupted builds are resumable without deleting unrelated data.

## Trusted base disk bootstrap

The repository also pins the previously external guest-disk trust input. The
lock at `rootless_vm/alpine_3_24_x86_64.lock.json` records the exact
official Alpine 3.24.1 BIOS/cloud-init image plus the URL, byte size, and digest
of every APK needed for QEMU Guest Agent, Docker, containerd, and runc. Build it
as the same ordinary user that runs the benchmark:

```bash
mkdir -m 700 $TOFU_EVAL_ROOT/.base-disk

/path/to/eval-venv/bin/python -m rootless_vm build-base \
  --lock rootless_vm/alpine_3_24_x86_64.lock.json \
  --output $TOFU_EVAL_ROOT/.base-disk/alpine-docker.qcow2 \
  --cache-root $TOFU_EVAL_ROOT/.base-disk/downloads \
  --state-root $TOFU_EVAL_ROOT/.base-disk/state \
  --qemu "$ROOTLESS_VM_QEMU" \
  --qemu-img "$ROOTLESS_VM_QEMU_IMG" \
  --json
```

Downloads happen in the host-side private cache and must satisfy their lock.
Installation then runs in a networkless, namespace/chroot/seccomp-confined TCG
VM from a read-only NoCloud ISO. A failed command forces shutdown without a
provisioning marker. Before publishing the standalone qcow2, the builder boots
it in a second disposable VM and checks the marker, QGA service, Docker CLI,
runc, and a live `docker info`. The adjacent mode-0600 JSON records the output
SHA-256, complete lock SHA-256, QEMU version, and verification output. The
recipe is reproducible and fully auditable; output bytes need not be identical
because a real OS boot writes timestamps and filesystem state.

## Diagnostic

```bash
export ROOTLESS_VM_QEMU=/absolute/path/qemu-system-x86_64
export ROOTLESS_VM_QEMU_IMG=/absolute/path/qemu-img
export ROOTLESS_VM_EGRESS_BRIDGE=/absolute/path/rootless-egress-bridge
python -m rootless_vm doctor --json
```

`qemu_sandbox`, `user_network`, `host_confinement`, and
`no_new_privileges` must be `true`, while `effective_capabilities` must be all
zeroes. A binary that merely advertises features in help is insufficient
because doctor performs a real QMP launch probe and reads the live process
security state.

### Why other rootless backends may still fail

"Unprivileged user namespaces are enabled" is not a sufficient preflight for
Docker-compatible isolation. Test the exact namespace combination and backend
operation before downloading task images. A common managed-host policy allows
`user+net` namespaces but rejects every mount in a user mount namespace; that
blocks rootless OCI runtimes even though a simple user-namespace probe passes.

| Backend | Additional host authority | Fail-closed decision |
| --- | --- | --- |
| KVM/Firecracker | A usable `/dev/kvm` | Use only after an explicit device and live-launch probe; absence is not an error for the TCG backend. |
| Rootless Docker/Podman | Working user **and mount** namespaces, plus any required UID mapping helpers | Reject when a real rootless container launch fails; access to a root-owned Docker socket is not rootless. |
| gVisor `runsc` | Working rootless mount setup; network mode has additional restrictions | Do not use `runsc do` for hostile tasks: gVisor documents that this convenience mode exposes the host filesystem read-only by default. Do not enable `--TESTONLY-unsafe-nonroot`. |
| QEMU TCG in this project | User and network namespaces, QEMU seccomp, chroot, and no host mounts | Supported fallback when the live QMP, confinement, and proxy probes all pass. |

The relevant upstream constraints are documented in gVisor's
[rootless guide](https://gvisor.dev/docs/user_guide/rootless/),
[security introduction](https://gvisor.dev/docs/architecture_guide/intro/), and
[platform guide](https://gvisor.dev/docs/user_guide/platforms/). In particular,
the built-in `--rootless` mode is primarily intended for `runsc do`, and its
network and filesystem behavior is not a drop-in replacement for a securely
configured OCI bundle. A failed mount/chroot probe must therefore fall back to
the confined TCG path, not to an unsafe flag or an unconfined native process.

## Harbor and Tofu adapters

The optional adapters remain separate from the isolation core:

- `rootless_vm.harbor_environment:RootlessQemuEnvironment` implements Harbor's
  `BaseEnvironment` over QEMU Guest Agent. It supports prebuilt Linux images,
  file/directory transfer, command execution, shared verifier runs, strict
  `network_mode = "no-network"`, and proxy-only `network_mode = "public"`.
- `rootless_vm.harbor_tofu_agent:TofuHostAgent` runs Tofu's model dispatcher in
  the host process and exposes bounded `run_command` and `submit_result` tools.
  A submission is accepted only after its validation command exits zero.
  Provider keys remain in Tofu and are never copied to Harbor environment
  variables or guest files. Persistent audit records keep an allowlisted route
  identity and timing fields, redact model reasoning to a length and SHA-256,
  and discard trace IDs and credential-derived fields such as key suffixes.
- `rootless_vm.harbor_deepseek_minimal_agent:DeepSeekMinimalHostAgent` keeps the
  same host-only credential boundary while reproducing DeepSeek Harness
  Minimal's one-line prompt, persistent `bash`, `str_replace_editor`, 16,000
  character tool-output cap, and thinking-tool replay contract. Both host
  adapters checkpoint an ATIF-v1.7 `trajectory.json`; explicit reasoning is
  represented only by a redaction marker, character count, and digest.

The environment takes two immutable inputs: a trusted VM base disk and an ISO
containing one or more Docker archives. Both accept an expected SHA-256. The
first cache build loads the archives into a Docker daemon entirely inside an
offline guest, exports a merged rootfs, and publishes the resulting qcow2 by
recipe digest. Repeated trials use that immutable disk only as a qcow2 backing
file and start the task with `runc`; offline tasks receive a new network
namespace, while public tasks share only the outer guest's restricted NIC.
Docker does not start on the hot path. Public mode requires this prepared runc
cache and rejects the Docker fallback. An optional digest-pinned Python image can seed
`/usr/local` for verifiers that otherwise download Python tooling during a run.

Example (paths and hashes are intentionally explicit):

```bash
export ROOTLESS_VM_QEMU=$TOFU_QEMU_ROOT/runtime/bin/qemu-system-x86_64
export ROOTLESS_VM_QEMU_IMG=$TOFU_QEMU_ROOT/runtime/bin/qemu-img
export ROOTLESS_VM_EGRESS_BRIDGE=$TOFU_QEMU_ROOT/runtime/bin/rootless-egress-bridge
export PYTHONPATH="$PWD:/path/to/harbor/src"

python -m rootless_vm run \
  --harbor /path/to/harbor \
  --task-path /path/to/task-with-no-network-policy \
  --base-disk /images/trusted-base.qcow2 \
  --base-disk-sha256 BASE_SHA256 \
  --payload-iso /images/task-images.iso \
  --payload-iso-sha256 ISO_SHA256 \
  --task-image org/task:pinned-export-tag \
  --python-runtime-image python@sha256:DIGEST \
  --state-root $TOFU_EVAL_ROOT/rootless-vm-state \
  --cache-root $TOFU_EVAL_ROOT/rootless-vm-cache \
  --jobs-dir $TOFU_EVAL_ROOT/rootless-vm-jobs \
  --model deepseek-v4-flash
```

The launcher creates state/cache/jobs roots at mode 0700, fixes concurrency at
one VM by default, and never places model credentials in process arguments.
Use `--dry-run` to inspect the generated Harbor command and `--oracle` for a
verifier plumbing check without a model call.

`--task-image` is the exact local name recorded inside the Docker archive, not
necessarily its registry pull reference. The verified payload ISO digest pins
the archive bytes; the registry digests below record upstream provenance. This
separation avoids a misleading failure where an image loaded under a tag is
then inspected under a registry digest alias that Docker did not import.

The task may declare `network_mode = "no-network"` or `network_mode = "public"`
(Harbor also derives public mode from Terminal-Bench's `allow_internet=true`).
Allowlist networking is rejected rather than silently receiving a different
policy. The state root must be a real mode-0700 directory.

### Public egress boundary

Public mode is intended for package managers and tools that honor
`HTTP_PROXY`/`HTTPS_PROXY`; loopback names and addresses alone are listed in
`NO_PROXY` so a task and its verifier can reach services inside the same
container. The launcher also enables the disposable guest's loopback device
before starting that container. It is not a general-purpose network. QEMU
`restrict=on` blocks direct connections to the host and Internet. QEMU's own
host network namespace also has no external interface. The guest sees only an
ephemeral proxy credential for `10.0.2.100:3128`; a fixed native bridge reaches
the external sanitized proxy through a private Unix socket. Host corporate
proxy credentials, if present, are sent to the sanitized proxy child over a
pipe and never enter QEMU, the guest, command arguments, or persistent files.

The host proxy resolves names itself, rejects loopback, RFC1918, link-local,
multicast, metadata, and mixed public/private answers, and connects using the
validated numeric address first. When a required corporate parent proxy refuses
CONNECT to that numeric destination, the proxy retries CONNECT to the already
DoH-validated hostname; TLS still pins the session with SNI and certificate
verification, so DNS is never delegated for a name that failed public
resolution. HTTPS remains end-to-end TLS through CONNECT. When this host itself
requires a corporate proxy that rejects numeric port 80, plain HTTP is upgraded
on the host side to certificate-verified HTTPS (numeric CONNECT, then hostname);
the guest still receives the requested HTTP response. Without the hostname
CONNECT retry, verifier `apt-get` of curl/uvx fails closed with HTTP 502
`cannot securely upgrade public HTTP destination` and the trial becomes a false
model zero (`curl: command not found` / `uvx: command not found`).
Connections, headers, idle
time, relay backpressure, and aggregate bytes are bounded. Established relay
writes allow 120 seconds for a TCG guest to resume draining before the tunnel
is closed. Plain proxy requests are restricted to
TCP 80 and CONNECT tunnels to TCP 443; SSH, mail, arbitrary TCP, UDP, ICMP, and
inbound networking remain unavailable.
All sessions under one private state root also share a 16-connection upstream
gate. The mode-0600 advisory locks contain no traffic or credentials; they
prevent parallel package managers in many VMs from multiplying into a parent
proxy reset storm while retaining per-session byte limits and authentication.
Agent package installs retain a generous transient-download retry budget. The
replaceable verifier phase limits uv to two concurrent downloads and uses a
bounded 20-attempt, 60-second network policy. If it still cannot bootstrap, the
result is infrastructure-invalid and the exact trial slot is regenerated
instead of becoming a false model zero.

Host/guest file transfers use 128 KiB QGA chunks, a byte ceiling, and one
monotonic twenty-minute deadline for the whole transfer. This avoids both the
old 32 KiB request storm and oversized JSON/base64 frames that can stall
virtio-serial under pure TCG, without restoring an unbounded per-chunk timeout.
Downloaded files stage in a private directory on the destination filesystem
before atomic replacement. A shared artifact volume may differ from `/tmp`;
cross-mount publication must work, interrupted transfers preserve the previous
complete file, and staging is reclaimed on both success and failure.
Guest proxy bypass lists include loopback and the exact hosts of declared
guestfwd services. Model control-plane traffic must use that declared route;
public egress policy, QEMU `restrict=on`, and service-forward validation remain
the authority. No private-network ranges or undeclared hosts are exempted.
The global egress gate retains one directory descriptor and at most one slot
descriptor per contender; failed probes close immediately. Descriptor use must
not multiply by the configured slot count or bypass the existing proxy limits.

### Planned verifier-cache boundary

Large Terminal-Bench verifiers currently reinstall uv, Python, PyTorch, and
other public dependencies in every disposable overlay. The safe acceleration
design is a task-checksum-keyed, immutable verifier cache disk built in a
separate confined VM from public dependency declarations only. It must contain
neither `/tests`, private fixtures, the solution, nor an agent workspace. The
disk is attached only after the agent phase and each trial receives a private
copy-on-write top layer, so a verifier cannot poison another trial or the
published cache. Hidden tests remain transferred separately at verification
time. The manifest must pin the task checksum, package inputs, builder/runtime
digests, and output-disk SHA-256; a mismatch falls back to the ordinary network
bootstrap rather than using an ambiguous cache. This preserves the benchmark
information boundary while removing repeated package downloads and most
dependency installation from the TCG hot path.

## Terminal-Bench 2.1 validation

The repository includes `scripts/rootless_terminal_bench_21.py` for a complete,
resume-friendly local workflow. `prepare-assets` verifies and stores each OCI
image as an ISO, `prepare-cache` builds immutable per-image qcow2 backing disks,
and `write-config` emits a secret-free Harbor configuration. When `--workers`
or `--concurrency` is omitted, the command takes one snapshot through the
shared launch-time resource probe: affinity/cgroup CPU, physical/cgroup memory
capacity and current headroom, and free space on the state/cache volume. It
then budgets against the largest selected task, preserves at least 2 GiB of
memory and 8 GiB of disk for the OS/browser, caps the adaptive result at four,
and falls back to one on any probe failure. The decision and raw probe values
are written to a mode-0600 `*.resources.json` file beside the generated assets,
cache, or Harbor config. Explicit `--workers`, `--concurrency`, and
`--agent-concurrency` remain dedicated-host overrides with their existing hard
ceilings.

A useful dedicated-host pure-TCG pipeline can keep more live trial VMs than
model agents, so verifier work overlaps later inference without increasing
provider pressure. The example below uses the adaptive personal-computer
default; add `--concurrency 8 --agent-concurrency 4` only after an operator has
reserved that capacity:

The harness is an explicit score/provenance dimension. Inspect the registry with
`list-harnesses`; never merge different profiles into one score ledger:

| `--harness` | Execution boundary | Trajectory | Intended comparison |
| --- | --- | --- | --- |
| `deepseek-minimal` | model and credentials host-only; tools in QEMU | ATIF-v1.7 plus redacted host dispatch audit | Published DeepSeek Harness Minimal result |
| `tofu` | model and credentials host-only; tools in QEMU | ATIF-v1.7 plus redacted host dispatch audit | Tofu recovery/checkpoint harness |
| `tofu-kimi` | production AgentRuntime and Kimi credentials host-only; two exclusive client tools in QEMU | native events + runtime/tool audit + ATIF-v1.7 | Formal Tofu candidate over the same Meituan Kimi |
| `codex-kimi` | pinned Codex in QEMU; Kimi credential in launcher-owned host proxy | raw Codex JSONL + tagged proxy usage + ATIF-v1.7 | Codex CLI 0.149.1 over the same Meituan Kimi |
| `codex` | Codex CLI, model access, and credential inside QEMU | Harbor's native ATIF-v1.7 conversion | Harbor Codex installed agent |
| `claude-code` | Claude Code CLI, model access, and credential inside QEMU | Harbor's native ATIF-v1.7 conversion | Harbor Claude Code installed agent |

The `codex` and `claude-code` profiles are default-deny because Harbor must expose a model
credential to the guest process. Configuration requires
`--allow-guest-credentials`; use only an explicitly authorized, short-lived,
scope-limited credential. The flag records authority but cannot make a
long-lived credential ephemeral. Tofu, DeepSeek Minimal, `tofu-kimi`, and
`codex-kimi` never need it. Both formal Kimi profiles are intentionally rejected by the legacy
`write-config`/single-task launchers; use `python -m evaluations.swebench run`
so the frozen runtime, credential boundary, attempt ledger, and evidence
sharding cannot be skipped. The plain `tofu` profile remains the diagnostic
recovery/checkpoint loop; only `tofu-kimi` is the production-runtime candidate.

The production adapter awaits durable event/audit writes off Harbor's shared
asyncio loop, using its bounded executor with at most one outstanding operation
per adapter. Every event still receives `fsync`; cancellation drains the owned
write before closing or hashing artifacts. `tofu-kimi-evidence/io-metrics.json`
records event-write count, total and maximum latency including executor wait.
Observer delivery lag is measured separately from provider inference latency;
a slow evidence filesystem must not silently consume other trials' deadlines.

The published DeepSeek-V4-Flash reference uses the exact one-line system prompt
`You are a helpful software engineer assistant.`, persistent `bash`, and
`str_replace_editor`. `TofuHostAgent` intentionally has a different contract
(`run_command` plus validated `submit_result`) and recovery policy, so its local
result must not be presented as a Minimal reproduction.

The Minimal profile uses DeepSeek Harness's 256,000-token default output cap.
For the physically selected Meituan deployment it records the observed 393,216
combined context limit and conservatively shrinks the per-turn completion
budget as the prompt grows. This preserves the official cap for ordinary turns
while preventing `input + max_tokens` from becoming a harness-induced HTTP 400.
The reservation starts at 1.5× the heuristic prompt estimate and raises that
ratio from observed provider token counts, with an additional fixed 2,048-token
margin for serialization variance.

```bash
python scripts/rootless_terminal_bench_21.py write-config \
  --tasks-root /absolute/terminal-bench/tasks \
  --assets-root $TOFU_EVAL_ROOT/tb21/assets \
  --control-root $TOFU_EVAL_ROOT/tb21/control \
  --state-root $TOFU_EVAL_ROOT/tb21/state \
  --cache-root $TOFU_EVAL_ROOT/tb21/cache \
  --jobs-dir $TOFU_EVAL_ROOT/tb21/jobs \
  --base-disk /absolute/trusted-base.qcow2 \
  --qemu "$ROOTLESS_VM_QEMU" \
  --qemu-img "$ROOTLESS_VM_QEMU_IMG" \
  --job-name deepseek-v4-attempt-1 \
  --attempts 1 \
  --global-dispatch-concurrency 4 \
  --egress-global-concurrency 16 \
  --max-retries 0 \
  --harness deepseek-minimal \
  --model deepseek-v4-flash \
  --reasoning-effort max \
  --temperature 1 \
  --top-p 0.95 \
  --runtime-timeout-multiplier 4

python scripts/rootless_terminal_bench_21.py run \
  --harbor /absolute/harbor \
  --config $TOFU_EVAL_ROOT/tb21/control/deepseek-v4-attempt-1.json
```

For a cheap plumbing smoke test, all three preparation/configuration phases
accept repeated `--task NAME`. `prepare-assets` uses pinned `pycdlib` when
`--genisoimage` is omitted, so ISO creation needs neither a host package nor
root. Completed image assets are retained and merged into the immutable index;
the full 89-task preparation therefore resumes rather than restarting. If
Docker Hub throttles anonymous downloads, repeat `--registry-mirror HOST` to
try explicit mirrors before the origin. Every selected endpoint and resolved
manifest digest is retained in `asset.json`; the guest still loads only the
digest-checked OCI payload.

After an interruption or infrastructure-invalid trial, generate replacement
configs from the current audited ledger instead of maintaining a static retry
list:

```bash
python scripts/rootless_terminal_bench_21.py write-retry-configs \
  $TOFU_EVAL_ROOT/tb21/jobs/deepseek-v4-attempt-* \
  --tasks-root /absolute/terminal-bench/tasks \
  --template $TOFU_EVAL_ROOT/tb21/control/deepseek-v4-attempt-1.json \
  --output-root $TOFU_EVAL_ROOT/tb21/control \
  --job-prefix deepseek-v4-resume-1
```

The command prints a manifest followed by configs suitable for `run-series`.
It freezes exactly the missing valid attempts per task, routes known TCG timing
classes to bounded profiles, and refuses a ledger that already has surplus or
unexpected valid samples.

`analyze` separates verifier-confirmed model failures from provider errors,
dependency-bootstrap failures, local watchdog failures, routing/privacy
violations, and known sub-second TCG timing distortion. Every detail includes a
root-cause `layer`, confidence, evidence codes, and retry scope. The aggregate
reports model, harness, provider, environment, verifier, routing, and security
layers separately. `score` accepts only audited exact-model attempts with
completed verification and exact binary rewards. Use `--output` under an
owned DolphinFS directory to retain the same ledger atomically with mode 0600.
`analyze` accepts that output contract as well. One-attempt and partial-task
runs are diagnostics, not leaderboard reproductions. Legacy `formal_score_percent`
is always null: final result snapshots cannot attest that internal retries did
not replace failures, even with pinned checksums and an exact 89×5 cohort.
Directory names are not an attempt ledger. Missing or surplus attempts, mixed
verifier/agent budgets and changed clocks remain explicit diagnostic reasons;
raw/provisional percentages are never presented as audited model scores.
These local acceptance rules are not a claim about current upstream submission
policy. Keep original jobs when creating diagnostic retry jobs; never use
replacement successes to hide failures. Calibrate budgets in a pilot and then
freeze a fresh complete cohort through the registered comparison launcher.
An agent timeout is scoreable only when a completed verifier returns 0 or 1:
a passing workspace remains a pass. Missing rewards or unresolved verifier
exceptions are unscored, not an assumed model zero.

DeepSeek Minimal host agent 1.0.2 also closes a timeout-cleanup defect in
1.0.1: terminating only the persistent Bash leader could leave its foreground
compiler, package manager, or training process alive in the disposable guest.
The reset now sends TERM and then KILL to the complete guest process group.
Analysis classifies every 1.0.1 trial whose structured transcript contains a
persistent-Bash timeout as `harness_timeout_process_leak`, regardless of its
numeric reward. Version 1.0.2 also preserves every provider-reported tool call
instead of retaining at most 16. A 1.0.1 trial is score-compatible only when it
has a structured dispatch audit, contains no persistent-Bash timeout, and its
observed per-turn maximum is below that old truncation boundary. The score and
retry-plan artifacts expose the accepted legacy count. This path-conditional
compatibility avoids both silently trusting contaminated workspaces and
needlessly rerunning unaffected trials.

Collect portable, privacy-safe trajectories and matching root-cause records
after any run:

```bash
python scripts/rootless_terminal_bench_21.py collect-trajectories \
  $TOFU_EVAL_ROOT/tb21/jobs/deepseek-v4-attempt-1 \
  --output-root $TOFU_EVAL_ROOT/tb21/trajectory-bundle \
  --expected-model deepseek-v4-flash-meituan
```

The bundle contains one sanitized ATIF file and `attribution.json` per trial,
plus a JSONL manifest and aggregate summary. It removes explicit reasoning and
credential-shaped values (including bearer and `sk-` tokens) from the copied
trajectory without rewriting Harbor's source artifact. Every bundle directory
is mode 0700 and every retained file is mode 0600. Interrupted legacy host runs
without ATIF are projected from their redacted dispatch audit and marked
`projected_host_audit`; absent or invalid traces remain visibly missing instead
of being fabricated. When an OpenAI-compatible response leaves the inactive
`input_tokens`/`output_tokens` aliases at zero but reports real counts through
`prompt_tokens`/`completion_tokens`, the collected copy normalizes those counts
from the matching redacted host audit and records
`usage_normalized_from_host_audit`; the source trajectory remains immutable.

`--agent-concurrency` is a per-job Harbor limit. Overlapping retry jobs share
the separate `--global-dispatch-concurrency` gate under the private control
root, so their limits cannot multiply into a Friday 429 storm. The gate is a
small set of mode-0600 advisory lock files in a mode-0700 directory: it opens
no service, stores no request data or credentials, and records each request's
queue delay as `gate_wait_ms` in the redacted audit transcript.

Timeout attribution treats that queue as causal only when its measured wait is
material: at least 120 seconds and at least 5% of the agent execution window.
Smaller waits remain model timeouts; material waits are classified as
`environment_dispatch_contention` and retried at low load.

The runtime multiplier compensates for pure-TCG wall-clock slowdown. It scales
Harbor's agent and verifier budgets, the environment's inner watchdog, and any
explicit timeout requested through an agent terminal tool exactly once. Every
scaled command still has a 1,800-second hard ceiling. The generated agent
metadata records both the default timeout and multiplier so legacy
under-scaled package-install failures can be audited without excusing failures
from the corrected runtime. New transcript tool records also include the
effective bounded timeout used for that individual command. A TCG-calibrated
result has different runtime conditions and is local diagnostic evidence;
current upstream policy must be independently verified before submission.

Public guest egress remains proxy-only and byte-bounded. The runtime also sets
Git/libcurl's `http.proxyAuthMethod=basic` through process-only Git config so
authenticated CONNECT works consistently without writing credentials into the
task image. Persisted agent transcripts redact credential-bearing URLs, proxy
authorization headers, and secret-shaped assignments; the model still receives
the live command result required to operate inside its disposable VM.
The direct-runc hot path also bind-mounts a runtime-owned, read-only
`/etc/hosts` containing only the standard localhost records. This matches the
Docker behavior expected by tasks without trusting an image-owned path or
exposing any host names.
Main-container commands run through Bash, matching Harbor's Docker adapter;
using POSIX `sh` here is not equivalent because some pinned benchmark scripts
depend on Bash features or on Bash's fallback for a canary comment placed
before the nominal shebang.

For a verifier proven to hit the scaled budget with no reward, use
`--verifier-timeout-multiplier` on its replacement job. This changes only the
local verifier wall clock; the agent phase and model-requested tool commands
retain `--runtime-timeout-multiplier`, preventing extra model deliberation from
being smuggled into a timing calibration.
Tasks that themselves launch a full virtual machine (for example
`qemu-startup`) run as TCG-on-TCG in this rootless backend because the host has
no KVM device. A timeout with a demonstrably live inner VM is classified
separately and must be retried at low load with a dedicated wall-clock scale;
it is not timing-equivalent to the Docker reference environment.
The timing classifier is bounded rather than an unlimited excuse: progressing
Caffe training and an active CompCert or Stan build are invalid only below an
8× agent scale, and an active nested-QEMU trial only below 16×. Exhausting those
calibrated retry budgets is counted as a model timeout for diagnosis. This
classification is not a license to pool selectively replaced attempts into a
formal score; all such replacement cohorts are diagnostic.

After any interrupted or infrastructure-invalid batch, `plan-retries` compares
the audited results with the pinned task checkout and groups tasks by the exact
number of replacement attempts still required for diagnosis. This reduces
pilot compute but does not prove an unbiased final five-shot cohort.
It emits explicitly diagnostic bounded retry profiles:
ordinary transient failures stay at 4×, verifier-only timeouts use an 8×
verifier budget, active pure-TCG builds and unscored agent-plus-verifier dual
timeouts run two at a time at 8×, and proven TCG-on-TCG boots run alone at 16×.
The `cancel-async-tasks` verifier's fixed 500 ms signal window gets its own
single-VM, `virtual_time_shift=0` instruction-counted clock profile; merely
reducing host contention does not repair guest startup-time distortion.
For each emitted group, the verifier multiplier is reduced when necessary so
the task's native verifier budget remains within the same 24-hour hard ceiling
enforced by `write-config`; the planner therefore cannot recommend a config
that the safety validator rejects.
The profiles are recommendations rather than score exemptions; the
classifier's calibrated upper bounds still decide when a later timeout becomes
a valid model failure.

The Tofu adapter's round cap defaults to 4,096 and is intentionally
non-binding; Harbor's dataset-derived agent timeout remains authoritative, as
in the upstream DeepSeek loop. Results from the legacy 128-round cap are
classified as harness-invalid and replaced rather than counted as model
timeouts.
Long tool trajectories are checkpointed before 300,000 prompt tokens by
default (`--context-checkpoint-tokens`). The disposable VM and its files keep
running while old chat turns are replaced with the original task plus recent
terminal evidence. A provider `PromptTooLongError` triggers the same recovery
once as a defensive fallback. The threshold leaves room below the Meituan
route's observed context ceiling for the configured 32,768-token completion;
checkpoint count and threshold remain in the audit metadata.
If `submit_result` supplies the exact same command that immediately preceded
it with exit status zero, the adapter reuses that audited result instead of
running an expensive compile or network-backed test twice. Any intervening or
different command invalidates the reuse; the official verifier still runs
independently afterward.
The tool schema also requires behavioral validation through the artifact's
real consumer or interface. Existence, log, and grep-only checks are called out
as insufficient; this prevents a plausible training log from masking a saved
model/configuration incompatibility that only an actual load would reveal.
The agent is also told to place a minimally functional deliverable at every
required final path early and then iterate there. This prevents long tuning
trajectories from expiring with their only implementation under a temporary or
reference filename. Required long computations start once their prerequisites
are known; cheap representative smoke checks are used during iteration, with a
single full-scale validation reserved for the final artifact.

Verification must distinguish an Oracle plumbing check from a model trial,
and unmodified upstream verification from an offline bootstrap replacement.
An offline test with unchanged assertions but replaced package installation
is diagnostic evidence, not an official benchmark result. Keep the task/image
digests, network policy, provider identity and phase timings in run artifacts;
historical timings are not a promise about a different host or provider.

The compact agent writes a reasoning-redacted, credential-free provider audit
record while retaining the working reasoning only in the host process for the
next model turn. Repeated working context can be reset after diagnosed
no-progress loops without losing the disposable VM workspace.

The agent also detects two consecutive output-only truncations or three
identical terminal results. It then starts a fresh reasoning context while
preserving the VM workspace and a bounded tail of terminal evidence. Empty
assistant responses receive a placeholder before the retry prompt, avoiding
same-role message repair in the Friday client. Full evaluation configs select
their output budget explicitly; truncated output-only turns receive a bounded
retry and can trigger a fresh context recovery. Provider latency is measured
separately: local QEMU/cache optimization cannot remove remote variance.

## Current limits

- Pure TCG is substantially slower than KVM.
- Prebuilt single-container Linux tasks and the pinned single-stage literal-FROM
  Dockerfile shape used by SWE-bench Verified are implemented. Compose, GPUs,
  arbitrary Dockerfiles, raw TCP/UDP guest networking, and network allowlists
  fail closed.
- OCI acquisition and ISO assembly are automated by the SWE-bench preparation
  CLI using a checksum-pinned user-local crane and pinned pure-Python pycdlib.
  Trusted base-image provisioning remains a separate digest-pinned input.
- The current per-task exported-rootfs cache favors isolation and resumability
  over cross-image layer deduplication. A full 500-task SWE-bench cache needs
  substantially more disk than Docker's shared content store; dataset-level
  content-addressed backing is the main remaining scale optimization.
- Address-space, descriptor, core, and file-size limits are enforced, but a
  cross-trial aggregate CPU quota still belongs in a future supervisor.
- Public mode must allow QEMU/libslirp to create the fixed per-connection proxy
  bridge process. `no_new_privs`, zero capabilities, a sanitized environment,
  fixed arguments, seccomp, and prlimits constrain it, but offline mode retains
  the smaller `spawn=deny` syscall surface.
- QEMU's seccomp sandbox materially narrows a device-emulation exploit, but no
  userspace hypervisor can promise elimination of every future vulnerability.
  The supported safety claim is the layered host confinement above, not TCG
  alone.

The split keeps the core reusable by other local evaluation harnesses and
makes the remaining packaging work independent of Harbor and Tofu.

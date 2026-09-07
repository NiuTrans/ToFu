# Rootless VM sandbox

`rootless_vm` is a small, harness-neutral isolation core for machines where an
ordinary user cannot use Docker, KVM, FUSE, mount namespaces, or a cloud
sandbox. It runs a complete guest kernel with QEMU TCG. Root inside the guest
does not grant root on the host.

## Threat model

The protected assets are the host filesystem, host processes, local services,
and long-lived credentials. The guest and every benchmark task are hostile.

The current MVP enforces these defaults:

- TCG is mandatory. There is no implicit KVM or namespace fallback.
- QEMU itself runs in new unprivileged user, network, PID, IPC, and UTS
  namespaces. Before QEMU starts, a launcher enters a per-session minimal
  chroot, brings up only that network namespace's loopback, drops every
  capability, and sets `no_new_privs`. The project, home directory, host
  `/tmp`, host processes, and host IP network are absent from QEMU's view.
- The jail contains private reflink/copies of only the exact QEMU executable,
  loader/libraries, required firmware, native proxy bridge, and disk inputs.
  Every qcow2 backing layer is cloned and rebased to a jail-local path; each
  run writes only to a new private top overlay.
- Networking is fail-closed. `no-network` attaches no NIC. `public` attaches a
  QEMU user-mode NIC with `restrict=on`; its only explicit route is an
  authenticated HTTP(S) proxy that rejects non-global IP addresses. Allowlist
  and arbitrary socket networking are not implemented.
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

## Rootless QEMU bootstrap

An ordinary x86-64 Linux user can build the required runtime into an explicit
private prefix. No system package manager, Docker daemon, `sudo`, setuid
helper, or write outside that prefix is used:

```bash
scripts/bootstrap_rootless_qemu.sh \
  --prefix /absolute/private/rootless-qemu \
  --jobs 8

export ROOTLESS_VM_QEMU=/absolute/private/rootless-qemu/runtime/bin/qemu-system-x86_64
export ROOTLESS_VM_QEMU_IMG=/absolute/private/rootless-qemu/runtime/bin/qemu-img
export ROOTLESS_VM_EGRESS_BRIDGE=/absolute/private/rootless-qemu/runtime/bin/rootless-egress-bridge
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
mkdir -m 700 /absolute/private/rootless-base

/path/to/eval-venv/bin/python -m rootless_vm build-base \
  --lock rootless_vm/alpine_3_24_x86_64.lock.json \
  --output /absolute/private/rootless-base/alpine-docker.qcow2 \
  --cache-root /absolute/private/rootless-base/downloads \
  --state-root /absolute/private/rootless-base/state \
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
export ROOTLESS_VM_QEMU=/opt/rootless-qemu/runtime/bin/qemu-system-x86_64
export ROOTLESS_VM_QEMU_IMG=/opt/rootless-qemu/runtime/bin/qemu-img
export ROOTLESS_VM_EGRESS_BRIDGE=/opt/rootless-qemu/runtime/bin/rootless-egress-bridge
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
  --state-root /private/rootless-vm-state \
  --cache-root /private/rootless-vm-cache \
  --jobs-dir /private/rootless-vm-jobs \
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
validated numeric address. HTTPS remains end-to-end TLS through CONNECT. When
this host itself requires a corporate proxy that rejects numeric port 80,
plain HTTP is upgraded on the host side to certificate-verified HTTPS; the
guest still receives the requested HTTP response. Connections, headers, idle
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
  --assets-root /private/tb21/assets \
  --control-root /private/tb21/control \
  --state-root /private/tb21/state \
  --cache-root /private/tb21/cache \
  --jobs-dir /private/tb21/jobs \
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
  --config /private/tb21/control/deepseek-v4-attempt-1.json
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
  /private/tb21/jobs/deepseek-v4-attempt-* \
  --tasks-root /absolute/terminal-bench/tasks \
  --template /private/tb21/control/deepseek-v4-attempt-1.json \
  --output-root /private/tb21/control \
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
layers separately. `score` accepts only audited exact-model attempts and emits
`score_percent` only when all 89 tasks have the requested exact attempt count.
Pass `--output /private/results/score.json` to retain that same ledger
atomically as a mode-0600 artifact in addition to stdout. `analyze` accepts the
same `--output` contract for its per-trial root-cause ledger.
Use `--expected-attempts 1` only for a fast smoke ledger. The Terminal-Bench 2.1
submission contract requires at least five trials per task, so published model
scores such as DeepSeek-V4-Flash-0731's 82.7 must be compared only with a
five-attempt (445-trial) ledger; a stochastic one-attempt score is not a
leaderboard reproduction. Pass `--tasks-root` when scoring to validate
all task identities against the pinned checkout; duplicate job directories,
missing attempts, and surplus valid attempts fail closed. It still reports the raw observed percentage and a
provisional valid-only percentage, but never presents incomplete coverage as a
final Terminal-Bench score. Retry infrastructure-invalid trials in a separate
job and pass both job directories to the analyzer/scorer. Harbor's bounded
retry loop applies only when a trial returns an exception; numeric verifier
failures, agent/verifier timeouts, missing rewards, safety refusals, and
authentication/model errors are not automatically retried. Consequently a
verifier-confirmed stochastic model failure is never selectively hidden.
An agent timeout is scoreable only when the subsequent verifier returns a
numeric reward: reward 1 remains a pass and numeric non-pass remains a model
timeout. If that verifier also ends without a reward, the final workspace is
unscored and the trial is infrastructure-invalid rather than an assumed zero.

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
  /private/tb21/jobs/deepseek-v4-attempt-1 \
  --output-root /private/tb21/trajectory-bundle \
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
effective bounded timeout used for that individual command. Because the public leaderboard does not permit
modified timeouts or resources, a TCG-calibrated result is a reproducible local
score, not an official leaderboard submission.

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
calibrated retry budgets is counted as a model timeout, ensuring the five-shot
run can terminate without selectively forgiving hard tasks forever.

After any interrupted or infrastructure-invalid batch, `plan-retries` compares
the audited results with the pinned task checkout and groups tasks by the exact
number of replacement attempts still required. This avoids rerunning valid
model failures, minimizes compute, and makes the final five-shot selection
independent of job-directory ordering. It also emits bounded retry profiles:
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

Two unrelated Terminal-Bench 2.1 tasks were exercised offline end to end with Harbor,
Tofu's internal corp-gateway provider, and `deepseek-v4-flash`:

- `regex-log`, image registry digest
  `sha256:90101b2e815323a8da20528a1439bebc407eb9761c9c68a3d557730856c878e9`;
- `polyglot-c-py`, image registry digest
  `sha256:0f1c3b7816d70cf5551573fd6aeef76893f2ae3000be2419997b6871b5d987ed`.

Both Oracle plumbing checks and both final DeepSeek trials received
`reward = 1.0` from the official task assertions. `polyglot-c-py` is a useful
second contract because it requires one generated file to execute correctly
under both Python and GCC rather than merely matching text.

Those upstream tasks normally download `curl`, `uv`, Python, and pytest during
verification. The no-network validation used each task's unchanged upstream
`test_outputs.py`, an offline shell entry point that directly invokes
the test module, and a pre-fetched `python:3.13-slim` image pinned to registry
digest
`sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a`.
This distinction matters: the tasks' assertions were unchanged, while their
network-dependent package bootstrap was replaced.

Measured on the same host using QEMU 11.1.0 pure TCG with libseccomp 2.6.0:

| Path | Environment | Agent | Verifier | Result |
|---|---:|---:|---:|---:|
| Original uncached Docker + DeepSeek | 86.48 s | 202.77 s | 4.25 s | 1.0 |
| Cached runc + DeepSeek, full history | 27.46 s | 276.53 s | 2.56 s | 1.0 |
| Cached runc + compact audited history | 27.02 s | 125.85 s | 2.37 s | 1.0 |
| Final adaptive-first-turn loop | 27.00 s | 206.12 s | 2.35 s | 1.0 |
| Cached runc + Oracle plumbing check | 26.76 s | 1.18 s | 3.68 s | 1.0 |

`polyglot-c-py` cross-task results on the same cache/runtime:

| Path | Environment | Agent | Verifier | Result |
|---|---:|---:|---:|---:|
| Cached runc + Oracle plumbing check | 28.46 s | 1.10 s | 6.15 s | 1.0 |
| Cached runc + DeepSeek, recovery loop | 39.29 s | 377.49 s | 3.48 s | 1.0 |
| 4K-output A/B experiment | 28.08 s | 410.41 s | 3.50 s | 1.0 |

The public implementation was additionally tested against the unmodified
upstream `regex-log` task, including its original `apt-get update`, `curl`
installation, uv 0.9.5 download, pytest 8.4.1 download, and official assertion:

| Public path | Environment | Agent | Verifier | Result |
|---|---:|---:|---:|---:|
| Cached runc + Oracle | 27.03 s | 1.09 s | 197.75 s | 1.0 |
| Cached runc + DeepSeek | 26.72 s | 263.45 s | 192.12 s | 1.0 |
| Host-confined runc + Oracle | 26.46 s | 1.30 s | 192.56 s | 1.0 |
| Host-confined runc + DeepSeek | 26.26 s | 165.52 s | 187.95 s | 1.0 |

The final host-confined model trial used the project's internal corp-gateway provider
and `deepseek-v4-flash`: 6 rounds, 22,326 input tokens, 11,454 output
tokens, no recovery reset, and 159.98 seconds of provider latency. The key
remained host-only. The entire run used local QEMU TCG; the only remote
services were the explicitly allowed model API and public package endpoints,
not a cloud sandbox.

The compact agent writes a reasoning-redacted, credential-free provider audit
record while retaining the working reasoning only in the host process for the
next model turn. Repeated working context can be reset after diagnosed
no-progress loops without losing the disposable VM workspace.

The agent also detects two consecutive output-only truncations or three
identical terminal results. It then starts a fresh reasoning context while
preserving the VM workspace and a bounded tail of terminal evidence. A
pre-fix `polyglot-c-py` run reached the 24-round limit with reward 0.0; the
subsequent 8K recovery-capable run submitted in 11 rounds with reward 1.0
without needing a reset, while the 4K A/B run exercised one reset and also
received reward 1.0. Empty assistant responses receive a placeholder before
the retry prompt, avoiding same-role message repair in the Friday client.

The 4K experiment is intentionally reported: it was slower and used more
rounds. Full evaluation configs now select their output budget explicitly;
truncated output-only turns receive a bounded retry and can trigger a fresh
context recovery. A live first-turn probe produced the correct terminal tool
call in 1.39 seconds. On the successful polyglot run, 365.24 of 377.49 agent
seconds were provider latency; local QEMU/cache optimization cannot remove
that remote variance.

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

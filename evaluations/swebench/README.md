# 无 root、无 Docker、无云沙箱的本地 Agent 评测

这套入口统一编排 SWE-bench Verified 和 Terminal-Bench 2.1。当前本地默认后端是
`rootless-qemu`，不是旧的 udocker/PRoot，也不是 Singularity：每个 trial 都在新的
QEMU TCG 虚拟机 overlay 中运行，结束后删除写层；宿主项目、`$HOME`、Docker socket、设备和
凭据都不挂载进 guest。

安全边界和结果口径是两件事：这个后端提供强于 host/PRoot 的本地隔离，但纯 TCG 的时钟和性能
不等同于官方 Docker/KVM/Daytona 环境。因此本地结果适合可复现回归和内部比较，不能冒充官方
leaderboard 提交。

## 固定输入

- Harbor 固定到 commit `ea2fee78517f2e591bad69fcf1e6731f9c23ec99`。
- SWE-bench Verified 固定到 Harbor registry digest
  `sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341`，恰好 500 题。
- Terminal-Bench 2.1 固定到 digest
  `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`，恰好 89 题。
- TB 2.1 正式本地分数要求每题恰好 5 个有效 trial，即 445 个；覆盖不完整时 scorer 不输出
  final percentage。
- QEMU、libseccomp、libslirp、`crane`、SWE-bench、`pycdlib` 和 Harbor 都固定版本或 digest。

## 安装专用评测环境

不要把评测依赖装进 Tofu 服务环境。以下路径必须位于仓库外的私有本地盘：

```bash
export TOFU_EVAL_VENV=/absolute/private/tofu-eval-venv
export TOFU_EVAL_ROOT=/absolute/private/tofu-evals
export TOFU_QEMU_ROOT=/absolute/private/rootless-qemu

uv venv "$TOFU_EVAL_VENV"
uv pip install --python "$TOFU_EVAL_VENV/bin/python" \
  -r evaluations/swebench/requirements.txt

scripts/bootstrap_rootless_qemu.sh \
  --prefix "$TOFU_QEMU_ROOT" --jobs 8

export ROOTLESS_VM_QEMU="$TOFU_QEMU_ROOT/runtime/bin/qemu-system-x86_64"
export ROOTLESS_VM_QEMU_IMG="$TOFU_QEMU_ROOT/runtime/bin/qemu-img"
export ROOTLESS_VM_EGRESS_BRIDGE="$TOFU_QEMU_ROOT/runtime/bin/rootless-egress-bridge"
```

bootstrap 全程不使用 `sudo`、系统包管理器、setuid helper、FUSE 或 Docker daemon，所有下载、
编译缓存和二进制都留在 mode-0700 的显式 prefix 中。完成前会做真实 QMP、seccomp、user
namespace 和 `no_new_privs` 探测。

评测还需要一个受信任的 Linux guest qcow2 基础盘，里面只包含 QEMU guest agent、Docker
导入/构建工具和 runc。仓库已经固定 Alpine 3.24.1 云镜像以及所有 APK 的官方 URL、大小和
哈希，可由普通用户完整重建；不要使用包含个人账号、SSH key 或云凭据的日常 VM 镜像：

```bash
mkdir -m 700 "$TOFU_EVAL_ROOT/.base-disk"

"$TOFU_EVAL_VENV/bin/python" -m rootless_vm build-base \
  --lock rootless_vm/alpine_3_24_x86_64.lock.json \
  --output "$TOFU_EVAL_ROOT/.base-disk/alpine-docker.qcow2" \
  --cache-root "$TOFU_EVAL_ROOT/.base-disk/downloads" \
  --state-root "$TOFU_EVAL_ROOT/.base-disk/state" \
  --qemu "$ROOTLESS_VM_QEMU" \
  --qemu-img "$ROOTLESS_VM_QEMU_IMG" \
  --json

export ROOTLESS_VM_BASE_DISK="$TOFU_EVAL_ROOT/.base-disk/alpine-docker.qcow2"
```

APK 安装发生在断网、一次性的隔离 VM 内；输出发布前还会在第二个 VM 内实测 QGA、Docker、
runc 和 daemon。相邻 JSON 保存输出 SHA-256、完整 lock SHA-256 与 QEMU 版本。

## 准备 SWE-bench Verified

先下载并校验 500 份轻量任务定义。下载有固定 digest、并发上限、重试和原子 ledger，重复执行
会直接命中本地缓存：

```bash
export TOFU_SWEBENCH_DEFS="$TOFU_EVAL_ROOT/.definitions/swebench-verified"
export TOFU_SWEBENCH_STORE="$TOFU_EVAL_ROOT/.image-store/swebench-verified"
export TOFU_SWEBENCH_CACHE="$TOFU_EVAL_ROOT/.image-cache/rootless-qemu"
export ROOTLESS_VM_BASE_DISK=/absolute/private/trusted-rootless-base.qcow2

"$TOFU_EVAL_VENV/bin/python" -m evaluations.swebench prepare-rootless \
  --benchmark swebench-verified \
  --phase definitions \
  --definitions-root "$TOFU_SWEBENCH_DEFS" \
  --output-root "$TOFU_EVAL_ROOT"
```

建议先做单题。`crane=auto` 会在私有工具缓存下载固定的 v0.21.9 Linux x86-64 release，并在
解包前校验 SHA-256；ISO 默认由 pinned `pycdlib` 生成，不依赖 `genisoimage`：

```bash
"$TOFU_EVAL_VENV/bin/python" -m evaluations.swebench prepare-rootless \
  --benchmark swebench-verified \
  --phase assets \
  --task swe-bench/psf__requests-1142 \
  --definitions-root "$TOFU_SWEBENCH_DEFS" \
  --rootless-image-store "$TOFU_SWEBENCH_STORE" \
  --output-root "$TOFU_EVAL_ROOT"

"$TOFU_EVAL_VENV/bin/python" -m evaluations.swebench prepare-rootless \
  --benchmark swebench-verified \
  --phase cache \
  --task swe-bench/psf__requests-1142 \
  --definitions-root "$TOFU_SWEBENCH_DEFS" \
  --rootless-image-store "$TOFU_SWEBENCH_STORE" \
  --rootless-base-disk "$ROOTLESS_VM_BASE_DISK" \
  --rootless-qemu "$ROOTLESS_VM_QEMU" \
  --rootless-qemu-img "$ROOTLESS_VM_QEMU_IMG" \
  --rootless-cache-root "$TOFU_SWEBENCH_CACHE" \
  --cache-workers 1 \
  --output-root "$TOFU_EVAL_ROOT"
```

SWE-bench 的 Harbor 定义使用 Dockerfile。框架只接受单一、字面量 `FROM` 的单阶段文件；拒绝
变量、多阶段构建、symlink 和超过 1 GiB 的 context。构建在受限 public QEMU guest 中完成，
网络只有认证、限流、限字节的 HTTP(S) proxy；发布缓存前会清除 Docker/containerd graph、构建
context 和可能含短期 proxy 凭据的元数据。正式 trial 只从不可变 qcow2 backing 启动新的 overlay。

去掉 `--task` 即准备全量 500 题。当前缓存格式按任务保存完整导出 rootfs，优先保证隔离和恢复，
但不具备 Docker content store 的跨镜像 layer 去重；全量前必须按实际样本估算数百 GiB 空间。
这是下一阶段的主要性能/容量优化点，不应拿官方 Docker 的 120 GiB 建议直接套用。

## 用项目 Meituan/Friday provider 跑单题

`TofuHostAgent` 在宿主进程读取项目已经配置的 provider/key，guest 只收到终端命令和输出，拿不到
key。若 key 只通过 `LLM_API_KEYS` 环境变量提供，可以额外传
`--secret-env LLM_API_KEYS`；job config 只持久化 `${LLM_API_KEYS}` 占位符。

```bash
"$TOFU_EVAL_VENV/bin/python" -m evaluations.swebench doctor \
  --benchmark swebench-verified \
  --task swe-bench/psf__requests-1142 \
  --rootless-base-disk "$ROOTLESS_VM_BASE_DISK" \
  --rootless-image-store "$TOFU_SWEBENCH_STORE" \
  --rootless-qemu "$ROOTLESS_VM_QEMU" \
  --rootless-qemu-img "$ROOTLESS_VM_QEMU_IMG" \
  --output-root "$TOFU_EVAL_ROOT"

"$TOFU_EVAL_VENV/bin/python" -m evaluations.swebench run \
  --benchmark swebench-verified \
  --agent rootless_vm.harbor_tofu_agent:TofuHostAgent \
  --agent-version 0.8.4 \
  --reasoning-effort max \
  --model deepseek-v4-flash-meituan \
  --task swe-bench/psf__requests-1142 \
  --backend rootless-qemu \
  --rootless-base-disk "$ROOTLESS_VM_BASE_DISK" \
  --rootless-image-store "$TOFU_SWEBENCH_STORE" \
  --rootless-qemu "$ROOTLESS_VM_QEMU" \
  --rootless-qemu-img "$ROOTLESS_VM_QEMU_IMG" \
  --rootless-cache-root "$TOFU_SWEBENCH_CACHE" \
  --run-id swebench-requests-smoke \
  --output-root "$TOFU_EVAL_ROOT"
```

同一 task 的不同 model 会得到不同 trial 和新的 VM overlay；共享的只有内容寻址的只读镜像与
prepared cache。中断后使用 `resume RUN_DIR`，完成后使用 `audit RUN_DIR`。预检或审计失败时不
发布分数。

## 用同一个 Kimi 跑固定版 Codex 基线

正式基线使用 `codex-kimi` profile，而不是 Harbor 自带的 `codex` agent。后者必须把 provider
credential 放进 guest，不能满足配对实验的凭据边界。`codex-kimi` 固定 Codex CLI 0.149.1 的
二进制和 SHA-256；launcher 在宿主回环启动独立 Responses→Kimi Chat 代理，再通过 QEMU 的单一
受限控制端点转发。Kimi URL/key 的值不会进入 Harbor 子进程、job config、命令或 guest。

```bash
export TOFU_CODEX_01491_BINARY=/absolute/pinned/codex
export TOFU_CODEX_01491_SHA256=<64-hex-sha256>
export TOFU_KIMI_PROVIDER_FACE=meituan-chat
export TOFU_KIMI_SLOT_ID=<non-secret-shared-slot-id>
export KIMI_CHAT_BASE_URL=<meituan-kimi-chat-base-url>
export KIMI_API_KEY=<host-only-key>

"$TOFU_EVAL_VENV/bin/python" -m evaluations.swebench run \
  --benchmark swebench-verified \
  --agent codex-kimi \
  --model kimi-k3 \
  --reasoning-effort high \
  --max-retries 0 \
  --task swe-bench/psf__requests-1142 \
  --backend rootless-qemu \
  --rootless-base-disk "$ROOTLESS_VM_BASE_DISK" \
  --rootless-image-store "$TOFU_SWEBENCH_STORE" \
  --rootless-qemu "$ROOTLESS_VM_QEMU" \
  --rootless-qemu-img "$ROOTLESS_VM_QEMU_IMG" \
  --rootless-cache-root "$TOFU_SWEBENCH_CACHE" \
  --release-run-root /absolute/release-baseline-run \
  --run-id codex-kimi-requests-smoke \
  --output-root "$TOFU_EVAL_ROOT"
```

Codex 强制 `--ignore-user-config --ephemeral --json`、Responses wire API、本地压缩及静态 trial
header；这些能力与参数见 OpenAI 官方的
[configuration reference](https://developers.openai.com/codex/config-reference) 和
[CLI reference](https://developers.openai.com/codex/cli/reference)。每个 trial 保留原始 Codex
JSONL、带 provider usage 的代理 shard 和 ATIF。`audit` 会重新调和三者，检查唯一 trial token、
固定二进制哈希、零远程 compact、唯一 guest 控制路由和凭据不落盘。恢复运行时必须重新提供同名
宿主环境变量；launcher 会在 manifest 固定的宿主端口重启代理，仍不会把值传给 Harbor。
provider face、非密钥 slot ID、Harbor 可执行文件 SHA-256、固定源码 commit 和 runner revision
也会进入不可变身份记录；配对 release manifest 必须逐项相同。非 dry-run 在产生任何付费 trial 前
要求 runner worktree 干净且 revision 可解析，resume 也必须仍是同一干净 revision。可发布运行还
必须传入已初始化的 `--release-run-root`；launcher 在 Harbor dispatch 前认领精确 task attempts，
resume 必须再次提供同一路径。未认领的付费 smoke 只能诊断，导出器拒绝接纳。

Harbor 0.21 的内部 retry 会删除失败 attempt 目录，无法证明失败调用成本完整。因此正式
`codex-kimi` 的 Harbor `--max-retries` 固定为 0，任何内部 retry 都使导出失败。release manifest
仍可预注册外层基础设施重试；每次外层运行必须先追加不可变 claim，失败 usage、付费工具与
raw/proxy 证据会进入最终 task 成本和 attempt 账本，因而不能丢弃失败 run 后挑成功样本。审计
通过后，将某个 SWE/TB slice 写入同一 baseline run store：

```bash
"$TOFU_EVAL_VENV/bin/python" -m evaluations.long_agent_release \
  export-codex-harbor \
  --harbor-run-dir /absolute/harbor-run \
  --run-root /absolute/release-baseline-run
```

导出器按 source task 内 Harbor trial name 的字典序确定 `trialIndex`（与 reward/耗时无关）。无
外层重试时 oracle-ready 墙钟为 `TrialResult.started_at → verifier.finished_at`；有重试时从最早保留
attempt 的 task start 计时，包含失败与重试间隔。它保存 raw JSONL、proxy metrics 和 ATIF，并
拒绝 provider/slot/harness/task digest、numeric reward、预认领顺序或生命周期证据漂移。

## 用同一个 Kimi 跑生产 Tofu 候选

正式候选使用 `tofu-kimi`，它调用生产公开 `AgentRuntime`，不是旧 `tofu` 私有 benchmark loop。
模型和 credential 留在 Harbor 宿主 agent；guest 只暴露独占的 `custom__run_command` 与
`custom__submit_result`。运行配置必须是非密钥、内容冻结的 JSON，并与候选 release manifest 的
arm、prompt contract、tool schema、provider/slot、thinking 和 agent revision 完全一致。

```bash
export TOFU_KIMI_PROVIDER_FACE=meituan-chat
export TOFU_KIMI_SLOT_ID=<non-secret-shared-slot-id>
export KIMI_CHAT_BASE_URL=<meituan-kimi-chat-base-url>
export KIMI_API_KEY=<host-only-key>

"$TOFU_EVAL_VENV/bin/python" -m evaluations.swebench run \
  --benchmark swebench-verified \
  --agent tofu-kimi \
  --model kimi-k3 \
  --reasoning-effort high \
  --max-retries 0 \
  --tofu-runtime-config /absolute/combined-v2-runtime.json \
  --tofu-experiment-arm combined_v2 \
  --task swe-bench/psf__requests-1142 \
  --backend rootless-qemu \
  --rootless-base-disk "$ROOTLESS_VM_BASE_DISK" \
  --rootless-image-store "$TOFU_SWEBENCH_STORE" \
  --rootless-qemu "$ROOTLESS_VM_QEMU" \
  --rootless-qemu-img "$ROOTLESS_VM_QEMU_IMG" \
  --rootless-cache-root "$TOFU_SWEBENCH_CACHE" \
  --release-run-root /absolute/release-candidate-run \
  --run-id tofu-kimi-requests-smoke \
  --output-root "$TOFU_EVAL_ROOT"
```

每题保留 `events.jsonl`、`runtime-evidence.json`、`tool-audit.json` 和 ATIF。`audit` 会逐轮核对
请求/上下文、模型与压缩 usage、cache、TTFT、prompt adoption、tool schema/call/result、终态与
输出；任何 fallback、证据错位、密钥落盘或非 verifier 完成都会拒绝。候选的校正墙钟等于原始
task-start→oracle-ready 墙钟，不扣除 Codex 代理 CPU。审计通过后幂等导入候选 run store：

```bash
"$TOFU_EVAL_VENV/bin/python" -m evaluations.long_agent_release \
  export-tofu-harbor \
  --harbor-run-dir /absolute/harbor-run \
  --run-root /absolute/release-candidate-run
```

与基线相同，Harbor 内部 retry 固定为 0；预注册的外层失败 attempt、模型/压缩调用、付费工具和
等待时间必须留在不可变账本，不能通过丢弃失败目录来挑选成功样本。候选 timeout/cancel 会在
继续抛错前保存脱敏的 partial `runtime-evidence.json`；关闭 post-dispatch attempt 时必须将它作为
`failed_attempt_runtime_evidence` artifact 保存，账本会逐项核对主调用及压缩 `modelUsages`。

## Terminal-Bench 2.1

TB 的完整本地流程和失败分类见 [Rootless VM sandbox](../../docs/ROOTLESS_VM_SANDBOX.md)。正式
协议必须是 89 × 5；`scripts/rootless_terminal_bench_21.py score` 只有在以下条件全部成立时才
输出百分制最终分数：

- 任务集合和固定 checkout 完全一致；
- 每题恰好 5 个有效 trial，没有 surplus；
- 每个有效 trial 都有 numeric verifier reward；
- dispatch audit 证明请求只由期望模型服务；
- 没有 reasoning/key/proxy credential 落盘。

SIGTERM/手动停止得到的 `CancelledError` 归类为 `infrastructure_cancelled`，不计 0 分，也不伪装
成网络失败。Agent timeout 只有在后续 verifier 给出 numeric reward 时才是可计分模型结果；
verifier 也无结果时必须重跑。

DeepSeek 公布的 V4 Flash TB 2.1 分数使用 DeepSeek Harness Minimal preset，不是 Tofu harness。
当前 Tofu 有不同的工具 schema、提交验证、上下文恢复与超时校准；因此本地 Tofu 分数必须明确
标为 Tofu-harness 结果，不能把两者当作同一实验。模型温度、`top_p` 和 reasoning effort 已按
官方公开配置对齐，并不消除 harness 差异。

## 产物和搜索隔离

默认产物位于仓库外。每个 root/run 目录都是 mode 0700，并自带拒绝递归的 `.gitignore` 和
`.ignore`。若显式放进仓库，只允许 `.eval-runs/` 或 `eval-runs/`；普通源码目录会 fail closed。
仓库自身还在 Git、项目扫描器、VS Code watcher/search、Pylance 和导出器中独立排除了
`*_workdir`、`evaluation_results`、`sb-cli-reports`、`jobs` 和 `trials`。

不要把这些防线理解成“日志可以含 key”。运行后仍应扫描 command、manifest、transcript、QEMU
参数、proxy 日志和 guest 持久盘；任何 credential 命中都使该批结果无效。

## 官方 patch 复核

已有 patch 的最终官方复核仍交给固定版本的 SWE-bench harness。它目前只支持 Docker 或 Modal；
这条限制与上面的本地 agent rollout 无关。不同 `model_name_or_path` 会拆成独立 run id，避免
官方 harness 以 `instance_id` 为键时覆盖另一模型的结果。

# 通用 Agent 评测：Harbor 执行适配器

这套入口统一编排 SWE-bench Verified、Terminal-Bench 2.1 / 3.0 / 4.0 和
DeepSWE v1.1。DeepSWE 和 TB 3.0 / 4.0 必须使用保留独立 verifier 的 Docker/cloud 后端。
当前本地默认后端是
`rootless-qemu`，不是旧的 udocker/PRoot，也不是 Singularity：每个 trial 都在新的
QEMU TCG 虚拟机 overlay 中运行，结束后删除写层；宿主项目、`$HOME`、Docker socket、设备和
凭据都不挂载进 guest。

安全边界和结果口径是两件事：这个后端提供强于 host/PRoot 的本地隔离，但纯 TCG 的时钟和性能
不等同于官方 Docker/KVM/Daytona 环境。因此本地结果适合可复现回归和内部比较，不能冒充官方
leaderboard 提交。

## 权威和扩展边界

通用入口是 `python -m evaluations`，不是一个新的执行循环：

```bash
python -m evaluations catalog
python -m evaluations harbor doctor --help
python -m evaluations harbor run --help
python -m evaluations platform prepare --help
```

`evaluations/benchmark_catalog.json` 是 benchmark 身份权威；`evaluations/catalog.py`
仅加载有界本地 JSON，验证 dataset digest、源码 commit、精确题集 lock 和重复次数。
Harbor 适配器消费该定义，不再把所有非 SWE-bench 的新题集当成 TB 2.1。
`rootless_vm/harness_profiles.py` 独立声明 agent harness、凭据边界和轨迹格式；
Tofu production、Codex proxy、OpenCode proxy 必须经过有生命周期和审计的正式 launcher，
不能通过通用 profile 绕过代理/凭据检查。旧 `python -m evaluations.swebench` 入口仍可用。

内部平台 batch 入口见 [platform_jobs/README.md](../platform_jobs/README.md)：打包已提交
runner 和 Git 对象证明，复用 SDK 的审批、提交与回收。平台成功单独记录，不转换为得分。

边界为 **benchmark catalog → execution adapter → agent harness / sandbox → evidence / scoring**。
目前 execution adapter 只有 Harbor，catalog 包含五个固定题集；这不是已经完成五个题集
实测或实现 SWE-bench Pro / Pier 的声明。新增 Harbor 格式题集需要审核 catalog、任务 lock、资源/网络需求、
verifier 语义和契约测试；非 Harbor harness 应单独实现执行适配器，复用身份/路径契约，
不扩充 TB 专用条件分支。需要 GPU 或不同评分规则的题集不得被静默降为 CPU/二元任务。
现有 `compare` 是 TB 2.1 的专用配对统计，不冒充通用评分器。

### DeepSWE 与 Frontier-Bench 后续版本

问题：把新任务交给旧 parser 或单环境 image store，可能丢弃独立 verifier、补丁收集
和结构化 reward，产生错误成功率。决定：题集声明 `required_capabilities`；doctor 和
实际 dispatch 都验证 Harbor parser 保留相应字段；当前 QEMU/Singularity 后端在创建
run 或准备镜像前拒绝这些题集。替代方案是把 verifier 合并进 agent 环境、只保留二元
reward 或放宽旧后端；这些方案改变任务语义，因此拒绝。parser 检查不是 sandbox
执行证明，仍须在目标 worker 上完成 oracle smoke 后才跑模型。

输入来自官方仓库和 Harbor Hub 公共 registry，2026-10-10 冻结；任务 lock 包含全部
任务身份而非挑选容易题：

| catalog key | 题数 | registry digest | 来源 revision |
|---|---:|---|---|
| `deepswe-v1.1` | 113 | `sha256:aaa82ceb8404dccc17689c9383f93dbcbc8f029a7601d2e3856a416f2cb89269` | `0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea` |
| `terminal-bench-3.0` | 74 | `sha256:a32a61879ea94eb9dc16fa1fbeb398759f0c07ca633d9d1f6aec760207036da3` | `2b0442c3c583b710ca8da14c8e601b99f2f1f244` |
| `terminal-bench-4.0` | 66 | `sha256:39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732` | `452bf305c6daa62fc59061d22133a7cbc7c1572e` |

[Frontier-Bench 仓库](https://github.com/harbor-framework/frontier-bench) 已重定向到
[新版 Terminal-Bench](https://github.com/harbor-framework/terminal-bench)。这里按实际
发布的 3.0 / 4.0 digest 注册，不把不明确的 Frontier 名称映射成 TB 2.1。
[DeepSWE v1.1](https://github.com/datacurve-ai/deep-swe) 使用独立 verifier，收集 agent
提交的 patch，在干净环境验证；官方 leaderboard 使用 Pier + mini-SWE-agent + Modal。
这里接入的是 Harbor 任务与原始结构化 rewards，尚未实现 Pier 的 namespace/网络契约适配，
不能宣称与其 leaderboard harness 等价。新题集默认五次是本地协议；尚未核实官方
最低重复数，`official_min_attempts=null`，不能仅凭次数生成 publication-ready 标志。

```bash
python -m evaluations catalog
python -m evaluations harbor doctor --benchmark deepswe-v1.1 --backend modal
python -m evaluations harbor run --benchmark deepswe-v1.1 --backend modal \
  --agent oracle --model oracle --task datacurve/abs-module-cache-flags --attempts 1 --concurrency 1
```

`mini-swe-agent` profile 明确为 guest-explicit；运行时需固定其版本和显式短期凭据。
Tofu production 文件工具目前依赖 rootless guest rootfs，仍不支持这些新题集的
Docker/cloud 执行；不能删除 backend guard 来假装接通。

### GLM-5.3 的配置差距与终态失败

只读诊断可直接读取 native wire receipts，并区分压缩预算耗尽、上下文超限和基础设施
故障；大 transcript 只读取末尾 64 KiB，报告保存字节范围和该片段 checksum，不声称
这是完整文件的 hash。主请求参数与 compaction 请求分开，缺省值保持 unknown。

```bash
python -m evaluations harbor diagnose /absolute/run/root --reference-profile glm-5.3-tb21
```

[GLM-5.3 官方说明](https://huggingface.co/zai-org/GLM-5.3#footnotes) 的 TB 2.1
配置为 Claude Code 2.1.207、6 小时、输出 65536、temperature=1、top_p=1。
`evaluations/glm53_tb21_runtime.json` 固定 Tofu 候选的输出预算为 65536；它只对齐此项，
不是官方 harness 的复刻。新候选需显式选择 `--tofu-tool-surface production`、
`--tofu-wire-proxy`，将配置文件、agent deadline、provider slot、软件 revision 和
完整题集/五次试验一起登记；不要改写旧 run 配置或继续混合运行。

问题：旧 retry classifier 把所有「did not reach a successful terminal state」归为
网络故障，包括本地压缩失败和上下文超限。决定：Tofu adapter 在异常 metadata 中保留
脱敏的 native failure receipt；只有明确 `retryable=true` 的 network/rate_limit/server_error
允许基础设施重跑。替代方案是按泛化 RuntimeError 文案重跑，会选择性替换 agent
失败并浪费预算，因此拒绝。旧试次没有 receipt 时需人工诊断，不能推断为瞬时错误。
任何已有结构化 reward 的试次都不会自动替换；缺失、无效和未评分仍保持独立。

## DolphinFS-only 宿主存储
## 宿主存储

默认根为 **checkout `.eval-runs/benchmarks`**，不再依赖宿主 HOME 或 XDG 默认值。
该 checkout 位于 `/mnt/dolphinfs`，写入型评测路径必须留在该卷；可显式设置同卷的
`TOFU_EVAL_ROOT`。宿主 `/tmp`、`/var/tmp`、`/dev/shm` 及符号链接路径均拒绝。
每个 run 的 `.runtime/` 保存专属 tmp、Python/uv/pip/HF/npm 缓存及 XDG 数据；
只传入子进程，不修改 controller 的全局 `tempfile.tempdir` 或 HOME；Harbor 子进程使用
私有 HOME（固定版 Harbor 的 cache 忽略 XDG，直接使用 `~/.cache/harbor`），不读取个人配置。
provider 通过已审核的宿主适配器和显式环境输入配置，不复制个人 HOME/凭据。
任务定义下载也在专用 helper 子进程中执行：Harbor 即使接收 `output_dir` 仍分配默认
TemporaryDirectory，因此 HOME/tmp 必须在 import 前隔离；缓存命中不再启动下载进程。
HF Hub、Apptainer/Singularity 的专属 cache/tmp 变量同样覆盖；预检工具也使用私有子进程环境。
manifest 的 `runtime_storage` 在 dispatch 前固定，resume 拒绝缺失/漂移的记录；
旧结果可继续只读分析，但需要新 run 才能采用新的存储契约。缓存随 run 显式清理，
不自动删除失败成本和轨迹；共享的可执行任务镜像仍必须是 digest-pinned 只读输入。

**DolphinFS 可写不等于支持本地 IPC。** 受影响部署的 pathname Unix socket `bind`
返回 `ENOSYS`，unlink-while-open 匿名临时文件的读回返回 `ENOENT`，具名文件正常。
`rootless_vm.storage_probe` 在模型 dispatch/QEMU 启动前验证具名写入、fsync/rename、
flock 及 Unix socket round trip；不支持就明确停止，不回退宿主 `/tmp`。
默认 pathname-IPC backend 在这种卷上仍不可运行。显式设置
`ROOTLESS_VM_ALLOW_PRIVATE_LOCAL_ROOTS=1` 可使用当前用户拥有的 0700 HOME 私有目录
承载 state、prepared cache 和 scratch；路径和存储能力仍必须通过预检，宿主共享临时
目录及 world-writable 祖先仍被拒绝。此选择写入启动配置，不能在 resume 时静默切换。
后续可审核继承 fd/socketpair IPC；不能使用裸宿主执行、关闭
namespace/seccomp、把 QGA 暴露为无认证 TCP/公共 abstract socket。
Guest/jail 内的 `/tmp` 是私有 VM/沙箱路径，不是宿主 `/tmp`，保留官方任务语义。

离线存储/目录测试无需应用存储装配。在此卷上使用以下专用测试通道，避免 pytest
fd capture 的匿名临时文件以及应用 conftest 禁止 checkout 内数据库的独立保护：

```bash
mkdir -p .eval-runs/framework-audit/tmp
export TMPDIR="$PWD/.eval-runs/framework-audit/tmp" TMP="$PWD/.eval-runs/framework-audit/tmp" TEMP="$PWD/.eval-runs/framework-audit/tmp"
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest --noconftest --capture=sys -o addopts= \
  --basetemp="$PWD/.eval-runs/framework-audit/pytest" \
  tests/evaluations/test_evaluation_catalog_paths.py tests/rootless_vm/test_rootless_host_paths.py \
  tests/project_mod/test_swebench_evaluation_framework.py tests/evaluations/test_tb21_comparison.py
```

此通道只适用于这些自包含测试，不替代生产应用的存储/安全门禁。

## 评分有效性与执行架构

旧 `scripts/rootless_terminal_bench_21.py score` 仍提供诊断投影，但不能绕过正式入口：
只接受精确二元 numeric reward（不接受 bool、NaN、Infinity、字符串或越界数）；必须有完整、
有序的 verifier 时间，且不能带未解决异常。`AgentTimeoutError` 后若 verifier 完成，则保留
其真实 0/1，不把超时一律计零。运行指纹包括 agent/environment/verifier 配置、超时倍率、
命令期限和虚拟时钟；混合指纹不给 `score_percent`。任何无效试次被排除、或失败驱动补跑，
只能是 diagnostic，不可保留旧条件成功并补新条件成功。旧入口的 `formal_score_percent`
始终为 null：即使规范题集 lock、89×5 和配置一致，仅最终结果仍不能证明 Harbor 内部没有
替换失败。不能以目录名、配置摘要替代运行前登记与完整尝试账本。
保留原始/暂定统计和诊断原因（`legacy_attempt_history_not_attested`），缺失与 infra error
不标为模型错误。公开比较仍以 `compare` 的固定全域账本和冻结配对注册为权威；
旧 config 指纹不是运行前签名注册证明。

本地执行的优先顺序取决于可验证的宿主能力，不做自动降级：

1. **有 `/dev/kvm` 权限的非 root VM worker**：更适合重计算、嵌套仿真和公平时间预算；
   仍需现有外层隔离，当前 TCG adapter 没有实现 KVM 模式。
2. **rootless OCI + gVisor systrap**：无需 KVM，但仍需 namespace/UID 映射、资源与网络
   限制；DolphinFS 上的 OCI graphroot 必须单独验证，不能默认受支持。
3. **当前 hardened TCG**：不要求 KVM，但所有外层隔离必须通过；TCG 本身不是 QEMU
   官方支持的安全边界，性能也不等价于硬件 VM。

本机未发现 `/dev/kvm`、`newuidmap/newgidmap`、runsc、Podman 或 Docker；不申请 root 安装，
不以 PRoot/裸宿主执行补位。保持当前挂载时，候选 transport 为 QGA/QMP 预连接 socketpair，
及 VM 私有 netns 内创建后通过 SCM_RIGHTS 交给宿主代理的多连接 listener。它尚未接入生产；
已用安装中的 QEMU 11.1.0 完成无磁盘 QMP socketpair 往返、零 capabilities、seccomp/NNP
能力探针，但这不是完整 VM/QGA/代理集成证明。必须验证 FD 白名单、持久连接恢复、
跨 trial/同 UID 访问隔离、代理背压/限额、取消及 orphan
清理，不能通过开放宿主全局 abstract socket 解决路径问题。详见
[QEMU security](https://www.qemu.org/docs/master/system/security.html)、
[gVisor platforms](https://gvisor.dev/docs/architecture_guide/platforms/)、
[Docker rootless prerequisites](https://docs.docker.com/engine/security/rootless/)。

## 固定输入

高吞吐运行先用 `capacity.plan_rootless_pair` 根据统一硬件探针、全部任务的 CPU/内存
请求和已有 VM 占用，计算两方相同的最大并发。它按最重活动题组校验预算，不减少题集，
不改变题内资源。`TOFU_RESOURCE_PROFILE=throughput` 启用生产运行时的硬件并发策略。
每次准备将资源环境写入 `manifest.runtime_resource_environment`；恢复运行注入登记值，
配对注册拒绝两方资源环境不同。提高并发使用新 run ID，旧轮次及未完成题仍保留在其全域报告中。

- Harbor 固定到 commit `ea2fee78517f2e591bad69fcf1e6731f9c23ec99`。
- SWE-bench Verified 固定到 Harbor registry digest
  `sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341`，恰好 500 题。
- Terminal-Bench 2.1 固定到 digest
  `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`，恰好 89 题。
- TB 2.1 正式本地分数要求每题恰好 5 个有效 trial，即 445 个；覆盖不完整时 scorer 不输出
  final percentage。
- QEMU、libseccomp、libslirp、`crane`、SWE-bench、`pycdlib` 和 Harbor 都固定版本或 digest。

## 安装专用评测环境

不要把评测依赖装进 Tofu 服务环境。以下路径使用 DolphinFS 上专用、忽略索引的私有目录：

```bash
export TOFU_EVAL_ROOT="$PWD/.eval-runs/benchmarks"
python -c 'import os; from pathlib import Path; from evaluations.swebench.artifacts import prepare_output_root; prepare_output_root(Path(os.environ["TOFU_EVAL_ROOT"]))'
export TOFU_EVAL_VENV="$TOFU_EVAL_ROOT/venv"
export TOFU_QEMU_ROOT="$TOFU_EVAL_ROOT/rootless-qemu"
mkdir -p "$TOFU_EVAL_ROOT/.runtime/tmp" "$TOFU_EVAL_ROOT/.runtime/cache"
chmod 700 "$TOFU_EVAL_ROOT" "$TOFU_EVAL_ROOT/.runtime" "$TOFU_EVAL_ROOT/.runtime/tmp" "$TOFU_EVAL_ROOT/.runtime/cache"
export ROOTLESS_VM_TMPDIR="$TOFU_EVAL_ROOT/.runtime/tmp"
export TMPDIR="$ROOTLESS_VM_TMPDIR" TMP="$ROOTLESS_VM_TMPDIR" TEMP="$ROOTLESS_VM_TMPDIR"
export UV_CACHE_DIR="$TOFU_EVAL_ROOT/.runtime/cache/uv"

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
export ROOTLESS_VM_BASE_DISK="$TOFU_EVAL_ROOT/.base-disk/alpine-docker.qcow2"

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

## 用项目 YourProvider/Friday provider 跑单题

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
  --model deepseek-v4-flash \
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
export TOFU_KIMI_PROVIDER_FACE=yourprovider-chat
export TOFU_KIMI_SLOT_ID=<non-secret-shared-slot-id>
export KIMI_CHAT_BASE_URL=<kimi-chat-base-url>
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
export TOFU_KIMI_PROVIDER_FACE=yourprovider-chat
export TOFU_KIMI_SLOT_ID=<non-secret-shared-slot-id>
export KIMI_CHAT_BASE_URL=<kimi-chat-base-url>
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

## OCI 归档完整性

镜像下载后、归档前逐个校验 `blobs/sha256`，拒绝损坏内容、非普通文件、符号链接
和空目录。GNU tar 的并发修改检查仍保持启用；出现 `file changed as we read it`
时不能忽略其退出码。可用既有 `--archive-tool /absolute/path/bsdtar` 做兼容性对照，
但发布前应另核对 ISO 中归档的 blob 字节摘要，不能仅以替代工具退出零判定正确。
离线校验规格：`tests/deploy/test_oci_blob_validation.py`。

## OpenCode 与 Tofu 的配对证据

### 使用 TofuDB 的应用运行路径

`python -m evaluations.application_agent --request request.json --origin
http://127.0.0.1:15000 --live` 通过当前应用的 `/api/v4/agent/run` 执行一项任务。
应用必须位于该题隔离环境内，预先配置同一注册模型和 provider，且原生 TofuDB
就绪。该入口保留应用原生工具、上下文和执行引擎；不调用 storage-free SDK。
它只负责一次任务的 HTTP 传输与收据，Harbor 仍负责环境和独立 verifier。

请求是精确字段 JSON：`model={creator_id,model_id}`、`provider_id`、`instruction`、
`project_root`、新的 `output_root`、`config`（显式 `maxTokens`）、`thinking`、
`timeout_seconds`、`artifact_max_bytes`。路径均为隔离环境内绝对路径。
证据容量至少 2048 字节，其中 1024 字节预留给失败回执；HTTP 轮询受剩余任务时间限制。
不加 `--live` 只校验、不调用模型；API 认证通过 `--api-token-env NAME` 引用
进程环境，值不进入请求文件或报告。模型路由必须经生产 admission 预先注册。

入口拒绝非 loopback、代理、重定向、缺失身份的存储和非 TofuDB 后端。
它禁用模型 fallback，固定 provider，使用生产 cursor 读取原始事件，并对
`view=durable` 返回的终态与 live 终态核对内容、状态、usage 和模型。
usage 复用生产持久化投影，剔除瞬时 wire 数据；完成判定复用原生 `AgentResult.ok`，
输出截断、异常流结束和未完成工具交接不能被 `done` 状态覆盖。
响应必须明确声明 `readSource=durable`，旧服务忽略 query 不算验证通过。
提交响应未知时只按独有 conversation correlation 查询、取消并等待，不重新提交。
失败和未收敛的取消仍保留结果收据；同一输出目录不能重用。

Problem：旧 Harbor candidate 使用 transient `AgentRuntime`，不能证明 TofuDB
执行。Decision：调用生产持久化 API，并读回同一存储 authority 的已提交终态。
Alternatives：只设置数据库环境变量、复用进程内终态、独立复制 agent loop
均不能满足这个要求。`completed` 只表示应用执行完成，`formal_score` 始终为空；
运行源码、物理模型/采样、隔离环境、Harbor 接入及全量比较仍须各自验收。
测试：`tests/evaluations/test_application_agent.py` 与
`tests/api_v4/test_durable_chat_replay.py`。

OpenCode 固定到 [1.19.0](https://github.com/anomalyco/opencode/releases/tag/v1.19.0)
（2026-10-15 查询 GitHub latest 与 npm latest 一致），使用
`rootless_vm.harbor_opencode_agent:OpenCodeGuestAgent`
及宿主 `evaluations/opencode_proxy/`。guest 只使用固定二进制、离线 provider cache
和限定代理端点；宿主固定物理模型并保留逐 trial usage。配置、轨迹与代理 usage 的
调和仍由 `audit` 负责。Tofu 审计按登记模型核对 request、prompt profile、物理 dispatch、
usage 和 ATIF；GLM 不会再被 Kimi 专用身份判断误拒绝，混入其他模型仍然失败。接入测试见 `tests/evaluations/test_opencode_formal_runtime.py`。
OpenCode ATIF 使用 `agent.extra.trial_id_sha256` 保存非密钥的试验关联哈希，避免被
通用 token 脱敏破坏。审计将其与宿主代理记录严格关联；兼容旧的有效 `trial_token`，
拒绝冲突、伪造或已丢失的身份。真实 credential 脱敏保持启用。导出测试必须经过实际
`write_collected_trajectory`，不能仅手写一个绕过脱敏的 JSON fixture。

历史结果可以直接生成可追溯诊断，不需要模型调用：

```bash
python -m evaluations.swebench compare \
  --baseline $TOFU_EVAL_ROOT/opencode-glm53 \
  --candidate $TOFU_EVAL_ROOT/tofu-glm53 \
  --output-root $TOFU_EVAL_ROOT/comparisons --run-id glm53-diagnostic
python -m evaluations.swebench comparison-verify \
  $TOFU_EVAL_ROOT/comparisons/glm53-diagnostic/report.json
```

`report.md` 是阅读入口；`report.json` 保留每题结果路径、task ref/checksum、源码
revision、配置身份、分析器/审计器/投影器/任务锁 SHA-256、原始文件 SHA-256/字节数、
异常、模型/工具事件边界、全域当前通过率及分差、
未完成题及审计失败项。只记录选定的非敏感字段，不把命令、prompt 或推理正文写入报告。
原始证据继续由源 run 保存；`comparison-verify` 重新计算哈希，检测删除或修改。
正在运行的源目录属于动态诊断，后续写入可能使旧报告校验失败；应保留已结束 run，
不要移动或改写其产物。每个 arm 最多读取 10000 个文件、2 GiB，总计最多 4 GiB；
单文件上限 64 MiB，越界拒绝。报告以新的私有 run 目录发布，不覆盖旧报告、不删除原始
证据；报告与源 run 的保留/清理由调用方显式管理，框架不会静默回收失败成本证据。

新实验先分别使用既有 `run --dry-run` 准备两个 run，再在任何 dispatch 之前登记。
Tofu runtime JSON 必须显式设置 `maxTokens`，与 OpenCode runtime manifest 中的
`outputTokens` 一致（默认 32768，可用 `--opencode-output-tokens` 显式调整）；
模型上下文上限也必须一致。
未知/隐式输出预算会阻断登记：

```bash
python -m evaluations.swebench comparison-register \
  --baseline $TOFU_EVAL_ROOT/opencode-new \
  --candidate $TOFU_EVAL_ROOT/tofu-new \
  --output-root $TOFU_EVAL_ROOT/comparisons --run-id glm53-pair \
  --target-pp 10 --seed 1729
python -m evaluations.swebench resume $TOFU_EVAL_ROOT/opencode-new
python -m evaluations.swebench resume $TOFU_EVAL_ROOT/tofu-new
```

登记仅接受 `prepared`、尚无 job/dispatch 的两个 run。它复用 launcher 锁，冻结任务集合、
每题次数、模型/provider/slot、Harbor/QEMU/base image 身份、并发、超时和零内部 retry；
两边完整配置分别按 SHA-256 固定。`resume` 可启动尚未执行的 prepared run，或继续已有
job；每次启动都复验登记摘要和既有源码/可执行文件/隔离预检。更改配置或重复启动已完成
run 会失败。该协议是本地固定模型、各 harness 原生配置比较；它不声称两种 harness 的
推理/采样参数相同。需要等采样参数的实验还必须固定并核对实际请求字段，不能从缺省值
推断等价。既有已运行 trial 不能事后注册成确认性证据。

指标是每题各 trial 的二元 reward 均值，再对题目等权平均，**不是 pass@5**。布尔值、
字符串、NaN/Infinity、缺失值不能充当 reward。模型 timeout 只有在 verifier 完成并给出
数值 reward 后才进入已评分统计；其他未解决执行异常保留为未评分。
**唯一统计域是锁定的全部 89 题，不计算共同有效子集分数。** 分母始终为
89 × 每题预定次数；当前通过率 = 已验证成功次数 / 该固定分母。即使只运行部分题，
其余题也以缺失进入完整账本。成功、已评分失败、未评分、缺失四类之和恒等于分母。
重复/多余 trial 不择优，该题全部预定次数记为未解决；未完成和 surplus 阻断正式结论。
报告中的当前通过率用于观察整个 harness 已交付的成功率，不将基础设施故障归因于模型。

正式本地判定要求预注册、两边各 89×5、完整审计及全部证据通过、launcher 成功。配对
bootstrap 仅在双方全域每个预定 attempt 都已评分时计算，固定种子、10000 次重采样，
以任务为簇（五次重复不当成五道独立题）；缺失时不从子集计算置信区间。
点估计达到 +10pp 时为 `point_target_met`；95% 区间下界也达到 +10pp 时才为
`target_confirmed`。CI 不能消除开发集选择偏差、服务端版本漂移或环境差异。
`compare --require-target` 只有 `target_confirmed` 才退出零，缺失证据始终阻断声明。
完整复测必须另建两个 run；开发诊断题不能被挑出来充当最终验收集。

GLM-5.3 的官方 88.2 分采用 Claude Code 2.1.207、`reasoning_effort=max`、
`temperature=1.0`、`top_p=1`、`max_new_tokens=65536`、6 小时超时，见
[模型卡及 Terminal-Bench 2.1 脚注](https://huggingface.co/zai-org/GLM-5.3/blob/main/README.md)。
这份来源没有在 TB 2.1 脚注中给出上下文长度、重复次数或完整容器资源，不从其他基准脚注移植。
本地历史 M17 跑次使用不同预算（Tofu high、agent 上限 3600 秒，另受 Harbor 题目超时约束），
且是 QEMU TCG。必须把预算、模型服务身份、工具能力及环境差异分别验证；88.2 是待对齐的
参照，不是本地运行已经复现的成绩。不能以放宽候选预算却保持基线预算来声明领先 10pp。

本地 GLM 的下一轮全域运行可使用下列预算参数（附加到上文对应 arm 的 `run --dry-run`
命令；不传 `--task` 或 `--limit`，第一次全域诊断用 `--attempts 1`，冻结验收用 `--attempts 5`）：

| arm | 显式预算参数 |
|---|---|
| Tofu | `--reasoning-effort max --tofu-agent-timeout-seconds 21600 --agent-timeout-override-seconds 21600`；runtime JSON 至少显式含 `{"maxTokens":65536}` |
| OpenCode | `--opencode-agent-timeout-seconds 21600 --agent-timeout-override-seconds 21600 --opencode-output-tokens 65536 --opencode-context-tokens 200000` |

`--agent-timeout-override-seconds` 写入 Harbor `agent.override_timeout_sec`，仅支持本地
GLM 比较，且必须等于该 arm 的原生 agent 上限；仅设置原生上限不会覆盖题目的较短期限。
外层期限、额外上限与 token 预算在登记、resume 中核对；Kimi release 的题目预算不变。
这组参数只对齐公开的推理档请求、输出预算和 agent 时限；实际 provider 采样、M17 服务
与公开权重的等价性、上下文及容器资源仍须核对，不能据此声称复现官方 88.2。

88.2 是厂商模型卡报告，不等于本项目已验证的官方审核榜单记录。模型卡没有公布 TB 2.1
完整资源、任务哈希和重复次数；本仓库的 89×5 是固定的本地验收契约，不从 TB 2.0 或
TB 3.0 的公开说明推定 TB 2.1 当前提交许可。增加倍率、修改虚拟时钟或失败后扩大预算，
必须另立实验身份，不能保留旧预算成功再补新预算成功以形成可比较分数。

生产 Tofu 的 client tool `execution.timeout_s` 固定为 1830 秒，覆盖最长 1800 秒
命令及交接余量；该宿主设置和干净源码版本一起冻结，模型参数不能扩展它。
通用自定义工具默认仍为 120 秒，上限 3600 秒，采用单调时钟。等待注册早于事件发布，
失败回传也在抛出异常前留下工具结果及交接接受状态。契约见
[Custom tools](../../docs/CUSTOM_TOOLS.md)，离线规格见
`tests/tools/test_custom_tool_isolation.py`、`tests/tools/test_harbor_tofu_runtime_agent.py` 和
`tests/evaluations/test_tb21_comparison.py`。

原生运行时的 `abort()` / `close(abort=True)` 必须经过生产任务引擎的统一取消入口，
同时设置取消标志、唤醒 client waiter 并执行已登记的资源清理。只设置 abort event
会使旧任务继续占用共享 executor，后续题目的事件消费与环境启动也因此排队。
`tests/tasks_pkg/test_public_agent_runtime.py` 覆盖真实 waiter 的停止及 40 次连续关闭无残留。
独占 custom tools 必须从本轮实际公布的 schema 编译 ToolContractV2；工具可见但缺少
执行契约时，严格验证会拒绝所有调用。该边界由 `test_tool_gateway_contract_assembly.py`
验证，不能通过关闭契约校验来修复。真实 provider + QEMU 的功能探针须核对 guest
实际产物、工具交接、取消终态及清理；功能探针通过不计作 Terminal-Bench 分数。

Harbor 适配器的事件写入、工具审计和运行证据持久化在有界 executor 上串行等待，
每个 agent 最多一个未完成写入；保留逐事件 `fsync`，取消时先排空已开始的写入再关闭。
慢共享磁盘不再同步占住 Harbor 的 asyncio loop；`io-metrics.json` 记录事件持久化总耗时
与最大耗时（含 executor 排队）。轨迹报告另记录模型流结束到观察者收到 usage 的延迟，
不将这段延迟误算成模型思考时间，也不仅凭该时间差断言磁盘是唯一原因。

## 产物和搜索隔离

默认产物位于 checkout 的 `.eval-runs/benchmarks`，显式外部根在此部署也必须位于 DolphinFS。
每个 root/run 目录都是 mode 0700，并自带拒绝递归的 `.gitignore` 和
`.ignore`。若显式放进仓库，只允许 `.eval-runs/` 或 `eval-runs/`；普通源码目录会 fail closed。
仓库自身还在 Git、项目扫描器、VS Code watcher/search、Pylance 和导出器中独立排除了
`*_workdir`、`evaluation_results`、`sb-cli-reports`、`jobs` 和 `trials`。

不要把这些防线理解成“日志可以含 key”。运行后仍应扫描 command、manifest、transcript、QEMU
参数、proxy 日志和 guest 持久盘；任何 credential 命中都使该批结果无效。

## 官方 patch 复核

已有 patch 的最终官方复核仍交给固定版本的 SWE-bench harness。它目前只支持 Docker 或 Modal；
这条限制与上面的本地 agent rollout 无关。不同 `model_name_or_path` 会拆成独立 run id，避免
官方 harness 以 `instance_id` 为键时覆盖另一模型的结果。

## 用量与成本统计

`comparison-register --reference-prices prices.json` 冻结 USD/百万 token 参考单价：
`inputUsdPerMillion`、`outputUsdPerMillion`、`cacheReadUsdPerMillion`、
`cacheWriteUsdPerMillion`。这是统一价格下的比较，不是实际账单；缺价格则只报告用量。
Tofu 按 `apiRounds` 单次入账（不重复加入 compactionUsage），OpenCode 优先读取 trial
标签对应的宿主代理逐请求回执，覆盖超时后缺失 guest 导出的情况。两边归档基础设施
重跑也进入总支出。无用量回执明确为未知，不能算成零或据此宣布成本显著下降。
成本比和延迟差以整题为簇重采样，保留全部重复；成本区间要求全域评分和用量完整。
按最终结果分组的成本只作事后诊断，不能充当运行时成功/失败 oracle。

Tofu `reasoning_effort=default` 保留生产运行时的默认决策；`runtime_config={}` 的
请求输出预算从生产 resolver 冻结到 manifest，再由模型/上下文传输约束裁剪。
实际请求参数回执与声明配置同时保留，不能把 harness 原生默认比较称为等采样比较。
模型身份使用维护目录的精确 creator/model，推理默认值通过生产准入边界安装；
供应商的 `glm-5.3-m17` 只作 Deployment wire ID，200k 限额属于 Offering。
这避免私有别名被误当成资料不全的新模型，也避免给 Kimi 套用 GLM 的推理协议。
证据投影分别核对全程一致的逻辑模型身份与精确的物理 dispatch wire ID；旧记录的
已登记别名仍可读取，但同族的另一部署不能通过核对。

`capacity.plan_rootless_pair` 同时接受实际 state 卷剩余空间和每 VM overlay 的
字节上界（virtual disk 加 qcow2 metadata 余量），在 CPU/内存宽度之外按磁盘收窄。
在缓存迁入后重新探测；空间变大时并发相应放大，不把旧轮次的并发硬编码为默认。

重复任务的基础设施重跑只归档未评分且已分类的失败；保留同题已评分的成功与失败
兄弟 attempt，交给 Harbor 的配置多重集恢复缺失次数，避免重跑变成择优或超额试验。
重复次数也进入资源峰值：同一道重任务的多个 attempt 可能同时运行，不能只按不同题的资源排序。
`comparison-register --infrastructure-retries N` 把上限纳入协议摘要；重跑入口拒绝与冻结值不同的上限，报告也检查已归档的周期是否越界。

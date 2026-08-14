# 无本地 Docker 的 Agent Benchmark 评测

这套入口支持 SWE-bench Verified 和 Terminal-Bench 2.1，并替代旧的 udocker/PRoot
runner。它不自行实现容器或改写测试：

- agent 评测交给固定版本的 [Harbor](https://github.com/harbor-framework/harbor)。本地默认使用
  Singularity/Apptainer；每个 `task × model` 都使用只读 SIF 基础镜像和新的 writable tmpfs，
  trial 结束后删除可写层，只共享不可变镜像缓存。
- 已有 patch 的最终复核交给固定版本的
  [SWE-bench 官方 harness](https://github.com/SWE-bench/SWE-bench)。同一文件中的不同模型
  会先拆成不同 run id 和目录，避免官方 harness 以 `instance_id` 为键时互相覆盖。
- udocker、PRoot 和 host/local backend 不在支持列表里。缺少真正的隔离能力时预检直接失败，
  不会静默降级。
- 产物默认写到仓库外的 `~/.local/state/tofu-evals/agent-benchmarks/`。每一级产物目录都自带
  `.gitignore` 和 `.ignore`；仓库的 Git、项目扫描器、VS Code watcher/search 和导出器也有
  独立兜底规则。

这里的“无 Docker”是指本机不安装、不启动 Docker daemon，也不依赖云 sandbox。Harbor 直接
拉取任务已经发布的 OCI 镜像并由 Apptainer 转为本地 SIF。Terminal-Bench 2.1 的 89 个任务都
提供了固定的预构建镜像，因此本地路径不需要解释或重新执行 Dockerfile。

## 安装

不要把评测依赖装进 Tofu 的服务环境。建议使用仓库外的专用 venv。Harbor 固定到已审计的
upstream commit（当前共享机的 Python index 没有 Harbor 0.21 包），安装时需要访问 GitHub：

```bash
python3 -m venv ~/.cache/tofu-evals-venv
uv pip install --python ~/.cache/tofu-evals-venv/bin/python \
  -r evaluations/swebench/requirements.txt
```

此外需要本地安装 Singularity 或 Apptainer。优先使用系统管理员提供的近期 Apptainer；没有
root 权限时可按[官方 unprivileged 安装说明](https://apptainer.org/docs/admin/main/installation.html)
装到用户目录。rootless fakeroot 的完整能力通常还需要管理员配置 `/etc/subuid`、`/etc/subgid`
以及 `newuidmap/newgidmap`：

```bash
export PATH="$HOME/.local/apptainer/bin:$PATH"
# 用户目录安装可直接修改自己的配置；系统安装需要管理员执行等价配置。
apptainer config global --set "sessiondir max size" 10240
export TOFU_EVAL_BACKEND=singularity
~/.cache/tofu-evals-venv/bin/python -m evaluations.swebench doctor \
  --benchmark terminal-bench-2.1 --backend singularity
```

`doctor` 会检查 Harbor 兼容版本、Terminal-Bench 固定 digest 的公开元数据、本地 runtime 与
mount namespace、至少 10 GiB 的 writable-tmpfs 上限、磁盘空间和产物位置。任一安全前提不成立
都会返回非零状态。模型供应商的 key（例如
`OPENAI_API_KEY`）在正式运行时另行通过 `--secret-env` 注入。

当前这台开发容器本身运行在受限 Kubernetes Pod 中：没有 `/dev/fuse`，且宿主禁止 Apptainer
所需的 mount propagation，所以它的本地 `doctor` 会失败。这一限制不能由 pip/conda 安装绕过；
需要在实体 Linux 主机/VM 上运行，或由平台按 Apptainer 官方 nested-container 要求开放相应
security options 和 `/dev/fuse`。这不影响评测入口在普通本地 Linux 上使用。

## 先跑交叉污染 smoke

同一个 task 在同一批次跑两个模型，应该生成两个独立 trial（本地后端会依次执行）：

```bash
export OPENAI_API_KEY=...
~/.cache/tofu-evals-venv/bin/python -m evaluations.swebench run \
  --benchmark swebench-verified \
  --agent codex \
  --model openai/model-a \
  --model openai/model-b \
  --task django__django-11099 \
  --backend singularity \
  --secret-env OPENAI_API_KEY \
  --run-id smoke-two-models
```

`--secret-env` 只把 `${OPENAI_API_KEY}` 占位符写进 job config；真实值从进程环境解析，
不会进入 manifest 或命令行。成功后运行：

```bash
~/.cache/tofu-evals-venv/bin/python -m evaluations.swebench audit \
  ~/.local/state/tofu-evals/agent-benchmarks/smoke-two-models
```

审计会核对预期 trial 数、完成数、基础设施错误数、唯一模型配置、隔离后端以及
`environment.delete=true`。默认任何错误都会令审计失败；只有分析已知失败时才使用
`--allow-errors`，不要用它发布分数。

## 全量 500 题

```bash
~/.cache/tofu-evals-venv/bin/python -m evaluations.swebench run \
  --benchmark swebench-verified \
  --agent codex \
  --model openai/model-a \
  --model openai/model-b \
  --backend singularity \
  --secret-env OPENAI_API_KEY \
  --run-id verified-20260813
```

默认数据集固定为 Harbor registry 的 `swebench-verified@1.0`（500 题），不是滚动的
`latest`。基础设施异常最多重试两次；agent timeout、verifier timeout、认证错误、额度错误和
解析错误不会被伪装成可重试基础设施抖动。

中断后只恢复未完成 trial：

```bash
~/.cache/tofu-evals-venv/bin/python -m evaluations.swebench resume \
  ~/.local/state/tofu-evals/agent-benchmarks/verified-20260813
```

## Terminal-Bench 2.1

数据集固定为官方 registry 的
`terminal-bench/terminal-bench-2-1@sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`，
共 89 题。默认每题运行
5 次，与官方 leaderboard config 的 trial 形状相同，因此一个模型的全量评测是 445 个独立
trial。先用单题、单次 smoke 验证凭据和 agent 版本：

```bash
export OPENAI_API_KEY=...
export CODEX_EVAL_VERSION=0.125.0  # 替换为实际被测版本

~/.cache/tofu-evals-venv/bin/python -m evaluations.swebench run \
  --benchmark terminal-bench-2.1 \
  --agent codex \
  --agent-version "$CODEX_EVAL_VERSION" \
  --reasoning-effort xhigh \
  --model openai/gpt-5.5 \
  --task terminal-bench/write-compressor \
  --attempts 1 \
  --backend singularity \
  --secret-env OPENAI_API_KEY \
  --run-id tb21-smoke
```

smoke 通过后，去掉 `--task` 和 `--attempts 1` 即运行固定的 89 × 5 全量协议：

```bash
~/.cache/tofu-evals-venv/bin/python -m evaluations.swebench run \
  --benchmark terminal-bench-2.1 \
  --agent codex \
  --agent-version "$CODEX_EVAL_VERSION" \
  --reasoning-effort xhigh \
  --model openai/gpt-5.5 \
  --backend singularity \
  --secret-env OPENAI_API_KEY \
  --run-id tb21-codex-20260813

~/.cache/tofu-evals-venv/bin/python -m evaluations.swebench audit \
  ~/.local/state/tofu-evals/agent-benchmarks/tb21-codex-20260813
```

审计会核对固定 dataset digest、Harbor 与 benchmark 来源 commit、89 × 模型数 × 5 的基数、
每个 `model × task` 恰好 5 个 trial、每个 trial 的结果文件，以及所有正奖励 trial 的
`agent/trajectory.json`。runner 永远不传 Harbor 的 `--upload`，manifest 也固定记录
`upload_enabled=false`。

这能生成可复现的内部评测结果，但不等于已被官方 leaderboard 接收。Terminal-Bench 2.1
官方仓库目前关闭社区提交，而且官方发布还包含静态检查与 reward-hacking 人工审核；本地 audit
不能替代该审核。

## 为什么本地强制串行

Harbor 的 Singularity 后端使用 `--containall --pid --fakeroot --writable-tmpfs` 隔离文件系统和
进程，但默认共享宿主网络，而且没有严格的单 trial cgroup。runner 因此强制
`n_concurrent_trials=1`，避免两个任务的 localhost 服务和端口相互污染，并在 manifest 中明确
记录 `network_namespace_isolation=false`、`strict_cgroup_isolation=false`。本地结果适合开发、
回归和内部比较；若需要与官方 Daytona 环境完全一致的 leaderboard 口径，仍需使用官方环境。

## 可选云后端

只有确实需要云并发时才安装额外依赖：

```bash
uv pip install --python ~/.cache/tofu-evals-venv/bin/python \
  -r evaluations/swebench/requirements-cloud.txt
```

然后显式选择 `--backend modal` 或 `--backend daytona`；核心本地安装不包含这些 SDK，也不需要
任何云 sandbox 凭据。

## 用官方 harness 复核 patch

输入可以是 JSON、JSON object map 或 JSONL；每行必须有 `instance_id`、
`model_name_or_path` 和 `model_patch`：

```bash
~/.cache/tofu-evals-venv/bin/python -m evaluations.swebench doctor \
  --official --backend modal

~/.cache/tofu-evals-venv/bin/python -m evaluations.swebench grade \
  --predictions /absolute/path/predictions.jsonl \
  --backend modal \
  --run-id official-verified-20260813
```

不同 `model_name_or_path` 自动拆分到独立工作目录和独立官方 `run_id`。这样同一个
`instance_id` 的 A/B patch 不会共享测试输出、完成标记或报告。Docker 可用时也可以指定
`--backend docker`；预检要求 daemon 可用且至少 120 GiB 空闲空间。
这一条是 SWE-bench 官方 patch harness 自身的限制，不影响上面的 Harbor + Apptainer agent
rollout；官方 patch harness 当前不提供 Singularity backend。

## 产物与发布边界

目录结构大致如下：

```text
TOFU_EVAL_ROOT/
  .gitignore                 # *，整个根目录拒绝 Git 收集
  .ignore                    # *，ripgrep/项目搜索拒绝递归
  <run-id>/
    manifest.json            # 无密钥的来源、版本、基数和状态
    job-config.json          # 只有 ${SECRET_NAME} 占位符
    launcher.log
    jobs/                    # Harbor trial、轨迹和 verifier 输出
```

不要把 `TOFU_EVAL_ROOT` 指到任意普通源码目录。若确实必须放进仓库，只允许
`<repo>/.eval-runs/` 或 `<repo>/eval-runs/`；其他仓库内路径会被 fail-closed 预检拒绝。

Harbor adapter 当前数据定义允许 agent/verifier 公网访问；本 runner 不会暗中改变上游任务
语义。若实验要求防止联网检索答案，应先发布一份带 Harbor phase network policy 的固定数据集，
并只允许模型 API 域名。不能把 prompt 中的“不要联网”当作网络隔离证据。

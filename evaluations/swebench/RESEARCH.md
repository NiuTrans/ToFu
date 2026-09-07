# 评测框架调研与取舍（2026-08-13）

## 一手资料

- [SWE-bench 官方评测指南](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/evaluation.md)：
  官方 harness 以 Docker 保证一致性，支持 Modal；提供 image cache level、自动清理和按
  instance 运行。官方 README 给出的本地容量建议是 120 GiB、16 GiB RAM、8 CPU。
- [SWE-bench sb-cli](https://github.com/SWE-bench/sb-cli)：官方托管评测提交、运行管理和报告
  获取。它适合最终 patch 评分，但不是通用 agent rollout runner。
- [Harbor](https://github.com/harbor-framework/harbor) 及其
  [SWE-bench adapter](https://github.com/harbor-framework/harbor/tree/main/adapters/swebench)：
  原生支持 SWE-bench Verified、Codex/Claude Code/OpenHands/mini-swe-agent 等 agent，支持
  Docker、Modal、Daytona、E2B、Runloop、GKE 等后端，并提供 trial 生命周期、并发、基础设施
  重试、resume、资源限制和结构化结果。adapter 文档记录了与官方结果的 parity 实验，也明确
  列出少量 oracle/云后端异常题，不能把它们误算成模型失败。
- [Terminal-Bench 2.1 官方仓库](https://github.com/harbor-framework/terminal-bench-2-1)、
  [固定数据集](https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/latest)
  与[排行榜](https://www.tbench.ai/leaderboard/terminal-bench/2.1)：官方提交配置固定数据集 digest，
  共 89 题，每题 `n_attempts: 5`，使用 Daytona 独立 sandbox。完整发布还要求静态检查、正奖励
  trial 的 trajectory 和 reward-hacking 审核；当前官方仓库注明社区提交已关闭。
- [OpenHands benchmarks](https://github.com/OpenHands/benchmarks)：同样采用“每实例容器”与
  Docker/远程 runtime；本地 runtime 明确没有隔离，只适合开发。这验证了“不具备容器能力时
  必须远程执行或 fail closed”的边界。
- [SWE-ReX](https://github.com/SWE-agent/swe-rex)：把执行环境抽象为 Docker、Modal、Fargate、
  Daytona 等独立 sandbox，并面向大规模并发；它适合作为运行时组件，但没有替代 Harbor 的
  dataset/agent/trial/result 编排层。
- [mini-swe-agent 的 SWE-bench 配置](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/config/benchmarks/swebench.yaml)：
  官方示例仍以 Docker 环境运行每题，进一步说明 host/conda 不能作为正式隔离后端。
- [Apptainer 安装与 user namespace 文档](https://apptainer.org/docs/admin/main/installation.html)：
  支持不安装 Docker daemon 的本地 OCI/SIF 运行，但非特权模式仍要求宿主允许 user/mount
  namespace；writable overlay 通常还依赖 FUSE/overlayFS。嵌套在受限容器内时，官方明确要求
  放开相应 seccomp/system-path 限制并提供 `/dev/fuse`。

## 对旧 udocker 方案的诊断

旧实现复用了按 task 命名的容器 root，并在 PRoot 上叠加了 TemporaryFile、mkfifo、DNS、
pathconf、tox、setuptools、httpbin、代理等 20 类 monkeypatch。这产生三个根本问题：

1. 容器 root 是可写且跨 model 复用的，`git reset/clean` 只清理 repository，清不掉 conda、
   home、tmp、服务进程和系统级缓存，污染不可能被证明不存在。
2. PRoot 没有真正的 mount/cgroup 隔离；当前主机的 user mount namespace 探测也返回
   `Operation not permitted`。继续补 shim 只能改变被测环境，不能得到官方等价性。
3. 可写 distro tree 和大量小文件位于 FUSE 项目盘，创建、切换和清理环境的 metadata I/O
   成为瓶颈；手工清理池只能隐藏延迟，无法消除它。

当前 Kubernetes service account 虽可创建和删除 Pod，但 RBAC 拒绝 `pods/exec` 与
`pods/log`。Harbor 需要在同一沙箱中多轮执行命令并回收日志，因此不能把“能创建 Pod”误报成
可用的 Kubernetes 后端；除非管理员补齐最小权限并对后端做集成验证，否则同样 fail closed。

当前 Pod 虽允许创建单独的 user namespace，但拒绝 root mount propagation，且没有
`/dev/fuse`。实际的 Apptainer pull 能成功生成 SIF，`exec --fakeroot --containall --pid
--writable-tmpfs` 则在 mount propagation 阶段得到 `Operation not permitted`。因此这台 Pod
不能承载嵌套本地 runtime；该路径面向允许 rootless Apptainer 的实体 Linux/VM。

## 决策矩阵

| 方案 | task/model 隔离 | 官方等价性 | 当前无 Docker 主机 | 大规模并发 | 结论 |
|---|---:|---:|---:|---:|---|
| udocker/PRoot | 弱 | 低（需大量 shim） | 可运行 | 低 | 删除出主路径 |
| host conda/worktree | 仅文件级 | 低 | 可运行 | 中 | 不允许作为正式评测 |
| 官方 Docker harness | 强 | 最高 | 不可运行 | 中 | 本地有 daemon 时用于 patch 复核 |
| 官方 Modal harness | 强 | 最高 | 可运行 | 高 | 当前环境的 patch 复核首选 |
| sb-cli | 托管 | 官方 | 可运行 | 托管 | 发布/远程评分备选 |
| Harbor + rootless QEMU/TCG | VM + user/net/PID/IPC/UTS namespace + seccomp + chroot | 同一任务定义/镜像，时钟性能不同 | 当前主机已实测 | 中 | 无 root 本地正式回归主路径 |
| Harbor + Apptainer | 文件/PID 强，网络/cgroup 较弱 | 使用同一预构建镜像 | 当前 Pod 不可运行 | 低（强制串行） | 兼容后端，不再是默认 |
| Harbor + 云 sandbox | 强 | adapter 有 parity 证据 | 可运行 | 高 | 多 agent/多模型 rollout 首选 |
| Harbor + Daytona（Terminal-Bench 2.1） | 强 | 官方提交配置 | 可运行 | 高 | TB 2.1 首选 |

## 落地原则

- 共享的只能是不可变 image/snapshot/cache；workspace、home、tmp、进程、网络命名空间和日志
  必须属于单个 trial。
- rootless QEMU 只共享固定 digest 的只读 base/cache；每个 trial 使用新 qcow2 overlay。QEMU
  自身再置于无宿主网卡的 user/net/PID/IPC/UTS namespace、最小 chroot、零 capability、
  `no_new_privs` 和 seccomp 内。公网只能经过拒绝私网/metadata、认证、限流和限字节的 proxy。
- Apptainer 仍共享宿主网络且无严格 per-trial cgroup，因此只作为兼容后端并强制单 trial 串行。
- runner 不实现 verifier，不对 upstream test script 做兼容性改写。
- 运行身份由固定 dataset version、agent、model、backend、项目 SHA 和 harness version 构成；
  manifest 原子写入，重复 run id 拒绝覆盖。
- Terminal-Bench 2.1 额外固定 registry digest、89 题基数和每题 5 次的正式 trial 形状；内部 runner
  不上传结果，也不把本地结构审计冒充官方 reward-hacking 审核。
- 重试只用于 Harbor 识别的基础设施异常。超时、认证、额度、模型不存在和 verifier 解析失败
  保留为真实失败。
- 默认产物在仓库外；Git ignore、通用 `.ignore`、项目扫描器、编辑器 watcher 和导出器是互相
  独立的防线。
- SWE-bench 当前每任务导出一个完整 runc rootfs，隔离和恢复已成立，但跨镜像 layer 去重不足。
  全量 500 题的下一步优化应是数据集级 content-addressed OCI/cache backing，而不是重新引入
  可写共享容器 root。

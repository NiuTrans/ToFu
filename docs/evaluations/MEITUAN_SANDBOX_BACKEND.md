# yourprovider-sandbox 评测后端（粒度 1：画布 TerminalBenchPipeline 提交端）

状态：实现中（设计已评审，资源/预算已拍板）
日期：2026-09-30
作者：tofu 评测框架

## 1. Problem

tofu 的 Terminal-Bench 2.1 评测框架目前只能在**本地**跑 trial（`rootless-qemu` /
`singularity` / `docker` 等 `ISOLATED_BACKENDS`），受单机 CPU/内存/IO 限制，且单机
IO 抖动污染 trial 计时与结果。目标是把 TB2.1 trial 卸载到美团内部沙盒平台
（EnvSandbox），同时不牺牲 release 证据链的完整性（可复现、不可擦除、环境身份可证明）。

### 1.1 已实测钉死的约束

- **沙盒平台不开放通用 exec**：所有可访问集群（974/672/857/387/9,561,433）要么网关无
  `/api/sandbox/run_cmd`（404），要么直连被 IP 白名单挡（403），要么 token 失效/镜像
  起不来。通用执行需集群级工具绑定+网关路由+镜像预热三者齐备，token 解锁不了；平台
  方也不接受开通用集群的请求。
- **唯一已验证路径**：tofu 作为 agentix kernel（`agentix@<branch>@simple@tofu`）装进画布
  TerminalBenchPipeline 容器跑。实证：画布 362610 / flow 871428 DONE（Tofu 0.17.4，
  13 题 canary first-attempt 9/13）。

## 2. Decision

新增与 `start_harbor_run`（本地执行）**平行**的"远程提交"通路。不往
`ISOLATED_BACKENDS` 加枚举——远程 run 不产本地 trial 树/manifest，硬塞会撑坏
`audit`/`comparison` 的证据链。

三个组件（全部走封装好的 `llm` MCP 工具，不依赖原始 webApi 脚本）：

### 2.1 提交器（submitter）

凭证走共享 `~/.llm-cli-token.json`（`SsoAuth` 自动续 refresh_token；SSO 会话会过期
且无法无人值守刷新 → 长效 pipeline 必须 `check_login` 探活 + 失效告警）。

序列（每步都 read-back 校验）：
1. `check_login` + `whoami`（探活 + owner 校验，`_require_experiment_owner` fail-closed）
2. **翻转 donor 画布 cache flag**：`_ENV_RUN_SPEC_SET_DISABLE_CACHE` /
   `_DISABLE_SCORE_CACHE` 翻为 `"True"`（fork 继承 False → judge COMPLETED 但分数被
   丢弃 `not upload eval result`；其他字段字节级不变），并在 donor 上起新 run 保活
3. `experiment_fork(base_id, model_id)` → fork 画布
4. `experiment_get` / `experiment_get_node_config`（校验模型绑定，防 `llm_debug` 继承）
5. `experiment_set_canvas_params` 重放锚点（fork 会重置 agent/tb 锚点为模板值）：
   - `agent = agentix@<branch>@simple@tofu`（固定 tofu commit + agentix commit + harbor commit 三 SHA）
   - `tb_git_branch`、`max_agent_timeout_sec=604800`（7d，秒）
   - `repeatRunTime=0`（禁平台层重试，证据不可擦除）
   - `flowJobSubmitStateTimeOut`/`flowJobSubmittedStateTimeOut`/`flowJobRunningStateTimeOut`=10080（分钟）
   - `newEvalFlowHangKillSeconds`=604800（秒）
6. `experiment_get_canvas_params` read-back（update→run 有传播竞态）
7. `experiment_run(experiment_id)` 启动（`/preview` 只物化、`/saveExperimentConfig` 只存配置）

**坑**：`experiment_run_node(dry_run=false)` 可能返回 schema 假错但服务端已启动——
盲重试会双开画布烧配额，必须先 `experiment_get` 确认 node status。

### 2.2 监控器（watcher）

`experiment_eval_jobs`：list 模式给状态（taskId/status/model binding）；`job_id=` 详情
模式给 `resourceConfigList`/`consumerList`/agent `workers`。只读、60s 间隔、3 次连续
observer error 即停。

### 2.3 收分器（scorer）

**MCP `experiment_eval_result` 只给汇总分**——per-trial reward 必须读 dolphinfs
trajectories。路径模板：

```
/path/to/your/data
  <evaluateName去MT前缀>/<agent别名>/trajectories/
  instance_<task>_<ts>_<hash>/...
```

两层 result.json：
- 顶层（~783B）：`stats.evals.agentix__tasks.reward_stats.reward` 按 trial_name 聚合（元信息）
- inner（~3.9KB，判分权威）：`verifier_result.rewards.reward` + `agent_info.version` +
  `exception_info.exception_type` + `config.environment.type` + 各阶段时间戳

判分四分类（`exception_info` 是独立维度，AgentTimeoutError ≠ reward 0）：
- numeric 0 / numeric 1 / empty(setup 失败，无 inner) / still-running

**按题审计（任一不满足即拒封，不用 latest/keep-best 补数）**：
- 每题恰好一次有效 attempt（重复 attempt / 缓存命中 / 缺题 → 拒封）
- 三层证据逐 trial 核验：①画布 agent 字段 ②`agent/setup/stdout.txt` 的
  `[AGENTIX_COMMIT_FULL]` ③inner `agent_info.version`，三者一致才归档

## 3. 环境身份（sandbox_identity）

远程 run 记为独立 kind，与本地 QEMU 分榜不混排：

```
kind: yourprovider-sandbox
cluster: <cluster_id>            # 672 / 974 / ...
evaluate_name: <MT/...>          # 唯一定位 dolphinfs 轨迹目录
tb_git_branch / agent_branch     # harbor 框架分支 + agentix 分支（含三 SHA）
benchmark_runspec_id / dataset_id
environment_type: docker         # 画布 TB pipeline 在沙盒里跑的是 Docker（实测 inner config）
```

`audit` / `comparison` 识别为合法但不与本地 run 同榜。

## 4. 资源与 mass 配置（已拍板）

TB2.1 = **89 题 × 5 attempt = 445 trial**（catalog 实证），纯 CPU（模型走 API，无 GPU
serving）→ GPU 并发上限（`longcat_harness` 96 卡）不构成约束，GPU cards 配 0/最小。

| 参数 | 值 | 依据 |
|---|---|---|
| Agent workers | **64** | 445 trial / 64 worker ≈ 7 轮；~300 核，`cpu_weight_convert`(1002核空闲) 充裕；理论 ~7h 压回 8h 派发截止内（32 worker 理论 ~14h 会超） |
| Group workers | 1 | 不变 |
| GPU cards | 0 / 最小 | TB2.1 无模型 GPU serving |
| CPU 队列 | `root.hldy_training_cluster.your-user.cpu_weight_convert` | 1002 vcores / 4TiB 空闲（首选） |
| GPU 队列 | `root.shxs_training_cluster.your-user.autoresearch_agent` | `longcat_harness`/`p0` 绑定（96 卡上限，TB2.1 不耗卡） |
| 资源码 | `longcat_harness` / `mlpSchedulePriority: p0` | 顶层画布字段，不做 tofu 侧参数化 |
| 超时 | 7d（604800s / 10080min） | 四层超时字段 |
| `repeatRunTime` | 0 | 禁平台重试 |

**Sizing 系数**（hope skill 实测）：Agent worker ≈ 4核/8GiB，AM ≈ 20-30核，Group ≈ 4核/16GiB。

**唯一未钉死硬点**：flow 级 8h 派发截止与 `flowJobRunningStateTimeOut`（分钟，7d）的
关系未确认。实现时先 worker=64 + EDS=20 题冒烟实测 8h 截止行为与 per-trial 收分链路，
通过后上全量 445 trial。

## 5. Alternatives considered（被否决项）

1. **通用 exec 后端**：实测堵死（404/403/token 失效/镜像起不来），平台不接受开通用集群。否决。
2. **往 `ISOLATED_BACKENDS` 加 `yourprovider-sandbox` 枚举**：错配，现有 backend 全是本地 Harbor
   执行，远程 run 不产本地 trial 树。否决。
3. **tofu 自研工具镜像注册成沙盒新工具（粒度 2）**：把 tofu runtime 打包成带通用 exec
   HTTP 服务的镜像注册到沙盒平台。暂搁置，优先级低于粒度 1。
4. **手动 fork + 人工收分**（现状 362094/362610）：已验证可行但不可复用、易错、无证据链
   集成。本设计把它自动化。
5. **依赖 `experiment_eval_result` 拿 per-trial 分**：只返回汇总分。否决。
6. **autoharness `YourProviderSandboxBackend` 的 upload/download**：pssdk `sandbox_upload/download`
   必须走 S3 预签名四配置，autoharness 未配置、从未真跑通。否决。

## 6. 实现切面

- `evaluations/swebench/yourprovider_sandbox/{submit,watch,score}.py`：三组件。
- `evaluations/swebench/cli.py`：新增子命令（与 `--backend` 平行的远程提交入口）。
- `evaluations/long_agent_release/` 导出器识别 `kind=yourprovider-sandbox`。
- 测试：提交器 dry-run（不落盘）+ 收分器对 871428 真实轨迹的离线重放判分。

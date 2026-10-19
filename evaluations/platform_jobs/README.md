# 在内部计算平台运行 Harbor 评测

`python -m evaluations platform prepare` 把已提交的评测代码打包为现有
`tofu-environments` SDK 的 batch job。准备不登录、不提交、不分配资源。
`plan_on_platform` / `collect_from_platform` 连接 SDK plan / collect；submit、
status、cancel 仍由 SDK `JobService` 管理。

Problem：Harbor runner 与内部平台独立，用户机器没有 Docker/root 时需要明确的
远端执行环境。Decision：把完整 runner 放入平台 worker，复用 SDK 的账号、审批、
CAS、超时和结果回收。Alternatives considered：虚构 MLP exec 接口无法验证；复制
平台提交循环会造成重复提交和所有权漂移。

这是 job launcher，执行适配器仍为 Harbor。平台不自动赋予 QEMU separate verifier
能力。Tofu 正式文件工具仍要求 rootless guest；新题集 catalog 接入不是实测完成。

## 准备

安装可选 SDK，或设置 `PYTHONPATH=packages/tofu-environments:$PWD`。
arguments.json 是现有 Harbor run 的 argv 数组，例如：

```json
["--benchmark", "terminal-bench-2.1", "--agent", "oracle", "--model", "oracle",
 "--backend", "docker", "--attempts", "1", "--task", "terminal-bench/adaptive-rejection-sampler"]
```

task 必须来自冻结 lock。正式模型跑测使用对应 harness 的完整参数。launcher 管理
输出、run ID、可执行文件和源码证明。内部 retries 固定 0，不覆盖失败 attempt。
worker 输出固定在源码根下 `.eval-runs/benchmarks`，遵循原 runner 的隔离路径约束；
该目录属于产物生命周期，不参与运行源码证明，打包仍检查链接和证据完整性。
不要在 argv/provider options 写凭据；值由可信 worker 环境提供。普通 provider 的
凭据环境变量名也须通过 `--secret-env` 声明，以检查发布产物。

```bash
python -m evaluations platform prepare \
  --repository "$PWD" --revision "$(git rev-parse HEAD)" \
  --project-root "$PWD" --output-root "$PWD/.eval-runs/platform" \
  --owner-id "$EVAL_OWNER_ID" --run-id "$EVAL_RUN_ID" --project "$EVAL_PROJECT" \
  --arguments arguments.json --provider-options provider-options.json \
  --cpu 4 --memory-gib 8 --gpu-count 0 \
  --runtime-seconds 900 --result-bytes 67108864
```

资源是示例审批参数，须按 trial 需求调整。默认源码 bytes/files 从一次可用内存和
磁盘探测推导，显式 limits 保留。同名 run 不替换。输出 worker-source.zip 和
platform-registration.json，固定 source/request/spec SHA。只导出已提交公开
runtime 和 SDK，不携带 data、logs、uploads 或私人 bundle。Git commit/tree/blob
证明拒绝源码修改、链接、任意模块重定位或额外源码。runner 和 resume 重新校验。

## 平台 embedding

复用既有 selected durable repository 与 actor-bound provider：

```python
from evaluations.platform_jobs.payload import plan_on_platform
from evaluations.platform_jobs.collection import collect_from_platform

plan = plan_on_platform(service, actor, registration, provider="hope")
# 把完整 plan 交给宿主既有 exact-plan 审批，不能使用无条件 authorize。
service.submit(actor, plan["operation_id"], plan["plan_hash"], authorize)
service.status(actor, plan["operation_id"])
result = collect_from_platform(service, actor, plan["operation_id"], destination, registration)
```

worker 镜像须预装项目依赖、固定 source commit 的 Harbor，以及所选 backend。
普通非 root worker 可使用已配置 QEMU；Docker backend 仍需 worker 侧 daemon/权限。
这里不自动安装依赖或开启 privileged。doctor 或 Harbor 来源检查失败不启动模型。
账号绑定缺失由 SDK 报错，不借用账号、不切换存储、不重放结果未知的提交。

## 结果

声明输出为 platform-result.json 和 platform-evidence.zip。wrapper 可 exit 0 以便
发布失败诊断；另记 preflight_failed / benchmark_failed / completed / evidence_failed
与 Harbor child exit code。completed 仅说明 runner 结束；摘要 formal_score 固定 null，
必须另查原生 rewards 与正式 audit。

doctor/runner 共用总 deadline，预留 10% 时间打包；SDK 负责进程组终止。result
budget 预留 8192 bytes 给 receipt，其余供 ZIP。保留失败 trial/verifier/agent 证据，
排除重建 runtime 缓存。声明凭据、链接或预算超限时报告 evidence_failed 并拒绝
发布部分 ZIP；原始证据留在 worker。collect 先查所有权/资源/exit，再查 request
绑定与 ZIP size/hash，不从平台成功生成得分。

```bash
python -m evaluations platform inspect /absolute/collected-directory \
  --registration /absolute/platform-registration.json
pytest tests/evaluations/test_platform_jobs.py
```

离线测试覆盖真实 Git、SDK 审批/CAS、不确定提交不重放、受控子进程失败产物、
发布拒绝和 resume 漂移。它不是集群验收，实际镜像/backend/队列必须另有 worker 证据。

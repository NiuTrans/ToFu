# Tofu 极致稳定运行手册

“永不失败”无法由单个进程承诺：`SIGKILL`、宿主机断电、内核故障和存储损坏都
可能绕过应用。生产目标应是：避免可预防故障、提交数据不丢、故障可取证、服务在
有界时间内恢复。推荐验收线是可用性 SLO ≥99.9%、单 worker 故障 RTO <60 秒、
已提交数据库事务 RPO=0、异机备份 RPO ≤24 小时。

## 一、最重要的硬要求

1. **生产服务使用独立容器/cgroup。** 不要让 Tofu 与 code-server、pytest、模型
   推理、浏览器或构建任务共享同一个内存额度。共享 cgroup 中任一兄弟进程和页缓存
   都能触发 memcg OOM；Python 无法捕获内核的 `SIGKILL`。
2. **必须有进程外生命周期所有者。** 源码部署执行一次
   `python serverctl.py install`；Docker 使用仓库的 Compose 配置和
   `restart: unless-stopped`。不要同时运行 manager、旧 `tofu_guard` 和
   supervisord 三套所有者。
3. **资源必须显式有界且留余量。** 先以 8 GiB limit / 2 GiB reservation 起步，
   用 7 天 p99 重新定容。limit 至少为合法峰值工作集的 1.5 倍，且硬 RSS 轮换阈值
   应低于 limit，给子进程、页缓存和关停 drain 留空间。
4. **状态必须持久化并异机备份。** `data/` 和 `uploads/` 不能放在容器可写层。
   SQLite 的本地验证快照不等于灾备；将 `TOFU_SQLITE_SNAPSHOT_DIR` 指向独立
   挂载/NAS，并把该目录复制到另一故障域，定期做真正的恢复演练。
5. **探针与告警不可缺。** liveness 使用 `/api/health`，启动阶段给足数据库初始化
   时间；外部监控还要检查 `db_ok`、重启次数、cgroup 使用率、OOM 计数和磁盘空间。

## 二、立即启用

源码部署：

```bash
python serverctl.py install
python serverctl.py status
python serverctl.py doctor
```

Docker 部署：

```bash
TOFU_MEMORY_LIMIT=8g TOFU_MEMORY_RESERVATION=2g \
TOFU_BACKUP_DIR=/mnt/remote-backup/tofu docker compose up -d
docker compose ps
docker compose logs --tail=200 tofu
```

Compose 默认具备独立 cgroup、8 GiB 内存上限、2 GiB reservation、PID 上限、
日志轮转、45 秒优雅停机、健康探针和自动重启。默认 `tofu-backups` 命名卷仍在
本机，只是便利配置；生产必须用 `TOFU_BACKUP_DIR` 指向另一个故障域。宿主机必须
还能为内核、Docker 和其他服务保留内存；不要把所有容器 limit 之和配置到物理
内存的 100%。

## 三、内存防线

| 防线 | 默认行为 | 目的 |
|---|---|---|
| 任务背压 | 最多 16 个 resident agent task | 防止并发线性放大工作集 |
| cgroup admission | 源码 96%；Compose 90% | 高压时拒绝新任务 |
| 大请求闸门 | 源码 95%；Compose 90% | 避免组装大 body 时被杀 |
| aggregate relief | 源码 92%；Compose 85% | 清缓存、丢日志页缓存、`malloc_trim` |
| 进程软 RSS | `min(4 GiB, cgroup limit × 50%)` | 在 worker 变胖时主动回收 |
| 进程硬 RSS | `min(8 GiB, cgroup limit × 70%)` | 应用与 manager 独立采样，超限优雅轮换 worker |
| 外部恢复 | manager / container runtime | OOM、崩溃、卡死后重新拉起 |
| worker 风暴熔断 | 120 秒内 5 次失败 | 指数退避，第 5 次暂停并要求显式恢复 |
| 任务恢复熔断 | 2 分钟 5 次 boot | 避免恢复任务形成重启风暴 |

可用 `.env` 显式调整：

```dotenv
TOFU_PROCESS_RSS_RELIEF_MB=4096
TOFU_PROCESS_RSS_RECYCLE_MB=6144
TOFU_PROCESS_RSS_COOLDOWN_SEC=300
TOFU_MAX_INFLIGHT_TASKS=16
TOFU_ADMISSION_CGROUP_PCT=90
TOFU_MANAGER_MAX_FAILURES=5
TOFU_MANAGER_FAILURE_WINDOW_SECS=120
TOFU_SQLITE_SNAPSHOT_DIR=/mnt/remote-backup/tofu
TOFU_SQLITE_SNAPSHOT_MAX_AGE_HOURS=26
```

阈值必须来自观测，不要为追求更高吞吐直接关闭保护。若合法单任务就超过软阈值，
先把 PDF/视频/浏览器等重任务迁到受限 worker，再提高阈值。
manager 只依赖标准库和 `/proc`，即使应用导入失败或仍运行旧代码，也会执行硬 RSS
上限；这个外部回收同样计入 120 秒滚动失败窗口，持续泄漏不会演变为无限重启风暴。

## 四、Kubernetes 基线

至少配置以下关键字段；单副本只提供自动恢复，不提供节点级高可用：

```yaml
spec:
  terminationGracePeriodSeconds: 45
  containers:
    - name: tofu
      resources:
        requests: {cpu: "1", memory: 2Gi}
        limits: {cpu: "4", memory: 8Gi}
      startupProbe:
        httpGet: {path: /api/health, port: 15000}
        periodSeconds: 5
        failureThreshold: 36
      readinessProbe:
        httpGet: {path: /api/health, port: 15000}
        periodSeconds: 10
        failureThreshold: 3
      livenessProbe:
        httpGet: {path: /api/health, port: 15000}
        periodSeconds: 30
        failureThreshold: 3
```

生产 Pod 不要附带 IDE、测试 runner 或模型 sidecar。要抗节点故障，需要至少两个
副本、反亲和/拓扑分散、PodDisruptionBudget、共享权威数据库、Redis admission/push
状态和会话亲和入口；两个副本各用独立 SQLite **不构成 HA**。

## 五、告警与值班

建议告警：

- cgroup memory ≥80% 持续 5 分钟告警，≥90% 立即告警；
- `oom_kill` 增量立即告警，不能只看进程退出码；
- 10 分钟内 worker 重启 ≥3 次立即告警；
- worker RSS 连续增长、回收后仍不下降立即告警；
- `/api/health` 失败 2 分钟、`db_ok=false` 30 秒、磁盘可用 <15%、inode <10%；
- 备份超 26 小时未成功、最近一次恢复演练失败立即告警。

每次排障先保存以下只读证据：

```bash
date -Is
python serverctl.py status --json
python serverctl.py doctor --json
tail -n 120 logs/cgroup_pressure.log
tail -n 200 logs/server-console.log
```

`doctor` 会显示 cgroup 使用量/上限、swap 上限、内核 OOM 次数、当前 worker RSS、
manager 实际执行的 worker RSS 硬上限、
最近快照年龄和路径。manager 状态还会保留滚动故障数、上次退出的时间相关原因和
实际恢复 RTO；`status`/`doctor` 在 degraded、crashloop、manager 离线、快照过期或
内存 ≥90% 时返回非零，适合直接接入监控。
`oom_kill` 增长是 memcg OOM 的硬证据；只有 “Killed” 而无 traceback 通常是
`SIGKILL`，不要误当 Python 异常处理。

## 六、发布与故障演练门禁

每次发布按顺序执行：

```bash
python -m pytest -q tests/test_cgroup_guard.py tests/test_server_manager.py \
  tests/test_shutdown_quiesce.py tests/test_shutdown_hard_deadline.py
python healthcheck.py
python serverctl.py doctor
```

每月至少在预发布环境演练一次：强制杀 worker 后确认 manager 在 60 秒内换 PID；
制造 85%/90% 内存压力确认先背压再恢复；中断数据库/网络确认错误可见且不会无限
重试；从异机备份恢复到空环境并校验 SQLite `integrity_check`。故障注入不要在有真实
进行中任务的生产实例上执行。

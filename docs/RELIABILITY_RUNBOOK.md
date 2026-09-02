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
   `python serverctl.py install-recovery`；Docker 使用仓库的 Compose 配置和
   `restart: unless-stopped`。不要同时运行 manager、旧 `tofu_guard` 和
   supervisord 三套所有者。
3. **资源必须自适应、有界且留余量。** 个人模式启动时取 host、affinity/cpuset、
   cgroup CPU 配额的最小值，同时结合物理/cgroup 内存容量、当前可用内存和解析后
   的真实数据卷（含 `TOFU_DATA_DIR` / XDG 布局）剩余空间推导预算；探查失败使用
   保守下限，所有推导仍有硬上限。Compose 先以
   4 GiB limit / 512 MiB reservation 起步。8 GiB 是整机基线，不是 Tofu 可独占配额。
4. **状态必须持久化并异机备份。** `data/` 和 `uploads/` 不能放在容器可写层。
   SQLite 的本地验证快照不等于灾备；将 `TOFU_SQLITE_SNAPSHOT_DIR` 指向独立
   挂载/NAS，并把该目录复制到另一故障域，定期做真正的恢复演练。
5. **探针与告警不可缺。** liveness 使用 `/health/live`，启动阶段给足数据库初始化
   时间；外部监控还要检查 `storage.ready`、重启次数、cgroup 使用率、OOM 计数和磁盘空间。

## 二、立即启用

源码部署：

```bash
python serverctl.py install-recovery
python serverctl.py status
python serverctl.py doctor
```

对 live worker 执行 `stop` / `restart` 会中断在途任务：交互终端必须确认，非交互
调用必须消费用户在 Tofu UI 中批准的一次性 `shutdown` / `restart` 记录。服务本来
就未运行时，`stop` 保持幂等且不要求批准；`status`、`doctor`、`logs` 均为只读。

Docker 部署：

```bash
TOFU_MEMORY_LIMIT=4g TOFU_MEMORY_RESERVATION=512m \
TOFU_BACKUP_VOLUME=/mnt/remote-backup/tofu docker compose up -d
docker compose ps
docker compose logs --tail=200 tofu
```

Compose 默认具备独立 cgroup、4 GiB 内存上限、512 MiB reservation、PID 上限、
日志轮转、45 秒优雅停机、健康探针和自动重启。默认 `tofu-backups` 命名卷仍在
本机，只是便利配置；生产必须用 `TOFU_BACKUP_VOLUME` 指向绝对宿主机/NAS 目录，
Compose 会把它挂到 `/app/data/backups`，并让自动 SQLite 快照写入该挂载。宿主机
必须还能为内核、Docker 和其他服务保留内存；不要把所有容器 limit 之和配置到
物理内存的 100%。

自动 SQLite 快照由独立 maintenance 子进程执行：先校验目标卷剩余容量，再用在线
backup API 分页复制，完成 `integrity_check`、fsync 后原子发布。夜间任务不会执行
`VACUUM INTO`；压缩只允许在停写的深度清理窗口运行。超时会终止整个子进程组，下一
次任务会根据 PID manifest 回收已过期且 owner 不存活的 `.tmp` 产物。

网络盘上的 SQLite 还有两个专用判据。新版进程中，`turn.search.backfill` 只写一个
小型重建标记，真正的历史扫描和检索写入位于 host-local 可丢弃投影；
`system.reclaim` 在 BeeGFS/NFS/FUSE 上会在进入唯一 writer **之前**返回
`unsupported_storage_topology`。若日志仍显示这两个 operation 成为
`held_by`，说明运行进程尚未加载新版代码，应在没有在途任务时执行一次经 UI 批准的
受控重启。重启后观察 sidecar health 的 `turn_search_projection`；需要缩小权威库时
先运行只读 `python3 scripts/storage_deep_clean.py --analyze`，再按
[`STORAGE.md`](STORAGE.md) 的停服窗口执行离线压缩，绝不在服务中手工 `VACUUM`。

日报只从 `data/config/daily_reports/<owner_user_id>/` 读取。缺少 owner
身份的平铺文件不是可信数据源，启动过程不会读取、复制或猜测其归属。

## 三、内存防线

| 防线 | 默认行为 | 目的 |
|---|---|---|
| 任务背压 | 个人探针 1..8；8 CPU/8 GiB 常见结果为 4 | 防止并发线性放大工作集 |
| allocator | 个人随数值预算 1..4；容器早期回退 2；分布式 8 | 避免大载荷释放后留下几十个 64 MiB arena |
| Sidecar RPC | 个人探针 2..12；分布式 64 | 与有效存储并发对齐，不把 256 个线程当吞吐 |
| serving-loop 空闲线程 | 个人探针 300..1800 秒；分布式 3600 秒 | 超过热线底线的 sync/agent burst 线程仅在 active=queued=0 时整代退役；容量不变、后续按需重建 |
| subscription 路由探针 | 活跃竞速最多 32；批次结束为 0 | 单路由 singleflight 不变；最后一个在途探针完成后整池退出，下次网络竞速按需重建 |
| tree-index 构建器 | 同时最多 2 个 root；批次结束为 0 | 每个 root 内部 walk 仍受独立 job/时间/条目预算约束；最后一个 root 完成后精确代次退出，下次 stale refresh 按需重建 |
| Project refresh 消费者 | summary/status/watch 各最多 2；个人空闲 30..300 秒后为 0 | owner+project/conv 合并和队列上限不变；submit 与超时退出同锁仲裁，后续事件按需补齐 worker |
| 在线技能验证 | 并发搜索共享最多 4；最后一批结束为 0 | 批次 lease 阻止每请求复制线程池；异常残留在新代发布前有界排空，验证/安装信任边界不变 |
| Codex 模型目录 | 登录/真实变化后 3 分钟；稳定时 6→12→24→48→60 分钟；未登录/分布式为 0 worker | ETag 与归一化行比较识别真实变化；失败保留 last-good 并同样退避，缓存新鲜度上限 60 分钟 |
| 远程 provider 模型目录 | 变化/待删除确认 6 小时；无变化 12 小时；失败 6→12→24→48 小时 | 每 provider 持久化 next-attempt，重启不绕过；Settings Save 强制立即同步；空/失败响应保留 last-good |
| 本地端点监控 | health + autodiscovery 共享 1 个 worker；空模型 HTTP 2→4→8→15 分钟 | TCP 拓扑仍每 2 分钟检查，新端口/Settings 变化立即全探测；`TOFU_LOCAL_AUTODISCOVER_MAX_INTERVAL` 最大硬限 3600 秒 |
| Project presence TTL | 空注册表 0 个线程；首个 peer 1 个共享 sweeper；最后 peer 回收后回到 0 | 启动不创建空闲 worker；25 秒 active/180 秒 idle TTL 与 owner 隔离不变，精确线程身份阻止旧批次释放新 owner |
| Swarm 会话清理 | 活跃 session 共享 1 个 300 秒 timer；0 session 时为 0 | import 不启动线程；精确 timer 代次在最后 session 移除时取消，后续 session 按需重建 |
| Billing reserve 回收 | 非 multi-user/计费关闭时 0；启用时每 5 分钟 1 个 durable claim | 单一 Sidecar sweep，跳过仍运行任务，幂等 release；无第二个 request-worker janitor |
| 日报缺失补偿 | 0 个专用常驻线程；主 scheduler 每 6 小时 1 个 owner-scoped durable claim | 启动仅在昨日缺失时排入有界 hint；maintenance 子线程不阻塞主 tick，Sidecar claim 防重复 |
| 直连/代理路径探测 | 最多 64 个主机；新路径/真实失败立即探测；默认失败退避 3→6→12→24→48→60 分钟，稳定上限 60 分钟，闲置 24 小时停止 | 真实请求成功会延后同路径的合成 GET；持久化保留真实 last-seen，重启不会把废弃主机重新激活 |
| 日志后台维护 | 安静态 2 个周期 worker、默认至多 5 次唤醒/小时；重复尾部 worker 为 0 | 核心/外部保留共享 15 分钟线程；聚合空闲仅按小时 TTL 唤醒、有积压仍 15 秒批刷；首个被压制 delta 才创建短生命周期尾部 worker |
| Git integration 队列 | 本地 submit/retry 立即唤醒；空队列探测 3→6→12→24→48→60 秒 | 每日空读 28,800→≤1,440（降低 95%）；跨进程 ready/崩溃 claim 检测仍 ≤60 秒；存储故障独立 5..30 秒退避且停机精确 join |
| 会话目录到达合并 | 与 Sidecar RPC 容量相同，硬上限 256；8 ms 到达窗 | 仅合并同 owner/同投影且尚未开始的读取；开始即摘除，完成结果不缓存，饱和时直读 |
| Project refresh 队列 | 个人探针 16..128；分布式 512（status/watch/summary 各一条） | 唯一 scope 有界，重复 scope 合并；过载只延迟可重建的 warm refresh |
| 增量翻译 | 活跃 accumulator 个人 2..16、分布式 32；每任务 preview 队列个人 8..32、分布式 64 | 限制线程和文本积压；终态 handoff 永不丢弃 |
| 存储启动余量 | `data/` 所在卷容量的 1%，限制在 256 MiB..2 GiB | 不让 SQLite/WAL 吃掉文件系统最后空间 |
| cgroup admission | 源码 96%；Compose 90% | 高压时拒绝新任务 |
| 大请求闸门 | 源码 95%；Compose 90% 触发检查；还需绝对余量低于 `max(64 MiB, body×8)` 才拒绝 | 共享 cgroup 不因百分比假阳性中断小请求 |
| aggregate relief | 源码 92%；Compose 85% | 清缓存、丢日志页缓存、`malloc_trim` |
| aggregate relief 冷却 | 连续 5 次无有效回收后默认 600 秒 | 共享 cgroup/FUSE slab 压力下停止无效缓存抖动，日志与 RSS 防线继续 |
| 进程软 RSS | 个人按有效容量/当前余量推导；8 GiB 常见结果 2 GiB | 在 worker 变胖时主动回收 |
| 进程硬 RSS | 个人按有效容量/当前余量推导；8 GiB 常见结果 3 GiB | 应用与 manager 独立采样，超限优雅轮换 worker |
| 外部恢复 | manager / container runtime | OOM、崩溃、卡死后重新拉起 |
| worker 风暴熔断 | 120 秒内 5 次失败 | 指数退避，第 5 次暂停并要求显式恢复 |
| 任务恢复熔断 | 2 分钟 5 次 boot | 避免恢复任务形成重启风暴 |

大请求闸门的百分比只是开始评估的信号，不是单独的拒绝条件。这点对
Tofu 与其他进程共享的大 cgroup 尤其重要：`99%` 可能仍有 1 GiB 以上绝对余量，
不应拒绝几 MiB 的请求。只有当百分比越线且请求特定峰值包络仍无法容纳时，
闸门才拒绝。编排器随后先不调用摘要模型地裁剪派生上下文，在同一模型上有界重试；
本地内存压力不是模型故障证据，不会触发模型切换或池救援。

可用 `.env` 显式调整：

```dotenv
TOFU_PROCESS_RSS_RELIEF_MB=2048
TOFU_PROCESS_RSS_RECYCLE_MB=2867
TOFU_PROCESS_RSS_COOLDOWN_SEC=300
TOFU_CGROUP_RELIEF_COOLDOWN_SEC=600
TOFU_MAX_INFLIGHT_TASKS=4
TOFU_EXECUTOR_IDLE_SECONDS=300
TOFU_PROJECT_REFRESH_IDLE_SECONDS=30
TOFU_PROJECT_REFRESH_QUEUE_CAPACITY=64
TOFU_PROJECT_UNDO_CACHE_CAPACITY=128
TOFU_INCREMENTAL_TRANSLATE_ACTIVE=8
TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY=16
TOFU_NETPATH_INTERVAL=180
TOFU_NETPATH_MAX_INTERVAL=3600
TOFU_MODEL_CATALOG_STABLE_INTERVAL=43200
TOFU_MODEL_CATALOG_FAILURE_INTERVAL=172800
TOFU_ADMISSION_CGROUP_PCT=90
TOFU_MANAGER_MAX_FAILURES=5
TOFU_MANAGER_FAILURE_WINDOW_SECS=120
TOFU_SQLITE_SNAPSHOT_DIR=/mnt/remote-backup/tofu
TOFU_SQLITE_SNAPSHOT_MAX_AGE_HOURS=26
```

`python serverctl.py doctor` 的 `Probe`/`Budget` 行会显示本次探查输入、实际数据
路径、日志/存储余量和当前零配置预算。启动器把同一次快照物化进 child 环境，避免
主进程与 Sidecar 因瞬时
余量变化选择不同并发。显式 `.env` 值优先于探针，并单独显示在 `Override` 行。

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
        httpGet: {path: /health/startup, port: 15000}
        periodSeconds: 5
        failureThreshold: 36
      readinessProbe:
        httpGet: {path: /health/ready, port: 15000}
        periodSeconds: 10
        failureThreshold: 3
      livenessProbe:
        httpGet: {path: /health/live, port: 15000}
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
- `/health/live` 失败 2 分钟、`/health/ready` 不就绪 30 秒、磁盘可用 <15%、inode <10%；
- 备份超 26 小时未成功、最近一次恢复演练失败立即告警。
- `system.metrics.attempt_events.by_type` 的写入字节增速异常，或任一
  `rejected_events` 增量立即告警；非终态事件 4 MiB 上限被拒绝通常表示完整投影再次
  泄漏进 replay transport，而不是应当调大阈值。

每次排障先保存以下只读证据：

```bash
date -Is
python serverctl.py support-bundle \
  --output "tofu-support-$(date +%Y%m%d-%H%M%S).json"
python serverctl.py status --json
python serverctl.py doctor --json
python serverctl.py logs -n 200
python3 -m lib.log_diagnostics --max-bytes 32768 --pretty
```

日志分流、脱敏、重复故障合并、保留预算和数据库无关诊断的权威契约见
[`LOGGING.md`](LOGGING.md)。不要先把完整 `app.log` / `error.log` 喂给模型；
先用有界诊断报告按 fingerprint/关联 ID 缩小到一段证据。
若已有侧边栏复制出的 conversation ID，执行
`python serverctl.py inspect-conversation <conversation-id>` 一次性读取权威存储投影和
匹配日志；该命令只读，但输出包含对话正文，分享前必须检查，必要时加 `--no-logs`。

支持包会自动包含有界且常见凭据尽力脱敏的 worker、资源压力与 faulthandler 等
诊断尾部，不读取对话存储或数据库；日志可能引用用户文本或未知格式的密钥，分享前
应检查，严格场景使用 `--no-logs`。`doctor` 会显示 cgroup 使用量/上限、swap 上限、内核 OOM 次数、当前 worker RSS、
manager 实际执行的 worker RSS 硬上限、
最近快照年龄和路径。manager 状态还会保留滚动故障数、上次退出的时间相关原因和
实际恢复 RTO。`status --json` 中 `ready` 表示 manager 状态与健康探测一致，
`applicationReachable` 表示通过 PID 身份校验的实际应用端点可达；两者不一致时
`portDrift` 与 `applicationUrl` 直接指出可用端口和生命周期漂移。`status` 在
manager 离线、启动卡住、端口漂移或运行态 conflict/crashloop/degraded 时返回非零；
`doctor` 仅在阻塞性运行故障或内存 ≥90%（即 `lifecycleHealthy=false`）
时返回非零。备份位置、快照新鲜度
等加固建议保留为结构化 warning，不会把仍可用的单机安装误报为宕机。
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

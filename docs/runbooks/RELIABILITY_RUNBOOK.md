# Tofu 极致稳定运行手册

“永不失败”无法由单个进程承诺：`SIGKILL`、宿主机断电、内核故障和存储损坏都
可能绕过应用。生产目标应是：避免可预防故障、提交数据不丢、故障可取证、服务在
有界时间内恢复。推荐验收线是可用性 SLO ≥99.9%、单 worker 故障 RTO <60 秒、
已提交数据库事务 RPO=0、异机备份 RPO ≤24 小时。

## 一、最重要的硬要求

**Executable launch.** Problem: an extracted helper tested its own
`__name__`, silently bypassing manager startup and letting a persisted stop
terminate a human launch. Decision: `server.py` owns the executable guard and
hands normal launches to the stdlib-only `bootstrap_pkg.server_entry` before
any `lib` import. The existing `serverctl.managed_start` automatically starts
or joins the manager and records the new start intent before spawning a
worker. No separate manager command is required. Managed/bootstrap/container
workers and in-place re-exec bypass delegation; importing `server` does not
start a manager. Early resource configuration uses the same caller-owned
guard. Alternatives rejected: clearing manager state in the worker races with
concurrent stop requests; weakening persisted-stop recovery can resurrect a
worker whose shutdown was interrupted. Behavioral subprocess tests exercise
cold launch, launch after stop, option forwarding, and worker/import bypass.

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
   模型执行路由最多保留 64 个排序候选，不能先物化 Deployment×Credential 全积再截断；
   `tests/model_routing/test_model_routing_contract.py` 用 100×200 组合验证排序等价与增量堆峰值 <512 KiB
   （不含已建立的权威文档索引），主模型填满候选额度时不再扫描未使用的降级模型。
4. **状态必须持久化并异机备份。** `data/` 和 `uploads/` 不能放在容器可写层。
   SQLite 的本地验证快照不等于灾备；将 `TOFU_SQLITE_SNAPSHOT_DIR` 指向独立
   挂载/NAS，并把该目录复制到另一故障域，定期做真正的恢复演练。
5. **探针与告警不可缺。** liveness 使用 `/health/live`，启动阶段给足数据库初始化
   时间；外部监控还要检查 `storage.ready`、重启次数、cgroup 使用率、OOM 计数和磁盘空间。

原生引擎重启前置准备由 `serverctl_pkg/native_engine.py` 负责。Problem：
并发准备共用一个 600 秒截止时间，等待锁约 495 秒后，实际编译只剩约 70 秒。
Decision：排队等待最多 600 秒；获得锁后，校验、下载、编译和发布另有最多
600 秒，整个准备最多 1200 秒。CLI 外层等待覆盖两阶段并留 30 秒回收余量；
阶段进度显示各自的剩余预算。所有失败仍在停服前返回，锁、源码身份、嵌入库
校验和发布回滚保持原有边界。Alternatives：仅放宽外层等待不会恢复内层编译
预算；取消锁会让多个准备进程同时改写缓存和发布文件；无界编译会滞留子进程。

## 二、立即启用

源码部署：

```bash
python serverctl.py install-recovery
python serverctl.py status
python serverctl.py doctor
```

`setsid` 只隔离终端会话，不会迁出启动者的 systemd cgroup。若从 IDE 启动，
IDE 的 `KillMode=control-group` 仍能同时终止 watchdog、manager、worker 和存储；
应由独立服务或已安装的 cron 恢复入口启动管理链，并核对各进程
`/proc/<pid>/cgroup` 不再属于 IDE 服务。仅有 watchdog 不代表已有外部恢复，
`install-recovery` 安装的每分钟检查和开机检查仍须存在。

**Manager checkout identity.** Problem: a symlinked terminal working directory
made the shell report a live watchdog as absent and could start a competing
watchdog against the same PID file. Decision: resolve the checkout's physical
path before launch and every lifecycle lookup. Alternatives rejected: deleting
the apparently stale PID file loses the live owner; accepting arbitrary matching
process names weakens checkout ownership. Subprocess tests launch and control
the same checkout through both paths and verify idempotency and teardown.

**Manager signal handling.** Problem: group termination and watchdog forwarding
can deliver SIGTERM twice while the first handler is writing its log, causing
buffered-I/O reentrancy. Decision: record the first signal, request HTTP-loop
shutdown once, and log the receipt after the loop has returned and cleanup has
run. Alternative rejected: logging inside each handler can interrupt the same
writer before shutdown is even requested. The regression injects both signals
and refuses any logging from the signal callback.

对 live worker 执行 `stop` / `restart` 会中断在途任务：交互终端必须确认，非交互
调用必须消费用户在 Tofu UI 中批准的一次性 `shutdown` / `restart` 记录。服务本来
就未运行时，`stop` 保持幂等且不要求批准；`status`、`doctor`、`logs` 均为只读。
应用内 restart 会在停止监听前按 manifest 中的全量构建输入 SHA-256 持锁修复并重新
验证完整 frontend artifact；内容不匹配、损坏或缺少本地 builder 时旧 worker 继续
在线，且不消费一次性批准或 cooldown。浏览器必须在这个同步预检开始前先绘制“准备
重启”，并在同一张进度卡中连续过渡到停机、启动和恢复；仅在该计划内中断作用域内
抑制通用离线告警。明确的 HTTP 拒绝立即恢复正常健康监控且不得进入新 boot 等待，
响应丢失才使用新 `bootId` 作为完成权威，超时则重新探测并恢复通用告警。
应用内 restart 经 Supervisor 延迟替换 worker 时，默认最多等待 15 秒让 `/health/quiescence` 变静；超时仍替换（人类批准已经接受打断）。无人值守延迟重启仍可等到空闲（默认 3600 秒）。冷却期内若接受重启的 PID 仍存活，429 会带 `restartPending`，而不是把“还没换进程”说成已经重启成功。
合法预构建
发布包无需 Node。生产 shutdown 只有在 Sidecar
auto-restart 线程已退出后才签发 storage-boundary release certificate；若有界 join
失败，进程退出并由 manager 等待 OS lease 真正释放后换 PID 恢复，不允许同 PID
re-exec 携带一个可能重新取得 SQLite lease 的后台线程。
manager 的自动 crash recovery 也会在每次 worker spawn 前调用同一个隔离的
`prepare-frontend` 门禁；因此 manager 长期存活期间发生的源码变更不能绕过校验。
源码 manager 还把启动它的 checkout 中 `supervisor.py` 作为生命周期所有权哨兵：
checkout 被删除或原子移走后，HTTP manager 与 watchdog 会在有界轮询内自行退出，
释放监听端口、继承描述符和内存；watchdog 不会对已失去所有权的路径无限自愈。

重启后的恢复边界必须被值班者准确理解，避免把正常墓碑当事故、把真事故当墓碑：
worker 重启时 `recover_stale_tasks_on_startup` 将中断回合置为 interrupted，
同一对话点击继续会从断点恢复；启动恢复同时把本次被中断的对话列表写入
runtime-state store 的 boot 级提示（`lib/tasks_pkg/manager/_pending_resume.py`，
每次启动重写、 dismiss 后清除、7 天 TTL 兜底），前端加载时经
`GET /api/v1/conversations/pending-resume` 读取并弹窗询问是否批量继续，
继续/忽略后通过 `POST …/pending-resume/dismiss` 剪枝或清除——它只是提示，
回合状态的权威仍在 v3 turn 生命周期；swarm 会话与子代理状态持久化于
`swarm_sessions` / `swarm_agents` 表，启动时 `rehydrate_swarms_on_startup`
恢复——已完成未投递的结果重新投递，运行中的子代理从回合检查点续跑（被中断
回合内已执行的副作用工具可能重跑一次，写类工具经审批门拦截）。等待人工回答
的提问卡片只存在于进程内存：中断或重启后旧卡片成为只读墓碑，不是数据丢失；
继续对话会基于完整历史发出新的可交互卡片。取证第一命令是
`python3 debug/inspect_conversation.py <conversation_id>`：只读、自动解析当前
SQLite authority，按服务相同的投影渲染回合轨迹、压缩回执与匹配日志行。
原生 `tofudb` 使用 `--api-url http://127.0.0.1:15000` 读取运行中的应用；
私有部署通过 `--api-token-env NAME` 指定 bearer token 所在环境变量，凭证不能写在
命令行。API 模式沿用服务授权，不接受 `--user-id` 冒充用户，不启动服务、不读
数据库文件或无法证明归属的本地日志，也不将压缩目录窗口声称为完整存储清单。
诊断总预算为 30 秒、128 次读取、32 MiB；单次响应最多 8 MiB，会话最多 10,000 条
消息。分页版本变化或预算耗尽会明确失败，不能将拼接或截断的结果当成完整证据。
逐阶段耗时与用户感知绘制证据由 turn trace 拥有
（`GET /api/v1/tasks/<task_id>/trace`，见 docs/contracts/TURN_TRACE_CONTRACT.md）。

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
[`STORAGE.md`](../STORAGE.md) 的停服窗口执行离线压缩，绝不在服务中手工 `VACUUM`。

日报只从 `data/config/daily_reports/<owner_user_id>/` 读取。缺少 owner
身份的平铺文件不是可信数据源，启动过程不会读取、复制或猜测其归属。

### 自进化阶段交付中断

先读取当前 run 和 `outcomes/<task_id>` 的分页输出，核对 archive 状态、原始
source digest 和消耗完整性。GET 只读，不触发模型或执行。使用现有 run reconcile
或 serving-loop sweep 消费已提交的终态成果；已有完整 chunks 可以补封存，评审
使用原样证据。`stage_outcome_recovery_refused` 需要检查缺失 chunk 或绑定错误，
不要重提目标来假装恢复，也不要把未知写操作自动重放。未完成的 provider 请求
仍显示消耗下界，不能当成零。详见 [STAGE_OUTCOMES.md](../STAGE_OUTCOMES.md)。

## 三、内存防线

| 防线 | 默认行为 | 目的 |
|---|---|---|
| 任务背压 | 个人探针按 CPU、内存容量、启动时可用内存和任务 RSS 包络推导 1..48 个根 Agent 槽；8 CPU/8 GiB 常见结果为 4，64 CPU/64 GiB 且可用 48 GiB 的参考结果为 18，超大服务器在默认 64 GiB worker 硬预算内最多 48 | 尽量利用服务器资源，同时给 OS、浏览器和进程非任务基线留出明确空间 |
| Agent 等待队列 | 有限 FIFO，默认每个执行槽 8 个等待项（总量 8..512）；绑定后仍为 `pending`，工作线程入口才是 `running` | 用户能区分“已受理、等本机 Agent 槽”和“已经执行”，队列饱和显式失败而非无限堆积 |
| 卡死槽恢复 | reaper 判定卡死后隔离旧线程，按独立预算补位；默认每 4 个逻辑槽最多 1 个、总计 1..4 | Python 无法安全杀线程；恢复吞吐但不让僵尸线程无限增加 |
| run_command 自动转后台 | 子进程运行 60 秒后转交，显式与转交命令共用最多 32 个进程内控制项；满额时保留前台，完成项优先回收 | 原执行 worker 继续读取输出，不另起命令；`tracemalloc` 实测 32 个转交控制项及其任务代理共 87,276 字节（约 2,727 字节/项，未含已有执行线程与输出缓冲），完成后释放任务代理；转交命令只在当前服务进程内可控制，显式后台命令另有重启记录 |
| allocator | 个人随数值预算 1..4；容器早期回退 2；分布式 8 | 避免大载荷释放后留下几十个 64 MiB arena |
| Sidecar RPC | 个人探针 2..12；分布式 64 | 与有效存储并发对齐，不把 256 个线程当吞吐 |
| serving-loop 空闲线程 | 个人探针 300..1800 秒；分布式 3600 秒 | 超过热线底线的 sync/agent burst 线程仅在 active=queued=0 时整代退役；容量不变、后续按需重建 |
| subscription 路由探针 | 活跃竞速最多 32；批次结束为 0 | 单路由 singleflight 不变；最后一个在途探针完成后整池退出，下次网络竞速按需重建 |
| tree-index 构建器 | 同时最多 2 个 root；批次结束为 0 | 每个 root 内部 walk 仍受独立 job/时间/条目预算约束；最后一个 root 完成后精确代次退出，下次 stale refresh 按需重建 |
| Project refresh 消费者 | summary/status/watch 各最多 2；个人空闲 30..300 秒后为 0 | owner+project/conv 合并和队列上限不变；submit 与超时退出同锁仲裁，后续事件按需补齐 worker |
| 在线技能验证 | 并发搜索共享最多 4；最后一批结束为 0 | 批次 lease 阻止每请求复制线程池；异常残留在新代发布前有界排空，验证/安装信任边界不变 |
| Codex 模型目录 | 登录/真实变化后 3 分钟；稳定时 6→12→24→48→60 分钟；未登录/分布式为 0 worker | ETag 与归一化行比较识别真实变化；失败保留 last-good 并同样退避，缓存新鲜度上限 60 分钟 |
| 远程 provider 模型目录 | 与本地端点监控共享 1 个 worker；启动 600 秒后首跑，之后固定每 6 小时 | 严格只增：既有行不改不删，用户删除的同步模型记入 per-owner dismissed 台账（`data/config/model_catalog_sync.json`）永不复活；>200 条的聚合目录整体跳过，单 pass 每 provider 至多导入 32 条；`TOFU_MODEL_CATALOG_SYNC=0` 整体退出 |
| 本地端点监控 | health + autodiscovery 共享 1 个 worker；空模型 HTTP 2→4→8→15 分钟 | TCP 拓扑仍每 2 分钟检查，新端口/Settings 变化立即全探测；`TOFU_LOCAL_AUTODISCOVER_MAX_INTERVAL` 最大硬限 3600 秒 |
| Project presence TTL | 空注册表 0 个线程；首个 peer 1 个共享 sweeper；最后 peer 回收后回到 0；有项目任务运行期间另有 1 个 presence-keepalive | 启动不创建空闲 worker；25 秒 active/180 秒 idle TTL 与 owner 隔离不变，精确线程身份阻止旧批次释放新 owner；keepalive 由任务注册表驱动每 10 秒刷新，无项目任务即退休 |
| Swarm 会话清理 | 活跃 session 共享 1 个 300 秒 timer；0 session 时为 0 | import 不启动线程；精确 timer 代次在最后 session 移除时取消，后续 session 按需重建 |
| Billing reserve 回收 | 非 multi-user/计费关闭时 0；启用时每 5 分钟 1 个 durable claim | 单一 Sidecar sweep，跳过仍运行任务，幂等 release；无第二个 request-worker janitor |
| 执行资源对账 | 每个执行最多 16 个显式资源；deadline tick 只请求 owner cancellation | `tofu_execution_sessions_active`、terminal invariant 与 resource release/deferred/failed 指标检测路由、计费、admission 泄漏；不在活跃 provider 下方强拆资源 |
| 日报缺失补偿 | 0 个专用常驻线程；主 scheduler 每 6 小时 1 个 owner-scoped durable claim | 启动仅在昨日缺失时排入有界 hint；maintenance 子线程不阻塞主 tick，Sidecar claim 防重复 |
| 直连/代理路径探测 | 最多 64 个主机；新路径/真实失败立即探测；默认失败退避 3→6→12→24→48→60 分钟，稳定上限 60 分钟，闲置 24 小时停止 | 真实请求成功会延后同路径的合成 GET；持久化保留真实 last-seen，重启不会把废弃主机重新激活 |
| 日志后台维护 | 安静态 2 个周期 worker、默认至多 5 次唤醒/小时；重复尾部 worker 为 0 | 核心/外部保留共享 15 分钟线程；聚合空闲仅按小时 TTL 唤醒、有积压仍 15 秒批刷；首个被压制 delta 才创建短生命周期尾部 worker |
| Git integration 队列 | 本地 submit/retry 立即唤醒；空队列探测 3→6→12→24→48→60 秒 | 每日空读 28,800→≤1,440（降低 95%）；跨进程 ready/崩溃 claim 检测仍 ≤60 秒；存储故障独立 5..30 秒退避且停机精确 join |
| 会话目录到达合并 | 与 Sidecar RPC 容量相同，硬上限 256；8 ms 到达窗 | 仅合并同 owner/同投影且尚未开始的读取；开始即摘除，完成结果不缓存，饱和时直读 |
| Project refresh 队列 | 个人探针 16..128；分布式 512（status/watch/summary 各一条） | 唯一 scope 有界，重复 scope 合并；过载只延迟可重建的 warm refresh |
| 增量翻译 | 活跃 accumulator 个人 2..16、分布式 32；每任务 preview 队列个人 8..32、分布式 64 | 限制线程和文本积压；终态 handoff 永不丢弃 |
| 共享翻译通道 | worker 个人 1..2、分布式 16；等待队列个人 4..32、分布式 128；空闲 60/600 秒退线程 | 发送前、显式文本、整轮、PPTX、论文共享按 owner 轮转的硬界队列；完整 Turn 译文先提交并终结任务，15 秒上限的可重建分段富化再排到同一 owner 队尾，饱和可丢弃且不新建线程；前台发送仅在自身 owner 内插队；同一 worker 值还限制实际 MT/LLM 并发；其他饱和后台返回可重试 `server_busy`/HTTP 503，发送路径降级为带 `server_busy` 原因的原文 |
| 翻译限流调用预算 | 每次翻译个人 4..8、分布式 16，显式硬上限 64 个真实上游限流响应 | 等待槽位不计数；预算耗尽终止整项可选翻译且禁止 direct fallback；交互式编码/研究调度默认仍可等待恢复 |
| 存储启动余量 | `data/` 所在卷容量的 1%，限制在 256 MiB..2 GiB | 不让 SQLite/WAL 吃掉文件系统最后空间 |
| cgroup admission | 源码 96%；Compose 90% | 高压时拒绝新任务 |
| process RSS admission | 新任务必须还能在 worker 硬上限内容纳一个 `TOFU_TASK_RSS_RESERVE_MB` 包络 | 即使共享 cgroup 尚有数百 GiB，也不再把已经接近自身上限的 worker 推过回收线 |
| 大请求闸门 | process RSS 每轮检查；共享 cgroup 在源码 95% / Compose 90% 时检查，且绝对余量低于 `max(64 MiB, body×8)` 才拒绝 | 先在软 RSS 线触发派生上下文裁剪，同时避免共享 cgroup 百分比假阳性 |
| aggregate relief | 源码 92%；Compose 85% | 清缓存、丢日志页缓存、`malloc_trim` |
| aggregate relief 冷却 | 连续 5 次无有效回收后默认 600→1200→2400→3600 秒封顶；压力跌回触发线下或有效回收后复位 | 共享 cgroup/FUSE slab 持续压力下减少无效热缓存清空，日志、OOM 检查与进程 RSS 防线继续 |
| 进程软 RSS | 个人按有效容量/当前余量推导；8 GiB 常见结果 2 GiB | 在 worker 变胖时主动回收 |
| 进程硬 RSS | 个人按有效容量/当前余量推导；8 GiB 常见结果 3 GiB | 应用与 manager 独立采样，超限优雅轮换 worker |
| 外部恢复 | manager / container runtime | OOM、崩溃、卡死后重新拉起 |
| worker 风暴熔断 | 120 秒内 5 次失败 | 指数退避，第 5 次暂停并要求显式恢复 |
| 任务恢复熔断 | 2 分钟 5 次 boot | 避免恢复任务形成重启风暴 |

`tofu_cgroup_relief_reclaimed_bytes_{latest,total}` 的 `process_rss` 来源可直接
归属于 Tofu；`shared_cgroup`、`cgroup_cache`、`heap_window` 与 `log_window` 是
动作边界前后的共享计数器观测，会混入兄弟进程并发变化，不能相加或解释为因果回收。
结合 `tofu_cgroup_relief_duration_seconds` 判断一次 relief 的收益/时延，再调整策略。

取消是资源释放边界，不只是 UI 状态。已在 Agent FIFO 中等待的任务应直接撤销队列项；
已经取得 worker、但在启动准备期间收到取消的任务，会在项目状态、工具装配、上下文
拼装等每个重阶段边界重新检查信号。入口已取消时不得启动项目扫描、外部编辑探针、
MCP 装配、内存选择或上下文预取；阶段中取消时，预取池在终态路径上取消尚未开始的
future 并释放任务引用。排障时若看到 `aborted_during_prep_<stage>`，该任务不应再有
后续 LLM 请求；若仍有，则是取消门禁回归。

共享 cgroup 的百分比只是开始评估的信号，不是单独的拒绝条件。这点对
Tofu 与其他进程共享的大 cgroup 尤其重要：`99%` 可能仍有 1 GiB 以上绝对余量，
不应拒绝几 MiB 的请求。只有当百分比越线且请求特定峰值包络仍无法容纳时，
该分支才拒绝。worker 自身的软 RSS 包络是另一条独立边界，不要求共享 cgroup 先高压；
否则单进程会先撞到按本机容量推导的 worker RSS 回收线。编排器随后先不调用摘要模型
地裁剪派生上下文，在同一模型上有界重试；
本地内存压力不是模型故障证据，不会触发模型切换或池救援。

准入与请求包络判定使用 reclaim-aware 口径（`effective_pct` = 用量减去
`memory.stat` 的立即可回收 page cache）。2026-09-18 事故：共享 cgroup 原始用量
97% 中约 120 GiB 是其他租户的可回收缓存（tofu 自身 RSS <1 GiB），按原始口径
拒绝一切新任务等于自我 DoS——同机 node/opencode/qemu 均无此自限。`memory.stat`
不可读时退回原始口径（保守）。cgroup 准入闸可用 `TOFU_ADMISSION_CGROUP_PCT=0`
整体关闭（保留更真实的进程自身 RSS 闸 `TOFU_ADMISSION_PROCESS_RSS`）。

数据目录在网络盘（FUSE/NFS）时，存储 RPC 的 5 秒默认 deadline 会被 fsync 风暴
打穿：任何请求（包括纯读）都在单一 authority 写者后排队超时，全线 503。
用 `TOFU_STORAGE_RPC_DEADLINE_S`（1..120）为该部署抬高下限；它不是掩盖死库的
手段，只是适应挂载延迟。`tofu-db` 在 strict 模式把每次命令响应绑定一次挂载
fdatasync；`TOFU_DB_DURABILITY=normal`（可选 `TOFU_DB_WAL_FLUSH_INTERVAL_MS`，
1..1000）是引擎契约内的显式有界 RPO 选择：ack 只等内存提交，后台 flusher 负责
fdatasync，掉电丢失窗口 ≤ flush 间隔，receipt 按 lost-ACK 幂等重放解决。
该选择必须显式做出，永远不是静默默认。

可用 `.env` 显式调整：

```dotenv
TOFU_PROCESS_RSS_RELIEF_MB=2048
TOFU_PROCESS_RSS_RECYCLE_MB=2867
TOFU_PROCESS_RSS_COOLDOWN_SEC=300
TOFU_TASK_RSS_RESERVE_MB=512
TOFU_CGROUP_RELIEF_COOLDOWN_SEC=600
TOFU_MAX_INFLIGHT_TASKS=4
TOFU_EXECUTOR_IDLE_SECONDS=300
TOFU_PROJECT_REFRESH_IDLE_SECONDS=30
TOFU_PROJECT_REFRESH_QUEUE_CAPACITY=64
TOFU_PROJECT_UNDO_CACHE_CAPACITY=128
TOFU_INCREMENTAL_TRANSLATE_ACTIVE=8
TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY=16
TOFU_INCREMENTAL_TRANSLATE_PREVIEW_SEGMENTS=32
TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS=30
TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MIN_CHARS=256
TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MAX_429_ATTEMPTS=1
FETCH_FILTER_MIN_CHARS=6000
FETCH_FILTER_GATE_MAX_CHARS=6000
TOFU_TRANSLATE_CACHE_MAX_MIB=256
TOFU_TRANSLATE_MAX_429_ATTEMPTS=8
TOFU_TRANSLATE_WORKERS=2
TOFU_TRANSLATE_QUEUE_CAPACITY=16
TOFU_TRANSLATE_WORKER_IDLE_SECONDS=60
TOFU_NETPATH_INTERVAL=180
TOFU_NETPATH_MAX_INTERVAL=3600
TOFU_MODEL_CATALOG_STABLE_INTERVAL=43200
TOFU_MODEL_CATALOG_FAILURE_INTERVAL=172800
TOFU_ADMISSION_CGROUP_PCT=90
TOFU_STORAGE_RPC_DEADLINE_S=20
TOFU_DB_DURABILITY=strict
TOFU_MANAGER_MAX_FAILURES=5
TOFU_MANAGER_FAILURE_WINDOW_SECS=120
TOFU_SQLITE_SNAPSHOT_DIR=/mnt/remote-backup/tofu
TOFU_SQLITE_SNAPSHOT_MAX_AGE_HOURS=26
```

`python serverctl.py doctor` 的 `Probe`/`Budget` 行会显示本次探查输入、实际数据
路径、日志/存储余量和当前零配置预算。启动器把同一次快照物化进 child 环境，避免
主进程与 Sidecar 因瞬时
余量变化选择不同并发。显式 `.env` 值优先于探针，并单独显示在 `Override` 行。
个人模式的 `TOFU_MAX_INFLIGHT_TASKS` 仍受 worker RSS 硬边界约束；要提高并发，必须
同时用测量结果提高 `TOFU_PROCESS_RSS_RECYCLE_MB` 或降低已证实的单任务包络，不能只把
任务数从 4 改成 8。

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
python3 -m lib.logging_pkg.log_diagnostics --max-bytes 32768 --pretty
```

日志分流、脱敏、重复故障合并、保留预算和数据库无关诊断的权威契约见
[`LOGGING.md`](../LOGGING.md)。不要先把完整 `app.log` / `error.log` 喂给模型；
先用有界诊断报告按 fingerprint/关联 ID 缩小到一段证据。
若已有侧边栏复制出的 conversation ID，执行
`python serverctl.py inspect-conversation <conversation-id>` 一次性读取权威存储投影和
压缩归档回执及匹配日志。命令会自动识别当前 Sidecar/fastpath 写前端，不会把遗留的
`data/tofu.db` 当成当前状态；无法唯一证明权威路径时会拒绝并给出下一步。该命令只读，
但输出包含对话正文，分享前必须检查，必要时加 `--no-logs`。

支持包会自动包含有界且常见凭据尽力脱敏的 worker、资源压力与 faulthandler 等
诊断尾部，不读取对话存储或数据库；日志可能引用用户文本或未知格式的密钥，分享前
应检查，严格场景使用 `--no-logs`。`doctor` 会显示 cgroup 使用量/上限、swap 上限、内核 OOM 次数、当前 worker RSS、
manager 实际执行的 worker RSS 硬上限、Supervisor 已加载/磁盘期望的源码指纹，以及
worker 当前/下一次启动的任务并发与 RSS 预算。`supervisor_source_stale` 只需执行
`serverctl.py ensure`，不会停止 worker；`worker_resource_budget_stale` 必须等任务空闲后
执行一次获批的 managed restart，禁止为消除 warning 强杀仍在运行的对话。
最近快照年龄和路径。manager 状态将 worker exit 分成 `workerFailureCount` 与
`plannedExitCount`；只有前者进入滚动故障窗口。`restartCount` 是升级前后兼容的历史
混合计数，不能再当 crash 数。clean marker 必须同时匹配 PID、host、worker generation
时间戳且只能消费一次；手动关机把 durable desired state 改成 stopped，显式 restart
证书或 manager 在发信号前持久化的 PID 意图会立即恢复。没有该意图的普通 SIGTERM
以及 memory recycle 都进入故障预算，防止外部信号或泄漏形成无限拉起。状态还会保留实际恢复 RTO。
`status --json` 中 `ready` 表示 manager 状态与健康探测一致，
`applicationReachable` 表示通过 PID 身份校验的实际应用端点可达；两者不一致时
`portDrift` 与 `applicationUrl` 直接指出可用端口和生命周期漂移。`status` 在
manager 离线、启动卡住、端口漂移或运行态 conflict/crashloop/degraded 时返回非零；
`doctor` 仅在阻塞性运行故障或内存 ≥90%（即 `lifecycleHealthy=false`）
时返回非零。备份位置、快照新鲜度
等加固建议保留为结构化 warning，不会把仍可用的单机安装误报为宕机。
`oom_kill` 增长是 memcg OOM 的硬证据；只有 “Killed” 而无 traceback 通常是
`SIGKILL`，不要误当 Python 异常处理。

监听丢失由 `server_fault_watchdog.py` 的连接观察与 `server_loop_watchdog.py` 的
连续五次丢失判定共同负责。问题：本地临时端口、FD 或 socket 内存耗尽会让探针
尚未发出便失败。决策：这类观察保持未知，不建立首次绑定事实，并中断丢失计数；
真实拒绝连接或连接超时仍记录不可用，loop heartbeat 与线程栈诊断独立运行。
不采用“任意 socket 错误都等于监听死亡”或“未知就是健康”的替代判断。

**存活期间恢复。** Problem：FUSE、网络或存储阻塞时，心跳过期和探针超时不能
证明后台线程与在途写入已经停止；强杀会丢失进程内任务状态，再启动仍依赖同一故障
源。Decision：manager 对存活且身份匹配的 worker 持续报告 degraded、保留 PID、
任务所有权和故障预算，定期探测；依赖恢复后同一 PID 自动回到 running。LoopWatch
监听失败只记录一次连续故障，继续心跳与探测，真正恢复后重新允许下一次告警。
启动锁不得因心跳过期被 unlink：活 owner 释放锁或退出之前拒绝第二实例。
独立 Supervisor 保留控制与诊断入口；应用循环完全卡住时应用 HTTP 本身仍可能
无法响应，不伪报健康。现有依赖重试仍按各自截止时间和所有权运行，不重放已完成
工具或切换存储。实际进程退出、显式获批 stop/restart 与既有 RSS 硬边界各自保留。
Alternatives：延长强杀阈值仍会中断可恢复任务；抢锁另起实例会产生双 owner；
将探针失败伪装成健康会掩盖不可用。本策略保存恢复机会，不承诺修复永久内核阻塞。

## 六、发布与故障演练门禁

每次发布按顺序执行：

```bash
python -m pytest -q tests/cgroup_guard/test_cgroup_guard.py tests/server/test_server_manager.py \
  tests/storage/test_storage_runtime_shutdown.py tests/lifecycle_approval/test_update_restart_guard.py \
  tests/tasks_pkg/test_shutdown_quiesce.py tests/server/test_shutdown_hard_deadline.py
python healthcheck.py
python serverctl.py doctor
```

每月至少在预发布环境演练一次：强制杀 worker 后确认 manager 在 60 秒内换 PID；
制造 85%/90% 内存压力确认先背压再恢复；中断数据库/网络确认错误可见且不会无限
重试；从异机备份恢复到空环境并校验 SQLite `integrity_check`。故障注入不要在有真实
进行中任务的生产实例上执行。

## Request route residency

Computed request routes are owned execution state and have no local slot quota.
The registry publishes each owned handle atomically and never evicts a live
request to make space. Requests retain the complete eligible primary/fallback
set by default; a caller may explicitly select a positive shortlist. Planned
route witnesses match the full primary set before secret resolution and keep
exact route identity. Capability queries and composer controls retain all
eligible groups. The resource controller observes registry counts; it neither
estimates a route ceiling nor resizes admission. TOFU_EPHEMERAL_SLOT_CAPACITY is
retired, and no second resource probe or eager pool allocation is added.

Group construction rolls back on cancellation and ordinary failure; audit
failures cannot orphan handles or interrupt release. Disposal tries every sibling
and remains retryable after a release failure. Execution settlement/discard owns
release; TTL never evicts productive routes. Registration completeness, owner,
capability, egress, credential and exact planned-witness checks remain mandatory.
Specifications: tests/llm_dispatch/test_ephemeral_capacity.py and the model-routing
capability/control contracts; slot materialization preserves provider isolation.

## Restart source validation latency

Problem: lifecycle preparation read more than a thousand frontend inputs
serially, re-statted files already classified by directory enumeration, and
the application route repeated the successful preparation owner's complete
source validation. This magnifies metadata/read latency on FUSE mounts.
Decision: enumerate with DirEntry metadata without following directory links,
read small source files concurrently with ordered SHA-256 framing, and use
one complete source validation at the application preparation boundary.
Worker lanes derive from the cached launch CPU/headroom observation; each lane
reserves 64 MiB of headroom and at most two 1 MiB-prefetch results are pending.
Large inputs retain streamed hashing; the temporary executor joins on every
exit, including a read failure. No persistent cache or thread is introduced.
Manager pre-stop and pre-spawn checks remain separate freshness boundaries.
Alternatives rejected: mtime-only caches miss equal-size/preserved-mtime edits;
dropping source freshness or post-repair validation permits stale bundles;
skipping the manager spawn gate could admit changes made during shutdown.

Measurement (2026-10-05, same 1,070 live-checkout inputs, three warm reads):
source hash median 0.464 s → 0.141 s, identical content digest; file stat calls
3,210 → 40 across the three reads. Application preparation also removes one
whole duplicate validation. The observed 63-core host used about 9.5 MiB
of additional peak RSS for the temporary read pool. Historical production
spans separately showed
22.878 s in-request preparation, 30.8 s storage shutdown, and 39.7 s storage
startup. These spans are separate costs; the source-hash measurement is not
an end-to-end restart claim. A disposable empty native authority opened in
0.004 s and closed in 1.265 s, demonstrating that the large authority's
shutdown cannot be explained by an unconditional 30-second timer.

# PG 本地盘播种迁移 Runbook（legacy FUSE pgdata → /tmp/tofu/pgdata）

> 触发背景（2026-08-01 error.log 审计）：PG 数据目录 21GB 跑在 DolphinFS FUSE
> 上（`data/pgdata`），是当日 118 次慢 DELETE（2.5–3.6s）、171 条慢查询、
> 2 次 `PG appears dead: timeout expired`、`GET /api/v1/timer/list` 500 的
> 共同温床。本地盘主备分离已 ENGAGED 但种子从未播种——每次启动
> `logs/error.log` 都会念 `[db_paths] Split engaged but local pgdata=… not
> yet populated`。本 runbook 是一次性播种的唯一执行路径。
>
> 机制源码：`lib/database/_pg_seed.py::_seed_local_pgdata_from_legacy`
> （opt-in `TOFU_DB_SEED_LOCAL=1`，幂等，verify-before-canonical，失败自动
> quarantine 本地半成品、legacy 保持权威）。

## 0. 前置事实（2026-08-01 实测）

| 项 | 值 | 出处 |
|---|---|---|
| legacy pgdata | `data/pgdata`（FUSE），21 GB | `du -sh` |
| 目标 local pgdata | `/tmp/tofu/pgdata`（本地 xfs） | `db_paths.resolve_pgdata_dir` |
| /tmp 可用空间 | 5.8 TB（需 ~21 GB，充足） | `df -h /tmp` |
| conversations 行数（验收基线） | 4394 | 实测 `SELECT count(*)` |
| 种子 dump 暂存 | `data/pg_backup.sql`（FUSE，数 GB） | `_pg_seed.py` `staged` |
| dump 超时 | 1800s（`TOFU_DB_SEED_DUMP_TIMEOUT` 可调） | `_pg_seed.py:162` |

## 1. 预检（全部必须通过）

```bash
# ① /tmp 空间 ≥ 2× pgdata
df -h /tmp
# ② 目标目录为空（播种幂等：已有 PG_VERSION 会跳过整个迁移）
ls /tmp/tofu/pgdata/PG_VERSION 2>/dev/null && echo '已播种过，无需再跑'
# ③ legacy 权威行数（记下，作为验收基线）
PGGSSENCMODE=disable psql -h 127.0.0.1 -p 15439 -U "$USER" -d tofu -tAc \
  'SELECT count(*) FROM conversations'
# ④ 无大批量写入在途（播种期间服务器会停，但先确认没有长跑任务）
curl -s http://127.0.0.1:15000/api/health
```

## 2. 执行（一条命令；必须走 shell 重启）

```bash
TOFU_DB_SEED_LOCAL=1 ./restart_15000.sh
```

- **必须 shell 重启**：UI 重启按钮走 `os.execv`，继承旧进程环境，注不进
  `TOFU_DB_SEED_LOCAL`（2026-08-01 egress 实测教训）。
- 变量经调用 shell 环境传入脚本，子进程 python 自然继承（restart_15000.sh:396
  的 env-prefix 只追加 PORT/BIND_HOST，不屏蔽其余环境）。
- 启动序列：停止旧服务 → DB bootstrap Step -1 触发种子 → legacy 在运行则直接
  复用做 `pg_dumpall`（fresh dump，**一致快照、零丢失窗口**；起不来才回退
  nightly dump 并 WARN）→ initdb+restore 进 `/tmp/tofu/pgdata` → 启动本地
  集群并校验 `conversations` 行数与源一致 → 通过才声明 local 为权威。
- 时长预估：dump+restore 21 GB，分钟级（FUSE 读 + 本地写）。期间服务不可用。

## 3. 验收（三条全过才算成）

```bash
# ① 启动日志出现成功标记（行数必须等于预检基线）
grep -a 'DB-Seed] SUCCESS' logs/app.log | tail -1
grep -a 'Local-primary split ENGAGED' logs/app.log | tail -1
# ② 活库的数据目录已切到本地
PGGSSENCMODE=disable psql -h 127.0.0.1 -p 15439 -U "$USER" -d tofu -tAc \
  'SHOW data_directory'   # 期望 /tmp/tofu/pgdata
# ③ 行数一致 + 业务冒烟
PGGSSENCMODE=disable psql -h 127.0.0.1 -p 15439 -U "$USER" -d tofu -tAc \
  'SELECT count(*) FROM conversations'   # == 预检基线（2026-08-01: 4394）
curl -s http://127.0.0.1:15000/api/health
```

## 4. 失败/回退

| 情形 | 自动行为 | 操作 |
|---|---|---|
| dump 失败 / restore 失败 / 校验行数不符 | 半成品 `/tmp/tofu/pgdata` 被 **quarantine**（改名隔离，永不过 gate），legacy 保持权威，CRITICAL 日志 | 直接 `TOFU_DB_SEED_LOCAL=1 ./restart_15000.sh` 重跑（幂等）；查 error.log `[DB-Seed]` 段定位 |
| 播种成功后才发现异常 | local 已是权威 | 停服 → `mv /tmp/tofu/pgdata /tmp/tofu/pgdata.bad-$(date +%s)` → 正常重启（gate 检测 local 未播种 + legacy 可恢复 ⇒ 自动回落 legacy） |

- **legacy `data/pgdata` 不要删**：种子成功日志也明说 PRESERVED。稳定运行
  数日 + owner 签字后才谈退役（届时本地主 + FUSE 备份的双层格局已生效）。
- 后续重启**不需要**再带 `TOFU_DB_SEED_LOCAL`（幂等：local 已播种即跳过）；
  带着也无害。

## 5. 播种后的世界

- 活库读写全部走本地 xfs → 慢查询族（DELETE task_events 2.5–3.6s）、
  `PG appears dead` 超时、FUSE 抖动引起的 LoopWatch 假停摆应基本消失。
- FUSE 角色变为纯备份目标（`resolve_backup_root` → `data/pg_backups`），
  正是 db_paths 设计的「local-disk-primary + FUSE-as-replication-target」。

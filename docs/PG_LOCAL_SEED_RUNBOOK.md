# PG 本地盘播种迁移 Runbook（legacy FUSE pgdata → /tmp/tofu/pgdata）
# PG 本地盘播种迁移 Runbook（legacy FUSE pgdata → /tmp/tofu/pgdata）

> **⛔ 已撤回 2026-08-06（owner 终裁，epic pt_4d321fb8f1c2400c 永久关票）：**
> 「不要使用除了项目以外的路径来解决这个问题，/tmp这些路径不准用来部署db，会丢的。
> 以后都不许想这个。」——**本 runbook 的每一步都禁止执行**。DB 永远留在项目目录
> （legacy FUSE pgdata）。代码机制保留但惰性化（opt-in、默认关，见
> `lib/database/_pg_seed.py`）。本文仅作为「探索过并被否决」的存档。


> 触发背景（2026-08-01 error.log 审计）：PG 数据目录 21GB 跑在 DolphinFS FUSE
> 上（`data/pgdata`），是当日 118 次慢 DELETE（2.5–3.6s）、171 条慢查询、
> 2 次 `PG appears dead: timeout expired`、`GET /api/v1/timer/list` 500 的
> 共同温床。
>
> **历史说明（已被 2026-08-06 终裁覆盖）：** 2026-08-05 曾短暂设计为
> DEFAULT-ON；当前代码已经撤回为默认关闭，普通 `python server.py` 绝不会
> 把数据库部署到 `/tmp`。本文以下命令仅为事故/设计档案，不得执行。
>
> 机制源码：`lib/database/_pg_seed.py::_migrate_local_primary_if_due`
> （幂等，verify-before-canonical，失败自动 quarantine 本地半成品、legacy
> 保持权威；失败进 6h 冷却标记，过窗自动重试——自愈）。**2026-08-05 起为
> 单启动原子迁移：播种+校验+翻转同一次启动完成**，只有 server 进程
> （`TOFU_SERVER_PROCESS` 标记）会触发；探针/工具进程只挂载不迁移。

## 0. 前置事实（2026-08-01 实测）

| 项 | 值 | 出处 |
|---|---|---|
| legacy pgdata | `data/pgdata`（FUSE），21 GB | `du -sh` |
| 目标 local pgdata | `/tmp/tofu/pgdata`（本地 xfs） | `db_paths.resolve_pgdata_dir` |
| /tmp 可用空间 | 5.8 TB（需 ~21 GB，充足） | `df -h /tmp` |
| conversations 行数（验收基线） | 4394 | 实测 `SELECT count(*)` |
| 种子 dump 暂存 | `data/pg_backup.sql`（FUSE，数 GB） | `_pg_seed.py` `staged` |
| dump 超时 | 1800s（`TOFU_DB_SEED_DUMP_TIMEOUT` 可调） | `_pg_seed.py` |

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

## 2. 历史执行过程（已撤回，禁止执行）

```bash
# 档案：旧方案需要显式设置 TOFU_DB_SEED_LOCAL=1。
# 当前不得运行；普通 python server.py 保持项目目录内的数据库不变。
```

- 启动序列（单启动原子）：DB bootstrap Step -1 触发迁移 → legacy 在运行则直接
  复用做 `pg_dumpall`（fresh dump，**一致快照、零丢失窗口**）→ initdb+restore
  进 `/tmp/tofu/pgdata` → 校验 `conversations` 行数与源一致 → **同一启动内**停
  legacy、把 local 切到钉死端口（15439）启动 → 本次启动即伺服 local。
- **本次启动会多花 10–20 分钟**（dump+restore ~46GB，FUSE 读 + 本地写），日志
  持续输出 `[DB-Seed]`/`[DB-Flip]` 进度——这是一次性成本，不是卡死。
- 失败语义：任一步失败自动 quarantine 半成品 + legacy 保持权威 + 写 6h 冷却
  标记（`/tmp/tofu/.seed_failed`）；修好根因后删标记或等过窗，下次启动自动
  重试。

## 3. 验收（三条全过才算成——同一次启动后即可查）

```bash
# ① 播种+翻转双成功日志（行数必须等于预检基线）
grep -a 'DB-Seed] SUCCESS' logs/app.log | tail -1
grep -a 'DB-Flip] SUCCESS' logs/app.log | tail -1
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
| dump 失败 / restore 失败 / 校验行数不符 | 半成品 `/tmp/tofu/pgdata` 被 **quarantine**（改名隔离，永不过 gate），legacy 保持权威，CRITICAL 日志 | 什么都不用做——下次启动**自动重试**（自愈设计）；先查 error.log `[DB-Seed]` 段排掉根因（如 /tmp 满） |
| 保持当前受支持行为 | — | 普通 `python server.py`（播种默认关闭） |
| 播种成功后才发现异常 | local 已是权威 | 停服 → `mv /tmp/tofu/pgdata /tmp/tofu/pgdata.bad-$(date +%s)` → `TOFU_DB_LOCAL_SPLIT=0` 重启（split 关闭时播种不触发，解析直接回 legacy） |

- **legacy `data/pgdata` 不要删**：种子成功日志也明说 PRESERVED。稳定运行
  数日 + owner 签字后才谈退役（届时本地主 + FUSE 备份的双层格局已生效）。
- 已知边缘（预先声明，非本批范围）：若 local 播种**成功之后** /tmp 被清空
  （节点重建等），自动播种会从冻结的 legacy 再播一份——数据回到播种时点，
  会有 CRITICAL/WARN 日志但属「陈旧复活」。这与现行的 legacy 回退行为同级，
  真要修需要 FUSE 侧的持久标记，另行立案。

## 5. 播种后的世界

- 活库读写全部走本地 xfs → 慢查询族（DELETE task_events 2.5–3.6s）、
  `PG appears dead` 超时、FUSE 抖动引起的 LoopWatch 假停摆应基本消失。
- FUSE 角色变为纯备份目标（`resolve_backup_root` → `data/pg_backups`），
  正是 db_paths 设计的「local-disk-primary + FUSE-as-replication-target」。

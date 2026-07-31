# Messages 行存储写路径翻转 — 决策就绪证据包

> 用途：`TOFU_MESSAGES_ROWS` 写路径翻转的优先级与范围裁决依据。
> 全部数字为 2026-07-27 在生产库实测（非抽样推断的地方均已注明样本量）。
> 关联：board `pt_341af8819c1848c1`（本证据的载体 epic）、
> JOURNAL 2026-07-27「硬刷新雪崩根修」条目、charter 行存储两条决策。

---

## 1. 为什么是现在 — 事故实测（2026-07-27 08:43 硬刷新雪崩）

单次硬刷新（约 100 秒窗口）产生的整 blob 重写：

| 指标 | 实测值 |
|---|---|
| `INSERT INTO conversations`（含整个 messages blob） | **25 条**，blob 单体 40–94 MB |
| `PATCH /messages/by-id` 整 blob 重写 | **34 条** |
| 慢查询（阈值 2s） | **44 条**，p50=3.6s / p90=8.3s / max=11.2s（平时全天 ~4 条） |
| 事件循环 | STALLED 8.8s（149 线程同处 `strip_null_bytes_deep`） |

根修 A（`json_dumps_pg` 快速路径，`c8587db5`）已消除 Python 侧 sanitize CPU，
但 **PG 执行侧的 2–11s 慢查询来自「改一条消息要重写整个 40–94MB blob」这一写入形状本身** —— 
这正是行存储写路径要消灭的东西：写路径翻转后，追加一条消息 = 写一行。

## 2. 行存储现状（2026-07-27 实测，对比 charter 2026-07-26 基线）

| 指标 | 基线(07-26) | 今天(07-27) | 结论 |
|---|---|---|---|
| 行存储会话数 / 行数 | 3,696 / 26,950 | **3,696 / 26,950** | **逐字节冻结** —— `TOFU_MESSAGES_ROWS` 关闭，无 dual-write，backfill 零推进 |
| 完整覆盖（rows ≥ blob msg_count） | — | **3,689** | 这些可以立刻过 parity |
| **部分覆盖（0 < rows < msg_count）** | — | **484** | charter 定义的「真正的杀手」形状，实存 |
| 空覆盖（rows=0, msg_count>0） | 464 | **477** | 与「空会话」不可区分 |
| Top-10 最大 blob 会话的行覆盖 | — | **9 个为 0 行** | 重写大户恰好全未覆盖 |

## 3. Parity 闸实测（`verify_conv_parity`，2026-07-27 只读运行）

| 样本 | 结果 |
|---|---|
| 完整覆盖中 msg_count 最大的 12 个（含 1163 条消息的 `mqyv664xjp3085`） | **12/12 OK**，search_text 逐字节一致（1.28MB 文本零差异） |
| 部分覆盖 3 个 | 3/3 MISMATCH（rows 126/120、122/114、36/29 —— 预期失败，由 `row_window_usable` 失败关闭兜住） |

**结论：行表示法本身已在真实数据上证明无损；唯一阻塞是覆盖率。**

## 4. 写路径翻转的真实结构缺口（迁移实施地图）

### 4.1 dual-write 挂钩覆盖：2 / ~19

当前仅 2 个写点挂了 `dual_write_conv`：

- `routes/conversations.py:1717`（sync PUT —— 硬刷新爆发的来源）
- `lib/chat/persistence.py:236`

**未挂钩的整 blob 写点（翻旗即行存储立刻变陈旧）：**

| 文件:行 | 写入者 |
|---|---|
| `lib/tasks_pkg/manager/_sync.py:345,352,994,1001,1532` | 任务管理器 sync（5 处） |
| `lib/tasks_pkg/persistence_store.py:84,100,131,289` | 任务持久化（4 处） |
| `lib/message_queue.py:1040,1069` | 消息队列（2 处） |
| `lib/translate/commit.py:256` | 翻译提交 |
| `lib/translate/segment_backfill.py:573` | 翻译段回填 |
| `lib/tasks_pkg/auto_translate/_assistant.py:194` | 自动翻译 |
| `lib/tasks_pkg/autopilot_baton.py:194` | autopilot 接力 |
| `lib/tasks_pkg/killed_recovery.py:436` | 崩溃恢复 |
| `lib/swarm/snapshot.py:245` / `lib/swarm/integration/_autocontinue.py:181` | swarm（2 处） |
| `lib/scheduler/_shared.py:459` | 调度器 |
| `routes/conversations.py:528` | sync PUT UPDATE 分支 |

### 4.2 `dual_write_conv` 是全量重建，不是增量

`backfill_conv` = `DELETE WHERE conv_id` + 逐条 upsert（`messages_rows.py:164`）。
对 1163 条消息的会话，**每追加一条消息 = 1163 次行 upsert** —— 比 blob 重写更甚。
写路径在高频写会话上翻旗前，dual-write 必须有**增量形态**（追加只插新 seq、编辑只动受影响 seq）。

### 4.3 backfill 无 fleet 执行器

`backfill_conv` 单会话幂等原语已存在，但没有全库批跑脚本（限流/断点续跑/进度）。
**优先补 top blob 大户** —— 重写成本最高的恰好是当前 0 覆盖的那 9 个（见 §2）。

### 4.4 闸与护栏（已就绪，不需重做）

- `verify_conv_parity`（`messages_rows.py:300`）—— 单会话判定已实测通过；
- `row_window_usable`（`lib/conv_ref/_detail.py`，charter 2026-07-26 决策）—— 行数 < blob 数即失败关闭回 blob，部分 backfill 不会漏数据；
- 读路径全程 OFF 且惰性 —— 写路径迁移期间读侧零风险。

## 5. 裁决建议（给 owner / 迁移实施者）

1. **迁移值得做且应尽快** —— §1 的写入形状是硬刷新雪崩的 PG 侧根因，A 修不了它。
2. **范围 = 四步，顺序不可换：** ① dual-write 增量形态改造；② 挂钩扇出到 §4.1 全部写点；
   ③ fleet backfill（top blob 优先）+ 全库 `verify_conv_parity`；④ owner 确认后翻 `TOFU_MESSAGES_ROWS`。
3. **PG 迁本地盘是独立运维迁移**（owner 2026-07-27 裁决），与本迁移正交，互不等待。
4. 读路径翻转（`TOFU_MESSAGES_ROWS_READ`）不在本范围 —— 按 charter 是翻转后的另一步。
---

## 6. 执行结果（2026-07-27 当日落地，pt_59140ecd ①②③）

- **① 增量 dual-write**：`dual_write_conv` 改为 count 探测 + 尾部追加 + tip 刷新（覆盖流式 finalize 的同数改写）+ 截断修复；编辑类调用方传 `changed_seqs`。统一钩 `mirror_write_and_commit`（旗关=纯 no-op，旗开=镜像+立即提交，pt_7e4afe73 持久性纪律）。
- **② 挂钩覆盖**：26 个整 blob 写点全部接钩（24 新 + 2 既有），AST 棘轮 `test_messages_rows_hook_coverage.py` 封锁新写点（UPDATE-messages / upsert-CONVERSATIONS / 裸 INSERT 无钩即红）。
- **③ fleet backfill 完成（221s）**：4,173 候选，3,697 原鲜跳过 + 476 重建，**parity 全 OK，0 残留**。独立 SQL 复核：4,173 会话 / 31,207 行，完整覆盖 **4,173 / 0 部分 / 0 空**（基线 3,689 / 484 / 477）。Top-5 blob 全 ✓。
- **过程中的两个实测发现（已修）**：①真实 blob 会携带**重复 `_msgId`**（pt_97f32163 事故形状，ms1uojtuhk9fze 实证）——backfill 与同 seq 外增量探测都做了去重（后序 dup 写空 id，meta 无损）；②backfill 与翻旗之间活跃会话会**内容漂移**（3 个活会话 tip 陈旧，count 相同但 parity 不符）——**翻旗时必须再跑一次全量 parity + 修复漂移**（count 相等骗得过 `row_window_usable`，骗不过 parity）。
- **剩余人工闸（④）**：owner 确认 → 设 `TOFU_MESSAGES_ROWS=1` + 重启 → dual-write 即时生效 → 翻旗后立即重跑本脚本 dry-run（应为全 fresh，修复残余漂移）→ 完成。读路径翻转（`TOFU_MESSAGES_ROWS_READ`）仍是之后的独立决策。

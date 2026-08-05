# 429 饱和有界升级 + 重试态投影 + 控制面失明根治 — 设计稿

> Epic: `pt_a21cd6ebda4d4c8d`（owner 2026-08-01 指令）
> 触发事故：conv `ms9ow2ttm0gnu0` 的 VU carrier `fb6d1f8d` 在 opus-5 上 429 循环
> **~75 分钟、3900+ 次、零 token**（16:54:56 → ~18:14 自愈）；后续 worker
> `7ddbc751` 落入同一面墙。本文三个交付全部有实测证据链。

## 0. 事故解剖（全部实测）

| 时间 | 事实 | 证据 |
|---|---|---|
| 16:42:07 | sankuai_key_2 当日 402 耗尽，禁用；opus-5→kimi-k3 回退救了 msg 9 | error.log |
| 16:54:53 | VU carrier fb6d1f8d 创建（父任务 4a233472 线程内联） | app.log |
| 16:54:56→18:14 | 429 循环 3900+ 次（两 key 交替），**零回退尝试、零 token** | app.log |
| ~17:09 | **fb6d1f8d 从内存注册表蒸发**（abort 404、abort-conv=0、conv-state ABSENT） | 实测 curl |
| 17:11:20 | 我的一次 poll 把活任务的 DB 行误翻 `interrupted`（"absent=crashed" 启发式） | app.log WARNING |
| 18:14 前后 | 限流窗口打开，round 1 流出 thinking（事件 856 条/分钟） | task_events 表 |
| 18:20:35 | VU 完成（done, 1236 字）,carrier settle | app.log |
| 18:20:4x | turn 4 worker 7ddbc751 生成——**又是 opus-5**（会话模型 16:58 已切 kimi-k3） | app.log |
| 18:22+ | 7ddbc751 429 循环中；**同样从注册表蒸发**（poll 再次报 NOT in memory） | 实测 curl |

三个独立的缺陷被这一根导火索引爆：

1. **429 无限循环无升级**（策略缺陷）——`lib/llm_dispatch/api.py:276` 注释
   明文「hard_attempts counts only non-429 failures; 429 loops forever」。
2. **Autopilot 链模型钉死**（bug #2）——`lib/tasks_pkg/autopilot_baton.py:371`
   `cfg = dict(task.get('config') or {})`：follow-up 直接拷贝父任务 config，
   **从不重解析会话当前 settings.model**。owner 16:58 把会话切到 kimi-k3，
   turn 4 依然走 opus-5。服务器自己在 quota 回退后持久化了 kimi-k3，链却不用。
3. **活任务从注册表蒸发**（bug #3，执行中新发现，最严重）——两个活任务
   （carrier + 普通 worker）在运行中从 `tasks` 消失：abort 404、busy 投影
   缺席、reaper 不可见、poll 误翻 DB 行、py-spy（172/201 线程）找不到幽灵
   线程。全仓唯一 pop 点 `discard_task`（autopilot.py:537，在
   `_run_single_turn` 返回后的 finally）当时未执行（无 settle 日志，直到
   18:20:35 自然完成才出现）。**蒸发路径未定案**。

## 1. 交付①：429 全 slot 饱和的有界升级

### 根因

`dispatch_chat`/`dispatch_stream` 的 429 不计 `hard_attempts`（api.py:276），
且 `_stream.py` 的调用带 `strict_model=True`（用户钉选的模型，429 只在同模型
slot 池内轮转）。402 会 `is_quota=True` → key 禁用 → 错误冒泡 →
`llm_fallback._call` 的 except 分支换模型；429 永远不冒泡，fallback 层
**永远收不到信号**。旁证：stuck-task reaper 的 `_dispatch_heartbeat` 在
dispatch 期间持续刷新（_stream.py `_on_waiting` 注释明写这是设计），所以
reaper **按设计**永远不杀 429 循环任务——两个子系统各自的「活着」定义
组合出一只**永生且不可见**的幽灵。

### 设计

新增**每次 dispatch 调用粒度的饱和计时器**：

- 当本次 dispatch 内，当前模型的**全部候选 slot 连续 429**（无任何一次
  attempt 拿到首字节）超过 `TOFU_429_SATURATION_SECS`（**2026-08-03 起默认 0 =
  关闭**：owner 指令「429 永不打断对话，重试零成本」——无限轮转为默认行为；
  设正数预算即恢复本交付的有界升级），dispatch 抛出
  `RateLimitError(is_saturation=True, status_code=429, reason='saturation:<model>:<secs>')`。
- `RateLimitError` 新增 `is_saturation` 属性（默认 False）。它与 `is_quota`
  **刻意分开**：key 是健康的（只是被挤），**不喂** key_stats 的
  「exhausted for today」，也不喂 slot 的 consecutive-429 计数列。
- 升级只发生**一次 per dispatch call**：抛出后由 `llm_fallback._call`
  既有 except（非 AbortedError）分支接住 → 走既有回退链换模型 → 新模型的
  dispatch 自带新计时器。回退模型也饱和 → 错误**有界地**浮出给用户
  （现在的行为是无限静默）。
- 计时器在任何 attempt 拿到首字节时重置；既有 per-(key,model) 排除与
  60s 排除重置行为不变；饱和判定只统计「真 429」，`is_gateway` /
  `is_shared_contention` 各走既有通道（shared contention 语义上就是
  「全项目饱和」，它同样计入饱和窗口——这正是本事故的形态）。
- 观测：升级时 `logger.warning` + `audit_log('llm_429_saturation',
  model, secs, cycles)` + 一条 phase 事件（见交付②）。

### 守卫（失败先行）

1. fake dispatcher 全 key 429：119s 内不升级（中途一次成功 attempt 即重置）。
2. 120s 后精确抛 `is_saturation`，key_stats **无** exhausted 记录。
3. 端到端：primary 饱和 → llm_fallback 换模型 → 任务在新模型完成
   （monkeypatch `_get_fallback_model`）。
4. env=0：与现行为逐字节一致（循环到 fake 设定的成功点）。
5. NEUTER×2：摘计时器 → 测试 2 红；摘 fallback 接住 → 测试 3 红。

## 2. 交付②：重试态投影到前端

### 实测纠偏（范围比想象小）

事件生产与转发**已存在**：`_on_retry`/`_on_waiting`（manager/_stream.py）
在 429 循环中持续发 `phase=retrying` 事件（带 model/attempt/i18n key）；
VU carrier 契约 `_VU_FORWARD_TYPES` 含 `'phase'`，会包装成
`autopilot_vu_event` 双发（fb6d1f8d 循环期间实测写了 **7154+** 条包装事件）。
**断的是前端渲染**：截图里气泡完全空白，说明附着到 carrier 的
「detached dummy assistant」/VU 气泡没有把这些帧画出来；且 busy 投影
丢失后（bug #3）发送钮回发送形态、侧栏却挂「未完成」，三信号互相矛盾。

### 设计

1. **前端**：VU 气泡 / dummy assistant 的 reducer 消费
   `autopilot_vu_event` 的 `inner.type=='phase'`（`phase=='retrying'`），
   复用普通任务的 retrying 渲染分支 + 既有 i18n key
   （`stream.phase.retryRateLimited` 等），显示「限流重试中 · 第 N 次 · 已等 Xs」。
   harness：模拟 carrier 发 retry 相位序列 → 断言气泡文案随 attempt 刷新。
2. **升级事件**（交付①触发时）：新增一行 phase 事件
   「<model> 限流持续 120s，切换 <fallback>」（复用 phase 缝与
   fallbackFrom/fallbackModel 投影，不加新事件类型）。
3. **busy 一致性**：发送钮/侧栏/连接状态的单一事实源是
   `snapshot_running_by_conv`——交付③修好注册表后此链自然恢复；
   本交付只在 harness 里钉「429 循环中 busy 恒真」。

## 3. 交付③：活任务对控制面失明（新发现，根因调查中）

### 已排除与已证实

- 已证实：`tasks`/`tasks_lock` 是 `_chat_runtime._tasks` 的别名（单注册表）；
  全仓唯一 pop 点 `discard_task`（finally 未在当时执行）；reaper 双钟
  fresh 故 spared（按设计）；cleanup/shed 只收终态任务。
- 已证实受影响面：VU carrier 与普通 worker 都可蒸发；父任务（终态，
  应在 3600s TTL 内）也提前消失。
- 未排除的嫌疑：** evaporate 路径未定案 **——需要观测性先补上才能抓现行。

### 设计（三步，按风险升序）

1. **可观测性（先行，零行为变更）**：`discard_task`、
   `TaskRuntime.cleanup_stale`、`shed_memory_under_pressure`、reaper、
   所有 abort sweep 在打点时带 task_id + 调用方摘要打 INFO。
   目标：下次蒸发发生时有日志指纹。
2. **poll 的 "absent=crashed" 启发式收口（止血，行为变更小）**：
   chat_poll 的 DB 分支只有在 `completed_at` **陈旧**（>2×checkpoint 间隔，
   即确无活跃写入者）时才翻 `interrupted`；checkpoint 新鲜但内存缺席 =
   「活着但不可注册」→ 报 `running` + WARNING（当前行为是直接把活任务的
   行翻成 interrupted——本事故中我的两次 poll 污染了两个活任务，
   靠后续 checkpoint 自愈）。
3. **DB 兜底的 abort 通道（根治控制面失明）**：abort 端点在注册表 miss
   且 DB 行 `status='running'` 时，把 abort 请求写进该行 metadata
   （`_abort_requested=ts`）；运行中任务的 `abort_check`（_stream.py 的
   lambda 现为只读 `task['aborted']`）在 checkpoint 节奏上回读该标记，
   读到即自杀（走正常 AbortedError 路径，有终态、有 settle）。
   这条通道顺带根治「重启前 quiesce 不可达」同族。

### 守卫

- ②③各有失败先行套件：活任务注册表摘除后 poll 不翻行（②）；
  注册表摘除后 abort 经 DB 通道仍能终止任务（③，端到端：
  起任务→`tasks.pop`→abort→断言任务在下一个 abort_check 死亡且
  终态行正确）。
- ②的 NEUTER：恢复旧启发式 → 红；③的 NEUTER：abort_check 不读
  tombstone → 红。

## 4. 非目标（明确排除）

- **不改** per-key 429 轮转的既有语义（单 key 429 轮转是对的）。
- **不动** `is_quota` 的 key 禁用通道（402 语义不变）。
- **不动** `_VU_FORWARD_TYPES` 的内容（'phase' 已在列，事件够用了）。
- bug #2 的修法**不在本票扩面**为「链上所有 config 项都重解析」——只
  `model`/`preset`（及由其派生的 model_info 钳制）在 `_start_followup_task`
  从会话 settings 重解析，其余 config 保持从父任务继承（turn 级状态）。

## 5. 实施顺序

1. 交付③-1（观测性日志）——先行合入，给蒸发抓指纹。
2. 交付①（429 升级）+ 守卫。
3. 交付②（前端投影 + harness）。
4. 交付③-2/③-3（poll 止血 + DB abort 通道）+ 守卫。
5. bug #2 修复（baton 重解析 model/preset）+ 守卫（harness：会话中途切
   模型 → follow-up 用新模型）。
6. 全环 + NEUTER + journal + board complete。

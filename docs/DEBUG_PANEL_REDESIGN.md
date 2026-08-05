# Debug 面板重设计 —— 请求检视器(Request Inspector)

> 状态:**owner 已拍板方向,P1 已开工**(2026-07-25)。
> 本文是重设计的唯一权威稿;`docs/RENDER_CONTRACT.md` / `docs/HEADLESS_API.md`
> 中关于 `messages_snapshot` 的字段描述以本稿 §3 的扩展为准。
> 类比一句话:把 debug 面板从「Elements 面板」升级成「Network 面板」——
> 一行一次 LLM 请求,点开看完整 payload 与响应元数据。

---

## 1. 背景与诊断(为什么重做)

旧面板(`static/js/core/debug_panel.js` + `index.html:681` 的右下悬浮盒)有五个结构性错位,
全部对着代码核实过:

| # | 错位 | 现状(证据) | 后果 |
|---|---|---|---|
| 1 | 归属错位 | 内容是会话级(`_debugCache[convId]`),容器却是全局悬浮窗 | 心智上对不上「它属于这个会话」 |
| 2 | 粒度错位 | 面板 = 最新一轮的整包 wire messages 平铺 | 长会话 50–100+ 条里人肉对账「问题气泡是哪条」 |
| 3 | 时间维丢失 | 每轮 `messages_snapshot` 都 SSE 推达(`_run.py:792`),前端只存最新一份 | 历史轮次的请求形态看不了——尽管 `task_events` 表 6h 内每轮都在(`event_log.py`,`append_event` durable-before-visible) |
| 4 | 请求要素不全 | 只有 messages + tools;没有 model/params、没有 response 侧(usage/trace_id/耗时) | 「完整暴露真实请求」只完成了 request body 的一个字段 |
| 5 | 无锚点 | 气泡与 wire 消息之间没有映射通道 | 「这个气泡 ↔ 哪次请求」全靠猜 |

**继承的地基(不动):**
- wire SSOT `lib/tasks_pkg/wire_messages.py` —— 冷热两路同一条管线,字节级一致;amber approx chip 诚实标注近似项;
- 气泡侧已有 `msg._taskId` + `apiRounds[].round`(1-based,与 snapshot 的 `roundNum` 同编号)+ `toolRounds[].llmRound`(0-based)——锚点数据链现成;
- `task_events` 持久化(6h TTL)+ `GET /api/v1/tasks/<id>/events` 游标回放 —— 请求历史的后端数据现成;
- `round_usage` 事件(`llm_fallback/_usage.py`)已带 `roundNum / model / tag / tokensIn / tokensOut / usage{trace_id, stream_elapsed_ms, …}` —— 响应侧元数据现成,只差 join。

---

## 2. owner 拍板(2026-07-25,逐字生效)

1. **形态 A**:右侧抽屉 + 请求列表;气泡内联增量(形态 B)二期再说;
2. **默认视图 = 请求列表**(Network 式),不是消息树;
3. **历史深度 = `task_events` 6h**;列表端点只回元数据,payload 按需取;
4. **气泡 `</>` 入口接受**,debug_mode 门控;
5. **snapshot 扩展一次加全**:model + params + response 侧(usage/trace_id/耗时,经 `round_usage` join)。

外加 owner 点名的三条边界(必须写进设计并落地):

- **发射点分型**:四个 `messages_snapshot` 发射点不都是 LLM 请求(见 §3),请求列表以 `kind='request'` 为准;
- **task 轴,不是 conv 轴**:一个 conv 随时间有 N 个 task(真人轮次 + VU 轮次);检视器 = task 列表 → 轮次列表两级;
- **覆盖边界诚实标注**:endpoint 模式与 swarm 子 agent 目前**完全不发** `messages_snapshot`
  (grep `lib/tasks_pkg/endpoint/`、`lib/swarm/` 零匹配,2026-07-25 核实)——
  本设计不覆盖它们,UI 上对这类任务挂诚实标注,**不许装成「这就是全部请求」**;
  补发射是独立后续 epic(§8 P4)。

---

## 3. 数据面:事件契约扩展(P1 本批落地)

### 3.1 `messages_snapshot` 新增字段

| 字段 | 取值 | 说明 |
|---|---|---|
| `kind` | `'request'` \| `'state'` | **必填**。请求 = 即将发往 LLM 的 payload;state = 状态镜像(不是请求) |
| `model` | 模型 id | 请求类必填;state 类有则填 |
| `params` | 对象,见 §3.3 | 仅 `kind='request'` 携带 |

四个发射点的分型(**冻结**):

| 发射点 | 位置 | `roundNum` | label 形态 | `kind` |
|---|---|---|---|---|
| 每轮请求前 | `orchestrator/_run.py` | int(1-based) | `Round N 请求前 · M条` | **`request`** |
| 工具结果后 | `tool_dispatch/_pipeline.py` | int | `Round N 工具结果后 · M条` | `state` |
| 最终回复后 | `orchestrator/_post_loop.py` | `'final'` | `最终回复后 · M条` | `state` |
| fallback 合成前 | `orchestrator/_finalize.py` | `'fallback'` | `Fallback · M条` | `state` |

> 注意 `roundNum` 类型本来就是不一致的(int vs 字符串标签)——这正是要引入
> 显式 `kind` 而不是靠解析 label 分类的原因。新增字段为 additive change,
> `EVENT_CONTRACT_VERSION` 不 bump(见 `lib/agent_core/events.py` 头注)。

### 3.2 响应侧:不扩 snapshot,走 join

response 侧元数据**已经**在 `round_usage` 事件里,按 `(taskId, roundNum)` join 即可。
**一轮可以有多次真实 HTTP 调用**(primary 失败→fallback 成功 = 2 次;FloorRetry 丢弃
尝试也计费),所以检视器的最小单位是 **attempt**(一次真实调用),不是 round:

| `round_usage.tag` | 含义 |
|---|---|
| `R{N}` | 主模型第 N 轮 |
| `R{N}-FALLBACK` | 第 N 轮回退模型 |
| `R{N}-REACTIVE` | reactive-compact 重试 |
| `R{N}-DISCARDED` / `…-REACTIVE-DISCARDED` | FloorRetry 丢弃尝试(仍计费) |
| `FALLBACK` | 终态 fallback 合成 |

### 3.3 请求列表行 schema(**冻结**——先定死,不做到一半再改)

**Task 行**(检视器第一级):

| 字段 | 来源 | 说明 |
|---|---|---|
| `taskId` | 事件流 / `msg._taskId` | 主键 |
| `convId` | 任务注册 | 归属会话 |
| `startedTs` | 首事件时间 | 排序键 |
| `status` | task registry | running/done/error/aborted |
| `requestCount` | kind=request 计数 | 行摘要 |
| `isVu` | task 标记 | VU 子任务(与真人轮次区分样式) |
| `coverage` | `'full'` \| `'uncovered'` | endpoint/swarm 驱动 = `uncovered`,挂诚实 chip(§7) |

**Request 行**(第二级,一次 LLM 请求 = 一行):

| 字段 | 来源 | 说明 |
|---|---|---|
| `roundNum` | snapshot(int) | 任务内 1-based |
| `ts` | 事件时间 | |
| `model` | snapshot.model | 该轮实际模型 |
| `params.maxTokens` | snapshot.params | |
| `params.temperature` | snapshot.params | |
| `params.thinkingEnabled` / `params.thinkingDepth` | snapshot.params | |
| `params.preset` | snapshot.params | |
| `params.responseFormat` | snapshot.params(可空) | |
| `messageCount` | snapshot.messages.length | |
| `toolsCount` | snapshot.tools.length(可空) | |
| `~tokens` | 前端估算(`_debugMsgTokens` 合计) | 诊断量级,非计费 |
| `label` | snapshot.label | 兼容旧渲染 |

**Attempt 行**(挂在 request 行下,来自 `round_usage` join):

| 字段 | 来源 |
|---|---|
| `tag` / `model` | round_usage |
| `tokensIn` / `tokensOut` | round_usage |
| `traceId` | `usage.trace_id` |
| `streamElapsedMs` | `usage.stream_elapsed_ms` |
| `cacheRead` / `cacheWrite` | `usage`(normalize_usage 口径) |

**State 快照行**(不进请求列表;详情视图里作为「该轮后的状态镜像」可选展开):
`subtype = post-tool | final | fallback`(由 roundNum/label 判定),`kind='state'`。

---

## 4. 检视器结构(task 轴)

```
会话 (conv)
 └─ Task 行(按 startedTs 倒序;真人轮次 / VU 轮次分样式)
     └─ Request 行 R1..Rn(kind=request)
         ├─ Attempt 行(0..k 次真实调用,round_usage join)
         ├─ Request payload 详情:params + tools + messages 树
         └─ State 镜像(可选:post-tool / final / fallback)
```

**气泡锚点**(P3):气泡 `</>` 按钮(debug_mode 门控)→ `msg._taskId` 定位 Task 行,
`apiRounds[].round`(1-based)定位 Request 行,抽屉打开即滚动到位。
编号一致性已核实:snapshot `roundNum = round_num + 1` 与 `apiRounds[].round` 同为任务内 1-based;
`toolRounds[].llmRound` 为 0-based,换算 `round = llmRound + 1`(finish_info.js 已有同款换算)。

**前端数据面**(P1 本批落地):`_debugRequests[taskId] = { rounds: Map<roundKey, roundEntry>, order: [...] }`,
SSE `messages_snapshot` **追加**进轮次表(不再整体覆盖);`_debugCache`(旧面板渲染源)保留不动,
旧面板在 P2 抽屉落地前继续工作。

---

## 5. UI 形态(P2,形态 A)

- 右侧 drawer(与 settings 同族),打开时把 chatinner 挤窄而不盖住——明确「属于这个会话」;
- 左栏请求列表(§3.3 行 schema),右栏选中请求详情(params + tools + messages 树 + attempts);
- **增量高亮**:第 N 轮详情默认折叠与 N-1 轮相同的前缀消息,高亮增量——排查 context drift 的主视角;
- 手机端:drawer 全屏化(沿用 `.debug-panel` 现有移动端断点惯例)。

## 6. 历史深度(拍板 3)

| 时间窗 | 数据源 |
|---|---|
| live(任务在飞) | SSE 追加(P1 数据面) |
| ≤6h | `GET /api/v1/tasks/<id>/requests`(P2 新增,**元数据-only** 行;payload 按需 `/requests/<roundNum>` 取)——从 `task_events` fold,不新建存储 |
| >6h | 旧 `/api/v1/conversations/<id>/debug-messages` 整包重建 + approx chip(降级视图,诚实标注) |

## 7. 覆盖边界(诚实标注,不许撒谎)

1. **endpoint 模式 / swarm 子 agent 无 snapshot**(2026-07-25 grep 实证)→ 这类 task 行
   `coverage='uncovered'`,UI 挂「此任务类型的 LLM 调用暂未纳入检视」chip(沿用 approx chip 机制);
   补发射见 §8 P4 后续 epic;
2. **transport 层变换不展开**(图片降采样/vision-strip/provider 信封)——沿用 wire_messages.py 的既有声明;
3. **state 快照不是请求**,绝不混进请求列表;
4. 大 payload 已在 `_strip_base64_for_snapshot` 截断(base64 占位、>100KB 文本截 1000 字符、
   >50KB tool args 截 2000 字符)——详情视图对截断处显示占位标记,不伪装完整。

## 8. 分期

| 期 | 内容 | 状态 |
|---|---|---|
| **P1** | 后端:四个发射点扩展 `kind`(+request 点 `model`/`params`);前端:按 task 按轮存 list 数据面(追加不覆盖)。静态守卫 + jsdom(failing-first + NEUTER) | **本批** |
| P2 | `/api/v1/tasks/<id>/requests` 元数据端点 + 右侧抽屉 + 请求列表 UI | 待开工 |
| P3 | 气泡 `</>` 锚点 + 前缀折叠增量高亮 | 待开工 |
| P4 | **独立 epic**:endpoint/swarm 调用点补发 snapshot(覆盖 `endpoint/_run`、planner/critic、swarm agent LLM 调用),摘除 uncovered chip | 待开工 |
| **P5** | **数据面重构(owner 2026-07-25 拍板,最高优先)**:snapshot 改增量存储(§10)+ 保留期 30 天 + 一次性迁移带逐字节校验(§11) | **本批** |
| P6 | 工具行就地展开入口(每行一枚 `</>`,默认展示增量)+ 上下文球内联输入框右侧 + 抽屉降为全文查看器 | 待开工 |

## 9. 测试与守卫

- 静态守卫:四个发射点必须带 `kind=`;request 点必须带 `model=` + `params=`(NEUTER 可咬);
- jsdom:同 task 两轮 snapshot 追加保留(不覆盖)、kind 路由(request→轮次表/state→镜像槽)、kind 缺失的旧事件按 request 兼容;
- 回归:`test_frontend_debug_preserve_open` / `test_frontend_debug_brain_badge` / `test_frontend_debug_approx_chip` / `test_frontend_sse_dispatch`(#22)/ `test_event_emit` / `test_wire_messages_fidelity` / `test_empty_assistant_ghost_wire` / `test_persist_vertical_block_relocate`;
- 生效边界:后端随提交+重启生效;前端走内容哈希 bundle,需重启 + 硬刷。

---

## 10. 增量存储格式(P5,owner 2026-07-25 拍板——格式冻结)

### 10.1 为何必需(实测数据,不是推测)

当前每轮都整包重存全量 `messages` + 全量 `tools`。单任务 `efb479f6` 实测:

| 项 | 实测值 |
|---|---|
| 全量存储 | **123.2 MB**(167 轮快照) |
| 增量存储(messages diff + tools 去重) | **1.9 MB** |
| 压缩比 | **65.7×** |
| `tools` 数组 | 每轮固定 **201,898 字节**、167 轮**逐字不变地重存 167 遍**(≈33 MB 纯冗余) |
| `messages` 每轮新增 | 通常 **2 条**,增量 2～28 KB;而整包是 180～294 KB |
| 全库占比 | `messages_snapshot` 占 `task_events` 总字节的 **92.4%**(均值 403 KB/条) |

结论:**单做 messages 增量不够** —— tools 的 33 MB 冗余会把压缩比从 65× 拉到 4×。
三类冗余必须同时消除。

### 10.2 四条格式要点(**冻结**)

**① `tools` 按内容哈希去重,全任务共用一份**

- 首次出现某 `toolsHash` 时,单独落一行 `tools_dict` 事件:
  `{type:'tools_dict', toolsHash, tools:[…]}`;
- 此后每轮 snapshot **只带 `toolsHash` 引用**,不带 `tools` 数组;
- 工具集真变了(哈希变)才落新的一份——实测一个任务通常全程只有 1～2 份。

**② `messages` 增量**

```
{ kind, roundNum, turn, model, params,          // 元数据(原样)
  baseRound,        // 基线轮(上一条同 task 同 turn 的 snapshot)
  prefixLen: K,     // 与基线共享的前 K 条
  prefixHash,       // 前 K 条的 canonical 哈希(校验用)
  newMessages: […], // 只存第 K+1 条起的新消息
  messageCount: N,  // 重建后应有的总条数(校验用)
  toolsHash, toolsCount, approxTokens }
```

- `approxTokens` / `messageCount` / `toolsCount` **写死在行里**——列表页(fold)不需要重建就能渲染;
- 共享前缀用**与前端同一套语义**(canonical JSON 位置对齐求最长共享前缀),不造第二套。

**③ 重复轮次不落整包**

实测大量 `newMessages=[]`(增量 2 字节)的行——同一轮的重复发射。
`prefixHash` 与前一条相同且 `newMessages` 为空 → 只落一行空记录
(`prefixLen == messageCount`,无 `newMessages` 字段),绝不再存一遍整包。

**④ 回放 API 形状不变**

`GET /api/v1/tasks/<id>/requests/<round>` 依旧返回**完整重建后的报文**。
重建在**服务端**完成(逐行回放:继承前 K 条 + 追加 newMessages),
前端与任何其他消费方**完全不感知增量**——否则每个消费方自己拼,迟早不一致。

### 10.3 重建失败 = 诚实降级,绝不假装

`prefixHash` 对不上(基线行被 prune 删了 / 写入时崩溃留下空洞)→
该轮标 `degraded: true` + `degradedReason`,返回已知的部分 + 前端挂诚实 chip
「该轮无法精确重建」。**不允许静默返回不完整报文。**

### 10.4 保留期分层(TTL)

| 事件类 | 保留期 | 理由 |
|---|---|---|
| `delta` / `phase` / `tool_progress` 等流式噪声 | **6h**(不变) | 只为 SSE 断线重连窗口服务 |
| `messages_snapshot` / `tools_dict` / `round_usage` / `round_start` / `round_end` | **30 天** | 请求检视器的结构事件;增量化后总量已降 20×+ |

> 顺序硬约束:**先增量化、再延长保留期**。先延长会把 FUSE 上的 pg 拖坠。

---

## 11. 一次性迁移 + 校验(P5,不允许「迁完就删」)

1. 逐任务扫 `messages_snapshot` 旧行 → 压成 §10.2 的增量形 + 抽出 `tools_dict`;
2. **写入新行后、删旧行前**,逐轮调用重建函数,与原始整包报文**逐字节比对**
   (canonical JSON 相等);任一轮不一致 → **该任务整体回滚、不删旧行、记 error 日志**;
3. 幂等:已增量化的行(带 `prefixLen`)跳过;可重跑、可分批;
4. 验收口径(owner 会重跑):
   - `SELECT SUM(pg_column_size(payload)) … WHERE type='messages_snapshot'` 总字节**下降 ≥ 20×**;
   - 随机 3 个历史任务逐轮调 `/requests/<round>`,与迁移前报文**逐字节一致**;
   - 无法精确重建的轮次有**诚实标注**。

---

## 12. 消息体结构化渲染(P7,owner 2026-08-05 拍板)

> 起因(owner 截图内联状态面板):「debug 面板里的 JSON 没有格式化——`arguments`
> 本身就是一个复杂 JSON 字符串,不渲染看得很困难;这个面板要好好再设计一下;
> 不要给每个工具调用的结果显示 TOOLS 大列表了,没意义。」

三条拍板,全部落在**共享渲染器**(`debug_panel.js::_renderMsgBodyHtml`,
抽屉详情与内联面板同一条路径,不造第二渲染器):

1. **arguments 解析渲染,不再裸转义**:`tool_calls[].function.arguments` 是
   JSON **字符串**,旧视图整包 colorJson 时它是一行带 `\n` 转义的超长引号串。
   新视图 `_debugTryParseJson` 解析后按 key 分块:长/多行字符串渲染为
   **真实换行的可读文本块**(`.debug-arg-val`,max-height 320px 滚动),
   嵌套对象/数组与「内容本身是 JSON 的字符串」渲染为语法着色 JSON,
   短标量留在行内。reasoning_content / content 同理进可读 `<pre class="debug-text">`
   (tool 结果若是 JSON 文本则解析渲染)。**原始信封不丢**:每块尾部
   `原始 JSON` <details> 保留完整 colorJson dump(复制路径与对账的 ground truth)。
2. **内联面板不再渲染 TOOLS schema 大列表**:每轮都相同的工具清单对一个
   「这一轮调用」面板是纯噪声(`_riRenderToolPanel` 不再调
   `updateDebugToolsBlock`)。**抽屉详情保留**——那里 tools 是请求 payload
   的组成部分,检视请求时它是有意义的。
3. **小增量自动展开**:内联面板渲染的消息数 ≤6 且总字符 ≤300KB 时
   `_debugOpenBlock` 全部展开——面板存在是为了「一眼回答这一轮」,
   不让用户逐条点开;大 payload 保持折叠按需渲染。

守卫:`tests/test_frontend_debug_structured_body.py`(jsdom:参数分块/真实换行/
无 TOOLS 块/自动展开/原始 JSON 保真 + 双 NEUTER;静态钉:内联面板永不回长
TOOLS 块)。`test_debug_panel_contrast` 的配色语义不变——结构化视图的
语法色沿用同一套已测 hex 调色板(`.debug-struct .debug-key` 等,
dark/light/tofu 三主题同值)。

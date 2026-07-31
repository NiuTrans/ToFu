# `_RoundState` 设计前置 — run_task 流式主循环 locals 清点

> 状态:**纯清点,零代码**。owner 审完本清单后拍板 `_RoundState` dataclass 形状,再切 slice。
> 扫描对象:`lib/tasks_pkg/orchestrator/_run.py` 主循环(while 起于 :520,循环体 :520–1295,
> 循环状态初始化 :505–512,跨迭代结果变量初始化 :451–456)。
> 扫描时间:2026-07-27,基于 commit `9718311b` 之后的工作树。
> 关联:pt_03f4cdf1(headline target)、charter「Agent 能力复用铁律」执行项、
> `docs/AGENT_CAPABILITY_GUIDE.md` §5(orchestrator → endpoint 迁移顺序)。

## 0. 总数速览(与票面「~30-40」对账)

| 类别 | 数量 | 说明 |
|---|---|---|
| **真·跨迭代 locals**(进 `_RoundState` 的候选) | **15** | 轮 N 写入、轮 N+1 或循环后读取 |
| 模块级常量(不进 state) | 2 | `_PREMATURE_RETRY_MAX`、`_MAX_CONSECUTIVE_TOOL_TIMEOUTS` |
| 轮内临时量(留作钩内 locals) | ~12 | 每轮重建,不跨迭代 |
| task-dict 通道(本就在 task 上,非 locals) | 7 | 跨轮挂账,但载体是 task dict 不是局部变量 |
| 只读配置/引用(进调用方上下文,不进 state) | ~14 | `cfg/tid/messages/original_messages/tool_list/…` |

票面「~30-40」把轮内临时量也算进去了;**真正的设计难点只有 15 个跨迭代量 + 7 条 task 通道**。

## 1. 跨迭代 locals 清单(15 个,`_RoundState` 候选)

### 1.1 循环控制组(control)— 8 个

| 变量 | 形状 | 写入点 | 读取点 | 跨迭代语义 | 建议字段 |
|---|---|---|---|---|---|
| `round_num` | int,init -1(:512) | :519 每轮 +1 | 全循环 + finalize:1306 | 轮次计数(0 起,事件里 +1 显示) | `control.round_num` —— 底盘 `rnd` 原生接管 |
| `_loop_exit_reason` | str,init 'max_rounds_exhausted'(:506) | :522(abort):923(llm break):1098(stream):1124(budget):1143(rounds 尽):1260(超时熔断) | finalize:1313 | 诊断:循环为什么结束 | `control.exit_reason` —— 底盘 `outcome.exit_reason` 同形,映射即可 |
| `_abort_detected_phase` | str\|None(:507) | :521(轮首):1096(stream 决策回传) | finalize:1314 | 诊断:abort 在哪个阶段被检出 | `control.abort_phase` |
| `_premature_retry_count` | int(:508) | :1092(自 analyse_stream_result) | :520(天花板!):1089(传入 analyse) | **直接参与循环条件** `round_num+1 <= max_tool_rounds + _premature_retry_count` | `control.premature_retry_count` —— **底盘 `retry_bonus`/`max_retry_bonus` 同形机制,迁移时替换为钩** |
| `_consecutive_tool_timeouts` | int(:510) | :1250(+1):1258(清零) | :1248–1257(熔断判定) | 连续工具超时计数,≥3 强制 break | `control.consecutive_tool_timeouts` —— **底盘无此机制,候选新钩(见 §3 缺口 B)** |
| `_last_checkpoint` | float epoch(:511) | :1267 | :1265(≥5s 节流) | 崩溃恢复 checkpoint 节流钟 | `control.last_checkpoint_ts` —— 底盘无 checkpoint,候选新钩(缺口 C) |
| (循环条件本身) | `while round_num+1 <= max_tool_rounds + _premature_retry_count` | — | :520 | 天花板随 retry 动态扩张 | 底盘 while 已内建同构(rnd > cap + bonus 退出) |
| (轮首 abort 检查) | `task['aborted']` | — | :521 | 三处 abort 检查之一 | 底盘 before-round 检查原生覆盖 |

### 1.2 LLM 结果组(llm)— 3+3 个

| 变量 | 形状 | 写入点 | 读取点 | 跨迭代语义 | 建议字段 |
|---|---|---|---|---|---|
| `assistant_msg` | dict\|None(:454) | :872(每轮 LLM 返回) | reconcile:1071、clean_msg:1160、translate:1174、finalize:1307 | 每轮覆盖,但**循环后读最后一轮** | `llm.assistant_msg` |
| `last_finish_reason` | str\|None(:452) | :873:1093:1120(budget):1138(rounds 尽) | analyse:1089、诊断、finalize:1311 | 同上,粘性「最后一次」 | `llm.last_finish_reason` |
| `last_usage` | dict\|None(:453) | :874(`or last_usage` —— 空不覆盖,粘性) | cache-break:934、analyse:1090、finalize:1311 | 同上 | `llm.last_usage` |
| `model` | str | :875(**fallback 换模型回写**) | snapshot:803、cache-break:936、budget:1117、finalize:1303 | fallback 后后续轮用新模型 | `llm.model`(初值来自配置) |
| `preset` | str | :876(fallback 回写) | build_body:815、finalize:1304 | 同上 | `llm.preset` |
| `thinking_enabled` | bool | :877(fallback 回写) | build_body/snapshot/finalize | 同上 | `llm.thinking_enabled` |

### 1.3 用量累积组(usage)— 2 个

| 变量 | 形状 | 写入点 | 读取点 | 跨迭代语义 | 建议字段 |
|---|---|---|---|---|---|
| `accumulated_usage` | dict(:455) | :866 传入 `_llm_call_with_fallback` 原地累加 | budget 闸:1116、finalize:1310 | 全循环累加 | `usage.accumulated` —— 底盘 `on_round_result` 钩原生位 |
| `api_rounds` | list(:456) | :866 传入原地 append;:937/:970 回戳 cacheBreak/toolCalls/writeBreakdown | finalize:1310 | 每轮一条,循环后整体消费 | `usage.api_rounds` |

### 1.4 工具状态组(tools)— 2 个

| 变量 | 形状 | 写入点 | 读取点 | 跨迭代语义 | 建议字段 |
|---|---|---|---|---|---|
| `tool_call_happened` | bool(:451) | :461(prefetch 注入):1154 | llm_call 入参:865、finalize:1309 | 「本任务是否发生过工具调用」锁存 | `tools.call_happened` |
| `tool_round_num` | int(:387) | :462(prefetch 偏移):1076(stream_acc 回读):1224(parse_tool_calls) | StreamAcc 构造:831、parse:1224 | 工具轮次编号分配器,流式预执行会消耗编号 | `tools.round_num` |

## 2. 轮内临时量(~12 个,留作钩内 locals,不进 state)

`_tools_this_round`(:689 每轮重建)、`_stream_acc`(:830)、`body`(:813)、`llm_result`(:864)、
`_cache_break`/`_tcs`/`_wb`/`_prev_turn_read`(cache 戳记族)、`clean_msg`(:1160)、
`parsed_tcs`(:1224)、`_tool_timed_out`(:1241)、peer/steer flush 临时族(`_peer_inject` 等)。
判据:全部满足「本轮写入、本轮读完、下轮不碰」。

## 3. 循环的四个底盘缺口(迁移前必须先改底盘,铁律第 4 条)

| 缺口 | 现状(行号) | 底盘现状 | 建议 |
|---|---|---|---|
| A. premature-retry 天花板扩张 | :520 天花板含 `_premature_retry_count` | ✅ 已有 `retry_bonus`/`max_retry_bonus` | 直接映射,`analyse_stream_result` 的判定逻辑包成 `retry_bonus(rnd,msg,finish,usage)` 钩 |
| B. 连续工具超时熔断 | :1248–1262,≥3 次 break + `tool_timeout` envelope | ❌ 无 | 新 `after_tools(rnd, results)->str\|None` 钩或复用 `before_round`(熔断计数属于 orchestrator,halt 理由 'tool_timeout') |
| C. 崩溃恢复 checkpoint(5s 节流) | :1264–1268 | ❌ 无 | 新 `on_round_end(rnd)` 钩或挂在 `execute_tools` 尾部(swarm 迁移时把 checkpoint 留在了自己的批量钩里——**注意两处收敛,别又长成两份**) |
| D. 预算闸(budget_exceeded) | :1114–1131 | ❌ 无 | `before_round` halt(理由 'budget_exceeded')——每轮顶检与现行位置(流后)有语义差,**需 owner 拍板**:流后检=本轮已花钱才停,轮首检=少花一轮钱 |

**工具供给语义对账(重要发现):** `_tools_this_round = tool_list if round_num < max_tool_rounds else None`(:689)——
orchestrator **本来就是 `tools_terminal_round=True` 语义**(末轮无工具逼模型收束),与底盘默认一致,无需翻转。
:1137 的 `tool_rounds_exhausted` 分支是「末轮后模型仍要工具」的兜底,映射为 outcome.exit_reason 的错误戳记即可。

## 4. task-dict 通道(7 条,非 locals,迁移时原样保留)

| 通道 | 作用 | 生命周期 |
|---|---|---|
| `task['_peer_inject_pending']` | peer 消息延迟确认(never-zero 修复) | 轮内 stash → LLM 成功后 flush |
| `task['_steer_inject_pending']` | human-steer 延迟确认(同上) | 同上 |
| `task['_inboxInjects']` / `['_peerInjects']` / `['_userSteerInjects']` | 展示用 sidecar 累积(下划线字段,绝不进 toolRounds) | 全循环累积,sync 层持久化 |
| `task['_compact_messages']` | context_compact 工具 handler 的活 messages 引用 | 轮内 set → 工具执行后 pop |
| `task['_dispatch_heartbeat']` | reaper 活性钟 | 工具执行前刷新 |

教训吸收:这些通道说明「跨轮状态」不一定以 locals 形式存在——`_RoundState` 设计**不要**试图把 task-dict 通道收编进 dataclass,它们的所有权属于 task(崩溃恢复/同步层直接消费),收编会制造第二个事实源。

## 5. 建议的 `_RoundState` 形状(待 owner 拍板)

```
_RoundState
├── control: round_num, exit_reason, abort_phase,
│            premature_retry_count(→底盘 retry_bonus 后删除),
│            consecutive_tool_timeouts, last_checkpoint_ts
├── llm:     model, preset, thinking_enabled,
│            assistant_msg, last_finish_reason, last_usage
├── usage:   accumulated, api_rounds
└── tools:   call_happened, round_num
```

显式不进 state:`task / cfg / tid / messages / original_messages / tool_list / max_tool_rounds /
search_enabled / response_format / thinking_depth / temperature / max_tokens /
project_path / project_enabled / all_search_results_text / _keep_tool_history / _conv_id`
(只读配置与中央可变引用,按参数传递)。

## 6. 退出路径全表(8 个 break + 1 个 continue + 自然落地)

| 位置 | 条件 | ROUND_END 事件 | exit_reason 形状 |
|---|---|---|---|
| :526 | 轮首 abort | 不发(注释明确:轮未开启) | `aborted_at_round_N` |
| :922 | llm_result `_loop_action=='break'` | 由 fallback 内部发 | 来自 llm_result |
| :927 | AbortedError 异常 | 不发 | `user_abort` |
| :1098 | stream_decision break | analyse 内部 | 来自 stream_decision |
| :1100 | stream_decision continue(premature retry) | **不发**(轮将重跑,不发假 END) | — |
| :1130 | 预算超 | reason='budget' | `budget_exceeded_round_N_$X` |
| :1148 | 工具轮预算尽 | reason='budget' | `tool_rounds_exhausted_N` |
| :1207 | 工具执行前 abort | reason='aborted' | `aborted_before_tools_round_N` |
| :1262 | 连续工具超时熔断 | 不发(前一轮已发 tools END?——**待 slice 时核对**) | `consecutive_tool_timeouts_N` |
| :1292 | 工具轮自然结束 | reason='tools' | (继续下轮) |

每轮顶部 :540 发 ROUND_START;abort 检查三处::521(轮首):1091-1099(stream 后,经 analyse):1194(工具执行前)。
**与底盘三检查对账:轮首=before-round ✅、stream 后=post-stream ✅、工具执行前=orchestrator 走批量钩时由钩前检查等价覆盖 ✅(语义:整批工具一次调用,批前查一次 = 现行行为逐字节)。**

## 7. 给 slice 执行者的三条纪律

1. 每个 slice 只搬一组(1.1/1.2/1.3/1.4),wire-parity 测试钉事件序列逐字节,NEUTER 按组配。
2. `_premature_retry_count` 是唯一直接参与循环条件的 local——它进底盘 `retry_bonus` 的那一刀是「真迁移」刀,其余都是「换容器」刀,把真迁移刀单独切一个 slice。
3. 缺口 B/C/D 的底盘新钩先落底盘 + 测试(照 swarm 三扩展的先例:底盘套件加用例),再接 orchestrator。

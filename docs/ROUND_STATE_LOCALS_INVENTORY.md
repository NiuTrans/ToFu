# `run_task` 流式主循环状态清点

> 最初的迁移前清点已于 2026-08-08 完成。本文记录当前契约，避免后续改动重新引入
> “工具轮数预算”或末轮撤掉工具的旧语义。

## 1. 循环终止契约

主编排器在任务有效时持续运行，并把同一份 `tool_list` 传给每一次模型调用。正常终止
条件是模型返回不含工具调用的 assistant 消息。工具轮数不是配置项，也不参与模型配置、
preset、API 请求或持久化任务结构。

独立安全机制仍然有效：

- 用户中止；
- token/cost 预算闸；
- 连续工具超时断路器；
- stream 异常和空响应的有限重试；
- 共享 agent loop 的无进展检测；
- 服务级 timeout 与崩溃恢复 checkpoint。

这些机制按各自语义终止任务，不能折算成“最多调用工具 N 轮”。

## 2. 跨迭代状态

| 组 | 字段 | 语义 |
|---|---|---|
| control | `round_num` | 仅用于编号、事件和遥测，不是上限 |
| control | `exit_reason`, `abort_phase` | 记录自然结束或具体安全机制 |
| control | `premature_retry_count` | 只约束 stream 异常重试 |
| control | `consecutive_tool_timeouts` | 连续超时断路器计数 |
| control | `last_checkpoint_ts` | 崩溃恢复 checkpoint 节流 |
| llm | `assistant_msg`, `last_finish_reason`, `last_usage` | 最后一轮模型结果 |
| llm | `model`, `preset`, `thinking_enabled` | fallback 后可更新的模型状态 |
| usage | `accumulated_usage`, `api_rounds` | 真实调用用量与逐轮账单 |
| tools | `tool_call_happened`, `tool_round_num` | 是否调用过工具及展示编号 |

`round_num` 和 `tool_round_num` 都是观测值。不得用它们形成数值循环条件或动态撤掉工具。

## 3. 轮内临时量

stream accumulator、request body、cache-break 标记、解析后的 tool calls、单轮 timeout
标记等每轮重建，不进入跨轮状态。请求准备层直接使用稳定的 `tool_list`。

## 4. task-dict 通道

下列通道的所有权仍属于 task，不应复制进第二个状态容器：

- `_peer_inject_pending` / `_steer_inject_pending`：延迟确认的注入消息；
- `_inboxInjects` / `_peerInjects` / `_userSteerInjects`：展示 sidecar；
- `_compact_messages`：context compact handler 的活 messages 引用；
- `_dispatch_heartbeat`：reaper 活性钟。

## 5. 修改纪律

1. 工具 schema 在所有轮次保持稳定，保护 prompt-cache 前缀。
2. 模型无工具调用时自然完成；不要注入反循环提示，也不要制造“最终无工具轮”。
3. 新的安全条件必须表达真实故障（超时、无进展、预算、中止），并有独立测试。
4. 历史会话中的 `tool_rounds_exhausted` 只允许在兼容读取和 UI 映射中出现；当前运行路径
   不得再生成这个值。

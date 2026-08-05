# 网关故障报告：your-llm-gateway.example.com 连接层劣化窗口（2026-07-24 16:42–17:33）

> **致**：AIGC 网关 owner
> **自**：Tofu（自托管 AI 助手）客户端侧
> **性质**：连接层故障协查请求 —— 客户端已自愈，但需要网关侧定位根因

---

## 1. TL;DR

2026-07-24 **16:42–17:33**（服务器本地时间）约 50 分钟内，客户端到
`https://api.openai.com/v1/chat/completions` 的连接出现三类故障，
**同时命中 7 个互不相关的模型**（aws.claude-opus-4.7 / yuju-claude-opus-4.7-evaDaily /
yuju-claude-opus-4.8-evaDaily / aws.claude-opus-4.8 / kimi-k3 /
deepseek-v3.2-doubao/baidu/huawei / deepseek-v4-flash-huawei），
且伴随每分钟 429 限流混战（窗口内 752 次）。跨模型同时段同症状 =
**故障点在网关/代理层，不在某个模型服务器，也不在客户端**。

客户端靠既有四层重试全部自愈（**0 次最终失败**），本报告附精确到秒的事件
样本与恢复锚点 M-TraceId，请协助对照网关侧日志定位。

## 2. 窗口内故障量级（客户端日志，16:40–17:35）

| 故障类 | 次数 | 含义 |
|---|---|---|
| SSE 流式中途断连（`ChunkedEncodingError: Broken pipe`） | 25 | 响应体迭代途中连接被对端掐断（EPIPE） |
| 连接建立/上传阶段失败（`Endpoint unreachable`） | 215 | 新请求连不上 / 请求体传不过去 |
| ├─ 其中：写请求体超时（`write operation timed out`） | 144 | 上传大 prompt 途中卡住超时 |
| └─ 其中：代理层断连（`ProxyError: RemoteDisconnected / Broken pipe`） | 34 | 客户端前置 HTTP 代理也无法转发 |
| 429 每分钟限流 | 752 | 窗口内 key 级配额严重争抢 |
| slot 连续错误冷却 | 最高 **96 连续错误**（aws.claude-opus-4.8）→ 冷却 300s；32→35 连续错误（yuju-claude-opus-4.7-evaDaily） | 客户端熔断统计 |

## 3. 症状分类与原始日志样例

### 3.1 流式中途断连（本报告的核心诉求）

SSE 响应已经开始返回、迭代途中连接被对端掐断。HTTPS 连接上 SSL 层把断连
上报为 EPIPE（与 ECONNRESET 同类，都是对端杀连接）：

```
2026-07-24 17:01:28 [WARNING] lib.llm._transport [run_task-4c989273]:
  [Task 4c989273][R16][D:example_corp_key_0:aws.claude-opus-4.7] ⚠ Transient error (attempt 1):
  ChunkedEncodingError: ("Connection broken: BrokenPipeError(32, 'Broken pipe')",
  BrokenPipeError(32, 'Broken pipe')) — retrying in 3.1s …
```

- 16:46–17:04 共 9 次，**8 次集中在 example_corp_key_0:aws.claude-opus-4.7**，
  但 17:01 前后 kimi-k3 / opus-4.8 也各出现。
- 客户端读超时是 300s，断连不是客户端超时（那会是 `ReadTimeout`）。
- 可疑模式：长 thinking 间隙（几十秒无字节流出）后连接被杀 —— 请优先核对
  SSE 路由的 idle/read timeout 配置。

### 3.2 连接建立/上传阶段失败

```
2026-07-24 17:04:52 [WARNING] lib.llm.stream [run_task-4c989273]:
  [Task 4c989273][R16][D:example_corp_key_0:aws.claude-opus-4.7] ✖ Endpoint unreachable (connect phase)
  https://api.openai.com/v1/chat/completions:
  HTTPSConnectionPool(host='your-llm-gateway.example.com', port=443): Max retries exceeded ...
  (Caused by ProxyError('Unable to connect to proxy', BrokenPipeError(32, 'Broken pipe')))
```

```
2026-07-24 17:05:17 ... [D:example_corp_key_0:yuju-claude-opus-4.7-evaDaily] ✖ Endpoint unreachable
  (connect phase): ('Connection aborted.', TimeoutError('The write operation timed out'))
```

### 3.3 429 限流混战（窗口背景，可能是故障诱因之一）

```
2026-07-24 17:05:04 [INFO] ... 429 rate-limited on example_corp_key_0:yuju-claude-opus-4.7-evaDaily
  — body: API HTTP 429: {"status":429,"message":"App:**8427在模型:yuju-claude-opus-4.7-evaDaily
  每分钟请求次数超过限制","ext":{"error":{"source":"AIGC","service":"aigc","stage":"validation"}}}
```

窗口内 752 次 429；客户端多个任务并发重试放大了压力，但每分钟配额本身
在正常使用强度下频繁触顶，也请一并评估。

## 4. 为什么判定故障在网关/代理层

| 证据 | 推论 |
|---|---|
| 7 个互不相关的模型**同一时段**出现同类断连 | 不是某个模型服务器的问题 |
| 客户端前置代理也报 `ProxyError: RemoteDisconnected` | 代理→网关段链路本身在断 |
| 写请求体超时 144 次 | 不是"模型生成慢"，是**连接通路上传都过不去** |
| 后台 ContentFilter 批量任务（deepseek 系）同样大面积 unreachable | 与交互任务无关的独立流量也被命中 |
| 17:08 起同一批 slot 陆续恢复成功 | 故障有明确的起止窗口，非持续性配置错误 |

## 5. 恢复锚点（供网关日志对照）

窗口后段同一任务恢复成功的请求：

| 时间 | 任务 | 模型 | M-TraceId |
|---|---|---|---|
| 17:08:00 | 4c989273（16:46 起反复断连的那个） | aws.claude-opus-4.7 | `286f285e6ad048d89aaed18514b5862d` |
| 17:08:15 | 05456242 | kimi-k3 | `00e967c21f774eb08029e2b96a2e3580` |
| 17:08:41 | 05456242 | kimi-k3 | `bdf1f86ebb8a4827bc292054dc1c5d66` |

注意：中途断连的请求**没有** M-TraceId 可附（流在 usage 返回前就死了），
请以上面第 3 节的精确到秒时间戳 + 模型/slot 名对照网关侧访问日志。

## 6. 请网关侧排查的清单

1. **SSE 路由的 idle/read timeout**：长 thinking（数十秒无字节）是否触发
   网关主动断连？对照 3.1 的时间戳确认断连发起方。
2. **上游 worker 重启/OOM/发布**：16:42–17:33 窗口内是否有实例重启或发布
   （解释连接被批量杀掉 + 有明确恢复时点）。
3. **LB / conntrack 会话老化**：长连接是否被中间设备静默回收。
4. **代理层健康**：34 次 ProxyError 指向客户端前置代理与网关之间的链路。
5. **429 配额**：每分钟限流在窗口内触发 752 次，是否配额调整或限流器异常。

## 7. 客户端侧已做/可做的缓解（供参考，不要求网关配合）

客户端现有四层自愈，本窗口内 **0 次任务最终失败**：

1. 传输层同 key 重试（5 次，3/6/12/24s 退避）—— 25 次断连全部当场愈合；
2. 调度层 slot 熔断 + 轮换（96 连续错误 → 冷却 300s）；
3. 编排层零字节/过早关闭透明重试 + 强制换 slot；
4. 整轮自动重试（`TURN AUTO-RETRY n/3`）+ 备用模型回退。

附：同日已将重试循环的日志纪律修正（中间尝试不再向错误日志倾倒全栈），
commit `64356c9a`；断连自愈后的内容收敛潜伏问题已立项 `pt_6e12b1ffd95a453e`。

---

## 8. 附录：第二波复发（2026-07-24 22:01–22:48，落笔时仍在窗口内）

首波 17:33 收敛约 4.5 小时后，**同模式故障复发**。复发证据比单点报告更能
说明问题：这是间歇性劣化，不是一次性发布事故。

### 8.1 量级与形状（两波统一改用 ✖ 直连失败行口径，便于横向比较）

| 项 | 第一波（16:42–17:33） | 第二波（22:01–22:48） |
|---|---|---|
| 写请求体超时（`write operation timed out`） | 95 次 | 26 次（另有同事件的 dispatcher failover 随行记录） |
| 受影响 key | 双 key 同模式 | **完全对称**：example_corp_key_0 ×13 / example_corp_key_1 ×13 |
| 受影响任务 | 单会话为主 | **7 个并发任务**（244a8b46 / 89016f44 / fc8ff0d6 / ca427ed1 / b7360769 / 3de85d5a / be3ca45a） |
| 命中轮次 | 高轮次大 prompt | R7–R101，集中高轮次大 prompt（R21 / R40 / R45 / R63 / R72 / R101） |
| 伴随 429 | 窗口内 752 次 | 仅背景量级（约 67 次/小时） |
| 任务终败 | 0 | 0（每次事件后 cooled 30s + 排除 slot 对 + failover，例：22:48:34 sticky 自动 key_1→key_0 重绑） |

注：第 2 节的 144 次为更宽口径（含同一事件的 dispatcher 二次记录行）。

### 8.2 与第一波同族的判定

1. **同一端点**：`https://api.openai.com/v1/chat/completions`。
2. **同一错误签名**：`('Connection aborted.', TimeoutError('The write operation timed out'))`，
   connect/upload 阶段失败（非读超时）。
3. **同一 key 对称性**：两个独立 key 各 13 次 —— 排除单 key 配额/单 key 链路，
   指向 key 上游的**共享通路**。
4. **同一负载相关**：集中命中高轮次（大请求体）回合，小请求时段无事件。
5. **起止窗口明确**：22:01:22 首发，22:48:34 为窗口内最后一次记录。

### 8.3 对第 6 节排查清单的增量信息

- 两波间隔约 4.5 小时、形状一致 → 建议优先核对**周期性因素**（上游 worker
  滚动重启 / 定时任务 / LB-conntrack 会话老化周期），而非一次性发布。
- 第二波**无** 429 混战伴随 → 429 更可能是第一波的背景噪声而非故障诱因，
  排查权重可下调。
- 第二波精确到秒的样本（全部 kimi-k3；每行均可在 `logs/app.log` 直接 grep 复现，供网关访问日志对照）：

  | 时间 | 任务 | 轮次 | key |
  |---|---|---|---|
  | 22:01:22 | 244a8b46 | R21 | key_0 |
  | 22:03:51 | fc8ff0d6 | R45 | key_0 |
  | 22:04:13 | 244a8b46 | R24 | key_1 |
  | 22:15:31 | fc8ff0d6 | R66 | key_0 |
  | 22:15:54 | 89016f44 | R29 | key_1 |
  | 22:46:31 | 89016f44 | R63 | key_0 |
  | 22:48:34 | b7360769 | R40 | key_1（窗口内最后一次） |

---

*数据采集：客户端 logs/app.log + logs/error.log，服务器本地时间。
（22:55 追加 §8：第二波复发附录，落笔时第二波仍在窗口内。
  23:10 勘误 §8.1/§8.3：样本表 3 行原引自 22:2x 时尚存、后被日志轮转/清理
  抹去的 dispatcher 冷却格式行，已全部替换为当前 `logs/app.log` 可 grep
  复现的 ✖ 行；任务计数与随行日志表述同步核正。）*

---

## 9. 附录：第三波（2026-07-25，双形态、首次终端可见失败）

### 9.1 TL;DR

2026-07-25 出现**两种不同形态**的连接层故障：

- **形态 A（写请求体超时，connect/upload 阶段）**：00:00:37–18:06:45 全天背景性
  发生 **594 次**，18:06 后停止；
- **形态 B（读超时，响应阶段 300s 零字节）**：08:08 孤立 1 例后，**18:00–21:39
  晚高峰 39 例**，逐小时升级（4→9→14→12），最后一例 21:39:17。

形态 B 与昨日两波的"全模型均摊"完全不同：**26 例可归属样本中 25 例集中在
yuju-claude-opus-5-evaDaily 单一路由**，且首次造成终端可见失败——**27 个会话
终败**（昨日两波均为 0）。终败暴露的配置缺口：当时部署未配置 fallback 模型
（用户侧报错带 `[no-fallback]` 标签）。21:48:22 配置 fallback=kimi-k3（热加载）；
22:06:47 修正为 aws.claude-opus-4.8（kimi-k3 当日 429 达 469 次，作为 fallback
有撞限流风险）。

### 9.2 两种形态对比（客户端 logs/app.log 口径）

| 项 | 形态 A：写超时 | 形态 B：读超时 |
|---|---|---|
| 时间分布 | 00:00–18:06 全天背景 | 08:08 孤立 1 例 + 18:00–21:39 晚高峰 |
| 次数 | 594 | 40 |
| 失败阶段 | connect/upload（请求体传不过去） | 响应读取（连接建立后 300s 零字节） |
| 模型分布 | 多模型均摊 | 25/26 例 yuju-claude-opus-5-evaDaily |
| 逐小时峰值 | 86/h（15 点） | 14/h（20 点） |
| 最后一例 | 18:06:45 | 21:39:17 |

形态 A 逐小时分布：00 时 72 / 07 时 84 / 08 时 50 / 15 时 86 / 16 时 82 /
17 时 72，其余时段 4–44；形态 B 逐小时：18 时 4 / 19 时 9 / 20 时 14 / 21 时 12。

### 9.3 首次终端可见失败（昨日 0 → 今日 27 会话）

- `LLM call failed at round` 终败涉及 **27 个不同会话**（conv= 去重），用户侧
  报错多带 `[no-fallback]` 上下文；
- 直接原因链：形态 B 持续 3.5h+ → 四层自愈（同 key 重试 / 熔断轮换 / 透明重试 /
  整轮重试）全部耗尽 → 当时 fallback 模型为空 → 错误上屏；
- 配置修复时间线：21:48:22 保存并热加载 fallback=kimi-k3
  （`Config changes applied (hot-reload): [… 'model_defaults' …]`）；
  22:06:47 改配 aws.claude-opus-4.8（`Dispatcher reset — new config active
  immediately`，无需重启）。
- 注意：22:06 后尚无新的主模型失败事件，**fallback 链路已经配置级+进程级验证，
  但未经过实战触发**；首个真实证据需待下一次主模型失败时产生
  （日志签名 `Model fallback: … → aws.claude-opus-4.8`）。

### 9.4 精确样本（供网关访问日志对照，均可在 logs/app.log grep 复现）

| 时间 | 任务 | 模型 | 会话 |
|---|---|---|---|
| 18:22:52 | e9479e2d | yuju-claude-opus-5-evaDaily | ms06h5te2as410 |
| 20:23:20 | c1973fdb | yuju-claude-opus-5-evaDaily | ms0afm46ezao52 |
| 20:29:21 | 4743fe2e | yuju-claude-opus-5-evaDaily | ms0aew1q6qtyi5 |
| 20:46:37 | 65639886 | yuju-claude-opus-5-evaDaily | ms07h4h4k4ol0z |
| 20:57:49 | 65639886 | yuju-claude-opus-5-evaDaily | ms07h4h4k4ol0z |
| 21:32:47 | a19b44b8 | yuju-claude-opus-5-evaDaily | ms06h5te2as410 |

注：终败读超时均为"300s 零字节"（客户端读超时 300s 主动放弃），**不是**对端
RST —— 与 §3.1 的 EPIPE 断连签名不同，请网关侧按"请求已受理但长时间无产出"
口径检索。

### 9.5 对第 6 节排查清单的增量信息

1. **新形态指向单路由容量**：形态 B 的 25/26 集中于
   yuju-claude-opus-5-evaDaily —— 优先核对该路由上游 worker 排队/僵死
   （请求被接受但 ≥300s 无 token 产出 = 典型上游排队或卡死）。
2. **形态 A 是全天背景**：594 次写超时跨 18 小时分布、18:06 后停止，与形态 B
   无时间重叠 —— 两者可能是独立问题，建议分开定位。
3. **间隔规律被打破**：昨日两波间隔 4.5h；今日全天背景 + 晚高峰连续 3.5h ——
   劣化在加重而非收敛。
4. **429 配额**：kimi-k3 当日 429 达 469 次（example_corp_key_0:430 / key_2:39），
   每分钟配额在正常使用强度下频繁触顶，请一并评估。

---

*22:10 追加 §9（服务器本地时间）：第三波双形态附录。形态 B 窗口（18:00–21:39）
结束后至落笔约 30 分钟无新事件；按前两波的复发规律，不能排除夜间再次复发。*

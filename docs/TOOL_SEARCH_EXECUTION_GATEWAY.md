# Tool Search 与稳定执行网关

本文记录 Tofu 的任务级工具发现和执行约束。核心原则是：模型可见集合不是权限集合，搜索结果也不是授权凭证；本地搜索通过固定的 `execute_tools` 网关执行命中工具，搜索前后不改变 wire tools。

## 三个集合

- `executable_tool_catalog`：当前任务真实可执行的完整服务端目录。默认包含环境、连接、租户和 MCP 策略允许的全部工具；Composer 开关只控制曝光，不从这里删工具。兼容模式 `selected_only` 才沿用“显式开启即授权”。
- wire tools：本轮实际发给模型的 schema。`eager` 工具直接可见，`searchable` 工具可以被本地或 Provider 原生 Tool Search 延迟发现。
- MCP active tools：Tofu 在调用模型前从 MCP allowed catalog 检索出的当前任务子集。只有该子集的原生 MCP schema 进入 wire tools；allowed catalog 仍是执行权限上限。

wire tools 只描述“模型当前看见什么”；执行权限始终由任务级 `executable_tool_catalog` 判定。因此 searchable 工具不需要动态加入 wire schema：本地搜索拿到服务端名称和参数 schema 后，通过固定可见的 `execute_tools` 调用。历史模型直接发出原始名称时仍可准入；搜索是教学和检索机制，不是调用凭证。

每一轮都会从当前配置重新生成 wire catalog、direct/deferred policy、namespace、目录规模和 Tool Search 模式。Provider 转换使用本轮投影生成最终 tools 数组；工具开关、MCP 目录和项目状态在下一轮直接生效。`executable_tool_catalog` 同样按本轮环境生成并用于本地搜索和执行准入；组装器从已有 authority 一次建立名称索引并随追加更新，动态大目录不会为每个工具重新扫描正在增长的列表。request-owned ToolContract 文档仍逐轮隔离复制；普通 JSON 字典/列表走带 memo 的专用克隆，非标准扩展类型回退 `deepcopy`，不会因提速而共享可变 schema 或引入进程级权限缓存。本地检索异常后的 fail-open 会恢复更完整的 schema，此时可用性优先。

## 对模型公开的本地搜索

本地模式固定增加发现与执行两个工具。搜索形态为：

```json
{
  "query": "编辑学城文档",
  "namespace": "xuecheng",
  "limit": 8,
  "cursor": null
}
```

`search_tools` 只检索当前 `executable_tool_catalog` 中**尚未向该任务披露**的成员，并返回其原始注册名、namespace、有界摘要、紧凑参数 schema、`execute_with: "execute_tools"` 和简短执行提示。披露集合合并两类身份：最近一次有界 `tool_wire_projection.toolNames`（Provider 实际收到的最终投影），以及本任务此前搜索页已经返回的 `(精确工具名, 当前完整 catalog schema 指纹)`。排除发生在打分和分页之前，所以同一 schema 即使匹配不同查询，也不会在后续轮次、后续页或故障降级目录中重新出现；schema 修订后指纹变化，工具可再次披露。该状态仅在运行中任务内保留，最多 512 个不含原 schema 的身份，不是注册、导入、授权或跨任务用户偏好。精确搜索命中两类已披露身份时行为不同：wire 可见工具返回空 `items`、`already_visible` 和“直接调用”的停止提示；仅搜索披露（不在 wire）的工具返回 `already_disclosed` 并重新附上该工具的完整条目——压缩可能已丢弃早先的披露页，拒绝重发只会让模型猜测参数名（事故会话 mtgzs6bnmglpfa 因此把 `task_id` 猜成 `epic_id`），此时提示明确“不在直接工具列表，走 execute_tools”。宽检索仍然省略两类身份；零结果且本任务已有披露时，提示会说明已披露工具被省略、可按精确名重新查看 schema。普通语义搜索不复制完整 help/errors；精确名称（含唯一维护别名）命中才返回最多 1,200 字符 help 和 8 个紧凑错误条目。完整 JSON 页硬限 24,000 字符，超出时从最低排名尾部分页，参数名和 required 字段不截断。下一步模型把精确名称与匹配 schema 的参数交给 `execute_tools`；搜索不会签发 admission receipt，也不会扩大 executable catalog。

普通调用使用：

```json
{
  "calls": [{"name": "read_files", "arguments": {"path": "README.md"}}],
  "execution": "auto"
}
```

搜索不是 admission receipt，也不是执行前置条件：如果模型已经知道任务目录中的精确工具名并给出符合该工具 schema 的参数，可以直接使用 `execute_tools.calls` 或 ToolScript；网关仍会重新校验名称、schema、权限与审批。

有数据依赖的调用可使用有界 ToolScript：`catalog.search(...)`、`tools.call(...)`、`tools.callMany(...)` 和 `tools.parallel(...)`。ToolScript 可调用 executable catalog 中的任何真实工具，包括写入或需要审批的工具；这些子调用仍进入原权限、审批、串行化和执行流水线。解释器没有 `eval`、import、宿主文件系统、进程或直接网络能力。

本地索引使用字段加权 BM25，并在索引侧做有限的中英文概念归一（包括下载、保存、复制、导出、安装、解压、登录、认证、授权及其中英文近义表达）。认证属于动作意图：查询与工具名同时命中该概念时获得动作名加权，因此“登录/授权/NOT_LOGGED_IN”查询会把 login/auth 工具排在泛化的文档搜索工具之前。每个
`ToolSpec` 可以通过 `search_hints` 提供按函数区分的 aliases/intents；MCP 则复用
`_meta.aliases` / `_meta.intents`。这些 sidecar 只参与服务端排序，不进入 tools
数组，也不会出现在结果或前端卡片中。规范化词项使用有界 LRU；缓存的是索引词，
不是权限或执行结果。

`search_tools` 与 `execute_tools` 的 schema 在本地模式中固定存在。首次部署会改变一次缓存前缀，但搜索结果和后续执行不会修改 tools 数组；稳定目录内的后续轮次保持相同 tools 字节。每个嵌套调用都会重新按本任务 executable catalog 校验，并照常经过审批、hook 和执行流水线，所以网关不能绕过权限边界。

网关会修正常见外壳、JSON 字符串参数、参数别名、schema default、大小写 enum、标量数组以及数字/布尔字符串。工具名按精确名称、namespace、维护别名和高置信模糊名称解析；模糊修复要求相似度至少 `0.90` 且领先第二名至少 `0.15`。缺少无 default 的必填参数、含义不明的 enum 或接近候选会结构化拒绝并返回候选与 `retry_hint`。写工具的名称修复不会绕过审批。

已验证 Provider 的 hosted PTC 继续使用原生协议。本地 Tool Search 的 ToolScript 只从 `execute_tools.program` 参数进入；Tofu 不会从 assistant 普通文本中提取代码并执行。

## Provider 策略

- 已验证的 OpenAI Responses 使用原生 Tool Search；Hosted PTC 能力保持原路径。
- 已验证的 Anthropic Messages 使用 hosted BM25 Tool Search 和 `defer_loading`。包含 `server_tool_use` / `tool_search_tool_result` 的 assistant blocks 会完整保存并在下一轮回放。
- Codex OAuth、旧 Claude 和其他兼容 Provider 默认使用本地 `search_tools`，命中后通过固定 `execute_tools` 执行。
- 非官方端点只有在真实能力探测明确为支持时才启用原生模式，不能仅按模型名升级。
- 原生请求因 Tool Search 字段被 400/404/422 拒绝时，同一请求降级到本地模式；无关请求错误不降级。本地检索异常时会标记下一轮 fail-open 到完整 executable catalog；当前轮的恢复目录仍只列未直接可见成员，避免以降级为由重复展示 wire tools。
- 小目录直接发送完整原生 schema，不额外注入任何 gateway 工具。即使目录较大，只要所有 schema 都已 eager 可见，也不会注入无意义的搜索工具。

设置 `tools.toolSearch` 支持 `auto | native | local | off`；`tools.executionScope` 支持默认的 `available` 和兼容模式 `selected_only`。前者使 Composer 开关表达“常驻可见/按需发现”，后者才表达旧式执行 allowlist。

## 搜索结果展示

`search_tools` 的模型结果保持机器可读 JSON；前端事件额外携带最小展示投影：工具名、namespace、描述和参数摘要。界面以卡片明确列出本次命中的工具，并标识必填参数、更多分页结果和 fail-open 状态。`execute_tools` 是纯协议适配层，不显示、不计入工具数；真实子工具照常显示成功、错误和审批。子调用尚未生成生命周期事件就被参数校验拒绝时，活动时间线显示“目标子工具已跳过”、稳定错误码、原因和重试提示，而不是显示 `execute_tools 执行失败`；已执行子工具的错误事件始终优先，外壳不得重复报错。冷回放前端会从对应 round 的有界 `toolContent` 恢复旧版校验诊断，无法恢复的外壳行才直接过滤。MCP 私有 `_meta`、完整索引字段和权限信息不会进入展示投影。

规范化只接受确定的一对一修复，且 live catalog 中的精确名称永远优先。例如仅有大结果续读工具时，`read_artifact(ref=...)` 可规范化为 `read_tool_artifact(artifact_ref=...)`；若真正的 `read_artifact` 同时可用，则不改名。所有修复写入不含参数值的审计记录，最终仍按请求自有 schema 校验。

## MCP 协作协议

Tofu 连接建立时拉取并缓存完整 `tools/list`，后续任务检索只读缓存，不逐轮重新请求。内部 ID 始终采用 `mcp__{server_id}__{tool_name}`，模型名称到 MCP server/tool 的映射可逆。

索引会读取工具 `_meta` 中的以下字段，但不会把这些私有检索元数据发给模型：

```json
{
  "bundle": "edit_document",
  "requires": ["prepare_doc_edit"],
  "profiles": ["editor"],
  "intents": ["编辑学城文档"],
  "aliases": ["学城", "Xuecheng", "km"],
  "risk": "write",
  "schemaHash": "sha256:...",
  "catalogVersion": "..."
}
```

服务端未提供 `schemaHash` 时，Tofu 对规范化后的 name、description、input schema 和 annotations 计算 SHA-256。缓存指纹包含 server、catalogVersion、工具名、schemaHash 以及检索元数据；目录未变化时复用同一索引。

MCP 预请求检索在 catalog build 时生成确定性倒排 postings，查询只遍历实际命中的 posting，排序公式与旧逐工具扫描完全一致。索引是可重建 LRU，其容量从 `TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY` 派生：lean fallback 4、8 GiB reference 8、distributed/硬上限 32；该进程级容量只解析一次，启动物化或操作员提供的合法正值先执行硬上限夹紧，不为未使用的默认值重复探测文件系统/cgroup。每个索引同时预计算 read-first fallback 顺序与可触发 exact/alias/intent boost 的最大查询长度；fallback tuple 每个工具只增加一个指向既有名称的引用，低信号请求不再重复风险归一/排序，严格长于上界的完整 query 不再扫描所有工具。含可检索词项的 phrase boost 只访问 postings 已命中的工具；候选达到目录的 7/8 时改用连续 tuple 扫描，避免密集字典查找，完全没有词项的标点查询继续全扫以保留 `++` 等 intent 子串。淘汰只导致下次从 bridge 的权威 catalog 重建，不改变允许集合。owner/conversation sticky state 同样派生为 1,024 / 2,048 / 4,096 条，保留不超过 24 小时，并在每个写路径（包括小 catalog 直接发送后的工具调用）执行严格上限。state 只保存一个 64-byte 的原始 query SHA-256 与规范化词项 SHA-256 组合、最多 32 个已用工具名与 64 个 active 工具名；原始摘要命中会在 Python 分词前复用稳定顺序，未命中仍用规范化摘要保留标点等价，旧十六进制状态也可读。超长 scope key 同样摘要化，不保留用户长文。每次 touch 使用进程单调时钟并把条目移到 LRU 尾部，TTL 清理只弹出有序的过期前缀并检查一个最老存活条目，系统时间回拨不会破坏顺序。内存压力可清空两者，下一轮从权威 catalog 与最近一轮有界工具历史恢复。

bridge 还按目录代际缓存一个只读的稳定顺序引用元组、内容指纹和私有检索文本 mapping；模型定义、预请求检索、通用 Tool Search sidecar 和公开快照共享这一派生源，不再每轮各自排序、复制、拼接并重新哈希。目录替换、真实断连或禁用集合有效变化会原子失效；重复配置写入和保留目录的 stdio parking 不会制造重建。共享检索 mapping 不可变，公开快照仍返回独立行与元数据字典；旧 bridge 缺少任一快路径时用同一纯投影函数回退，检索异常仍 fail-open 到完整允许目录。

默认最多选择 8 个基础工具。已调用工具和仍相关工具保持原有顺序；明显意图变化时才轮换未使用工具。`bundle` 同伴占普通名额，`requires` 是硬依赖，可以使最终集合超过 8，并且依赖会排在依赖方之前。

如果 MCP 声明 `tools.listChanged=true`，Tofu 注册 notification handler；收到 `notifications/tools/list_changed` 后只重新拉取该 server 的目录并使相关索引失效。任务上的已调用记录会保留，再与新的 allowed catalog 求交集。

`hope-mcp` 和 `xuecheng-mcp` 的 profile 环境变量仍定义服务端允许上限，例如 `XUECHENG_TOOLSET=reader,editor`。只有上限变化需要重启连接；每任务 active 选择由 Tofu 完成。

冻结模拟用户语料和实验执行代码位于 `evaluations/tool_search/`；下载语料固定包含“把浏览器页面里的最新版压缩包下载到服务器本地”。报告同时记录模型改写 query 的召回、原始用户意图召回和本地检索 mean/p95/max 延迟，避免只优化模型事后造出的关键词。文档只保留当前执行契约，不复制一次性的对比结论。

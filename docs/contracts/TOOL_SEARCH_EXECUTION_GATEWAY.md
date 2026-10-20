# Tool Search 与稳定执行网关

本文记录 Tofu 的任务级工具发现和执行约束。核心原则是：模型可见集合不是权限集合，搜索结果也不是授权凭证；本地搜索通过固定的 `execute_tools` 网关执行命中工具，搜索前后不改变 wire tools。

自进化执行阶段保留 `evolution_coordinate` 和 `evolution_source` 的直接
展示；探索、材料、教学和重建索引工具可按需发现，仍属于原执行目录。
Problem: 每轮重复展示较大的探索 schema 增加输入开销。
Decision: 使用现有 searchable 策略和固定网关，协调入口保留 direct 声明。
Alternative: 删除执行工具或缩小权限目录会损失长期研究能力，故不采用。
Schema 缩减本身不证明任务完成率、模型轮数或实际总成本改善。

## 三个集合

- `executable_tool_catalog`：当前任务真实可执行的完整服务端目录。默认包含环境、连接、租户和 MCP 策略允许的全部工具；Composer 开关只控制曝光，不从这里删工具。兼容模式 `selected_only` 才沿用“显式开启即授权”。
- wire tools：本轮实际发给模型的 schema。`eager` 工具直接可见，`searchable` 工具可以被本地或 Provider 原生 Tool Search 延迟发现。
- MCP active tools：Tofu 在调用模型前从 MCP allowed catalog 检索出的当前任务子集。只有该子集的原生 MCP schema 进入 wire tools；allowed catalog 仍是执行权限上限。

wire tools 只描述“模型当前看见什么”；执行权限始终由任务级 `executable_tool_catalog` 判定。因此 searchable 工具不需要动态加入 wire schema：本地搜索拿到服务端名称和参数 schema 后，通过固定可见的 `execute_tools` 调用。历史模型直接发出原始名称时仍可准入；搜索是教学和检索机制，不是调用凭证。

每一轮都会从当前配置重新生成 wire catalog、direct/deferred policy、namespace、目录规模和 Tool Search 模式。Provider 转换使用本轮投影生成最终 tools 数组；工具开关、MCP 目录和项目状态在下一轮直接生效。`executable_tool_catalog` 同样按本轮环境生成并用于本地搜索和执行准入；组装器从已有 authority 一次建立名称索引并随追加更新，动态大目录不会为每个工具重新扫描正在增长的列表。request-owned ToolContract 文档仍逐轮隔离复制；普通 JSON 字典/列表走带 memo 的专用克隆，非标准扩展类型回退 `deepcopy`，不会因提速而共享可变 schema 或引入进程级权限缓存。本地检索异常后的 fail-open 会恢复更完整的 schema，此时可用性优先。

## 对模型公开的本地搜索

**Namespace alias decision:** Models can confuse MCP wire-name prefixes with server IDs. The schema recommends a bare server/category (for example `llm`) or omitting an uncertain filter. Search accepts `mcp_llm` and `mcp__llm` as aliases for `llm`, generically for any server present in the task catalog. An exact catalog namespace wins before prefix stripping; an alias must resolve to a matching MCP tool and canonical server namespace. Case/outer whitespace normalization, visibility/disclosure exclusions, ranking and pagination remain unchanged. Unknown prefixes/names retain the empty-result recovery hint. Fuzzy namespace matching or silently dropping an unknown filter was rejected because it can return a different server's tools instead of the requested scope.

本地模式固定增加发现与执行两个工具。搜索形态为：

```json
{
  "query": "编辑学城文档",
  "namespace": "xuecheng",
  "limit": 8,
  "cursor": null
}
```

namespace 是搜索域：过滤时只保留该 namespace 下的工具，未过滤时它随结果返回。MCP 工具的 namespace 不是统一的 `'mcp'` 桶，而是其所属 **server id**（从 `mcp__{server_id}__{tool_name}` 解析；解析不到时回退 spec category）。因此 `namespace="xuecheng"` 精确命中该 server 的全部工具，`available_namespaces` 也会列出真实 server 名（`xuecheng`、`llm`、`overleaf`…）。`'mcp'` 仍保留为伞形别名：以 `mcp__` 开头的工具都匹配它，所以旧的 `namespace="mcp"` 过滤继续有效。过滤命中空目录且非精确名查找时，响应附带 `available_namespaces` 并提示改用真实 server 名或不带过滤重试，避免模型把“namespace 不存在”误判为“能力缺失”。

`search_tools` 只检索当前 `executable_tool_catalog` 中**尚未向该任务披露**的成员，并返回其原始注册名、namespace、有界摘要、紧凑参数 schema、`execute_with: "execute_tools"` 和简短执行提示。披露集合合并两类身份：最近一次有界 `tool_wire_projection.toolNames`（Provider 实际收到的最终投影），以及本任务此前搜索页已经返回的 `(精确工具名, 当前完整 catalog schema 指纹)`。排除发生在打分和分页之前，所以同一 schema 即使匹配不同查询，也不会在后续轮次、后续页或故障降级目录中重新出现；schema 修订后指纹变化，工具可再次披露。该状态仅在运行中任务内保留，最多 512 个不含原 schema 的身份，不是注册、导入、授权或跨任务用户偏好。
除 `(名称, schema 指纹)` 外，每条 ledger 身份还携带一份有界参数签名（required/optional 参数名，各至多 16 个、单名截断 64 字符，不含任何参数值），供压缩后的上下文恢复使用。
精确搜索命中两类已披露身份时行为不同：wire 可见工具返回空 `items`、`already_visible` 和“直接调用”的停止提示；仅搜索披露（不在 wire）的工具返回 `already_disclosed` 并重新附上该工具的完整条目——压缩可能已丢弃早先的披露页，拒绝重发只会让模型猜测参数名（事故会话 mtgzs6bnmglpfa 因此把 `task_id` 猜成 `epic_id`），此时提示明确“不在直接工具列表，走 execute_tools”。

披露是一次性投递，但 ledger 是持久的：搜索页本身会随轮次老化、被压缩摘要吞掉，而任务上的 ledger 不会。Composer 因此在每轮尾部注入 `disclosed_tools` 块（`tools.disclosure`，placement=tail，≤700 tokens），从 ledger 恢复“已披露但不在 wire”的 deferred 工具名单及其参数签名（至多 16 条、每条至多 8 个参数名），并明确指示“用 execute_tools 按精确全名调用，不要重跑宽检索；需要完整 schema 时用单个精确名搜索重新披露”。块跳过 wire 已可见或已不在 executable catalog 的名字；ledger 不可用时静默省略（`state_unavailable`），不阻断组装。该块只恢复名字与签名，不重新渲染完整 schema——精确名搜索的 `already_disclosed` 路径仍是唯一的按需全文重披露通道。
宽检索仍然省略两类身份；零结果且本任务已有披露时，提示会说明已披露工具被省略、可按精确名重新查看 schema。
handler 侧还对同一任务内逐字节相同的搜索计数：第二次起结果带 `repeated_query` 字段，宽检索/零结果检索的 notice 替换为破循环指令（目录不变，重搜不会改变结果——直接行动或本轮视为无此能力）；但精确名恢复（`already_disclosed` / `already_visible` / `resolved_name`）是压缩后的合法找回，保留原恢复提示不被破循环文案覆盖。
普通语义搜索不复制完整 help/errors；精确名称（含唯一维护别名）命中才返回最多 1,200 字符 help 和 8 个紧凑错误条目。完整 JSON 页硬限 24,000 字符，超出时从最低排名尾部分页，参数名和 required 字段不截断。下一步模型把精确名称与匹配 schema 的参数交给 `execute_tools`；搜索不会签发 admission receipt，也不会扩大 executable catalog。

普通调用使用：

```json
{
  "calls": [{"name": "read_files", "arguments": {"path": "README.md"}}],
  "execution": "auto"
}
```

搜索不是 admission receipt，也不是执行前置条件：如果模型已经知道任务目录中的精确工具名并给出符合该工具 schema 的参数，可以直接使用 `execute_tools.calls` 或 JavaScript 程序；网关仍会重新校验名称、schema、权限与审批。

数据处理使用 `execute_program({code})`，其中 `code` 是异步 JavaScript 函数体。`await tools.exact_tool_name(args)` 或 `await tools.invoke(name, args)` 返回解码后的完整数据；`await tools.call(...)`、`await tools.callMany(...)` 和 `await tools.parallel(...)` 返回收据。`catalog.search(...)` 只检索 schema。JavaScript 可调用 executable catalog 中的任何真实工具，包括写入或需要审批的工具；这些子调用仍进入原权限、审批、串行化和执行流水线。程序运行在独立 WASM 实例内；动态 JS 求值仍在相同实例中。程序没有 import、定时器、宿主文件系统、进程或直接网络能力。

JavaScript 在执行端处理循环、分支、筛选和结果组合，仅把有界证据交给模型。无依赖的调用使用 `calls[]` 或 `await tools.parallel(calls)`；需要模型逐步理解结果的任务可以直接调用工具。遇到无法确定的语义决策时，`tools.observe(reason, data)` 立即结束整个程序（包括嵌套循环和回调），返回判断原因及证据，后续调用不执行。它是成功交回模型，不是异常重试，也不保留隐式续执行栈；下一程序使用已有证据，不能重复先前副作用。

`tools.value(receipt)` 检查 `status === "done"` 且 `output_truncated === false`，返回解析后的 JSON 或普通文本。失败、缺失或截断的输出会自动交回模型，不能进入下游调用。需要有意恢复失败时，仍可直接检查原始回执。程序记录实际调用阶段、显式并行阶段、内部交付字节及最终结果字节；这些计数不等于已验证的模型往返、token、费用或延迟收益。

`catalog.search({query, namespace?, limit?, cursor?})` 使用 `search_tools` 的参数契约；也接受位置参数 `catalog.search(query, namespace="", limit=8, cursor="")`。对象形式不可混用额外位置参数；未知字段、错误类型、空查询和越界参数返回带重试提示的解释器错误，不能把对象字符串化后继续检索。目录查询不计为真实子工具调用；已知精确工具名和 schema 时不必先搜索。

`tools.call(name, arguments)` 返回回执而非裸业务对象：成功为 `status: "done"` 和 `output`，失败提供 `error`；`output_truncated` 表示内部结果是否不完整。JSON 文本通过 `JSON.parse(receipt.output)` 读取，不对普通文本盲目解析。以下示意使用已知 schema 的两个测试工具，在一次 `execute_program.code` 中完成两步依赖：

```js
const first = tools.call("lookup_record", {key: "example"});
if (first.status != "done" || first.output_truncated) return first;
const record = JSON.parse(first.output);
const second = tools.call("read_details", {record_id: record.id});
if (second.status != "done" || second.output_truncated) return second;
return JSON.parse(second.output);
```

搜索结果的 `ptc_eligible` 仅描述 hosted OpenAI PTC 的审核资格，不是本地 JavaScript 的权限开关；本地执行仍逐子调用校验任务目录、schema、权限与审批。

本地入口的启用同样独立于 hosted 白名单：`on` 在任务目录有实际 function 工具时常驻；`auto` 还要求有界数据处理意图，或已审核读工具的观察到的 fan-out。仅有搜索/执行网关或没有 function 的目录不自我激活。纯 MCP、自定义工具和写工具目录都可获得本地入口，但未审核工具不会因此获得 hosted `allowed_callers`；官方 Responses 没有审核工具时同样走本地后端。

**Problem / Decision / Alternatives:** hosted 白名单曾同时控制本地曝光，导致自定义工具的完整合法目录没有程序入口。现在按任务目录判定本地可用性，按显式审核判定 hosted 可用性。把第三方工具自动加入 hosted 白名单会扩大上游执行资格，维持旧门槛则阻断合法本地任务，两者都被拒绝。

本地索引使用字段加权 BM25，并在索引侧做有限的中英文概念归一（包括下载、保存、复制、导出、安装、解压、登录、认证、授权及其中英文近义表达）。认证属于动作意图：查询与工具名同时命中该概念时获得动作名加权，因此“登录/授权/NOT_LOGGED_IN”查询会把 login/auth 工具排在泛化的文档搜索工具之前。每个
`ToolSpec` 可以通过 `search_hints` 提供按函数区分的 aliases/intents；MCP 则复用
`_meta.aliases` / `_meta.intents`。这些 sidecar 只参与服务端排序，不进入 tools
数组，也不会出现在结果或前端卡片中。规范化词项使用有界 LRU；缓存的是索引词，
不是权限或执行结果。

`search_tools` 与 `execute_tools` 的 schema 在本地模式中固定存在。首次部署会改变一次缓存前缀，但搜索结果和后续执行不会修改 tools 数组；稳定目录内的后续轮次保持相同 tools 字节。每个嵌套调用都会重新按本任务 executable catalog 校验，并照常经过审批、hook 和执行流水线，所以网关不能绕过权限边界。

网关会修正常见外壳、JSON 字符串参数、参数别名、schema default、大小写 enum、标量数组以及数字/布尔字符串。批量数组参数（如 `searches`/`reads`/`urls`）还有两条确定性规则：条目缺少与顶层单数参数同名同型（string）的必填键时，把顶层值拷贝进该条目（`batch_item_promote`，值是模型自己已提供的，不是捏造的）；拷贝后仍缺必填键的条目被丢弃并记录 `drop_invalid_batch_item`，其余有效条目照常执行，全部无效时维持 `missing_required_arguments` 硬错误。工具名按精确名称、namespace、维护别名和高置信模糊名称解析；模糊修复要求相似度至少 `0.90` 且领先第二名至少 `0.15`。缺少无 default 的必填参数、含义不明的 enum 或接近候选会结构化拒绝并返回候选与 `retry_hint`。写工具的名称修复不会绕过审批。

已验证 Provider 的 hosted PTC 继续使用原生协议。新模型请求通过独立的 `execute_program.code` 入口提交本地 JavaScript；已有 `execute_tools.program` 调用继续沿同一结算路径处理；Tofu 不会从 assistant 普通文本中提取代码并执行。

**精确整数传输（Problem / Decision / Alternatives）：** JSON 工具数据中的 64 位 ID 经普通 JavaScript `Number` 解码会静默舍入，使后续调用命中错误对象。私有传输包用带路径的十进制字符串保存超出 `Number.MAX_SAFE_INTEGER` 的整数，再在隔离实例中恢复为 `BigInt`；安全整数、浮点数、布尔值和原有字符串保持原类型。`tools.invoke` / `tools.value`、结构化 receipt 和 `catalog.search` 共用此边界，BigInt 子调用参数恢复为精确 JSON 整数并照常接受 schema/权限校验。独立 BigInt 聚合结果与 `tools.observe` 数据显示为精确十进制字符串；原始 receipt 仍保持宿主整数和 canonical 身份。需要自行 `JSON.stringify` 时显式 `String(value)`。Alternatives—静默舍入破坏数据依赖；把所有数值转成字符串改变正常 schema；一律把大整数交回模型则阻断本可精确执行的依赖链。普通 `JSON.parse` 保留 JavaScript 原生数值语义；完整 receipt 使用 `tools.value`。续读拼接出的 JSON 文本使用同步 `tools.parseJSON(text)`：原始整数 token 超出安全范围时转为 BigInt，安全整数和小数/指数 token 保持 Number，字符串保持字符串；这不是任意精度十进制解析。语法错误可在 guest 捕获，非字符串输入拒绝，不能据此推断来源完整性或 canonical 身份。它使用固定引擎的 reviver source，不新增宿主操作，沿用 guest heap/fuel 边界；Alternatives—每段程序重写 reviver 重复开销并易错，新增 host parser 扩大跨边界传输。 在本机 2,400 行、20 次交替热运行中，普通/精确文本解析中位时延为 15.26/26.99 ms，WASM 线性内存为 1.625/1.938 MiB；普通解析的 ID 已舍入，此测量是精度成本，不是模型收益或 RSS。 在本机固定 2,400 行、每行一个 64 位 ID 的 20 次热运行中，精度修复前/后的中位时延为 22.46/33.04 ms，WASM 线性内存为 2.50/3.31 MiB；此差异是精确转换的本地成本，不是模型端收益。仅发生整数转换的容器才复制，传输路径在本次交付后释放；私有 wire 随探测派生 heap 缩放；正常返回值交给结果预算与完整 artifact 交付，模型 handoff 使用相同完整结果交付。

## Provider 策略

- 已验证的 OpenAI Responses 使用原生 Tool Search；Hosted PTC 能力保持原路径。
- 已验证的 Anthropic Messages 使用 hosted BM25 Tool Search 和 `defer_loading`。包含 `server_tool_use` / `tool_search_tool_result` 的 assistant blocks 会完整保存并在下一轮回放。
- Codex OAuth、旧 Claude 和其他兼容 Provider 默认使用本地 `search_tools`，命中后通过固定 `execute_tools` 执行。
- 非官方端点只有在真实能力探测明确为支持时才启用原生模式，不能仅按模型名升级。
- 原生请求因 Tool Search 字段被 400/404/422 拒绝时，同一请求降级到本地模式；无关请求错误不降级。本地检索异常时会标记下一轮 fail-open 到完整 executable catalog；当前轮的恢复目录仍只列未直接可见成员，避免以降级为由重复展示 wire tools。
- 小目录直接发送完整原生 schema，不额外注入任何 gateway 工具。即使目录较大，只要所有 schema 都已 eager 可见，也不会注入无意义的搜索工具。

设置 `tools.toolSearch` 支持 `auto | native | local | off`；`tools.executionScope` 支持默认的 `available` 和兼容模式 `selected_only`。前者使 Composer 开关表达“常驻可见/按需发现”，后者才表达旧式执行 allowlist。

## 浏览器能力可用性

**Problem:** 扩展断线使工具消失，模型无法区分未知名称、暂不可用和任务策略限制。**Decision:** `lib/browser/availability.py` 从 owner-scoped bridge registry 只读投影连接证据，注册器、每轮 `environment`、普通/程序内本地搜索共用此来源。搜索返回 `capability_availability`（`capability`、`namespace`、`available`、`reason_code`、`message`、`recovery_hint`，精确已知工具额外返回 `tool_name`）。原因分别为 `browser_extension_disconnected`、已连接但目录不允许的 `browser_tools_outside_task_catalog`、缺少身份的 `browser_owner_required` 和探测失败的 `browser_extension_status_unknown`；不附可调用 schema、不授予权限、不暴露客户端或其他用户。服务端无法读取未连接扩展的配置地址，只建议检查服务地址和认证，不断言地址填错。独立的服务端页面预览不依赖扩展。目录逐轮更新，连接恢复后下一轮重新判断；重复查询计数按目录名称指纹隔离。精确名（含单个前导 `+`）缺失返回零候选，不拆词推荐；namespace 是否存在按完整目录判定，不受可见/已披露排除影响。前端只展示同一结果的 `tool_catalog_availability` 最小原因/恢复投影，不另查连接真值。**Alternatives:** 仅前端告警无法让模型感知；给未连接工具 schema 混淆权限；查询全局客户端越过用户边界；断言地址填错缺少证据，均不采用。

## 搜索结果展示

`search_tools` 的模型结果保持机器可读 JSON；前端事件额外携带最小展示投影：工具名、namespace、描述和参数摘要。界面以卡片明确列出本次命中的工具，并标识必填参数、更多分页结果和 fail-open 状态。`execute_tools` 是纯协议适配层，不显示、不计入工具数；真实子工具照常显示成功、错误和审批。子调用尚未生成生命周期事件就被参数校验拒绝时，活动时间线显示“目标子工具已跳过”、稳定错误码、原因和重试提示，而不是显示 `execute_tools 执行失败`；已执行子工具的错误事件始终优先，外壳不得重复报错。未生成子调用的程序错误保留实际 `execute_tools` 身份及错误消息、恢复建议，不再显示已退役的 ToolScript 名称。冷回放前端会从对应 round 的有界 `toolContent` 恢复旧版校验诊断，无法恢复的外壳行才直接过滤。MCP 私有 `_meta`、完整索引字段和权限信息不会进入展示投影。

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

## First-class code action

### Local interface selection

**Problem:** A resident JavaScript action made every model reason about code,
async calls, result shapes and private handles even for ordinary tool use.
The observed empty-input `.ref` failure was one avoidable authoring failure;
better diagnostics alone cannot establish a model's JavaScript proficiency.

**Decision:** `tools.programmaticCalling` defaults to `auto`. Local routes keep
ordinary tools and use JSON `execute_tools.calls` when batching is selected;
discovery uses the same JSON gateway. Independent calls with known arguments
can run in parallel. Sequential batching preserves order but does not substitute
prior results into arguments: the model must inspect a prerequisite result
before supplying dependent arguments. Both interfaces use the same task
catalog, contracts, approvals, replay ledger and execution pipeline.

**Program scope in model guidance.** Problem: independently shortened code and
legacy-program descriptions dropped different execution constraints. Decision:
both program schema factories prepend one shared scope statement: task
permissions and approvals still apply; the guest has no ambient imports,
timers, files, processes or network, and external effects use `tools.*`.
Authoring guidance remains separate, and the JSON-only batch description keeps
its own surface. This describes existing execution behavior; it adds no
permission gate. Alternatives rejected: restoring one draft alone leaves the
other entry point able to drift; separate copies repeat the same ownership
problem. Verify `test_program_schema_scope.py`, program policy and guest tests.

Each gateway child is an explicit occurrence, including identical calls in one
batch. Problem: applying the physical provider-response repetition guard to
this host-constructed batch discarded later children and manufactured empty
successful receipts. Decision: the shared parser has a host-only repetition
policy; gateway batches retain every child while provider responses keep their
existing guard. Ordinary contract/authority checks and freshness-aware read
reuse still apply. Alternatives rejected: copying the first receipt invents
execution evidence for writes; dropping receipts silently changes batch shape.
Verify `test_gateway_child_occurrences.py` and `test_tool_call_flood_admission.py`.

Explicit `on` enables the local `execute_program` action alongside ordinary
tools. Where discovery already exposes `execute_tools`, it remains a JSON-only
recovery path. Existing explicit `on` settings remain explicit; only absent or
invalid settings default to `auto`. `TOFU_PTC_TIER=program|batch` remains an
operator/evaluation override; `off` wins over it. A batch override also disables
hosted programs. Auto may use provider-native PTC only through the existing
selected-route support and reviewed-tool gates. A fallback route defaults to
JSON; native support is not evidence of local code proficiency.

Gateway contract admission, context guidance, local execution and final wire
projection follow this policy. A stale `program` tier alone cannot promote an
auto request at the provider boundary. Prompt latches record the requested mode:
unchanged settings keep schemas stable across rounds; changing the setting or
loading a legacy latch recomputes the decision. Programs cannot bypass `auto`
batch policy through the legacy `execute_tools.program` parameter.

**Alternatives considered:** model-name/size/context heuristics are not execution
evidence; forcing code for all models adds unnecessary failure modes; removing
JSON from explicit program mode removes recovery without proving a quality gain.
No new model capability is registered. Promoting additional local routes needs
paired task success, authoring failures, model usage and latency evidence.

Offline schema measurement against `8d9926433` uses the repository token counter
on the same discovery/execution gateway schemas. The former resident program
surface costs 614 tokens for the Kimi/DeepSeek counters and 632 for GPT-5.4;
the default JSON surface costs 326 and 341 respectively (46–47% less).
These are gateway-only local estimates, excluding ordinary tools and messages,
not provider billing or live small-model success/latency measurements.

**Problem:** Live optional-adoption diagnostics showed programs used after
ordinary reads, still fetching known inputs sequentially. Compact output alone
did not reduce total input tokens; a mixed batch/code parameter obscured the
code action. **Decision:** local program tier exposes `execute_program` with
one required `code` field. Discovery retains a separate batch `execute_tools`
gateway. Ordinary tools stay available; the action is not forced. The adapter
uses the existing gateway normalizer, task catalog, replay ledger, approvals,
JavaScript engine and settlement. `programmaticCalling=off|auto` local batch policy and batch recovery
reject programs at execution, including legacy requests.

`tools.EXACT_NAME(args)` and dynamic property calls through `tools[name]` use the same complete-value
bridge as `tools.invoke(name,args)`. Property access grants no authority; the
host validates the real task catalog and schema. Helpers retain their names;
colliding tools use `invoke`. The namespace is frozen with no prototype;
`then` is absent to prevent accidental Promise-assimilation calls. Unknown,
failed or incomplete calls hand evidence back to the model before later effects.

**Alternatives:** more prose in the mixed gateway leaves the ambiguous action;
forcing gateway-only exposure confounds adoption and harms semantic/direct
controls. The model interface states operations once and keeps the context guide
focused on cursor traversal, reuse, independent branches and handoff; its complete
attributed carrier fits 128 local tokens. Repeating the API manual taxes small tasks.
The [OpenAI code-action guide](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling)
and [Anthropic named async tool guide](https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling)
provide interface precedents; [CodeAct](https://arxiv.org/abs/2402.01030)
supports revising code actions from observations. None establishes a Tofu
benefit. Promotion requires paired quality, natural adoption, usage and latency with unchanged workloads/oracles.

### Program result projection

**Problem:** A code action's compact answer was wrapped in execution counters
and child receipts. Large retained children could also make a complete answer
look like an incomplete source preview. Failed code lacked the observed data
shape needed to correct mistaken properties such as `array.items`.

**Decision:** `execute_program` returns the exact JSON value on success;
statistics remain in native program events and the UI, including failed runs.
Actual counters precede Store closure; pre-execution refusal has none. On failure it returns
the typed error and bounded observed input structures, including settled call
identities. Complete inline evidence supplies structure; array items are samples.
Object samples expose up to eight 64-character keys, honest omission/shortening
indicators and no values. Artifact receipts retain owner-bound references; no store is read.
The legacy mixed gateway retains its public response envelope.

Canonical trees keep the returned value and complete child evidence. A private
child is archived input, not missing answer content; this runtime-only policy
cannot be supplied by application JSON. An oversized returned value still
uses normal budget, artifact and partial-coverage behavior. JSON null, empty
strings and JSON-looking strings retain their actual returned type.

**Alternatives:** deleting child evidence breaks replay and audit; including
all inputs or counters in the model answer defeats private processing;
inventing an output schema from one sample hides uncertainty. These changes
do not establish model quality, adoption or latency benefit.

## Program reasoning boundaries

**Problem:** A predetermined chain is only one use of programmatic calling.
Semantic pre-planning hides uncertainty; adoption alone proves no benefit. Artifact
read/search declare decoded pages, indexed batches and refusals; terminal cursors
do not prove source completeness. Shared definitions remove repeated page/error
shapes; local-reference/union types are bounded conveniences, never network reads.

**Decision:** Bounded programs use loops, branches, batches and compact output;
`tools.observe(reason, data)` hands evidence back and ends execution,
including inside loops/lambdas; later operations never run. It is a successful observation, not an interpreter failure or retry. The next program
starts from returned evidence and existing receipts;
there is no saved stack or implicit replay of earlier side effects.
`tools.value(receipt)` returns decoded JSON or plain text only from a `done`
receipt with explicitly complete output. Failed, missing or truncated evidence
ends the program with an observation before it can feed a dependent call.
Manual receipt handling remains available for explicit recovery logic.

Programs record call stages, explicitly requested parallel stages, outcome,
private child delivery bytes and final result bytes. These are execution
measurements, not measured token/cost savings or a proven direct-call baseline.
`tool_calls` counts gateway invocation attempts, including name/schema rejection;
`successful_tool_calls` counts returned `done` receipts once at dispatch. It
does not certify source coverage or business correctness. Every settled sibling
is counted before an observation can stop unwrapping; receipt/value rereads and
retained reads add no successful call. Missing-return evidence distinguishes
`completedToolCalls` from `toolCallAttempts`. Cards show success versus attempts
only when both observed counts are consistent; old records keep their old view.
Actual host handoffs also carry `executionReceipt` with attempted/successful
tool calls and retained reads, so the model sees progress without private
inputs or sibling return values. Semantic `data` and ordinary successful return
values stay unchanged. Counts are not a certificate of business correctness.
Compact JSON adds at most 27 bytes per stats object and 89 per handoff at the
existing 16-call/16-read bounds; handoffs use the same heap-scaled result delivery.
No new cache, pool or retained-input lifetime is introduced.
**Problem:** A rejected bare-name call appeared as one fresh call.
**Decision:** Keep exact names and prefixes, and expose actual success separately
from attempt quota. **Alternatives rejected:** Guessing aliases weakens name admission; deriving
success from program completion mistakes semantic handoff for executed tools.
V2 routing records `expectedSavings.basis=program_surface_unmeasured`; catalog
size alone never supplies a saved-model-round estimate.
Zero-child and one-child programs may add overhead. Ordinary direct calls
remain appropriate when the model must reason about each result.

**Alternatives considered:** Gateway-only exposure and serial-chain nudges
cannot remove genuinely necessary reasoning turns. Treating observations as
errors would cause retries and risk duplicate effects. A persistent suspended
interpreter adds lifecycle/state growth before evidence supports its benefit.
Replacing the language runtime requires separate sandbox and cancellation
verification; these observation semantics are independent of that choice.
This follows [Anthropic's workload boundaries](https://www.anthropic.com/engineering/advanced-tool-use),
[execution-side result filtering](https://www.anthropic.com/engineering/code-execution-with-mcp),
and [Cloudflare's composition and control flow guidance](https://developers.cloudflare.com/agents/tools/codemode/).

## Isolated full JavaScript adapter
**Problem:** Additive exposure can remain unused or receive copied literals; complete API documents cannot fit large catalogs. Model observation and exact schema discovery must remain available without a predetermined call sequence. **Decision:** `tools.programmaticExposure='code_api'` is an explicit local experiment, frozen with the task's first orchestration decision. Its program description carries `tools.invoke` overloads and exact argument/declared decoded-result schemas for small wire catalogs. Only equal descriptions, arguments and results share one definition with every exact name retained; parameters and constraints are never merged. Large catalogs carry a bounded exact-name/purpose index, with explicit listed/total counts; unlisted methods remain searchable. `catalog.search` full-name lookups preserve complete schemas or return `arguments_schema_too_large`. Discovery and invocation may share one program. Both keep controls; execution still uses the complete task-owned catalog and its ordinary permission/schema/ approval gates. Optional `ToolContractV2.decoded_result_schema` describes successful local `tools.invoke` values; exact discovery preserves it and direct wire projects it into description text. It is producer documentation, not validation, freshness or coverage; absent outputs stay unknown. MCP structured-content schemas and hosted text envelopes are never inferred as decoded contracts. The compiler adds no cache or worker; full documents allow 64 methods/32 KiB metadata. Decoded-result schemas allow 32 KiB each within existing catalog bounds; all modes share 1,200 action-schema tokens. Index summaries use at most 120 characters per entry and 32 KiB of encoded metadata. Invalid/hosted/ambiguous catalogs, undiscoverably large schemas and required native schemas (`tool_choice`) retain additive exposure, with explicit reasons and effective exposure. Batch tier retains its recovery surface; default exposure remains additive. **Alternatives considered:** guessing fields from samples or MCP envelopes invents return guarantees; more prose left dataflow unchanged; aliases weaken identities; changing exposure breaks cache stability. Generated documents change no authority or call sequence; grouping avoids duplicate definitions. The projection records documentation mode, schema groups, index coverage and tokens. Code-only adoption is constrained and cannot be reported as a voluntary choice. Compare complete answers/source reads, child calls, model rounds, total tokens and latency; a program invocation alone is not a win. This follows the API pattern in the [Cloudflare Code Mode documentation](https://developers.cloudflare.com/agents/model-context-protocol/codemode/).

**Problem:** The retired hand-written ToolScript subset rejected familiar JavaScript syntax and adds authoring friction before any measured program benefit. **Decision:** `lib/tools/javascript.execute_javascript_program` supplies a separately verified async-function-body engine in a fresh Wasmtime Store and pinned QuickJS-NG WASM reactor. `tools.invoke(name,args)` yields decoded complete evidence; `tools.call` yields receipts, `tools.value` unwraps them, and `Promise.all` sibling calls enter one existing parallel batch callback after the VM yields. The callback ports must still be bound to the owning task gateway; the engine grants no independent catalog or execution authority. Batch preflight rejects the entire group before dispatch if any name, schema, or authority check fails. It returns one receipt per input in the original order: invalid inputs keep their typed error; valid siblings receive `batch_rejected` with the first rejection as `cause`. Attempts count toward the program quota, successes remain zero, and unwrapping hands the actual rejection and progress back to the model. A shortened error-only list is not a valid callback result. Independently settled execution failures still retain any successful sibling receipts; preflight rejection never dispatches those siblings. **Alternative considered:** executing valid siblings despite a preflight error would change existing effect authority; reporting `invalid_result` hides the repairable name/schema error. Both are rejected. `program_contract.py` is the shared evidence, handoff, search-input and bounds owner for the JavaScript runtime. Host terminal signals belong to one instance and escape guest `try/catch/finally` after returning from WASM; they never pass through Wasmtime's process-global callback-exception slot. The private guest bridge preserves original receipt identity and mutations for gateway witness checks; spread/JSON copies do not gain canonical provenance. Guest dynamic evaluation has only the same bounded JS capabilities. There is no module loader, filesystem, process, inherited descriptor, environment, timer or direct network capability.

This adapter is the sole local JavaScript runtime for `execute_program.code`
and already-admitted `execute_tools.program` calls. The model-facing
schema describes an async JavaScript function body and requires awaiting tool
evidence before reading it; standard syntax includes destructuring, classes and
try/catch. No custom syntax repair rewrites submitted code. Authoring failures
return typed diagnostics without re-running effects; existing zero-child
repeated-failure batch recovery remains. Saved receipts protect duplicate
gateway frames from replay. A later round/world that reuses a provider call ID
creates a distinct program ledger row and generated child IDs; an exact frame
duplicate returns its original receipt without creating another trajectory.
There is no syntax-based engine fallback.

New runs declare `backend=local_javascript`; historical ledgers without this
field preserve `local_toolscript`. `program_backend` is the shared projection
and recovered-adoption identity authority. Start/output events and reload
rounds carry exact heap, linear-memory, active guest time and WASM fuel limits
plus actual engine statistics (including active guest seconds); obsolete parser AST/step/repair ceilings are not advertised.

The custom interpreter is removed. Current runtime/host evidence failures share
`ProgramExecutionError`; syntax and evidence error codes stay machine-readable.
A disabled program returns `program_disabled` with `reason=authoring_failures`
or `batch_tier`; direct tools and bounded `calls[]` remain available. Legacy
private task-latch names stay readable for in-flight recovery, but never select
an interpreter. Rejected alternative: keeping a dormant custom parser alongside
standard JavaScript would preserve conflicting authoring/repair rules.

Its tests do not establish model adoption, end-to-end latency or net savings.
**Alternatives considered:** Expanding the custom parser perpetuates a bespoke
language; a native QuickJS bridge leaves engine faults outside the WASM memory
boundary; a Node/TypeScript dependency complicates the public Python runtime.
The [upstream reactor](https://github.com/vercel-labs/quickjs-wasi/tree/5a7a0eeda87c99542f8cf3095b6d61ecfa755977)
and [Wasmtime Python interface](https://bytecodealliance.github.io/wasmtime-py/)
provide the retained implementation basis. Engine bytes, package integrity,
upstream commits and notices are pinned in `lib/tools/javascript/assets/engine.json`.

Problem: wire descriptions and native admission previously built a detached
result-schema object only to discard it or serialize it again. Decision:
`serialize_decoded_result_schema` shares the canonical finite-JSON/32 KiB
admission with cloning and renders ordinary string-keyed declarations directly.
Numeric keys and extension containers keep decode/normalize/render behavior;
inspection is bounded by the frozen encoding size. Clone consumers still receive
independent objects. Alternatives rejected: removing metadata validation changes
refusal, and a shared schema cache adds mutable ownership and retention. Exact
serialized text, late-metadata withdrawal and producer mutation are tested in
`tests/tools/test_decoded_result_schema_serialization.py`.


## Explicit turn inputs and working values

**Problem:** After inspecting tool data or making a semantic choice, another
program otherwise rereads sources, including small manifests, or copies literals. A fresh VM cannot
retain derived state. Display artifact references are not private input handles.
**Decision:** `tools.result(name,args)` reads the latest eligible original
post-hook snapshot with exact arguments; `tools.result({ref})` reads a versioned
`input_` handle. Neither executes a source tool or recovers model previews.
`artifactRef`/`tool-result:` belongs to artifact recovery. Wrong namespaces return typed recovery guidance. Use directory references as variables to avoid copying IDs. Returns are decoded data, not receipts or manifests.
**Problem:** a program can mistake retained lookup for a first page fetch. **Decision:** the schema distinguishes fresh `tools.invoke` calls from historical `tools.result` reads; missing-result guidance permits an explicit new authorized read while preserving unknown prior effects. Absence is not proof that an operation never ran. **Alternative rejected:** implicit invocation on a lookup miss can repeat effects and invent historical evidence. Retained reads still stop the program before later effects.
Original captures admit JSON arrays/objects of any size, or outputs at least 4 KiB, at most 1 MiB.
Failed, explicitly partial, media and child-manifest sources are excluded;
unknown upstream coverage remains unknown, without recovering missing bytes.

**Problem:** A partial preview can obscure retained completeness, and its read expression can be mistaken for a guest global.
**Decision:** Partial views expose owned `programInput:{ref,read,bytes,sourceCoverage,retentionCoverage:"complete",scope:"task_turn"}`.
Use the host-derived `read` expression string in code for the entire received response; `programInput` is metadata, not a guest global.
Optional validated `jsonType` (`array/object/string/number/boolean/null`) describes that original value,
not the preview wrapper or JavaScript `typeof` (large integers use BigInt). Legacy pointers normalize to this form.
Unknown types stay omitted; source coverage remains independent and promises neither completeness nor freshness.
Complete views add zero model bytes; aggregate truncation preserves and counts metadata; producer JSON grants no authority.
**Alternatives rejected:** Guest globals duplicate the input API; inferring source completeness from preview truncation or retained byte completeness encourages wrong reads.

`tools.keep(value)` explicitly freezes computed JSON, including small cursors,
into the same ephemeral bank. It returns `{kind:"program_value",ref,bytes,
sourceCoverage:"unknown"}`. Later programs use `tools.result({ref})`; name/args
lookup remains exclusive to original calls. Keep before `tools.observe` when
model judgment is needed, then return only the necessary candidates and handle.
Values use the guest's ordinary result JSON codec (BigInt becomes a string);
receipt-shaped objects remain untrusted JSON, never native tool evidence.
No VM objects, closures, files, workers or durable state are retained.
Program-authored values do not assert source completeness or source provenance.

Application and storage-free SDK hosts use runtime-only canonical descriptors;
JSON replay or copied metadata cannot restore authority. Every access requires
explicit owner, current task ID, active program tier and nonterminal turn.
Original inputs require exact current catalog membership. Computed values require
an identical fingerprint of the sorted executable names: any membership change
revokes access conservatively, since exact dependencies are not asserted.
All references are per-capture immutable snapshots; replacement, eviction,
revocation and turn cleanup never alias a handle to newer data.

The shared launch-probed working set bounds entries and charged bytes; see
[resource limits](../RESOURCE_CONTROL.md). Terminal callbacks and new Flow turns
release it; cancellation rejects reads, keeps and late admission. A value too
large for the working set is rejected before evicting existing entries.
Each program permits sixteen fresh calls, sixteen retained read attempts,
sixteen directory queries and sixteen keep attempts, independently. Reads share
the existing 1 MiB private delivery budget. `value_keeps`/`value_keep_attempts`
and `result_reads`/`result_read_attempts` count actual work, not estimated savings.

Missing, released, evicted or over-budget reads hand back
`retained_result_unavailable`; an unavailable keeper returns
`program_value_unavailable`. Neither silently rereads sources. Typed errors and
handoffs unwind past guest catches/finally before later effects; completed work
and admitted values remain available until their declared cleanup boundary.
The card recognizes handoff only from runtime `outcome=needs_model`; returned
business objects named `kind`, `reason` or `data` remain normal JSON results.

`tools.inputs(query={})` returns `{inputs,total,nextOffset}` without source bodies
or storage reads. Original entries have `{name,args,ref,bytes,sourceCoverage}`;
computed entries have the keeper's metadata shape and no fake tool name or args.
Query accepts optional exact `name`, nonnegative `offset`, and `limit` 1..16;
a name query excludes computed values. Pages reflect current availability.
An empty page is a valid directory result, not evidence of an empty source or
of whether the source tool executed. Successful tools can return partial or
artifact-backed content which is intentionally ineligible for this complete
input bank. For required reuse, prefer `tools.result(exactName, exactArgs)` or
an advertised `programInput.read`: unavailable evidence ends the program through
the existing handoff. Directory callers must check `inputs.length` before
indexing. Recover an `artifactRef` through `read_tool_artifact`; never replay
completed effects merely to repopulate this directory.

**Problem:** unchecked directory indexing hid unavailable retained evidence
behind a generic JavaScript property error. **Decision:** JavaScript failures
carry the last observed directory query's name, ordinal and page counts in
`lastInputLookup`; an empty page adds recovery guidance. This is context, not a
claim that the lookup caused every later error. Original error classification
and message remain intact. Only one metadata record lives in the invocation;
input arguments, handles and bodies are excluded. **Alternatives rejected:**
throwing on every empty directory breaks optional lookups; fabricating an entry
or retaining partial data would falsely certify source completeness; automatic
source replay can duplicate effects. No new cache, executor or recovery API is
introduced. A compact UTF-8 JSON probe measured 84 bytes for an empty
`get_conversation` lookup and 668 bytes for a 200-character CJK query name;
later queries replace the record instead of accumulating a trace.

Original arguments and input references remain runtime-only and charged to the
bank. Reference selection avoids JSON argument numeric representation loss.
The optional `context.programmaticInputs` composer directory stays off by default;
when enabled it admits four expressions, 256 tokens and 4,096 bytes, without
arguments or bodies. It labels computed values separately and withdraws stale
entries on acquisition failure, revocation, terminal cleanup or an empty bank.

**Alternatives considered:** Persistent VMs add continuation authority and idle
resources; automatic memoization changes freshness and effects; durable storage
adds writes and violates storage-free SDK composition; promoting arbitrary
program results to original evidence invents provenance; artifact-ref aliases
cross the display/storage authority boundary. Explicit bounded JSON state allows
inspection, model judgment and continuation without those changes. Real SDK
execution verifies the handoff and zero additional source reads; model adoption,
correctness and latency benefit still require real paired evidence.

## Program body and explicit results

**Problem:** A function definition passed as the entire async body does not run
that function. Treating the resulting JavaScript `undefined` as successful JSON
`null` conceals missing work and invites repeated tool calls.
**Decision:** The schema requests an async body and explicit return. Its guidance
combines reading and derivation before returning, with keys/types or small samples
for unknown structure. Raw data remains available when semantic judgment needs it;
the code API advertises undeclared outputs without inventing a result schema.
The isolated guest
marks only the root `undefined` result; the host returns a
`missing_program_result` observation with `outcome=needs_model`, measured
completed calls and explicit return guidance. Definitions are never invoked
automatically. Completed effects and replay settlement remain authoritative;
repair code must not repeat those effects. Explicit `return null`, absent object
properties and undefined array elements retain their existing JSON behavior.
The shared card explains the missing result rather than showing successful null.
**Alternatives considered:** Automatically calling a guessed function changes
standard JavaScript and may execute unintended effects. Rejecting definitions
would prohibit valid helper functions. Keeping silent null hides the error;
throwing away execution statistics would obscure completed effects. Problem: fixed output rejection confused execution with delivery. Decision: returns and model handoffs use heap-scaled wire and canonical artifact/token-budget delivery. Failures project typed errors, recovery actions and bounded diagnostics while retaining child evidence. Alternatives rejected: unbounded guest memory, replaying completed effects, and merely raising a fixed cap.

MCP 初次检索查询使用共享 `is_human_message` 来源语义，而非仅凭 user 传输角色。
Problem: 内部续行、上下文和纯同伴消息也可通过 user 角色传递，替换真实检索意图。
Decision: 跳过这些载体，同时保留显式人类转向和未标注的历史用户消息。
Alternative: 文本标签匹配会误伤用户原文中的标签；只看角色无法区分来源。
既有调用历史种子、固定 wire、完整执行目录和审批边界继续由原有机制负责。

搜索回执的执行入口由最新 `tool_wire_projection` 决定；缺少该诊断时使用
已锁定的 wire schema。仅显示 `execute_program` 时，`execute_with` 与 notice
提供 `await tools.invoke(name, arguments)` 的代码入口；JSON 入口仍显示时
保留 `execute_tools`。Problem: 固定回执指向未展示的旧入口会诱导无效调用。
Decision: 纯检索保持默认兼容，任务适配器传入实际展示入口；权限和 schema
校验仍在既有流水线。Alternative: 恢复冗余 schema 增加每轮输入，且没有修复
回执对实际投影的描述。固定策略内保持 schema 稳定，不要求不同策略同形。

### Gateway argument repair admission

**Problem:** model-generated `execute_tools.calls` sometimes arrives as a JSON
array encoded inside a string. The canonical gateway schema was absent from
the shared repair index, so repeated calls failed before reaching the gateway.
**Decision:** index the existing gateway schema factories in the common repair
boundary. Existing stringified-JSON repair applies to declared top-level types;
nested user arguments retain their data. Contract validation, executable-tool
lookup and approval still run after repair.
**Alternatives considered:** prompt-only reminders did not prevent measured
repeats; gateway-specific parsing would duplicate the shared repair authority.

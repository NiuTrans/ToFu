# Tool Search：开源方案对比与模拟用户评测

更新日期：2026-08-11

## 目标与不可破坏的约束

Tool Search 的主要职责是让模型知道当前任务有哪些能力，而不是发放权限。
本实现坚持四个约束：

1. 会话的 provider-visible `tools` 数组保持字节稳定，搜索后不追加 schema，避免破坏 prompt cache。
2. 完整 executable catalog 始终由服务端持有；即使模型没有先搜索，只要它直接调用的是 catalog 中的真实名字，仍进入普通审批、hook 和执行流水线。
3. 搜索结果不能提升权限；不在 executable catalog 中的名字始终拒绝。
4. 小目录或全 eager 目录直接暴露原始工具，不强迫模型多走一次搜索。

## 2026-08-11 开源实现快照

所有结论来自对应仓库的固定 commit，而不是产品宣传页。

| 系统 | 主要方案 | 缓存处理 | 搜索后直接调用 | 对本实现的启发/限制 |
|---|---|---|---|---|
| OpenCode `d041eee` | 普通模式直接暴露 MCP；实验 Code Mode 用一个 `execute` 工具承载完整 MCP catalog 和程序化调用 | 请求前按工具名排序；Code Mode 的外层 schema 稳定 | Code Mode 中原始 MCP 名字不再是 native tool，需要走 `execute` | 学习稳定排序、执行时重新检查权限；但完整目录仍在 `execute` 说明中，且改变原生调用体验 |
| Pi `f8c71c6` | extension 显式激活工具；在 transcript 记录 `addedToolNames`，按 provider 使用 `additional_tools`、合成 `tool_search_output` 或 Anthropic `defer_loading` | 激活可由 transcript 重放；provider 不支持时回退全量 | 依赖激活工具先添加定义，不是对任意大目录的语义搜索 | 学习 transcript 可重放和 capability fallback；不采用“激活才有执行权” |
| Goose `d9d2aef` | Traditional 模式全量；Code Mode 暴露 `list_functions`、`get_function_details`、`execute_typescript`，也支持 filesystem disclosure | 外层 meta-tools 稳定，普通工具排序；内部 callback 配置按内容 hash 重建 | Catalog/Filesystem Code Mode 要经程序执行，不能原名 native 直调 | 学习 catalog/details 两级发现与批处理；但 wrapper 改变调用体验，文档注明非文本结果有限制 |
| Gemini CLI `eef19f2` | registry 收集 built-in/discovered/MCP，过滤后把 active declarations 直接发给模型 | 有稳定的 registry 顺序与过滤，没有大目录 deferred search | 已声明工具原生调用 | 适合中小目录；大 MCP 目录仍把完整 schema 送入上下文 |
| Qwen Code `062a613` | `shouldDefer` + `tool_search`；名字提醒、关键词/`select:` 搜索、session reveal、`setTools()` 同步 | schema 预算足够时启动预加载；否则每次 reveal 会改变后续声明列表，并在失败时回滚 | 未 reveal 的工具没有 API declaration，需下一轮才能调用 | 最接近本问题；学习私有 `searchHint`、稳定排序、恢复会话和回滚，但不满足“搜索不破坏 prefix、未搜索也能执行” |
| Tofu candidate | request-live eager surface + `search_tools`；完整 authority catalog 与 wire catalog 分离；搜索只返回原始 schema，绝不 mutate provider tools | 本轮按稳定顺序生成 tools/policy/threshold；私有检索 sidecar 不上 wire | 可以；最终仍以 executable catalog + 普通审批流水线为准 | 保留原生工具体验，同时兼容 OpenAI/Anthropic native search 和本地 fallback |

对应一手源码：

- [OpenCode Code Mode](https://github.com/anomalyco/opencode/blob/d041eee55c4b669f583fcbe0eb73e78d53393ae8/packages/opencode/src/tool/code-mode.ts)、[request 稳定排序](https://github.com/anomalyco/opencode/blob/d041eee55c4b669f583fcbe0eb73e78d53393ae8/packages/opencode/src/session/llm/request.ts)
- [Pi deferred split](https://github.com/badlogic/pi-mono/blob/f8c71c6a0693bc7f71f84e92783315d6a725a721/packages/ai/src/utils/deferred-tools.ts)、[additional tools / tool-search transcript](https://github.com/badlogic/pi-mono/blob/f8c71c6a0693bc7f71f84e92783315d6a725a721/packages/ai/src/api/openai-responses-shared.ts)
- [Goose Code Mode implementation](https://github.com/block/goose/blob/d9d2aef09366da227eca655e45dfb13e47cca545/crates/goose/src/agents/platform_extensions/code_execution.rs)、[stable provider tools](https://github.com/block/goose/blob/d9d2aef09366da227eca655e45dfb13e47cca545/crates/goose/src/agents/reply_parts.rs)
- [Gemini CLI ToolRegistry](https://github.com/google-gemini/gemini-cli/blob/eef19f25c325f35634bdf5fdea5f245414ed4390/packages/core/src/tools/tool-registry.ts)
- [Qwen Code ToolSearch](https://github.com/QwenLM/qwen-code/blob/062a6132b966ab6a342a0c0e45fe316cc7bd32fe/packages/core/src/tools/tool-search.ts)、[deferred registry](https://github.com/QwenLM/qwen-code/blob/062a6132b966ab6a342a0c0e45fe316cc7bd32fe/packages/core/src/tools/tool-registry.ts)、[schema-budget preload / resume](https://github.com/QwenLM/qwen-code/blob/062a6132b966ab6a342a0c0e45fe316cc7bd32fe/packages/core/src/core/client.ts)

## 模拟用户实验

### 设计

- 冻结 28 个代表性 built-in/MCP schema、12 个真实意图。
- 配置的大模型生成英文、中文和刻意避开工具术语的口语表达，共 60 条。
- 第二次独立调用只看用户请求，不看 oracle，生成模型实际会填写的 `search_tools` query。它只存在于评测冻结流程，线上没有第二个 Agent 或额外调用。
- 第三次独立调用只看用户请求和检索结果，选择最终工具。
- 命中与最终正确性全部由冻结 target 精确判定；模型不能给自己打分。
- 生成语料和 model-generated query 已冻结在 `evaluations/tool_search/fixtures/kimi_k3_20260811.json`，后续 arm 共用同一输入。

### 结果

`Raw user` 是直接用口语请求搜索，压力最大；`Model-generated query` 是当前模型调用 `search_tools` 时自己填写的短 query，更接近真实 tool loop。

| Arm | 输入 | Recall@1 | Recall@5 | 空结果率 |
|---|---:|---:|---:|---:|
| 旧精确词 BM25 | Raw user | 10.00% | 10.00% | 71.67% |
| Qwen keyword reference + hints | Raw user | 35.00% | 35.00% | 61.67% |
| 新检索（无私有 hints） | Raw user | 63.33% | 93.33% | 6.67% |
| 新检索（ToolSpec/MCP 私有 hints） | Raw user | **95.00%** | **100.00%** | **0.00%** |
| 旧精确词 BM25 | Model-generated query | 75.00% | 83.33% | 8.33% |
| Qwen keyword reference + hints | Model-generated query | 100.00% | 100.00% | 0.00% |
| 新检索（私有 hints） | Model-generated query | **100.00%** | **100.00%** | **0.00%** |

旧 arm 的最终工具选择正确率为 83.33%；新 arm 在同一批 60 个 episode
上为 100%。旧实现的确定性失败集中在：代码引用、回忆历史决定、中文口语和
schema 使用不同动词。新实现没有依赖 embedding 或额外模型调用来修复这些问题。

Qwen reference arm 是对固定 commit 中关键词权重、stop words 和 action aliases 的
Python 移植，用来比较检索机制；它不声称复现 Qwen Code 的完整 prompt/runtime。

### 延迟

500 工具、私有 hints 开启的本地 microbenchmark：

- 优化中间版本：约 243ms/次。
- 预编译概念词表 + 16,384 项有界 LRU 后：cold 23.91ms，hot 5.72ms。

LRU 只缓存规范化后的私有索引词，不缓存权限，也不改变 catalog 内容。

### 复现

```bash
python -m evaluations.tool_search.cli \
  --arm legacy \
  --replay evaluations/tool_search/fixtures/kimi_k3_20260811.json

python -m evaluations.tool_search.cli \
  --arm qwen --search-metadata \
  --raw-users \
  --replay evaluations/tool_search/fixtures/kimi_k3_20260811.json

python -m evaluations.tool_search.cli \
  --arm candidate --search-metadata \
  --raw-users \
  --replay evaluations/tool_search/fixtures/kimi_k3_20260811.json

# 重新使用当前配置的大模型生成用户、model query 和最终选择
python -m evaluations.tool_search.cli --live --select --search-metadata
```

## 本轮实现改动

三个检索器的边界：legacy 只做旧版精确词面 BM25；Qwen reference 移植固定 commit 的字段权重、stop words 与 action aliases；candidate 是线上实现，额外使用私有 per-tool hints、中英文概念归一、CJK 降噪、字段权重与 action tie-break。

- 本地检索加入字段权重、英文/中文概念归一、CJK 噪声抑制和 action-name tie-break。
- `ToolSpec.search_hints` 保存每个 built-in 的私有 aliases/intents；MCP 复用 `_meta.aliases`、`_meta.intents`、profiles 和 bundle。
- 私有 hints 从 registry 传到 task handler，既不进入 provider body，也不出现在 search result。
- 搜索仍返回 catalog 中的原始 name/description/arguments schema；执行权来自 server-owned executable catalog，不要求先搜索或 reveal。
- 增加 legacy/Qwen/candidate 三个可复现 arm、LLM 用户/query/selector 模拟和精确 oracle。

## 仍需长期观察

- 当前语义层覆盖中英文；其他语言主要依赖 tool schema/MCP metadata 的词面命中。
- 28 工具合成目录用于稳定回归，不替代真实租户目录的 shadow telemetry。
- 单一 simulator 可能形成模型偏好；发布门应继续加入不同模型和真实匿名 query 的离线回放。
- Provider-native Tool Search 的召回由上游实现控制；本地 fallback 和权限边界仍需保留。

# 新 Agent 能力上手指南 — 挂在共享底盘上,不要重造轮子

> **铁律(charter 2026-07-27,owner 拍板):** 新增任何 agent 驱动的功能 MUST 建在共享底盘上:
> agent 循环用 `lib/agent_loop.py` 的 `run_agent_loop`(+ `AbortSignal`),
> 「一句话 → 成品」类能力用 `lib/production/` 的 ProductionRuntime + 阶段图。
> **禁止**新增私有的多轮工具调用循环、私有的中止逻辑、私有的崩溃恢复。
> 棘轮 `tests/test_agent_loop_adoption_guard.py` 会把新私有循环打成红。
> 底盘缺能力时**改底盘**,不在调用方打补丁。

---

## 1. 你要建的是哪种能力?

| 形状 | 例子 | 该用的底盘 |
|---|---|---|
| 多轮「LLM ↔ 工具」循环,直到模型不再调工具 | paper 报告、QA、自由检索、综述 | `run_agent_loop` + `AbortSignal`(本文档) |
| 「一句话 → 成品」流水线(研究→分镜→渲染→合成) | 播客、motion 视频、长篇报告 | `lib/production/` ProductionRuntime + `Stage` 阶段图(见 `docs/modules/production.md`) |
| 单发 LLM 调用(无工具、无循环) | 翻译、摘要、术语回填 | 直接 `dispatch_chat` / `dispatch_stream`,**不需要** agent 底盘 |

判断错了形状才会觉得底盘不够用——先对号再动手。

## 2. `run_agent_loop` 五分钟接入

底盘拥有**控制流 + 三处中止检查**(轮前 / 流后 / 工具间——第三处修过「Stop 点了没反应」的真 bug,不可省);
你的引擎只提供几个钩子,所有引擎特定的 I/O(事件、内容缓冲、usage 累加)都留在钩子里。

```python
from lib.agent_loop import AbortSignal, LoopDirective, run_agent_loop

# ① 中止信号:三种存量机制统一成一个谓词,按你的场景选一个构造器
abort = AbortSignal.from_event(task['abort_event'])   # threading.Event(paper 引擎)
abort = AbortSignal.from_task_flag(task)              # task['aborted'] 旗标(chat)
abort = AbortSignal.from_callback(self.abort_check)   # 回调(swarm);None → 永不中止
abort = AbortSignal.never()                           # 没有中止路径(如定时器轮询)

# ② 钩子:一轮 LLM 调用(包 dispatch_stream,带上你自己的回调)
def dispatch(rnd, tools):
    return dispatch_stream(messages, model=model, tools=tools, ...)

# ③ 钩子:执行一个工具调用并自己发事件/追加 tool 消息
def execute_tool(rnd, tc): ...

outcome = run_agent_loop(
    abort=abort,
    round_tools=MY_TOOLS,
    dispatch=dispatch,
    execute_tool=execute_tool,
    on_round_result=lambda rnd, msg, finish, usage: accumulate(usage),   # 可选
    on_tool_round=lambda rnd, msg: messages.append(assistant_turn(msg)), # 可选
    retry_bonus=detect_premature_close,  # 可选:识别流过早关闭并重试
)

if outcome.aborted:
    ...  # 不要落盘最终结果;outcome.exit_reason 说明在哪一处中止
```

**契约要点:**
- `dispatch(rnd, tools)` 返回 `(msg, finish, usage)`;`msg['tool_calls']` 非空则驱动工具执行。
- `round_tools` 每轮保持可用；没有“工具轮数”超参数。模型返回无工具调用的
  assistant 消息时自然结束。不要靠末轮撤掉工具来逼模型收束。
- `execute_tool` 只在「工具间中止检查」通过后被调;它负责发自己的 `tool_start`/`tool_done` 事件并把 `role:'tool'` 消息追加进 messages。
- 循环**不捕异常**——dispatcher 的 `AbortedError` 原样传播到你的 handler。
- `AbortSignal` 实例可直接传给 `dispatch_stream(abort_check=signal.is_set)`。
- provider continuation、预算/oracle 门和任务特定熔断只能由
  `decide_round` / `before_tools` / `after_tools` 返回 typed
  `LoopDirective`；调用方不再写自己的 `continue` / `break` 循环。

## 3. 现成的 `execute_tool` 范本:别重抄

`lib/paper/tools.py` 的 **`make_research_tool_executor`** 是共享闭包工厂:
解析+schema 修复参数 → 发 `tool_start` → 跑 `_execute_report_tool` → 发 `tool_done`(带
engineBreakdown/verticals)→ 经 `ToolResultEnvelopeV2` 追加 8k 单结果/24k 轮聚合上限的
tool 消息，超限证据转 owner-scoped artifact。paper 的 insight 与 recommend
两个引擎曾经各自内联了一份逐字节相同的闭包,现在只剩这一个。你的能力若需要
web_search / fetch_url 工具,**直接复用** `_execute_report_tool`(它复用 chat 的
`_web_search_one`/`_fetch_url_one`,前端渲染 schema 与聊天模式完全一致)——另起炉灶的
平行实现会静默丢掉 chat helper 计算的字段。

## 4. 已在底盘上的调用方(照抄最近的一个)

| 调用方 | 文件 | 特点 |
|---|---|---|
| paper 报告引擎 | `lib/paper/report_engine/__init__.py` | threading.Event 中止 + 内容缓冲 + 事件投影,最全范本 |
| paper QA 引擎 | `lib/paper/qa_engine.py` | 最小接入,适合入门抄 |
| paper 综述/洞察/立意/自由检索 | `survey.py` / `insight_engine/_synthesize.py` / `ideate.py` / `recommend_engine/_research.py` | facade 打补丁纪律(测试 patch 引擎属性而非 dispatch 本体) |
| scheduler timer | `lib/scheduler/timer/_poll.py` | `AbortSignal.never()` 的无中止路径范本 |
| 视频分镜作者 | `lib/motion_video/_scene_author.py` | 窄工具集 + 每场景 token 预算 + 失败降级范本 |
| root chat | `lib/tasks_pkg/orchestrator/_root_agent_loop.py` | typed policy hooks + task wire/event 投影；主迁移范本 |

## 5. 私有循环零豁免

生产代码中已没有祖父豁免的 LLM↔tool 私有循环。
`tests/test_agent_loop_adoption_guard.py` 扫描所有受管代码，并用合成 AST
样例自测检测器；检测器不再依赖一个真实违规循环留在仓库里。

## 6.  checklist(提交前自查)

- [ ] 没有新写 `while` + LLM 调用 + 工具处理的循环(棘轮会红)。
- [ ] 中止走 `AbortSignal` 三个构造器之一,没有自己读 `task['aborted']` / 自造 Event 谓词。
- [ ] 工具执行复用或仿照 `make_research_tool_executor`,没有平行实现 search/fetch。
- [ ] 崩溃恢复/去重/进度若是「成品类」能力,走 `lib/production/` 而非自建 TaskRuntime。
- [ ] 跑了 `tests/test_agent_loop_adoption_guard.py` 与 `tests/test_agent_loop.py`。

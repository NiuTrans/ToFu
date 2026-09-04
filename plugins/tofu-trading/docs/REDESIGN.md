# 交易模块重做 — 审计与设计稿

> 状态:**审计已完成(实证),设计待拍板**。零代码改动。
> 日期:2026-07-26

---

## 0. 一句话结论

旧模块不是"功能不好",而是**建模建错了**:它把自己建成"每天发命令的机器人",
而真实用户是"手动、按自己节奏、经常不照做的人"。所以最该扔的不是 UI,
是**推荐的数据模型**。

---

## 1. 现状实证(每条都可复现)

### 1.1 它根本没在运行

| 证据 | 值 |
|---|---|
| `import tofu_trading` | `ModuleNotFoundError` — 未安装 |
| `data/config/features.json` | `"trading_enabled": false` |
| 最后一次提交 | 2026-06-10(6 周前) |
| 提交总数 | 4 次(全是抽包搬迁,无功能迭代) |

### 1.2 三个致命缺陷(按严重度排序)

#### ① 采纳闭环是**假的** — 这正是本次需求的核心

`trading_recommendations` 有 `adopted` 列(`_schema_impl.py:92` / `:456`),
但全仓 grep(Python + JS)**零读、零写**。

> 也就是说:系统**从来不知道**你有没有照做。
> 你说的"用户今天没照做 / 漏了几天怎么办" —— 旧模块对此**完全无感**。

#### ② 自我进化循环**数学上不可能学习**

`strategy_evolution.py:156` 定义 `record_decision_outcome`(唯一写入口),
`:17` 导出它 —— **零调用者**。因此 `evaluate_strategy_history` 恒返回
`'No lessons yet.'`(`:98`)。

> 修正一处审计口误:`trading_strategy_performance` 确有另外两个写入方
> (`llm_simulator.py:409`、`strategy_data.py:297`)。死的是
> `strategy_evolution` 自己那条路径,不是整张表。结论不变。

连带 `adaptive_decision_engine.py`(958 行)包着两个死学习器,
`cycle.py:213` 本来就绕过它。**合计约 4,400 行从未跑过的代码。**

#### ③ 单租户 —— 挂上多用户宿主会**互相覆盖持仓**

`user_id` 在整个 `tofu_trading` 包里**零出现**。

```
trading_holdings.py:23   SELECT * FROM trading_holdings          -- 无 WHERE
trading_holdings.py:109  DELETE FROM trading_holdings            -- 清空全表
```

宿主是多租户的(`task_user_id` / `_request_user_id`),插件不是。

### 1.3 数据源:一半是死的

以下为**代码静态判断**;当前真实可用性见 **§5 实测**(结论有两处相反)。

| 状态 | 端点 |
|---|---|
| ❌ **域名打错,从未通过** | `fundgz.1702.com`(`info.py:238`)— 真实域名是 `fundgz.1234567.com.cn`(但 §5 实测该真域名**也**不可用) |
| ❌ 国内不可达 | investing.com RSS ×7(`news_apis.py:285-291`)、Google News RSS(`sources.py:165`) |
| ⚠️ **拿的是延迟行情** | `push2delay` 用了 10 处,实时是 `push2` |
| ⚠️ 抓来的 token 写死 | `ut=fa5fd194…`、`token=D43BF722…`(`info.py:354`) |
| ⚠️ 代码以为可用,实测不可靠 | `push2his` K 线(10 处依赖)— 见 §5 |

### 1.4 前端:第二套设计系统 + 手机上不可用

- `trading.css` 自称 "FundPro — Premium FinTech Terminal v8",自建 `:root`
  (`--bg1..bg4` / `--t1..t4` / `--accent:#6390FF`),与宿主
  (`--bg-primary` / `--text-primary` / `--accent:#6e56cf`)**零重叠**,不 `@import` 宿主 CSS。
- **只有暗色**,无 `[data-theme="light"]` —— 亮色用户从聊天点进来是一块黑板。
- **持仓表**:10 列表格只包了 `overflow-x:auto`(`css:478`),移动端断点
  (`css:693-706`)**没碰 `.data-table`**。
- **每日建议(brain)**:`repeat(4,1fr)` 四张卡(`css:929`)**无任何移动端覆盖**,
  375px 屏上每张约 90px。

### 1.5 一个当场可见的 bug

`trading_autopilot.py:47-95` 用 `asyncio.to_thread(brain_state)` 包 **async** 处理器
—— 返回未 await 的协程,**7 个 autopilot 端点全坏**。

---

## 2. 重做的核心:从「命令」翻成「对账」

这是整个设计稿唯一重要的一节。

### 2.1 旧模型(坏)

```
每天 09:00 → 生成"今日建议:买 A 3000 元" → 存 trading_daily_briefing(date 主键)
                                          ↓
                                    用户没照做 → 建议烂在库里,没人知道
                                    用户漏 3 天 → 3 条互相矛盾的旧命令
```

`trading_daily_briefing` 以 `date` 为主键 —— 这个 schema 本身就假设了
"每天一条、且当天有效"。

### 2.2 新模型(对账 / reconcile)

不再存"今天该干什么",而是存**目标组合**(target portfolio),
每次打开时**现算差额**:

```
目标组合(慢变量,几周才动一次)
        +
你的真实持仓(你说了才算)
        +
今天的实时价格
        ↓
   现算:还差什么 → 今日操作清单
```

**这样漏几天就自动没问题了** —— 因为清单是**当场从"你现在实际在哪"算出来的**,
不是从"昨天我叫你干什么"接着往下走。你昨天没买,今天它自己会重新算
(而且用今天的价格,可能结论就变了 —— 这才是对的)。

> 类比:这不是"每天给你一张待办清单"(漏一天就乱),
> 而是"给你一张地图和你的当前位置"(什么时候看都对)。

### 2.3 三个具体好处(直接对应你的问题)

| 你的问题 | 对账模型怎么答 |
|---|---|
| 用户今天没照做? | 无事发生。明天重算,差额还在那儿(或因价格变化而改变) |
| 漏了几天? | 无事发生。没有"积压的命令队列"这个概念 |
| 只跟了一半? | 差额自动变小。清单下次只显示剩下那半 |

### 2.4 需要的新表(替代 `trading_daily_briefing`)

```
trading_target        目标组合:user_id, symbol, target_weight, rationale, valid_from
trading_position      真实持仓:user_id, symbol, shares, cost, as_of        ← 用户确认过的事实
trading_drift_log     每次对账的快照:user_id, date, drift_json, shown_at
trading_action         建议动作:user_id, symbol, side, amount, reason,
                                status(pending/done/skipped/expired),
                                acted_at, actual_price   ← ★ 采纳闭环,真的写
```

关键:`trading_action.status` **必须真的被写**。这是①号缺陷的正面修复。
只有它被写,"我的建议到底准不准"才能算出来 —— 这也是"提高收益"的**前提**,
因为没有反馈就没有校准。

---

## 3. 分期(建议)

| 期 | 内容 | 为什么这个顺序 |
|---|---|---|
| **P0** | 多租户 `user_id` + 数据源体检 + 修 7 个坏端点 | 不做这个,后面全是沙上建塔 |
| **P1** | 对账引擎(§2)+ `trading_action` 闭环 | 这是本次需求的**主体** |
| **P2** | 前端:并入宿主设计系统、亮/暗色、持仓改卡片式、移动端可用 | 每天要看的东西必须手机能用 |
| **P3** | 删掉 ~4,400 行死学习栈;保留 backtest/strategy 数学 | 先让活的部分对,再清死的 |
| **P4** | 收益校准:用 P1 攒下的真实采纳数据回测建议质量 | 必须等 P1 攒够数据才有意义 |

**保留**(有真实测试 `test_quant_parity.py:368-401`,T+1 无未来函数是真的):
`trading_backtest_engine/`、`trading_strategy_engine/` 的 `risk_metrics` + `portfolio`、
`trading/` 数据层(修完死端点)、`screening.py`。

**删除**:`strategy_evolution` / `strategy_learner` / `adaptive_decision_engine` /
`backtest_learner` / `debate`。

---

## 4. 待拍板(需要 owner 回答)

1. **标的范围**:A 股 / 公募基金 / 两者?(决定数据源和费率模型)
2. **对账频率**:每天一次,还是打开就算?
3. **目标组合谁定**:AI 提议 + 你批准,还是你定 AI 只做偏离提醒?
4. **旧数据**:18 张 `trading_*` 表当前是空的(插件从未装),可否直接换 schema 不做迁移?
5. **P3 删除**是否授权?(约 4,400 行)

---

## 5. 数据源实测(★ 本机实跑,2026-07-26)

**本机在企业代理后**(`http_proxy=http://10.229.18.27:8412`),这改变了结论 ——
必须做**多源 + 健康检查 + 回落**,不能单押东财。

| 端点 | 实测 | 结论 |
|---|---|---|
| `push2` 实时行情 | ✅ 200,132B,**无需 header** | **主行情源** |
| `pingzhongdata/{code}.js` | ✅ 200,**754KB**,含完整净值史 | **主基金源**(一个请求拿全历史) |
| `qt.gtimg.cn` 腾讯行情 | ✅ 200,518B | 行情回落源 |
| `web.ifzq.gtimg.cn` 腾讯 K 线 | ✅ 200,1.9KB,真实前复权数据 | **K 线主源**(见下) |
| `push2his` K 线 | ⚠️ **4 次里 1 次直接断连**,其余返回 `rc:102`(无数据) | **不可靠,降为回落** |
| `push2` 非 his K 线 | ❌ 502 | 不可用 |
| `fundgz.1702.com`(代码里的) | ❌ 返回错误页 | 确认是打错的域名 |
| `fundgz.1234567.com.cn`(真域名) | ❌ 返回 HTML 而非 JSON | **盘中估值整个不可用** |

**两条与研究结论相反的实测发现:**

1. **K 线主源要换成腾讯,不是东财。** 东财 `push2his` 在本机代理下不稳定
   (断连 + `rc:102`),而腾讯 `web.ifzq.gtimg.cn` 稳定返回前复权数据。
   旧代码 10 处依赖 `push2his`,这是 P0 要改的。
2. **盘中估值(fundgz)两个域名都不可用。** 所以「实时估算今天赚了多少」这个功能
   **做不了**,只能用 T-1 净值 + 持仓股实时价自己估。设计上必须诚实标注
   「估算」而非「实时净值」。

---

## 6. 合规(研究结论,非法律意见)

非法证券咨询的认定是**三要件叠加**(《证券投资顾问业务暂行规定》第二条):
①无牌照 ②向**第三方客户**提供 ③**直接或间接获取经济利益**。

自用工具(只给自己/家人、不收费、不公开发布)**不满足 ②③**,不构成证券投资咨询。

**但产品文案上仍应:** 输出标注「参考 / 回溯分析」,避免「投资建议」字样;
不公开、不商业化。AKShare 自己的 README 也是这个姿态。

---

## 7. 诚实边界

- §1 所有实证均可复现,命令见各条引用的 `file:line`。
- §5 数据源为**本机实测**(带企业代理),换网络环境结论可能不同 ——
  所以 P0 要把这套探测**做成代码里的健康检查**,而不是写死在文档里。
- 收益能否提高**无法在设计阶段承诺**。可承诺的是:P1 让「建议 vs 实际」第一次可测量,
  这是校准的前提,不是收益的保证。

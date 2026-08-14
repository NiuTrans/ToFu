# SITE_KNOWLEDGE_LAYER_DESIGN.md — 站点知识层:让 Tofu 操作任意网站

> 状态:**v2 · 2026-08-13 · 站点知识与原生浏览器运行时已实施**
> 浏览器能力归属、权限、会话、适配器与搜索设置的最终定案见
> `BROWSER_ADAPTER_RUNTIME_DESIGN.md`；本文 P0–P3 章节保留为演进背景。
> 驱动会话:owner「把知识层固化成固定流程,遇到拦截直接优化;安全权限后置,功能先行;
> 每内化一个网站,设置里就追加一条」。
> 前置阅读:JOURNAL 2026-08-03 OpenCLI 学习条目 + 「操作任意网站」设计讨论条目;
> 记忆 `opencli-study-xhs-browser-bridge` / `tofu-browser-bridge-inventory`。

---

## 1. 背景与问题

### 1.1 痛点(小红书是最尖锐的样本)

| 痛点 | 根因(实测) |
|---|---|
| token 配置繁琐 | 要手动抠 cookie/签名参数喂给服务器 |
| 被小红书风控警告 | tofu-search 走**服务器无头 Playwright 池 + 异地回放导出 cookie**(`xhs.py` 旧路)——headless 指纹 + 服务器 IP 与 cookie 签发环境不一致,是风控最经典的识别信号 |
| 抓不到笔记正文 | 架构上只有搜索卡片路径,没有详情页路径;且详情 URL 需要 `xsec_token`,服务器匿名请求必死 |

### 1.2 OpenCLI 的启示(jackwener/opencli,已克隆至 chatui 同级)

**永不自己签名**——让真实登录的 Chrome 自己开门:页面 JS 完成签名,适配器只读渲染结果。
其工程资产是三层模型:

| 层 | 角色 | OpenCLI | Tofu 现状(盘点实锤) |
|---|---|---|---|
| 运输层 | 伸进用户浏览器的手和眼 | daemon+扩展+chrome.debugger | ✅ 已有:Tofu Browser Bridge 扩展(27 命令)+ `/api/browser/poll` + bridge token,生产在跑 |
| 原语层 | 对网站可说的动作词汇 | open/click/eval/extract/network | ✅ 基本已有:16 个 `browser_*` 工具;**缺网络拦截、缺「后台标签页内 wait+extract 组合原语」** |
| 知识层 | 每个网站的地图和攻略 | 100+ adapter + 策略分类 + 站点记忆 + 创作流水线 | ❌ 完全没有——**本设计的主体** |

**结论:Tofu 缺的不是手和嘴,是地图册。** 本设计把知识层落成一套固定流程 + 一个注册表。

---

## 2. 核心机制:拦截自愈固定流程(The Ladder)

owner 指令:「遇到拦截问题就直接走固定流程优化」。固定流程定义如下,**任何 fetch/search 路径命中拦截信号时自动拾级而上,全程无需人工介入**:

```
  正常路径(服务器 HTTP / 搜索引擎)
      │ 命中拦截信号(§2.1)
      ▼
① REROUTE 换路:浏览器桥接管(用户真实 Chrome,原生登录态,页面自签名)
      │ 成功 → 返回结果,并 ② RECORD 记账
      ▼
② RECORD 记账:站点台账记录「域名 × 信号类型 × 换路结果」
      │
      ▼
③ INTERNALIZE 内化:站点在注册表中获得/更新知识条目
   (策略 + 选择器 + 坑位标记;P0 由代码内置,P2 起可由攻略作者 workflow 生成)
      │
      ▼
④ KNOWLEDGE-FIRST 下次直通:同站点的后续访问直接走知识指定的最优路径,
   不再先撞墙再换路(节省一次失败 + 一次风控暴露)
```

### 2.1 拦截信号分类(Detect)

| 信号 | 来源 | 例子 |
|---|---|---|
| HTTP 状态 | `do_request` | 401/403/429/406 |
| 安全页标记 | 页面文本/DOM | 小红书「安全限制」error 300017/300031、滑块、验证页 |
| 登录墙 | `_looks_like_login_wall` | SSO 重定向页、登录弹窗页 |
| JS 空壳 | 正文长度/标记 | SPA 未渲染壳、`<noscript>` 主体 |
| 连续空结果 | 引擎级统计 | XHS `_RiskGuard` 连续空抓取(现有) |

①②④ 在 P0 落地(换路 + 已有日志/audit 记账 + 知识条目驱动路径选择);
③ 的「自动生成知识」是 P3(攻略创作 workflow),P0/P1 的知识由代码内置。

### 2.2 设计原则

- **活会话 > 凭据回放**(Live session beats replay):用户浏览器在线时,任何登录态访问都优先走浏览器桥;cookie 回放降级为兜底。这是风控根因的直接解药。
- **DOM 优先于 API**:OpenCLI 实测 API 拦截类适配器修复频率是 DOM 类的 7-8 倍;能读渲染后 DOM 就不碰签名 API。
- **知识是数据,不是代码分支**:站点知识(选择器/URL 模板/坑位)放在注册表条目里,P2 起站点接入不改引擎代码。
- **换路必须可观测**:每次换路 INFO 日志(站点、原因、路径、耗时),换路失败 WARNING——遵守 CLAUDE.md §2 日志纪律。

---

## 3. 站点注册表(设置页「每内化一个网站就追加一条」)

### 3.1 回答 owner 的问题:现有开关会不会不再需要?

**不会消失,会长大。** 设置里的「需要登录的来源」(auth_sources)就是注册表的胚胎:
它已是「条目追加式」(内置 2 条 + 用户可加自定义源,上限 64)且自带开关/登录引导/cookie 字段。
演进方向 = 把它从「凭据提供者」泛化为「站点接入注册表」:

```
条目 schema(P0 → P1 演进,向后兼容——新字段全部 optional):
{
  domain:      'xiaohongshu.com',        # 既有
  label:       'Xiaohongshu / RED',      # 既有
  aliases:     ['xhslink.com'],          # 既有
  login_url:   'https://…',              # 既有(登录引导)
  enabled:     true,                     # 既有(开关)
  fields:      [cookie 字段规格…],        # 既有(回放兜底 + 登录引导用)
  cookies:     […],                      # 既有(回放兜底)

  access_strategy: 'browser_first',       # P1 新增:browser_first(默认) | cookies_replay | public
  knowledge: {                            # P1 新增:站点知识(攻略)
    search:  { url_template, wait_selector, extractor_js },
    detail:  { url_patterns, wait_selector, blocked_markers },
    pitfalls: [ '裸 ID 打不开详情页,必须带 xsec_token', … ]
  },
  stats: { interceptions: n, browser_saves: n, last_signal: '…' }   # ② RECORD 的落点
}
```

- **UI(P1)**:设置段落标题从「需要登录的来源」演进为「站点接入」;每条卡片显示
  策略徽章(browser_first/cookies_replay)+ 知识状态(已内化/仅凭据)+ 开关不变。

#### 3.1.1 活会话是一等凭证(owner 第二问 2026-08-03:「为何还要用户配 cookie?」)

定案:**browser_first 条目的凭证是用户浏览器里的活会话,不是本库里的 cookie。**
OpenCLI 的「免配置」=装扩展+浏览器里登录一次;tofu 完全同构——一次性装桥扩展,
日常浏览器本就在登录态。此前三道旧闸门强迫粘贴 cookie,全部拆除:

  1. **卡片开关**:`has_cookies` 才准拨 → `browser_first`/`public` 行永远可拨
     (cookies_replay 无 cookie 仍禁拨——没有可回放的东西);
  2. **match_source**:无 cookie 不匹配 → browser_first 行无 cookie 也匹配
     (public 永不匹配);tofu-search `_replay()` 对无 cookie 行短路(匿名池加载
     登录墙=纯浪费+bot 特征);
  3. **引导流程**:面板主路径改为「在你的浏览器登录该站 → 检测到会话 → 启用」,
     cookie 粘贴降级为「离线兜底(可选)」;卡片新增活会话徽标
     (`GET /api/v1/auth-sources/<domain>/live-session` — 桥 `get_cookies`
     域级探测,**cookie 值永不离开浏览器**,只比对目录声明的会话 cookie 名;
     20s TTL,`?refresh=1` 强制重探)。

由此「搜索小红书」的完整配置 = 装扩展(一次性)+ 浏览器里登录小红书(日常已在)
+ 拨开关——与 OpenCLI 完全等价的零粘贴体验。
  **内化一个网站 = 追加一条**,与 owner 的要求逐字对齐。
- **迁移**:内置两条目(xiaohongshu.com、example-corp.com)自动获得 `access_strategy` 默认值,
  老 `auth_sources.json` 无需任何手工迁移。
- **消费链**:tofu-search 经 `AuthSourceProvider.get_source()` 拿到整条 row(含新字段)——
  依赖方向不变(host → library),library 不认识的新键原样透传。

### 3.2 策略分类(继承 OpenCLI,按 Tofu 实际裁剪)

| 策略 | 含义 | 适用 |
|---|---|---|
| `browser_first`(默认) | 浏览器桥优先,池回放兜底 | 登录墙 + 风控敏感站(XHS、内网 SSO) |
| `cookies_replay` | 服务器池回放 cookie(旧行为) | 风控迟钝、用户浏览器常离线的站 |
| `public` | 无需登录,正常 HTTP | 普通公开站(无需条目) |

OpenCLI 的 UI_SELECTOR/DOM_STATE/PAGE_FETCH/INTERCEPT 是「数据怎么抽」的细分,
Tofu 归入 knowledge.search/detail 的 extractor 形态,不提升为顶层策略——顶层只回答「谁去开门」。

---

## 4. 新原语:`BrowserProvider.scrape`(P0 交付)

搜索卡片/结构化内容需要「导航 → 等选择器 → 页内跑提取 JS」的组合,现有
`fetch_url/fetch_html`(纯文本/HTML)不够。扩展命令面已齐备
(`create_tab` 默认后台 `active:false`、`wait_for_element`、`execute_js`、`close_tab`),
**P0 零扩展改动**,纯服务端组合:

```python
class BrowserProvider:                       # tofu_search/providers.py
    def scrape(self, url, *, wait_selector='', extractor_js='[]',
               timeout=20, scrolls=0):
        """后台标签页打开 url,等 wait_selector,页内执行 extractor_js,
        返回其 JSON 结果(list/dict);路径不可用返回 None(≠ 空结果[])。"""
        return None                          # 默认惰性,host 可选实现
```

chatui `_ChatuiBrowserProvider.scrape` 组合四个既有命令实现;`scrolls` 留给
懒加载站点(P1,XHS 搜索凑满一屏暂不需要)。

**契约关键点:`None`(路径不可用,换兜底)与 `[]`(路径通了、真空结果,计入风控 backoff)严格区分。**

---

## 5. P0 落地详案(本批已实施)

### 5.1 XHS 搜索引擎换路(tofu-search `xhs.py`)

| 项 | 旧 | 新 |
|---|---|---|
| 主路径 | `_pw_pool.search_authenticated`(服务器无头池+回放 cookie) | `provider.scrape`(用户真实 Chrome,原生登录态,**不需要导出 cookie**) |
| 兜底 | 无 | 浏览器不可用/返回 None → `_pw_pool`(有 cookie 时) |
| 可用性判定 | `enabled && cookies` | `enabled && (cookies 或 浏览器在线)` |
| 风控护栏 | `_RiskGuard`( pacing/缓存/连续空 backoff) | **原样保留**,对两条路径同样生效(护的是账号,与走哪条路无关) |

### 5.2 登录态抓取换路(tofu-search `fetch/core.py` 阶梯)

auth-source 命中的 URL(小红书笔记、美团内网页)访问顺序:

```
旧:池回放 cookie →(墙/空)→ 浏览器桥
新:浏览器桥(活会话) → 池回放 cookie(兜底) → 匿名管线
```

笔记正文因此获得:扩展后台标签页真实渲染笔记页(带 `xsec_token` 的 URL 从搜索结果透传),
innerText 抽出标题+正文+互动区文本。结构化提取(点/藏/评计数、话题标签)留给
knowledge.detail(P1)——P0 先把「拿不到」变成「拿得到」。

### 5.3 chatui 侧

- `lib/search_bridge.py`:`_ChatuiBrowserProvider.scrape` 组合实现(§4)。
- `requirements.txt`:floor `tofu-search>=0.7.0`。

### 5.4 明确不做(本批)

- 不改扩展(P0 零扩展改动);不加审批/安全门(owner 指令:功能先行,安全后置,§7 记档);
- 不动设置 UI(P1 演进);不动 example-corp 条目字段(自动继承 browser-first 行为)。

---

## 6. 路线图

| 期 | 内容 | 状态 |
|---|---|---|
| **P0** | scrape 原语 + XHS 搜索换路 + auth fetch browser-first + 设计稿 + CLAUDE.md 纠正 | ✅(tofu-search `ffa67e7` 0.7.0 + chatui `88bf9c67`) |
| P1 | 注册表泛化(auth_sources → 站点接入:strategy 字段 + UI 徽章与段落改名「站点接入」);域名清单数据化(tofu-search 消费 access_strategy + 登录 hints 读注册表 fields) | **前半 ✅**(tofu-search `056a23b` 0.7.2 + chatui 本批,epic `pt_689b73b305fe4810`)。余量:`knowledge.detail` 结构化笔记提取、`stats`/RECORD 台账落盘——另起切片 |
| P2 | 知识即数据:引擎读注册表 knowledge 而非内置常量 | **缝已交付**(P3 批提前落地:SiteKnowledgeProvider + lib/site_knowledge 覆盖式存储)。刻意偏离记档:XHS 内置选择器**保留在引擎代码**作保底,store 只放医生验证过的覆盖条目——内置永远是最新版本基线,覆盖条目只在其上生效;「迁出为首个数据条目」因此不做 |
| **P3** | 攻略老化 autofix:漂移信号(anchors>0 但 0 卡片)→ site_doctor 骑 `run_agent_loop` 重侦察(inspect→try_extractor→**只钉验证过的参数对**;登录墙=give_up)→ 回写 lib/site_knowledge | ✅(tofu-search `ed9f7c2` 0.7.1 + chatui 本批)。**缓办记档**:扩展网络拦截 + toString 伪装——autofix 环不需要(composite 判例:组合既有 27 命令优先),且扩展是部署最慢一层;真正需要页内 hook 的站点出现时再立项 |

---

## 7. 安全与同意（v2 已回补）

- 浏览器只读对当前用户默认允许，设置中以域名拒绝列表管理例外；
- 写操作首次经人工审批后，持久为「用户 + 精确域名 + 浏览器客户端 + profile」授权；
- 每次动作前后重新校验当前 URL，跨域重定向不继承权限；
- 适配器显式声明 `read/write`、输入输出 schema、所需扩展能力和超时；
- 授权可在搜索设置中撤销，审计参数摘要不记录 cookie、凭据、页面正文或上传数据。

---

## 8. 测试与验收

- tofu-search:`tests/test_xhs_browser_reroute.py`(换路顺序/None vs [] 语义/可用性新判定/护栏共存)
  + auth browser-first 顺序测试 + `tests/test_xhs_drift_signal.py`(知识覆盖/内
  置保底/漂移发射语义/监听器吞异常/漂移仍喂退避/池路径保列表形)。
- chatui:`tests/test_browser_scrape_provider.py`(命令组合序列/close_tab 必发/异常归零)
  + `tests/test_site_doctor.py`(存储版本单调/空 extractor 拒钉/触发四闸——
  env 杀开关/未知站/冷却/单飞/医生环骑 run_agent_loop:验证过才钉、钉错参数拒、
  give_up 零写、异常永不 raise)。
- 验收(真机):浏览器扩展在线 + XHS 已登录 → 搜索出卡片;笔记 URL fetch 出正文;
  扩展离线 → 自动回落旧路,日志可见。

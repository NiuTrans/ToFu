# FETCH_IDENTITY_PATHS_DESIGN.md — 「人能拿到的，agent 也能拿到」获取能力设计

> 触发样本：`https://api.openai.com/ml/modelPlaza/modelInfo?...`（FRIDAY 模型工厂，
> SSO + SPA）。2026-07-27 由 owner 拍板方向后修订：**身份主路径 = 客户端浏览器桥，
> 服务器端凭证库降为兜底**。本文所有现状断言均逐文件核实并带行锚。

---

## 1. 事故解剖：aigc 页面死在哪一环（全链路实测）

服务器侧 fetch 链（`tofu_search/fetch/core.py::fetch_url`）对 aigc URL 的实际轨迹：

| 步 | 环节 | 结果 | 证据 |
|---|---|---|---|
| 1 | 静态 GET | **200，6534 字节 SPA 空壳**（`<div id="app">` + 埋点脚本） | curl 实测 `http=200 size=6534` |
| 2 | SPA 空壳检测 `_looks_like_spa_shell` | 命中 | core.py SPA-shell 分支（"HTML present but too little extracted text"） |
| 3 | 服务器 Playwright 兜底 `_try_playwright_fallback` | **死路**：无 SSO 会话，渲染出的是登录墙 | Playwright 已装（`chromium_headless_shell-1223` 实测在机），但匿名渲染 SSO 站只会得到登录页 |
| 4 | 浏览器扩展兜底 `_try_browser_fetch` | **从未被调用** | core.py 中 `_try_browser_fetch` 的全部调用点（:194/:201/:243/:259/:268/:277）**只挂在传输层失败**（401/403/406/429/5xx/timeout/ConnectionError）；「200 + 空壳」分支在 Playwright 失败后直接 `return None` |
| 5 | 扩展在线？ | **否**（实测 `is_extension_connected() == False`） | 即便断点接上，本轮也无人接单 |
| 6 | auth_sources（服务器 cookie 重放） | 只有 xiaohongshu 默认条目，disabled、0 cookie | `data/config/auth_sources.json` 实测 |

**一句话根因：链不是没建，是「200 + SPA 空壳 / 登录墙」这一类结局没有被链接进身份兜底**——
传输错误有兜底，「拿到了但拿到的是壳」没有。

## 2. 家底盘点（比我方初版设计假设的多得多）

| 能力 | 状态 | 落点 |
|---|---|---|
| L0 静态抓取 | ✅ 生产在用 | tofu_search fetch 链 |
| L1a 服务器渲染（匿名） | ✅ 已装可用 | `tofu_search/fetch/playwright_pool._pw_pool`，chromium_headless_shell-1223 在机 |
| L1b 客户端渲染（带用户会话） | ✅ 已建 | `browser_extension/background.js` `cmdFetchUrl`（隐藏后台 tab，继承用户 cookie，取完关 tab）；服务端缝 `lib/browser/fetch.py:11 fetch_url_via_browser` |
| L2a 身份：用户浏览器活会话 | ✅ 通道已建 | 同 L1b；`lib/search_bridge.py:158` 已把它接进 tofu_search 的 BrowserProvider 缝 |
| L2b 身份：服务器 cookie 库 | ✅ 已建 | `lib/auth_sources.py`（域名→cookies+proxy 存储、audit_log、redact 清单）+ `routes/api_v1/auth_sources.py:134` 交互式登录（headful Playwright 捕获 storage_state） |
| L2c 身份：CIBA/OAuth 机器授权 | ✅ 有先例 | xuecheng MCP 大象 push 登录（SSO 家族机器可走的授权通道） |
| L3 交互（click/fill/scroll） | ✅ 已建 | background.js 全命令集 + `lib/browser/dispatch.py` 20 个 browser_* 工具 |

**初版设计的「四层阶梯要建三层」是错的——真正缺的是：①一个断点（§3）②桥的安全属性（§5）③三路径的路由纪律（§4）。**

## 3. 唯一代码断点：「拿到壳」不链接身份兜底

`tofu_search/fetch/core.py` 需要补三条缝（均为「Playwright/匿名链已判定拿不到内容」之后、return None 之前）：

1. **SPA 空壳分支** → `_try_browser_fetch(url, max_chars, reason='spa_shell')`
2. **bot 墙/登录墙分支**（`_is_bot_protection` / `_is_bot_extracted_text` 命中且 Playwright 也失败）→ `reason='login_wall'`
3. **auth-source 重放失败 fall-through**（"auth-source fetch yielded nothing"）→ `reason='auth_source_failed'`（cookie 过期时用户浏览器可能仍有活会话）

行为不变式（守卫断言「结果」，不锚私有符号）：
- 无扩展在线时行为与今天**逐字节一致**（`_try_browser_fetch` 对未连接 provider 是 no-op）；
- 摘掉任一缝 → 对应 reason 的兜底调用计数归零，守卫必红；
- `lib/browser/fetch.py` 的 401/403 docstring 承诺（"fallback when server-side fetch gets 401/403"）扩展为 「401/403/SPA 空壳/登录墙」。

**为什么改 tofu_search 而不是在 chatui 工具层打补丁**：fetch 链是底盘（charter：底盘缺能力改底盘）；
在 `lib/tools/browser.py` 外层再捕一次 None 会制造第二条判断「什么是失败」的逻辑——那正是 charter 禁止的调用方补丁。

## 4. 三条身份路径对比与路由纪律

| 维度 | P1 浏览器桥（主） | P2 CIBA/OAuth（次） | P3 auth_sources cookie 重放（兜底） |
|---|---|---|---|
| 身份驻留 | **用户机器，从不出域** | 服务器（scoped token） | 服务器（明文 cookie，`data/config/`，export 已排除） |
| 覆盖面 | 用户浏览器里登录的**一切站点**（含 SSO/扫码/硬件 key） | 仅支持该授权协议的平台（SSO 家族） | 任何能粘贴 Cookie 头的站 |
| 过期/重授权 | 无（用户自己续自己的会话） | token 可刷，机器自愈 | 用户手动重贴，会腐烂 |
| 在线前提 | 用户浏览器开着 + 扩展在线 | 无 | 无 |
| 安全面 | 桥本身（§5，当前未认证=主要风险） | 小（scoped、可吊销） | 凭证库被读=会话全失 |
| 现状 | 通道已建，断点未接，扩展未连 | xuecheng 已通；aigc 是否吃同一 SSO ticket **未探** | 机制已建，aigc 未配置 |

**路由纪律（fetch 链内的判定顺序，已实现度标注）：**
1. 域名有 enabled auth-source → 服务器 cookie 重放（P3，已有）——它最先试是因为成败最快、不依赖用户在线；
2. P3 失败 / 无配置 → 匿名链 → 空壳/登录墙 → **浏览器桥**（P1，本次补的断点）；
3. P2 不进 fetch 热路径——它是「平台级集成」（如 xuecheng MCP 独立工具面），当某站点证明吃 SSO ticket 时作为该域名的专用增强，不做通用兜底。
4. **反方向红线**：不得把 P1 拿到的页面里的 cookie/token 回存进 P3 库；身份只被「使用」，不被「迁移」。

## 5. 威胁模型（owner 要求逐形态写清）

桥的 attack surface 按部署形态分：

| 部署形态 | 谁能摸到 `/api/browser/poll` | 拿到什么 |
|---|---|---|
| loopback（单机） | 本机任意进程 | 同用户权限，风险≈本机恶意软件，可接受（文档化豁免） |
| LAN（默认，`TOFU_BRIDGE_SECRET` 未设=无认证） | 同网段任何人 | **全部**：poll 领走命令=在用户浏览器里执行 JS/读任意 tab/读全站 cookie 罐（manifest 17 权限 + `<all_urls>`，background.js 还会扫 tab 自动发现服务器） |
| 隧道/relay（多用户） | 任何知道地址的人 | 同上 + 跨租户：A 的 agent 领走 B 的浏览器命令（无 user_id 作用域，`grep user_id lib/browser/` = 0 实测） |

**结论（owner 已拍板，本文记录不再重开）**：桥的认证 + user_id 作用域 + protocol_version 硬化（板上 `pt_130129b5edff4556`，B0）是身份抓取的**前置依赖，不是并行项**。在 B0 落地前，P1 路径只允许在 loopback 形态下使用；§3 的断点代码本身可以先落（无扩展时 no-op，零攻击面变化），但**不得在任何文档/UI 里引导用户在非 loopback 形态下依赖它**。

P3 库的威胁面：`data/config/auth_sources.json` 明文 cookie。已被 `export.py` 整体排除；`list_sources` 只回 redacted。残余风险 = 服务器本地读权限者拷贝会话——接受并文档化（与 `lib/oauth/` token 同级姿态）。

## 6. 分期（每期独立可交付）

| 期 | 内容 | 前置 |
|---|---|---|
| **F0**（✅ 已落地 2026-07-27：tofu-search `495f63f`+`1e66521`，0.5.2 已 deploy，验收 `tests/_acceptance_fetch_identity_fallback.py` 7/7） | tofu_search core.py 补兜底缝（§3 三条 + known_spa/第二处 bot 墙同型两分支，共 5 处）+ 行为守卫 10 条（NEUTER 摘 spa_shell 缝精确红 3 条）+ 回归 293/293 | 无（无扩展时 no-op） |
| **F1** | B0 桥硬化：认证默认强制（non-loopback）+ user_id 作用域 + protocol_version —— 板上 pt_130129b5edff4556 已开 | 无 |
| **F2** | your-llm-gateway.example.com 实测验收：扩展连上后 fetch_url 直出模型清单；同时探一次 SSO ticket 能否被 P2 复用（决定 P2 是否立项） | F0 + F1 + 用户浏览器在线 |
| **F3**（兜底路径，按需） | aigc 接入 auth_sources：用户 Settings 粘贴一次 Cookie 头（或走交互式登录），服务器 Playwright 重放——零新代码，今天就能做，定位为「用户浏览器不在线时的降级」 | 无 |

**F0 守卫纪律**：按 charter「守卫断言结果」——断言「SPA 空壳且 Playwright 失败时，browser provider 被以 reason='spa_shell' 调用一次」，不断言 core.py 的行号/符号名。

## 7. 决策日志（已拍板，不再重开）

1. 身份主路径 = 客户端浏览器桥；凭证库兜底（owner 2026-07-27，推翻初版「服务器 cookie 收割为主」）。
2. 桥认证/user_id 硬化是前置依赖，不是并行项（owner 2026-07-27）。
3. 样本站点三枚：km.internal.example.com（P2 先例）、your-llm-gateway.example.com（目标）、任意第三 SSO 内网工具。
4. 断点修在 tofu_search 底盘，不在 chatui 调用方打补丁（charter 能力复用铁律）。
5. 不建密码代填、不回迁身份、不在非 loopback 形态引导依赖未认证的桥。

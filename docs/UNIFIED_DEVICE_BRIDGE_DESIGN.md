# Tofu 统一设备桥(Unified Device Bridge, UDB)设计稿
—— 一台设备、一次配对、两个执行器

> **状态:DESIGN — 四条拍板已落(2026-07-27,owner),B0 为安全前置,先于任何合并动作。**
> Board epic:`pt_130129b5edff4556`。
> 关联既有设计:`docs/REMOTE_WORKTREE_DESIGN.md`(RWA,P0–P5 已落地)。
> 本稿全部事实性结论均于 2026-07-27 在盘上逐文件核实(标注 `文件:行号`),
> 权限一栏的调用计数为 `grep -o "chrome\.<api>\b"` 实测值,非引用他文。

---

## 0. 问题与拍板

**问题。** 部署在远端服务器上的 Tofu 已经能控制用户本地机器的**文件与命令**
(RWA,`docs/REMOTE_WORKTREE_DESIGN.md`,P0–P5 全部落地),也能控制用户的
**浏览器**(Chrome 扩展桥,`lib/browser/`)。两者各自有一条长轮询桥、各自一套
命令队列、各自一份安装说明。是否应当合并?并给出一份好的安装引导?

**拍板(2026-07-27):合传输层与身份层,不合执行层。**

不是把「手」和「眼睛」缝成一个器官,而是**承认它们属于同一个人** —— 同一个身份、
同一次授权、同一个在线指示灯,但仍然是两只独立的手脚。

| 层 | 合并? | 理由 |
|---|---|---|
| **能力层**(浏览器控制 ↔ 文件/shell 控制) | **不合** | 两者跑在不同进程、不同沙箱。扩展在 Chrome 内,带着用户登录态与 cookie,但写不了文件;Python agent 能写文件跑命令,但拿不到浏览器会话。浏览器还可能被用户关掉。**必须是两个执行器。** |
| **传输层**(队列 / TTL / 长轮询 / 寻址 / 认证) | **合** | 已经是同一个东西写了两遍(§2.1 逐字节对照),且合并顺带关掉 §3 的安全洞 |
| **身份层**(「我的这台机器」) | **合(重点)** | 用户心里的单位是「我的 Mac」,不是「一个 agent_id 加一个 clientId」 |
| **安装引导** | **合** | 一台设备配对一次,而不是两套密钥、四份互相矛盾的文档 |

**为什么这次合并的第一优先级是安全而不是省代码**:见 §3。浏览器桥在隧道部署下
是一个**未认证的浏览器会话接管原语**,而这恰好就是本目标要求的部署形态。

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│  Studio(浏览器,任意位置)                                       │
│  设备选择器:[我的 MacBook  文件✓ 命令✓ 浏览器✓]                │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTPS
┌──────────────────────────▼──────────────────────────────────────┐
│  Tofu Server                                                    │
│  ③ 执行路由层  按 capability 路由:fs/shell → agent,browser → ext │
│  ② 设备注册表  device_id → {capabilities, user_id, endpoints}     │
│  ① 共享队列底盘 TTL / 寻址 / 长轮询 / user-scope fail-closed      │
└───────────┬──────────────────────────────┬──────────────────────┘
            │ 长轮询                        │ 长轮询
┌───────────▼──────────────┐  ┌────────────▼─────────────────────┐
│ 执行器 A:desktop agent   │  │ 执行器 B:Chrome 扩展             │
│ project_* / desktop_*     │  │ 浏览器命令集                     │
│ 文件 · shell · GUI        │  │ tab · cookie · CDP 截图          │
└───────────────────────────┘  └──────────────────────────────────┘
              └────────── 同一个 device_id、同一次配对 ──────────┘
```

---

## 2. 现状拆解(2026-07-27 盘上逐文件核实)

### 2.1 两条桥已经是同一个东西写了两遍

| 维度 | desktop 桥 | browser 桥 | 判定 |
|---|---|---|---|
| 端点 | `POST /api/desktop/poll`(`routes/desktop.py:131`) | `POST /api/browser/poll`(`routes/browser.py:91`) | — |
| **响应体** | `{commands:[{id,type,params}]}`(`bridge.py:461-468`) | `{commands:[{id,type,params}]}`(`_dispatch.py:150-154`) | **逐字节相同** |
| **结果项** | `{id, result, error}`(`_run.py:213`) | `{id, result, error}`(`background.js:289`) | **逐字节相同** |
| 长轮询窗口 | `TOFU_DESKTOP_POLL_WAIT` 默认 8s(`bridge.py:75`) | `TOFU_BROWSER_POLL_WAIT` 默认 8s(`_state.py:83`) | 同值,两个名字 |
| 在线判定 | 15s(`bridge.py:66`) | 15s(`_registry.py:56`) | 同值 |
| 认证头 | `X-Bridge-Secret`(`routes/desktop.py:64`) | `X-Bridge-Secret`(`routes/browser.py:68`) | 同名 |
| 401 信封 | `{error:'bridge_auth_required', hint:…}` | 同一句话(`routes/browser.py:82-88`) | **逐字相同** |
| 队列实现 | 内存 dict + Event + TTL 清扫 + async waiter | 同上,独立一份 | **近似重复** |

`lib/desktop/bridge.py:21` 的注释自陈:「Command Queue (**mirrors lib/browser.py pattern**)」
—— 重复是有意抄的,不是巧合。

这是 charter 已处理过两次的形状:`agent_verdict` 被手抄 4 份 → 收敛;三个
`runtime.py` 67% 逐字节相同 → 抽出 `ProductionRuntime`。**这是第三例。**

### 2.2 关键不对称:desktop 修好了的洞,browser 原样活着

RWA P4a 已为 desktop 桥补齐**每用户作用域**,理由写在
`docs/REMOTE_WORKTREE_DESIGN.md:142`:

> `TOFU_BRIDGE_SECRET` 全局单密钥在多租户 relay 部署下 A 用户的 agent 能领走 B 用户的命令

desktop 侧的落地(实测全链):

| 环节 | 位置 |
|---|---|
| 注册带 `user_id` | `lib/desktop/bridge.py:166,190` |
| 状态端点按租户过滤 | `bridge.py:218-231` `list_agents(user_id=…)` |
| **投递谓词首闸 fail-closed** | `bridge.py:268` `if (cmd.get('user_id') or '') != (poller_user or ''): return False` |
| 入队闸只数自己的 agent | `bridge.py:278-305` |
| 每用户 token(scope `agents:bridge`) | `routes/api_v1/desktop.py:76,108-123` |

browser 侧实测:

- `grep user_id lib/browser/` → **0 命中**;
- `routes/browser.py` 全文 186 行 → **无** `validate_token`、**无** `require_auth`;
- 客户端注册表只存 `{first_seen, last_poll, name, poll_count, chrome_major}`
  (`_registry.py:33-34`)—— **没有 user_id 这个字段**;
- 投递谓词 `_dispatch.py:157-159` 只比对 `target_client`,**没有用户维度**。

### 2.3 打包好的桌面 app 并不是远程控制客户端

`desktop/launcher.py:326-327`:

```python
server_url = f'http://127.0.0.1:{port}'          # 硬编码回环
bridge_secret = (os.environ.get('TOFU_BRIDGE_SECRET') or '').strip()   # 只认环境变量
```

托盘 agent 连的是**它自己刚 spawn 出来的那个本地 server**(`launcher.py:174-185`)。
所以那个 .exe 回答的是「**在我笔记本上跑一个 Tofu**」,不是「**让我远端的 Tofu
碰我的笔记本**」—— 恰好不是本目标要的场景。托盘没有任何字段可填服务器地址、
token 或共享根;README 教的 `--server https://…` 隧道流程是**纯 CLI** 的。

**推论:光写文档办不到「好的安装引导」。** 文档会指着一条二进制走不了的路。
`launcher.py` 必须补配置面(B3),否则引导是空头承诺。

### 2.4 现有安装文档的实测状态

| 文档 | 实测 |
|---|---|
| `docs/INSTALL.md` | 19 个标题,`grep -ci 'bridge\|extension\|desktop\|agent'` = **0**。全篇只讲服务器安装,**一个字都没提这两条桥** |
| `docs/README_EXTENSION.md` | 教三种装法,引用 `dev_extension.sh` / `install_extension.sh` / `install_extension.bat`(:9,:13,:24,:27,:31,:64-66,:75)——`find_files` 实测**三个脚本全部不存在**;文档仍自称 "ChatUI"、指向 `chatui/` 目录 |
| `README.md:343-350` / `README_CN.md:334-342` | 3 步 load-unpacked,**不提 bridge secret** |
| `index.html:1480-1546` 应用内模态框 | 现存**最好**的一份:下载 ZIP → 开发者模式 → 验证 Connected,含同机绝对路径一键复制(`main_toolbar_ui.js:798-812`)与 Chrome 142+ LNA 提示 |
| `docs/chrome-web-store/` | 提交套件,**非用户安装文档**;尚未提交,`REVIEW_RISKS.md:76-89` 自评「拒绝可能性高」 |

即:**没有任何一页可以指给用户看「怎么装这两条桥」。**

---

## 3. 威胁模型(本稿新增,决定优先级)

### 3.1 扩展的权限面(实测)

`browser_extension/manifest.json` 声明 **17 个 permissions + `host_permissions: ["<all_urls>"]`**,
含 `debugger`(CDP 挂载)、`cookies`、`history`、`bookmarks`、`management`、
`declarativeNetRequest`、`clipboardRead`。

且 `background.js:117-135` `autoDetectServer()` 会**扫描已打开的 tab、找标题含
"Tofu" 的页面,自动把它当服务器并开始轮询**。

### 3.2 认证默认档(实测 —— 两道门,不是一道)

**桥自己的门是敞开的。** `.env.example:161`:「Leave empty for LAN-only mode
(**default**)」;`routes/browser.py:63-65`:`expected` 为空 → **直接
`return True`**。桥端点自身默认零认证。

**但它前面还有一道全局门。** `routes/api_v1/auth.py:317-326`:open 模式下
未持凭据的请求,只有 `_OPEN_MODE_ALLOW_REMOTE or _remote_is_loopback()`
才拿到合成 admin 上下文,否则**落进 private 拒绝路径**。实测在真实
`server.app` 上打 `/api/browser/poll`(`TOFU_BRIDGE_SECRET` 未设):

| 对端 | 实测 HTTP |
|---|---|
| `192.168.1.50` | **401** |
| `127.0.0.1` | 200 |
| `::1` | 200 |

日志实证:`Auth: open-mode synthetic admin refused for non-loopback peer
192.168.1.50 on /api/browser/poll`。

> **⚠️ 本节是一次自我推翻,保留过程作为方法论样本(与 charter 的
> `cache_control` 翻转同族)。**
>
> 本稿初版据代码推导写下「LAN 形态未认证即可轮询领命令」,**推导链每一步都
> 成立**(桥函数确实 `return True`、扩展权限确实是 `<all_urls>`+`cookies`),
> **结论却是错的** —— 因为推导只看了 `routes/browser.py` 这一层,没看到
> `auth.py` 的全局 `before_request` 已经在它前面拦了一道。实测 401 才把这个
> 遗漏暴露出来。
>
> **纪律:本仓的威胁模型结论必须实测,推导不算。** 「读了相关文件、逻辑自洽」
> 不足以断言一个攻击面存在或不存在 —— 全局中间件、装饰器、上游 hook 都可能
> 在你读的那个函数之外改变结局。写威胁模型表的每一格,都要有一次真实请求
> 的状态码支撑。

### 3.2b 那道全局门本身就是缺陷(它是纯 IP 判定)

`_remote_is_loopback()`(`auth.py:169-198`)读 `request.remote_addr`,
文档注释自陈「A reverse proxy on the same host still presents 127.0.0.1, so a
loopback-only proxy deployment keeps working」—— 这句话把**缺陷写成了特性**:

- 隧道部署的标准形态就是 nginx / ngrok / cloudflared **跑在同一台机器上**
  反代到 127.0.0.1;
- 此时**公网来的每一个请求** `remote_addr` 都是 `127.0.0.1`,全部拿到
  「回环即可信」的合成 admin;
- 而 `ProxyFix` 从未接线(`pt_30d400a167df4440`:`TOFU_TRUST_PROXY_HOPS`
  全库只出现在 `.env.example` 与一个自建 mini-app 测试里,`grep ProxyFix
  server.py` 零命中),所以**没有任何配置能让它看见真实客户端 IP**。

**所以「回环免认证」在本仓的部署形态下等价于「全网免认证」。**
这既否掉了本稿初版的 IP 豁免方案,也说明那道既有全局闸必须同批根修(§3.4)。

### 3.2c 测试期恒真旁路:`'<local>'`

`auth.py:177-179`:

```python
if addr == '<local>':
    return True
```

Quart 进程内测试客户端**默认上报 `'<local>'`**,被判为回环。后果:

- 任何**不显式传 `scope_base={'client': (...)}`** 的桥测试都在测**豁免路径**,
  「未设凭据 → 200」恒绿,**测不到远程拒绝** —— 这正是现存
  `test_bridge_auth.py` 那批断言至今全绿却没暴露问题的原因;
- 它是 `pt_30d400a167df4440` 的**第三个实例**(又一处「长得像本地就当本地」)。
  生产走 Hypercorn 真 socket 地址,故这行**不是线上漏洞**,但它是一条
  **测试期恒真的旁路**,会让守卫失去区分力。已记进该票,B0 不动它。

**由此得出一条验收硬约束(§8):桥相关的每个测试 MUST 显式传
`scope_base={'client': (...)}`。缺省 client 等于自动获得豁免,任何
「无凭据 → 200」的断言在缺省下都是假绿。**

### 3.3 三种部署形态下的攻击面

每一格都有实测状态码或实测配置支撑,无推导结论。

| 形态 | 谁能打到 `/api/browser/poll` | 实测结局 | 攻击者得到什么 |
|---|---|---|---|
| **回环**(`127.0.0.1`,打包 app 现状) | 本机任意进程 / 任意本地用户 | **200**(实测) | 本机任意进程可冒充扩展领走命令。本机已失陷时风险有限,但**「本机任意用户」≠「我的 agent」**——多用户机器上另一个登录用户也算本机。§3.4 因此不给 IP 豁免 |
| **局域网**(`BIND_HOST=0.0.0.0`,直连无反代) | 同网段任意设备 | **401**(实测,由 `auth.py:317-326` 全局闸拦下) | 已被既有全局闸挡住。**本稿初版判为「未认证放行」是错的**(见 §3.2 自我推翻) |
| **同机反代隧道**(nginx / ngrok / cloudflared → 127.0.0.1,**本目标要的形态**) | **公网任意人** | **200** —— 因为 `remote_addr` 恒为 `127.0.0.1`(§3.2b) | 读任意 tab、`chrome.cookies.getAll` 导出**全站登录态**、`chrome.history`、`debugger` 挂 CDP。**不可接受** |
| **relay 多租户** | 任一已认证租户 | 200 | 领走**任一其他租户**的命令(§2.2:browser 桥无 user 维度)。**不可接受** |
| 任意形态 + `TOFU_OPEN_MODE_ALLOW_REMOTE=1` | 公网任意人 | 200(`auth.py:318` 首条或分支) | 同上。该变量**一开就整体放行**,桥也跟着放行 |

**结论(修正版):真正致命的不是「LAN 未认证」,而是「同机反代下回环判定失效」。**
本目标要的恰好就是隧道部署,所以这一格就是必须关掉的那一格。`<all_urls>` +
`cookies` 意味着它能导出用户**所有已登录站点**的认证会话。

这不是「顺手做掉的安全加固」,而是**目标能否安全交付的前置条件**。一份把用户
引导进这个配置的安装指南,比没有指南更糟。

### 3.4 因此:认证默认档必须翻转,且豁免的是身份不是 IP

**核心判据(owner 2026-07-27 拍板):豁免的是「持有本机进程凭据的 agent」这个
身份,不是「IP 长得像回环」这个表象。** 后者在同机反代下可被公网触发(§3.2b),
前者不能 —— 反代能伪造 IP,但拿不到父进程内存里的随机串。

- **取消 IP 豁免这条路。** `remote_addr` **不参与任何放行判定**。
- **进程内凭据(loopback token)**:打包 app 启动时 `secrets.token_urlsafe()`
  自铸一枚,**只存在于父进程内存**,经函数传参注入同进程 spawn 的托盘 agent
  线程(`launcher.py:_start_computer_control`)。
  **禁止落盘、禁止写环境变量** —— 落盘会把「只有本机进程能读」降级成「任何
  能读该文件的本地用户都能读」,退回 §2.3 那个 `os.environ` 的老路;写 env
  则会被同机任意进程通过 `/proc/<pid>/environ` 读到(同一台机器上的另一个
  登录用户即可)。
- **每用户 token**:复用既有 `agents:bridge` scope(`routes/api_v1/desktop.py:76`),
  零新表。
- **投递首闸**:镜像 `bridge.py:268`,用户不匹配即不可投递,fail-closed。
- **既有全局闸同批根修**:`auth.py:318` 的 `_remote_is_loopback()` 判据替换为
  同一套进程内凭据判定。**只换判据,不动 open-mode 的其余语义**(边界收窄,
  避免变成认证面大重构)。

### 3.4b 不变量:`TOFU_OPEN_MODE_ALLOW_REMOTE` 不能降级桥

**该变量只能放开「普通 UI」的远程访问。桥端点永远要求真凭据,没有任何环境
变量能把它降级。**

理由:桥能取 cookie、挂 CDP、写文件、跑 shell —— 它的失陷后果与「让 UI 可远程
访问」不在同一量级。一个为了「我自己前面挂了 auth 网关」而设的开关,不该顺带
把设备控制面也交出去。

守卫钉死(§8):`ALLOW_REMOTE=1` + 无凭据 + 非回环 → 桥仍 **401**。

---

## 4. 权限审计:17 项逐条 keep/drop

调用计数为 2026-07-27 实测(`grep -o "chrome\.<api>\b" background.js popup.js`)。

| # | 权限 | 实测调用 | 判定 | 依据 |
|---|---|---|---|---|
| 1 | `tabs` | 38 | **KEEP** | 核心:列 tab、按 id 查找、激活、等导航 |
| 2 | `scripting` | 14 | **KEEP** | 核心:注入内容脚本读页面 / 点击 / 填表 |
| 3 | `debugger` | 15 | **KEEP(高危,须显式告知)** | 仅整页截图(CDP `Page.captureScreenshot`)。**权限面远超用途**——挂上 CDP 等于拿到该 tab 的完全控制。安装引导必须明写 |
| 4 | `storage` | 6 | **KEEP** | 存 serverUrl / bridgeSecret / clientId |
| 5 | `cookies` | 3 | **KEEP(高危,须显式告知)** | 任务携带登录态。**这是 §3.3 里最值钱的失窃目标** |
| 6 | `alarms` | 2 | **KEEP** | MV3 service worker 保活,长轮询不被 Chrome 掐 |
| 7 | `notifications` | 1 | **KEEP** | 断连提示 |
| 8 | `history` | 1 | **KEEP(可选能力,应默认关)** | 只在任务显式要求时用。建议做成能力开关而非常驻权限 |
| 9 | `bookmarks` | 1 | **KEEP(可选能力,应默认关)** | 同上 |
| 10 | `downloads` | **1** | **KEEP** | `background.js:1966` `chrome.downloads.download(opts)` —— **见下方 ⚠️** |
| 11 | `activeTab` | **0** | **DROP** | 0 次 `chrome.activeTab`(它本就无独立 API,靠 `tabs` 足够) |
| 12 | `webNavigation` | 0 | **DROP** | 等待导航用 `chrome.tabs.onUpdated`,只需 `tabs` |
| 13 | `clipboardRead` | 0 | **DROP** | 无任何剪贴板读 |
| 14 | `clipboardWrite` | 0 | **DROP** | 无任何剪贴板写 |
| 15 | `declarativeNetRequest` | 0 | **DROP** | 无 DNR 规则集、无调用 |
| 16 | `management` | 0 | **DROP** | 无调用。可枚举/禁用**其他扩展**,审核红旗 |
| 17 | `offscreen` | 0 | **DROP** | 无 offscreen 文档 |
| — | `host_permissions: <all_urls>` | — | **KEEP(不可避免,须显式告知)** | 任务目标站点无法预先枚举 |

**净结果:17 → 10,砍掉 7 项零调用权限。**

> ⚠️ **本审计推翻了 `docs/chrome-web-store/` 的两处内容,必须一并修正:**
>
> 1. **`manifest.store.json` 会静默弄坏 `download` 命令。** 它的 permissions
>    列表(10 项)**不含 `downloads`**,而 `background.js:1966` 真的调用
>    `chrome.downloads.download`,且 `download` 是扩展支持的 27 个 wire 命令之一。
>    按现状提交商店版,`download` 命令会在运行时抛错。正确的 10 项 =
>    现列表 − `activeTab` + `downloads`。
> 2. **`PERMISSIONS_JUSTIFICATION.md:99-108` 的「移除 6 项」表已过期。**
>    应为 **7 项**(补 `activeTab`),且该文档头部写 4.3.0 而 shipped manifest
>    已是 4.5.0 —— 版本已漂移,其「never called」结论必须按本表重新实测过的
>    数字更新,不得再直接引用。
>
> **统一设备默认安装收窄后的 10 项清单**,而非 shipped 的 17 项。

---

## 5. 五条硬约束(落地时逐条自证,任一违反该期不得提交)

### 5.1 Wire 契约单一事实源(继承 RWA §3.1,不得降级)

命令 `type` = **完整工具名**,执行器命令表键 = wire type,**逐字相等**。
禁止任何剥前缀 / 加前缀 / 别名映射 —— 两套格式并存正是
`pt_08a6d1afe79c4dfd`(「Studio 桌面工具全灭」)的成因。

现状:desktop 侧已合规(`_dispatch.py:46-72`,18 个键全带前缀);
**browser 侧不合规** —— wire 是裸名(`list_tabs`,`background.js:319-345`),
LLM 名 `browser_*` 在 `lib/browser/dispatch.py:114-135` 另做映射。
**拍板:扩展侧改成完整工具名**,不加第二层映射。

### 5.2 版本协商先于合并

desktop 有 `protocol_version`(`_run.py:93-106`);**browser 实测 0 命中**
(`grep PROTOCOL_VERSION browser_extension/` 无结果)。

**顺序不可反:必须先给 browser 桥加版本帧,再动 wire 名。** 否则老扩展在
合并当天**静默失效**——用户看到的是「浏览器控制忽然不工作了」,且无任何提示。
加了版本帧,老扩展会**响亮地**报「请升级」。

### 5.3 用户作用域 fail-closed

投递谓词首闸比对用户,**不匹配即不可投递**,镜像 `bridge.py:268`。
两边都空 = 单用户世界,行为逐字节不变(回退档)。

### 5.4 认证默认强制,豁免按身份不按来源

见 §3.4。**`remote_addr` 不得参与任何放行判定** —— 它在同机反代下由公网请求
继承(§3.2b),是可被外部触发的表象。唯一豁免是「持有进程内凭据」,该凭据
不落盘、不进环境变量。

`TOFU_OPEN_MODE_ALLOW_REMOTE` 对桥端点无效(§3.4b),这是不变量:任何环境
变量都不能把设备控制面降级为免凭据。

### 5.5 执行器不合并

`fs`/`shell` 与 `browser` 是两个执行器、两个进程、两套沙箱。设备对象登记
capability 集合,派发按 capability 路由;**能力不在线时诚实报错**,不静默降级、
不跨执行器代偿。

---

## 6. 分期实施(每期独立可交付)

| 期 | 内容 | 主要文件 | 验收 |
|---|---|---|---|
| **B0**(本轮) | **浏览器桥补 user 作用域 + 认证默认强制 + 版本帧**。注册表加 `user_id`;投递首闸镜像 `bridge.py:268`;非回环强制认证;每用户 token 复用 `agents:bridge`;扩展加 `protocol_version` | `lib/browser/queue/_registry.py`、`_dispatch.py`、`_state.py`、`routes/browser.py`、`browser_extension/background.js` | 跨租户投递不可能(NEUTER:摘掉 user 闸 → 必红);非回环无凭据 401;回环仍通;`test_browser_async_poll.py` / `test_browser_queue_ttl.py` 保持绿 |
| **B1** | 抽出共享队列底盘(TTL / 寻址 / 长轮询 / user-scope),两条桥骑上去 | 新 `lib/device_bridge/` | 两桥 wire 逐字节不变;重复实现收敛成一份 |
| **B2** | 统一设备注册表:一个 `device_id`,多 capability;Settings→Devices 一行一台机器 | `lib/device_bridge/registry.py`、`settings/devices.js` | 一台机器两个执行器显示为一行三个能力灯 |
| **B3** | `launcher.py` 配置面(server URL / token / share roots)。~~同机自动把 token 交给扩展(配对码)~~ —— **配对码半已于 2026-07-31 由 owner 取消(D-关票),理由见 §7 #3**;配置面半仍开放 | `desktop/launcher.py` | 打包 app 能连远端服务器 |
| **B4** | 一份 canonical 安装页并入 `docs/INSTALL.md`;重写 `docs/README_EXTENSION.md`;收窄默认安装权限至 10 项 | `docs/INSTALL.md`、`docs/README_EXTENSION.md`、`manifest.store.json` | 用户从零到两条桥都在线,只看一页 |

**B0 是 B1–B4 的前置**,原因见 §3:在洞开着的时候做合并与安装引导,等于把更多
用户更快地引导进一个会话接管原语。

---

## 7. 决策日志(2026-07-27 owner 拍板,本稿视为已决,不再开问)

1. **B0 先行,且认证默认强制**(非 `TOFU_BRIDGE_SECRET`-可选)。每用户 token
   复用既有 `agents:bridge` scope;浏览器注册表补 `user_id`;投递首闸镜像
   `bridge.py:268`。回环可保留有文档记录的豁免,非回环必须认证。
2. **前缀:扩展改用完整工具名。** RWA §3.1 是真实事故产物,不加第二层映射。
   `protocol_version` 同批次落地,版本协商**先于**任何合并。
3. **~~做配对码~~ —— 已于 2026-07-31 被 owner 推翻(D-关票,`pt_ea4dda44ec0e485a` 关闭)。**
   原决定:复制粘贴一次性密钥正是用户把密钥贴到不安全处的成因。同机托盘
   直写 `chrome.storage.local`;远程发短时效一次性码。本条取代 2026-07-26
   `REMOTE_WORKTREE_DESIGN.md:415` 的「仅 Devices 页」选择 —— 那次选择做出时
   托盘根本连不上远端服务器(§2.3),前提已变。
   **推翻理由(2026-07-31 实测):**
   ①「同机托盘直写 `chrome.storage.local`」**机制证伪** —— Chrome 对扩展外部
   只开两条通道(`externally_connectable` 声明的扩展/网页经 runtime.sendMessage;
   `nativeMessaging` 经注册的 native host),**不存在外部进程直写扩展存储的机制**;
   托盘是本机 Python 进程,两者都不是。
   ② 两个替代通道对本部署形态也不可达:`externally_connectable` 需固定扩展 ID
   (load-unpacked 每次安装 ID 随机)且 matches 无法预先枚举自建部署地址;
   `nativeMessaging` 需按平台注册 native host,源码运行用户不可用。
   ③ 现状已达「一次粘贴、永久生效」:B0 落地后(`tests/test_browser_user_scope.py`
   12/12)每用户 token 由 `POST /api/v1/desktop/token` 一键 mint(scope
   `agents:bridge`),`autoDetectServer()` 自动发现 serverUrl,粘贴一次即持久化。
   owner 判定:为一次性步骤建配对码基础设施不划算。**不要再以本拍板为由
   重新发起配对码调查;若未来要重启,须先解决 ① 的通道问题。**
4. **能力模型是主干**:一台设备、登记能力(`fs`/`shell`/`browser`)、两个执行器。
   **不合并执行器。**

### 本稿新增、需在实施中带上的两条

5. **`manifest.store.json` 的 `downloads` 缺失是真缺陷**(§4 ⚠️),B4 修正;
   在此之前不得按现状提交商店版。
6. **`PERMISSIONS_JUSTIFICATION.md` 的「移除 6 项」表已过期**(应为 7 项,
   且版本 4.3.0 vs shipped 4.5.0),B4 按 §4 实测表重写。

---

## 8. 测试与验收(对齐仓内既有范式)

- **failing-first**:B0 的每条断言先在未修码上跑红,记录红的条数;
- **⚠️ `scope_base` 强制(§3.2c)**:桥相关的每个测试 MUST 显式传
  `scope_base={'client': (ip, port)}`。Quart 测试客户端缺省上报 `'<local>'`
  会被 `auth.py:177` 判为回环 → 自动获得豁免,任何「无凭据 → 200」的断言在
  缺省下都是**假绿**。这是本套件最容易悄悄失效的一点;
- **NEUTER 三发**,逐发精确咬:
  1. 摘掉 user 闸 → 跨租户投递放行 → 跨租户测试必红;
  2. 摘掉桥的非回环认证 → 未认证远程轮询放行 → 401 断言必红;
  3. 摘掉 `ALLOW_REMOTE` 对桥的无效化 → 设了该变量即放行桥 → §3.4b 守卫必红。

  逐发 `cp` 备份 → 毒化 → 跑 → 还原 → `diff -q` 逐字节确认(**禁 git stash**,
  树上常有兄弟 WIP);
- **相邻环零新增红**:`test_bridge_auth.py`、`test_browser_async_poll.py`、
  `test_browser_queue_ttl.py`、`test_desktop_bridge_addressing.py`;
- **守卫类型**(charter 2026-07-27):跨租户不可投递属**行为守卫**,必须断言
  **结果**(「B 的命令不会出现在 A 的 poll 响应里」),而不是断言某个私有函数
  存在或某行源码长什么样;
- **共享 HEAD 纪律**:精确 pathspec 提交,sibling WIP 零触碰。

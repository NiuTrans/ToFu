# Tofu 远程工作树代理(Remote Worktree Agent, RWA)设计稿
—— Studio 无缝改本地代码(Windows / macOS),不共享文件系统

> **状态:IN PROGRESS — 方向已拍(2026-07-25「意图共享,非文件系统共享」);§8 五项实施拍板已落(2026-07-26,全部按建议项:2A+3A+4A+5A+6A);**P0–P5 全部落地**(2026-07-26);① 号先行票已闭环(`c1579401`)。**
> Board epic:`pt_7977b1e823454e5b`。
> 关联潜伏 bug(先行票,不进本设计批):`pt_08a6d1afe79c4dfd`(desktop wire 前缀错配,§2.3)。
> 本稿全部事实性结论均于 2026-07-25 在盘上逐文件核实(§2 标注文件:行号)。

---

## 0. 问题与拍板

**问题。** 现状部署:服务器在远端,Web 客户端从任意位置连服务器。反过来——
**服务器(Studio)能不能控制用户的本地客户端?** 例:Studio 里一次会话直接改
用户 MacBook / Windows PC 上 `~/code/myapp` 的代码文件。直觉解是「共享文件系统」。

**拍板(2026-07-25):不共享文件系统,共享意图。**

- 文件永远只有**一棵真实副本**,在用户本地磁盘上;服务器**不建副本、不镜像、不同步**。
- 所有写操作经本地代理**直达本地磁盘**;LLM 看到的是同一套工具语义
  (`write_file` / `apply_diff` / `run_command`),执行时路由到本地代理。
- 本地机器在概念上是服务器的一个「外设 worktree」——如同 `git worktree` 把另一份
  检出挂到同一个对象库,RWA 把「本地磁盘上的一棵工作树」挂进 Studio 的「项目」概念。

**为什么否掉文件系统共享**(三条,对应本仓三笔血泪账):

| # | 理由 | 仓内证据 |
|---|---|---|
| 1 | 服务器建副本 = 把「本地↔服务器」拉进共享树冲突域(本地 IDE 改、agent 改副本、回传时三方冲突) | JOURNAL「共享树纠缠」第一至九弹:sibling WIP 互扫、NC 治愈误伤合法提交、apply_diff 报成功未落盘 |
| 2 | 共享 FS 需要双向稳定连接 + 守护进程 + 冲突解决策略(NFS/Syncthing/rsync),且把冲突域从单点扩成三点 | `docs/PROJECT_BRAIN_WORKTREE_ISOLATION.md` §0:authorship-by-inference 是灾难根源 |
| 3 | 权限模型跨平台不一致(UID/GID/ACL),安全边界是整棵目录树 | `lib/project_mod/abs_path_guard.py` 已证明:路径策略必须在「执行的那台机器」上判定(§3.5) |

---

## 1. 总体架构(三层)

```
┌─────────────────────────────────────────────────────────────────┐
│  Studio(浏览器,任意位置)                                       │
│  项目选择器:[server:/srv/chatui] [remote:macbook:~/code/myapp]  │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTPS / WebSocket
┌──────────────────────────▼──────────────────────────────────────┐
│  Tofu Server                                                    │
│  ③ 执行路由层  handlers/project.py:按 project 类型路由执行器     │
│  ② 工具投影层  tools/registry:ToolContext.project_remote         │
│  ① 桥接层      desktop/bridge.py:按 agent_id 寻址的命令队列      │
│                routes/desktop.py:poll v2(注册帧 + 寻址下发)     │
└──────────────────────────┬──────────────────────────────────────┘
                           │ 长轮询(agent 主动拉取,零入站端口/零 NAT 穿透)
┌──────────────────────────▼──────────────────────────────────────┐
│  用户本地机器(Windows / macOS)                                  │
│  ① 本地执行引擎 desktop_agent:project_* 命令集                  │
│     路径校验(对着声明的 share_roots)/ snapshot-before-write /  │
│     freshness 门 / 流式 run_command / 进程树 kill               │
└─────────────────────────────────────────────────────────────────┘
```

三层分工(与「产出底盘」设计稿同一范式:底盘 / 配方 / 手册):

1. **本地执行引擎(agent 侧)**——文件操作 + 命令执行 + 安全网,全部在本地完成,
   服务器永远碰不到文件内容以外的任何东西。
2. **工具投影层(server 侧 registry)**——决定「本会话给 LLM 看的 schema」,
   工具名不变,description 注明在本地执行(见 §4.2 同名策略)。
3. **执行路由层(server 侧 executor)**——按会话绑定的项目类型,把同一名称的
   工具调用路由到「服务器文件系统」或「某台具体机器的某个共享根」。

---

## 2. 现状拆解(2026-07-25 盘上逐文件核实)

### 2.1 已有的地基(不要重造)

| 资产 | 位置 | 现状 |
|---|---|---|
| 本地代理轮询循环 | `lib/desktop_agent/_run.py:run_agent` | 长轮询 `/api/desktop/poll`,断线指数退避,`stop_event` 干净停机 |
| 命令调度表 + 权限分级 | `lib/desktop_agent/_dispatch.py` | 12 个系统级命令;deny-by-default 三层(`allow_write`/`allow_exec`/`allow_gui`),`system_info type=kill` 参数级 exec 门 |
| 进程内命令队列 + 异步长轮询 | `lib/desktop/bridge.py` | `send_desktop_command` 阻塞 RPC;`take_pending_commands_async` asyncio.Event 唤醒,不钉 worker 线程;命令 TTL 90s |
| Bridge 认证 | `routes/desktop.py:_check_bridge_auth` | `TOFU_BRIDGE_SECRET` 全局单密钥,timing-safe 比较,审计日志 |
| LLM 工具 schema | `lib/desktop_tools.py` | 10 个 `desktop_*` 工具;`desktop_move_file` 刻意不暴露 |
| 工具注册接缝 | `lib/tools/registry/_build.py:_build_desktop` | `desktopEnabled` 开关 + agent 在线双门 |
| 托盘打包 | `desktop/launcher.py` | PyInstaller 单 app,托盘开关 + 权限分层勾选,权限 dict 热生效 |
| 项目路径解析 | `lib/project_mod/` | `project_path` 全程显式参数传递,无 `os.getcwd()` 依赖(worktree 隔离设计稿 §3.1 已证) |

### 2.2 缺的四块(= 本设计的建造面)

1. **agent 无身份**:`lib/desktop/` 全目录 grep 无 `agent_id`/`X-Agent` 痕迹。
   `take_pending_commands` 把**全部** pending 命令交给**任意**轮询者——
   两台机器(Win+Mac)同时挂着时,给 Mac 的命令会在 PC 上执行。
   这不是「缺路由」,是**错投递**。
2. **无项目级命令**:agent 只有系统级命令(list/read/write/run/screenshot/GUI),
   没有 `apply_diff` / `grep_search` / `find_files`——Studio 改代码的核心工具集。
3. **本地写入零安全网**:服务器端写文件有 freshness 门 + `.tofu/file-history`
   快照 + `.tofu_trash` 回收;agent 侧裸写,改错无 undo。
4. **run_command 不平价**:agent 侧 `cmd_run_local` 批量输出 + 默认 30s 超时
   (`_exec.py`)——`npm test` / dev server 直接废掉;无流式、无进程树 kill。

### 2.3 顺带抓获的 HEAD 活 bug(先行票,不进本设计批)

**`pt_08a6d1afe79c4dfd` — desktop 工具 wire 前缀错配,Studio 桌面工具全灭。**

- 唯一活路径 `lib/tasks_pkg/handlers/misc/_agents.py:46 _run_desktop`:
  `cmd_type = fn_name.replace('desktop_', '', 1)` —— 剥掉前缀入队;
- agent 侧 `COMMANDS` 键全部**带**前缀(`lib/desktop_agent/_dispatch.py`);
- 实测:`desktop_list_files` → wire 入队 `'list_files'` → agent 回
  `Unknown command: list_files`(2026-07-25 进程内实测,输出见票);
- 保留完整前缀的 `routes/desktop.py:execute_desktop_tool` 是**零调用方死代码**;
- 复现测试:`tests/test_desktop_cmdtype_parity.py`(一绿一 xfail strict,
  修复后 XPASS 转红提醒摘标记)。

**另一个已核实的批准门洞**:`ToolSpec('desktop', _build_desktop)` 未声明
`write_tools`(`lib/tools/registry/_build.py:335`),而 `_WRITE_TOOLS` 分区 =
`_WRITE_TOOLS_BASE` ∪ ToolSpec 声明(`lib/tasks_pkg/tool_dispatch/_flags.py:79-92`)
—— `desktop_write_file` / `desktop_run_command` 等**既进并行派发池(竞态)又绕过
Manual 写批准门**(`_approval.py` 只拦分区内的工具)。本设计 P3 一并关掉(§5 P3),
不另开票,因为它只是「未声明」,补声明即是修复。

---

## 3. 五条硬约束(owner 2026-07-25 复核拍板,不可降级)

> 每一期落地时必须逐条自证;任何一期违反任一条,该期不得提交。

### 3.1 Wire 契约单一事实源

命令 `type` = **完整工具名**(`desktop_list_files` / `project_write_file`),
agent 命令表键 = wire type,逐字相等。禁止任何形式的剥前缀/加前缀/别名映射
——两套格式并存正是 ① 号 bug 的成因。契约守卫:
`tests/test_desktop_cmdtype_parity.py`(已入库),P0 起每期必须保持其绿
(修复 ① 后摘 xfail,转永久守卫)。

### 3.2 Agent 身份 + 用户维度绑定

- **注册帧**:agent 每次 poll 携带 `{agent_id, machine_name, platform,
  protocol_version, share_roots:[{name, path}]}`;`agent_id` 为本地持久化 uuid
  (首次启动生成,存数据目录)。服务器维护 agent 注册表(内存 + 重启可丢),
  `connected` 判定沿用 15s 窗口惯例。
- **寻址投递**:命令入队时携带目标 `agent_id`;`take_pending_commands(agent_id)`
  只下发该 agent 的命令。**兼容回退**:恰好一个 agent 在线且命令未寻址时
  发给该 agent(单agent 现状字节不变);多 agent 在线时未寻址命令**挂起并报诚实错**,
  绝不随机投递。
- **用户绑定**:`TOFU_BRIDGE_SECRET` 全局单密钥在多租户 relay 部署下
  A 用户的 agent 能领走 B 用户的命令。Bridge token 改为**每用户**:
  authed UI 颁发(Settings → Devices),服务器存哈希,agent 持 token,
  服务器映射 token→user_id,命令继承 task 的用户作用域,跨用户命令不可见。
  单用户部署配 `TOFU_BRIDGE_SECRET` 时行为与今日一致(回退档)。

### 3.3 本地写入安全网平价

- **snapshot-before-write**:agent 侧每次写/改前,把原文件快照进
  `<share_root>/.tofu/file-history/<md5(绝对路径)>/<epoch>`(镜像服务器
  `commit_round` 语义),写失败可回滚;该目录已被各消费方忽略。
- **freshness 门**:agent 按会话跟踪「读过的文件 mtime/sha」,外部(IDE/用户)
  改过的文件拒绝写入,重读后才放行——语义与服务器 freshness 门一致
  (含 JOURNAL 续9/续14 的教训:**重读必须刷新令牌**)。
- **写批准门闭合**:给 `ToolSpec('desktop')` 补声明
  `write_tools={'desktop_write_file','desktop_move_file','desktop_run_command',
  'desktop_open_app','desktop_open_file'}`(`desktop_system_info` 保持豁免,
  其 kill 分支已由 agent 侧参数级 exec 门把守;GUI/screenshot 走 allow_gui 层,
  不进写分区);远程项目的写工具沿用服务器同名(`write_file` 等),
  天然继承 `_WRITE_TOOLS_BASE` 的串行派发 + Manual 批准门(§4.2)。

### 3.4 run_command 平价下限

本地执行必须达到服务器端 `run_command` 的能力下限:

| 能力 | 服务器端 | agent 侧方案 |
|---|---|---|
| 流式输出 | PTY streaming + `_partialOutput` 断线重放 | poll 周期内分片上传(`stream_seq` 递增),服务器拼帧 |
| 进程树 kill | process-tree kill | `psutil.children(recursive=True)`(已在依赖清单);Windows 注记 Job Object |
| 破坏性命令守卫 | `lib/project_mod/command_analysis.py` | **直接 import 复用**——agent 跑的就是 tofu 代码库,无需重写 |
| 超时 | 长超时可调 | 参数透传,默认放宽(建议 300s),上限由 allow_exec 层把守 |

### 3.5 路径校验下沉 agent 侧

`abs_path_guard._within_registered_root` 拿**服务器端**注册的 root 做 realpath
判定——`C:\Users\...` / `~/code/myapp` 在服务器上永远不在任何 root 内,
若在服务器判定则全拒。因此:

- **agent 侧**:对**自己声明的** `share_roots` 做 realpath 判定(symlink 跟随、
  Windows 大小写不敏感/UNC/盘符、macOS 默认 APFS 大小写),越界即拒;
- **服务器侧**只做意图级校验:root 名已知、目标 agent 在线、用户作用域匹配;
- `abs_path_guard` 不适用于远程路径(它不碰远端文件系统),远程调用在路由层
  分流,根本不进 `_resolve_write_path`。

---

## 4. 关键机制设计

### 4.1 Poll 协议 v2(注册帧 + 寻址)

```
POST /api/desktop/poll
Headers: X-Bridge-Secret(回退档) 或  Authorization: Bearer <per-user bridge token>
Body: {
  "agent": {"agent_id":"...", "machine_name":"MacBook-Pro", "platform":"darwin",
            "protocol_version":2, "share_roots":[{"name":"myapp","path":"~/code/myapp"}]},
  "results": [...],            # 与今日相同
  "streams": [{cmd_id, seq, stream, data, done}]   # 新增:run_command 流式分片
}
→ 200 {
  "commands": [{id, type, params, agent_id}],      # 只含本 agent 的命令
  "protocol_version": 2
}
```

- 旧 agent(无注册帧,protocol v1):恰好一个在线时按回退档服务,否则 409 提示升级。
- 服务器注册表内存态即可(重启即重注册,agent 1s 轮询自动恢复),不落库。

### 4.2 工具名策略:同名路由(拍板项 §8.3,建议采纳)

远程项目**沿用同一套工具名**(`write_file`/`apply_diff`/`run_command`…):

- **Prompt-cache 稳定**:schema 不变,切换项目类型不破前缀;
  description 变化(注明本地执行)走既有的一次性 latch-clear 模式
  (`ToolContext.project_ready` / `multiroot_active` 的 OFF→ON 先例)。
- **批准门/串行派发/写分区全部继承**:`write_file` 等本就在 `_WRITE_TOOLS_BASE`,
  零新增机制满足约束 3.3 第三条。
- **路由在执行器层**:`_handle_project_tool` 读会话绑定的 `project_remote`
  (`''` | `{agent_id, root}`),空则走 `_EXEC_HANDLERS`(今日路径,字节不变),
  非空则翻译为 `project_<fn>` 命令寻址入队。
- 否决的备选:`remote_*` 前缀新名——schema 翻倍、批准门要重声明两套、
  模型要学两套语义,全是成本没有收益。

### 4.3 Agent 侧项目命令集(与服务器工具一一对应)

| wire type | 对应服务器工具 | 备注 |
|---|---|---|
| `project_list_dir` | `list_dir` | root 相对路径 |
| `project_read_files` | `read_files` | 沿用 maxSize 上限;图片/PDF 转 base64 分段 |
| `project_write_file` | `write_file` | snapshot-before-write + freshness 门 |
| `project_apply_diff` | `apply_diff`/`apply_diffs` | 同上;read-before-edit 门 agent 侧自证 |
| `project_grep_search` | `grep_search` | 复用 `lib/project_mod` 的 ignore 规则 |
| `project_find_files` | `find_files` | 同上 |
| `project_run_command` | `run_command` | §3.4 平价下限 |

实现要点:agent 与服务器同代码库,`command_analysis`、ignore 规则、
快照语义全部 **import 级复用**,不重写。权限映射:`project_*` 写族归
`allow_write`,`project_run_command` 归 `allow_exec`(与 desktop 族一致)。

### 4.4 离线 / 断线语义

- agent 离线:命令在队列等到 TTL(90s 现值,远程写命令建议放宽到 300s)
  后报诚实错(「目标机器不在线」),模型可见,UI 徽标诚实呈现;
- agent 重连:重新注册,**不重放**已过期/已解决的命令(队列以 event 状态为准,
  现有 `resolve_results` 语义不变);
- 写命令不幂等,TTL 过期即死,绝不延迟补投(约束 3.2 的「绝不随机投递」延伸到时间轴)。

### 4.5 大 payload

- 队列 JSON 现有 500KB 截断(`_run.py`)→ 读:沿用 `maxSize` 上限;
  写:>2MB 分片,agent 侧收齐后原子拼装(临时文件 + rename);
- 截图/图片沿用 base64 + maxDimension 降采样。

---

## 5. 分阶段实施(每期 env-gated,独立可落地)

> 总闸:`TOFU_REMOTE_WORKTREE`(默认 `off` = 字节不变)。P0 是全局前置;
> ① 号 bug(`pt_08a6d1afe79c4dfd`)独立先行,不在本 epic 任何一期内。

| 期 | 内容 | 主要文件 | 验收 |
|---|---|---|---|
| **P0** ✅ | **Bridge 身份与寻址**(已落地 2026-07-26):poll v2 注册帧、agent 注册表、寻址投递、多 agent 未寻址拒发报错、单 agent 回退档 | `lib/desktop/bridge.py`、`routes/desktop.py`、`lib/desktop_agent/_run.py` | 双假 agent 并发 poll,命令各归其主;未寻址命令拒发且模型收到诚实错;单 agent 回退档字节不变 |
| **P1** ✅ | **Agent 项目命令集 + 安全网**(已落地 2026-07-26):`project_*` 七命令、share_roots 路径校验、snapshot-before-write、freshness 门(重读刷新令牌) | `lib/desktop_agent/_project.py`(新)、`_dispatch.py`、`config.py` | 越界路径全拒(含符号链接/兄弟前缀/绝对路径);快照可回滚;外部改动后写入被拒、重读后放行;`.tofu` 对项目工具不可见 |
| **P2** ✅ | **run_command 平价**(已落地 2026-07-26):流式分片、进程树 kill、删除目标锁根、超时放宽 | `lib/desktop_agent/_exec.py`、`_project.py`、`_run.py`、`bridge.py`(流帧) | 长命令分片按 seq 稠密上行;kill 后子进程全灭;`rm -rf ~` 类被拦;30s+ 命令不再误杀 |
| **P3** ✅ | **工具投影 + 执行路由**(已落地 2026-07-26):`ToolContext.project_remote`、`_handle_project_tool` 路由、`ToolSpec('desktop')` 补 `write_tools` 声明(关批准门洞) | `_spec.py`、`_build.py`、`handlers/project.py`、`lib/desktop/remote.py`(新) | 同名 schema 不变仅描述提示;远程 `write_file` 按 agent_id 寻址;总闸 off 字节不变;状态逐请求生效;批准门洞闭合 |
| **P4**(P4a ✅ + P4b-1 ✅ + P4b-2a ✅ + P4b-2b ✅ 流帧 UI 2026-07-26) | **入口与前端**:每用户 bridge token ✅、poll 认证+用户作用域 ✅、`agent_run remote:` 绑定 ✅、Settings→Devices 页 ✅(agents 表 + 颁发/吊销,scope 化 DELETE 不借 admin)、**伪路径 `remote:<agent>:<root>` 复用 projectPath 持久化** ✅ / 项目选择器远程分组(P4b-2)、流帧 UI(P4b-2) | `agent_run.py`、`routes/desktop.py`、`bridge.py`、`remote.py`、`api_v1/desktop.py`、`settings/devices.js` ✅ / `project.js`、流渲染(P4b-2) | 后端链+Devices 页全绿;token 错/无 scope 即 401;跨用户 fail-closed;离线灰显(P4b-2) |
| **P5** ✅ | **Project Brain 集成**(已落地 2026-07-26):post/claim 时远程绑定 token 自动并入 write_set,dispatch 降级串行化 | `lib/conversations/project_board.py` | 两会话同根 write_set 重叠 → 软降级不同时 dispatch;不同根不互斥;NEUTER 实证合并承重 |

- **P0 落地注记(2026-07-26):** 注册帧/注册表/心跳/寻址谓词(`_deliverable`)/入队闸
  (`_addressing_enqueue_error`)/kill switch(`TOFU_DESKTOP_ADDRESSING=0`)+ agent 侧稳定身份
  (`lib/desktop_agent/config.py`,首启生成持久化)+ status 端点 `agents` 列表;
  套件 `tests/test_desktop_bridge_addressing.py` **24 测**(含双 NEUTER)。拍板②A 兑现:
  v1 轮询者在无 v2 注册的世界里 wire 投影键逐字节不变(`{id,type,params}`)。
  每用户 token(§3.2 第三条)属 P4(⑤A),P0 不打断现有全局 secret。

- **P1 落地注记(2026-07-26):** `lib/desktop_agent/_project.py`(333 行)七命令;路径校验严格
  root-relative + realpath containment;freshness 门(mtime_ns+size 双因子,外部改动必触);
  快照 `<root>/.tofu/file-history/<md5>/<epoch_ns>`;grep/find 复用 `lib/project_mod/read_tools`
  (ignore 规则 import 级共享,`IGNORE_DIRS` 补 `.tofu`);`project_run_command` 为基础平价
  (dangerous + catastrophic-delete 守卫 import 复用、cwd 锁根、timeout 放宽至 300s)——
  流式/进程树 kill 属 P2。套件 `tests/test_desktop_agent_project.py` **36 测**(含双 NEUTER:
  剥 freshness 门 → 陈旧写放过;剥路径校验 → 逃逸写出根外)。

- **P2 落地注记(2026-07-26):** ①agent 执行离开 poll 循环(`_run.py` 拦截 `project_run_command`
  → `start_project_run` 后台线程,心跳不再被长命令卡过 15s 窗口);②流帧契约
  `{cmd_id, seq, stream, data, done}`,seq 稠密唯一、done 居尾,outbox 断线重发
  由服务器 `resolve_streams` **按 seq 去重**拼帧(`get_command_stream` 增量读,
  TTL 90s 清扫);③`os.read` 原始 fd 分片(不等管道满/进程退出);④超时进程树 kill
  (psutil.children(recursive),软依赖降级 proc.kill);⑤**删除目标锁根守卫**:
  `command_analysis._is_catastrophic_delete` 的锁根规则只对服务器 restricted 主体生效,
  agent 侧自建 `_check_delete_targets_within`——`rm -rf ~` 类拦下;⑥平价实证:
  `rm -rf /abs` 连根内也拒(DANGEROUS_PATTERNS[0] 与服务器同一条)。
  **〔⑥已被取代,2026-07-28〕** 该 blunt regex 把 `rm -rf /tmp/wt_fill` 这类
  scoped 临时清理也恒拒(误伤,见 tests/test_run_command_rm_rf_scoped.py),
  已全库移除;删除命令现统一由参数解析守卫裁决——深度 <2 拒(⑤的
  catastrophic 深度规则)、越 share root 拒(⑤的锁根守卫,sudo/doas 前缀
  可看穿)、根内 scoped 删除(含绝对路径)放行,与服务器端规则一致。
  套件 `tests/test_desktop_exec_streaming.py` **18 测**(含 NEUTER:剥锁根守卫 → 越界删除放过)。

- **P3 落地注记(2026-07-26):** ①绑定契约单一事实源 `lib/desktop/remote.py`
  (总闸 `TOFU_REMOTE_WORKTREE` + `cfg['project_remote']={agent_id,root}`,
  投影与路由共用,防两面漂移);②投影:`with_remote_hint` 只改描述、名称+参数
  schema 逐字节不变(拍板 3A),OFF→ON 一次性 latch-clear(第三个 sticky,
  镜像 project_ready);③路由:`_handle_project_tool` 在 content_ref 解析后分流,
  服务器 FS 门刻意不适用(agent 自守,有测试钉死);未映射工具(apply_diffs/
  insert_content(s)/create_project/read_files 批量/inspect_image)诚实报错;
  远程 run_command 桥超时=命令超时+30s(流帧已在桥内,UI 渲染属 P4);
  ④`ToolSpec('desktop')` 补 provides(10 个 LLM 可见名)+ write_tools(5 个)——
  desktop 写/执行工具进串行写分区,批准门洞闭合。
  套件 `tests/test_remote_worktree_routing.py` **19 测**(含 NEUTER:剥路由 → 落回服务器路径)。
  **顺带抓获潜伏 bug(独立批 `c1685520`):** `_build_rg_cmd/_build_grep_cmd` 的
  `list(IGNORE_DIRS)[:30]` —— set 超 30 条后按进程哈希种子随机丢排除项,
  node_modules 以 ~40% 概率泄回 grep 结果(P1 套件首个行为断言暴露)。

- **P4a 落地注记(2026-07-26,后端半):** ①bridge token 复用 api_keys 生命周期
  (新 scope `agents:bridge`,零新表;Settings→Keys 已可管)——拍板 5A 的 Devices 页
  属 P4b;②poll 认证顺序:全局 secret(legacy 超户)→ per-user token → 401,
  user_id 打进注册表;③命令用户作用域 fail-closed:`_deliverable` 首闸、入队闸
  按 caller 过滤在线集、回退档只数自己的 agent、user_id 永不上 wire;
  ④`agent_run` config 别名 `remote='<agent>:<root>'`(在线/root 已声明/用户匹配
  三重校验,拒则诚实 400)+ `audit_log`;⑤远程绑定隐含 project_enabled
  (model_config 派生);⑥status 端点按 caller user 过滤。
  套件 `tests/test_remote_worktree_entry.py` **29 测**。
  **过程事故(共享树):** sibling 的 `lib/llm_sanitize/_gateway.py` 破窗 WIP
  (IndentationError)两度翻转阻断全仓 import;无 live peer,已 `git stash` 保管
  (stash@{0},恢复 `git stash pop`),树恢复后全绿。

- **P4b-1 落地注记(2026-07-26,Devices 页):** ①**伪路径**:conv.projectPath =
  `remote:<agent>:<root>` 复用全部既有持久化机制,`resolve_conv_config` 翻译为
  `cfg['project_remote']` 并清掉 projectPath(总闸 off 逐字节不翻译)——P4b-2 的
  选择器远程分组因此只是「产出一个伪路径」,零新持久化管道;②Devices 三端点:
  `GET /devices`(agents+tokens 元数据按 caller 过滤)、`POST /token`(原文只回一次,
  201)、`DELETE /token/<id>`(属主+scope 双校验,**不借 admin 宽权**);
  ③面板机制:tab 按钮 + `SETTINGS_PANEL:devices` 标记 + 片段自动注入 +
  `settings/devices.js` 入 `_BUNDLE_FILES` + core_panel 填充钩子;
  ④套件:后端 12 测(`test_remote_worktree_devices.py`)+ 前端 8 测
  (`test_frontend_devices_tab.py`,含 NEUTER 钩子承重 + jsdom 11 探针)。

- **P4b-2a 落地注记(2026-07-26,选择器远程分组,拍板 6A 兑现):** ①伪路径短路:
  `_restoreConvProject` 遇 `remote:` 会话**绝不调 setPaths**(服务器无法扫描,
  调了即 400/误清——NEUTER 实证),渲染合成 bar 态,徽章显示 `agent:root`;
  ②目录浏览弹窗顶部「远程设备」分组:在线 agent 共享根一键
  `mpAddBrowsedPath('remote:…')`(与本地文件夹同一套工作区/保存/持久化机制),
  离线 agent 灰显不可加,无 agent 整段隐藏;③套件
  `tests/test_frontend_remote_picker.py` 5 测(装配 3 钉 + jsdom 13 探针 + NEUTER)。
  流帧 UI(远程 run_command 实时输出)属 P4b-2b。

- **P4b-2b 落地注记(2026-07-26,流帧 UI,前端零改动):** 远程 run_command 的桥内流帧
  由 watcher(0.25s 轮询)按偏移增量喂进服务器 run_command 既有的
  `_make_run_command_progress_cb` → `tool_progress` SSE 通道,终端块增量渲染**无需任何
  前端改动**;`send_desktop_command` 接受预置 `cmd_id`;终态 meta 按终端块契约成型
  (toolName/command/output/exitCode);`GET /api/v1/desktop/streams/<id>` 调试端点。
  套件 `tests/test_remote_stream_ui.py` 6 测,含**真桥+真 agent 执行器 e2e**:
  命令在飞(sleep 未醒)时 `tool_progress` 已含前半输出。
- **E2E/文档补强(2026-07-26,先行于 P5):** kill-switch 演练(双总闸全关 → 伪路径
  不翻译 + 桥 drain-all,与 RWA 之前逐字节一致,流帧套件第 6 测);README/README_CN
  用户文档(Remote Worktree 小节:旅程 + 安全边界 + 总闸)。P5 行(Project Brain
  write_set 集成)仍待——注意跨会话同根写的串行语义已由 P1 freshness 门天然保证
  (后写者被拒需重读),P5 要补的是脑内 dispatch 层的声明与互斥。

- **P5 落地注记(2026-07-26):** `_conv_remote_token`(读 conv settings 的伪路径绑定)
  + `_merge_remote_token`(幂等去重)在 **post_task 与 claim_task 两处**把
  `remote:<agent>:<root>` token 并入 epic write_set(claim 与 CAS 同事务);
  既有 `_paths_intersect`/`select_dispatchable` 零改动——`:` 分隔天然无前缀
  containment(`app` vs `app2` 不误撞)。效果:同根 epic 被软降级不同时 dispatch,
  不同根不互斥。套件 `tests/test_remote_brain_writeset.py` **14 测**
  (post/claim 合并、去重、语义矩阵、dispatch 集成、NEUTER 合并承重)。
  跨会话同根写的**数据层**串行仍由 P1 freshness 门兜底(后写者被拒需重读);
  本层是脑内 dispatch 的**调度层**互斥,两道防线语义互补。

工作量估算:P0 ~250 行 / P1 ~400 行 / P2 ~200 行 / P3 ~150 行 / P4 ~300 行+前端 / P5 ~80 行,
全部为薄接缝改动,无新框架。

---

## 6. 风险与开放问题

| 风险/问题 | 说明 | 缓解 |
|---|---|---|
| Windows 路径/权限 | 盘符、UNC、ACL、UAC 提权边界 | P1 路径校验参数化覆盖 Win 形态;提权不做(用户态够覆盖代码目录) |
| 协议版本漂移 | agent 与服务器版本不同步 | 注册帧 `protocol_version` 协商;v1 agent 回退档;打包 app 自带更新检查(`launcher.py` 已有) |
| bridge token 泄漏 | token = 本地 RCE(分层内) | 每用户 + 可吊销 + 服务器存哈希;审计日志沿用 bridge_auth_fail 通道 |
| 长命令 vs 轮询节奏 | 1s poll 决定流式分片最小粒度 | 分片随每次 poll 批量上行,`_partialOutput` 模式重放;延迟上限 = poll 间隔,可接受 |
| 本地 FUSE/网络盘 | 本地工作树在 SMB/sshfs 上 | agent 是本地进程,快照与写入同盘;慢但正确,不做特殊处理 |
| 多用户共用一台机器 | 同一台 PC 两个用户各起 agent | agent_id + token 双键;share_roots 按用户隔离 |
| LSP 级 IDE 集成(远期) | 实时光标/补全/诊断 | 出路线图,不在本 epic;RWA 命令粒度先覆盖 80% 场景 |

---

## 7. 测试与验收(对齐仓内既有范式)

- **契约守卫**(已入库):`tests/test_desktop_cmdtype_parity.py` —— wire type ⇔
  agent 命令表逐字相等;① 修复后摘 xfail 转永久守卫。
- **每期纪律**:failing-first + NEUTER(剥核心机制即转红)+ `--collect-only` 闸
  + 相邻套件零新增红;共享 HEAD 精确 pathspec 提交,sibling WIP 零触碰。
- **P0–P3 双端契约测试**:假 agent(直接调 dispatch)vs 假桥(monkeypatch
  `send_desktop_command`),断言 wire 双向字段;不依赖真进程。
- **真 e2e(标 slow)**:起真 agent 子进程 + 真 Quart app,完成一次
  `write_file` 闭环(写 → 快照 → 重读 → freshness 拒绝 → 重读 → 写)。
- **前端(P4)**:JSDOM 探针(项目选择器列出远程根 / 离线灰显 / token 吊销后
  项目不可用诚实态)。

---

## 8. 拍板请求

> **拍板结果(2026-07-26,board question-block 一键答复):全部按建议项 ——
> 2A(v1 单 agent 回退档)+ 3A(同名路由)+ 4A(远程写默认 Manual)+
> 5A(Settings → Devices 设备管理页)+ 6A(项目面板加「远程设备」分组)。
> P0 当即开工并已落地。**

1. ~~总体方向~~ ✅ **已拍(2026-07-25):意图共享,不共享文件系统。**
2. **Poll v2 兼容策略**:旧 v1 agent(无注册帧)——(A) 单 agent 回退档继续服务
   (建议)/ (B) 强制升级,拒绝服务。
3. **工具名策略**:(A) 同名路由(建议,§4.2)/ (B) `remote_*` 前缀新名。
4. **远程写批准门默认档**:(A) 默认 Manual(建议,与服务器端写一致)/ (B) 跟随会话现有档位。
5. **每用户 bridge token 的 UI 形态**:(A) Settings → Devices 设备管理页
   (建议,与 OAuth/Keys 同区)/ (B) 托盘 app 内配对码。
6. **P4 项目选择器形态**:(A) 现有项目面板加「远程设备」分组(建议)/
   (B) 独立设备面板。

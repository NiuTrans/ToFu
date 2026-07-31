# tofu_guard × re-exec 竞态根修设计稿(复核版)

> Epic `pt_aa3cd224b3b346e7`。状态:**待 owner 复核放行**,未动码。
> 复核材料:判据全部实测,落点精确到文件:行,含一次设计自查修正。

## 1. 事故与实测判据

| 判据 | 实测值 | 出处 |
|---|---|---|
| 正常 HTTP re-exec 窗口 | **45s**(12:20:41 SIGTERM → 12:21:26 ready) | access.log + app.log,12:20/12:23 各抢跑一次 |
| 病态慢 boot(99% cgroup 内存) | **~17min**(14:27 进程起 → ~14:44 bind) | 14:21 事故崩溃环 |
| 病态窗口内守卫抢跑 | **4 次**(14:31/14:39/14:41/14:43),全部撞 `data/.server.lock` 死亡 | watchdog.log「DIED during startup」 |
| 熔断器被假死亡污染 | 14:30 两次误跳「NOT relaunching」——真 boot 若再死一次舰队即 stranded | watchdog.log CRASH STORM |
| 唯一没让事故扩大 | 实例锁本身:4 撞 0 双开 | server_15000.log |
| `BOOT_GRACE` | **180s**(deploy/tofu_guard.sh:70)——病态 boot 的 1/5.7 | 代码 |

## 2. 根因机制(三层)

1. **etimes 时钟对 re-exec 必然失效。** `os.execv` 保留 pid,`ps etimes` 从**原始**启动计时:一个跑了 3 天的进程 re-exec 后 etimes=3 天 ≫ BOOT_GRACE。守卫 (d) mid-boot 让位(deploy/tofu_guard.sh:215)在 re-exec 窗口永不生效——12:20/12:23 两次抢跑的机制。
2. **BOOT_GRACE=180s 对病态 boot 必然失效。** 99% 内存压力下 boot 需 ~17min;180s 一过守卫每 15s 探一次、判一次假死亡、抢一次 relaunch——14:31 起连抢 4 次的机制。
3. **假死亡直接污染熔断计数。** `record_death_evidence`(tofu_guard.sh:224)在每次假死亡判定时记账;storm 窗口滑动后熔断器振荡(14:30 说 NOT relaunching、14:31 又 relaunch),真正的崩溃反而可能拿不到 relaunch 额度。

## 3. 选型:守卫侧(B),否 re-exec 持锁(A)

- **A(被否):** `_perform_server_reexec` 持 `.restart.lock` 跨 execv,新进程 boot 后释放。问题:①锁 fd 跨 execv 传递正是 fd-9 刚修掉的失败模式(pt_2a05e161b9814bc2);②boot 崩在释放点前 → 锁永久泄漏 → 此后每次脚本重启在 [pre/5b] 阻塞 60s 空退;③契约横跨 update.py + server.py + 守卫 + 脚本四个组件。
- **B(采纳):** `.restart.lock` 保持单一 owner(脚本),智能全部放在唯一需要它的组件(守卫)。两个服务侧落点各 2 行。

## 4. 设计三件套(精确落点)

### ① re-exec 标记(覆盖「旧进程已死、新进程未 exec」空窗)

- **写:** `routes/api_v1/update.py::_perform_server_reexec` 关闭前写 `data/.reexec_in_progress`(`{pid, ts}` 一行 JSON,+2 行)。shutdown 路径不写(shutdown 本来就该被守卫视为死亡?——**不写**,shutdown 是运维显式停机,守卫 relaunch 语义由既有 DISABLED_FLAG 纪律覆盖,不在本票扩)。
- **清:** `server.py:2953` `_boot('Ready — handing off to Hypercorn.')` 处顺带删除该文件(+2 行,best-effort try/except)。
- **守卫让位:** `check_once` 在 (b) 与 (c) 之间:标记存在且 mtime < **300s**(45s 窗口的 6.7×)→ 让位并记日志。marker 只需覆盖「死亡→新进程拿到实例锁」的数秒空窗;超时后第②层接管,300s 是纯兜底。

### ② boot-in-progress 无 TTL 让位(替代 etimes 时钟)

`check_once` 在 ① 之后、判死之前:无监听 + 无 HTTP 时——

1. `flock -n data/.server.lock -c true` 失败(锁被持)→ 读首行 `<pid>@<host>`;
2. **活性交叉检查(自查修正,见 §5):** 该 pid 在本机存活且 cmdline 含 `server.py` → **boot 进行中,让位**;boot 时长 > 600s 打 WARNING(可观测)但继续让位——wedge 处置是运维决策,不是守卫每 15s 的重复启动;
3. pid 已死 → 锁是**陈旧锁**(SIGKILL/孤儿持 fd/FUSE 延迟释放)→ 不让位,走判死;relaunch 后新进程由既有 `_reclaim_stale_instance_lock`(server.py:260)摘除陈旧锁。
4. 既有 (d) 年轻进程检查保留为第三信号(覆盖 `TOFU_SKIP_LOCK=1` 的手动 boot,该路径不持实例锁)。

### ③ ~~storm 记账净化~~ → **撤销(自查修正)**

原第三件「撞锁死亡不计入熔断计数」经复核**冗余**:污染源于**假死亡判定**(`record_death_evidence` 只在判死时记账),①② 两根修后假死亡不再发生,计数器自然干净。多一层日志签名匹配只是给永不触发的情况加代码。撤销,理由留此备查。

## 5. 设计自查修正记录(复核预演)

| 发现 | 原设计缺陷 | 修正 |
|---|---|---|
| FUSE/孤儿持锁 | 第②层只判「锁被持」→ 服务器被 SIGKILL 后孤儿/延迟释放会让守卫**永远让位**(比竞态更糟:真死了没人拉) | 增加活性交叉检查(§4②.2/.3):锁持有者的**记录 pid 存活且是 server.py** 才让位;死则判死,relaunch 由服务端 reclaim 摘锁 |
| storm 净化 | 把症状当根因 | 撤销(§4③) |
| BOOT_GRACE 引用值 | 复核前稿写 90s | 实测 180s(tofu_guard.sh:70),结论不变(17min 的 1/5.7) |

## 6. 失败模式预算

| 场景 | 行为 |
|---|---|
| marker 写失败(盘满/权限) | best-effort,re-exec 继续;退化为今天的行为(可能抢跑一次,撞锁无害) |
| marker 残留(boot 崩,没走到清除点) | 300s TTL 过期 → 第②层(实例锁+活性)接管 → 正常判死 relaunch |
| 服务器 SIGKILL,孤儿持锁 | ②.3 判死 → relaunch → 服务端 `_reclaim_stale_instance_lock` 摘锁 |
| 服务器 wedge(FUSE 挂死,活进程持锁不 bind) | 让位 + 600s 起每轮 WARNING;不抢跑(wedge 处置属运维) |
| `TOFU_SKIP_LOCK=1` 手动 boot | 不持实例锁 → ② 不适用 → 既有 (d) 180s 年轻进程让位兜底 |
| 守卫自身 relaunch 后 | 新进程持实例锁 → 后续探测②让位;既有 90s boot grace 保留不冲突 |

## 7. 测试计划(不碰真服务器)

新套件 `tests/test_tofu_guard_reexec_race.py`,沿用审批闸批的成熟形态(孤儿启动器 + `--once` 单发 + 测试端口 + `/bin/true` 桩 + 副本 defang):

1. **①层:** 造新鲜 marker + 无监听 → 守卫 `--once` 让位不 relaunch;NEUTER(剥 marker 检查)→ 同形态 relaunch;
2. **②层活锁:** stub 进程持 `data/.server.lock`(副本指向测试锁路径)不监听 → 让位;NEUTER → relaunch;
3. **②层死锁:** 锁文件记录已死 pid → 判死 relaunch(reclaim 路径不拦);
4. **TTL:** marker 造 301s 旧 → 不让位义务解除;
5. 静态锚:update.py 写 marker、server.py 清 marker 两行在库。

## 8. 实施清单

| 文件 | 改动 |
|---|---|
| `routes/api_v1/update.py` | +~4 行:re-exec 前写 marker(audit 复用既有日志) |
| `server.py` | +~3 行:ready 点清 marker |
| `deploy/tofu_guard.sh` | check_once 插两层让位(~25 行,含注释) |
| `tests/test_tofu_guard_reexec_race.py` | 新套件(~150 行) |
| `JOURNAL.md` | 落地记录 |

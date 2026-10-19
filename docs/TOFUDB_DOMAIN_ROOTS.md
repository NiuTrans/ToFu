# 按执行域准备不可变状态根

本文拥有 `benchmarks/domain_roots/` 的实验协议、比较边界与验证方法。
它是 [Harness 数据库研究](TOFUDB_HARNESS_DATABASE.md) 的一个独立组件实验。
没有接入生产 authority、Turn、任务调度或持久格式迁移。

## Problem

当前 authority 已有并行逻辑准备和私有 blob 编码。最终提交仍需要验证业务读写集，
处理共享索引、实体根与 WAL。不能把原有并行准备重新命名为架构突破。
本实验询问：按执行域独立构造不可变索引页，能否让这部分 CPU 工作跨核执行，
同时保留单一已提交前沿和跨域原子性？

## Decision

一个实验数据库只打开一个现有原生 `Engine`，或一个 SQLite 文件。
它不是每域独立提交的数据库集合。每域拥有不可变二叉页树；域根目录也是不可变
平衡树。更新复制被修改的叶子与路径；提交时只复制受影响域的目录路径，
不是复制整个历史、全部域状态或全部 receipts。

协议顺序固定：

1. 一次捕获 `Arc<Frontier>`。同一请求的所有域都从这个前沿读取。
2. 在工作线程验证谓词、构造不可变后继页。写域、显式读域和谓词读域全部记入依赖。
3. 单一提交 owner 重验完整依赖的 generation/root/geometry，并最终重验命令 ID。
4. 用一次 Strict native WAL 提交或一次 SQLite `BEGIN IMMEDIATE`、WAL/FULL 事务，
   保存新页、被读取的旧域根、本次更新的新域根与同一 receipt。
5. 持久化成功后，一次替换前沿，再让 receipt 可见，最后回复成功。

读过但未写的域也参加重验，防止 write-skew；净内容不变的写仍推进 generation，
防止 ABA。冲突发生在持久化前；失败不会发布任何部分域根。命令重放返回原 receipt，
不再次执行状态变化。权限身份是显式 Scope；这个离线实验信任调用者身份，
没有生产认证中间件，不能据此声称完成多租户授权。

receipt 的只读探测有独立短锁。它不与 WAL fsync 共用提交锁。真实 WAL sync 阻塞测试
要求另一独立域在该 I/O 尚未释放时完成准备；只有多个线程同时进入 barrier 不足以证明这一点。

任意持久化错误及持久化后、发布前的异常均将实例标为需要重开。
恢复只接受连续、有完整提交决定的根集合；没有决定的准备结果不进入恢复前沿。
此前已持有的快照继续指向一致的旧根，不受后续发布影响。

## 与 SQLite 的强对照

两个后端在同一个 Rust 程序中执行相同的页树、不可变目录、并行准备、依赖验证、
receipt 与重试策略。SQLite 使用单文件 `blocks` 和 `decisions` 表、prepared statements、
WAL/FULL、32 MiB page cache、mmap=0。原生入口是 raw Engine，默认 32 MiB block cache，
没有 EntityDatabase 的额外 page cache；两者另有完全相同的应用内不可变状态缓存。
配置容量相同不代表缓存组成或实际 RSS 相同。

对照优先链接当前已有的 SQLite 3.53.4 动态库，保存实际版本、编译选项、动态库链
及二进制哈希。它不是每请求启动进程，也没有 Python/Rust 语言差异。
各测量进程内部只有一个数据库实例与相同数量的工作线程。

原生仍通过现有 `Engine::commit_transaction` 携带页字节；全局提交路径仍支付页验证、
哈希、WAL 编码、复制和持久化成本。工作线程外移的是不可变页树构造。
本版**没有**实现只携带描述符的提交；不能声称全局串行工作已与 payload 字节数无关。

Alternatives：每域各开一个数据库而省略原子协调器会改变原子性，拒绝作为基线。
SQLite ATTACH/WAL 不是跨文件原子协议。本版先完成单文件强对照；多文件持久准备加
统一决定日志尚未实现。提前耐久化私有块可缩小决定记录，但会增加同步、孤儿数据与
回收协议成本，因此也没有和当前协议混为同一个结果。

## 可运行验证

在配置好 Cargo 与本地 SQLite 动态链接环境后：

```sh
cargo test --manifest-path benchmarks/domain_roots/Cargo.toml --test protocol -- --test-threads=1
python -m pytest benchmarks/domain_roots/tests/test_measure.py
cargo build --release --manifest-path benchmarks/domain_roots/Cargo.toml --bins
python benchmarks/domain_roots/measure.py --binary /frozen/measure --runtime /frozen/sqlite-runtime --output /new/results
```

原生故障矩阵覆盖每个实际到达的写/同步调用，包含短写；在旧 Engine 的 Drop 之前，
通过公开 VFS 的 crash/durable_file 接口复制独立耐久镜像。SQL 在每个页/决定写边界
注入事务错误，验证完整回滚。两个后端还分别在决定前、耐久后、发布后以及 ACK 后
真实 SIGKILL，再检查跨域状态与 receipt 同时存在或同时不存在。
这些是进程崩溃与确定性 VFS 证据，不是任意设备断电认证。

独立账户向量模型验证逐步状态和 receipt；并发读者检查跨域总量不会因前沿混合而改变。
Python 测量验证器另外按实际提交顺序重放请求，检查 read_sequence 之后全部依赖域
都未被修改、每个中间返回值、generation、固定成功工作量、重开验证与失败重试成本。

默认矩阵是 1/4/8 workers × 独立域/热点域/100% 跨域 × native/SQL/SQL/native。
每进程完成相同 128 个成功事务，另记冲突次数、丢弃的准备字节、prepare/commit 时间、
CPU、空间、关闭与重开成本。计时包含工作线程创建和 join，初始化与完整校验在计时外。
原始样本与每腿结果必须保留；未执行的矩阵不能用于结论。

## 边界

域集合和每域槽位数由 genesis 固定；没有动态分域、根回收、历史裁剪、跨文件协调器、
外部 effect、生产 outbox 或权限变更协议。所有状态页在内存形成共同缓存，故该实验
衡量准备与持久化写路径，不衡量数据库随机读或长上下文检索。

实验保留全部决定与已写页。未提交页只在内存存在，失败后释放；提交过的旧页暂不回收。
恢复重放全部保留的决定元数据，再加载当前根的可达页，不是已经实现了前沿大小的恢复。
最多 512 域、每域 4096 个槽位、每叶 16 KiB evidence、单请求 256 个变化、10 万决定；
genesis、准备与决定还有独立字节边界。实验调用者限制并发和保留快照数量；这些上限
不是生产资源探测与 GC 策略。

当前生产 Entity overlay 已允许独立旧快照写入在逻辑 OCC 下提交。
本实验未把它作为吞吐臂，也未与生产 Agent 任务端到端比较。
即使 native 在本协议中胜出，也只能证明指定同语言、同协议组件的差异；
不能直接声称已经替换线上写路径或实现最强 Harness。

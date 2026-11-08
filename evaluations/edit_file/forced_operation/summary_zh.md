# 第 10 节素材：edit_file / write_file 强制模式对照实验（editbench-4cbf7e4a，实验 1+2 终版）

数据：/tmp/editbench-4cbf7e4a（raw/*.jsonl 逐次记录 + MANIFEST.sha256）。预注册：worktree evaluations/edit_file/forced_operation/PREREG.md（306ab0c06b，先于第一次模型调用提交）。
口径：均值 [95% CI]，CI 为按任务聚类的 bootstrap（10,000 次、60 个任务）；三个模型等权；payload = 工具调用参数的 o200k token；completion = 服务商报告的输出 token（含推理）。

## 实验设计（设计理由）
- 60 个编辑任务，取自 6 个公开仓库的真实源码（requests / click / express / cobra / log / ky，覆盖 Python、JS、Go、Rust、TS）。纯插入 24 个（锚点短/中/长各 8 个），替换、删除、多处编辑各 12 个。每个任务有逐字节的期望结果。
- 四种编辑方式，每种只提供对应的工具：A1 Tofu edit_file 只准 replace；A2 Tofu edit_file insert 优先；A3 Claude Code Edit（old/new 字符串）；A4 Codex apply_patch。A1/A2 直接调用 Tofu 生产实现（ingest_tool_call + tool_edit_file）。A3/A4 是忠实移植，并用 CC 2.1.207 打包代码和 Codex 23 个官方场景夹具钉住行为。
- 模型：GLM-5.3、Claude Opus 5、Kimi K3，每个任务重复 3 次，共 2,160 次尝试，无缺失。DeepSeek-V4-Pro 的共享 key 大面积返回 402（余额耗尽）和 429，跑了 31/720 后停掉，按预注册的偏差记录换成 Kimi。

## 主要结论（实测）
1. **insert 确实省 token，省的正是被重复的锚点。** 纯插入任务里，A2 比 A1 每次少 16.0 [10.8, 21.1] 个 payload token，约占编辑调用的 20%。锚点越长省得越多：短锚点 8.2、中锚点 15.3、长锚点 24.5 token（占比 15% / 20% / 23%）。三个模型方向一致：Claude −13.8、GLM −18.5、Kimi −15.7。
2. **省下来的这点 token 在总输出里很小，还可能被推理吃掉。** 即使在这种最小任务里，编辑 payload 也只占模型输出 token 的约 1/5（A1：21%），其余主要是推理。insert 优先时：
   - 插入任务的平均 completion 增加 167 [−17, 440]，GLM +265、Kimi +214、Claude +21；
   - 中位数反而略低，差额来自长尾：GLM 有 4 次推理到 16k 上限仍未发起调用，原因是在纠结 insert 内容是按行还是按原始文本拼接，而 schema 没写清楚这一点。
3. **insert 没有带来正确率收益。** 纯插入的最终成功率差 −0.9 [−4.6, 3.2] 个百分点，首次成功率差 −3.7 [−8.3, 0.9]；多处编辑的首次成功率显著下降 10.2 [2.8, 19.4] 个百分点。分模型看，Claude +4.2、GLM −4.2、Kimi −2.8。这支持 2026-10-21 把描述改为 "Prefer replace" 的决定：保留 insert，但不主推。
4. **Tofu replace 与 Claude Code Edit 等价。** 成功率差 −0.2 [−2.0, 1.1] 个百分点，payload 差 +1.0 [−2.4, 4.8]。在明确告诉模型用哪种操作时，必填 operation 带来的 schema 拒绝为 0/1,080。对照：TB2.1 中由模型自选操作时，8.1% 的调用被拒。
5. **Codex patch 风格最不稳。** 成功率比 A1 低 7.6 [4.6, 10.9] 个百分点，首次成功率低 11.5 [7.6, 15.7]。payload 并不更省（−1.4 [−6.6, 3.9]），因为模型实际只给中位 2 行上下文，而不是约定的 3 行。
6. **最大的失败类型是"工具报成功、文件却错了"，几乎都是空白问题。** 每种方式 540 次中：A1 7 次、A2 11 次、A3 7 次、A4 26 次。

## 产品层发现（实测，附证据）
- **行中锚点的 insert 会把行劈开，回执却显示 OK。** 实例：GLM 用 `"fmt"` 做 insert_before 锚点，结果 Go import 缩进错位。schema 写的是 "Shortest-unique-anchor"，正好鼓励这种锚点。
- **insert 的远端回显守卫有误伤。** 新代码块末尾恰好与锚点行相同时（ky body.ts，`return 0;` + `}`），合法插入被拒绝并要求改用 replace。
- **insert 的换行语义没写进 schema。** 实现是按行插入（`_splice_insertion`），这是推理模型反复纠结的直接原因。
- **Codex 默认 NormalizeToLf 会用补丁里的上下文行覆盖文件原行。** 模糊匹配时，缩进写错的上下文行会被悄悄写进文件（features/src/lib.rs:1185 默认关闭 PreserveLineEndings）。

## 一句话（面试用）
insert 把"重复锚点"这部分 token 省掉了，每次插入约 20%、长锚点约 23%，与设计预期一致。但在真实成本里，这部分远小于推理 token；在推理模型上，insert 还会放大长尾思考，正确率也没有提升。所以我们保留 insert 能力、默认推荐 replace（设计理由：同一份描述不教两种同等推荐的写法）；更值得做的是把 insert 的换行语义写清楚、拒绝行中锚点的 insert。

## write_file 实验 2（实测；模型同上，每格 3 次重复）
- **代码里实际有三种模式**（lib/tools/project.py:142）：`content` 整文件写、`append=true` 追加、`content_ref` 引用之前某轮工具结果的字符切片。生产日志（11-06 09:51–18:10）中，write_file 共 592 次，其中 append 133 次（22.5%），content_ref 0 次。
- **追加 vs 整文件重写**（CSV/服务日志，现有内容约 0.4K/1.8K/7K token）：
  - append 每次少写 2,668 [722, 4,965] 个 payload token，少 2,861 个输出 token，快 30.5 秒；
  - 在 7K 量级的文件上，少写 6,289 token（约 30 倍），快 76 秒；
  - 正确率无差异：83.3% vs 85.2%，差 −1.9 [−16.7, 14.8]。Claude 与 GLM 两种方式都是 18/18。
- **大文件（555–1,050 行）中间改一行**：
  - edit_file 成功率 100%，整文件重写 66.7%，差 +33.3 [27.8, 38.9] 个百分点；
  - payload 76 vs 5,815 token（约 77 倍），耗时 10.7 vs 90.4 秒；
  - 重写失败的原因是模型改丢或改动了无关行。
- **content_ref vs 直接写**：即使提示里直接给出轮次号，content_ref 的成功率也只有 40.7%，直接写为 74.1%，差 −33.3 [−55.6, −11.1]。这是 content_ref 的最好情况，生产环境里模型能否看到轮次号未核实。
  - payload 少 243 token，但输出 token 多 12,284、慢 160 秒；
  - 原因是 content_ref 按工具结果的字符位置切片，而工具结果自带 `$ 命令` 和 `[exit code]` 外框，模型只能自己数字符；
  - 只有 56% 的尝试真正用上了 content_ref。
- **Kimi K3 的 write_file 调用大多只带 path、不带 content。** 根因是 content 与 content_ref 二选一，而 provider 不接受根 oneOf，schema 只能把 path 标为 required。探索性复测（未预注册，2 个任务 × 3 次）：
  - 原 schema：成功 3/6，只带 path 的调用 9/12；
  - 把 content 加入 required：成功 6/6，只带 path 的调用 0/6。
  - 结论：二选一需要换一种表达方式，例如拆参数或拆工具。
- **数据缺陷声明**：E2a 中三个 markdown 日志任务的期望文件多了一个结尾空行，而指令只要求"以换行结尾"，两种方式都因此失败。已按偏差单列，不计入上面的 E2a 数字。

## 一句话（write_file）
append 和 edit_file 是真正省 token 的设计：增长型日志和大文件局部修改，输出少几十倍，正确率不降，大文件上还更高。content_ref 是反面例子：payload 省了，却逼模型数字符，推理成本暴涨、成功率减半，生产中也 0 次使用。

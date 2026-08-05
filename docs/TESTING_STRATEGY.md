# TESTING_STRATEGY.md — Tofu 测试体系分层战略

> 2026-08-04 owner 拍板版。三路 swarm 调研（业界实践 / 后端家底 / 前端家底）后的
> 分层方案与防负优化纪律。本文是测试工作的 north star：新增测试前先对层入座。

---

## 1. 家底盘点（2026-08-04 实测）

| 层 | 数量 | 机制 |
|---|---|---|
| 后端单元 | ~950 套件 | pytest + conftest 自动打标（无标记者兜底 unit） |
| 集成/API | ~40 套件 | Flask test client + mock LLM |
| 守护棘轮 | ~113 套件 | drift/parity/contract/invariant，只降不升 |
| 前端 jsdom | **467 套件 / 321 个 run_harness 调用点** | Python 驱动 node+jsdom eval 真实生产 JS |
| 前后端契约缝 | 1 族 | `test_frontend_backend_contract.py`：api.js 路径 ⇄ live url_map |
| 真浏览器 E2E | 4 套件 | Playwright；`test_e2e_smoke.py` 已是「真 app+真浏览器+stub LLM」hermetic 车道 |
| 总量 | **15,528 用例**（collect-only 87.7s） | CI: lint / unit×2 / api / frontend(node) / e2e |

## 2. 业界对照（一手源）

| 做法 | 我们的对齐状态 |
|---|---|
| 金字塔 70/20/10（[Google](https://testing.googleblog.com/2015/04/just-say-no-to-more-end-to-end-tests.html)） | 比例大致符合；E2E 偏薄但在正确的一侧 |
| 奖杯「mostly integration」（[Dodds](https://kentcdodds.com/blog/write-tests)） | jsdom 族本质是集成测试（eval 真实模块组合） |
| S/M/L hermetic 分级（[Google Test Sizes](https://testing.googleblog.com/2010/12/test-sizes.html)） | markers(unit/api/visual/slow/live_llm) 已是同构物 |
| 契约测试：monolith 用静态契约而非 Pact（[Fowler](https://martinfowler.com/articles/consumerDrivenContracts.html)） | **已在做**（路径⇄路由表），P1 升级到字段形状 |
| flake 隔离+SLA+删除（[Google Flaky](https://testing.googleblog.com/2016/05/flaky-tests-at-google-and-how-we.html)） | 有「预存红」三板斧文化，未制度化 SLA → P2 |
| 覆盖率只作信号（[Fowler](https://martinfowler.com/bliki/TestCoverage.html)） | 遵守，不设全仓门槛 |
| 测试选择（[Google Taming](https://doi.org/10.1109/ICSE-SEIP.2017.16)、[Meta](https://arxiv.org/abs/1810.05286)、微软 Herzig ICSE'15） | **先量墙钟时间**，>15min 才做路径映射选择；不上 ML |

## 3. 三个真缺口（按 ROI 排序）

1. **skip 静默吸收**：CI 已有 node 车道、Makefile 已预检，但 CI frontend job 与
   本地裸跑 `pytest tests/test_frontend_*.py` 时，node/jsdom 缺席 → 467 套件
   全部 skip 而退出码为 0（绿）。~40 处手写 node 检查 + 4 处 module-level skip
   + `run_harness` 自身 skip，全是同一个静默洞。
2. **断言地板是虚的**：`run_harness` 默认 `min_pass=1`，且用
   `output.count('PASS')` 子串计数（非行锚定）；102 个文件中只有 60 个显式
   声明 min_pass —— 约 42 个文件的地板是「至少 1 行像 PASS 的输出」。
3. **真浏览器主干道巡检薄**：e2e hermetic 车道只有 1 条旅程；业界惯例
   10–50 条关键旅程守 release 闸。

## 4. 分层方案（owner 拍板）

### P0（本批）
- **P0-1 skip 必须响亮**：`TOFU_REQUIRE_FRONTEND=1` 时
  `tests/_jsdom.py` 的 skip 变硬失败；conftest 会话末哨兵统计
  `test_frontend_*` 的 node/jsdom/npm/tsc 类 skip，>0 即红；Makefile
  `test-frontend` 与 CI frontend job 注入该环境变量。
- **P0-2 结构化断言地板**：`_jsdom_harness.js` 的 `report()` 追加结构化
  尾行 `__JSDOM_RESULT__ {"pass":N,"fail":M}`；`run_harness` 优先解析尾行
  （缺失走行锚定计数，不再子串计数）；新增 `expect_pass=` 精确申报；新棘轮
  扫描调用点，未申报 expect_pass 的数量只降不升——新套件必须申报。
- **P0-3 浏览器主干道**：以 `test_e2e_smoke.py` 为母版扩到 10–20 条关键旅程
  （发消息→流式→工具卡→中止 / 会话恢复 / 设置保存 / 上传），接 release 闸。
  先实测 `make test-unit` 墙钟时间，再定是否做按路径的选择运行。

### P1 ✅(2026-08-04 落地 tests/test_api_field_contract.py)
- 契约守卫从「路径存在」升级到「字段形状」：只钉 top-N 核心端点的响应字段
  （轻量 schema pin，**不上 Pact**）。消费者驱动——只钉前端真读的字段,
  响应多出新字段不破钉(防快照剧场);checker 自带 NEUTER 咬合自证。

### P2 ✅(2026-08-04 落地,防负优化制度化)
- **①棘轮殡葬审计**(`scripts/ratchet_audit.py` → `docs/RATCHET_AUDIT.md`):
  152 个守护套件机械分类——锚定 92(NEUTER 咬合证明/事故引用 pt_/commit/
  JOURNAL/事故/家族锚×21)、殡葬候选 60。候选**不自动删**(删保护要人判),
  逐个:补事故链接/补 NEUTER/降级/删除。增量由
  `tests/test_ratchet_incident_link.py` 守:**新守护套件必须带锚,无锚禁入**
  (存量祖父化于 tests/_ratchet_guard_baseline.json);FAMILY_ANCHORS 工件
  必须在库(防洗白)。
- **②flake/预存红三步(制度化):** 隔离—— flake 或预存红先挂板票
  (三态分诊:本批引入/预存/兄弟 churn,票据须带纯净 HEAD 复现证据);
  SLA—— 自挂票起 7 天内根修或方向对齐;删除—— SLA 过期未处置,默认
  删除该测试(一个能被无视的测试比没有更糟,Google flake 政策同义)。
  季度重跑 `ratchet_audit.py --write-docs` 刷新候选清单。
- **③按路径选择运行**(`scripts/test_select.py` + `make test-affected`):
  静态反向索引(测试文件→AST import+字面路径引用,mtime 缓存 ~4s 冷/
  <1s 热),改动∩引用=入选 + 爆炸半径表(conftest→全量,jsdom 助手/
  api.js→前端族)+ 守卫核心常跑;选择超 40% 直接跑全量。**不上 ML**——
  透明映射可审计,15k 规模用不上预测模型。全量层(unit 19m58s 实测)
  仍是 CI/预推闸门;选择器只做迭代内环。

## 5. 明确不做（防负优化清单）

- 不追全仓覆盖率数字（Goodhart）；覆盖率只看新代码 diff。
- 不上 Pact（单体+单消费者，静态契约已够）。
- 不上 ML 测试选择（15k 规模全量跑得起；先实测再议）。
- 不把 E2E 扩成主防线（E2E 只守主干道，比例不逾 10%）。
- 新测试默认 failing-first；写不红的测试不进库（NEUTER 纪律延续）。

## 6. 操作细则

- 跑测试统一 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 或 Makefile 的 `-p no:napari` 基底。
- 每批改动后必跑 `--collect-only` 闸。
- 期望前端套件执行的环境（CI frontend job、`make test-frontend`）必须设
  `TOFU_REQUIRE_FRONTEND=1`；未设的环境保持静默 skip（贡献者无 npm 不炸）。
- 新 jsdom 套件：`run_harness(..., expect_pass=N)`，N = harness 实际断言数；
  未申报会被 `test_frontend_harness_expect_ratchet` 计红。

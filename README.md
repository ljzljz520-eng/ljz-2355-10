# 游戏规则百科 · Game Rules Encyclopedia

文档前端检索 **角色 / 地图 / 计分**；规则服务对 **指定补丁的示例局面** 做可逐步解释的计分；
关系库存储 **规则依赖、生效区间与勘误**。纯 Python 标准库实现（HTTP + SQLite + 原生前端），无第三方依赖。

## 运行

```bash
python -m app.server          # 默认 http://127.0.0.1:8080
python -m app.server 9000     # 指定端口
python -m unittest discover -s tests -v   # 21 个验收/单元测试
```

首次启动自动建库、灌入种子语料、为每个补丁预编译规则包、构建检索索引，并冻结全部比赛示例。
数据库位于 `data/rules.db`（删除后重启可重建）。

## 六个页面

| 页签 | 能力 |
|---|---|
| ① 百科检索 | 全文检索角色/地图/道具/计分；**默认只搜当前版**，勾选“历史过滤”并选补丁可查任意历史时点；支持类型过滤 |
| ② 局面解释 | 选补丁（含同日早/晚更槽位）+ 粘贴局面 JSON + 求值预算，返回分数、逐步来源、冲突/诊断与冻结闭包，可**导出报告** |
| ③ 比赛示例与历史回放 | 每个示例永久绑定发布时冻结包；“冻结回放”绝不引入新公式；“现行规则参考”仅作 advisory，不改冻结分 |
| ④ 预编译 vs 查询组合 | 列出每补丁的预编译包与按需查询包，点击查看冻结版本、生效区间与诊断；内容哈希一致即同一包 |
| ⑤ 勘误与生效区间 | 勘误修正关系（错误版 → 修正版）+ 全库区间重叠审计 |
| ⑥ 索引管理 | 分批构建、模拟中断、断点恢复；中断期间检索靠已提交批次 + LIKE 回退，永不下线 |

## 核心设计

### 1. 整套规则闭包冻结（不能旧数值 + 新公式）

* 每个锚点（补丁时间坐标 + 根规则集合）生成一个**闭包**：从根规则出发，沿表达式 `ref`
  与参数 `requires/modifies` 依赖闭包展开，把到达的**每一个规则的具体版本**（含表达式、参数、
  生效区间）整体冻结。
* `package_id = sha256(锚点坐标, 排序后的根, 每个规则版本的 id/label/expr/params)`。
  内容变一个字节（例如计分公式被勘误）身份就变；锚点不同即使公式相同也是不同包。
* 历史回放只读取 `examples.frozen_package_id` 指向的包；包缺失则**拒绝回放**，绝不回退到当前规则。
  求值器是闭包局部的，任何情况下都不会回库“取最新版”。

### 2. 时间轴坐标与生效区间

* 区间以整数坐标存储：`patch_seq * 10 + day_slot`，半开区间 `[start, end)`。
  普通补丁占 `seq*10`；**同日先后**两更占 `41`（早）/`42`（晚），把“当天先后”变成普通区间边界，
  杜绝同 seq 平局。
* `resolve_rule(rule, coord)` 命中多个版本 → `OVERLAPPING_INTERVAL` 错误；闭包编译会对其中
  所有规则做两两重叠审计，重叠即把包标记为 `error`，**禁止求值**（不猜一个版本）。

### 3. 有界求值语义 vs 非法递归（引用成环）

DSL 表达式静态分析（`app/graph.py`，Tarjan SCC）区分三种环：

* **有界自定点（合法）**：规则在 `fixed{ start, next, until, max_iter }` 内通过
  `{"t":"prev"}`（上一轮值）引用自身，`until` 用 `{"t":"prev"}` 与 `{"t":"candidate"}`
  比较判定收敛，且有 `max_iter` 硬上限 → 正常求值并回报收敛轮数（见 `combo_chain`）。
* **多规则全有界环**：当前求值器只支持单规则定点，单独报 `BOUNDED_CYCLE_UNSUPPORTED`。
* **无界互递归（非法）**：未被定点构造包裹的环（见 `bad_loop ↔ bad_loop_b`），编译期
  `ILLEGAL_RECURSION` 直接拒绝，报出涉及规则与环边，包不可求值。
* 运行期另有一道栈检测兜底：若漏网成环，求值时抛 `ILLEGAL_RECURSION`。
* 定点到 `max_iter` 仍不收敛 → `BOUNDED_ITERATION_DIVERGES`（见 `diverge_rule`），
  不用未定值计分；预算耗尽 → `EVALUATION_BUDGET_EXCEEDED`，两者都带规则名与现场数据。

### 4. 预编译每补丁 vs 按查询组合（依赖校验 + 缓存身份）

* `precompiled`：每个补丁的全部生效计分规则为根，提前校验并固化；查询是一次索引命中。
* `query`：按请求的根集合即时展开；缓存键 = `(锚点坐标, 排序根集合, 模式)` 的哈希，
  相同请求 `cache_hit=true`，且**内容指纹与预编译包逐字节一致**（测试保证 `package_id` 相同）。
* 两种模式都先过依赖校验：版本不可解析（未引入/已下线/区间重叠）、非法环都会写进包诊断。

### 5. 勘误：另标修正关系，不重写历史

* `errata(bad_version → fixed_version, note)` 记录修正；错误版本保留 `status=corrected`。
* 已发布示例的 `(situation, frozen_package, expected_score)` 永不更新；
  回放照常得到旧分（如慢局在缺陷版下得 50 奖励），同时在结果里附 `errata_notices`。
* “现行规则参考”（advisory）明确标注不是官方分，且不会回写示例。

### 6. 道具与地图条件冲突

同 `conflict_group` 的规则同时成立时，按 `priority` 仲裁，**赢家通吃**，输家分数撤回，
并产出 `CONDITION_CONFLICT_SUPPRESSED` 警告（写明两个规则与撤回分值）。

### 7. 可中断索引

索引分批提交（`index_tokens` + `index_state` 游标/代数），构建中途“崩溃”只丢当前批；
检索在 `building` 状态下仍可工作（已提交倒排 + LIKE 回退）；恢复从游标继续，不重复索引。

## 目录

```
app/db.py          SQLite 模式与连接（规则/版本/区间/依赖/勘误/示例/规则包/缓存/索引/解释记录）
app/graph.py       DSL 遍历、依赖边提取、Tarjan SCC、有界环 vs 非法递归分类
app/versions.py    时间坐标、版本唯一解析、区间重叠审计
app/packages.py    闭包展开、依赖校验、内容指纹、预编译与查询缓存身份
app/evaluator.py   DSL 求值器：有界定点、预算、门控、冲突仲裁、逐步贡献
app/explain.py     局面解释、冻结回放、advisory、报告导出（包摘要 + 局面输入 + 分数明细）
app/searchindex.py 分词、可中断/可恢复的批量索引、当前默认 + 历史过滤检索
app/seed.py        种子语料（5 个补丁、同日两更、勘误、有界定点、非法环、发散规则、5 个示例）
app/server.py      标准库 HTTP：/api/* JSON 接口 + static 前端
app/static/        index.html + app.js（无框架单页）
tests/test_core.py 21 个测试（5 个验收场景 + 图分析 + 缓存身份 + 勘误冻结）
```

## 验收场景对照

| 要求 | 种子/接口 | 测试 |
|---|---|---|
| 补丁同日先后 | v1.3 早更(110,坐标41)/晚更(125,坐标42) | `TestSameDayPatches` |
| 生效范围重叠 | 动态插入 rogue 版本，闭包报 `OVERLAPPING_INTERVAL` 并拒求值 | `TestOverlappingIntervals` |
| 道具与地图条件冲突 | 护符(40,优先级60) vs 地形(30,优先级50) 同组仲裁 | `TestConditionConflict` |
| 示例计算超预算 | `budget=1` 超支；`diverge_rule` 定点发散 | `TestBudgetAndBoundedIteration` |
| 索引更新中断 | 建 2 批后中断，检索仍可用，恢复后 done=total | `TestSearchAndIndex` |
| 页面逐步解释分数来源 | `/api/explain` 的 `step_explanation` + 前端时间线 | `TestExplanationAndReport` |
| 导出含规则包摘要 + 局面输入 | `/api/report` 的 `package_summary`/`situation_input` | 同上 |
| 历史闭包不混版 | `ex_ice_rush` 冻结缺陷版 390，v1.4 advisory 为 455 | `TestFrozenClosureAndErrata` |
| 环的有界/非法区分 | combo 合法定点；bad_loop 非法；多规则有界单独诊断 | `TestCycleClassification*` |
| 预编译 vs 查询身份 | 相同闭包 `package_id` 相等、重复查询缓存命中 | `TestCompileStrategies` |

# 游戏规则百科（规则版本化 · 闭包冻结 · 有界/非法递归诊断 · 历史回放）

一个可运行的全栈演示：文档前端检索**角色 / 地图 / 计分**；规则服务解释**指定补丁**下的示例局面；
关系库存储**规则依赖、生效区间与勘误**。仅依赖 Python 标准库（含 sqlite3）。

## 运行

```bash
python3 demo.py                 # 命令行一键演示全部 8 个验收场景
python3 -m unittest tests.test_acceptance -v   # 28 个验收测试
python3 -m rules_enc.server --port 8080         # Web: http://127.0.0.1:8080/
```

## 数据时间线（UTC）

| 时间 | 事件 |
|---|---|
| 2026-09-01 | `1.0.0` 首发（角色/地图/道具/击杀基础计分） |
| 2026-09-05 | 公开赛 `ex_opener`，冻结 1.0.0 闭包 |
| 2026-09-10 09:00 | `1.1.0` 赛季更新：霓虹庭院、磁铁（条件冲突）、**有界连环 combo**、赏金 |
| 2026-09-10 18:30 | `1.1.1` **同日热修**：击杀 24h 限时 2x、map_wins 冲突裁决、Nova 40 |
| 2026-09-11 12:00 | 总决赛 `ex_grand_final`（落在热修窗口），冻结当时闭包 |
| 2026-09-11 18:30 | 热修击杀窗口到期（priority 叠加/回落） |
| 2026-09-20 09:00 | 勘误 **E-1**：只计排位击杀；**不重写已发布示例**，另标修正关系 |
| 2026-09-20 12:00 | `1.1.2` 澄清落地（ranked_kills、Nova 45） |
| 2026-10-01 | “当前”，默认检索版本 = 1.1.2 |

另有 `test-heavy` 实验补丁（test 类型），用 `iter_drag(n)` 演示**示例计算超预算**。

## 核心设计

### 1. 规则 DSL 与依赖闭包
`rules_enc/parser.py` 递归下降解析表达式（算术/比较/逻辑/三元、`input(...)`、`ref(...)`、
内置函数）。`rules_enc/graph.py` 用 Tarjan SCC 建求值计划：

- **无环引用**：拓扑序一次求值；
- **有界求值（bounded fixpoint）**：环中每条规则都必须声明
  `fixpoint{max_iter, tol, init}`，求值器做**同步迭代**（组内引用读上一轮快照），
  `max|Δ| ≤ tol` 即收敛，记录每轮值；
- **非法递归**：环中只要有规则没声明有界语义（或 max_iter 不一致），**编译期**报
  `IllegalRecursion` 并给出环路径与未声明规则，补丁包不可用；
- 有界但**非收缩**（如 `x=2y+1, y=2x+1`）在 `max_iter` 内不收敛 =>
  运行期 `BUDGET_EXCEEDED / iteration_cap`，拒绝给分并给出最后变化量。

另设 **AST 节点预算**（默认 200000，可按局面覆盖），`iter_drag` 等迭代逐项计数，
超限 => `BUDGET_EXCEEDED / node_budget`。

### 2. 闭包冻结：杜绝“旧角色数值 + 新计分公式”
任意时间点 `as_of`，按 `valid_from ≤ t < valid_to` 对每个**规则键**选出唯一版本，
连同当时生效的冲突策略，构成不可变**规则闭包**。闭包身份：

```
hash = sha256(canonical({as_of, rules[...14个身份字段...], policies}))   # 完整64位
```

- `ref('character') / ref('kill') / ref('combo_ping')` 等**逻辑键**在编译时被重写到
  闭包内**实际选中的版本 id**，公式永远引用同一闭包的配套版本，跨版本错配在结构上不可能；
- 历史比赛示例只存 `snapshot_hash`，回放从冻结快照重编译，**不**随当前版漂移；
- **预编译补丁包**与**按查询组合**走同一个 `compile_closure`，同一时间点身份哈希相同，
  缓存以哈希为键共享对象；身份字段差一个空格都会改变哈希。
  `GET /api/packages` 对每个包做发布时刻重组合并比对（验收可见全部 identity_ok）。

### 3. 生效区间重叠与条件冲突（两件不同的事）
- **区间重叠**（同一规则键多版本同时生效）：按 `priority` 取高者；并列 =>
  `OVERLAP_AMBIGUOUS`，无法唯一求解。热修 kill `priority=10` 覆盖基础 `priority=0`，
  窗口结束自动回落。
- **条件冲突**（不同规则条件同真且属同一 `conflict_group`）：闭包内若有
  `conflict_policies`（winner/priority，带生效区间）则裁决、败者规则标记“被压制”得 0；
  无策略 => `RULE_CONFLICT`，列出候选且明确“无法唯一求解”。1.1.0 无策略报错，
  1.1.1 配 `map_wins` 后可解。

### 4. 检索（默认当前版，保留历史过滤）
`search_index` 独立成表，`rebuild_index` 用**影子表 + 事务内改名**：
- 先写 `search_index_stage`（可 `fail_after` 注入中断）→ 单事务 `RENAME` 交换；
- 中断只污染影子表，线上索引与搜索不受影响，直接重试恢复（`GET /api/admin/index-status`）。
- 搜索默认按 `now` 的实际区间过滤（过期热修也不返回）；勾选历史过滤可给任意 `as_of`；
  “包含全部历史版本”可检索被取代版本。

### 5. 勘误治理
`errata` 与 `example_corrections` 分离：E-1 **不修改** `examples.expected`，
原始已发布总分保持 761.25；修正闭包（1.1.2 内容）的重算值 466.25 作为
`corrects` 关系并列展示。

### 6. 逐步解释与导出报告
每个规则求值产生一个 Step：公式、使用到的 `inputs`、`ref` 解析后取值、
是否生效/被压制、有界环的迭代轮次与每轮值、槽位汇总构成的分数加和。
前端可展开逐步查看总分如何由角色/地图/道具/击杀/连击/赏金组成。
`POST /api/report` 导出 JSON 或 Markdown，始终包含**规则包摘要 + 局面输入**，
无法求解时包含结构化**诊断**（错误码 + 环/候选/预算用量等细节）。

## 目录
```
rules_enc/
  ast_nodes.py tokenizer.py parser.py   # DSL
  graph.py        # Tarjan SCC，有界/非法递归分类
  evaluator.py    # 求值：拓扑/同步不动点、槽位汇总、冲突、预算、逐步追踪
  compiler.py     # 闭包解析、ref 重写、规范化哈希、编译
  repository.py   # SQLite：包/规则版本/依赖/区间/策略/勘误/示例/快照/索引
  indexing.py     # 检索与原子重建（中断安全）
  seed.py         # 时间线种子数据 + 预编译 + 冻结示例 + 修正关系
  service.py      # 规则服务、身份缓存、报告
  server.py       # 标准库 HTTP API + 静态前端
web/ index.html app.css app.js
tests/test_acceptance.py   # 28 tests
demo.py
```

## 关键 HTTP API
```
GET  /api/meta | /api/packages | /api/packages/{id}/summary
GET  /api/search?q=&as_of=&type=&all=
GET  /api/examples | /api/examples/{id}             # 冻结闭包回放
POST /api/explain   {package_id|as_of|snapshot_hash, inputs, node_budget}
POST /api/report    {..., format:"json"|"md"}
POST /api/admin/reindex?fail_after=N | GET /api/admin/index-status
```

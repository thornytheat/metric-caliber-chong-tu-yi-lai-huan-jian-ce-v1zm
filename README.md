# metricring · 指标口径注册与依赖门禁

指标体系组的注册表门禁工具：把「同一个 GMV 在三个看板算法不同」这类口径事故挡在发布之前，
并在改口径前算清下游影响面。

## 能力

- **指标注册**：指标名 + 版本 + 表达式（`metric:`/`dim:` 引用、`sum/avg/count` 包裹）+ 单位/聚合/维度/负责人/是否发布，落 SQLite（命名卷 `metricring-data`），启动幂等 seed。
- **公式解析与归一化**：去空白、去冗余括号、去 `*1`、常量化、统一前缀；只做结构等价变换，`a/b` 与 `a/b*100` 归一化后仍然不同。
- **依赖图 + 环检测**：节点是「指标@版本」，边是表达式里的 `metric:` 引用；DFS 三色找环并给出环路径与涉及指标。
- **同名异口径**：同名指标按归一化表达式分组，>1 组即 conflict，列出各组版本与差异摘要。
- **变更门禁**：改表达式/维度/聚合 → 算全部传递下游，分 `breaking`（移除下游依赖的维度、改聚合、引入环、引用未发布版本）与 `requires_sync`。
- **发布校验**：被引用版本必须 `published=true`，且不得处于环中、不得与已发布基线口径不同。
- **体检历史对比**：每次体检接续「已确认基线」，把冲突/环/发布阻断/无法解析引用按**新增、持续、消失**列出；
  新增项带实际变化的指标版本（expression/normalized/aggregation/dimensions/published 的前后值）、涉及版本与当前依赖图上的传递下游。
  基线只能由人手动确认：新体检先是候选，确认前不会切换基线。体检失败会记录错误并保留旧结论，重试以同一
  `attempt_key` 复用该记录（不重复写入），每次失败尝试的轨迹也保留在记录里。

## 核心规则（后端真算）

| 规则 | 模块 | 归一化/判定口径 |
|---|---|---|
| 公式解析 | `app/expr.py` | 递归下降，`+ - * / ( )`、`metric:name[@ver]`、`dim:name`、`sum/avg/count` |
| 归一化 | `app/expr.py` | 去空白、丢冗余括号（按优先级重新加）、消 `*1`/`1*`/`/1`/`+0`、数字常量化、前缀小写 |
| 依赖图 | `app/graph.py` | 节点 `name@version`；裸引用 `metric:x` 解析到「最新已发布版本」，`metric:x@2` 精确解析 |
| 环检测 | `app/graph.py` | DFS 白/灰/黑三色，灰色邻居即环，按节点集合去重 |
| 口径冲突 | `app/conflict.py` | 同名 → 归一化分组 → 组数 > 1 即 conflict |
| 变更门禁 | `app/gate.py` | 传递下游 + `breaking`/`requires_sync` 判定 |
| 发布门禁 | `app/gate.py` | `unpublished_reference` / `missing_reference` / `cycle_member` / `caliber_conflict` |
| 历史对比 | `app/history.py` | 问题身份键：冲突按指标名、环按节点集合、阻断按指标@版本、未解析引用按引用方→目标；新增/持续/消失 + 指标版本变化 |

样例数据 `sample_data/metrics.json`：24 个指标版本 / 20 个指标名，故意包含
**3 组同名异口径**（gmv 三口径、uv 两口径、conversion_rate 原值与百分比）、
**2 条依赖环**（cpa↔cpc、unit_price↔gmv_unit）、
**4 个跨版本依赖**（gmv@2→gmv@1、uv@2→uv@1、pay_gmv_share→gmv@3、uv_per_gmv→uv@2）、
**2 个未发布依赖**（gmv@3、uv@2）。

## 启动

```bash
cd batch-20261114b/projects/gb-626
cp .env.example .env          # 可选，默认 18215
docker compose up -d --build
curl -s http://127.0.0.1:18215/health
docker compose down -v        # 重建数据库：删掉 metricring-data 卷即可
```

## 演示步骤

1. 打开 `http://127.0.0.1:18215/` → 点「执行注册表体检」→ 结果页：3 组同名异口径、2 条依赖环、2 个发布阻断、逐条依据（原始表达式 vs 归一化表达式）；
   首份体检全部列为「新增（待确认基线）」，点「确认为新基线」后，后续体检只报告相对基线的增减。
2. `/workbench` 选 `gmv_daily@1`，把维度从 `dt,channel` 改成 `dt` → 「计算变更影响面」：6 个下游中有 2 个 breaking（`gmv_7d`、`pay_gmv_share` 按 channel 分组），其余 4 个 requires_sync。
3. `/review` 登记修正后的 `gmv_daily@2` 并发布 → 门禁放行；再试发布 `gmv@3`（支付口径）→ 被同名异口径拦截。

## 页面

| 路由 | 内容 |
|---|---|
| `/` | 使用者/场景/输入、注册表概览、冲突与环预览、演示步骤、体检按钮 |
| `/workbench` | 选指标、编辑表达式/维度/聚合、解析归一化、影响面、上游依赖树、注册表全量 |
| `/result/{check_id}` | 候选/基线结论、相对基线的新增/持续/消失（新增项带版本变化与受影响下游）、冲突 3 组、环 2 条、发布阻断、跨版本依赖、全部指标归一化对比；候选可确认或失败可重试 |
| `/review` | 最近体检、变更影响面（breaking / requires_sync）、发布按钮、版本历史与历史体检 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/registry` | 注册表 + 统计（冲突/环/阻断计数） |
| GET | `/api/metrics` | 指标列表 |
| POST | `/api/parse` | `{expression}` → 归一化、引用清单、不可解析引用；语法错误 400 |
| POST | `/api/check` | 执行体检并落库（候选）；可带 `attempt_key` 做幂等重试，返回完整结论与历史对比 |
| GET | `/api/check/{id}` | 读取体检结果；不存在 404 |
| POST | `/api/check/{id}/confirm` | 把候选体检确认为新基线；非候选状态 409、不存在 404 |
| GET | `/api/baseline` | 当前已确认基线（没有则 `has_baseline=false`） |
| POST | `/api/impact` | `{name,version?,expression,aggregation?,dimensions?}` → 影响面；指标不存在 404、表达式非法 400 |
| POST | `/api/registry` | 登记新版本；重复 409、字段非法或缺引用 400 |
| POST | `/api/publish` | 发布校验；被拦截 409 并返回 blockers |

```bash
# 注册表与体检
curl -s http://127.0.0.1:18215/api/registry | python3 -m json.tool | head -20
curl -s -X POST http://127.0.0.1:18215/api/check | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['stats'])"
```

## 目录

```
app/
  main.py        # FastAPI 路由（薄）
  expr.py        # 公式解析 + 归一化（纯函数）
  graph.py       # 依赖图 + DFS 三色环检测
  conflict.py    # 同名异口径
  gate.py        # 变更门禁 + 发布门禁
  history.py     # 体检历史对比（新增/持续/消失 + 指标变化 + 下游）
  registry.py    # 指标注册校验/索引/版本解析
  report.py      # 体检编排
  store.py       # SQLite
  seed.py        # 幂等 seed
  settings.py    # 路径常量
  templates/     # base/index/workbench/result/review
  static/        # app.js（原生）、style.css
sample_data/metrics.json
tests/           # test_expr / test_graph / test_conflict / test_gate / test_history / test_check_lifecycle / test_api
```

## 测试

```bash
docker compose run --rm app python -m pytest -q
```

## 已知限制

- 归一化只做结构等价（去空白/括号/`*1`/常量化），不做代数化简：`a*2/2`、`a+a` 不会被合并成 `a` 或 `2*a`。
- 依赖解析是静态的：裸引用固定解析到「最新已发布版本」，不回放历史时点（想固定就写 `@版本`）。
- 环检测覆盖指标引用环；`dim:` 维度引用不参与图，维度冲突靠「变更门禁的维度移除判定」兜底。
- 表达式不含窗口/时间过滤语法（`dt=` 之类），明细口径差异由 `dim:` 与描述字段表达。
- 单进程 SQLite（WAL），不做并发写入队列；演示规模 24 条指标足够。
- 历史对比是「两份快照之间」的差异，不做时点回溯：被移除版本只在「实际变化」里体现，问题身份键按当前/基线快照各自计算。
- 问题身份键是启发式的：同名冲突按指标名、环按节点集合（方向变化不算新问题）、阻断按指标@版本、未解析引用按引用方→目标。
  如果同一指标名下口径分组结构大改，它在「持续」里会显示版本/组数变化，而不是拆成多条新增。

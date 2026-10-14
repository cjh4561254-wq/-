# 核心工作流：一次提问从进到出

> 这份文档写的是**系统实际怎么跑**，不是"设计上打算怎么跑"。
> 所有行号按**当次工作区**实测（HEAD `a677425` 加上一处未提交的
> `app/agents/orchestrator.py` 补丁，口径见 `docs/ARCHITECTURE.md` §14.9 末尾）；
> 所有耗时与输出都是本机实测（Windows 11 / Python 3.12.8，
> **没配 API Key**，所以规划和报告走的是规则兜底 + 模板路径）。
> 想直接看一整段真实运行输出：[docs/DEMO.md](DEMO.md)。

---

## 0. 一分钟总览

```
   你 / 前端                接口层                        后台线程                  状态存在哪
   ─────────                ──────                        ────────                  ──────────
 ① 选文件      ──POST /api/datasets/upload──►  七步：校验→落盘→解析→登记  ──►  store.py 的进程内 dict
                                                                                  （键 = dataset_id）
 ② 提问        ──POST /api/analysis (202)──►  background.submit ───────────►  registry.py 的进程内 dict
                立刻返回 workflow_id                    │                        （键 = workflow_id）
                                                        ▼
                                        run_workflow(state)   ← 整条链路只在这一个后台线程里跑
                                        ┌──────────────────────────────────────────┐
                                        │ 1 profiling    build_profile()     零 LLM │
                                        │ 2 planning     planner.plan()      ★模型① │
                                        │ 3 execution    analysis.run_task()  零 LLM │
                                        │ 4 charting     charts.render_chart()零 LLM │
                                        │ 5 reporting    validation.build_report() ★模型② │
                                        │ 6 validation   validation.validate()零 LLM │
                                        └──────────────────────────────────────────┘
 ③ 每秒轮询    ──GET /api/workflows/{id}─────────►  WorkflowStatusData.from_state()（13 个小字段）
 ④ 拿报告      ──GET /api/workflows/{id}/report──►  ReportData = report + validation + results + llm_used
 ⑤ 看图        ──GET /api/workflows/{id}/charts/{filename}──►  FileResponse(image/png)
```

★ 只有标了「模型」的两步会发出网络请求；其余四步是纯 pandas。**模型不参与任何数值计算**，
它只在第 2 步填一张"用哪个工具、带什么参数"的表（`TaskPlan`），在第 5 步把已经算好的数字说成人话。
执行阶段（第 3 步）一行 LLM 调用都没有 —— 这是全项目最重要的一条约束，
`tests/test_orchestrator.py` 里有一条 AST 检查盯着它（`orchestrator` 不许 import `app.llm`）。

---

## 1. 七个环节（每段：输入 / 做什么 / 输出 / 失败怎么办）

### 1.1 上传：`POST /api/datasets/upload`

- **输入**：一个 multipart 文件（只认 `.csv` / `.xlsx`；`.xls` 会被告知"请另存为 .xlsx"）。
- **做什么**：`app/api/routes_datasets.py:113` 的七步，顺序不能换：

  | 步 | 行号 | 做什么 | 为什么是这个位置 |
  |---|---|---|---|
  | ① | 125-134 | 扩展名白名单 | 必须在任何写盘之前，否则能把任意内容先落进上传目录 |
  | ② | 136-139 | `dataset_id = uuid4().hex` | id 由服务端生成，用户文件名不参与命名 |
  | ③ | 141-142 | 分块读 + 累计大小，超限抛 413 | 超限时磁盘上**什么都没有** |
  | ④ | 144-146 | 对**原始字节**算 sha256 | 语义是"用户传上来的那份文件" |
  | ⑤ | 148-160 | 落盘为 `{dataset_id}.{ext}`，写前 `resolve()` 复核目录 | 落盘名与用户输入完全解耦 |
  | ⑥ | 162-174 | 真解析一遍（`load_dataframe`），失败**删掉刚写的文件** | 解析不了的文件不留垃圾 |
  | ⑦ | 176-186 | 登记进 `store`（进程内 dict） | 先有文件再登记，不会留下"有元信息没文件"的假记录 |

- **输出**：`ApiResponse[Dataset]` —— 元信息 + 前 5 行预览（`preview_rows`）。
  实测 `data/samples/sales.csv`：`row_count=2168`、`column_count=9`、`size_bytes=141028`。
- **失败怎么办**：415 `UNSUPPORTED_FILE` / 413 `FILE_TOO_LARGE` / 400 `BAD_REQUEST`
  （空文件、只有表头、解析失败、超行/列上限）。表见 §4。

### 1.2 画像：`GET /api/datasets/{dataset_id}/profile`

- **输入**：`dataset_id`。
- **做什么**：`app/data/profiler.py:68` 的 `build_profile(df, dataset_id)` —— 缺失值、重复行、
  语义类型（numeric / categorical / datetime / boolean / text）、五数概括。**零 LLM**，全是 pandas。
- **输出**：`DatasetProfile`。首次调用时算，之后走 `store` 里的缓存（同一个 `dataset_id` 只算一次）。
- **失败怎么办**：404 `NOT_FOUND`（id 不存在）。
- 顺带说一句：`POST /api/analysis` 的 profiling 阶段复用的就是这份画像，不会重算一遍。

### 1.3 提问：`POST /api/analysis` → **202**

- **输入**：`{"dataset_id": "...", "question": "按地区分组汇总销售额", "max_steps": 6}`。
- **做什么**：`app/api/routes_analysis.py:49-54`，装饰器上写死 `status_code=202`：
  查数据集（不存在 → 404）→ 建 `WorkflowState` → `registry.create(state)`
  → `background.submit(state, tasks)`（`app/core/background.py:40`）。
- **输出**：**HTTP 202** + `AnalysisAccepted`：`workflow_id`（32 位十六进制）、`dataset_id`、
  `status="PENDING"`、`created_at`。**这里不返回分析结果**，因为它还没算。
- **失败怎么办**：404 `NOT_FOUND`（数据集不存在）；422 / 400 `BAD_REQUEST`（请求体不合法）。
  注意 `SERVICE_UNAVAILABLE`（503）在当前实现里**没有触发点**：模型没配或连不上都不是失败，
  而是退回规则兜底（见 §3 最后一段）。

### 1.4 六个阶段：`profiling → planning → execution → charting → reporting → validation`

`app/agents/orchestrator.py:47` 的 `_STEPS` 就是这个顺序，`state.step_index` 是"已完成几个阶段"。

| 阶段 | 代码 | 输入 | 输出 | 失败怎么办 |
|---|---|---|---|---|
| profiling | `orchestrator.py:79-91` → `profiler.build_profile` | DataFrame | `DatasetProfile` | 抛异常 → 整条 `FAILED` |
| planning | `:93-110` → `planner.plan()` | 问题 + 画像 | `TaskPlan`（≤ `max_steps` 个任务） | 模型不可用 → 规则兜底；模型输出解析不了 → 重试 2 次后兜底 |
| execution | `:112-116` → `analysis.run_task()` ×N | `AnalysisTask` + DataFrame | `ToolResult` 追加进 `state.results` | 单任务失败不拖垮整条（见 §3） |
| charting | `:118-122` → `charts.render_chart()` | 成功且有图的任务 | `ChartResult` 追加进 `state.charts` | 出图失败 → 记 `MISSING_CHART_FILE` 警告，继续 |
| reporting | `:124-138` → `validation.build_report()` | `state.results` | `FinalReport`，`has_report=True` | 抛异常 → 不放 `report`，记一条 `INTERNAL_ERROR`，工作流不判 `FAILED` |
| validation | `:140-149` → `validation.validate()` | 报告 + 工具结果 | `ValidationResult`（数字溯源） | 校验本身出错也只记 issue，不判 `FAILED` |

执行阶段每个任务的实际动作（`app/agents/analysis.py`）：
查白名单工具 → `validate_call()` 做 5 类校验的前 4 类（`app/tools/validate.py:58`）
→ 调工具函数 `func(df, params)` → 返回值清洗成可 JSON 化（`app/tools/jsonable.py`）
→ 装进 `ToolResult`。第 5 类校验（`BAD_RESULT`，返回结构不对）在这一步的出口处做。

### 1.5 轮询：`GET /api/workflows/{workflow_id}`

- **输入**：`workflow_id`。
- **做什么**：`app/api/routes_workflows.py:85-118`：查 registry → **顺手做一次惰性超时检查**
  （`_expire_if_stale`，`:230`）→ 用 `WorkflowStatusData.from_state(state)` 生成响应。
- **输出**：13 个小字段：`workflow_id / dataset_id / question / status / step / step_index /
  total_steps / tasks[] / charts[] / error / created_at / finished_at / has_report`。
  **故意不含 `report` 和 `results`** —— 实测同一份状态下 `WorkflowState` 序列化出来 14 431 字节，
  这个小状态只要 1 318 字节（约 10.9 倍），而前端每秒问一次、每次几十分钟。
  `tasks[]` 是 `TaskBrief`（只有 `task_id / description / status / error`，**没有 `tool_name`**）。
  `has_report` 是**现算**的（`state.report is not None`），不是抄 `state.has_report` 字段，
  这样"接口 6 说有报告、接口 7 却给 400"这种自相矛盾不可能出现。
- **失败怎么办**：404 `NOT_FOUND`（id 没登记过，含服务重启后内存态丢失的情况）。
  **超时不是错误响应**：状态会被就地改成 `FAILED`，但 HTTP 仍是 **200**（见 §4）。

### 1.6 报告：`GET /api/workflows/{workflow_id}/report`

- **输入**：`workflow_id`。
- **做什么**：`routes_workflows.py:121-162`。两条分支必须分得开：
  工作流不存在 → **404**（这个 id 彻底没救了）；工作流在、报告还没生成 → **400**
  （`detail` 里带当前 `status`，告诉用户卡在哪一步）。
- **输出**：`ReportData` = `{report, validation, results, llm_used}`。
  `llm_used` 从 `report.llm_used` 取，不另存一份（避免两份不一致骗人）。
- **失败怎么办**：404 / 400，见表 §4。

### 1.7 图表：`GET /api/workflows/{workflow_id}/charts/{filename}`

- **输入**：`filename`，形态固定是 `[0-9a-f]{12}.png`（`routes_workflows.py:62` 的正则）。
  这个正则同时是第一道路径穿越防线：`..`、`/`、`\`、盘符、空串全都匹配不上。
- **做什么**：再查一次 registry → 把目标路径 `resolve()` 后确认它还在该工作流的图表目录里
  （`is_relative_to`）→ `FileResponse(media_type="image/png")`。
- **输出**：PNG 字节流。实测一张 6 点柱状图 24 020 字节。
- **失败怎么办**：400（文件名形态不对）/ 404（工作流不存在，或 PNG 文件已经不在了）。

---

## 2. 链路上传的是什么（数据结构）

| 数据 | 定义在哪 | 在链路上代表什么 |
|---|---|---|
| `Dataset` / `DatasetSummary` | `app/schemas_dataset.py` | 上传的返回值；`preview_rows` 是给前端表格用的 |
| `DatasetProfile` | 同上 | 画像；planning 阶段的输入之一 |
| `AnalysisTask` / `TaskPlan` | `app/schemas_workflow.py:116` / `:144` | **模型与真实计算之间的唯一接口**：模型只能填"工具名 + 参数 + 要不要出图" |
| `ToolResult` | `app/schemas_tool.py:55` | 一个工具跑完的结果（10 个字段）：`task_id / tool_name / ok / data / columns / row_count / elapsed_ms / error_code / error_message / created_at` |
| `ChartResult` | `app/schemas_tool.py:84-130` | 一张图（10 个字段）：`chart_id / filename / title / chart_type / url / source_task_id / x_field / y_field / point_count / created_at`；构造时有一个 `model_validator` 强制 `filename == chart_id + ".png"` 且 `url` 以 `/charts/{filename}` 结尾 |
| `FinalReport` | `app/schemas_workflow.py:198` | 报告：`sections[]`（表格直接复制 `ToolResult.data.rows`，不给模型改写） |
| `ValidationResult` | `app/schemas_workflow.py:174` | 校验结果：`passed / checked_numbers / unsupported_numbers[] / issues[]` |
| `WorkflowState` | `app/schemas_workflow.py:215` | **全链路唯一的可变状态**，放在 registry 的进程内 dict 里 |
| `WorkflowStatusData` | `app/schemas_workflow.py:319` | 轮询用的小状态（13 字段） |

两条硬规矩：

1. **`pd.DataFrame` 不进任何 Pydantic 模型**。它又大又会变，而且 FastAPI 序列化会报
   `is not JSON serializable`。模型只传 `dataset_id`，DataFrame 住在 `app/data/store.py` 的 dict 里。
2. **只有 Orchestrator 能改 `WorkflowState`**。子 Agent 都返回新对象；`render_chart()` 连 `state`
   都不收 —— 出图后"登记进 `state.charts`"由 `orchestrator.py:243-283` 紧跟其后做，
   免得出现"PNG 写好了但列表里没有"或者反过来"登记了但文件没写"。

---

## 3. 状态机与重试

### 3.1 工作流状态（`WorkflowStatus`，10 个值，`app/schemas_workflow.py:64-81`）

```
PENDING ──► PROFILING ──► PLANNING ──► EXECUTING ──► CHARTING ──► REPORTING ──► VALIDATING
                                                                                     │
                                                                      ┌──────────────┴──────────────┐
                                                                      ▼                             ▼
                                                                  SUCCESS              SUCCESS_WITH_WARNINGS
                                                                                              
任何阶段抛未捕获异常 ──────────────────────────────────────────────────────────────► FAILED
```

**终态只有三个**（`TERMINAL_STATUSES`，`schemas_workflow.py:86`）：`SUCCESS`、
`SUCCESS_WITH_WARNINGS`、`FAILED`。前端拿这个集合决定"还要不要继续轮询"。

`status` 与 `step` 不是一回事：`status` 是"对外怎么描述"，`step` 是"此刻具体在做什么"
（`WorkflowStep` 七个值，`schemas_workflow.py:91-99`）。整条链路走完时 `step="done"`、
`step_index=6`（= 已完成阶段数）。

`SUCCESS_WITH_WARNINGS` 有四种成因，全都来自 `_final_status()`（`orchestrator.py:295-324`）：
① 有任务 `FAILED`；② 有任务 `SKIPPED`（依赖的任务失败了）；③ 报告没出来（`state.error` 非空）；
④ 校验在报告里查出了 error 级 issue（编造的数字、不存在的列名）。
这四条**没有任何一条通往 `FAILED`** —— 校验发现问题只是说明"报告里有东西对不上"，
数字其实都算出来了，判成失败会把已经到手的结果一起扔掉。

反过来，真会变成 `FAILED` 的只有这几种（都能在代码里指出具体行）：

| 触发 | 在哪 | `error.code` |
|---|---|---|
| 数据集已经不在进程内存里（服务重启过） | `orchestrator.py:69-77` | `NOT_FOUND` |
| profiling 抛异常（后面所有阶段都没有依据了） | `:83-91` | `INTERNAL_ERROR` |
| planning 抛异常（`plan()` 的契约被破坏） | `:102-110` | `INTERNAL_ERROR` |
| 超过 `max_workflow_seconds` | `:335` / 轮询侧的 `:230` | `WORKFLOW_TIMEOUT` |
| 工具调用次数超过 `tool_call_limit` | `:346` | `INTERNAL_ERROR` |
| 后台线程里逃出未捕获异常 | `app/core/background.py:53-77` | `INTERNAL_ERROR` |

### 3.2 任务状态（`TaskStatus`，6 个值，`schemas_workflow.py:48-61`）

`PENDING / RUNNING / SUCCESS / FAILED / RETRYING / SKIPPED`。
同一时刻最多一个任务是 `RUNNING`（执行是顺序的，不并发）。
依赖任务失败时，后面的任务会被标成 `SKIPPED` 而不是硬跑。

### 3.3 重试上限（都在代码里，不是文档里的愿望）

| 名字 | 值 | 在哪 | 管什么 |
|---|---|---|---|
| `_MAX_WORKFLOW_RETRIES` | 6 | `orchestrator.py:50` | 整条工作流的重试总预算（`WorkflowState.retry_count` 的 `le=6` 与它对齐） |
| `_MAX_TASK_RETRIES` | 1 | `orchestrator.py:54` | 单个任务最多额外重试 1 次 |
| `llm_max_retries` | 2 | `app/config.py:96` | LLM 客户端内部重试次数，`总请求数 = 2 + 1 = 3`（`app/llm/client.py:129-130`） |
| `tool_call_limit` | 20 | `app/config.py:107` | 工具调用总次数上限（超出 → `INTERNAL_ERROR`，`orchestrator.py:343-352`） |
| `max_workflow_seconds` | 180 | `app/config.py:104` | 整条链路的时间上限（超出 → `WORKFLOW_TIMEOUT`） |

**哪些失败会重试**（`orchestrator.py:198-240`）：

- `BAD_PARAMS` / `COLUMN_NOT_FOUND` / `TOOL_NOT_FOUND` / `BAD_OPERATION` → **不重试**。
  参数是模型给的，重试同一个参数只会得到同一个结果。
- `BAD_RESULT`（工具自己抛了 `KeyError`/`TypeError`/`ValueError`，或返回值进不了 JSON）→
  重试 1 次；再失败就**跳过这个任务继续跑后面的**，这才是"换策略"。

**重试会不会叠乘**：任务级重试会消耗 1 点工作流级预算（`orchestrator.py:226`
`state.retry_count += 1`），所以整条工作流最多 6 次任务级重试，不会出现"每个任务各重试 6 次"。
两次尝试都会留在 `state.results` 里（`:230`），排查时"第一次报了什么"很重要。
LLM 客户端那 3 次请求不经过 `retry_count`，它发生在 planning / reporting 内部。

**重试耗尽怎么办**：单个任务反复失败 → 该任务 `FAILED`，其余任务继续；
只要还有任务失败或跳过，最终状态就是 `SUCCESS_WITH_WARNINGS`；一个成功的任务都没有时，
报告里会有一段"没有可用的分析结果"（`validation.py:107-122`），状态仍是 `SUCCESS_WITH_WARNINGS`。
只有 §3.1 那张表里的六种情况才会走到 `FAILED`。

> 一个诚实的说明：`build_report(state, validation)` 有一个"带着校验结果把报告重写一遍"的参数位
> （`validation.py:71-82`），但**当前 Orchestrator 只调了一次**（`orchestrator.py:127`），
> 这条重写路径还没有接线。文档不把它说成"已经实现"。

---

## 4. 失败时的表现（HTTP + `error.code` + 前端看到什么）

| 失败 | HTTP | `error.code` | 前端看到 |
|---|---|---|---|
| 文件类型不支持（含 `.xls`） | 415 | `UNSUPPORTED_FILE` | 就地提示"请另存为 .xlsx 再上传" |
| 超过 `max_upload_mb` | 413 | `FILE_TOO_LARGE` | 提示换个小文件 |
| 空文件 / 只有表头 / 解析失败 / 超行或列上限 | 400 | `BAD_REQUEST` | 显示 `error.message` |
| `dataset_id` 不存在 | 404 | `NOT_FOUND` | "数据集不存在，请重新上传" |
| 请求体不合法（缺字段、类型错） | 422 | `BAD_REQUEST` | 显示 `error.message` |
| `workflow_id` 没登记过（含服务重启） | 404 | `NOT_FOUND` | 停止轮询，提示重新提问 |
| 报告还没生成就去取 | 400 | `BAD_REQUEST` | **继续轮询**（`detail` 里带当前 status） |
| 图表文件名形态不对 | 400 | `BAD_REQUEST` | 忽略这张图 |
| 图表 PNG 不在磁盘上了 | 404 | `NOT_FOUND` | 图裂，报告仍可读 |
| **工作流超时** | **200** | `WORKFLOW_TIMEOUT` | 状态变 `FAILED`，前端停止轮询并显示"分析超时" |
| 未捕获异常 | 500 | `INTERNAL_ERROR` | 显示"服务器内部错误" |
| 依赖不可用（预留） | 503 | `SERVICE_UNAVAILABLE` | 前端把它列为可重试（`app/web/app.js:37`） |

两个反直觉点值得单独记住：

1. **超时的 HTTP 码是 200**。超时不是一个"这次查询失败"，而是"这条工作流失败了" ——
   查询本身成功了，返回的就是它的状态。映射写在 `app/errors.py` 的 `ERROR_STATUS` 里。
2. **`SERVICE_UNAVAILABLE` 目前没有任何触发点**。它是为"模型配了但连不上、且我们不兜底"这种
   未来场景预留的；当前实现里模型不可用一律退回规则兜底，工作流照样出报告。
   把它写成"会发生"是不诚实的。

超时是怎么被发现的：**没有任何定时器在后台扫**。`_expire_if_stale()`（`routes_workflows.py:230`）
在每次轮询时判断"心跳已经多久没刷新"，超过 `max_workflow_seconds` 就地改成 `FAILED`。
为什么这么设计：后台线程可能已经死了（进程被杀、线程里出现无法捕获的错误），定时器也就没人
去重启它；而"用户来轮询"是必然发生的动作。它还**必须先判终态再判超时** ——
顺序写反的话，一条已经成功的工作流会在三分钟后的某次普通轮询里被改判成 `FAILED`。

---

## 5. 时间线（本机实测，规则兜底路径）

一次真实运行（`data/samples/sales.csv`，2168 行 × 9 列，问题「按地区分组汇总销售额」）：

| 时刻 | 发生了什么 | 实测 |
|---|---|---|
| T+0 | `POST /api/analysis` 返回 202 + `workflow_id` | 5.2 ms |
| +1.8 ms | 轮询第一次看到 `PROFILING`（`step_index=0`） | 已经开跑 |
| +62.2 ms | 轮询看到 `CHARTING`（`step_index=3`，`charts=0`） | 前三阶段合计约 60 ms |
| +184.7 ms | 轮询看到 `SUCCESS`（`step_index=6`，`charts=1`，`has_report=true`） | 全链路 184.7 ms |
| 之后 | `GET .../report` → `sections=1`、`checked_numbers=2`、`issues=[]`、`llm_used=false` | — |
| 之后 | `GET .../charts/8ab0226437da.png` → `200 image/png`，24 020 字节 | x=地区, y=销售额_sum, 6 个点 |

另一轮（问题「哪个地区销售额最高」，命中 Top N 规则）：从 POST 返回到终态 **135 ms**，
`top_n` 任务成功，图表 20 942 字节。

三点如实说明：

- 上表是**没配 key、走规则兜底**的耗时。配了 key 之后 planning 与 reporting 要等模型往返，
  这两步会从"几毫秒"变成"几秒到几十秒"，其余阶段不变。
- 表里的 20 ms 一次是我为了看清阶段而在探针里用的间隔；前端是 **1 秒**一次
  （`app/web/app.js:50` 的 `POLL_INTERVAL_MS = 1000`）。小数据上你通常只会看到
  `PENDING → SUCCESS` 两三个点，中间阶段根本来不及被看到 —— 这不是卡住。
- 阶段那么快的原因是：2168 行数据上 `groupby` 是毫秒级，出图（matplotlib 第一次 import）
  反而是最慢的一步。

---

## 6. 手工复现（命令都能直接粘贴）

在仓库根打开 PowerShell。**第 0 步**起服务：

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --port 8000
```

另开一个窗口，**第 1 步**上传示例数据（`curl.exe` 而不是 `Invoke-WebRequest`：
PowerShell 5.1 的那个 cmdlet 不支持 `-Form`）：

```powershell
curl.exe -s -F "file=@data/samples/sales.csv" http://127.0.0.1:8000/api/datasets/upload
```

预期：`ok=true`，`data.dataset_id` 是 32 位十六进制，`row_count` 是 2168，`column_count` 是 9。
把它复制出来，下面用 `$id` 代表。

**第 2 步**提问。请求体必须写成 **UTF-8 文件**再让 curl 读，原因是本机实测过的一个坑：
把 JSON 直接当内联参数传给 `curl.exe` 时，PowerShell 5.1 会把里面的双引号吞掉，
服务端收到的是 `{'type': 'json_invalid', ... 'Expecting property name enclosed in double quotes'}`
→ `HTTP 422`（**不是**中文乱码的问题，是引号的问题）：

```powershell
$id = "<把 dataset_id 粘这里>"
$body = '{"dataset_id":"' + $id + '","question":"按地区分组汇总销售额","max_steps":6}'
Set-Content -Path .tmp\body.json -Value $body -Encoding UTF8
curl.exe -s -w "`nHTTP %{http_code}`n" -X POST -H "Content-Type: application/json" -d "@.tmp/body.json" http://127.0.0.1:8000/api/analysis
```

（`Set-Content -Encoding UTF8` 在 PowerShell 5.1 上会写成"带 BOM"的 UTF-8，本机实测服务端
照样正常解析、中文问题原样入库；换个写法也行，只要保证文件是 UTF-8。）

预期：**`HTTP 202`**，返回 `data.workflow_id` 与 `data.status = "PENDING"`。

**第 3 步**轮询（每 1 秒一次，看到终态就停）：

```powershell
$wf = "<把 workflow_id 粘这里>"
do {
  $s = Invoke-RestMethod "http://127.0.0.1:8000/api/workflows/$wf"
  "{0}  {1}  step_index={2}  charts={3}" -f $s.data.status, $s.data.step, $s.data.step_index, @($s.data.charts).Count
  Start-Sleep -Seconds 1
} while ($s.data.status -notin @("SUCCESS", "SUCCESS_WITH_WARNINGS", "FAILED"))
```

预期：最后一行是 `SUCCESS  done  step_index=6  charts=1`（`step` 是 `done`，不是 `validation`）。

**第 4 步**取报告与图表：

```powershell
# 报告（含校验结果）
Invoke-RestMethod "http://127.0.0.1:8000/api/workflows/$wf/report" |
  ForEach-Object { $_.data.report.summary; $_.data.report.sections[0].text; "校验: passed=$($_.data.validation.passed) checked=$($_.data.validation.checked_numbers) issues=$(@($_.data.validation.issues).Count)"; "llm_used=$($_.data.llm_used)" }

# 图表（把 url 原样拼在服务地址后面，不要自己拼路径）
$url = (Invoke-RestMethod "http://127.0.0.1:8000/api/workflows/$wf").data.charts[0].url
curl.exe -s -o .tmp\chart.png -w "HTTP %{http_code}  %{content_type}  %{size_download} bytes`n" "http://127.0.0.1:8000$url"
Start-Process .tmp\chart.png   # 打开看看，中文标题不是方框
```

预期报告片段（实测原文）：

```
本次未使用大模型（llm_used=false），以下结论为工具结果的直接汇总

针对问题「按地区分组汇总销售额」，系统一共规划了 1 个分析任务，成功 1 个。
排在最前面的一组是 地区 = 华南，销售额_sum = 24,871,657.00（共 6 行结果）。（工具：group_aggregate，任务：T1）
校验: passed=True checked=2 issues=0
llm_used=False
```

**嫌麻烦就用一条命令**：`python scripts/demo.py` —— 它自己起 uvicorn（端口 8128，
**不占 8000**）、走真实 HTTP 跑完九段、最后自己关服务并自查端口已释放。
真实输出见 [docs/DEMO.md](DEMO.md)。

---

## 7. 单进程与并发限制（用之前必须知道）

- **状态全在内存里**：数据集在 `app/data/store.py` 的 dict，工作流在 `app/core/registry.py` 的 dict。
  服务一重启，两者全没 —— 老的 `dataset_id` / `workflow_id` 一律返回 404（`detail` 里写明了原因）。
- **必须单进程**：`uvicorn --workers 4` 会让上传落在一个进程、查询落到另一个进程，
  表现就是"刚传完就 404"。后台任务用的是 FastAPI 的 `BackgroundTasks`，也在同一个进程里。
  这也是不引入 Celery / Redis 的原因（见 `docs/ARCHITECTURE.md` §11）。
- **同一进程内可以并发请求，但执行是顺序的**：多个提问会各自起一条工作流，各自的执行阶段
  顺序跑；数据库级别的并发问题不存在（因为没有数据库）。
- **进程内的 dict 加了锁**（`registry.py` 的 `_lock`），保护的是"读-改-写"那一下；
  `WorkflowState` 内部字段的写入不需要锁，因为整条 `run_workflow` 只在单一后台线程里顺序执行，
  并且拿到的是同一个对象引用。
- **重启不丢历史**这件事目前**没做**：没有 SQLite、没有归档文件。这是有意的取舍
  （`docs/DECISIONS.md` D-14），写在文档里比假装有更省事。

---

## 8. 相关文件

| 想改什么 | 去哪 |
|---|---|
| 阶段顺序、重试规则、终态判定 | `app/agents/orchestrator.py` |
| 规则兜底怎么把问题映射成工具调用 | `app/agents/planner.py`（`_RULES` 优先级表） |
| 5 类校验 | `app/tools/validate.py`、`app/tools/registry.py`、`app/agents/analysis.py` |
| 数字溯源 / 报告拼装 | `app/agents/validation.py` |
| 图表与中文字体 | `app/agents/charts.py` |
| 接口与状态码 | `app/api/routes_*.py`、`app/errors.py` |
| 为什么这么设计、为什么不引入 X | [docs/ARCHITECTURE.md](ARCHITECTURE.md) §11、[docs/DECISIONS.md](DECISIONS.md) |
| 面试怎么讲 | [docs/INTERVIEW_QA.md](INTERVIEW_QA.md)、[docs/HIGHLIGHTS.md](HIGHLIGHTS.md) |
| 真实运行输出 | [docs/DEMO.md](DEMO.md) |

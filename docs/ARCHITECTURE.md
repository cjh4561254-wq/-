# AI Data Analyst Agent —— 系统架构设计（v1.1）

> 文档性质：**设计方案**，不是实现。第 4 节的 Pydantic 模型只写在本文档的代码块里，落地成 `.py` 文件由对应负责人在指定日期做。
> 版本：**v1.1**（2026-10-13 修订）。v1.0 → v1.1 按 `docs/REVIEW_ARCHITECTURE.md` 的 9 条审核意见修订，逐条落实记录见 **第 14.2 节**。
> 适用水平：会基础 Python、会调用大模型 API、正在学 FastAPI。全文避免抽象术语；出现一个新名词时，第一次出现就给一句大白话解释。
> 环境事实（已核实，含第一阶段返工 commit 460cf8c）：Windows 11 + PowerShell 5.1、Python 3.12.8、项目根目录 `D:\个人项目01\ai-data-analyst-agent`、虚拟环境 `.venv`（`--system-site-packages`）。已装：fastapi 0.136.3、uvicorn 0.48.0、pydantic 2.13.4、pandas 3.0.3、numpy 2.4.6、pytest 9.1.1、httpx 0.28.1、openai 2.41.0、python-dotenv 1.2.2、python-multipart 0.0.32、openpyxl 3.1.5、matplotlib 3.11.2。
>
> **两条已经生效的代码约定（本文档全部示例都遵守，后续所有新代码也必须遵守）**：
> 1. **取配置一律 `from app import config` + 函数内 `config.get_settings()`**；不存在模块级 `settings` 对象，测试里改完环境变量用 `config.reset_settings_cache()`。理由见 `app/config.py` 的 docstring：模块级对象在 import 时就绑死了，测试会"静默失效"。
> 2. **matplotlib 缓存目录**由 `app/config.py` 顶部设置 `os.environ.setdefault("MPLCONFIGDIR", <项目根>/.mplcache)`（并 `mkdir`）。这是现实坑的修复：本机 `C:\Users\jun\AppData\Local\matplotlib` 曾拒绝访问（WinError 5）。因此 **`agents/charts.py` 里不允许出现 `matplotlib.use("Agg")` 之外的后端设置，也不允许改 `MPLCONFIGDIR`**，直接 `from matplotlib.figure import Figure` 使用即可。

---

## 0. 一句话说清这个系统在干什么

用户上传一个 CSV/Excel → 用中文提一个问题（例：「按地区看销售额前 5，并画柱状图」）→ 大模型**只负责把问题翻译成一份"分析计划"**（选哪几个工具、用哪些字段）→ 后端用 pandas 真实执行这些工具 → 得到真实数字和图表 → 大模型**只负责用中文解释这些数字** → 校验 Agent 检查"报告里出现的每个数字是否真的来自工具结果" → 返回网页。

**贯穿全系统的铁律：数字只能由 Python 算出来。大模型不许做算术，不许发明字段名，不许直接执行代码。**

---

## 1. 总体架构图（含每步的输入/输出数据类型）

### 1.1 全景图

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  ① 浏览器（单页 app/web/index.html + app.js + style.css）                      │
│     输入：用户点选的 CSV/XLSX 文件（File 对象） + 中文问题（string）             │
│     输出：fetch() 的 JSON 请求体 {question: str}；每 1 秒轮询一次状态            │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                │ ② HTTP（JSON；上传那一次是 multipart/form-data）
                                ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  ③ FastAPI 接口层 app/api/                                                     │
│     routes_health / routes_datasets / routes_analysis / routes_workflows       │
│     输入：HTTP 请求 → Pydantic 校验（UploadFile / QuestionRequest 等）           │
│     输出：ApiResponse[T] 统一外壳 {ok, data, error}（已实现于 app/schemas.py）    │
│     职责：只做校验、取数据、编排队列，**不写 pandas、不拼 Prompt**                │
└───────────────────────────────┬──────────────────────────────────────────────┘
        │ 上传/画像：同步执行        │ 分析：立刻返回 workflow_id，后台线程干活
        ▼                          ▼
┌──────────────────────────┐  ┌──────────────────────────────────────────────┐
│ ④ 数据层 app/data/        │  │ ⑤ 后台执行 app/core/background.py             │
│   loader.py 读文件        │  │   BackgroundTasks.add_task(run_workflow, id)  │
│   → DataFrame            │  │   → 独立线程里跑 Orchestrator（见第 8 节）      │
│   profiler.py 画像        │  └───────────────────┬──────────────────────────┘
│   store.py 进程内缓存      │                      │
│   输入：文件路径 Path      │                      ▼
│   输出：DataFrame /        │  ┌──────────────────────────────────────────────┐
│        DatasetProfile      │  │ ⑥ Orchestrator app/agents/orchestrator.py     │
└──────────────────────────┘  │   输入：WorkflowState（含 question、dataset_id）│
                                │   输出：更新后的 WorkflowState（含 report）     │
                                │   唯一有权改 WorkflowState.status / 重试计数的人 │
                                └───┬──────────┬──────────┬──────────┬─────────┘
                                    │          │          │          │
        ┌───────────────────────────┘          │          │          └──────────────────────┐
        ▼                                      ▼          ▼                                 ▼
┌────────────────────┐   ┌────────────────────────┐   ┌──────────────────┐   ┌────────────────────────┐
│ ⑦ Data Profiling   │   │ ⑧ Data Analysis        │   │(⑧自带子步骤)      │   │ ⑨ Validation & Report  │
│  agents/profiling  │   │  agents/analysis       │   │ 图表在 analysis   │   │  agents/validation     │
│  输入：dataset_id   │   │  输入：AnalysisTask     │   │ 内部生成，不单独  │   │  输入：ToolResult[]     │
│  输出：DatasetProfile│   │  输出：ToolResult       │   │ 设 Agent          │   │       + ChartResult[]  │
│  调 LLM：否         │   │  调 LLM：**否**        │   │                  │   │  输出：ValidationResult │
│                    │   │  （LLM 只在规划阶段用） │   │                  │   │       + FinalReport    │
└─────────┬──────────┘   └───────────┬────────────┘   └────────┬─────────┘   └───────────┬────────────┘
          │                          │                          │                        │
          └──────────────────────────┴──────────┬───────────────┴────────────────────────┘
                                                ▼
                              ┌────────────────────────────────────────┐
                              │ ⑩ 工具层 app/tools/（白名单，共 7 个）    │
                              │   registry.py 注册表 + 内置工具箱        │
                              │   输入：pd.DataFrame + 已校验的参数字典   │
                              │   输出：ToolResult.data（纯 Python 可     │
                              │         JSON 序列化的数字/字符串/列表）    │
                              │   铁律：唯一算数的地方，且只算白名单动作   │
                              └────────────────┬───────────────────────┘
                                               │ 需要模型时（只有两处）：
                                               │  A. 规划：question + profile → TaskPlan
                                               │  B. 解释：ToolResult[] → 文字段落
                                               ▼
                              ┌────────────────────────────────────────┐
                              │ ⑪ LLM 客户端 app/llm/client.py          │
                              │   openai SDK（OpenAI 兼容协议）          │
                              │   输入：messages + tools 白名单           │
                              │   输出：文本 或 tool_calls 参数 JSON      │
                              │   超时 llm_timeout_seconds，重试 ≤2 次    │
                              └────────────────────────────────────────┘

返回路径（自下而上）：
  WorkflowState.report / charts  →  GET /api/workflows/{id}、/report、/charts/{name}
  →  ApiResponse[T]  →  浏览器 JS 渲染（文本 + <img src="/api/workflows/{id}/charts/xxx.png">）
```

### 1.2 编号对照表（每一步的输入/输出类型）

| 步骤 | 名称 | 输入（类型） | 输出（类型） |
|---|---|---|---|
| ① | 浏览器 | 用户文件选择、问题文本 | `File`、`str` |
| ② | HTTP 传输 | 请求体 | JSON / multipart |
| ③ | FastAPI 路由 | `UploadFile`、`QuestionRequest`、路径参数 `str` | `ApiResponse[DatasetSummary]` 等 |
| ④ | 数据层 | `Path`（磁盘路径） | `pd.DataFrame`、`DatasetProfile` |
| ⑤ | 后台执行器 | `workflow_id: str` | 无返回（副作用：改 `WorkflowRegistry`） |
| ⑥ | Orchestrator | `WorkflowState` | `WorkflowState`（更新后） |
| ⑦ | Data Profiling Agent | `dataset_id: str` | `DatasetProfile` |
| ⑧ | Data Analysis Agent | `AnalysisTask` | `ToolResult`（可能带 `ChartResult`） |
| ⑨ | Validation & Report Agent | `ToolResult[]`、`ChartResult[]` | `ValidationResult`、`FinalReport` |
| ⑩ | 工具层 | `pd.DataFrame`、`dict` 参数 | `ToolResult.data: dict[str, Any]` |
| ⑪ | LLM 客户端 | `list[dict]` messages、`list[dict]` tools | `str` 或 `tool_calls` 参数 |

### 1.3 五次 Agent 之间的数据流（按时间顺序）

```
PENDING → [profiling] DatasetProfile → [planning, 调 LLM] TaskPlan
        → for task in plan.tasks:  [execution, 调工具] ToolResult  (顺序执行，不并行)
        → [charting, 调工具] ChartResult（由 analysis 步骤按需触发）
        → [reporting, 调 LLM] FinalReport（文字段落）
        → [validation, 不调 LLM] ValidationResult（逐个数字回查）
        → status = SUCCESS / SUCCESS_WITH_WARNINGS → 写回 WorkflowState
```

---

## 2. 模块划分与目录结构（第 7 天的完整目标树）

记号：`[D?]` = 第几天实现；`[角色]`：`LEAD`=Project Lead、`ARCH`=System Architect、`BE`=Backend、`AI`=AI Agent、`FE`=Frontend、`QA`=QA。`(已存在)` = 第一阶段已写好、本设计不改动。

```
ai-data-analyst-agent/
├─ docs/
│  └─ ARCHITECTURE.md                 [D1][ARCH] 本文档（唯一架构交付物）
├─ app/
│  ├─ __init__.py                     (已存在) __version__ = "0.1.0"
│  ├─ main.py                         (已存在) D6 追加静态文件挂载 /static 与 web 首页路由
│  ├─ config.py                       (已存在) 已有 max_upload_mb(43) / upload_path(54) / max_upload_bytes(60)；D2 追加 max_rows、max_columns、max_workflow_seconds、charts_dir、db_path
│  ├─ runtime.py                      (已存在) STARTED_AT + uptime_seconds()
│  ├─ schemas.py                      (已存在) D2 追加 ApiError 新错误码
│  ├─ schemas_dataset.py              [D2][BE] Dataset / DatasetSummary / DatasetProfile / ColumnProfile（见 4.1、4.2）
│  ├─ schemas_tool.py                 [D3][AI] ToolResult / ChartResult / ToolSpec（见 4.7、4.8）
│  ├─ schemas_workflow.py             [D3–D5][AI] **全部工作流相关 Pydantic 模型**（见 B-1 修订，4.3–4.6、4.9–4.11）
│  ├─ errors.py                       [D2][BE] AppError 异常 + ERROR_STATUS 映射 + sanitize_detail()（去绝对路径/用户名）；**不写 HTTP handler**（handler 在 main.py）
│  ├─ api/
│  │  ├─ __init__.py                  (已存在)
│  │  ├─ routes_health.py             (已存在) D1 不动
│  │  ├─ routes_datasets.py           [D2][BE] POST /datasets/upload、GET /datasets/{id}、GET /datasets/{id}/profile
│  │  ├─ routes_analysis.py           [D4][BE] POST /analysis（立刻返回 workflow_id）
│  │  └─ routes_workflows.py          [D3][BE] GET /workflows/{id}、/report、/charts/{filename}
│  ├─ data/
│  │  ├─ __init__.py                  [D2][BE]
│  │  ├─ loader.py                    [D2][BE] load_dataframe(path, ext) → DataFrame；编码/分隔符兜底
│  │  ├─ profiler.py                  [D3][AI] build_profile(df, dataset_id) → DatasetProfile（不调 LLM）
│  │  └─ store.py                     [D2][BE] DatasetStore：进程内 dict，存 DataFrame 与 DatasetSummary
│  ├─ core/
│  │  ├─ __init__.py                  [D2][BE]
│  │  ├─ registry.py                  [D2][BE] WorkflowRegistry：只存（dict + Lock + create/get/touch），**一行模型定义都没有**（模型全在 schemas_workflow.py）
│  │  └─ background.py                [D4][BE] submit_workflow(state) → 用 BackgroundTasks 跑线程
│  ├─ tools/
│  │  ├─ __init__.py                  [D3][AI]
│  │  ├─ registry.py                  [D3][AI] ToolSpec 定义 + register/get/list_specs/to_openai_tools
│  │  ├─ describe.py                  [D3][AI] describe_dataset、missing_values
│  │  ├─ aggregate.py                 [D3][AI] group_aggregate、top_n
│  │  ├─ compare.py                   [D3][AI] compare_groups、correlation
│  │  └─ timeseries.py                [D3][AI] time_series_summary（pandas 3.0 写法，见 13.3）
│  ├─ llm/
│  │  ├─ __init__.py                  [D4][AI]
│  │  ├─ client.py                    [D4][AI] chat(messages, tools=None) → LLMReply；超时/重试/错误封装
│  │  └─ prompts.py                   [D4][AI] 规划 Prompt、解释 Prompt（含"不许造数"硬约束）
│  ├─ agents/
│  │  ├─ __init__.py                  [D4][AI]
│  │  ├─ orchestrator.py              [D4][AI] run_workflow(state)：状态机唯一所有者
│  │  ├─ planner.py                   [D4][AI] plan(state, profile) → TaskPlan（调 LLM，附规则式兜底）
│  │  ├─ analysis.py                  [D4][AI] run_task(task, df) → ToolResult；需要时调图表工具
│  │  ├─ charts.py                    [D5][AI] render_chart(ToolResult, chart_type, ...) → ChartResult
│  │  └─ validation.py                [D5][AI] validate(state) → ValidationResult；build_report(...) → FinalReport
│  └─ web/
│     ├─ index.html                   [D6][FE] 单页：上传区 / 提问区 / 状态区 / 图表区 / 报告区
│     ├─ app.js                       [D6][FE] fetch 调用 + 1 秒轮询 + 渲染
│     └─ style.css                    [D6][FE] 原生 CSS，不用框架
├─ tests/
│  ├─ test_health.py                  (已存在) D1 不动
│  ├─ test_error_format.py            (已存在, 460cf8c) 404/405/422/路由抛错的统一外壳 + CORS 来源限制；**不要动**
│  ├─ test_loader.py                  [D2][QA] 正常 CSV / GBK CSV / xlsx / 空文件 / 只有表头
│  ├─ test_upload_api.py              [D2][QA] 上传成功、超限、非法扩展名、路径穿越文件名
│  ├─ test_tools.py                   [D3][QA] 7 个工具的正确性 + 5 类校验的拒绝路径
│  ├─ test_profiler.py                [D3][QA] 画像字段与缺失值/重复行统计
│  ├─ test_workflow_state.py          [D4][QA] 状态迁移、重试上限、超时收尾
│  ├─ test_analysis_api.py            [D4][QA] 未配 LLM 时降级返回、workflow 生命周期
│  ├─ test_validation.py              [D5][QA] 报告里塞入假数字必须被校验拦下
│  ├─ test_charts.py                  [D5][QA] PNG 落地、文件名安全、Content-Type
│  └─ test_security.py                [D6][QA] 上传白名单、大小、穿越、密钥不出现在响应里
├─ data/
│  ├─ uploads/.gitkeep                (已存在) 上传原件，文件名 = uuid4().hex + 安全扩展名
│  ├─ charts/.gitkeep                 [D5][BE] 生成的 PNG
│  └─ samples/
│     └─ sales.csv                    [D3][LEAD] 唯一演示数据（中文列名，含缺失值/重复行/日期列）；定名就是 `data/samples/sales.csv`，第 6 天有富余再加第二份数据
├─ .env.example  .gitignore  pytest.ini  requirements.txt  README.md   (已存在)
```

### 2.1 依赖方向（只允许箭头指向的方向）

```
api/       →  core/ + data/ + agents/ + schemas_*.py + errors.py
agents/    →  tools/ + llm/ + data/ + core/ + schemas_*.py + errors.py
tools/     →  （只依赖 pandas / numpy）
data/      →  （只依赖 pandas / openpyxl）
llm/       →  app/config.py + openai
core/      →  schemas_workflow.py（只 import 类型，不定义类型）
schemas_*.py → （零依赖，只 import pydantic）
errors.py  →  schemas.py（只为了用 ErrorCode）
```

**禁止**：`tools/` 反向 import `agents/`；`data/` import `api/`；任何模块 import `main.py`（handlers 在 `main.py` 里注册，`errors.py` 只定义异常与映射表，所以方向永远是 `api/ → errors.py`，不会反向）。出现反向依赖说明职责放错了地方，必须在评审时改掉而不是打补丁。

**"读文件"和"算统计"为什么分开**（第二阶段的文件划分依据，`docs/REVIEW_ARCHITECTURE.md` A-3 拍板）：

| 文件 | 职责 | 不负责 |
|---|---|---|
| `app/data/loader.py` | `load_dataframe(path, ext) -> pd.DataFrame`；编码/分隔符兜底；行数/列数上限 | 不生成 `dataset_id`、不落盘、不碰 HTTP |
| `app/data/store.py` | `DatasetStore`：进程内 dict，存 DataFrame + `DatasetSummary`；负责落盘名生成与读回 | 不做统计计算 |
| `app/data/profiler.py` | `build_profile(df, dataset_id) -> DatasetProfile` | 不调 LLM、不做业务判断 |
| `app/errors.py` | `AppError` 异常 + `ERROR_STATUS` 映射 + `sanitize_detail()` | 不写 HTTP handler |
| `app/api/routes_datasets.py` | 3 个接口的编排 | 不写 pandas |

这样分之后 `tools/` 才不会反向 import `api/`，依赖方向是一条能一句话讲清的线，而不是多余的抽象。**`app/datasets.py` 与 `app/security/files.py` 这两个提法已作废**，最终清单就是上表 5 个文件 + 第 2 节目录树。

---

## 3. 五个 Agent 的职责边界

统一约定（先记住这 5 条，后面每个 Agent 都不再重复）：

1. **子 Agent 不得修改全局工作流状态**。具体做法：子 Agent 的入参是**普通数据**（`dataset_id`、`AnalysisTask`、`pd.DataFrame`），返回**新的结果对象**；把结果写进 `WorkflowState` 的动作**只有** Orchestrator 做。
2. **子 Agent 不得互相调用**。需要两个 Agent 合作时由 Orchestrator 串起来（例如先 profiling 再 planning）。
3. **只有 `planner` 和 `validation.build_report` 会调用大模型**，其余 Agent 一行 LLM 调用都没有。这样"数字是真的"这件事在代码结构上就能看出来。
4. **大模型能做的**：把中文问题翻译成工具调用计划；把已经算好的数字解释成中文结论；指出"哪些数据质量可能影响结论"。
5. **大模型不能做的**：自己算任何数字；发明 `profile` 里不存在的列名；输出 Python/SQL 代码；决定重试和最终状态；跳过校验。

### 3.1 Orchestrator（`app/agents/orchestrator.py`，函数 `run_workflow`）

| 项目 | 内容 |
|---|---|
| 输入 | `state: WorkflowState`（`workflow_id`、`dataset_id`、`question`、`status`、`step`、`retry_count`、`max_steps`） |
| 输出 | 无返回值；原地更新 `state`（`status`、`step`、`profile`、`plan`、`results`、`charts`、`report`、`validation`、`error`、`finished_at`） |
| 能做 | ① 按固定顺序推进阶段：profiling → planning → execution → charting → reporting → validation；② 唯一有权改 `status`/`step`/`retry_count` 的地方；③ 决定是否重试、是否降级、是否立刻终止；④ 捕获所有异常并转成 `ApiError` 写进 `state.error`；⑤ 每推进一步刷新 `Heartbeat`，供超时检测使用 |
| **不能做** | ① 不自己算数字（一切通过 `tools/`）；② 不解析用户的自然语言（那是 planner 的事）；③ 不生成图表 PNG（那是 charts.py）；④ 不直接写 HTTP 响应（那是 api 层的事）；⑤ 不 import FastAPI |
| 调用 LLM | **不直接调**。它调用 `planner.plan()` 和 `validation.build_report()`，由那两个模块去调 LLM |
| LLM 职责边界 | 无（Orchestrator 自己不产生任何自然语言） |

### 3.2 Data Profiling Agent（`app/agents/` 外的 `app/data/profiler.py`，函数 `build_profile`）

> 放在 `data/` 而不是 `agents/`，因为它**完全不调 LLM**、纯粹是 pandas 统计；在 `agents/` 里的只是它的编排位置。这是刻意的：见到 `data/` 就知道这里没有模型参与。

| 项目 | 内容 |
|---|---|
| 输入 | `df: pd.DataFrame`、`dataset_id: str`、`filename: str` |
| 输出 | `DatasetProfile`（见 4.2） |
| 能做 | ① 逐列判断类型（数值/文本/日期/布尔）；② 统计缺失值、唯一值数量；③ 数值列给 `min/max/mean/std/median/q1/q3`；④ 文本列给前 3 个高频取值；⑤ 日期列给最小/最大时间；⑥ 统计完全重复行；⑦ 产出 `warnings`（例："`销售额` 有 12.3% 缺失，分组汇总结果可能偏低"） |
| **不能做** | ① 不做任何业务分析（不算"销售额 Top 5"，那是工具的事）；② 不删行、不改数据；③ 不调 LLM；④ 不决定用哪个字段分析 |
| 调用 LLM | **否**，一行都不调 |
| LLM 职责边界 | 不适用。画像结果的用途是**喂给 planner 当上下文**，让模型知道有哪些真实存在的列名和类型 |

### 3.3 Data Analysis Agent（`app/agents/analysis.py`，函数 `run_task`）

| 项目 | 内容 |
|---|---|
| 输入 | `task: AnalysisTask`（`task_id`、`order`、`kind`、`tool_name`、`params`、`description`）、已从 `DatasetStore` 取出的 `df: pd.DataFrame`、`dataset_id: str` |
| 输出 | `ToolResult`（见 4.7）；若任务带 `chart` 指令则结果里附 `ChartResult` |
| 能做 | ① 按 `task.tool_name` 从**白名单注册表**取出执行函数；② 跑 5 类校验（见第 6.3 节），任一不通过就直接返回 `ok=False` 的 `ToolResult`，**不重试、不猜**；③ 调 `tools/` 里的函数真正计算；④ 把返回值统一成 `ToolResult.data`（`dict[str, Any]`，必须能被 `json.dumps` 序列化）；⑤ 在需要时调用 `agents/charts.py` 出图 |
| **不能做** | ① 不写 pandas 分析代码——**这里一行 `df.groupby` 都不许有**，全部通过工具函数；② 不生成 SQL、不 `exec`/`eval`；③ 不修改 `WorkflowState`；④ 不调 LLM；⑤ 不改 `task` 本身（要改由 Orchestrator 改了再传新的） |
| 调用 LLM | **否**。这是整个设计里最关键的隔离：**执行阶段无模型参与**，所以工具返回的每个数字必然是 pandas 算的 |
| LLM 职责边界 | 不适用。`AnalysisTask` 已经是结构化参数（含 `group_by: list[str]`、`metric: str|None`），模型的工作在规划阶段已经结束 |

### 3.4 Visualization Agent（`app/agents/charts.py`，函数 `render_chart`）

> 名称上算一个 Agent（做"图表生成"这件事），实现上是一个普通函数模块，**不做独立进程、不做独立服务**。

| 项目 | 内容 |
|---|---|
| 输入 | `tool_result: ToolResult`、`chart_type: Literal["bar","line","pie","scatter"]`、`title: str`、`x_field: str`、`y_field: str`、`workflow_id: str` |
| 输出 | `ChartResult`（见 4.8：含 `chart_id`、`filename`、`url`、`chart_type`、`title`、`source_task_id`） |
| 能做 | ① 只从**已经算好的 `ToolResult.data`** 里取数（例如分组聚合的 `rows`），转成绘图用的两个列表；② 用 `matplotlib.use("Agg")`（非交互后端，服务器上没有显示器也能存图）；③ 中文标题要设置 `rcParams["font.sans-serif"]`（Windows 用 `Microsoft YaHei`），否则中文变方框；④ 保存到 `data/charts/{workflow_id}/{chart_id}.png`，`dpi=120`；⑤ 返回可访问的 URL 路径 |
| **不能做** | ① 不做任何聚合计算（"前 5 名"必须由 `top_n` 工具算好，图表只负责画）；② 不读原始 `df`；③ 不接受模型传来的原始数据点（防"编造图表"）；④ 不在文件名里使用用户输入的任何字符；⑤ 不调 LLM |
| 调用 LLM | **否** |
| LLM 职责边界 | 不适用。`chart_type` 由 planner 在 `AnalysisTask.chart` 里给出，只能是 4 种之一，否则校验拒绝 |

### 3.5 Validation & Report Agent（`app/agents/validation.py`）

这个 Agent 内部是**两个纯 Python 函数**，都不调 LLM（除了 `build_report` 里那一次"把数字翻译成中文"的调用）：

#### 3.5.1 `validate(state) -> ValidationResult`（完全不调 LLM）

| 项目 | 内容 |
|---|---|
| 输入 | `state.results: list[ToolResult]`、`state.charts: list[ChartResult]`、`state.report: FinalReport \| None`、`state.profile: DatasetProfile` |
| 输出 | `ValidationResult`（见 4.9） |
| 能做 | ① **数字溯源校验**：把 `FinalReport.sections[].text` 里的所有数字用正则抽出来，逐个在 `ToolResult.data` 的所有数值里找"允许的近似值"（容差规则：绝对值相等、或四舍五入到 0/1/2 位小数后相等、或百分比/小数换算后相等）；找不到就记为 `UNSUPPORTED_NUMBER` 问题；② **任务校验**：`plan.tasks` 中失败的、被跳过的、返回 0 行的，逐条记问题；③ **图表校验**：`ChartResult.source_task_id` 必须能在 `results` 里找到，PNG 文件必须真的存在；④ **画像一致性**：报告里提到的列名必须出现在 `DatasetProfile.columns[].name` 里，否则记 `UNKNOWN_COLUMN`；⑤ 给出 `severity`（`error` / `warning` / `info`）与 `passed`（只要没有 `error` 且所有任务成功，`passed=True`） |
| **不能做** | ① 不修改报告内容（发现问题就报告问题，不偷偷改数字）；② 不重跑工具；③ 不决定 `WorkflowState.status`（那是 Orchestrator 的事，Orchestrator 依据 `ValidationResult.passed` 决定） |
| 调用 LLM | **否** |
| LLM 职责边界 | 不适用 |

#### 3.5.2 `build_report(state, validation) -> FinalReport`（会调用 LLM 一次）

| 项目 | 内容 |
|---|---|
| 输入 | `state.question`、`state.profile`、`state.results`（已裁剪为各任务 Top 20 行，控制 token）、`state.charts`、`validation.issues` |
| 输出 | `FinalReport`（见 4.10） |
| 能做 | ① 让模型**看着 `results` 里已有的数字**写 3–5 段中文结论：数据概况、关键发现、数据质量提示；② 每段必须带 `cites: list[str]`，指向 `task_id`（例如 `["T1","T3"]`）以证明这段话有依据 |
| **不能做** | ① 不许出现 `results` 里没有的数字（会被 `validate` 抓出来）；② 不许执行任何代码；③ 不许改变数值精度之外的东西（不允许"四舍五入到百万"）；④ 不许承诺业务建议之外的事（模型只做解释，不做决策） |
| 调用 LLM | **是，1 次**（文本生成，不带 tools） |
| LLM 职责边界 | 输入是"已经算好的数字表"，输出是"中文解释"。Prompt 里写死三条：只能引用输入中出现的数字；不能新增列名；不能写代码。若模型输出被校验拦下 → 触发 1 次**重写**（把 `validation.issues` 附在 Prompt 里让它改），再不过就保留原文但把 `status` 降级为 `SUCCESS_WITH_WARNINGS` |

---

## 4. 核心数据结构设计（字段级）

约定：`必填` 列里「是」= 无默认值，必须显式给出；「否」= 有默认值。
类型写法沿用 Python 3.12：`str | None`、`list[str]`、`dict[str, Any]`。

**模型的文件归属（B-1 修订，已定死）**：`schemas_dataset.py`（数据集） / `schemas_tool.py`（工具） / `schemas_workflow.py`（工作流）三个文件，**全部只放 Pydantic 模型与枚举，零业务逻辑**。判断标准一句话：**"数据长什么样"进 `schemas_*.py`，"数据放在哪、怎么同步"进 `core/`**。所以 `WorkflowState` 虽然跟运行状态有关，它**仍然定义在 `app/schemas_workflow.py`**，`app/core/registry.py` 里一行模型定义都没有 —— 读者搜 `class WorkflowState` 一定搜得到，这对正在学 FastAPI 的读者很重要。

### 4.0 跨模块统一代码约定（460cf8c 之后强制遵守）

新写的每个模块（`data/`、`tools/`、`llm/`、`agents/`、`core/`、`api/routes_*.py`）都必须遵守这三条，否则会和已完成的第一阶段不一致：

```python
# ① 取配置：工厂函数，函数内现取；测试里用 config.reset_settings_cache()
from app import config

def my_func(...):
    settings = config.get_settings()          # ✅ 正确
    ...

# ❌ 绝对禁止（已从 app/config.py 删除）：
# from app.config import settings
# from app.config import get_settings        # 直接 import 函数也会绕过测试的缓存重置语义
```

```python
# ② 抛业务错误：统一 raise AppError(code, message, detail=None, status_code=None)，
#    由 app/main.py 的 handler 翻译成真实 HTTP 状态 + 统一外壳
#    {"ok":false,"data":null,"error":{code,message,detail}}
from app.errors import AppError
from app.schemas import ErrorCode

raise AppError(ErrorCode.NOT_FOUND, "没有这个数据集")            # → HTTP 404
raise AppError(ErrorCode.UNSUPPORTED_FILE, "只支持 .csv / .xlsx")  # → HTTP 415
raise AppError(ErrorCode.FILE_TOO_LARGE, "文件超过 10 MB 上限")    # → HTTP 413
raise AppError(ErrorCode.BAD_REQUEST, "问题不能为空")             # → HTTP 400

# 状态码默认由 ERROR_STATUS[code] 查表得到（§7.3），**路由里不要手写状态码**；
# status_code 只在极少数"同一个 code 要配不同状态码"时显式传入（第一版没有这种场景）。
# 唯一例外：框架自己抛的 HTTPException / RequestValidationError，由 main.py
# 的同两个 handler 兜住（404 / 405 / 422），业务代码不需要操心。
```

```python
# ③ 禁止绕过统一外壳：任何情况下都不要手写 JSONResponse 直接返回错误，
#    否则前端会遇到"有的响应有 error 对象、有的没有"的第二种形状。
#    ❌ 绝对禁止（A-1 拍板）：
#    return JSONResponse(status_code=415, content={"error": {...}})
#    return ApiResponse[None](ok=False, error=...)   # 也是错的：HTTP 会是 200
```

**唯一需要显式声明状态码的地方**是 `POST /api/analysis`：它返回 **202 Accepted**（"受理了，还没做完"），写成 `@router.post("/analysis", status_code=202)`。其余接口成功一律 200。

`app/data/store.py`、`app/core/registry.py` 这类**模块级单例**（`_frames`、`_workflows` 字典）可以存在，但要和 `config.py` 的处理保持一致：**字典在模块级、访问一律走模块函数**（`store.get_frame(...)`、`registry.get(...)`），不要在别的模块里 `from app.data.store import _frames` 直接操作内部字典。

### 4.0.1 关键决定：`pd.DataFrame` 不放进任何 Pydantic 模型

原因：DataFrame 是可变的大对象，Pydantic 校验会尝试复制/序列化它，既慢又容易在 FastAPI 响应里报 `is not JSON serializable`。所以：

- `DataFrame` 只存在 `app/data/store.py` 的进程内字典里：`DatasetStore._frames: dict[str, pd.DataFrame]`，键是 `dataset_id`；
- Pydantic 模型里只出现 `dataset_id: str`（小字符串），需要 DataFrame 时用 `store.get_frame(dataset_id)` 现取。

### 4.1 `Dataset` / `DatasetSummary`（`app/schemas_dataset.py`，D2）

| 类名 | 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|---|
| `Dataset` | `dataset_id` | `str` | 是 | `uuid4().hex`，全局唯一 |
| | `original_filename` | `str` | 是 | 用户上传时的原始文件名（**只存不用于拼路径**） |
| | `stored_filename` | `str` | 是 | 落盘名：`{dataset_id}.{csv\|xlsx}`，与用户输入完全无关 |
| | `file_ext` | `Literal["csv","xlsx"]` | 是 | 白名单扩展名（`.xls` 一律拒绝，避免老格式坑） |
| | `size_bytes` | `int` | 是 | 文件字节数 |
| | `sha256` | `str` | 是 | 内容哈希，用于"同一个文件重复上传"判断 |
| | `row_count` | `int` | 是 | 行数（不含表头） |
| | `column_count` | `int` | 是 | 列数（含未参与分析的列） |
| | `columns` | `list[str]` | 是 | 列名原样列表（保持顺序） |
| | `dtypes` | `dict[str,str]` | 是 | `{列名: 原始 dtype 字符串}`，例 `{"销售额":"float64"}` |
| | `created_at` | `datetime` | 是 | 上传时间（UTC） |
| | `preview_rows` | `list[dict[str,str]]` | 否 | 前 5 行、值统一转 `str`（避免 `NaN` 不能序列化） |
| `DatasetSummary` | `dataset_id` | `str` | 是 | 给前端列表用的精简版 |
| | `original_filename` | `str` | 是 | |
| | `row_count` | `int` | 是 | |
| | `column_count` | `int` | 是 | |
| | `created_at` | `datetime` | 是 | |
| | `has_profile` | `bool` | 否 | 是否已生成画像（默认 `False`） |

### 4.2 `DatasetProfile`（`app/schemas_dataset.py`，D3）

| 类名 | 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|---|
| `ColumnProfile` | `name` | `str` | 是 | 列名 |
| | `dtype` | `str` | 是 | pandas dtype 字符串 |
| | `semantic_type` | `Literal["numeric","categorical","datetime","boolean","text"]` | 是 | 我们的判断结果（planner 主要看这个） |
| | `null_count` | `int` | 是 | 缺失个数 |
| | `null_ratio` | `float` | 是 | 缺失比例，保留 4 位小数 |
| | `unique_count` | `int` | 是 | 去重后的取值个数 |
| | `min_value` | `float \| str \| None` | 否 | 数值列最小值；日期列放 ISO 字符串 |
| | `max_value` | `float \| str \| None` | 否 | 同上，最大值 |
| | `mean_value` | `float \| None` | 否 | 仅数值列 |
| | `std_value` | `float \| None` | 否 | 仅数值列，`ddof=1` |
| | `median_value` | `float \| None` | 否 | 仅数值列 |
| | `q1_value` | `float \| None` | 否 | 25% 分位 |
| | `q3_value` | `float \| None` | 否 | 75% 分位 |
| | `top_values` | `list[dict[str,Any]]` | 否 | 文本/分类列前 3 个高频值 `[{"value":"华东","count":42}]` |
| | `sample_values` | `list[str]` | 否 | 前 3 个非空样例值 |
| `DatasetProfile` | `dataset_id` | `str` | 是 | |
| | `row_count` | `int` | 是 | 总行数 |
| | `column_count` | `int` | 是 | 总列数 |
| | `columns` | `list[ColumnProfile]` | 是 | 逐列画像 |
| | `duplicate_row_count` | `int` | 是 | 完全重复行数 |
| | `memory_bytes` | `int` | 是 | `df.memory_usage(deep=True).sum()` |
| | `warnings` | `list[str]` | 否 | 面向用户的中文提示，例 `"列 销售额 缺失 12.30%，相关汇总可能偏低"` |
| | `built_at` | `datetime` | 是 | 画像时间 |

### 4.3 `AnalysisTask` 与 `TaskStatus`（`app/schemas_workflow.py`，D3/D4）

`TaskStatus`（`str, Enum`，值要和 JSON 里出现的一模一样）：

| 枚举成员 | 值 | 含义 |
|---|---|---|
| `PENDING` | `"PENDING"` | 已进入计划，还没执行 |
| `RUNNING` | `"RUNNING"` | 正在执行（同一时刻最多一个任务处于该状态） |
| `SUCCESS` | `"SUCCESS"` | 工具执行成功且校验通过 |
| `FAILED` | `"FAILED"` | 校验拒绝或工具抛异常，重试已用尽 |
| `RETRYING` | `"RETRYING"` | 失败后正在重试（`attempt` 递增） |
| `SKIPPED` | `"SKIPPED"` | 因依赖任务失败被跳过 |

`AnalysisTask`：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `task_id` | `str` | 是 | `"T1"`、`"T2"`……由 planner 编号，报告里引用它做溯源 |
| `order` | `int` | 是 | 执行顺序，从 1 开始，连续 |
| `kind` | `Literal["describe","aggregate","compare","trend","missing","correlation"]` | 是 | 语义类别（给人看，也用于默认图表选择） |
| `tool_name` | `str` | 是 | **必须在工具白名单里** |
| `params` | `dict[str, Any]` | 是 | 工具参数（键名必须与工具 schema 完全一致） |
| `description` | `str` | 是 | 一句中文说明，例 `"按 地区 汇总 销售额，取前 5"`，会显示在网页上 |
| `depends_on` | `list[str]` | 否 | 依赖的 `task_id`，默认 `[]`（第一版基本都是空） |
| `chart` | `ChartRequest \| None` | 否 | 该任务要不要出图，默认 `None` |
| `status` | `TaskStatus` | 否 | 默认 `TaskStatus.PENDING` |
| `attempt` | `int` | 否 | 默认 `0`，重试次数 |
| `error` | `str \| None` | 否 | 失败原因（中文） |

`ChartRequest`：`chart_type: Literal["bar","line","pie","scatter"]`（必填）、`x_field: str`（必填）、`y_field: str`（必填）、`title: str`（必填，≤40 字）。

### 4.4 `TaskPlan`（`app/schemas_workflow.py`，D4）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `plan_id` | `str` | 是 | `uuid4().hex[:8]` |
| `question` | `str` | 是 | 用户原问题（原样保存，便于复盘） |
| `dataset_id` | `str` | 是 | 针对哪个数据集 |
| `tasks` | `list[AnalysisTask]` | 是 | 最少 1 个、最多 `max_steps`（默认 6）个 |
| `reasoning` | `str` | 否 | 模型给的一句话规划理由（只做展示，不参与判断） |
| `source` | `Literal["llm","rule_fallback"]` | 是 | 计划来自模型还是规则兜底（**必须落库/落日志**，便于统计模型稳定性） |
| `created_at` | `datetime` | 是 | |

### 4.5 `WorkflowState`（`app/schemas_workflow.py`，D2/D4 —— **不在 `core/registry.py` 里定义**，见 B-1）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `workflow_id` | `str` | 是 | `uuid4().hex`，对外的主键 |
| `dataset_id` | `str` | 是 | 关联数据集 |
| `question` | `str` | 是 | 用户问题，1–500 字 |
| `status` | `WorkflowStatus` | 是 | 见 4.6；初始 `PENDING` |
| `step` | `str` | 是 | 当前阶段名：`profiling`/`planning`/`execution`/`charting`/`reporting`/`validation`/`done` |
| `step_index` | `int` | 否 | 默认 `0`，已完成的阶段数（前端进度条用） |
| `total_steps` | `int` | 否 | 默认 `6` |
| `profile` | `DatasetProfile \| None` | 否 | 画像结果 |
| `plan` | `TaskPlan \| None` | 否 | 分析计划 |
| `results` | `list[ToolResult]` | 否 | 默认 `[]`，按 `task_id` 顺序 |
| `charts` | `list[ChartResult]` | 否 | 默认 `[]` |
| `report` | `FinalReport \| None` | 否 | 最终报告 |
| `has_report` | `bool` | 否 | 默认 `False`。**由 `report is not None` 决定**（见 7.1 第 3 条）；到达终态前若已有报告也必须为 `True`，前端据此决定是否去取报告 |
| `validation` | `ValidationResult \| None` | 否 | 校验结果 |
| `retry_count` | `int` | 否 | 默认 `0`，**整条工作流**的重试总次数 |
| `max_steps` | `int` | 否 | 默认 `6`，计划里最多几个任务 |
| `tool_call_count` | `int` | 否 | 默认 `0`，已执行的工具调用总数（含重试） |
| `error` | `ApiError \| None` | 否 | 顶层失败原因（复用 `app/schemas.py` 的 `ApiError`） |
| `created_at` | `datetime` | 是 | 创建时间 |
| `started_at` | `datetime \| None` | 否 | 后台线程真正开始的时间 |
| `finished_at` | `datetime \| None` | 否 | 结束时间 |
| `heartbeat_at` | `datetime \| None` | 否 | 每推进一步刷新；轮询接口发现"超过 `max_workflow_seconds` 没刷新"就落 `FAILED` |

### 4.6 `WorkflowStatus`（枚举）

`PENDING` / `PROFILING` / `PLANNING` / `EXECUTING` / `CHARTING` / `REPORTING` / `VALIDATING` / `SUCCESS` / `SUCCESS_WITH_WARNINGS` / `FAILED`。

- `step` 是"细粒度当前动作"，`status` 是"粗粒度对外状态"。前端只需要判断 `status` 是否落在终态集合 `{SUCCESS, SUCCESS_WITH_WARNINGS, FAILED}` 里，来决定是否停止轮询。
- `SUCCESS_WITH_WARNINGS` 用来表达"结果可用，但有数据质量问题或某些任务失败"，避免把"不完美"一律说成失败。

### 4.7 `ToolResult`（`app/schemas_tool.py`，D3）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `task_id` | `str` | 是 | 对应 `AnalysisTask.task_id` |
| `tool_name` | `str` | 是 | 实际执行的工具名 |
| `ok` | `bool` | 是 | 工具是否成功 |
| `data` | `dict[str, Any]` | 是 | **纯数字/字符串/列表的字典**，必须能被 `json.dumps` 序列化；每个工具的内部结构见 6.4 |
| `columns` | `list[str]` | 否 | 若 `data` 是表格形式（`{"rows":[...],"columns":[...]}`），冗余到这里方便前端 |
| `row_count` | `int` | 否 | 返回的数据行数（0 行要记 warning） |
| `elapsed_ms` | `int` | 是 | 工具函数实际耗时（毫秒） |
| `error_code` | `str \| None` | 否 | 失败时的机器码：`TOOL_NOT_FOUND` / `BAD_PARAMS` / `COLUMN_NOT_FOUND` / `BAD_OPERATION` / `BAD_RESULT` |
| `error_message` | `str \| None` | 否 | 失败时的中文说明（直接给前端看） |
| `created_at` | `datetime` | 是 | |

### 4.8 `ChartResult`（`app/schemas_tool.py`，D5）

**A-2 拍板的 5 个必备字段**（接口 6 / 接口 8 / §9.1 E 区三处共用同一份定义，不许各写一份）：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `chart_id` | `str` | 是 | `uuid4().hex[:12]`（磁盘文件名也由它决定） |
| `filename` | `str` | 是 | 磁盘文件名，例 `a1b2c3d4e5f6.png`（**只由 `chart_id` 决定**，与用户输入无关） |
| `title` | `str` | 是 | 图标题（中文，已做长度截断 ≤60 字） |
| `chart_type` | `Literal["bar","line","pie","scatter"]` | 是 | 图型 |
| `url` | `str` | 是 | **由后端拼好的**完整相对路径：`/api/workflows/{workflow_id}/charts/{filename}`。**前端不做任何字符串拼接**，直接 `<img src="{ChartResult.url}">` |

**另外 5 个字段保留**（它们不属于 A-2 的 5 个必备字段，但**不能删**，删了会让校验 Agent 失去依据）：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `source_task_id` | `str` | 是 | **数据来源任务**，校验 Agent 靠它验证"图不是凭空画的"（§3 校验③） |
| `x_field` | `str` | 是 | X 轴来源字段名 |
| `y_field` | `str` | 是 | Y 轴来源字段名 |
| `point_count` | `int` | 是 | 图上数据点个数（用于"图里有几根柱子"的校验） |
| `created_at` | `datetime` | 是 | |

> **三处表述的对齐（A-2 要求写死）**：
> 1. **接口 6**（`GET /api/workflows/{id}`）返回 `charts: list[ChartResult]`，即上面这 10 个字段的完整列表；
> 2. **接口 8**（`GET /api/workflows/{id}/charts/{filename}`）**只认 `filename`**（正则 `^[0-9a-f]{12}\.png$` 校验后送回 PNG 二进制），它不知道也不关心 `chart_id`/`url`；
> 3. **§9.1 E 区**（前端）只用 `ChartResult.url` 作为 `<img src>`，用 `ChartResult.title` 做图注，**不拼 URL、不猜文件名**。
>
> 换句话说：`url` 是"给前端用的地址"，`filename` 是"给后端用的钥匙"，两者都在同一个对象里，谁也不用推导谁。

### 4.9 `ValidationResult`（`app/schemas_workflow.py`，D5）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `passed` | `bool` | 是 | 没有 `error` 级问题即 `True` |
| `checked_numbers` | `int` | 是 | 一共检查了多少个数字（给一个"确实查过"的量） |
| `unsupported_numbers` | `list[str]` | 否 | 在工具结果里找不到出处的数字（**这是防编造的核心产出**） |
| `issues` | `list[ValidationIssue]` | 否 | 问题明细，默认 `[]` |
| `checked_at` | `datetime` | 是 | |

`ValidationIssue`：`severity: Literal["error","warning","info"]`（必填）、`code: Literal["UNSUPPORTED_NUMBER","UNKNOWN_COLUMN","EMPTY_RESULT","FAILED_TASK","MISSING_CHART_FILE","SHORT_REPORT"]`（必填）、`message: str`（必填，中文）、`task_id: str | None`（否）。

### 4.10 `FinalReport`（`app/schemas_workflow.py`，D5）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `report_id` | `str` | 是 | `uuid4().hex[:8]` |
| `title` | `str` | 是 | 报告标题，例 `"销售额按地区分析"` |
| `summary` | `str` | 是 | 3–5 句总述（模型生成，数字必须来自 `results`） |
| `sections` | `list[ReportSection]` | 是 | 至少 1 段，通常 3 段 |
| `data_quality_notes` | `list[str]` | 否 | 直接抄 `DatasetProfile.warnings`，不由模型编 |
| `citations` | `list[str]` | 是 | 命中的 `task_id` 列表 |
| `generated_at` | `datetime` | 是 | |
| `llm_used` | `bool` | 是 | 是否真的调了模型（未配 LLM 时为 `False`，前端要显示"规则模式"提示） |

`ReportSection`：`heading: str`（必填）、`text: str`（必填）、`cites: list[str]`（必填，`task_id` 列表）、`table: list[dict[str,Any]] | None`（否，**直接复制** `ToolResult.data["rows"]` 的前 20 行，保证网页表格和工具输出完全一致）。

### 4.11 `QuestionRequest`（`app/schemas_workflow.py`，D4）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `dataset_id` | `str` | 是 | 针对哪个数据集 |
| `question` | `str` | 是 | 中文问题，校验 `min_length=2, max_length=500` |
| `max_steps` | `int` | 否 | 默认 `6`，范围 `1..8`（给"我想换更复杂的计划"留一个旋钮，也防止模型无限加任务） |

### 4.12 模型放在哪些文件里（避免一个文件膨胀成几百行）

| 文件 | 包含的模型 | 负责日 |
|---|---|---|
| `app/schemas.py`（已存在） | `ErrorCode`、`ApiError`、`ApiResponse[T]` | D1 |
| `app/schemas_dataset.py` | `Dataset`、`DatasetSummary`、`ColumnProfile`、`DatasetProfile` | D2 |
| `app/schemas_tool.py` | `ToolResult`、`ChartResult`、`ToolSpec` | D3 |
| `app/schemas_workflow.py` | `TaskStatus`、`WorkflowStatus`、`AnalysisTask`、`ChartRequest`、`TaskPlan`、`WorkflowState`、`ValidationIssue`、`ValidationResult`、`ReportSection`、`FinalReport`、`QuestionRequest` | D3–D5 |

**不动 `app/schemas.py` 已有的三个模型**——因为它们被 `routes_health.py` 和 `tests/test_health.py` 依赖，改了会破坏第一天的验收。

---

## 5. 工作流状态机

### 5.1 工作流级状态迁移（`WorkflowState.status`）

```
                  POST /api/analysis （参数校验通过）
                              │
                              ▼
                         ┌─────────┐
                         │ PENDING │  已入注册表，后台任务已排队，HTTP 已返回 workflow_id
                         └────┬────┘
                              │ 后台线程启动，取到 DataFrame
                              ▼
                        ┌───────────┐
                        │ PROFILING │ ── 画像异常（文件读不出来）──┐
                        └─────┬─────┘                              │
                              │ 画像完成（含 warnings）             │
                              ▼                                    │
                        ┌──────────┐                               │
                        │ PLANNING │ ── LLM 超时/JSON 解析失败 ──┐   │
                        └────┬─────┘    → 规则兜底计划           │   │
                             │ 计划就绪                            │   │
                             ▼                                     │   │
                        ┌───────────┐                              │   │
              ┌────────▶│ EXECUTING │ ◀── 单任务重试（≤2 次）───┐    │   │
              │         └─────┬─────┘                          │    │   │
              │               │ 所有任务有终态（SUCCESS/FAILED） │    │   │
              │               ▼                                │    │   │
              │         ┌──────────┐                           │    │   │
              │         │ CHARTING │ ── 出图失败 ── 记 warning ──┘    │   │
              │         └────┬─────┘  （不阻断流程）                  │   │
              │              ▼                                       │   │
              │        ┌───────────┐                                 │   │
              │        │ REPORTING │ ── LLM 失败 → 模板化文字报告     │   │
              │        └─────┬─────┘   （llm_used=False）             │   │
              │              ▼                                       │   │
              │        ┌────────────┐                                │   │
              │        │ VALIDATING │ ── 有 error 且没重写过 → 带问题重写 1 次
              │        └─────┬──────┘
              │              │
              │      ┌───────┴────────┐
              │      ▼                ▼
              │ ┌──────────┐   ┌───────────────────────┐
              │ │ SUCCESS  │   │ SUCCESS_WITH_WARNINGS │
              │ └──────────┘   └───────────────────────┘
              │
              └──────── 任何阶段抛未捕获异常 / 超过 max_workflow_seconds / 工具调用超总上限
                                    │
                                    ▼
                               ┌────────┐
                               │ FAILED │  error 写入具体原因（中文 + code）
                               └────────┘
```

终态集合：`{SUCCESS, SUCCESS_WITH_WARNINGS, FAILED}`。到达终态时 Orchestrator 必须做三件事：写 `finished_at`、写最后一次 `heartbeat_at`、把 `step` 置为 `"done"`。

### 5.2 单任务状态迁移（`AnalysisTask.status`）

```
PENDING ──▶ RUNNING ──▶ SUCCESS
                │
                ├─▶ FAILED（attempt 已达上限 / 校验拒绝且不可重试）
                └─▶ RETRYING ──▶ RUNNING ──▶ SUCCESS | FAILED
PENDING ──▶ SKIPPED（depends_on 里有 FAILED 的任务）
```

### 5.3 重试规则（写死，不做"智能重试"）

| 情况 | 是否重试 | 最多几次 | 说明 |
|---|---|---|---|
| 工具抛 `KeyError`（列不存在） | **不重试** | 0 | 重试不会让列出现，直接 `COLUMN_NOT_FOUND` |
| 参数校验失败（第 6.3 节 5 类校验） | **不重试** | 0 | 参数是模型给的，重试同一个参数没意义 |
| 工具抛 `TypeError`/`ValueError`（例：对文本列求均值） | 是，且**换策略**：跳过该任务 | 1 | 重试时把该任务标记 `FAILED` 并继续后面的任务 |
| LLM 调用超时 / 连接错误（HTTP 429、5xx） | 是 | `settings.llm_max_retries`（默认 2，不含首次） | 每次之间 `time.sleep(1.5 * attempt)`，退避 |
| LLM 返回的 `tool_calls` 参数不是合法 JSON | 是 | 2 | 第 1 次重试时把"你的 JSON 不合法"附回 Prompt |
| 报告校验发现编造数字 | 是，**重写报告** | 1 | 附上 `ValidationResult.issues` 让模型改；再不过 → `SUCCESS_WITH_WARNINGS` |
| 整条工作流的工具调用总数 | — | `TOOL_CALL_LIMIT = 20` | 超过就 `FAILED`（防模型/重试组合成死循环） |
| 整条工作流总时长 | — | `max_workflow_seconds = 180` | 轮询接口按 `heartbeat_at` 判定超时并落 `FAILED` |

`WorkflowState.retry_count` 记录**所有**重试次数之和，上限 6；达到上限后不再重试任何东西，直接进入收尾流程。

### 5.4 失败后的整体收尾（"部分成功"怎么表达）

1. **单个任务失败**：不中断工作流。该任务的 `ToolResult.ok=False`，继续执行其他任务；`SKIPPED` 只影响显式声明 `depends_on` 的任务。
2. **出图失败**：记 `ValidationIssue(code="MISSING_CHART_FILE", severity="warning")`，图表列表里不放它，流程继续。
3. **报告生成失败**（LLM 不可用）：用**模板**生成报告——把每个 `ToolResult` 的 `description` 和关键数字拼成段落，`llm_used=False`，并在 `summary` 里明确写"本次未使用大模型，以下结论为工具结果的直接汇总"。这样即使没配 API Key，第 4 天也能演示完整链路（这一条对七天排期很关键）。
4. **顶层异常**：`background.py` 里 `try/except BaseException` 兜住，写 `status=FAILED`、`error=ApiError(code=INTERNAL_ERROR, message="分析过程中出现未预期错误", detail=str(exc))`。**绝不允许线程静默死掉导致前端永远转圈**。
5. **有任务失败但仍然出报告**：`status = SUCCESS_WITH_WARNINGS`，报告首段写明"以下 N 项分析未能完成：……"。

---

## 6. 工具注册表设计

### 6.1 一个工具长什么样（`app/tools/registry.py` 的 `ToolSpec`）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `name` | `str` | 是 | 工具唯一名，`snake_case`，例 `"group_aggregate"` |
| `description` | `str` | 是 | 给大模型看的中文说明（会进 `to_openai_tools()`） |
| `params_schema` | `dict[str, Any]` | 是 | JSON Schema 风格：`{"type":"object","properties":{...},"required":[...]}` |
| `func` | `Callable[[pd.DataFrame, dict[str, Any]], dict[str, Any]]` | 是 | 执行函数。**统一签名**：吃 DataFrame + 参数，吐可 JSON 序列化的字典 |
| `category` | `Literal["describe","aggregate","compare","trend","missing","correlation"]` | 是 | 对应 `AnalysisTask.kind`，用于规划时的候选筛选 |
| `needs_chart` | `bool` | 否 | 默认 `False`；`True` 表示结果天然适合出图，planner 可优先给它配 `ChartRequest` |

注册表对外只有 5 个函数（都是普通函数，不用元类、不用装饰器魔法）：

```python
def register(spec: ToolSpec) -> None: ...            # 启动时把 7 个工具登记进来
def get(name: str) -> ToolSpec | None: ...            # 取不到返回 None，不抛异常
def list_names() -> list[str]: ...                    # 白名单，供校验与测试用
def list_specs() -> list[ToolSpec]: ...               # 给 planner 拼 tools 参数
def to_openai_tools() -> list[dict]: ...              # 转成 OpenAI tools 格式：{"type":"function","function":{...}}
```

### 6.2 「大模型只能从白名单里选工具」是怎么保证的

1. `planner.py` 调模型时，`tools=` 参数**只**来自 `registry.to_openai_tools()`，模型在物理上只看得见这 7 个函数的签名。
2. 模型返回的 `AnalysisTask.tool_name` 必须 `registry.get(name) is not None`，否则**丢弃该任务**并记 warning（不是猜一个相似的工具名去凑）。
3. `analysis.py` 执行时**再查一次**注册表（`spec = registry.get(task.tool_name)`）。**绝不使用 `getattr(module, task.tool_name)` 或 `eval` 来动态取函数**——这是全项目唯一允许的"动态调用"，且白名单是硬编码的。
4. 测试里有一条：构造一个 `tool_name="os.system"` 的 `AnalysisTask`，必须被拒绝且返回 `error_code="TOOL_NOT_FOUND"`。

### 6.3 后端必须做的 5 类校验（按顺序，前面不过就不做后面）

| 序号 | 校验 | 检查内容 | 不通过时 |
|---|---|---|---|
| 1 | **工具名存在** | `registry.get(name) is not None` | `error_code="TOOL_NOT_FOUND"`，不重试 |
| 2 | **参数合法** | 用 `params_schema` 逐项检查：必填键是否都在；类型对不对（`str`/`int`/`float`/`list[str]`/枚举值）；数值范围（例 `n` 在 `1..50`）；枚举白名单（例 `agg` ∈ `{sum,mean,count,min,max,median}`） | `error_code="BAD_PARAMS"`，`error_message` 指出具体哪个键错了 |
| 3 | **字段存在** | 每个字段参数（`group_by`、`metric`、`date_column`）都必须在 `df.columns` 里，且 `metric` 必须是数值列（除非 `agg="count"`） | `error_code="COLUMN_NOT_FOUND"`，消息里附上**真实列名列表**（便于用户/前端看到，也便于下一次规划修正） |
| 4 | **操作合法** | `sort_order` ∈ `{asc,desc}`；`ascending`/`n` 类型与范围正确；`group_by` 与 `metric` 不重复；聚合方式对所选列类型有意义（例：对文本列用 `mean` → 拒绝） | `error_code="BAD_OPERATION"` |
| 5 | **返回结构符合预期** | 工具执行完，用 `ToolResult` 做一次 Pydantic 校验；再检查 `data` 里没有 `NaN`/`inf`（`math.isfinite`）、没有 `numpy` 标量类型（统一 `float()`/`int()` 转换）、能 `json.dumps(data, ensure_ascii=False)` 成功（`allow_nan=False`） | `error_code="BAD_RESULT"`，这是**开发期 bug 的信号**，日志里要打完整堆栈 |

校验 1–4 在 `app/tools/validate.py`（或 `registry.py` 内的 `validate_call(tool_name, params, df)` 函数）里统一实现；校验 5 在 `analysis.py` 收尾处实现。**同一个校验函数被 API 层和 Agent 层共用**，避免"两处各写一份、规则慢慢不一致"。

### 6.4 第一版要实现的 7 个工具

| # | `name` | 用途 | 参数（`params_schema`） | 返回 `ToolResult.data` 结构 |
|---|---|---|---|---|
| 1 | `describe_dataset` | 描述性统计：行数、列数、每个数值列的五数概括 | `columns: list[str]`（选填，默认全部数值列） | `{"row_count":1200,"columns":["销售额"],"stats":[{"column":"销售额","count":1180,"mean":523.4,"std":88.2,"min":12.0,"q1":470.1,"median":520.0,"q3":577.3,"max":990.0}]}` |
| 2 | `group_aggregate` | 分组聚合：按若干列分组，对指标列做聚合 | `group_by: list[str]`（必填，1–3 个）、`metric: str\|None`、`agg: Literal["sum","mean","count","min","max","median"]`（必填）、`sort_by: str\|None`、`sort_order: Literal["asc","desc"]`（默认 `desc`）、`limit: int\|None`（1–100） | `{"columns":["地区","销售额_sum"],"rows":[{"地区":"华东","销售额_sum":120345.5}],"row_count":4,"group_count":4}` |
| 3 | `top_n` | 排序取前 N：按某列排序取前/后 N 行（可带分组） | `by: str`（必填）、`n: int`（必填，1–50）、`ascending: bool`（默认 `False`）、`group_by: list[str]\|None`、`columns: list[str]\|None` | `{"columns":["商品","销售额"],"rows":[{"商品":"A","销售额":9999.0}],"row_count":5}` |
| 4 | `missing_values` | 缺失值统计：每列缺失个数/比例，以及完全重复行数 | `columns: list[str]\|None`（默认全部列） | `{"row_count":1200,"duplicate_row_count":8,"columns":[{"column":"销售额","null_count":20,"null_ratio":0.0167}]}` |
| 5 | `compare_groups` | 组间对比：对同一指标在两组（或某分类的各组）之间比较均值/总和差异 | `group_by: str`（必填，单列）、`metric: str`（必填，数值列）、`agg: Literal["sum","mean","count"]`（默认 `mean`）、`top_k: int`（默认 10，1–30） | `{"metric":"客单价","columns":["地区","客单价_mean","差值"],"rows":[{"地区":"华东","客单价_mean":120.5,"差值":15.2}],"row_count":6}` |
| 6 | `correlation` | 相关系数：数值列两两之间的 Pearson 相关系数矩阵 | `columns: list[str]\|None`（默认全部数值列，至少 2 列） | `{"columns":["销售额","利润"],"matrix":[[1.0,0.62],[0.62,1.0]],"pairs":[{"x":"销售额","y":"利润","r":0.62}]}` |
| 7 | `time_series_summary` | 时间序列汇总：按时间粒度（日/周/月）汇总指标，可选算环比 | `date_column: str`（必填）、`metric: str`（必填）、`freq: Literal["D","W","M"]`（默认 `"M"`）、`agg: Literal["sum","mean","count"]`（默认 `"sum"`）、`include_growth: bool`（默认 `False`） | `{"columns":["period","销售额_sum","环比"],"rows":[{"period":"2025-01","销售额_sum":1000.0,"环比":null}],"row_count":12}` |

**统一执行函数签名**（所有 7 个都长这样，便于注册表统一调度）：

```python
def group_aggregate(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """参数已由 validate_call 校验过；这里只做计算，不做类型检查（重复检查会散掉规则）。"""
```

**明确不做（第一版）**：任意 SQL 查询、任意 Python 表达式、`df.query()` 透传用户字符串、回归/预测/聚类、多数据集 join。理由：这些要么需要"让模型写代码"（违反铁律），要么校验成本高于收益。第七天之后若要做，必须**先写进本文档第 6.3 节**再实现。

**pandas 3.0 必须注意的写法**（3.0 是 2025 年的大版本，网上教程大多是 1.x，直接抄会踩坑）：
- 链式赋值彻底失效：写 `df["新列"] = ...` 或 `df = df.assign(...)`，绝不用 `df[df.销售额>100]["等级"] = "高"`。
- `groupby` 用关键字参数收口：`df.groupby(by=["地区"], as_index=False, dropna=False, observed=True)`。分类列分组默认不显示空组合，`observed=False` 才能看到全部分类。
- 不要再用 `df.append()`（2.0 已删除）、`inplace=True`（3.0 起大量方法不再支持，统一改成接收返回值）。
- 时间列先显式转换：`pd.to_datetime(s, errors="coerce")`，再 `.dt.to_period("M")`；不要依赖自动推断。
- 缺失值判断用 `s.isna()`；`pd.NA` 与 `np.nan` 混用会出现"看起来相等其实是 NA"的坑，工具返回值统一走 `_to_jsonable()` 辅助函数做清洗。

---

## 7. API 设计（8 个必需接口）

统一约定：
- 全部返回 `ApiResponse[T]` 外壳（已存在于 `app/schemas.py`）：成功 `{"ok":true,"data":{...},"error":null}`，失败 `{"ok":false,"data":null,"error":{"code","message","detail"}}`。
- **HTTP 状态码表达 HTTP 语义，并且必须是真的；`error.code` 表达业务语义。两者不冲突，同时给。**（A-1 拍板，推翻 v1.0 的"业务错误一律 200"——原方案的理由不成立，代价却真实：见下方"为什么"）
- **前端只有一套解析逻辑**：用 `fetch`（不是 axios），**4xx/5xx 不会抛异常**，`.json()` 照样解析出 `{"ok":false,...}`。所以前端的写法永远是这两行，不用分支：
  ```js
  const resp = await fetch(url, {...});
  const body = await resp.json();               // 4xx/5xx 也能正常解析
  if (!resp.ok || !body.ok) { showError(body.error); return; }   // 只有这一套
  ```
- **框架抛的异常也已经统一成同一个外壳**（`app/main.py` 的两个 exception handler，`tests/test_error_format.py` 守护）——这条是 460cf8c 落实的，本节所有接口都建立在它之上。

#### 7.0 HTTP 状态码 → 错误码映射表（**逐行照此实现，不许自创**）

| 情况 | HTTP 状态 | `error.code` |
|---|---|---|
| 请求成功（同步完成） | 200 | — |
| 请求已受理、后台执行（`POST /api/analysis`） | **202** | — |
| 参数格式/类型不对（`RequestValidationError`） | 422 | `BAD_REQUEST` |
| 业务参数非法（问题为空、问题过长） | 400 | `BAD_REQUEST` |
| 资源不存在（路径、`dataset_id`、`workflow_id`） | 404 | `NOT_FOUND` |
| 方法不对 | 405 | `BAD_REQUEST` |
| 扩展名不在白名单 / 文件伪装 | 415 | `UNSUPPORTED_FILE` |
| 文件超过 `max_upload_bytes` | 413 | `FILE_TOO_LARGE` |
| 文件内容解析失败（坏编码、空文件、只有表头、超宽） | 400 | `BAD_REQUEST` |
| 外部依赖不可用（模型未配置且无法兜底） | 503 | `BAD_REQUEST` |
| 工作流超时 | **200** | `WORKFLOW_TIMEOUT` |
| 服务器内部未预期错误 | 500 | `INTERNAL_ERROR` |

**这张表里有三行需要解释**：
- **413 / 415 是 HTTP 本来就有的语义**：413 Payload Too Large、415 Unsupported Media Type。"用户传了一个 70 MB 的文件被拒绝"用 200 回答等于向 HTTP 层撒谎 —— 打开 F12 看到一片 200，排查时必须点开每个响应体才知道发生了什么；413 一眼就知道。
- **`WORKFLOW_TIMEOUT` 走 200**：请求本身完全成功（202 已受理、轮询到了终态），"超时"是**这次分析的结果**，不是请求失败；它属于业务语义，所以放 `error.code`。
- **503 的含义是"服务现在做不了、以后可能行"**。第一版只有一处：模型未配置、连规则兜底也用不了，所以 `POST /api/analysis` 无法受理。实现方式是 `raise AppError(ErrorCode.BAD_REQUEST, "...", status_code=503)`（`status_code` 参数的唯一用途，见 §7.3）。前端此时只需显示 `message` + "重试"按钮，不要提示"请检查你的输入"；**不要按 `code` 猜提示语，按 HTTP 状态码分支**（503 → 稍后重试，400 → 检查输入）。

**落到代码的约束（禁止绕过统一外壳）**：
1. 所有错误响应体形状**永远**是 `{"ok":false,"data":null,"error":{"code","message","detail"}}`，只有 HTTP 状态码不同。
2. 业务错误统一 `raise AppError(code, message, detail=None, status_code=None)`（定义在 `app/errors.py`），由 `app/main.py` 的 handler 翻译成 `ApiResponse` + 状态码；**状态码默认查 `ERROR_STATUS` 表（§7.3），路由里不要手写状态码**。
3. **禁止**在路由里手写 `return JSONResponse(status_code=..., content={"error":...})`，也**禁止**用 `ApiResponse(ok=False, ...)` 当返回体来表达失败（那样 HTTP 会是 200）。只有这一种例外：`WORKFLOW_TIMEOUT`（表里指定为 200）。
4. `POST /api/analysis` 的 202 用装饰器声明：`@router.post("/analysis", status_code=202)`。

| # | 接口 | Method + Path | 请求体 | 成功响应 `data` | 主要错误码（**含 HTTP 状态码**） |
|---|---|---|---|---|---|
| 1 | 健康检查 | `GET /api/health` | 无 | `HealthData{status,app_name,version,uptime_seconds,llm_configured,upload_dir_ready}`（**已实现**，含 460cf8c 新增的 `upload_dir_ready`） | 无（`llm_configured=false`、`upload_dir_ready=false` 都不算错误，属于"能跑但要提示"的状态） |
| 2 | 上传数据集 | `POST /api/datasets/upload` | `multipart/form-data`，字段名 `file` | **200** + `DatasetSummary{dataset_id,original_filename,row_count,column_count,created_at,has_profile}` | **415** `UNSUPPORTED_FILE`（扩展名不在白名单/文件伪装）、**413** `FILE_TOO_LARGE`（> `max_upload_bytes`）、**400** `BAD_REQUEST`（空文件/坏编码/只有表头/超过行数列数上限） |
| 3 | 查询数据集 | `GET /api/datasets/{dataset_id}` | 无 | **200** + `Dataset`（含 `columns`、`dtypes`、`preview_rows`） | **404** `NOT_FOUND` |
| 4 | 生成/读取画像 | `GET /api/datasets/{dataset_id}/profile` | 无 | **200** + `DatasetProfile`；首次调用时**同步**生成（≤2 秒）并缓存进 `DatasetSummary.has_profile` | **404** `NOT_FOUND`、**500** `INTERNAL_ERROR` |
| 5 | 提交分析 | `POST /api/analysis` | `QuestionRequest{dataset_id,question,max_steps?}` | **202** + `{workflow_id,status:"PENDING",dataset_id,created_at}`，**立即返回，不等分析** | **404** `NOT_FOUND`（数据集不存在）、**400** `BAD_REQUEST`（问题为空/过长）、**503** `BAD_REQUEST`（模型未配置且无法兜底，`detail` 写"请在 .env 配置 LLM_*"） |
| 6 | 查询工作流状态 | `GET /api/workflows/{workflow_id}` | 无 | **200** + `{workflow_id,status,step,step_index,total_steps,question,dataset_id,tasks:[{task_id,description,status,error}],charts:[ChartResult],error,created_at,finished_at,has_report}` —— **不含 `report`**（报告可能很大，单独接口取） | **404** `NOT_FOUND`、**200** `WORKFLOW_TIMEOUT`（轮询时判定超时并落 `FAILED`，此时 `error.code=WORKFLOW_TIMEOUT`）、**500** `INTERNAL_ERROR`。注意：**工作流自身 `FAILED`（含超时）仍然是 HTTP 200** —— 查询请求成功了，失败的是被查询的工作流 |
| 7 | 获取报告 | `GET /api/workflows/{workflow_id}/report` | 无 | **200** + `{report:FinalReport, validation:ValidationResult, results:[ToolResult], llm_used:bool}` | **404** `NOT_FOUND`（没有这个工作流）；报告还没生成 → **400** `BAD_REQUEST`（`message="报告尚未生成"`，`detail` 带当前 `status`） |
| 8 | 获取图表 | `GET /api/workflows/{workflow_id}/charts/{filename}` | 无 | **200** + PNG 二进制（`Content-Type: image/png`） | **404** `NOT_FOUND`（文件不存在）、**400** `BAD_REQUEST`（文件名含 `/`、`\`、`..` 或不是 12 位 hex + `.png` —— 这是"非法输入"，不是"资源不存在"，所以是 400 不是 404） |

> **读表提醒**：`NOT_FOUND` 在接口 3/5/6/7 都是 **404**，`UNSUPPORTED_FILE` 是 **415**，`FILE_TOO_LARGE` 是 **413** —— 同一个错误码在不同接口里的状态码是**一致的**，因为它由 `app/errors.py` 的 `ERROR_STATUS` 映射表统一决定，不在路由里逐处指定。这就是 A-1 要解决的"同一个 NOT_FOUND 有时 404 有时 200"的根源。

### 7.1 我在你给的 8 个接口之外做的两处调整（已于第 14 节拍板通过）

1. **不加** `GET /api/datasets`（数据集列表）。理由：单用户、单会话的展示项目，前端只需要"最近上传的那一个"，加列表接口会让"数据集属于谁"变成一个新问题（需要 cookie/会话），而第一版没有登录体系。**若第 6 天演示需要一次性对比两个文件，再加这个接口，用进程内注册表直接 `list()` 即可，成本 20 行。**
2. **`GET /api/workflows/{id}` 不含 `report` / `results`**，由接口 7 单独提供。理由：`results` 里的表格可能有几百行，1 秒一次轮询会白白传几十 KB；状态接口保持小（<2 KB），轮询才便宜。
3. **接口 6 必须带 `has_report: bool`**（第 14 节决策 2 的补充要求）。理由：报告走独立接口，前端如果只拿到 `status`，要么靠猜（"看到 SUCCESS 就去请求报告"——但 `SUCCESS_WITH_WARNINGS`、超时 `FAILED` 也都可能有报告），要么多发一次很可能失败的请求。**规则：`has_report = (report is not None)`**，与接口 7 能否成功一一对应，前端逻辑退化成一行：`if (data.has_report) fetchReport()`。

```python
# app/api/routes_workflows.py 的取状态实现（关键 4 行）
from app.errors import AppError
from app.schemas import ErrorCode

state = registry.get(workflow_id)          # ① 内存态是唯一事实来源
if state is None:
    raise AppError(ErrorCode.NOT_FOUND, "没有这个工作流")   # ② → HTTP 404，由 main.py handler 翻译
data = WorkflowStatusData.from_state(state)  # ③ from_state 里：has_report = state.report is not None
# ④ 绝不在这个接口里读 SQLite，也不手写 JSONResponse —— 见 8.3.1 与 7.0 约束
```

接口 7 的 `llm_used` 字段与 `FinalReport.llm_used` 同源（第 4.10 节），前端据此在报告页显示"本次由模型生成"或"本次未使用大模型（模板汇总）"的横幅——这是第 14 节决策 1 要求的可见性。

### 7.1.1 §9.1 E 区（前端图表区）的对齐写法

```js
// 唯一正确写法：url 直接用后端给的字段，不拼字符串
for (const c of state.charts) {
  img.src = c.url;             // 后端已拼成 /api/workflows/{workflow_id}/charts/{filename}
  img.alt = c.title;
}
```

理由（A-2 指出的三处不一致）：如果前端自己拼 `/charts/` + `c.filename`，一旦接口 8 的路径前缀调整（例如挂到 `/api/v1/`），前端会静默产出 404 的图片链接，而且**测试很难覆盖**（HTML 属性不会被 pytest 检查）。把拼接责任放在后端一处，前端只做渲染。

### 7.2 错误码补充（写在 `app/schemas.py` 的 `ErrorCode` 里，D2）

现在是 5 个：`BAD_REQUEST / NOT_FOUND / UNSUPPORTED_FILE / FILE_TOO_LARGE / INTERNAL_ERROR`。建议**只加 1 个**：`WORKFLOW_TIMEOUT`（工作流超时专用，前端可以提示"数据太大或模型太慢，请缩小问题范围"）。其余（工具类错误）留在 `ToolResult.error_code` 这一层，不上升到 HTTP 错误码——因为它们不是"请求失败"，而是"任务失败"，工作流仍然能返回 `SUCCESS_WITH_WARNINGS`。

### 7.3 `ERROR_STATUS` 映射表（`app/errors.py`，D2 —— A-1 的落地点）

```python
# app/errors.py —— 与仓库里已实现的版本逐行一致（D2 已落地，勿照旧稿重写）
DEFAULT_MESSAGE: dict[ErrorCode, str] = {          # message 兜底，避免出现空 message
    ErrorCode.BAD_REQUEST: "请求不合法",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.FILE_TOO_LARGE: "文件太大",
    ErrorCode.UNSUPPORTED_FILE: "文件类型不支持",
    ErrorCode.WORKFLOW_TIMEOUT: "分析超时",
    ErrorCode.INTERNAL_ERROR: "服务器内部错误",
}

class AppError(Exception):
    """业务错误。由 app/main.py 的 handler 翻译成统一响应外壳。"""
    def __init__(self, code: ErrorCode, message: str | None = None,
                 detail: str | None = None, status_code: int | None = None) -> None:
        self.code = code
        self.message = message or DEFAULT_MESSAGE.get(code, "请求失败")
        self.detail = detail          # 清洗不在这里做，见下方 ①
        self.status_code = status_code or ERROR_STATUS.get(code, 500)
        super().__init__(self.message)

# 唯一事实来源：错误码 → HTTP 状态。见 §7.0 的表，逐行对应。
ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.BAD_REQUEST:      400,
    ErrorCode.NOT_FOUND:        404,
    ErrorCode.UNSUPPORTED_FILE: 415,
    ErrorCode.FILE_TOO_LARGE:   413,
    ErrorCode.INTERNAL_ERROR:   500,
    ErrorCode.WORKFLOW_TIMEOUT: 200,   # 请求成功、业务结果超时（§7.0 最后三行的例外）
}
```

**三条实现细节（照抄代码时会用到）**：
1. **清洗发生在 handler，不在 `AppError.__init__` 里**：`app/main.py` 的 `app_error_handler` 写的是 `detail=sanitize_detail(exc.detail) if exc.detail else request.url.path`。好处是"清洗"只有一道关口，4 个 handler（`AppError` / `HTTPException` / `RequestValidationError` / 兜底 `Exception`）用的是同一个函数，不会出现"某个路径忘了洗"。
2. **不要手写 422 的映射**：`ERROR_STATUS` 里 `BAD_REQUEST → 400`，而框架的 `RequestValidationError` 由 `app/main.py` 显式返回 **422**（与 §7.0 表一致）。这就是"同一个 `error.code=BAD_REQUEST` 可以配两个状态码"的现象，属于 A-1 拍板的刻意设计，不是 bug。
3. **503 用 `status_code` 显式覆盖**（唯一需要它的地方）：模型未配置且无法兜底时 `raise AppError(ErrorCode.BAD_REQUEST, "...", status_code=503)` —— 这正是第 4 个参数存在的理由，语义可再优化的意见记在 §14.2 末尾。

**为什么把映射表放在 `errors.py` 而不是每个路由里**：状态码一旦分散，就会重现 A-1 那个问题——同一个 `NOT_FOUND` 在一个接口是 404、在另一个接口因为"顺手返回了 ApiResponse"变成 200。集中一张表之后，`tests/test_error_format.py` 只要多测几条就能锁住全部接口的一致性。

---

## 8. 耗时任务执行机制

### 8.1 为什么必须先想这件事（真实耗时估算）

| 步骤 | 典型耗时 | 慢在哪 |
|---|---|---|
| 上传 + 读文件（1 万行 CSV） | 0.3–2 秒 | 磁盘 + pandas 解析 |
| 画像 | 0.2–3 秒 | 逐列统计 + 唯一值计数 |
| 规划（调 LLM） | 3–20 秒 | 网络 + 模型推理 |
| 执行 5 个工具 | 0.1–2 秒 | pandas 计算 |
| 出图 3 张 | 1–3 秒 | matplotlib 首次 import 就要 1 秒以上 |
| 报告（调 LLM） | 5–30 秒 | 模型输出 800 字 |
| **合计** | **10–60 秒** | |

如果 `POST /api/analysis` 一直挂着等结果：浏览器转圈一分钟、公司/学校网络中间层会在 30 秒超时、uvicorn 也会把 `--timeout-keep-alive` 拖满。所以**必须"立刻返回 ID + 后台干活 + 前端轮询"**。

### 8.2 第一版机制（第 4 天）：FastAPI `BackgroundTasks` + 进程内字典 + 前端轮询

一共三个零件，都是基础 Python：

#### 8.2.1 锁保护什么、不保护什么（B-2 修订，**别在 `state.status = ...` 外面加锁**）

```python
# 零件 1：进程内工作流注册表（app/core/registry.py）
_workflows: dict[str, WorkflowState] = {}      # 键：workflow_id
_lock = threading.Lock()                        # 只保护 _workflows 这个 dict 本身

def create(state: WorkflowState) -> None: ...   # with _lock: _workflows[state.workflow_id] = state
def get(workflow_id: str) -> WorkflowState | None: ...   # 返回**同一个对象引用**，便于后台线程原地更新
def touch(workflow_id: str) -> None: ...        # 更新 heartbeat_at，供超时检测
```

**两句话说清边界（重要，避免"到处加锁"或"以为加了锁"两种错）**：

1. **`_lock` 保护的只有 `registry` 那个 dict 的"读-改-写"**：`create` 写入新键、`get` 取引用、以及将来可能有的"遍历全部工作流"。因为这些操作可能来自不同线程（FastAPI 把同步路由丢进线程池）。
2. **`WorkflowState` 内部字段的写入不需要加锁**：`run_workflow` 只在**一个**后台线程里顺序执行，而且它拿到的是**同一个对象引用**（不是副本），所以"后台线程写 `state.status`、轮询线程读 `state.status`"是安全的——Python 的单个属性赋值是原子的，我们只需要保证**写入顺序**（见 §13 风险 5：先写结果字段、最后写 `status`，`status` 是唯一的完成信号）。

**禁止**：① 为了"保险"给每个 `state.xxx = ...` 都包 `with _lock:` —— 锁的范围会扩散到整个 Orchestrator，反而制造死锁风险；② 在 `get()` 里返回 `state.model_copy()` 或 `deepcopy` —— 那样后台线程更新的是副本，轮询永远看不到进度。

#### 8.2.2 出图必须"写文件"和"登记"在同一个地方连续完成（B-2 补漏）

**写死规则**：
- `agents/charts.py` 的 `render_chart(...)` **只负责**：算绘图数据 → 存 PNG → 返回 `ChartResult` 对象。**它绝不自己去改 `state`**（不 import registry、不碰 `state.charts`）。
- **由 `agents/analysis.py`（任务执行者）把返回的 `ChartResult` 追加进 `state.charts`**，紧跟在调用 `render_chart()` 之后、同一个函数体内连续两行：
  ```python
  # app/agents/analysis.py
  chart = render_chart(df, task.chart, workflow_id=state.workflow_id, chart_id=...)  # 写文件
  state.charts.append(chart)                                                        # 立刻登记
  ```
- **为什么必须写死**：如果 `charts.py` 只把 PNG 写进 `data/charts/{workflow_id}/` 而没登记到 `state.charts`，接口 8 按 `chart_id` 校验时会 404（前端看到裂图），**而磁盘上却留着一堆孤儿 PNG**——排查时"文件明明在、接口却说没有"是最费时间的一类问题。让"写"与"登记"在一个地方连续发生，这个状态就不可能不一致。
- 出图失败时**不要登记半个对象**：`render_chart` 抛异常 → `analysis.py` 捕获 → 记 `ValidationIssue(code="MISSING_CHART_FILE", severity="warning")` → 继续后面的任务（见 5.4 第 2 条）。

#### 8.2.3 `DatasetStore` 与工作流 registry 同属"进程内状态"（B-2 补充）

`app/data/store.py` 的 `_frames: dict[str, pd.DataFrame]` + `_summaries: dict[str, DatasetSummary]` **也是进程内字典**，所以 §8.2 末尾"只能单进程运行"的边界**同样适用于数据集**：在 `uvicorn --workers 4` 下，上传请求可能落在 A 进程、`GET /api/datasets/{id}` 落在 B 进程 → B 的字典里没有这个 `dataset_id` → 404，而磁盘上的文件明明都在。

**这条要和工作流的限制一起写进 README 的"已知限制"**：① 只能单进程启动（不加 `--workers`）；② 重启后数据集与工作流都从内存消失（SQLite 只归档历史，见 8.3.1）。两条的共同根因是同一个（进程内 `dict`），讲的时候一起讲，比分开讲更清楚。

```python
# 零件 2：后台执行器（app/core/background.py）
def submit(state: WorkflowState, tasks: BackgroundTasks) -> None:
    """register() 之后调用。tasks.add_task 会把 run_workflow 放到 Starlette 的线程池里执行。"""
    tasks.add_task(_run_safely, state)

def _run_safely(state: WorkflowState) -> None:
    try:
        state.started_at = datetime.now(timezone.utc)
        run_workflow(state)          # Orchestrator，同步函数
    except BaseException as exc:      # 兜底：绝不让线程静默死掉
        state.status = WorkflowStatus.FAILED
        state.error = ApiError(code=ErrorCode.INTERNAL_ERROR,
                               message="分析过程中出现未预期错误", detail=str(exc)[:500])
    finally:
        state.finished_at = datetime.now(timezone.utc)
        state.step = "done"
```

```python
# 零件 3：接口（app/api/routes_analysis.py）
@router.post("/analysis", response_model=ApiResponse[AnalysisAccepted])
def start_analysis(req: QuestionRequest, background: BackgroundTasks) -> ApiResponse[AnalysisAccepted]:
    # 1) 校验 dataset_id 存在（不存在 → ok:false, NOT_FOUND）
    # 2) 建 WorkflowState(status=PENDING) → registry.create()
    # 3) background.submit(state, background)
    # 4) 立刻 return，HTTP 只等了不到 1 毫秒
```

**为什么这样就不阻塞请求**：`BackgroundTasks` 注册的回调在**响应发出去之后**才执行；`run_workflow` 是同步函数，Starlette 会把它丢进线程池（默认 40 个工作线程），所以它既不占用处理请求的事件循环，也不影响别的请求。前端每秒 `GET /api/workflows/{id}` 读 `status/step`，读完了就渲染进度条。

**这套方案的已知边界（必须写进 README，别让它变成"面试时被问倒的地方"）**：
1. **只能单进程运行**。`uvicorn --workers 4` 之后，上传请求可能落在 A 进程、轮询落在 B 进程，B 的字典里没有这个 `workflow_id` → 404。**启动命令固定为单进程**（不加 `--workers`）。
2. **重启即丢失**。`Ctrl+C` 之后所有工作流没了。演示时先启动服务再上传，属于可接受。
3. **没有任务队列**。10 个人同时点分析，就是 10 个线程在跑 pandas，CPU 会满。个人展示项目可接受。
4. **超时靠"惰性检测"**：不是真定时器，而是轮询接口里检查 `now - heartbeat_at > max_workflow_seconds` 就落 `FAILED`。实现 10 行，不会出现"前端的圈永远转"。

### 8.3 什么时候升级、升级成什么（触发条件必须写清楚，不预先做）

> **第 14 节决策 3 已拍板：D6 只做 SQLite 持久化，SSE 明确标注为"不做"。** 因此下表第 1 行（SQLite）是**已批准的 D6 可选任务**，第 4 行（SSE）从"可选"降为"明确不做，只作为将来的方向记录"。

| 触发条件（满足任一） | 升级动作 | 学习成本 |
|---|---|---|
| 需要 `--workers > 1`，或服务重启后仍要能查到历史工作流 | 把 `WorkflowState` 落 SQLite（`data/app.db`，用已装的 SQLAlchemy），启动时按 `workflow_id` 从库里加载；`registry` 退化为"缓存" | 低：加 1 张表 + 2 个函数 |
| 分析经常超过 60 秒、或想同时跑多个分析而不抢占 | 引入**线程池 + 队列**（`concurrent.futures.ThreadPoolExecutor(max_workers=2)`），`submit` 改成入队，`status` 加 `QUEUED` | 低：标准库，不加依赖 |
| 需要多台机器 / 需要任务不丢 | 才考虑 Celery + Redis（或 RQ + Redis） | 高：多一个进程、一个中间件、一套配置；个人项目**不建议** |
| ~~需要"边跑边看"（不用轮询）~~ | ~~加 SSE~~ —— **本项目不做**（见第 14 节决策 3：1 秒轮询已够用，收益小、学习成本不低，两个都做会挤掉 D5 校验时间）。方向记在这里，留待将来 | — |

**升级铁律**：上面每一条都必须**先改本文档第 8.3 节**（写清触发条件已满足的证据），再动代码。没到触发条件就实现，属于过度工程。

#### 8.3.1 SQLite 的读写路径约定（决策 3 的第 ① 点补充，必须遵守）

D6 加 SQLite 之后，**内存态 `registry._workflows` 仍然是唯一事实来源（single source of truth）**，SQLite 只做**历史归档写入**：

```
写路径：run_workflow 到终态时 → registry 更新内存对象（原地）
                              → 顺手 archive.save_snapshot(state) 写一条库记录
                              → 失败也只记日志（logging.warning），绝不影响工作流收尾

读路径：GET /api/workflows/{id}          → 只读内存 registry。
        GET /api/workflows/{id}/report   → 只读内存 registry。
        历史列表（若做）GET /api/workflows/history → 只读 SQLite。
```

**明确禁止**："有时读内存、有时读库"的混合读路径（例如"内存里没有就 fallback 去查库，查到了再塞回内存"）。理由：那会引入最难查的一类 bug——同一个 `workflow_id` 在两个地方状态不一致、`has_report` 一会儿 true 一会儿 false、后台线程还在原地改的对象已经不在字典里。**宁可"重启后查不到这个 ID（404），但历史列表里能看到"，也不要"半活的复活对象"。**

对外表现因此是两条清晰的路：
- `GET /api/workflows/{old_id}`（重启后）→ **404 + `NOT_FOUND`**，`detail` 写"该工作流已不在内存中（服务可能已重启），历史记录见历史列表"；
- `GET /api/workflows/history`（可选）→ 从 SQLite 读归档记录（`workflow_id / question / status / created_at / finished_at / has_report / llm_used`），**只读展示，不提供"继续运行"**。

#### 8.3.2 已知限制（必须同时写进 README，不许让人误以为"持久化 = 断点续跑"）

1. **SQLite 存的是"历史归档"，不是"断点续跑"**：服务重启后内存态丢失，**正在运行中的工作流不会恢复，也不会被标成失败**——它只是从内存里消失（前端轮询到 404 就提示"服务已重启，请重新提交"）。
2. **重启后历史记录仍在**：已到达终态的工作流可从 SQLite 查到（问题、状态、时间、报告文本），但**报告接口不提供对这些旧 ID 的服务**（读路径只有内存，见 8.3.1），历史记录里直接带上归档的报告内容。
3. **仍然只能单进程**：SQLite 不会让 `--workers 4` 变安全（内存字典仍然分裂）；要支持多进程必须先走 8.3 第 1 行的完整改造（读路径也改库），那是另一个决定。
4. **归档是"尽力而为"**：写库失败只记 warning，不让工作流失败——因为工作流的成功与否不该由归档决定。

---

## 9. 前端方案（`app/web/`，原生 HTML + CSS + JS）

### 9.1 页面区块划分（单页，从上到下）

| 区块 | 元素 | 调用的接口 | 交互 |
|---|---|---|---|
| A 上传区 | `<input type="file" accept=".csv,.xlsx">` + `<button>上传</button>` + `<div id="upload-state">` | `POST /api/datasets/upload` | 选完文件点上传；成功显示"文件名 / 行数 / 列数"，并把 `dataset_id` 存进 JS 变量 `state.datasetId` |
| B 数据概览区 | 表格 `id="preview"`、列名列表、`<span id="llm-badge">` | `GET /api/datasets/{id}` + `GET /api/datasets/{id}/profile` | 上传后自动请求一次；显示前 5 行预览、列名、以及"缺失值最多的 3 列"；**顺带展示 `/api/health` 的 `llm_configured`**，若为 false 显示灰字"未配置大模型，将使用规则模式" |
| C 提问区 | `<textarea id="question">` + 3 个示例问题按钮 + `<button id="ask">开始分析</button>` | `POST /api/analysis` | 示例按钮：把常见问题（"各地区的销售额汇总""销售额前 5 的商品""哪些列有缺失值"）一键填进文本框，方便演示 |
| D 状态区 | `<div id="steps">`（6 个步骤圆点 + 文字） + `<span id="elapsed">` | `GET /api/workflows/{id}`（每 1000 ms） | 把 `step` 映射成中文：profiling→"正在读取数据"、planning→"正在制定分析计划"、execution→"正在计算"、charting→"正在生成图表"、reporting→"正在撰写报告"、validation→"正在校验数字"；已完成的打勾，当前的高亮 |
| E 图表区 | `<div id="charts">`，每个 `<img src="{ChartResult.url}">` + 标题 | 图片由 `GET /api/workflows/{id}/charts/{filename}` 直接返回 | 图不下载到本地，直接 `<img>` 指向后端 URL，省去 base64 转换 |
| F 报告区 | `<div id="report">`：标题 + `summary` + 各 `section`（标题/正文/小表格）+ 数据质量提示 + 校验结果徽章 | `GET /api/workflows/{id}/report` | 状态进入终态后才请求一次；`validation.passed === false` 时顶部显示黄色横幅，把 `unsupported_numbers` 列出来（**这个横幅就是这个项目最有说服力的展示点**：它证明系统在自我检查） |

### 9.2 轮询逻辑（`app.js`，约 30 行）

```js
async function pollWorkflow(workflowId) {
  const TERMINAL = ["SUCCESS", "SUCCESS_WITH_WARNINGS", "FAILED"];
  const timer = setInterval(async () => {
    const resp = await fetch(`/api/workflows/${workflowId}`).then(r => r.json());
    if (!resp.ok) { clearInterval(timer); showError(resp.error.message); return; }
    renderSteps(resp.data);                       // 更新 D 区
    if (TERMINAL.includes(resp.data.status)) {
      clearInterval(timer);
      renderCharts(resp.data.charts);             // 更新 E 区
      const rep = await fetch(`/api/workflows/${workflowId}/report`).then(r => r.json());
      if (rep.ok) renderReport(rep.data); else showError(rep.error.message);
    }
  }, 1000);
}
```

规则：**只轮询状态接口**（小），**不轮询报告接口**（大）；进入终态立刻 `clearInterval`，避免关掉页面后还在打请求（可以再加 `visibilitychange` 事件，隐藏标签页时暂停轮询，属于优化项）。

### 9.3 前端不做的事

- 不用 React/Vue/打包工具（第一版是"能看懂每一行"的展示，不需要构建链）。
- 不在前端做任何计算或格式化数字（千分位、百分比也应尽量后端算好；如果要做，只允许纯展示格式化，绝不做统计）。
- 不做多文件对比、不做拖拽上传、不做登录。
- 静态文件挂载（D6）：`app.mount("/static", StaticFiles(directory=app/web), name="static")`，并且给 `GET /` 改成返回 `index.html`（现有 `/` 返回 JSON 提示，D6 时保留 `/api/health` 与 `/docs`，把 `/` 让给页面）。**注意 `data/` 目录绝不挂载**，图表一律走第 8 号接口，避免把上传的原始文件暴露成静态资源。

---

## 10. 安全设计清单

| # | 项目 | 具体要求 | 实现位置 |
|---|---|---|---|
| 1 | 上传格式白名单 | 只允许扩展名 `.csv` / `.xlsx`（大小写不敏感）。`.xls`、`.csv.gz`、`.zip`、`.parquet`、无扩展名一律拒绝。**只信扩展名 + 解析成功这两道关，不信 `Content-Type`**（浏览器给的 `Content-Type` 经常是 `application/octet-stream`，参考价值低） | `routes_datasets.py` + `loader.py` |
| 2 | 大小限制 | 在**读之前**用 `UploadFile.size` 或分块累加判断，超过 `settings.max_upload_bytes`（默认 10 MB）立刻返回 `FILE_TOO_LARGE`，并且**不把已经读到的内容写进磁盘**。README 里写清"超过 10 MB 请先在 Excel 里裁剪" | `routes_datasets.py` |
| 3 | 路径穿越防护 | ① 落盘名一律用 `{uuid4().hex}.{ext}`，**用户文件名只写进数据库/内存字段，从不参与拼路径**；② 读取时再做一次确认：`resolved = target.resolve(); assert resolved.parent == upload_path.resolve()`（用 `Path.resolve()` 消掉 `..` 之后比较父目录）；③ 文件名黑名单：含 `..`、`/`、`\`、`:`、`\0` 直接拒绝 | `routes_datasets.py`、`data/store.py` |
| 4 | 安全文件命名 | 图表文件名只由 `chart_id`（12 位 hex）决定；`ChartResult.filename` 用正则 `^[0-9a-f]{12}\.png$` 校验后才允许读；工作流目录名只由 `workflow_id`（32 位 hex）决定 | `routes_workflows.py`、`agents/charts.py` |
| 5 | 密钥管理 | 密钥只从 `.env` 读（`pydantic-settings`），`.env` 已在 `.gitignore`；`Settings.llm_api_key` 已带 `repr=False`，任何 `print(settings)` 都不会带出原文；**任何 API 响应与日志里都不允许出现密钥**（测试用 `assert "sk-" not in resp.text` 之类的方式兜底，不要只靠人工检查） | `config.py`（已做）+ QA 测试 |
| 6 | 模型调用超时与重试上限 | `llm_timeout_seconds`（默认 60，上限 300）+ `llm_max_retries`（默认 2，上限 5）已经写在 Settings 里，**必须真的传进 openai 客户端**：`OpenAI(base_url=..., api_key=..., timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries)`。另外在 orchestrator 层再限一次总时长（`max_workflow_seconds=180`） | `llm/client.py` |
| 7 | 禁止 eval / exec / 动态导入 | 全项目禁止 `eval(`、`exec(`、`compile(`、`__import__(`、`getattr(__builtins__, ...)`；工具调用只走显式白名单注册表。**测试里加一条源码检查（B-3 修订：用 `ast` 而不是字符串搜索）**，见下方代码。**为什么不用"读文本找 `eval(` 子串"**：那样注释、docstring、以及 `test_security.py` 自己都会命中（测试里必须写出 `eval(` 这个字符串），要么误报要么被迫写"注释里出现不算"这种无法机检的规则；用 AST 只认**真的被调用**的节点 | `tests/test_security.py` |

```python
# tests/test_security.py 的源码检查（B-3 修订版，约 25 行）
import ast
from pathlib import Path

BANNED_CALLS = {"eval", "exec", "compile", "__import__"}
BANNED_ATTRS = {"system", "popen"}          # os.system / os.popen

def test_no_dynamic_execution_in_source():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    hits: list[str] = []
    for py in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))   # 注释与 docstring 天然不在语法树里
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in BANNED_CALLS):
                hits.append(f"{py.name}:{node.lineno}: {node.func.id}()")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in BANNED_ATTRS):
                hits.append(f"{py.name}:{node.lineno}: .{node.func.attr}()")
    assert hits == [], f"发现动态执行调用：{hits}"
```

**它不做什么（要诚实说明）**：AST 检查只能防"我们自己写的代码直接调用 `eval`"，防不住 `getattr(obj, name)()` 这种间接调用。所以它的定位是"低成本挡住 99% 的偷懒写法"，真正的安全边界仍然是 §6 的工具白名单注册表（只有注册表里的 7 个函数可以被调用，名字来自模型也必须过 `registry.py` 的查表）。
| 8 | 图表与报告访问限制 | 不做 `StaticFiles` 挂载 `data/`；图表与报告都通过带 `workflow_id` 的接口访问，并且**校验 `workflow_id` 是 32 位 hex**、`chart_id` 属于该工作流的 `charts` 列表（而不是"文件存在就返回"）。这样随机猜文件名的请求会 404 | `routes_workflows.py` |
| 9 | 返回数据不泄露本地路径 | `ApiError.detail` 里允许出现开发排查信息，但**必须去掉绝对路径与用户名**：`errors.py` 的 `sanitize_detail()` 用正则把 `D:\...`、`C:\Users\...` 替换成 `<path>`。**B-4 补充：这条规则必须有测试守护**（`detail` 会把异常原文塞进去，`FileNotFoundError: [Errno 2] No such file or directory: 'D:\\个人项目01\\...'` 是最常见的一条），否则它只是一句注释。测试见下方代码 | `errors.py` + `tests/test_security.py` |

```python
# tests/test_security.py —— sanitize_detail 的守护测试（B-4 新增，约 15 行）
from app.errors import AppError, sanitize_detail
from app.schemas import ErrorCode

def test_sanitize_detail_removes_drive_and_username():
    raw = (r"FileNotFoundError: [Errno 2] No such file or directory: "
           r"'D:\个人项目01\ai-data-analyst-agent\data\uploads\ab12.csv'")
    cleaned = sanitize_detail(raw)
    assert "D:" not in cleaned and "C:" not in cleaned      # 盘符没了
    assert "个人项目01" not in cleaned and "jun" not in cleaned   # 项目路径与用户名没了
    assert "<path>" in cleaned                              # 但保留了"这里原本有个路径"的信息

def test_sanitize_detail_keeps_plain_message():
    assert sanitize_detail("没有这个数据集") == "没有这个数据集"

def test_unexpected_error_response_has_no_local_path(client):
    """端到端：真的发一次会抛 FileNotFoundError 的请求，检查响应体。
    detail 里也不许出现盘符（§10 第 9 条的最终验收口径）。"""
    r = client.get("/api/datasets/not-a-real-id")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert "D:" not in r.text and "C:\\Users" not in r.text
```
| 10 | CORS 收紧 | 已经只允许 127.0.0.1 / localhost 的 8000 与 5500，`allow_credentials=False`。**不要为了图省事改成 `["*"]`** | `main.py`（已做） |
| 11 | 上传文件不执行 | 绝不把上传目录放到 Python 的 import 路径上，也不用 `pandas.read_pickle` / `read_json`（pickle 反序列化能执行任意代码）。只允许 `read_csv` / `read_excel` | `loader.py` |
| 12 | Excel 炸弹与超宽表 | 行数上限（例 20 万行）与列数上限（例 200 列）超过即 `BAD_REQUEST`；公式不计算（`openpyxl` 读值不读公式）；避免 `read_excel` 把整个工作簿所有 sheet 都读进来（只读第一个 sheet 或指定 sheet） | `loader.py` |

---

## 11. 技术选型：为什么「不引入」这些东西

原则：**每一个依赖都要能回答"没有它我少什么"**。下面是逐个说明，格式统一为「它的用途 / 不用的替代方案 / 什么时候才值得引入」。

| 技术 | 它能解决什么 | 第一版不用它，替代方案 | 什么时候值得引入 |
|---|---|---|---|
| **LangGraph** | 用图（节点 + 边 + 状态）描述多 Agent 流程，自带状态机、检查点、条件分支、人工审核环节 | 我们的流程是**固定顺序的 6 个阶段 + 最多 6 个任务的循环**，用 `orchestrator.py` 里一个 `for` 循环 + `if/elif` 就能写清，一共约 150 行。自己写的好处：出问题时你能一眼看出在第几步、状态存在哪、为什么重试；LangGraph 出问题时你要先学会它的 `StateGraph`/`Reducer`/`checkpoint` 概念才能调试 | 流程出现**真正的分支与回溯**（例：校验不过要回到规划阶段重规划、需要人工审批节点、需要从中断点续跑）。第一版若硬上：多学 2–3 天概念、调试成本翻倍，而展示效果不变 |
| **Celery** | 分布式任务队列：把任务丢给独立 worker 进程，支持重试、定时、结果后端 | 一个分析任务最慢 60 秒，并发量是"演示者一个人点"。`BackgroundTasks` + 线程池完全够用，且**零新依赖** | 需要多 worker 进程、需要任务持久化/定时调度、分析耗时进入分钟级、或要部署到多台机器 |
| **Redis** | 缓存 / 消息中间件 / 分布式锁 | 数据集和 DataFrame 放进程内 `dict`；工作流状态也在 `dict`。**项目只有一个人用，进程内存是最快的"缓存"** | 要多进程共享状态，或 Celery 需要 broker。引入前先问：为什么不先加 SQLite？（SQLite 已经够解决"重启不丢"） |
| **Docker** | 环境隔离与一键部署 | 你的环境是 Windows 11 + `.venv`（`--system-site-packages`），`requirements.txt` 已经能复现。Docker Desktop 在 Windows 上要开 WSL2、多占几个 GB 内存，学习成本与收益不成比例 | 需要给别人"一条命令跑起来"的交付（例：面试官要本地跑）→ 那时加一个 `Dockerfile`（FROM python:3.12-slim + pip install + uvicorn）大约 15 行，成本可控。**放在第 7 天之后，不进七天排期** |
| **SQLAlchemy / SQLite** | 持久化：重启后仍能查询历史工作流 | 第一版状态全在内存。**但它在"进阶可选"里，第 6 天若有富余，优先加这一项**（见 8.3 触发条件 1） | 需要"历史记录"页、需要多进程、需要交付后复盘 |
| **向量数据库 / RAG** | 语义检索 | 完全没有场景：用户数据是结构化表格，字段名和类型是精确的，靠 `DatasetProfile` 就能让模型看全 | 出现"用自然语言在大量自由文本里找相关内容"的需求时 |
| **前端框架（React/Vue）** | 组件化与状态管理 | 单页 + 6 个区块 + 一次轮询，原生 JS 约 200 行能写清；引入框架就要引入 node_modules 与构建步骤，还会挡住"每一行都能读懂"这个目标 | 页面超过 3 个路由、需要组件复用或团队协作时 |
| **PandasAI / 让模型写 pandas 代码** | 让模型直接生成代码分析 | **明确禁止**。它直接违反项目铁律（数字必须由我们自己的白名单工具算出），而且生成的代码会 `exec`，安全与可控性都不满足 | 永不引入（与本项目的核心卖点冲突） |
| **SSE / WebSocket** | 实时进度推送 | 1 秒一次轮询，实现 10 行，够用 | 需要秒级以内的实时性、或轮询请求量成为瓶颈（见 8.3 第 4 条） |

**新依赖的总闸门**：任何新增第三方依赖，必须先在 `requirements.txt` 的注释里写一行"用途 + 不用的替代方案"，否则不合并。这条规则本身也写进 README。

---

## 12. 七天开发计划与依赖顺序

前提：第一阶段骨架已完成（`app/main.py`、`config.py`、`schemas.py`、`runtime.py`、`routes_health.py`、`tests/test_health.py`），第一天只做验收。

验收命令统一形式（PowerShell 5.1，在项目根目录执行）：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q
python -m uvicorn app.main:app --reload --port 8000
Invoke-RestMethod http://127.0.0.1:8000/api/health | ConvertTo-Json -Depth 5
```

| 天 | 目标 | 负责人（主 / 协） | 可并行的部分 | 验收标准（命令可验） |
|---|---|---|---|---|
| **D1** | 骨架验收 + 架构文档定稿 | LEAD / ARCH | ARCH 写文档与 LEAD 跑验收完全并行 | ① `python -m pytest -q` 全绿（现有 8 条左右）；② `/api/health` 返回三键外壳且 `llm_configured=false`；③ `docs/ARCHITECTURE.md` 存在且第 14 节的 3 个问题已拍板 |
| **D2** | 数据层 + 上传接口（**串行，是后面所有天的地基**） | BE / QA | BE 写 `loader.py`+`store.py`+`routes_datasets.py`；QA 同时写 `tests/test_loader.py`、`tests/test_upload_api.py`；LEAD 生成 `data/samples/sales.csv` | ① `pytest tests/test_loader.py tests/test_upload_api.py -q` 全绿；② 上传 `sales.csv` 返回 `dataset_id`，磁盘上文件名是 32 位 hex；③ 上传 `.txt` → `ok:false, code:UNSUPPORTED_FILE`；④ 上传 11 MB 文件 → `FILE_TOO_LARGE`；⑤ 文件名 `..\..\evil.csv` 上传后磁盘文件仍在 `data/uploads/` 内（用 `Get-ChildItem -Recurse` 确认没有越界文件） |
| **D3** | 工具层（**串行，核心资产**） | AI / BE、QA | 7 个工具可以拆给 2 人：`describe.py`+`missing_values` 一人，`aggregate.py`+`compare.py`+`timeseries.py` 一人；profiler 与 routes_datasets 的 `/profile` 可并行 | ① 每个工具至少 3 条测试（正常 / 参数错 / 列不存在）；② `python -m pytest tests/test_tools.py tests/test_profiler.py -q` 全绿；③ **对照测试**：手工用 Excel 算一遍 `sales.csv` 的"各地区销售额汇总"，与 `group_aggregate` 的返回完全一致（这条是"数字是真的"的第一道证据）；④ `GET /api/datasets/{id}/profile` 返回的 `null_count` 与 `df.isna().sum()` 一致 |
| **D4** | LLM 客户端 + 规划 + Orchestrator + 分析接口（**串行，最大风险点**） | AI / BE | AI 写 `llm/`+`planner.py`+`orchestrator.py`+`analysis.py`；BE 写 `core/registry.py`+`core/background.py`+`routes_analysis.py`；QA 写 `test_workflow_state.py`。**接口先按 Pydantic 模型定好，两边对着模型写，不需要等对方完成** | ① **兜底模式 + 真实数据必须能跑出完整报告**（第 14 节决策 1 的验收条件）：未配 LLM 时 `POST /api/analysis` 用规则兜底计划跑完全流程，`status=SUCCESS`、`report.llm_used=false`，报告有正文、有表格、有 `cites`，**且报告里明确写清哪一步用了模型、哪一步没用**（模板报告首段固定写"本次未使用大模型（llm_used=false），以下结论为工具结果的直接汇总"）；② 配上 LLM 后：同一问题返回的 `plan.source == "llm"`，且 `tasks[].tool_name` 全在白名单内，报告里写明数字来自 pandas 工具、模型只负责解释；③ 把 `LLM_BASE_URL` 改成错误地址 → 工作流仍是 `SUCCESS`（走兜底），而不是 500；④ 轮询接口在 3 秒内能查到 `step` 变化 |
| **D5** | 图表 + 报告 + 校验（报告依赖 D4 的 `ToolResult`，**串行接在 D4 后**） | AI / FE（提前搭页面骨架） | AI 写 `agents/charts.py`+`validation.py`；FE 可以**用假 JSON 先把页面写完**（不需要等后端）；QA 写 `test_validation.py`、`test_charts.py` | ① `GET /api/workflows/{id}/charts/{f}.png` 返回 `image/png` 且能在浏览器打开（中文标题不乱码）；② `pytest tests/test_validation.py -q` 全绿，其中**必含一条**：手工把一个不存在的数字塞进 `FinalReport.sections[].text`，`validate()` 必须产出 `UNSUPPORTED_NUMBER` 且 `passed=false`；③ 报告里的表格数字与 `ToolResult.data["rows"]` 逐字段相同（写一条断言） |
| **D6** | 前端联调 + 安全检查 + SQLite 持久化 | FE / QA、BE | FE 写 `web/` 三件套；QA 写 `test_security.py`；BE 加 SQLite 持久化（**第 14 节决策 3 已定为做它、不做 SSE**） | ① 浏览器打开 `http://127.0.0.1:8000/` 能完成"上传 → 提问 → 看进度 → 看图 → 看报告"全流程；② `pytest -q` 全绿（含安全测试：AST 源码检查、响应不含 `sk-`、密钥不进 `repr`、`sanitize_detail` 去路径）；③ 上传 `.txt` 和超大文件时页面显示中文错误而不是白屏；④ 断网/模型超时场景页面在 180 秒内必出结果（含失败提示），不会永远转圈 |
| **D7** | 收尾：演示数据 + README + 简历材料 | LEAD / 全员 | ① LEAD 写 README（架构图、启动步骤、3 个演示问题、已知限制）；② QA 跑**全新克隆验证**（`git clone` 到临时目录 → 建 venv → `pip install -r requirements.txt` → `pytest`）；③ ARCH 更新本文档的"实际实现与设计差异"一节；④ 录一段 90 秒演示（含校验拦截编造数字的画面） | ① 全新目录按 README 步骤能在 10 分钟内跑起来；② `pytest -q` 全绿；③ 用 `data/samples/sales.csv` 的三个示例问题都能出图出报告；④ 故意把某个工具注释掉再跑，`SUCCESS_WITH_WARNINGS` 能正常展示（证明容错不是写在 PPT 上的） |

### 12.1 依赖顺序（关键路径）

```
D1 骨架 ──▶ D2 数据层/上传 ──▶ D3 工具层 ──▶ D4 LLM+编排 ──▶ D5 图表+报告+校验 ──▶ D6 前端联调 ──▶ D7 收尾
                    │                 │                │                    ▲
                    │                 │                └── 前端可提前用假 JSON 并行（不依赖后端）──┘
                    │                 └── profiler / profile 接口 与 工具实现 可并行（不同文件）
                    └── QA 测试与实现并行编写（对着 Pydantic 模型写，不等实现完成）
```

**必须串行**：D2 → D3 → D4（每一环的输出是下一环的输入类型；提前开工会导致接口反复改）。
**可以并行**：同一文件之外的一切。D4 与 D5 之间也可以部分并行（`charts.py` 只依赖 `ToolResult` 类型，可以在 D4 中把类型定稿后先写）。

### 12.2 卡点预案（哪一天超时了怎么退）

| 卡点 | 退守方案（保证当天仍有可运行成果） |
|---|---|
| D3 工具写不完 7 个 | 只保留 4 个：`describe_dataset`、`group_aggregate`、`top_n`、`missing_values`（覆盖 80% 的自然语言问题），其余 3 个顺延 |
| D4 模型输出的 JSON 不稳定 | 直接切 `source="rule_fallback"`：用关键词规则把中文问题映射到工具（含"前 N"→`top_n`、"各地区/每月"→`group_aggregate`/"时间"→`time_series_summary`），**LLM 只用来写报告**。这样"多智能体"故事略减，但**核心卖点（数字真实 + 校验拦截）完整保留** |
| D5 中文标题乱码 | 用系统字体 `Microsoft YaHei`，若仍乱码就把图标题改成英文（图内英文、报告中文），不阻塞验收 |
| D6 前端时间不够 | 退化成一个**极简页面**（一个 `<textarea>` + 一个按钮 + 一个 `<pre>` 展示 JSON 报告 + 图片），能演示全流程即可；美化留到 D7 之后 |

---

## 13. 风险清单（技术风险 + 应对）

| # | 风险 | 具体表现 | 应对措施（必须有动作，不能只写"注意"） |
|---|---|---|---|
| 1 | **真实模型输出不稳定** | 同一个问题两次返回：一次合法 JSON，一次带解释性前后缀；或用了不存在的工具名 `groupby_sum`；或把 `group_by` 写成字符串而不是列表 | ① `planner.py` 里用**三层解析**：先 `json.loads`，失败则用正则抽第一个 `{...}` 块再 `json.loads`，再失败才重试（最多 2 次，第二次把错误附回 Prompt）；② 解析成功后仍然**逐字段过 Pydantic 校验**，`group_by` 是字符串时自动包成 `["地区"]`（只允许这一种宽容修正，其余一律拒绝）；③ 工具名不在白名单 → 丢弃该任务并记 warning；④ `source` 字段记录是 llm 还是兜底，便于统计"模型成功率"；⑤ **永远有规则兜底计划**——模型彻底失败时系统仍然可用（这是 D4 的验收项之一） |
| 2 | **用户数据字段千奇百怪** | 中文列名带空格/换行、重名列（`销售额`、`销售额.1`）、表头不在第一行、单元格里混了 `"1,234"` 或 `"12%"` 这类文本、日期是 `2025/1/3` 或 Excel 序列号 | ① `loader.py` 只做**最小清洗**：`str.strip()` 列名、重名列加 `_2` 后缀、全空行/全空列删除；**绝不猜业务含义**；② 数值列带千分位时**不改原值**，而是在 `DatasetProfile.warnings` 里写明"列 X 被识别为文本，因为它包含非数字字符"，让模型和用户都知道；③ 日期列识别用 `pd.to_datetime(errors="coerce")` 的**成功率阈值**（≥0.8 才认定为日期），而不是看 dtype；④ 校验第 3 类（字段存在）报错时，`error_message` 必须附**真实列名列表**，便于用户改问法；⑤ 测试里放一个"脏到极致"的 CSV（中文列名 + 缺失 + 重复 + 文本数字） |
| 3 | **pandas 3.0 与旧教程 API 差异** | 照着 1.x 教程写 `df.append()`（已删除）、`inplace=True`（大量方法不再支持）、链式赋值（CoW 下静默不生效，最容易踩——代码不报错但结果不对） | ① 第 6.4 节末尾列出的写法约定是**强制**的；② `tests/test_tools.py` 里加"链式赋值检测"用例：构造一个会因 CoW 失效的调用，断言抛异常（`pandas.errors.ChainedAssignmentError`）而不是静默通过；③ 所有分组统一写 `groupby(..., as_index=False, dropna=False, observed=True)`；④ 不用 `inplace`，统一 `df = df.assign(...)`；⑤ 排期上把 D3 的半天留给"API 差异踩坑"，QA 在这一天必须用真实 pandas 3.0 跑一遍全部工具 |
| 4 | **模型编造数字（本项目的核心卖点，也是最大风险）** | 报告写"销售额同比增长 23.5%"，但工具从来没算过 23.5 | ① `validate()` 的数字溯源是**必做项**（D5 验收硬指标），且容差规则写死；② 只要出现 `UNSUPPORTED_NUMBER`：先让模型带问题重写 1 次；仍失败 → `SUCCESS_WITH_WARNINGS` + 前端黄色横幅列出可疑数字；③ 报告里的表格**直接复制** `ToolResult.data["rows"]`，不给模型改写表格的机会；④ Prompt 里给模型看的是"数字表"，并要求每段带 `cites`，无 `cites` 的段落直接判 `SHORT_REPORT` |
| 5 | **多线程 + 可变全局状态（并发 bug）** | 轮询接口读到半更新的 `WorkflowState`（比如 `status=SUCCESS` 但 `report` 还是 `None`），前端崩在 `report.summary` 上；或两个请求同时写同一个 dict | ① 顺序固定为「先写结果字段 → 最后写 `status`」，`status` 是**唯一**的完成信号（读到终态时其他字段必已就绪）；② `registry` 的读改写一律 `with _lock:`；③ 单进程运行（8.2 已写死）；④ 前端对 `report` 做空值防御（`if (!rep.data.report) return;`）；⑤ 测试里起 5 个线程同时 `create/get/touch`，跑 200 次断言无异常 |
| 6 | **大模型不可用 / 没有 API Key / 网络不通** | 第 4 天要演示，第 4 天密钥还没配；或调用 429/超时 | ① `settings.llm_configured` 为 `false` 时**系统照常工作**（规则计划 + 模板报告，`llm_used=false`），并且 `/api/health` 已经把 `llm_configured` 暴露出来；② 客户端超时/重试参数已经配置化（第 10 节第 6 条）；③ 演示前**准备一个不依赖网络的备用演示**（同样的 `sales.csv` + 兜底模式）；④ 报告里明确写"本次未使用大模型"，这是诚实展示，不是缺陷 |
| 7 | **性能与内存** | 上传 50 MB Excel / 20 万行 × 100 列，画像要跑十几秒，DataFrame 常驻内存不释放；10 次上传后内存涨到几个 GB | ① 上传前限制大小（10 MB）与行数（20 万行）与列数（200 列）；② `DatasetStore` 加**容量上限**（例最多保留 5 个 DataFrame，超了按 `created_at` 淘汰并删除磁盘文件），并用 `df.memory_usage(deep=True)` 记录占用（画像里已有 `memory_bytes`）；③ 画像对高基数列（`unique_count > 10000`）只做近似统计（跳过 `top_values` 精确计数）；④ 不做的优化也要写清：**第一版不做流式读取、不做分块处理**（收益低于复杂度） |
| 8 | **中文编码与乱码** | CSV 是 GBK（Windows 上 Excel 另存的 CSV 默认 GBK），`read_csv` 直接 `UnicodeDecodeError`；matplotlib 中文显示成方框 | ① `loader.py` 按顺序尝试 `utf-8-sig` → `utf-8` → `gbk` → `gb18030`，全部失败才报 `BAD_REQUEST`，并把"检测到的编码"写进 `DatasetProfile.warnings`；② 分隔符尝试 `,` → `;` → `\t`（通过第一行的字符统计启发式判断）；③ matplotlib 设 `rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]` 与 `rcParams["axes.unicode_minus"] = False`；④ 测试里放一个 GBK 编码的 CSV 文件（用 `encoding="gbk"` 写出来）作为固定用例 |
| 9 | **时间与排期风险（最大的非技术风险）** | D4 卡住一天，D5/D6 被压缩，最后一天在赶工中把"校验"砍掉——而校验恰恰是简历里最值钱的部分 | ① 排期把**校验**（D5 的一部分）视为**不可砍项**，砍它之前先砍前端美化、SQLite 历史（SSE 已按第 14 节决策 3 明确不做，不占预算）；② 每天结束必须有可运行成果（第 12 节的退守方案已为每天准备好"缩小版"）；③ LEAD 每天收工前跑一次 `pytest -q` 并把输出贴进 README 的"进度"一节，避免"以为完成了" |
| 10 | **演示环境与真实环境不一致** | 换一台机器（面试官电脑）没有 `Microsoft YaHei`、没有 `--system-site-packages` 的全局包、Python 版本不同，跑不起来 | ① `requirements.txt` 用下界（`>=`）而不是锁定版本，兼容 3.10–3.12；② README 写清 Python 版本与安装命令（含国内镜像源）；③ D7 做**全新克隆验证**（第 12 节 D7 验收项 ①）；④ 不使用任何绝对路径（现有 `config.py` 用 `PROJECT_ROOT` 拼接，已经是对的，后续代码必须沿用这个模式） |

---

## 14. 决策记录（3 个待拍板问题 → 已拍板，含拍板人追加的约束）

> 状态：**3 条全部已拍板（Project Lead）**，本节的"最终决定"是**执行口径**，各节正文（7.1、8.3、12、13）已与本节对齐。改动本节需同步改动对应正文，否则视为不一致。

### 决策 1：第 4 天演示时如果 API Key 还没到位，是否接受"规则兜底模式"作为 D4 的验收口径？

| | 内容 |
|---|---|
| 我的建议 | 接受。D4 的验收拆成"无 LLM 也能跑完全流程"和"配了 LLM 时计划来自模型"两条，前者不依赖密钥 |
| **最终决定** | **接受**。理由比"省事"更硬：**D4 的验收不该被"API Key 有没有额度、网络通不通"这三个外部变量卡住**。这正好落实用户总提示词规则 7（保留真实错误信息、不用假结果绕过）——兜底模式下报告必须显式标注 `llm_used=false`，模型只负责解释、数字仍然**全部来自 pandas**，所以"结果可信"这条底线没有被破坏 |
| **追加的验收条件** | **兜底模式 + 真实数据必须能跑出完整报告，且报告里明确写清哪一步用了模型、哪一步没用。** 落到可验证的三点：① `POST /api/analysis`（不配 LLM）→ `status=SUCCESS` 且报告有正文/表格/`cites`；② `report.llm_used=false`，`FinalReport.summary` 首段含"本次未使用大模型"字样；③ 接口 7 返回的 `llm_used` 字段与 `FinalReport.llm_used` 一致，前端据此显示横幅 |
| 代价（保留） | 需要额外花 1 小时和 AI Agent Engineer 一起把"关键词 → 工具"的规则兜底写清楚。这段代码将在整个项目里一直保留，用于模型故障降级，**不是临时补丁** |

### 决策 2：`GET /api/workflows/{id}` 是否同意"只返回状态、不返回报告与结果"（报告走第 7 号接口）？

| | 内容 |
|---|---|
| 我的建议 | 同意。轮询会每秒打一次，报告可能几十 KB；分开之后前端逻辑也更清楚（状态区轮询、报告区一次性取） |
| **最终决定** | **同意**（只回状态、报告走独立接口）。轮询接口要高频调用，**保持小响应体是对的** |
| **追加的约束** | **响应里必须带一个字段让前端知道"报告已经可以取了"**：`has_report: bool`（见 7.1 第 3 条）。规则 `has_report = (report is not None)`；光靠 `status` 到终态是不够的，因为 `SUCCESS_WITH_WARNINGS` 和超时 `FAILED` 也可能有报告，前端不能靠猜、也不该多发一次注定失败的请求 |
| 备选（未采纳） | 同一接口加 `?include=report`。会让"状态接口"有两个返回结构，前端多写一个分支，**不值得** |

### 决策 3：D6 的"SQLite 持久化历史工作流"和"SSE 实时进度"两个可选项，最多做哪一个？

| | 内容 |
|---|---|
| 我的建议 | 只做 SQLite 持久化（8.3 触发条件 1）。能解决一个真实且会被问到的问题（"重启后数据还在吗？"），约 40 行、用已装的 SQLAlchemy；SSE 只是把 1 秒轮询换成推送，用户体验提升很小、学习成本不低 |
| **最终决定** | **选 SQLite，SSE 明确标注为"不做"**。8.3 表格里的 SSE 一行已划掉；第 13 节风险 9 的"可砍顺序"已改为"前端美化 → SQLite 历史"（SSE 不再占预算，因为它已经不做）；第 12 节 D6 的目标栏也已从"SSE（可选）"改为"SQLite 持久化" |
| **追加的约束 ①（最关键）** | **SQLite 只做历史归档写入，内存态 dict 仍是唯一事实来源。** 不要让读写路径变成"有时读内存有时读库"，那会引入最难查的一类 bug。完整读写路径约定见 **8.3.1**（写：终态后归档，失败只记 warning；读：接口只读内存；历史列表只读库；禁止 fallback 复活对象） |
| **追加的约束 ②** | 文档里必须**显式写明"重启后内存态丢失、但历史记录仍在"这个已知限制**，而不是让人以为"持久化 = 断点续跑"。见 **8.3.2**（4 条已知限制，其中"正在运行的工作流重启后不会恢复也不会被标失败"要写进 README） |
| 未采纳的选项 | SSE。**承认它是个好技术**，但本项目不需要——用户只有一个人、1 秒轮询已够用，多出来的学习成本会挤掉 D5 的校验时间（而校验是简历里最值钱的部分） |

### 14.1 与第一阶段代码（commit 460cf8c）的一致性核对结果

| 核对项 | 结论 |
|---|---|
| `app/config.py` 删除模块级 `settings` 对象 | **文档已同步**：第 4.0 节写成强制约定（`from app import config` + 函数内 `config.get_settings()`、测试用 `config.reset_settings_cache()`），并列出两种禁止写法。附录 B 里"`app/config.py` 不改已有字段、D2 只追加"的描述仍然成立——删除模块级对象是行为变化，不是字段变化 |
| `app/main.py` 统一错误处理器 | **文档已同步**：第 7 节开头新增"框架异常 → 同一外壳"映射表（404/405 → `NOT_FOUND`/`BAD_REQUEST`，422 → `BAD_REQUEST`，`detail` 内容），并明确"HTTP 状态码表达 HTTP 语义、`error.code` 表达业务语义"。**v1.1 更新**：原先"业务错误一律 200"的绝对表述已**整段删除**，改为 §7.0 的完整状态码映射表（含 202/413/415/503）+ `app/errors.py` 的 `ERROR_STATUS` 集中映射，理由与代价见 **§14.2 第 1 条** |
| lifespan 创建上传目录 / `/api/health` 新增 `upload_dir_ready` | **文档已同步**：接口 1 的成功响应字段列表已含 `upload_dir_ready`，并注明"`false` 不算错误，属于能跑但要提示的状态"。**建议**：第 9 节页面把"服务状态"小徽章做成 `llm_configured` + `upload_dir_ready` 两个点，避免上传目录不可写时前端只显示"在线" |
| `tests/test_error_format.py` 已存在（守护上述外壳 + CORS） | **文档已同步**：目录树里标注为"(已存在, 460cf8c) … **不要动**"，避免后续把它的期望改成旧格式 |
| matplotlib 缓存目录坑（WinError 5） | **文档已同步**：`config.py` 顶部已 `os.environ.setdefault("MPLCONFIGDIR", <项目根>/.mplcache)`，所以第 13 节风险 8 的 matplotlib 部分只需保证"中文字体 rcParams"，**不需要**再处理缓存目录；第 4.0 节已写死"`agents/charts.py` 不许改 `MPLCONFIGDIR`、不许设非 Agg 后端" |
| 其他发现的不一致 | 见下方"给 Project Lead 的核对备注" |

### 14.2 本轮 9 项修订的落实情况（v1.0 → v1.1，审核意见 `docs/REVIEW_ARCHITECTURE.md`）

> 修订日期 2026-10-13，修订人 architect。逐条对应审核意见 D 节的 9 条清单。**9 条全部已改**，无未改项。

| # | 审核条目 | 是否已改 | 改在哪（小节） | 怎么改的（可 grep 的关键词） |
|---|---|---|---|---|
| 1 | **[阻塞] A-1** HTTP 状态码自相矛盾 | ✅ 已改 | §7 开头统一约定、**新增 §7.0 状态码映射表**、§7 接口表错误码列、**新增 §7.3 `ERROR_STATUS`**、§4.0 代码约定、§14.1 | 删除"业务错误一律 HTTP 200"整句；照抄 A-1 表 12 行（200/202/400/404/405/413/415/422/500/503 + `WORKFLOW_TIMEOUT`→200）；接口表每格写成 `**415** UNSUPPORTED_FILE` 形式；`POST /api/analysis` 改 **202**；补 4 条"禁止绕过统一外壳"约束 |
| 2 | **[阻塞] A-2** `charts` 三处不一致 | ✅ 已改 | §4.8、§7 接口 6 / 接口 8、**新增 §7.1.1**、§9.1 E 区 | 写死 `ChartResult` 字段与 `url` **由后端拼好** `/api/workflows/{workflow_id}/charts/{filename}`；§7.1.1 给出前端唯一正确写法 `img.src = c.url`（不拼字符串），并说明"前端拼接会让 404 的图片链接无法被 pytest 覆盖" |
| 3 | **[阻塞] A-3** 目录设计冲突 | ✅ 已改 | §2 目录树、§2.1、§3 | 收口为 `app/data/{loader,store,profiler}.py` + `app/errors.py` + `app/api/routes_datasets.py` 一份清单；同一处标注"`app/datasets.py` 与 `app/security/files.py` 这两个提法已作废"（保留此说明是为了让后来者知道它们**不是**被漏掉） |
| 4 | B-1 `WorkflowState` 定义位置分裂 | ✅ 已改 | §4.0 判据、§4.5 | 全部模型定义在 `app/schemas_workflow.py`；`app/core/registry.py` **一行模型定义都没有**；判据一句话："数据长什么样"进 `schemas_*.py`，"数据放在哪、怎么同步"进 `core/` |
| 5 | B-2 锁说明矛盾 + 漏"登记图表" | ✅ 已改 | **新增 §8.2.1 / §8.2.2 / §8.2.3** | ① `_lock` 只保护 registry 的 dict，`state` 内部字段**不需要**加锁（附"禁止到处加锁 / 禁止返回 deepcopy"两条禁令）；② `charts.py` 不许改 `state`，由 `analysis.py` 在**同一函数体内连续两行**写文件 + `state.charts.append(chart)`，并说明"写"与"登记"分离会产生孤儿 PNG + 接口 404；③ `DatasetStore` 同属进程内，单进程限制同样适用，要一起写进 README |
| 6 | B-3 源码扫描测试做法 | ✅ 已改 | §10 第 7 条 | 改为 `ast.parse` + `ast.walk` 判断 `ast.Call` 的 `eval/exec/compile/__import__`（附约 25 行测试代码）；写明"注释与 docstring 天然不在语法树里"；并诚实标注 AST 检查**防不住** `getattr(obj, name)()` 这类间接调用，真正的边界是工具白名单注册表 |
| 7 | B-4 `sanitize_detail()` 缺测试 | ✅ 已改 | §10 第 9 条 | 补 15 行测试：构造含 `D:\个人项目01\...` 的 `FileNotFoundError` 原文，断言盘符/项目路径/用户名都消失且保留 `<path>`；另加一条端到端断言（404 响应体里不出现盘符）。§7.3 的 `AppError.__init__` 里把 `sanitize_detail` 接上，不再是"只写在注释里" |
| 8 | B-5 示例数据命名冲突 | ✅ 已改 | §2 目录树 `data/samples/`、§12 的 D2/D3/D7 行、§13 风险 6 | 定名 `data/samples/sales.csv`（"定名就是它"已写进目录树注释）；`students.xlsx` 已去掉，明确"演示一个数据集足够，第 6 天有富余再加" |
| 9 | D-9 追加本轮修订记录 | ✅ 已改 | **本节 §14.2** + 文档头部 | 文档标题与版本行改为 **v1.1（2026-10-13 修订）**，并指向本节 |

**顺带做掉的两处一致性修正（不在 9 条里，但同源）**：
1. **`AppError` 签名统一为 4 参数** `AppError(code, message, detail=None, status_code=None)`：审核意见写的 `status_code=...` 三参数与我 §7.3 的两参数查表写法不一致，照哪边写都会让 backend-engineer 卡一下。现在两者并存——**默认查表**（保住"状态码集中在一处"这个 A-1 的核心收益），`status_code` 只作为极少数场景的显式覆盖。
2. **§14.1 里"原先'业务错误一律 200'已改为……"的描述**已更新为"整段删除 + 指向 §7.0"，避免这一节自己留下旧说法。

**唯一想请 Lead 再拍一次的一处（不影响 9 条验收，也不阻塞 backend 开工）**：A-1 表里"外部依赖不可用（模型未配置且无法兜底）"配的是 **503 + `BAD_REQUEST`**。HTTP 状态码 503 是对的，但 `error.code=BAD_REQUEST` 会让排查的人以为是自己请求写错了（`BAD_REQUEST` 的语义是"你的请求有问题"）。**核查后发现 backend 已经用 §7.3 的 `status_code` 参数把这个坑绕过去了**（`raise AppError(ErrorCode.BAD_REQUEST, "...", status_code=503)`，见 `app/errors.py` 的 `__init__` docstring 与已落地的 `ERROR_STATUS`），所以不改也能跑、且状态码正确。若 Lead 愿意，建议在 `ErrorCode` 里补一个 `SERVICE_UNAVAILABLE`（`ERROR_STATUS` 加 1 行 → 503），这样前端就可以按 `code` 而不是按 HTTP 状态码分流。**代价约 2 行代码**（枚举 1 个值 + 映射表 1 行）；若认为"少一个错误码更简单"，本条按原样保留，不影响任何验收。

---

## 附录 A：术语速查（给基础 Python 水平读者）

| 词 | 大白话解释 |
|---|---|
| Agent（智能体） | 一个负责某类工作的模块。在本项目里就是"一个 Python 文件 + 几个函数"，不是独立进程、不是微服务 |
| Orchestrator（编排器） | 总调度：按顺序调用其他 Agent，并决定失败后重试还是放弃。它也是唯一能改工作流状态的地方 |
| Tool Calling（工具调用） | 把 Python 函数的名称、说明、参数格式告诉大模型，模型不执行代码，只是**说出它想调哪个函数、参数是什么**，由我们的代码去执行 |
| 白名单 | 一张写死的允许清单。不在清单里的名字一律拒绝，防止模型"想到什么调什么" |
| Pydantic 模型 | 用类来描述"一份数据长什么样"（有哪些字段、什么类型），并在创建对象时自动检查。检查不过就抛错，拦住脏数据 |
| 溯源（citation） | 报告里的每句话标注它的数字是从哪个任务来的（`cites: ["T1","T3"]`），校验时能顺着查回去 |
| 兜底（fallback） | 主方案失败时的备用方案。本项目里指"模型不可用时用关键词规则生成计划"和"用模板生成报告" |
| 轮询（polling） | 前端每隔 1 秒问一次"做完了吗"，而不是后端主动推给它。实现简单，代价是有一点点延迟和请求量 |
| 进程内状态 | 存在 Python 的内存字典里（`dict`）。好处是简单飞快，坏处是**重启就没了、多进程看不见**（8.2 已写明） |

## 附录 B：本文档与已存在代码的关系（不改动清单）

| 已存在文件 | 本文档的处理方式 |
|---|---|
| `app/schemas.py` | **不改**已有 `ApiResponse`/`ApiError`/`ErrorCode`；D2 只在 `ErrorCode` 里追加 `WORKFLOW_TIMEOUT`（追加不算破坏） |
| `app/config.py` | **不改**已有字段；D2 追加 `charts_dir`、`max_workflow_seconds`、`tool_call_limit`、`max_dataset_rows` |
| `app/main.py` | D6 追加静态文件挂载与首页路由；CORS 白名单保持不动 |
| `app/runtime.py` | 不动 |
| `tests/test_health.py` | 不动（如果 D6 把 `/` 改成返回 HTML，**这条测试会失败**——届时必须同步修改测试并在 README 记一笔，这是唯一预期会改动的已有测试） |
| `README.md` | 属于 Project Lead，本设计不改；第 12 节 D7 的工作里包含 README 更新清单 |

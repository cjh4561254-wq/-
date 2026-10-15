# AI Data Analyst Agent —— 系统架构设计（v1.1）

> 文档性质：**设计方案**，不是实现。第 4 节的 Pydantic 模型只写在本文档的代码块里，落地成 `.py` 文件由对应负责人在指定日期做。
> 版本：**v1.1**（2026-10-13 修订）。v1.0 → v1.1 按 `docs/REVIEW_ARCHITECTURE.md` 的 9 条审核意见修订，逐条落实记录见 **第 14.2 节**。
> 适用水平：会基础 Python、会调用大模型 API、正在学 FastAPI。全文避免抽象术语；出现一个新名词时，第一次出现就给一句大白话解释。
> 环境事实（已核实，含第一阶段返工 commit `460cf8c`、第二阶段契约冻结 commit `44566c6`）：Windows 11 + PowerShell 5.1、Python 3.12.8、项目根目录 `D:\个人项目01\ai-data-analyst-agent`、虚拟环境 `.venv`（`--system-site-packages`）。已装：fastapi 0.136.3、uvicorn 0.48.0、pydantic 2.13.4、pandas 3.0.3、numpy 2.4.6、pytest 9.1.1、httpx 0.28.1、openai 2.41.0、python-dotenv 1.2.2、python-multipart 0.0.32、openpyxl 3.1.5、matplotlib 3.11.2（精确快照另见 `requirements.lock.txt`，分工见第 11 节）。
>
> **两条已经生效的代码约定（本文档全部示例都遵守，后续所有新代码也必须遵守）**：
> 1. **取配置一律 `from app import config` + 函数内 `config.get_settings()`**；不存在模块级 `settings` 对象，测试里改完环境变量用 `config.reset_settings_cache()`。理由见 `app/config.py` 的 docstring：模块级对象在 import 时就绑死了，测试会"静默失效"。
> 2. **matplotlib 缓存目录**由 `app/config.py` 顶部设置 `os.environ.setdefault("MPLCONFIGDIR", <项目根>/.mplcache)`（并 `mkdir`）。这是现实坑的修复：本机 `C:\Users\jun\AppData\Local\matplotlib` 曾拒绝访问（WinError 5）。因此 **`agents/charts.py` 里不允许出现 `matplotlib.use("Agg")` 之外的后端设置，也不允许改 `MPLCONFIGDIR`**，直接 `from matplotlib.figure import Figure` 使用即可。

---

## 0. 一句话说清这个系统在干什么

用户上传一个 CSV/Excel → 用中文提一个问题（例：「按地区看销售额前 5，并画柱状图」）→ 大模型**只负责把问题翻译成一份"分析计划"**（选哪几个工具、用哪些字段）→ 后端用 pandas 真实执行这些工具 → 得到真实数字和图表 → 大模型**只负责用中文解释这些数字** → 校验 Agent 检查"报告中需要溯源的数字是否能在工具结果或数据画像中找到依据" → 返回网页。

**贯穿全系统的铁律：数字只能由 Python 算出来。大模型不许做算术，不许发明字段名，不许直接执行代码。**

---

## 1. 总体架构图（含每步的输入/输出数据类型）

### 1.1 全景图

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  ① 浏览器（单页 app/web/index.html + app.css + app.js）                       │
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

> **`[AI]` / `[BE]` 这一列的判据（v1.1 写死，避免每加一个文件都要重新讨论一次）**：
> - **`[BE]`** = 该模块的**每一步数值都由 Python / pandas 算出来，模型不参与**；
> - **`[AI]`** = 该模块**有真实的 LLM 调用点**（哪怕只是兜底路径里的一次调用）。
>
> 所以这一列的用途是回答"**模型到底参与了哪几步**"：**数一下全树里 `[AI]` 有几个就是答案**。按此判据，`[AI]` 只出现在 **5 个文件**里 —— `llm/__init__.py`、`llm/client.py`、`llm/prompts.py`（`llm/` 整个包就是模型边界）、`agents/planner.py`（规划）、`agents/validation.py`（其中的 `build_report` 写报告），对应**三个功能区**：模型请求层 / 规划 / 报告撰写。**其余全是 `[BE]`**，包括 7 个工具、`profiler.py`、`orchestrator.py`、`analysis.py`、`charts.py` —— 这正是"执行阶段零 LLM 参与"在目录结构上的体现。
> **这一列现在的适用范围与例外**：`app/` 下的行全部按上面这套判据（只回答"模型参与了吗"）；`tests/`、`web/`、`data/`、`docs/` 行里的 `[QA]`、`[FE]`、`[LEAD]`、`[ARCH]` **仍是负责方**（那几类文件不存在"模型参与"的问题，也从来没人按 AI 参与度去读它们）。**两种含义不要混着引用。**
>
> **想知道某个 `app/` 文件是谁写的，看第 12 节排期表，不要看这一列。** 对照关系：`loader.py`/`store.py`/`profiler.py`/`errors.py`/`config.py`/`routes_datasets.py`/`routes_analysis.py`/`core/*` = **BE**；`tools/*`/`llm/*`/`agents/*` = **AI**（工具层可按第 12 节 D3 拆一半给 BE）；`web/*` = **FE**；`tests/*` = **QA**。
>
> **这条标记约定的权威说明见 `docs/DECISIONS.md`**：列语义（`app/` 下按 AI 参与度、其余目录按负责方）= **D-03**，`app/data/profiler.py` 归属 D2 = **D-04**。以后要改这条约定，**两处一起改**。

```
ai-data-analyst-agent/
├─ .env.example  .gitignore  .gitattributes  pytest.ini  requirements.txt  requirements.lock.txt  README.md   (已存在)
├─ conftest.py                    (已存在, dde1115) 把 pytest 的临时目录钉进项目内 `.tmp/`；见 D-07
├─ docs/
│  ├─ ARCHITECTURE.md                 [D1][ARCH] 本文档（唯一架构交付物）
│  ├─ DECISIONS.md                    (已存在) 决策记录 D-01..D-15；文档与代码冲突时以本文档的"当时"为准
│  ├─ REVIEW_ARCHITECTURE.md          (已存在) 对本表的第一轮审核意见（v1.0 → v1.1 的 9 条修订来源）
│  └─ STAGE2_ACCEPTANCE.md            (已存在, 0833ff2) 第二阶段（上传/预览/画像/示例数据）验收报告
├─ scripts/
│  ├─ __init__.py                     (已存在, c15aa92) 只为让测试能 import scripts.make_sample_data
│  └─ make_sample_data.py             (已存在, c15aa92) 固定 seed 生成 data/samples/sales.csv（可复现性由 .gitattributes 保证，见 D-15）
├─ app/
│  ├─ __init__.py                     (已存在) __version__ = "0.1.0"
│  ├─ main.py                         (已存在) D6 追加静态文件挂载 /static 与 web 首页路由
│  ├─ config.py                       (已存在) Settings 共 16 个字段（见 §4.0.1，字段表为准，别信本文行号）；含 temp_dir/temp_path；D4 冻结时追加 charts_dir（+ charts_path 属性）、max_workflow_seconds、tool_call_limit；D6-SQLite 追加 db_path（+ db_file 属性）
│  ├─ runtime.py                      (已存在) STARTED_AT + uptime_seconds() + runtime_environment()（lifespan 里设临时目录，见 D-07）
│  ├─ schemas.py                      (已存在) ApiError / ApiResponse[T] / ErrorCode（8 个码，见 §7.0）
│  ├─ schemas_dataset.py              (已存在, 44566c6) FileExt / ALLOWED_EXTENSIONS / SemanticType 三个模块级别名常量 + Dataset / DatasetSummary / DatasetProfile / ColumnProfile（见 4.1、4.2）
│  ├─ schemas_tool.py                 (已存在, de86db2) ToolErrorCode / ChartType / ToolResult / ChartResult（见 4.7、4.8；**ToolSpec 不在这个文件**，它按 §6.1 定义在 `tools/registry.py`）
│  ├─ schemas_workflow.py             (已存在, de86db2) **全部工作流相关 Pydantic 模型**（见 B-1 修订，4.3–4.6、4.9–4.12）；D4 冻结时新增 AnalysisAccepted（见 4.14.1）
│  ├─ schemas_llm.py                  (已存在, D4 冻结时新建) LLMToolCall / LLMReply：**模型请求层的边界类型**；"必须回传 reasoning_content"的校验在这里（见 4.14.1、4.14.2）
│  ├─ errors.py                       (已存在, fe155af) AppError 异常 + ERROR_STATUS 映射 + sanitize_detail()（去绝对路径/用户名）；**不写 HTTP handler**（handler 在 main.py，共 4 个）
│  ├─ api/
│  │  ├─ __init__.py                  (已存在)
│  │  ├─ routes_health.py             (已存在) D1 不动
│  │  ├─ routes_datasets.py           (已存在, 9441429) POST /datasets/upload、GET /datasets/{id}、GET /datasets/{id}/profile
│  │  ├─ routes_analysis.py           [D4][BE] POST /analysis（立刻返回 workflow_id）
│  │  └─ routes_workflows.py          [D6][BE] GET /workflows/{id}、/report、/charts/{filename}
│  ├─ data/
│  │  ├─ __init__.py                  (已存在, 34dd3a9)
│  │  ├─ loader.py                    (已存在, 34dd3a9) load_dataframe(path, ext) → (DataFrame, encoding)；编码/分隔符兜底
│  │  ├─ profiler.py                  (已存在, 34dd3a9) build_profile(df, dataset_id) → DatasetProfile（纯 pandas 计算，**零 LLM 参与**；实际就在 D2 做，见 14.3）
│  │  └─ store.py                     (已存在, 34dd3a9) DatasetStore：进程内 dict，存 DataFrame 与 DatasetSummary；**不写盘**（落盘在路由层）
│  ├─ core/
│  │  ├─ __init__.py                  (已存在, c41afc0)
│  │  ├─ registry.py                  (已存在, c41afc0) WorkflowRegistry：只存（dict + Lock + create/get/touch），**一行模型定义都没有**（模型全在 schemas_workflow.py）
│  │  └─ background.py                [D4][BE] submit(state, tasks) → BackgroundTasks；_run_safely 用 except BaseException 兜底 + finally 收尾（见 8.2）
│  ├─ tools/
│  │  ├─ __init__.py                  (已存在, 15edd69)
│  │  ├─ registry.py                  (已存在, 15edd69) ToolSpec 定义 + register/get/list_specs/to_openai_tools；**7 个工具函数**在这里登记（见 §6）
│  │  ├─ jsonable.py                  (已存在, 15edd69) to_jsonable / ensure_jsonable：把工具返回值清洗成可 JSON 化的纯 Python 类型（**校验 5** 的落点，见 §6.3）
│  │  ├─ validate.py                  (已存在, 15edd69) validate_call(tool_name, params, df) + ToolValidationError：校验 1–4，**顺序前置短路**（工具名 → 参数 → 列存在 → 操作合法）
│  │  ├─ describe.py                  (已存在, 15edd69) describe_dataset、missing_values
│  │  ├─ aggregate.py                 (已存在, 15edd69) group_aggregate、top_n
│  │  ├─ compare.py                   (已存在, 15edd69) compare_groups、correlation
│  │  └─ timeseries.py                (已存在, 15edd69) time_series_summary（pandas 3.0 写法，见 13.3）
│  │                                  ↑ 本目录磁盘上共 **8 个 .py**，工具函数是 **7 个**——两个数字含义不同，别混（见 §14.6）
│  ├─ llm/
│  │  ├─ __init__.py                  [D4][AI]
│  │  ├─ client.py                    [D4][AI] chat(messages, tools=None) → LLMReply；超时/重试/错误封装
│  │  └─ prompts.py                   [D4][AI] 规划 Prompt、解释 Prompt（含"不许造数"硬约束）
│  ├─ agents/
│  │  ├─ __init__.py                  (已存在, c41afc0)
│  │  ├─ orchestrator.py              [D4][BE] run_workflow(state)：状态机唯一所有者（自身不调模型，只调 planner）
│  │  ├─ planner.py                   (已存在, c41afc0) plan_with_rules(question, profile, max_steps) → TaskPlan（**规则兜底，已落地**）+ plan(question, profile, max_steps)（**调 LLM**，D4 加；签名见 4.14.3，所以本文件标 [AI]）
│  │  ├─ analysis.py                  (已存在, c41afc0) run_task(task, df) → ToolResult；需要时调图表工具
│  │  ├─ charts.py                    [D5][BE] render_chart(ToolResult, chart_type, ...) → ChartResult
│  │  └─ validation.py                [D5][AI] validate(state) → ValidationResult（纯代码校验，无模型）；build_report(...) → FinalReport（**调 LLM**，所以本模块算 [AI]）
│  └─ web/
│     ├─ index.html                   [D6][FE] 单页应用外壳：顶栏 + 侧边导航 + 6 个视图 + 内联 SVG 图标精灵
│     ├─ app.js                       [D6][FE] fetch 调用 + 1 秒轮询 + 视图路由（hash）+ 渲染
│     └─ app.css                      [D6][FE] 原生 CSS，设计令牌 + 组件 + 两档响应式断点
├─ tests/
│  ├─ __init__.py                     (已存在) 让 tests/ 成为一个包（与 app/ 下同名测试文件不冲突）
│  ├─ test_health.py                  (已存在) D1 不动
│  ├─ test_error_format.py            (已存在, 460cf8c) 404/405/422/路由抛错的统一外壳 + CORS 来源限制
│  ├─ test_errors.py                  (已存在, fe155af) AppError 状态码表、sanitize_detail 的路径/用户名清洗
│  ├─ test_loader.py                  (已存在, 34dd3a9) 正常 CSV / GBK CSV / xlsx / 空文件 / 只有表头 / 编码口径（见 D-12）
│  ├─ test_store.py                   (已存在, 34dd3a9) register / build_stored_filename / 32 位 hex 与白名单两道校验
│  ├─ test_profiler.py                (已存在, 34dd3a9) 画像字段与缺失值/重复行统计 / semantic_type 八例
│  ├─ test_data_layer_e2e.py          (已存在, 34dd3a9) 三个模块串起来跑真实文件，末端 json.dumps(allow_nan=False)
│  ├─ test_upload.py                  (已存在, 9441429) 上传成功、超限、非法扩展名、路径穿越文件名、惰性画像缓存（**注意文件名是 test_upload.py，不是 test_upload_api.py**）
│  ├─ test_temp_dir.py                (已存在, 24aeac1) 启动前不 import 副作用 / 启动中 gettempdir 在项目内 / 退出后还原
│  ├─ test_sample_data.py             (已存在, c15aa92) 示例数据的字节可复现、脏数据数量、销售额=销量×单价
│  ├─ test_tool_registry.py           (已存在, 15edd69) 注册表：ToolSpec 定义 / register / get / list_specs / to_openai_tools（209 行）
│  ├─ test_tools.py                   (已存在, 15edd69) 7 个工具的正确性 + 5 类校验的拒绝路径（653 行）
│  ├─ test_workflow_registry.py       (已存在, c41afc0) WorkflowRegistry 的 create/get/touch 与线程安全（151 行）
│  ├─ test_analysis_agent.py          (已存在, c41afc0) 执行层：run_task 串工具、工具失败不炸整个任务（312 行）
│  ├─ test_planner_rules.py           (已存在, c41afc0) 规则兜底计划器：关键词 → 工具/参数、越界问题不硬编（372 行）
│  ├─ test_contracts_d3.py            (已存在, 555cb80) Lead 钉 D3 契约：model_dump(mode="json") + json.dumps(allow_nan=False) 必须过、ChartResult 的 filename/url/chart_id 三者一致、WorkflowState 派生字段与限额（544 行 / 24 个 test 函数 / pytest 收集 **32 项**）
│  ├─ test_contracts_d4.py            (已存在, D4 冻结时新建) Lead 钉 D4 契约：LLMReply 必须携带 reasoning_content（含三条反例）、AnalysisAccepted 四字段不泄漏内部字段、三个新配置字段的默认值与边界，以及五个 D4 函数签名的**棘轮**（未写则 skip、写了但与契约不符则 fail；D4 结束时跳过条数应为 0）
│  ├─ test_workflow_state.py          [D4][QA] 状态迁移、重试上限、超时收尾
│  ├─ test_analysis_api.py            [D4][QA] 未配 LLM 时降级返回、workflow 生命周期
│  ├─ test_validation.py              [D5][QA] 报告里塞入假数字必须被校验拦下
│  ├─ test_charts.py                  [D5][QA] PNG 落地、文件名安全、Content-Type
│  └─ test_security.py                [D6][QA] 上传白名单、大小、穿越、密钥不出现在响应里
├─ data/
│  ├─ uploads/.gitkeep                (已存在) 上传原件，文件名 = uuid4().hex + 安全扩展名
│  ├─ charts/.gitkeep                 [D5][BE] 生成的 PNG
│  └─ samples/
│     └─ sales.csv                    (已存在, c15aa92) 唯一演示数据（2168 行 × 9 列，中文列名，故意含缺失值/重复行/异常值）；行尾由 .gitattributes 钉成 LF（见 D-15）
```

**根目录那个 `.env.example` 的定位**：它是**环境变量清单**，不是模板——应用既不读它、也不读 `.env`，配置来源只有进程的环境变量（见 §4.0.1 与 `docs/DECISIONS.md` D-19）。所以改它不会改变运行行为，要改配置请设系统环境变量（`README.md` 的「配置」一节写了怎么设）。

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
| 能做 | ① **数字溯源校验**：扫描 `FinalReport.summary`、每个 `sections[].text` 与 `data_quality_notes` 中的数字，逐个在 `ToolResult.data` 与 `DatasetProfile` 的数值来源中查找可接受值（绝对值相等；一般数值先过相对差 0.1% 的量级门槛，再比较 1/2 位舍入；百分比/小数换算按展示精度比较）；无出处记 `UNSUPPORTED_NUMBER`；② **任务校验**：`plan.tasks` 中失败、跳过或成功但返回空结果的任务逐条记问题；③ **图表校验**：`ChartResult.source_task_id` 必须能在 `results` 中找到，PNG 文件必须存在；④ **画像一致性**：报告提到的列名必须在 `DatasetProfile.columns[].name` 中，否则记 `UNKNOWN_COLUMN`；⑤ 给出 `severity`（`error` / `warning` / `info`）与 `passed`（没有 error 级问题才为 True） |
| **不能做** | ① 不修改报告内容（发现问题就报告问题，不偷偷改数字）；② 不重跑工具；③ 不决定 `WorkflowState.status`（那是 Orchestrator 的事，Orchestrator 依据 `ValidationResult.passed` 决定） |
| 调用 LLM | **否** |
| LLM 职责边界 | 不适用 |

#### 3.5.2 `build_report(state, validation) -> FinalReport`（会调用 LLM 一次）

| 项目 | 内容 |
|---|---|
| 输入 | `state.question`、`state.results` 中成功工具的结果（列表裁到最多 20 项）、工具列名、可选的 `validation.issues`；不向模型发送原始 DataFrame |
| 输出 | `FinalReport`（见 4.10）；模型只生成 `sections[].text`，其余字段由代码组装 |
| 能做 | ① 让模型根据工具结果写中文结论段；② 为每段提供 `cites`，指向相关 `task_id` |
| **不能做** | ① 不写 summary、质量提示、表格、引用总表或 LLM 标记；② 不执行代码；③ 不能新增无出处数字（后续由 `validate` 核对）；④ 不替用户作业务决策 |
| 调用 LLM | 正常情况下 1 次；仅在校验发现 error 级 `UNSUPPORTED_NUMBER` 时最多再调用 1 次重写 |
| LLM 职责边界 | 模型只写正文段落文本；summary、`data_quality_notes`、表格、citations 与首段标记均由代码根据真实状态生成。首次输出被数字校验拦下时，最多给一次带校验问题的重写机会；重写仍不通过则保留最终报告并将工作流降级为 `SUCCESS_WITH_WARNINGS` |

---

## 4. 核心数据结构设计（字段级）

约定：`必填` 列里「是」= 无默认值，必须显式给出；「否」= 有默认值。
类型写法沿用 Python 3.12：`str | None`、`list[str]`、`dict[str, Any]`。

**模型的文件归属（B-1 修订，已定死）**：`schemas_dataset.py`（数据集） / `schemas_tool.py`（工具） / `schemas_workflow.py`（工作流）三个文件，**全部只放 Pydantic 模型与枚举，零业务逻辑**。判断标准一句话：**"数据长什么样"进 `schemas_*.py`，"数据放在哪、怎么同步"进 `core/`**。所以 `WorkflowState` 虽然跟运行状态有关，它**仍然定义在 `app/schemas_workflow.py`**，`app/core/registry.py` 里一行模型定义都没有 —— 读者搜 `class WorkflowState` 一定搜得到，这对正在学 FastAPI 的读者很重要。**唯一一个不在 `schemas_*.py` 里的"模型"是 `ToolSpec`**：它回答的是"工具怎么被调用"（带 `func` 可调用对象），属于工具层，定义在 `app/tools/registry.py`（见 §6.1）。逐文件的完整清单见 §4.13。

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
raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "大模型未配置且无法兜底")  # → HTTP 503（枚举自带映射）
#   ↑ D4 起这一处不可达（`plan()` 保证能兜底），但**这行示例不要删**：
#     它演示的是"新增一个 ErrorCode 只需在 ERROR_STATUS 里加一行"，与可达性无关。

# 状态码全部由 ERROR_STATUS[code] 查表得到（§7.3），**路由里不要手写状态码**。
# status_code 参数（第 4 个位置参数）目前没有调用点，**留着别删**：等真出现
# "同一个 code 需要两个不同状态码"时从调用点传，比往枚举里塞一个新 code 更好。
# 典型场景：将来某处想让 NOT_FOUND 之外的东西配 503，而 NOT_FOUND 在别处必须是 404 ——
# 那时 POST /api/analysis 才会写成 AppError(..., status_code=503)；
# 现在 503 有枚举自带的映射，所以**不需要**传。
# 另一类来源是框架自己抛的 HTTPException / RequestValidationError，由 main.py
# 的四个 handler 兜住（404 / 405 / 422 / 500），业务代码不需要操心。
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

### 4.0.1 配置项总表（`app/config.py`，**已有字段照抄，不要重命名**）

**配置来源只有一个：进程的环境变量**（Windows 上就是「系统属性 → 环境变量」里那些）。**不读 `.env`**：`app/config.py:61-70` 的 `model_config` 里故意没有 `env_file`（想临时读某个 dotenv 文件仍然可以显式传 `Settings(_env_file=...)`，实测可用）。背景：本机环境变量里配的是 DeepSeek 官方名 `DEEPSEEK_*`，而应用原先只认 `LLM_*`，于是"机器上明明配了 key，应用却报没配置"。决策见 `docs/DECISIONS.md` **D-19**。

**三个大模型字段各认两个名字，`LLM_*` 优先、`DEEPSEEK_*` 兜底**（常量在 `app/config.py:46-48`，并集 `LLM_ENV_VAR_NAMES` 在 `:51-55`）：

| 字段 | 认的名字（**前面的优先**，`AliasChoices`） | 默认值 |
|---|---|---|
| `llm_base_url` | `LLM_BASE_URL` / `DEEPSEEK_BASE_URL` | `""`（空 = 明确不接模型） |
| `llm_model` | `LLM_MODEL` / `DEEPSEEK_MODEL` | `"deepseek-flash"` |
| `llm_api_key` | `LLM_API_KEY` / `DEEPSEEK_API_KEY` | `""` |

**顺序不能反**：反了的话本机那套 `DEEPSEEK_*` 会悄悄盖掉显式设置的 `LLM_*`，而"我明明设了 `LLM_MODEL` 却不生效"是最难查的一类配置问题（`app/config.py:84-86` 有同样的注释）。根 `conftest.py:230-250` 的 autouse 夹具 `_isolate_llm_env` 在每个用例开始前清空这六个名字，清的就是 `LLM_ENV_VAR_NAMES` 这一份清单——否则"未配置"类用例在**有 key 的机器上必红**，而用户的机器就是有 key 的那台。

为什么给一张表：`Settings` 里已经有 16 个字段，写代码时最容易犯的错是"自己造一个名字相近的配置"（例如把 `max_upload_mb` 写成 `max_file_mb`、把 `max_rows` 写成 `max_dataset_rows`），造出来还不报错，只是**永远不会生效**（默认值一直在起作用）。写任何配置相关代码前先在这张表里查一下。

**字段名 → 环境变量名**：没有别名的字段，环境变量名就是**字段名的大写形式**——`model_config` 里 `case_sensitive=False`、`env_prefix=""`（实测 `APP_NAME` / `MAX_ROWS` / `UPLOAD_DIR` 分别改到 `app_name` / `max_rows` / `upload_dir`），所以下面表格最后一列里的名字，就是能拿去 `setx` 的名字。只有三个大模型字段例外：它们用了 `AliasChoices`，名字写死在上面那张小表里。还要分清两种错法的响法：**名字写错会静默失效**（默认值一直在起作用，查不出来），**值写错立刻报错**（`MAX_ROWS=not-an-int` 直接抛 `ValidationError`，实测）。

| 字段 | 类型 / 约束 | 默认值 | 对应环境变量 | 用途 | 状态 |
|---|---|---|---|---|---|
| `app_name` | `str` | `"AI Data Analyst Agent"` | `APP_NAME` | 健康检查返回的展示名 | 已存在 |
| `app_version` | `str` | `"0.1.0"` | `APP_VERSION` | 同上 | 已存在 |
| `upload_dir` | `str` | `"data/uploads"` | `UPLOAD_DIR` | 上传目录（相对项目根）；用 `settings.upload_path` 取 `Path` | 已存在 |
| `max_upload_mb` | `int`，`gt=0, le=200` | `10` | `MAX_UPLOAD_MB` | 单文件**字节数**上限；代码里用 `settings.max_upload_bytes`（= `mb * 1024 * 1024`） | 已存在 |
| `max_rows` | `int`，`gt=0, le=5_000_000` | `200_000` | `MAX_ROWS` | 解析后**行数**上限（第 10 节第 2b 条） | **v1.1 新增** |
| `max_columns` | `int`，`gt=0, le=2_000` | `200` | `MAX_COLUMNS` | 解析后**列数**上限（同上） | **v1.1 新增** |
| `temp_dir` | `str`，`min_length=1` | `".tmp"` | `TEMP_DIR` | 临时文件目录（相对项目根）；用 `settings.temp_path` 取 `Path`。**不是给业务代码读的**：`app/main.py` 的 lifespan 用 `app/runtime.py` 的 `runtime_environment()` 把 `TEMP`/`TMP`/`TMPDIR` 指到这里，Starlette 处理 >1 MB 上传时的 spool 文件就落在里面；目录不可写会让**服务启动失败**（而不是悄悄换个地方）。原因见 `docs/DECISIONS.md` D-07 | **v1.1 新增** |
| `llm_base_url` | `str` | `""` | `LLM_BASE_URL` / `DEEPSEEK_BASE_URL`（前者优先） | 模型服务地址（DeepSeek 等 OpenAI 兼容端点） | 已存在（v1.2 起认两名） |
| `llm_model` | `str` | `"deepseek-flash"` | `LLM_MODEL` / `DEEPSEEK_MODEL`（前者优先） | 模型名；**留空就是 `deepseek-flash`**（本机没配模型名，见 §14.7） | 已存在（v1.2 起认两名 + 有默认值） |
| `llm_api_key` | `str`，`repr=False` | `""` | `LLM_API_KEY` / `DEEPSEEK_API_KEY`（前者优先） | 密钥；**任何日志/响应里都不许出现** | 已存在（v1.2 起认两名） |
| `llm_timeout_seconds` | `int`，`gt=0, le=300` | `60` | `LLM_TIMEOUT_SECONDS` | 单次模型调用超时 | 已存在 |
| `llm_max_retries` | `int`，`ge=0, le=5` | `2` | `LLM_MAX_RETRIES` | 模型调用重试上限 | 已存在 |
| `charts_dir` | `str`，`min_length=1` | `"data/charts"` | `CHARTS_DIR` | 生成的 PNG 落地目录（相对项目根）；用 `settings.charts_path` 取 `Path`。接口 8 按文件名从这里取图 | **D4 冻结时新增** |
| `max_workflow_seconds` | `int`，`gt=0, le=3600` | `180` | `MAX_WORKFLOW_SECONDS` | 整条工作流总时长上限；轮询接口按 `heartbeat_at` **惰性**判定超时（§8.2 边界 4）。必须再兜一层的原因：模型超时重试会叠加，只靠单次超时会让前端一直转圈 | **D4 冻结时新增** |
| `tool_call_limit` | `int`，`ge=1, le=200` | `20` | `TOOL_CALL_LIMIT` | 一条工作流允许的模型工具调用总数；防"模型重试 × 任务重试"组合成死循环，也顺带防住无限花钱 | **D4 冻结时新增** |
| `db_path` | `str` | `"data/db/app.db"` | `DB_PATH` | SQLite 历史归档库路径（决策 3：只做归档写入，内存态仍是唯一事实来源）。**空字符串 = 关闭归档**（`get_history_store()` 返回 `None`，接口 9 回 `enabled=false` + 空列表 + 200）。用 `settings.db_file` 取 `Path`；该属性在 `db_path` 为空时会退回项目根，**调用方必须先判断归档是否开启**，别直接拿它 `sqlite3.connect()` | **D6-SQLite 追加**（已落地） |

六个**计算属性**（不是配置项，环境变量里没有它们，别去设）：`settings.upload_path`（`Path` 形式的上传目录，`main.py` 的 lifespan 已用它 `mkdir`）、`settings.charts_path`（同上，图表目录）、`settings.temp_path`（同上，临时目录）、`settings.db_file`（同上，归档库文件；**D6-SQLite 追加**）、`settings.max_upload_bytes`（字节数）、`settings.llm_configured`（`llm_base_url` / `llm_model` / `llm_api_key` **三项都非空**才为 `True`，**兜底模式的判定依据**，也是 `GET /api/health` 里 `llm_configured` 的来源）。

### 4.0.2 关键决定：`pd.DataFrame` 不放进任何 Pydantic 模型

原因：DataFrame 是可变的大对象，Pydantic 校验会尝试复制/序列化它，既慢又容易在 FastAPI 响应里报 `is not JSON serializable`。所以：

- `DataFrame` 只存在 `app/data/store.py` 的进程内字典里：`DatasetStore._frames: dict[str, pd.DataFrame]`，键是 `dataset_id`；
- Pydantic 模型里只出现 `dataset_id: str`（小字符串），需要 DataFrame 时用 `store.get_frame(dataset_id)` 现取。

### 4.1 `Dataset` / `DatasetSummary`（`app/schemas_dataset.py`，D2）

| 类名 | 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|---|
| `DatasetSummary`<br>（上传接口的返回体） | `dataset_id` | `str` | 是 | `uuid4().hex`，全局唯一（32 位小写十六进制） |
| | `original_filename` | `str` | 是 | 用户上传时的原始文件名（**只存不用于拼路径**） |
| | `row_count` | `int` | 是 | 行数（不含表头），`ge=0` |
| | `column_count` | `int` | 是 | 列数，`ge=0` |
| | `created_at` | `datetime` | 是 | 上传时间（UTC） |
| | `has_profile` | `bool` | 否 | 是否已生成画像（默认 `False`） |
| `Dataset(DatasetSummary)`<br>（详情接口的返回体） | *（继承上面 6 个字段）* | — | — | **实现时用继承，不要重抄一遍字段**：`class Dataset(DatasetSummary)` —— `Dataset` 就是"摘要 + 文件与预览信息"。以后给 `DatasetSummary` 加字段，`Dataset` 自动拿到，不会漏 |
| | `stored_filename` | `str` | 是 | 落盘名：`{dataset_id}.{csv\|xlsx}`，与用户输入完全无关 |
| | `file_ext` | `Literal["csv","xlsx"]` | 是 | 白名单扩展名（`.xls` 一律拒绝，避免老格式坑） |
| | `size_bytes` | `int` | 是 | 文件字节数，`ge=0` |
| | `sha256` | `str` | 是 | 内容哈希，用于"同一个文件重复上传"判断 |
| | `columns` | `list[str]` | 否 | 列名原样列表（保持顺序），默认 `[]` |
| | `dtypes` | `dict[str,str]` | 否 | `{列名: 原始 dtype 字符串}`，例 `{"销售额":"float64"}`，默认 `{}` |
| | `preview_rows` | `list[dict[str,str]]` | 否 | 前 5 行、值统一转 `str`（避免 `NaN` / `Timestamp` 不能序列化），默认 `[]` |

> **上传接口为什么只返回 `DatasetSummary` 而不返回 `Dataset`**：预览 + 列名 + dtype 加起来可能几 KB，上传响应应该尽快返回；完整信息走 `GET /api/datasets/{id}`（§7 接口 3）。这也是"两个模型都保留"的理由，不是为了对称好看。
> **实现已落地**（`app/schemas_dataset.py`，123 行，D2 交付），字段与本表逐个对应，照抄本表不会冲突。

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
| `task_id` | `str` | 是 | `"T1"`、`"T2"`……由 planner 编号，报告里引用它做溯源；代码里带正则约束 `^T[0-9]{1,2}$`（最多两位数字） |
| `order` | `int` | 是 | 执行顺序，从 1 开始，连续 |
| `kind` | `Literal["describe","aggregate","compare","trend","missing","correlation"]` | 是 | 语义类别（给人看，也用于默认图表选择） |
| `tool_name` | `str` | 是 | **必须在工具白名单里** |
| `params` | `dict[str, Any]` | 是 | 工具参数（键名必须与工具 schema 完全一致） |
| `description` | `str` | 是 | 一句中文说明，例 `"按 地区 汇总 销售额，取前 5"`，会显示在网页上 |
| `depends_on` | `list[str]` | 否 | 依赖的 `task_id`，默认 `[]`（第一版基本都是空） |
| `chart` | `ChartRequest \| None` | 否 | 该任务要不要出图，默认 `None` |
| `status` | `TaskStatus` | 否 | 默认 `TaskStatus.PENDING` |
| `attempt` | `int` | 否 | 默认 `0`，代码里约束范围 `ge=0, le=2`（走完 2 次重试仍失败就落 `FAILED`，见 §5.3） |
| `error` | `str \| None` | 否 | 失败原因（中文） |

`ChartRequest`：`chart_type: Literal["bar","line","pie","scatter"]`（必填）、`x_field: str`（必填）、`y_field: str`（必填）、`title: str`（必填，≤40 字）。

### 4.4 `TaskPlan`（`app/schemas_workflow.py`，D4）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `plan_id` | `str` | 是 | `uuid4().hex[:8]` |
| `question` | `str` | 是 | 用户原问题（原样保存，便于复盘） |
| `dataset_id` | `str` | 是 | 针对哪个数据集 |
| `tasks` | `list[AnalysisTask]` | 是 | 代码里是 `min_length=1, max_length=8`：**硬上限 8 个**。`max_steps`（默认 6）只是请求侧的旋钮，它让计划更短，但突破不了 8 |
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
| `step` | `WorkflowStep` = `Literal["profiling","planning","execution","charting","reporting","validation","done"]` | 否 | 默认 `"profiling"`。**不是裸 `str`**：写错一个值 Pydantic 当场报错，而不是让前端拿到一个查不到文案的字符串 |
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

- `step` 是"细粒度当前动作"（类型是 `WorkflowStep`，7 个取值见 4.5），`status` 是"粗粒度对外状态"。前端靠 `status` 是否落在**终态集合**里决定"还要不要继续轮询"：这个集合在代码里是**模块级常量** `TERMINAL_STATUSES: frozenset[WorkflowStatus] = frozenset({WorkflowStatus.SUCCESS, WorkflowStatus.SUCCESS_WITH_WARNINGS, WorkflowStatus.FAILED})`（`app/schemas_workflow.py`，定义在枚举下面）。**接口、前端、测试三处要 import 同一个常量**，不要各自再写一遍 `in (...)` —— 写错一处就会多轮询几秒或永远转下去。
- `TaskStatus`（单个任务，6 个值见 4.3）与 `WorkflowStatus`（整条工作流，10 个值）是**两个枚举**，值域不同，别互相赋值：`WorkflowStatusData.status` 用后者、`TaskBrief.status` 用前者。
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
| `error_code` | `ToolErrorCode \| None` | 否 | 失败时的机器码 5 选 1：`TOOL_NOT_FOUND` / `BAD_PARAMS` / `COLUMN_NOT_FOUND` / `BAD_OPERATION` / `BAD_RESULT`。`ToolErrorCode` 是 `schemas_tool.py` 顶部的 `Literal` 别名（不是裸 `str`）；它与 `app/schemas.py` 的 `ErrorCode` 是**两套码**，别混用（见 §7.2 结尾） |
| `error_message` | `str \| None` | 否 | 失败时的中文说明（直接给前端看） |
| `created_at` | `datetime` | 是 | |

### 4.8 `ChartResult`（`app/schemas_tool.py`，D5）

**A-2 拍板的 5 个必备字段**（接口 6 / 接口 8 / §9.1 分析报告视图三处共用同一份定义，不许各写一份）：

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
> 3. **§9.1 分析报告视图**（前端图表卡）只用 `ChartResult.url` 作为 `<img src>`，用 `ChartResult.title` 做图注，**不拼 URL、不猜文件名**。
>
> 换句话说：`url` 是"给前端用的地址"，`filename` 是"给后端用的钥匙"，两者都在同一个对象里，谁也不用推导谁。
>
> **这条约定已经是"会报错的代码"，不再只是文档里的提醒**（`de86db2` 起）：`ChartResult` 自带一个 `@model_validator(mode="after")`，**构造对象的那一刻**就检查 ① `filename == chart_id + ".png"`、② `url.endswith("/charts/" + filename)`，任一不满足直接抛 `ValueError`。为什么值得写进代码：这三者一旦对不上，前端拿到的 `<img src>` 就是 404，而**图片 404 不会让任何测试变红**（浏览器里只是一张破图），与其等演示时才发现，不如让它当场炸。

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
| `summary` | `str` | 是 | 代码生成的总述（数字按来源规则校验） |
| `sections` | `list[ReportSection]` | 是 | 至少 1 段，通常 3 段 |
| `data_quality_notes` | `list[str]` | 否 | 代码根据 `DatasetProfile` 生成；其文本也纳入数字来源校验 |
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

### 4.12 `WorkflowStatusData` / `TaskBrief`（接口 6 的返回体，`app/schemas_workflow.py`，D3）

**为什么要单独定义一个模型，而不是直接返回 `WorkflowState`**——两个理由，第二个更长远：

1. **体积**：状态接口 1 秒被调一次，必须保持小。用一份**合成**状态量过（6 个任务 / 6 个工具结果各 20 行 / 1 张图 / 3 段报告）：

   | 序列化对象 | 字节数 | 按 1 秒轮询、一次分析按 60 秒算 |
   |---|---|---|
   | `WorkflowState.model_dump_json()` | 14 431 B（14.1 KB） | 多传约 0.8 MB |
   | `WorkflowStatusData.from_state(state).model_dump_json()` | 1 318 B（1.3 KB） | 全程也就约 77 KB |

   比值 ≈ **10.9 倍**。（这是合成数据、不是真实工作流跑出来的，量级参考即可；量法与数字见 §14.5。）

2. **契约稳定**：若直接返回 `WorkflowState`，将来给 `state` 加一个**内部**字段（例如给重试逻辑加个计数器），它会**立刻出现在 API 响应里**——前端莫名多收到一个东西，而且没有任何测试会因此变红。`WorkflowStatusData` 是一张"显式清单"：想让它进状态接口，必须在这里加一行。

字段（13 个）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `workflow_id` | `str` | 主键 |
| `dataset_id` | `str` | 关联的数据集 |
| `question` | `str` | 用户原问题 |
| `status` | `WorkflowStatus` | 粗粒度状态；前端靠 `TERMINAL_STATUSES` 判断是否停止轮询（见 4.6） |
| `step` | `WorkflowStep` | 当前阶段名 |
| `step_index` | `int` | 已完成的阶段数（进度条用） |
| `total_steps` | `int` | 总阶段数 |
| `tasks` | `list[TaskBrief]` | 任务精简列表，默认 `[]` |
| `charts` | `list[ChartResult]` | 已生成的图（**完整 10 个字段**；接口 8 只认其中的 `filename`，见 4.8） |
| `error` | `ApiError \| None` | 顶层失败原因 |
| `created_at` | `datetime` | 创建时间 |
| `finished_at` | `datetime \| None` | 结束时间 |
| `has_report` | `bool` | 是否已有报告可取；**`from_state()` 里是现算的**，见下 |

`TaskBrief`（4 个字段，只给前端要显示的东西）：`task_id` / `description` / `status`（`TaskStatus`）/ `error`。

**`from_state()` 里唯一需要动脑的地方：`has_report` 是现算的**

```python
has_report=state.report is not None,      # 注意：不是抄 state.has_report
```

- `WorkflowState.has_report` 是 `report` 的**派生字段**；派生值天然带一个风险：**某个分支只设了 `report`、忘了同步 `has_report`**，于是前端一直不去取报告，而工作流看上去是成功的。
- 在接口这一层**现算一次**（`state.report is not None`），接口的正确性就不再依赖"每个写 `report` 的地方都记得同步"这个约定。代价是每次多算一次 `is not None`，可以忽略。
- **实测过它确实生效**：把 `state.report = None` 但故意不改 `state.has_report`（保留陈旧值 `True`），`from_state()` 返回的 `has_report` 是 `False`——接口信的是现算值，不是陈旧字段。
- 由此定一条纪律：`state.has_report` 只作为**写入方的便利标记**；任何**读取方**（接口、校验、报告）都不要拿它做判断，一律用 `state.report is not None`。
- 顺带：`tasks` 里**不含 `params`**——用户不需要看见工具参数（那是排查时才看的东西），所以 `TaskBrief.from_task()` 只抄 4 个字段。

### 4.13 模型放在哪些文件里（避免一个文件膨胀成几百行）

| 文件 | 包含的模型 | 负责日 |
|---|---|---|
| `app/schemas.py`（已存在） | `ErrorCode`（8 个码）、`ApiError`、`ApiResponse[T]`、`T`（`TypeVar`） | D1 |
| `app/schemas_dataset.py`（已存在, `44566c6`） | `FileExt`、`ALLOWED_EXTENSIONS`（模块级 `frozenset`）、`SemanticType`（`Literal` 别名，`ALLOWED_EXTENSIONS` 是它的取值集合）、`DatasetSummary`、`Dataset`、`ColumnProfile`、`DatasetProfile`——**共 7 个顶层定义** | D2 |
| `app/schemas_tool.py`（已存在, `de86db2`） | `ToolErrorCode`、`ChartType`（两个 `Literal` 别名）、`ToolResult`、`ChartResult` | D3 |
| `app/schemas_workflow.py`（已存在, `de86db2`） | `TaskStatus`、`WorkflowStatus`、`TERMINAL_STATUSES`（模块级 `frozenset`）、`WorkflowStep`（`Literal`）、`ChartRequest`、`AnalysisTask`、`TaskPlan`、`ValidationIssue`、`ValidationResult`、`ReportSection`、`FinalReport`、`WorkflowState`、`QuestionRequest`、`AnalysisAccepted`（D4 冻结时新增）、`WorkflowStatusData`、`TaskBrief`——**共 16 个顶层定义** | D3–D5 |
| `app/schemas_llm.py`（已存在, D4 冻结时新建） | `LLMToolCall`、`LLMReply`——**共 2 个顶层定义**。`message` 必须带 `reasoning_content` 的校验在这里，理由见 §4.14.2 | D4 |
| `app/tools/registry.py` | `ToolSpec`（**只在这里定义**，见 §6.1）。`schemas_tool.py` 里**没有**它 | D3 |

**`LLMReply` 为什么不放 `schemas_workflow.py`**：判据是 §4.0 的"数据长什么样"。`schemas_workflow.py` 回答的是"**一条工作流**长什么样"，而 `LLMReply` 回答的是"**一次模型调用**返回了什么"——它既不属于工作流也不属于工具，是 `app/llm/` 这个包的边界类型。放进去还会让 `schemas_workflow.py` 被迫承载"模型请求层"的概念，而 D5 的 `charts.py`/`validation.py` 都不需要知道模型调用的形状。

**`ToolSpec` 为什么不放 `schemas_tool.py`**：它的 `func` 字段是"怎么执行这个工具"，属于**工具层怎么被调用**，不属于"数据长什么样"；放进 `schemas_*.py` 还会让模型文件被迫依赖 pandas/`Callable` 的类型注解。文件归属的判据见 §4.0。

**怎么自检这张表**（别靠眼睛——这类偏差的特征正是"看不出来"）：用 `ast.parse` 读两个模型文件，取顶层 `ClassDef`/`Assign`/`AnnAssign` 的名字集合，与上表做**双向**集合差（"代码有、表里没有"和"表里有、代码没有"都要打出来）。只比一个方向会漏掉一半。方法与当次输出见 §14.5。

**不动 `app/schemas.py` 已有的三个模型**——因为它们被 `routes_health.py` 和 `tests/test_health.py` 依赖，改了会破坏第一天的验收。

### 4.14 D4 契约冻结：模型请求层与受理回执（`de86db2` 之后新增）

D4 是本项目**最大风险点**（第 12.2 节），也是唯一需要"AI / BE / QA 三方同时开工"的一天。所以这一天开工前先把**函数签名与数据形状**钉死：三个人对着同一份契约写，谁都不用等谁。本节的每个签名都已在代码里落地（见 §14.7 的基线）。

#### 4.14.1 新增两个模型文件/模型（代码已落地）

| 位置 | 模型 | 字段 |
|---|---|---|
| `app/schemas_llm.py`（**新文件**） | `LLMToolCall` | `id: str`（模型给的调用 id）、`name: str`（**可能是白名单外的名字**）、`arguments: str`（**原始 JSON 字符串，刻意不解析成 dict**） |
| 同上 | `LLMReply` | `message: dict[str, Any]`、`content: str \| None`、`reasoning_content: str \| None`、`tool_calls: list[LLMToolCall]`、`model: str`、`finish_reason: str \| None`、`prompt_tokens/completion_tokens/total_tokens: int \| None`、`elapsed_ms: int`、`attempts: int` |
| `app/schemas_workflow.py` | `AnalysisAccepted` | `workflow_id`（正则 `^[0-9a-f]{32}$`）、`dataset_id`、`status: WorkflowStatus`（默认 `PENDING`）、`created_at: datetime`；另有 `from_state(state)` classmethod |

**`LLMToolCall.arguments` 为什么是 `str` 不是 `dict`**：模型可能给出非法 JSON（第 13 节风险 1 就列了这一条）。解析 + 三层容错是 `planner.py` 的职责，不是"数据形状"的职责；而且**保留原文**才能在重试时把"你的 JSON 不合法：<原文>"附回 Prompt。解析放在 `planner` 里，坏输入就成了它自己的分支，而不是让模型层的构造直接抛异常。

**`AnalysisAccepted` 为什么必须单独定义**：第 8.2 节的示例代码 `response_model=ApiResponse[AnalysisAccepted]` 从 D1 就在用这个名字，但直到 D4 冻结契约时才真正落地 —— **"引用先于定义出现"**正是 §14.5 第 4 条记下的那类偏差（读起来自洽，实际有个洞）。它只带 4 个字段的理由：接口 5 的语义是"已受理"，此刻后台线程可能还没启动；回完整状态会让前端误以为"已经在跑了"。

#### 4.14.2 `LLMReply.message`：把"必须回传 `reasoning_content`"变成构造期约束

这是本阶段**唯一一个"写错就直接 400"**的坑，所以单独成节。

DeepSeek 官方文档（思考模式）原文：

> 携带了 `tools` 参数的请求，在后续所有请求中，必须完整回传 `reasoning_content` 给 API —— 即使该轮模型未实际进行工具调用。若您的代码中未正确回传 `reasoning_content`，API 会返回 400 报错。

**注意后果是 400 而不是降级**。而 openai SDK 的 `response.choices[0].message` 之所以能直接 `messages.append(...)` 回传，是因为 SDK 把未知字段塞进了 `model_extra` 并原样序列化 —— 这是 **SDK 的版本相关行为，不是 API 契约**（换版本可能静默丢掉，而丢掉的后果是线上 400）。

因此契约规定两件事：

```python
# ① client.chat() 内部：手工拼 message，不把 SDK 对象透传给上层
message = {
    "role": "assistant",
    "content": choice.message.content,
    "tool_calls": [tc.model_dump() for tc in (choice.message.tool_calls or [])],
}
if reasoning := getattr(choice.message, "reasoning_content", None):
    message["reasoning_content"] = reasoning     # ← 少了这行，下一轮 400

# ② 上层回填：原样 append，不做任何加工
messages.append(reply.message)
```

`LLMReply` 上的 `@model_validator(mode="after")` 把这条约定变成**构造时就拦得住**的约束，检查三件事：`message["role"]` 必须是 `"assistant"`；拿到了 `reasoning_content` 就**必须**出现在 `message` 里；`tool_calls` 数量必须与 `message["tool_calls"]` 对得上。

**为什么放模型里而不是写进 client 的注释**：注释拦不住改动，而这个校验在**任何**构造 `LLMReply` 的地方都会跑（包括将来新增的调用点）。

#### 4.14.3 D4 要实现的五个函数签名（三方照此开工）

```python
# app/llm/client.py                                        [D4][AI]
def chat(messages: list[dict], tools: list[dict] | None = None) -> LLMReply: ...
    # 未配置 LLM → 抛 LLMNotConfigured（调用方转兜底，不是 500）
    # 超时/连接错误/429/5xx → 按 settings.llm_max_retries 重试，间隔 time.sleep(1.5 * attempt)
    # 401/402/400/422 → 不重试，直接抛 LLMCallError（重试没有意义，只是白花时间）

# app/agents/planner.py                                     [D4][AI]
def plan(question: str, profile: DatasetProfile, max_steps: int = 6) -> TaskPlan: ...
    # LLM 路径：调 client.chat(messages, tools=to_openai_tools())，三层解析（json.loads → 正则抽 {...} → 重试）
    # 任何一步失败（未配置/超时/JSON 坏/工具名全非法）→ 退回 plan_with_rules()，source="rule_fallback"
    # 恒不抛异常：这个函数必须永远返回一个可执行的 TaskPlan

# app/agents/orchestrator.py                                [D4][BE]
def run_workflow(state: WorkflowState) -> None: ...
    # 原地更新 state；唯一有权改 status/step/retry_count 的地方；不 import FastAPI；不直接调 LLM

# app/core/background.py                                    [D4][BE]
def submit(state: WorkflowState, tasks: BackgroundTasks) -> None: ...
def _run_safely(state: WorkflowState) -> None: ...        # except BaseException 兜底 + finally 收尾

# app/api/routes_analysis.py                                [D4][BE]
@router.post("/analysis", status_code=202, response_model=ApiResponse[AnalysisAccepted])
def start_analysis(req: QuestionRequest, background: BackgroundTasks) -> ApiResponse[AnalysisAccepted]: ...
```

**`planner.plan()` 恒不抛异常**这条要写清楚理由：它是"永远有规则兜底"（第 13 节风险 1 第 ⑤ 条）在代码层面的落点。模型彻底失败时系统仍然可用 —— 这是 D4 的验收项之一，所以兜底不能是"尽量"，必须是"函数签名保证"。

#### 4.14.4 D4 新增三个配置字段（已进 `app/config.py`，见 §4.0.1）

`charts_dir`（`data/charts`，D5 用）、`max_workflow_seconds`（`180`）、`tool_call_limit`（`20`），外加计算属性 `settings.charts_path`。**一次补齐**的理由：§4.0.1 表定过纪律"写配置相关代码前先在表里查一下，不要自己造名字相近的字段"—— 造出来还不报错，只是永远不生效。

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
| LLM 调用超时 / 连接错误（HTTP 429、5xx） | 是 | `settings.llm_max_retries`（默认 2，不含首次） | 每次之间 `time.sleep(1.5 * attempt)`，退避。**只有这一层重试**：`OpenAI(..., max_retries=0)`，见下方"为什么钉死 SDK 重试" |
| LLM 返回的 `tool_calls` 参数不是合法 JSON | 是 | 2 | 第 1 次重试时把"你的 JSON 不合法"附回 Prompt |
| 报告校验发现编造数字 | 是，**重写报告** | 1 | 附上 `ValidationResult.issues` 让模型改；再不过 → `SUCCESS_WITH_WARNINGS` |
| 整条工作流的工具调用总数 | — | `TOOL_CALL_LIMIT = 20` | 超过就 `FAILED`（防模型/重试组合成死循环） |
| 整条工作流总时长 | — | `max_workflow_seconds = 180` | 轮询接口按 `heartbeat_at` 判定超时并落 `FAILED` |

`WorkflowState.retry_count` 记录**所有**重试次数之和，上限 6；达到上限后不再重试任何东西，直接进入收尾流程。

**为什么钉死 SDK 重试（`max_retries=0`，D-25）。** `OpenAI(...)` 客户端自己也会重试，
外面这个循环也会重试，两层是**叠乘**的：传 `settings.llm_max_retries`（默认 2）时，
单次 `chat()` 最坏 `3 × 3 = 9` 次 HTTP，再叠上 SDK 的指数退避
（`min(0.5 * 2^n, 8.0)` 秒，且尊重服务端的 `Retry-After`，最长 60 秒）约 9 分钟；
一条工作流最多 5 次 `chat()`（planner 3 + 报告 2）→ 最坏 45 次请求、约 45 分钟。

而 `max_workflow_seconds`（默认 180）只在阶段与任务**之间**检查
（`orchestrator` 的 `_stop_reason`），**打断不了一次正在阻塞的 HTTP 调用** ——
也就是说这套叠乘能让实际耗时远超自己的预算。所以把 SDK 那层钉成 0，只留外层：
单次 `chat()` 最坏 `1 + llm_max_retries` 次 HTTP（默认 3 次），退避 1.5s / 3.0s，约 3 分钟。

这条曾经是**文档与代码不一致**：D4 起本表就写着"重试 `llm_max_retries`"，
而代码传的是 `max_retries=settings.llm_max_retries`（实际重试 8 次）。
D6 收口阶段由 Lead 拍板收敛，记在 `docs/DECISIONS.md` D-25；
`tests/test_llm_client.py` 里有两条用例专门盯住这个常量（配置改成 5 也不许传给 SDK）。

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
| 5 | `compare_groups` | 组间对比：对同一指标在两组（或某分类的各组）之间比较均值/总和差异 | `group_by: str`（必填，单列）、`metric: str`（必填，数值列）、`agg: Literal["mean","sum","count","min","max","median"]`（默认 `mean`）、`top_k: int`（默认 10，1–30） | `{"metric":"客单价","columns":["地区","客单价_mean","差值"],"rows":[{"地区":"华东","客单价_mean":120.5,"差值":15.2}],"row_count":6}` |
| 6 | `correlation` | 相关系数：数值列两两之间的 Pearson 相关系数矩阵 | `columns: list[str]\|None`（默认全部数值列，至少 2 列） | `{"columns":["销售额","利润"],"matrix":[[1.0,0.62],[0.62,1.0]],"pairs":[{"x":"销售额","y":"利润","r":0.62}]}` |
| 7 | `time_series_summary` | 时间序列汇总：按时间粒度（日/周/月）汇总指标，可选算环比 | `date_column: str`（必填）、`metric: str`（必填）、`freq: Literal["D","W","M"]`（默认 `"M"`）、`agg: Literal["sum","mean","count","min","max","median"]`（默认 `"sum"`）、`include_growth: bool`（默认 `False`） | `{"columns":["period","销售额_sum","环比"],"rows":[{"period":"2025-01","销售额_sum":1000.0,"环比":null}],"row_count":12}` |

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

## 7. API 设计（8 个必需接口 + D6-SQLite 追加的 2 个只读历史接口）

> **编号沿用既有风格**：接口 **1–8** 是原计划的 8 个必需接口；接口 **9 / 10** 是 D6-SQLite 交付时追加的两个**只读**历史接口（`GET /api/workflows/history`、`GET /api/workflows/history/{workflow_id}`），实现在 `app/api/routes_workflows.py`。下表是**完整的编号总表**（1–10），下方 7.1 的既有说明里凡提到"8 个接口"均指 1–8。

统一约定：
- 全部返回 `ApiResponse[T]` 外壳（已存在于 `app/schemas.py`）：成功 `{"ok":true,"data":{...},"error":null}`，失败 `{"ok":false,"data":null,"error":{"code","message","detail"}}`。
- **HTTP 状态码表达 HTTP 语义，并且必须是真的；`error.code` 表达业务语义。两者不冲突，同时给。**（A-1 拍板，推翻 v1.0 的"业务错误一律 200"——原方案的理由不成立，代价却真实：见下方"为什么"）
- **前端只有一套解析逻辑**：用 `fetch`（不是 axios），**4xx/5xx 不会抛异常**，`.json()` 照样解析出 `{"ok":false,...}`。所以前端的写法永远是这两行，不用分支：
  ```js
  const resp = await fetch(url, {...});
  const body = await resp.json();               // 4xx/5xx 也能正常解析
  if (!resp.ok || !body.ok) { showError(body.error); return; }   // 只有这一套
  ```
- **框架抛的异常也已经统一成同一个外壳**（`app/main.py` 的四个 exception handler：`AppError` / `StarletteHTTPException`（含 404、405）/ `RequestValidationError`（422）/ 兜底 `Exception`（500），`tests/test_error_format.py` 守护）——这条是 460cf8c 落实的，本节所有接口都建立在它之上。
  - 框架异常的 `error.code` 由 `app/main.py` 的 `_FRAMEWORK_STATUS_CODES` 表决定：`404 → NOT_FOUND`、`405 → METHOD_NOT_ALLOWED`、其余 → `BAD_REQUEST`（422 走这条）。**405 不归 `BAD_REQUEST`**：前端按 `code` 选文案，把"方法不对"说成"参数写错"会把排查方向带偏。
  - 405 响应**必须转发 `Allow` 头**（RFC 7231 §6.5.5 要求），所以 handler 里是 `JSONResponse(status_code=exc.status_code, headers=exc.headers, ...)`——手写 `return JSONResponse(...)` 很容易把 `headers` 丢掉，这也是"禁止绕过统一外壳"的原因之一。

#### 7.0 HTTP 状态码 → 错误码映射表（**逐行照此实现，不许自创**）

| 情况 | HTTP 状态 | `error.code` |
|---|---|---|
| 请求成功（同步完成） | 200 | — |
| 请求已受理、后台执行（`POST /api/analysis`） | **202** | — |
| 请求体字段不合法（`RequestValidationError`：问题为空/过长、`max_steps` 越界、缺字段、类型不对） | **422** | `BAD_REQUEST` |
| 业务参数非法（D4 起只剩上传接口的文件解析失败走这条） | 400 | `BAD_REQUEST` |
| 资源不存在（路径、`dataset_id`、`workflow_id`） | 404 | `NOT_FOUND` |
| 方法不对 | 405 | `METHOD_NOT_ALLOWED` |
| 扩展名不在白名单 / 文件伪装 | 415 | `UNSUPPORTED_FILE` |
| 文件超过 `max_upload_bytes` | 413 | `FILE_TOO_LARGE` |
| 文件内容解析失败（坏编码、空文件、只有表头、超宽） | 400 | `BAD_REQUEST` |
| 外部依赖不可用（模型未配置且无法兜底） | 503 | `SERVICE_UNAVAILABLE` |
| 工作流超时 | **200** | `WORKFLOW_TIMEOUT` |
| 服务器内部未预期错误 | 500 | `INTERNAL_ERROR` |

**这张表里有三行需要解释**：
- **413 / 415 是 HTTP 本来就有的语义**：413 Payload Too Large、415 Unsupported Media Type。"用户传了一个 70 MB 的文件被拒绝"用 200 回答等于向 HTTP 层撒谎 —— 打开 F12 看到一片 200，排查时必须点开每个响应体才知道发生了什么；413 一眼就知道。
- **`WORKFLOW_TIMEOUT` 走 200**：请求本身完全成功（202 已受理、轮询到了终态），"超时"是**这次分析的结果**，不是请求失败；它属于业务语义，所以放 `error.code`。
- **503 的含义是"服务现在做不了、以后可能行"**。第一版只有一处：模型未配置、连规则兜底也用不了，所以 `POST /api/analysis` 无法受理。**v1.1 更新：这里改用专用错误码 `SERVICE_UNAVAILABLE`**（`raise AppError(ErrorCode.SERVICE_UNAVAILABLE, ...)`，状态码由 `ERROR_STATUS` 查表得到 503），不再借用 `BAD_REQUEST` —— 借用的后果是前端按 `code` 分流时会提示"请检查你的输入"，把"我们的依赖挂了"说成"你的请求写错了"。前端此时显示 `message`（"服务暂时不可用，请稍后重试"）+ "重试"按钮即可。
  **D4 实测补充：这个唯一场景在 D4 之后不可达了。** D4 的 `planner.plan()` 是签名级保证不抛异常的（任何失败都退回 `plan_with_rules()`），而规则兜底只依赖 `DatasetProfile`、不依赖任何外部服务，所以"无法兜底"这个前提不成立 —— 未配模型时接口返回 **202**、工作流随后 `SUCCESS`（D4 验收标准 ①）。**码保留、表不动**：它仍然表达一个真实的语义 —— D5 之后"连兜底也用不了"的候选场景已经出现两个（数据集在进程内存里丢了、报告生成也失败了），到那时直接用它，不必再改表。详见 §7.0 接口表下方的注 ②。

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
| 5 | 提交分析 | `POST /api/analysis` | `QuestionRequest{dataset_id,question,max_steps?}` | **202** + `{workflow_id,status:"PENDING",dataset_id,created_at}`，**立即返回，不等分析** | **404** `NOT_FOUND`（数据集不存在）、**422** `BAD_REQUEST`（问题为空/过长、`max_steps` 越界、缺字段 —— 由 `QuestionRequest` 的字段约束在进函数前拦下，见下方注 ①）。**503 在 D4 已不可达**，见下方注 ② |
| 6 | 查询工作流状态 | `GET /api/workflows/{workflow_id}` | 无 | **200** + `{workflow_id,status,step,step_index,total_steps,question,dataset_id,tasks:[{task_id,description,status,error}],charts:[ChartResult],error,created_at,finished_at,has_report}` —— **不含 `report`**（报告可能很大，单独接口取） | **404** `NOT_FOUND`、**200** `WORKFLOW_TIMEOUT`（轮询时判定超时并落 `FAILED`，此时 `error.code=WORKFLOW_TIMEOUT`）、**500** `INTERNAL_ERROR`。注意：**工作流自身 `FAILED`（含超时）仍然是 HTTP 200** —— 查询请求成功了，失败的是被查询的工作流 |
| 7 | 获取报告 | `GET /api/workflows/{workflow_id}/report` | 无 | **200** + `{report:FinalReport, validation:ValidationResult, results:[ToolResult], llm_used:bool}` | **404** `NOT_FOUND`（没有这个工作流）；报告还没生成 → **400** `BAD_REQUEST`（`message="报告尚未生成"`，`detail` 带当前 `status`） |
| 8 | 获取图表 | `GET /api/workflows/{workflow_id}/charts/{filename}` | 无 | **200** + PNG 二进制（`Content-Type: image/png`） | **404** `NOT_FOUND`（文件不存在）、**400** `BAD_REQUEST`（文件名含 `/`、`\`、`..` 或不是 12 位 hex + `.png` —— 这是"非法输入"，不是"资源不存在"，所以是 400 不是 404） |
| 9 | 历史归档列表（**D6-SQLite 新增**） | `GET /api/workflows/history` | 查询参数 `limit`（默认 20，`ge=1, le=100`）、`offset`（默认 0，`ge=0`） | **200** + `HistoryListData{items:[HistoryItem], total, limit, offset, enabled}`。`HistoryItem` = `workflow_id / question / status / created_at / finished_at / has_report / llm_used`。**只读 SQLite，不读内存 registry**（见 8.3.1）。归档关闭（`DB_PATH` 为空）时 `enabled=false` + 空列表，**仍然是 200** —— 用户主动关了一个功能，不是他请求错了。排序在库里定死（`finished_at DESC NULLS LAST, created_at DESC`），接口层不再排 | **422** `BAD_REQUEST`（`limit` / `offset` 不符合查询约束）；**500** `INTERNAL_ERROR`（底层读库/schema 操作失败；正常关闭归档与空库路径为 200）。关闭归档时 `list_history()` 返回空列表、`enabled=false`；启用时查询并返回记录。底层 `HistoryStore` 操作仍可能抛 `HistoryStoreError`，由统一异常处理器转为 500。**注意路由声明顺序**：本路由必须写在 `GET /api/workflows/{workflow_id}` **之前**，否则 `/workflows/history` 会被后者接走、`workflow_id="history"` → 404。`tests/test_history_api.py` 有一条用例专门钉这个 |
| 10 | 历史归档详情（**D6-SQLite 新增**） | `GET /api/workflows/history/{workflow_id}` | 无 | **200** + `HistoryDetailData{item:HistoryItem, report}` —— `report` 是**归档那一刻的报告正文**，没归档报告时为 `None`。只读 SQLite | **404** `NOT_FOUND`（库里没有这个 `workflow_id`）。**它不会把记录塞回内存 registry**，所以对同一个 ID，接口 10 可能 200 而接口 6/7 是 404 —— 这是设计，不是不一致（见 8.3.1 的禁令） |

> **读表提醒（v1.1）**：`NOT_FOUND` 在接口 3/5/6/7 都是 **404**，`UNSUPPORTED_FILE` 是 **415**，`FILE_TOO_LARGE` 是 **413**，`SERVICE_UNAVAILABLE` 是 **503**，`METHOD_NOT_ALLOWED` 是 **405** —— 同一个错误码在不同接口里的状态码是**一致的**，因为它由 `app/errors.py` 的 `ERROR_STATUS` 映射表（§7.3）统一决定，不在路由里逐处指定。这就是 A-1 要解决的"同一个 NOT_FOUND 有时 404 有时 200"的根源。**框架抛的 405 也走同一个 `code`**（`app/main.py` 的 handler 里 `METHOD_NOT_ALLOWED`），所以前端看到 405 时 `body.error.code` 是 `"METHOD_NOT_ALLOWED"`，不是 `"BAD_REQUEST"`。

**注 ①（D4 实测修正）：接口 5 的"400 问题为空/过长"实际上到不了。** 原始设计写的是 400，但 `QuestionRequest.question` 上有 `min_length=2, max_length=500`、`max_steps` 上有 `ge=1, le=8`，Pydantic 在**进入函数体之前**就抛 `RequestValidationError`，被 `app/main.py` 的 handler 翻成 **422 + `BAD_REQUEST`**。实测七种输入（1 字符 / 空串 / 501 字符 / `max_steps=0` / `max_steps=99` / 缺字段 / 不存在的 `dataset_id`）里，前六种全是 422，只有"数据集不存在"是 404。**这不是 bug，是更早拦下**：400 那条分支在当前模型约束下是死代码，所以文档照实测改。**保留 400 这个码**（§7.0 表里它仍由上传接口的文件解析失败在用）。

**注 ②（D4 实测修正）：接口 5 的 503 在 D4 起不可达。** 原始条件是"模型未配置**且无法兜底**"，而 D4 的 `planner.plan()` 是**签名级保证不抛异常**的（内部 `except Exception` 兜到规则兜底），而规则兜底只依赖 `DatasetProfile`（内存里的一个对象）、不碰任何外部服务，所以"无法兜底"这个前提在 D4 不可能成立 —— 实测未配模型时 `POST /api/analysis` 返回 **202**，工作流随后 `SUCCESS`、`report.llm_used=false`（正是 D4 验收标准 ①）。**503 这个码本身保留**（§7.3 的表不动）：到 D5，"连兜底也用不了"的候选场景已经出现了两个（数据集在进程内存里丢了 → 接口 404；报告生成也失败 → `_minimal_report` 仍能出一段说明），届时直接用这个现成语义，不必改表。**当前结论：D4/D5 的接口 5 只有 404 与 422 两种错误。**

### 7.1 我在你给的 8 个接口之外做的两处调整（已于第 14 节拍板通过）

> 本节讲的是**原计划 8 个接口**范围内的两处调整（接口 1–8）。D6-SQLite 之后又多出接口 9 / 10，那两个不在这里讨论，见 §7 总表与 §8.3.1。

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

### 7.1.1 §9.1 分析报告视图（前端图表卡）的对齐写法

```js
// 唯一正确写法：url 直接用后端给的字段，不拼字符串
for (const c of state.charts) {
  img.src = c.url;             // 后端已拼成 /api/workflows/{workflow_id}/charts/{filename}
  img.alt = c.title;
}
```

理由（A-2 指出的三处不一致）：如果前端自己拼 `/charts/` + `c.filename`，一旦接口 8 的路径前缀调整（例如挂到 `/api/v1/`），前端会静默产出 404 的图片链接，而且**测试很难覆盖**（HTML 属性不会被 pytest 检查）。把拼接责任放在后端一处，前端只做渲染。

### 7.2 错误码补充（写在 `app/schemas.py` 的 `ErrorCode` 里，D2）

现在（v1.1）是 **8 个，已全部落地**（`app/schemas.py` 的 `ErrorCode`，顺序与仓库一致）：

```
BAD_REQUEST / NOT_FOUND / METHOD_NOT_ALLOWED / UNSUPPORTED_FILE
FILE_TOO_LARGE / SERVICE_UNAVAILABLE / WORKFLOW_TIMEOUT / INTERNAL_ERROR
```

| 错误码 | HTTP | 什么时候用 |
|---|---|---|
| `BAD_REQUEST` | 400（框架校验另见 422） | 业务参数非法（上传文件解析失败、只有表头、坏编码；问题过长/为空走 422，见 §7.0 注 ①） |
| `NOT_FOUND` | 404 | 数据集 / 工作流 / 图表不存在 |
| `METHOD_NOT_ALLOWED` | 405 | HTTP 方法不对（`app/main.py` 把框架的 405 翻译过来）。**v1.1 新增**：原先归 `BAD_REQUEST`，同样会把"你请求的方式错了"说成"你的参数写错了" |
| `UNSUPPORTED_FILE` | 415 | 扩展名不在白名单 / 文件伪装 |
| `FILE_TOO_LARGE` | 413 | 超过 `max_upload_bytes` |
| `SERVICE_UNAVAILABLE` | 503 | 依赖不可用（模型未配置且无法兜底）。**v1.1 新增**，理由见 §7.0 的 503 说明 |
| `WORKFLOW_TIMEOUT` | **200** | 工作流超时（这**是**结果，不是请求失败） |
| `INTERNAL_ERROR` | 500 | 服务器内部未预期错误，只由兜底 handler 产生 |

其余（工具类错误 `TOOL_NOT_FOUND` / `BAD_PARAMS` / `COLUMN_NOT_FOUND` / `BAD_OPERATION` / `BAD_RESULT`）留在 `ToolResult.error_code` 这一层，**不上升到 HTTP 错误码**——因为它们不是"请求失败"，而是"任务失败"，工作流仍然能返回 `SUCCESS_WITH_WARNINGS`。**两组码不要混用**：`ErrorCode` 是枚举、约束 HTTP 状态码；`error_code` 是字符串、只在工具结果里出现。

### 7.3 `ERROR_STATUS` 映射表（`app/errors.py`，D2 —— A-1 的落地点）

```python
# app/errors.py —— 与仓库里已实现的版本逐行一致（D2 已落地，勿照旧稿重写）
DEFAULT_MESSAGE: dict[ErrorCode, str] = {          # message 兜底，避免出现空 message
    ErrorCode.BAD_REQUEST: "请求不合法",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.METHOD_NOT_ALLOWED: "请求方法不支持",
    ErrorCode.FILE_TOO_LARGE: "文件太大",
    ErrorCode.UNSUPPORTED_FILE: "文件类型不支持",
    ErrorCode.SERVICE_UNAVAILABLE: "服务暂时不可用，请稍后重试",
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
    ErrorCode.BAD_REQUEST:          400,
    ErrorCode.NOT_FOUND:            404,
    ErrorCode.METHOD_NOT_ALLOWED:   405,
    ErrorCode.FILE_TOO_LARGE:       413,
    ErrorCode.UNSUPPORTED_FILE:     415,
    ErrorCode.SERVICE_UNAVAILABLE:  503,
    ErrorCode.WORKFLOW_TIMEOUT:     200,   # 请求成功、业务结果超时（§7.0 最后几行的例外）
    ErrorCode.INTERNAL_ERROR:       500,
}
```

**三条实现细节（照抄代码时会用到）**：
1. **清洗发生在 handler，不在 `AppError.__init__` 里**：`app/main.py` 的 `app_error_handler` 写的是 `detail=sanitize_detail(exc.detail) if exc.detail else request.url.path`。好处是"清洗"只有一道关口，4 个 handler（`AppError` / `HTTPException` / `RequestValidationError` / 兜底 `Exception`）用的是同一个函数，不会出现"某个路径忘了洗"。
2. **不要手写 422 的映射**：`ERROR_STATUS` 里 `BAD_REQUEST → 400`，而框架的 `RequestValidationError` 由 `app/main.py` 显式返回 **422**（与 §7.0 表一致）。这就是"同一个 `error.code=BAD_REQUEST` 可以配两个状态码"的现象，属于 A-1 拍板的刻意设计，不是 bug。
3. **`status_code` 参数现在没有调用点了（这是好事，别删）**：v1.0 时 503 需要 `raise AppError(ErrorCode.BAD_REQUEST, "...", status_code=503)` 来借状态码；v1.1 加了 `SERVICE_UNAVAILABLE → 503` 之后，**每个错误码都能查表得到正确的状态码**，第 4 个参数就空了出来。保留它的理由：将来真出现"同一个 `code` 要配两个状态码"的场景（例：上传接口的坏编码想给 415 而不是 400），不必再改构造函数签名、不必再改调用方。**默认不传，传之前先想清楚为什么不给它一个专用 `code`。**

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
# status_code=202：语义是"已受理"，不是"已完成"（§7.0 状态码表）
@router.post("/analysis", status_code=202, response_model=ApiResponse[AnalysisAccepted])
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

> **第 14 节决策 3 已拍板：D6 只做 SQLite 持久化，SSE 明确标注为"不做"。** 下表第 1 行（SQLite）**已经交付**（见下方 8.3 的"已交付"记录与 8.3.1 / 8.3.2），第 4 行（SSE）从"可选"降为"明确不做，只作为将来的方向记录"。**触发条件 1 的第一半（"重启后仍要能查到历史工作流"）已由这次交付满足；第二半（`--workers > 1`）仍然没有满足** —— 见 8.3.2 第 3 条。

**已交付（D6-SQLite，D6 之后补做，非 D6 当天完成）**：表 1 行的"历史归档"部分已按如下方式落地，**与下表原始描述有出入的地方以本节为准**：

| 项 | 原始设计（本表第 1 行） | 实际实现 |
|---|---|---|
| 库路径 | `data/app.db` | **`data/db/app.db`**（按仓库已有的 `.gitignore` 规则选址：`data/db/*` 早已被挡，`data/app.db` 得新加一条规则） |
| 访问方式 | "已装的 SQLAlchemy" | **标准库 `sqlite3`，零新依赖**（仓库**从来没装过** SQLAlchemy，`requirements.txt` 里也没有任何数据库包） |
| 连接模型 | 未写 | **短连接**（每次操作开一个连接、用完关）+ 进程内**一把写锁**，不是连接池 |
| 写入语句 | 未写 | `INSERT ... ON CONFLICT(workflow_id) DO UPDATE`（同一 `workflow_id` 重复归档是 UPSERT，不是插两行） |
| 表 | "加 1 张表" | 1 张表 `workflow_history`，**一个工作流一行**（不是"每个状态一行"） |
| 读路径 | "启动时按 `workflow_id` 从库里加载，`registry` 退化为缓存" | **没做，也明确不做**（见 8.3.1 的禁令）。读路径仍然只有内存，库只服务于"历史列表 / 历史详情"两个只读接口 |

代码位置：`app/data/history.py`（`HistoryStore`：`ensure_schema()` / `archive()` / `get()` / `list_recent()` / `count()`）、`app/core/archive.py`（`archive_if_terminal()`，**唯一写入口**）、`app/config.py` 的 `db_path`。

| 触发条件（满足任一） | 升级动作 | 学习成本 |
|---|---|---|
| ~~需要 `--workers > 1`，或~~ 服务重启后仍要能查到历史工作流 | ✅ **已交付（D6-SQLite）**：落 SQLite 归档表（`data/db/app.db`，**标准库 `sqlite3`**），终态后 `archive_if_terminal()` 写一条；只读历史接口从库里读。**注意这只解决了"查历史"，没有解决 `--workers > 1`** —— 完整的那一步（读路径也改库、`registry` 退化为缓存）**没有做** | 低：加 1 张表 + 2 个函数 |
| 分析经常超过 60 秒、或想同时跑多个分析而不抢占 | 引入**线程池 + 队列**（`concurrent.futures.ThreadPoolExecutor(max_workers=2)`），`submit` 改成入队，`status` 加 `QUEUED` | 低：标准库，不加依赖 |
| 需要多台机器 / 需要任务不丢 | 才考虑 Celery + Redis（或 RQ + Redis） | 高：多一个进程、一个中间件、一套配置；个人项目**不建议** |
| ~~需要"边跑边看"（不用轮询）~~ | ~~加 SSE~~ —— **本项目不做**（见第 14 节决策 3：1 秒轮询已够用，收益小、学习成本不低，两个都做会挤掉 D5 校验时间）。方向记在这里，留待将来 | — |

**升级铁律**：上面每一条都必须**先改本文档第 8.3 节**（写清触发条件已满足的证据），再动代码。没到触发条件就实现，属于过度工程。

#### 8.3.1 SQLite 的读写路径约定（决策 3 的第 ① 点补充，必须遵守）

**以下四条已按此实现**（D6-SQLite 落地后仍然是这四条，一条没改），映射到真实函数名：

```
① 内存态唯一事实来源
   `registry._workflows`（`app/core/registry.py`）仍是唯一事实来源；
   SQLite 只做历史归档写入，**任何接口都不许"内存没有就 fallback 查库"**。

② 写只写终态
   唯一写入口是 `app/core/archive.py` 的 `archive_if_terminal(state) -> bool`
   （返回"这次到底写没写"）。它内部用 `app/data/history.py` 的
   `HistoryStore.archive(record)` 做 `INSERT ... ON CONFLICT(workflow_id) DO UPDATE`（UPSERT）。
   写失败只记 warning（`logging.warning`），**绝不影响工作流收尾**。

③ 接口只读内存
   `GET /api/workflows/{id}`（接口 6）、`GET /api/workflows/{id}/report`（接口 7）、
   `GET /api/workflows/{id}/charts/{filename}`（接口 8）**只读内存 registry**。

④ 历史列表只读库 / 禁止 fallback 复活
   `GET /api/workflows/history`（接口 9）与 `GET /api/workflows/history/{workflow_id}`（接口 10）
   走 `get_history_store()` 只读 SQLite；库里的老 ID **不会**被塞回内存 registry。
```

**明确禁止**："有时读内存、有时读库"的混合读路径（例如"内存里没有就 fallback 去查库，查到了再塞回内存"）。理由：那会引入最难查的一类 bug——同一个 `workflow_id` 在两个地方状态不一致、`has_report` 一会儿 true 一会儿 false、后台线程还在原地改的对象已经不在字典里。**宁可"重启后查不到这个 ID（404），但历史列表里能看到"，也不要"半活的复活对象"。**

对外表现因此是两条清晰的路：
- `GET /api/workflows/{old_id}`（重启后）→ **404 + `NOT_FOUND`**，`detail` 写"该工作流已不在内存中（服务可能已重启），历史记录见历史列表"；
- `GET /api/workflows/history` → **已交付**（不再是"可选"）：从 SQLite 读归档记录（`workflow_id / question / status / created_at / finished_at / has_report / llm_used`），分页参数 `limit`（默认 20，1–100）/ `offset`（默认 0），**只读展示，不提供"继续运行"**；`GET /api/workflows/history/{workflow_id}` 额外还原归档的报告正文。

#### 8.3.2 已知限制（必须同时写进 README，不许让人误以为"持久化 = 断点续跑"）

1. **SQLite 存的是"历史归档"，不是"断点续跑"**：服务重启后内存态丢失，**正在运行中的工作流不会恢复，也不会被标成失败**——它只是从内存里消失（前端轮询到 404 就提示"服务已重启，请重新提交"）。
2. **重启后历史记录仍在**：**已做到**（D6-SQLite）。做法是：工作流进入终态时 `archive_if_terminal()` 把一条记录 UPSERT 进 SQLite（`data/db/app.db` 的 `workflow_history` 表，一个 `workflow_id` 一行），所以进程死了、内存 dict 清空了，**库里的行还在**；下一个进程启动后 `GET /api/workflows/history` 从库读，就能看到这些旧记录。但**报告接口不提供对这些旧 ID 的服务**（读路径只有内存，见 8.3.1）——报告正文是在写入时**一起归档进库的**，所以只有"历史详情"接口能拿到它，`GET /api/workflows/{old_id}/report` 仍然是 404。
3. **仍然只能单进程**：SQLite 不会让 `--workers 4` 变安全（内存字典仍然分裂）；要支持多进程必须先走 8.3 第 1 行的完整改造（读路径也改库），那是另一个决定。
4. **归档是"尽力而为"**：写库失败只记 warning，不让工作流失败——因为工作流的成功与否不该由归档决定。

---

## 9. 前端方案（`app/web/`，原生 HTML + CSS + JS）

### 9.1 页面结构（v1.2 企业级工作台：顶栏 + 侧边导航 + 6 个视图）

v1.1 是"单列 6 张卡片从上往下堆"（上传 → 概览 → 提问 → 进度 → 图表 → 报告），
v1.2 把它升级成**企业级工作台**：顶部品牌栏固定，左侧导航切换视图，主内容区一次只显示一个视图。
**6 个视图的 id 与元素全部保留 v1.1 的 17 个必需 id**（`tests/test_frontend.py:35-53` 钉住），
所以这是一次**纯前端重组**：没有新增接口、没有改后端、没有改任何一份契约。

| 视图 | `data-view` | 元素 | 调用的接口 |
|---|---|---|---|
| 工作台 | `workbench` | 上传卡（`label.dropzone#dropzone[for=file]` 包住 `input#file` + `button#upload` + `p#upload-state`，**支持点击与拖拽两种方式**）、数据概览卡（`p#dataset-summary` + `div#kpi-grid`）、「开始一次分析」卡、**最近分析任务**卡（`div#recent-tasks`） | `POST /api/datasets/upload`、`GET /api/health`、`GET /api/workflows/history` |
| 数据集 | `dataset` | 字段结构 `ul#columns`、数据预览 `table#preview`、数据质量 `ul#missing-top3` + `ul#profile-warnings` + `p#dedup-note` | `GET /api/datasets/{id}` + `GET /api/datasets/{id}/profile` |
| 市场分析 | `analysis` | `p#analysis-context`（显示本次用的数据集）、`textarea#question` + 4 个示例问题按钮、`button#ask`；执行过程卡：`ol#steps`（六阶段面板）、`span#elapsed`、`span#workflow-state`、`ul#task-list`、`div#workflow-error` | `POST /api/analysis` + `GET /api/workflows/{id}`（每 1000 ms） |
| 分析报告 | `report` | 图表卡 `div#charts`（每个 `<img src="{ChartResult.url}">` + 标题 + `chart_type`/`point_count` 元信息）、报告卡 `div#report`（标题 + `summary` + 各 `section` + 数据质量提示 + 校验横幅） | `GET /api/workflows/{id}/report`（终态后请求一次）；图片由 `GET /api/workflows/{id}/charts/{filename}` 直接返回 |
| 历史任务 | `history` | `div#history-list`（接口 9 的归档列表）、`span#history-meta`、`button#history-refresh`；详情卡 `section#history-detail-card`（`div#history-detail`） | `GET /api/workflows/history` + `GET /api/workflows/history/{id}` |
| 设置（只读） | `settings` | `table#settings-health`（`/api/health` 的真实字段）+ 一张说明卡：**后端没有任何写配置接口**，所以这里不摆放点了没反应的开关 | `GET /api/health` |

**导航与视图路由**：侧边导航 6 项都是 `<a href="#/xxx" data-view="xxx">`，靠 `hashchange` 切视图
（`app.js` 的 `VIEWS` 表 + `DEFAULT_VIEW`）。用 hash 而不是 History API 的原因：
刷新页面、前进后退、把地址粘给别人都能落到同一个视图，而**不需要服务端加任何路由**
（`GET /` 仍然只返回 `index.html` 一个文件）。

**只有真实存在的能力才做成入口**（这一条是硬约束，不是审美）：
- **不做「最近数据集」列表** —— 后端没有 `GET /api/datasets`，数据集只存在服务进程内存里，
  刷新页面就拿不回旧的 `dataset_id`。工作台那张卡明写了这件事，而不是放一个永远空的列表。
- **不做报告导出** —— 后端没有导出接口，也不假装有。
- **不做设置项的开关** —— 后端没有任何写配置的接口，配置只来自进程环境变量；
  设置页改为列出真实的键名与改法（改完要重启服务）。
- 归档被关掉时（`DB_PATH=""`）接口 9 返回 **HTTP 200 + `enabled=false`**，
  前端显示中性空态并说明"归档被关掉了"，**不是**红色错误态。

### 9.1.1 设计令牌与对比度（v1.2）

`app/web/app.css` 的 `:root` 是一套**设计令牌**（品牌色阶 `--brand-50…900`、中性色阶 `--n-0…900`、
语义四件套 `--ok/--warn/--err/--info` 各含 `-fg/-bg/-line`、字号 `--fs-xs…4xl`、间距 `--sp-1…10`、
圆角 `--r-sm/md/lg/pill`、阴影 `--sh-1/2/3`、层级 `--z-*`）。组件只引用令牌，不写死色值。

**对比度是算出来的，不是看出来的**：正文与背景 ≥ 4.5:1、大字与控件边框 ≥ 3:1（WCAG 2.1）。
首版有三个色值不达标，处理方式是**改色值而不是放宽门槛**
（`--n-400` `#a3a39e`→`#8f8f8a` 得 3.25:1 用于控件边框；`--n-500` `#7a7a75`→`#73736e` 得 4.77:1 用于最弱可读文字；
`--n-300` 被重新归类为**装饰性分隔线**，不承担任何对比度要求）。

> ⚠️ **这一节的口径后来被 §9.1.3 修正过，别只读到这里。**
> 上面算的全是**令牌两两之间**的对比度，而真实渲染还要经过**级联**：
> `.btn:disabled` 当年用了 `opacity`，它把整个元素与页面做 alpha 混合，
> 于是主按钮变成"白字压白底"（对比度 **1.00**），而这份令牌表**全程报绿**。
> 结论：**"算出来的"必须算的是最终渲染色，不是声明色** ——
> 只比对令牌的脚本量的是"我打算用什么色"，用户看到的是另一回事。

**深色模式本期不做**（这是决定，不是遗漏），三条理由：
① 图表是服务端 matplotlib 生成的**不透明白底 PNG**，深色页面里每张图都是一块刺眼白方块，纯前端修不了；
② 做深色就要把上面所有对比度重新算一遍，而展示价值在"数字可核对"不在主题切换；
③ 没有主题切换接口，只能 `prefers-color-scheme` 自动跟随，用户无法手选，体验反而更差。
`.charts figure` 已固定白底，是留给将来的正确起点。

### 9.1.2 两处窄屏修复（都由真实浏览器量出来，不是看截图猜的）

用户要求"不要只截图检查桌面页面就宣称响应式设计完成"，所以四档宽度（1440 / 834 / 390 / 320）
都用 Playwright 量了真实几何。量出两个截图里不明显的缺陷：

**① 上传区里杵着一个原生控件。** `.dropzone` 是企业级拖拽区，但里面直接渲染了浏览器原生的
`<input type="file">`（那个「Choose File / No file chosen」按钮），视觉上非常突兀。
修法是视觉隐藏，**但不能用 `display:none` / `visibility:hidden`** —— 外层 `<label>` 本身不可聚焦，
键盘用户唯一的入口就是这个 input，藏出 tab 序列等于"不用鼠标就再也选不了文件"。
所以用标准 sr-only 裁剪法（`position:absolute` + `1px` + `clip-path:inset(50%)`）：
仍在 tab 序列里、屏幕阅读器仍能读到、肉眼看不见。配套加 `.dropzone:focus-within`
把焦点环画在整块拖拽区上，否则键盘用户看不到自己在哪。
选了哪个文件由 `#upload-state` 那行文字反馈，**信息没有丢**。

**② 320px 下品牌名被无声裁掉。** `.topbar__product` 有 `white-space:nowrap` 却没有
`text-overflow`，实测 320px 时被硬切成「AI Market Intellige」——看起来像渲染坏了。
修法是补 `overflow:hidden` + `text-overflow:ellipsis` + `min-width:0`
（flex 子项默认 `min-width:auto` 会拒绝收缩），同时给 `.topbar__mark` / `.topbar__right`
加 `flex:none` 让"该让的让、不该让的不让"。

**③ 但加了省略号之后仍然不够体面 —— 这一步是量出来的，不是想出来的。**
补完 ② 之后 320px 确实不再硬切了，可是量出来的数字是：
顶栏右侧三件套（工作区文案已隐藏，剩徽章 90px + 接口文档 71px）要吃掉 173px，
留给品牌名的只有 **49px**，截出来是「AI M…」。**有省略号 ≠ 看得清**，
`text-overflow` 只保证"切得整齐"，不保证"还剩得下信息"。
所以补了第三刀：`@media (max-width: 480px) { .topbar__link { display: none; } }`。
选它而不是选徽章，理由是**接口文档是工具链链接、不是业务动作**，
而且设置视图里还有同一条（`index.html` 的「接口文档」小节写着 `/docs` 是 FastAPI 自带文档）——
**能力没有丢，只是不再占顶栏**。改完品牌名从 49px 回到 **125px**（320px）／160px（360px）／187px（390px），
截出来是「AI Market Intel…」，这才叫体面。徽章（大模型配置状态）在任何宽度都保留，
因为它反映的是**本次分析能不能走模型**，属于业务状态。

> 这一条的方法论价值大于修复本身：**"我修了"和"修好了"之间差一次测量**。
> ② 的修复让断言从"硬切"变成"有省略号"，探针全绿；如果就此收工，
> 交付的就是一个在 320px 上写着「AI M…」的顶栏。是**加了一条"品牌名剩多宽"的测量**
> 才把问题逼出来的。数字诚实不只是"不编数"，也包括"不拿一个刚好变绿的指标冒充修好了"。

这三条都记在这里而不是只改代码，因为**判据是"肉眼看不见"与"挤得有礼貌"，属于设计决定**，
不是能被断言机械钉住的行为。

### 9.1.3 禁用态按钮渲染成空白白框（对比度 1.00，截图才发现）

这是前端改版收尾时抓到的最后一个真缺陷，也是**唯一一个"已有对比度脚本全程报绿、
却被截图逮住"**的缺陷 —— 所以它值得单独记一节。

**现象**：`.tmp/ui_probe/desktop-analysis.png` 里，没有数据集时分析页的主按钮
「开始分析」渲染成一个**空白的浅色圆角框**，里面的字和图标全都不见了。

**根因**在 `app.css` 的这两条规则叠加：

```css
.btn--primary { background: var(--brand-600); border-color: var(--brand-600); color: var(--n-0); }
.btn:disabled, .btn[aria-disabled="true"] {
  opacity: .5;
  background: var(--n-0);      /* ← 通用规则把主按钮的底色改回白色 */
  border-color: var(--n-300);
  /* ← 但没有覆盖 color，于是 .btn--primary 的白色字色原样留着 */
}
```

结果就是**白字压白底**：`--n-0` 叠 `--n-0`，实测对比度 **1.00**。
`opacity:.5` 让它更糊，但不是主因 —— 主因是通用禁用态只换了底色、没换字色。

**为什么对比度脚本没抓到它（关键教训）**：我原本那份脚本只做一件事 ——
把 `:root` 里的令牌两两算 WCAG 对比度，全都达标。可是
**`opacity` 不是"把颜色调暗"，它把整个元素与背后的页面做 alpha 混合**，
最终渲染色是 `alpha × 前景 + (1-alpha) × 页面底色`。静态比对令牌**算不出**这个结果，
缺陷正好藏在这个盲区里。这不是脚本写错了，是**度量层次选错了**：
它量的是"我打算用什么色"，而用户看到的是"渲染出来什么色"。

**修法**：禁用态一律改成**显式配色**，不再用 `opacity`：

```css
.btn:disabled, .btn[aria-disabled="true"] {
  cursor: not-allowed; background: var(--n-100);
  border-color: var(--n-300); color: var(--n-600);          /* 5.94:1 */
}
.btn--primary:disabled, .btn--primary[aria-disabled="true"] {
  background: var(--brand-100); border-color: var(--brand-200);
  color: var(--brand-700);                                   /* 8.44:1 */
}
```

主按钮的禁用态单独给一套**品牌色**而不是退化成灰按钮：它仍然是这一页的主操作，
只是当前不可用。`.btn--primary:disabled` 必须写在 `.btn--primary:hover/:active`
**之后** —— 两者权重相同（都是 0-2-0），靠源码顺序取胜。

加载态同理：`.btn[data-loading="true"]` 原来还有 `opacity: .75`，
而加载中的按钮**带着 `disabled` 属性**，会吃到上面那套禁用配色，再乘 .75 就把对比度
压到 4.5 以下（实测次按钮 2.70、主按钮 4.36）。转圈加文案已经是足够的信号，不需要再暗一档。

**验证**（三层，都真跑了）：

1. `.tmp/probe_disabled_after.py` —— **从 `app.css` 里把色值读出来**（不是抄一份常量表，
   抄一份的话改了 CSS 忘了改脚本，脚本会继续报绿），算得 5 项全过：
   通用禁用态 5.94、主按钮禁用态 8.44、常态 7.48、hover 9.94。
2. `.tmp/probe_btn_render.py` —— 真浏览器里读 `getComputedStyle` 的**最终渲染色**，
   分析页 `#ask` 实测 `color=rgb(35,67,113) bg=rgb(230,237,247) opacity=1 对比度=8.44`。
3. `tests/test_frontend.py::test_disabled_buttons_stay_readable` —— 静态回归锁，
   断言两条禁用态规则里**不许出现 `opacity`**（出现就直接失败，理由写在用例 docstring 里），
   并把 `var(--x)` 顺着令牌表解析成 hex 后算对比度必须 ≥ 4.5。
   变异测试 `.tmp/mutate_disabled.py` **6/6 全抓**，其中 M1 就是把原始缺陷
   （`opacity:.5` + 没覆盖 `color`）原样放回去。

> 与方法论的关系：9.1.2 的教训是"**我修了**和**修好了**之间差一次测量"，
> 这一条的教训是"**测了**和**测对了**之间差一个层次"。
> 两份探针都在报绿，差别在于一份量令牌、一份量渲染 —— 而缺陷只存在于后者。


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
      if (!resp.data.has_report) return;           // 没有报告就别发这次请求（§7.1 第 3 条）
      const rep = await fetch(`/api/workflows/${workflowId}/report`).then(r => r.json());
      if (rep.ok) renderReport(rep.data); else showError(rep.error.message);
    }
  }, 1000);
}
```

### 9.2.1 前端错误提示怎么写（v1.1，配合 §7.0 的状态码表）

**默认一行就够**：`showError(body.error)` 把 `error.message` 显示成红色文字——后端每个错误码都有中文兜底文案（§7.3 的 `DEFAULT_MESSAGE`），所以前端**不需要**维护一张"错误码 → 中文"的对照表（那会变成第二份事实来源，后端改了文案前端不知道）。

只在**需要区别对待**时才看 `error.code`，第一版只有两处：

| `error.code` | 前端行为 | 为什么不能一视同仁 |
|---|---|---|
| `SERVICE_UNAVAILABLE` | 文案下方多给一个"重试"按钮 | 这是"等一下再来"，不是用户能改的问题 |
| `FILE_TOO_LARGE` / `UNSUPPORTED_FILE` | 在上传区就地提示"换个文件"，不要弹全局错误 | 用户下一步动作就是重新选文件，把提示放在选择控件旁边最省事 |

其余一律显示 `message` 即可。**不要按 HTTP 状态码分支**（503 与 500 对用户没区别，对排查也没区别）——按 `code` 分支，因为 `code` 是后端明确承诺的业务语义。

规则：**只轮询状态接口**（小），**不轮询报告接口**（大）；进入终态立刻 `clearInterval`，避免关掉页面后还在打请求（可以再加 `visibilitychange` 事件，隐藏标签页时暂停轮询，属于优化项）。

### 9.3 前端不做的事

- 不用 React/Vue/打包工具（第一版是"能看懂每一行"的展示，不需要构建链）。
- 不在前端做任何计算或格式化数字（千分位、百分比也应尽量后端算好；如果要做，只允许纯展示格式化，绝不做统计）。
- 不做多文件对比、不做登录。
- **不做深色模式**（v1.2 决定，理由见 §9.1.1）。
- **不做假进度**：不用前端定时器假装后台智能体在工作（进度只来自接口 6 的真实 `step` / `status`）；
  上传也不做进度条（`fetch` 没有上传进度事件，宁可不做也不做假的）。
- **不伪造指标**：页面上的每个数字都必须来自某个接口的真实字段；
  接口没返回的指标（例如"去重后行数"）不显示，也不在前端自己算一个出来充数。
- 静态文件挂载（D6）：`app.mount("/static", StaticFiles(directory=app/web), name="static")`，并且给 `GET /` 改成返回 `index.html`（现有 `/` 返回 JSON 提示，D6 时保留 `/api/health` 与 `/docs`，把 `/` 让给页面）。**注意 `data/` 目录绝不挂载**，图表一律走第 8 号接口，避免把上传的原始文件暴露成静态资源。

---

## 10. 安全设计清单

| # | 项目 | 具体要求 | 实现位置 |
|---|---|---|---|
| 1 | 上传格式白名单 | 只允许扩展名 `.csv` / `.xlsx`（大小写不敏感）。`.xls`、`.csv.gz`、`.zip`、`.parquet`、无扩展名一律拒绝。**只信扩展名 + 解析成功这两道关，不信 `Content-Type`**（浏览器给的 `Content-Type` 经常是 `application/octet-stream`，参考价值低） | `routes_datasets.py` + `loader.py` |
| 2 | 大小限制 | 在**读之前**用 `UploadFile.size` 或分块累加判断，超过 `settings.max_upload_bytes`（默认 10 MB）立刻返回 `FILE_TOO_LARGE`，并且**不把已经读到的内容写进磁盘**。README 里写清"超过 10 MB 请先在 Excel 里裁剪" | `routes_datasets.py` |
| 2b | **行数与列数两道闸**（v1.1 追加） | **字节数上限挡不住"很小但很宽/很长"的文件**：200 列 × 20 万行的 CSV 可能只有 3 MB，却能把内存拖死（每列一个 object/float64 数组 + 画像逐列统计）。所以 `app/config.py` 已有 `max_rows`（默认 **200000**，`Field(gt=0, le=5_000_000)`）与 `max_columns`（默认 **200**，`Field(gt=0, le=2_000)`），`.env.example` 有 `MAX_ROWS` / `MAX_COLUMNS`。**判在哪一步**：读文件时无法知道行数（CSV 得先解析），所以先用 `pd.read_csv(..., nrows=settings.max_rows + 1)` / `read_excel(...)` 读**前 N+1 行**——读到第 N+1 行说明超限，立刻抛 `AppError(ErrorCode.BAD_REQUEST, "行数超过上限 200000")`；列数在拿到 DataFrame 后立刻判 `df.shape[1] > settings.max_columns`。两个上限都用 `BAD_REQUEST`（400）而不是 413：**413 的语义是"请求体太大"，而这里限制的是解析后的形状**，报文本身可能只有 3 MB | `loader.py`（读时判行、读完判列）|
| 3 | 路径穿越防护 | ① 落盘名一律用 `{uuid4().hex}.{ext}`，**用户文件名只写进数据库/内存字段，从不参与拼路径**；② 读取时再做一次确认：`resolved = target.resolve(); assert resolved.parent == upload_path.resolve()`（用 `Path.resolve()` 消掉 `..` 之后比较父目录）；③ 文件名黑名单：含 `..`、`/`、`\`、`:`、`\0` 直接拒绝 | `routes_datasets.py`、`data/store.py` |
| 4 | 安全文件命名 | 图表文件名只由 `chart_id`（12 位 hex）决定；`ChartResult.filename` 用正则 `^[0-9a-f]{12}\.png$` 校验后才允许读；工作流目录名只由 `workflow_id`（32 位 hex）决定 | `routes_workflows.py`、`agents/charts.py` |
| 5 | 密钥管理 | 密钥只从**进程环境变量**读（`app/config.py`；**不读 `.env`**，见 §4.0.1 与 D-19）；`.env` 仍在 `.gitignore` 里，但那是**防误提交**（谁本地写了一个），不是应用要读它；`Settings.llm_api_key` 已带 `repr=False`，任何 `print(settings)` 都不会带出原文；**任何 API 响应与日志里都不允许出现密钥**（测试用 `assert "sk-" not in resp.text` 之类的方式兜底，不要只靠人工检查） | `config.py`（已做）+ QA 测试 |
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
| 10 | CORS 收紧 | 只允许 127.0.0.1 / localhost 的本机端口（`8128`、`8000`、`5500`），`allow_credentials=False`。**不要为了图省事改成 `["*"]`** | `main.py`（已做） |
| 11 | 上传文件不执行 | 绝不把上传目录放到 Python 的 import 路径上，也不用 `pandas.read_pickle` / `read_json`（pickle 反序列化能执行任意代码）。只允许 `read_csv` / `read_excel` | `loader.py` |
| 12 | Excel 炸弹与超宽表 | 行数上限与列数上限超过即 `BAD_REQUEST`。**具体数值以 `app/config.py` 为准**（`max_rows=200000`、`max_columns=200`，v1.1 已加），实现细节见第 10 节第 2b 条（读前 `nrows=max_rows+1` 判行、读后判列）。公式不计算（`openpyxl` 读值不读公式）；避免 `read_excel` 把整个工作簿所有 sheet 都读进来（只读第一个 sheet 或指定 sheet） | `loader.py` |

---

## 11. 技术选型：为什么「不引入」这些东西

原则：**每一个依赖都要能回答"没有它我少什么"**。下面是逐个说明，格式统一为「它的用途 / 不用的替代方案 / 什么时候才值得引入」。

| 技术 | 它能解决什么 | 第一版不用它，替代方案 | 什么时候值得引入 |
|---|---|---|---|
| **LangGraph** | 用图（节点 + 边 + 状态）描述多 Agent 流程，自带状态机、检查点、条件分支、人工审核环节 | 我们的流程是**固定顺序的 6 个阶段 + 最多 6 个任务的循环**，用 `orchestrator.py` 里一个 `for` 循环 + `if/elif` 就能写清，一共约 150 行。自己写的好处：出问题时你能一眼看出在第几步、状态存在哪、为什么重试；LangGraph 出问题时你要先学会它的 `StateGraph`/`Reducer`/`checkpoint` 概念才能调试 | 流程出现**真正的分支与回溯**（例：校验不过要回到规划阶段重规划、需要人工审批节点、需要从中断点续跑）。第一版若硬上：多学 2–3 天概念、调试成本翻倍，而展示效果不变 |
| **Celery** | 分布式任务队列：把任务丢给独立 worker 进程，支持重试、定时、结果后端 | 一个分析任务最慢 60 秒，并发量是"演示者一个人点"。`BackgroundTasks` + 线程池完全够用，且**零新依赖** | 需要多 worker 进程、需要任务持久化/定时调度、分析耗时进入分钟级、或要部署到多台机器 |
| **Redis** | 缓存 / 消息中间件 / 分布式锁 | 数据集和 DataFrame 放进程内 `dict`；工作流状态也在 `dict`。**项目只有一个人用，进程内存是最快的"缓存"** | 要多进程共享状态，或 Celery 需要 broker。引入前先问：为什么不先加 SQLite？（**"重启不丢历史"这一半 SQLite 已经解决了**，见 §8.3；剩下那一半是"多进程共享状态"，SQLite 的归档表并不能解决它） |
| **Docker** | 环境隔离与一键部署 | 你的环境是 Windows 11 + `.venv`（`--system-site-packages`），`requirements.txt` 已经能复现。Docker Desktop 在 Windows 上要开 WSL2、多占几个 GB 内存，学习成本与收益不成比例 | 需要给别人"一条命令跑起来"的交付（例：面试官要本地跑）→ 那时加一个 `Dockerfile`（FROM python:3.12-slim + pip install + uvicorn）大约 15 行，成本可控。**放在第 7 天之后，不进七天排期** |
| **SQLAlchemy** | 持久化：用 ORM 描述表、迁移与关系（注：早期文档写成"用**已装的** SQLAlchemy"，那是错的 —— 仓库从来没装过它） | 本项目只需要"一张表的 UPSERT + 一次 SELECT"，用**标准库 `sqlite3`** 约 100 行就写完了，**零新依赖**。ORM 在这里是**负资产**：多一层抽象、多一个依赖，换来的迁移/多表能力一条都用不上 | 出现多张有关联的表、需要迁移管理、或要在多种数据库之间切换时 |
| **SQLite（历史归档）** | 持久化：重启后仍能查询历史工作流 | **第一版状态全在内存；这一项已在 D6 之后补做（D6-SQLite，见 §8.3）**：只做归档写入（`data/db/app.db`），读路径仍然只有内存 | 已采用。下一步（读路径也改库、支持多进程）在需要 `--workers > 1` 时再做，见 §8.3 触发条件 1 |
| **向量数据库 / RAG** | 语义检索 | 完全没有场景：用户数据是结构化表格，字段名和类型是精确的，靠 `DatasetProfile` 就能让模型看全 | 出现"用自然语言在大量自由文本里找相关内容"的需求时 |
| **前端框架（React/Vue）** | 组件化与状态管理 | 单页 + 6 个区块 + 一次轮询，原生 JS 约 200 行能写清；引入框架就要引入 node_modules 与构建步骤，还会挡住"每一行都能读懂"这个目标 | 页面超过 3 个路由、需要组件复用或团队协作时 |
| **PandasAI / 让模型写 pandas 代码** | 让模型直接生成代码分析 | **明确禁止**。它直接违反项目铁律（数字必须由我们自己的白名单工具算出），而且生成的代码会 `exec`，安全与可控性都不满足 | 永不引入（与本项目的核心卖点冲突） |
| **SSE / WebSocket** | 实时进度推送 | 1 秒一次轮询，实现 10 行，够用 | 需要秒级以内的实时性、或轮询请求量成为瓶颈（见 8.3 第 4 条） |

**新依赖的总闸门**：任何新增第三方依赖，必须先在 `requirements.txt` 的注释里写一行"用途 + 不用的替代方案"，否则不合并。这条规则本身也写进 README。

**`requirements.txt` 与 `requirements.lock.txt` 的分工**（v1.1 追加，两份都要保留）：

| 文件 | 写什么 | 谁在用 | 为什么不能只留一份 |
|---|---|---|---|
| `requirements.txt` | 版本**区间**（`fastapi>=0.110` 这类下界），是允许范围 | 别人 clone 后 `pip install -r requirements.txt`，允许装到更新的小版本 | 写成精确锁会把"在更新版本上也能跑"这件事变成不可能，而且 3.10–3.12 的兼容性无法覆盖 |
| `requirements.lock.txt` | **本机实测通过的精确版本**（12 个直接依赖，`importlib.metadata.version()` 逐个取值，不用 `pip freeze`——`.venv` 是 `--system-site-packages` 建的，freeze 会带出整台机器的 185 个包） | 面试官/评审要"一字节不差复现这套版本"时 `pip install -r requirements.lock.txt` | 复现问题（"我这儿跑不起来"）时最需要的是**当时验证过的确切版本**，区间给不出这个信息 |

已实测的 12 个直接依赖：`fastapi==0.136.3`、`uvicorn==0.48.0`、`pydantic==2.13.4`、`pydantic-settings==2.15.0`、`python-multipart==0.0.32`、`pandas==3.0.3`、`numpy==2.4.6`、`matplotlib==3.11.2`、`openpyxl==3.1.5`、`openai==2.41.0`、`pytest==9.1.1`、`httpx==0.28.1`。**注意 `requirements.lock.txt` 是快照不是约束**：区间仍以 `requirements.txt` 为准。

---

## 12. 七天开发计划与依赖顺序

前提：第一阶段骨架已完成（`app/main.py`、`config.py`、`schemas.py`、`runtime.py`、`routes_health.py`、`tests/test_health.py`），第一天只做验收。

验收命令统一形式（PowerShell 5.1，在项目根目录执行）：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q
python -m uvicorn app.main:app --reload --port 8128
Invoke-RestMethod http://127.0.0.1:8128/api/health | ConvertTo-Json -Depth 5
```

> 端口 `8128` 是 D6 之后统一的文档端口（`8000` 在本机被别的项目占着，绑不上；
> 见 README「为什么是 8128 而不是 8000」）。本节下面那张表里 D1–D6 的验收记录
> 写的是当时真实用过的端口，**不改写历史**，但命令请一律用 `8128`。

| 天 | 目标 | 负责人（主 / 协） | 可并行的部分 | 验收标准（命令可验） |
|---|---|---|---|---|
| **D1** | 骨架验收 + 架构文档定稿 | LEAD / ARCH | ARCH 写文档与 LEAD 跑验收完全并行 | ① `python -m pytest -q` 全绿（现有 8 条左右）；② `/api/health` 返回三键外壳且 `llm_configured=false`；③ `docs/ARCHITECTURE.md` 存在且第 14 节的 3 个问题已拍板 |
| **D2** | 数据层 + 上传接口（**串行，是后面所有天的地基**） | BE / QA | BE 写 `loader.py`+`store.py`+`routes_datasets.py`；QA 同时写 `tests/test_loader.py`、`tests/test_upload_api.py`；LEAD 生成 `data/samples/sales.csv` | ① `pytest tests/test_loader.py tests/test_upload_api.py -q` 全绿；② 上传 `sales.csv` 返回 `dataset_id`，磁盘上文件名是 32 位 hex；③ 上传 `.txt` → `ok:false, code:UNSUPPORTED_FILE`；④ 上传 11 MB 文件 → `FILE_TOO_LARGE`；⑤ 文件名 `..\..\evil.csv` 上传后磁盘文件仍在 `data/uploads/` 内（用 `Get-ChildItem -Recurse` 确认没有越界文件） |
| **D3** | 工具层（**串行，核心资产**） | AI / BE、QA | 7 个工具可以拆给 2 人：`describe.py`+`missing_values` 一人，`aggregate.py`+`compare.py`+`timeseries.py` 一人；profiler 与 routes_datasets 的 `/profile` 可并行 | ① 每个工具至少 3 条测试（正常 / 参数错 / 列不存在）；② `python -m pytest tests/test_tools.py tests/test_profiler.py -q` 全绿；③ **对照测试**：手工用 Excel 算一遍 `sales.csv` 的"各地区销售额汇总"，与 `group_aggregate` 的返回完全一致（这条是"数字是真的"的第一道证据）；④ `GET /api/datasets/{id}/profile` 返回的 `null_count` 与 `df.isna().sum()` 一致 |
| **D4** | LLM 客户端 + 规划 + Orchestrator + 分析接口（**串行，最大风险点**） | AI / BE | AI 写 `llm/`+`planner.py`+`orchestrator.py`+`analysis.py`；BE 写 `core/registry.py`+`core/background.py`+`routes_analysis.py`；QA 写 `test_workflow_state.py`。**接口先按 Pydantic 模型定好，两边对着模型写，不需要等对方完成** | ① **兜底模式 + 真实数据必须能跑出完整报告**（第 14 节决策 1 的验收条件）：未配 LLM 时 `POST /api/analysis` 用规则兜底计划跑完全流程，`status=SUCCESS`、`report.llm_used=false`，报告有正文、有表格、有 `cites`，**且报告里明确写清哪一步用了模型、哪一步没用**（模板报告首段固定写"本次未使用大模型（llm_used=false），以下结论为工具结果的直接汇总"）；② 配上 LLM 后：同一问题返回的 `plan.source == "llm"`，且 `tasks[].tool_name` 全在白名单内，报告里写明数字来自 pandas 工具、模型只负责解释；③ 把 `LLM_BASE_URL` 改成错误地址 → 工作流仍是 `SUCCESS`（走兜底），而不是 500；④ 轮询接口在 3 秒内能查到 `step` 变化 |
| **D5** | 图表 + 报告 + 校验（报告依赖 D4 的 `ToolResult`，**串行接在 D4 后**） | AI / FE（提前搭页面骨架） | AI 写 `agents/charts.py`+`validation.py`；FE 可以**用假 JSON 先把页面写完**（不需要等后端）；QA 写 `test_validation.py`、`test_charts.py` | ① `GET /api/workflows/{id}/charts/{f}.png` 返回 `image/png` 且能在浏览器打开（中文标题不乱码）；② `pytest tests/test_validation.py -q` 全绿，其中**必含一条**：手工把一个不存在的数字塞进 `FinalReport.sections[].text`，`validate()` 必须产出 `UNSUPPORTED_NUMBER` 且 `passed=false`；③ 报告里的表格数字与 `ToolResult.data["rows"]` 逐字段相同（写一条断言） |
| **D6** | 前端联调 + 安全检查 + SQLite 持久化 | FE / QA、BE | FE 写 `web/` 三件套；QA 写 `test_security.py`；BE 加 SQLite 持久化（**第 14 节决策 3 已定为做它、不做 SSE**） | ① 浏览器打开 `http://127.0.0.1:8000/` 能完成"上传 → 提问 → 看进度 → 看图 → 看报告"全流程；② `pytest -q` 全绿（含安全测试：AST 源码检查、响应不含 `sk-`、密钥不进 `repr`、`sanitize_detail` 去路径）；③ 上传 `.txt` 和超大文件时页面显示中文错误而不是白屏；④ 断网/模型超时场景页面在 180 秒内必出结果（含失败提示），不会永远转圈。**⑤ SQLite 持久化（当初这一天的第 5 项）：D6 当天没做完，交付后才补上（D6-SQLite）。详见下方"时间线"** |
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
| 9 | **时间与排期风险（最大的非技术风险）** | D4 卡住一天，D5/D6 被压缩，最后一天在赶工中把"校验"砍掉——而校验恰恰是简历里最值钱的部分 | ① 排期把**校验**（D5 的一部分）视为**不可砍项**，砍它之前先砍前端美化、SQLite 历史（SSE 已按第 14 节决策 3 明确不做，不占预算）；② 每天结束必须有可运行成果（第 12 节的退守方案已为每天准备好"缩小版"）；③ LEAD 每天收工前跑一次 `pytest -q` 并把输出贴进 README 的"进度"一节，避免"以为完成了"。**事后复盘：这条风险真的发生了 —— D6 当天 SQLite 历史就是被砍掉的那一项**（精力去了前端联调与安全测试），一直到交付后才由用户拍板补做（D6-SQLite）。所以"可砍顺序"这个排法本身是对的，代价也对得上：砍掉的东西没有消失，它变成了一个确定的欠债 |
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
| **⚠️ 时间线（如实记录，不要读成"D6 就做完了"）** | 上面这条**只是拍板**。**D6 当天 SQLite 一行都没写** —— 那一天的精力去了前端联调与安全测试（`web/` 三件套 + `test_security.py`），SQLite 就是被按"可砍顺序"砍掉的那一项。所以在这之后很长一段时间里，本文档 §8.3 / §14 的 SQLite 设计稿**是计划，不是已实现的事实**，而它被误读成了完成时态（**这正是本项目"文档从不撒谎"规矩要防的那类错**：不是写了假话，是**计划时态被读成了完成时态**）。**后来由用户拍板补做**，代号 **D6-SQLite**，在 D6 交付之后才落地；补做时**改了一个选型**：不用 SQLAlchemy（见下一行），用**标准库 `sqlite3`**。 |
| **⚠️ 落地时改掉的选型（必须写清为什么）** | 上面"我的建议"里写的"用**已装的** SQLAlchemy"——**那时它根本没装**，仓库里从来没有任何 SQLAlchemy 依赖（`requirements.txt` 通篇没有数据库包），这句话当时就是**假的**。补做时改为**标准库 `sqlite3`，零新依赖**。理由：本项目只需要"一张表的 UPSERT + 一次 SELECT"，ORM 换来的迁移/关系/多数据库能力一条都用不上，**用 ORM 是负资产**。库路径也从设计稿的 `data/app.db` 改成 **`data/db/app.db`**（按仓库已有的 `.gitignore` 规则选址：`data/db/*` 早就被挡，`data/app.db` 得新加一条规则）。完整实现口径见 §8.3 的"已交付"表 |
| **追加的约束 ①（最关键）** | **SQLite 只做历史归档写入，内存态 dict 仍是唯一事实来源。** 不要让读写路径变成"有时读内存有时读库"，那会引入最难查的一类 bug。完整读写路径约定见 **8.3.1**（写：终态后归档，失败只记 warning；读：接口只读内存；历史列表只读库；禁止 fallback 复活对象） |
| **追加的约束 ②** | 文档里必须**显式写明"重启后内存态丢失、但历史记录仍在"这个已知限制**，而不是让人以为"持久化 = 断点续跑"。见 **8.3.2**（4 条已知限制，其中"正在运行的工作流重启后不会恢复也不会被标失败"要写进 README） |
| 未采纳的选项 | SSE。**承认它是个好技术**，但本项目不需要——用户只有一个人、1 秒轮询已够用，多出来的学习成本会挤掉 D5 的校验时间（而校验是简历里最值钱的部分） |

### 14.1 与第一阶段代码（commit 460cf8c）的一致性核对结果

| 核对项 | 结论 |
|---|---|
| `app/config.py` 删除模块级 `settings` 对象 | **文档已同步**：第 4.0 节写成强制约定（`from app import config` + 函数内 `config.get_settings()`、测试用 `config.reset_settings_cache()`），并列出两种禁止写法。附录 B 里"`app/config.py` 不改已有字段、D2 只追加"的描述仍然成立——删除模块级对象是行为变化，不是字段变化 |
| `app/main.py` 统一错误处理器 | **文档已同步**：第 7 节开头新增"框架异常 → 同一外壳"映射表（404 → `NOT_FOUND`、422 → `BAD_REQUEST`，`detail` 内容），并明确"HTTP 状态码表达 HTTP 语义、`error.code` 表达业务语义"。**v1.1 更新**：原先"业务错误一律 200"的绝对表述已**整段删除**，改为 §7.0 的完整状态码映射表（含 202/413/415/503）+ `app/errors.py` 的 `ERROR_STATUS` 集中映射，理由与代价见 **§14.2 第 1 条**；405 后来也从 `BAD_REQUEST` 拆成专用的 `METHOD_NOT_ALLOWED`（见 §14.2 第 11 条） |
| lifespan 创建上传目录 / `/api/health` 新增 `upload_dir_ready` | **文档已同步**：接口 1 的成功响应字段列表已含 `upload_dir_ready`，并注明"`false` 不算错误，属于能跑但要提示的状态"。**建议**：第 9 节页面把"服务状态"小徽章做成 `llm_configured` + `upload_dir_ready` 两个点，避免上传目录不可写时前端只显示"在线" |
| `tests/test_error_format.py` 已存在（守护上述外壳 + CORS） | **文档已同步**：目录树里标注为"(已存在, 460cf8c) … **不要动**"，避免后续把它的期望改成旧格式 |
| matplotlib 缓存目录坑（WinError 5） | **文档已同步**：`config.py` 顶部已 `os.environ.setdefault("MPLCONFIGDIR", <项目根>/.mplcache)`，所以第 13 节风险 8 的 matplotlib 部分只需保证"中文字体 rcParams"，**不需要**再处理缓存目录；第 4.0 节已写死"`agents/charts.py` 不许改 `MPLCONFIGDIR`、不许设非 Agg 后端" |
| 其他发现的不一致 | 见下方"给 Project Lead 的核对备注" |

### 14.2 本轮 9 项修订的落实情况（v1.0 → v1.1，审核意见 `docs/REVIEW_ARCHITECTURE.md`）

> 修订日期 2026-10-13，修订人 architect。逐条对应审核意见 D 节的 9 条清单。**9 条全部已改**，无未改项。

| # | 审核条目 | 是否已改 | 改在哪（小节） | 怎么改的（可 grep 的关键词） |
|---|---|---|---|---|
| 1 | **[阻塞] A-1** HTTP 状态码自相矛盾 | ✅ 已改 | §7 开头统一约定、**新增 §7.0 状态码映射表**、§7 接口表错误码列、**新增 §7.3 `ERROR_STATUS`**、§4.0 代码约定、§14.1 | 删除"业务错误一律 HTTP 200"整句；照抄 A-1 表 12 行（200/202/400/404/405/413/415/422/500/503 + `WORKFLOW_TIMEOUT`→200）；接口表每格写成 `**415** UNSUPPORTED_FILE` 形式；`POST /api/analysis` 改 **202**；补 4 条"禁止绕过统一外壳"约束 |
| 2 | **[阻塞] A-2** `charts` 三处不一致 | ✅ 已改 | §4.8、§7 接口 6 / 接口 8、**新增 §7.1.1**、§9.1 分析报告视图 | 写死 `ChartResult` 字段与 `url` **由后端拼好** `/api/workflows/{workflow_id}/charts/{filename}`；§7.1.1 给出前端唯一正确写法 `img.src = c.url`（不拼字符串），并说明"前端拼接会让 404 的图片链接无法被 pytest 覆盖" |
| 3 | **[阻塞] A-3** 目录设计冲突 | ✅ 已改 | §2 目录树、**§2.1**、§3 | 收口为 `app/data/{loader,store,profiler}.py` + `app/errors.py` + `app/api/routes_datasets.py` 一份清单；同一处（§2.1 末尾）标注"`app/datasets.py` 与 `app/security/files.py` 这两个提法已作废"（保留此说明是为了让后来者知道它们**不是**被漏掉） |
| 4 | B-1 `WorkflowState` 定义位置分裂 | ✅ 已改 | §4.0 判据、§4.5 | 全部模型定义在 `app/schemas_workflow.py`；`app/core/registry.py` **一行模型定义都没有**；判据一句话："数据长什么样"进 `schemas_*.py`，"数据放在哪、怎么同步"进 `core/` |
| 5 | B-2 锁说明矛盾 + 漏"登记图表" | ✅ 已改 | **新增 §8.2.1 / §8.2.2 / §8.2.3** | ① `_lock` 只保护 registry 的 dict，`state` 内部字段**不需要**加锁（附"禁止到处加锁 / 禁止返回 deepcopy"两条禁令）；② `charts.py` 不许改 `state`，由 `analysis.py` 在**同一函数体内连续两行**写文件 + `state.charts.append(chart)`，并说明"写"与"登记"分离会产生孤儿 PNG + 接口 404；③ `DatasetStore` 同属进程内，单进程限制同样适用，要一起写进 README |
| 6 | B-3 源码扫描测试做法 | ✅ 已改 | §10 第 7 条 | 改为 `ast.parse` + `ast.walk` 判断 `ast.Call` 的 `eval/exec/compile/__import__`（附约 25 行测试代码）；写明"注释与 docstring 天然不在语法树里"；并诚实标注 AST 检查**防不住** `getattr(obj, name)()` 这类间接调用，真正的边界是工具白名单注册表 |
| 7 | B-4 `sanitize_detail()` 缺测试 | ✅ 已改 | §10 第 9 条 | 补 15 行测试：构造含 `D:\个人项目01\...` 的 `FileNotFoundError` 原文，断言盘符/项目路径/用户名都消失且保留 `<path>`；另加一条端到端断言（404 响应体里不出现盘符）。§7.3 的 `AppError.__init__` 里把 `sanitize_detail` 接上，不再是"只写在注释里" |
| 8 | B-5 示例数据命名冲突 | ✅ 已改 | §2 目录树 `data/samples/`、§12 的 D2/D3/D7 行、§13 风险 6 | 定名 `data/samples/sales.csv`（"定名就是它"已写进目录树注释）；`students.xlsx` 已去掉，明确"演示一个数据集足够，第 6 天有富余再加" |
| 9 | D-9 追加本轮修订记录 | ✅ 已改 | **本节 §14.2** + 文档头部 | 文档标题与版本行改为 **v1.1（2026-10-13 修订）**，并指向本节 |

**顺带做掉的两处一致性修正（不在 9 条里，但同源）**：
1. **`AppError` 签名统一为 4 参数** `AppError(code, message, detail=None, status_code=None)`：审核意见写的 `status_code=...` 三参数与我 §7.3 的两参数查表写法不一致，照哪边写都会让 backend-engineer 卡一下。现在两者并存——**默认查表**（保住"状态码集中在一处"这个 A-1 的核心收益），`status_code` 只作为极少数场景的显式覆盖（v1.1 收尾后已无调用点，见下条）。
2. **§14.1 里"原先'业务错误一律 200'已改为……"的描述**已更新为"整段删除 + 指向 §7.0"，避免这一节自己留下旧说法。

**v1.1 收尾追加（不在 9 条里，Lead 拍板后由 Lead 改代码、我同步文档）**：

| # | 追加项 | 谁做的 | 文档同步位置 |
|---|---|---|---|
| 10 | `ErrorCode.SERVICE_UNAVAILABLE`（→503），不再借用 `BAD_REQUEST` | Lead 改 `app/schemas.py` + `app/errors.py` | §7.0 表、§7.0 的 503 段、§7.1 接口 5、§7.2 对照表、§7.3 两个代码块、本节末段 |
| 11 | `ErrorCode.METHOD_NOT_ALLOWED`（→405），原来归 `BAD_REQUEST` | 同上 | 同上（§7.0 表 405 行、§7.2 对照表、§7.3 两个代码块） |
| 12 | `AppError.status_code` 参数从此没有调用点（保留不删） | 我（结论） | §7.3 第 3 条 |
| 13 | 非清零验收口径正式化（正文 vs 作废说明/变更记录） | 我提议、Lead 采纳 | 本节末段 |

同步后自证：§7.3 的两个代码块与 `app/errors.py`（217 行）**逐行一致**（8 个错误码、8 条 `DEFAULT_MESSAGE`）；§7.2 的 8 个错误码顺序与 `app/schemas.py:29-36` 一致。

**本轮唯一一处"提出来但没自己改"的地方，已由 Lead 拍板并落地（v1.1 收尾同步）**：

A-1 表里"外部依赖不可用（模型未配置且无法兜底）"原本配的是 **503 + `BAD_REQUEST`**。状态码 503 是对的，但 `error.code=BAD_REQUEST` 会让前端按 `code` 分流时提示"请检查你的输入"，把"我们的依赖挂了"说成"你的请求写错了"——这是会浪费排查时间的那类错误提示。**Lead 的决定：采纳，加 `ErrorCode.SERVICE_UNAVAILABLE`（→503）**；并顺手把 405 也补了 `ErrorCode.METHOD_NOT_ALLOWED`（原来归 `BAD_REQUEST`，同样的指错方向问题）。

- **落地位置**：`app/schemas.py` 的 `ErrorCode`（8 个值）+ `app/errors.py` 的 `ERROR_STATUS` / `DEFAULT_MESSAGE`（8 行），**已由 Lead 直接实现**（不是我改的代码）。
- **文档同步（我改的这两处）**：§7.0 状态码表的两行（405 → `METHOD_NOT_ALLOWED`、503 → `SERVICE_UNAVAILABLE`）、§7.1 接口 5 的错误码列、§7.3 的 `DEFAULT_MESSAGE` 与 `ERROR_STATUS` 两个代码块、§7.2 从"现在是 5 个 + 建议加 1 个"改写为 8 个的对照表、§7.0 的 503 段落。两份代码块现在与仓库**逐行一致**，照抄不会错。
- **附带结论（我写进 §7.3 第 3 条）**：`AppError.__init__` 的第 4 个参数 `status_code` 现在**没有调用点了**。它不删——将来出现"同一个 `code` 要配两个状态码"时不必改构造函数签名；但默认不传。
- **代价**：枚举 2 个值 + 映射表 2 行 + `DEFAULT_MESSAGE` 2 行，与我在回报里估的"约 2 行"同量级。

**正式口径（引用就用这一版，Lead 于 v1.1 定稿）**：

> `docs/ARCHITECTURE.md` **正文**（除 §2.1 末尾「已作废」说明与 §14.2 变更记录外）不得出现 `app/datasets.py`、`app/security/files.py`、`sales_sample.csv`；不得把"业务错误一律 HTTP 200"写成设计表述。

**怎么自己核这条口径（不要照抄某个固定数字）**：本节的变更记录本身**必须**提到这些字符串（否则无法追溯到"删了什么、为什么留"），所以**计数会随本节编辑而变**——本次逐处核对的结果是：三个旧文件名/路径里，只有"已作废"那一条出现在 §2.1 末尾，其余**全部集中在本节**；`sales_sample.csv` 与"业务错误一律 HTTP 200"更是只在本节出现。**结论口径是"§2.1 与 §14.2 之外为 0 处"，不是"全文档 0 处"。** 想复核就跑下面这条（PowerShell，逐处打印行号与所属节标题，比单看一个数字可靠）：

```powershell
# 两个坑，都必须写进脚本里，否则判据本身不可信：
# ① 大小写：显式用 -imatch。PowerShell 的 -match 默认就不区分大小写（-cmatch 才区分），
#    但判据要写死成"不区分大小写"才严密 —— 否则 http 200 / HTTP 200、
#    Sales_Sample.CSV 这类写法会被漏检。（-notmatch / -inotmatch 同理。）
# ② 编码：必须 -Encoding UTF8。PowerShell 5.1 的 Get-Content 默认按**系统 ANSI 代码页**
#    读文件，本机是 GBK —— 用它读 UTF-8 的中文文档，一行会变成乱码，而且**行号会错**
#    （实测：同一份 1458 行的文档，加 -Encoding UTF8 得 1458 行、行号与编辑器一致；
#    不加只有 1068 行，行号整体前移，报出来的位置没法用来核对）。Select-String 同理。
$f = "docs\ARCHITECTURE.md"
$sec = ""; $n = 0; $bad = 0
Get-Content $f -Encoding UTF8 | ForEach-Object {
    $n++
    if ($_ -imatch '^#{2,3} ') { $sec = $_.Trim() }
    if ($_ -imatch 'app/datasets\.py|app/security/files\.py|sales_sample\.csv|业务错误一律\s*http\s*200') {
        $ok = $sec -imatch '^### (2\.1|14\.2|14\.3)'
        if (-not $ok) { $bad++ }
        "{0}  {1,5}  {2}  ::  {3}" -f $(if ($ok) { "OK " } else { "BAD" }), $n, $sec, $_.Trim().Substring(0, [Math]::Min(60, $_.Trim().Length))
    }
}
"非白名单节命中 = $bad（判据：必须为 0，不分大小写）"
# 期望：命中行全部落在 §2.1（1 处"已作废"说明）与 §14.2 / §14.3（变更记录）里，
#       最后一行打印 非白名单节命中 = 0。
```

> ⚠️ **本节里出现的任何"共 N 处"数字都会随本节编辑而变大**（因为这些字符串就写在 §14.2 里），**不要把它当验收基线**。当次核对（v1.1 收尾时）的真实分布是：`sales_sample.csv` → 仅 §14.2；`app/datasets.py` / `app/security/files.py` → §2.1 末尾「已作废」说明 1 处 + §14.2 若干处；`业务错误一律 HTTP 200` → 仅 §14.2。**验收判据请用上面那段 PowerShell 的结论（"§2.1 与 §14.2 之外为 0 处"），不要用数字。**

### 14.3 第二阶段契约冻结（commit `44566c6`）后的文档同步记录

基线：`git log --oneline -5` = `44566c6`（第二阶段契约冻结）← `fe155af`（统一错误契约落地）← `460cf8c` ← `9d37bcb`。本轮同步只改 `docs/ARCHITECTURE.md`，未触碰任何 `.py`。

| # | Lead 要求 / 我发现的事项 | 处理 |
|---|---|---|
| 1 | `app/config.py` 追加 `max_rows`（默认 200000，`gt=0, le=5_000_000`）与 `max_columns`（默认 200，`gt=0, le=2_000`），`.env.example` 已有 `MAX_ROWS`/`MAX_COLUMNS` | §10 **新增第 2b 条**（字节数之外的两道闸 + `nrows=max_rows+1` 的判行写法 + 为什么用 400 而不是 413）；§13 第 12 条改为"数值以 `config.py` 为准"；§2 目录树 config.py 行标注为**已有**；**新增 §4.0.1 配置项总表** |
| 2 | 验收口径定稿（引用版本） | §14.2 只保留**一版**正式口径（即上一段引用的那一版），删掉了口径草稿与相关解释性文字，避免同一口径在文档里出现两种写法 |
| 3 | `requirements.lock.txt`（12 个直接依赖的实测精确版本）已同步 | §11 新增"两份 requirements 的分工"表（区间 vs 快照，以及**为什么不用 `pip freeze`**：`.venv` 是 `--system-site-packages` 建的，freeze 会带出整台机器 185 个包）；文档头的环境事实补 `44566c6` |
| 4 | `profiler.py` 的标注（**改了两次，最终定案**） | Lead 先说改 `[D2][BE]`、后说"标 `[D3][AI]` 是对的，不用改"，第三次讲清了判据并定案：**排期表不是承诺，实际做完的那天才是记录** —— `profiler.py` 今天就随 task-8 在实现，文档却写 D3，会让后续对进度的人对不上。**最终：`[D2][BE]`**，并保留括注"纯 pandas 计算，**零 LLM 参与**；实际就在 D2 做" |
| 5 | `[AI]` / `[BE]` 这一列到底是什么意思（Lead 指出"问题在那一列本身，不在 `profiler.py` 这一格"） | §2 记号表下方**新增列语义定义**：`[BE]` = 每一步数值都由 Python/pandas 算出、模型不参与；`[AI]` = 有真实 LLM 调用点（哪怕只在兜底路径）。按此判据把 `app/` 下所有行重新标了一遍（`tools/*`、`schemas_tool.py`、`schemas_workflow.py`、`orchestrator.py`、`analysis.py`、`charts.py` 等从 `[AI]` 改成 `[BE]`），**全树 `[AI]` 只剩 5 个文件**（`llm/` 三个 + `planner.py` + `validation.py`）。这一句同时把"不能只靠换系统提示词就声称做了多智能体"变成**可数判据**：数 `[AI]` 就知道模型参与了几步 |
| 6 | `routes_workflows.py` 标 `[D3]`、正文放在 D6 | 已改 `[D6][BE]`。Lead 给的**通用规则**（以后不用再问）：**标注/编号与正文事实打架时，一律以正文事实为准并改标注** |
| 7 | 验收脚本要**不区分大小写**；另发现**编码会让行号错** | §14.2 的 PowerShell 改为显式 `-imatch`（并注明 `-match` 默认就不区分大小写、`-cmatch` 才区分，但判据写死不靠默认行为），正则用 `业务错误一律\s*http\s*200` 兜住 `HTTP 200` / `http 200`，脚本末尾直接打印 `非白名单节命中 = N` 作判据。**同时补了第 ② 个坑（实测发现）**：`Get-Content` 在 PowerShell 5.1 下按系统 ANSI 代码页（本机 GBK）读文件，读 UTF-8 中文文档会**乱码且行号整体前移**——同一份文档加 `-Encoding UTF8` 得 1463 行、行号与编辑器一致，不加只有 1068 行。所以脚本里必须 `-Encoding UTF8`（`Select-String` 同理） |
| 8 | `app/main.py` 的第 4 个 handler（兜底 `Exception` → 500） | 此前已在 §7 补上"四个 handler"，本轮核对无误 |

**给后来者的一条提醒**：本轮的全部改动都是"**把已有代码的事实写进文档**"，没有一处是新增设计。这类同步的价值在于：`Settings` 字段名、`ERROR_STATUS` 映射、`requirements.lock.txt` 版本这些内容**写错不会报错、只会静默失效**（例如自己造一个 `max_dataset_rows`，默认值一直在起作用，看起来代码没问题）。所以每次契约冻结后都应该做一次这样的核对，而不是等到写代码时才发现名字对不上。

### 14.4 第二阶段门禁（task-5）的对齐记录

基线：`eb276df`（第 2 天收尾）→ `7465e8e`（DECISIONS 计数口径）→ `2c2d2c1`（本节）。核对方法：脚本把 §2 目录树点名的文件、§4.0.1 的字段名分别与真实仓库做集合比对（**不靠人眼扫**，因为这类偏差的特征正是"看不出来"）。

**查出的偏差（都是"文档说 A、代码做 B"，没有一条会让测试变红）**：

| # | 偏差 | 处理 |
|---|---|---|
| 1 | §4.0.1 表里没有 `temp_dir`（代码 12 个字段 vs 表里 11 个） | 表格补一行（含"不是给业务代码读的"这句用途说明）；表头"11 个字段"改 12；`app/config.py` 的 `temp_path` docstring 一并扩写。提交 `2c2d2c1` |
| 2 | §2 目录树把已交付的文件仍标成计划（`schemas_dataset.py`、`errors.py`、`routes_datasets.py`、`data/` 三个、`test_profiler.py`） | 全部改为 `(已存在, <首次交付的 commit>)` |
| 3 | §2 目录树漏了 11 个真实存在的文件（`conftest.py`、`.gitattributes`、`scripts/`、`docs/DECISIONS.md`、`docs/STAGE2_ACCEPTANCE.md`、`docs/REVIEW_ARCHITECTURE.md`、`tests/__init__.py`、`tests/test_errors.py`、`tests/test_store.py`、`tests/test_data_layer_e2e.py`、`tests/test_temp_dir.py`、`tests/test_sample_data.py`） | 全部补进树 |
| 4 | 树里写的 `tests/test_upload_api.py` **不存在**，真实文件名是 `tests/test_upload.py`（`9441429` 交付） | 改名并加一句"**注意文件名是 test_upload.py，不是 test_upload_api.py**" |
| 5 | 树里 `app/runtime.py` 只写 `STARTED_AT + uptime_seconds()`，漏了 `24aeac1` 新增的 `runtime_environment()` | 补上 |
| 6 | 树里 `config.py` 行写"已有 max_upload_mb(43) / upload_path(54) / max_upload_bytes(60)"——**行号是硬编码的，早就不对了** | 改成指向 §4.0.1 并写明"字段表为准，别信本文行号" |
| 7 | `core/` 两个文件标 `[D2][BE]`，正文与排期表都是 D3 | 改 `[D3][BE]`（沿用 §14.2 第 6 条定的通用规则：**标注与正文打架时以正文为准**） |
| 8 | 附录 B 仍写"已在的 11 个"、`ErrorCode` 仍写"D2 只在其中追加 `WORKFLOW_TIMEOUT`" | 分别改成 12 个与"共 8 个码"；`app/runtime.py` 那行从"不动"改成"已改过（24aeac1）"；补上 `app/errors.py` 一行（含"`AppError.status_code` 留着别删"） |
| 9 | §2 树把 `data/samples/sales.csv` 标 `[D3][LEAD]` | 改 `(已存在, c15aa92)`；行尾策略指向 D-15 |

**查证无误、不动的**：`HealthData` 含 `upload_dir_ready`；§7.3 的两个代码块与 `app/errors.py` 逐行一致（8 个码、顺序 400/404/405/413/415/503/200/500）；§6.4 的 7 个工具与 `TOOL_NAMES` 一致；"业务错误一律 HTTP 200"只出现在 §14.2 的变更记录里；`app/datasets.py` / `app/security/files.py` / `sales_sample.csv` 三个作废名字只在 §2.1 末尾与 §14.2 里。

**留下的教训（与 §14.3 末尾那条同源）**：目录树和字段表这类"清单式文档"的腐烂方式是**加法腐烂**——代码只增不减，文档不加就悄悄落后；而它偏偏不会让任何测试变红。所以核对方式必须是脚本做集合比对，并且要**双向比**（文档有代码无 / 代码有文档无），只比一个方向会漏掉一半。

### 14.5 第三天契约冻结（commit `de86db2`）后的文档同步记录

基线：`de86db2`（父 `fcf9546`）—— D3 契约冻结，新增 `app/schemas_tool.py`（130 行）与 `app/schemas_workflow.py`（359 行）。本轮**只改 `docs/ARCHITECTURE.md`**，未触碰任何 `.py` / `tests/`。

核对方法与 §14.4 同源：用 `ast.parse` 读两个模型文件，取**顶层** `ClassDef`（连同它的字段名）/ `Assign` / `AnnAssign`，与 §4 各节的表格做**双向集合差**。可复用骨架（脚本写在项目外的 `%TEMP%`，不进仓库）：

```python
import ast, io
names = {}
for path in (r"app\schemas_tool.py", r"app\schemas_workflow.py"):
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in tree.body:                                   # 只看顶层定义
        if isinstance(node, ast.ClassDef):
            names[node.name] = [s.target.id for s in node.body
                                if isinstance(s, ast.AnnAssign)]      # ← 字段名
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):  # 类型别名 / 模块级常量
            t = node.targets[0] if isinstance(node, ast.Assign) else node.target
            names[getattr(t, "id", "?")] = []
# 再与文档表格做双向差集：代码有表无 / 表有代码无，两个方向都要打印
```

**脚本判定的偏差（修前 → 修后）**：

| # | 偏差（脚本判定，不是印象） | 处理 |
|---|---|---|
| 1 | 文件归属表（当时是 §4.12，本轮为给新小节腾位置顺延为 **§4.13**）把 `ToolSpec` 算进 `app/schemas_tool.py`，但该文件顶层只有 4 个定义：`ToolErrorCode`、`ChartType`、`ToolResult`、`ChartResult`；而 §6.1 早就写明 `ToolSpec` 在 `app/tools/registry.py` | 表里删 `ToolSpec`、补两个 `Literal` 别名；另起一行 `app/tools/registry.py` = `ToolSpec`；§2 目录树与 §4 开头的归属规则各加一句"ToolSpec **不在** `schemas_tool.py`"。**这样 §4 与 §6.1 不再互相矛盾** |
| 2 | `app/schemas_workflow.py` 顶层 **15** 个定义，表里只写了 11 个（漏 `WorkflowStep`、`TERMINAL_STATUSES`、`WorkflowStatusData`、`TaskBrief`） | 表补齐，并写明"共 15 个顶层定义" |
| 3 | `app/schemas_dataset.py` 的 `FileExt`、`SemanticType` 两个别名从未进过这张表 | 一并补上（同一张清单，漏一个下次还会漂） |
| 4 | `WorkflowStatusData` / `TaskBrief` **全文没有定义**，但 §7.1 的代码示例早就在用 `WorkflowStatusData.from_state(state)` —— 文档引用了一个自己没定义的模型 | **新增 §4.12 整节**：为何不直接返回 `WorkflowState`（体积 + 契约稳定）、为何 `has_report` 现算、13 个字段表、`TaskBrief` 4 个字段 |
| 5 | §4.4 写 `tasks` "最多 `max_steps`（默认 6）个"，代码是 `max_length=8` 的**硬上限** | 改成"硬上限 8；`max_steps` 只是请求侧旋钮，突破不了 8" |
| 6 | §4.5 写 `step` 是 `str`、必填；代码是 `WorkflowStep`（`Literal`）且有默认值 `"profiling"` | 改成七个取值 + 默认值（"必填"列由「是」改「否」） |
| 7 | §4.6 把终态集合只写在一句中文里；代码里它是模块级 `TERMINAL_STATUSES` | 写明常量名、类型 `frozenset[WorkflowStatus]`、以及"接口/前端/测试三处 import 同一个常量"；另加一句：`TaskStatus` 与 `WorkflowStatus` 是两个枚举，别互相赋值 |
| 8 | §4.7 `error_code` 类型写成 `str \| None`；代码是 `ToolErrorCode \| None`（5 选 1 的 `Literal`） | 改类型，并点明它与 `app/schemas.py` 的 `ErrorCode` 是**两套码** |
| 9 | §4.3 代码里的约束（`task_id` 正则 `^T[0-9]{1,2}$`、`attempt` 的 `le=2`）文档没写 | 补进字段表 |
| 10 | `app/schemas_dataset.py` 的 `ALLOWED_EXTENSIONS`（模块级 `frozenset`，`FileExt` 的取值集合）也没进表。**这一条是脚本的盲区自己暴露出来的**：第一版脚本只判 `ast.Assign`，而它是**带注解的赋值**（`TERMINAL_STATUSES` 同理），补上 `ast.AnnAssign` 分支后才看见 | 补进 §4.13 表（并写明该文件"共 7 个顶层定义"），§2 目录树那一行同步改成"FileExt / ALLOWED_EXTENSIONS / SemanticType 三个模块级别名常量 + 4 个模型"。**教训：检查工具本身也会漏，第一版的"全部一致"是假绿** |

**修后复核（同一次脚本重跑，双向差集）**：

```text
app/schemas_tool.py     : 文档 4  / 代码 4   → 无差异
app/schemas_workflow.py : 文档 15 / 代码 15  → 无差异
app/schemas_dataset.py  : 文档 7  / 代码 7   → 无差异
app/schemas.py          : 文档 4  / 代码 4   → 无差异
```

**查证无误、不动的**（脚本输出"字段集合一致 ✓"）：§4.4 `TaskPlan`(7 字段)、§4.5 `WorkflowState`(22)、§4.7 `ToolResult`(10)、§4.8 `ChartResult`(10)、§4.9 `ValidationResult`(5)、§4.10 `FinalReport`(8)、§4.11 `QuestionRequest`(3)、§4.12 `WorkflowStatusData`(13，本轮新增的小节首次纳入比对) —— 字段名集合与代码**一个不多一个不少**。§4.3 `AnalysisTask`(11) 也一致（该小节有两张表：`TaskStatus` 6 个成员 + `AnalysisTask` 11 字段，按节聚合的脚本会把它们混在一起，所以这一节要按表分开看，这一项是本轮唯一需要人眼确认的）。`TaskBrief`(4) 在 §4.12 里是按行内列表写的、不是表格，所以不在脚本比对范围内，靠人核。

**脚本里两条必须写对的细节**（否则"全部一致"是假的）：① 锚点要指向**文件归属表**所在的小节（本轮它从 §4.12 挪到 §4.13，脚本没跟着改的话会读到空表，输出"代码有、表里没有：全部"）；② 表格第二列里的中文括注（如"（两个 `Literal` 别名）"）要先剥掉再抽反引号词，否则 `ChartType`（两个 `Literal` 别名）会被当成一个名字，diff 里全是噪音。

**`ChartResult` 的约定已经从"文档提醒"升级成"会报错的代码"**：`de86db2` 起带 `@model_validator(mode="after")`，构造时检查 `filename == chart_id + ".png"` 且 `url.endswith("/charts/" + filename)`。§4.8 结尾补了这一段——**文档里的提醒和构造时抛错的校验，强度差一个量级**。

**量过的数字（合成状态，脚本 `d3_payload.py`）**：6 个任务 / 6 个工具结果（各 20 行）/ 1 张图 / 3 段报告 → `WorkflowState.model_dump_json()` = **14 431 B**，`WorkflowStatusData.from_state(state).model_dump_json()` = **1 318 B**（10.9 倍）。另把 `state.report` 置回 `None`、故意保留陈旧的 `state.has_report = True`，`from_state()` 返回 `has_report=False` —— 证明接口读的是现算值。**这是合成数据，不是真实工作流**；量法与原始输出只在本节可追溯，没写进 §4.12 的结论以外的地方。

**本轮明确没动的两处（当时等 backend 的提交）**：§2 目录树里 `app/core/*` 与 `app/tools/*` 当时全是计划标注——那两个包在 `de86db2` 时**磁盘上还不存在**（`core/`、`tools/` 均未落地）。**已在 §14.6 补齐**（`15edd69` + `c41afc0` 之后按实际文件同步），本节这一句保留为历史记录。

**这一轮最值得记住的一条**："文档引用了一个自己没定义的模型"（第 4 条）比字段写错更隐蔽：**引用会先于定义出现**（写接口契约时先想清楚返回体长什么样，模型却晚一步才落地）。若没人做集合比对，它会一直停在"读起来自洽"的状态——§7.1 的示例代码一直在用 `WorkflowStatusData.from_state(state)`，而 §4 里当时没有这个模型。

### 14.6 工具层 / 执行层落地（`15edd69`、`c41afc0`）后的目录树同步

基线：`c41afc0`（父 `15edd69` ← `de86db2`）。

- `15edd69`「feat(tools): D3 工具层 7 个白名单工具 + 4 类校验 + 返回值清洗」：新增 `app/tools/` **8 个 .py**、`tests/test_tool_registry.py`（209 行）、`tests/test_tools.py`（653 行）。
- `c41afc0`「feat(agents): D3 执行层」：新增 `app/agents/{__init__,analysis,planner}.py`、`app/core/{__init__,registry}.py`、`tests/{test_workflow_registry,test_analysis_agent,test_planner_rules}.py`。

§14.5 末尾那句"等 backend 的提交"指的就是这两个包，**本轮已按实际磁盘同步**。

**核对方法（这次不看字段，看文件）**：脚本读 §2 树里每个包的代码块（`app/core`、`app/tools`、`app/agents`、`tests`），用 `([A-Za-z_][A-Za-z0-9_]*\.py)` 抽出名字集合，与 `os.listdir` 做**双向**差集；再补一条全局检查"`app/` 下每个 .py 是否在树里出现过"。判据是这两个方向都为空（计划中的文件除外，它们本来就不该在磁盘上）。

**脚本输出（修前，行号是修前快照）**：

```text
--- app/core (文档块 L172-175) ---
  磁盘 2 个 / 文档 4 个
  >> 磁盘有、文档没有: 无
  >> 文档有、磁盘没有: background.py（D4 计划，保留）
                        schemas_workflow.py ← 解析噪音，不是树条目（见下）
--- app/tools (L176-182) ---
  磁盘 8 个 / 文档 6 个
  >> 磁盘有、文档没有: jsonable.py、validate.py
--- app/agents (L187-193) ---
  磁盘 3 个 / 文档 6 个
  >> 磁盘有、文档没有: 无
  >> 文档有、磁盘没有: orchestrator.py、charts.py、validation.py（D4/D5 计划，保留）
--- tests (L198-215) ---
  磁盘 17 个 / 文档 18 个
  >> 磁盘有、文档没有: test_analysis_agent.py、test_contracts_d3.py、
                       test_planner_rules.py、test_tool_registry.py、test_workflow_registry.py
  >> 文档有、磁盘没有: test_analysis_api.py、test_charts.py、test_security.py、
                       test_upload_api.py、test_validation.py、test_workflow_state.py
B. app/ 下未出现在树里的 .py: app\tools\jsonable.py、app\tools\validate.py（修前）
```

**两个必须说明的解析噪音**（否则这份输出会被误读成"文档多写了两个文件"）：

1. `schemas_workflow.py` 被算进了 `app/core` 块——它出现在 `registry.py` 那行的注释里（"模型全在 `schemas_workflow.py`"），**不是树里的文件条目**。教训：按块抽名字会把注释里的提及一起捞进来，判据要看"行首是不是真的树条目（`├─`/`└─`）"。
2. `test_upload_api.py` 来自文档里那句刻意写的提醒"**注意文件名是 `test_upload.py`，不是 `test_upload_api.py`**"——它**不是**计划中的文件，是反例说明。这条提醒从 D1 就在，保留。

**修后复核（同一次脚本重跑，改完 §2 之后）**：

```text
--- app/tools  --- 文档 8  / 磁盘 8   >> 磁盘有、文档没有: 无
--- app/agents --- 文档 6  / 磁盘 3   >> 磁盘有、文档没有: 无
                                      >> 文档有、磁盘没有: charts.py、orchestrator.py、validation.py（D4/D5 计划，预期）
--- app/core   --- 文档 4  / 磁盘 2   >> 磁盘有、文档没有: 无
                                      >> 文档有、磁盘没有: background.py（D4 计划）+ schemas_workflow.py（注释噪音）
--- tests      --- 文档 23 / 磁盘 17  >> 磁盘有、文档没有: 无
                                      >> 文档有、磁盘没有: test_analysis_api / test_charts / test_security /
                                                          test_validation / test_workflow_state（D4–D6 计划）
                                                          + test_upload_api.py（反例说明）
B. app/ 下未出现在树里的 .py: 无
```

判据：**"磁盘有、文档没有" 四个包全部为 0**；反向"文档有、磁盘没有"只剩计划项与上面两个已解释的噪音。

**改了哪些行**：

| 位置 | 原来 | 现在（依据） |
|---|---|---|
| §2 `app/tools/` | 6 行、全是 `[D3][BE]` 计划标注 | 8 行、全是 `(已存在, 15edd69)`；新增 `jsonable.py`（`to_jsonable` / `ensure_jsonable`，**校验 5 的落点**）与 `validate.py`（`validate_call(tool_name, params, df)` + `ToolValidationError`，**校验 1–4 按顺序前置短路**）；下面补一行"本目录 8 个 .py ≠ 7 个工具函数" |
| §2 `app/agents/` | `__init__` / `analysis` / `planner` 都写 `[D4]` | 三行改成 `(已存在, c41afc0)`（它们在 `c41afc0` 已落地）；`planner.py` 的 **`[AI]` 保留**——今天只有规则路径，而 `[AI]` 的判据是"有真实 LLM 调用点，哪怕只在兜底路径"；`orchestrator.py` / `charts.py` / `validation.py` 仍是计划标注 |
| §2 `app/core/` | 两行 `[D3][BE]` | `(已存在, c41afc0)`；`background.py` 保留 `[D4][BE]` |
| §2 `tests/` | `test_tools.py` 是 `[D3][QA]` 计划 | 改成 `(已存在, 15edd69)`；新增 5 行：`test_tool_registry.py`（`15edd69`，209 行）、`test_workflow_registry.py`（`c41afc0`，151 行）、`test_analysis_agent.py`（`c41afc0`，312 行）、`test_planner_rules.py`（`c41afc0`，372 行）、`test_contracts_d3.py`（**工作区新增、尚未提交**） |
| §14.5 末尾 | "本轮明确没动的两处（等 backend 的提交）…此刻在磁盘上还不存在" | 改成"当时…**已在 §14.6 补齐**"，保留为历史记录 |

**`test_contracts_d3.py` 的量过的数字**（我跑的，不是转述）：544 行、24 个 `def test_` 函数，`python -m pytest tests/test_contracts_d3.py --collect-only -q` 输出 **32 tests collected**（差额来自 `parametrize`）。它覆盖的是"契约能不能真的序列化出去"：`model_dump(mode="json")` + `json.dumps(allow_nan=False)` 必须通过、`ChartResult` 的 `filename`/`url`/`chart_id` 三者一致、`WorkflowState` 派生字段与各种限额。

**两个会持续踩的数字**：

- `app/tools/` 磁盘上 **8 个 .py** vs 白名单 **7 个工具函数**（`describe_dataset`、`missing_values`、`group_aggregate`、`top_n`、`compare_groups`、`correlation`、`time_series_summary`）——前者多出的 4 个是 `__init__.py` / `registry.py` / `jsonable.py` / `validate.py`，它们不是工具。
- `tests/` 磁盘 **17 个 .py**，其中 **5 个还没写**（`test_workflow_state.py` / `test_analysis_api.py` 是 D4，`test_validation.py` / `test_charts.py` 是 D5，`test_security.py` 是 D6）。第 18 个"文档里有的" `test_upload_api.py` 是反例说明，不是计划项。

**本轮明确没动的部分**：`app/llm/`、`app/agents/{orchestrator,charts,validation}.py`、`app/api/routes_analysis.py`、`app/core/background.py`、`app/web/`、以及 `tests/` 里那 5 个 D4–D6 计划项——它们尚未落地，保留计划标注。

**§2 与 §14.6 里 `test_contracts_d3.py` 那一行已按 Lead 的提交 `555cb80` 更新**（原文写的是"工作区新增未提交"）。这条本身也是个教训：**文档里"尚未提交"这类状态词一定会过期**，而它不会让任何测试变红 —— 所以写文档时能写 hash 就写 hash，写不了 hash 就写"截至 <基线>"。

### 14.7 第四天契约冻结（D4）的同步记录

**基线**：`5123fb7`（D3 收尾完成，全量 352 passed）。本节记录 D4 开工前**冻结契约**这一轮的改动，以及**为什么先做这件事**。

**为什么要先冻结**：D4 是第 12.2 节点名的"最大风险点"，也是唯一需要三方同时开工的一天（AI 写 `llm/` + `planner.plan()` + `orchestrator.py`；BE 写 `core/background.py` + `routes_analysis.py`；QA 写 `test_workflow_state.py` / `test_analysis_api.py`）。三方并行时，"接口长什么样"必须由一份**已提交的**文档定义，而不是靠三个人在聊天里对齐 —— 后者会产生"我以为你会传 dict，你传了 Pydantic 对象"这类只能在集成时才暴露的错。所以本轮**先写契约、再派单**。

**本轮改动的 6 个文件**：

| 文件 | 改动 | 为什么 |
|---|---|---|
| `app/schemas_llm.py` | **新建**（`LLMToolCall` / `LLMReply` + `reasoning_content` 校验） | §4.14.2：漏传 `reasoning_content` 的后果是 API 400，不是降级。把它做成构造期约束 |
| `app/schemas_workflow.py` | 新增 `AnalysisAccepted`（16 个顶层定义，原 15） | §8.2 的示例代码从 D1 就在用这个名字，但代码里一直没有它 —— **"引用先于定义出现"** |
| `app/config.py` | 新增 `charts_dir` / `max_workflow_seconds` / `tool_call_limit` + `charts_path` 属性（12 → 15 字段） | §4.0.1 定过纪律"写配置相关代码前先查表"；一次补齐，避免 D5 再造一个名字相近的字段 |
| `.env.example` | 补 4 个键（`CHARTS_DIR` / `MAX_WORKFLOW_SECONDS` / `TOOL_CALL_LIMIT`）+ DeepSeek 填法示例 | 用户已选 DeepSeek 官方 API。示例里写明模型名是 `deepseek-flash`，因为旧文档的 `deepseek-chat` 已不是推荐写法 |
| `docs/ARCHITECTURE.md` | 新增 §4.14（4 小节）、§14.7（本节）；同步 §4.0.1（12→15 字段 + 4 个计算属性）、§4.13（新增 `schemas_llm.py` 行、`schemas_workflow.py` 15→16）、§2 目录树（`config.py`/`schemas_workflow.py`/新增 `schemas_llm.py`/`planner.py`/`background.py`/`test_contracts_d4.py`） | 新增的代码让这四处表格全部过期 |
| `tests/test_contracts_d4.py` | **新建**（25 项：19 passed + 6 skipped） | 见下 |

**`test_contracts_d4.py` 的"棘轮"设计**：D3 的契约测试只能钉已存在的模型，D4 还要冻结**五个还没写的函数签名**（`chat` / `plan` / `run_workflow` / `submit` / `start_analysis`）。所以：

- 模块或函数**不存在** → `pytest.skip`（D4 开工前这是正常的，不制造假红）
- 存在但参数名/顺序/默认值与 §4.14.3 不符 → **fail**（有人改了约定就必须回来看契约）

**"跳过条数应当在 D4 结束时归零"是一个可数的验收判据** —— 而不是"感觉写完了"。这也是本设计的主要目的：把"契约有没有被遵守"变成一条 `pytest` 输出里能直接读到的数字。

**变异测试（`docs/DECISIONS.md` D-16 第 3 条：期望值必须来自已观测事实）**：基线 `(19, 0, 6)`，三个变异分别打在"validator / 接口形状 / 配置默认值"三类断言上：

| 变异 | 结果 | 说明 |
|---|---|---|
| 删掉 `LLMReply` 的 `reasoning_content` 校验 | `1 failed, 18 passed, 6 skipped` | 钉住"漏传会被拦住" |
| `AnalysisAccepted` 多带一个 `retry_count` 字段 | `2 failed, 17 passed, 6 skipped` | 钉住"不泄漏内部字段" |
| `charts_dir` 默认值改成 `"charts"` | `2 failed, 17 passed, 6 skipped` | 钉住配置默认值 |

还原后复跑 = `(19, 0, 6)`，与基线逐字一致。**三个变异红数都 > 0，没有假绿。**

**实测的工具事实（不是转述文档）**：

- `LLMReply.message` 的 `reasoning_content` 校验会拦下"拿到了却没写进 message"，错误信息里含 `reasoning_content` 字样。
- `Settings` 字段数实测 **15**（`list(Settings.model_fields)`）。
- 顶层定义数实测：`schemas.py` 4 / `schemas_dataset.py` 7 / `schemas_tool.py` 4 / `schemas_workflow.py` **16** / `schemas_llm.py` **2**。
- 全量测试 **371 passed, 6 skipped**（原 352 passed + 19 新增通过 + 6 棘轮跳过）。

**关于 DeepSeek API 的两条运行时事实（Lead 用 `httpx.MockTransport` 截获真实请求体实测，不是读文档得出的）**：

1. openai SDK 2.41.0 下，`message.reasoning_content` **可读**（落在 `model_extra` 里，`model_dump()` 也含它）。
2. `messages.append(sdk_message)` 与手工拼 dict **两条路径实际都把 `reasoning_content` 发出去了**。

**尽管如此，契约仍规定手工拼 dict**：能 work 是因为 SDK 把未知字段塞进 `model_extra` 并原样序列化 —— 这是**版本相关行为，不是 API 契约**，换版本可能静默丢掉，而丢掉的后果是线上 400。手工拼是显式的、不依赖 SDK 内部机制。

**本轮明确没动的部分**：`app/llm/`（整包）、`app/agents/orchestrator.py`、`app/core/background.py`、`app/api/routes_analysis.py`、`app/agents/{charts,validation}.py`、`app/api/routes_workflows.py`、`app/web/`、以及 `tests/` 里那 5 个 D4–D6 计划项 —— 契约已定，但实现尚未落地。`app/agents/planner.py` 只加 `plan()` 的**签名契约**，函数本身也还没写。

### 14.8 配置来源改为系统环境变量（D-19）后的同步记录

**基线**：`d4fd727`（= `db95df3` 改配置层 + `d4fd727` 钉测试子进程编码；全量实测 **490 passed**）。触发原因是一句用户指令：「别读取.env了，我本地电脑的环境配置里面有deep seek的key」。

**代码侧做了什么**（Lead 落地，本节只记录事实，不改代码）：`app/config.py:61-70` 的 `model_config` 删掉 `env_file`（**不读 `.env`**）；三个大模型字段改用 `AliasChoices` 各认两个名字（`LLM_*` 优先 / `DEEPSEEK_*` 兜底，常量在 `:46-48`）；`llm_model` 默认值改成 `"deepseek-flash"`；根 `conftest.py:230-250` 新增 autouse 夹具 `_isolate_llm_env`（每个用例前清空那六个名字）。决策编号：**D-19**（配置来源改为系统环境变量并认 `DEEPSEEK_*` 三名）、**D-20**（测试不许依赖外壳的编码设置）。

**实测的 6 条行为**（我跑的探针 `.tmp/probe_config_source.py`，不是读代码猜的）：

| # | 输入 | 实测结果 |
|---|---|---|
| 1 | 六个名字全清 | `llm_model == "deepseek-flash"`、`llm_configured is False` |
| 2 | 同时设 `LLM_MODEL` 与 `DEEPSEEK_MODEL` | 取到 `LLM_MODEL` 的值（**`LLM_*` 赢**） |
| 3 | 只设 `DEEPSEEK_MODEL` | 取到它的值（兜底生效） |
| 4 | 只设 `DEEPSEEK_BASE_URL` + `DEEPSEEK_API_KEY` | `llm_configured is True` |
| 5 | cwd 里放一个写着 `LLM_MODEL=FROM_DOTENV_FILE` 的 `.env`，直接 `Settings()` | 仍是默认值 —— **确实不读 `.env`**（`Settings.model_config["env_file"] is None`） |
| 6 | `Settings(_env_file=<那个文件>)` 显式传 | 读到 `FROM_DOTENV_FILE` —— 显式逃生门仍然可用 |

**"环境变量设完不生效"的实测**（写进 README 之前先验过）：`setx` 之后立刻用 `[Environment]::GetEnvironmentVariable(..., "User")` 能读回新值（注册表层已写入），但从**已经在运行的进程**里再启动子进程，拿到的仍是旧环境块 —— 所以"设完必须新开终端、正在跑的服务要重启"是有实测支撑的，不是套话。探针用的是自造变量名（`AIDA_ENV_PROBE`），写完立刻删除；机器级 `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY` 只读"有没有"，值一律不打印、不进文档。

**本轮改的三个文件**（写入范围仅这三个）：

| 文件 | 改了什么 |
|---|---|
| `README.md` | 「配置」一节整段重写：来源 = 进程环境变量、不读 `.env`；Windows 设变量的图形界面路径与 `setx` 两种写法（`/M` = 系统级，需管理员）；**设完要新开终端**；六个名字与优先级表；默认模型 `deepseek-flash`；自检两条（`[bool]$env:DEEPSEEK_API_KEY`、`GET /api/health` 的 `llm_configured`）。测试一节把"三种 `.env` 环境"表换成"夹具清空六个名字"的说明（D-19/D-20）；目录树 `config.py` / `.env.example` 两行注释同步；安全约定第 1 条改为"只从环境变量读"；「设完必须新开终端」那段写明验证用的是**自造的临时变量名**（不碰真实密钥，`/M` 的语义来自 `setx /?`） |
| `.env.example` | 从"模板"改写为**环境变量清单**：开头写明"应用不读它、也不读 `.env`"、要生效该怎么设；三个大模型键补别名说明（`LLM_*` 优先）。**无任何真实值**；示例命令一律用 `<你的密钥>` 占位（全文 `sk-` 出现 **0** 次） |
| `docs/ARCHITECTURE.md` | §4.0.1 增"配置来源"段 + 六个名字对照表 + 夹具说明；表中三个大模型字段的"对应环境变量"列改为两名（前者优先）、`llm_model` 默认值 `""` → `"deepseek-flash"`；计算属性 4 → **5** 个（补 `temp_path`）；§10 第 5 条密钥来源；§2 追加 `.env.example` 定位说明；附录 B 的 `config.py` 行 12 → **15** 个字段；§4.0.1 补"**字段名 → 环境变量名 = 大写**"的映射规则（`case_sensitive=False`、`env_prefix=""`，实测 `APP_NAME`/`MAX_ROWS`/`UPLOAD_DIR`），并区分"名字写错静默失效 / 值写错立刻抛 `ValidationError`"；本节 |

**剩下的 `.env` 提法都有明确指向，不是漏改**：`os.environ` 与 `python-dotenv` 字面上含 `.env` 子串但与 dotenv 文件无关；§10 第 2b 条里"`.env.example` 有 `MAX_ROWS`/`MAX_COLUMNS`"仍成立（清单里有这两个键）；§14.2 与 §14.7 的行是**历史记录**，保留当时的说法。按 `grep -n "\.env"` 复查时，先看本节这一句，再逐处判断。

---

### 14.9 收尾交付（task-27）：三份新文档 + README 进度同步

**基线**：`a677425`（= `a703364` D6 前端单页 + `a677425` 端到端演示脚本 `scripts/demo.py` 与 `docs/DEMO.md`）。本节只记录"文档追上了代码"这件事，**没有一处新增设计**。

**新增的三个文件**（都在 `docs/`）：

| 文件 | 覆盖什么 | 为什么单独成篇 |
|---|---|---|
| `docs/WORKFLOW.md` | 一次提问从进到出：上传七步 → 画像 → 提问（202）→ 六阶段 → 轮询 → 报告 → 图表；状态机与重试上限；六种会变成 `FAILED` 的情况；每个接口的失败表现（含 `WORKFLOW_TIMEOUT` 的 HTTP 码是 **200** 这个反直觉点）；末尾是可直接复制的 PowerShell 复现命令 | 架构文档回答"为什么这样设计"，但没人从"一次请求怎么流动"的角度串过全链路 |
| `docs/INTERVIEW_QA.md` | 12 条问答，每条 `问 / 答 / 追问 / 证据（文件:行号 或 命令）` | 面试要的是"能被追问三层还站得住"的答案，散落在各处会漏 |
| `docs/HIGHLIGHTS.md` | 亮点 5 条 + 难点 5 条（现象 → 根因 → 怎么定位 → 怎么修 → 怎么防复发）+ "最有价值的 3 条" | 踩过的坑不写下来，下次会再踩；写"注意 XX"没用，要写清当时的现象与定位路径 |

**`README.md` 同步了四处**（这是本轮唯一被修改的既有文件）：

| 位置 | 原来 | 现在 |
|---|---|---|
| 第 7 行起的项目状态块 | "第 2 天 / 共 7 天"，只列到上传与画像 | "第 7 天 / 共 7 天、HEAD `a677425`"，列全链路 + 三份新文档的入口链接 |
| 启动后的地址表 | `GET /` = "入口提示" | `GET /` = **分析页面**（返回 `app/web/index.html`），并补 `/static/*` 的去向（`app/main.py:79` 挂载 `app/web/`） |
| 测试一节与目录树 | 期望值 **149 passed**（第二阶段口径） | **594 passed**（实测：`python -m pytest -q`，含 1 条 doctest；不含工作区里尚未提交的 `tests/test_charts_coverage.py` 那 7 条，含它是 601） |
| 目录树与接口表 | 目录树只有 `app/{api,data}`；接口表 6 行 | 目录树补齐 `app/{core,tools,llm,agents,web}` 与 `scripts/demo.py`、`data/charts/`、三份新文档；接口表 11 行（接口 1–8 + `/` + `/static/*` + `/docs`） |
| 七天计划表 | 第 3–7 天全是"未开始" | 每天写"交付了什么 + 提交 hash 区间"（`9d37bcb`→`460cf8c`、`fe155af`→`eb276df`、`de86db2`→`5123fb7`、`8850927`→`aa5b05e`、`d18fdbf`→`618acc2`、`a703364`、`a677425`+本次提交） |

**本轮实测的数字**（真跑过，不是估的）：

- `python -m pytest -q` → **594 passed in 16.94s**（另一个口径：含未提交的 `tests/test_charts_coverage.py` 是 601 passed in 25.63s）。
- 真机端到端（服务起在本机端口，**未配 API Key**，走规则兜底）：上传 `data/samples/sales.csv` → `row_count=2168`、`column_count=9`、`size_bytes=141028`；`POST /api/analysis` → **202**；轮询 → `SUCCESS`、`step=done`、`step_index=6`、`has_report=true`；报告 → `checked_numbers=2`、`issues=0`、`llm_used=false`；图表 → `200 image/png`、**24 020 字节**。

**行号口径（重要）**：本文档与 `docs/WORKFLOW.md` 里的 `app/` 行号取自**当次工作区**，即 HEAD `a677425` 加上 Lead 工作区里一处**尚未提交**的 `app/agents/orchestrator.py` 补丁（+20/−5）。那处补丁给 `_final_status()` 加了第 4 种降级原因（校验查出 error 级 issue → `SUCCESS_WITH_WARNINGS`），`WORKFLOW.md` §3.1 就是按**打完补丁**的版本写的。如果该补丁被回退或改写，`orchestrator.py` 的行号会整体前移（≤146 行前移 1 行、>295 行前移 9~15 行）—— 照 `grep -n` 重新对齐即可，**不要照抄旧行号**。

**本轮明确没动的**：`app/`、`tests/`、`scripts/`、`.env.example`、`.gitignore`、`requirements*.txt` 一律未改（写入范围只有 `README.md` 与三份新文档 + `docs/ARCHITECTURE.md` 本节）。提交时逐文件点名，工作区里 Lead 的 `M app/agents/orchestrator.py` 与 QA 的 `?? tests/test_charts_coverage.py` **没有**进本次提交。

---

### 14.10 D6-SQLite 落地（决策 3 的最终交付）后的文档同步记录

**这件事的性质**：§8.3 / §8.3.1 / §8.3.2 / §14 决策 3 早就把 SQLite 设计写完了 —— **但那是计划**。D6 当天它一行都没写（精力去了前端联调与安全测试），而后来的很长一段时间里，这批文档被**当成了已完成的事实来读**。这次的改动是把这批文字从"计划"落成"已实现"，**并且在正文里如实留下时间线**，不假装它当初就做完了。

**代码侧实际交付的东西**（本节只记录文档同步，设计口径以 §8.3 为准）：

| 文件 | 内容 |
|---|---|
| `app/data/history.py` | `HistoryStore`（标准库 `sqlite3`，短连接 + 一把写锁；表 `workflow_history`，一个工作流一行；`archive()` 是 `INSERT ... ON CONFLICT(workflow_id) DO UPDATE`）、`HistoryRecord`、`HistoryStoreError`、`get_history_store()`（`db_path` 为空时返回 `None` = 归档关闭）、`reset_history_store_cache()` |
| `app/core/archive.py` | `archive_if_terminal(state) -> bool` —— **唯一写入口**；`build_record(state)`。写库失败只记 warning，不影响工作流收尾 |
| `app/config.py` | 第 16 个字段 `db_path`（默认 `data/db/app.db`，环境变量 `DB_PATH`）+ 第 6 个计算属性 `db_file` |
| `app/api/routes_workflows.py` | 接口 9 `GET /api/workflows/history`、接口 10 `GET /api/workflows/history/{workflow_id}`（两个都只读库，**必须声明在 `GET /api/workflows/{workflow_id}` 之前**） |
| `requirements.txt` / `requirements.lock.txt` | **一个字都没加** —— `sqlite3` 是标准库，零新依赖 |

**本文档这次改了什么**（逐处；早先关于“接口 9 不会返回错误”的描述已由下面第 5 项纠正，详见接口总表）：

1. **§4.0.1 配置项总表**：`db_path` 那行从"**D6 追加**（**尚未在 `config.py` 里**）"改成"**D6-SQLite 追加**（已落地）"，并补上默认值、环境变量名、"空串 = 关闭归档"；表下"五个**计算属性**"改成"**六个**"，把 `db_file` 列进去。
2. **§8.3 触发条件表**：新增一段"**已交付（D6-SQLite，D6 之后补做，非 D6 当天完成）**"，用一张对照表逐条写出**原始设计 vs 实际实现**的五处差异（库路径、访问方式、连接模型、写入语句、表结构），并写明**读路径"启动时从库加载"这一半明确不做**；表第 1 行由"已批准的 D6 可选任务"改为"已交付"，并把触发条件"`--workers > 1`"那半划掉、注明**仍然没满足**。
3. **§8.3.1**：开头"D6 **加** SQLite **之后**"改成"**以下四条已按此实现**"；四条约定**原文一条没改**，但每条后面挂上真实函数名（`archive_if_terminal()` 唯一写入口、`HistoryStore.archive()`、`get_history_store()`）；"历史列表（若做）"去掉"若做"。
4. **§8.3.2**：第 2 条从"重启后历史记录仍在"（当时是计划）改成**已做到 + 怎么做到的**（终态时 UPSERT 进 `workflow_history`，一个 ID 一行，进程死了行还在）；第 1、3、4 条**未改**（本来就与实现一致）。
5. **§7 接口总表**：编号总表从 8 行补到 **10 行**，新增接口 9 / 10（含分页参数、返回体、路由声明顺序这个坑）；章标题改成"8 个必需接口 + D6-SQLite 追加的 2 个只读历史接口"，§7.1 加一句限定"本节讲的是原计划 8 个接口"。**接口 9 的错误栏经实现复核已补齐**：正常空库与关闭归档都返回 200；非法分页参数会被框架拒为 422；SQLite/schema 读操作失败可转为 500。
6. **§11 技术选型**：原先只有一行 **"SQLAlchemy / SQLite"**，其中写着"用**已装的** SQLAlchemy"——**那是假的，仓库从来没装过**。拆成两行：**"SQLAlchemy"**（如实写"早期文档写成'已装的'是错的"、用 ORM 在本项目是负资产）与 **"SQLite（历史归档）"**（已采用，写明只做归档写入、读路径仍是内存）。Redis 那一行"SQLite 已经够解决重启不丢"补上限定：**只解决了"重启不丢历史"这一半，"多进程共享状态"没解决**。
7. **§12 开发计划 D6 行**：目标栏保留"第 14 节决策 3 已定为做它、不做 SSE"（**这是当时的事实，不改**），验收栏末尾加第 ⑤ 项，说明**这一项 D6 当天没做完、交付后才补上**。
8. **§13 风险 9**：应对措施原文保留，末尾加一句**事后复盘**——这条风险**真的发生了**，被砍掉的正是 SQLite 历史。
9. **§14 决策 3**："我的建议"行**原文保留**（它记录的是当时的判断，包括那句"用已装的 SQLAlchemy"——保留它才能看出错在哪），但下方**新增两行**：**"⚠️ 时间线（如实记录，不要读成'D6 就做完了'）"** 与 **"⚠️ 落地时改掉的选型（必须写清为什么）"**。
10. **附录 B**：`app/config.py` 行由"已在的 **15 个**"改成"**16 个**"并列出 `db_path`，末尾"若 D6 做 SQLite 再追加 `db_path`"（已完成）改成陈述句；`app/main.py` 行补一句"D6-SQLite 落地时 `main.py` 没有再改"。

**行号口径**：本节与 §8.3 / §7 一律改用**章节名 + 函数名/接口编号**引用，**不写 `文件:行号`** —— 因为这次插入内容后所有后续行号都推移了，而"行号会漂移"正是本仓库已经有 `scripts/check_docs.py` 专门防腐烂的原因。凡确需行号的场合（如 `app/api/routes_workflows.py` 的"必须声明在 `get_workflow` 之前"）都改成了**语义描述 + 守护用例名**（`tests/test_history_api.py`）。

---

### 14.11 文档化端口 8000 → 8128 的同步（README 的启动命令在本机跑不起来）

**怎么发现的**：不是读代码读出来的，是**照 README 原样敲了一遍**。`README.md` 的启动命令写的是 `--port 8000`，本机执行直接失败：

```
ERROR:    [Errno 13] error while attempting to bind on address ('127.0.0.1', 8000):
          [winerror 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。
```

8000 被另一个项目（`D:\deep_agent_search`）占着。`scripts/demo.py:48` 一直用的是 `PORT = 8128`，所以**自动化演示从来没撞上，只有照着 README 手敲的人会撞**。

**改了哪些地方**（同一件事的四类残留）：

| 位置 | 原来 | 现在 |
|---|---|---|
| `README.md` 启动命令（2 处） | `--port 8000` | `--port 8128` |
| `README.md` 手动跑一遍的 4 条 `curl`/`Invoke-RestMethod` | `127.0.0.1:8000` | `127.0.0.1:8128` |
| `docs/WORKFLOW.md` §6 手工复现（7 处命令） | 同上 | 同上 |
| `app/main.py` 的 `allow_origins` | 只有 8000 / 5500 | 补 `127.0.0.1:8128` 与 `localhost:8128` |
| `docs/ARCHITECTURE.md` §10 第 10 条、§12 验收命令 | 只提 8000 | 同步 |

**CORS 那一条是真正的缺陷，不只是文字**：文档改了端口而白名单没改，照文档启动后**从另一个本机页面调接口会被浏览器拦掉**，而且当时没有任何测试会变红 —— `tests/test_error_format.py::test_cors_allows_local_origin_only` 只量了 8000。现在新增 `test_cors_allows_the_port_documented_in_readme`：它**不写死端口号**，而是用正则从 `README.md` 的启动命令里把端口读出来再断言，所以以后"改了文档忘了改代码"会直接红。牙齿证明：删掉 `app/main.py` 里那两行 8128，该用例 `1 failed, 5 passed`，报错是 `assert None == 'http://127.0.0.1:8128'`；还原后字节一致、6 passed。

**同时修掉一处早就漂了的行号引用**：`README.md` 与 `docs/ARCHITECTURE.md` 都写着"`app/main.py:79` 挂载 `app/web/`"，而 `app.mount("/static", ...)` 当时在 **113** 行（改端口后是 118），79 行是一段异常处理代码。**这不是本次改端口造成的**（`git show HEAD:app/main.py` 里就已经是 113），是历史漂移。改法是**引用符号名而不是行号**（"见 `app/main.py` 里的 `app.mount("/static", ...)` 那一行"）—— 行号会漂，符号名不会。这也是为什么 §14.10 已经定过"本节一律不写 `文件:行号`"。

**历史文档不改写**：`docs/STAGE2_ACCEPTANCE.md` 与 `docs/ARCHITECTURE.md` §12 的 D1–D6 验收表里保留当时的 8000（那是**当时真实用过**的端口），只在命令旁加一句"今天照抄请换成 8128"。把历史记录改成今天的数字，等于伪造当时的证据。

**本轮实测**：全量 `python -m pytest -q` → **820 passed in 96.94s**（仓库根）与 **820 passed in 103.50s**（`cd tests` 入口），两处一致；`scripts/check_docs.py` rc=0；`GET http://127.0.0.1:8128/api/health` 在真服务上返回 `ok=true`、`llm_configured=true`。

> 上面这两个 820 是**当时的实测值**，记录不改写。之后 §9.1.3 补了一条
> `test_disabled_buttons_stay_readable`，全量变成 **821 passed**。

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
| `app/schemas.py` | **不改**已有 `ApiResponse`/`ApiError`/`ErrorCode`；`ErrorCode` 现在共 **8 个码**（`BAD_REQUEST`/`NOT_FOUND`/`METHOD_NOT_ALLOWED`/`FILE_TOO_LARGE`/`UNSUPPORTED_FILE`/`SERVICE_UNAVAILABLE`/`WORKFLOW_TIMEOUT`/`INTERNAL_ERROR`，与 §7.0 表逐行对应）。后三个是 `fe155af` 追加的（追加不算破坏） |
| `app/config.py` | **不改**已有字段；已在的 **16 个**以 **§4.0.1 的配置项总表**为准（`app_name`/`app_version`/`upload_dir`/`temp_dir`/`max_upload_mb`/`max_rows`/`max_columns`/`llm_base_url`/`llm_model`/`llm_api_key`/`llm_timeout_seconds`/`llm_max_retries`/`charts_dir`/`max_workflow_seconds`/`tool_call_limit`/**`db_path`**）。配置来源 = **进程环境变量，不读 `.env`**；三个大模型字段自 `db95df3` 起各认两个名字（`LLM_*` 优先、`DEEPSEEK_*` 兜底），`llm_model` 默认 `"deepseek-flash"` —— 见 D-19 与 §14.8。第 16 个字段 `db_path`（D6-SQLite 追加，默认 `data/db/app.db`，环境变量 `DB_PATH`）见 §4.0.1 与 §8.3 |
| `app/main.py` | D6 追加静态文件挂载与首页路由；CORS 白名单保持不动。**已存在的 4 个异常处理器不要动**（`AppError` / `StarletteHTTPException` / `RequestValidationError` / 兜底 `Exception`，见 §7）。D6-SQLite 落地时 `main.py` **没有再改** |
| `app/runtime.py` | **已改过（`24aeac1`）**：除 `STARTED_AT` / `uptime_seconds()` 外新增 `runtime_environment(settings)`，由 `main.py` 的 lifespan 包住 `yield`。文档不重复它的实现，原因见 `docs/DECISIONS.md` D-07 |
| `app/errors.py` | **已存在（`fe155af`）**，本文档 §7.3 的两个代码块要与它逐行一致；`AppError.status_code` 目前没有调用点，**留着别删**（将来"模型不可用"想返回 503 时从调用点传，不要往枚举里塞新码） |
| `tests/test_health.py` | 已随第一阶段返工改过（不再硬断言 `llm_configured is False`，改为"接口报的值 == 配置真实值"）。D6 若把 `/` 改成返回 HTML，**`test_root_returns_documentation_hint` 会失败**——届时必须同步修改测试并在 README 记一笔，这是唯一预期会改动的已有测试 |
| `README.md` | 属于 Project Lead，本设计不改；第 12 节 D7 的工作里包含 README 更新清单 |

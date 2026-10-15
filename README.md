# AI Data Analyst Agent

多智能体 AI 数据分析系统：上传 CSV / Excel，用自然语言提出分析需求，
由主控智能体调度专业子智能体，用真实的 Python 工具完成统计计算与图表生成，
并校验报告中的关键数字。

> **项目状态：第 7 天 / 共 7 天 —— 功能与验证已完成；本轮收尾改动仍待最终提交。**
> （当前提交用 `git log -1` 查；这里刻意不写死 hash —— 写死的那一行每提交一次就变成假话。）
> 已完成：应用骨架与统一错误契约、**文件上传（CSV / xlsx）与数据画像**、
> **7 个白名单分析工具 + 5 类校验**、**大模型接入（没配 key 时自动走规则兜底，
> 报告里会写明 `llm_used=false`）**、**六阶段多智能体编排**（唯一状态所有者 + 重试上限）、
> **图表生成**、**报告数字溯源校验**、**企业级前端工作台**（顶栏 + 侧边导航 + 6 个视图）、**端到端演示脚本**、
> **SQLite 历史归档（D6-SQLite，D6 之后补做）**，817 项自动化测试。
> 一次提问在系统里到底怎么走：[docs/WORKFLOW.md](docs/WORKFLOW.md)；
> 亮点与踩过的坑：[docs/HIGHLIGHTS.md](docs/HIGHLIGHTS.md)；
> 面试里最可能被问的 12 个问题：[docs/INTERVIEW_QA.md](docs/INTERVIEW_QA.md)。
> README 只写实际跑通的东西，不会提前写"已完成"。

---

## 环境要求

| 项目 | 版本 | 说明 |
|---|---|---|
| 操作系统 | Windows 10 / 11 | 本项目在 Windows 11 + PowerShell 5.1 上开发验证 |
| Python | 3.10 及以上（开发用 3.12.8） | 用到 `str \| None` 语法，需要 3.10+ |
| 网络 | 能访问 PyPI | 装依赖时需要 |

## 安装

```powershell
# 1. 进入项目目录
cd "D:\个人项目01\ai-data-analyst-agent"

# 2. 创建虚拟环境
#    --system-site-packages 表示同时复用全局已装的包（本项目大量依赖已全局存在，
#    这样能少下载几百 MB）。如果你希望环境完全独立，去掉这个参数即可。
python -m venv .venv --system-site-packages

# 3. 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 4. 安装依赖（走清华镜像，比官方源快）
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**如果第 3 步报"禁止运行脚本"**，两种解法任选：

```powershell
# 解法 A：改当前用户的执行策略（推荐，只影响你自己，不需要管理员）
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# 解法 B：不激活，每条命令直接用虚拟环境的 python
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

需要"一字不差复现这台机器上验证过的那组版本"时，用同目录的 `requirements.lock.txt`
（只列本项目直接依赖的 12 个包及其实测版本）：

```powershell
python -m pip install -r requirements.lock.txt
```

不要用 `python -m pip freeze > requirements.lock.txt` 自己生成：本项目的 `.venv`
是用 `--system-site-packages` 建的，`freeze` 出来是整台机器上百个包（torch、pymilvus……），
不是这个项目的锁，别人拿它安装会装一堆无关的东西。

## 配置

**配置来源只有一个：进程的环境变量**（Windows 上就是「系统属性 → 环境变量」里那些）。
应用**不读 `.env` 文件**：`app/config.py` 的 `model_config` 里故意没有 `env_file`。
所以网上常见的"复制 `.env.example` 为 `.env` 再填值"这条在本项目**已经无效** ——
`.env.example` 现在只当"有哪些键"的清单用，改它不会影响运行。

### 怎么设（Windows 11）

图形界面：`Win + R` → 输入 `sysdm.cpl` → 「高级」→「环境变量」→ 新建。
「用户变量」只影响你自己、**不需要管理员**；「系统变量」影响整台机器、**需要管理员**。
选错作用域不会报错，只会"看起来设了却不生效"；另外这里点完「确定」也**不会**改变
已经开着的终端（原因和下面命令行那段一样，见那条实测结论）。

命令行等价写法（用户级）：

```powershell
setx LLM_API_KEY "sk-你的密钥"
```

系统级（要管理员 PowerShell；`/M` 表示写到 HKEY_LOCAL_MACHINE，语义实测自 `setx /?`）：

```powershell
setx DEEPSEEK_API_KEY "sk-你的密钥" /M
```

> **设完必须新开一个终端**；已经在跑的服务要**重启**。这条是实测的（用的是自造的
> 临时变量名，避免碰真实密钥；上面 `setx` 那条命令的写法也照这个验过）：`setx` 之后
> `[Environment]::GetEnvironmentVariable("<变量名>", "User")` 立刻能读回新值
> （注册表层已写入），而**启动在 `setx` 之前的进程**里 `$env:<变量名>` 仍然是空的 ——
> 原因：环境变量是进程启动时复制的一份，不是每次读都去查注册表。

设完自检（在**新开的** PowerShell 里）：

```powershell
[bool]$env:DEEPSEEK_API_KEY        # True 表示这个终端能看到
```

### 应用认哪些名字（六个，`LLM_*` 优先）

| 字段 | 认的环境变量名（**前面的优先**） | 默认值 |
|---|---|---|
| `llm_base_url` | `LLM_BASE_URL` / `DEEPSEEK_BASE_URL` | `""`（空 = 明确不接模型） |
| `llm_model` | `LLM_MODEL` / `DEEPSEEK_MODEL` | `deepseek-flash` |
| `llm_api_key` | `LLM_API_KEY` / `DEEPSEEK_API_KEY` | `""` |

- **两个名字都给时 `LLM_*` 赢**。顺序写死在 `app/config.py` 里（`AliasChoices`），不能反：
  反了的话本机那套 `DEEPSEEK_*` 会悄悄盖掉你显式设置的 `LLM_*`，
  而"我明明设了 `LLM_MODEL` 却不生效"是最难查的一类配置问题。
- 模型名有默认值 `deepseek-flash`（实测 `GET https://api.deepseek.com/models` 返回
  `deepseek-flash` 与 `deepseek-v4-pro`）。本机环境只给了 base_url 与 key、没给模型名，
  所以留空也能用；要换模型就设 `LLM_MODEL`。
- 三项都非空时 `settings.llm_configured` 才是 `True`，`GET /api/health` 返回的
  `llm_configured` 就是它 —— 这是"配置到底生效没有"最快的自检。
  第 4 天才真正需要密钥；现在不设也能跑（走兜底模式）。

### 历史归档认哪个名字（一个）

| 字段 | 环境变量名 | 默认值 |
|---|---|---|
| `db_path` | `DB_PATH` | `data/db/app.db` |

- 工作流跑到**终态**时，归档那条记录会写进这个 SQLite 库；**服务重启后仍能查到**（见下面「接口」）。
- **设成空串 `""` 就关闭归档**：不再写库，`GET /api/workflows/history` 返回
  `enabled=false` + 空列表（**HTTP 200**，不是报错 —— 是你主动关了一个功能）。
- 库文件由 `.gitignore` 的 `data/db/*` 挡住，**不会被误提交**（这个规则原本是 `data/db/*.db`，
  后来发现 SQLite 写事务期间还会生成 `app.db-journal`，只挡 `*.db` 不够）。

安全上做了三件事：密钥只从**环境变量**读取（应用不读 `.env`，原因见
[docs/DECISIONS.md](docs/DECISIONS.md) D-19）；`Settings.llm_api_key` 设置了
`repr=False`，打印配置不会带出密钥原文；`.gitignore` 仍然排除 `.env` ——
这一条不是"应用要读它"，而是防止谁本地写了一个 `.env` 之后误提交。

## 启动

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload --port 8000
```

启动后访问：

| 地址 | 用途 |
|---|---|
| http://127.0.0.1:8000/ | **分析工作台**（前端单页：工作台 → 数据集 → 市场分析 → 分析报告 → 历史任务 → 设置，共 6 个视图） |
| http://127.0.0.1:8000/api/health | 健康检查，返回 JSON |
| http://127.0.0.1:8000/docs | 自动生成的接口文档（可直接点按钮试接口） |

页面用的 CSS / JS 由 `/static/*` 提供（挂载的是 `app/web/` 目录，见 `app/main.py:79`）。

`--reload` 表示改代码后自动重启，方便开发；正式跑的时候去掉这个参数。

## 测试

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

预期：**817 passed**（816 条功能测试 + 1 条 doctest）。测试用 FastAPI 的 `TestClient`，
**不需要先启动服务，也不占用端口**（后台任务是在同一个进程里跑的，`TestClient` 会等它跑完）。

其中那条 doctest 来自 `app/errors.py` 里 `sanitize_detail` 的 docstring
（`pytest.ini` 里开了 `--doctest-modules`，`testpaths = tests app`）。
**在仓库根和 `cd tests` 跑都应为 817 passed**（两个入口都会收集到 `app/errors.py` 那条 doctest）。
为什么要跑它：docstring 里写着"输入这个会输出那个"，那就是承诺；不跑的话，
代码改了而注释没改，注释会一直骗人。

> 收集阶段会 import 整个 `app` 包，所以以后往 `app` 里加模块时，
> **不要在模块顶层做有副作用的事**（建目录、读配置文件、发网络请求）——
> 那会在"收集测试"时就发生。
> 目前唯一的例外是 `app/config.py` 里设置 `MPLCONFIGDIR`：matplotlib 在 import
> 时就决定配置目录，等不到服务启动，所以它必须留在那里（临时目录**不**在那里设置，
> 改由启动时显式设置，原因见 `docs/DECISIONS.md` D-07）。

测试**不依赖你 shell 里的环境变量**：根目录 `conftest.py` 有一个 autouse 夹具
（`_isolate_llm_env`），每个用例开始前把那六个名字清空。原因是本机**真的**在
系统环境变量里配了 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` —— 不清的话，
"未配置"这个前提在这台机器上根本不成立，测试就会在没 key 的机器上绿、
在有 key 的机器上红（而后者才是你的机器）。夹具清的是
`app.config.LLM_ENV_VAR_NAMES` 这一份清单，不另外抄一份，免得两边漂移。
决策记录见 [docs/DECISIONS.md](docs/DECISIONS.md) D-19（配置来源改为系统环境变量）
与 D-20（测试不许依赖外壳的编码设置）。

### 交接文档的自检

[docs/HISTORY_ROBUSTNESS.md](docs/HISTORY_ROBUSTNESS.md) 是历史归档健壮性的交接文档，
里面写了大量「代码第 NNN 行是什么」「用例 X 钉住行为 Y」「期望值 200」这类**会腐烂的断言**。
`scripts/check_docs.py` 把它们变成可执行检查：

```powershell
& ".\.venv\Scripts\python.exe" scripts\check_docs.py
```

退出码 `0` = 文档与仓库一致；`1` = 逐条打印对不上的地方。它核对三个维度：
**行号锚点**（文档里的 `` `文件:NNN` `` 必须与代码里自己声明的锚点注释同行）、
**用例存在性**（文档提到的 `test_*` 必须真实存在，用 `ast` 收集）、
**断言方向**（文档写出 `<用例名>[instances=N,requests=M] -> 200`；脚本会执行用例并核对参数实例数、每实例 GET 数及全部响应状态码）。

> 它防腐烂，**不防遗漏**：只核对文档里写了的东西。判断性叙述（「为什么修」）
> 无法机械复核；代码锚点与文档引用**同谋地一起改错**它也看不出来。
> 设计边界与探针有效性证明见该文档第 ⑦ 节。

## 目录结构

```
ai-data-analyst-agent/
├─ app/
│  ├─ __init__.py
│  ├─ main.py                  # FastAPI 实例、CORS、启动钩子、挂载 /static、四个异常处理器
│  ├─ config.py                # Settings：把系统环境变量读成有类型的配置对象（不读 .env，D-19）
│  ├─ runtime.py               # 进程状态 + 启动时的运行环境（临时目录探测与回退）
│  ├─ errors.py                # AppError、错误码到 HTTP 状态码的映射、路径脱敏
│  ├─ schemas.py               # ApiResponse 统一响应外壳、ApiError、ErrorCode
│  ├─ schemas_dataset.py       # Dataset / DatasetSummary / DatasetProfile
│  ├─ schemas_tool.py          # ToolResult / ChartResult（含 filename ↔ url 一致性校验）
│  ├─ schemas_workflow.py      # AnalysisTask / TaskPlan / FinalReport / WorkflowState / WorkflowStatusData
│  ├─ schemas_llm.py           # 模型回复的解析结构（LLMReply）
│  ├─ api/
│  │  ├─ routes_health.py      # 接口 1：GET /api/health
│  │  ├─ routes_datasets.py    # 接口 2-4：上传 / 详情 / 画像
│  │  ├─ routes_analysis.py    # 接口 5：POST /api/analysis（202，立刻返回）
│  │  └─ routes_workflows.py   # 接口 6-10：轮询 / 报告 / 图表 / 历史列表 / 历史详情
│  ├─ core/
│  │  ├─ background.py         # BackgroundTasks 外壳：起后台、兜异常、盖 finished_at
│  │  ├─ registry.py           # 工作流注册表（进程内 dict + 一把锁）
│  │  └─ archive.py            # 终态归档：archive_if_terminal() 是唯一的写库入口（标准库 sqlite3）
│  ├─ data/
│  │  ├─ loader.py             # 读 CSV / xlsx：编码、分隔符、最小清洗、行列上限
│  │  ├─ store.py              # 数据集仓库（DataFrame 只在内存里，不进 Pydantic）
│  │  ├─ profiler.py           # 数据画像：缺失值、重复行、语义类型、五数概括（零 LLM）
│  │  └─ history.py            # HistoryStore：SQLite 归档库（短连接 + 一把写锁，UPSERT）
│  ├─ tools/
│  │  ├─ registry.py           # 白名单注册表：7 个工具的名字 / 参数 schema / 返回结构
│  │  ├─ validate.py           # 5 类校验的前 4 类（工具名 → 参数 → 字段 → 操作）
│  │  ├─ jsonable.py           # 返回值清洗：numpy 标量 / NaN / Timestamp → 纯 JSON 类型
│  │  ├─ describe.py           # describe_dataset / missing_values
│  │  ├─ aggregate.py          # group_aggregate / top_n
│  │  ├─ compare.py            # compare_groups / correlation
│  │  └─ timeseries.py         # time_series_summary
│  ├─ llm/
│  │  ├─ client.py             # OpenAI 兼容客户端：超时 + 重试 + 拿不到合法 JSON 就报错
│  │  └─ prompts.py            # 两处模型调用的提示词（规划 / 写报告）
│  ├─ agents/
│  │  ├─ planner.py            # 问题 → TaskPlan（先走规则；配了 key 才问模型）
│  │  ├─ analysis.py           # 执行单个任务：查白名单 → 校验 → 调工具 → 装 ToolResult
│  │  ├─ charts.py             # 出图（中文字体 + 只摆位置、不做任何聚合）
│  │  ├─ validation.py         # 报告数字溯源校验 + 报告拼装
│  │  └─ orchestrator.py       # 六阶段编排：全项目唯一有权改 WorkflowState 的地方
│  └─ web/                     # 前端单页（挂到 /static）：index.html（外壳+内联 SVG 图标）
│                              #   app.css（设计令牌+组件+两档响应式）/ app.js（fetch+轮询+hash 路由）
├─ scripts/
│  ├─ make_sample_data.py      # 生成示例数据集（固定 seed，可重复）
│  ├─ check_docs.py            # 核对 docs/HISTORY_ROBUSTNESS.md 的断言与仓库现状是否还对得上
│  └─ demo.py                  # 端到端演示：自己起服务、走真实 HTTP 跑完十段（编号 0-9）、自己关掉
├─ tests/                      # 817 项（37 个测试模块），覆盖数据层 / 工具层 / 编排 / 接口 / 安全 / 前端
├─ data/
│  ├─ uploads/                 # 上传文件落地目录（内容不进 git）
│  ├─ charts/                  # 生成的 PNG（可重新生成，不进 git）
│  ├─ db/                      # SQLite 归档库（默认 app.db，由 .gitignore 的 data/db/* 挡住，不进 git）
│  └─ samples/
│     └─ sales.csv             # 示例销售数据：2168 行 × 9 列，故意含缺失值 / 重复行 / 异常值
├─ docs/
│  ├─ ARCHITECTURE.md          # 架构设计（目录树、字段级模型、接口、校验、安全、技术选型）
│  ├─ WORKFLOW.md              # 一次提问从进到出（阶段 / 状态机 / 失败表现 / 手工复现命令）
│  ├─ INTERVIEW_QA.md          # 面试问答 12 条（问 / 答 / 追问 / 证据）
│  ├─ HIGHLIGHTS.md            # 亮点与难点（现象 → 根因 → 定位 → 修复 → 防复发）
│  ├─ DECISIONS.md             # 决策记录：每条都写了"为什么"
│  ├─ DEMO.md                  # scripts/demo.py 的真实运行输出
│  ├─ HISTORY_ROBUSTNESS.md    # 历史归档健壮性：坏数据降级、边界取证、文档自检与变异证据
│  └─ STAGE2_ACCEPTANCE.md     # 第二阶段的验收说明（交付物、怎么自己跑一遍）
├─ .env.example                # 环境变量清单（应用不读它；只有键名与占位值，无真实密钥）
├─ .gitattributes              # 行尾策略：示例 CSV 钉成 LF，保证克隆后字节一致
├─ .gitignore
├─ pytest.ini
└─ requirements.txt
```

## 接口约定

### 成功

```json
{ "ok": true, "data": { }, "error": null }
```

### 失败

```json
{
  "ok": false,
  "data": null,
  "error": { "code": "NOT_FOUND", "message": "Not Found", "detail": "/api/nope" }
}
```

**失败也走同一个外壳**，这一点是特意做的：FastAPI 默认失败时返回 `{"detail": ...}`，
结构和成功响应不一样。前端如果只写一套解析逻辑，就会在报 404 的那一刻崩掉 ——
而那正是最需要把错误提示给用户的时刻。所以 `app/main.py` 里注册了异常处理器，
把框架自己抛的 404 / 405 / 参数校验错误也翻译成上面的格式。

错误码取值：`BAD_REQUEST`、`NOT_FOUND`、`METHOD_NOT_ALLOWED`、`UNSUPPORTED_FILE`、
`FILE_TOO_LARGE`、`SERVICE_UNAVAILABLE`、`WORKFLOW_TIMEOUT`、`INTERNAL_ERROR`。

### 已实现

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | **分析页面**（返回 `app/web/index.html`） |
| GET | `/static/*` | 页面用的 CSS / JS（挂载 `app/web/` 目录） |
| GET | `/api/health` | 服务状态与配置自检 |
| POST | `/api/datasets/upload` | 上传 CSV / xlsx，落盘 + 解析 + 登记 |
| GET | `/api/datasets/{dataset_id}` | 数据集详情（含前 5 行预览） |
| GET | `/api/datasets/{dataset_id}/profile` | 数据画像（首次调用时计算，之后走缓存） |
| POST | `/api/analysis` | **提交分析问题，返回 202 + `workflow_id`**（立刻返回，不等算完） |
| GET | `/api/workflows/{workflow_id}` | 轮询状态：13 个小字段，**故意不含报告** |
| GET | `/api/workflows/{workflow_id}/report` | 完整报告 + 校验结果（`report` / `validation` / `results` / `llm_used`） |
| GET | `/api/workflows/{workflow_id}/charts/{filename}` | 图表 PNG（`filename` 形如 `a1b2c3d4e5f6.png`） |
| GET | `/api/workflows/history` | **历史归档列表**（只读 SQLite 库，新→旧；分页 `limit` 1-100 默认 20、`offset` ≥0 默认 0） |
| GET | `/api/workflows/history/{workflow_id}` | **历史归档详情**（归档记录 + 还原的报告正文；没归档报告时 `report` 为 `null`） |
| GET | `/docs` | Swagger 接口文档（FastAPI 自动生成） |

两个历史接口的边界（**别把它们读成"重启后什么都在"**）：

- **只读**：它们只查库、不查内存，**也不提供"继续运行"** —— 拿着历史 `workflow_id` 去调
  **状态 / 报告 / 图表**三个接口，仍然是 `404 NOT_FOUND`。
- **只覆盖终态工作流**：跑到终态才写库，重启时**正在运行**的工作流不会被恢复，也不会被标成失败。
- **没有鉴权**：归档列表里的任何一条都能被任何人查到（本地单机工具，不开放到公网）。
- **归档关掉时返回 200**：`DB_PATH=""` → `enabled=false` + 空列表，不是 4xx。
- **声明顺序有讲究**：`/api/workflows/history` 与 `/api/workflows/{workflow_id}` 路径形状相同，
  所以历史路由**必须声明在前**，否则会被后者当成 `workflow_id="history"` 吞掉而 404
  （`tests/test_history_api.py` 钉住了这一点）。

分析链路为什么要 202 + 轮询、每个阶段失败时长什么样、六种会变成 `FAILED` 的情况，
都写在 [docs/WORKFLOW.md](docs/WORKFLOW.md)（含可直接复制的 PowerShell 复现命令）。

上传的完整流程（顺序不能换，每一步都对应一个具体的失败场景）：

```
校验扩展名 → 生成 dataset_id → 分块读并累计大小（超限立刻 413，不落盘）
→ 算 sha256 → 落盘为 {dataset_id}.{ext} → 解析（失败就删掉刚写的文件）→ 登记
```

| 情况 | HTTP | error.code |
|---|---|---|
| 上传成功 | 200 | — |
| 不是 csv / xlsx（含旧版 .xls） | 415 | `UNSUPPORTED_FILE` |
| 超过 `MAX_UPLOAD_MB` | 413 | `FILE_TOO_LARGE` |
| 空文件 / 只有表头 / 解析失败 / 超行列上限 | 400 | `BAD_REQUEST` |
| dataset_id 不存在 | 404 | `NOT_FOUND` |

### 手动跑一遍（不用写代码）

先起服务（见上面的"启动"），另开一个 PowerShell 窗口：

```powershell
# 1. 造一个带中文的小 CSV
@'
日期,地区,销售额
2024-01-05,华东,1200
2024-01-06,华北,3400
2024-01-07,华东,5600
'@ | Set-Content -Path "$env:TEMP\sales.csv" -Encoding UTF8

# 2. 上传（PowerShell 5.1 的 Invoke-WebRequest 不支持 -Form，用 curl.exe）
curl.exe -s -F "file=@$env:TEMP\sales.csv" http://127.0.0.1:8000/api/datasets/upload
```

响应里的 `data.dataset_id` 复制出来，接着：

```powershell
# 3. 看详情（含前 5 行预览）
curl.exe -s http://127.0.0.1:8000/api/datasets/<把 id 粘这里>

# 4. 看数据画像（缺失值、重复行、语义类型、五数概括）
curl.exe -s http://127.0.0.1:8000/api/datasets/<把 id 粘这里>/profile
```

画像里的数字全部由 pandas 真实算出。上面这个 3 行的小数据是为了**能手算核对**：
`销售额` 是 1200 / 3400 / 5600，所以 `mean_value` 是 3400、`std_value` 是 2200
（样本标准差，`ddof=1`），可以自己用计算器对一遍。**数据换了，数字就跟着变** ——
没有任何写死的结果。

想看"真实规模的数据长什么样"，直接传仓库里那份示例数据（2168 行 × 9 列）：

```powershell
curl.exe -s -F "file=@data/samples/sales.csv" http://127.0.0.1:8000/api/datasets/upload
```

这份数据是 `scripts/make_sample_data.py` 用固定随机种子生成的，**故意**含有缺失值、
完全重复行和异常值，所以它的画像会真的报出问题来 —— 这正是"数据质量检查"要演示的东西。
它同时保证 `销售额 = 销量 × 单价` 精确成立，方便用 Excel 或计算器核对分析工具的返回值。

> 示例数据是靠 [.gitattributes](.gitattributes) 把行尾钉成 LF 的。原因是本机
> `core.autocrlf=true`，不加这一行的话**干净克隆**出来的 `sales.csv` 会被换成 CRLF，
> 字节数从 141028 变成 143197，那三条"逐字节一致"的测试就会在别人的机器上红。
> 细节和五种写法的对比见 [docs/DECISIONS.md](docs/DECISIONS.md) D-15。

`/api/health` 返回示例：

```json
{
  "ok": true,
  "data": {
    "status": "healthy",
    "app_name": "AI Data Analyst Agent",
    "version": "0.1.0",
    "uptime_seconds": 7.77,
    "llm_configured": false,
    "upload_dir_ready": true
  },
  "error": null
}
```

`upload_dir_ready` 为 `false` 说明上传目录建不出来（权限等问题）。
这个检查放在启动钩子里做，让环境问题在启动日志里就暴露，而不是等用户点上传才报错。

## 安全约定

- API Key 只从**进程环境变量**读取（应用不读 `.env`，见 D-19），仓库内不存任何真实密钥。
- `Settings.llm_api_key` 设置了 `repr=False`，打印配置不会带出密钥原文。
- `.gitignore` 排除 `.env`、`.venv`、`data/uploads/*`、`data/db/*`、`data/charts/*`、`.tmp/`、
  matplotlib 缓存。其中 `.env` 这条是**防止误提交**（谁本地写了一个），不是应用要读它；
  `data/db/*` 挡的是 SQLite 归档库（写事务期间还会产生 `app.db-journal`，所以挡的是整个目录）。
- CORS 只允许本机来源，不使用通配符 `*`。
- 上传只按**扩展名白名单 + 真的能解析成功**两道关判断，不信客户端报的 `Content-Type`。
- 落盘名固定是 `{dataset_id}.{ext}`（32 位十六进制 id），用户原始文件名一个字符都不参与路径；
  写盘前用 `Path.resolve()` 复核目标目录就是上传目录。
- 大小检查是"边读边累计、超限立刻中止"，不是"整个读进内存再判断"。
- 拒绝旧版 `.xls` 时给的是能照着做的话（"请在 Excel 里另存为 .xlsx 后再上传"），不是一句"不支持"。
- 错误响应里的 `detail` 会清洗掉本机绝对路径与用户名（`app/errors.py` 的 `sanitize_detail`）。
- 全项目不使用 `eval` / `exec` / `compile` / `__import__`，不执行任何模型生成的代码。

## 七天计划与当前进度

| 阶段 | 内容 | 状态与提交 |
|---|---|---|
| 第 1 天 | 项目骨架、配置、健康检查、统一错误格式、测试 | ✅ 完成（`9d37bcb` → `460cf8c`），经独立 QA 复验 |
| 第 2 天 | 错误契约收口、数据集模型冻结、上传 / 详情 / 画像三个接口、示例数据集 | ✅ 完成（`fe155af` → `eb276df`），经独立 QA 复验 + 门禁（19 项攻击用例、干净检出全绿） |
| 第 3 天 | 工作流数据结构、7 个白名单工具 + 5 类校验、执行层与主控智能体底盘 | ✅ 完成（`de86db2` → `5123fb7`），D3 契约由 `tests/test_contracts_d3.py` 钉住 |
| 第 4 天 | 接入大模型 API（规划 + 写报告，未配 key 走规则兜底）、六阶段编排、轮询 / 报告 / 图表三个接口 | ✅ 完成（`8850927` → `aa5b05e`） |
| 第 5 天 | 图表智能体、数字溯源校验（六类 issue）、规则路径也配图 | ✅ 完成（`d18fdbf` → `618acc2`），D-22/D-23 记录了两处"修好必须能证明改回去会红" |
| 第 6 天 | 网页交互：上传 / 概览 / 提问 / 六步进度 / 图表 / 报告校验，前后端联调 | ✅ 完成（`a703364`，前端由 `tests/test_frontend.py` 钉住） |
| 第 7 天 | 端到端演示脚本与真实输出（[docs/DEMO.md](docs/DEMO.md)）、完整测试、三份交付文档（[WORKFLOW](docs/WORKFLOW.md) / [INTERVIEW_QA](docs/INTERVIEW_QA.md) / [HIGHLIGHTS](docs/HIGHLIGHTS.md)）、README 与架构文档收尾 | ✅ 完成（`a677425` + 本次文档提交） |
| 第 7 天之后 | **D6-SQLite 补做**：把第 6 天拍板、当天没做完的 SQLite 历史归档落地（标准库 `sqlite3`、零新依赖）+ 两个只读历史接口 | ✅ 完成，决策记录见 [docs/DECISIONS.md](docs/DECISIONS.md) D-24（**如实记：这条是拍板在 D6、D6 没做、之后才补上的**） |

## 已知限制（用之前先看这一节）

- **重启后，数据集与运行中的工作流仍然全丢**：数据集在 `app/data/store.py` 的 dict、
  工作流在 `app/core/registry.py` 的 dict，都只在内存里。老的 `dataset_id` 一律 404；
  老的 `workflow_id` 调**状态 / 报告 / 图表**也一律 404。
- **唯一能在重启后查到的，是已经跑到终态的工作流的归档记录**，并且只能通过
  `GET /api/workflows/history`（列表）与 `GET /api/workflows/history/{workflow_id}`（详情）读。
  它**不是**"重启不丢数据"：归档里没有 `DataFrame`、没有数据集、没有运行态，
  **也不能"继续运行"** —— 它只是"当时那条工作流长什么样"的一份只读记录。
- **仍然必须单进程**：`uvicorn --workers 4` 会让上传落在一个进程、查询落到另一个进程，
  表现就是"刚传完就 404"。**SQLite 归档没有改变这一点** —— 进程内的 dict 依然是唯一的运行态
  事实来源，多进程下它们仍然各自分裂。
- **历史接口没有鉴权**：本地单机工具，归档列表谁都能查；不要直接暴露到公网。
- 归档**可以被关掉**（`DB_PATH=""`）：历史列表返回 200 + `enabled=false` + 空列表；历史详情因无归档记录返回 404 `NOT_FOUND`。

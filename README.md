# AI Data Analyst Agent

多智能体 AI 数据分析系统：上传 CSV / Excel，用自然语言提出分析需求，
由主控智能体调度专业子智能体，用真实的 Python 工具完成统计计算与图表生成，
并校验报告中的关键数字。

> **项目状态：第 2 天 / 共 7 天 —— 上传、数据理解与示例数据集已完成，通过独立 QA 复验，
> 并通过第二阶段门禁（真实 HTTP 19 项攻击用例 + 干净检出全绿）。**
> 已完成：应用骨架、配置层、健康检查、统一错误格式、**文件上传（CSV / xlsx）**、
> **数据预览与详情**、**数据画像（缺失值 / 重复行 / 类型识别 / 五数概括）**、
> **可复现的示例数据集**（`data/samples/sales.csv` + 生成脚本）、149 项自动化测试。
> 验收证据见 [docs/STAGE2_ACCEPTANCE.md](docs/STAGE2_ACCEPTANCE.md)。
> 分析工具、多智能体调度、大模型接入在第 3 天起逐步加入。
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
| http://127.0.0.1:8000/api/health | 健康检查，返回 JSON |
| http://127.0.0.1:8000/docs | 自动生成的接口文档（可直接点按钮试接口） |
| http://127.0.0.1:8000/ | 入口提示 |

`--reload` 表示改代码后自动重启，方便开发；正式跑的时候去掉这个参数。

## 测试

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

预期：**149 passed**（148 条功能测试 + 1 条 doctest）。测试用 FastAPI 的 `TestClient`，
**不需要先启动服务，也不占用端口**。

其中那条 doctest 来自 `app/errors.py` 里 `sanitize_detail` 的 docstring
（`pytest.ini` 里开了 `--doctest-modules`，`testpaths = tests app`）。
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

## 目录结构

```
ai-data-analyst-agent/
├─ app/
│  ├─ __init__.py
│  ├─ main.py                  # FastAPI 实例、CORS、启动钩子、统一错误处理
│  ├─ config.py                # Settings：把系统环境变量读成有类型的配置对象（不读 .env，D-19）
│  ├─ runtime.py               # 进程状态 + 启动时的运行环境（临时目录）
│  ├─ errors.py                # AppError、错误码到 HTTP 状态码的映射、路径脱敏
│  ├─ schemas.py               # ApiResponse 统一响应外壳、ApiError、错误码
│  ├─ schemas_dataset.py       # Dataset / DatasetProfile 等数据契约
│  ├─ api/
│  │  ├─ routes_health.py      # GET /api/health
│  │  └─ routes_datasets.py    # 上传 / 详情 / 画像
│  └─ data/
│     ├─ loader.py             # 读 CSV / xlsx：编码、分隔符、最小清洗、上限
│     ├─ store.py              # 进程内数据集仓库（不写盘）
│     └─ profiler.py           # 数据画像：缺失值、重复行、语义类型、五数概括
├─ scripts/
│  └─ make_sample_data.py      # 生成示例数据集（固定 seed，可重复）
├─ tests/                      # 149 项，覆盖 loader / store / profiler / 接口 / 安全 / 示例数据
├─ data/
│  ├─ uploads/                 # 上传文件落地目录（内容不进 git）
│  └─ samples/
│     └─ sales.csv             # 示例销售数据：2168 行 × 9 列，故意含缺失值 / 重复行 / 异常值
├─ docs/
│  ├─ ARCHITECTURE.md          # 架构设计（含目录树、接口、校验、安全清单）
│  ├─ STAGE2_ACCEPTANCE.md     # 第二阶段的验收说明（交付物、怎么自己跑一遍）
│  └─ DECISIONS.md             # 决策记录：每条都写了"为什么"
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
| GET | `/` | 入口提示 |
| GET | `/api/health` | 服务状态与配置自检 |
| POST | `/api/datasets/upload` | 上传 CSV / xlsx，落盘 + 解析 + 登记 |
| GET | `/api/datasets/{dataset_id}` | 数据集详情（含前 5 行预览） |
| GET | `/api/datasets/{dataset_id}/profile` | 数据画像（首次调用时计算，之后走缓存） |
| GET | `/docs` | Swagger 接口文档（FastAPI 自动生成） |

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
- `.gitignore` 排除 `.env`、`.venv`、`data/uploads/*`、`.tmp/`、matplotlib 缓存。
  其中 `.env` 这条是**防止误提交**（谁本地写了一个），不是应用要读它。
- CORS 只允许本机来源，不使用通配符 `*`。
- 上传只按**扩展名白名单 + 真的能解析成功**两道关判断，不信客户端报的 `Content-Type`。
- 落盘名固定是 `{dataset_id}.{ext}`（32 位十六进制 id），用户原始文件名一个字符都不参与路径；
  写盘前用 `Path.resolve()` 复核目标目录就是上传目录。
- 大小检查是"边读边累计、超限立刻中止"，不是"整个读进内存再判断"。
- 拒绝旧版 `.xls` 时给的是能照着做的话（"请在 Excel 里另存为 .xlsx 后再上传"），不是一句"不支持"。
- 错误响应里的 `detail` 会清洗掉本机绝对路径与用户名（`app/errors.py` 的 `sanitize_detail`）。
- 全项目不使用 `eval` / `exec` / `compile` / `__import__`，不执行任何模型生成的代码。

## 七天计划与当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 第 1 天 | 项目骨架、配置、健康检查、统一错误格式、测试 | ✅ 完成（经独立 QA 复验） |
| 第 2 天 | 文件上传与数据预览、数据画像、示例数据集 | ✅ 完成（见 [docs/STAGE2_ACCEPTANCE.md](docs/STAGE2_ACCEPTANCE.md)），经独立 QA 复验 + 门禁（19 项攻击用例、干净检出全绿） |
| 第 3 天 | 工作流数据结构、主控智能体、数据理解智能体 | 未开始 |
| 第 4 天 | 接入大模型 API、Tool Calling、数据分析智能体 | 未开始 |
| 第 5 天 | 可视化智能体、结果校验、报告生成、串通完整流程 | 未开始 |
| 第 6 天 | 网页交互、前后端联调、任务记录与失败处理 | 未开始 |
| 第 7 天 | 完整测试、演示案例、架构文档、面试准备 | 未开始 |

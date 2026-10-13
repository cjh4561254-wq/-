# AI Data Analyst Agent

多智能体 AI 数据分析系统：上传 CSV / Excel，用自然语言提出分析需求，
由主控智能体调度专业子智能体，用真实的 Python 工具完成统计计算与图表生成，
并校验报告中的关键数字。

> **项目状态：第 2 天 / 共 7 天 —— 上传、数据理解与示例数据集已完成并通过独立 QA 复验。**
> 已完成：应用骨架、配置层、健康检查、统一错误格式、**文件上传（CSV / xlsx）**、
> **数据预览与详情**、**数据画像（缺失值 / 重复行 / 类型识别 / 五数概括）**、
> **可复现的示例数据集**（`data/samples/sales.csv` + 生成脚本）、149 项自动化测试。
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

```powershell
Copy-Item .env.example .env
```

`.env` 已被 `.gitignore` 忽略，**不会提交到 git**。
大模型相关的三个变量现在可以留空，第 4 天才需要填：

| 变量 | 含义 | 示例 |
|---|---|---|
| `LLM_BASE_URL` | 兼容 OpenAI 协议的接口地址 | `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 模型名 | `deepseek-chat` |
| `LLM_API_KEY` | 密钥，**只放在 `.env` 里** | `sk-...` |

安全上做了三件事：密钥只从环境变量 / `.env` 读取；`Settings.llm_api_key`
设置了 `repr=False`，打印配置不会带出密钥原文；`.gitignore` 排除 `.env`。

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
> **不要在模块顶层做有副作用的事**（建目录、读 .env、发网络请求）——
> 那会在"收集测试"时就发生。
> 目前唯一的例外是 `app/config.py` 里设置 `MPLCONFIGDIR`：matplotlib 在 import
> 时就决定配置目录，等不到服务启动，所以它必须留在那里（临时目录**不**在那里设置，
> 改由启动时显式设置，原因见 `docs/DECISIONS.md` D-07）。

测试特意做到"三种环境下都通过"，因为第 4 天你填上真实密钥后测试不该变红：

| 场景 | 用途 |
|---|---|
| `.env` 为空 | 默认开发状态 |
| `.env` 已填 `LLM_*` | 第 4 天之后的真实状态 |
| `.env` 文件根本不存在 | 别人刚 clone 下来时 |

## 目录结构

```
ai-data-analyst-agent/
├─ app/
│  ├─ __init__.py
│  ├─ main.py                  # FastAPI 实例、CORS、启动钩子、统一错误处理
│  ├─ config.py                # Settings：把 .env 读成有类型的配置对象
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
├─ .env.example                # 配置模板（只有占位符，无真实密钥）
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

- API Key 只从环境变量 / `.env` 读取，仓库内不存任何真实密钥。
- `Settings.llm_api_key` 设置了 `repr=False`，打印配置不会带出密钥原文。
- `.gitignore` 排除 `.env`、`.venv`、`data/uploads/*`、`.tmp/`、matplotlib 缓存。
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
| 第 2 天 | 文件上传与数据预览、数据画像、示例数据集 | ✅ 完成（见 [docs/STAGE2_ACCEPTANCE.md](docs/STAGE2_ACCEPTANCE.md)），经独立 QA 复验 |
| 第 3 天 | 工作流数据结构、主控智能体、数据理解智能体 | 未开始 |
| 第 4 天 | 接入大模型 API、Tool Calling、数据分析智能体 | 未开始 |
| 第 5 天 | 可视化智能体、结果校验、报告生成、串通完整流程 | 未开始 |
| 第 6 天 | 网页交互、前后端联调、任务记录与失败处理 | 未开始 |
| 第 7 天 | 完整测试、演示案例、架构文档、面试准备 | 未开始 |

# AI Data Analyst Agent

多智能体 AI 数据分析系统：上传 CSV / Excel，用自然语言提出分析需求，
由主控智能体调度专业子智能体，用真实的 Python 工具完成统计计算与图表生成，
并校验报告中的关键数字。

> **项目状态：第 1 天 / 共 7 天 —— 骨架已跑通并通过独立 QA 复验，业务功能尚未实现。**
> 已完成：应用骨架、配置层、健康检查接口、统一错误格式、16 项自动化测试。
> 上传、分析、智能体调度等在第 2 天起逐步加入。
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

需要"一字不差复现别人的环境"时：

```powershell
python -m pip freeze > requirements.lock.txt   # 导出精确版本
python -m pip install -r requirements.lock.txt # 按精确版本安装
```

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

预期：**16 passed**。测试用 FastAPI 的 `TestClient`，**不需要先启动服务，也不占用端口**。

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
│  ├─ schemas.py               # ApiResponse 统一响应外壳、ApiError、错误码
│  ├─ runtime.py               # 进程级运行时状态（启动时刻 / uptime）
│  └─ api/
│     ├─ __init__.py
│     └─ routes_health.py      # GET /api/health
├─ tests/
│  ├─ test_health.py           # 接口、配置、密钥安全
│  ├─ test_error_format.py     # 404/405/422 统一格式、CORS
│  └─ __init__.py
├─ data/
│  └─ uploads/                 # 上传文件落地目录（内容不进 git）
├─ .env.example                # 配置模板（只有占位符，无真实密钥）
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

错误码取值：`BAD_REQUEST`、`NOT_FOUND`、`UNSUPPORTED_FILE`、`FILE_TOO_LARGE`、`INTERNAL_ERROR`。

### 已实现

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 入口提示 |
| GET | `/api/health` | 服务状态与配置自检 |
| GET | `/docs` | Swagger 接口文档（FastAPI 自动生成） |

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
- `.gitignore` 排除 `.env`、`.venv`、`data/uploads/*`、matplotlib 缓存。
- CORS 只允许本机来源，不使用通配符 `*`。
- 上传文件的格式与大小校验在第 2 天实现。

## 七天计划与当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 第 1 天 | 项目骨架、配置、健康检查、统一错误格式、测试 | ✅ 完成（16 项测试通过，经独立 QA 复验） |
| 第 2 天 | 文件上传与数据预览、示例数据集 | 未开始 |
| 第 3 天 | 工作流数据结构、主控智能体、数据理解智能体 | 未开始 |
| 第 4 天 | 接入大模型 API、Tool Calling、数据分析智能体 | 未开始 |
| 第 5 天 | 可视化智能体、结果校验、报告生成、串通完整流程 | 未开始 |
| 第 6 天 | 网页交互、前后端联调、任务记录与失败处理 | 未开始 |
| 第 7 天 | 完整测试、演示案例、架构文档、面试准备 | 未开始 |

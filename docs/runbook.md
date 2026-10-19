# 运行手册（Runbook）：从零把这套系统跑起来

> 交付物 6「运行与配置说明」的**可执行版**。`README.md` 讲的是"这是什么、有哪些接口"，
> 本文件讲的是"**在一台干净机器上，具体敲哪几条命令，预期看到什么，坏了我怎么查**"。
>
> 所有命令都在 **PowerShell** 里跑，工作目录是**项目根目录**（下称 `<项目根>`，
> 本次开发机上是 `D:\个人项目01\ai-data-analyst-agent`）。
> 文中出现的实际版本号来自本次开发机的实测（Python 3.12.8 / Node 24.16.0）。

---

## 0. 一页速查

| 我想干什么 | 敲这一条 |
|---|---|
| 装依赖 | `& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt` |
| 起服务 | `& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| 打开界面 | 浏览器访问 `http://127.0.0.1:8000/` |
| 跑全部测试 | `& ".\.venv\Scripts\python.exe" -m pytest -q -p no:randomly` |
| 看我的环境对不对 | `& ".\.venv\Scripts\python.exe" scripts\check_env.py` |
| 建账号 / 改密码 | `& ".\.venv\Scripts\python.exe" scripts\manage_account.py --help`（见 §6） |
| 服务起不来 | 见 §7 的「常见问题对照表」 |

---

## 1. 你需要先有的东西

| 组件 | 版本要求 | 本次实测 | 怎么确认 |
|---|---|---|---|
| Python | ≥ 3.11（本项目用 3.12 语法：`X \| None`、`list[str]`） | **3.12.8** | `python --version` |
| pip | 随 Python | 25.x | `python -m pip --version` |
| Node.js | **仅测试用**（跑 `node --check` 检查前端 JS 语法）；服务本身**不需要** Node | **24.16.0** | `node --version` |
| 浏览器 | 任意现代浏览器（前端是手写 SPA，无构建步骤） | Chromium（Playwright 自带） | 直接打开 |

**先说清楚三件事，避免踩坑：**

1. **前端没有任何构建步骤。** `app/web/` 下就是 7 个静态文件（`index.html`/`app.css`/`app.js`/
   `tokens.css`/`login.html`/`login.css`/`login.js`），改完刷新浏览器即可。
   **你不需要 `npm install`，也不需要 `npm run build`。** Node 只在"检查 JS 语法"时用。
2. **数据库是 SQLite，文件型、零配置。** 不需要装 MySQL/PostgreSQL，不需要建库。
   位置默认是项目根下的 `app.db`（可用环境变量 `DB_PATH` 改，见 §5）。
3. **数据集本体存在进程内存里，不落盘。** 服务一重启，**已上传的数据集列表会清空**
   （历史任务/报告存在 SQLite 里，重启后仍在）。这是本期的**已知限制**，不是 bug，
   界面在数据集页会如实告诉用户。详见 `docs/limitations.md`。

---

## 2. 虚拟环境（重要：本项目自带一个 venv，请复用它）

本项目**已经带了一个虚拟环境** `<项目根>\.venv`，里面装好了全部依赖。你只要**激活**它，
或者**直接用它的 python.exe 全路径**（文中一律用后者，因为它不依赖你当前在哪个目录）。

```powershell
# 方式 A（推荐，最不容易出错）：直接用 venv 里的 python，不激活
cd D:\个人项目01\ai-data-analyst-agent
& ".\.venv\Scripts\python.exe" --version
# 预期输出： Python 3.12.8

# 方式 B：激活（激活后当前这个终端里 python 就是 venv 的）
.\.venv\Scripts\Activate.ps1
python --version
```

> **为什么会看到 `Activate.ps1 无法加载，因为在此系统上禁止运行脚本`？**
> 这是 PowerShell 默认的执行策略。两个办法：① 用方式 A；② 只对当前窗口放开：
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`（关掉窗口就失效，安全）。

**如果你想从零建一个自己的环境**（比如换机器）：

```powershell
cd D:\个人项目01\ai-data-analyst-agent
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
```

---

## 3. 装依赖

```powershell
cd D:\个人项目01\ai-data-analyst-agent
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
```

**怎么知道装好了**：这条命令最后会打印一批 `Successfully installed ...`。
再用一条命令确认关键的几个包都在：

```powershell
& ".\.venv\Scripts\python.exe" -m pip list --format=freeze |
  Select-String -Pattern '^(fastapi|uvicorn|pandas|matplotlib|pytest|playwright|httpx|pydantic)='
```

预期看到（版本可能更新，只要不报"找不到"即可）：

```
fastapi==0.136.3
httpx==0.28.x
matplotlib==3.11.2
pandas==3.0.3
playwright==1.5x
pydantic==2.x
pytest==9.1.1
uvicorn==0.3x
```

> **⚠️ 这里**没有** `argon2`，这是对的**。密码哈希用的是**标准库** `hashlib.pbkdf2_hmac`
> （`app/core/auth.py:113`，默认 **240000** 轮 PBKDF2-SHA256），**零第三方依赖**：
> `requirements.txt` 与 `requirements.lock.txt` 里都没有 argon2。
> 早先本文档在这里列过 `argon2-cffi`，是错的 —— 本机之所以"看着有"，
> 是因为 venv 带 `--system-site-packages`、argon2 装在系统级 Python 里。
> **在干净机器上，那条检查会报"找不到"，让人误以为认证装不上。**

**依赖分组说明**：

| 包 | 干什么用 | 不装会怎样 |
|---|---|---|
| `fastapi` / `uvicorn` / `pydantic` | Web 服务与数据校验 | 服务起不来 |
| `pandas` | 读 CSV/Excel、算画像与统计 | 上传数据集直接失败 |
| `matplotlib` | **服务端**画图表（PNG 落盘，前端只展示） | 分析任务能跑，但出图阶段报错 |
| （密码哈希） | **标准库** `hashlib.pbkdf2_hmac`，无需任何包 | — |
| `httpx` | 测试用 HTTP 客户端 | 部分测试报错 |
| `pytest` | 测试 | 跑不了测试 |
| `playwright` | **只测试用**：真实浏览器端到端验收 | 跑不了浏览器验收脚本；**跑服务不需要它** |

> **Playwright 还需要下载浏览器内核**（只做浏览器验收时才需要）：
> ```powershell
> & ".\.venv\Scripts\python.exe" -m playwright install chromium
> ```
> 国内网络慢的话可以加 `--with-deps` 或先设镜像；本项目**运行不需要**这一步。

---

## 4. 起服务

### 4.1 标准启动（开发用）

```powershell
cd D:\个人项目01\ai-data-analyst-agent
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

预期输出（关键几行）：

```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

**这个窗口别关**（关了服务就停）。要停服务就在这个窗口按 `Ctrl+C`。

**自检一下**（另开一个窗口）：

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/health" | ConvertTo-Json -Depth 5
```

预期（真实输出，本机实测）：

```json
{"ok": true, "data": {"status": "healthy", "app_name": "AI Data Analyst Agent",
 "version": "0.1.0", "uptime_seconds": 2.67, "llm_configured": false,
 "upload_dir_ready": true}, "error": null}
```

> **`uptime_seconds` 很有用**：它是这个进程活了多久。
> 当你怀疑"我改的代码怎么没生效"时，先看这个数——如果它有十分钟以上，
> 说明你测的是**旧进程**。

### 4.2 改了后端代码想自动重启

加 `--reload`：

```powershell
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

> ⚠️ **本项目踩过的坑（真实事故）**：不加 `--reload` 起着的服务，是**把当时的代码读进内存**的。
> 之后你改了 `app/api/*.py` 却没重启，浏览器就会拿**旧代码**执行，可能报出
> 已经修好的错误（或者相反：报出磁盘上根本不存在的错误）。
> **规矩：看到任何 500 之前，先确认服务的启动时间。** 这条已写进最终报告 §1。

### 4.3 只想被自己这台机器访问

`--host 127.0.0.1` 就是**只监听本机**，局域网里别的机器访问不到。想让同事访问用
`--host 0.0.0.0`——**但请先读 §8 的安全提醒**：本项目默认没有 HTTPS，会话 Cookie 不带
`Secure`，`0.0.0.0` + 明文 HTTP 会把会话凭证暴露在网络上。

---

## 5. 环境变量（全部可选，不设也能跑）

配置**只从环境变量读**。项目根有一份 **`.env.example`**（58 行，每一项都带中文注释）
列出全部键位和默认值。

> ⚠️ **最容易踩的坑**：`.env.example` 是**给人看的样例**，**应用不会自动读取 `.env` 文件**。
> 想让某个变量生效，必须在**启动服务之前**用 `$env:XXX = "..."` 设进当前 PowerShell 会话
> （或者写进系统环境变量）。设完必须**重启服务**才生效。

### 5.1 常用变量

| 变量 | 默认值 | 作用 | 什么时候要改 |
|---|---|---|---|
| `DB_PATH` | `data/db/app.db` | 归档库（分析历史/报告）的位置 | 想把数据放到别处，或想跑一个"干净实例" |
| `UPLOAD_DIR` | `data/uploads` | 上传文件的落盘目录 | 同上 |
| `CHARTS_DIR` | `data/charts` | 服务端生成的 PNG 存这里 | 同上 |
| `TEMP_DIR` | 系统临时目录 | 服务自己的临时文件 | 同上 |
| `AUTH_DB_PATH` | `data/db/auth.db` | **账号库**的位置（**与归档库是两个文件**） | 做隔离；或想把账号与归档分开管理 |
| `DEMO_LOGIN_ENABLED` | `1`（开启） | 登录页是否显示「进入演示」按钮（一键登录 `DEMO_USERNAME` 账号） | 部署里有真实数据时要设成 `0` 关闭 |
| `DEMO_USERNAME` | `demo` | 演示账号的用户名；账号不存在时第一次点按钮自动创建 | 想换一个不与真实账号冲突的用户名 |
| `MAX_UPLOAD_MB` | `10` | 单个上传文件大小上限（MB） | 要传更大的表 |
| `MAX_ROWS` | `200000` | 单表最大行数 | 同上 |
| `LLM_API_KEY` | **空** | 大模型 API Key | **不配也能用**：报告走"规则模板"生成，不调用大模型 |
| `LLM_BASE_URL` / `LLM_MODEL` | 见 `.env.example` | 大模型服务地址与模型名 | 同上 |

> **为什么账号库和归档库是两个文件？** 因为 `DB_PATH` 置空 = 明确"关闭归档"，
> 而认证不可关闭（关掉就没有登录了）。放同一个文件会出现"为了关归档而清空 `DB_PATH`，
> 结果连账号库一起挪走"的连带伤害。两个库各自独立，谁也关不掉谁。
> 上面这些默认值可以用一条命令打印出来核对：见 §0 的 `scripts\check_env.py`。

> **「演示登录」是什么、有什么代价？** 默认开启：登录页多一个「进入演示」按钮，点一下就用
> `DEMO_USERNAME` 账号的会话进入应用，不需要密码（服务端给这个账号设的是随机密码，用表单登不进去）。
> 它共用本部署的**同一份数据**——演示账号能看到、上传、跑分析，看到的就是你现有的数据。
> 所以部署里有真实数据时，启动前设 `$env:DEMO_LOGIN_ENABLED = "0"` 并**重启服务**关闭。
> 安全闸：账号库为空（还没完成首次部署）时接口直接 403，避免被抢管理员；若 `DEMO_USERNAME`
> 已被一个真实账号占用（创建时的显示名不是「演示账号」），接口也返回 403，不会顶替真实账号。

### 5.2 起一个"隔离实例"（测试/演示常用）

想让实验数据完全不污染你的日常数据：

```powershell
cd D:\个人项目01\ai-data-analyst-agent
$env:DB_PATH      = "$PWD\.tmp\demo\app.db"
$env:AUTH_DB_PATH = "$PWD\.tmp\demo\auth.db"
$env:UPLOAD_DIR   = "$PWD\.tmp\demo\uploads"
$env:TEMP_DIR     = "$PWD\.tmp\demo\tmp"
New-Item -ItemType Directory -Force -Path "$PWD\.tmp\demo" | Out-Null

& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

`$env:` 的赋值**只对当前这个 PowerShell 窗口有效**，关掉就没了——这正是我们想要的。
**`AUTH_DB_PATH` 一定要一起改**，否则账号库仍然是全局那份：隔离出来的实例会用你现有账号登录，
或者因为账号库非空而**看不到**首次部署引导。

### 5.3 不配大模型会怎样（重要，别误判为"坏了"）

`LLM_API_KEY` 为空时：
- **一切照常**：上传、画像、分析、出图、报告、归档都能跑；
- 报告正文由**规则模板**生成，界面上会明确标出「规则模板」这个来源标签；
- 顶栏的"大模型"徽章显示为**未配置**，鼠标悬停会说明原因（这是**如实告知**，不是报错）。

---

## 6. 首次使用：建第一个账号

> **本项目没有"注册"页面，也没有注册接口。** 这是**刻意**的：多租户自助注册涉及
> 邮箱验证、密码重置、滥用防护等一整套能力，本期没有做，所以界面上不给入口
> （给了就是假按钮）。请用下面任一方式建号。

### 6.1 方式 A：浏览器里走"首次部署"引导（推荐，最简单）

1. 起服务，浏览器打开 `http://127.0.0.1:8000/`；
2. **当用户库为空**时，服务会自动把你带到 `http://127.0.0.1:8000/login?bootstrap=1`；
3. 页面上会多出一个「确认密码」框。填用户名 + 密码 + 确认密码，提交；
4. 成功后进入工作台。

> 这个入口**只在用户库为空时可写**。一旦有账号，`POST /api/auth/bootstrap` 一律返回 **403**，
> 页面也会给出明确提示——**不会**成为"任何人在任何时刻都能建管理员"的后门。

### 6.2 方式 B：命令行建号（建第二/第三个账号的唯一方式）

项目自带运维脚本 `scripts/manage_account.py`（**本文档里所有命令都在这台机器上真实跑过**；该脚本的实测原始输出与场景清单见 `docs/runbook-evidence.md`）：

```powershell
cd D:\个人项目01\ai-data-analyst-agent
$env:MIS_NEW_PASSWORD = "换成你自己的密码"        # 密码从环境变量读，不进命令历史
& ".\.venv\Scripts\python.exe" scripts\manage_account.py create `
    --username analyst_li --display-name "李分析"
```

预期输出：

```
建号成功：id=2 username=analyst_li display_name=李分析
```

其他子命令：

```powershell
# 列出所有账号（含各自有几个已登录会话）
& ".\.venv\Scripts\python.exe" scripts\manage_account.py list

# 改密码 —— 会**同时作废该账号所有已登录会话**
$env:MIS_NEW_PASSWORD = "新密码"
& ".\.venv\Scripts\python.exe" scripts\manage_account.py passwd --username analyst_li

# 停用 / 启用
& ".\.venv\Scripts\python.exe" scripts\manage_account.py disable --username analyst_li
& ".\.venv\Scripts\python.exe" scripts\manage_account.py disable --username analyst_li --enable
```

**退出码**：`0` 成功；`1` 业务失败（用户名已存在 / 找不到账号）；`2` 密码不合法或没设环境变量；
`3` 该版本缺少相应能力（脚本会如实拒绝，**不会**绕过应用层直接改 SQL）。

**实测过的 9 个场景**（`create` 空库、建号、重复建号被拒、密码太短被拒、没设环境变量被拒、
改密、改不存在的账号、列账号、库内无明文密码）—— 退出码与预期一致；
建出来的账号在**真实服务**上登录成功（`POST /api/auth/login` → 200，拿到 `mis_session` Cookie，
此后 `/api/datasets` 200）。

> ⚠️ **为什么必须用这个脚本、不能自己 `AuthStore(...)`？**
> 认证栈的 store 是配置层决定的**单例**（`get_auth_store()`）。自己 new 一个会绕过
> `AUTH_DB_PATH` 的解析，跑到默认库上去，于是出现"我明明改了密码，怎么登录还是旧密码"。
> 脚本内部走的就是应用的同一条路径。

**建第二个账号特别注意**：两个账号的数据是**互相看不见**的（越权请求一律 404）。
想验证隔离：

```powershell
& ".\.venv\Scripts\python.exe" .tmp\lead_iso\verify_isolation.py
# 预期： 21/21 PASS
```

### 6.3 忘记密码怎么办

本期**没有**"忘记密码/邮件重置"（界面上也没有这个链接——不摆假入口）。
用 §6.2 的 `passwd` 子命令即可，它会**连旧会话一起作废**：

```powershell
$env:MIS_NEW_PASSWORD = "新的密码"
& ".\.venv\Scripts\python.exe" scripts\manage_account.py passwd --username analyst_li
# 预期： 改密成功：username=analyst_li，已作废该账号的 N 个已登录会话
```

作废之后，该用户**已经登录的浏览器**下次发请求会拿到 401，页面会引导重新登录
（这条行为有自动化测试守着）。

---

## 7. 常见问题（对照表：看到什么现象 → 大概率是什么原因）

| 现象 / 报错原文 | 原因 | 怎么办 |
|---|---|---|
| `ModuleNotFoundError: No module named 'app'` | 你在**别的目录**下跑命令 | 先 `cd <项目根>`；写脚本时若按路径运行，脚本里要 `sys.path.insert(0, str(ROOT))` |
| `ModuleNotFoundError: No module named 'fastapi'` | 用了系统 Python，不是 `.venv` 的 | 用 `& ".\.venv\Scripts\python.exe" -m ...` |
| `ERROR: [Errno 10048] error while attempting to bind on address ... 通常每个套接字地址只允许使用一次` | 端口被占了 | 换端口 `--port 8001`，或查出占用进程：`Get-NetTCPConnection -LocalPort 8000 -State Listen` |
| 打开页面一直转圈，或立刻跳回 `/login` | ① 未登录（正常行为，受保护路由会跳登录页）；② 服务刚重启、会话还在但**数据集没了**（内存态） | 正常登录即可；数据集需重新上传 |
| 分析任务能建，但**没有图表** | `matplotlib` 没装，或 `CHARTS_DIR` 不可写 | 装 `matplotlib`；确认目录存在且可写 |
| 上传大文件报 4xx | 超过 `MAX_UPLOAD_MB` / `MAX_ROWS` | 调大环境变量后**重启**服务 |
| 日志里出现 `归档失败（...），分析结果不受影响` | 归档是**旁路**：写归档出错不会让分析失败，但**历史列表会是空的** | 这是一类真实缺陷（本项目修过一个同类 bug：`owner_user_id` 漏传）。看完整报错，不要只看前半句 |
| 历史列表条数对不上、明明跑过任务却没有记录 | ① 服务重启导致内存态数据集丢失（归档记录仍应在库中）；② 归档真失败（见上一行） | 直接查库：`& ".\.venv\Scripts\python.exe" -c "import sqlite3;print(sqlite3.connect('app.db').execute('select count(*) from history_records').fetchone())"` |
| 中文在 PowerShell 里显示成乱码，或 `Get-Content` 判断"没有这段文字" | **PowerShell 的字符串处理常出假阴性** | 按字节读：`[System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($p))` |
| Playwright 报 `EPERM: operation not permitted, mkdtemp 'C:\Users\...\Temp\playwright-artifacts-...'` | 系统 `TEMP` 目录对当前进程不可写 | 跑之前把临时目录指到项目内：`$env:TEMP="$PWD\.tmp\pwtemp"; $env:TMP=$env:TEMP` |
| matplotlib 报 `mkdir -p failed` 并往当前目录拉屎 | `MPLCONFIGDIR` 不可写 | `$env:MPLCONFIGDIR="$PWD\.tmp\mplcache"` |
| `pytest` 收集到一堆莫名其妙的用例 / `inifile` 指向 `D:\pyproject.toml` | 你**不在项目根**跑 pytest（本机上 `D:\` 根目录恰好有个 `pyproject.toml`） | **必须在 `<项目根>` 下跑** |

---

## 8. 安全提醒（部署前**务必**读）

本项目的认证是**真实的服务端认证**（标准库 PBKDF2-HMAC-SHA256 哈希、HttpOnly Cookie、服务端校验凭据、
失败限流、会话失效），但它**面向的是本地/内网单人使用**这个场景。上公网前至少要处理：

| 项 | 现状 | 上生产要做什么 |
|---|---|---|
| 传输加密 | **HTTP 明文** | 前面挂 HTTPS 反代（Nginx/Caddy），并把 Cookie 设成 `Secure` |
| 会话 Cookie | `HttpOnly` + `SameSite=Lax`；**本地开发模式下不带 `Secure`** | 部署环境开 `Secure` |
| 注册 | **无自助注册**（刻意） | 要放开必须先补邮箱验证、密码重置、滥用防护 |
| 多租户 | 资源**已按用户归属隔离**（越权 404），但**没有工作区/团队概念** | 需要团队协作时再设计工作区模型 |
| 数据集存储 | **进程内存**，重启即丢 | 换持久化存储 |
| 部署形态 | **单进程** | 内存态决定了目前**不能**多 worker 水平扩展 |
| 密钥 | 只从环境变量读，**不写前端、不写死** | 用密钥管理服务，别塞进启动命令历史 |

**绝对不要做的事**：把 `LLM_API_KEY` 或管理员密码写进前端代码、写进 git、贴进聊天记录。

---

## 9. 跑测试

### 9.1 全量（约 2 分 15 秒）

```powershell
cd D:\个人项目01\ai-data-analyst-agent
& ".\.venv\Scripts\python.exe" -m pytest -q -p no:randomly
```

**预期**：`889 passed`（这是本次交付冻结时的数字），0 failed、0 error。

> 为什么要 `-p no:randomly`？装了这个插件时它会打乱用例顺序，报错信息不好对照。
> 不加也能过，只是输出顺序会变。
>
> 为什么必须 `cd` 到项目根？见 §7 最后一行。

### 9.2 只跑某一类（开发时常用，秒级）

```powershell
# 认证相关
& ".\.venv\Scripts\python.exe" -m pytest -q -p no:randomly tests\test_auth_api.py

# 数据集接口
& ".\.venv\Scripts\python.exe" -m pytest -q -p no:randomly tests\test_datasets_api.py

# 前端静态契约（7 个文件、文案、白名单）
& ".\.venv\Scripts\python.exe" -m pytest -q -p no:randomly tests\test_frontend.py
# 预期： 19 passed

# 图表与编排（改动 charts.py / orchestrator.py 后必跑）
& ".\.venv\Scripts\python.exe" -m pytest -q -p no:randomly `
    tests\test_charts.py tests\test_charts_coverage.py tests\test_orchestrator.py `
    tests\test_validation_blindspots.py tests\test_validation_numbers.py
# 预期： 124 passed
```

### 9.3 前端语法检查（不需要构建）

```powershell
node --check app\web\app.js
node --check app\web\login.js
# 两条都无输出、退出码 0 = 语法没问题
```

### 9.4 真实浏览器验收（需要 Playwright 与浏览器内核）

```powershell
$env:TEMP="$PWD\.tmp\pwtemp"; $env:TMP=$env:TEMP     # 必须先设，否则 EPERM
& ".\.venv\Scripts\python.exe" .tmp\acceptance\run_matrix.py --viewports 1440x900,390x844
```

完整六档矩阵与它的前置闸门说明见 `.tmp/acceptance/README.md`；
正式轮跑之前必须让 `check_port_logic.py` 与 `check_selectors.py` 都返回退出码 0。

---

## 10. 界面导览（第一次打开时对照看）

| 路由 | 页面 | 主要用途 |
|---|---|---|
| `/login` | 登录页（**独立页面**，不是 SPA 的一部分） | 登录 / 首次部署建号；未登录访问受保护路由会被 303 跳到这里 |
| `#/workbench` | 工作台 Market Intelligence Overview | 看数据资产/任务/报告的概览，进上传与建任务的入口 |
| `#/dataset` | 数据集 | 上传 CSV/Excel、看文件名类型大小行数列数、字段画像 |
| `#/analysis` | 市场分析工作区 | 选数据集 + 自然语言提问 → 建分析任务 → 看六个阶段的真实状态 |
| `#/report/<id>` | 分析结果与报告 | 核心指标、发现、图表、明细表、数据质量与局限说明 |
| `#/history` | 历史任务 | 归档过的分析记录、状态、结果入口 |
| `#/settings` | 设置 | 当前账号信息、退出登录、系统运行信息 |
| `/docs` | 接口文档（FastAPI 自带） | 想用接口或脚本取数据时看这个 |

**关于"Agent Runs"**：任务书里列的 `Agent Runs`（智能体运行记录）在本项目的落点是
**分析工作区的状态区 + 历史任务的详情**，没有单独的顶级导航项——因为后端目前只暴露
**整条任务的状态与阶段**，没有逐步骤日志。**宁可不开一个内容空白的页面**，这条决策
记在 `docs/final-report.md`。

---

## 11. 一句话给排错的人

遇到怪现象，按这个顺序问四个问题：

1. **服务是什么时候起的？**（旧进程会拿旧代码给出假报错）
2. **我量的是不是同一版代码？**（跑之前先算 7 个前端文件 + 关键后端文件的 sha256）
3. **报错和截图/现场一致吗？**（不一致时先怀疑判据，再怀疑产品）
4. **这个数字能指回它的来源字段/来源帧吗？**（指不回去的数字不要写进结论）

这四条是本次交付中真实踩出来的，细节见 `docs/final-report.md` §1 与 §12.6.5。

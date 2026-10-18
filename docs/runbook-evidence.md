# runbook 命令实测证据

> 这份文件回答一个问题：**`docs/runbook.md` 里让你敲的命令，凭什么说真的跑过？**
>
> 规矩很简单：runbook 里每一条"预期输出"，这里必须能找到一次真实执行的记录。
> 记录里只写**真实发生的事**——包括失败的、跟预期不一样的。修过的地方写明修前是什么样。

## 0. 怎么读这份文件

| 列 | 含义 |
|---|---|
| 命令 | 实际敲下去的那一行（含工作目录） |
| 退出码 | 进程退出码。`0` = 成功 |
| 观察到的输出 | **原文抄录**，不是"应该是这样" |
| 结论 | 与 runbook 的对应关系：✅ 一致 / ⚠️ 有出入（runbook 已改） |

**这些记录不构成"以后也一定成功"的保证。** 它们只证明：在记录的那个时间、
那台机器、那个版本上，命令确实产生了那个结果。换机器、换版本、
换环境变量，都要重跑。

## 1. 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-10-16 |
| 工作目录 | `D:\个人项目01\ai-data-analyst-agent` |
| Python | `.\.venv\Scripts\python.exe`（3.12.8，venv 带 `--system-site-packages`） |
| 操作系统 | Windows |

## 2. `scripts/check_env.py` —— 环境自检

**runbook 位置**：§0 速查表、§4.3
**实测输出文件**：`.tmp/lead_final/check_env_out.txt`

```
命令     : & ".\.venv\Scripts\python.exe" scripts\check_env.py
退出码   : 0
```

观察到的关键行（原文）：

```
[ok] fastapi 0.136.3
[ok] uvicorn 0.48.0
[ok] pydantic 2.13.4
[ok] pandas 3.0.3
[ok] matplotlib 3.11.2
[ok] argon2 25.1.0
[ok] pytest 9.1.1
[ok] httpx 0.28.1
[ok] playwright 1.60.0
[warn] 端口 8000 被占用
```

7 个前端文件按字节数逐个校验：39258 / 59100 / 133331 / 21620 / 20753 / 34002 / 47791
——与冻结集逐位一致。

**结论**：✅ 与 runbook §4.3 描述一致。

> ⚠️ **这一行的 `[ok] argon2` 后来被查出是错的**（见 §6）。它在**这台机器上**能过，
> 是因为 argon2 装在系统级 Python 里（venv 带 `--system-site-packages`）。
> 但本项目认证的真实实现是**标准库 `hashlib.pbkdf2_hmac`**，
> `requirements.txt` 与 `requirements.lock.txt` 里**都没有 argon2**。
> 也就是说：**换一台干净的机器，这一行会从 `[ok]` 变成 `[FAIL]`，
> 并给出"认证不可用"的错误结论。** 已作为缺陷修（见 §6）。

## 3. `scripts/manage_account.py` —— 账号运维

**runbook 位置**：§6.2、§6.3
**隔离数据目录**：`.tmp/lead_final/acct_demo/`（`DB_PATH` / `AUTH_DB_PATH` 都指向它，
**没有碰** `data/db/auth.db`）

### 3.1 九个场景

| # | 命令（`MIS_NEW_PASSWORD` 视命令而定） | 退出码 | 观察到的输出（原文摘录） |
|---|---|---|---|
| 1 | `list`（空库） | **0** | 表头正常，无数据行 |
| 2 | `create --username demo_admin --display-name 演示管理员` | **0** | `建号成功：id=1 username=demo_admin display_name=演示管理员` |
| 3 | `list` | **0** | 表格含 `id` / `username` / `display_name` / `sessions` / `last_login_at` |
| 4 | 重复 `create --username demo_admin` | **1** | `建号失败：用户名已存在: demo_admin` |
| 5 | `create`，密码 3 位 | **2** | 密码长度不合法 |
| 6 | `create`，未设 `MIS_NEW_PASSWORD` | **2** | 未设置环境变量 |
| 7 | `passwd --username demo_admin` | **0** | `已作废该账号的 0 个已登录会话` |
| 8 | `passwd --username` 不存在的账号 | **1** | 找不到账号 |
| 9 | `--help` 与 4 个子命令 `--help` | **0** | `usage: manage_account.py [-h] {create,passwd,list,disable} ...` |

### 3.2 库内实况（用**原始 SQL 直读**，不用被测对象自证）

```sql
-- sqlite3 直连 .tmp/lead_final/acct_demo/auth.db
.tables  -> ['auth_users', 'sqlite_sequence', 'auth_sessions']
SELECT * FROM auth_users;
-> [(1, 'demo_admin', '演示管理员', 240000, 64, 32)]
```

即：**迭代次数 240000**（PBKDF2-HMAC-SHA256）、**哈希长 64 字节**、**盐长 32 字节**，
**库内没有明文密码列**。

> 这条为什么重要：如果只看"建号成功"，一个把明文密码写进库的实现也会通过。
> 必须**绕过应用层直读库**才能证明落库的是哈希。

### 3.3 建出来的账号在真实服务上登录

```
服务     : uvicorn app.main:app，端口 8310（隔离实例，pid 22700）
GET  /api/health         -> 200 {"ok":true,"data":{"status":"healthy",...}}
POST /api/auth/login     -> 200   display_name = "演示管理员"
                          Set-Cookie: mis_session=...
GET  /api/datasets       -> 200
```

验证完已 `Stop-Process` 关闭该实例。

**结论**：✅ 与 runbook §6.2 一致。**子命令名与选项名逐字对得上**（`create` / `passwd` /
`list` / `disable`，以及 `--username` / `--display-name` / `--enable`），
环境变量名 `MIS_NEW_PASSWORD` 也对得上，退出码四个（0/1/2/3）对得上。

> ⚠️ **退出码 `2` 的含义有出入**（task-27 的 A-7 发现）：runbook §6.2 原来写
> 「`2` 密码不合法**或**没设环境变量」，读起来像"跟随应用层配置"。
> 实际脚本里**自己写死了一个最小长度**，与 `app/core/auth.py` 的最小长度**恰好都是 8**。
> 两个 8 今天是巧合；后端改成 10 的那天，脚本会放着 8 位密码过去。已作为缺陷修（见 §6）。

## 4. `scripts/check_docs.py` —— 文档锚点自检

```
命令     : & ".\.venv\Scripts\python.exe" scripts\check_docs.py
退出码   : 0
耗时     : 2.2s
stdout   : 10171 字符
末行     : 行号锚点 7 条 / 用例 24 个全部存在 / 断言方向 12 个参数化实例
```

**结论**：✅ 未放宽任何判据。

## 5. pytest —— 全量回归

```
命令     : & ".\.venv\Scripts\python.exe" -m pytest -q -p no:randomly
工作目录 : D:\个人项目01\ai-data-analyst-agent     ← 必须是这里
前置     : $env:TEMP / $env:TMP 指向项目内 .tmp\pwtemp
           $env:MPLCONFIGDIR 指向 .tmp\mplcache
```

```
======================= 889 passed in 104.13s (0:01:44) =======================
退出码   : 0
```

> **两个必须记住的坑**（本机实测）：
> 1. **不能在项目根之外跑**。`D:\pyproject.toml` 存在，从别处跑会把 rootdir 解析到 `D:\`，
>    收集结果完全错乱。
> 2. **`--timeout` 用不了**（没装 `pytest-timeout`）。

## 6. 修过的地方（写清楚"改前是什么样"）

| # | 位置 | 改前 | 改后 | 怎么发现的 |
|---|---|---|---|---|
| 1 | `scripts/check_env.py` 依赖表 | 把 `argon2` 当**必需**依赖，打 `[ok] —— 密码哈希（认证必需）` | **整条删掉**（不是降级）——本项目根本不依赖它 | task-27 逐条核 `requirements.txt`，发现本机是靠系统级 Python 蒙混过关 |
| 2 | `docs/runbook.md:91/:97/:115/:349` | 把 argon2 写成了**要装的依赖**：检查命令里含它、预期输出第一行列它、依赖表写「不装认证不可用」、安全章写「argon2 哈希」 | 四处全部去掉 / 改为**标准库 `hashlib.pbkdf2_hmac`** | task-28 收尾时 Lead 顺着第 1 条 grep 全仓，发现**文档比脚本更严重**——见下面 §6.1 |
| 3 | `scripts/check_env.py:43` | `print()` 遇到 GBK 编码不了的字符会抛 `UnicodeEncodeError` 崩掉 | 输出流改为容错编码 | task-27 做对照实验，证明"TEMP 不可写 + GBK 控制台"**单独不能**复现，真凶是那行 `print` |
| 4 | `scripts/manage_account.py` 最小密码长度 | 自己写死一个长度字面量，与后端权威值恰好相同 | 改为从后端权威来源取 | task-27 的 A-7 |

> 这四条都记在这里，是因为**runbook 就是照着脚本写的**：
> 脚本说错了，runbook 就跟着错。反过来，runbook 里任何一句"预期输出"变了，
> 也该回到这里加一行。

### 6.1 为什么第 2 条比第 1 条严重（值得单独记）

**两者是同一个假事实，但载体不同，后果完全不同。**

- 写在 `check_env.py` 里：它只是**自己报错**（打一条 `[FAIL]`），影响面 = 跑自检的那一次。
- 写进 `docs/runbook.md`：它变成了**用户会照着做的指令**。三处叠加的具体后果是——
  用户在干净机器上跑 §3 的 `pip list` 检查，**预期输出列表里的第一行
  `argon2-cffi==25.x` 不会出现**；再对照依赖表看到「`argon2-cffi` … 不装会怎样：
  **认证不可用**」→ 得出**「我的认证装不上」**这个错误结论 →
  **去装一个本项目根本不需要的包**。

同一句假事实，写在代码注释里是噪音，写进 runbook 就变成会误导操作的指令。

**发现路径（可复用的动作）**：改了某个事实之后，**全仓 grep 这个事实的逐字形态，
不限定路径**。本轮就是这么找到的：修完 `check_env.py` 后 grep `argon`，
一条命令、一秒，命中 `docs/runbook.md` 4 处。
教训是 **范围要按「什么事实被改了」划，不能按「我改了什么文件」划** ——
后者会系统性地漏掉"引用这个事实的文档"，而那恰恰是最该扫的地方。

**同一类的第二次（本轮还发生过一次）**：`app/agents/validation.py` 的报告首段文案
`本次未使用大模型（llm_used=false）…` 被改掉后，`docs/DECISIONS.md:976`、
`docs/DEMO.md:475`、`docs/WORKFLOW.md:367` 三处**逐字引用**它的文档同时过期。
三处的性质与处置见 `docs/final-report.md` §12.7.6。

### 6.2 文档主张可以机械核对（下一步该做的）

`docs/runbook.md` 里每一条「命令 + **预期输出**」都是一个**可执行主张**：
跑一遍、和文档逐行 diff 就能判定真伪。本轮 4 处里有两处正是这种形态
（`:91` 的 `pip list` 命令、`:97` 的预期输出块），而且**读起来完全正常** ——
靠人读审是发现不了的，只能靠机械核对。

这类检查应当和 `scripts/check_docs.py` 同级、挂在冻结前跑，而不是靠"某人记得看一眼"。

## 7. 这份文件自己没有覆盖的

- **换机器**：全部记录都来自同一台 Windows 机器。
- **真机浏览器**：§3.3 的登录是用 HTTP 客户端做的，不是浏览器。
- **非 ASCII 控制台**：§6 第 3 条的修复只验证了"构造一个含不可编码字符的消息"这一种情形。
- **`scripts/manage_account.py` 的 `disable` / `--enable` 路径**：只有 `--help` 被跑到，
  真正停用/启用一个账号的行为**没有**在本文档中记录（runbook 里那条命令因此属于
  "命令存在、但本文件没有实测记录"）。
- **§6 第 2 条（argon2 文档修正）没有留在"改后"的实测输出**：改完后 `check_env.py`
  跑出 exit 0，但输出里**本来就不会有** argon2 这一行 —— 所以"它没出现"是
  **缺席证据**，不是正面证据。正面证据是任务书式的核对：`argon` 在
  `requirements.txt` 与 `requirements.lock.txt` 里各 0 命中，而
  `app/core/auth.py:113` 用的是标准库 `hashlib.pbkdf2_hmac`。
- **本文档自身也可能有同类错误**：它记录的是"某条命令当时跑出了什么"，
  但**没有**对 runbook 全篇做逐条机械核对（§6.2 提的那件事还没做）。

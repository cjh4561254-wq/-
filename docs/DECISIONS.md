# 决策记录（Decision Log）

这份文件只记录**已经拍板、并且会影响后续代码怎么写**的决定。

为什么不把决定只写在聊天里：决定一旦只存在于对话中，过两天就没有人知道
"当时为什么这么定"，代码里就会出现两种互相矛盾的写法。判断一条决定该不该
记在这里的标准是——**如果它被违反，会不会产生一个看起来能跑、但语义错了的系统。**

每条包含四部分：决定 / 理由 / 影响到的文件 / 当前状态。

---

## D-01 错误响应：HTTP 状态码真实 + error.code 表达业务语义

**决定**
- HTTP 状态码必须真实反映 HTTP 语义（404 就是 404，413 就是 413）。
- 业务语义放在响应体的 `error.code` 里。
- 两者同时给，响应体外壳恒定：`{"ok": false, "data": null, "error": {...}}`。

映射表（唯一事实来源是 `app/errors.py` 的 `ERROR_STATUS`）：

| error.code | HTTP | 什么时候用 |
| --- | --- | --- |
| BAD_REQUEST | 400 | 参数不合法、请求体有问题 |
| NOT_FOUND | 404 | 数据集／工作流不存在 |
| METHOD_NOT_ALLOWED | 405 | 方法不对 |
| FILE_TOO_LARGE | 413 | 上传超过 `MAX_UPLOAD_MB` |
| UNSUPPORTED_FILE | 415 | 扩展名不在白名单 |
| SERVICE_UNAVAILABLE | 503 | 依赖不可用 |
| WORKFLOW_TIMEOUT | 200 | 工作流后台执行超时 |
| INTERNAL_ERROR | 500 | 未预期异常 |

**理由**
1. 架构设计稿原本写的是"业务错误一律 HTTP 200 + `ok:false`"。**这条被推翻了。**
2. 推翻的第一个理由：原方案的前提不成立。前端用的是原生 `fetch`，不是 axios ——
   `fetch` 在 4xx/5xx 时**不会抛异常**，照样能 `.json()` 解析出 `{"ok":false,...}`。
   所以"统一成 200 才能让前端只写一套解析逻辑"这个论据是错的：真实状态码下
   前端同样只需要一套逻辑（先解析 JSON，再看 `ok`）。
3. 第二个理由：用 `200 OK` 去表示"你那个 70MB 的文件被拒绝了"，是在 HTTP 这一层
   说假话。以后接监控、日志、网关、CDN 时，200 会被统计成"成功请求"。
4. 第三个理由："一律 200"本来就做不到——422（请求体校验失败）和路径级 404
   是框架在进到业务代码之前就返回的，除非把框架的默认行为也整个改写。
   一个"大部分时候成立、偶尔不成立"的约定比没有约定更危险。
5. `WORKFLOW_TIMEOUT` 走 200 是刻意的：工作流在后台异步执行，轮询请求**本身**
   是成功的，它成功地带回了"这个工作流已经超时失败"这个信息。状态码描述的是
   "这次 HTTP 请求怎么样"，不是"那个后台任务怎么样"。

**影响到**：`app/errors.py`（`ERROR_STATUS` / `DEFAULT_MESSAGE` / `AppError`）、
`app/main.py`（四个异常处理器）、`app/schemas.py`（`ErrorCode`）、
所有 API 路由、前端 `app/web/app.js` 的错误分支。

**状态**：已落地。`tests/test_errors.py` 与 `tests/test_error_format.py` 守护。
代码注释里写明了"注册 `fastapi.HTTPException` 会漏掉 404"这个踩过的坑。

---

## D-02 数字只能由白名单工具里的 pandas 算出来

**决定**
- `app/tools/` 里的函数是唯一允许做统计计算的地方，每个函数只依赖 pandas/numpy。
- 大模型只能**选择**工具、填参数、解释结果；不能生成代码，也不能自己产出数字。
- 禁止 `eval` / `exec` / `compile` / `__import__`，禁止把用户或模型的字符串当代码跑。
- 报告表格里的数字直接从 `ToolResult` 复制，不给模型改写的机会。

**理由**：这是整个项目可信度的地基。一旦允许模型"顺手算一下"，报告里的数字
就无法溯源，校验智能体也就失去了比对基准。这条是"不可砍项"。

**影响到**：`app/tools/*`、`app/agents/analysis.py`、`app/agents/validation.py`、
`app/agents/charts.py`。`app/data/profiler.py` 同样零 LLM 参与（D-03）。

**状态**：已定，D3/D4 落地。

---

## D-03 `app/` 目录树里的标记是"这一列=AI 参与度"，不是负责方

**决定**
- `app/` 下：`[AI]` 表示该文件会调用大模型，`[BE]` 表示纯 Python 逻辑。
- 其余目录（`tests/`、`web/`、`data/`、`docs/`）的标记仍是**负责方**（`[QA]`/`[FE]`/`[LEAD]`/`[ARCH]`）。
- 两种含义不要混着引用；"某个 `app/` 文件归谁写"看架构文档第 12 节排期表。
- 用法：**数一下 `app/` 下 `[AI]` 有几行，就是模型参与了几步。**

**理由**：一开始这一列是负责方，但 `app/` 下的文件经常"谁写的"和"跑不跑模型"
是两件事。实测重标后 `app/` 下只剩 5 行 `[AI]`：`llm/__init__.py`、`llm/client.py`、
`llm/prompts.py`、`agents/planner.py`、`agents/validation.py` —— 恰好对应
"模型请求层 / 规划 / 报告撰写"三个功能区，和 D-02、D-04 完全吻合。
保留非 `app/` 行的负责方含义，是因为"谁写测试"这个信息不该丢。

**影响到**：`docs/ARCHITECTURE.md` §2 目录树、§12 排期表。

**状态**：已落地。全树 `app/` 下 `[AI]` = 5 行。

---

## D-04 `app/data/profiler.py` 零 LLM 参与，属于 D2 而不是 D4

**决定**：数据理解智能体的**统计部分**（缺失值、重复行、类型识别、语义类型推断）
全部是 pandas 计算，真实在第二天做完；模型只在需要解释数据质量时参与，那部分
不在 `profiler.py` 里。

**理由**：排期表里原本把它标 `[D4][AI]`，暗示"要等接上大模型才能做"。
实际上它一行模型调用都不需要。排期表不是承诺，**实际做完的那天才是记录** ——
把能做的工作拖到"等 API 就绪"之后，是纯粹的时间浪费。

**影响到**：`docs/ARCHITECTURE.md` §2 目录树、§12 排期表、§14.3。

**状态**：已落地。`app/data/profiler.py` 在 commit `ff5b5cd` 中已存在。

---

## D-05 并发写作者在场时，禁止 `git add -A` / `git add <目录>`

**决定**：多人（含 AI 队友）同时改同一个仓库时，暂存必须**逐个文件点名**，
例如 `git add app/errors.py tests/test_errors.py`。

**理由**：这是一次真实事故。Lead 执行 `git add -A app tests docs` 时，
把 backend-engineer 当时还是 untracked 状态的 `app/data/{__init__,loader,store,profiler}.py`
（944 行，他正在写）一并扫进了自己的提交。后果：
- 他的工作以一种他没同意的方式被记录；
- 提交的语义变成"一个错误契约修复 + 半个数据层"，以后 `git bisect` 和 code review 都受影响；
- 他之后继续修改这些文件时，diff 的基线变得难以解释。

**规避**：提交前先 `git status --short` 逐行确认；看到 `??` 的目录先问归属。

**影响到**：所有协作者的提交习惯。

**状态**：已作为教训记录。`ff5b5cd` 的提交信息中写明了这次失误与保留原因。

---

## D-06 验收基线永远是"已提交且全绿的那个 commit"

**决定**
- 任何"通过了"的结论，必须能指向一个 commit hash。
- 工作区有未提交改动时，测试结果不能作为验收依据（因为无法复现）。
- 交付验收前必须 `git status` 干净。

**理由**：第一阶段踩过一次：Lead 描述"已修复 3 个阻塞项"，但提交因为
PowerShell 解析器报错（提交信息里的引号触发了 `RedirectionNotSupported`）
**根本没执行**，工作区却是已修复状态。QA 复验时看到的是"工作区与 HEAD 不一致、
pytest 红"，无法判断到底修没修。**修复是真的，证据是假的** —— 这比不修复更危险，
因为它会让人先相信、后在别处踩坑。

**影响到**：所有人的验收流程。提交信息里含中文或引号时，用 here-string
写入临时文件再 `git commit -F`，不要写成内联字符串。

**状态**：已落地。基线演进：`9d37bcb` → `460cf8c` → `fe155af` → `44566c6` → `ff5b5cd` → `87c3019` → `dde1115` → `9cf7f4c`。

---

## D-07 测试临时产物必须落在项目内，且"只覆盖 pytest"是已知边界

**背景（实测出来的环境事实，不是预防性措施）**：本项目所在机器上，**Python 子进程无法写入系统临时目录**。实测：

```
os.environ["TEMP"]                    -> 'C:\Users\jun\AppData\Local\Temp'
open(%TEMP%\probe, "w")               -> PermissionError [Errno 13] Permission denied
tempfile.gettempdir()   (cwd=项目根)   -> 'D:\个人项目01\ai-data-analyst-agent'
tempfile.mkdtemp()                    -> 'D:\个人项目01\ai-data-analyst-agent\tmpwd4n6n_2'
```

注意有个会骗人的不对称：**PowerShell 的 `Set-Content` 写得进 `%TEMP%`，Python 写不进**。所以"我在 shell 里验证过临时目录可用"是假绿，必须用 Python 子进程验证。

`tempfile` 在所有候选目录都失败后会退回 `cwd`，于是跑一次测试就在**仓库根**留下 `pytest-of-jun/` 和一串 `tmpXXXXXXXX/`；只要有人手滑 `git add -A`（正是 D-05 那次事故的入口），垃圾就进仓库了。

**决定**
1. `conftest.py` 把 `TMPDIR` / `TEMP` / `TMP` 指向项目内 `.tmp/`，并清 `tempfile.tempdir` 缓存（不清不生效）。
2. **第 2 道独立保险**：`pytest_configure` 里显式钉住 `basetemp = .tmp/pytest`。只靠第 1 条不够——那条链路上任何一环变了（有人命令行传 `--basetemp`、别的插件动过缓存、下面第 3 条降级了）产物就回项目根。
3. `.tmp/` 建不出来时**降级为 warning + 继续跑**，不抛异常。曾经的后果：有人把 `.tmp` 建成同名文件 → `FileExistsError` 停在 conftest → `ImportError while loading conftest`，**整个会话起不来**，看起来像"测试全坏了"。取舍很明确：**脏目录可接受，起不来测试不可接受**。
4. 测试里**只用 pytest 的 `tmp_path` / `tmp_path_factory`，不许用 `tempfile.mkdtemp()`**。

**已知边界（选择记录而不是修）**：以上只覆盖 pytest。`python -m uvicorn app.main:app` 或手工脚本在项目根启动时，`gettempdir()` 仍是项目根——因为改环境变量的是 conftest，它们不加载它。

不修的理由：全仓库只有 `conftest.py` 直接调用 `tempfile`（`app/`、`tests/` 都没有），matplotlib 的缓存由 `app/config.py` 的 `MPLCONFIGDIR` 单独兜住，所以**当前不产生任何实际产物**。真要修，得把"import 就改全局环境变量"挪进 `app/__init__.py`——那比问题本身更脏（任何 `import app` 的进程环境都被改）。所以选择**留下痕迹**：下一个接手的人应当知道这个边界是知道的，而不是漏的。

**还没被覆盖的另一半**：把仓库解压到 `%TEMP%` 下时**连 pytest 都起不来**（`FileNotFoundError: No usable temporary directory found in [...]`，四个历史提交都如此）。即本方案的前提是"项目根可写"。项目本身若落在 Python 写不了的位置，仍然救不了。

**影响到**：`conftest.py`、`.gitignore`（`/tmp????????/` 与 `/pytest-of-*/` 兜底，**开头的斜杠不能省**——不带 `/` 的 pattern 在任意层级生效，实测会误伤 `app/tmp_uploads/`、`data/tmp_uploads/`）、所有人的测试写法、README 的测试章节。

**状态**：已落地，commit `dde1115`（第 1 条）与 `9cf7f4c`（第 2、3 条 + `.gitignore` 锚定）。两条独立保险都已实测，反证见这两个提交的提交信息。

**待补（还没做，别当成已完成）**
- 本文件尚未记录 **D2 上传"分块读进内存 + 累计大小、超限立刻 raise"** 的决策（选了它而不是"边写边算 + 失败删文件"：换来"磁盘上不留半个文件"，代价是峰值内存 ≈ 上限 + 一次 chunk，`MAX_UPLOAD_MB` 放到几百 MB 时要改回边写边算）。
- 本文件尚未记录 **`.xls` 明确不支持、只收 `.csv` / `.xlsx`** 的决策与"请另存为 .xlsx"的提示口径。

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

**状态**：已落地。基线演进：`9d37bcb` → `460cf8c` → `fe155af` → `44566c6` → `ff5b5cd` → `87c3019` → `dde1115` → `9cf7f4c` → `0fb4a8b` → `8c9b02c` → `62abfa2`。

**注意**：这个列表会随着每次提交变长。它的用途是"出事时知道该回到哪一个点"，
而不是"证明进度"。真要看当前基线直接 `git log --oneline -1`。

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
2. **第 2 道独立保险**：`pytest_configure` 里显式钉住 `basetemp`。只靠第 1 条不够——那条链路上任何一环变了（有人命令行传 `--basetemp`、别的插件动过缓存、下面第 3 条降级了）产物就回项目根。
   **但钉的时候有两个前提，踩过坑所以写死在这里**（见 `conftest.py` 的 `pytest_configure` docstring）：
   - **必须绝对路径**。pytest 把相对的 `basetemp` 按**调用目录**解析（不是 rootdir），所以
     `.tmp/pytest` 在 `cd tests` 之后变成 `<root>/tests/.tmp/pytest`；
   - **父目录必须已存在**。pytest 用 `basetemp.mkdir(mode=0o700)` 创建它，没有 `parents=True`、
     也没有 `exist_ok=True`（`_pytest/tmpdir.py` 的 `TempPathFactory.getbasetemp`），父目录不存在
     就抛 `FileNotFoundError: [WinError 3]`，`.tmp` 是文件就抛 `FileExistsError`。
   这两条合起来造成过一次真实回归（QA 在 `9cf7f4c` 冻结副本上抓到的）：
   **这个"保险"把"从子目录跑能过"变成了"从子目录跑必挂"**，而且唯一用 `tmp_path` 的那个测试
   在 setup 阶段就 ERROR，看起来与被测代码毫无关系。所以现在用 `TEMP_DIR`
   （由 `Path(__file__).resolve().parent` 推出，与 CWD 无关）**且只在 `.tmp/` 确认可用时才钉**：
   钉一个建不出来的路径，等于亲手把会话搞挂。修复在 `8c9b02c`。

   顺带一条同源发现（也在 `8c9b02c`）：`cd tests; pytest` 时 `app/` **整个不收集**，
   根因是 `_pytest/config/__init__.py:1415` 的 `_decide_args()` 只在
   `invocation_dir == rootpath` 时才用 ini 的 `testpaths`，否则退回"从当前目录递归"。
   后果是 `app/errors.py` 那条 doctest **静默消失**（根目录 88 passed / 子目录 87 passed），
   而静默少一条回归测试正是开 `--doctest-modules` 要防的"注释谎报"。
   现在 `conftest.py` 在"调用方没给显式目标且调用目录不是 rootdir"时把 `app/` 补进 `config.args`；
   显式点名文件时不补（否则同一个 `pytest test_health.py` 在两个入口会跑出不同的条数）。
3. `.tmp/` 建不出来时**降级为 warning + 继续跑**，不抛异常。曾经的后果：有人把 `.tmp` 建成同名文件 → `FileExistsError` 停在 conftest → `ImportError while loading conftest`，**整个会话起不来**，看起来像"测试全坏了"。取舍很明确：**脏目录可接受，起不来测试不可接受**。
4. 测试里**只用 pytest 的 `tmp_path` / `tmp_path_factory`，不许用 `tempfile.mkdtemp()`**。

**已知边界（选择记录而不是修）**：以上只覆盖 pytest。`python -m uvicorn app.main:app` 或手工脚本在项目根启动时，`gettempdir()` 仍是项目根——因为改环境变量的是 conftest，它们不加载它。

不修的理由：全仓库只有 `conftest.py` 直接调用 `tempfile`（`app/`、`tests/` 都没有），matplotlib 的缓存由 `app/config.py` 的 `MPLCONFIGDIR` 单独兜住，所以**当前不产生任何实际产物**。真要修，得把"import 就改全局环境变量"挪进 `app/__init__.py`——那比问题本身更脏（任何 `import app` 的进程环境都被改）。所以选择**留下痕迹**：下一个接手的人应当知道这个边界是知道的，而不是漏的。

**还没被覆盖的另一半**：把仓库解压到 `%TEMP%` 下时**连 pytest 都起不来**（`FileNotFoundError: No usable temporary directory found in [...]`，四个历史提交都如此）。即本方案的前提是"项目根可写"。项目本身若落在 Python 写不了的位置，仍然救不了。

**影响到**：`conftest.py`、`.gitignore`（`/tmp????????/` 与 `/pytest-of-*/` 兜底，**开头的斜杠不能省**——不带 `/` 的 pattern 在任意层级生效，实测会误伤 `app/tmp_uploads/`、`data/tmp_uploads/`）、所有人的测试写法、README 的测试章节。

**状态**：已落地，commit `dde1115`（第 1 条）、`9cf7f4c`（第 2、3 条 + `.gitignore` 锚定）、`8c9b02c`（第 2 条的两个前提 + `app/` 收集）。全部实测过，反证见这三个提交的提交信息。

**本轮已补齐**：下面两条原来挂在这里当"待补"，现在各自成节，不再重复：
- 上传字节处理顺序 → **D-09**
- `.xls` 明确不支持 → **D-08**

---

## D-08 只收 `.csv` / `.xlsx`，明确不支持 `.xls`

**决定**
- **入口白名单只有 `csv` 和 `xlsx`，不接收 `xls`。** 唯一事实来源是
  `app/schemas_dataset.py` 的 `ALLOWED_EXTENSIONS`（从 `FileExt.__args__` 派生）。
- 用户上传 `.xls` 时返回 `UNSUPPORTED_FILE`（HTTP 415），并且 detail 必须给出
  **下一步能照着做**的话："暂不支持旧版 .xls 格式，请在 Excel 里另存为 .xlsx 后再上传"。

**理由**
1. `.xls` 是二进制专有格式，pandas 读它需要 `xlrd`（已停止维护，且新版 pandas 不再支持）。
2. 在**入口**按扩展名拒掉，比"读了一半才报错"好：用户立刻知道该做什么，
   而不是等上传完、解析到某个奇怪的地方才失败。
3. 只说"不支持 xls"不够 —— 用户不知道该干什么。用户最容易踩的就是这个坑
   （自己电脑上的老表格默认存成 `.xls`），所以报错要带动作。
4. 这个清单必须**只有一份**：之前它散落在路由层、而 `loader.py` 里还有一份独立的
   `if ext == "csv" / elif "xlsx"`。两份清单迟早漂移——给 `FileExt` 加了扩展名却没实现
   loader，用户就会遇到"上传通过了，读的时候才说不支持"。
   `tests/test_loader.py::test_every_allowed_extension_is_actually_loadable` 盯着这件事。

**影响到**：`app/schemas_dataset.py`（`FileExt` / `ALLOWED_EXTENSIONS`）、
`app/data/loader.py`（解析分支与报错文案）、`app/data/store.py`
（`build_stored_filename` 用它做第二道校验）、`app/api/routes_datasets.py`（入口白名单）、
`.env.example` 无关、前端 `accept` 属性。

**状态**：已落地。`tests/test_loader.py::test_legacy_xls_gets_a_human_hint` 与
`tests/test_store.py::test_build_stored_filename_rejects_a_bad_extension` 守护。

---

## D-09 上传：分块读进内存 + 累计大小，超限立刻 raise（不落盘、不留半个文件）

**决定**：`POST /api/datasets/upload` 的字节处理顺序固定为 ——
**先校验扩展名 → 生成 `dataset_id` → 分块读 `UploadFile` 并在累计超过
`MAX_UPLOAD_MB` 时立刻 `raise AppError(FILE_TOO_LARGE)` → 对读到的字节算 sha256
→ 一次性 `write_bytes()` 落盘**。

**具体规则**
1. 累计的字节**全部留在内存里**，落盘是"通过了全部检查之后的一次性动作"。
2. 超限时**磁盘上什么都不留** —— 不需要"失败删文件"这种补偿动作。
3. 分块读（不是 `await file.read()` 一把梭）的意义是**能在超限时提前中止**，
   而不是"省内存"：本项目上限 10 MB，一把读也不会有事；但把上限调到几百 MB 时，
   第 1 条的峰值内存会变成问题，那时要改回"边写边算 + 失败删文件"。

**为什么选它，而不是"边写边算 + 失败删文件"**
- 换来的是"磁盘上不留半个文件"这条**不需要靠补偿动作维持**的性质。补偿动作会失败
  （进程被杀、磁盘满、异常路径漏了一个），而"没写"不会失败。
- 代价写在明处：峰值内存 ≈ 上限 + 一次 chunk 的大小。这是一个**有意识**的取舍，
  不是一个没想到的后果。
- 另一条理由：`Dataset.sha256` 的语义被定为"用户**原始上传字节**的 sha256"。
  先读进内存再对同一串字节既算哈希又写盘，磁盘内容和 `sha256` / `size_bytes`
  严格一致；"边写边算"则要么把哈希和写入耦合在一起，要么读完文件再回读一遍。

**边界（明确接受）**：`MAX_UPLOAD_MB` 若调到几百 MB 以上，这条决策需要重新评估。

**影响到**：`app/api/routes_datasets.py`、`app/data/store.py`
（`register()` 只登记不落盘，落盘在路由层）、`app/config.py`（`max_upload_bytes`）。

**状态**：决策已定，实现在 task-8 之后的路由层（`app/api/routes_datasets.py` 尚未存在）。

---

## D-10 Profiler（描述性事实）与 tools（分析结果）的分界

**决定**：`app/data/profiler.py` 允许用 pandas 计算**描述数据集本身的事实**：
dtype、语义类型、缺失值数量与比例、唯一值数量、五数概括与均值/标准差、
重复行数、内存占用。这些是"这份数据长什么样"的元信息，不是对用户问题的回答。

**仍然不可动摇的（D-02 原文一个字不改）**：模型不允许产出数字；
**出现在分析报告里的结论性数字，只能来自 `app/tools/` 里被白名单调用的函数。**

**理由**：D-02 原来的措辞是"`app/tools/` 里的函数是唯一允许做统计计算的地方"。
这句话字面上把 `profiler.py` 也框进去了，而 profiler 确实在做 `null_count`、
`std_value`（ddof=1）、五数概括这类计算 —— QA 复核时指出这是一处**真实的自相矛盾**。
两种改法里选了"补一条分界"而不是"让 profiler 调 tools"：
- 让 profiler 调 tools 会把"数据理解"变成"数据分析"的服务调用，profiler 还要依赖
  `app/tools/`，而架构 §2.1 规定 `data/` 只依赖 pandas/openpyxl，不许反向依赖；
- 更关键的是，profiler 的数**不是给用户看的结论**，它只是 planner 排计划时的输入
  （"这列有 12% 缺失，别默认拿它做求和"）。把两者硬塞进同一层，反而模糊了
  "哪些数字是分析结果"这个最重要的问题。

**判定标准（面试可以直接答这句）**：这个数字会不会出现在给用户的**分析报告**里？
会 → 必须来自 `app/tools/` 的白名单函数；不会、只是描述数据本身 → profiler 可以算。
两类都必须由 pandas 真正算出来，模型一律不许产出数字。

**影响到**：`app/data/profiler.py`、`app/tools/*`、`app/agents/planner.py`、
`app/agents/validation.py`、`docs/ARCHITECTURE.md` §2.1 依赖方向。

**状态**：已定。`app/data/profiler.py` 在 commit `ff5b5cd` 中已存在并带 14 条测试。

---

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

**补充（2026-10-13，backend-engineer 实测发现）：「全绿」必须在提交态上测，工作区绿 ≠ 提交态绿。**

具体案例：`62abfa2` 单独 checkout 出来跑是**红的** —— 它里面
`tests/test_loader.py` 已经有一条硬断言 `assert "另存为 .xlsx" in excinfo.value.detail`，
但同一个 commit 的 `app/data/loader.py` 里连「另存为」三个字都没有
（那句提示当时还在 backend-engineer 的工作区、没入库）。而 Lead 报的 105 passed
是**工作区口径**。Lead 复核为真：`git show 62abfa2:app/data/loader.py`
匹配「另存为」= 0 处，`git show 62abfa2:tests/test_loader.py` = 1 处。

成因是并发写作的典型交错：**测试先入库、被测代码还在别人手上**。
不是谁的疏忽，但结论一样 —— 两个人各跑一次工作区都是绿的，仓库里那个点却是红的。
所以验收动作固定为：`git stash -u`（或 `git archive` 到仓库外的副本）后跑全量，
跑完再 `git stash pop`。

**影响到**：任务描述里凡写「全量 pytest 绿」，都指提交态。

**状态**：已落地。基线演进：`9d37bcb` → `460cf8c` → `fe155af` → `44566c6` →
`ff5b5cd` → `87c3019` → `dde1115` → `9cf7f4c` → `0fb4a8b` → `8c9b02c` → `2024148`。
（`62abfa2` 提交态是红的，见上面的补充，**不要拿它当基线**；`34dd3a9` 才是数据层的
第一个提交态全绿点。）

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

**已知边界（2026-10-13 已修，机制见下）**：以上只覆盖 pytest。`python -m uvicorn app.main:app`
或手工脚本在项目根启动时，`gettempdir()` 曾是项目根 —— 因为改环境变量的是 conftest，它们不加载它。

**修订（2026-10-13）：生产入口也覆盖了，但换成了"启动时显式设置"**

早期修法是"在 `app/config.py` 顶部 `os.environ.setdefault`"（因为它是任何入口最早
import 的模块）。**这个做法已被推翻**，原因两条，第二条是关键：

1. import 一个包就改进程全局环境，副作用太脏，而且"谁先 import 了谁"决定行为；
2. 它**猜错过**。当时写的规则是"`TEMP` 已设就尊重调用方"，而这台机器的
   `TEMP=C:\Users\jun\AppData\Local\Temp` **存在但 Python 写不进去** ——
   尊重它的结果是临时文件照旧退回仓库根，全程没有任何报错。
   实测：只设环境变量、不清 `tempfile.tempdir` 缓存时，`gettempdir()` 依然返回仓库根。

现在的机制：

- `app/runtime.py` 的 `runtime_environment(settings)`：建目录 → 真写一个文件确认可写
  （写不进去就让启动失败，保留原始异常）→ 设 `TEMP`/`TMP`/`TMPDIR` → 清
  `tempfile.tempdir` 缓存 → yield → 退出时恢复现场（环境变量与缓存都还原）。
- `app/main.py` 的 lifespan 用它包住 `yield`，所以 `uvicorn app.main:app` 起来的
  服务在处理任何请求之前就已经设好。
- 目录来自 `settings.temp_path`（`TEMP_DIR`，默认项目内 `.tmp/`）——**应用明确指定，
  不继承、不猜测系统 `TEMP`**。换台机器行为一致。
- `app/config.py` 只保留 `MPLCONFIGDIR`：matplotlib 在 import 时就决定配置目录，
  来不及等到 lifespan。两处机制不同是**约束不同**，不是疏忽。

**真实证据（不是推理）**：起真实 uvicorn，慢速上传 3 MB 文件（触发 Starlette 1 MB
的 spool 阈值），请求处理**期间**每 0.15 秒采样两个目录：

```
采样 41 次：仓库根出现 tmp* = 0 次  ← 必须为 0
           .tmp/  出现 tmp* = 24 次
上传后：仓库根 tmp* = []，.tmp/ tmp* = []
```

只数"事后 cwd 里有没有垃圾"是不够的：spool 文件会被 `close()` 清掉，事后看永远
干净，两种实现都能通过。所以判据必须是"处理**期间**"。


**还没被覆盖的另一半**：把仓库解压到 `%TEMP%` 下时**连 pytest 都起不来**（`FileNotFoundError: No usable temporary directory found in [...]`，四个历史提交都如此）。即本方案的前提是"项目根可写"。项目本身若落在 Python 写不了的位置，仍然救不了。

**影响到**：`conftest.py`、`.gitignore`（`/pytest-of-*/`、`/tmp????????`、`/tmp????????_*` 兜底，**开头的斜杠不能省**——不带 `/` 的 pattern 在任意层级生效，实测会误伤 `app/tmp_uploads/`、`data/tmp_uploads/`；另外**不能带尾斜杠**，带了只匹配目录，而 Starlette spool 出来的正是同名格式的普通文件）、所有人的测试写法、README 的测试章节。

**状态**：已落地，commit `dde1115`（第 1 条）、`9cf7f4c`（第 2、3 条 + `.gitignore` 锚定）、`8c9b02c`（第 2 条的两个前提 + `app/` 收集）、`24aeac1`（第 4 条修订：生产入口改由 lifespan 显式设置）。全部实测过，反证见这几个提交的提交信息。

**另一条兜底（`24aeac1` 之后补的）**：`.gitignore` 原先是 `/tmp????????/`（带尾斜杠），**只匹配目录**。决定性的证据是在一个临时 git 仓库里做的 A/B（旧规则 vs 新规则各建一个仓库、真的把目录和文件建出来、用 `git check-ignore -q` 的退出码判定）：

```
--- 旧规则 '/tmp????????/' ---
    tmpabcd1234    (文件) ignored=False   ← 漏
    tmpdir12345    (目录) ignored=True
    tmpwd4n6n_2    (文件) ignored=False   ← 漏
--- 新规则 '/tmp????????' + '/tmp????????_*' ---
    tmpabcd1234    (文件) ignored=True
    tmpdir12345    (目录) ignored=True
    tmpwd4n6n_2    (文件) ignored=True
```

漏掉的正是最该拦的那种：Starlette 处理 >1 MB 上传时用 `SpooledTemporaryFile`，滚盘生成的是**文件**。现在改成三条（`/pytest-of-*/`、`/tmp????????`、`/tmp????????_*`，第三条管 tempfile 撞名重试的 `_1`/`_2` 后缀）。反向样本也验过（共 21 条，正反样本全对）：`pytest-of-jun.txt`、`app/tmp_uploads/x.csv`、`data/tmp_uploads/x.csv`、`tmp_short`、`tmp12345678901`、`docs/tmpabcd1234`、`app/tmpabcd1234` 都**不**被忽略，而 `data/uploads/.gitkeep`（被 `!` 规则救回）也不被忽略。

**验证这件事本身有两个坑，都踩过**：
1. **判据必须是 `git check-ignore -q` 的退出码，不能用 `-v` 的退出码。** `-v` 只要"有规则命中"就退出 0，**否定规则（`!xxx`）命中时它也退出 0** —— 于是 `.gitkeep` 会被误判成"被忽略"（第一版探针就报了这条假警报）。
2. **`--no-index` 问一个不存在的路径时，带尾斜杠的规则无从判断**（git 不知道它是不是目录），会答"不忽略"。所以目录类正样本必须先真的建出来再问。

需要说清的是：这是**兜底不是解法** —— 真正的解法是上面第 4 条把 spool 落点钉进 `.tmp/`。兜底只保证"万一又漏到仓库根，它不会出现在 `git status` 里"。

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

**状态**：**已实现**。路由层 `app/api/routes_datasets.py` 在提交 `9441429`
（task-3，上传/详情/画像三个接口）落地，七步顺序与上面写的一致；
`tests/test_upload.py:173` `test_upload_over_the_size_limit_is_413_and_writes_nothing`
钉住"超限时磁盘上什么都不留"，`tests/test_upload.py:204/215/230` 钉住三种
解析失败同样不留文件。

（原先这里写的是"实现在 task-8 之后的路由层（`app/api/routes_datasets.py` 尚未存在）"。
那句在 `9441429` 之后就已经过期 —— 留着会让后来读文档的人以为这个决策还悬着。
文档里的"状态"行属于会腐烂的那类信息，改动落库时应该顺手一起改。）

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

## D-11 执行 Python 脚本的方式：写文件再跑，不要内联在命令里

**背景（反复踩了三次才确认的）**：在 PowerShell 里把 Python 源码内联进命令
（无论是 `python -c "..."` 还是 `python -c @'...'@` here-string），**内容会被改写**，
而且失败方式看起来像"我的代码写错了"。

实测三种破坏：

1. **反斜杠转义序列被提前解释**：源码里写 `probe = "` + 反斜杠 + `u0041"`
   （本意是让 Python 去转义），到达 Python 时只剩 `\u0041`，
   报 `SyntaxError: unexpected character after line continuation character`。
   更隐蔽的一次：一个 form-feed 转义在传输中变成了**真的控制字符**，
   把一行劈成两半，报 `SyntaxError: unterminated string literal`，
   结果我以为是自己脚本的语法错，其实脚本根本没跑起来。
2. **引号被吞**：`print("  hello, 中文 OK")` 到达 Python 变成
   `print(  hello, 中文 OK)`，报 `SyntaxError: invalid syntax`。
   同一批命令里别的 `print("...")` 却没事 —— 所以它**不是确定性规则**，
   而是某些字符组合触发，这正是它危险的地方：你没法靠"避开某个字符"来防。
3. **静默不执行**：有的命令没有任何报错、也没有任何输出就结束了。

**规则**：需要跑 Python 时 —— 用编辑工具把脚本**写成 `.tmp/xxx.py` 文件**，
然后 `& ".\.venv\Scripts\python.exe" .tmp\xxx.py` 执行。一次性脚本跑完删掉。
`.tmp/` 已在 `.gitignore` 里（D-07）。

**为什么写进决策记录**：这不是"操作习惯"，它**污染过调查结论**。
有一次我用内联脚本"验证" `git show` 的输出，脚本静默没跑，我差点据此下结论。
凡是"跑个脚本看看"得到的证据，都要能指出脚本文件本身。

**代价（明确接受）**：多一次文件写入和一次删除。相比"证据可能来自一个根本没跑的脚本"，
这个代价可以忽略。

**影响到**：所有人的调试流程。提交信息仍然走 here-string + `git commit -F`
（那是**文本内容**，不是可执行代码，且已经稳定用过多次）。

**状态**：已确认。三次真实失败分别发生在修复 `tests/test_loader.py` 假绿断言、
补 D-06 文字、验证 `git show` 输出时。

**补记（2026-10-13，写提交信息时又踩了一次同源的坑）**：D-11 说的是"Python 源码
不要内联进命令"，但**提交信息**一直走 `Set-Content -Encoding UTF8` + `git commit -F`
—— 而 PowerShell 5.1 的 `Set-Content -Encoding UTF8` **会写 BOM**。
实测（`.tmp/bom_lab`，前 3 字节）：

```
a_setcontent.txt  EF BB BF  BOM=True    ← Set-Content -Encoding UTF8
b_outfile.txt     EF BB BF  BOM=True    ← Out-File -Encoding UTF8
c_dotnet.txt      E4 B8 AD  BOM=False   ← [System.IO.File]::WriteAllText + UTF8Encoding($false)
```

BOM 跟着 `git commit -F` 进了提交对象。用 `git cat-file commit <hash>` 看原始字节
（**不能用 `git log --format=%s`** —— 那是 PowerShell 管道解码后的样子，正是 D-11
说的那种"看起来像证据"的东西）：

```
e4ab7f3  BOM=False   ← 修好之后
24aeac1  BOM=False
9441429  BOM=False
df63d12  BOM=True    ← 之前几个都带
```

修法：提交信息文件一律用 `.tmp/write_commit_msg.py`（Python 以 `encoding="utf-8"`
写）而不是 `Set-Content`。**已经在提交里的 BOM 不回改** —— 那是重写历史，
收益只有一个不可见字符，不值得。

---

## D-12 报给用户的编码，描述**文件实际的样子**，不描述"哪个解码器读成功了"

**背景（QA 提的次要问题，被实验证实是真问题）**：`CSV_ENCODINGS` 的第一位是
`utf-8-sig`，而 Python 的 `utf-8-sig` 解码器对**没有 BOM** 的普通 UTF-8 文件
同样能正常解码（BOM 只是可选前缀）。于是第一个就命中并 `return df, encoding`，
**永远走不到 `"utf-8"`**。

实测（同一份内容，只有 BOM 有无之别）：

```
probe_bom.csv  （带 BOM）→ reported encoding = 'utf-8-sig'   ✓
probe_nobom.csv（无 BOM）→ reported encoding = 'utf-8-sig'   ✗ 文件里没有 BOM
```

后果不是算错数，而是**日志在说一件不真实的事**：`app/api/routes_datasets.py:171-174`
会打印 `[upload] xxx.csv 解析成功：N 行 M 列，编码 utf-8-sig`，
排错的人会照着"这是带 BOM 的文件"去查。

**决定**
- **解码逻辑一个字不动**（`CSV_ENCODINGS` 的顺序、`_read_csv` 的循环都不改）。
  那是核心逻辑，动它要重跑整套编码测试，收益与风险不成比例。
- 在读成功之后加一步翻译：`app/data/loader.py` 的 `_describe_encoding(path, encoding)`
  —— 只有当命中的是 `utf-8-sig` 时才回头读文件头 3 字节，是 `EF BB BF` 就报
  `utf-8-sig`，否则报 `utf-8`；其他编码原样返回。
- `load_dataframe` 的 docstring 相应改成"encoding **描述这个文件实际是什么编码**"。

**为什么这样切**："怎么读的"和"怎么报的"分开，两边都能单独讲清楚；
翻译函数的 docstring 里留了完整来龙去脉，后来的人不会以为它多余。

**真实端到端证据**（起真实 uvicorn，三个文件各上传一次，看服务端日志原文）：

```
[upload] eeecf930....csv 解析成功：2 行 2 列，编码 utf-8       ← 普通 UTF-8（无 BOM）
[upload] 6b00face....csv 解析成功：2 行 2 列，编码 utf-8-sig   ← 真的带 BOM
[upload] 360f8079....csv 解析成功：1 行 2 列，编码 gbk
```

**有牙的证据**：把翻译那行注释掉 → `tests/test_loader.py` 与
`tests/test_data_layer_e2e.py` 共 `2 failed`，红在
`AssertionError: assert 'utf-8-sig' == 'utf-8'`；恢复后全绿（当时是 137 passed；
总数会随测试增加而变，**别把这里的数字当成"现在多少项"**）。

**测试为什么是两条而不是一条**：单看"带 BOM 报 utf-8-sig"或单看"不带 BOM 报 utf-8"，
一个恒真的实现（永远返回 `utf-8-sig`、或永远返回 `utf-8`）都能骗过其中一条。
`test_load_utf8_csv_returns_frame_and_encoding` 与
`test_csv_with_bom_is_reported_as_utf8_sig` 必须一起看。

**影响到**：`app/data/loader.py`、`tests/test_loader.py`、
`tests/test_data_layer_e2e.py`、`app/api/routes_datasets.py`（只消费不修改）。

**状态**：已实现，当时全绿为 `137 passed`（仓库根与 `tests/` 子目录两个入口都验过）。
**当前基线计数不写在这里** —— 会腐烂。要看现在多少项，跑一次 `pytest -q`，或看 `README.md` 的状态块。

---

## D-13 `_read_csv` 故意不接 `LookupError`

**背景**：QA 指出 `_read_csv` 只 `except UnicodeDecodeError / EmptyDataError / ParserError`，
不接 `LookupError`。如果将来有人把 `CSV_ENCODINGS` 里某个编码名打错，
Python 找不到这个编码会抛 `LookupError`，用户会看到 500 而不是 400。

**决定：不接。** 打错编码名是**我们的 bug**，不是用户的文件有问题。
把它翻译成"无法识别这个 CSV 的编码（已依次尝试……）"，用户会去反复检查自己那个
完全正常的文件 —— 那是拿谎话换一个好看的状态码。500 + 完整 traceback 才是这件事的实话，
`app/main.py` 的兜底处理器已经负责它。

**当前不可利用**：`CSV_ENCODINGS` 是模块常量，用户碰不到。这条记下来是为了
"下次有人想接 `LookupError`"时知道这不是疏忽。

**影响到**：`app/data/loader.py`（注释写在 `CSV_ENCODINGS` 上方）。

**状态**：已定，无需改动代码。

---

## D-14 白名单式的忽略规则要**推导**，不要凭印象列；探针假红比假绿更危险

**背景**：项目根出现三个空目录 `pip-build-tracker-2z2fh4kg`、`pip-ephem-wheel-cache-9yohrahm`、`pip-install-c0exc6do`（同一秒创建，`2026-10-13 11:26:04`），而 `.gitignore` 当时只挡了 `pip-unpack-*/`，所以它们露在 `git status` 里。

**根因与 D-07 完全相同**：pip 内部用 `tempfile.mkdtemp()`，本机 Python 子进程写不进系统临时目录 → `tempfile` 退回 cwd（项目根）。不是 pip 的毛病，也不是新问题，只是**同一根因的另一个受害者**。

**决定：前缀名单必须从生成点推导，不能凭印象列。** pip 生成临时目录只有一条路径：

```
pip/_internal/utils/temp_dir.py:174
    path = os.path.realpath(tempfile.mkdtemp(prefix=f"pip-{kind}-"))
```

所以前缀恒等于 `pip-{kind}-`，把 kind 的取值列全就穷尽了。kind 的权威来源 = 源码里 `kind="..."` 的字面量 + `temp_dir.py:26-30` 的 `tempdir_kinds` 枚举，逐处核对（本机 pip 26.2.1）：

```
build-env-install  build_env.py:296      build-tracker  build_tracker.py:47
download           download.py:105       install        install.py:440 / lock.py:118
metadata           _dists.py:128         modern-metadata metadata.py:23
target             install.py:423        uninstall      req_uninstall.py:214/238
unpack             prepare.py:110/467    wheel          wheel.py:109
build-env / ephem-wheel-cache / req-build   ← tempdir_kinds 枚举
```

`pip-build-env-install-` 不用单列 —— `pip-build-env-*/` 的 `*` 能吃到后面的 `install-xxx`（**实测过**，不是想当然）。

**这个推导过程真的抓到了漏网的**：第一版凭印象只写了 8 条，探针跑出 4 条漏网（`pip-download-*`、`pip-metadata-*`、`pip-target-*`、`pip-uninstall-*`），补齐后 14 条正样本 + 9 条反样本全对。

**探针假红比假绿更危险**：第一版探针直接 `git check-ignore -q --no-index pip-unpack-abcd1234`，6 条"应被忽略"的样本全报不忽略，**看起来像 `.gitignore` 写错了**。真实原因是判据无效 —— 规则是 `pip-unpack-*/`（**带尾斜杠 = 只匹配目录**），而 git 对一个**不存在**的路径无从判断"是不是目录"，于是带尾斜杠的规则一律不生效。第一版唯一通过的 3 条，恰好是磁盘上**真实存在**的那 3 个目录 —— 这不是巧合，是判据在按"文件存不存在"给答案。决定性对照：

```
git check-ignore -q --no-index pip-unpack-abcd1234    → exit=1（不忽略）
git check-ignore -q --no-index pip-unpack-abcd1234/   → exit=0（忽略）
New-Item -ItemType Directory pip-unpack-probe99; 再问 → exit=0（忽略）
```

同一个名字，加不加尾斜杠、存不存在，答案完全不同。**所以目录类正样本必须先真的建出来再问。**（这条在 D-07 的"验证坑"第 2 条里记过一次，这次是同一个坑的复发 —— 说明写进文档还不够，得在探针里写成注释。）

**为什么假红更危险**：假绿会让你以为做完了，假红会**让你去"修"一段本来正确的代码**。这次差点就把好好的 `pip-unpack-*/` 改成不带尾斜杠，那会让 `pip-unpack-*` 这类**文件**也被忽略 —— 修出一个真 bug。

**影响到**：`.gitignore`（新增 12 条 `pip-*-*/`）、`.tmp/check_pip_gitignore.py`（探针，已把两个坑写成文件头注释）。

**状态**：已落地。`git check-ignore` 实测 14 条正样本全忽略、9 条反样本零误伤；探针跑完不留残留（第一版漏了收中间目录 `nested/`，已修）。

---

## D-15 示例数据的行尾用 `.gitattributes` 钉住（`text eol=lf`），不放宽测试

**背景**：`data/samples/sales.csv` 提交后，在**干净检出**里字节会变。

本机 `core.autocrlf=true` 且仓库原先没有 `.gitattributes`，于是：

- 提交时 git 把 CRLF 归一化成 LF → 仓库里的 blob 是 LF（141028 字节，md5 `910d6723…`）
- **检出**时 git 把 LF 换回 CRLF → 干净克隆出来是 143197 字节、2169 个 CR、md5 `6d4f6dad…`

后果：`tests/test_sample_data.py` 的三条**逐字节**断言在克隆里必红。实测（`git clone` 副本，HEAD `c15aa92`）：

```
146 passed, 3 failed
FAILED tests/test_sample_data.py::test_file_is_utf8_with_bom_and_lf_only
FAILED tests/test_sample_data.py::test_regenerating_in_process_reproduces_the_committed_file
FAILED tests/test_sample_data.py::test_running_the_script_reproduces_the_committed_file
```

**为什么不改测试**：那三条断言钉的是这个示例数据集**唯一的可复现性承诺** —— "任何人 clone 下来拿到的都是同一个文件"。放宽成"先统一换行再比较"，承诺就从测试里消失，降级成提交信息里的一句口头保证。而且第 7 天的验收正是"全新克隆 → 按 README 跑 → pytest 全绿"。**该修的是行尾策略，不是断言。**

**五种写法的 A/B（全部真实跑过，每种都造一个独立提交 + worktree 检出）**：

| 写法 | 检出 md5 一致 | 检出 CRLF | 把 CRLF 提交进去后仓库 blob 的 CRLF | 测试 |
|---|---|---|---|---|
| 不加 `.gitattributes`（基线） | ✗ | 2169 | 0（归一化） | 3 failed, 9 passed |
| `data/samples/sales.csv -text` | ✓ | 0 | **2169（CRLF 原样进仓库）** | 12 passed |
| **`data/samples/*.csv text eol=lf`** | **✓** | **0** | **0（自愈）** | **12 passed** |
| `data/samples/** -text` | ✓ | 0 | 2169 | 12 passed |
| `* text=auto eol=lf` | ✓ | 0 | 0 | 12 passed |

**决定：`data/samples/*.csv text eol=lf`。** 理由是它**严格强于** `-text`：

- `-text` 让 git 对该文件**不做任何转换** —— 谁把 CRLF 提交进去，CRLF 就原样存进仓库（实测 blob 的 CRLF 数 = 2169），下次检出又带 CRLF，测试再红一次。**它只是把问题挡住，不会自愈。**
- `text eol=lf` 在提交时归一化回 LF（实测 blob 的 CRLF 数 = 0）—— 即使有人用编辑器把文件存成 CRLF，仓库也会自己愈合。

**用 `*.csv` 通配而不是钉死单个文件**：第 6 天有富余会再加第二份示例数据，通配让新 CSV 自动获得同样保证，不用记得回来改。

**刻意不用**另外两种：

- `data/samples/**` 会把未来的 `.xlsx` 也当文本做行尾转换 —— **那会改坏二进制文件**（xlsx 是 zip，内容里就含 `0D 0A` 字节序列）。
- 全仓 `* text=auto eol=lf` 会一次性改变所有文件在 Windows 上的检出行为，而本项目只有示例数据需要逐字节稳定；源码是 CRLF 还是 LF 不影响任何测试。收尾期不做范围外的大改动。

**作用域已实测**（`git check-attr`）：只有 `data/samples/` 下的 csv 得到 `text: set` / `eol: lf`；`app/`、`tests/`、`docs/`、`requirements.txt` 全部仍是 `unspecified`。工作区里本来就有 6 个带 CRLF 的源文件（`app/data/loader.py`、`app/errors.py`、`docs/DECISIONS.md`、`tests/test_errors.py`、`tests/test_loader.py`、`tests/test_upload.py`），**本决定不碰它们**。

**验证方法（探针怎么在"不污染主工作区"的前提下造干净检出）**：`.tmp/probe_gitattributes.py`。踩到的坑：从 Python 子进程调 `git clone` 在本机会炸在 msys 层（`sh.exe: *** fatal error - NtCreateDirectoryObject(\BaseNamedObjects\msys-2.0S5-…): 0xC0000022` / `fatal: Could not read from remote repository`），而同一个命令在 PowerShell 里能跑。改用 **plumbing 造提交（`read-tree` + `hash-object -w` + `update-index` + `write-tree` + `commit-tree`）+ `git worktree add --detach`** 检出，等价于干净检出，且完全不碰主仓库的工作区与分支（只留可回收的悬挂对象）。

**影响到**：`.gitattributes`（新增，含逐条理由的注释）、`data/samples/*.csv`、第 7 天的"全新克隆验收"。

**状态**：已落地。主工作区全量 149 passed；干净检出里 `tests/test_sample_data.py` 12 passed、md5 `910d6723…`。

---

## D-16 验证"全局状态有没有残留"必须用**独立进程**，且要当场读

**背景**：D3 复验（QA task-16）发现 `tests/test_workflow_registry.py` 只在 `setup_function()`
里 `registry.clear()`，没有 teardown，于是**最后一条用例**登记的工作流会活到整个会话结束。
QA 实测：正序跑完 `registry.count()=0`、**反序跑完 =1**，两次都是绿灯 —— 用例之间已经通过
全局状态连在一起了，只是碰巧还没有别的文件去数注册表里有几条。

**Lead 修完之后做了变异测试（拿掉 teardown，期望残留变回 1），结果两次都是 0** ——
差点得出"这个改动什么都没修"的错误结论。真正的原因是**探针自己的 bug**，有两层：

1. **同一个进程里连跑两次 `pytest.main()`，第二次跑的是内存里的旧模块。**
   `tests/*.py` 在第一次就被 import 进 `sys.modules`，源码改动对第二次无效。
   pytest 其实**警告过**：
   ```
   PytestAssertRewriteWarning: Module already imported so cannot be rewritten; reverse_plugin
   ```
   被我当成噪声漏看了。
2. 修成独立进程后，我又把 `subprocess` 的 `env` 手动收窄成一个"最小环境"
   （只留 `SYSTEMROOT`/`PATH`/`PYTHONIOENCODING`/`PYTHONPATH`），结果子进程
   `exit=1`、残留文件根本没生成，被读成 `-1` —— **又一个假红**。

**决定**

1. **验证进程级全局状态（注册表、`tempfile` 缓存、`sys.modules`）的残留，一律起独立进程**，
   并让被测进程在**会话结束的那一刻**（`pytest_sessionfinish` 钩子）把值写到文件里再读，
   不要在宿主机进程里"跑完再看"。
2. **不要手工构造 `subprocess` 的 `env`**。继承父进程环境，只覆盖必须改的那几个键
   （这里是 `PYTHONIOENCODING`、`PYTHONPATH`）。为了"干净"而收窄环境，会把
   `SystemRoot` 之外的路径、`TEMP`、`USERPROFILE` 之类一起砍掉，得到的是一个
   与被测环境无关的假红。
3. 变异测试的**期望值必须来自已观测的事实**，并在脚本里写死：
   本次是"有 teardown → 0、无 teardown → 1"（QA 实测过的两个数）。
   两边相同就说明探针无效，**不能**据此宣布修复有效。

**最终证据**（独立进程 + `pytest_sessionfinish` 读值）：

| 实现 | 反序跑完 `registry.count()` |
|---|---|
| 有 `teardown_function` | **0** |
| 无 `teardown_function` | **1** |

与 QA 的原始观测逐字一致。**探针无效时的"0"和真修好的"0"长得一模一样** ——
这就是为什么变异那一半不能省。

**影响到**：`tests/test_workflow_registry.py`（新增 `teardown_function`）、
后续所有"验证全局状态"的探针（`.tmp/residual_plugin.py` 的写法可复用）。

**状态**：已落地。全量 352 passed；反序跑完残留 0。

---

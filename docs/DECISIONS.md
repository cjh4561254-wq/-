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

**补充（D4 期间新增的三条同类证据 —— 都是"探针在撒谎"，不是被测代码有问题）**

上面那两条不是偶然。D4 期间又攒了三条，症状不同、病根一样：**验证工具自己出错时，
给出的结论和"真的没问题"无法区分**。

4. **变异脚本还原文件时把行尾改坏了。** 用 `Path.write_text(text, encoding="utf-8")`
   还原，Windows 文本模式会把 `\n` 翻译成 `\r\n`。文件**内容**一样、**字节**不一样，
   而且 `git diff` 也看不出来（git 在 `core.autocrlf=true` 下自己也会归一化）。
   后果是"还原后 sha256 逐字节一致"这条自检失效，变异测试的基线不再可信。
   **写法：`write_text(text, encoding="utf-8", newline="")`**，并在脚本里断言还原后
   的 `sha256` 与备份相同。

5. **探针把 `b"\r"` 写成了 `b'\\r'`。** 一个查"仓库里哪些 `.py` 的 blob 带 CR"的探针，
   常量写成了两个字节的"反斜杠 + r"，于是**连 500 多行的文件都报 `CR = 0`**。
   这个 `0` 本身就是脚本坏了的证据 —— 正常情况下 500 行的文件必然有 500 多个 LF，
   而 `b'\\r'` 永远不会出现在任何文本里。
   **写法：用 `CR = bytes([13])` / `LF = bytes([10])`，并让探针先自检**
   （喂一个已知含 CRLF 的字节串，必须报 CRLF；报不出来就直接宣布结论不可用）。

6. **"等价变异"会让变异测试假装通过。** D4 的 `routes_workflows.py` 变异里，
   第一版把"先判终态再判超时"改成了"先算超时值但仍在赋值之前判终态" ——
   **行为完全没变**，所以 27 条用例照样全绿。如果只看到"没红"就下结论，
   会得出"这批测试抓不住顺序问题"，而实际上改法根本没改到东西。
   **判据：变异必须改变可观测行为**；改完先问一句"这个改动会让哪一条断言的输出不一样"，
   答不上来就说明这是等价变异，要换一个靶子。

**一句话**：第 3 条说的"两边相同就说明探针无效"，在 D4 被印证了三次。
**变异测试的真正价值不是"变红"，而是"能变红"** —— 变不红的时候，第一个要怀疑的是探针，
不是代码。

7. **前提变了、结论没重测。** D4 期间 backend-engineer 报"uvicorn 进程下 Starlette 的
   multipart spool 仍会在仓库根短暂留 `tmpXXXXXXXX`，建议 lifespan 覆盖到 uvicorn 进程"。
   他看到的 `tmp5l6t2vg9` / `tmpar8_5c5m` 是**真实现测**，但那是 TEMP 覆盖落地**之前**的
   观测；之后没重测就当现状复述了。复现实验（真实 uvicorn + `curl --limit-rate 300K`
   上传 2 850 022 字节，每 0.15 秒采样 166 次）结果是**仓库根 0 次命中、`.tmp/` 40 次命中**，
   与既有结论一致。
   **这不是"他看错了"，是"证据过期了"** —— 与 D-15 里"文档写'尚未提交'一定会过期"同类，
   只不过这次过期的是**口头结论**。判据：**引用一条观测时，先问它是在哪个提交/哪次改动
   之前测的**；前提变过就要重测，否则它和"探针坏掉"一样，会把人引向不存在的 bug。

8. **错误前提会沿着回报链往下传。** 同一轮里，backend-engineer 的回报中列了一个
   `updated_at` 字段（实际**全仓库 `.py` 里 grep 0 命中**，是回报里凭印象写的），
   Lead 照着这个前提给他下了一条"给 `updated_at` 补注释"的指令。如果双方都不核实，
   最终产物会是"一个解释不存在字段的注释"，或者更糟——有人为了对齐文档把它**真加进代码**。
   **判据：回报里的字段名/数字，凡是要写进指令或文档的，先 grep 一次。**
   （顺带确立了防线口径：**不要用注释防这类错，用断言** —— 注释不会失败，
   "响应键集合恰好等于契约字段"这条断言才会。）

---

## D-17 冻结"还没写的函数签名"用**棘轮**，不要直接 fail

**背景**：D4 是三方并行开工的一天（AI 写 `llm/` + `planner.plan()` + `orchestrator.py`，
BE 写 `core/background.py` + `routes_analysis.py`，QA 写两个测试文件）。并行开工的前提是
**接口先定死**，否则三个人各自理解一套，错只能在集成时才暴露。

于是要在开工前把五个函数的签名写进契约测试。但这五个函数**此刻还不存在**。

**两条都试过，只有一条能用**：

| 写法 | 后果 |
|---|---|
| 直接 `from app.llm.client import chat` | 收集期 `ImportError` → **整个测试文件报错**，契约测试自己成了红点，且分不清"契约错了"还是"还没写" |
| 全部 `skip` 直到函数出现 | D4 结束时无法区分"五个都写对了"和"五个都还没写" |
| **棘轮（采用）**：不存在 → `skip`；存在但签名不符 → `fail` | 契约测试**现在就是绿的**（不制造假红），而"跳过条数"是一个可数的进度指标 |

**决定**

1. 冻结未实现的签名时，用 `try: import / except ImportError: return None`，
   `None` → `pytest.skip(f"... 还没写（D4 施工中）")`。
2. **钉参数名，不只钉个数**。调用方是**按关键字**调用的
   （`plan(question=..., profile=...)`），名字变了就是破坏契约，哪怕位置和个数都一样。
   所以断言写成 `[(p.name, p.default) for p in signature(func).parameters.values()] == 契约列表`。
3. **"跳过条数归零"要写成明文验收判据**（本次：D4 结束时 6 skipped → 0）。
   否则"契约有没有被遵守"只能靠感觉，而感觉读不出来。
4. 每个棘轮项都要有一条**现在就能跑**的姊妹断言，否则这个文件在 D4 期间等于没测东西。
   本次是三个变异测试（validator / 接口形状 / 配置默认值），基线 `(19, 0, 6)`，
   三个变异的红数分别是 1 / 2 / 2 —— **都 > 0，无假绿**。

**为什么不直接等函数写完再写测试**：D4 的价值就在于三方并行。等写完再对齐，
就等于放弃并行、把集成风险全部推迟到最后 —— 那正是第 12.2 节把 D4 列为"最大风险点"的原因。

**影响到**：`tests/test_contracts_d4.py`（25 项 = 19 passed + 6 skipped）、
将来 D5/D6 冻结 `render_chart` / `validate` / `build_report` 时照此办理。

**状态**：已落地（提交见 §14.7）。D4 施工中，跳过条数应为 6；D4 完成后应为 0。

---

## D-18 会引发 400 的字段，不依赖 SDK 的"版本相关行为"来传递

**背景**：DeepSeek 思考模式的官方文档写死了一条硬约束：

> 携带了 `tools` 参数的请求，在后续所有请求中，必须完整回传 `reasoning_content`
> 给 API —— 即使该轮模型未实际进行工具调用。若您的代码中未正确回传
> `reasoning_content`，API 会返回 400 报错。

**注意后果是 400，不是降级。**

官方 Tool Calls 示例里的写法是 `messages.append(response.choices[0].message)` ——
直接把 SDK 返回的 message 对象追加进历史。这个写法**能 work**，但原因值得警惕。

**Lead 用 `httpx.MockTransport` 截获真实请求体实测（openai 2.41.0）**：

| 路径 | 第 2 轮请求体里是否含 `reasoning_content` |
|---|---|
| `messages.append(sdk_message)` | **True** |
| 手工拼 dict | **True** |

两条都对。**但契约仍规定必须手工拼 dict**，理由：

`message.reasoning_content` 之所以能被 SDK 序列化出去，是因为 openai SDK 把不认识的字段
塞进了 `message.model_extra` 并**原样回传**。这是 **SDK 的版本相关行为，不是 API 契约** ——
SDK 某次升级如果收紧了 `model_extra` 的处理，这个字段就会**静默消失**，
而消失的后果是**线上 400**（不是"少了个字段但还能跑"）。

**决定**

1. `client.chat()` **手工拼** `message` dict（`role` / `content` / `tool_calls` /
   `reasoning_content`），**不把 SDK 对象透传给上层**。
2. 把"必须带上它"做成**构造期约束**：`LLMReply` 上的 `@model_validator(mode="after")`
   检查"拿到了 `reasoning_content` 就必须出现在 `message` 里"。放模型里而不是写进
   client 的注释 —— 注释拦不住改动，而这个校验在**任何**构造 `LLMReply` 的地方都会跑。
3. 判据用**实际发出的 JSON**，不用"SDK 对象上有没有这个属性"。
   这次两个判据恰好同向，但它们是两回事：前者是契约，后者是实现细节。

**一般化**：凡是"字段丢了会直接报错（而不是降级）"的地方，都要把它变成
**代码里拦得住的约束**，而不是一条写在文档或注释里的约定。降级类字段可以从宽，
报错类字段必须从紧。

**影响到**：`app/schemas_llm.py`（`LLMReply` 的 validator）、
`app/llm/client.py`（D4 实现时的落点）、`tests/test_contracts_d4.py`
（`TestLLMReplyCarriesReasoningContent` 的 5 条，含 3 条反例）。

**状态**：契约已落地并配变异测试（删掉校验 → `1 failed`）。`client.py` 本身 D4 施工中。

---

## D-19 配置来源改为**系统环境变量**，不再读 `.env`；大模型三名各认两个名字

**背景（用户指令，逐字）**：

> 别读取.env了，我本地电脑的环境配置里面有deep seek的key

**第一步不是改代码，是先证明"不读 `.env` 不会悄悄改行为"**。用
`.tmp/probe_env_compare.py`（`DotEnvSettingsSource` 解析 `.env`，只比较长度与同/异，
不打印值）实测：`.env` 里出现的 15 个键，值**全部等于** `Settings` 的默认值。
所以去掉 `.env` 对当前取值**零影响**，是一次纯粹的减法。这一步不能省 ——
否则就成了"一边改配置来源、一边悄悄改行为"，两件事混在一起，出问题时分不清是谁干的。

**机器上到底有什么（只看名字与长度，从来看值）**：

| 作用域 | 命中 |
|---|---|
| 用户级（HKCU） | 一个都没有 |
| 机器级（HKLM） | `DEEPSEEK_API_KEY`(35)、`DEEPSEEK_BASE_URL`(24)、`OPENAI_API_KEY`(51)、`OPENAI_BASE_URL`(26)、`GJ_API_KEY`(51)、`QY_API_KEY`(49)、`MINERU_TOKEN` |

**一条容易误判的事实**：当前进程的环境块里，`*_API_KEY` / `*_TOKEN` 这类名字
**一个都继承不到**（只有 `DEEPSEEK_BASE_URL` 这种非密钥变量在）。所以
"`$env:DEEPSEEK_API_KEY` 是空的"**不能**推出"机器上没配"。
要验证真实情况，必须显式从机器作用域取：

```powershell
[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','Machine')
```

注入之后实测可用：`GET https://api.deepseek.com/models` 返回 `deepseek-flash`
与 `deepseek-v4-pro` 两个模型 id。

**决定**

1. **`Settings.model_config` 里去掉 `env_file` / `env_file_encoding`**
   （`app/config.py:61-70`，注释写明"故意没有"）。配置来源只剩一个：进程环境变量。
   显式 `Settings(_env_file=<path>)` 仍然可用（实测文件里的 `MAX_ROWS=424242` 生效）——
   那是显式传入，与"悄悄读一个 `.env`"是两回事。
2. **三个大模型字段各接两个环境变量名**，`AliasChoices(*names)`，**`LLM_*` 在前**：

   | 字段 | 名字（左优先） |
   |---|---|
   | `llm_base_url` | `LLM_BASE_URL` / `DEEPSEEK_BASE_URL` |
   | `llm_model` | `LLM_MODEL` / `DEEPSEEK_MODEL` |
   | `llm_api_key` | `LLM_API_KEY` / `DEEPSEEK_API_KEY` |

   顺序不能反：反了的话机器上那个 `DEEPSEEK_*` 会**悄悄盖掉**显式设置的 `LLM_*`，
   而"我明明设了 `LLM_MODEL` 却不生效"是最难查的一类配置问题。
   三组名字与并集 `LLM_ENV_VAR_NAMES`（`app/config.py:46-55`）**同时被字段声明与
   测试夹具使用** —— 清单只有一份，抄成两份迟早漏一个，漏掉的那个正好是本机真实存在的名字。
3. **`llm_model` 默认 `"deepseek-flash"`**（`app/config.py:93`）。依据是上面 `/models`
   的实测返回值。理由：本机只给了 base_url 和 key，**没有模型名这一项**；留空会让
   `llm_configured` 永远为 `False` —— 表现就是"配了 key 却不生效"。
4. `model_config` 加 `validate_by_name=True`（pydantic ≥2.11 的新名字，老写法叫
   `populate_by_name`）。因为 `validation_alias` 会把**字段名**从取值来源里顶掉，
   而 `Settings(llm_api_key="x")` 这种按字段名传参在测试里用得很顺手。

**候选形态先探针后落地**：`.tmp/probe_candidate_settings.py` 在真实
`pydantic 2.13.4 + pydantic-settings 2.15.0` 上跑了 9 条，全 PASS：
只给 `DEEPSEEK_*` 能配上；不给模型名落到默认值；两套名字都给时 `LLM_*` 赢；
`DEEPSEEK_MODEL` 能覆盖默认值；按字段名传参仍生效；**不配 `env_file` 就不读 cwd 下的
`.env`**（哨兵 `MAX_ROWS=424242` 被忽略）；显式 `_env_file=` 仍被读取；
`repr()` 不含密钥；环境变量名大小写不敏感。

**改完之后立刻暴露的一个真问题**：用户机器上**真的有 key**，于是所有断言
"未配置"的用例（`Settings().llm_configured is False`、没配模型时必须抛
`LLMNotConfigured`）会**在没 key 的机器上绿、在有 key 的机器上红** —— 而后者才是用户的机器。
处理办法不是把测试改成跳过，而是加一个 autouse 夹具
`conftest.py:230-241 _isolate_llm_env`：每个用例开始前把六个名字全部清空
（清单直接取自 `config.LLM_ENV_VAR_NAMES`，不另抄一份）。
**测试不许假设"我这台机器碰巧没配"。**

**取值规则本身要有测试**（`tests/test_config.py`，9 条）：名字清单是**字面量**（不从实现里读，
改名字就必须同时改测试与文档）；六个名字每个都要真的接上线；只给 `DEEPSEEK_*` 就算配好；
两套都给时 `LLM_*` 优先；`DEEPSEEK_MODEL` 覆盖默认值；`LLM_API_KEY=""` 能压住兜底名字
（否则用户想"临时关掉模型"就做不到）；`repr` 不泄密；cwd 下放 `.env` 也不被读。

**一般化**：配置写错**不会报错**，只会静默不生效（接口正常、日志正常、模型一次都没被调用）。
凡是"名字对不上就等于没配"的地方，都要有一条把**取值规则本身**钉成测试，
而不是靠"我读了一遍代码觉得对"。

**影响到**：`app/config.py`、`conftest.py`、`app/llm/client.py`（`LLMNotConfigured`
文案现在同时说明 `LLM_*` 与 `DEEPSEEK_*`）、`tests/test_config.py`（新建）、
`tests/test_health.py`、`tests/test_llm_client.py`、`README.md` / `.env.example` /
`docs/ARCHITECTURE.md` §4.0.1（task-21）。

**状态**：代码与测试已落地（提交 `db95df3`），文档同步进行中。
`.env` 文件本身没有删（那是用户机器上的本地文件），只是**不再被读**。

---

## D-20 测试不许依赖外壳的编码设置（子进程 stdout 的编码）

**背景**：全量测试里 `tests/test_temp_dir.py::test_startup_pins_temp_files_into_the_project`
红了，报的是路径里的中文变成了替换字符：

```
E  assert 'D:\\\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\u013f01\\...' == 'D:\\个人项目01\\...'
```

**归因（实测，不靠推理）**：

| 条件 | 结果 |
|---|---|
| 同一份代码，外壳里 `PYTHONUTF8=1` | **3 passed** |
| 不带 `PYTHONUTF8` | **1 failed** |
| `git archive HEAD` 导出到仓库外（提交态、不含工作区改动） | **1 failed** |

第三行最关键：提交态也红，说明它**与本次工作区改动无关**，是这条测试**自己的**环境耦合。

**机制**：`tests/test_temp_dir.py:79-88` 的 `_run_in_clean_process()` 用
`subprocess.run(..., encoding="utf-8", errors="replace")` 解码子进程 stdout；
而子进程的 stdout 是管道时，Python 用**区域编码**（本机 cp936）写字节。
本仓库路径含非 ASCII 字符（`个人项目01`），两边编码不一致 → 解出来就是 `\ufffd`。
纯 ASCII 路径的机器上永远看不到这个失败 —— 所以它是典型的"只在我这儿绿"。

**决定**：在 `_run_in_clean_process()` 里给子进程显式钉住 IO 编码
（`env["PYTHONIOENCODING"] = "utf-8"`），父进程按 utf-8 解码，两边对齐。
**不改断言、不改被测行为**。如果反过来把父进程改成按区域编码解码，
测试就又会变成"跟着外壳走"。

**一条更一般的教训**：这类失败的表征是"**和我这次改的东西完全无关的用例红了**"。
它很容易被当成 flake 重跑了事。正确的问题是："它上次绿的时候，外壳的环境是什么？"
—— 也就是 D-16 第 7 条（前提变了、结论没重测）在测试基础设施上的同一张脸。

**同一轮还修掉两条由本轮改动引出的测试缺陷**，两条都值得记，因为它们的失败方式很有代表性：

1. `tests/test_config.py` 里断言 `"env_file" not in Settings.model_config` —— 实测
   `SettingsConfigDict` **本来就会带一个 `env_file=None` 的默认键**，于是这条断言
   在什么都没改的情况下就红。判据要写成**键的值**（`model_config.get("env_file") is None`），
   不是键在不在。一条永远红的用例抓不住任何回归。
2. `conftest.py` 的夹具在**拆除阶段**调用 `config.reset_settings_cache()`，而
   `tests/test_planner_rules.py::test_llm_is_configured_reads_settings_at_call_time`
   会把 `config.get_settings` 换成假对象。夹具拆除发生在 monkeypatch 还原**之前**，
   于是拆除时拿到的是假对象、没有 `cache_clear` → 用例明明通过了却报 **ERROR**。
   修法：**在夹具建立时就把真函数的 `cache_clear` 抓在手里**
   （`clear_settings_cache = config.get_settings.cache_clear`），之后只调用这个引用。

---

## D-21 D4 验收② 只能用**真 key** 证明；Mock 证明的是"代码路径通"，不是"真的调得通"

**背景**：D4 验收标准 ② 是「配上 LLM 后，同一问题返回的 `plan.source == "llm"`」。
在此之前它一直验不了（D4 期间 `.env` 的 LLM 三项为空）。用户（m05971）说明机器上就有
DeepSeek key，这条路才通。契约测试与 `MockTransport` 只能证明**代码路径**是通的 ——
它们连不上任何东西，也证明不了 `reasoning_content` 回传契约在**真 API** 上成立
（那条契约的失败方式是 400，不是降级，见 D-18）。

**做法**：`.tmp/probe_d4_real_llm.py`，key 从**机器作用域**显式注入子进程
（`[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','Machine')`），
**只打印长度与前三字符，绝不打印值**。一次运行验两件事：

1. **D-18 的回传契约**：带 `tools` 的第 1 轮 → 第 2 轮必须原样回传
   `reasoning_content`（连同 `tool_call_id` 与工具结果），否则真 API 返回 400。
2. **D4 验收②**：用真实 `data/samples/sales.csv` 走完整 HTTP 流程，
   断言 `plan.source == "llm"`、工具名全在白名单内、报告已生成。

**结果：14 PASS / 0 FAIL**（两次独立运行，两轮请求都**没有** 400）。

| 观测 | 第 1 次运行 | 第 2 次运行 |
|---|---|---|
| 第 1 轮 `reasoning_content` 长度 | 374 | 355 |
| 第 2 轮 `reasoning_content` 长度 | 579 | 0（该轮模型直接作答、无工具调用） |
| 第 1 轮耗时 | 14.52 s | 2.13 s |

端到端（真实数据 2168 行 × 9 列）：上传 200 → `POST /api/analysis` **202** →
终态 `SUCCESS` / `step=done` / `error=None` → `plan.source=llm`、
`tasks=['group_aggregate']`、参数 `{'group_by': ['地区'], 'metric': '销售额', 'agg': 'sum'}`
→ 工具 `ok/failed = 1/0` → 报告 `llm_used=False`、`sections=1`、每节都带 `cites`，
首段逐字 `本次未使用大模型（llm_used=false），以下结论为工具结果的直接汇总`。

**必须说清楚的分工**：这一轮里**模型只参与了"规划"这一步**（`plan.source=llm`），
报告仍是模板（`llm_used=False`）。这是 D4/D5 的边界，**不是缺陷** ——
D5 才把"解释"也交给模型。把这两件事混在一句话里说（"跑通了真实大模型"），
读者会以为报告也是模型写的。

**两条容易误读的地方（自己先踩过）**：

- 探针用 `TestClient`，而 `TestClient` 会在响应返回**之前**把 `BackgroundTasks`
  同步跑完，所以"`POST /api/analysis` 耗时 1.85 s"**不能**当成"接口在等分析"的证据
  （与 D-16 第 2 条同族）。要验"接口不阻塞"，只能走真实 uvicorn。
- `llm_configured=True` 之后，"未配置时应如何"的测试必须由夹具先把六个变量名清空，
  否则在**用户这台机器上必红**（见 D-19）。

**一般化**：凡是"外部契约以**报错**（而不是降级）方式失败"的地方，Mock 测试是不充分的 ——
要留一条**真调用**的验收路径，并且明确写出它证明了什么、没证明什么。

**状态**：D4 验收② 已用真 key 实测通过；探针在 `.tmp/`（不进仓库）。

---

## D-22 "数字对、话是假的"要当成一类 bug；修好了必须证明"改回去会红"

**背景**：D4 QA 复验（基线 `d4fd727`）报了两条"重要"问题。它们的共同点是
**一个数字都没错，错的是描述这些数字的话**，而且两条都是**零测试覆盖**：

1. `app/agents/validation.py` 的 `_summarize_correlation` 取 `pairs[0]`，
   而 `app/tools/compare.py:76-85` 的 `pairs` 是按**列序**构造的、没有排序。
   sales.csv 上 `pairs[0]` = 销量/单价 `r=-0.0457`，真正 |r| 最大的是
   销售额/利润 **0.9872** —— 于是报告写「相关性最强的一对是 销量 与 单价，
   相关系数 -0.05」。
2. `build_report` 里 `llm_used` **硬编码 `False`**（理由是"D4 只有模板路径"）。
   真 key 规划成功后 `plan.source == "llm"`，而报告首段、接口 7 的 `llm_used`、
   前端横幅三处一致显示"未使用大模型"。

**为什么零覆盖本身就是问题**：QA 把实现改成正确写法时 **490 passed，一条都没红**。
也就是说 —— "报告里这句话是假的"这个 bug，在当时是**被整个测试集体放过去**的。

**决定**

1. **诚实性字段（`llm_used`、首段 NOTICE、"最强/最弱"这类排序词）按 `plan.source`
   等真值推导，不写死。** 理由：字段 docstring 已经把它定义为"这次到底有没有用模型"
   （`app/schemas_workflow.py:210`），写死会让它变成一句**永远成立但可以随时为假**的陈述。
2. **修好之后必须证明"改回去会红"**，否则等于没修。本次变异 3/3 有牙：

   | 变异 | 红线数 | 说明 |
   |---|---|---|
   | `max(|r|)` → `valid[0]` | 2 | correlation 挑最强那对 |
   | `_llm_used` → `return False` | 3 | D4 的硬编码写法 |
   | 首段恒 `TEMPLATE_NOTICE` | 2 | 报告首段二选一 |

   三个变异都要**先写测试再变异**（第一轮没测试时 3/3 全绿），
   还原后逐字符一致、复跑仍 505 passed / 0 failed。
3. **跨文件引用"另一个文件已经排好序"时，先 grep 确认。** 第 1 条的笔记
   （`app/agents/validation.py:201-202`）称"compare.py 已按 |r| 排好序" ——
   那句话让 review 的人也一起放过去了。**注释里的前提和代码里的前提一样会骗人**，
   要一起验。

**影响到**：`app/agents/validation.py`（`_summarize_correlation` / `_llm_used` /
`_summary` / `_minimal_report` / 两个 NOTICE 常量）、
`tests/test_validation.py`（新增 9 条，逐字写死常量、不从实现 import）。

**状态**：已落地（`aff1df6`）。505 passed / 4 skipped；
真 key 实测 `plan.source=llm` → `llm_used=True` + 首段为 `LLM_PLANNED_NOTICE`，
六地区零容差对账通过。

---

## D-23 "数字可追溯"这条规矩要连**我们自己写的模板**一起管住；等价变异要如实报

**背景**：D5 把"溯源"做成了真功能（`validate()` 检查报告中需溯源的数字是否能从工具结果或数据画像找到出处）。
它立刻抓出一个 **D4 模板自己的 bug**：`_summarize_missing` 里写
「另有 `len(with_nulls) - 3` 列也存在缺失」。这个差值是**展示层现算的**，
工具结果里没有任何一个键对应它 —— 也就是一个"追不到出处"的数字。

**它为什么在 D4 一直没被发现**：六任务探针里，`len(with_nulls)` 恰好等于 2，
那句 `f"另有 {len(with_nulls) - 3} 列"` 渲染成 `-1`，看着像"格式笔误"而不是"假数字"。
单独跑"检查数据集的缺失值"时 `unsupported_numbers=['2']`、`passed=False`：
**换个数据集就会把一份诚实的报告抓成编造。**（已改成"其余有缺失的列见下方表格"，
信息没丢 —— 表格里就是完整 9 行统计。）

**决定**

1. **"报告里出现的每个数字都能追到 `ToolResult.data`"这条规矩，对模板路径和模型路径
   同等适用。** 模型会编数字，模板的算术也会 —— 前者是刻意的攻击面，后者是无意的漏洞，
   而在用户眼里**两者都表现为"报告里有个数字，但我不知道它哪来的"**。
   所以溯源校验默认对两条路径都跑。
2. **"共 N 行结果"这类规模量走结构计数合法**：N = `len(rows)`，它描述的是结果集的形状，
   不是样本统计量。判据是"这个数能不能由调用方手里的对象长度得出" —— 能，就不算新数字。
3. **等价变异要如实报出来，并且给出机械证明。** D5 的 16 个变异里有 2 个是等价的
   （M13：`_CHART_TYPE_BY_TOOL.get(tool, "bar")` 的默认值；M14：规则路径恒传 `agg="mean"`）。
   写法是枚举 16 个输入，逐一对齐变异前后的行为，**得到 0 处差异**。
   硬凑一条红是自欺 —— 等价变异暴露的是**断言的能力边界**，不是测试的疏漏。
   实现者把 M13 换成真 bug 形态（"参数不全时编造列名"）后立刻被抓住。

**一句话**：**只测"系统会产生的输入"，测不出"系统不该产生的行为"。**
M13 那条断言（"不该出图的工具不配图"）在规则路径上永远无法被区分，
因为那个默认值 `"bar"` 在任何**可达**输入下都不会被用到。
要让它变红，必须喂一个规则路径产生不了的输入 —— 那时需要另建一条用例，
而不是改坏它旁边的某一行。

**影响到**：`app/agents/validation.py`（`_summarize_missing` 措辞、
`_check_numbers` 的正则与容差、六类 issue）、
`tests/test_validation_numbers.py`（33 条）、`.gitignore`（`data/charts/*`）。

**状态**：已落地（`d18fdbf` / `.gitignore` 补在 `3caab04`）。578 passed / 0 failed / 0 skipped；
Lead 独立复算（纯 stdlib csv 手算六地区 + 手工塞假数字 + AST 扫零 LLM + 派生列口径）全部通过。

---

## D-25 重试只留一层：`OpenAI(..., max_retries=0)`，SDK 的重试必须钉死

**背景**：`app/llm/client.py` 里有两层重试，而且**是叠乘的**：

- 外层是 `chat()` 里的 `for attempt in range(1, total_attempts + 1)`，
  `total_attempts = settings.llm_max_retries + 1`，退避 `time.sleep(1.5 * attempt)`；
- 内层是 `OpenAI(..., max_retries=settings.llm_max_retries)` 传给 SDK 的那个值，
  它会自己重试 `408 / 409 / 429 / ≥500 / 连接错误`，退避 `min(0.5 * 2^n, 8.0)` 秒，
  并且尊重服务端的 `Retry-After`（最长 60 秒）。

默认 `llm_max_retries = 2` 时，**单次 `chat()` 最坏 9 次 HTTP、约 9.2 分钟墙钟**；
一条工作流最多 5 次 `chat()`（planner 3 + 报告 2）→ 最坏 45 次请求、约 45 分钟。

这件事在 D4 就被写进了回报（"单次 chat() 最坏 9 次 HTTP"），当时的处置是
「属于契约变更，D4 不做，交 Lead 拍板」。**结果是它一直悬着**，而且
`docs/ARCHITECTURE.md` §5.3 的重试表从 D4 起就写着"重试 `settings.llm_max_retries`（默认 2）"
—— 文档描述的是**收敛后**的语义，代码当时是 8 次。**一句计划时态的话被读成了事实。**

**为什么必须修，而不是"反正只是慢"**：`max_workflow_seconds`（默认 180）只在
阶段与任务**之间**检查（`orchestrator._stop_reason`），**打断不了一次正在阻塞的 HTTP 调用**。
也就是说这套叠乘能让一条工作流的实际耗时达到自己预算的 **15 倍以上**，
而"超时保护"在这种情况下完全不起作用 —— 它不是性能问题，是**保护机制失效**。

**决定**

1. **`max_retries=0` 写死，不取自配置。** 重试只由外层循环负责，
   于是单次 `chat()` 最坏 `1 + llm_max_retries` 次 HTTP（默认 3 次）、退避 1.5s / 3.0s，约 3 分钟。
2. **`llm_max_retries` 的语义收窄成"总重试次数"**，不再需要乘上 SDK 那份看不见的重试。
   它的名字到这里才名副其实。
3. **必须是常量，不能是 `0 if ... else ...`**：配置项仍在，但它只影响外层循环。
   测试用两条用例钉住 —— `tests/test_llm_client.py` 里一条断言写死的 `0`，
   另一条把 `LLM_MAX_RETRIES` 设成 `5` 再断言 SDK 拿到的仍是 `0`。
   这样"好心把它改回配置值"会立刻红。
4. **本次改动是 Lead 直接做的，不是分派出去的。** 原因如实记下：
   这个任务（`task-30`）曾两次分派给同一个成员，两次都没有产生任何改动；
   它卡在收口的关键路径上，而它本身是一行常量加一处文档 —— 继续等它的机会成本
   高于"换手"的成本。**这是流程上的妥协，不是设计上的**。

**没做到的**：本次**没有**实测"真实 429/5xx 下 SDK 只发一次请求"。
现有证据是：`max_retries=0` 真的传进了 `OpenAI()`（用 `httpx.MockTransport`
截获构造参数断言），以及外层循环的行为有既有用例覆盖。
"SDK 拿到 0 之后确实不再重试"依赖 SDK 的既有语义，不是本项目实测的结论。

**影响到**：`app/llm/client.py`（构造参数一行 + 顶部两段"重试策略"文档整段重写）、
`tests/test_llm_client.py`（`test_timeout_and_max_retries_are_passed_to_the_sdk`
改名并改断言；新增一条配置为 5 时仍传 0 的用例）、`docs/ARCHITECTURE.md` §5.3。

**状态**：已落地（当时实测 799 passed / 0 failed / 0 skipped；之后 D6-SQLite 补做又加了用例，
当前全量为 **817 passed**（仓库根、`tests/` 两个入口一致；见 D-24）。

---

## D-24 SQLite 历史归档：拍板在 D6、**D6 没做**、D6 交付之后才补上；选型从 SQLAlchemy 改成标准库 `sqlite3`

**背景（这条的重点是时间线，不是技术）**

`docs/ARCHITECTURE.md` 第 14 节**决策 3** 早在 D6 之前就拍了板：**做 SQLite 历史归档、明确不做 SSE**。
配套的 §8.3（触发条件）、§8.3.1（读写路径约定）、§8.3.2（已知限制）也写得很完整。

**但是 D6 当天，SQLite 一行代码都没写。** D6 的精力去了前端联调与安全测试
（`web/` 三件套 + `tests/test_security.py`），SQLite 正是被"可砍顺序"砍掉的那一项
（`docs/ARCHITECTURE.md` §13 风险 9 事后复盘里已如实记下这一点）。
**于是出现了一个更隐蔽的问题：这批文字是"计划时态"，却一直在被当成"完成时态"读。**
文档里写着"选 SQLite"，读者（包括后来接手的人）会默认它已经有了。
这类错误**不会让任何一条测试变红** —— 它没有代码可失败，只有人会信。

**用户在本轮拍板"补做"**（代号 D6-SQLite），条件是：**标准库 `sqlite3`，零新依赖**。

**决定**

1. **做归档写入，不做状态恢复。** 工作流进入终态（`TERMINAL_STATUSES`）时，把一条记录
   UPSERT 进 SQLite。**内存态 `registry._workflows` 仍然是唯一的运行态事实来源**，
   `app/agents/orchestrator.py` 的原有执行路径**一行都没有改**。
   理由：真正被问到的问题是"重启后还能查到之前的工作流吗"，这个能答；
   而"重启后接着把没跑完的工作流跑完"需要把运行态外置，成本与风险都远高于收益。
2. **选型从 SQLAlchemy 改成标准库 `sqlite3`。**
   §14 决策 3 里写的"用**已装的** SQLAlchemy"——**那是假的：仓库从来没装过任何数据库包**
   （`requirements.txt` 里 12 个直接依赖，一个是数据库都没有）。
   本项目的真实需求只有两件事：一张表的 UPSERT（`archive()`）和一次分页 SELECT
   （`list_recent()`）。用 ORM 描述这两件事属于**负资产**：多一层抽象、多一个依赖，
   换来的是本项目不需要的迁移/多表/关系映射能力。
   所以用标准库，**`requirements.txt` 与 `requirements.lock.txt` 一个字都没加**。
3. **落地路径是 `data/db/app.db`，不是设计稿里的 `data/app.db`。**
   选址依据是**仓库已有的 ignore 规则**：`.gitignore` 早就挡了 `data/db/*`
   （原本是 `data/db/*.db`，后来发现 SQLite 写事务期间会产生 `app.db-journal`，
   只挡 `*.db` 不够，才放宽成 `data/db/*`）。选 `data/app.db` 反而要新加一条 ignore 规则。
   **结果：库文件天然不会被误提交，也不需要改 `.gitignore`。**
4. **归档失败不能影响分析结果。** `archive_if_terminal()` 的契约是**"绝不抛异常"**：
   磁盘满、目录只读、文件被占用，一律只记 warning 并返回 `False`。
   理由：用户要的是那份分析报告，归档是旁路；旁路坏了不能把主路带崩。
5. **归档可以被关掉。** `DB_PATH` 设成空串 = 显式关闭归档，`get_history_store()` 返回 `None`。
   这时 `GET /api/workflows/history` 返回 **HTTP 200 + `enabled=false` + 空列表**；
   历史详情因没有归档记录而返回 **404 `NOT_FOUND`**。这不是错误状态不一致：列表是在查询功能可用性，
   详情是在查询一条不存在的记录。

**这条决策真正的教训**：**"文档里写了"不等于"代码里有"。**
D-22 第 3 条早就说过"注释里的前提一样会骗人"，这一条是它在文档层面的同型问题：
计划时态的句子会在几轮之后自动升格成事实。这次的处置是在正文里**留下时间线**
（§8.3 与 §14 决策 3 都加了"D6 当天没做、之后补做"的如实记录），
而不是把文字改干净就算完 —— **把"补做"伪装成"当初就做完了"，等于又撒了一次谎。**

**影响到**：`app/data/history.py`（新增 `HistoryStore` / `HistoryRecord` / `HistoryStoreError` /
`get_history_store()` / `reset_history_store_cache()`）、`app/core/archive.py`（新增
`archive_if_terminal()` / `build_record()`）、`app/config.py`（第 16 个字段 `db_path` +
第 6 个计算属性 `db_file`）、`app/api/routes_workflows.py`（接口 9 / 10）、
`.gitignore`（`data/db/*.db` 放宽为 `data/db/*`）、
`tests/test_history_store.py` / `tests/test_history_archive.py` / `tests/test_history_api.py` /
`tests/test_history_acceptance.py` / `tests/test_history_robustness.py`；
文档侧同步 `docs/ARCHITECTURE.md`（§4.0.1 / §7 / §8.3 / §8.3.1 / §8.3.2 / §11 / §12 / §13 / §14 / 附录 B）、
`docs/WORKFLOW.md` §7、`README.md`、`docs/DEMO.md`、`docs/STAGE2_ACCEPTANCE.md`。

**状态**：已落地。task-34 时全量为 **800 passed**（仓库根入口与 `tests/` 入口一致）；当前 **817 passed**（含 task-40 护栏用例，两个入口一致）。
未做：运行态恢复、多进程共享、历史记录"继续运行"、鉴权 —— 这四条都**不在本次范围内**，
`docs/ARCHITECTURE.md` §8.3.2 与本条一并写明。

---


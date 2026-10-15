# 亮点与难点

> 这份文件写给两类人：想快速看懂"这个项目做对了什么"的读者，以及要决定"值不值得给面试机会"的面试官。
> **诚实前提**：这不是什么高深技术。它是一个 7 天、单人、本机跑的项目，
> 本文保留 2026-10-14 的历史快照：基线 `a703364`，当时 `594 passed`（实测 `python -m pytest -q`，约 17 秒；后续提交 `a677425` 只加演示脚本与 `docs/DEMO.md`，本文件引用的行号未受影响）。
> 当前代码与测试规模以 [README 测试说明](../README.md#测试) 为准；不要把本文件历史快照当作当前测试状态。
> 下面每一条都给出**可复现的证据**；不厉害的地方我单列在最后一节，不吹。
>
> 配套阅读：[docs/INTERVIEW_QA.md](INTERVIEW_QA.md)（12 个追问）、[docs/WORKFLOW.md](WORKFLOW.md)（完整链路）、
> [docs/DECISIONS.md](DECISIONS.md)（每个决定的理由与代价）。

---

## 一、亮点（5 条）

### 亮点 1：报告中的数字有机械可核对的来源（数字溯源校验）

**是什么**：报告里的表格**不是模型写的**，是从 `ToolResult.data["rows"]` 原样复制的
（`_copy_table`，`app/agents/validation.py:386`）；模型可写正文段落，但不能改表格、摘要或质量提示。
写完之后，`validate()`（`:551-604`）扫描摘要、正文和质量提示中的数字，
到 `ToolResult.data` 与 `DatasetProfile` 中逐个核对；查不到就记 `UNSUPPORTED_NUMBER`（error 级）。

**为什么算亮点**：LLM 报告常见风险是**写出没有工具依据的数字**。提示词不能提供保证；本项目让模型只写正文段落，并用机械校验检查正文、摘要和质量提示中的数字是否能从工具结果或数据画像追溯。表格、摘要和质量提示由代码生成，表格直接复制工具结果。校验层只读状态，不改报告、不重跑工具，也不决定工作流状态。

**证据**：`app/agents/validation.py:386,532,551-604,725-844`；6 种 issue code 被测试冻结
（`tests/test_contracts_d5.py:88-95,163`）；千分位与百分号口径见 `tests/test_validation_numbers.py:211,330`。
容差只覆盖"写法差异"，见 [INTERVIEW_QA 第 7 题](INTERVIEW_QA.md)。

### 亮点 2：诚实标注不是口号，是从真值推导出来的字段

**是什么**：目前有三种首段文案：`TEMPLATE_NOTICE`（没用模型）、`LLM_PLANNED_NOTICE`（模型参与规划、正文是模板）和 `LLM_WROTE_NOTICE`（模型撰写了正文）（`app/agents/validation.py:57,62,73`）。`llm_used` 仍只表示模型是否参与规划，由 `_llm_used` 根据 `state.plan.source` 推导（`:225-242`）；它不单独表示正文是否由模型写。

**为什么算亮点**：这条规矩是**QA 复验逼出来的**——D4 阶段 `llm_used` 曾经硬编码 `False`，
真 key 规划成功之后，报告首段、接口 7 的字段、前端横幅三处一起显示"未使用大模型"，
而当时**490 个测试一条都没红**（`docs/DECISIONS.md:1008-1013`）。
现在的保护是：首段文案按计划来源与正文来源分别选择，并由 `tests/test_validation.py` / `tests/test_report_llm.py` 覆盖其行为；不能把 `llm_used` 误读成“正文是否由模型撰写”。

**证据**：`app/agents/validation.py:57,62,136,326-337`；`tests/test_validation.py:286-293`；
`docs/DECISIONS.md` D-22（第 1017-1019 行）。

### 亮点 3：白名单 + 5 类校验短路（模型永远碰不到代码）

**是什么**：工具是**代码即白名单**——`_REGISTRY` 只有 `register()` 能写，
`get(name)` 取不到返回 `None`，**没有 `getattr(module, name)`、没有 `eval`**
（`app/tools/registry.py:6,56,62,69`）。模型只能通过 function calling 报"工具名 + 参数"，
参数依次过 4 类校验（`app/tools/validate.py:19-22,58`）：

```
TOOL_NOT_FOUND → BAD_PARAMS → COLUMN_NOT_FOUND → BAD_OPERATION   （这 4 类在 validate_call 里）
BAD_RESULT                                                        （第 5 类在执行后：NaN / 结构不符）
```

**为什么算亮点**：安全边界写在了"代码允许什么"上，而不是"模型今天想干什么"上。
参数类型只有 str/int/float/bool + 列名 + 操作符枚举，列名必须真的在 `df.columns` 里 ——
即使模型被注入成 `__import__('os').system(...)`，那也只是个过不了列名校验的字符串。

**证据**：`app/tools/registry.py:6,56,62,69`；`app/tools/validate.py:19-22`；
`app/agents/analysis.py:42-61`；注入面测试：`tests/test_tool_registry.py:64-66`
（`os.system`/`eval`/`open`/`__import__` 全部取不到）、`tests/test_tools.py:569-580`、
`tests/test_analysis_agent.py:176-185`。

### 亮点 4：从"模型没配"到"模型挂了"，提供规则与模板兜底

**是什么**：四层降级，每一层都有代码和文案证据：

| 情况 | 表现 |
|---|---|
| `llm_configured=false`（没填 key） | Planner 走 `plan_with_rules`，`source="rule_fallback"`（`app/agents/planner.py:160-161,225`） |
| 配置不全就调模型 | `chat()` 抛 `LLMNotConfigured`（`app/llm/client.py:65,111-112`），调用方转规则兜底 |
| 模型网络/5xx 抖动 | SDK `max_retries=0`，项目循环按 `llm_max_retries=2` 最多尝试 3 次（`app/llm/client.py:133-148`） |
| 报告生成失败 | `_minimal_report` 产出一份最小报告（`app/agents/validation.py:417`），状态降级不崩 |

**为什么算亮点**："模型不可用"不是分析失败的唯一原因；Planner 可走规则计划，报告生成可退回模板，
因此项目仍能在未配置模型时运行演示。外部模型调用仍可能耗时或失败；这里描述的是可观察的降级路径，不承诺任何环境下绝不失败。

**证据**：`app/agents/planner.py:9,160-161,225,274,287,318,329`；
`app/llm/client.py:72,114-148`；
`app/agents/validation.py:417`；`app/agents/orchestrator.py:295-324`。

### 亮点 5：运行时可写目录探测（`runtime_environment`）——先证明，再假设

**是什么**：启动时（`app/runtime.py:21-61`）真的**创建一个临时文件**来证明"这个目录现在可写"，
并且记录下探测到的 `TEMP` / `TMP` / `TMPDIR` 名字（`:43`）；
探测失败才把 `tempfile.tempdir` 指回项目内目录（`:51`），并恢复环境变量（`:53-61`）。

**为什么算亮点**：它来自一个**假绿教训**——PowerShell 的 `Set-Content` 能往 `%TEMP%` 写，
而 **Python 子进程写不进去**（`PermissionError [Errno 13]`，`docs/DECISIONS.md:178-183`）。
所以"我在 shell 里试过临时目录可用"这个结论是**错的**。这条探测把"环境假设"变成了运行时事实，
顺带解释了为什么 `MPLCONFIGDIR`（必须在 import 前设）走的是另一条路（`app/config.py:26-33`）。

**证据**：`app/runtime.py:12,21,40,43,51-61`；`app/config.py:26-33`；`docs/DECISIONS.md:172-183`（D-07）。

---

## 二、难点（5 条，每条：现象 → 根因 → 怎么定位 → 怎么修 → 怎么防止再犯）

### 难点 1：`matplotlib` 的中文字体与配置目录——错了**不报错，只画方框**

- **现象**：图里的中文标题全变成 `□□`；另一台机器上还多出一堆临时缓存目录，导入变慢。
- **根因**：① matplotlib 默认字体不含中文，缺字形时**不抛异常**，只画空框；
  ② `MPLCONFIGDIR` 是**在 import matplotlib 的那一刻**被读取的，晚一步设就没用了，
  而本机默认缓存目录 `C:\Users\jun\AppData\Local\matplotlib` 拒绝访问（WinError 5）。
- **怎么定位**：画一张带中文标题的图，肉眼比对；缓存问题看导入耗时与 `.mplcache` 是否生成。
- **怎么修**：`app/config.py:31-33` 在 import 时刻 `mkdir` + `setdefault("MPLCONFIGDIR", ...)`；
  `app/agents/charts.py:45` 把 `from app import config` **排在 matplotlib 之前**，
  `:85` 的 `_install_chinese_font()` 在**模块导入时**就执行（`:111`），
  设置 `font.sans-serif` 与 `axes.unicode_minus=False`（`:107-108`）。
- **怎么防止再犯**：把"顺序"写成注释里的硬约束（`charts.py:28-31` 明确写出三件事），
  并让测试真的渲染一张图（`tests/test_charts.py`）。
- **证据**：`app/config.py:26-33`；`app/agents/charts.py:28-31,45,85,107-111`。

### 难点 2：Python 子进程写不了 `%TEMP%`，`tempfile` 会**静默回退到当前目录**

- **现象**：仓库根目录反复冒出 `tmpmx_0akgk/`、`pytest-of-jun/` 这类目录，
  跑一次测试长一批；而 shell 里手测"临时目录可用"。
- **根因**：本机 Python 子进程对 `%TEMP%` 只有读权限（`PermissionError [Errno 13]`），
  而 `tempfile` 找不到可用目录时**不会报错**，会静默回退到 cwd。
  PowerShell 的 `Set-Content` 却能写 —— 这就是那个会骗人的不对称。
- **怎么定位**：`python -c "open(os.environ['TEMP']+'/x','w')"` 直接复现；
  再看 `tempfile.gettempdir()` 的实际返回值。
- **怎么修**：`app/runtime.py:21-61` 启动时**真的建一个临时文件**验证可写性，
  不可写就把 `tempfile.tempdir` 指向项目内目录（`.tmp/` 已被 `.gitignore` 排除）。
- **怎么防止再犯**：把 `%TEMP%` 下的路径与目录名写进文档与决策（`docs/DECISIONS.md` D-07），
  并承认残留边界：这方案的前提是"项目根可写"。
- **证据**：`app/runtime.py:21-61`；`docs/DECISIONS.md:172-183,251`。

### 难点 3：`core.autocrlf=true` 让干净 clone 的 `sales.csv` 变了字节，3 条测试红

- **现象**：本机全绿，**克隆到新目录跑就 `146 passed, 3 failed`**。
- **根因**：仓库原先没有 `.gitattributes`。提交时 git 把 CRLF 归一化成 LF（blob 是对的），
  但**检出**时又换回 CRLF：干净检出的 `data/samples/sales.csv` 是 **143197 字节 / 2169 个 CR / md5 `6d4f6dad…`**，
  而生成脚本写出的是 **141028 字节 / 纯 LF / md5 `910d6723…`**。
  三条**逐字节**断言（UTF-8+BOM+LF、重新生成与仓库那份一致）在干净检出上必红。
- **怎么定位**：对比"本机文件"与"`git archive` / 新克隆里的文件"的字节数与 CR 数——
  注意 `git diff` 看不出来，因为它在 `autocrlf=true` 下自己也会归一化。
- **怎么修**：`.gitattributes` 钉 `data/samples/*.csv text eol=lf`（`:60`），
  实测对比过 `-text`：`-text` 只是把问题挡住（谁提交 CRLF 就存 CRLF），
  `text eol=lf` 会在提交时归一化，**仓库会自愈**，所以选严格更强的那条（`.gitattributes:17-27`）。
- **怎么防止再犯**：把"可复现性承诺"写成测试（逐字节断言），并额外钉 `*.py text eol=lf`（`:61`）。
- **证据**：`.gitattributes:1-61`；`tests/test_sample_data.py`；`docs/DECISIONS.md` D-15（第 569 行起）。

### 难点 4：`TestClient` 会**等后台任务跑完**，所以"接口没阻塞"不能用 HTTP 耗时证明

- **现象**：想证明 `POST /api/analysis` 是"立刻返回、后台跑"，
  于是量响应耗时——但测出来永远是"等分析跑完"的时间。
- **根因**：Starlette 的 `TestClient` 在把响应交给调用方之前，会**先把 `BackgroundTasks` 执行完**。
  也就是说：用 `TestClient` 测不出"异步性"，耗时指标是假的。
- **怎么定位**：对比"响应耗时为分析总时长"与"真实 uvicorn + curl 下的毫秒级返回"。
- **怎么修**：把"受理"与"执行"拆开证明——接口层断言 **202 + `workflow_id` + 状态为 `PENDING`**
  （`tests/test_workflows_api.py`），后台行为用直接调 `run_workflow(state)` 的单测覆盖；
  真正的手工验证写进 [docs/WORKFLOW.md](WORKFLOW.md) 的"手工复现"一节（起服务 + curl）。
- **怎么防止再犯**：把这条写进决策记录，明确"哪些结论不能用 TestClient 证明"。
- **证据**：`docs/DECISIONS.md:985`（D-21 内）；`app/api/routes_analysis.py:49-54,96-98`；
  `app/core/background.py:40-50`。

### 难点 5："修好了但没测试钉住" = 没修（变异证明纪律）

- **现象**：QA 把两处错误实现改成正确写法，**`490 passed`，一条都没红**。
  两处错的都是"数字全对、话是假的"：
  ① `_summarize_correlation` 取 `pairs[0]`，而 `compare.py:76-85` 的 `pairs` 是按**列序**构造、没排序 ——
  报告于是写"相关性最强的一对是 销量 与 单价，-0.05"，而真正 |r| 最大的是 销售额/利润 **0.9872**；
  ② `llm_used` 硬编码 `False`。
- **根因**：测试只覆盖了"数字算得对"，没有覆盖"描述数字的话是否成立"；
  而且一条注释（`app/agents/validation.py:201-202`）声称"compare.py 已按 |r| 排好序"——
  **注释里的前提和代码里的前提一样会骗人**，review 的人也一起放过去了。
- **怎么定位**：QA 用"把正确实现改回错误实现，看有没有测试变红"来反向验证覆盖。
- **怎么修**：`docs/DECISIONS.md` D-22 把"改回去会红"定为纪律，并如实报出第一次 **3/3 全绿**；
  先补测试再变异，本次变异 **3/3 有牙**：

  | 变异 | 红线数 |
  |---|---|
  | `max(\|r\|)` → `valid[0]` | 2 |
  | `_llm_used` → `return False` | 3 |
  | 首段恒 `TEMPLATE_NOTICE` | 2 |

  修完复跑 **505 passed / 4 skipped**，真 key 实测 `plan.source=llm` → `llm_used=True` + `LLM_PLANNED_NOTICE`。
- **怎么防止再犯**：跨文件引用"另一个文件已经排好序"这类前提时，**先 grep 确认**（D-22 第 3 条）。
- **证据**：`docs/DECISIONS.md:998-1041`（D-22）；`app/agents/validation.py`；
  `tests/test_validation.py`（D-22 一次加了 9 条，逐字写死常量）。

---

## 三、踩过的坑里最值得记的 3 条

1. **"我在 shell 里验证过"不是验证**（难点 2）——
   PowerShell 写得进 `%TEMP%`、Python 子进程写不进，两者不是同一个权限上下文。
   凡是"环境能不能写 / 能不能连"的结论，必须用**未来真正执行它的那个运行时**去验证。
2. **可复现性承诺要被逐字节钉住，否则它只是句口号**（难点 3）——
   `core.autocrlf=true` + 没有 `.gitattributes` 的组合，让"任何人 clone 下来拿到同一个文件"这句话
   在干净检出上直接为假（143197 vs 141028 字节）。修的是行尾策略，不是放宽断言。
3. **"修好了"不等于"有测试钉住"**（难点 5）——
   正确实现改回错误实现而测试全绿，说明那段逻辑**根本没有被测试**。
   这条纪律顺带解释了为什么本项目大量使用"逐字断言 + 冻结集合 + AST 检查"这种看起来笨的测试。

---

## 四、诚实边界（这些不是亮点）

- **单进程**：运行态仍在进程内存里，`uvicorn --workers 4` 会让上传与轮询落到不同进程；
  重启后未完成的工作流和数据集不会恢复。已完成工作流的摘要与报告正文由 D6 之后补做的
  SQLite 历史归档保留，但它不提供运行态恢复，也不支持多进程共享 registry（详见
  `docs/ARCHITECTURE.md` §8.3、`docs/DECISIONS.md` D-24）。
- **数字校验的边界**：只保证"报告里写出来的数字有出处"，
  不保证"该写的数字都写了"；容差只覆盖格式差异，见 [INTERVIEW_QA 第 7 题](INTERVIEW_QA.md)。
- **没有队列/优先级/背压**：提交 100 个分析就是 100 个后台线程。
- **前端仍然是原生 DOM 操作**：没有框架、没有构建步骤、没有 CDN。
  但"没有前端测试"这句已经过时 —— `tests/test_frontend.py` 有 16 条用例，
  其中一半是**真 HTTP**（上传 415/413 的真实响应码、`/data/` 没被挂成静态资源、
  `/static/../app/main.py` 拿不到），另一半是**源码契约**（`STEP_ORDER` 必须与后端 `_STEPS` 逐项相同、
  三个前端文件里不得出现任何绝对网址、`app/web/` 只允许三个文件）。
  **它们钉的是"结构和契约"，不是"像素"** —— 版面好不好看仍然只能靠人看。
- **没有身份认证、没有多租户、没有限流**：它是本机单人工具，不是对外服务。
- **没有 Docker / CI**：`docs/ARCHITECTURE.md` §11 里写了触发条件（要交付/要部署时再加，约 15 行）。

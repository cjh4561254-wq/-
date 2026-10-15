# 面试问答准备

> 这份文件回答"这个项目经不经得起追问"。
> 每条格式固定：**问** / **答** / **追问** / **证据**。
> 证据里的行号是 2026-10-14 在 commit `a703364` 上 grep/read 出来的，命令可直接复现；
> 数字类断言全部实测（含一段可重跑的 ddof 探针），没有"大概是"。
> 配套阅读：[docs/ARCHITECTURE.md](ARCHITECTURE.md)（设计）、[docs/DECISIONS.md](DECISIONS.md)（每个决定的理由与代价）、
> [docs/WORKFLOW.md](WORKFLOW.md)（一次提问的完整链路）、[docs/HIGHLIGHTS.md](HIGHLIGHTS.md)（亮点与踩过的坑）。

---

## 0. 先用 30 秒讲清这个项目

用户上传 CSV / Excel → 用一句自然语言提问（"哪个地区销售额最高"）→ 主控智能体把问题拆成
最多 8 个任务 → 每个任务交给一个**白名单里的 Python 工具**去算（pandas 真算，模型不算数）→
出图 → 生成报告 → 最后一步检查摘要、正文与质量提示中的数字能否追溯到工具结果或数据画像。

三条线要记住：**模型只负责"拆问题"和"写正文"，数字全部来自 pandas**；**报告里的每个数字都能溯源**；
**模型不可用时有规则计划与模板报告兜底路径**；这不代表任意运行时故障下都保证产出报告。

---

## 1. 为什么不用 LangGraph / Celery / Redis / Docker？

**答**：因为每条都能给出"什么时候才需要"的触发条件，而现在是 7 天、单用户、本机跑。

- **LangGraph**：它解决的是"有真分支、要回溯、要人工审批、要断点续跑"的图。我这个流程是
  **固定 6 阶段 + 最多 8 个任务的循环**（`profiling → planning → execution → charting → reporting → validation`），
  一个后台线程里的顺序 `for`/`if` 就能写清，读代码的人不用先学一个框架。
  触发条件：真的出现"某阶段失败要回退到上一阶段重规划"或"要人工审批中间结果"。
- **Celery**：需要 broker（RabbitMQ/Redis）+ 独立的 worker 进程 + 序列化边界。
  我的任务最慢 180 秒上限（`max_workflow_seconds=180`），而且只有一个用户，
  `BackgroundTasks` 就够。触发条件：要并发多用户、要任务持久化排队、要跨机器。
- **Redis**：进程内 `dict` 是最快的运行态存档（`app/core/registry.py:37`），跨进程反而要序列化。
  终态历史现会用标准库 `sqlite3` 归档到 `data/db/app.db`，重启后可通过只读历史列表/详情查询；
  但运行中的工作流与数据集仍在进程内存中，重启后不会恢复，SQLite 也不让多进程共享 registry。
  这是 D6 之后补做的 D6-SQLite（D-24），不是 D6 当天完成的。
  触发条件：多进程（`--workers`）或多机部署。
- **Docker**：Windows 上要 WSL2，装环境的时间会挤掉测代码的时间；交付时再加也来得及
  （一个 15 行的 Dockerfile）。触发条件：面试官要"一条命令跑起来"或要部署到服务器。

**追问**：
- *"那你这不是没用到多智能体？"* —— 有 5 个职责分离的 Agent（Orchestrator / Planner / Analysis / Chart / Validation），
  边界写在 `docs/ARCHITECTURE.md` §3；"多智能体"在我这里的定义是**职责与权限分离**，不是"必须上框架"。
- *"框架能省多少代码？"* —— 编排 + 状态机约 400 行（`app/agents/orchestrator.py` 431 行），
  引入框架省不掉这些，只会把"看得见的 400 行"换成"看不见的框架行为"。
- *"为什么不用 FastAPI 的依赖注入做状态管理？"* —— 状态必须是**进程级单例**且被后台线程改，
  请求级的 DI 生命周期对不上（见第 4 题）。

**证据**：`docs/ARCHITECTURE.md` §11「不引入清单」表（每个技术一行"为什么现在不引入 + 触发条件"）；
`app/core/background.py:40-50`（`BackgroundTasks` 只有 10 行）；`app/core/registry.py:37,40`（dict + Lock）；
`requirements.lock.txt` 只有 12 个直接依赖。

---

## 2. 为什么 Orchestrator 是唯一的状态所有者？子智能体为什么不许互调？

**答**：因为一个可变对象被两个执行体同时改，是这类项目最容易出的"幽灵 bug"——
现象是"状态偶尔卡在 executing"，日志里却看不出谁改的。

规矩定成三条，并且都能被机器检查：

1. **只有一个对象**：`WorkflowState`（`app/schemas_workflow.py:215`），存在进程内的 `registry` 里。
2. **只有 Orchestrator 能改 `status` / `step` / `retry_count`**：`run_workflow(state)` 原地改，不返回东西
   （`app/agents/orchestrator.py:57`）；子 Agent（Analysis / Chart / Validation）**只返回新对象**
   （`ToolResult` / `ChartResult` / `ValidationResult`），不碰 state 的流程字段。
3. **子 Agent 之间不互相调用**：Analysis 只调 `app/tools/`；Validation 只读 state 和结果，连模型都不调。

**追问**：
- *"靠自觉吗？"* —— 不靠。用 AST 检查钉住：`tests/test_contracts_d4.py:75-97`
  断言 `run_workflow` 所在模块的 import 根集合里**没有 fastapi/starlette**（否则有人 `from fastapi import HTTPException`
  就把 Web 层耦进编排层了）；`tests/test_contracts_d5.py:226-238` 断言校验模块里不出现
  `client.chat` / `from app.llm`，即"校验永远不问模型"。
- *"那 `status` 什么时候算写完？"* —— 约定是**先写结果字段、最后写 `status`**，`status` 是唯一的完成信号；
  前端只按 `status` 判断（`app/web/app.js:1140-1157` 进终态才去取报告）。
- *"`_run_safely` 又写一次 `finished_at`，不算破坏所有权？"* —— 那是**故意的重复**：
  后台线程被强杀时也得有个收尾。正常路径上它是幂等的（`app/agents/orchestrator.py:60-63` 有注释说明）。

**证据**：`app/agents/orchestrator.py:57`（`run_workflow` 只改不返回）；
`tests/test_contracts_d4.py:75-97`；`tests/test_contracts_d5.py:226-238`；
`docs/ARCHITECTURE.md` §3（5 个 Agent 的职责与"不能做"）、§5（状态机）。

---

## 3. 为什么绝不 `eval`/`exec` 模型生成的代码？白名单怎么防注入？

**答**：一旦执行模型生成的代码，这个系统的安全边界就等于"模型今天有没有被注入"，
而不是"我的代码允许什么"。所以设计上**模型只能报工具名和参数**，代码是死的：

- 工具注册表是**代码即白名单**：`_REGISTRY` 只有 `register()` 能写（`app/tools/registry.py:56,62`），
  `get(name)` 取不到就返回 `None`（`:69-72`），**没有 `getattr(module, name)`、没有 `eval`**。
- 模型通过 OpenAI function calling 报 `{"name": ..., "arguments": {...}}`，名字不在白名单就是 `TOOL_NOT_FOUND`，
  参数要过 5 类校验的前 4 类（`validate_call`，`app/agents/analysis.py:61`）。
- 参数本身没有"表达式"这个类型：只有 str/int/float/bool + 列名，列名必须真的在 `df.columns` 里，
  聚合操作符必须 ∈ 白名单（`sum/mean/count/min/max/median`）。
  所以即使模型被诱导写 `__import__('os').system('rm -rf /')`，它也只是个**字符串参数**，过不了列名/操作符校验。

**追问**：
- *"`getattr` 你项目里也有啊？"* —— 有，但只用在 SDK 响应的可选字段上（`app/llm/client.py:198,207,245`），
  是"兼容 SDK 版本差异"的读取，不是"按名字取函数来执行"。
- *"怎么证明模型报的恶意工具名真的取不到东西？"* —— 参数化测试：
  `tests/test_tool_registry.py:64-66` 用 `os.system` / `eval` / `open` / `__import__` / `""` 去取，全部是 `None`；
  `tests/test_tools.py:569-580` 断言调用后是 `TOOL_NOT_FOUND` 且**什么都没执行**；
  `tests/test_analysis_agent.py:176-185` 断言 `os.system` 走到 Agent 层也是 `TOOL_NOT_FOUND`。
- *"还有别的注入面吗？"* —— 上传路径（第 10 题）、报告里的数字（第 6 题）、
  以及"模型写的正文"——它只影响文字，不影响数字与表格（表格是从工具结果原样复制的）。

**证据**：`app/tools/registry.py:6`（注释里写明不用 `getattr`）、`:56,62,69`;
`app/agents/analysis.py:42-61`；`tests/test_tool_registry.py:64-66`；
`tests/test_tools.py:569-580`；`tests/test_analysis_agent.py:176-185`；`docs/ARCHITECTURE.md` §10 安全清单。

---

## 4. 为什么返回 202 + 轮询，而不是长连接 / WebSocket？这个方案有什么缺陷？

**答**：因为分析要几十秒，而 HTTP 请求不该挂那么久。`POST /api/analysis` 只做三件事
（校验数据集存在 → 登记 `WorkflowState` → 排后台任务），然后立刻返回 `202 Accepted` + `workflow_id`
（`app/api/routes_analysis.py:49-54,94-98`）。前端每秒轮询一次状态（`app/web/app.js:52`），
`status` 进终态且 `has_report=true` 时才去取报告。

选轮询不选 SSE/WebSocket：轮询的实现是"一个定时器 + 一次 GET"，20 行、可断点重试、不用学新协议；
SSE 的心跳/重连/代理缓冲问题在 7 天里是负收益。

**这个方案有三个已知缺陷，我都写在文档里，不装看不见**：

1. **单进程**：状态在进程内存里（`app/core/registry.py:37`），
   一旦用 `uvicorn --workers 4`，"上传的进程"和"轮询的进程"不是同一个 → 用户会看到 404。
   所以启动命令里明确禁止 `--workers`。
2. **运行态重启即丢，但终态历史有 SQLite 归档**：进程重启后老 `workflow_id` / `dataset_id` 不会恢复为可继续轮询的运行态；已完成工作流的摘要与报告正文可从历史归档接口读取。SQLite 历史归档是后来补做的 D6 收尾（D-24），不是运行态恢复或多进程 registry。
3. **没有队列**：提交 100 个分析就是 100 个后台线程，没有优先级、没有背压。触发条件是"真的有人用"。

**追问**：
- *"为什么不用 `run_in_executor` 自己管线程池？"* —— `BackgroundTasks` 在响应发出去之后执行，
  语义正好是"受理了、稍后跑"；自己管线程池还要处理关停与异常，收益不明显。
- *"前端怎么知道该取报告了？"* —— `GET /api/workflows/{id}` 返回 `has_report`，
  它是**现算**的 `state.report is not None`（`app/schemas_workflow.py:371`），
  不是抄来的陈旧布尔值——这样不可能出现"状态说 true、取报告给 400"。
- *"报告为什么不塞进状态接口？"* —— 状态接口每秒被调一次，报告可能有几十 KB，
  所以拆成接口 7；实测 `WorkflowStatusData.from_state(state)` 的 JSON 是 **1318 B**，
  而整个 `WorkflowState.model_dump_json()` 是 **14431 B**（约 11 倍），
  差的就是 `plan` / `results` / `report` 这些内部字段。

**证据**：`app/api/routes_analysis.py:49-54,70-98`；`app/core/background.py:40-50`；
`app/core/registry.py:37`；`app/web/app.js:52,1077-1082,1140-1157`；
`docs/ARCHITECTURE.md` §8（后台执行）、§8.2.3（单进程）、§8.3.2（已知限制）。

---

## 5. 重试上限为什么这么定？会不会叠乘？

**答**：重试分属模型 HTTP 调用、单个分析任务和整条工作流三个层面；但不能笼统说它们“互不相乘”。一次 `chat()` 内只由项目自己的循环重试：`OpenAI` 被显式设置为 `max_retries=0`，所以 `llm_max_retries=2` 时最多 3 次 HTTP 尝试。工作流可能发起多次 `chat()`，因此总请求数还取决于模型调用次数。

| 层 | 上限 | 管什么 | 在哪 |
|---|---|---|---|
| 模型 HTTP 层 | SDK `max_retries=0`；项目循环由 `llm_max_retries=2` 控制（最多 3 次尝试） | 一次 `chat()` 调用内的连接错误 / 超时 / 429 / 500 / 503 | `app/config.py` 的重试配置、`app/llm/client.py` 的 `max_retries=0` 与外层循环 |
| 单个任务 | `_MAX_TASK_RETRIES=1`（同任务再跑 1 次） | 工具返回 `BAD_RESULT`（NaN、结构不符） | `app/agents/orchestrator.py:54,222-226` |
| 整条工作流 | `_MAX_WORKFLOW_RETRIES=6`（与 `retry_count` 的 `le=6` 对齐） | 所有任务重试次数的**总账** | `app/agents/orchestrator.py:50`、`app/schemas_workflow.py:257` |

为什么是这些数：单任务 1 次是因为"重试要换策略，而换策略就是失败就跳过、别拖垮其他任务"；
整条 6 次是因为 `TaskPlan` 最多 8 个任务，全部失败也只需要 8 次机会的一半略多，
配合"每次重试都要过 `retry_count < 6` 这道闸"就能保证有限；模型 2 次是经验值，再往上只是让用户多等。

**边界要分开看**：工具任务重试受 `_MAX_TASK_RETRIES`、`tool_call_limit` 与工作流重试账本约束；模型 HTTP 每次调用最多 3 次尝试。`max_workflow_seconds=180` 在阶段/任务之间检查，不能中断一条已经阻塞的 HTTP 请求，因此它不是单次模型调用的硬墙钟上限。整条工作流的模型总请求数随实际 planner / report 调用次数变化，不应用工具调用上限直接推算。

**追问**：
- *"重试耗尽了用户看到什么？"* —— 单任务：该任务 `FAILED`/`SKIPPED`（依赖它的任务跳过），
  工作流降级成 `SUCCESS_WITH_WARNINGS`，报告里保留"哪几项没跑出来"；整条：`FAILED` + `error.code`。
- *"为什么工具调用超限走 `INTERNAL_ERROR` 而不是 `WORKFLOW_TIMEOUT`？"* —— 因为没有"超时"这回事，
  是我们没兜住的失控；用超时会误导成"只是慢"（`app/agents/orchestrator.py:344-345` 有注释）。
- *"`attempt` 最多两次，那第一次的结果呢？"* —— 都留在 `state.results` 里（审计用），
  报告取的是同一 `task_id` 的最后一次（`app/agents/orchestrator.py:286-292`）。

**证据**：`app/config.py` 的重试/超时/工具调用限制；`app/llm/client.py` 的显式 SDK `max_retries=0` 与唯一 HTTP 重试循环；`app/agents/orchestrator.py` 的任务及工作流重试边界；`app/schemas_workflow.py` 的 `retry_count` 上限。

---

## 6. "报告里的数字如何做来源核对？"这条规矩怎么落地的？

**答**：模型只写 `sections[].text`；表格、摘要和质量提示由代码生成，校验器再对报告可见文本做数字溯源。

1. 报告里的所有**表格**都从 `ToolResult.data["rows"]` 原样复制（`_copy_table`），模型不能改表格数字；`summary` 与 `data_quality_notes` 也由模板生成。
2. `validate(state)` 扫描 `summary`、各 `sections[].text` 和 `data_quality_notes`；`_allowed_numbers(state)` 从成功的 `ToolResult.data` 与 `DatasetProfile` 收集出处，找不到来源就记 `UNSUPPORTED_NUMBER`（`severity="error"`）。
3. 校验**只读 state、绝不调模型**（`app/agents/validation.py:554`、`tests/test_contracts_d5.py:226-238`），
   而且**自己绝不抛异常**：万一校验代码崩了，返回一份 `passed=False` 的最小结果并写日志，
   不能让"最后一道防线自己塌了还毁掉已经算好的结果"（`app/agents/validation.py:559-586`）。

**追问**：
- *"怎么保证没有漏检？"* —— 保证不了"漏检"，只能保证"查到的都是真的"：
  容差规则见第 7 题；我没有做"报告必须写全所有数字"的检查。
- *"`checked_numbers` 是什么？"* —— 抽到并尝试回查的数字个数，报告里会展示
  "核对了 N 个数字、M 个没找到出处"，这是给用户看的可信度指标。
- *"6 个 code 会不会随手加？"* —— 加不了：`tests/test_contracts_d5.py:88-95` 把 6 个 code
  写成集合冻结住，`:163` 断言实际集合完全相等。

**证据**：`app/agents/validation.py:386,532,551-604,725-844`；
`tests/test_contracts_d5.py:88-95,163`；`docs/ARCHITECTURE.md` §3.5（校验 Agent 的三条"不能做"）。

---

## 7. `_same_number` 的容差为什么要留？又为什么收窄？

**答**：留容差是因为**同一个事实有几种合法写法**，不留就会把正确报告判成"编造数字"：

- 千分位与两位小数：报告写 `24,871,657.00`，工具结果里是 `24871657.0`；
- 百分比与小数：报告写 `1.1%`，工具结果里是 `0.0111`；
- 正负号：工具返回 `-3.2`，报告写的是"下降了 3.2"（幅度）。

所以 `_same_number`（用函数名定位，行号随变更可能漂移）先认绝对值相等（含符号互换），
再让一般数值比较通过**相对差 0.1% 的量级门槛**后才四舍五入到 1/2 位；百分比与小数的 ×100 / ÷100
换算单独比较，允许报告展示精度造成的差异。**不放宽成任意相对误差**（比如"差 1% 就算同一个数"）：
否则 `24,871,657` 与 `24,871,000` 也可能被判成同一个数，"溯源"就名存实亡——
容差只覆盖**写法与展示精度**，不能洗白明显数值差异。

**追问**：
- *"那 `0.99` 和 `99%` 呢？"* —— 走 ×100 那条规则，认。
- *"千分位怎么读成数字的？"* —— `_to_float` 先 `replace(",", "")` 再 `float()`
  （`app/agents/validation.py:812-817`），所以 `24,871,657.00` 能进比较。
- *"有边界用例吗？"* —— `tests/test_validation_numbers.py:211`（千分位字面量原样在 data 里）、
  `:330`（记 float 会让日志里的人对不上原文，所以报告里保留千分位文本）。
- *"会不会把 1 和 1.0 判成不同？"* —— 不会，`round` 那条规则覆盖。

**证据**：`app/agents/validation.py:812-844`；`tests/test_validation_numbers.py:211,330`；
`tests/test_charts.py:82,202`（图表与报告同源的断言用同一批数字，`24871657.0`）。

---

## 8. 校验发现问题，为什么只降级成 `SUCCESS_WITH_WARNINGS`、绝不 `FAILED`？

**答**：因为**结果其实已经拿到了**。校验查出"报告里有个数字没有出处"，说明报告有硬伤，
但 `state.results` 是完整的、表格是工具算的，用户翻一眼就能自己判断——
这时候判 `FAILED` 等于把已经算好的东西一起扔掉，代价远大于收益。

所以 `_final_status`（`app/agents/orchestrator.py:295-324`）的**四条分支全部只返回
`SUCCESS_WITH_WARNINGS`，没有任何一条通往 `FAILED`**：① 有任务失败；② 有任务被跳过；
③ 报告没出来；④ 校验查出 error 级问题。判定口径只留一处：只读 `ValidationResult.passed`
（`ValidationResult.passed` 的定义是"没有 error 级 issue"，warning 不影响），
避免"校验说通过、状态说没通过"两头对不上。

那什么时候才 `FAILED`？只有两种"整条流程没法继续"的情况：**超时**（`WORKFLOW_TIMEOUT`）和
**工具调用失控**（`INTERNAL_ERROR`），都在 `_stop_reason`（`:327-354`）。

**追问**：
- *"那 error 级 issue 有什么后果？"* —— 状态降级 + 报告里带 `validation` 段（`issues` 列表），
  前端在报告页显示"未通过溯源核对"的提示。
- *"报告没出来为什么不是 FAILED？"* —— 因为模型不可达时我们还有**模板报告**兜底，
  真出不来会在 `state.error` 里写明原因，用户看到的是"降级 + 说明"，不是"白等一场"。

**证据**：`app/agents/orchestrator.py:295-354`；`app/agents/validation.py:589-604`；
`docs/ARCHITECTURE.md` §5.4（状态机与"降级 ≠ 失败"）。

---

## 9. 模板报告和模型报告的"诚实标注"为什么必须按真值推导？

**答**：因为最容易骗人的不是模型，是我们自己的兜底逻辑。
如果"没配模型"的报告写成"本次未使用大模型"、而"模型参与了规划、但正文还是模板"的报告
也写同一句话，那用户就无法区分这两种情况了。

做法是：报告里有一个布尔字段 `llm_used` 和两个常量文案——
`TEMPLATE_NOTICE`（`app/agents/validation.py:57`）、
`LLM_PLANNED_NOTICE`（`:62`）；二者**互斥且逐字断言**。
`llm_used` 不是传进来的参数，而是**从 `state.plan.source` 推导**（`_llm_used`，`:136`），
`source` 只会是 `llm` / `rule_fallback` 两种，规则兜底就是 `False`。

**追问**：
- *"文档里说'三个 NOTICE 常量'？"* —— 那是任务书的口径，**代码里只有两个**常量 + 一个 `llm_used` 布尔。
  我按代码写文档（`:57`、`:62` 实测），这类"任务描述与代码不一致"的地方以代码为准。
- *"怎么防止有人把两个常量抄成一样？"* —— `tests/test_validation.py:286-293`
  断言两个模块级常量**逐字等于**期望值、且互不相同（注释里写明"抄成别名会让 `llm_used=True` 的报告一边说自己用了模型、
  一边写着没用"）。
- *"那 `llm_used=True` 的报告会不会其实是模板正文？"* —— 会有这种中间态（模型只参与了规划），
  所以首段用 `LLM_PLANNED_NOTICE` 单独一句话说明，而不是复用"没用模型"那句。

**证据**：`app/agents/validation.py:57,62,136,326-337,417-423`；
`tests/test_validation.py:286-293`；`tests/test_orchestrator.py:188-199`；
`docs/DECISIONS.md` D-22（第 1017 行：诚实性字段按 `plan.source` 推导）。

---

## 10. 上传接口的七个步骤，各自防的是什么？

**答**：顺序不能换，每一步对应一个具体的失败场景（`app/api/routes_datasets.py:113-186`）：

| 步 | 做什么 | 防什么 |
|---|---|---|
| ① | 扩展名白名单（`.csv` / `.xlsx`） | **在任何写盘动作之前**拒绝：否则能用 `evil.csv` 把任意内容先落进上传目录 |
| ② | `dataset_id = uuid4().hex`（32 位无连字符） | 用户文件名一个字符都不参与路径；形态在 `store` 里还有一道会炸的检查 |
| ③ | 分块读 + 累计大小，超限立刻 413 | 不落盘、不把大文件读进内存（`_read_with_limit`） |
| ④ | 算 sha256（用户上传的原始字节） | 将来判断"是不是同一个文件"；重新序列化会改变字节 |
| ⑤ | 落盘为 `{dataset_id}.{ext}` + `resolve()` 复核父目录 | 路径穿越；且**不 mkdir**——目录问题应该在上传时就暴露 |
| ⑥ | 真解析一遍，失败就删掉刚写的文件 | 磁盘堆积垃圾文件（用户重传还会再写一份） |
| ⑦ | 解析成功后才登记 | "有元信息、却没文件"的假记录 |

**追问**：
- *"为什么不信 `Content-Type`？"* —— 客户端可伪造；**扩展名白名单 + 真能解析成功**才是证据。
- *"`.xls` 呢？"* —— 拒绝，并给出可执行的话（"请在 Excel 里另存为 .xlsx"），而不是一句"不支持"。
- *"行数 / 列数谁管？"* —— 尺寸之外还有两道闸：`max_rows`（默认 20 万）在 `pd.read_csv(nrows=...)` 时判、
  `max_columns`（默认 200）拿到 DataFrame 后判，超限是 `400 BAD_REQUEST`（不是 413——413 的语义是"请求体太大"，
  而这里限的是**解析后的形状**，200 列 × 20 万行的 CSV 可能只有 3 MB）。

**证据**：`app/api/routes_datasets.py:113-186`（①–⑦ 的注释就在代码里）；
`app/data/store.py:63`（`build_stored_filename`）；`README.md` 的上传错误表；
`docs/ARCHITECTURE.md` §10 第 2b 条。

---

## 11. pandas 的 `ddof` 陷阱是什么？

**答**：同一个标准差异，**pandas 和 numpy 的默认口径不同**：
`Series.std()` 默认 `ddof=1`（样本标准差），`ndarray.std()` 默认 `ddof=0`（总体标准差）。
在项目的示例数据 `data/samples/sales.csv` 的 `销售额` 列（2150 个非空值）上实测：

```
pandas  Series.std()       = 373186.8466036403   # ddof=1
numpy   arr.std(ddof=0)    = 373100.04887096176  # ddof=0
两者比值 1.000232639295924 = sqrt(n/(n-1))，n = 2150
```

差 86.8（约 0.023%）。数字看起来"差不多"，但两个数字**同时出现在报告里**时，
用户会问"到底哪个是对的"，而校验层的数字溯源也会因为"两个值都在允许集里"而放过去——
所以口径必须在代码里写死：`describe` 工具显式 `values.std(ddof=1)`（`app/tools/describe.py:42`），
`profiler` 用 pandas 默认（等于 ddof=1，`app/data/profiler.py:139` 有注释），
画像模型的字段描述里写明"（ddof=1，样本标准差）"（`app/schemas_dataset.py:106`）。

**追问**：
- *"`df.describe()` 是哪个口径？"* —— ddof=1，和上面选的一致（实测同为 `373186.8466036403`）。
- *"怎么复现？"* —— `.tmp/probe_ddof.py`（16 行）读 `data/samples/sales.csv` 打印四个值，
  命令：`& ".\.venv\Scripts\python.exe" ".\.tmp\probe_ddof.py"`。
- *"为什么不用 numpy？"* —— 项目里所有统计都走 pandas（工具层只 import pandas/numpy），
  用 numpy 的唯一风险就是这种默认参数差异，所以不混用。

**证据**：`.tmp/probe_ddof.py`（本轮新增，可重跑）；`app/tools/describe.py:42`；
`app/data/profiler.py:139`；`app/schemas_dataset.py:106`。

---

## 12. 为什么 `DataFrame` 绝不进 Pydantic 模型？

**答**：三个理由，任何一个都足够：**可变**（模型是"数据快照"的语义，DataFrame 会被就地改）、
**大**（几十万行逐格校验会慢到不可接受）、**序列化不了**（FastAPI 返回时会
`Object of type DataFrame is not JSON serializable`——这错在开发期很容易漏，一上线就 500）。

所以边界划成：**模型只传 `dataset_id`，DataFrame 存在 `app/data/store.py` 的进程内单例里**（`:113,244`），
路由层用 `store.get_frame(dataset_id)` 取；工具的输出必须过 `to_jsonable` / `ensure_jsonable`
（`app/tools/jsonable.py:55,83`）清洗成纯 Python 类型，才允许放进 `ToolResult.data`。

**追问**：
- *"内存怎么办？"* —— 单进程、重启即丢（文档里写明"已知限制"），
  外加 `max_rows` / `max_columns` 两道闸，超限的文件根本进不来。
- *"为什么不把 DataFrame 序列化成 parquet 存盘？"* —— 7 天里没必要：进程内 dict 是最快的方案，
  真要持久化，第一步是 SQLite 存历史记录，而不是存 DataFrame 副本。
- *"`profile` 为什么进模型？"* —— 画像是**计算结果**（列名、dtype、缺失数、五数概括），
  是纯标量结构，正好适合模型承载；它跟"把 DataFrame 塞进模型"是两件事。

**证据**：`app/data/store.py:113,244`（`DatasetStore` / 模块级 `store`）、`:179-212`（get / get_frame / exists）；
`app/tools/jsonable.py:55,83`；`docs/ARCHITECTURE.md` §4.0.2（DataFrame 不进 Pydantic 的理由）。

---

## 附：三个"答不上来就露馅"的细节

1. **`WORKFLOW_TIMEOUT` 的 HTTP 状态码是 200**，不是 5xx：因为"分析超时"是我自己判定的业务终态，
   响应外壳 `ok=false` + `error.code=WORKFLOW_TIMEOUT`；把超时做成 5xx 会让前端把它当成"服务器崩了"去重试。
   （`app/errors.py` 的 `ERROR_STATUS`，见 `docs/ARCHITECTURE.md` §7.0 表。）
2. **`has_report` 是现算的**，不是抄 `state.has_report`（`app/schemas_workflow.py:371`）。
3. **`status` 是唯一的完成信号**，前端只在终态取一次报告（`app/web/app.js:1140-1157`），
   所以不可能出现"报告取到了但还在跑"的中间态。

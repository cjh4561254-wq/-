# 05 · 态势带取数设计（战略态势带 = 3 个业务问题 × 一句结论 × 证据数）

> 归属：`docs/refactor/05-posture-data-flow.md`。方案 01 重构 Wave A 产出物。
> **本文只定"取哪些字段、按什么规则排、取不到时说什么"，不写实现。** 实现由 Wave B 在 `app/web/app.js` 落地。
> 上游约束：`docs/design-options/option-01.md` 第 5/7/10/11 节、`docs/design-options/00-brief-and-matrix.md` 第 2.1/2.2/2.3 节。
> **Wave D 状态（2026-10-17）**：本文的口径**已被采纳为实现事实**（D-4 / D-5 / D-6 / D-7 全部照此落地），
> 四列取法的**唯一规定**现已收进 `docs/COMMAND_CENTER_MIGRATION.md` §3——改口径只改那一处，本文继续作为论证与取证记录。
> 本文里的 30 处 `app.js:NNN` 与 `routes_*.py:NNN` 行号**都**是写作时点快照：`check_docs.py` 只核对
> `docs/HISTORY_ROBUSTNESS.md` 里声明过的锚点，**本文的任何行号都不在守卫范围内**，定位请用函数名/符号名。
> 全部结论均已对着当前代码核对；每处判断后面都标了取证行号，方便 Wave B 复核而不是相信本文。

---

## 0. 口径速查（先看这张表，后面全是论证）

| 态势带上的东西 | 唯一口径 | 来源 | 取不到时 |
| --- | --- | --- | --- |
| 业务问题（第 1 列） | `history.items[].question` **原样**，不截不断句不改写 | `app/api/routes_analysis.py:90` → `WorkflowState.question` → `app/core/archive.py:62` → `routes_workflows.py:217` | 只有全空白 → 显示"（这条记录没有留下问题文本）" |
| 一句结论（第 2 列） | `report.sections[0].text` **原样**，CSS 两行截断 | `app/schemas_workflow.py:186-196`；模板路径 `validation.py:269-283`；模型路径 `report_llm.py:323-357` | `sections` 非数组/首段 text 空 → "这条记录没有可显示的结论正文" |
| 小标题（结论上方一行灰字） | `report.sections[0].heading` | 同上（模板路径它等于任务描述） | 空 → 不渲染这一行 |
| 证据数（第 3 列） | `new Set(report.citations).size`，即**成功产出的取数任务数** | `validation.py:185-191` / `report_llm.py:370-372` | 字段不是数组 → 显示"—（未提供引用信息）"；**是空数组 → 显示 0** |
| 正文来源标注 | `report.summary` 的**第一个段落**与三个常量逐字比对 | `app/agents/validation.py:63/68/79`（`TEMPLATE_NOTICE` / `LLM_PLANNED_NOTICE` / `LLM_WROTE_NOTICE`） | 认不出来 → "来源说明未识别"，绝不回落到 `llm_used` 去猜"大模型撰写" |
| 状态药丸 | `items[].status`（只可能是 `SUCCESS` / `SUCCESS_WITH_WARNINGS`） | `routes_workflows.py:222`、`orchestrator.py:333-363` | 未知字符串 → 该行不合格（不进态势带） |
| 行内时间 | `items[].finished_at`，标签写"分析完成于" | `routes_workflows.py:223`、`archive.py:65` | null → 退回 `created_at` 并改标签为"创建于" |
| 逐条依据角标 | `report.sections[0].cites`（数组，元素是 `T#`） | `schemas_workflow.py:191` | 空 → 不渲染角标 |
| 「查看报告」跳转 | `#/history/{workflow_id}`（现有深链接，走 `openHistoryDetail`） | `app.js:792 go()`、`app.js:3292 openHistoryDetail()` | — |

**一句话口径**：态势带 = 「用户问过什么（真实原文）× 报告正文第一段（真实原文）× 这份报告汇总了几次成功的取数（真实计数）」，按后端归档库自己的"完成时间新→旧"取前三条有报告的记录。

---

## 1. 数据源：接口 10，不是接口 7（本文最重要的一条更正）

`option-01.md:138` 写的是"用 `GET /api/workflows/history` + 逐条 `GET /api/workflows/{id}/report` 在前端拼"。**这条要改**：报告正文要取 `GET /api/workflows/history/{id}`（接口 10），不是 `GET /api/workflows/{id}/report`（接口 7）。三个理由，每一条都是代码里读出来的：

1. **两者读的地方不一样，接口 7 对归档记录根本不可靠。**
   接口 7 的 `get_report` 走 `registry.get(workflow_id)`（`routes_workflows.py:387`），读的是**进程内注册表**；`history` 读的是 **SQLite 归档库**（`routes_workflows.py:260-270`）。服务重启后注册表是空的、归档还在 —— 于是一条 `has_report=true` 的历史记录，拿它的 id 去打接口 7 会得到 **404 "工作流不存在"**（`ownership.py` 的 `_WORKFLOW_NOT_FOUND_DETAIL` 原话就是"工作流状态存在服务进程的内存里，服务重启后全部丢失"）。态势带是"打开页面就看到最近三条结论"的主场景，重启后必须还能出内容；用接口 7 就等于把首屏绑在一次进程生命周期上。
2. **接口 10 恒 200，不会 400；降级形状也更干净。**
   接口 7 没有报告时抛 `BAD_REQUEST`（`routes_workflows.py:391-397`）→ HTTP 400；接口 10 只可能 404（记录不存在/不属于你，`routes_workflows.py:312-316` + `ownership.py`），有记录就 200，报告解析不出来时 `report=null`（`routes_workflows.py:183-203 _parse_report`）。前端只要写一套分支。
3. **接口 10 更省。** 它返回 `{item, report}`（`routes_workflows.py:150-154`）；接口 7 返回 `{report, validation, results, llm_used}`，其中 `results` 是 `list[ToolResult]`，每个都可能带最多 20 行表格（`validation.py:85 _MAX_TABLE_ROWS`），8 个任务一起就是态势带用不上的几十 KB。

### 1.1 明确禁令（Wave B 不许越线）

- **禁止调用 `GET /api/workflows/{id}`（接口 6）**。它不是只读接口：`get_workflow` 里有一句 `_expire_if_stale(state)`（`routes_workflows.py:359`），它会**改 state 并落库归档**（`routes_workflows.py:477-534`，最后一行 `archive_if_terminal(state)`）。态势带是一块只读展示板，绝不能因为"看一眼列表"就把某条工作流判成 FAILED 并写进归档。这正是要避免的"会触发任务的调用"。
- 禁止对 `has_report !== true` 的 id 发任何报告请求（接口 7 会 400，接口 10 会白跑一次）。
- 禁止调用任何 POST（`/api/analysis`、`/api/auth/*` 除会话检查外）。
- 禁止在态势带请求图表 PNG（接口 8 读内存注册表，同 1.1 第一条的问题，且重启后必 404）。
- 禁止用 HTTP 状态码分流：`api()` 只看 `body.ok / body.error.code`，除 401 之外不看状态码（`app.js:352-361`）。分流一律用业务码 `NOT_FOUND` / `BAD_REQUEST` / `NETWORK_ERROR` / `UNAUTHORIZED`。

---

## 2. 逐条回答设计问题

### 2.1 "3 条结论"的选取规则（确定性 + 为什么诚实）

后端能用来排序的真实字段只有：`status`、`created_at`、`finished_at`、`has_report`、`llm_used`、`error_code`。**没有 importance / 权重 / 已读 / 置顶 / 业务标签**，任何"哪个结论更重要"的说法都是前端编造。所以规则分两段：先筛资格，再决定顺序。

**资格过滤（三条都要满足）**

```
item.has_report === true
&& (item.status === "SUCCESS" || item.status === "SUCCESS_WITH_WARNINGS")
&& typeof item.workflow_id === "string"
```

为什么把 `FAILED` 排除：`status` 由 `orchestrator._final_status()` 决定，语义是"这份报告能不能直接用"（`orchestrator.py:333-363`），四种"有话说"一律降为 `SUCCESS_WITH_WARNINGS`，没有任何一条通往 `FAILED`。所以 `FAILED` 意味着这条流程没有正常收尾。
⚠️ 但**不能反过来推**"有报告就一定不是 FAILED"：`_expire_if_stale()` 会把一条已经写好报告、只是心跳断了的工作流就地判成 `FAILED` 并归档（`routes_workflows.py:513-534` + `archive.py:67-69` 用 `state.report is not None` 现算 `has_report`）。也就是说 `status=FAILED && has_report=true` 是**可能出现的真实组合**。本规则选择它不进态势带 —— 因为态势带要说的是"这是结论"，而一条被判定超时的工作流不能这样承诺；它照常出现在「历史记录」里，不丢数据。
未知 `status` 字符串（跨版本降级值，`routes_workflows.py:109-120` + `_coerce_enum`）一律不合格：认不出来就意味着不能替它宣称"成功"。

**顺序：不在前端重排，直接用后端归档库给的顺序。**

库里排序是写死的：`ORDER BY finished_at IS NULL, finished_at DESC, created_at DESC`（`app/data/history.py:179`，`list_recent` 于 `:368-390` 使用），而且时间统一存 UTC ISO 串（`history.py:62-71` 专门论证了"字典序 == 时间序"，才能安全配合 LIMIT/OFFSET）。所以 `history?limit=12&offset=0` 返回的本身就是"最近完成的在前"。

诚实性论证（Wave B 注释里要写这句）：

> 本视图不引入后端没有的排序权。顺序 = 归档库自己的"完成时间新→旧"，前端只筛不排；因此不存在"每页各自排一遍导致跨页顺序错乱"的经典错位（`routes_workflows.py:246-249` 的告警正是针对这件事），也不需要假装"重要度"。

**取前 3 条**（`POSTURE_MAX_ROWS = 3`）。不足 3 条就画 1–2 行，并在末行位置写一句实话（见 §4 场景 5），**不补占位假行**。

*（可选变体，Wave B 默认不实现）* 若产品坚持"干净的先于带警告的"，唯一诚实的写法是把它命名为**可信度优先而非重要度优先**：`rank(status)` = `SUCCESS`→0，`SUCCESS_WITH_WARNINGS`→1，作为**第一关键字**、`finished_at` 作第二关键字，并用稳定排序。一旦启用，行内必须同时露出状态药丸，让人看见"这一条其实是带警告完成的"。本文不启用它，因为它会让一条三个月前的干净结论压掉今天刚出的带警告结论，而界面上没有任何字段能替这个选择背书。

### 2.2 问题文本来源：确认 `items[].question` 就是用户原话

链路逐跳核实（**确认成立**）：

`QuestionRequest.question`（`schemas_workflow.py:296-304`，`min_length=2, max_length=500`）→ `routes_analysis.py:90 question=req.question`（**没有 `.strip()`、没有改写**）→ `WorkflowState.question`（`schemas_workflow.py:234`，`min_length=1`）→ `archive.py:62 question=state.question` → SQLite `question` 列 → `routes_workflows.py:221 question=record.question` → `items[].question`。

所以它 100% 是用户敲进去的那句话，把它当"业务问题"不是转述、不是 AI 摘要，是最强的真话。三个边界：

1. **可能很长**（≤500 字）。处理：CSS 两行 `line-clamp`，`title` 属性放全文；**不要在 JS 里 `slice()` 截断** —— 理由与 `report_llm.py:25-36`（"为什么不修剪模型的文字"）同源：句子里可能带数字，`"12,463"` 截成 `"12,46"` 会把真话变成不存在的数，而 CSS 省略号只改可见像素、不改内容。
2. **可能含换行**（`min_length=2` 不挡 `\n`，pydantic 2.13.4 默认不 strip 空白，本仓库 `QuestionRequest` 未设 `str_strip_whitespace`，已 grep 确认无 `field_validator`）。处理：容器上 `white-space: normal` 就够，**不要** `pre-line`（用户粘贴进来的连续空行会把态势带行撑高）。
3. **可能全是空白**（`"  "` 两个空格就满足 `min_length=2`）。处理：`String(item.question).trim()` 为空 → 显示 `（这条记录没有留下问题文本）`。不许拿 `report.title` 顶替 —— 模板/模型两条路径的 `title` 都是 `"分析报告：" + _clip(question, 60)`（`validation.py:221`、`report_llm.py:376`），问题空白时 title 也只是同一句话的空版本，等于换个地方再空一次。

### 2.3 一句结论来源：`sections[0].text`，**不是 `summary`**

读 `validation.py` 后确定：`summary` 不是业务结论，是**这份报告的元说明**。`_summary()`（`validation.py:432-485`）返回三段用 `\n\n` 拼接的文本：

| 段 | 内容 | 出处 |
| --- | --- | --- |
| 第 1 段 | 三选一的**逐字诚实标记常量**（见 §2.4） | `validation.py:66-84` |
| 第 2 段 | `针对问题「{question}」，系统成功完成了 {citations.length} 个分析任务。` | `validation.py:471-473` |
| 第 3 段（仅在有失败/跳过时） | `未能完成的任务 —— 失败：…；跳过：…` | `validation.py:475-485` |

它里面**没有任何业务结论**，第 2 段还和 `question` 重复。所以拿 `summary` 当"一句结论"会得到一句元话语，视觉上是废话、口径上是把"用户原话"在同一行说两遍。

**结论位 = `report.sections[0].text`。** 两条路径的实际长相：

- 规则模板（`validation.py:269-283 _section_for`）：`{_summarize(result)}（工具：{tool_name}，任务：{task_id}）`。`heading` = 计划里的中文任务描述。句子由 `_summarize_rows` / `_summarize_describe` / `_summarize_correlation` / `_summarize_timeseries` / `_summarize_missing`（`validation.py:284-429`）产出，全是"排在最前面的一组是 地区 = 华东，销售额_sum = 1,234,567.00（共 6 行结果）。"这类真实数字句。
- 大模型撰写（`report_llm.py`）：`sections` 是模型写的 3–5 段，每段**必须有非空 `heading`/`text` 且 `cites` 必须指到真实 task_id**，否则整段丢弃、一段都不剩就整份退回模板（`report_llm.py:290-315`、`:323-357`）。所以模型路径的 `sections[0].text` 一定是一段真正文。

`sections[0]` 的排序含义要如实交代：模板路径 `sections` 顺序 = 计划任务顺序（`validation.py:251-262 _latest_results` 按 `plan.tasks` 排），即 **`T1` 那一段**；模型路径顺序 = 模型输出顺序。两者都**不是"重要性第一名"**。因此界面上：

- 结论行上方的小标签写「**报告首段结论**」而不是「关键发现」「最重要」；
- 结论句右侧渲染 `sections[0].cites` 角标（现有 `app.js:2847` 已经写「依据：T1、T3」，口径一致，直接复用）；
- 行尾「查看报告」跳到历史详情（那里有全部段落 + 表格 + 质量提示）。

**展示差异（大模型撰写 / 规则模板）—— 产品要诚实标注正文来源**：

| | 规则模板（`llm_used=false`） | 模型规划 + 模板汇总 | 大模型撰写正文 |
| --- | --- | --- | --- |
| 触发 | `plan.source == "rule_fallback"` 或模型未配置（`validation.py:134-152 should_try_llm_report`） | `plan.source=="llm"` 但 `report_llm.write()` 返回 `None`（超时/JSON 坏/数字无出处/太短）→ 退回模板（`validation.py:111-132 _build`） | 模型段落可用（`report_llm.py:359-390 _assemble`，`model_wrote=True`） |
| `summary` 首段 | `TEMPLATE_NOTICE`＝"本次未使用大模型，以下结论为工具结果的直接汇总" | `LLM_PLANNED_NOTICE`＝"本次由大模型参与规划（选择了分析工具与参数），但报告正文是模板自动汇总的…" | `LLM_WROTE_NOTICE`＝"本次报告正文由大模型根据工具结果撰写，其中所有数字均来自下方引用的工具调用，可逐条核对" |
| `report.llm_used` | `false` | **`true`** | **`true`** |
| 结论句长相 | 数据播报句，带"（工具：…，任务：T1）"尾巴 | 同上 | 解释性中文，无尾巴 |
| 态势带标注 | `规则模板汇总` | `模型规划 · 模板汇总` | `大模型撰写正文` |

⚠️ **`llm_used` 单独不足以区分这三态**：它的口径是"计划是不是模型给的"（`validation.py:231-245 _llm_used` 只看 `plan.source`），中间那一态（模型规划、正文仍是模板）与最后一态在 `llm_used` 上**都是 `true`**。若前端用 `llm_used ? "大模型撰写" : "规则模板"`（现有 `app.js:2825 srcTag(report.llm_used ? "llm" : "rules")` 正是这个二选一写法），就会在中间态说出一句假话 —— 而 `validation.py:72-78` 明确把这件事记为 D-22 类"数字全对、陈述是假的"。**这是本次取数设计里最容易踩的诚实性坑。**

**定案**：来源标注以 `summary` 第一段与三个常量的**逐字比对**为准（`summary.split("\n\n")[0]`，命中哪个常量就是哪一态）；比对不上（将来文案改版）→ 标注写成 `来源说明未识别`，同时该行其余内容照常显示。这是"宁可不说，也不猜"。三个常量作为字符串常量集中写在一个 `POSTURE_SOURCE_NOTICES` 表里，注释标出取证行号，提醒 Wave C 改文案要同步。

`summary` **可能为空吗**？两条正常路径都不会为空（第 1、2 段是无条件拼接的常量句）。唯一"看起来不对"的是内部异常兜底的 `_minimal_report()`（`validation.py:549-572`）：`summary = 常量 + "\n\n生成报告时遇到问题，只保留了基本说明。"`，`citations=[]`，`sections=[{heading:"报告生成遇到问题", text:"分析任务已经执行完毕，但把结果整理成报告时出错了。…", cites:[]}]`。**这份报告在字段上和正常报告无法区分**（没有 `is_minimal` 标志）。好消息是不需要区分：它进态势带会长成"结论：分析任务已经执行完毕，但把结果整理成报告时出错了。／证据 0"——**这恰好是真话**，比误标成"有结论"好。所以不做任何字符串嗅探来特判它（嗅探 = 依赖文案，一改就失效）。

### 2.4 证据数唯一口径：`citations` 去重长度，并写明它数的是什么

后端确实存在两个不同数字，都会被称为"证据"：

| 候选 | 定义（代码事实） | 是否可当"证据数" |
| --- | --- | --- |
| `report.citations` | **本次成功产出结果的任务 id 列表**。模板：`for result in _latest_results(state): if result.ok: citations.append(result.task_id)`（`validation.py:185-191`）；模型路径**同一算法**（`report_llm.py:370-372`，注释明写"`citations` 是这次成功产出了结果的任务，不是模型引用了哪些任务"）。`_latest_results` 按 task_id 去重（`validation.py:251-262`），所以**天然无重复**。`_minimal_report` 与"全部任务失败"时为 `[]` | ✅ **定为唯一口径** |
| `sections[].cites` 并集 | 正文**实际引用到**的任务。模板路径每段 `cites=[自己的 task_id]`（`validation.py:269-283`），所以并集 == `citations`；模型路径并集可能**真子集**（模型没引用某个成功任务；且 `cites` 里指向假任务的会被滤掉，`report_llm.py:344-348`） | ❌ 不用于证据数；用作**行内角标**（第 2 列尾部），因为它才是"这句话挂在哪几次取数上" |
| `validation.checked_numbers` | 校验扫过多少个数字（`schemas_workflow.py:178`，`validation.py:897-934 _check_numbers`）。⚠️ **接口 10 不返回 validation**（只有接口 7 有，`routes_workflows.py:86-103`），所以态势带拿不到；且接口 10 的 `results` 也没有 | ❌ 不进态势带（要它就得改后端，违反"不动后端"） |

为什么 `citations` 是对的那个：**"报告里每个数字能追回到某次工具调用"这句话，可数的那一半就是"这次有几条成功的工具调用被写进了报告"**。校验层做的事是把 `summary / sections[].text / data_quality_notes` 三处文本里的每个数字（扫描范围由 `validation.py:880 _scanned_texts` 定义）拿去 `_allowed_numbers` 这个出处池里查（池子 = `state.results` 全部数值 + 字符串内嵌数字 + 结构计数 + `state.profile`，`validation.py:938-972`）。出处池比 `citations` **更大**（含失败任务的残留数值、含画像），而 `sections[].cites` **可能更小**。所以：

- 报"每个数字都核对过"这种**全称断言**，前端手里没有可比对的池子 → **不许这么说**；
- 报"有几条成功的取数调用被汇总进来"这种**计数**，`citations.length` 是后端自己算的那个，且和 `summary` 第 2 段"系统成功完成了 N 个分析任务"**同一个数**（`validation.py:471-473` 用的就是 `len(citations)`）→ 两处互相印证，用户点对进去能在报告里数出来。

**UI 写法（唯一，不许出现第二个数字）**：

- 计数单元文字：`取数调用 N 次`（**不要**写"N 条证据""N 个依据""N 项核对"）；
- `title` / `aria-label`：`本报告汇总了 N 次成功的取数任务（citations）；点开报告可见逐条 T# 依据`；
- `N === 0` 且 `Array.isArray(citations)` → 显示 `取数调用 0 次`，并在该行加一条小字 `这条记录没有汇总到任何成功的取数`（这是 `reports[0]` 属于"没有可用的分析结果"/兜底报告的正常表现，不是错误）；
- `citations` 字段缺失或不是数组（跨版本/坏数据）→ 显示 `—`，小字 `未提供引用信息`。**绝不允许把"不知道"渲染成 0**，那是把未知说成一个真实的零。

前端仍做一次 `Array.from(new Set(...))` 去重再取长度，作为对"后端哪天不再去重"的防御；注释里写明今天后端已经去重（`validation.py:251-262`），这层只是防漂移。

### 2.5 每行其余字段（4 列布局对齐 option-01 第 5 节）

`问题 / 结论 / 证据 / 动作` 四列，逐项落地：

1. **问题列**：`item.question`（clamp 2 行）+ 状态药丸（`historyStatusText` 复用，`app.js:2902`，认不出的字符串照原样显示）+ 行内时间（`分析完成于 {本地时间}`，`title` 放 UTC 原值，沿用 `openHistoryDetail` 里 `app.js:3317-3335` 已确立的口径）。
2. **结论列**：小标签 `报告首段结论` + 来源标注（三态，见 §2.3）+ `sections[0].heading`（灰字一行）+ `sections[0].text`（clamp 2 行）+ `cites` 角标。
3. **证据列**：`取数调用 N 次`。
4. **动作列**：`查看报告` → `go("history") + openHistoryDetail(id)`，与 `loadHistory` 现有行为逐字一致（`app.js:3088-3093`）。**不放任何"导出/分享/删除"**（`00-brief` 2.2-1/2）。

---

## 3. 请求预算与缓存（硬上限）

```js
const POSTURE = {
  historyLimit: 12,       // GET /api/workflows/history?limit=12&offset=0
  maxRows: 3,             // 最多渲染 3 行
  maxBodyRequests: 3,     // 最多 3 次接口 10（只给前 3 个合格候选）
  successTtlMs: 5 * 60 * 1000,   // 成功取回的正文缓存 5 分钟
  failureTtlMs: 30 * 1000,       // 失败的负缓存 30 秒（防着一把梭重试打后端）
};
```

- **首屏总预算：最多 1 + 3 = 4 个 GET，并发上限 3。** 12 条 history（后端上限 100，我们只用 12：`routes_workflows.py:242`）；候选池取前 3 个 `has_report===true && status∈{SUCCESS, SUCCESS_WITH_WARNINGS}`，一一对应一次接口 10。**不给第 4 个及以后的候选发请求。**
- **不分页、不自动翻第二页。** `history` 无筛选参数（`:241-245` 只有 `limit/offset`），若第 1 页 12 条里只有 2 条有报告，就**照实只画 2 行**并说明；想看更多由用户点「刷新/看全部历史」，不是首屏自动打第二页（一次翻页 = 再一次 history + 最多 3 次详情，成本要人主动付）。
- **同一 `workflow_id` 不重复取。** `postureCache: Map<workflow_id, {at, state, report, error}>`；命中且未过期 → 直接渲染，0 请求。`state` ∈ `"ok" | "empty" | "not_found" | "error"`（`empty` 指 200 但 `report===null`，见 §4 场景 10）。负缓存只对 `not_found/error` 生效，`empty` 是稳定事实、缓 5 分钟即可。
- **切走再回来**：走缓存 —— 命中 TTL 内就 0 请求；过期只重发 **history 1 次** + 仅那些"缓存里没有合格记录"的 id。绝不为已在缓存里的记录重复取正文。
- **去重（in-flight）**：`posturePending: Map<workflow_id, Promise>`，同一 id 并发请求合并成一条。视图切换抖动 / 深链接 / 刷新键都靠它挡住。
- **过期响应丢弃**：沿用 `loadHistory` 的 `ticket` 机制（`app.js:3111-3121`）——每次 `loadPosture()` 自增 ticket，响应回来时若不是最新 ticket 就直接 return，防止旧页数据盖新数据。
- **与轮询隔离**：态势带**不进 1000ms 轮询循环**（`app.js:2467 state.pollTimer`）。它读的是归档，一秒一次纯属自扰（`00-brief` 2.2-4 的"不假装实时"）。唯一联动：现有轮询到终态、报告落地后（`app.js:2527-2541` 的终态分支），把该 workflow_id 从 `postureCache` 里 **invalidate**（`delete`），并在态势带当前可见时重跑一次 `loadPosture()`；不可见就等下次进入视图时自然取。这样"刚跑完的分析"能自己升上态势带，而不需要定时轰炸。
- 后端侧代价说明（写给评审）：归档读是短连接 SQLite `SELECT`（`history.py:368-390`），4 个 GET 的量级与现有「历史记录」页一次翻页相当，不构成新压力。

---

## 4. 降级形态（一种都不许编数据）

Carbon 空态三成因在这里必须分开：**还没有** / **取不到** / **功能关着**。三者文案不许混用。

| # | 触发条件（可判定的代码条件） | 标题文案 | 辅助文案 | 动作 |
| --- | --- | --- | --- | --- |
| 1 | `history` 请求 in-flight | 无文案，3 行骨架 | — | 容器 `aria-busy="true"` |
| 2 | `!result.ok`，`error.code==="NETWORK_ERROR"` | 此刻连不上服务 | 请确认后端还在运行；下面的内容不是"没有结论"，是没读到 | 「重试」（`isRetryable` 不含此码，但重试按钮对用户有价值） |
| 3 | `!result.ok`，`error.code==="SERVICE_UNAVAILABLE"` | 服务暂时不可用 | 稍后自动可恢复，可以点重试 | 「重试」（现有 `RETRYABLE` 口径，`app.js:55` + `isRetryable` `:385`） |
| 4 | `!result.ok`，其它码（`INTERNAL_ERROR`/`BAD_RESPONSE` 等） | 态势带读取失败：`{error.message}`（`{error.code}`） | 其余页面不受影响，可先去看历史记录 | 「重试」+「查看历史记录」 |
| 5 | `data.enabled === false` | 这台服务没有开启任务归档，所以这里没有可展示的结论 | 归档是部署开关（`DB_PATH` 为空）关掉的，不是你没有分析过。做完的分析仍可在本次会话里查看 | 「去发起一次分析」 |
| 6 | `enabled===true && items.length===0`（`total===0`） | 还没有任何分析记录 | 第一个结论会在你完成第一次分析后出现在这里 | 「去发起一次分析」 |
| 7 | `items.length>0 && 合格候选为 0` | 最近的 `N` 次分析还没有产出报告 | 它们要么失败了，要么没有跑到出报告的那一步 —— 这跟"还没做过分析"是两件事。逐条原因在历史记录里 | 「查看历史记录」 |
| 8 | 合格候选 1–2 条 | 正常渲染 1–2 行 + 尾部一行说明：`可用的真实结论只有 {候选数} 条，所以这里只有 {候选数} 行。` | — | 「查看历史记录」 |
| 9 | 某条接口 10 返回 `!ok && code==="NOT_FOUND"` | 该行保留问题/时间（来自 history），结论位显示 `这条记录的报告读不到了` | 小字：`归档里有这条记录，但报告正文取不回来（多半是服务重启前的记录）。` | 「在历史记录里打开」 |
| 10 | 接口 10 返回 200 但 `report===null` 而 `item.has_report===true` | 结论位 `报告已归档但正文无法解析` | 与 `openHistoryDetail` 现有文案一致（`app.js:3350`），两处不许各写一套 | 「查看归档详情」 |
| 11 | 接口 10 返回 `NETWORK_ERROR` / `INTERNAL_ERROR` | 该行结论位 `这条结论暂时取不到` | `（{code}）` | 「重试这一行」（只重发这 1 个请求） |
| 12 | 任一次请求 `error.code==="UNAUTHORIZED"` 或 HTTP 401 | **不写本地文案**：`api()` 会调 `sessionLost()`（`app.js:492-513`）整页换成登录门并清 `state`；`loadPosture` 只需在 ticket 检查下静默退出，不渲染 | — | — |
| 13 | `sections` 不是数组 / `sections[0].text` 空 | 结论位 `这条记录没有可显示的结论正文` | 不许回落到 `summary`（那不是结论） | 「查看报告」 |
| 14 | `citations` 不是数组 | 证据列 `—` + 小字 `未提供引用信息` | 见 §2.4：不许把未知渲染成 0 | 「查看报告」 |

文案纪律：自然中文、无内部字段名（不许在界面上出现 `citations` / `llm_used` / `has_report` 这些词，只在 `title` 的补充说明里给字段名可以，正文里不行 —— 这条与 `validation.py:57-62` 对 `TEMPLATE_NOTICE` 的措辞要求同一个口径：`"这句话直接上屏，措辞必须是用户视角的"`）。

---

## 5. "数据截至"该怎么写（歧义与唯一定案）

`option-01.md:51` 命令条要显示 `数据截至 2026-10-15`，`option-01.md:105` 报告头部还要显示 `来源数据集`。**核实结果：这两个在态势带/命令条上都拿不到。**

证据链：

- `HistoryItem` 的字段只有 `workflow_id/question/status/created_at/finished_at/has_report/llm_used/error_code`（`routes_workflows.py:123-130`）——**没有 `dataset_id`**。
- `HistoryDetailData` 只有 `{item, report}`（`:150-154`）；`FinalReport` 没有数据集标识（`schemas_workflow.py:198-212`）。
- 接口 7 的 `ReportData` 有 `results`，但 `ToolResult` 也没有 `dataset_id`（`schemas_tool.py:62-81`）。
- 唯一带 `dataset_id` 的是接口 6 的 `WorkflowStatusData`（`schemas_workflow.py:343-368`，`dataset_id` 在 `:357`），而**接口 6 有写副作用**（§1.1 禁令），且只对进程内工作流有效。
- `data_quality_notes` 全来自画像 warnings（`validation.py:487-500`），已 grep `app/data/profiler.py` 确认告警文案不含文件名。

所以"取所选报告来源数据集的 `GET /api/datasets/{id}` 上传时间"这条路**在技术上不成立**（没有 id 可传）。而且就算凑一个 —— 比如用"当前选中的数据集"的 `uploaded_at` —— 那是**把不相干的数据凑成一句断言**：态势带三条结论可能来自三个不同的、甚至已被替换的数据集，`00-brief` 2.3 的诚实要求正好禁这类张冠李戴。

**定案（唯一写法）**：

1. 命令条上的全局时间戳 → 改为 **`列表更新于 {短时刻}`**（用本次 history 请求发起的客户端时刻，走现有 `fmtClock` 的"截至 HH:MM"用法，`app.js:2081-2090`），`title` 写：`这是结论列表的刷新时间，不是数据本身的截止时间。` 现有 `loadRecentTasks` 已经是这个口径（`app.js:3043-3048`「· 截至 11:57 · N 条」），不新造第二套说法。
2. 每行自己的时间 → **`分析完成于 {finished_at 本地时间}`**，`title` 放 UTC 原值。这是这批字段里唯一真正属于"这次分析的收尾时刻"的量。⚠️ 歧义要点明：`archive.py:65` 在 `finished_at` 为空时会用 `archived_at` 顶上，所以严格说是"归档里的完成时间"；`finished_at===null` 时前端退回 `created_at` 并把标签改成 `创建于`，不许继续写成"完成于"。
3. 若要给"这份报告本身"的时间，用 `report.generated_at`（`schemas_workflow.py:209`），标签写 `报告生成于`；它和"分析完成于"可以不同（校验后重写会再生成一版，`orchestrator.py:179-180`），两个都在时**各写各的标签、不合并**。
   ⚠️ 顺带纠一个现有 bug，Wave B 若复用报告渲染会撞上：`renderReportInto` 里读的是 `report.created_at`（`app.js:2826`），而 `FinalReport` 没有这个字段（只有 `generated_at`），所以"生成时间"这一句现在**永远不会出现**。修它属于 Wave B/C 的范围，本文只登记。
4. **「来源数据集」这一项从态势带与报告头部删除**（除非将来后端给 `HistoryItem` 加 `dataset_id`，那是后端改动，不在本次范围）。

---

## 6. 性能与首次进入体验

- **首屏结构**：进入 `command` 视图立刻渲染 3 行**骨架**（问题/结论/证据三个格子各一条灰条），容器 `aria-busy="true"`；history 落地后按真实候选条数改成 1–3 行真结构 + 每格 `取数中…` 占位；每份正文各自落地，**逐行填充，不等三条齐**（3 个请求并发，谁先回来谁先上屏，行位置按候选顺序固定，不会因为乱序而跳动）。
- **骨架样式冲突要裁决一次**：`option-01.md` 第 11 节要 `1.3s shimmer`，但现有 `app.css:998-1004 .skeleton` 注释明确写着"改成静态灰条 —— `docs/ux-spec-final.md` §2.3 规定静态灰条不闪烁，而且扫光渐变撞上『不要大面积渐变』的品牌红线"。**本设计沿用静态灰条**（不新增 shimmer），把动画预算留给第 11 节里的"视图切换淡入 120–200ms"。这条冲突写在这里，是要 Wave C 别以为漏做了动效。
- **轮询刷新不闪烁（option-01 第 11 节第 4 行）**：态势带的任何刷新（显式刷新键、TTL 过期、刚完成一条分析后的 invalidate 重载）都必须**原地替换文本节点**：不 `clear()` 整个容器、不重放骨架、不加淡入。只更新被换掉的那一格 + `列表更新于 HH:MM`。这条是"轮询不该伪装成实时推送"的同一族要求。
- **失败行不许清空成功行**：单行取回失败只改那一行（§4 场景 11），其余两行保留。
- **预算保证**：首屏最坏 4 个 GET，重复进入 0 个（TTL 内）。任何路径都不会超过 `1 + 3`。
- **可访问性**：行是可聚焦的链接/按钮（`查看报告`），整行不做假可点；`aria-busy` 只在取数期间存在；空态文案放进带 `role="status"` 的区域，屏幕阅读器能读到"还没有分析记录"与"读不到"的区别。

---

## 7. Wave B 照抄的函数签名与伪代码

复用现有工具函数：`api()`（`app.js:328`）、`errorOf()`（`:378`）、`isRetryable()`（`:385`）、`pickField()`（`:1105`）、`el()` / `clear()` / `emptyBlock()`（`:223`）、`fmtDateTime()` / `fmtClock()` / `fmtInt()`（`:240+`、`:2081+`）、`historyStatusText()`（`:2902`）、`srcTag()`（`:2073`）、`renderReportInto()`（`:2818`，详情抽屉用）。API 表已有 `API.history` / `API.historyDetail(id)`（`:45-46`），**不需要新增接口地址**。

```js
/* ==== 常量（三个 notice 必须逐字复制，取证行号写在注释里） ==== */
var POSTURE_MAX_ROWS = 3;
var POSTURE_HISTORY_LIMIT = 12;          // 后端 1–100；见 routes_workflows.py:242
var POSTURE_BODY_BUDGET = 3;             // 最多 3 次接口 10
var POSTURE_OK_TTL_MS = 5 * 60 * 1000;
var POSTURE_FAIL_TTL_MS = 30 * 1000;
var POSTURE_ELIGIBLE_STATUS = ["SUCCESS", "SUCCESS_WITH_WARNINGS"];

// 逐字来自 app/agents/validation.py:66 / :73 / :80。改后端文案要同步这里。
var POSTURE_SOURCE_NOTICES = [
  { key: "model-wrote", label: "大模型撰写正文",
    notice: "本次报告正文由大模型根据工具结果撰写，其中所有数字均来自下方引用的工具调用，可逐条核对" },
  { key: "model-planned", label: "模型规划 · 模板汇总",
    notice: "本次由大模型参与规划（选择了分析工具与参数），但报告正文是模板自动汇总的，以下结论仍为工具结果的直接汇总" },
  { key: "rules", label: "规则模板汇总",
    notice: "本次未使用大模型，以下结论为工具结果的直接汇总" }
];

/* ==== 状态袋（形状对齐现有 reportCatalog / historyList 的写法） ==== */
var posture = {
  loading: false, loaded: false, enabled: null,
  ticket: 0, updatedAt: null, error: null,
  rows: [],            // [{item, body}] —— body 见 fetchPostureBody 的返回
  emptyKind: null      // null|"loading"|"error"|"disabled"|"no-record"|"no-report"|"partial"
};
var postureCache = new Map();    // workflow_id -> {at, state, report, error}
var posturePending = new Map();  // workflow_id -> Promise（同一 id 合并请求）

/* ==== 纯函数（可单测，不发请求） ==== */
function postureEligible(item) {}          // -> boolean；§2.1 的三条资格
function postureCandidates(items) {}       // -> item[]；filter + slice(0, 3)，**不排序**
function postureQuestionText(item) {}      // -> {text, missing:boolean}；trim 为空则 missing
function postureSourceTag(report) {}       // -> {key,label,matched}；summary 首段逐字比对，认不出 matched=false
function postureConclusion(report) {}      // -> {heading, text, cites[], missing:boolean}
function postureEvidence(report) {}        // -> {count:int|null, taskIds[], known:boolean}
function postureTimeLabel(item) {}         // -> {label, value}；完成于/创建于 二选一，不硬说

/* ==== 取数 ==== */
async function fetchPostureHistory() {
  // GET /api/workflows/history?limit=12&offset=0
  return api(API.history + "?limit=" + POSTURE_HISTORY_LIMIT + "&offset=0");
}

async function fetchPostureBody(workflowId, force) {
  // 单一职责：拿一条归档记录的报告正文。缓存/去重/负缓存都在这层。
  // 返回 {state:"ok"|"empty"|"not_found"|"error", report|null, error}
  //   ok       —— 200 且 data.report 是对象
  //   empty    —— 200 但 data.report 为 null（归档正文解析不出来，routes_workflows.py:183-203）
  //   not_found—— error.code === "NOT_FOUND"（记录没了/不是你的）
  //   error    —— 其余（NETWORK_ERROR / INTERNAL_ERROR / 意外的 BAD_REQUEST …）
  // 禁止在这层加任何重试循环；重试由用户点按触发。
}

async function loadPosture(force) {
  // 1) ticket 自增；渲染骨架；aria-busy=true（§6 首屏结构）
  // 2) fetchPostureHistory() -> 非最新 ticket 直接 return
  // 3) 按 §4 场景分派 emptyKind；enabled===false / items 空 / 无合格候选 三态各自文案
  // 4) candidates.forEach(取前 3) -> 先落"问题 + 结论位取数中"的空行
  //    每行 fetchPostureBody().then(只替换该行；不 clear 整块)
  // 5) posture.updatedAt = 请求发起时刻 -> 命令条「列表更新于 HH:MM」
}

/* ==== 渲染 ==== */
function renderPosture() {}                // 分发：骨架 / 行 / 空态
function renderPostureRow(row) {}          // 4 列，§2.5；只改自己那一行
function renderPostureEmpty(kind, ctx) {}  // ctx = {itemsCount, candidatesCount}；§4 表
function renderPostureSkeleton(n) {}       // 静态 .skeleton，无 shimmer
```

**接线点（Wave B 只改这几处，别处不动）**：

1. `VIEWS` 增加 `command`（含 `crumb/title/sub`）；`VIEW_GROUP` 归入"指挥"组（现有位置：`app.js:117-147`）。
2. `applyView()` 的视图分支里加 `else if (name === "command") { loadPosture(); }`（`app.js:873` 起那串 `if/else`，与 `history → loadHistory()` 同层）。
3. `hashParts()` / 深链接 `applyDeepLink()` 无需为 `command` 加 id 形态（态势带不带实体 id）。
4. `pollOnce()` 的终态分支（`app.js:2527-2541`）加一句 `postureCache.delete(state.workflowId)`，并在 `state.view === "command"` 时 `loadPosture()`（不新增定时器）。
5. 会话过期不用改：`sessionLost()` 已经 `stopPolling()` + 清 `state`（`app.js:492-513`）。

**可复用之处，明确写出来免得 Wave B 重写**：

- `loadHistory()`（`app.js:3099`）的 `ticket` 守卫、`enabled===false` 分支、空态文案（`app.js:3085`「还没有任何分析记录。做完第一次分析后，这里会出现它。」）、失败态 + 「重试」按钮（`:3126-3143`）—— 态势带的 §4 场景 5/6/4 与它逐字同源，**文案必须复用同一条字符串**，不许两处各写一套。
- `loadReportCatalog()`（`app.js:3200`）已经做了 `items.filter(item => item.has_report === true)`（`:3241-3243`）和 `reportCatalog.withReport === 0` 的区分（`total===0` vs 有记录但无报告）—— 这正是 §2.1 的资格过滤前半段与 §4 场景 6/7 的区分，Wave B 直接把这段判断抽成 `postureEligible` 后共用，别留两份。
- `openHistoryDetail()`（`app.js:3292`）已经处理了"200 + `report===null` + `has_report===true`"的文案（`:3348-3352`），场景 10 直接沿用它的句子。
- `HISTORY_PAGE_SIZE = 20`（`app.js:2967`）留给历史/目录页；态势带用独立的 12，两者不要互相"顺手统一"。

---

## 8. 判定为"做不到 / 必须降级"的点（宁可少画，不画假的）

1. **"重要度排序"做不到。** 后端无任何字段可承载，UI 不许出现"重点/置顶/优先级"，只能按归档库时间序（§2.1）。
2. **命令条的"数据截至 2026-10-15"做不到。** 归档链路（history → historyDetail）里既没有 `dataset_id` 也没有数据集上传时间，接口 6 虽有 `dataset_id` 但有写副作用被禁用。改成"列表更新于 + 每行分析完成于"（§5）。
3. **态势带行里的"来源文件名"做不到**（`option-01.md:105` 要求的那个）。同上，没有 id 也没有名字可查 → **删除这一项**，改为 `取数调用 N 次` + `cites` 角标。
4. **结论句里的"升降色 delta"做不到。** 没有任何字段表达同比/环比/变化方向；方向语义只存在于句子文字里（如 `_summarize_timeseries` 的"由 X 变为 Y"，`validation.py:393-408`）。→ 态势带**不画涨跌箭头、不画 delta 百分比**，`--ok-700/--err-700` 只用于状态药丸与校验 severity（option-01 第 4 节本来就规定状态色只在有语义时露脸，这里正好一致）。
5. **"证据数 = 已核对数字个数"做不到。** `validation.checked_numbers` 不在接口 10 的返回里（`routes_workflows.py:150-154`），取它得改后端。→ 证据数唯一口径就是 `citations.length`，并写清数的是"成功的取数任务"（§2.4）。
6. **`llm_used` 不能当"正文是谁写的"。** 它只区分两态，而真实有三态。→ 用 `summary` 首段常量比对，认不出就标"未识别"（§2.3）；现有 `srcTag(report.llm_used ? "llm" : "rules")`（`app.js:2825`）在态势带**不许照抄**。
7. **兜底报告（`_minimal_report`）无法从字段上识别。** 没有标志位，靠文案嗅探不稳。→ 不做特判；它会自然长成"结论 = 报告整理出错了 / 取数调用 0 次"，这本身诚实（§2.3）。
8. **重启后的历史记录，图表与逐条工具结果拿不到。** 接口 7/8 都读内存注册表。→ 态势带不放图（本来就不放）；详情抽屉里图表区对归档记录要显示"图已随进程结束，归档只保留报告文本"，不许留一个空白 `<img>` 让人以为坏了。
9. **首屏不做实时感。** 无 SSE/WebSocket，态势带不参与 1000ms 轮询，刷新靠 TTL + 用户动作 + 终态 invalidate（§3/§6）。
10. **骨架 shimmer 与现有规范冲突，本设计选静态灰条**，`option-01.md` 第 11 节的 `1.3s shimmer` 在 Wave C 若要保留，需先同步改 `docs/ux-spec-final.md` §2.3 与 `app.css:998-1004` 的注释（§6）。

---

## 9. Wave B 自检清单（每条都可对着代码验）

- [ ] 首屏最坏只发 4 个 GET（1 history + 3 historyDetail），代码里没有任何第 4 次详情请求的路径。
- [ ] 全文件搜不到 `API.report(` 的态势带调用、搜不到 `API.workflow(` 的态势带调用（接口 6 有写副作用）。
- [ ] 没有任何 `has_report !== true` 的 id 被拿去发请求。
- [ ] `postureCandidates` 里没有 `.sort(` —— 前端不重排（若启用 §2.1 可选变体，必须在注释里写清"可信度优先，不是重要度"，且状态药丸同时可见）。
- [ ] 问题/结论文本只用 CSS `line-clamp` 截断，JS 里没有对 `question` / `sections[].text` 的 `slice/substring`。
- [ ] 来源标注走 `summary` 首段常量比对，三态 + "未识别"；没有 `llm_used ? "大模型" : "规则模板"` 的二选一写法。
- [ ] 证据数只有一个数字；`citations` 非数组时显示 `—`，空数组时显示 `0`。
- [ ] §4 的 14 个场景逐条有对应文案，且"还没有分析"（场景 6）与"取不到"（场景 4/9/11）与"功能关着"（场景 5）三种文案互不通用。
- [ ] 刷新时不 `clear()` 整块容器、不重放骨架；`列表更新于 HH:MM` 走 `fmtClock`。
- [ ] 会话过期只由 `api()` 的 401 分支处理，态势带不写自己的"登录失效"文案。
- [ ] 界面上不出现 `citations` / `llm_used` / `has_report` / `T#` 以外的内部术语（`T1/T2` 是后端给用户的原文，可以出现）。

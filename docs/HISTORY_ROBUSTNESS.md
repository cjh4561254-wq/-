# 历史归档健壮性：已实证缺陷与修复指引

> 状态：**部分已修（工作区未提交）**。
> - 缺陷 1（`status`）与缺陷 2（`error_code`）**已修**：`routes_workflows.py` 现在用
>   `_coerce_enum(enum_cls, raw)` 降级（认不出来就原样保留字符串），并且
>   `HistoryItem.status` / `error_code` 的字段类型已放开成 `... | str`。
> - 缺陷 3（`report_text`）**仍未修**：`routes_workflows.py:268` 的
>   `FinalReport.model_validate_json(record.report_text)` 依旧无兜底，坏 `report_text`
>   的详情请求仍然 500。
>
> 本文做取证与交接，不改产品代码。适用于接手 P3 这条线的人：
> 读完本文应当**不需要再问任何人**就能动手修剩下的缺陷 3。
>
> 取证执行者：`task29-qa`（独立验收方）。所有状态码、响应体、字节数均为本机实跑所得，
> 非推断。文件路径均为仓库内相对路径。
>
> ⚠️ 行号会随修复漂移：`routes_workflows.py` 修复后从 462 行变成 493 行，
> 缺陷 1/2 的构造点从 `:154`/`:159` 移到 **`:181`/`:187`**，缺陷 3 从 `:237` 移到 **`:268`**。
> 本文正文保留**修复前的行号**并同时标注修复后行号，读代码时以表达式为准，别只信行号。


---

## ① 缺陷证据链

三个缺陷**同一个模式**：把历史库里读回来的 TEXT 列**硬转**成枚举 / pydantic 模型，
失败时异常一路冒到 `app/main.py` 的兜底 handler，被翻成 `500 INTERNAL_ERROR`。
库里的数据是好的，坏的是**读取侧的信任假设**——它假设"我们能写进去的，一定是我们认识的"。
一旦枚举改名（跨版本持久化），这个假设就破了。

### 缺陷 1：`app/api/routes_workflows.py:154` —— 非法 `status` 整页 500【必填列】

```python
status=WorkflowStatus(record.status),     # routes_workflows.py:154
```

`WorkflowStatus` 是 `(str, Enum)`（`app/schemas_workflow.py:64`，10 个成员）。
`WorkflowStatus("RUNNING")` 抛 `ValueError: 'RUNNING' is not a valid WorkflowStatus`。

调用点 `app/api/routes_workflows.py:198` 是**无逐行保护的列表推导**：
```python
items=[_item_from_record(record) for record in records],   # routes_workflows.py:198
```
所以**一条坏行毁掉整页**。

复现（脚本 `.tmp/acc/repro_p3.py` 步骤 1，库内 4 行、其中 1 行 `status='RUNNING'`）：
```
GET /api/workflows/history -> 500
body = {"ok": false, "data": null, "error": {"code": "INTERNAL_ERROR",
        "message": "服务器内部错误，请查看服务端日志",
        "detail": "'RUNNING' is not a valid WorkflowStatus"}}
```
服务端日志：`[error] 未预期的异常 GET /api/workflows/history: ValueError("'RUNNING' is not a valid WorkflowStatus")`

**分页毒杀（重要）**：毒杀**严格按页发生** —— 一个分页请求 500，当且仅当**这一页包含坏行**。
实测（`.tmp/acc/check_paging_claim.py`，25 行、1 条坏行、`limit=5`）：

| 坏行在排序里的位次 | `limit=5` 各页 (offset, status) | 死掉的页 | 默认 `limit=20` 首页 |
|---|---|---|---|
| 第 25 位（最旧） | `[(0,200), (5,200), (10,200), (15,200), (20,500)]` | `[20]` | **200** |
| 第 14 位（中间） | `[(0,200), (5,200), (10,500), (15,200), (20,200)]` | `[10]` | **500** |
| 第 1 位（最新） | `[(0,500), (5,200), (10,200), (15,200), (20,200)]` | `[0]` | **500** |

⚠️ 关键：**决定死不死的是坏行的"排序位次"，不是它的时间新旧**，因为
`HistoryStore.list_recent()` 用 `ORDER BY finished_at IS NULL, finished_at DESC, created_at DESC`
排序，而 `archive_if_terminal` 对没有 `finished_at` 的记录会**用 `archived_at` 兜底**
（`app/core/archive.py`），所以「什么时候归档的」与「排在多前」可以不一致。

**实际影响**（默认 `limit=20`）：
- 坏行落在**前 20 个位次**（最前面那 20 条）里 → **首页必坏**，
  用户打开历史面板直接看到 500，且无从自救；
- 坏行排在第 21 位及以后 → 首页正常，翻到包含它的那一页才 500，
  也就是**"翻着翻着突然崩"**，比首页崩更难排查。
- ⚠️ **换 offset 不是绕行方案**：坏行永远属于某一页，翻到那一页就死。
  上表第二行（第 14 位，首页 500）与第一行（第 25 位，首页 200 但第 5 页 500）就是证据。


**影响面量化**：10 条合法 + 1 条非法 -> 500 且 `data=null`，用户**拿到 0 行**（本应 10 行）。
`HistoryStore.count()` 仍是 11、`list_recent()` 仍返回 11 条 —— 数据没丢，只是送不出去。

### 缺陷 2：`app/api/routes_workflows.py:159` —— 非法 `error_code` 同样整页 500【可空列】

```python
error_code=ErrorCode(record.error_code) if record.error_code is not None else None,   # :159
```

`ErrorCode`（`app/schemas.py`，8 个成员）同样硬转，异常类型同为 `ValueError`：

| 注入值 | 列表接口 | detail 原文 |
|---|---|---|
| `'TIMEOUT_LEGACY'` | 500 | `"'TIMEOUT_LEGACY' is not a valid ErrorCode"` |
| `'bogus'` | 500 | `"'bogus' is not a valid ErrorCode"` |
| `''`（空串） | 500 | `"'' is not a valid ErrorCode"` |

栈原文：`routes_workflows.py:198` in `list_history` → `routes_workflows.py:159` in `_item_from_record` → `enum.py:1165 raise ve_exc`

**与缺陷 1 的关键差异（决定修复优先级）**：

| | `status`（:154） | `error_code`（:159） |
|---|---|---|
| 列约束 | **NOT NULL，每条记录都有** | 可空，只有失败工作流才填 |
| 枚举改名后 | **全库每一条**同时变毒 | 只有"失败 **且** 带非法码"的行变毒 |
| 实际暴露面 | 大 | **小得多**（失败记录的**子集**） |
| 单次后果 | 整页 0 行 | **完全相同**，也是整页 0 行 |

⚠️ 所以：`error_code` 触发概率低，但**后果一样严重，不该按"影响小"降级不修**。

**双坏字段先炸 status**：`_item_from_record` 的关键字实参按**声明顺序**求值，
`status`（`:154`）在 `error_code`（`:159`）之前。所以一行两个字段都坏时，
`detail` 报的是 **status** —— 隔离对照（status 合法、只 error_code 坏）才报 error_code。
⚠️ 修复不能只看"谁先炸"就只修一处：**只修 `:159` 对双坏行零效果**（实测证据：
只给 `:159` 加容错后，双坏行的 `detail` 仍是 `"'RUNNING' is not a valid WorkflowStatus"`，
行为与修复前完全一致）。

### 缺陷 3：`app/api/routes_workflows.py:237` —— 坏 `report_text` 打炸详情接口【已实证 · 已修】

> **状态：task-38 已修**，详见⑤节修复 3。下面的"500 行为"是**修复前**的取证快照，
> 保留它是为了让后来者看明白**为什么**要修、以及测试为什么曾经断言 500。
> ⚠️ 修复后 `:237` 这行已挪到 `:294` 并变成 `_parse_report(record.report_text)`。

```python
report = (
    FinalReport.model_validate_json(record.report_text)    # routes_workflows.py:237
    if record.report_text is not None
    else None
)
```

这一处**此前只是候选（task-36 未实证）**，task-37 已实证为**真缺陷**。

**关键差别：抛的不是 `ValueError`，是 pydantic 的 `ValidationError`。**

⚠️ `ValidationError` 不是 `ValueError` 的子类，所以"兜底 handler 会不会接它"
**必须实测，不许推断**。`app/main.py` 的 `@app.exception_handler(Exception)` 确实接了 ——
实测见下（`test_fallback_handler_leaks_no_paths` 把这条钉成断言）。

三种构造的**原始状态码 + 异常类型**（`GET /api/workflows/history/{wid}`）：

| 注入的 `report_text` | HTTP | 失败类型（detail 里的 token） |
|---|---|---|
| `"not json"` | **500** | `json_invalid`（Invalid JSON: expected ident at line 1 column 2） |
| `'{"title": 123}'` | **500** | `string_type`（Input should be a valid string） |
| `'{"unknown_field": 1}'` | **500** | `missing`（6 validation errors：report_id/title/summary/sections/generated_at/llm_used 全缺） |
| `''`（空串） | **500** | `json_invalid`（EOF while parsing a value at line 1 column 0） |

响应体形状（以 `'{"unknown_field": 1}'` 为例，逐字）：
```json
{"ok": false, "data": null,
 "error": {"code": "INTERNAL_ERROR",
           "message": "服务器内部错误，请查看服务端日志",
           "detail": "6 validation errors for FinalReport\nreport_id\n  Field required [type=missing, input_value={'unknown_field': 1}, input_type=dict]\n    For further information visit https://errors.pydantic.dev/2.13/v/missing\ntitle\n  Field requi"}}
```

**两个对照组（证明爆炸半径比缺陷 1/2 小得多）**：

1. **列表接口不受影响**。列表**根本不解析 `report_text`**，也不回该字段，
   所以同一坏行下列表仍是 `200`、`total` 与 `items` 正常。
   > 对比：缺陷 1/2 的爆炸半径是**整个列表**（用户拿到 0 行）；
   > 缺陷 3 只毁**详情**。
2. **`report_text IS NULL` 的行不受影响**。被 `if record.report_text is not None` 短路，
   详情返回 `200` 且 `report: null`。
   > 这是**主流路径**：正常产品代码里"没有报告"写的就是 NULL
   > （`app/core/archive.py` 的 `report_text=... if report is not None else None`），
   > 所以日常使用不会碰到缺陷 3。

**烧掉的是"自己的详情"**：坏行在列表里可见（列表不解析它），但拿它的 id 打详情必 500 ——
这条记录**永远看不回来**，只能直连库。

**数据层不背锅**（与缺陷 1/2 一致的取证方式）：`HistoryStore.get()` 对
`report_text="not json"` **原样读出**，不解析、不抛，`rec.report_text == "not json"`。

⚠️ **异常信息不泄漏路径**（实测属性，已断言）：detail 是
`sanitize_detail(str(exc))[:500]` —— 长度**正好 500**（硬编码切片，不是 pydantic 自己截的；
`sanitize_detail` 对 1200 字符输入原样返回 1200）。对整段响应体做子串扫描：
`D:\` / `ai-data-analyst-agent` / `\Users\` / `AppData` / `Traceback` / `File "` **全部 0 命中**。
> 注意：`detail` 里确实出现字符串 `.py` 共 2 次，但那来自 pydantic 的报错文档链接
> `https://errors.pydantic.dev/2.13/v/missing`，**不是文件路径**。

### 为什么这是"低危但不该不修"

**正常产品路径产不出任何毒行**（静态 + 运行时两层已证）：
`state.status` 全仓只有 5 个写入点（`app/agents/orchestrator.py:432/441/455`、
`app/api/routes_workflows.py:429`、`app/core/background.py:69`），全部赋 `WorkflowStatus` 枚举成员；
`build_record()` 对 10 个 `WorkflowStatus` 成员 / 8 个 `ErrorCode` 成员逐个产出，非法值 0 个。

所以毒行**只能来自跨版本持久化**：将来枚举改名或删值，老库里的字符串就成了新代码不认识的值。
（`test_a_renamed_enum_value_poisons_the_listing` / `test_a_renamed_error_code_poisons_the_listing`
用 `UPDATE` 反向演示了这一点。）

**结论：现在不会发作，但一旦发作就是"历史功能整体不可用"，且用户无从自救。修。**

---

## ② 枚举 / 模型硬转点清单

判据（三条**同时**成立才算**真风险**）：
1. 值来自**持久化**（库里读回来的字符串）或**用户输入**；
2. 用 `Enum(...)` / 模型构造器**强制转换**且**无兜底**（不带 try、不带默认值）；
3. 非法值时**没有 pydantic 校验**先拦下来。

> **⚠️ 本表是 task-36/37 的取证快照，行号与"会不会 500"都是**修复前**的状态。**
> task-38 已把前三行全部修掉（`_coerce_enum` ×2 + `_parse_report`），
> 现在的行号是 `:207` / `:212` / `:294`，行为是**降级不再 500**（见⑤节）。
> 保留原表是因为"哪些落进了清单、哪些是假警报"这个**判据本身**才是交付核心。

| 文件:行 | 表达式 | 来源 | 持久化/用户输入 | 会不会 500 | 现有测试覆盖 |
|---|---|---|---|---|---|
| `app/api/routes_workflows.py:154` | `WorkflowStatus(record.status)` | 库 TEXT 列 `status` | **持久化** | **会**（修复前） | 是（task-35） |
| `app/api/routes_workflows.py:159` | `ErrorCode(record.error_code) if ...` | 库 TEXT 列 `error_code` | **持久化** | **会**（修复前） | 是（task-36） |
| `app/api/routes_workflows.py:237` | `FinalReport.model_validate_json(...)` | 库 TEXT 列 `report_text` | **持久化** | **会**（仅详情，修复前） | 是（task-37） |
| `app/schemas_workflow.py:110` | `chart_type: ChartType`（Literal 字段） | 用户/LLM 计划参数 | 用户输入 | 不会 | 是（pydantic 422） |
| `app/schemas_dataset.py:76` | `file_ext: FileExt`（Literal 字段） | 上传文件扩展名 | 用户输入 | 不会 | 是（pydantic 422） |
| `app/schemas.py:42` | `code: ErrorCode`（Enum 字段） | 各处构造 `ApiError` | 内部 | 不会 | 是（pydantic） |
| `app/schemas_workflow.py` | `status: WorkflowStatus`（Enum 字段） | 内存状态对象 | 内部 | 不会 | 是（pydantic） |
| `app/agents/orchestrator.py:227-277` | `task.status = TaskStatus.X` | 内存 task 对象 | 内部常量 | 不会 | 是 |
| `app/schemas_tool.py:52` | `ChartType = Literal[...]`（类型别名） | 工具层图表类型 | 用户输入 | 不会 | 是（pydantic 422） |

**真风险 3 处**，全在 `_item_from_record` / `get_history_detail` 的读取边界上 ——
task-38 已全部修复。

---

## ③ 真风险 vs 假警报

**判据**：进清单的必须同时满足上面三条（① 值来自持久化或用户输入、② 强制构造且无兜底、
③ 非法值没有 pydantic 校验先拦截）。绝大多数"看起来像硬转"的地方
其实是**假警报**，把它们当成缺陷会造成无谓的改动风险。

**假警报 A：枚举作为 pydantic 字段类型出现。**
`schemas.py:42` 的 `code: ErrorCode`、`schemas_workflow.py` 的 `status: WorkflowStatus`、
`schemas_dataset.py:76` 的 `file_ext: FileExt` —— 非法值会被 pydantic 翻成 `ValidationError`
（路由层变成 **422**，或内部构造时立刻失败），**在到达"硬转"之前就被拦住**。
判据 ③ 不成立，不算这一模式。

**假警报 B：`FileExt` / `ChartType` 根本不是 Enum。**
它们是 `typing.Literal`：

```python
FileExt   = Literal["csv", "xlsx"]                       # app/schemas_dataset.py
ChartType = Literal["bar", "line", "pie", "scatter"]     # app/schemas_tool.py:52
```

实测：`isinstance(FileExt, type)` 为 `False`；`FileExt("csv")` 抛
`TypeError: Cannot instantiate typing.Literal`。
**它们没有可调用的构造器**，不存在"枚举硬转"这种缺陷模式。

**假警报 C：`app/tools/validate.py:53` 的 `self.error_code = error_code`。**
这是**普通 str 赋值**，不是枚举构造。而且它的词汇
（`TOOL_NOT_FOUND` / `BAD_PARAMS` / `COLUMN_NOT_FOUND` / `BAD_OPERATION`）
与 `ErrorCode` 的 8 个成员**完全不相交**；`ToolResult.error_code` 的类型是
`Optional[Literal[...]]`，与 `WorkflowStatus`/`ErrorCode` 无关；
且它**从不写进历史库**——归档只取 `state.error.code.value`，
而 `state.error` 是 `app/schemas.py:39` 的 `ApiError`（`code: ErrorCode`，pydantic 保证合法）。
三重理由，纯假警报。

**全仓只有 3 个 Enum**：`ErrorCode` / `TaskStatus` / `WorkflowStatus`。
`test_the_enum_census_documents_exactly_two_hard_cast_sites` 把"全仓
`WorkflowStatus(record` / `ErrorCode(record` 恰好 2 处"钉住，新增硬转点会让它变红。

---

## ④ 复现步骤（照抄即可，输出为实跑所得）

前置：项目根 `D:\个人项目01\ai-data-analyst-agent`，一律用仓库自带的 venv Python
（系统 python 没装依赖）。

```powershell
cd D:\个人项目01\ai-data-analyst-agent
$env:PYTHONIOENCODING='utf-8'
```

**步骤 1 — 跑现成的最小复现脚本**（隔离库建在 `.tmp/acc/repro_db/`，绝不碰 `data/db/`）：

```powershell
& ".\.venv\Scripts\python.exe" .tmp\acc\repro_p3.py
```

它建 4 行：1 条好行 + 1 条坏 `report_text` + 1 条坏 `error_code` + 1 条坏 `status`，
然后逐个打两个接口。实跑输出（stdout，已滤掉服务端 traceback）：
（脚本会先删 `.tmp/acc/repro_db/` 再重建，**可重复跑**）

```
库文件 = D:\个人项目01\ai-data-analyst-agent\.tmp\acc\repro_db\app.db
库内行数 = 4
========================================================================
步骤 1：列表接口（坏 status 那条在库里 -> 整页应该炸）
========================================================================
GET /api/workflows/history -> 500
body = {"ok": false, "data": null, "error": {"code": "INTERNAL_ERROR",
        "message": "服务器内部错误，请查看服务端日志",
        "detail": "'RUNNING' is not a valid WorkflowStatus"}}

========================================================================
步骤 2：分页（库内 4 行，list_recent 顺序：a002 坏 status > a003 坏 error_code > a004 坏 report > a001 好行）
========================================================================
  ?limit=1&offset=0   -> 500      # 第 1 位 a002，坏 status
  ?limit=1&offset=1   -> 500      # 第 2 位 a003，坏 error_code
  ?limit=1&offset=2   -> 200      # 第 3 位 a004，坏 report_text —— 列表不解析它，所以不炸
  ?limit=20&offset=0  -> 500      # 默认页，包含坏 status / error_code

========================================================================
步骤 3：逐行打详情接口
========================================================================
  好行               -> 200
  坏 report_text    -> 500 | detail=1 validation error for FinalReport   Invalid JSON: expected ident at line 1 column 2 [type=json_invalid, input
  坏 error_code     -> 500 | detail='TIMEOUT_LEGACY' is not a valid ErrorCode
  坏 status         -> 500 | detail='RUNNING' is not a valid WorkflowStatus

========================================================================
步骤 4：数据层对坏行完全正常（不背锅）
========================================================================
  好行               store.get() -> status='SUCCESS' error_code=None report_text=None
  坏 report_text    store.get() -> status='SUCCESS' error_code=None report_text='not json'
  坏 error_code     store.get() -> status='FAILED' error_code='TIMEOUT_LEGACY' report_text=None
  坏 status         store.get() -> status='RUNNING' error_code=None report_text=None
  store.list_recent(limit=10) -> 4 行

复现结束。以上 500 都是**记录在案的缺陷**，修好后期望 200 + 跳过坏行。
```

**步骤 2 — 手工原始 SQL 注入**（若想自己造，别用脚本）。
注意：产品代码路径**产不出**毒行（pydantic 先拦），所以只能直插绕过门面。
先把库建出来（跑一次 lifespan 即可），再执行：

```python
import sqlite3
conn = sqlite3.connect(r"<你的 DB_PATH>")
conn.execute(
    "INSERT INTO workflow_history (workflow_id, question, status, created_at,"
    " finished_at, has_report, llm_used, report_text, error_code, schema_version,"
    " archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
    ("f" * 32, "毒行探针", "RUNNING",          # <- 枚举外 status
     "2026-10-14T00:00:00+00:00", "2026-10-14T00:00:00+00:00",
     0, 0, None, None, 1, "2026-10-14T00:00:00+00:00"),
)
conn.commit(); conn.close()
```
然后 `GET /api/workflows/history` 即 500。把 `status` 换成 `"SUCCESS"`、
`report_text` 换成 `"not json"`、或 `error_code` 换成 `"TIMEOUT_LEGACY"`，
分别命中缺陷 1 / 3 / 2。

**步骤 3 — 跑取证测试**（task-37 时 41 条，覆盖三个缺陷 + 对照组；task-38 后增至 44 条）：

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests/test_history_robustness.py -q
```

⚠️ **不要在 PowerShell 里用 `Get-Content` / `Out-String` 去数行或扫字节** ——
PowerShell 会重组行尾，数出来是假的。要数行就 `read_bytes()` 数 `\n`。

---

## ⑤ 修复状态：三处全部已修

**结论：本文取证的三处降级缺口，task-38 已全部修复。**
修复方式与本文原先的建议**不同**（值得记一笔）：最终没有选"跳过坏行"，而是选了
**降级** —— 记录**仍然返回**，不让任何一条坏数据把用户的整页历史打没。

### 逐处对照

#### 修复 1：`status`（必填列，原 `:154`）

| | |
|---|---|
| **旧行为** | `status=WorkflowStatus(record.status)` 硬转 → 非法值抛 `ValueError` → 兜底 handler 翻 **500**，同页合法行**一条都拿不到** |
| **新行为** | 认不出来时**原样透出字符串**：`"RUNNING"` 就回 `"RUNNING"`，`" Success"` 就回 `" Success"`（**逐字**，不 strip、不改大小写） |
| **修复手法** | 新增 `_coerce_enum(enum_cls, raw)`（定义见「行号锚点」块的 `:155`），`except ValueError: return raw`；调用点 `:215` |
| **对应用例** | `test_one_bad_row_no_longer_poisons_the_listing`、`test_miscased_and_bogus_statuses_are_passed_through_verbatim`（7 参数化）、`test_every_page_is_served_even_the_one_with_the_bad_row`、`test_ten_legal_rows_plus_one_bad_row_all_eleven_are_returned` |

#### 修复 2：`error_code`（可空列，原 `:159`）

| | |
|---|---|
| **旧行为** | `ErrorCode(record.error_code)` 硬转 → `'TIMEOUT_LEGACY'` / `'bogus'` / `''` 全部 **500** |
| **新行为** | 同样降级透出；★ **`''` 回 `""` 而不是 `None`** —— 空串与"无错误"语义不同，不许合并 |
| **修复手法** | 同一 `_coerce_enum`，调用点 `:223`（`error_code is not None` 短路保留） |
| **对应用例** | `test_one_bad_error_code_no_longer_poisons_the_listing`（3 参数化）、`..._poisons_the_detail_endpoint`（3 参数化）、`test_null_error_code_rows_are_unaffected`、`test_a_row_bad_only_in_error_code_blames_error_code` |

#### 修复 3：`report_text`（可空列，原 `:237`，修复后 `:312`）

| | |
|---|---|
| **旧行为** | `FinalReport.model_validate_json(record.report_text)` 无兜底 → pydantic `ValidationError` → 兜底 handler 翻 **500**，**该条记录的详情永久打不开**（列表正常，所以用户看到"能列出、点进去就报错"） |
| **新行为** | 解析失败 → **`report` 降级为 `None`**，记录本身（`status`/`question`/时间/`has_report`）**照常返回**；**同时记一条 warning 日志**（不吞错） |
| **修复手法** | 新增 `_parse_report(raw) -> FinalReport \| None`（定义见「行号锚点」块的 `:178`），`except ValidationError: logger.warning(..., exc_info=True); return None`；调用点 `:312` |
| **对应用例** | `test_a_bad_report_text_no_longer_breaks_the_detail_endpoint`（3 参数化）、`test_an_empty_string_report_text_is_also_poison`、`test_a_bad_report_text_is_logged_not_swallowed`、`test_a_bad_report_text_row_does_not_poison_its_page`、`test_the_report_path_no_longer_reaches_the_fallback_handler` |

★ **与 status 降级的不对称是刻意的**：`status` 降级成字符串后用户还能看到值本身；
而 `FinalReport` 是一棵嵌套结构，给它喂半截数据比给 `None` 更糟。
理由写在 `_parse_report` 的 docstring 里。

### 行号锚点

下面每个 `行号锚点` 行由 `scripts/check_docs.py` 核对：它与
`app/api/routes_workflows.py` 里**代码自己声明**的同名锚点注释互相印证
（代码里写着 ``# **:NNN** `<该行原文>` ``）。**改代码时这两个地方必须一起改**，
否则 `check_docs.py` 会红 —— 这正是它存在的意义：把「文档腐烂」变成一次红灯。

行号锚点 `app/api/routes_workflows.py:158`
行号锚点 `app/api/routes_workflows.py:183`
行号锚点 `app/api/routes_workflows.py:219`
行号锚点 `app/api/routes_workflows.py:226`
行号锚点 `app/api/routes_workflows.py:277`
行号锚点 `app/api/routes_workflows.py:322`
行号锚点 `app/api/routes_workflows.py:331`

### ⚠️ 本轮最值钱的坑：只加 `_coerce_enum` 不够

必须**同时**把 `HistoryItem` 的字段类型从 `WorkflowStatus` 放开成 `WorkflowStatus | str`
（`error_code` 同理加 `| str`）。否则降级出来的字符串会被 pydantic **再次拒收**，
失败点从 `ValueError` 挪到模型的 enum 校验 —— 11 种坏值**依然全部 500**。

这个坑**真实发生过**：第一轮修复后复测仍全红。
现在由 `test_the_enum_census_documents_exactly_two_hard_cast_sites` 与探针 MFIELD 守住
（MFIELD = 只把字段类型收回去 → **16 条变红**）。

### 修复后更新过的测试期望值

`tests/test_history_robustness.py` 的期望值**写死**（D-16 第 3 条），本轮全部翻成正向契约：

| 常量 | 旧值 | 现值 |
|---|---|---|
| `_EXPECT_LIST_BAD_ROW` | 500 | **200** |
| `_EXPECT_DETAIL_BAD_ROW` | 500 | **200** |
| `_EXPECT_LIST_BAD_ERROR_CODE` | 500 | **200** |
| `_EXPECT_DETAIL_BAD_ERROR_CODE` | 500 | **200** |
| `_EXPECT_DETAIL_BAD_REPORT` | 500 | **200** |
| `_EXPECT_EMPTY_STRING_REPORT` | 500 | **200** |

⚠️ **一个都没有改成"要么 500 要么 200"** —— 那是无效探针（两边都过 = 什么都没测）。
翻成 200 之后本文件用**五个变异**重新自证过（见⑥节末），全部能变红。

### 降级契约（已落地）

1. 坏行**出现在返回列表里**，`status` 逐字等于库里那个值（含前导/尾随空格、空串）；
2. `error_code` 空串透出为 `""`，与 `None`（无错误）**语义不同**，必须区分；
3. 同页合法行**一条不少**（1 坏 + N 合法 → N+1 行）；
4. `total` 仍是 `store.count()`（**全库行数，含坏行**）—— 前端据此算总页数；
5. 两处枚举构造（`:215` / `:223`）**必须同批修**，只修一处对"双坏行"零效果。

⚠️ `WorkflowStatus` **没有 `UNKNOWN` / `INVALID` 成员**（10 个全是业务状态），
所以"降级成一个占位枚举值"这条路走不通（要么新增成员牵动前端与 `TERMINAL_STATUSES`，
要么用 `FAILED` 撒谎）。**原样保留字符串**是唯一不撒谎的降级方式 —— 这也是本轮选择它的原因。

### 断言方向

下面每一行都是 `scripts/check_docs.py` 的**可执行断言**：它会以真实夹具**执行**该用例，
把每个参数化实例发出的**全部 GET 请求**的状态码、实例数与每实例请求数，
与这里声明的期望比对。所以「文档说 200、用例其实已经是 500」或请求被短路/实例被删，
都会变成一次红灯，而不是一句无人核对的散文。

投喂行 `test_one_bad_row_no_longer_poisons_the_listing[instances=1,requests=1] -> 200`
投喂行 `test_ten_legal_rows_plus_one_bad_row_all_eleven_are_returned[instances=1,requests=1] -> 200`
投喂行 `test_one_bad_error_code_no_longer_poisons_the_listing[instances=3,requests=1] -> 200`
投喂行 `test_a_bad_report_text_no_longer_breaks_the_detail_endpoint[instances=3,requests=1] -> 200`
投喂行 `test_a_bad_report_text_row_does_not_poison_its_page[instances=1,requests=2] -> 200`
投喂行 `test_the_listing_is_unaffected_by_a_bad_report_text[instances=1,requests=2] -> 200`
投喂行 `test_a_null_report_text_row_is_unaffected[instances=1,requests=2] -> 200`
投喂行 `test_a_healthy_history_is_unaffected[instances=1,requests=2] -> 200`

### 遗留：`tests/test_errors.py` 仍是兜底 handler 的守卫

修掉 `:312` 之后，"坏 `report_text` → 抛 `ValidationError` → 兜底 handler 翻 500"
这条**触发路径消失了**。于是 task-37 的 `test_fallback_handler_leaks_no_paths`
（它当年靠这条路实测"`ValidationError` 确实被 `@app.exception_handler(Exception)` 接住"
+ "响应体不泄漏本机路径"）**不再有可测对象**。

**处理方式与理由**（判断留痕，避免后人误以为"改绿了就行"）：
- 该属性由 **`tests/test_errors.py`** 独立覆盖 —— 它用一条专门抛 `RuntimeError` 的动态
  路由打真实 500，并断言 `"D:" not in detail` / `"个人项目01" not in detail`，
  还断言了 `ServerErrorMiddleware` 上挂的是 `unexpected_exception_handler` 而非
  `app_error_handler`（混用会静默退化成空 body 500）。**所以不需要本文件替它兜底测试。**
- 本文件里那条用例**改名**为 `test_the_report_path_no_longer_reaches_the_fallback_handler`，
  只钉**新契约**（200 + `report is None` + 响应体无 500 时代产物），
  **不假装**它还在测兜底 handler。
- 它现在的作用是**回归哨兵**：谁再把 `_parse_report` 的 `except` 去掉，它就红（见探针 M268）。

---

## ⑥ 方法学：这次取证踩过的坑

留在这里，避免下一个人重踩。

### 坑 1（最重要）：变异 / 还原**必须用字节级 I/O**

```python
# ✗ 错：文本模式不是字节保真的
src = path.read_text()
path.write_text(src)          # 整文件行尾被改写！

# ✓ 对：字节级，并以 HEAD 为还原基准
original = path.read_bytes()
mutated  = original.replace(old, new, 1)
path.write_bytes(mutated)
# ...跑测试...
restored = subprocess.run(["git", "show", "HEAD:app/api/routes_workflows.py"]).stdout
path.write_bytes(restored)
assert path.read_bytes() == restored, "还原不是逐字节一致，立即中止"
```

**实测教训**：本仓库 `app/api/routes_workflows.py` 是 **LF**（21439 bytes，CRLF=0，裸 LF=462）。
用 `read_text()` / `write_text()` 读回再写会把**全部 462 行转成 CRLF**，
字节数膨胀到 **21901**。一次还原校验 `assert restored == orig_bytes` 因此失败并中止
（没有留下损坏，但那一步是运气）。

**还原基准选哪个 —— 这一步选错会造成真实损失（task-38 实测撞到）**：

```python
# ✗ 危险：目标文件有**未提交**改动时，这会把你/别人的修复整个抹掉
orig = subprocess.run(["git", "show", "HEAD:app/api/routes_workflows.py"], ...).stdout

# ✓ 安全：备份**工作区当前字节**，用 try/finally 保证异常也能还原
orig = TARGET.read_bytes()
BAK = TARGET.with_suffix(".py.accbak")
BAK.write_bytes(orig)
try:
    ...                      # 改坏 -> 跑 -> 观察
finally:
    TARGET.write_bytes(orig)
assert TARGET.read_bytes() == orig      # 逐字节校验
```

task-38 时 Lead 的修复**尚未提交**：`git show HEAD:app/api/routes_workflows.py` 是
**21439 bytes 且完全不含 `_coerce_enum`**，而工作区是 **23167 bytes**。若按 HEAD 还原，
修复会被无声抹掉。**判据：先跑 `git status --short <目标文件>`，只要它不是干净的，
就绝对不能拿 HEAD 当还原基准。**

两种基准的可取之处仍不同：`git show HEAD:` 的好处是**即使脚本中途崩了也能一键还原**
（文件处于已知状态）；取工作区字节则需要 `try/finally` 兜底。**工程上最稳的是两者都用**：
先把工作区字节备份到 `.accbak`（防崩），再在 `finally` 里还原并逐字节校验。

### 坑 2：`subprocess` 跑 pytest 要显式指定 `encoding="utf-8", errors="replace"`

子进程输出是 UTF-8，父进程默认按 **GBK** 解码。一旦遇到不能解码的字节，
`subprocess` 的 `_readerthread` 会抛 `UnicodeDecodeError` **死掉**，
`proc.stdout` 变成 `None`，下一行 `proc.stdout + proc.stderr` 直接
`TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'`。
⚠️ 这个失败**发生在"改坏"之后、"还原"之前**，会把产品文件**留在坏状态**。
所以变异脚本必须用 `try / finally` 还原（见坑 1 的 code block）。

### 坑 3：`subprocess` 跑 pytest 要传**完整环境**

传最小 env 会制造两个假象（都不是产品问题）：
- 缺 `SystemRoot` / `ComSpec` → `subprocess` 起 `git` 时
  `FileNotFoundError: [WinError 2] 系统找不到指定的文件`；
- 缺 `USERNAME` / `USER` → `tests/test_errors.py:213` 的
  `pytest.skip("环境里没有 USERNAME/USER：用户名清洗分支本就不生效，无需断言")` 触发，
  总数从 `728 passed` 变成 `727 passed + 1 skipped`。

**环境缺陷不该伪装成产品缺陷**，反之亦然。看到 `skipped` 先查是不是 env 造出来的。

### 坑 4：作探针的脚本必须**幂等**

第一次跑完 `.tmp/acc/repro_db/app.db` 就存在了，第二次跑会
`sqlite3.IntegrityError: UNIQUE constraint failed: workflow_history.workflow_id`
（固定 workflow_id 直插）。脚本开头 `shutil.rmtree(WORK)` 一下。

### 坑 5：PowerShell 里 `2>$null` 而不是 `2>/dev/null`

`2>/dev/null` 会被当成路径 `D:\dev\null` 报 `OpenError`。

### 坑 6：不要用 `Get-Content` / `Out-String` 数行或扫字节

PowerShell 会重组行尾，数出来的行数与字节都不真实。
要数行：`path.read_bytes().count(b"\n")`。

### 探针有效性（本任务自证）

探针必须能**变红**，否则"通过"没有信息量。我对 `:237` 做了 2 个变异
（`.tmp/acc/p3_probe_validity.py`，字节级 I/O + `git show HEAD:` 还原基准）：

| 变异 | 改法 | 基线 | 改坏 | 还原校验 | 复跑 |
|---|---|---|---|---|---|
| **M1** 详情不再解析 `report_text` | `FinalReport.model_validate_json(record.report_text) if ... else None` → `None` | 41 passed | **7 failed, 34 passed** | 与 HEAD 逐字节一致 True（21439 B） | 41 passed |
| **M2** 详情吞掉解析异常（模拟"只兜底解析失败"的部分修复） | 同上 → `_parse_report_lenient(record.report_text)`（try/except → None） | 41 passed | **15 failed, 26 passed** | 与 HEAD 逐字节一致 True（21439 B） | 41 passed |

M1 的 7 条红线（全是本任务新增的 `:237` 用例）：
```
test_a_bad_report_text_breaks_the_detail_endpoint[not-json]
test_a_bad_report_text_breaks_the_detail_endpoint[title-not-a-string]
test_a_bad_report_text_breaks_the_detail_endpoint[unknown-field-only]
test_an_empty_string_report_text_is_also_poison
test_the_listing_is_unaffected_by_a_bad_report_text
test_a_healthy_report_still_round_trips_through_the_detail_endpoint
test_fallback_handler_leaks_no_paths
```

**结论：基线 0 红线 → 变异必红 → 还原后逐字节回到 HEAD 且复跑全绿。**
说明这些断言真的在测 `:237`，不是空转。

⚠️ M2 的 15 条红线里多数**不是** `:237` 的用例 —— 但成因不是"辅助函数改变了源码形状"
这种含糊说法。已定位到确切机制，**而且它是我的变异脚本自身的缺陷**：
M2 把 `_parse_report_lenient` 插在了 `def get_history_detail` 的**锚点前**，
而那个位置正好夹在装饰器 `@router.get("/workflows/history/{workflow_id}")`（改后第 208-212 行）
与被装饰函数之间 —— 装饰器于是绑到了 `_parse_report_lenient(raw)` 上，
`raw` 变成**必填 query 参数**，详情接口整体返回
`422 BAD_REQUEST "Field required: loc ('query','raw')"`。
那 15 条红线是**路由绑定被破坏**的副产物，与产品行为无关。
**教训：往被装饰函数前插入 helper 是危险变异手法 —— 会静默改写装饰器绑定。
干净的做法是把 helper 定义在文件更靠前的模块级位置（如 import 之后），
或干脆内联成 try/except 而不新增函数。**
看 M1 的 7 条才是干净信号。

---

### 探针有效性（task-38 复证：缺陷 1/2 修复之后）

Lead 修完 `status` / `error_code` 之后，测试的期望值被翻成 200。
**翻完之后必须重新证明探针还活着** —— 否则"42 passed"可能只是断言写松了。
做法：把修复**逐处拆掉**（`.tmp/acc2/prove_r38.py`，字节级 I/O + 工作区字节备份 + `try/finally`）。

| 变异 | 改法 | 基线 | 改坏 | 红线 |
|---|---|---|---|---|
| **M154** | 只把 `:181` 换回 `WorkflowStatus(record.status)` 裸硬转 | 42 passed | rc=1 | **17 failed** |
| **M159** | 只把 `:187` 换回 `ErrorCode(record.error_code)` 裸硬转 | 42 passed | rc=1 | **12 failed** |
| **MBOTH** | 两处都换回裸硬转（= 回到修复前） | 42 passed | rc=1 | **27 failed** |
| **MFIELD** | 只把 `HistoryItem.status` 的 `\| str` 收回去（模拟"只改构造侧"） | 42 passed | rc=1 | **16 failed** |

```
还原与工作区原始字节逐字节一致 -> True（23167 bytes）
复跑 rc=0 | 42 passed
总结论 -> 全部变异被我方用例抓住
```

三个有价值的读数：
1. **M154（17）与 M159（12）红线集合不同** —— 说明两处**各有独立覆盖**，
   不存在"只测了其中一处"的假覆盖。
2. **MFIELD（16 条）单独变红** —— 证明「只加 `_coerce_enum` 不放开字段类型」
   这个**真实发生过的半吊子修复**会被测试抓住，不会溜过去。
3. **MBOTH = 27 < 17 + 12 = 29** —— 有两条用例（双坏行那两条）在"两处都坏"时
   只贡献一次失败，因为它们断言的是**整体可用**（200）而不是逐字段。
   这是设计使然，不是遗漏。

---

### 探针有效性（task-38 终局自证：三处全修、期望值全翻成 200 之后）

⚠️ 这一段是**最重要的**：期望值从 500 翻成 200 之后，必须重新证明探针**还活着**——
否则"44 passed"可能只是断言写松了。做法是把三处修复**逐个拆掉**，
`.tmp/acc2/prove_r38b.py`（字节级 I/O + 工作区字节备份 + `try/finally`）。

| 变异 | 改法 | 红线数 |
|---|---|---|
| **M154** | 只把 `:207` 换回 `WorkflowStatus(record.status)` 裸硬转 | **17 failed** / 27 passed |
| **M159** | 只把 `:212-216` 换回 `ErrorCode(record.error_code)` 裸硬转 | **12 failed** / 32 passed |
| **M268** | 把 `:294` 换回 `FinalReport.model_validate_json(...)` 裸硬转 | **8 failed** / 36 passed |
| **MBOTH** | 两处枚举同时换回裸硬转（= 回到修复前） | **27 failed** / 17 passed |
| **MFIELD** | 只把 `HistoryItem.status` 的 `\| str` 收回去（模拟半吊子修复） | **16 failed** / 28 passed |

```
BASELINE rc=0 | 44 passed in 2.45s
（每个变异：改坏 -> 红线 -> 还原逐字节一致 True（24578 bytes）-> 复跑 44 passed）
总结论 -> 全部变异被我方用例抓住
```

**M268 的 8 条红线**（这是 Lead 点名要补的第五个变异，全部落在报告路径上）：
```
test_a_bad_report_text_no_longer_breaks_the_detail_endpoint[not-json]
test_a_bad_report_text_no_longer_breaks_the_detail_endpoint[title-not-a-string]
test_a_bad_report_text_no_longer_breaks_the_detail_endpoint[unknown-field-only]
test_a_bad_report_text_is_logged_not_swallowed
test_an_empty_string_report_text_is_also_poison
test_the_listing_is_unaffected_by_a_bad_report_text
test_a_bad_report_text_row_does_not_poison_its_page
test_the_report_path_no_longer_reaches_the_fallback_handler
```

★ **M268 恰好红了 8 条 `report_text` 用例，而 M154/M159 一条都没碰上** ——
说明三处修复**各有独立的用例覆盖**，没有哪一处是"靠别处的用例顺带过"的。

---

## ⑦ 防腐烂：`scripts/check_docs.py`

**护栏本身现由 pytest 自动化守护**：`tests/test_check_docs.py` 在隔离镜像里注入变异，
覆盖文档漂移、用例改名、真实状态回归、实例/请求计数缩水及执行异常报告；
变异不写入工作树里的真实源文件。镜像位于仓库级 `.tmp/task40_mirror/`，而非 `tmp_path`：
本机 `tempfile` 会退回 cwd，且从 `tests/` 启动时相对 `--basetemp` 可能将 `tmp_path`
落入 `tests/` 子目录。源码与文档 SHA-256 证明排除临时目录、pytest 缓存与字节码等运行产物。

本文是交接文档，但它靠**大量「代码第 NNN 行是表达式 Z」「用例 X 钉住行为 Y」
「期望值 200」**来讲故事 —— 这些断言**无法靠人肉长期复核**。代码一改，
行号漂移、用例改名、断言方向翻转，文档就开始骗人，而读它的人
（接手人 / 面试官）没有理由怀疑它。

`scripts/check_docs.py` 把这些断言变成**可执行的检查**：

```
& ".\.venv\Scripts\python.exe" scripts\check_docs.py
```

退出码 `0` = 全部对得上；`1` = 有对不上的条目（逐条打印）。

### 它核对哪三个维度

| 维度 | 做法 | 能抓住什么 |
|---|---|---|
| **① 行号锚点** | 本文「行号锚点」块里的 `` `文件.py:NNN` ``，必须与**代码里自己声明的**锚点注释（`` # **:NNN** `<该行原文>` ``）落在同一行 | 代码改了一行、注释/文档没跟上 |
| **② 用例存在性** | 文档提到或被锚点标注的每个 `test_*`，必须在 `tests/` 里真实存在（用 `ast` 收集，**不扫文本**） | 用例被改名/删除，文档还在引用它 |
| **③ 断言方向** | 文档「断言方向」块写明 `<用例名[instances=N,requests=M] -> 200`，脚本在隔离执行上下文中调用该用例，逐实例核对实例数、每实例请求数及所有请求状态码（不是 pytest fixture 注入） | 文档期望与实际实例数、请求数或状态码不一致 |

维度 ③ 的采集钩子是 `tests/test_history_robustness.py` 里的
`_record_acceptance_status(func)`：它只**观察**不改行为，现会记录每个参数化实例
发出的全部 GET 状态码；投喂行同时声明参数实例数与每实例请求数，脚本逐项核对。
若用例断言失败或依赖 pytest fixture 而无法直接调用，脚本将返回一条 `[用例执行]`
Problem、退出码 1，不会把裸 traceback 当作有效的核对结果。

### 它**不**保证什么（诚实边界）

- 只核对文档里**写了**的东西。文档漏写的属性（某个坏行不再返回、
  某个字段被悄悄丢掉）它一无所知 —— **它防腐烂，不防遗漏。**
- 断言方向投喂仅支持无 pytest fixture 参数的用例（可含 `parametrize`）；
  fixture 型用例不会被注入 fixture，而是被干净地报告为 `[用例执行]`，因此不应列为投喂对象。
- 请求计数针对被采集钩子拦截到的 `TestClient.get` 调用；它不是通用 HTTP 流量记录器。
- 不核对散文：判断性叙述（「为什么修」「影响面多大」）无法机械复核。
- 只要代码锚点与文档引用**同谋地一起改错**，它看不出来（两者一致它就绿）。
  **它防的是单边漂移。**

### 探针有效性（task-39 历史记录：3 种漂移各注入一次）

一个"守护哨兵"如果对任何改坏都无反应，它就是安慰剂。三类漂移各注入一次，
`.tmp/prove_check_docs.py`（**字节级 I/O** + 工作区字节备份 + `try/finally`）：

```
BASELINE rc=0 | 文档与仓库一致：行号锚点 7 条（代码锚点 7 个）、
                用例 23 个全部存在、断言方向 12 个参数化实例（16 个请求）
```

> 上面是 task-39 当时的历史快照（23 个测试用例）。当前 `scripts/check_docs.py`
> 的运行结果另计：24 个 `test_*` 名称全部存在，12 个参数化实例、16 个 GET 请求；
> 新增的 `tests/test_check_docs.py` 不改变这里 8 条投喂用例的实测计数。

=== M1 代码锚点行号漂移一位（:215 -> :216）===
  改坏 rc=1 | 不一致 2 条：   ← 代码锚点自校验 + 文档对不上，双响
  还原逐字节一致 -> True（26310 bytes）；复跑 rc=0
=== M2 文档锚点行号漂移一位（:215 -> :216）===
  改坏 rc=1 | 不一致 1 条：
    [行号漂移] docs/HISTORY_ROBUSTNESS.md:401 声明
      `app/api/routes_workflows.py:216` 是 'status=_coerce_enum(...)'，
      实际是 'created_at=record.created_at,'
  还原逐字节一致 -> True（39908 bytes）；复跑 rc=0
=== M3 文档期望值 200 改成 500 ===
  改坏 rc=1 | 不一致 1 条：
    [断言方向] 文档期望 500，实测 200
  还原逐字节一致 -> True；复跑 rc=0
=== M4 产品代码退回 WorkflowStatus(record.status) ===
  改坏 rc=1 | [用例执行] AssertionError: 期望 200，实际 500：'RUNNING' is not a valid WorkflowStatus
  还原逐字节一致；复跑 rc=0
=== M5 投喂用例断言改成必假 ===
  改坏 rc=1 | [用例执行] AssertionError: TASK40_FORCED_ASSERTION_FAILURE
  还原逐字节一致；复跑 rc=0
=== M6 参数实例 3 -> 2 ===
  改坏 rc=1 | [断言方向] 文档声明 3 个实例，实测 2 个
  还原逐字节一致；复跑 rc=0
=== M7 两请求用例短路为一请求 ===
  改坏 rc=1 | [断言方向] 文档声明每实例 2 个请求，实测 1 个
  还原逐字节一致；复跑 rc=0
```

上述变异全部抓住，并要求对应 Problem 类型/关键错误信息；镜像副本恢复后再次运行 rc=0。
实例计数对账修复了参数化实例 3→2 的假绿；请求计数对账修复了“仅记录首个请求”导致的部分请求假绿。Fixture 参数用例如被投喂会以 `[用例执行] TypeError` 干净失败；因此选作维度 3 投喂的用例必须不依赖 pytest fixture。


---

## 附：本文相关文件

| 路径 | 作用 |
|---|---|
| `tests/test_history_robustness.py` | **44 条**用例：task-35 16 + task-36 16 + task-37 9 + task-38 3；三处缺陷**全部**已翻成 200 降级契约 |
| `.tmp/acc/repro_p3.py` | ④ 的最小复现脚本（**不在 git 里**，是临时脚本） |
| `.tmp/acc/p3_probe_validity.py` | 缺陷 3 的变异 + 逐字节还原校验脚本（**不在 git 里**，task-38 时已过时） |
| `.tmp/acc2/prove_r38.py` | 第一轮探针（M154/M159/MBOTH/MFIELD，42 条时代） |
| `.tmp/acc2/prove_r38b.py` | **终局探针**（五个变异，44 条时代，工作区字节还原） |
| `.tmp/acc2/rename_stale.py` | 把过时用例名批量改成正向命名的字节级脚本（**不在 git 里**） |
| `scripts/check_docs.py` | ⑦ 防腐烂核对脚本：行号锚点 / 用例存在性 / 断言方向三个维度（**在 git 里**） |
| `.tmp/prove_check_docs.py` | ⑦ 的探针有效性证明（3 种漂移注入 + 逐字节还原，**不在 git 里**） |
| `app/api/routes_workflows.py` | 三处修复全在此：`:155` `_coerce_enum` / `:178` `_parse_report` / 调用点 `:215` `:223` `:312` |
| `app/main.py` | 兜底 handler（把未预期异常翻成 500），`sanitize_detail(str(exc))[:500]` |
| `tests/test_errors.py` | 兜底 handler 的**独立**守卫（`unexpected_exception_handler` + 路径不泄漏） |
| `app/data/history.py` | 数据层，对毒行完全正常，**不需要改** |


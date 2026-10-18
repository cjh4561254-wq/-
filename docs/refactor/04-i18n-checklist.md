# 04 · 全中文化核对清单（Wave A 产出 → Wave B 唯一依据）

> **本文档只读产出**：Wave A 未修改 `app/` 下任何文件，`app.js` / `index.html` / `login.*` 的改动全部留给 Wave B。
> **硬边界**：不允许为了"看起来中文化"去改后端返回的真实字段名、枚举值、错误码。那会同时打破接口契约与 `tests/` 里逐条核对枚举的用例。
> **取证方式**：`PYTHONPATH=. .venv/Scripts/python.exe` + `starlette.testclient.TestClient(app)` 真实调用（登录 → 上传 → 画像 → 分析 → 轮询 → 报告 → 历史 → 各种错误响应），响应体原文见 §9。探针脚本跑完已删除，未留在仓库。

---

## 0. 一页结论

| 类别 | 本质 | 处理方式 | 数量 |
|---|---|---|---|
| **(a) 纯 UI 文案直译** | 前端自己写的英文句子 | 逐条换成中文（本文给措辞） | **12 处**（不是 7 处）|
| **(a′) 英文品牌名 / 品牌主张句** | 有意设计的品牌资产，不是漏翻 | **等 Lead 决策**，不由中文化任务顺手改 | 12 处里的 **6 处** |
| **(b) 后端枚举与技术名** | 值本身必须保持英文，展示层要中文 | 中文主显 + 原值可查证（`title` / 次要小字），并**堵住 `|| 原值` 兜底** | 8 组上屏 + 5 组不上屏 |
| **(c) 用户数据集列名** | 用户的真实数据，翻错比不翻更糟 | 判定树：原样 / 查表 / 确定性格式化，**禁止机器翻译** | 4 个渲染入口 |
| **(d) 错误码是否要译** | 后端 `message` 已是中文，`code` 只是标识 | **保留** `app.js:10-11` 的规定，但补一条呈现契约；真正要改的是 `error.detail` | 1 个真漏点 |

**最大的三处英文暴露，都不在已知的 7 条里**（这是本轮复核最重要的结论）：

1. `app.js:2504` 轮询状态行：`"状态 " + data.status + " · 步骤 " + data.step` —— **没有过任何映射表**，每秒把 `SUCCESS` / `EXECUTING` / `done` / `profiling` 直接画到屏幕上。实测渲染结果：`状态 SUCCESS · 步骤 done（6/6）`、`状态 PENDING · 步骤 profiling（0/6）`。
2. `app.js:2797` `renderTable` 把 `Object.keys(rows[0])` 当表头 —— 实测表头是 `region` / `sales_amount_sum`（后者还是我们工具自己拼出来的派生列，见 §5）。
3. `app.js:405` 错误框无条件渲染 `error.detail` —— 422 的 detail 是 pydantic 英文原文，500 的是 traceback，`routes_workflows.py:396` 的 detail 里还夹着裸枚举值。实测见 §9。

其余按 `|| 原值` 兜底会漏英文的三处（`app.js:2427`、`:2655`、`:2917`）与已知清单一致，本文给统一兜底策略。

---

## 1. Wave B 动手前必须知道的"会红"清单

这些是 `tests/` 里逐字钉住的字符串/结构，**改了就红**。列在这里是为了让 Wave B 不必试错。

| # | 钉子 | 位置 | 对 Wave B 的约束 |
|---|---|---|---|
| N1 | `app/web/` 文件名单**全等**白名单：`app.css, app.js, index.html, login.css, login.html, login.js, tokens.css` | `tests/test_frontend.py:342-380` | **不能新增 `i18n.js` / `locales.js`**。映射表必须写在 `app.js`、`login.js` 各自文件内（重复的公共项按 §7 各留一份）|
| N2 | `const STEP_ORDER = [...]` 解析出来的 6 个值必须与 `app/agents/orchestrator.py:48 _STEPS` **逐字全等** | `tests/test_frontend.py:228-243` | 后端 `WorkflowStep` 其实有 **7 个值**（多了 `done`，`app/schemas_workflow.py:91-99`）。**不许把 `done` 加进 `STEP_ORDER`**，只加进中文映射表 |
| N3 | `STEP_LABELS` 六条"正在…"文案必须存在 | `tests/test_frontend.py:214-225`、`:245-247` | 可以**新增键**（如 `done`），不能删/改这六条中文 |
| N4 | `const TERMINAL = [...]` 那一行必须同时含三个终态码 | `tests/test_frontend.py:250-262` | 只加映射表，**不动 TERMINAL/RETRYABLE 的字面量** |
| N5 | `FILE_TOO_LARGE` / `UNSUPPORTED_FILE` / `SERVICE_UNAVAILABLE` 等码必须在 `app.js` 里以字面量出现 | `tests/test_frontend.py:272-283` | 中文映射表的**键**就是这些字面量，天然满足；不许把键改成中文 |
| N6 | API 路径字面量（含 `/api/datasets/${id}/profile`）必须原样出现在 `app.js` | `tests/test_frontend.py:199-211` | 不动 `API` 对象 |
| N7 | `app.js` / `index.html` / `app.css` 里不许出现 `http://` `https://`（注释也算） | `tests/test_frontend.py:139-149` | 新增注释/映射表里别写绝对网址 |
| N8 | 不许按 HTTP 状态码判断业务成败（`resp.ok` / `status === 200` / `status_code` 三个串不能出现） | `tests/test_frontend.py:285-295` | 兜底函数别顺手写状态码分支 |
| N9 | `index.html` 必须含"接口文档"四个字 | `tests/test_frontend.py:454` | `(a)` 里统一术语为「接口文档」正好与它一致；**不能反过来统一成「API 文档」** |
| N10 | 17 个必备元素 id（`REQUIRED_IDS`）必须还在 `index.html` | `tests/test_frontend.py:101-112` | 加中文标签时不要顺手删元素 |
| N11 | 页面数字/状态必须来自后端真实字段，拿不到写"尚未获取" | `app.js:16-19` 文件头 | 兜底中文**不能说谎**："未识别的状态（原值 EXECUTING）"可以，"已完成"不行 |
| N12 | 报告 `summary` 首段是后端诚实标记原文，且测试禁止其中出现 `字段名=值` | `tests/test_report_notice_has_no_internal_field_names.py:40-45,122-127` | `app.js:2832` 那句**照原样显示**，前端不做二次润色 |

好消息：`tests/` 里**没有**任何用例断言 `STEP_STATE_TEXT` / `TASK_STATUS_TEXT` / `CHART_TYPE_TEXT` / `historyStatusText` / `SRC_TEXT` / `"Market Intelligence Workspace"` / `"Turn Market Data Into Decisions."` / `login.html` 的品牌文案（已 grep 全量确认；命中的只有 `docs/`）。所以 (b)(c) 的映射表与 (a′) 的品牌串在**技术上都可改**，(a′) 不改的理由是设计决策，见 §3。

---

## 2. (a) 纯 UI 文案直译 —— 复核后 12 处

复核范围：`app/web/` 全部七个文件（`.css` 三个只用 `content:` 输出 `▲▼` 符号，无文字，已排除）。除用户可见正文外，还单独扫了 `title` / `placeholder` / `aria-label` / `document.title` / JS 拼接串 / CSS 生成内容 / `<option>` / SVG `<text>`。

### A-1 表：可直接照抄的纯文案（6 处，Wave B 无需等决策）

| # | 位置 | 原文 | 建议中文 | 措辞理由 |
|---|---|---|---|---|
| A1 | `index.html:207` | `Market Intelligence Workspace` | 市场情报工作台 | 与面包屑「工作台」（`VIEWS.workbench.crumb`）同一个词，不引入第二套叫法 |
| A2 | `app.js:120` | `title: "Market Intelligence Workspace"` | `title: "市场情报工作台"` | 与 A1 逐字一致（H1 初值与 JS 写入值必须同串，否则首屏闪一下英文） |
| A3 | `index.html:648` | `<h3 class="sub-title">API 文档</h3>` | `接口文档` | 全站已有 4 处「接口文档」（`index.html:119`、`:649` 语义、`login.html:302`、`tests/test_frontend.py:454` 钉子），`API 文档` 是唯一异类；N9 决定只能往「接口文档」统一 |
| A4 | **`index.html:51`（新增）** | `本页依赖 JavaScript 运行；当前浏览器禁用了脚本，因此无法确认登录状态。` | `本页需要浏览器脚本才能运行；当前浏览器禁用了脚本，因此无法确认登录状态。` | 同句后半已经是中文口径"禁用了脚本"，前半却用技术名，自相矛盾；改后整句同一说法 |
| A5 | `login.html:318` | `本页需要 JavaScript` | `本页需要浏览器脚本` | 与 A4 同措辞（原来记的":319 ×2"实际跨 318/319 两行） |
| A6 | `login.html:319` | `……请在启用 JavaScript 的浏览器中打开。` | `……请在允许运行脚本的浏览器中打开本页。` | 同上 |

### A-2 表：英文品牌名与主张句（6 处）→ **归 (a′)，等 Lead 拍板，Wave B 先不动**

| # | 位置 | 原文 | 性质 |
|---|---|---|---|
| A7 | `index.html:98` | 可见文本 + `title="AI Market Intelligence Studio"`（同元素两处） | 顶栏英文品牌行 |
| A8 | `login.html:46` | `AI Market Intelligence Studio`（`.brand__product`） | 登录页英文品牌行，下一行 `:47` 就是中文副名 |
| A9 | `login.html:8` | `<title>登录 · AI Market Intelligence Studio</title>` | `<title>` 里的品牌串 |
| A10 | `login.js:480` | `"首次部署 · AI Market Intelligence Studio"`（**`document.title`**，已知清单漏掉的类型） | 同上 |
| A11 | `login.js:481` | `"登录 · AI Market Intelligence Studio"`（**`document.title`**） | 同上 |
| A12 | `login.html:56` | `Turn Market Data Into Decisions.`（H1 主张句） | 品牌 slogan |

**为什么这 6 处不是"漏翻"**：`docs/design-system.md:318` 有一条显式规则 —— "中英不混排成一行：品牌区用两行，英文名 + 中文副名"，`docs/design-research.md:151/543` 又规定登录页 H1 必须承担主张句 `Turn Market Data Into Decisions.`，`docs/final-report.md:298` 还记录了"验收时 /login 逐字含该句"。也就是说这 6 处是**上一阶段的设计决策落地**，不是疏忽。

**给 Lead 的两个选项（本文按 A 方案写映射，B 方案只需删元素）**：

- **A（推荐，成本低）**：保留英文品牌行与主张句，但确认"中文是主显"：`index.html:98/99` 与 `login.html:46/47` 已经是英文小字 + 中文主字的双行结构，只需在 CSS 上把中文行的字号提到主级、英文降为 `--fs-xs` 次级；`<title>` / `document.title`（A9-A11）改成 `登录 · AI 市场情报与智能分析平台`，英文串不进标签页（标签页宽度会截断，中文更易认）。
- **B（彻底全中文）**：删掉 A7/A8 两行英文元素、A12 换成中文主张句「让市场数据变成可核对的决策。」、A9-A11 同 A 方案。**代价**：要同步改 `docs/design-system.md:318` 与 `docs/design-research.md:151/543` 两条已定稿规则，且 `docs/final-report.md:298` 的验收记录要补一段"变更说明"，否则文档开始骗人。`tests/` 里没有钉子，所以不会红 —— 这是纯粹的产品决策。

### A-3 复核确认"不算英文暴露"的项（Wave B 别顺手改）

| 位置 | 串 | 判定 |
|---|---|---|
| `index.html:244`、`app.js:1359/1755` | `.csv / .xlsx`、`CSV / XLSX` | 文件格式名，是用户拿去上传时的检索词，翻译等于帮倒忙 |
| `index.html:645` | `<code>.env.example</code>` | 真实文件名 |
| `index.html:304`(`login.html`) | `/app` | 真实路由 |
| `app.js:370/402`、`login.js:755/781` | `HTTP` | 协议名，且都在中文句子里当限定语 |
| `app.js:3334` | `原始时间戳（UTC）` | 时区标识，去掉会造成口径丢失 |
| `app.js:2068 SRC_TEXT.ai` | `AI 解释` | 来源标签，与「大模型撰写」是两类口径 |
| `login.js:1046-1047` | `Retry-After` | HTTP 响应头名，且句子主显是中文（"秒数取自服务端 Retry-After 响应头"）；保留 |
| `login.html:191/232` 等 `data-hint-*` | 全中文 | 已合规 |
| `index.html` 全部 `aria-label` / `title` | 全中文 | 已合规 |
| `app.css:1167-1168` `content:` | `▲` `▼` | 符号非文字 |
| `document.title`（app.js） | 无写入 | `index.html:7` 的中文 `<title>` 是唯一来源，已合规 |

---

## 3. (b) 后端枚举与技术名 —— 全量清单 + 兜底策略

### B-0 兜底策略（统一规定，覆盖 `:2427`、`:2655`、`:2917` 三处 `|| 原值`）

原则：**中文主显 + 原值可查证，且不许静默把英文原值放到主显位**。

- **命中映射** → 主显中文；原值进 `title="服务端原值：EXECUTING"`（徽章、行内小字一律 `title`）。
- **未命中映射** → 主显 `未识别的<标签>`（例：`未识别的状态`）；原值放**同节点的次要小字** `<span class="origin">EXECUTING</span>`，并在 `title` 里重复一次。为什么原值必须**可见**而不只是 `title`：N11 要求界面不得凭空说"成功"，用户/排查者要能看见"后端到底给了什么"；而触屏设备没有 hover，只放 `title` 等于把信息藏起来。
- `.origin` 的样式（Wave B 在 `app.css` 加一条）：`font-family:var(--font-mono); font-size:var(--fs-xs); color:var(--n-500);` —— 明确"这是技术标识，不是正文"。
- 未命中**不许**回落到"猜一个中文"（`EXECUTING` 猜成"计算中"就是撒谎，`VALIDATING` 也一样）。
- 现成的正确范式已经在代码里，Wave B 照抄它的形状即可：`app.js:3381`（`服务状态` 行，中文主显 + `cell.title = "服务端原始值：healthy"`）与 `app.js:3323-3334`（创建时间主显本地时间、`title` 放 UTC 原值）。

实现上用一个公共函数，避免每处各写一份兜底（键仍是后端字面量，满足 N5）：

```js
function zhEnum(map, raw, label) {
  var known = Object.prototype.hasOwnProperty.call(map, raw);
  return {
    text: known ? map[raw] : "未识别的" + label,   // 主显永远是中文
    origin: known ? null : String(raw),            // 只有不认识时才把原值挤上正文
    raw: String(raw),                              // 进 title，永远可查证
  };
}
```

### B-1 表：会上屏的 8 组

| # | 枚举/技术名 | 来源（后端） | 值 | 现有中文映射 | 现有上屏点 | 兜底是否漏英文 | 处置 |
|---|---|---|---|---|---|---|---|
| E1 | 工作流状态 `WorkflowStatus` | `app/schemas_workflow.py:64-81` | 10：`PENDING`/`PROFILING`/`PLANNING`/`EXECUTING`/`CHARTING`/`REPORTING`/`VALIDATING`/`SUCCESS`/`SUCCESS_WITH_WARNINGS`/`FAILED` | `app.js:2902-2916`（10 值齐全） | `:2928` 历史行、`:3068` 计数行、`:3322` 归档详情、**`:2504` 轮询状态行** | `:2917` `|| String(status)` 会漏；**`:2504` 根本没查表** | 补映射 + **`:2504` 改走 `historyStatusText`** |
| E2 | 阶段名 `WorkflowStep` | `app/schemas_workflow.py:91-99` | **7**（6 个阶段 + `done`） | `STEP_NAMES`(`app.js:77`) / `STEP_LABELS`(`:64`) 各 6 值，**都没有 `done`** | `:2340`（有 `|| step` 兜底）、**`:2504` 步骤名**（裸值） | 是：实测终态时画成 `步骤 done` | 两张表各加 `done`；`:2504` 走 `STEP_NAMES` |
| E3 | 阶段状态 | 前端自造（`:88-94`，5 值） | `waiting`/`active`/`done`/`failed`/`skipped` | `STEP_STATE_TEXT` | `:2341/2389/2393/2399/2404` | 否（全是表内取值） | 不动 |
| E4 | 任务状态 `TaskStatus` | `app/schemas_workflow.py:48-61` | 6：`PENDING`/`RUNNING`/`SUCCESS`/`FAILED`/`RETRYING`/`SKIPPED` | `app.js:98-105` | `:2427` | **是**：`|| task.status` | 换 `zhEnum` |
| E5 | 图型 `ChartType` | `app/schemas_tool.py:52` | 4：`bar`/`line`/`pie`/`scatter` | `app.js:109-114` | `:2655` | **是**：`|| chart.chart_type` | 换 `zhEnum` |
| E6 | 校验问题码 `issue.code` | `app/agents/validation.py:732,798,836,927,1142,1187` | 6：`FAILED_TASK`/`EMPTY_RESULT`/`MISSING_CHART_FILE`/`UNSUPPORTED_NUMBER`/`UNKNOWN_COLUMN`/`SHORT_REPORT` | **无表** | `:2764`、`:2784`（`issue.code + "：" + issue.message`） | 是（code 在主显位开头） | 新增 `VALIDATION_ISSUE_TEXT`；主显改中文，code 降到 `.origin`/`title`（message 本来就是后端中文，不动） |
| E7 | 校验问题级别 `severity` | `app/schemas_workflow.py:161` | 3：`error`/`warning`/`info` | 无 | **当前不上屏**（`:2761/:2781` 只用来筛选） | 否 | 不建映射表（建了就是死代码）；⚠️ 顺带记录：`info` 级 issue **现在被静默丢弃**，与中文化无关但值得 Lead 知道 |
| E8 | pandas dtype | 后端 `Dataset.dtypes` / `ColumnStat.dtype`（实测值 `str`/`int64/float64`） | 开放集 | **无表** | **`app.js:1974`** `name + "（" + dtype + "）"` | 是（`object`/`datetime64[ns]`/`bool` 全裸上屏） | 新增 `DTYPE_TEXT`（含常见 8 值）+ 未命中走 `zhEnum` |
| E9 | 布尔 `llm_used` | 多处（`:2825/:2893/:2932/:3326`） | `true`/`false` | 三处已中文（`srcTag`、`大模型/规则模板`） | **`:2893` `"llm_used=" + String(...)`** | 是：整串就是英文 | 改「是否用了大模型：是/否」+ `title="服务端原值：llm_used=true"` |
| E10 | 布尔 `has_report` | `:2929/:3325` | `true`/`false` | 已中文（有报告/无报告、是/否） | — | 否 | 不动 |
| E11 | 错误码 `ErrorCode` | `app/schemas.py:20-44` | 11：`BAD_REQUEST`/`NOT_FOUND`/`METHOD_NOT_ALLOWED`/`UNSUPPORTED_FILE`/`FILE_TOO_LARGE`/`SERVICE_UNAVAILABLE`/`WORKFLOW_TIMEOUT`/`INTERNAL_ERROR`/`UNAUTHORIZED`/`FORBIDDEN`/`RATE_LIMITED` | 不建表（见 (d)） | `:398/:646/:753/:1320/:1752/:1802/:1885/:3132`、`login.js:755/781/991/1004` | 半是：`[CODE]` 这种裸方括号在主显串尾部 | 保留码不译；统一成「（原值码：CODE）」并加 `title`，见 §6 |
| E12 | 失败原因码 `item.error_code` | 归档表，取值同上 | 实测 `WORKFLOW_TIMEOUT` | 无 | `:2941` `"失败原因 " + item.error_code` | 是 | 与 `ERROR_HINT`（§8 里那张**只做提示增强、不做替换**的小表）配合：中文说明在前，原码进 `.origin` |
| E13 | 健康检查 `status` / `app_name` | `app/api/routes_health.py`，实测 `healthy` / `AI Data Analyst Agent` | — | `:3381` 已中文 + `title` 原值 | `:3382` 服务名原样 | 否（`app_name` 是服务标识，属专有标识类，与文件名同级） | 不动 `app_name`；建议部署侧把 `APP_NAME` 配成中文（配置层，非契约层，`tests/test_health.py:70/181` 只与 settings 比，不钉字面值） |

### B-2 表：确认**不上屏**、因此**不要建映射**的 5 组（避免死代码）

| 组 | 值 | 为什么不上屏 |
|---|---|---|
| `semantic_type` | 5：`numeric`/`categorical`/`datetime`/`boolean`/`text`（`app/schemas_dataset.py:47`） | `grep semantic_type app/web/*.js` **零命中**；后端只在 `app/llm/prompts.py:94` 自己给 LLM 配中文标签 |
| `tool_name` | `group_aggregate` 等 | `app.js` 无引用 |
| `TERMINAL` / `RETRYABLE` / `STEP_ORDER` / `ICON_NAMES` / `CHARTS_EMPTY_TEXT` 的键 / `data-step`·`data-view`·`data-state` 属性值 | 英文 | 纯前端逻辑/选择器/DOM 契约（N2/N4/N5），翻译即坏 |
| 报告 `citations`、`task_id`、`workflow_id`、`report_id`、`dataset_id`、`filename`、`sha256` | 十六进制/技术名 | 可查证标识，中文标签已经给了（`依据：`/`任务编号`/`数据编号`/`报告编号`） |
| 图表 `x_field` / `y_field` | 实测 `region` / `sales_amount_sum` | 值就是列名 → **走 (c) 的同一个 `displayColumn()`**，不在 (b) 里另建表 |

---

## 4. (c) 列名 —— 最大的一处，方案与"禁区"

### C-0 现状与实测

`app.js:2789-2815 renderTable`：`var keys = Object.keys(rows[0]);` → `keys.forEach(function (key) { headRow.appendChild(el("th", null, key)); })`。表头**逐字等于后端 JSON 的键**。

实测（§9 证据 E）一次真实分析的表格表头：`region` | `sales_amount_sum`。而同一份用户数据如果列名是中文，后端会给出 `销售额_sum` 这种**中英混排**的键（`app/tools/aggregate.py:36` `metric_column = f"{metric}_{agg}"`，注释里举的例子正是 `销售额_sum`）。`app/tools/compare.py:41` 还会直接产出中文列名 `差值`。

同一问题的另外三个入口（Wave B 必须一起改，否则四处口径不同）：

| 入口 | 位置 | 现在怎么显示 |
|---|---|---|
| 报告/归档表格表头 | `app.js:2797` | 键原样 |
| 数据预览表头 | `app.js:1933-1935` | `data.columns` 原样 |
| 字段结构列表 | `app.js:1972-1974` | `列名（dtype）`，dtype 是英文（见 E8）|
| 缺失值 Top3 | `app.js:2018` | `列名：N 个缺失（x%）` |
| 图表横/纵轴 | `app.js:2661/2664` | `横轴 region` |

### C-1 判定树（唯一实现点 `displayColumn(rawKey)`，四处共用）

```
1. rawKey 含非 ASCII（中文/日文/…）        → 原样返回，不做任何变换
2. 命中 COLUMN_LABELS（我们工具自产的键）  → 返回表内中文
3. 命中派生后缀 _<agg>                     → 前缀递归走 1→4，后缀替换成中文括注
4. 其余：确定性格式化（只做无损的三类）     → 见 C-3
5. 任何情况下：DOM 同时保留原始键          → 见 C-4
```

**禁止清单（违反就是功能缺陷，不是风格问题）**：

- **禁止机器翻译用户列名**。没有离线词典，任何 `region → 区域` 都是猜；用户的 `region` 可能真叫"部门"。翻译错了用户无法在数据里找回那一列。
- **禁止把中文列名"顺手改成英文"**（`销售额 → amount`）—— 那是替用户改数据。
- **禁止用展示名当取值键**。`renderTable` 里 `row[key]`、`renderPreview` 里 `row[name]` 必须继续用**原始键**取值；只有 `th` 的文本和 `data-label` 变。这一条改错会画出一张全 `—` 的表。
- **禁止合并同名/相近列**，禁止重排键顺序（顺序是后端给的，重排会影响"表格里前三列就是答案"这一类隐含口径）。

### C-2 三类必须区别对待的键

| 类别 | 例子（实测/源码） | 归属 | 处置 |
|---|---|---|---|
| **用户自己的列名** | `region`、`华东`所在列、`销售额` | 用户资产 | 只走规则 1 与 4；**永不查 COLUMN_LABELS**（表里没有它） |
| **我们工具拼出来的派生列** | `sales_amount_sum`、`销售额_mean`、`count`（`aggregate.py:36` 无 metric 时） | 我们拥有词表 | 后缀替换 + 前缀按用户列名处理：`sales_amount_sum` → `sales amount（合计）`；`销售额_sum` → `销售额（合计）` |
| **我们工具自产的结构键** | `column`/`null_count`/`null_ratio`（`describe.py:70-76`）、`x`/`y`/`r`（`compare.py` pairs）、`row_count`/`group_count`、`差值` | 我们拥有词表 | 直接进 `COLUMN_LABELS`（`差值` 已是中文，表里不必再列）|

### C-3 "确定性格式化"的允许范围（无损、可逆、不猜语义）

| 规则 | 前 → 后 | 备注 |
|---|---|---|
| C3a | 全小写 snake_case → 空格分词：`sales_amount` → `sales amount` | 仅当键是纯 ASCII 且只含 `[a-z0-9_]`；含大写的键**不动**（大小写可能是用户有意的，如 `ID`/`Revenue`） |
| C3b | 尾部 `_id` → `… 编号`：`owner_id` → `owner 编号` | 当前不上屏，低优先 |
| C3c | 聚合后缀 → 中文括注：见 §8 的 `AGG_SUFFIX_TEXT`（6 个值，来自 `app/tools/registry.py:210-212` 的枚举，**不是猜的**） | `sum/mean/count/min/max/median` |
| C3d | 过长截断：> 24 字符截到 22 + `…`，完整值进 `th` 的 `title` | 与 `.dtable` 窄列/窄屏堆叠一致 |
| C3e | 空键（`""`）→ `（未命名列）`，`title` 写"后端返回的键是空字符串" | 实测 pandas 会把无名列变成 `Unnamed: 0`，那是后端的原始值，原样显示即可，不额外加工 |

不做的事：Title Case（`sales amount` → `Sales Amount`，对中文读者零收益还有害）、单复数归一、缩写展开（`amt`→`amount` 属于猜）。

### C-4 原始键与展示名在 DOM 里同时存在（与 `docs/refactor/02-structure-contract.md` §5.2 对齐）

```html
<th scope="col" data-col="sales_amount_sum" title="原始列名：sales_amount_sum">销售额（合计）</th>
<td data-col="sales_amount_sum" data-label="销售额（合计）" class="num">2,400.00</td>
```

- `th[data-col]` = 后端原始键，**永不显示**，供 CSS、窄屏堆叠、测试与排查定位（`02` 文档 §5.2 定的就是这两个属性名，Wave B 必须沿用，别自创 `data-key`）。
- `td[data-label]` = 展示名，被 `02` 文档 §5.1 的 `.dtable td::before { content:attr(data-label) }` 消费（≤768px 堆叠态）；`td[data-col]` = 原始键。
- 原始键**还必须在 `title` 里**：`data-col` 用户看不见，`title` 才是"我能核对后端到底给了什么"的出口 —— 这正是 (b) 兜底策略在 (c) 的对应物。
- 展示名 == 原始键时（用户列名本来就合规），`title` 不重复写一遍，避免多余的悬浮提示（与 `app.js:1776` 已有的"截断才给 title"同一口径）。
- `#preview`（`index.html:415`）**没有** `.dtable` 类，Wave B 若要给它上同一套 DOM，就顺手把 `class="dtable"` 加上（`app.css:1171-1173` 的三条规则能直接复用）；不给也不阻塞。
- `#dataset-table`（`index.html:364-387`）的静态 `th` + `data-sort="size_bytes"` 结构**不动**（`02` 文档 §5.2 明文规定），它的表头本来就是中文。

---

## 5. (d) `app.js:10-11`「不维护错误码→中文对照表」这条规定要不要推翻

### D-1 结论：**保留**，并补一条"呈现契约"

这条规定与"全中文化"**不冲突**，理由全部来自实测（§9）：

1. 后端**没有**给用户看的英文。`app/errors.py:82-94 DEFAULT_MESSAGE` 十一条款全是中文（"请求不合法""资源不存在""请先登录"…），路由里也一律传中文 message（`routes_auth.py:220 "用户名或密码不正确"`、`routes_workflows.py:394 "报告尚未生成"`、`routes_datasets.py:144` 的 415 是"不支持的文件类型：txt"）。
2. 前端所有错误文案的**主显已经是 `error.message`（中文）**，code 只出现在括号/方括号里（`:398`、`:753`、`:1320`、`:1752`、`:1802`、`:3132`）。也就是说这条规定实际执行的正是 (b) 的原则："中文主显 + 原值可查证"。
3. 反过来做才有害：前端自建一张 11 值码表，后端改了话术或加了码，页面就会说**与响应体不一致的中文**（`app.js:10-11` 写的就是这条理由，而且 §9 的 401/404/415 三个响应体证明 message 本来就是后端现写的中文，比表更准）。

**要补的不是"翻译"，是"位置"** —— 建议把规定 2 改写为（Wave B 同步改 `app.js:10-11` 的注释文本）：

> 2. **不维护「错误码 → 中文」对照表**：文案一律用后端给的 `message`。但错误码**不进主显位** ——
>    统一渲染成「中文话术 + `（原值码：CODE）`」，并在 `title` 里写一句它只是标识。
>    `error.detail` 是**给开发者的原始诊断**，可能整段是英文（422/500 实测如此），
>    一律不写进正文，只放进折叠的「原始诊断」区。

### D-2 逐条判定：这几处到底会不会把英文给用户看

| 位置 | 现在拼出什么 | 会不会暴露英文 | 实测依据 |
|---|---|---|---|
| `login.js:755` `"HTTP "+status+" · "+code` | `服务端返回 HTTP 500 · INTERNAL_ERROR（服务器内部错误）。…` | **主显已是中文**，英文只有 `HTTP` + 码。低危 | §9 C：422 同形状；`errors.py:90` INTERNAL_ERROR = "服务器内部错误" |
| `login.js:781` | `账号状态接口返回了无法解析的结果（HTTP 400：请求参数校验失败）。…` | 同上，**低危** | §9 C |
| `login.js:950`（401）`title: info.message` | `用户名或密码不正确` | **不会**，纯中文 | §9 F（401 响应体原文） |
| `login.js:990`（422）`title: info.message` | `请求参数校验失败` | **不会** —— 因为 422 已被 `app/main.py:244-262` 的 handler 包进统一外壳 | §9 C |
| `login.js:204` FastAPI 原生 `detail[0].msg` | 期望拿到 `"Field required"` 之类英文 | **不会发生**：这段是**死代码**。`main.py` 注册了 `RequestValidationError` handler，422 响应里 `error.message` 恒为中文、`payload.detail` 不再是数组，`:199/:202` 两个分支都进不去 | §9 C 原文：`{"error":{"message":"请求参数校验失败","detail":"[{'msg': 'String should have at least 1 character'..."}]` |
| `login.js` 其余 | `info.detail` **从未被渲染**（`readError` 取了它，但没有任何 `setAlert` 传它） | 不会 | grep 结果：`:193-196` 赋值，无消费点 |
| **`app.js:405` `el("p","detail", String(error.detail))`** | 422 → pydantic 英文原文；500 → 清洗过的 traceback；`routes_workflows.py:396` → `这条工作流当前状态是 PENDING，等它跑完再来取。`（中文句里夹裸枚举） | **会，而且是唯一的真漏点**：detail 无条件写进正文 | §9 C（422 全英文 detail）、§9 H（400 detail 夹 `PENDING`） |
| `app.js:646` 会话门 `[UNKNOWN]`、`:398 [CODE]` | 中文主显 + 裸方括号码 | 低危，改呈现位置即可 | D-1 |

### D-3 `:2827` 与 `:2893` 的格式化方案

| 位置 | 现状 | 方案 |
|---|---|---|
| `app.js:2826-2828` `meta "生成时间 " + report.created_at` | **两个问题叠在一起**：① `FinalReport` 的字段是 `generated_at`（实测 §9 E，`app/schemas_workflow.py:209`），**`created_at` 不存在** → 这一行今天根本不渲染（静默死分支）；② 即使存在也是裸 ISO | 改成 `fmtDateTime(report.generated_at)`（`app.js:540-549`，走 `toLocaleString("zh-CN", {hour12:false})`），并把原始 UTC 串放 `title="原始时间戳（UTC）：…"`（照 `:3323-3334` 的既有范式）。⚠️ 这是**前端读错字段名**，不是后端契约问题：后端仍返回 `generated_at`，不动 |
| `app.js:2893` `"llm_used=" + String(data.llm_used)` | 整串英文 `字段名=值` | 改「是否用了大模型：是／否」，`title="服务端原值：llm_used=true"`。注意：`tests/test_report_notice_has_no_internal_field_names.py:41` 专门禁止正文出现 `字段名=值`，这一处是它在**前端**的同款问题，改法与后端那次保持一致（保留两层信息，只换措辞） |
| `app.js:2937` `when.title = item.created_at` | 已经是"中文主显 + UTC 原值" | 不动，作为范式 |
| `app.js:1821/3048` `fmtClock` | 只出 `HH:MM` | 不动 |

---

## 6. 可直接抄的中文映射表 JS 片段

放在 `app.js` `CHART_TYPE_TEXT`（`:114`）之后、`VIEWS`（`:117`）之前；`login.js` 只用 §6-5 那一小段（其余它在分析页之外，不需要枚举表）。**所有键保持后端英文**，因此 N2/N4/N5 全部不受影响。

```js
  /* ================================================== 中文化：枚举 → 中文 */
  /* 口径：这里只改“显示”，不改“取值”。键必须是后端字面量（有测试逐条核对枚举）。
     兜底策略：命中 → 中文主显 + title 写原值；未命中 → 中文“未识别” + 原值进 .origin 小字。 */

  // 1) 工作流状态（后端 WorkflowStatus 全 10 值）。
  //    app.js:2902 historyStatusText 改为直接查这张表，不再自带副本，避免两份表漂开。
  var WORKFLOW_STATUS_TEXT = {
    PENDING: "等待中",
    PROFILING: "读取数据中",
    PLANNING: "制定计划中",
    EXECUTING: "计算中",
    CHARTING: "出图中",
    REPORTING: "写报告中",
    VALIDATING: "校验中",
    SUCCESS: "成功",
    SUCCESS_WITH_WARNINGS: "成功（有告警）",
    FAILED: "失败",
  };

  // 2) 阶段名（后端 WorkflowStep 是 7 值：6 个阶段 + done）。
  //    STEP_NAMES / STEP_LABELS 各加这一条即可；STEP_ORDER 一个字都不许动。
  var STEP_DONE_TEXT = "全部阶段已跑完";

  // 3) pandas dtype（开放集，给常见 8 值；未命中走 zhEnum 兜底）。
  var DTYPE_TEXT = {
    int64: "整数", int32: "整数", float64: "小数", float32: "小数",
    bool: "是/否", object: "文本或混合", str: "文本",
    "datetime64[ns]": "日期时间", datetime64: "日期时间",
  };

  // 4) 校验问题码（app/agents/validation.py 里真实出现的 6 个）。
  var VALIDATION_ISSUE_TEXT = {
    FAILED_TASK: "有任务失败",
    EMPTY_RESULT: "有任务没有结果",
    MISSING_CHART_FILE: "图表文件缺失",
    UNSUPPORTED_NUMBER: "数字找不到出处",
    UNKNOWN_COLUMN: "引用了不存在的列",
    SHORT_REPORT: "报告正文过短",
  };

  // 5) 错误码：只给“这句码值大概是什么意思”的一句话hint，
  //    永远不改写后端 message 本身（app.js:10-11 的规定保留）。
  //    login.js 需要的那几条单独在 login.js 内复制一份，别跨文件引用。
  var ERROR_HINT = {
    BAD_REQUEST: "请求不符合接口要求",
    NOT_FOUND: "要找的东西不在服务端",
    METHOD_NOT_ALLOWED: "请求方法不支持",
    UNSUPPORTED_FILE: "文件类型不支持",
    FILE_TOO_LARGE: "文件超出大小上限",
    SERVICE_UNAVAILABLE: "服务暂时不可用",
    WORKFLOW_TIMEOUT: "分析超过时间上限",
    INTERNAL_ERROR: "服务端内部错误",
    UNAUTHORIZED: "需要登录",
    FORBIDDEN: "没有权限",
    RATE_LIMITED: "操作太频繁",
    UNKNOWN: "服务没有给出原因",
  };

  // 6) 列名：只收“我们工具自产”的键，绝不收用户列名。
  var COLUMN_LABELS = {
    column: "列名", null_count: "缺失个数", null_ratio: "缺失比例",
    unique_count: "不重复取值数", row_count: "行数", group_count: "组数",
    pair_count: "比较的对数", valid_pair_count: "有效对数",
    count: "行数", x: "第一列", y: "第二列", r: "相关系数",
    value: "取值", name: "名称", size_bytes: "字节数",
  };

  // 7) 聚合后缀（取值集合来自 app/tools/registry.py:210-212，不是猜的）。
  var AGG_SUFFIX_TEXT = {
    sum: "合计", mean: "平均", count: "行数",
    min: "最小", max: "最大", median: "中位",
  };

  /* 统一兜底：主显永远中文，原值可查证 */
  function zhEnum(map, raw, label) {
    var known = Object.prototype.hasOwnProperty.call(map, raw);
    return {
      text: known ? map[raw] : "未识别的" + label,
      origin: known ? null : String(raw),
      raw: String(raw)
    };
  }
  function zhEnumNode(map, raw, label, className) {
    var info = zhEnum(map, raw, label);
    var node = el("span", className || null, info.text);
    node.title = "服务端原值：" + info.raw;
    if (info.origin) {
      node.appendChild(el("span", "origin", info.origin));
    }
    return node;
  }

  /* 列名展示名：判定树见 docs/refactor/04-i18n-checklist.md §C-1。
     只做确定性变换，绝不翻译用户列名。原始键由调用方继续用于取值。 */
  function displayColumn(rawKey) {
    var key = String(rawKey === null || rawKey === undefined ? "" : rawKey);
    if (!key) {
      return "";
    }
    if (/[^\x00-\x7F]/.test(key)) {         // 规则1：含中文等非 ASCII → 原样
      return key;
    }
    var known = Object.prototype.hasOwnProperty.call(COLUMN_LABELS, key);
    if (known) {                             // 规则2：我们自产的结构键
      return COLUMN_LABELS[key];
    }
    var m = key.match(/^(.*)_(sum|mean|count|min|max|median)$/); // 规则3：派生后缀
    if (m && m[1]) {
      return displayColumn(m[1]) + "（" + AGG_SUFFIX_TEXT[m[2]] + "）";
    }
    var out = key                            // 规则4：无损格式化
      .replace(/_id$/, " 编号")
      .replace(/_/g, " ");
    if (out.length > 24) {                   // 规则5：截断 + title 兜原值（由调用方写）
      out = out.slice(0, 22) + "…";
    }
    return out;
  }

  /* 时间：一律走已有的 fmtDateTime(app.js:540)；原始 UTC 串进 title。 */
  function timeNode(label, iso, className) {
    var node = el("span", className || null, label + fmtDateTime(iso));
    if (iso) {
      node.title = "原始时间戳（UTC）：" + iso;
    }
    return node;
  }
```

### §6 的接法清单（Wave B 逐处替换，共 14 处）

| # | 位置 | 现在 | 改成 |
|---|---|---|---|
| 1 | `app.js:2504` | `"状态 " + data.status + " · 步骤 " + data.step` | 用 `zhEnumNode(WORKFLOW_STATUS_TEXT, data.status, "状态")` + `zhEnumNode(STEP_NAMES 扩表, data.step, "阶段")` 两个节点拼进 `#workflow-state`（`textContent` 要换成 `clear()` + `appendChild`） |
| 2 | `app.js:2340` | `STEP_NAMES[step] || STEP_LABELS[step] || step` | `STEP_NAMES[step] \|\| STEP_LABELS[step] \|\| "未识别的阶段"` + `.origin` 小字 |
| 3 | `app.js:2917` `historyStatusText` | `known[status] \|\| String(status)` | 表体删掉，改 `return zhEnum(WORKFLOW_STATUS_TEXT, status, "状态")`，调用点按 §6-1 的形状渲染（`:2928/:3068/:3322` 三处） |
| 4 | `app.js:2427` | `TASK_STATUS_TEXT[task.status] \|\| task.status` | `zhEnumNode(TASK_STATUS_TEXT, task.status, "任务状态")` |
| 5 | `app.js:2655` | `CHART_TYPE_TEXT[...] \|\| chart.chart_type \|\| "未知图型"` | `zhEnumNode(CHART_TYPE_TEXT, chart.chart_type, "图型")` |
| 6 | `app.js:2764/2784` | `issue.code + "：" + issue.message` | 中文主显 `VALIDATION_ISSUE_TEXT`（兜底"校验提示"）+ `：` + **`issue.message` 原样**（后端中文），code 进 `.origin` + `title` |
| 7 | `app.js:1974` | `name + "（" + dtype + "）"` | `displayColumn(name) + "（" + zhEnum(DTYPE_TEXT, dtype, "类型") + "）"`，dtype 原值进 `li.title` |
| 8 | `app.js:2797/1934/2018/2661/2664` | 键原样上屏 | `displayColumn(key)`；`th`/`td` 按 §C-4 挂 `data-col`/`data-label`/`title`；**取值仍用 raw key** |
| 9 | `app.js:2827` | `"生成时间 " + report.created_at` | `timeNode("生成时间 ", report.generated_at)`（字段名按实测纠正） |
| 10 | `app.js:2893` | `"llm_used=" + String(...)` | `"是否用了大模型：" + (v ? "是" : "否")` + `title="服务端原值：llm_used=…"` |
| 11 | `app.js:2941` | `"失败原因 " + item.error_code` | `ERROR_HINT[item.error_code] \|\| "失败原因（服务端未说明）"` 主显 + `.origin` 原码 + `title` |
| 12 | `app.js:398/646` | `" [" + code + "]"` | `"（原值码：" + code + "）"`，`title="错误码是技术标识，不是故障原因；故障原因见上面那句"` |
| 13 | **`app.js:405`** | `el("p","detail", String(error.detail))` | 改渲染进 `<details class="diag"><summary>原始诊断（开发者用，可能含英文）</summary><pre>…</pre></details>`；`WORKFLOW_TIMEOUT` 那条 (`:402`) 的中文说明仍在 `summary` 之前 |
| 14 | `login.js:755/781/991/1004` | `HTTP x · CODE` | 保持中文在前，码改 `（原值码：CODE）`；顺带在 `login.js:202-205` 上方补注释"422 已被 main.py 的 handler 翻成外壳，这两个分支是历史兜底，正常不会走到（实测 §9 C）" |

---

## 7. 实测证据（`.venv\Scripts\python.exe` + `TestClient` 真实响应体原文）

探针环境：`AUTH_DB_PATH` / `UPLOAD_DIR` 指到 `.tmp/i18n-probe/`，`LLM_API_KEY=""`（走规则模板那条诚实路径）。CSV 列名为英文 `region,product,sales_amount,profit,report_date`，值为中文 `华东/华南/华北`。

**A｜`GET /api/auth/me`（无 cookie）→ HTTP 200，不是 401**
```json
{"ok": true, "data": {"authenticated": false, "user": null, "needs_bootstrap": true, "session_expires_at": null}, "error": null}
```

**B｜`POST /api/auth/bootstrap` → 200**（`display_name` 中文原样回来，认证链路对中文无副作用）
```json
{"ok": true, "data": {"authenticated": true, "user": {"id": 1, "username": "probe-admin", "display_name": "探针管理员", "created_at": "2026-10-16T13:20:56.877194+00:00", "last_login_at": null}, "needs_bootstrap": false, "session_expires_at": null}, "error": null}
```

**C｜422 的真实形状（`POST /api/auth/login` 空串 / 缺字段；`GET /api/datasets?sort=bad_field`）—— 决定 (d) 结论的关键一条**
```json
{"ok": false, "data": null, "error": {"code": "BAD_REQUEST", "message": "请求参数校验失败", "detail": "[{'type': 'string_too_short', 'loc': ('body', 'username'), 'msg': 'String should have at least 1 character', 'input': '', 'ctx': {'min_length': 1}}, {'type': 'string_too_short', 'loc': ('body', 'password'), 'msg': 'String should have at least 1 character', 'input': '', 'ctx': {'min_length': 1}}]"}}
```
```json
{"ok": false, "data": null, "error": {"code": "BAD_REQUEST", "message": "请求参数校验失败", "detail": "[{'type': 'missing', 'loc': ('body', 'password'), 'msg': 'Field required', 'input': {'username': 'probe-admin'}}]"}}
```
```json
{"ok": false, "data": null, "error": {"code": "BAD_REQUEST", "message": "请求参数校验失败", "detail": "[{'type': 'literal_error', 'loc': ('query', 'sort'), 'msg': \"Input should be 'uploaded_at', 'filename' or 'rows'\", 'input': 'bad_field', 'ctx': {'expected': \"'uploaded_at', 'filename' or 'rows'\"}}]"}}
```
→ **三条同时证明**：① `message` 恒为中文（`login.js:950/990` 用它当 title 不会漏英文）；② 顶层没有 `detail` 数组，`login.js:202-205` 的 `first.msg` 分支是死代码；③ **英文全在 `error.detail` 里**，而 `app.js:405` 会把它无条件写进正文 —— 这就是 (d) 唯一要改的点。

**D｜404 / 415 的 detail 是中文（业务码保持英文，符合规定 2）**
```json
{"ok": false, "data": null, "error": {"code": "NOT_FOUND", "message": "数据集不存在", "detail": "这个 dataset_id 没有登记过，或者它不属于当前登录用户。数据存在服务进程的内存里，服务重启后全部丢失。"}}
```
```json
{"ok": false, "data": null, "error": {"code": "UNSUPPORTED_FILE", "message": "不支持的文件类型：txt", "detail": "只支持 csv / xlsx，收到 .txt"}}
```

**F｜401（错密码）**
```json
{"ok": false, "data": null, "error": {"code": "UNAUTHORIZED", "message": "用户名或密码不正确", "detail": "/api/auth/login"}}
```

**G｜`GET /api/datasets/{id}` 的真实列名与 dtype（(c)/(E8) 的直接证据）**
```json
"columns": ["region", "product", "sales_amount", "profit", "report_date"],
"dtypes": {"region": "str", "product": "str", "sales_amount": "float64", "profit": "int64", "report_date": "str"},
"preview_rows": [{"region": "华东", "product": "A", "sales_amount": "1200.0", "profit": "300", "report_date": "2026-01-05"}, …]
```
→ `app.js:1974` 今天会在字段结构里画出 `region（str）`、`profit（int64）`。

**H｜画像的 `semantic_type`（值确实存在，但前端不引用，故不上屏）**
```json
{"name": "region", "dtype": "str", "semantic_type": "categorical", "null_count": 0, "null_ratio": 0.0, "unique_count": 3, "top_values": [{"value": "华东", "count": 2}, …]}
{"name": "sales_amount", "dtype": "float64", "semantic_type": "numeric", "null_count": 1, "null_ratio": 0.2, …}
{"name": "report_date", "dtype": "str", "semantic_type": "datetime", "min_value": "2026-01-05T00:00:00", …}
```

**I｜工作流终态（(b) 最大漏点的证据）**
```json
{"ok": true, "data": {"workflow_id": "7da8fd9e1cb24b0da2b63c163b5bab1", "question": "各地区的销售额汇总", "status": "SUCCESS", "step": "done", "step_index": 6, "total_steps": 6, "tasks": [{"task_id": "T1", "description": "按region分组汇总sales_amount，从高到低取前 20 组", "status": "SUCCESS", "error": null}], "charts": [{"chart_id": "c9243e2fce45", "filename": "c9243e2fce45.png", "title": "按region分组汇总sales_amount，从高到低取前 20 组", "chart_type": "bar", "x_field": "region", "y_field": "sales_amount_sum", "point_count": 3, "created_at": "2026-10-16T13:20:57.040140Z"}], "created_at": "2026-10-16T13:20:56.960827Z", "finished_at": "2026-10-16T13:20:57.048296Z", "has_report": true}, "error": null}
```
按 `app.js:2504` 现写法渲染 → **屏幕上逐字是**：`状态 SUCCESS · 步骤 done（6/6）`。
未终态（向 registry 塞一条 `PENDING` 状态后轮询）→ `状态 PENDING · 步骤 profiling（0/6）`。
注意 `"step": "done"`：**`done` 不在 `STEP_NAMES`/`STEP_LABELS` 里**，所以 §6-2/§6-3 必须补这个值。

**J｜报告体（(c) 表头 + `llm_used` 的证据）**
```json
{"report": {"report_id": "735d6fd5", "title": "分析报告：各地区的销售额汇总", "summary": "本次未使用大模型，以下结论为工具结果的直接汇总\n\n针对问题「各地区的销售额汇总」，系统成功完成了 1 个分析任务。", "sections": [{"heading": "按region分组汇总sales_amount，从高到低取前 20 组", "text": "排在最前面的一组是 region = 华南，sales_amount_sum = 2,400.00（共 3 行结果）。（工具：group_aggregate，任务：T1）", "cites": ["T1"], "table": [{"region": "华南", "sales_amount_sum": 2400.0}, {"region": "华东", "sales_amount_sum": 1900.0}, {"region": "华北", "sales_amount_sum": 0.0}]}], "data_quality_notes": ["列 sales_amount 缺失 20.00%，相关汇总（均值、合计）可能偏低", "有 1 列存在缺失值：sales_amount"], "generated_at": "2026-10-16T13:20:57.040140Z", "llm_used": false}, "validation": {"passed": true, "checked_numbers": 5, "unsupported_numbers": [], "issues": []}, "llm_used": false}
```
→ ① `renderTable` 的表头逐字是 `region` / `sales_amount_sum`；② `FinalReport` **只有 `generated_at`，没有 `created_at`**（`app.js:2827` 是静默死分支，见 §D-3）；③ `summary`/`sections.text` 里夹带的英文是**后端生成的列名引用**（见下方 K-4），前端不改写。

**K｜`GET /api/workflows/history`**
```json
{"items": [{"workflow_id": "7da8fd9e…", "question": "各地区的销售额汇总", "status": "SUCCESS", "created_at": "2026-10-16T13:20:56.960827Z", "has_report": true, "llm_used": false, "error_code": null}, {"workflow_id": "0123456789abcdef0123456789abcdef", "question": "按地区看销售额", "status": "FAILED", "has_report": false, "llm_used": false, "error_code": "WORKFLOW_TIMEOUT"}], "total": 82, "limit": 3, "offset": 0, "enabled": true}
```
→ `app.js:2941` 画出 `失败原因 WORKFLOW_TIMEOUT`（E12 的例子，真实存在）。

**L｜`GET /api/health`**
```json
{"ok": true, "data": {"status": "healthy", "app_name": "AI Data Analyst Agent", "version": "0.1.0", "uptime_seconds": 1.35, "llm_configured": false, "upload_dir_ready": true}, "error": null}
```
→ `app.js:3381` 已经把 `healthy` 翻成"正常"并把原值放 `title`（**(b) 兜底策略的现成范式**）；`app_name` 属服务标识，原样（E13）。

**M｜`GET /api/workflows/{id}/report`（非终态）→ 400**
```json
{"ok": false, "data": null, "error": {"code": "BAD_REQUEST", "message": "报告尚未生成", "detail": "这条工作流当前状态是 PENDING，等它跑完再来取。"}}
```
→ 源码 `app/api/routes_workflows.py:396 detail=f"…{state.status.value}…"`：中文 detail 里夹**裸枚举**。这条也走 §6-13 的折叠区，不在正文；若要彻底干净，改的是后端 detail 措辞（属另一条票，不在中文化范围内）。

### K-4 记一条"不属于 (a)/(b)/(c) 但确实有英文"的第五类

`report.sections[].heading` / `text` / `chart.title` 是**后端（规则模板或大模型）生成的自然语言**，实测逐字含 `按region分组汇总sales_amount`、`工具：group_aggregate`。它们是后端真实内容，前端若做正则替换就会破"数字/文字必须来自后端真实字段"的纪律（N11），也会让报告与 `results` 对不上。
**处置**：本轮不动；建议 Lead 单开一条票，把措辞权交回后端（`app/agents/report_llm.py` / `validation.py` 的模板层，或在任务描述里给用户列名配一个展示别名）。

---

## 8. Wave B 落地顺序（按能独立验证的粒度切）

1. **先做 (a) 里 A-1 的 6 处**（零风险，只动 `index.html`/`app.js` 字面文本；N9 只要求"接口文档"存在，统一方向正确）。跑 `pytest tests/test_frontend.py`。
2. **再做 (b) §6 的 1-6 与 9-13**（新增映射表 + 兜底函数；关键收益：`状态 EXECUTING` 这类每秒可见的英文消失）。跑 `pytest tests/test_frontend.py tests/test_orchestrator.py`（N2/N3/N4/N5 都在这里）。
3. **然后做 (c) §C-4 + §6-8**：`displayColumn()` 一处实现，四个入口共用；先改 `renderTable`，再改 `renderPreview` / `renderColumns` / `missing-top3` / 图表轴。**每改一处都用 §9-G/J 的样例数据人工看一眼表头与取值是否还对得上**（取值仍用 raw key 是这条最容易写错的地方）。
4. **`app.css` 加 `.origin` 与 `details.diag` 两条样式**（`tokens.css` 只读，不加令牌；`tests/test_frontend.py:416` 那类 CSS 断言只碰 `.topbar__product`，不冲突）。
5. **(a′) 品牌 6 处等 Lead 决策**，不要顺手改；若采纳 A 方案，只改 `login.html:8` + `login.js:480/481` 的标签页标题即可先让"用户看到的第一行字"是中文。
6. 验收：`PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/test_frontend.py tests/test_error_format.py tests/test_report_notice_has_no_internal_field_names.py -q`，再用浏览器跑一遍 §9 的同一条数据（英文列名 CSV + "各地区的销售额汇总"），人工确认屏幕上**除 (a′) 品牌行与"原始诊断"折叠区之外**没有整词英文。

---

## 9. 本文档的自检

- 没有修改 `app/` 与 `tests/` 下任何文件（`git status` 只多这一份 `docs/refactor/04-i18n-checklist.md`；探针脚本写在 `.tmp/` 并已删除）。
- 行号按写入时的 `app/web/app.js`（3695 行）/ `index.html`（658 行）/ `login.html`（328 行）/ `login.js`（1084 行）核对；Wave B 改动后行号会漂，**以本文给出的原文串为定位依据**。
  > **Wave D 复量（2026-10-17）**：那四个数已经变成 `app.js` 5223 / `index.html` 753 / `login.html` 257 / `login.js` 1114。
  > 上面那句"以原文串为定位依据"因此**从建议升级成硬性要求**：本文 **96 处**前端行号锚点里实测有
  > **3 处指向已越界、11 处指向空行**（全量统计与处置规则见 `docs/COMMAND_CENTER_MIGRATION.md` §8），
  > `check_docs.py` 对这些一律不管。
- 与 `docs/refactor/02-structure-contract.md` §5.1/§5.2 的 `.dtable` 契约一致（`data-col` / `data-label` 属性名沿用，未另创）。

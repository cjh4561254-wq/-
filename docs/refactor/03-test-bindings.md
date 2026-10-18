# 03 · 测试约束侦察：重构 `app/web/` 之前，哪些断言会被改坏

> 角色：Wave A「方案 01 重构」的**只读侦察**产物。本文不修改任何文件，只回答一个问题：
> **改 UI 之前，哪些测试/文档声称会因这次改动而失败。**
>
> 侦察范围：`app/web/`（7 个文件 8889 行）、`tests/`（56 个文件）、`scripts/`、`conftest.py`、`docs/*.md`、`README.md`。
> 分级：🔴 一定破（不处理就红）/ 🟡 可能破（取决于改法）/ 🟢 不受影响。

---

## 0. 基线事实（全部实测，不是推断）

| 项 | 实测结果 | 命令 / 出处 |
|---|---|---|
| 全量收集用例数 | **902 collected**（不是文档里的 821） | `.venv\Scripts\python.exe -m pytest --collect-only -q` |
| `tests/test_frontend.py` | **19 passed** | 2026-10-16 本次会话实测 |
| `tests/test_auth_api.py` | **37 passed** | 同上 |
| `scripts/check_docs.py` | **rc=0**，`行号锚点 7 条（代码锚点 7 个）、用例 24 个全部存在、断言方向 12 个参数化实例（16 个请求）` | 同上 |
| `app/web/` 文件与行数 | `app.css` 1564 / `app.js` 3695 / `index.html` 658 / `tokens.css` 402 / `login.css` 1158 / `login.js` 1084 / `login.html` 328；目录内**无子目录、无 `__init__.py`** | `wc -l app/web/*` |
| 前端文件被 pytest 收集？ | **否**。`pytest.ini testpaths = tests app` + `--doctest-modules`，但 `app/web/` 无 `.py`，所以 `.js/.css/.html` 不进收集范围 | `pytest.ini`、`app/web/` 列表 |

### 0b. Wave D 回填（2026-10-17 实跑，**上面的 0 节数字保留作 Wave A 时点快照**）

上表那些数字是**重构之前**的取证，重构会把它改红，所以这里补一份"现在"的：

| 项 | Wave A 快照（保留） | Wave D 实测（2026-10-17） |
|---|---|---|
| 全量收集 / 实跑 | 902 collected | **941 collected / 941 passed**（`pytest -q`，104.61s；= `tests/` 940 条功能用例 + `app/errors.py` 一条 doctest。本次会话内先量到 939，随后并发 agent 补了 2 条守卫变 941 —— **这条也只是时点，以你自己的实跑为准**） |
| 新增守卫来源 | — | `tests/test_frontend_command_center.py`（37 条，Wave B 落地的指挥中心守卫） |
| `tests/` 文件数 | 56 个文件 | **42 个 `tests/test_*.py`**（另有 `__init__.py` 与辅助模块 `auth_support.py`；两处口径统计的对象不同，比对请以 `ls tests/test_*.py \| wc -l` 为准） |
| `app/web/` 行数 | 7 个文件 8889 行（`app.css` 1564 / `app.js` 3695 / `index.html` 658 / `tokens.css` 402 / `login.css` 1158 / `login.js` 1084 / `login.html` 328） | 7 个文件 **10979 行**（`app.css` **2076** / `app.js` **5223** / `index.html` **753** / `tokens.css` **642** / `login.css` **914** / `login.js` **1114** / `login.html` **257**）。复跑：`wc -l app/web/*`。**注意**：Wave D 期间 `app/web` 仍被并发 agent 写入，本文落盘后约一小时内同一批数从 10824 变成 10979 —— 所以这一行**只当"量过"的证明，不当台账** |
| `scripts/check_docs.py` | rc=0 | **仍 rc=0**（行号锚点 7 条 / 用例 24 个全部存在 / 断言方向 12 个参数化实例、16 个请求）——**它本轮一个字都没被触发，因为它只读 `docs/HISTORY_ROBUSTNESS.md`**（见下面结论 2，这条判断继续成立） |
| README 端口守卫 | `test_error_format.py:118-140` 从 README 抓 `--port` 核对 CORS | **仍然有效**：本轮改 README 时**没有新增任何** `uvicorn app.main:app … --port <数字>` 形式的行（只保留 8128），这条守卫实测依旧绿 |
| 前端锚点腐烂（本轮新量） | — | 扫描 10 个文档里的 `app.js|app.css|index.html|tokens.css|login.*:NNN` 引用：**526 处**，其中 **6 处指向已越界**、**47 处指向空行**（第一次量是 525/6/52，一小时后变 526/6/47 —— 数字自己就在漂）；`check_docs.py` 对它们**零管辖**（不读 CSS/JS/HTML）。量化说明见 `docs/COMMAND_CENTER_MIGRATION.md` §8 |

**三条最重要的总体结论**（先给判断，证据见下文）：
1. **前端的"测试钉"全部集中在 3 个文件里**：`tests/test_frontend.py`（19 条）、`tests/test_auth_api.py`（4 条页面路由/静态资源）、`tests/test_health.py`（1 条页脚入口）。**没有** 任何浏览器/E2E 测试（全仓 grep `playwright|selenium|BeautifulSoup|lxml` → 只有 `scripts/check_env.py:151` 把它列为**可选依赖**，无实现）。唯一的前端之外关联地雷是 `tests/test_error_format.py:118-140`，它用正则从 **README** 抓启动端口核对 CORS（见 §2 B07）。
2. **`scripts/check_docs.py` 与前端重构正交**：它只核对 `docs/HISTORY_ROBUSTNESS.md` 一个文件（`scripts/check_docs.py:49`），代码侧只扫 `app/**/*.py` + `scripts/**/*.py`（`:51` `CODE_DIRS`）。该文档里 **前端引用 0 处**（grep 实测）。所以 `test_check_docs.py` 那 14 条镜像变异用例不会因改 UI 变红 —— 但它会在 `docs/HISTORY_ROBUSTNESS.md` 或 `app/api/routes_workflows.py` 被顺手改动时变红（见 §3.3）。
3. **真正的"文档一致性风险"不在被脚本钉住的那份文档里，而在没被任何脚本核对的 215 个前端行号锚点里**，而且这些锚点**在本次重构之前就已经大面积失效**（实测 30 条指向空行/孤立 `}`，154/234 条符号与锚定行不匹配）。详见 §4。这是本次侦察最大的一条发现：**"文档已过期"是既有债务，不是重构造成的**；重构只是让它彻底不可修复。因此建议**换判据**（锚符号不锚行号），而不是逐条追数字。

---

## 1. 总表 A：`tests/test_frontend.py` 逐条（19 条 + 4 个内部 helper）

`file:line` 均为 `tests/test_frontend.py` 的行号。"钉的是什么"只写**机械判据**（正则/子串/相等），不写意图。

| # | 用例 | `file:line` | 钉住的机械判据（原文摘要） | 分级 | 为什么 / 怎么办 |
|---|---|---|---|---|---|
| A01 | `test_root_serves_the_frontend_page` | `:101`，判据 `:111-112`，名单 `:35-53` | `GET /` 是 200 + `text/html` + **不含** `application/json`；`REQUIRED_IDS` **17 个** id 必须逐个以 `id="<name>"` 字面串出现在响应体里：`file upload upload-state dataset-summary llm-badge preview columns missing-top3 question ask ask-state steps elapsed workflow-state workflow-error charts report` | 🔴 | 只要任何一个 id 改名/合并/删除就红。**建议：DOM 骨架 id 一个都不动**（Wave A 只做表现层，id 是 JS 的取数锚点，改了要连带改 `app.js` 的 `$("…")` 30+ 处）。若确需重命名 → Wave C 同步改 `:35-53` 名单，并在本文登记。 |
| A02 | `test_page_declares_utf8_and_is_valid_utf8` | `:115`，判据 `:121-124` | `index.html` 字节必须能 `decode("utf-8")`；文本含 `<meta charset=` 且含 `charset="utf-8"`（大小写不敏感） | 🟢 | 保留这一行即可。**注意**：新文件（如拆出的 `.html` 片段）不在断言范围内，写成 GBK 不会红。 |
| A03 | `test_static_assets_are_served` | `:127`，判据 `:130-136` | `GET /static/app.js` 200 且 ct 含 `javascript`；`GET /static/app.css` 200 且 ct 含 `css` | 🔴 | 拆文件可以，但 **`app.js` / `app.css` 这两个名字必须继续存在且可取**。若把 `app.css` 改成 `base.css`+`views.css` 并用 `@import` 聚合 → 本条仍绿（`app.css` 还在）。若直接删掉 `app.css` → 🔴。 |
| A04 | `test_page_links_its_own_assets_and_nothing_external` | `:139`，判据 `:141-149` | ① `index.html` 必须含 `/static/app.css` 与 `/static/app.js`；② **`app.css` 的位置必须早于 `app.js`**（`html.index(...)< html.index(...)`）；③ 对 `("index.html","app.css","app.js")` **逐个文件**断言全文**不含** `http://` 且**不含** `https://` | 🔴 | 三个破点：(a) 把 `<script>` 提到 `<link>` 之前；(b) 内联 SVG 补 `xmlns="http://www.w3.org/2000/svg"`（现有 `index.html:66-84` 的 19 个 `<symbol>` **故意不写 xmlns**，见 `login.html:15-18` 的注释说明）；(c) 引 CDN 字体/图标。**⚠ 判据漏洞：`login.html`/`login.js`/`login.css`/`tokens.css` 不在这三个文件名单里**，给登录页加外链**不会红**。→ 建议 Wave C 把名单扩到 7 个文件（加强守卫）。 |
| A05 | `test_data_directory_is_not_mounted_as_static` | `:152`，判据 `:163-169` | 真实上传后 `GET /data/uploads/<stored>` 与 `/data/samples/sales.csv` 必须 404/405 | 🟢 | 纯后端挂载策略，前端表现层改不到。 |
| A06 | `test_static_mount_does_not_escape_the_web_directory` | `:172`，判据 `:174-179` | `/static/../app/main.py` 与 `/static/..%2fapp%2fmain.py` 必须 403/404 | 🟡 | 现在是扁平 7 文件、无子目录。若重构**建子目录**（如 `app/web/js/`）本条仍绿；但会连带 A16 破。 |
| A07 | `test_api_and_docs_are_untouched_by_the_page` | `:182` | `/api/health` 200 + `ok=true` + `llm_configured`；`/docs` 200 html | 🟢 | 后端。 |
| A08 | `test_js_references_the_same_endpoints_as_the_backend` | `:199`，判据 `:202-211` | `app.js` 文本必须含 7 个路径字面串：`/api/datasets/upload`、`/api/datasets/`、`` /api/datasets/${id}/profile ``、`/api/analysis`、`/api/workflows/`、`/charts/`、`/api/health` | 🔴 | 注意第 3 个是**模板字符串原文**，即必须是 `` `/api/datasets/${id}/profile` `` 这种写法。现由 `app.js:30-46` 的 `const API = {…}` 集中提供，`app.js:29` 的注释已经写明"拼出来的字符串里没有完整那条，用例就判红"。**若重构把 API 表改成 `BASE + "/datasets/" + id + "/profile"` 这类拼装 → 直接红。** 建议：`API` 常量表整体保留原样（它属于契约层，不是表现层）。 |
| A09 | `test_js_knows_all_six_step_labels` | `:214`，判据 `:217-225` | 6 个中文串必须逐字在 `app.js`：`正在读取数据 / 正在制定分析计划 / 正在计算 / 正在生成图表 / 正在撰写报告 / 正在校验数字` | 🔴 | **这是最容易被"顺手优化文案"改坏的一条**。见 §6。 |
| A10 | `test_js_step_order_matches_the_backend_step_vocabulary` | `:228`，判据 `:240-247` | ① 正则 `const STEP_ORDER = \[(.*?)\];`（re.S）必须命中；② 数组内**双引号**包裹的名字序列必须与 `app/agents/orchestrator.py::_STEPS` **全等**；③ `js.split("const STEP_LABELS = {",1)[1].split("};",1)[0]` 切出的块里，每个 step 必须有 `^\s*<step>\s*:` 形式的键 | 🔴 | 三个隐藏前提：`STEP_ORDER`/`STEP_LABELS` 这两个**声明名必须保留**（现在在 `app.js:62` / `app.js:64`）、数组元素必须是**双引号**（改成单引号 → `re.findall(r'"([A-Za-z_]+)"')` 返回空 → 红）、`STEP_LABELS` 对象后必须紧跟 `};`。若重构统一 JS 引号风格为单引号 → 🔴 **必破，且与 UI 无关**。建议：保留双引号，或在 Wave C 把正则放宽为 `["']`。 |
| A11 | `test_js_polls_the_status_endpoint_and_stops_at_every_terminal_status` | `:250`，判据 `:254-269` | ① 子串 `POLL_INTERVAL_MS = 1000`；② 取**第一行**以 `const TERMINAL` 开头的行，该行内必须同时含 `SUCCESS` / `SUCCESS_WITH_WARNINGS` / `FAILED`；③ `clearInterval`；④ `has_report`；⑤ **`js.count("API.report(") == 1`（恰好一次）** | 🔴 | ②是**单行判据**：`TERMINAL` 现在是 `app.js:52` 的一行；Prettier 式格式化把它拆成多行 → 三个终态只命中第一个 → 🔴。⑤是**精确计数**：新增任何第二处报告调用（例如"下载报告"按钮、报告懒加载重试）→ 🔴。④注意 `has_report` 在 `app.js` 出现多次无妨。建议：`TERMINAL`/`STEP_ORDER`/`POLL_INTERVAL_MS` 三行声明**格式冻结**，不参与任何自动格式化。 |
| A12 | `test_js_handles_the_four_edge_cases` | `:272`，判据 `:275-282` | `app.js` 必须含 5 个标记：`FILE_TOO_LARGE`、`UNSUPPORTED_FILE`、`SERVICE_UNAVAILABLE`、`WORKFLOW_TIMEOUT`、`data.error` | 🔴 | 若重构把错误码处理抽到 `errors.js`（新文件）→ 这些标记**离开 app.js** → 🔴。结合 A16（白名单）看，**把 app.js 拆成多文件这一步在本项目里代价最高**。 |
| A13 | `test_js_does_not_branch_on_http_status` | `:285`，判据 `:292-295` | **禁止**子串：`resp.ok`、`response.ok`、`status === 200`、`status_code` | 🟡 守卫 | 防倒退的守卫，**必须保留**。但有洞：`r.ok` / `res.ok` / `status==200` / `status >= 400` 都绕得过去。**建议加强**（Wave C）为正则扫描 `\b[A-Za-z_]\w*\.ok\b` 与 `status\s*(===?|>=|<=)\s*\d{3}`。重构若引入 `if (res.status === 401)` 做会话失效判断 → 字面不违规但违反意图，应改判据后再写。 |
| A14 | `test_upload_error_codes_match_the_backend` | `:298` | 真发 415/413 请求核对 `UNSUPPORTED_FILE` / `FILE_TOO_LARGE` | 🟢 | 后端。 |
| A15 | `test_error_shell_shape_used_by_the_frontend` | `:326` | 统一外壳 `{ok,data,error}` 形状 | 🟢 | 后端。 |
| A16 | `test_web_directory_has_no_stray_files` | `:342`，判据 `:367-379` | `sorted(path.name for path in WEB_DIR.iterdir())` **必须与 7 个名字的全列表全等**：`app.css app.js index.html login.css login.html login.js tokens.css`；并且 `index.html`、`login.html` 必须真实存在 | 🔴 **最高危** | 这条是**结构性总闸**：(a) **新增任何一个文件 → 红**（含临时文件、`.map`、`favicon.svg`、`README`）；(b) **新增任何子目录 → 红**（`iterdir()` 不过滤目录，`js/`、`components/`、`__pycache__` 都算一个条目）；(c) 删掉/改名任何 7 文件之一 → 红。docstring 的注释里还带着**两处过期行号**（`:354` 说 login.html 第 17-18 行，实测在 **20-21**；`:356` 说第 317 行，实测 **326**）。**决策建议：Wave A 明确"不拆文件、不建目录"，把 7 个名字当作硬边界**；这能一次性省掉 A03/A04/A08/A12 的连带返工。若产品方案 01 一定要拆，必须 Wave C 同步改白名单 + 同步把 A12 的标记判据改成"扫 app/web 全部 js"。 |
| A17 | `test_file_input_stays_reachable_by_keyboard` | `:385`，判据 `:397-413` | ① `app.css` 里正则 `\.dropzone input\[type="file"\]\s*\{(.*?)\}` 必须命中（**选择器原文与双引号必须逐字**）；② 该块内**禁止** `display: none` / `display:none` / `visibility: hidden` / `visibility:hidden`；③ 该块内必须有 `clip-path` 与 `position: absolute`；④ `app.css` 全文必须含 `.dropzone:focus-within` | 🔴 | 现状：`app.css:780` 的 sr-only 块（含 `clip-path: inset(50%)`）、`app.css:794` 的 `:focus-within`。三个破点：把隐藏 input 改 `display:none`（守卫本意，该红）；把选择器写成 `.dropzone>input[type='file']` 或单引号（**该红但不该红**，属判据过死）；把 dropzone 样式移到新 css 文件（A16 已禁止）。**建议保留结构 + 保留这条**。 |
| A18 | `test_topbar_brand_truncates_politely_and_yields_at_320px` | `:416`，判据 `:433-454` | ① `\.topbar__product\s*\{(.*?)\}`（re.S，**取第一个**命中）块内必须同时有 `text-overflow: ellipsis`、`overflow: hidden`、`min-width: 0`；② `@media (max-width: 480px)\s*\{(.*?)\n\}` 必须命中且块内含 `.topbar__link` 与 `display: none`；③ 该 480 块内**禁止**出现 `#llm-badge` 或 `llm-badge`；④ `index.html` 必须含 `接口文档` | 🔴 | 现状：`app.css:248` `.topbar__product{…}`、`app.css:1297-1306` 的 480 块（内含 `.topbar__link { display: none; }` 于 1298）。**四个隐藏前提**：(i) 选择器 `.topbar__product` 必须**独占一个规则**（若合并成 `.topbar__product, .foo {` → 正则 `\s*\{` 不匹配 → "找不到样式块"）；(ii) `@media (max-width: 480px)` **必须恰好一个且第一个就是它**（`re.search` 取首个；若在文件更靠前处新增一条 480 断点，而 `.topbar__link` 在第二条里 → 🔴）；(iii) `\n\}` 依赖"闭括号单独成行"的排版 —— 把 `}` 写成行尾（`… }` 同一行）会让捕获块越界吞掉后面的规则，可能把 `llm-badge` 吞进来 → 🔴；(iv) 顶栏"接口文档"链接不能删（④与 A19/T-Health 交叉）。**方案 01 若要重排顶栏，这条必须一起改。** |
| A19 | `test_disabled_buttons_stay_readable` + helper `_css_tokens`/`_css_rule`/`_declared_color`/`_contrast` | `:498`（helper `:460-495`） | ① `app.css` 必须有一个 `:root\s*\{(.*?)\n\}` 块，且解析出 `--name: #rrggbb` 令牌表（现 `app.css:21-117`）；② 对 4 个选择器取**最后一条**同名规则：`.btn:disabled, .btn[aria-disabled="true"]`（`:615`）、`.btn--primary:disabled, .btn--primary[aria-disabled="true"]`（`:632`）、`.btn[data-loading="true"]`（`:656`）、`.btn--primary`（`:622`）；③ 每条规则**禁止** `opacity`；④ 必须用 `color: var(--x)` / **`background: var(--x)`**（不是 `background-color`）书写，且 `--x` 必须在①的第一块 `:root` 里有 hex 定义；⑤ 解析出的双色 WCAG 对比度 ≥ **4.5**（禁用态两条 + 主按钮常态一条） | 🔴 | 这条是**最反直觉的一条**，因为它钉的是"颜色可静态计算"这件事。破点清单：<br>(a) 把 `:root` 令牌整体搬去 `tokens.css`、`app.css` 只留组件 → **`app.css 里找不到 :root`**（helper `:463`）；<br>(b) `app.css` 里出现**第二个 `:root`** 且在前面 → `re.search` 取首个，`--brand-600` 若只定义在第二个块 → "用了未定义的令牌"；<br>(c) 令牌写成 `rgb()`/`hsl()`/3 位 hex → `_css_tokens` 的正则 `#[0-9a-fA-F]{6}` 直接**不收集**它 → "用了未定义的令牌"；<br>(d) 给禁用态改成 `background-color:` → "规则里没有写 background"；<br>(e) 换色后对比度掉到 4.5 以下 → 真缺陷，该红；<br>(f) 同选择器**后出现一条更晚的规则**（如深色模式补丁、hover 提升）带着 `opacity` → `_css_rule` 取最后一条 → 红。<br>**方案 01 若换配色体系（例如全量走 `--ds-*` 新令牌），这条必然要一起改。** |

**小结（表 A）**：19 条里 🔴 **13**（A01/A03/A04/A08/A09/A10/A11/A12/A16/A17/A18/A19 = 12，加 helper `_css_tokens`/`_css_rule`/`_declared_color` 这条链并入 A19）、🟡 **2**（A06/A13）、🟢 **4**（A02/A05/A07/A14/A15 中的 4 条 + A02 视为 🟢）。其中 A01/A16/A19 三条是"改动面积放大源"，建议当作**硬边界**而非"待同步的测试"。

**全篇分级合计**：🔴 **15** 条（表 A 13 + 表 B 的 B01/B06）、🟡 **9** 条（A06/A13 + B03/B07/B08/B10/B11/B12/B14）、其余 🟢；另 §4.1 列出 **14 组"重构前就已失效"的行号锚点**、§4 统计 **215 条前端文档锚点中 30 条确定失配 + 154 条符号失配**。

---

## 2. 总表 B：`tests/` 之外与之内，还有哪些地方在**读前端内容**并做断言

全仓 grep `app/web|WEB_DIR|/static|StaticFiles|\.js"|\.html"|read_text` 的完整结果（除 `test_frontend.py`）：

| # | 位置 | 判据（机械） | 分级 | 说明 |
|---|---|---|---|---|
| B01 | `tests/test_health.py:92-105` `test_root_hint` | `GET /` 200 + html + **响应体含 `/docs` 与 `/api/health`**（纯子串） | 🔴 | 现在由 `index.html:118`（顶栏接口文档链接）与 `index.html:191`（`<a href="/api/health">健康检查</a>`）满足。**页脚/顶栏这两个入口链接不许同时删掉**，否则这条红（docstring 明说它的意图是"根路径要一眼确认服务状态并给出文档入口"）。 |
| B02 | `tests/test_auth_api.py:318-322` `test_login_page_is_public` | `GET /login` 200 + ct 以 `text/html` 开头 | 🟢 | 只测路由，不测 DOM。 |
| B03 | `tests/test_auth_api.py:325-333` `test_app_page_redirects_anonymous_to_login` | `GET /app`（不跟随）→ **303** 且 `location == "/login"`（精确等值，**不带任何查询串**） | 🟡 | 由 `app/main.py:386-392` 决定，后端固定回 `/login`。**前端重构改不到**；但如果顺手给 303 加 `?next=` 参数 → 🔴 且与 UI 无关。docstring 明确写了"为什么把 next 参数省掉（防开放重定向）"。 |
| B04 | `tests/test_auth_api.py:336-339` `test_app_page_serves_index_when_logged_in` | `/app` 已登录 → 200 html | 🟢 | |
| B05 | `tests/test_auth_api.py:342-349` `test_root_stays_public` | 匿名 `GET /` → 200 | 🟢 | |
| B06 | `tests/test_auth_api.py:353-356` `test_web_dir_assets_are_reachable` | `GET /static/tokens.css` 200 + ct 含 `text/css` | 🔴 | **`tokens.css` 这个文件名被两条独立钉住**（A16 白名单 + 本条）。重构不许把它并进 `app.css`。 |
| B07 | `tests/test_error_format.py:118-140` `test_cors_allows_the_port_documented_in_readme` | 用正则从 **`README.md`** 抓所有 `uvicorn app.main:app … --port (\d+)`，逐个断言 `http://127.0.0.1:<port>` 在 CORS 白名单里 | 🟡 **本次重构的隐藏地雷** | CORS 白名单当前是 8128/8000/5500（`app/main.py:134-141`）。**若在 README 里为前端预览写一条新端口命令（如 `--port 5173`、`--port 3000`）→ 这条直接红。** Wave A 若要补"如何本地预览 UI"的说明，只能用 8128/8000/5500 三个已放行端口之一。 |
| B08 | `scripts/check_env.py:241-257` | 环境体检脚本断言 `app/web/` 下**7 个文件存在**（section 标题写"应为 7 个，无构建步骤"），缺任一 → `bad()` | 🟡 | **不是 pytest 用例**（不在 `tests/`，`testpaths` 不含 `scripts`），但被 `docs/runbook.md` 当作验收步骤。**删/改名前端文件 → runbook 体检变红。** 多出的文件不会在这里报错（与 A16 的"全等"判据宽严不同）。 |
| B09 | `scripts/demo.py:225-228` | 端到端演示脚本 `GET /static/app.js` 与 `/static/app.css` 并打印状态码/长度 | 🟢 | 只打印，不断言文件名以外的东西；但同样要求两个文件存在（否则 ct 取不到 → 脚本 `die`）。 |
| B10 | `app/main.py:148-156`、`328-344`、`347-359`、`362-393` | `WEB_DIR = PROJECT_ROOT/"app"/"web"`，`app.mount("/static", StaticFiles(directory=WEB_DIR))`；`/`→`index.html`、`/login`→`login.html`、`/app`→`index.html` 或 303 | 🟡 | **文件名被硬编码在 4 处 FileResponse**。改名 = 改后端。 |
| B11 | `app/data/store.py:66`（注释） | 「前端 `app/web/app.js:982-1004` 已经按这三个名字在传参」 | 🟡 文档债 | **锚点已失效**：真实位置是 `app.js:1066-1096`（契约注释）与 `app.js:1228-1240`（`datasetsQuery()`）。不被任何脚本核对（不在 `HISTORY_ROBUSTNESS.md`）。重构后彻底漂移 → 建议改成符号引用 `app.js::datasetsQuery()`。 |
| B12 | `app/schemas_dataset.py:127`（docstring） | 「前端 `app/web/app.js:1029-1038` 的 `datasetCols()`」 | 🟡 文档债 | **已失效**：`datasetCols` 实际在 **`app.js:1124`**。同上建议。 |
| B13 | `conftest.py`（全文 346 行） | **无任何前端夹具**：没有静态文件加载、没有 HTML 解析、没有期望视图名列表 | 🟢 | 它只做四件事：tempfile 重定向、`AUTH_DB_PATH` 相对路径、LLM 环境变量隔离、`TestClient` 自动带会话 cookie（`:302-320`）。**`test_frontend.py` 的 17 个 id 名单（A01）就是全仓唯一的"视图/元素契约"**，它不在 conftest 里，所以没有"改一处生效两处"的便利。 |
| B14 | `tests/test_check_docs.py:53` `GUARDED_DIRS=("app","scripts","docs","tests")` + `:94-125` `_tree_hashes` | 变异前后对 `app/`、`docs/`、`tests/`、`scripts/` 全部文件取 sha256 比对，要求**真仓库逐字节不变** | 🟡 | **只在 `pytest` 运行期间有意义**：如果重构（或设计 demo 预览）**并行**往 `docs/` 或 `app/` 写文件，而这一刻正好在跑这条用例 → 假红。已排除的运行目录只有 `__pycache__ / .pytest_cache / .mypy_cache / .ruff_cache / .tmp / .coverage`、`tmp[a-z0-9_-]{8}`、`pytest-of-*`（`:54-61`、`:111-117`）。**建议：Wave A 期间不要并发跑全量测试，或先把 `docs/design-options/` 的产物写完再跑。** |

> 补充核实：`tests/test_history_acceptance.py:651-666` 会 `PROJECT_ROOT.rglob("*.py")` 扫全仓找 ORM import。**只看 `.py`**，前端重构不触发；但**若为前端预览引入任何 Python 侧的 build/serve 脚本，绝不能 import flask/fastapi 之外的 ORM**。

---

## 3. `scripts/check_docs.py` + `tests/test_check_docs.py`：它到底核对谁

### 3.1 覆盖面（读码结论，逐条给行号）

| 维度 | 判据实现 | 作用对象 |
|---|---|---|
| ① 行号锚点 | 代码侧 `collect_line_anchors`（`:123-195`）只扫 `app/**/*.py` + `scripts/**/*.py`（`CODE_DIRS` `:51`，`scripts/` 自身 `:128-129` 被跳过），只认**声明形态** `# **path:NNN** \`引文\``（`LINE_ANCHOR_DECL_RE` `:65-67`）；文档侧 `collect_doc_anchors`（`:213-290`）**只读 `docs/HISTORY_ROBUSTNESS.md`**（`DOC` `:49`） | 只有 `.py` 的行 |
| ② 用例存在性 | `collect_test_cases`（`:293-300`）用 `ast` 收集 `tests/**/*.py` 的 `test_*` 函数；文档里出现的 `test_*` 必须存在（`:378-381`） | `tests/` 里的函数名 |
| ③ 断言方向 | 解析文档里的 `投喂行 \`用例[instances=N,requests=M] -> 状态码\``（`CASE_ANCHOR_RE` `:68-72`），**真的执行** `tests/test_history_robustness.py` 的用例并逐请求核对状态码（`:303-328`、`:384-433`） | 后端 HTTP 行为 |
| ④ 裸行号禁令 | **仅在**标题含 `修复状态 / 行号锚点 / 当前` 的 `## ` 章节里（`_CURRENT_SECTION_MARKERS` `:79`、`_in_current_block` `:198-210`），对 `` `file.ext:NNN` ``（`DOC_LINE_REF_RE` `:73`，扩展名含 **`js`/`html`/`md`/`txt`**）报「裸行号引用」；`.md` 自身豁免（`:280-281`） | `HISTORY_ROBUSTNESS.md` 一个文件 |

### 3.2 对前端重构的实际结论

* **`HISTORY_ROBUSTNESS.md` 里的前端引用 = 0 处**（实测 grep），所以维度 ①②③④ 对本次重构**全部无感**。`test_check_docs.py` 14 条镜像用例的基线（`_mirror` `:238-257` 要求 rc=0）**不会因改 UI 变红**。
* **但有一个真实的联动风险**：`test_check_docs.py` 的镜像用 `shutil.copytree` 复刻整个 `app/`（`:64` `_MIRROR_ENTRIES`，`:136-157`）。它跑 14 次。前端文件体积 ~360 KB，**若重构把 `app/web` 撑到几 MB（加图片/字体/构建产物），这 14 个用例会明显变慢**（不是红，是慢）。A16 已经拦住了多余文件，顺带解决了这件事。
* **维度 ④ 的一个前瞻提醒**：`DOC_LINE_REF_RE` 是**认识 `.js` 和 `.html` 的**。今天没有触发，纯粹因为 `HISTORY_ROBUSTNESS.md` 里没写前端。**如果 Wave C 想把"前端契约"补进这份文档**（听起来是好主意），那么在"当前"章节里写 `` `app.js:52` `` 这类裸引用会**立刻报「裸行号引用」**，而代码侧又**无法为 `.js` 建立锚点**（`collect_line_anchors` 只扫 `.py`，且 `target.is_file()` 后要求该行文本等于引文——`.js` 里加 `// **:52** \`…\`` 也不会被收集，因为 `CODE_DIRS` 只 rglob `*.py`）。
  → **结论：不要把前端行号写进 `HISTORY_ROBUSTNESS.md`。** 前端的"文档↔代码"一致性应当由 `tests/test_frontend.py` 的符号级断言承担（它已经是这么设计的：A08/A10/A11 锚的是**常量名**而不是行号）。这是一条应当写进重构约定的判断。

### 3.3 `test_check_docs.py` 里写死的**后端**行号（顺手要守，别误伤）

`tests/test_check_docs.py` 用**硬编码行号**做变异：`ROUTES` 的第 **219** 行是目标、**218** 行是声明（`:322-359`、`:416-441`，`_TARGET_LINE` `:84`、`replace_line(219, …)`）。对应 `app/api/routes_workflows.py:218`（声明）/ `:219`（`status=_coerce_enum(...)`），另有 `:157`、`:182`、`:225`、`:276`、`:321`、`:330` 六条同族锚点。
🟡 **只要重构顺手碰 `routes_workflows.py`（哪怕只在文件头加两行注释），这 14 条用例就会大面积假红。** 约定：**Wave A 一律不碰 `app/api/routes_workflows.py`**；若必须碰，同时改这 7 处声明行号 + `test_check_docs.py` 里的 219/218 两处字面量。

---

## 4. 文档债全量盘点：已失效的行号锚点（重构前就已过期）

方法：正则抽 `docs/**/*.md` + `README.md` 里所有 `` `<前端文件>:NNN` `` 引用，与磁盘文件真实行内容比对。

* **唯一引用数：215**（含 4 条指向**根本不存在**的文件）
* **确定失配 30 条**：锚点落在**空行或孤立 `}`** 上
* **符号级失配 154 / 234**：锚点 ±2 行内找不到该文档句里点名的选择器/函数/令牌
* **完全正确的只剩少数**：`app.js:52`（`const TERMINAL`）、`:58`（`POLL_INTERVAL_MS`）、`:62`（`STEP_ORDER`）、`:117`（`var VIEWS`）、`app.css:243`、`:769`、`:1167`、`:1231`、`:1252`

### 4.1 需要**立刻**修的（它们写的是"现状"，重构后 100% 变成谎话）

| 声称 | 出处 | 真实位置 | 备注 |
|---|---|---|---|
| `.dot--ok` = `app.css:282` | `app/web/tokens.css:252` | **`app.css:299`** | 同一行还写着裸 hex `#4cc38a`，而 `app.css:299` 现在已是 `background: var(--ds-color-bar-dot-ok)` —— **注释描述的实现方式也已经变了** |
| `.dot--warn` = `app.css:283` | `tokens.css:253` | **`app.css:300`** | 同上（`#e0b25a`） |
| `.dot--err` = `app.css:284` | `tokens.css:254` | **`app.css:301`** | 同上（`#e08a80`） |
| `.error-box .detail` = `app.css:775` | `tokens.css:255` | **`app.css:858`** | 同上（`#7a2b23`） |
| 「值必须与 `app.css:21-113` 逐字一致」 | `tokens.css:23`、`tokens.css:48`；`docs/final-report.md:168`、`:301`（并称"兼容层 71 个"） | `app.css` 的 `:root` 实际是 **`21-117`** | 实测：`21-113` 切片里只有 **67 个**令牌，`tokens.css` 与之**同名同值 0 处不一致**（我跑了 parity 比对）。所以 71 vs 67 的差额正好落在 114-117（`--z-pop/--z-gate/--topbar-h/--sidebar-w/--content-max`）。**范围数字本身就是错的** |
| 「三个裸 hex 没有对应 :root 令牌可引用，`app.css:282-284`、`app.css:775`」 | `docs/design-system.md:156`、`:221` | 该说法**已被 tokens.css 推翻**（现在有 `--ds-color-bar-dot-*` / `--ds-color-danger-strong`），行号也错 | 属"判断性叙述 + 行号"双过期 |
| 「login.html 第 17-18 行按 tokens.css→login.css 引用」 | `tests/test_frontend.py:354`（docstring） | **`login.html:20-21`** | **在测试文件的注释里**，不会被任何测试抓到 —— 正是本项目 `AGENTS` 风格最忌讳的"注释谎报" |
| 「login.html 第 317 行 `<script src="/static/login.js" defer>`」 | `tests/test_frontend.py:356` | **`login.html:326`** | 同上 |
| 「`tests/test_frontend.py:469` 会验对比度」 | `docs/design-system.md:533` | 对比度断言实际在 `:520-549`；`:469` 是 helper `_css_rule` | 自我引用的锚点；Wave C 改测试时同步 |
| 「`index.html:11-12` 已是 tokens→app 的顺序」 | `docs/design-system.md:62` | **`index.html:14-15`** | A04 要求 app.css 早于 app.js，与此不冲突 |
| 「`index.html:30-56` 共 18 个图标」 | `docs/design-system.md:116` | `<symbol>` 在 **`index.html:66-84`**，实测 **19 个** | |
| 「`app.css:1040` 的 `--sidebar-w:190px`、`app.css:1048` 的 `--topbar-h:52px`」 | `docs/design-system.md:58`、`:397`、`:398` | 这两个覆盖值现在在 **`app.css:115`、`:114`**（声明处）与响应式区 `:1236`/`:444`；`:1040/:1048` 已是 `.steps` 规则 | 完全指错 |
| 「`app.js:1188` / `:1513` / `:1555` / `:1595` / `:382` / `:416` / `:731` / `:1319` / `:1418` / `:249/:260/:274` / `:73`」 | `docs/ux-spec-final.md:226`、`docs/design-directions.md:119/:124/:534/:574/:583/:605`、`docs/design-system.md:309/:426/:484/:488` | 实测：`fmtInt 281`、`fmtBytes 292`、`fmtPercent 306`、`STEP_STATE_TEXT 88`、`viewFromHash 788`、`currentTarget 450`、`applyView 835`、`paintSteps 2363`、`pollOnce 2471`、`openHistoryDetail 3292`、`fetchHistory(5,0) 3024`、`datasetsQuery 1228` | **`design-directions.md:119/:124` 的"事实性结论"依赖这些行号**（"只认三种路由 `#/workbench|history|status`"）—— 实际 `app.js:117` 的 `var VIEWS` 有 **6 个视图**（workbench/dataset/analysis/report/history/settings），`index.html` 有 6 组 `data-view`。**方向 B/C 的"缺路由"论断已经过期**，这是方案 01 选型的直接输入，值得单独确认 |
| `direction-A/B/C.html:12/:157` | `docs/design-directions.md:195/:258/:327/:429` | **文件不存在**（4 条悬空引用） | 早期设计稿，非 `app/web` |

### 4.2 数字型声称（改了会"文档骗人"，但测试无感）

| 声称 | 出处 | 现状 |
|---|---|---|
| 「全量 821 passed」 | `docs/DECISIONS.md:1136`、`:1199`、`:1268`；`docs/ARCHITECTURE.md:2182-2183` | 实测 **902 collected**。这些段落**自己已写明"当时的实测值，记录不改写"**（`ARCHITECTURE.md:2182`）→ **按项目约定不要改**，但**新文档一律写新值** |
| 「`app/web/app.js` 133331 B / SHA256 `124E01F6…`」 | `docs/final-report.md:611` | 现在仍是 133331 B（未改），**一旦动 app.js 这个哈希台账就作废** |
| 「本报告全部证据锚定 `app.js 131350/918a891c`」等 7 个哈希 | `docs/ui-review.md:116`、`:583`、`:641` | 同上；`ui-review.md` 是**评分报告**，其"评分口径说明"段落明确按"当时冻结版打分"，**不应追溯修改** |
| 「文件共 **1564 行**；task-24 块起于 **1495**…1489/1493/1494」 | `docs/ui-review.md:720` | 1564 = 当前真实行数，**动 `app.css` 就红字变谎话**（且这段在做"最小性证明"） |
| 「令牌与对比度脚本 `.tmp/design/token_parity.py`、`contrast_check.py` exit 0」 | `docs/final-report.md:301` | `.tmp/` 是运行产物、已不在（`.tmp` 被 gitignore 语义排除）→ 这两条"证据"**已不可复现**。重构时**不要把它当回归网**，需要的话重建脚本（但 `.tmp/` 不算交付物） |

### 4.3 每个文档的引用密度（决定"要不要逐条改"）

| 文档 | 前端引用条数 | 建议 |
|---|---|---|
| `docs/design-system.md` | **210**（含 73 条符号级失配） | **不逐条改行号**。改为"符号引用"约定：`.btn--primary:disabled` / `tokens.css::--ds-measure-report`。在文档头部加一句"本文的 `file:NNN` 形式引用已于 <日期> 统一降级为符号引用，不再承诺行号" |
| `docs/ui-review.md` | 119 | **冻结**（评分报告，自带"版本锚定"声明），只在开头追加一行"本报告针对重构前基线，证据哈希不再可复现" |
| `docs/final-report.md` | 84 | 同上，**冻结 + 声明** |
| `docs/a11y-audit.md` | 60 | 同上，**冻结 + 声明**（它的 `login.css:809-826` 类锚点也在漂移） |
| `docs/ux-spec-final.md` | 45 | **要改** —— 它是"实现规格"，行号过期会误导实现方；改符号引用 |
| `docs/ARCHITECTURE.md` | 17 | 只改接口层对 `app/web` 的描述，不动历史数字 |
| `docs/design-directions.md` | 12 | **必须核对**：`viewFromHash` "只认三种视图"的论断已过期（§4.1），影响方案选型 |
| `docs/DECISIONS.md`、`README.md`、`docs/limitations.md`、`docs/DEMO.md`、`docs/runbook.md`、`docs/HIGHLIGHTS.md`、`docs/INTERVIEW_QA.md`、`docs/WORKFLOW.md`、`docs/design-options/*.md` | 1-10 | README 改文件名清单（`:188-203`、`:328-329`、`:387-390`）+ **注意 B07 端口地雷**；其余按需 |

---

## 5. `conftest.py` 专项回答

**没有前端相关夹具**（逐项确认，`conftest.py:1-346`）：

| 猜想 | 实测 |
|---|---|
| 静态文件加载夹具 | 无。`test_frontend.py` 用模块私有的 `_read(name)`（`:84-85`）和 `WEB_DIR`（`:32`）直接 `read_text` |
| HTML 解析夹具 | 无。全仓无 `BeautifulSoup/lxml/html.parser`（grep 确认），所有"DOM 断言"都是**纯文本子串匹配** |
| 期望的视图名列表 | **不在 conftest**。唯一事实来源是 `app.js:117` 的 `var VIEWS` 与 `index.html` 的 `data-view`；测试里唯一被钉住的"名列表"是 `REQUIRED_IDS`（A01）与 `orchestrator._STEPS`（A10） |
| 认证夹具 | 有，但与 DOM 无关：`_patch_testclient_for_auth()`（`:302-320`，autouse session 级 `:326-331`）、`_reset_auth_store_cache()`（`:334-346`）、`AUTH_DB_PATH` 相对路径（`:158-160`） |

🟢 **conftest 整体不受前端重构影响**。唯一注意事项：`pytest_configure` 的动作 2（`:229-243`）从 `tests/` 目录跑时会把 `app/` 补进收集范围 —— 因为 `--doctest-modules` 存在，**若重构在 `app/web/` 下放任何 `.py`（例如"生成 CSS 的脚本"），它会被当成 doctest 模块收集**，而 `app/web` 没有 `__init__.py` → 收集 ERROR。约定：**`app/web/` 下永远不放 `.py`**。

---

## 6. 被钉住的中文文案（改措辞就破）

| 文案 | 钉在哪 | 分级 |
|---|---|---|
| `正在读取数据` / `正在制定分析计划` / `正在计算` / `正在生成图表` / `正在撰写报告` / `正在校验数字` | `tests/test_frontend.py:217-225`，实体在 `app.js:64-72` 的 `const STEP_LABELS` | 🔴 逐字比对，**改标点（如去掉"正在"）都红** |
| 同上 6 个 step 的**英文键名** `profiling/planning/execution/charting/reporting/validation` | `:240-247`，与 `app/agents/orchestrator.py::_STEPS` 全等 | 🔴 后端契约，前端不许自创名 |
| `接口文档` | `:454`（在 `index.html` 里找）；实体 `index.html:119`、`:649` | 🔴 |
| `/docs`、`/api/health`（链接 URL，非文案） | `tests/test_health.py:104-105` | 🔴 |
| 其余全部中文 UI 文案（`VIEWS` 里的 crumb/title/sub 18 条、状态徽章 `app.js:975-977` 的「未配置大模型，将使用规则模式」、错误提示、空态文案、KPI 卡标题 `app.js:2136-2171` 等） | **无任何测试钉** | 🟢 **可自由改写**。这是本次侦察给出的最大自由度 |
| `login.html` / `login.js` / `login.css` 里的**全部**文案与 DOM | **无任何测试钉**（只被 A16 白名单钉文件名） | 🟢 登录页 DOM 与账户菜单可随便改；只需守住 B02/B03/B06/B10 |

---

## 7. 最小破面改动顺序（≤8 步）

前提判断：**A16（7 文件白名单全等）是唯一的"结构性总闸"**。绕开它 = 一次改动省掉 A03/A04/A08/A12 四条连带返工。所以顺序的核心原则是 **"不拆文件、不改 id、不动常量声明的排版"**，破面从 13 条降到 3 条。

| 步 | 动作 | 为什么这一步破得最少 | 需要同时动的测试 |
|---|---|---|---|
| **0** | 先跑基线并存档：`pytest tests/test_frontend.py tests/test_auth_api.py tests/test_health.py` + `python scripts/check_docs.py`，记录 19/37/… passed、rc=0 | 本项目已有"基线不绿则变异用例失去证明力"的先例（`test_check_docs.py:238-257`） | 无 |
| **1** | `tokens.css` **只加令牌、不改名、不删 `:root`**，并把 §4.1 的 4 条过期注释（`app.css:282/283/284/775`）改成符号引用 | A19 要求 `app.css` **自己也保留一个含 hex 的 `:root`**，所以令牌只能"两边都有"；tokens.css 只动注释 → **零破面** | 无（只需更新 `design-system.md`、`final-report.md:168`） |
| **2** | `app.css` **内部重写组件层**：保持 ① 首个 `:root{…}` 块（21-117）完整、② `.btn*` 四条规则的选择器原文 + `color/background: var(--hex 令牌)` 写法、③ `.dropzone input[type="file"]` 的 sr-only 块（`clip-path` + `position:absolute`）、④ `.dropzone:focus-within`、⑤ `.topbar__product` 独占规则、⑥ **全文件恰好一条** `@media (max-width: 480px)` 且块内含 `.topbar__link{display:none}`、**不含** `llm-badge` | 这一步能覆盖 80% 的视觉重构量（版式、间距、卡片、表格、配色）而**只碰 A17/A18/A19**，而且只要 6 个锚点保留就 🟢 | 若换配色体系：改 `test_disabled_buttons_stay_readable` 的令牌表与阈值（A19）；其余不动 |
| **3** | `index.html` **保留 17 个 id、6 个 `data-view`、顶栏「接口文档」链接、`/api/health` 与 `/docs` 两个入口**，只重排外围结构（section 嵌套、class、内联 SVG 精灵 `index.html:57-84`）；**`<link app.css>` 必须在 `<script app.js>` 之前**；**新写的 SVG 不要加 `xmlns`** | 满足 A01/A04/B01 → 🟢。DOM 骨架不动，`app.js` 的 `$()` 一处都不改，风险从"前后端联动"降为"纯 CSS" | 无（若新增 id，A01 不要求它们存在，无感） |
| **4** | `app.js` **只动渲染函数体**，冻结顶部常量区（`:30-46` API 表、`:52 TERMINAL`、`:58 POLL_INTERVAL_MS`、`:62 STEP_ORDER`、`:64-72 STEP_LABELS`）的**文本与单行排版**；保持 `API.report(` 恰好一处调用；保持 4 个错误码字面量与 `data.error` 在 `app.js` 内 | 满足 A08/A09/A10/A11/A12/A13 → 🟢。**关键约定：不格式化这一段**（Prettier/单引号/多行数组都会造成假红） | 只有确实要改 6 条步骤文案时，才同步 `test_frontend.py:217-225` + `orchestrator._STEPS`（**后端优先**） |
| **5** | 登录页三件套（`login.html`/`login.js`/`login.css`）**自由重构**：DOM、文案、账户/初始化版式都可换 | 🟢 —— 实测**没有任何一条测试读这三个文件的内容**（§6 末行）。仅守：文件名不变（B06/A16）、`/static/login.js` 与 `tokens.css` 仍被引用、后端 FileResponse（B10）不动 | **顺带修 `tests/test_frontend.py:354/:356` 两处谎报行号**（改测试文件里的注释，属 Wave C） |
| **6** | 若方案 01 **确实需要拆文件/建目录**：把它单独成一步，**同一次提交里**同步 (a) `test_frontend.py:367-376` 白名单、(b) `scripts/check_env.py:242-249` 名单、(c) A12/A08 的扫描范围从 `app.js` 扩到"全部 `.js`"、(d) A04 的"外链禁令"扩到全部 7+ 文件 | 这是唯一"必须改测试"的路径；集中一次做，避免 A03/A04/A08/A12 分四次红 | 🔴 上述 4 处，一次性 |
| **7** | **加强守卫（Wave C，与重构同批）**：① `outline: none` 只允许出现在 `:focus` 自绘焦点环的相邻规则里（现在唯一合法处是 `app.css:695`，`login.css:47` 的注释声明了这个约定却无人核对）；② 用正则把 A13 的 HTTP 状态码禁令补齐洞（`\w+\.ok\b`、`status\s*===?\s*\d{3}`）；③ 新增"17 个 id 与 `app.js` 里 `$("…")` 取用的 id 双向一致"的守卫（今天没有任何测试保证 `index.html` 与 `app.js` 的 id 对得上，A01 只单向查 HTML）；④ `index.html` 的 6 个 `data-view` 必须与 `app.js` 的 `Object.keys(VIEWS)` 全等 | 现在这三件事**只靠注释和肉眼**，正是本项目最反对的"静默不亮"类缺陷（`test_frontend.py:228-235` 的 docstring 自己就是这么发现的） | 新增测试（不改现有） |
| **8** | 文档收尾：**只改"实现规格类"文档**（`ux-spec-final.md`、`ARCHITECTURE.md` 前端章节、`README.md` 文件名清单、`design-directions.md` 的过期视图论断），把 `file:NNN` 换成符号引用；**冻结** `ui-review.md`/`final-report.md`/`a11y-audit.md`/`DECISIONS.md`/`ARCHITECTURE.md` 的历史数字段（它们已声明"当时实测值，记录不改写"）。**`HISTORY_ROBUSTNESS.md` 一个字节都不要动**（§3.2：往里写前端行号会让 check_docs 变红，而 `.js` 无法建立代码侧锚点） | 48 小时追 154 个漂移锚点必然追不完，且**追的过程本身会造成 §3.3 的并行写冲突**（B14） | `scripts/check_docs.py` 无需改；跑一遍确认 rc=0 |

**避开的三个具体地雷（不占步数但会打断节奏）**：
1. README 里出现新的 `uvicorn app.main:app --port <新端口>` → `tests/test_error_format.py:118-140` 红（B07）。预览前端就用 8128。
2. 顺手改 `app/api/routes_workflows.py`（哪怕只加注释） → `test_check_docs.py` 的 14 条硬编码 218/219 行号变异用例假红（§3.3）。
3. Wave A 期间并行往 `docs/`、`app/` 写文件 + 同时跑全量测试 → `test_check_docs.py:289-315` 的"真仓库逐字节不变"断言假红（B14）。

---

## 8. 保留 / 加强 / 该放宽（守卫型断言的分类意见）

* **必须保留（防真实倒退，不许为了让测试绿而放松）**：A13（禁按 HTTP 状态码判断，超时是 200）、A17（sr-only 不许 `display:none`）、A18（480px 藏链接不藏徽章 —— 业务状态 vs 工具链入口的区分）、A19（禁用态不许 `opacity`，对比度可静态算准）、A05/A06（`data/` 不挂静态、`/static` 不越狱）、A04 的"零外链"（离线可演示）。这 7 条的 docstring 都写着"这是真实缺陷的回归锁"，放松它们等于把缺陷放回去。
* **建议放宽判据形式、不放宽意图**（这几条会因为"无关的代码风格改动"假红，是重构摩擦的主要来源）：A10 的**双引号**要求、A11 的**`TERMINAL` 必须单行**、A17/A18 的**选择器逐字 + 独占规则**、A18 的**"全文件只允许一条 480 断点"**、A19 的**"必须叫 `background:` 且必须是 var(--hex 令牌)"**。→ 改成 AST/更宽正则，或改为"取该规则声明块"的方式，同时把阈值（4.5、三个终态、6 个步骤）保持不动。
* **应当加强**：见第 7 步的第 7 项（4 条新守卫）。
* **可以删掉/合并**：无。19 条没有一条是纯冗余的（A02/A07 与 A03 有轻微重叠，但成本为零）。

---

## 附：本次侦察用到的核对方式（便于复现，均为只读）

```
wc -l app/web/*                          # 7 个文件 8889 行
./.venv/Scripts/python.exe -m pytest --collect-only -q          # 902 collected
./.venv/Scripts/python.exe -m pytest tests/test_frontend.py tests/test_auth_api.py -q   # 56 passed
./.venv/Scripts/python.exe scripts/check_docs.py                # rc=0，7 锚点 / 24 用例 / 12 实例 / 16 请求
```
文档锚点比对：正则 `` `(?:app/web/)?([\w.-]+\.(?:css|js|html)):(\d+)` `` 扫 `docs/**/*.md` + `README.md` → 215 条唯一引用；对每条读磁盘真实行，再在文档句里抽选择器/函数名与锚点 ±2 行做符号匹配 → 30 条确定失配 + 154 条符号失配 + 4 条指向不存在的文件。

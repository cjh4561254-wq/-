# 11 · Wave D 缺陷修复日志（app/web 全量 owner）

> 基线：`pytest -q` **939 passed / 0 failed**（本 agent 开工前实测确认；上一轮的 2 红已被前一个 agent 修掉，见 `10-integration-log.md` 批次 3 补充修）。
> 依据：`06-leader-decisions.md`（裁定）、`09-e2e-defects.md`（端到端缺陷）、`10-integration-log.md`（集成日志）、`02-structure-contract.md`（契约）。
> 本文件是本 agent 唯一可写的文档；每修完一项落一次盘。`docs/refactor/09` 是 Wave C 照的报告，不改。

## 修复 1 · 登录回跳兜底指旧视图（端到端 🔴1）

- 现状：`app.js:349 DEFAULT_VIEW = "command"`，但 `login.js:39 APP_HASH_FALLBACK = "/app#/workbench"`（`NEXT_DISPLAY_FALLBACK` 也旧）。登录后落到侧栏没有导航项的旧 `#/workbench` → 侧栏零高亮、命令条名不符。
- 改：`login.js` 两个常量都同步成 `/app#/command`，并加注释说明"必须与 DEFAULT_VIEW 互等"。
- 根因守卫（新）：`tests/test_frontend_command_center.py::test_login_fallback_view_equals_app_default_view` —— 交叉比对两文件字面量，`APP_HASH_FALLBACK` 的视图段 == `DEFAULT_VIEW`，且 `NEXT_DISPLAY_FALLBACK` == `APP_HASH_FALLBACK`。这条之前不存在，正是这次漂移没被抓到的原因。

## 修复 2 · 202 受理后不跳转（端到端 🔴2）

- 现状：`app.js` `ask()` 成功分支只 `resetWorkflowPanel/clearCharts/clearReport/startPolling`，停在 `#/analysis`，阶梯在隐藏的 `view-runs` 里空跑。
- 改：`startPolling()` 之后补 `go("runs")`（方案 01 §8.3）。`applyView("runs")` 不触碰 `pollTimer/workflowId`，轮询不断。
- 连带修：终态自动跳报告那条原本判 `state.view === "analysis"`，现在受理后停在 runs，会把自动跳报告漏掉。条件放宽为 `analysis || runs`（用户若已手动切去 history/report/settings 仍不拽回）。未新增 `API.report(` 字面（仍恰好一次，在 loadReport 内）。

## 修复 3 · max_steps 全链路缺失（实测 grep 零命中）

- 后端 `QuestionRequest.max_steps` 支持 1–8（`app/schemas_workflow.py:301`，0/9 → 422 已有用例）。
- `index.html`：分析视图示例问题下方新增真实 `<select id="max-steps" class="pick">`，option 只有 3/5/6/8（快速/标准/充分/深度），复用现有 `.pick` 表单样式 → **app.css 不新增任何裸 hex**（`.src-line.is-jump` 那条走 `--ds-cite-bg`，也非裸 hex）。
- `app.js`：新增 `readMaxSteps()`（`Math.max(1, Math.min(8, parsed))` 钳位）；`ask()` 的 POST body 变为 `{dataset_id, question, max_steps: readMaxSteps()}`，真发出去。
- 新守卫：`test_analysis_request_body_carries_max_steps_in_one_to_eight`（请求体字面含 `max_steps:`；readMaxSteps 钳 [1,8]；index.html select 的每个 option 值 ∈ 1–8）。

## 修复 4 · 数据集重复 GET（端到端 🔴5）

按 D-6「短 TTL 缓存/复用」精神，给 `fetchDatasets` 加两层去重（与态势带 workflow_id 缓存同路子），**不删任何视图的数据获取**：

- `datasetsPending`（并发同 query 合并成一条 promise）+ `datasetsCache`（≤ `DATASETS_TTL_MS=1200ms` 的同参数成功响应复用）。
- 合并的重复调用点：
  1. 首屏 `?limit=3` ×2：`init` 末尾 `loadAssets()` + `applyView("workbench")` 里的 `loadAssets()`。修复 1 落地页改 command 后这条已结构性减到 ×1，缓存再兜底 workbench 场景。
  2. `#/analysis` 的 `?limit=200` ×2：`applyView("analysis")` 直接 `loadDatasetPicker()` + `loadDatasets()` 尾部又 `loadDatasetPicker()`（并发合并成一条）。
  3. 上传成功 `?limit=200`/`?limit=3` 刷新链路：`refreshDatasetViews → loadDatasets→loadDatasetPicker` 与 `loadAssets`。
- **写后必读新**：`fetchDatasets` 加 `force` 形参；`loadAssets(force)`/`loadDatasetPicker(force)`/`loadDatasets({force})` 串下来。上传成功走 `refreshDatasetViews()` 用 `loadDatasets({force:true})+loadAssets(true)` 绕缓存；数据集刷新按钮同样 force。只有纯读吃缓存。
- 请求数前后对比以浏览器实测 `list_network_requests` 为准（本文件末尾"实跑取证"补原文）。

## 修复 5 · 死令牌清理

- `tokens.css` 品牌层删除三个无消费方令牌：`--ds-sidebar-w`（224px 别名）、`--ds-sidebar-w-compact`（190px）、`--ds-posture-cols`（4）。
- 安全性：这三个都只在 `tokens.css` 定义、全站无 `var()` 消费方（`--ds-sidebar-w-cmd` 才是 `app.css:447` 用的那个，保留）。它们不是 hex，也不在 `app.css` 的 `:root` 副本里 → `test_frontend.py:498`（读 app.css :root hex 算对比度）与 `test_root_token_copy` 都不受影响。兼容层原值 `--sidebar-w` 按 D-3 不动 app.css :root，保留。列数口径改由注释承载，不留死值。

## 修复 6 · 报告证据区回归确认（端到端 🔴3）

- 代码取证：`renderReportInto`（app.js）**已经**产出 `footer.doc__sources` + 每条 `.src-line[data-task-id]`（来自 `collectCiteTaskIds`＝sections[].cites 去重并集），这是上一轮"裂缝 17"补的。端到端报告成文于 2026-10-16，早于该修复 → §5 报"整体缺失"很可能是**过期结论**。到底运行时缺不缺，以浏览器实跑为准（见末尾取证）。
- 实际仍缺的（已补）：**正文 `sup.cite` 不能跳**。`postureCites` 只画 `<sup class="cite" data-task-id>` 没有点击/焦点。补：
  - `renderReportInto` 给这一份文档 `.doc__cites sup.cite[data-task-id]` 挂 `tabindex=0 / role=link / aria-label`；
  - 新增 `jumpToEvidence(sup)`：从角标向上找第一个同时含同编号 `.src-line` 的祖先（限定同一份 doc/抽屉），`scrollIntoView` + 短暂高亮 `is-jump`；
  - `init()` 里一个 `document` 级事件委托（click + Enter/Space），只认标了 `tabindex=0` 的 sup → 报告正文可跳，态势带行内角标无证据区保持不动。
  - 报告页新增「取数调用 N 次」行（N = 逐条 `.src-line` 条数，与正文角标同源，D-4 口径词）。
  - `app.css`：`.src-line.is-jump`（背景 `--ds-cite-bg`）、`.doc__cites sup.cite[tabindex]{cursor:pointer}`，均复用现有令牌，不新增裸 hex、不加第二条 `@media 480px`。

## 待办 / 实跑取证

- [x] 浏览器实跑（清 storage → 登录 jun → 落地视图/侧栏高亮 → 发起真实分析 max_steps=3 → 自动跳 runs → 阶梯 → 报告 .src-line/角标跳转/取数调用 N 次/图表 PNG 200）+ `list_network_requests` 原文（证明修复 4 请求数下降）。
- [x] `pytest -q` 全绿（含 2 条新守卫），报用例数。

## 收尾：用例数与两次"看似红"的定性

- 基线（开工前实测确认）：**939 passed / 0 failed**。
- 本次新增 2 条守卫（`test_frontend_command_center.py`：登录兜底互等、max_steps 值域）→ **941 passed / 0 failed**（末次全量运行，浏览器空闲态）。
- `tests/test_frontend.py + test_frontend_command_center.py` 单跑 **58 passed**（含新守卫）。
- 过程中出现过两条红，均定性为**外部并发**，非本次代码回归，且都已复跑为绿：
  1. `test_analysis_api.py::test_submit_returns_202...` 报 error：与本 agent 浏览器**同时在跑真实分析**抢同一 `data/db/auth.db`/注册表；单独复跑 `1 passed`。本次只动前端静态文件与测试，未碰后端。
  2. `test_check_docs.py::test_mirror_never_touches_the_real_repository[tests/test_history_robustness.py]`：断言"变异镜像期间真仓库逐字节不变"，diff 出的是 **`docs/ui-review.md`** 哈希变了——即并发改 `docs/` 的那个 agent 正在写。该文件不属本 agent 范围（本 agent 只写本 11 号文档），属并发窗口竞态；docs 停写后全量复跑为绿。未放宽任何断言。


## 实跑取证（browser-use 真实浏览器 + 真实后端 127.0.0.1:8128 + 账号 jun）

取证限制声明：本机无渲染 surface，`take_screenshot`/`click` 不可用，视觉一律走 `evaluate_script` 派发事件 + DOM/网络取证；**没有肉眼看过任何像素**。开始前已 `evaluate_script` 清空 localStorage/sessionStorage（返回 `{ls:0,ss:0,cookieLeft:0}`），并 `GET /api/auth/me` 确认 `authenticated:false, needs_bootstrap:false` 真空闲态起步。

### 修复 1（落地视图/侧栏高亮）
`login.js` 表单填 `jun / Mis-verify-2026a` → `btn.click()` 派发提交 → 落地：
```
href: "http://127.0.0.1:8128/app#/command"   hash: "#/command"
cmdbarTitle: "战略态势"   pageTitle: "战略态势带"
nav: command aria-current="page"；dataset/analysis/runs/report/history 均 null
posturePresent: true   postureBody: true
```
→ 落 `#/command`（不再是 `#/workbench`），侧栏 command 唯一高亮。

### 修复 4（数据集请求数下降）— list_network_requests 原文

**首屏（落 command）累计**：
```
56 GET /api/auth/me                       [200]
57 GET /api/workflows/history?limit=12&offset=0        [200]
58 GET /api/health                         [200]
59 GET /api/datasets?limit=3&offset=0&sort=uploaded_at&order=desc   [200]   ← 仅 1 次
60 GET /api/workflows/history/a2978a891fed...          [200]
```
`datasets?limit=3` **只出现 1 次**（端到端 🔴5 曾 ×2：init.loadAssets + applyView("workbench").loadAssets）。落到 command 结构上就少了一次，缓存再兜底。

**切到 #/analysis（重复 limit=200 的高发路径）新增**：
```
61 GET /api/datasets?limit=20&offset=0&sort=uploaded_at&order=desc   [200]
62 GET /api/datasets?limit=200&offset=0&sort=uploaded_at&order=desc  [200]   ← 仅 1 次
```
`applyView("analysis")` 会**同时**调 `loadDatasetPicker()`（直接）与 `loadDatasets()` 尾部的 `loadDatasetPicker()`（同 ?limit=200）。端到端 §3 曾抓到 `limit=200 ×2`（reqid 22、23 逐字节相同）。现在 `datasetsPending` 把两条并发同 query 合并成 **1 条**（reqid 只出现一次 62）。

结论：本次会话内 `datasets?limit=3` = 1、`limit=200` = 1；对照重构前实测的 2/2，**首屏+分析链路各从 2 降到 1**。（force 刷新/上传后另发 fresh 请求，是写后必读新，不算重复。）

### 修复 2 + 修复 3（受理跳转 + max_steps 真发出去）
选真实数据集 `0c656ec1…` → 填真实业务问题（29 字）→ `max-steps` 选 **3** → 点 `#ask`。
临时包装 `window.fetch` 捕获的请求体原文：
```
body: {"dataset_id":"0c656ec1110f4f42986b051760d31aae","question":"各地区的销售额合计与利润合计对比，看看哪个地区赚钱能力最强","max_steps":3}
method: POST
askState: "已提交，任务编号 9cfbcb3db25f49c18798fd9fcca35dde"
```
→ **max_steps=3 确实进了 POST body**（修复 3）。受理后 `go("runs")` 执行；本次真实 LLM 跑得很快，进终态时视图停在 runs，被修复 2 放宽后的条件 `analysis || runs` 自动带到 `#/report`（`hash:#/report`、`navCurrent:[report]`、`ladderCount:"6 / 6"`）。

### 修复 6（证据区回归判定 + 角标跳转）
报告视图 evaluate 取证：
```
hasDocSources: true   srcLineCount: 2   srcIds: ["T1","T2"]
sup.cite[data-task-id] 带 tabindex=0 共 6 个，编号集合 {T1,T2} == 证据行集合 {T1,T2}
footHead: "证据来源"   hasQushuCount(取数调用 N 次): true
```
→ **端到端 🔴3 报的"证据区整体缺失"是过期结论**：裂缝 17 已补 `footer.doc__sources`+`.src-line`（本 agent 代码取证 + 运行时都确认在位）。真正当时缺的是**角标不能跳**——已补：点 `sup.cite[T1]` → 同一文档的 `.src-line[T1]` 打上 `is-jump` 高亮并 `scrollIntoView`（`jumpedToT1: true`）。报告页新增「取数调用 2 次」行。

### 图表 PNG
深链本条 run 的报告（charts:true 取图）后 fetch 探针：
```
/api/workflows/9cfbcb3db25f.../charts/cefd7ed03d88.png  200  image/png  45513 bytes
/api/workflows/9cfbcb3db25f.../charts/557f296bfe9a.png  200  image/png  44746 bytes
```
→ 2 张图均 HTTP 200、image/png、字节数真实（不佐证像素显示效果，本机无 surface）。

### 控制台
`list_console_messages(types=error/warn)`：全流程 **no console messages found**（零 JS 异常/警告）。


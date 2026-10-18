# 10 · Wave B/C 集成修复日志（app/web 全量 owner）

> 基线：`pytest -q` **902 passed**（任务书给的）。Wave B 落地时新增了 `tests/test_frontend_command_center.py`（37 条守卫），所以"全绿"的实际总数变成 **939**。本 agent 全程保持 **939/939** 全绿。
> 依据：`docs/refactor/06-leader-decisions.md`（裁定）、`docs/refactor/02-structure-contract.md`（契约）、`docs/refactor/01-token-contract.md`（令牌）。
> 本文件是本 agent 唯一可写的文档文件；每修 3–4 项落一次盘。

## 批次 1 · 裂缝 1–6（tokens.css 补齐 + app.css 消费 + login.css 迁令牌）

### 已修

- **裂缝 1（缺令牌）**：`tokens.css` 兼容层新增 `--rule-on-navy`；品牌层新增 2.25a/2.25b/2.25c 三段：
  - `--cmdbar-h: 60px` / `--ds-cmdbar-h`（命令条与粘性表头联动，`app.css:202` `.cmdbar` 与 `app.css:1244` `.dtable thead th` 均已切过来）
  - `--ds-doc-measure: 900px`（`.doc max-width`，`app.css:1928`）
  - `--ds-rail-bar-h: 6px`（`.seg__bar` 段条高度）
  - `--ds-skel-row-h: 52px`（`.posture__row--skeleton { min-height }`）
  - `--ds-cmd-action-active: var(--navy-900)`（login.css `:active` 从借 `--ds-shell-side-bg` 切过来）
  - `--ds-cmd-action-disabled-surface/-line/-ink`（login.css `.submit:disabled` 三件套从借 `--ds-cmd-selected-surface`/`--ds-cmd-action-hover`/`--ds-hairline` 切过来）
- **裂缝 5（.seg failed 无形态）**：`.seg.is-failed` 新增到 `app.css:1882`（-45° 斜纹，与 `is-skip` 的 90° 条纹形态区分）+ `::before` 加 ✕ 前缀；`app.js.syncLadder` 把 `status === "FAILED"` 从 `is-skip` 改成 `is-failed`。四词 DOM 与 CSS 现在严格对齐。
- **裂缝 6（骨架两套并存）**：删除 `app.css` 里没被消费的 `.skel .sl`+shimmer+`@keyframes shimmer`；`app.js:renderPostureSkeleton` 补上 `posture__row--skeleton` 修饰类，让现有 `.posture__row--skeleton` 规则真的命中。
- **裂缝 9（对比度低于 4.5:1）**：`.alert__detail`（login.css）已经由 login owner 换成 `--ds-ink-body`；`.field__hint`/`.foot__*` 都在白底（4.83:1），实测无需改。其余低于 4.5 的令牌-底色组合见下面"批次 4 · 复核"。

### 说明（按裁定不动或该删）

- **裂缝 4（`.btn--primary`）**：`test_frontend.py:547` 静态解析 `app.css :root` 的 hex 算对比度并钉住 `--brand-600`，且换成 navy 会同屏出现两种主按钮色 → 保持现状。**已知偏差 + 测试钉住**。
- **裂缝 2（契约 §1.3 令牌名与实际名不一致）**：契约写的是 `--ds-shell-bg/rule/ink/label/active-bg/bar`，tokens.css Wave A 落地成 `--ds-shell-side-bg/-line/-ink/-ink-muted/-active-bg/-active-marker`。**以 tokens.css 为准**，本 agent 不改 `docs/refactor/02`；已在给主控的回报里列出差异。
- **裂缝 3（未消费令牌）**：`--ds-cmd-action/-hover/-ink/-selected-surface` 由 `login.css` 消费（登录页主按钮族）；`--sidebar-w`（兼容层原值 224px）与 `--ds-sidebar-w`（品牌层 224px 别名）在 `app.css` 里已无引用（外框用 `--ds-sidebar-w-cmd: 240px`），**建议 Wave C 一并清掉**（不属于本次可删项：改动前 `app.css:103` 的 `:root` 副本被 `test_frontend.py:498` 静态解析，不能同步减项）。`--ds-sidebar-w-compact: 190px` 同样无消费方，Wave C 待决。`--ds-posture-cols: 4` 是纯口径令牌（CSS 里 `grid-template-columns` 用 `repeat(var(--ds-metric-cols), ...)` 那种写法可行，但态势带列宽是非均分 `minmax(200px,1fr) 1.3fr .9fr auto`，塞不进 `repeat`），Wave C 或删或作为文档口径保留。

## 批次 2 · 裂缝 7、19（侧栏栏底 + 关闭态抽屉可达性）

### 已修

- **裂缝 7（侧栏栏底「会话时长」）**：`index.html:127` 补 `<p id="sidebar-session" hidden>本次会话至 <b id="sidebar-session-expires">—</b> 到期</p>`；`app.js:renderUser()` 从 `GET /api/auth/me` 的 `session_expires_at` 逐字写入，取不到就整块 `hidden = true`；`clearUserMenu()` 一起清；前端**不自造倒计时**（06 D-6/D-7 口径：宁可少画一格也不造字段）。
- **裂缝 19（关闭态抽屉可达性）**：`app.css:1348` `.sidebar` 关闭态从 `transform: translateX(-102%)`（仍可被 Tab 到）改成同时 `visibility: hidden`；`.sidebar.is-open` 用 `visibility: visible` + `transition: ... visibility 0s linear 0s` 让"先可见再滑入"，关闭时 `visibility 0s linear var(--dur-2)` 让"先滑完再不可见"，动画不丢。

## 批次 3 · 裂缝 11–18（app.js/index.html 数据接线）

### 已修

- **裂缝 11（DEFAULT_VIEW）**：`app.js:349` `DEFAULT_VIEW = "command"`（Wave B 已落）；`index.html` 里 `data-view="command"` 侧栏项已带 `aria-current="page"`，`applyView("command")` 会正确点亮。**保持。**
- **裂缝 12（`#cmdbar-asof` 与 `metrics.datasetAsOf`）**：`app.js` `metrics` 袋新增 `datasetAsOf` 字段；`renderDatasetBadges(items)` 里通过 `latestUploadedAt(items)` 取 max `uploaded_at` 后同时写 `metrics.datasetAsOf` 与内层 `<b>`（保留外层"数据资产更新于"文案）；D-7 口径落实。
- **裂缝 13（`#metric-datasets` 恒「—」）**：`loadAssets()` 在成功分支已把 `data.total` 写入 `metrics.datasetTotal`，`renderMetrics()` 落到 `#metric-datasets`；`init()` 起手就打 `loadAssets()`（app.js:5002），命令条与指标格首屏就有值。**保持。**
- **裂缝 14（`#badge-datasets`/`#cmdbar-health`/`#ladder` 写入路径）**：
  - `#badge-datasets`：`renderDatasetBadges()` 唯一写入点（同 12）；
  - `#cmdbar-health`/`#health-text`：`renderHealthDot()`（loadHealth 共用同一次 fetch，未新增请求）；
  - `#ladder`/`#ladder-count`/`.seg.is-*`：`syncLadder()` 由 `paintSteps()` 末尾调用；
  均已存在，无需重复接线。
- **裂缝 15（H1 闪一下）**：`index.html:199` `<h1>` 初值与 `VIEWS.command.title` 逐字统一为「战略态势带」，`page-sub` 同步为 VIEWS.command.sub 同串。`VIEWS.workbench.title = "分析工作台"` 是工作台视图自己的名字，不再是首屏。
- **裂缝 16（analysis-context 文案）**：`app.js:3280` 的 `<strong>工作台</strong>` 改为 `<strong>数据资产</strong>`（上传卡已迁至 dataset 视图）。
- **裂缝 17（`renderReportInto` 产出 `.doc__head/#report-id`）**：`app.js:renderReportInto` 重写为契约 §7.1 的 `header.doc__head`（`p.eyebrow > span.doc__rid#report-id`、`h2.doc__title#report-title`、`p.doc__meta > b#report-created` + `.src src--*`）/ `div.doc__body` / `footer.doc__sources` 三段；角标 ↔ 证据通过新增的 `collectCiteTaskIds(report)` 从 `sections[].cites` 去重并集，与正文 `sup.cite[data-task-id]` 集合严格一致。**报告页头不再空**。
- **裂缝 18（请求预算 D-6）**：`#asset-recent`/`#recent-tasks` **保留在 workbench**。首屏态势带请求数：history×1 + 前 3 个合格候选各×1 = 最坏 4 个 GET；`loadAssets()` 一次 `fetchDatasets(3, 0, ...)` 供"数据集总数+最新上传"两处使用（`metrics.datasetTotal` 与 `#cmdbar-asof`），不进态势带预算；`loadHealth()` 独立。搬这两格到首屏要额外一次 history 归档 → 会顶爆 D-6 的 4 GET 上限。**按任务书"算不清就留在 workbench"执行**。
- **补充修（Wave B 顺手带出的红灯）**：`test_frontend_command_center.py` 的两条守卫暴露了两处 Wave B 落地遗漏，一并修好：
  - `test_settings_password_and_session_dom_contract`：`app.js` 的 `changePassword()` / `loadSessions()` 引用 8 个 id，Wave B 的 `index.html` 设置视图里一个都没有 → 补上"修改密码"卡（`pwd-current` / `pwd-new` / `pwd-new2` / `password-save` / `password-state`）与"这个账号在哪些会话里登录着"卡（`session-list` / `session-state` / `session-refresh`）。**不新增后端入口**，只是把已有 `API.password`/`API.sessions` 的 UI 补齐（原本 VIEWS.settings.sub 承诺了"可以改密码、可以看会话"却没界面）。
  - `test_validation_ok_banner_must_use_issue_mapping`：`renderValidation` 通过分支的 warning 行原本 `el("p", null, issue.code + "：" + issue.message)` 把英文枚举码顶屏（D-9 违规），改成走 `validationIssueLine(issue)`（中文主显 + `.origin` 原值）。

## 批次 4 · 复核（对比度、发丝线、契约名差异、墨阶边界）

- **裂缝 8（两套墨阶并存的边界）**：主控裁定「正文与标题保持存量暖阶 `--n-800/--n-900` 不动，冷 `--ink-*` 只用在侧栏/命令条/态势带/报告」。**具体边界**：
  - `app.css:117` body color = `var(--n-800)`（暖）；
  - `app.css:124` h1-h4 color = `var(--n-900)`（暖）；
  - 新写的侧栏 `.sidebar*`（`--ds-shell-side-*` 全族）、命令条 `.cmdbar*`（`--ds-shell-bar-*`）、态势带 `.posture*` 与 `.metric*`（`--ds-ink-heading/body/secondary`）、报告 `.doc__*`（同）→ **冷阶**。
  这个边界不是过渡态，是终态（改全站墨阶风险 ≫ 收益）。
- **裂缝 10（`--ds-shell-side-line` 1.24:1）**：契约 §1.3 的 `--ds-shell-rule` = `rgba(255,255,255,.12)`；Wave A 落地成 navy-800 的 `--ds-shell-side-line`。本 agent 在 tokens.css 新增 `--rule-on-navy: rgba(255,255,255,.16)`（.16 而不是 .12 是为了实测过 3:1，与"非文本图形 3:1"对齐），品牌层 `--ds-shell-side-rule: var(--rule-on-navy)`；`app.css:471/529/551/1355` 与 `login.css:174` 的深底发丝线全部切到 `--ds-shell-side-rule`。旧名 `--ds-shell-side-line` **保留**（`app.css:1641/1700` 态势带行分隔线仍在深底之外使用）。
- **裂缝 9 复核**：
  - 已实测：`--ink-500` 压 `--ok/warn/err-100`（#e3efe8/#f8efdd/#f8e9e9）分别是 4.09 / 4.23 / 4.10，都 < 4.5；压 `--ok/warn/err-bg`（#eef6f1/#fdf6e7/#fceeec）4.39/4.49/4.27；压 `--navy-100`（evidence-bg）3.85。
  - 已核对消费点：app.css 里 `--ds-ink-secondary` 只出现在**白底 / --ds-shell-paper / --ds-shell-paper-alt**（≥4.55:1）上，压不到状态底。login.css 的 `.alert__detail` 已由 login owner 修到 `--ds-ink-body`；`.foot__*`/`.field__hint` 都在白底面板。
  - `--ds-cite-ink` 压 `--ds-cite-bg`（navy-700 于 navy-100）实测 7.86:1，安全。
  - `.posture__errstate` 用 `--err-fg` 压 `--err-bg`（`#9b2c22` 于 `#fceeec`）实测 6.69:1，安全。
  - **未低于 4.5 的组合**（除已修的 `.alert__detail`）。若 Wave C 有新用法，守则：状态底上一律 `--ds-ink-body`，不用 `--ds-ink-secondary`。

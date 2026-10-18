# 09 · Wave C 端到端实测缺陷记录

> 实测人：Wave C E2E 照（browser-use 真实浏览器 + 真实后端 `127.0.0.1:8128`，模型 deepseek-flash 真实调用）。
> 实测日期：2026-10-16（UTC 16:38 起）。本文**只记录缺陷，不含修复代码**。
> 取证方式限制：本机 `take_screenshot`/`click` 可能不可用，全部证据来自 DOM 快照（take_snapshot）、`evaluate_script`、`list_network_requests`、`list_console_messages` 与 curl 原文。**未肉眼看过任何像素**，视觉判断留给主控。
> 严重度分级：🔴 阻断（功能坏/报错/英文上屏/假数据/假按钮）｜🟡 偏差（与 02/05/06 或 option-01 不一致）｜🔵 建议。

---

## 0. 环境与前提核查（先于一切 UI 测试）

### P0-1 🔴（测试前提阻断）「账号库为空」这一环境前提**不成立**，首次部署态无法按原计划实测

- **现象**：主控给的前提是「`data/auth.db` 的 users 表 0 行 → `needs_bootstrap=true` 首次部署态」。实测 `GET /api/auth/me`（匿名）返回 `"needs_bootstrap": false`。
- **根因**：主控核查时探错了文件。应用配置的账号库默认路径是 **`data/db/auth.db`**（`app/config.py:129` `auth_db_path: str = "data/db/auth.db"`；`scripts/manage_account.py:23` 同样声明），**不是** `data/auth.db`。
  - `data/auth.db` 是一个 **0 字节空文件**（无任何表；疑似被某次 sqlite 探活命令顺手创建，或是上一个会话的残留）。它不参与任何运行时逻辑。
  - 真实库 `data/db/auth.db` 里 `auth_users` 表已有 **1 个账号**：`id=2, username='test-admin', display_name='测试管理员', created_at=2026-10-16T03:52:32Z, last_login_at=2026-10-16T10:20:15Z, is_active=1`，另有 `auth_sessions` 23 行。
  - 顺带记录：该账号 `iterations=1000`，而配置默认 `auth_pbkdf2_iterations=240_000`（`app/config.py:150`），且 `hash_password` 文档注释写"产品用配置默认值，并有用例断言默认值不低于 240000"（`app/core/auth.py:103-111`）——说明 test-admin 是在**调低了 PBKDF2 迭代的环境变量下**建的号。对本地测试无害，但如果这个库会被当成"交付态"演示，密码哈希强度与文档承诺不符。
- **后果**：登录页只会呈现「日常登录」形态；`needs_bootstrap=true` 触发的「创建第一个管理员」形态在本环境**不可能自然出现**。步骤 1 的建号路径（bootstrap UI）被封死——`POST /api/auth/bootstrap` 在非空库上按设计返回 403（`app/api/routes_auth.py:169-173`，实测见下）。
- **处置（如实声明）**：为解除对步骤 2–8 的整体阻断，我使用**项目自带的运维脚本** `scripts/manage_account.py create`（该脚本存在的理由就是"本项目没有自助注册，第二个账号只能从服务端建"——见其模块 docstring）建了测试账号 `jun`。**这不是通过应用 API 建的**，因此「bootstrap 全链路（UI 表单 → POST /auth/bootstrap → 自动签发会话）」这一段**属于未实测**，只做了静态代码核对 + DOM 强制渲染核对（见 §1）。
- **疑似归属**：非应用代码缺陷，属**环境/交接前提错误**；但「仓库里同时存在 `data/auth.db`(0B, 无用) 与 `data/db/auth.db`(真实)」这对近邻路径是本次误判的直接诱因，建议清理或文档标注（🔵）。

**证据（curl 原文）**：
- `GET /api/auth/me` → `{"ok":true,"data":{"authenticated":false,"user":null,"needs_bootstrap":false,"session_expires_at":null}}`
- `sqlite3 data/db/auth.db`：`auth_users` 1 行（见上），`auth_sessions` 23 行；`data/auth.db` 大小 0 字节、`sqlite_master` 为空。

### P0-2 建号记录（测试账号凭据，供主控真浏览器复登）

- 用户名：`jun`；密码：`Mis-verify-2026a`；显示名：`验证用账号`。
- 建号命令：`MIS_NEW_PASSWORD='Mis-verify-2026a' .venv/Scripts/python.exe scripts/manage_account.py create --username jun --display-name 验证用账号`（输出见 §0 附）。
- 该账号经 `validate_password_strength`（应用层同一把尺子）校验通过。
- 凭据终态确认：§7 的改密回环后，`POST /api/auth/login {jun / Mis-verify-2026a}` **实测 200**（curl，改密后最终态），用户可直接用这组凭据在真浏览器登录验收。

---

## 1. 首次部署态 / 登录页（部分实测 + 部分静态核对）

### 1.1 日常登录形态（真实实测，匿名 `needs_bootstrap=false` 走此形态）✅ 基本合格

- 打开 `/login`（已先 `evaluate_script` 清空 localStorage/sessionStorage，均为 `{}`，浏览器无残留 storage；`document.cookie` 为空）。
- DOM 快照证据：`h1「欢迎回来」`、副文案「用你的工作台账号继续未完成的分析。」、`#username`（`autocomplete="username"`）、`#password`（`autocomplete="current-password"`）、显示密码按钮（`aria-pressed`）、提交按钮初始 `disabled` 且 `describedby="submit-reason"`、skip-link「跳到登录表单」、`#alert` `{role:"alert", aria-live:"assertive", hidden:true}`。
- 页脚「接口文档」链接存在（`/docs`）。无英文上屏。

### 1.2 「创建第一个管理员」bootstrap 形态 —— **未能端到端实测**（受 P0-1 阻断），改为「CSS 强制显形 + 代码核对」

- 方法：`document.documentElement.setAttribute('data-auth-mode','bootstrap')` 强制显形（纯 CSS 开关，`login.css:218-240`），**未**经过服务端 `needs_bootstrap=true` 驱动。
- 强制显形后的 DOM 核对结果（全部符合预期）：
  - eyebrow「首次部署」✅；h1「创建第一个管理员」✅；副文案「这个部署还没有任何账号，第一个管理员由你在这里设定；创建成功后这个入口不会再出现。」✅
  - `#display-name`：`name="display_name"`、`autocomplete="name"`、非必填、提示「留空时，显示名称就用用户名。」✅
  - `#password-confirm`：`name="password_confirm"`、`autocomplete="new-password"`、提示「这一项只在本地比对，服务端收到的请求里没有它」✅（契约 §8.2 要求写明的"仅本地校验"句已存在）
  - 提交按钮文案切换为「创建管理员并进入」✅；登录形态各元素同步隐藏 ✅
- **未覆盖项（如实登记）**：`applyMode()` 在真实 bootstrap 下把 `#password` 的 autocomplete 改为 `new-password`（`login.js:494-496`）与 `document.title` 改「首次部署 · …」（`login.js:488-490`）由 JS 驱动，本次强制显形只动 CSS 开关、未跑 `applyMode`，所以这两项**只有代码证据、没有运行时证据**。
- bootstrap 服务端闸门（实测）：`POST /api/auth/bootstrap`（探针用户名）→ **403** `{"code":"FORBIDDEN","message":"账号库已经有账号了，不能再执行初始化","detail":"/api/auth/bootstrap"}`。竞态分支 `login.js:981-987`（403 → 「这个部署已经有账号了」+ 切换到登录出路）代码在位，无法在本环境触发（需真空库）。
- 🟡 小瑕疵：403 响应的 `error.detail` 直接放了请求路径 `/api/auth/bootstrap`，语义偏弱（detail 本应承担"原始诊断"）。疑似归属：`routes_auth.py` 的错误封装（`app/errors.py`/`AppError`），前端按 D-9 会把它收进折叠区，上屏风险低。

### 1.3 登录成功回跳落错视图 🔴（本轮最确定的新缺陷）

- **现象**：`/login` 用 `jun` 登录成功后，浏览器落到 **`/app#/workbench`**（旧「工作台」视图），而不是首屏战略态势带 `#/command`。
- **证据（DOM 快照原文）**：落地页 `url="http://127.0.0.1:8128/app#/workbench"`；命令条 `#cmdbar-title`＝「工作台」；`h1#page-title`＝「分析工作台」；主区是旧内容（region「数据与任务统计」、h2「数据概览」、h2「最近上传」、h2「最近分析任务」），**没有** `.posture` 态势带；侧栏 6 个 `.nav__item` **全部无** `aria-current`（导航里根本没有 `data-view="workbench"` 项）。
- **根因指向**：`app/web/login.js:39` `var APP_HASH_FALLBACK = "/app#/workbench";`（无 `next=` 时的登录回跳兜底）。Wave B 把默认视图改成了 `command`（`app.js:349 DEFAULT_VIEW="command"`，`app.js:1307` 注释也说"首屏就是态势带"），但 login.js 的兜底常量没跟上。
- **复现**：清 cookie → 开 `/login` → 输入正确账号密码提交 → 观察落地 hash。
- **影响**：用户完成注册/登录流程后第一眼看到的是与导航失联的旧工作台（侧栏零高亮、命令条名不符），恰是任务要求的"登录后首屏必须看到战略态势带"的反例。疑似归属：`login.js`。

### 1.4 `workbench` 与 `command` 双视图并存，违背 02 契约的"单一首屏键" 🟡

- **现象**：`section.view` 实际有 8 个：`command / workbench / dataset / analysis / runs / report / history / settings`（evaluate 取证）。`VIEWS` 里 `command`（战略态势）与 `workbench`（工作台）并存（`app.js:291-300`），`VIEW_GROUP` 各归"指挥"（`app.js:337-340`）。侧栏只挂 `command`，`workbench` 成为**导航不可达、仅靠旧链接/回跳可达**的孤儿视图，且内容与 `command` 高度重叠（旧 statstrip/最近任务面板）。
- **契约对照**：02 契约 §0.1 定案「**不改键名**，workbench 显示名改"战略态势"」且明确警告改键名会破坏深链；05 §7 接线点却写「`VIEWS` 增加 `command`」。两份 Wave A 文档互相冲突，实现取了后者、留下了前者没删干净。`app.js:1305-1307` 的注释自认"没有 command 宿主就留在 workbench"。
- **风险**：旧深链 `#/workbench`、`applyDeepLink`、书签、以及本次 1.3 的回跳都会落在一个不在导航里的页面。需要主控裁定：删 `workbench` 或让它重定向到 `command`。疑似归属：`app.js` + `index.html`。

### 1.5 其它登录页核对项 ✅
- 「接口文档」文案（非「API 文档」）✅（D-11）。
- 三形态中「失败态」文案表在 §9 实测。


---

## 2. 登录后首屏 · 战略态势带（空态实测）

**先说结论**：`#/command` 视图本体的空态是合格的真实空态（见 2.2）；但**登录后默认落不到它**（见 1.3 🔴），且落地时 `#/workbench` 会出现"侧栏无高亮 + 命令条名不符 + 重复请求"（见 2.3/2.4）。

### 2.1 ✅ 通过项
- 侧栏 3 组 6 项：指挥（战略态势 / 数据资产）、分析（发起分析 / Agent 运行）、成果（分析报告 / 历史记录），与 02 §1.2 一致；当前项 `aria-current="page"` 只挂在 `command`（evaluate 取证）。
- 命令条：`#cmdbar-title`「战略态势」、`数据资产更新于 —`（D-7 落实）、`服务正常`、`#llm-badge`「已配置大模型」、唯一主按钮「发起新分析」；**全页 `document.body.innerText` 不含「数据截至」**（evaluate 断言 `false`）。
- 「接口文档」链接 DOM 存在（但当前隐藏窗口宽度下 computed `display:none`，命中 `@media(max-width:480px)` 的 `.topbar__link` 让位规则——本机 `innerWidth===0` 属无 surface 视口，**不能据此判定真机也隐藏**，留给主控肉眼验收）。
- `.metrics` 4 格（`#metrics-grid`）：数据集总数 / 运行中任务 / 已出报告 / 分析失败，每格带真实来源与样本口径小字（"样本＝最近 0 条归档记录"、"服务端没有全站「运行中」列表接口"）——符合 D-7/首屏硬约束（无销售额等业务指标凭空出现）。

### 2.2 空态（真实实测：items=0 → 场景 6）
- `#posture-body` 渲染 `.posture__empty[role="status"]`：h4「还没有任何分析记录。做完第一次分析后，这里会出现它。」+ p「第一个结论会在你完成第一次分析后出现在这里。」+ 按钮「去发起一次分析」。全中文，无英文残留；与场景 5（归档关着）/场景 7（有记录无报告）文案可区分（`app.js` 三态分支在位，代码核对）。
- 🟡 偏差：该空态 CTAs 是 `<button class="btn btn--ghost btn--sm">`，契约 02 §3.4 规定空态动作是 `a.btn--primary.btn--sm href="#/analysis"`（可右键新开的真链接）。行为是否等价跳转：待 §4 点按验证（本轮用 hash 导航代替点击，见环境限制）。

### 2.3 🟡 落地视图（workbench）与侧栏脱钩的连带症状
- 现象：`#/workbench` 激活时，6 个 `.nav__item` 的 `aria-current` 全为 `null`（该视图没有对应导航项）——键盘/读屏用户失去"我在哪"的锚点。
- 证据：1.3 的 `navCurrent` 数组（落地时）与 `#cmdbar-title=工作台`、`#page-title=分析工作台`。
- 疑似归属：`app.js`（VIEWS 双键遗留）+ `login.js:39`。

### 2.4 🟡 首屏网络：同一请求重复两次 + 为不可见视图打档
- 证据（`list_network_requests`，登录落地 `/app#/workbench` 后累计）：
  1. `GET /api/auth/me [200]`
  2. `GET /api/workflows/history?limit=5&offset=0 [200]`（workbench 的"最近分析任务"面板）
  3. `GET /api/datasets?limit=3&offset=0&sort=uploaded_at&order=desc [200]`
  4. `GET /api/health [200]`
  5. `GET /api/datasets?limit=3&offset=0&sort=uploaded_at&order=desc [200]`（**与 3 完全重复**）
- 归因：`app.js:5002` init 全局 `loadAssets()` 一次 + `applyView("workbench")` 分支 `app.js:1086` 又 `loadAssets()` 一次；两者参数相同、间隔 <100ms，无任何去重/缓存。
- 态势带本体的预算（1×history?limit=12 + ≤3 详情）在 `#/command` 下实测成立：空库时只发 `GET /api/workflows/history?limit=12&offset=0` 1 个请求，**没有**对 `has_report=false` 的记录打报告请求，也没有出现 400/404。


---

## 3. 上传真实数据 `data/samples/sales.csv` ✅（一处小瑕疵）

- 路径：`#/dataset` → 「上传数据」卡（`#file` 选择文件，`upload_file` 工具直接注入，`change` 事件真实触发）→ `#upload-state` 显示「已选择：sales.csv，点「上传」开始」→ 点「上传」。
- 结果（DOM 快照）：`POST /api/datasets/upload → 200`；「已上传：sales.csv · 2,168 行 × 9 列 · 数据编号 0c656ec1…ae」；列表出现真实行（137.7 KB、2026/10/17 00:45:30、2,168 行、9 列、缺失单元格 105——已画像，非「未计算」）；**侧栏「数据资产」徽标从 hidden→1**；命令条「数据资产更新于 2026-10-17」；指标「数据集总数」0→1。
- 字段结构表全部中文：`日期（文本）`…`客户评分（小数）`——**没有出现 `region（str）`/`int64` 这类列类型英文上屏**（D-10 的 `:1974` 漏点已修，实测通过）。
- 数据预览前 5 行显示用户自己的中文列名（日期/地区/商品/类别/销量/单价/销售额/利润/客户评分）✅。
- 诚实性加分项：重复行提示「服务端不提供『去重后行数』字段，所以这里不做减法」；「缺失不等于 0，写成 0 就是在编数据」。
- 🔴→🟡 小瑕疵：上传成功后 `loadDatasets` 链路重复发了两次 `GET /api/datasets?limit=200`（网络记录 reqid 22、23 完全相同）与 `limit=20`/`limit=3` 混用刷新；疑似归属：`app.js`（`loadDatasets` 尾部又调 `loadDatasetPicker`，同时上传成功分支再触发一次列表刷新，`app.js:2642` 附近）。功能不坏，属预算浪费。
- 🔵 `#file` 输入框的无障碍名把 dropzone 两行文案整段读成「点击选择文件，或把文件拖到这里 表格第一行会被当作列名」（快照 uid=3_44），读屏会念冗余文案。疑似归属：`index.html` dropzone 结构。

## 4. 发起一次真实分析（UI 全链路实测）

- 表单：`#/analysis` → 选数据集下拉（真实列表 sales.csv 自动选中为当前数据集）→ `#question` 填真实业务问题（36 字）→ `#ask`「开始分析」。
- **`POST /api/analysis → 202` ✅**（网络记录 reqid 24），`#ask-state` 显示「已提交，任务编号 a2978a89…」。真实 deepseek 规划 + 3 个任务 + LLM 正文，整条约 75 秒跑完。
- 🔴 **提交后没有自动进入「Agent 运行」视图**。实测提交成功后 hash 停在 `#/analysis`（10 秒时取证：`hash="#/analysis"`，而阶梯已推进到 4/6——它在 hidden 的 `view-runs` 里默默变化）。代码取证：`app.js:3341-3372` 提交成功分支只 `startPolling()`，**没有 `go("runs")`**；页面文案还写着「这一页只负责发起；跑起来的六个阶段状态在『Agent 运行』里看」——等于让用户自己发现要手动切视图，期间发起页只有一行「已提交」。
  - 连带：`#badge-runs` 徽标在整个运行期间都不可见吗？——否，徽标机制正常（`state.workflowId+pollTimer` 口径），但因用户不在 runs 视图，首屏感知不到进度。
- ✅ 阶段阶梯（runs 视图 DOM 取证）：6 段 `data-step` ＝ profiling/planning/execution/charting/reporting/validation；类名走 `is-done/is-run/默认`；`#ladder-count` 运行中为「4 / 6」、空态「— / 6」；口径行「进度口径 = 已完成阶段数 / 总阶段数（…）。后端只给阶段序号，不给阶段级耗时」——**界面上没有任何阶段耗时/时间戳**（唯一时间 = 整条墙钟「已用时 10.0 秒」，契约允许）。
- ✅ 轮询状态行中文化（D-10 修复验证）：运行中实测为「状态 写报告中 · 步骤 撰写报告（4/6）」，**没有出现** `状态 SUCCESS · 步骤 done` 这类英文。
- ✅ 终态后自动跳 `#/report`（`app.js:3636 go("report")`），报告目录出现真实条目：状态「成功」、「有报告」、「计划由大模型给出」、创建时间与任务编号。
- 🟡 前端发起请求不带 `max_steps` 旋钮（`app.js:3344` body 只有 `dataset_id/question`，后端默认 6）。本轮任务要求的 `max_steps=3` **无法从 UI 达到**，实际以默认值跑完（真实结果只规划出 3 个任务，速度尚可）。入口缺失本身不算 bug（后端有默认），但记录供主控知晓：UI 没有任何限额控件。

## 5. 报告结果核对（真实 LLM 报告）

- ✅ `article.doc` 存在（`#report.doc`）；`report-id`「报告编号 d3f697b3」；来源徽章「大模型撰写正文」（三态标注，非 `llm_used` 二选一）；「生成时间 2026/10/17 00:46:27」**正常出现**——05 §5.3 登记的 `report.created_at` 永不显示 bug 已被 Wave B 修掉。
- ✅ 校验区「数字已逐条核对（共核对 51 个数字）」；正文 4 段 + 小结，数字与表格一致（华南 24871657.0 …西北 7989816.0，与 CSV 真实聚合相符——抽查 销售额_sum 列合计≈24,871,657，非编造）。
- ✅ 图表：3 张 PNG `<img>` 已挂载（`/api/workflows/{id}/charts/34f0cb4347e5.png` 等）；**HTTP 层实测 200**（页内 fetch 探针：`200 / image/png / 45,513 字节`）。注：本机浏览器无渲染 surface，`img.complete` 始终 false / `naturalWidth` 0，**像素是否真的画出来我无法断言**，需主控肉眼确认。图注真实：「本图 6 个数据点 / 横轴 地区 / 纵轴 销售额_sum / 来自任务 T1」。
- ✅ `.dtable` 表头：`data-col` 挂原始键、文本＝用户中文列名原样（`地区`、`销售额_sum`、`利润_sum`、`客户评分_mean`、`差值`），没有机器翻译、没有英文类型上屏。
- 🔴 **证据区整体缺失，`cites` 角标是死元素**：
  - 02 契约 §7.1 规定 `article.doc` 尾部要有 `footer.doc__sources`（「证据来源」+ 每条 `.src-line[data-task-id]`），并且 §7.2 硬规则 1 要求「正文每段 `sup.cite` 的集合＝证据区 `.src-line` 的集合」。
  - 实测：报告视图与历史详情抽屉里 `.src-line` 数量均为 **0**；`document.body.innerText` **不含「证据来源」**，也没有任务步骤 5 要求的「取数调用 N 次」行（`取数调用` 只存在于态势带行内，`app.js:1982`）；grep `app.js` 全文无 `src-line`/`doc__sources`（仅 CSS/契约有此概念，JS 从未产出）。
  - 点击正文 `sup.cite`（T1）：无任何跳转/滚动/hash 变化，也没有对应锚点目标（`querySelector('[data-task-id="T1"]:not(sup)')` → null，`#T1` → null）。**「角标可跳到证据」这条契约验收不通过**。
  - 「依据：T1 T2 T3」文本行存在（来自 `sections[].cites` 渲染），用户看得到编号却**无处可查**每个 T# 对应哪次工具调用、什么参数、返回了什么。
  - 疑似归属：`app.js renderReportInto`（约 `:2818` 时代码段的 Wave B 改写）漏实现 footer；`index.html` doc 壳里 `footer.doc__sources` 静态壳也不存在。

## 6. 复访首屏 · 真实态势带与请求预算（有 1 条真实记录）

- **请求预算 ✅**：`#/command` 重新加载后整页仅 5 个 GET：`auth/me`（会话门）+ `health` + `datasets?limit=3`（命令条/徽标）+ 态势带链路 **恰好 2 个**：`GET /api/workflows/history?limit=12&offset=0` 与 `GET /api/workflows/history/a2978a89…`（接口 10）。**≤ 1+3 上限**；**没有**接口 7 `/report` 请求、**没有**对 `has_report=false` 的发请求、**没有**接口 6 `GET /api/workflows/{id}` 参与态势带路径、无 400/404。
- ✅ 行内容口径逐项核对（DOM 快照）：
  - 问题列＝`item.question` 用户原话逐字（含标点）；状态药丸「成功」；行内时间「**分析完成于 2026/10/17 00:46:27**」（`finished_at`，非 created_at）。
  - 结论列：小标签「**报告首段结论**」（非"关键发现"）+「大模型撰写正文」来源标注 + 小节 heading「分析口径与分组概览」+ 正文＝**与报告 `sections[0].text` 逐字一致**（两视图文本比对相同，非 summary；summary 那句"系统成功完成了 3 个分析任务"没有被当成结论画上）。
  - 证据列：「**取数调用 3 次**」＝`citations` 去重数，与报告内「3 个工具结果」互相印证；行内角标 T1/T2/T3。
  - 尾部诚实说明：「可用的真实结论只有 1 条，所以这里只有 1 行。」没有补占位假行。
- ✅ 指标带样本口径：四个指标格各有「样本＝最近 1 条归档记录」「只算本页面正在轮询的那一条；服务端没有全站『运行中』列表接口」等说明，区块尾还有一段总口径声明（「凡是没查到的数字都写『—』——『—』是"还没拿到"，不是 0」）。
- ✅ 「查看报告」按钮行为：`go("history") + openHistoryDetail`（hash 变 `#/history`，抽屉打开并显示完整归档详情/正文/表格/质量提示），与 05 §2.5-4 规定一致；抽屉文本三行诚实（「有没有报告 是」「分析计划是谁给的 大模型」「失败原因（无）」）。
- 🟡 05 §2.5 要求行内角标可点/可查，同样落空（见 5 🔴 证据区缺失）。
- 🟡 口径细节：态势带 heading 里出现「**列表更新于 00:48**」+ 命令条「数据资产更新于 2026-10-17」两个时间并排，语义不同（列表刷新时刻 vs 数据集上传日），文案都算诚实，但同屏两个"更新于"易混。仅记录，不建议改数据源。


---

## 7. 设置页两个新入口（改密 / 会话列表）—— 🔴 后端有、JS 有、**DOM 没有**

### 7.1 后端能力实测（页内 fetch，账号 `jun`，全部真实请求）
- `GET /api/auth/sessions` → 200，只返回元信息 `{created_at, last_seen_at, expires_at, current}`，无 token（`current:true` 标记正确）。
- `POST /api/auth/password`（当前密码正确）→ 200；随后：
  - 旧密码 `POST /api/auth/login` → **401**（旧凭据确实失效）；
  - 会话列表从 1 条变回 **1 条全新会话**（`created_at` 为改密时刻、`current:true`）→ **"其他设备全部踢掉、当前设备重新签发"** 的后端语义成立；
  - 已把密码改回 `Mis-verify-2026a`（二次 `POST /password` 200），最终可用凭据不变。
- 退出登录后 `DELETE` 语义：`POST /auth/logout` 后浏览器回到 `/login`（实测跳转成功）。

### 7.2 设置页 UI 实测：两个入口**在页面上不存在**
- `#/settings` 全文（614 字，evaluate 取证）只有三块：当前账号（显示名/登录账号/会话到期）、退出登录、运行状态 + 「为什么这里改不了任何东西 / 本页只读」说明 + 配置说明 + 接口文档。**没有**「修改密码」卡片，**没有**「登录设备/会话列表」区块；`#view-settings` 内查询 `input/button` 无任何匹配（evaluate 取证 `inputs:[]`）。
- 网络侧印证：整页停留期间**从未发出** `GET /api/auth/sessions`（`loadSessions` 首行 `if (!box) return;` 静默退出，`app.js:4597-4599`）。
- JS 侧证据：`app.js:39-40` 定义了 `API.password/API.sessions`；`changePassword()`（`app.js:4512`）需要的 `#pwd-current/#pwd-new/#pwd-new2/#password-save/#password-state` 与 `loadSessions()` 需要的 `#session-list/#session-state/#session-refresh` 在 `index.html` 的 settings 段**一个都没有**（grep `index.html` 设置区仅 632-686 行、无 password/session 卡片）。`changePassword()` 里甚至写好了自白文案：「这个页面缺少输入密码的表单（设置页的『修改密码』卡片没渲染出来）」（`app.js:4527`）——**Wave B 的 app.js 接完了线路，index.html 的 DOM 没交付**，形成死代码。
- 🔴 连带诚实性问题：设置页大字宣告「**为什么这里改不了任何东西 / 本页只读**」，而系统实际存在可用的改密能力（含"踢掉其他设备"这一安全承诺）——页面把**能力缺失**当成了事实陈述，用户永远无法自助改密，只能找运维脚本。登录页的边界声明「密码重置……尚未实现」指的是忘记密码找回，本身没说错，但与本页合读会让人误以为整个凭据管理都没做。
- 疑似归属：`index.html`（settings 段缺两张卡），`app.js` 无需改逻辑只需补宿主元素；文案归属 `index.html`。

## 8. 中文化残留扫描（每视图 `innerText` 正则抓取，登录后 8 个视图全扫）

- 扫描口径：`/command, /workbench, /dataset, /analysis, /runs, /report, /history, /settings` + 登录页 + 账户浮层 + 历史详情抽屉。命中词表：`SUCCESS|FAILED|PENDING|done|created_at|has_report|llm_used|region(str)|int64|HTTP|error|sum|mean|group_count…`。
- 结果（命中即全量）：
  - `/report`、`/history`（详情抽屉）、`/command`（态势带结论）：`group_count`、`row_count`（出现在 LLM 报告正文句子「group_count 与 row_count 均为 6」里，逐字取证于三视图 innerText）——**来源是模型生成的正文本身**，不是前端映射漏网。疑似归属：`app/agents/report_llm.py` 提示词（未约束模型引用原始字段名时要给中文注解）。按 D-9「错误码/原始值可查证」的宽松口径可豁免，但它是当前唯一上屏的成组英文内部字段名，主控裁定。🟡
  - `/report`、`/history`：`_sum`、`_mean`（表头 `销售额_sum`、`客户评分_mean`）——后端聚合派生列名原样上屏（`02 §5.2` 规定查不到 `COLUMN_LABELS` 就原样显示，行为合规）。但对照本轮验收话术"snake_case 列名要格式化"，`地区`/`销售额` 中文 + `_sum` 英文后缀的混排**未做后缀中文化**（如「销售额（合计）」）。归属：`app.js COLUMN_LABELS`（可只处理 `_sum/_mean/_count` 后缀映射，不违反"不猜中文"）。🔵
  - 其余视图（analysis/runs/settings/dataset/workbench/登录页/浮层）：**零命中**。特别确认：运行中状态行（「状态 写报告中 · 步骤 撰写报告（4/6）」）、步骤五行（等待执行/已完成/正在执行）、历史目录（成功/有报告/计划由大模型给出）、错误码（401 分支只显示中文，错误码收在可查证节点）都已中文化——D-10 三处必修点实测未复现。
  - 允许保留项（非缺陷）：侧栏英文副行「AI Market Intelligence Studio」（D-11 明确允许）；「CSV/XLSX/PNG/`/api/datasets`/`/docs`」等技术名。
- ⚠️ 扫描局限声明：样例 `sales.csv` 的列名全是中文，`total_amount → 销售总额` 这类 **英文列名 → 展示名** 的 `COLUMN_LABELS` 真路径**没有被本次数据覆盖**，不能据此说该能力已验证。

## 9. 三态与错误（退出 → 错误密码）

- 退出登录：`#logout` → `POST /auth/logout` → 302/replace 回 `/login`，账户浮层消失。✅
- 错误密码**仅一次**（遵守"不触发限流"约束）：alert 显示 `role="alert" aria-live="assertive"`「**用户名或密码不正确**」+ detail「用户名与密码不匹配。为避免用户名枚举，服务端不会说明具体是哪一项不对；连续多次失败会触发短时限制。」；`#alert-detail` 为 `aria-live="off"`（符合 §8.2 契约）；失败后**密码框被清空、焦点落回 `#password`、提交按钮 disabled 且 reason=「请填写密码。」**（防重放+防连点，行为正确）。✅
- `POST /api/auth/login` 401 响应体 `{code:"UNAUTHORIZED", message:"用户名或密码不正确", detail:"/api/auth/login"}`——detail 依旧是路径回显（同 1.2 尾注）。
- **429 分支未实测**（刻意不制造连续失败，避免锁死账号）。代码核对：`beginLockout()` + `Retry-After` 解析 + 「请在 N 秒后重试」倒计时 + 按钮禁用链路在位（`login.js:410-413, 975-979`）。留给主控决定是否另起环境验证。
- 未实测分支：503（需要空 `AUTH_DB_PATH` 重启服务）、库损坏分支。

## 10. 控制台与网络总收口

- `list_console_messages`（types=error/warn，含 preserved）：全程仅 **2 条 error，且都是本轮故意制造的 401 资源加载提示**（`/api/auth/login`），**零 JS 异常/警告**。
- 异常网络请求归因：
  - `POST /api/auth/bootstrap → 403`：本轮**故意探针**（验证非空库闸门），预期内。
  - `POST /api/auth/login → 401` ×2：一次故意错密码、一次改密后验证旧密码失效，均预期内。
  - 其余全部 200/202/303/302 正常。无 5xx。无静默失败的请求。
  - 🟡 冗余请求清单（非错误但属预算债）：登录后首屏 `GET /api/datasets?limit=3…` 连发 2 次（`app.js:5002` 与 `applyView("workbench")` 各一次）；上传成功链路 `limit=200` 连发 2 次。见 §2.4/§3。

## 11. 假数据 / 假能力排查（诚信专节）—— 结论：**未发现任何编造的"伪真实"**

逐项排查（每条都有上文的运行时证据）：
1. **占位样例有没有被当真数据画出来？** 没有。态势带空态时只画空态文案；有数据后该行问题/结论/证据/时间全部来自 `history` + `history/{id}` 真实返回（数字与 CSV 聚合抽查一致）。首屏四指标只有 数据集数/运行中/已出报告/失败数 四个可导出字段，**没出现**原型里的「华东 Q3 销售额 / 品类份额 / 华南促销 ROI」占位行，**没有**自绘趋势图（D-1 落地），**没有**▲12.4% 之类升降 delta（02 §3.3-4 红线）。
2. **"报告首段结论"是不是真的 `sections[0].text`？** 是。态势带结论文本与报告正文第一段、历史详情抽屉第一段**逐字相同**（三处交叉比对），且不是 `summary`（summary 里"系统成功完成了 3 个分析任务"那句元话语未被冒充结论）。
3. **证据数口径对不对？** 「取数调用 3 次」= `citations` 去重数 = 报告内「3 个工具结果」= 正文 T1/T2/T3 三个成功任务，三处互证一致；`citations` 缺失/空数组的降级（`—` vs `0`）代码在位（本轮无法制造该形态，未实测）。
4. **有没有出现不存在的后端能力入口？** 反向发现一处：设置页宣称「改不了任何东西」，**掩盖了真实存在的能力**（改密/会话列表）——这不是假能力，是**真能力假缺失**（见 §7，最接近诚信红线的一条）。正向排查干净：`健康检查`、`/docs`、`接口文档`、`查看报告` 全部指向真实可达端点/视图；`图表由服务端按真实数据生成` 属实（PNG 200、字节数真实）。
5. **有没有"看起来像数据"的死元素？** 有，但属"缺实现"而非"造假"：正文 `sup.cite` 角标点击无跳转、无证据区（§5 🔴）。它显示的是真实 T# 编号，只是无处可查——对用户的承诺（"点「查看报告」可以逐条核对数字"）目前**兑现了一半**（能看到原文与表格，点不到"这条依据是哪次调用"）。
6. **报告目录「数据集」列为空的处理**：页面明写「目前这一列还显示不出这份报告用的是哪份数据……等后续版本把数据集信息记录进来」——**如实降级，无凑数**。
7. 本轮唯一被我注入页面的"非自然状态"是 §1.2 的 CSS 强制 bootstrap 显形，**未**用它伪造成功文案，且已声明该段不构成端到端证据。

---

## 附：缺陷计数与清单速览

| 级别 | 数量 | 条目 |
| --- | --- | --- |
| 🔴 阻断 | **4** | R1 登录后落到 `#/workbench`（`login.js:39`）；R2 提交分析后不自动切「Agent 运行」（`app.js:3371` 缺 `go("runs")`）；R3 报告证据区/角标跳转整体缺失（`app.js renderReportInto` + `index.html` doc 壳）；R4 设置页缺「修改密码」「会话列表」两张卡（后端能力被"本页只读"文案掩盖，`index.html` settings 段） |
| 🟡 偏差 | **6** | Y1 `workbench`/`command` 双视图并存违背 02 §0.1（05 §7 与 02 契约互斥，需主控裁定）；Y2 首屏/上传链路重复 GET（datasets 同参×2、limit=200×2）；Y3 空态 CTA 是 button 非契约规定的 `a.btn--primary[href]`；Y4 LLM 正文夹带 `group_count/row_count` 原始字段名（后端提示词）；Y5 侧栏「健康检查」直链 JSON 端点（非契约元素，点了丢 SPA 状态）；Y6 `data/auth.db` 0 字节空文件与真实库 `data/db/auth.db` 近邻共存（本次误判诱因，含 test-admin `iterations=1000` 环境残留问题） |
| 🔵 建议 | 3 | B1 `_sum/_mean` 后缀可显示为「（合计）/（均值）」；B2 `#file` 无障碍名冗长；B3 401/403 的 `error.detail` 回显请求路径意义不大 |

> 环境说明（诚实声明）：本机浏览器无渲染 surface（`innerWidth=0`），`<img>` 不完成解码、`.cmdbar__link` 命中 480px 让位规则等**像素/断点级判断全部未做视觉确认**；图表 PNG 以 HTTP 200+字节数+content-type 佐证存在，不佐证显示效果。未修改任何 `app/web/` 文件；唯一的写入是账号库新增 `jun`（经项目自带运维脚本）与两次 `jun` 的自助改密回环（已改回原密码）。


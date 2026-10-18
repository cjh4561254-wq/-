# 02 · 结构契约（方案 01 重构 · Wave A 产物）

> 状态：**定稿**。本文是 Wave B 四个实现 agent（`index.html` / `app.css` / `app.js` / `login.*`）的唯一共同语言。
> 任何一条与原型 `design-options/option-01/index.html` 冲突之处，以本文为准（本文已把原型折算成"接真实后端"的形态）；
> 本文标注 ⚠️ 的条目是需要主控裁定或 Wave B 完成后回填测试的条目。
>
> 基准：原型 DOM/类名 + `docs/design-options/option-01.md` §5/§6/§7/§9/§10 与 :61-62 行的 DOM 级区别自检。
> 落地原则：**优先沿用原型类名语义**（`.cmdbar` `.posture` `.ladder` `.doc` 等），现有 `app.css` 组件类（`.card` `.panel` `.dtable` `.src` `.pill` `.state` 等）能复用就复用。
>
> ---
> ## ⚠️ Wave D 回填（2026-10-17）：本文哪些条目已被裁定取代
>
> 本文是 Wave A 写的**契约**，Wave B/C 落地后有 6 处被 `06-leader-decisions.md` 的裁定或实测事实取代。
> 就地标注如下，**原条文不删**（它是设计过程的证据），但引用本文前先读这一屏：
>
> | 本文位置 | 原契约写的 | 现行事实 / 裁定 | 依据 |
> | --- | --- | --- | --- |
> | §0.1 定案表 | 「战略态势」映射到现有 `workbench` 键 | 新增独立键 **`command`** 作为首屏（`DEFAULT_VIEW = "command"`），`workbench` 保留但降级为二级视图（上传/概览/最近任务）；VIEWS 实际 **8 键** | Wave B 落地，`app.js` 的 `VIEWS`/`DEFAULT_VIEW`；`tests/test_frontend_command_center.py::test_nav_and_sections_stay_in_sync_with_js_view_registry` |
> | §1.3 令牌名 | `--ds-shell-bg/rule/ink/ink-active/label/active-bg/bar` | **以 `tokens.css` 实际名为准**：`--ds-shell-side-bg`、`-side-ink`、`-side-ink-muted`、`-side-line`、`-side-active-bg`、`-side-active-marker(-w)`、`-side-hover-bg`、`-side-badge-bg/-badge-ink`、`-bar-bg/-bar-line/-bar-ink/-bar-ink-muted`、`-paper/-paper-alt/-paper-sunk/-paper-deep`。另：`-side-rule`（= 兼容层 `--rule-on-navy`，12%→**16%** 透明白）是后来为"深底发丝线在 navy-800 上只有 1.24:1"新增的；旧名 `-side-line` 因 `app.css` 已引用而**保留**，两者用途不同（见 §1.3 下的对照） | 裂缝 2 / 裂缝 10，记录在 `10-integration-log.md` 批次 1 与批次 4 |
> | §2.2 / §2.3 命令条 | `#cmdbar-asof` 显示「数据截至 `<max uploaded_at>`」 | **不显示"数据截至"**，文案改成「**数据资产更新于** `<max uploaded_at>`」——归档链路没有 `dataset_id`，没有任何字段能证明结论对应哪份数据 | **D-7** |
> | §3.1 / §3.3 态势带 | 结论列 = `report.summary`；证据数 = `sections[].cites` 去重并集；逐条取 `GET /api/workflows/{id}/report` | 结论列 = **`sections[0].text`**；证据数 = **去重 `citations.length`**，文案「取数调用 N 次」；正文取数走**只读** `GET /api/workflows/history/{id}`（恒 200），**禁止**接口 7 与接口 6 | **D-5 / D-4 / D-6**；口径唯一规定在 `docs/COMMAND_CENTER_MIGRATION.md` §3 |
> | §3.3 第 4 条 | （原型每行结论后的 ▲/▼ delta） | **去掉升降 delta**：后端无任何同比/环比字段 | **D-7** |
> | §5.1 / §8.1 / §9 断点 | `@media (max-width:768px)` | **沿用存量 480 / 760 / 1080**，768 一律落地成 **760**；不新增断点、不改测试 | **D-2** |
> | §9 表首行 | 「≤768 侧栏转顶部横向标签行」 | 侧栏转**抽屉**（沿用现有 `.is-open` + `#nav-toggle` 机制），没有重排成横推行；关闭态另加 `visibility: hidden` 让 Tab 打不到 | Wave B 落地 + 裂缝 19 |
>
> **不可回退清单 §11 全部继续有效**（Wave B 已按它实现，一条都没被推翻）。
> 本文里所有 `app.js:NNN` / `app.css:NNN` / `index.html:NNN` 形式的行号是 Wave A 写作时点的快照，
> `scripts/check_docs.py` 不核对前端文件，所以它们**没有守卫**；定位请用符号名（函数名、令牌名、CSS 选择器）。

---

## 0. 词汇表与视图模型（先定这个，否则四个 agent 各说各话）

### 0.1 视图 key：保留现有 VIEWS 词表，新增 `runs`，`settings` 下栏

现有 `app.js:117-148` 的 VIEWS 六键是 `workbench / dataset / analysis / report / history / settings`，
hash 路由 `hashParts()`（`app.js:766-786`）用 `VIEWS` 成员判定合法视图，深链形态 `#/dataset/{id}`、`#/report/{id}`、`#/history/{id}`（`app.js:803-828`）。

**定案（方案 01 的 3 组 6 项映射到这套词表）：**

| 侧栏项（中文标签） | 视图 key | 分组 | 来源 |
| --- | --- | --- | --- |
| 战略态势 | `workbench` | 指挥 | 改造现有 workbench：首屏替换为态势带+hairline 指标+业务表；仍是 `DEFAULT_VIEW` |
| 数据资产 | `dataset` | 指挥 | 现有 dataset 视图 |
| 发起分析 | `analysis` | 分析 | 现有 analysis 视图（**只留发起表单**） |
| Agent 运行 | `runs` | 分析 | **新增 key**：把现 analysis 视图里的"执行过程"卡片（`#steps` `#elapsed` `#workflow-state` `#task-list` `#workflow-error` + 阶段阶梯）拆成独立视图 |
| 分析报告 | `report` | 成果 | 现 report 视图（含报告目录 + `article.doc` 阅读态） |
| 历史记录 | `history` | 成果 | 现 history 视图 |
| （不在侧栏）设置 | `settings` | — | 保留 VIEWS 键与视图 section，入口移到命令条账户菜单（§2.4） |

- **不改键名**（`workbench` 显示名改"战略态势"即可）：改键名会同时破坏深链、`applyDeepLink`、测试外的一切引用，收益为零。
- `runs` 加入后 VIEWS 七键；`VIEW_GROUP`（`app.js:150-159`）同步改为 `{workbench:"指挥", dataset:"指挥", analysis:"分析", runs:"分析", report:"成果", history:"成果", settings:"账户"}`。
  注释 `app.js:150` 说"与 .nav__group 三段标题逐字一致"——这条继续成立，新标题只有三个：**指挥 / 分析 / 成果**。
- VIEWS 每项的 `crumb` 字段继续存在，值即侧栏中文标签；`#crumb` 元素**删除**（未被测试钉死），它的职责由命令条 `#cmdbar-title` 承担（§2）。

### 0.2 统一状态词（四个文件共用，不许另造）

- 视图激活：导航项 `[aria-current="page"]`；视图容器 `section.view[hidden]`（沿用 `app.js:838-842` 的 `.view[data-view]` + `hidden` 机制，**类名 `view` 与属性 `data-view` 不许改**——`applyView` 的 `querySelectorAll(".view[data-view]")` 依赖它）。
- 阶段状态：`done / run / pending / skip` 四词（§6 详述 `pending`＝原 waiting）。
- 会话门：`body[data-auth="pending"|"ready"]`（沿用 `index.html:22`）。

---

## 1. 外框（`240px 1fr` + 会话门 + 侧栏 nav）

### 1.1 根结构（`index.html` 产出 → `app.css` 消费 → `app.js` 消费）

```html
<body data-auth="pending">
  <a class="skip-link" href="#content">跳到主内容</a>   <!-- 保留，见 §11 钉子 -->
  <div class="authgate" id="auth-gate" …>…</div>        <!-- 保留现机制，原样 -->
  <svg class="icon-sprite" …>…</svg>                    <!-- 保留：绝不能加 xmlns；文件内禁止出现任何绝对网址 -->

  <div class="frame" id="frame">                        <!-- 替代现 .shell；顶栏 header 移进 .col -->
    <aside class="sidebar" id="sidebar" aria-label="主导航">…</aside>
    <div class="col">
      <header class="cmdbar">…</header>
      <main class="content" id="content" tabindex="-1">
        <div class="pagehead"> <h1 id="page-title">… <p id="page-sub">…   <!-- 两个 id 被 app.js 写入，保留 -->
        <section class="view" id="view-workbench" data-view="workbench">…
        <!-- 其余 6 个 view section 同现状，id 规则 view-<key> 不变 -->
      </main>
    </div>
  </div>
  <script src="/static/app.js"></script>
</body>
```

CSS 契约（`app.css`）：

```css
.frame { display:grid; grid-template-columns:240px 1fr; min-height:100vh; }
body:not([data-auth="ready"]) .frame { display:none; }   /* 会话门未过时整个外壳不可见 */
.cmdbar { position:sticky; top:0; z-index:var(--z-topbar); }
.content { max-width:1220px; padding:var(--sp-6) var(--sp-8) var(--sp-10); }  /* 原型 .stage 口径 */
```

**`body.in-login` 的实现方式（定案）**：本项目的登录是**独立页面**（`/login`，`tests/test_auth_api.py:318-333` 钉死 `/app` 未登录 303 → `/login`），所以方案文档里的 `body.in-login` **不在 index.html 落地**；"未认证不暴露导航"由上面 `body:not([data-auth="ready"]) .frame{display:none}` 这一条等价承担。Wave B 不需要引入 `in-login` 类；login.html 也不加。⚠️ 若主控坚持单页形态，改的是本节 + app.js 的 checkSession，不动 §11 任何钉子。

### 1.2 侧栏 3 组 6 项（`index.html` 产出，`app.js` 的 `applyView` 写 aria-current）

沿用原型 `.navgrp` 结构，但**导航项用 `<a href="#/…">`（hash 路由、可分享、深链）而不是原型 `<button data-view>`**；类名保留现有 app.css 的 `.nav__item`（`applyView` 的选择器 `.nav__item[data-view]` 不改，`app.js:844`），新增原型语义类 `navgrp`：

```html
<nav class="nav" aria-label="主导航">
  <div class="nav__group-wrap">                      <!-- 原型 .navgrp -->
    <p class="nav__group" id="nav-group-command">指挥</p>   <!-- 沿用 .nav__group（app.css:456），文案＝VIEW_GROUP -->
    <a class="nav__item" href="#/workbench" data-view="workbench" aria-current="page">
      <svg class="icon" aria-hidden="true"><use href="#i-spark"></use></svg>
      <span>战略态势</span>
    </a>
    <a class="nav__item" href="#/dataset" data-view="dataset">
      <svg class="icon" aria-hidden="true"><use href="#i-dataset"></use></svg>
      <span>数据资产</span><span class="nav__badge" id="badge-datasets" hidden>—</span>
    </a>
  </div>
  <!-- 「分析」组：发起分析(#i-analysis) / Agent 运行(#i-history 复用或新增 symbol) + #badge-runs -->
  <!-- 「成果」组：分析报告(#i-report) / 历史记录(#i-refresh 换回原 symbol 自定) -->
</nav>
```

- 当前项：**只认 `aria-current="page"`**（app.css:482 已有该选择器，改样式即可，`is-active` 类不引入，避免 JS 双写）。
- 计数徽标（原型 `.nav-item .badge` → 落地 `.nav__badge`）：
  - `#badge-datasets`：数据集总数。来源 `GET /api/datasets` 响应 `total`（现有 `loadDatasets/loadAssets` 已拿过）；`total` 为 0 或请求失败 → `hidden`。
  - `#badge-runs`：**本次会话正在轮询的工作流条数（0 或 1）**。后端没有"全部运行中"列表接口（`/api/workflows/history` 只归档终态记录，口径不能拿来当"运行中数"），来源定为 `state.workflowId !== null && state.pollTimer !== null`。数字为 0 时 `hidden`。⚠️ 与方案文档"运行中数来自真实接口统计口径"的偏差在此钉明，由主控裁定是否接受；不许编造聚合接口。
  - 徽标一律 `font-variant-numeric:tabular-nums`、`--ds-shell-ink-subtle` 色（§1.3）。
- 栏底（原型 `.side-foot` → `.sidebar__foot` 沿用现类名，app.css 已有）：部署形态文案沿用现 index.html:190-193 的真实表述（"原始文件只存在服务端内存与上传目录…"），**不放**原型里的假版本号 v2.4.1。

### 1.3 侧栏令牌（`tokens.css` 唯一 owner 的 agent 负责新增，其余人只 `var()` 消费）

> **【裁定/事实回填 · Wave D】下表是 Wave A 提案名，不是落地名。** `tokens.css` 实际用 `--ds-shell-side-*` 一族
> （`-side-bg / -side-ink / -side-ink-muted / -side-line / -side-active-bg / -side-active-marker[-w] / -side-hover-bg /
> -side-badge-bg / -side-badge-ink`）+ 命令条 `-bar-*` + 四级纸面 `-paper-*`；**以 tokens.css 为准**，
> 契约不返工、`app.css` 的既有引用不改名。深底发丝线另有一条 `-side-rule`（= `--rule-on-navy`，16% 透明白），
> 因为 `--navy-800` 压在 `--navy-900` 上实测只有 1.24:1。完整对照见本文开头的「Wave D 回填」表第二行。
> 表里那条"主控定是否直接复用 `--brand-900`"也已定：新命名空间 `--navy-900 = #0B2239` 与兼容层
> `--brand-900 = #14263f` **并存不重指**（`01-token-contract.md` §0 的核对结论）。

在 `tokens.css` `:root` 追加一组 `--ds-shell-*` 语义令牌（方案文档附节已授权"新增少量、兼容层指向同值"）：

| 令牌 | 值（对齐方案 01 §4） | 用途 |
| --- | --- | --- |
| `--ds-shell-bg` | `#0B2239`（≈现 `--brand-900` #14263f 的深一档；主控定是否直接复用 --brand-900） | 侧栏底 |
| `--ds-shell-rule` | `rgba(255,255,255,.12)` | 侧栏分隔线 |
| `--ds-shell-ink` | `#C6D2DE` | 未选中导航字 |
| `--ds-shell-ink-active` | `#FFFFFF` | 选中/hover 字 |
| `--ds-shell-label` | `#6E9CC2`（现 `--brand-400` 近值） | 分组小标题、徽标 |
| `--ds-shell-active-bg` | 现 `--brand-800`（#1c3557） | 当前项底色 |
| `--ds-shell-bar` | `#6E9CC2` | 当前项左侧 3px 竖标（`.nav__item[aria-current="page"]::before`） |

约束：不引入 backdrop-filter / 外链字体 / @font-face；圆角 ≤2px（侧栏内新组件用 `--r-sm` 或 2px 常量）。

---

## 2. 命令条 `.cmdbar`（非导航）

### 2.1 定性（对应 option-01.md :51 与 :61-62 自检）

- **不含导航、不含全局搜索**：DOM 内不得出现 `<nav>`、不得出现任何 `role="search"` 或全局搜索 `<input type="search">`（数据集视图里的页内搜索 `#dataset-search` 不在此列，它在 main 内）。
- 与 06 的关键区别：06 顶栏是一级导航；这里顶栏只是上下文+动作条，导航只活在侧栏。

### 2.2 DOM（`index.html` 产出 → `app.css` 消费 → `app.js` 写入其中三处）

```html
<header class="cmdbar">
  <button class="cmdbar__menu" id="nav-toggle" type="button"
          aria-label="展开或收起导航" aria-expanded="false" aria-controls="sidebar">…</button> <!-- ≤768px 用，沿用现 id -->
  <span class="cmdbar__title" id="cmdbar-title">战略态势</span>          <!-- app.js applyView 写入 = VIEWS[name].crumb -->
  <span class="cmdbar__asof" id="cmdbar-asof">数据截至 <b>—</b></span>   <!-- app.js 写入，见 §2.3 -->
  <span class="cmdbar__note" id="cmdbar-note">数据来自服务端真实接口 · 内网单进程部署</span>
  <span class="cmdbar__spacer" aria-hidden="true"></span>
  <span class="cmdbar__health" id="cmdbar-health" role="status">
    <span class="dot dot--ok" aria-hidden="true"></span><span id="health-text">正在检查服务…</span>
  </span>
  <span id="llm-badge" class="badge badge--grey" role="status">正在检查大模型配置…</span> <!-- ⚠️ 被测试钉死，原样保留（§11#4/#13） -->
  <a class="cmdbar__link topbar__link" href="/docs" title="打开自动生成的接口文档">
    <svg class="icon" aria-hidden="true"><use href="#i-external"></use></svg>接口文档</a>  <!-- 类名 topbar__link 被测试依赖，保留 -->
  <a class="btn btn--primary cmdbar__launch" href="#/analysis">发起新分析</a>   <!-- 每屏唯一主按钮；用 href 不写 JS -->
  <div class="cmdbar__user">
    <button class="userbtn" id="user-btn" type="button" hidden aria-haspopup="true"
            aria-expanded="false" aria-controls="user-popover">…</button>          <!-- 沿用现 id/类 -->
    <div class="userpop" id="user-popover" aria-labelledby="user-btn" hidden>
      <p class="userpop__row"><span>登录账号</span><strong id="user-account">—</strong></p>
      <p class="userpop__row"><span>显示名</span><strong id="user-name">—</strong></p>
      <p class="userpop__row"><span>会话到期</span><strong id="user-expiry">—</strong></p>
      <a class="userpop__row userpop__settings" href="#/settings">设置（只读）</a>   <!-- ← settings 的唯一入口，不可丢 -->
      <button class="btn btn--ghost userpop__logout" id="logout" type="button">…退出登录</button>
      <p class="state" id="logout-state" role="status"></p>
    </div>
  </div>
</header>
```

- 账户菜单**必须能到达设置**：`href="#/settings"` 走现有 hash 路由，`applyView("settings")` 的 `loadSettings()` 链路完全不动。侧栏没有设置项，但设置视图、VIEWS 键、`#view-settings` section 三者都在。
- 设计声明文案（`.cmdbar__note`）：原型那句"视觉方案示意 · 数据为占位样例 · 未接入后端"**不能上生产页**——那是对真实接入页面的谎报。定案文案如上；若主控要保留"方案 01 示意"属性，只在 demo 开关下出现，不许写死。

### 2.3 真实数据来源

| 元素 | 接口 | 字段 |
| --- | --- | --- |
| 服务健康点 `#cmdbar-health` | `GET /api/health` | `ok===true` → `dot--ok`+"服务正常"；`ok!==true` 或请求失败 → `dot--err`+"服务异常"。与 `#llm-badge` 共用同一次 fetch 结果（`llm_configured` 字段现由既有 renderHealth 消费，不新开请求） |
| 账户三行 | `GET /api/auth/me` | 现 `renderUser()` 原样（`app.js` 未变更逻辑） |
| 数据截至 `#cmdbar-asof b` | `GET /api/datasets` | `items[].uploaded_at` 的**最大值**，格式 `YYYY-MM-DD`，口径是"最近一份数据的上传日"。列表为空或失败 → `—`（`—`＝没查到，不是 0，沿用 index.html:214-215 注释的口径纪律）。**后端没有"数据覆盖截至日"字段**，不许把 generated_at/finished_at 冒充数据截至。 |

`app.css`：`.cmdbar{position:sticky;top:0;z-index:var(--z-topbar);background:var(--ds-color-surface);border-bottom:1px solid var(--n-200);height:60px;display:flex;align-items:center;gap:var(--sp-5);padding:0 var(--sp-8);}` 焦点一律 `outline`（`--brand-500` 现令牌），无 box-shadow 发光。

---

## 3. 战略态势带 `.posture`（`workbench` 视图首屏）

### 3.1 DOM（`index.html` 出壳，`app.js` 填行，`app.css` 出样式）

```html
<section class="posture" id="posture" aria-label="战略态势带">
  <div class="posture__head">
    <h2>战略态势带 <span class="posture__asof" id="posture-asof-note">· 数据截至 —</span></h2>
    <p class="posture__hint">每条结论来自已归档且有报告的运行，点「查看报告」回到原文核对。</p>
  </div>
  <div id="posture-body">        <!-- app.js 注入 .posture__row × ≤3；加载时是 .skel，见 §3.4 -->
    <article class="posture__row">
      <div class="posture__q"><span class="eyebrow">业务问题</span><p>{item.question}</p></div>
      <div class="posture__a"><span class="eyebrow">结论</span><p class="verdict">{report.summary}</p></div>
      <div class="posture__e"><span class="eyebrow">证据</span>
        <p>{N} 项 <span class="src src--api">依据</span> <span class="posture__cites">{sup.cite × 去重 cites}</span></p>
      </div>
      <div class="posture__go"><a class="btn btn--ghost btn--sm" href="#/report/{item.workflow_id}">查看报告</a></div>
    </article>
  </div>
</section>
```

### 3.2 grid 定义（`app.css`）

```css
.posture { border:1px solid var(--n-200); background:var(--ds-color-surface); }
.posture__row {
  display:grid;
  grid-template-columns:minmax(200px,1fr) minmax(240px,1.3fr) minmax(180px,.9fr) auto;
  gap:var(--sp-6); align-items:center;
  padding:var(--sp-4) var(--sp-5); border-bottom:1px solid var(--n-100);
  transition:background var(--dur-1) var(--ease);
}
.posture__row:hover { background:var(--ds-color-surface-hover); }
.posture__row:last-child { border-bottom:0; }
```

### 3.3 数据装配（`app.js` 唯一负责，其余文件不许自己拼）

> **【裁定回填 · Wave D】本节三处已被取代，现行口径只有一处规定它：`docs/COMMAND_CENTER_MIGRATION.md` §3。**
> ① 结论列不是 `report.summary`，是 **`report.sections[0].text`**（**D-5**：`summary` 是"成功 N 个任务"的元说明，
> 把它当结论是把元数据当洞察）。② 证据数不是 `sections[].cites` 去重并集，是 **去重后的 `citations.length`**，
> 文案写「取数调用 N 次」；`sections[].cites` 只做正文角标（**D-4**，本节第 3 条当年选的口径被驳回）。
> ③ 正文**不**走 `GET /api/workflows/{id}/report`，走只读归档接口 `GET /api/workflows/history/{id}`，
> 并且**禁止**调用 `GET /api/workflows/{id}`（它内部的 `_expire_if_stale` 会写状态并落库 —— 探测即污染，**D-6**）。
> ④ 升降 delta 整条不渲染（**D-7**）。下面的原文保留，作为"当时怎么论证"的记录。

1. `GET /api/workflows/history?limit=20` → `items[]`；过滤 `has_report===true` 且 `status ∈ TERMINAL`（沿用现常量，别新枚举），按返回顺序（后端已按 `finished_at DESC NULLS LAST, created_at DESC` 排好，`routes_workflows.py:248` 注释）取前 **3** 条。
2. 逐条 `GET /api/workflows/{id}/report`（该接口返回体即现有 `loadReport` 消费的 `{report, validation, results, …}`）：
   - 问题列＝`item.question`（history 字段，`schemas_workflow.py:HistoryItem`）；
   - 结论列＝`report.summary` 原文（`FinalReport.summary`，`schemas_workflow.py` "3–5 句总述"）；
   - 证据列与角标＝**`sections[].cites` 去重并集**（`ReportSection.cites`，"依据的 task_id 列表"）——`sup.cite` 逐个渲染并集成员，`N` 取并集大小；
   - 查看报告＝深链 `#/report/{workflow_id}`（走现有 `applyDeepLink`，不新写加载路径）。
3. **证据数口径的歧义（钉死并上报）**：备选口径是 `report.citations.length`（`FinalReport.citations`，"命中的 task_id 列表"）。两口径**可以不相等**：后端规则模板下二者一一对应（`validation.py:190-191` 每个成功任务 append 一次），但 LLM 段落允许一段引多个 task（`report_llm.py:114`），此时 cites 并集 > citations。契约取 **cites 去重并集**，理由：它和正文角标、证据区行严格一一对应，"证据数"才核得住。⚠️ 主控可改，改口径只动 `app.js` 一个函数。
4. **升降 delta 不渲染**：原型每行结论后的 `▲12.4%` 是占位样例；后端无任何同比/环比字段。真实落地结论列＝summary 纯文本，`.delta` 类只用于业务表/指标区中**确有**两个真实值可算差的情形（首版没有 → `.delta` 只出现在报告正文文本本身自带方向词时不做处理）。此偏差对应 option-01.md §13 缺点 1/2 的落地成本，已知悉。
5. 数据截至同步：`#posture-asof-note` 与 `#cmdbar-asof` 用同一个计算结果（§2.3），由 app.js 一次写入两处。

### 3.4 三态

- **不足 3 条**：按实际条数渲染 1–2 行，不补空行、不摆占位。
- **列表为空 / 全部无报告**：`#posture-body` 换成现有 `.empty` 组件（`app.css:978`）：`h4 "还没有可展示的结论"` + p 说明 + `a.btn--primary.btn--sm href="#/analysis"` 发起第一次分析。
- **取数失败**（history 或任一 report 非 `ok`）：整带换 `.posture__errstate`（新类，形态=原型 `.errstate`：`--err-bg` 底 + 一句真实原因 + "重试"按钮走同一次装配）。注意失败文案来自 `body.error.message`，不许按 HTTP 状态码编（`app.js` 规矩 1）。
- **加载中**：`.skel` 骨架三行（原型类名 `.skel .sl`，1.3s shimmer；`prefers-reduced-motion` 下停动画）。

### 3.5 态势带以下（同视图）

`.metrics` hairline 指标（§4）→ `.two` 双列（`grid-template-columns:1.6fr 1fr`；左：现有 workbench 的真实面板复用 `.panel`，右：现 `.kpis` 概览或数据趋势）。现 workbench 的上传卡移入 `dataset` 视图首位置（上传是"数据资产"的动作，不是态势带的）。上传相关测试 ids（`#file #upload #upload-state #dropzone`）随迁移原样保留。

---

## 4. hairline 指标网格 `.metrics`

### 4.1 契约（`index.html` 出结构 → `app.js` 填数 → `app.css` 出样式）

```css
.metrics { display:grid; grid-template-columns:repeat(4,1fr);
           border:1px solid var(--n-200); border-right:0; background:var(--ds-color-surface); }
.metrics .metric { padding:var(--sp-4) var(--sp-5); border-right:1px solid var(--n-200); }
/* 无 box-shadow、无圆角（或 ≤2px）、格子之间只靠 1px 分隔线 */
.metric__value { font-size:var(--fs-4xl); font-weight:300; font-variant-numeric:tabular-nums;
                 letter-spacing:-.02em; color:var(--ds-color-ink-strong); }
```

```html
<div class="metrics" id="metrics-grid">
  <div class="metric"><p class="metric__value" id="metric-1">—</p>
    <p class="metric__label">数据集</p><p class="metric__note"><span class="src src--api">/api/datasets</span></p></div>
  … 共 4 格 …
</div>
```

### 4.2 与现有 `.statstrip` / `.kpi` 的关系（定案）

- **`.statstrip`（app.css:1362）与四个 `#asset-*` ids 保留**——`#asset-total/#asset-task-total/#asset-last-success/#asset-current` 不在测试 REQUIRED_IDS，但 app.js 的 `loadAssets()` 写入它们；这套计数行**搬家进 `.metrics` 的 4 格**：`#metrics-grid` 内直接放现有 `.stat` 结构（`.stat__label/.stat__value/.stat__note` 类名不变、ids 不变），外层容器由 `.statstrip` 换为 `.metrics`。
- 结论：**复用 `.stat` 内部结构，替换外框容器**；`.statstrip` 规则保留但不再被 index.html 引用（app.css 可后续清理，Wave B 不删，防止任何选择器回归）。
- `.kpi` / `.kpis`（app.css:862-891，dataset 概览小卡）**不动**——它是字段级指标不是本页 hero，不承担 hairline 网格职责，两套并存。
- 拿不到的数字写 `—`，note 写"正在读取…/读取失败"，沿用现口径（`index.html:214` 注释的"没查过不是 0"）。

---

## 5. 报表级表格 `.dtable`

### 5.1 新增的 CSS 契约（`app.css`）

```css
.dtable thead th { position:sticky; top:60px; z-index:2;
                   background:var(--n-100);            /* paper-sunk 等价 */
                   font-size:var(--fs-xs); letter-spacing:.06em; text-transform:none; }
.dtable td.is-num, .dtable th.is-num { text-align:right; font-variant-numeric:tabular-nums; } /* 已有(1171-1172)，保留 */
.dtable tbody tr:hover { background:var(--n-25); }                                            /* 已有(1173)，保留 */
@media (max-width:768px) {
  .dtable thead { position:absolute; left:-9999px; }
  .dtable, .dtable tbody, .dtable tr, .dtable td { display:block; width:100%; }
  .dtable td { display:flex; justify-content:space-between; gap:var(--sp-3); border:0; }
  .dtable td::before { content:attr(data-label); font-size:var(--fs-xs); color:var(--ds-color-ink-muted); font-weight:600; }
}
```

`sticky top:60px` 与 `.cmdbar` 高度联动：cmdbar 高度用 `--cmdbar-h:60px` 令牌（tokens.css owner 加），两处都 `var(--cmdbar-h)`。

### 5.2 表头文案来源问题（`app.js:2789-2815 renderTable` 的定案改法）

现状：`renderTable` 把 `Object.keys(rows[0])`（后端 snake_case 列名，如 `total_amount`）直接写进 `th`。契约规定 DOM 上同时挂**原始列名 + 展示名**两样东西：

```html
<th scope="col" data-col="total_amount" class="is-num">销售总额</th>
<td data-col="total_amount" data-label="销售总额" class="num">1,860</td>
```

- `th[data-col]`＝后端原始键（永不显示给用户，供 CSS/测试/排查定位）；`th` 文本＝展示名。
- `td[data-label]`＝展示名（窄屏堆叠时 `::before` 消费），`td[data-col]`＝原始键。
- 展示名解析顺序（app.js 唯一实现点）：`COLUMN_LABELS[key]` 查表 → 查不到则**原样显示 key 本身**，不猜中文（沿用 `app.js:107-114` "认不出来的值原样显示"纪律）。`COLUMN_LABELS` 首批只收后端已知口径的常见列（region/销售额之类，Wave B 按 `ToolResult.data.rows` 实际样例填），**没把握就不填**。
- 现有 `#dataset-table`（index.html:364-387）的静态 `th` + `data-sort` 结构不动。

---

## 6. 阶段阶梯 `.ladder`（`runs` 视图）

### 6.1 DOM（`index.html` 出双结构：阶梯 + 明细列表；`app.js paintSteps` 同步点亮）

```html
<div class="ladder" id="ladder" role="img" aria-label="进度：已完成 — 步 / 共 6 步">
  <div class="seg" data-step="profiling"><i class="seg__bar" aria-hidden="true"></i><span class="seg__lb">读取数据</span></div>
  <div class="seg" data-step="planning">…<span class="seg__lb">制定计划</span></div>
  <div class="seg" data-step="execution">…<span class="seg__lb">计算</span></div>
  <div class="seg" data-step="charting">…<span class="seg__lb">生成图表</span></div>
  <div class="seg" data-step="reporting">…<span class="seg__lb">撰写报告</span></div>
  <div class="seg" data-step="validation">…<span class="seg__lb">校验数字</span></div>
</div>
<p class="ladder__caliber">进度口径 = step_index / total_steps（<span id="ladder-count">— / 6</span>）。后端不暴露阶段级耗时。</p>
<ol id="steps" class="steps">   <!-- 现结构原样保留（测试钉 id=steps，paintSteps 依赖） -->
  <li data-state="waiting"><span class="dot"></span><span class="steps__name">读取数据</span><span class="steps__state">等待执行</span></li> …
</ol>
```

- `seg__lb` 六段文案＝现 `STEP_NAMES`（`app.js:77-84`：读取数据/制定计划/计算/生成图表/撰写报告/校验数字），**不另抄一套**。
- `data-step` 六值＝`STEP_ORDER`（`app.js:62`，被 `test_frontend.py:228-247` 与后端 `_STEPS` 逐字比对，**一个字母都不能改**）。

### 6.2 状态词与映射（`app.js` 产生，`app.css` 消费）

| 状态 | `.seg` 类 | `#steps li` | 视觉 |
| --- | --- | --- | --- |
| 已完成 | `is-done` | `data-state="done"`（现机制不变） | bar 底色 `--ds-shell-active-bg`（实心蓝） |
| 进行中 | `is-run` | `data-state="active"` | bar `--brand-500`，**脉冲**：`animation:ladder-pulse 1.2s ease infinite`（只动 opacity，1→.35） |
| 未达 | （无类） | `data-state="waiting"` | bar `--ds-color-surface-sunken` 灰轨 |
| 跳过/未执行 | `is-skip` | `data-state="skipped"`/`failed` | 跳过＝`repeating-linear-gradient(90deg, var(--n-300) 0 3px, transparent 3px 6px)` 斜纹；failed＝`--err-*` 三色 |

`paintSteps`（`app.js:2363-2411`）改造边界：**函数名、签名 `(step, status)`、`li` 的 done/active/failed classList 写法、`STEP_STATE_TEXT` 五词（`app.js:88-94`）全部保留**；函数尾部追加"同步 `.ladder .seg` 状态 + 写 `#ladder-count` + 更新 aria-label"约 20 行。状态词对外契约用 done/active/pending/skip 时，DOM 上 `.seg` 用 `is-done/is-run/is-skip` 三态 + 默认，`li` 用原 `data-state` 词汇——两套词各自文件内自洽，映射表如上，不许第三个词。

### 6.3 严禁项（验收红线）

- 任何**阶段级耗时、阶段时间戳、按时间推算的百分比**不得出现（option-01.md §9 / 00-brief 2.2-3）。只读 `step_index/total_steps` 与 `status/step`。
- 允许：整条任务墙钟 `#elapsed`（"已用时"，created_at→finished_at 真实值，`app.js:2437-2454` 逻辑原样）；轮询说明文案沿用"每 1000ms 轮询…非实时推送"（`POLL_INTERVAL_MS=1000` 被测试钉字面量）。
- 工作流十值/任务六值中文映射＝现 `historyStatusText`/`TASK_STATUS_TEXT` 原样复用，不新表。

---

## 7. 报告页 `article.doc`

### 7.1 DOM 契约（壳在 `index.html`，内容由 `app.js renderReportInto` 产出，样式在 `app.css`）

```html
<article class="doc" id="report">          <!-- ⚠️ id=report 被测试钉死（REQUIRED_IDS），换壳不换 id -->
  <header class="doc__head">
    <p class="eyebrow">分析报告 · 编号 <span class="doc__rid" id="report-id">…</span></p>
    <h2 class="doc__title" id="report-title">…</h2>     <!-- 注意：现类名 .report-title 改为 id 直挂，app.js 同步 -->
    <p class="doc__meta">
      <span>生成时间 <b id="report-created">…</b></span>
      <span>数据截至 <b class="asof-ref">…</b></span>    <!-- 同 §2.3 口径，取不到写 — -->
      <span>来源 <span class="src src--llm">大模型撰写</span></span>  <!-- srcTag() 现组件（app.js:2073），llm_used 决定 -->
    </p>
  </header>
  <div class="doc__body">
    <section class="doc__section">
      <h3>{section.heading}</h3>
      <p class="doc__text">{section.text}</p>            <!-- .hl 见下 -->
      <p class="doc__cites">依据：<sup class="cite" data-task-id="T1">T1</sup>…</p>  <!-- section.cites 逐条，class=cites 机制沿用 app.js:2845-2847 -->
    </section> …
    <section class="doc__quality">…data_quality_notes…</section>
    <section class="doc__validation">
      <p class="sev sev--error">error</p><p class="sev sev--warning">warning</p><p class="sev sev--info">info</p> <!-- 徽标类 sev--{severity}，三级字面量来自后端枚举（schemas_workflow.ValidationIssue.severity），message 原文跟在徽标后 -->
    </section>
  </div>
  <footer class="doc__sources">
    <h4>证据来源</h4>
    <div class="src-line" data-task-id="T1"><span class="src-line__tid">T1</span>
      <span class="src-line__desc">{tasks[].description 对应 task_id}</span>
      <span class="src-line__tool">{工具名，字段缺失则整段不渲染}</span></div> …
  </footer>
</article>
```

### 7.2 三条硬规则

1. **角标↔证据一一对应**：正文每段 `sup.cite[data-task-id]` 的集合＝证据区 `.src-line[data-task-id]` 的集合（都来自各 `sections[].cites` 去重并集）。渲染函数一个：`renderReportInto(container, report)`（`app.js:2818`）改内部不改签名；历史详情复用同一函数。
2. **`.hl` 关键数字**：后端正文没有高亮标记。定案：`app.js` 只允许对"出现在 `validation.checked_numbers` 核对范围里"的纯数字 token 做 `<span class="hl">` 包裹——首版实现允许**不做 .hl**（宁缺毋滥，包裹规则错了就是给没核对过的数字加冕）；若做，只包 `sections[].text` 中能在 `results` 找到原值的数字。样式契约：`.hl{font-weight:700;color:var(--ds-color-ink-strong);font-variant-numeric:tabular-nums}`。
3. **无导出/下载/分享**：DOM 里不得出现这三个按钮或对应图标 symbol 引用（sprite 里的 `#i-download` 保留定义没关系，doc 视图不许 `use` 它）。

---

## 8. 登录页（`login.html` + `login.css` + `login.js`，归 Wave B 第 4 个 agent）

### 8.1 两栏

`app/web/login.html` 现结构已是左品牌面板(`section.brand`) + 右表单(`section.panel`)；改为 option-01 §6 的口径：

```css
.auth__shell { display:grid; grid-template-columns:1.05fr .95fr; min-height:100vh; }  /* 现类名 auth__shell 保留（body class="auth" 也保留） */
.brand { background:var(--brand-900); color:#fff; }      /* 侧值/文案下见 8.2 */
@media (max-width:768px) { .auth__shell{grid-template-columns:1fr} .brand{display:none} }
```

### 8.2 三形态（现有 `data-mode` 机制保留并补第三形态文案）

| 形态 | 触发 | 契约 |
| --- | --- | --- |
| 日常登录 | `/api/auth/me` 正常且已建账号 | 标题"欢迎回来"；hint 沿用现 `data-hint-login` 三条 |
| 建管理员 | `me` 返回 `needs_bootstrap=true`（现 login.js 已实现分流；`/login?bootstrap=1` 亦由 `scripts/check_env.py:220` 提示） | `[data-mode="bootstrap"]` 元素显形：标题"创建第一个管理员"、副文案用现 index 那句（login.html:149）；**显示名称** `#display-name[name="display_name"]`（可选，后端 `display_name` 真实字段）；**确认密码** `#password-confirm` 保留并写明"仅本地校验，后端 bootstrap 请求不含此字段"（现有 hint 文案 250 行需补这句——⚠️ 唯一文案增改点）；提交按钮文案走现 `data-mode` 双 span（267-268）→ `POST /auth/bootstrap` 口径不变 |
| 失败态 | 401/429/503 | 统一进现 `#alert`（`role="alert" aria-live="assertive"` 保留）；文案表：401"账号或密码不正确"（不泄露用户是否存在——`tests/test_auth_api.py:477` 后端契约，前端不许自造"用户不存在"）；429"尝试次数过多，请在 {Retry-After} 秒后重试"（倒计时只改 `#alert-detail`，其 `aria-live="off"` 保留）；503"服务暂时不可用，请联系管理员或稍后重试" + `#alert-retry` |

口令：`autocomplete` 绑定保持现值（username / current-password / new-password）。全页唯一主按钮＝`#submit`。

### 8.3 归属

`login.html` 出 DOM（现有 ids 全保留：`auth-form username display-name password password-confirm submit alert notice …`）；`login.css` 只改版式（两栏比 1.05fr .95fr、brand-900 面板、文案层）；`login.js` 逻辑基本不动（三形态分流已存在），只补 401/429/503 文案表若缺。`/login`、`/static/login.*` 的路由行为不许碰（`tests/test_auth_api.py:316-354`）。

---

## 9. 响应式三档（每区块降级一览）

> **【裁定回填 · Wave D】本节表头的"≤768"落地成了 ≤760**（**D-2**：沿用存量断点 **480 / 760 / 1080**，
> 不新增断点、不改测试；`test_frontend.py` 钉住了"某文件内只允许一条 `@media (max-width:480px)`"，
> 8px 差没有视觉后果）。同理，`@media (max-width:768px)` 在本节与 §5.1 / §8.1 的代码片段里都应读作 **760**。
> 另两处以落地为准：① 侧栏在窄档是**抽屉**（沿用 `.is-open` + `#nav-toggle`），不是横推行；
> ② 命令条文案是「数据资产更新于」，不是"数据截至"（**D-7**）。

| 区块 | 1440（基准） | ≤1080 | ≤768 |
| --- | --- | --- | --- |
| 外框 `.frame` | `240px 1fr` | 不变 | 单列：`.sidebar` 由 `#nav-toggle` 变抽屉（沿用现 `.is-open` 机制，`app.js:913+`）；侧栏 DOM 不重排为横推行——本项目已有抽屉实现，别为原型重写一遍 |
| `.cmdbar` | 一行 60px | 隐藏 `.cmdbar__note` | `height:auto; flex-wrap:wrap; padding:10px var(--sp-4)`；`#llm-badge` **不许在任何断点隐藏**（`test_frontend.py:450-452`）；480px 断点让位规则沿用（见 §11#13） |
| `.posture__row` | 4 列 | `1fr 1fr`（动作列并入第二行尾） | 单列堆叠，行间 `--n-100` 分隔 |
| `.metrics` | 4 列 | `repeat(2,1fr)` 2×2 | 1 列（border-right→border-bottom 对应换边） |
| `.two` | `1.6fr 1fr` | `1fr` | `1fr` |
| `.dtable` | 全表+粘性头 | 全表+横向内滚（`.table-wrap`） | `td::before`+`data-label` 堆叠（§5.1），无整页横滚 |
| `.ladder` | 6 段一行 | 不变 | `.seg{flex:1 0 30%}` 换行 |
| `article.doc` | max-width 900px | 不变 | padding 减半，meta 换行 |
| 登录 | 1.05fr .95fr | 不变 | 单列、隐藏 `.brand` 面板 |

---

## 10. 文件归属与衔接点

| 文件（Wave B owner） | 主导区块 | 它**产生**的 class/id | 它**消费**的来自他人 |
| --- | --- | --- | --- |
| `app/web/index.html` | §1 外框/侧栏骨架、§2 命令条 DOM、§3/§4 容器、§5 静态表头、§6 `.ladder`+`#steps` 壳、§7 `article.doc` 壳、六个 `section.view[data-view]`（+新增 `view-runs`） | 全部结构与 ids（含 §11 钉子 ids、`data-view`、`data-step`、`data-col`/`data-label` 静态表头、`.topbar__link`、`.dropzone`） | 只引 `/static/tokens.css` → `/static/app.css`（**这个先后与字面量不许动**，`test_frontend.py:139-144`） |
| `app/web/app.css` | 上述所有区块的样式；`.dtable` 增强、`.posture/.metrics/.ladder/.doc/.cmdbar/.nav__item[aria-current]` | 类定义的**唯一来源** | index.html 的类名/ids；tokens.css 的 `--ds-shell-*`、`--cmdbar-h` |
| `app/web/app.js` | VIEWS/VIEW_GROUP(+runs)、`applyView`（写 `#cmdbar-title`/aria-current/hidden）、`paintSteps` 扩展、态势带装配(§3.3)、`renderTable`(§5.2)、`renderReportInto`(§7)、徽标/asof 数据 | 运行时 DOM：`.posture__row`、`.seg.is-*`、`sup.cite`、`.src-line`、`td[data-label]`、`#badge-*` | index.html 的 ids（§11 全部）、`.view[data-view]`/`.nav__item[data-view]`/`#steps li` 选择器语义 |
| `app/web/login.html/css/js` | §8 全部 | 登录页自有的版式类（`.auth__shell .brand .panel …` 现名保留） | tokens.css（与主页面同源，不许自带色值，`test_frontend.py:342-362` 注释的授权逻辑） |

衔接红线：**class 由 index.html/app.css 定义、由 app.js 按名读写**。任何一方要改名，先回本文改行，再动代码。跨文件的选择器契约清单（app.js 会 query 的新选择器）：`#posture-body`、`#metrics-grid`、`.ladder .seg[data-step]`、`#ladder-count`、`#cmdbar-title`、`#cmdbar-health`、`#cmdbar-asof`、`#badge-datasets`、`#badge-runs`、`#report-id`、`.userpop__settings`。

---

## 11. 不可回退清单（逐条亲眼核对过，`file:line` 均可复验）

判据两档：**【保留】**＝结构/字面量必须原样，否则测试红；**【同步】**＝重构后必须同批改这条测试（改动受控、需主控批准）。

### A. `tests/test_frontend.py`

1. 【保留】`:35-53` REQUIRED_IDS 十七个元素 id（`file upload upload-state dataset-summary llm-badge preview columns missing-top3 question ask ask-state steps elapsed workflow-state workflow-error charts report`）在 `GET /` 返回体中以 `id="…"` 形式存在（`:101-112` 断言）。迁移进 runs/dataset 视图可以，**删/改名不行**；`steps/elapsed/workflow-state/workflow-error` 搬到 `runs` 后仍在 index.html。
2. 【保留】`:115-124` `index.html` 必须含 `<meta charset="utf-8">` 且文件是合法 UTF-8（Windows GBK 乱码防线，index.html:4 注释）。
3. 【保留】`:127-136` `/static/app.js`、`/static/app.css` 可取到（路由不动）。
4. 【保留】`:139-149` `index.html` 中 `/static/app.css` 先于 `/static/app.js`；且 `index.html/app.css/app.js` **三个文件的纯文本里不得出现 `http://` 或 `https://`——注释里的举例也算**。新代码注释里写接口只用相对路径（`/api/…`），要举例"网址"时写 `//` 开头或不写。原型文件在 `design-options/` 下不受此测试管辖。
5. 【保留】`:152-211`（安全类：`data/` 不挂载、`/static` 不越狱、`/api/health`+`/docs` 可达）——与前端文件无关但重构期间别顺手动 `app/main.py`。
6. 【保留】`:199-211` `app.js` 字面量接口路径七条：`/api/datasets/upload`、`/api/datasets/`、`/api/datasets/${id}/profile`、`/api/analysis`、`/api/workflows/`、`/charts/`、`/api/health`（`API` 表 `app.js:30-47` 原样，模板字符串写法不许改成拼接）。
7. 【保留】`:214-225` 六条"正在…"步骤文案（`STEP_LABELS`，`app.js:64-71` 原样）。
8. 【保留】`:228-247` `const STEP_ORDER = [`（`const` 声明+字面量数组，`app.js:62`）逐字与后端 `_STEPS` 匹配；`const STEP_LABELS = {` 的声明形状（`:245` 用它切块）不许变。§6 的 `data-step` 就是这六个值。
9. 【保留】`:250-269` 字面量 `POLL_INTERVAL_MS = 1000`、以 `const TERMINAL` 开头的单行内含 SUCCESS/SUCCESS_WITH_WARNINGS/FAILED（`app.js:52`）、`clearInterval`、`has_report`、`js.count("API.report(") == 1`（报告接口全 js 只允许一处调用——§3.3 态势带取报告**必须复用 `loadReport` 这条唯一调用点或其封装**，不能在装配函数里写第二个 `API.report(`。⚠️ 这是态势带实现最大的暗坑：逐条取 report 请写 `await api(API.report(id))` 一次并抽成共享函数，让字面计数保持为 1——若封装导致 `API.report(` 出现两次，测试红，需走【同步】申请。定案：状态常量+封装写法保持字面量恰好一处）。
10. 【保留】`:272-295` 错误码字面量（FILE_TOO_LARGE/UNSUPPORTED_FILE/SERVICE_UNAVAILABLE/WORKFLOW_TIMEOUT/data.error）与禁 `resp.ok`/`response.ok`/`status === 200`/`status_code`。
11. 【保留】`:342-379` `app/web` 目录**排序后恰好七个文件名**（app.css/app.js/index.html/login.css/login.html/login.js/tokens.css）。Wave B 不得新增/改名/拆分任何前端文件（新组件一律进现有六件，tokens 进 tokens.css）。
12. 【保留】`:385-413` `app.css` 必须有 `.dropzone input[type="file"]` 规则块，块内**无** display:none/visibility:hidden，**有** `clip-path` 与 `position: absolute`；且 CSS 全文含 `.dropzone:focus-within`。上传卡搬家时整块带走。
13. 【同步或保留二选一，默认保留】`:416-455` 依赖旧顶栏名：`.topbar__product` 规则块（须含 `text-overflow: ellipsis`/`overflow: hidden`/`min-width: 0`）、`@media (max-width: 480px)` 块内含 `.topbar__link` + `display: none` 且不含 `llm-badge`、`index.html` 含"接口文档"四字。→ 低成本路径：cmdbar 里的品牌/链接**继续沿用这两个类名**（`.topbar__link` 已在 §2.2 保留；给 `#cmdbar-title` 外加一个 `.topbar__product` 承载 span 即可满足正则）。若主控批准改名，则同批改这份测试的 4 处选择器。
14. 【保留】`:460-549` `app.css` 必须有 `:root{…}` 十六进制令牌块；`.btn:disabled, .btn[aria-disabled="true"]`、`.btn--primary:disabled, .btn--primary[aria-disabled="true"]`、`.btn[data-loading="true"]`、`.btn--primary` 四条规则**逐字选择器**存在；禁用态不得用 opacity，color/background 走 `var(--令牌)` 且静态对比度 ≥4.5。→ 新按钮（发起新分析等）继续吃 `.btn/.btn--primary` 类，别造 `.button` 体系。
15. 【保留】`:101-113` 之上另注意：`GET /` 页体即 index.html，任何"骨架先渲染假工作台"的写法都受 `data-auth` 门约束（§1.1）。

### B. `tests/test_health.py`

16. 【保留】`:95-102` `GET /` 返回 `text/html`（`/` 路由仍发 index.html，不改返回方式）。

### C. `tests/test_auth_api.py`

17. 【保留】`:316-333` `/login` 公开且 `text/html`；`/app` 未登录 303 且 `location === "/login"`。§1.1 的会话门机制与此配套，不许改成"单页内嵌登录"（那会同时废掉 #17 与 login.* 的存在意义）。
18. 【保留】`:345-354`（同段）`/static/tokens.css` 200 可达——tokens.css 是唯一令牌入口。
19. 【保留】`:357-424` login.html 作为文件被服务端路由读取（内容无断言；类名自由，ids 见 §8）。

### D. `tests/test_check_docs.py`

20. 【保留（无关但勿伤）】该文件**不钉前端 DOM**；它钉的是 `docs/HISTORY_ROBUSTNESS.md` 台账与 `app/api/routes_workflows.py:218-219` 行号锚点（`:84-88` `_TARGET_LINE`/`_DECL_LINE`、`:321-360` 行号 219/220、`replace_line(219,…)`）。Wave A/B 均不许触碰 `routes_workflows.py` 的这两行或在其上方插行（前端重构完全不需要动它）。`_tree_hashes`（`:94-125`）对 `app/ tests/ scripts/ docs/` 全树比对，只在镜像变异中生效——**Wave B 改 `app/web/*` 后若有人跑旧基线镜像缓存不会误伤**，但 docs/ 下被锚定的文件不许挪行。
21. 【结论】`scripts/check_docs.py` 全文（454 行）无一处读取 `app/web/*`（grep 无 `index.html/app.js/app.css/tokens/login` 命中）——文档台账对前端**零结构断言**。
22. 【结论】`conftest.py`（346 行）无前端断言（只重定向 tempdir、AUTH_DB_PATH、TestClient 自动会话）。

### E. 非 pytest 但会咬人

23. 【保留】`scripts/check_env.py:239-254` 启动自检要求 app/web 存在上述七个文件（warn/bad 级别，同 #11）。
24. 【行数阈值】**未发现**：四个指定文件里没有任何"前端文件行数上限/下限"断言（全文检索 行数/len/splitlines 已核）；唯一行级断言是 `test_frontend.py:256-258` 对 `const TERMINAL` 所在**行**的搜索（属 #9）。若主控清单里有"行数阈值"一说，来源应在其它测试文件，请转告具体名字，本契约按"无此约束"记录。

### F. 本文档自身

25. 契约中所有片段**不含** `http://`/`https://` 字面量；Wave B 抄片段进三个受 `:139-149` 管辖的文件时，同样不许引入。

# AI Market Intelligence Studio —— 企业级升级最终验收报告

> 本文件由 **Lead（主智能体）** 亲自撰写，是任务书第十二部分「最终交付」的总入口。
> 撰写原则：**区分「已实现 / 已测试 / 已浏览器验证 / 尚未验证 / 暂未实现」五种状态**，
> 凡是没跑过的就不写"已验证"，凡是靠代码读出来的就标明"读代码"。
>
> 状态标记约定：`【已实现】`、`【已测试】`、`【已浏览器验证】`、`【尚未验证】`、`【暂未实现】`
> 表格中不再保留 `TBD`：全部数字已在代码冻结后填入（冻结集见 §12.6.7）。
>
> **权威验收轮 = `.tmp/acceptance/runs/final-freeze4`**（2026-10-16 15:17–15:18，六档视口）。
> 早先的 `final-freeze3` 及其之前两轮均已被它取代（`runs/INDEX.md` 逐轮标注 ★ / ⛔），
> 本报告凡引用"权威轮"，一律指 `final-freeze4`；引用 `freeze3` 的地方已就地标明。

---

## 0. 一句话结论

待冻结后填写（见第 12 节）。

---

## 1. 项目与环境

| 项 | 值 |
|---|---|
| 项目名 | AI Market Intelligence Studio（AI 市场情报与智能分析平台） |
| 项目根 | `D:\个人项目01\ai-data-analyst-agent` |
| 技术栈（未更换） | FastAPI + Python 3.12.8 + pandas + matplotlib + SQLite；前端为**手写 SPA，无框架、无构建、无 CDN** |
| 运行时 | Node v24.16.0（仅用于 `node --check` 语法校验）、git 2.54.0 |
| Python 解释器 | `D:\个人项目01\ai-data-analyst-agent\.venv\Scripts\python.exe` |
| 启动命令 | `.\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --port 8128`（**必须单进程**：`--workers 4` 会让上传落在一个进程、查询落到另一个进程） |
| 数据目录 | 数据集元信息在**进程内存**（重启即丢）；归档库 `data/db/app.db`；账号库 `data/db/auth.db` |

**环境坑（已实测，写下来避免后人重复踩）**

1. 本机系统 TEMP（`C:\Users\jun\AppData\Local\Temp`）对 Python 子进程不可写，
   Playwright 启动会 `EPERM: operation not permitted, mkdtemp ...playwright-artifacts-...`。
   解法：先把 `$env:TEMP`/`$env:TMP` 指到项目内 `.tmp\pwtemp` 再 launch。
2. matplotlib 需要 `$env:MPLCONFIGDIR` 指向可写目录（否则报 `mkdir -p failed`）。
3. 本机 **`D:\` 根目录存在 `D:\pyproject.toml`**，因此 pytest **必须在项目根目录跑**，
   否则 rootdir 解析到 `D:\`，收集结果完全错乱。
4. **不带 `--reload` 的 uvicorn 不会重载 `.py`**：一位队友用 11:2x 启动的进程测到
   `GET /api/workflows/history` → 500 `HistoryStore.list_recent() missing 1 required keyword-only argument: 'owner_user_id'`，
   而当时磁盘上的代码签名与调用点都已正确。**结论：任何以 500 为证据的结论，必须先报出该服务的启动时间**，
   并确认进程加载的是当前代码（重启后同一请求变 200）。这不属于产品缺陷。

---

## 2. 多智能体实际使用情况（交付物第 11 项）

任务书要求「必须真实使用多个子智能体协作，至少 6 个，建议 7–8 个；禁止伪造多智能体记录」。
本次实际派生 **8 名队友**（全部 `context: fresh`），并且每名队友都有
**自己的写范围**，跨文件接缝由 Lead 裁决：

| 角色（任务书编号） | 队员 target | 负责的交付物 | 写范围 |
|---|---|---|---|
| Agent 1 国际设计研究员 | `design-research` | `docs/design-research.md`（含 §13 实现参考简报） | 该文件 + `.tmp/research` |
| Agent 2 UX 产品体验架构师 | `ux-architect` | `docs/design-directions.md`（三套方向 + 定案）、`docs/ux-spec-final.md`、`docs/limitations.md`、图表改造 | 上述文件 + `app/agents/charts.py` + `.tmp/ux`、`.tmp/charts` |
| Agent 3 高级 UI 视觉设计师 | `design-system` | `app/web/tokens.css`、`docs/design-system.md` | 这两个文件 + `.tmp/design` |
| Agent 4 高级前端工程师 | `frontend-engineer` | `app/web/index.html`、`app/web/app.css`、`app/web/app.js`（会话门、数据集列表、全站视觉重构） | 该三件套 + `.tmp/frontend*` |
| Agent 5 交互与动效工程师 | `login-page` | `app/web/login.html`、`login.css`、`login.js` | 该三件套 + `.tmp/login` |
| Agent 6 独立设计评审官 | `design-research`（**兼任**） | `docs/ui-review.md`、预评审文档与 27 张实测截图 | 该文件 + `.tmp/review` |
| Agent 7 浏览器视觉测试工程师 | `acceptance` | `.tmp/acceptance/run_acceptance.py` + 12 流程证据包 | `.tmp/acceptance` |
| Agent 8 响应式与无障碍专家 | `a11y-responsive` | `.tmp/a11y/a11y_audit.py`、`docs/a11y-audit.md` | 该文件 + `.tmp/a11y` |
| Lead（主智能体） | `lead` | 任务拆解与裁决、后端认证复核、隔离复核、README、全量测试、本报告 | `README.md`、`docs/final-report.md`、`.tmp/lead*` |

**必须如实记录的两件事**

1. **Agent 6 由 Agent 1 兼任**：DSH 的团队上限是 8 名成员，派生第 9 名（专职独立评审官）时
   工具直接报错 `Error: Team member limit 8 reached`。因此独立评审由 `design-research` 承担。
   它的**独立性依据**是：它没有写过任何一行 `app/web/**`、`app/*.py`、`tests/**`，
   只写文档与测量脚本；并且它的评审**只使用自己在真实 chromium 里测出的数值**。
   **但它同时是 Agent 1（调研）**，这层"同一名队员身兼二职"是真实限制，不掩饰。
2. **并行编辑噪声**：全量 pytest 必须在**代码冻结窗口**里跑，否则会出现
   单测的 sha256 镜像比对抓到别人正在写一半的文件（本次真的发生了两次，
   归因与自愈方式见第 10 节）。
3. **模型同构（第 11 项只能算"部分达成"，必须说清）**：任务书建议 7–8 个**不同角色**，
   本项目确实做到了"不同角色 + 不同写范围 + 可核验的独立产物"；但
   **所有队友实际使用的是同一个模型 `deepseek-v4.1-flash`**（会话默认模型），
   **没有**实现"不同角色配不同模型/不同厂商"的异构组合。
   因此第 11 项交付物记为**部分达成**：多智能体协作是真的，
   但**不能宣称做到了模型层面的异构协作**。
   另：`list_agents` 显示其中 `design-research` 与 `a11y-responsive` 的 `model` 字段为
   `deepseek-flash`，这是本会话默认模型变更之前创建的成员 ——
   按 DSH 规则**已存在的成员无法改模型**，正确做法是新派生成员；
   但这两名成员当时正在跑**最终验证轮**（纯测量工作，产物可由我用哈希/像素/原始 JSON 独立复核），
   中途替换会作废已采集的证据，故保留其产物并按同口径复核，**此处如实记录这一偏差**。

---

## 3. 交付物 2：真实登录认证（复用/新建说明）

### 3.1 结论：**不是加前端表单，是完整的服务端认证** 【已实现】【已测试】

后端（`app/api/routes_auth.py` 363 行、`app/core/auth.py`、`app/data/users.py`）：

| 能力 | 实现要点 |
|---|---|
| 密码哈希 | PBKDF2-HMAC-SHA256，`iterations=240000`，每账号独立盐；库中**无明文**（Lead 用字节扫描复核） |
| 会话 | 服务端 SQLite 会话表，令牌只存 SHA-256，Cookie `mis_session`：`HttpOnly; SameSite=lax; Path=/; Max-Age=28800` |
| `Secure` | 本机 http 开发必须 `AUTH_COOKIE_SECURE=False`；**部署 HTTPS 时必须设 `AUTH_COOKIE_SECURE=1`**（写进 README） |
| 首次部署 | `POST /api/auth/bootstrap` **只在账号库为空时可用**，且**不预置默认管理员**；已有账号再调 → 403 |
| 登录失败 | 统一文案「用户名或密码不正确」；**先查限流再验密码**（否则被锁账号仍能白跑 PBKDF2，是 CPU 耗尽入口） |
| 限流 | 连续失败 5 次 → 429 + `Retry-After`；这是 Lead 发现的**真实生产缺陷**（`login_locked_for()` 在 `locked_until == 0` 时把计数 pop 掉，导致限流永不生效），已修并被 36/36 复验覆盖 |
| 改密码 | 必须提供当前密码；成功后**踢掉该账号全部会话**并给当前设备重签 |
| 退出 | 服务端销毁 + 客户端删 cookie，接口本身幂等（不要求已登录） |
| 受保护路由 | `app/main.py:319-323` `_PROTECTED = [Depends(require_user)]` 集中挂在 datasets / analysis / workflows 三个 router 的 `include_router(...)` 上（漏写单个接口=裸奔，集中一处才显眼）；`/api/health` 与 auth 接口**故意公开** |

### 3.2 Lead 的独立复核（真实 HTTP，非 mock）【已测试】

- **认证行为 36/36 PASS**：`.tmp/lead_auth_verify/verify_auth.py`
  含匿名态、未登录 401、bootstrap、重复 bootstrap 403、logout 失效、
  错误密码 vs 不存在用户名文案逐字一致、**失败序列 `[401,401,401,401,401,429]` + `Retry-After=301`**、
  改密码踢旧会话、会话列表不泄 token、库内无明文。
- **多账号资源归属 21/21 PASS**：`.tmp/lead_iso/verify_isolation.py`
  真实 ASGI + 真实 SQLite + 真实 cookie（`with TestClient(app)` 让 lifespan 真跑）。关键断言：
  A 上传后 `GET /api/datasets` 有 1 条；B 登录后 `total=0`；B 读 A 的数据集/画像/任务/报告**全部 404**
  （不是 403，不泄露存在性，文案与"真的不存在"逐字相同）；
  **B 用 A 的 `dataset_id` 建分析任务也 404**（`app/api/routes_analysis.py:77 require_owned_dataset`）；
  A 重新登录后自己的资源仍可读（隔离没误伤）。

### 3.3 必须写进限制的认证事实 【暂未实现】

- **没有自助注册接口**：第二个账号只能用服务端 `UserStore.create_user()` 建（等价运维建号）。
  所以"多账号"目前只有运维能触发；因此**不得宣称"已实现多租户自助开户"**。
- 没有忘记密码/邮件重置（**不摆入口**）。
- 没有"记住登录/会话续期"（固定 8 小时）。
- 因此任务书提到的 `/register`、`/forgot-password` **不开放**（后端不存在这两个能力）。

---

## 4. 页面与路由（任务书第六、七部分）

| 路由 | 页面 | 结论 | 评分（§12.9.3 终审） |
|---|---|---|---|
| `/login` | 登录页（含首次部署 bootstrap 模式、401/429/5xx 三种失败态） | 【已实现】【已测试】【已浏览器验证】 | 86 |
| `/app#/workbench` | 工作台（真实统计 + 最近任务/报告 + 上传入口 + 引导式空态） | 【已实现】【已测试】【已浏览器验证】 | 85 |
| `/app#/dataset`、`/app#/dataset/{id}` | 数据集（真实列表/排序/分页/搜索 `q`/画像） | 【已实现】【已测试】【已浏览器验证】 | 89 |
| `/app#/analysis` | 市场分析工作区（数据与上下文 → 任务输入 → Agent 真实执行状态） | 【已实现】【已测试】【已浏览器验证】 | 89 |
| `/app#/report`、`/app#/report/{id}` | 分析结果与报告阅读 | 【已实现】【已测试】【已浏览器验证】 | 87 |
| `/app#/history`、`/app#/history/{id}` | Agent Runs / 历史归档 | 【已实现】【已测试】【已浏览器验证】 | 85 |
| `/app#/settings` | 设置（只放真实能力） | 【已实现】【已测试】【已浏览器验证】 | 85 |

七条路由的浏览器端到端验收落在权威轮 `.tmp/acceptance/runs/final-freeze4`（§11），
评分出自独立评审官在**最终冻结哈希**上的终审（`docs/ui-review.md` §12.9，
**七页全部 ≥85、每页无 P2**）。

未实现的能力**不作为正常入口出现**（无删除数据集、无任务取消、无导出按钮）。

---

## 5. 三套设计方向与交叉评审（任务书第三部分）

- `docs/design-directions.md`：三套**真机可预览**的单文件原型（`.tmp/design3/direction-A|B|C.html`）+
  6 张 1440 宽真实截图 + 完整说明 + 横向比较 + **含反对意见的交叉评审** + 不采用做法清单。
  三套在构图/信息组织/字体气质/密度上都不同，**不是同一套换三种颜色**。
- 三套的配色不是目测：脚本用 WCAG 2.1 公式算精确值，查出并修掉 4 处不达标原始色值
  （A 弱文字 3.56→5.20、A 数据柱 2.32→3.89、B 次色条 2.70→4.81、C 卡片弱文字 4.21→5.16）。
- **Lead 定案 D1–D7**（记录在 `docs/design-directions.md` §11.5，理由齐全）：
  D1 双形态（操作者页面 = C 的外层气质 + A 的线框纪律；报告阅读页 = B 的阅读语言，不换字体族、不引入酒红）；
  D2 数据集列表接口做；D3 深链接做；D4 登录页 + 首次部署态做；D5 不用衬线；
  D6 **阶段级耗时禁止显示**（后端没有阶段时间戳，显示就是编造）；D7 措辞收窄为「报告里的每个数字都能追回到某一次具体的工具调用」。

---

## 6. 设计系统（交付物 4）

- `app/web/tokens.css`：**402 行 / 209 个令牌**（兼容层 71 个与 `app.css:21-113` **同名字同值**，
  品牌层 138 个 `--ds-*`）；**故意不启用深色模式**（后端 matplotlib 出的是浅底 PNG，深色下会变白斑）。
- `docs/design-system.md`：1436 行，17 章 + 附录 B 自动令牌表；每条规则给"为什么"与可核对数值。
- 三个自检脚本**真实通过**（Lead 复跑过）：
  - `token_parity.py` → PASS（209 个令牌文档全覆盖、兼容层逐字同值、**0 个"只写在文档里的令牌"**）；
  - `contrast_check.py` → PASS（浅色 47 个配对 0 不达标；深色候选 25 个配对 0 不达标）；
  - `token_coverage.py` → 任务书要求的 14 类令牌**缺失 0 类**。
- 真实修掉的对比度缺陷：`--ds-color-ink-subtle` 由 `--n-500`(#73736e, 4.446:1) 改为 `#6f6f6f` 系实测 **4.712:1 / 白底 5.051:1**；
  渲染层 4 处 `--n-500` 误用（`app.css:439/466/476/494`）由前端改成 `--ds-color-ink-subtle`。
- 色盲筛查（不判红但影响实现）：浅色 deuteranopia 下 `--ds-chart-1` vs `--ds-chart-5` 模拟色最小距离仅 **0.021**，
  → 图表系列**绝不许只靠颜色区分**，必须叠加线型/标记/直接标注（已在 `app/agents/charts.py` 落地）。

---

## 7. 数据可视化改造（真实落地）

- 图表是**后端 matplotlib 服务端渲染 PNG**，前端只是 `<img src="/api/workflows/{id}/charts/{name}">`（本项目没有任何前端图表库/构建）。
- 改造后：统一品牌色板与字号阶梯、弱化横向网格、`18` 类饼图合并为 6 类 + 「其他」（体积 62424→39275 B）、
  超长文字不截断而是加脚注、刻度格式化为 `2,487万 / 24.87亿`、空月份轴修正（13→12）。
- 契约未破坏：`chart_type` / `filename` / `point_count` 等字段保持兼容，`_DPI=120` 未变。

---

## 8. Agent 执行过程的真实性（任务书明令禁止假进度）

- 独立核查 `docs/ux-spec-final.md` §7 + `.tmp/ux/stage-truth.md`：
  **13/13 个阶段渲染元素都有真实后端字段支撑，0 个伪造**。
- 真实轮询证据：`t=16ms` 拿到 `PROFILING step_index=0`、`t=85ms` 拿到 `CHARTING step_index=3`、
  `t=327ms` 拿到 `SUCCESS step_index=6`；前端轮询间隔 `POLL_INTERVAL_MS=1000`（`app/web/app.js:58`）。
- **硬规则**：只显示真实 `status` / `step` / `step_index` / `tasks[].status`；
  `step_index/total_steps` 当"已完成阶段数"是对的；**阶段级耗时一个都不许显示**，
  页面显式写明「阶段级耗时：后端暂未提供」（后端改造需求：`orchestrator.py` 每次 `_set_step` 记 `(stage, at)`）。
- 顺手修掉一个真缺陷：`#elapsed` 曾显示负数与"2 分 60 秒"（`formatSeconds()` 进位 bug）。

---

## 9. 已知限制与未完成功能（交付物 10）

以 `docs/limitations.md`（ux-architect 交付，每条带代码证据）为准，Lead 另行独立确认的关键几条：

| 限制 | 证据 | 对 UI 的约束 |
|---|---|---|
| 数据集元信息在**进程内存**，服务重启即丢 | `app/data/store.py` 进程内字典；接口 detail 原文自陈 | 不得宣称"已持久化" |
| **无自助注册接口** | `app/api/routes_auth.py` 只有 bootstrap/login/logout/password/sessions | 不摆注册入口 |
| 无忘记密码/邮件重置 | 同上 | 不摆入口 |
| **无阶段级时间戳** | `app/schemas_workflow.py` 只有工作流级 `created_at/started_at/finished_at` | 不显示阶段耗时 |
| 无任务取消 / 无数据集删除 | `app/api/**` 无对应路由 | 不摆按钮 |
| 无 PDF/Excel 导出 | 同上（替代方案：`window.print()` + 打印样式表） | 不摆导出按钮 |
| 单进程部署 | README.md:530 | 文档写明 |
| LLM 未配置（`LLM_API_KEY` 为空） | `app/config.py` 明写**不再读 `.env`**，配置只来自进程环境变量 | 六阶段仍真实执行（真实 pandas + matplotlib），**只有报告叙述文字走规则模板**（`llm_used=0`），界面用「规则模板 / 大模型撰写」标签如实反映，**不许设假 key** |

---

## 9.5 本次「发现并真实修复」的缺陷清单（都可复现、都有证据）

> 这一节专门用来回答任务书的「不得把代码写完说成已验证」：每条都写清**谁发现的、机制是什么、怎么修、用什么证据确认**。

| # | 缺陷 | 机制（为什么坏） | 发现者 → 修复 → 验证 |
|---|---|---|---|
| 1 | **登录限流永远不生效** | `app/core/auth.py` 的 `login_locked_for()` 在 `locked_until == 0` 时把失败计数 pop 掉，于是连错 5 次也不会锁 | Lead 发现 → backend-tests 修 → Lead 复跑：`[401,401,401,401,401,429]` + `Retry-After=301`（36/36 PASS） |
| 2 | **归档静默失败**（历史永远为空） | `HistoryRecord.owner_user_id` 被做成必填无默认值，而 `app/core/archive.py` 的 `build_record()` 没传 → 归档是旁路、异常被吞 → 分析能跑完但历史列表恒空 | acceptance 发现（服务端日志 `TypeError: HistoryRecord.__init__() missing 1 required positional argument: 'owner_user_id'`）→ 修复 → 独立探针 `.tmp/acceptance/repro_archive_owner_bug.py` 用**原始 SQL 直读库**确认（有归属→落库且 `get_for_user(2)=None`；无归属→NULL 谁也查不到） |
| 3 | **写路径没有归属隔离，主链路断裂** | `POST /api/analysis` 当时**没有** `Depends(require_user)`、归属校验用的是全局 `store.exists()`、建的 `WorkflowState` 没写 owner → 于是「拿别人的 dataset_id 提问会被 202 受理」，而"owner 为 NULL = 谁也不给看"导致**作者自己轮询也是 404**（上传→提问→看进度→看图→看报告整条链断掉） | ux-architect 发现（隔离实例实测：202 受理 + 120 次轮询全 404）→ backend-tests 修（`require_owned_dataset(...)` + 创建时写死 `owner_*`）→ Lead 独立复验 **21/21 PASS**（含「B 用 A 的 dataset_id 建任务 → 404」与「A 能轮询到自己的任务」） |
| 4 | **图表在深链接/刷新后不显示** | `app/web/app.js` 里 `renderCharts(...)` 只挂在「轮询进入终态」这一个分支上，而深链接路径当时是 `clearCharts(); loadReport();`，`loadReport()` 又只渲染正文 → 分享链接/硬刷新打不开图 | a11y-responsive 报告了 DOM 实测 `#charts figure=0/img=0`（后端同轮 200 且 `charts` 有 1 条）→ **Lead 读代码复核机制成立**（当时全文件只有一处 `renderCharts` 调用）→ frontend-engineer 在 task-16 修：`loadReport(options)` 增加 `if (opts.charts) loadChartsFor(state.workflowId);`（注释写明"报告接口不返回图表，所以需要图表的入口顺便取一次工作流状态并画图"）→ **冻结版上三条路径实测都能出图**：会话内 / 详情 URL 硬刷新 / 直接打开深链接都是 `#charts figure=1`、`img` 有非空 alt、`naturalWidth=960` |
| 4b | （诚实更正）上面第 4 条的**原始 DOM 观测后来被观测者自己推翻** | a11y-responsive 复查发现：早期那条 `figure=0` 是**探针测法错误** —— SPA 里 `page.reload()` 停在"报告中心"列表页，那里本来就没有图。**代码层面的机制与后来落地的修复都是真的**，但"当时那条 DOM 数字"不能当作证据引用 | 观测者本人纠正并写进 `docs/a11y-audit.md` §11.2；结论改为以冻结版三条路径的实测为准 |
| 5 | 运行时长显示负数与「2 分 60 秒」 | `app/web/app.js` 用 `Date.now() - created_at`（服务端时钟与浏览器时钟差可为负），且 `formatSeconds()` 没有进位处理 | ux-architect 发现 → frontend-engineer 修 |
| 6 | 历史页看不到 96% 的归档 | 前端写死 `fetchHistory(20, 0)`，后端 limit/offset 早就支持 → 476 条里 456 条永远打不开 | ux-architect 发现 → frontend-engineer 加分页 |
| 7 | `POST /api/analysis` 返回 200 被当成完成 | 语义应是 **202 Accepted**（已受理≠已完成） | backend-tests 修正并写进文档（Lead 的第一版脚本按 200 断言属脚本 bug） |
| 8 | `scripts/check_docs.py` 有 7 个**从来没成立过**的行号锚点 | 锚点行号是手写的，代码移位后没人发现；另有一处写死 `OWNER_ID = 1` 的猜测 | backend-tests 发现 → 修 → 现 rc=0 |
| 9 | 4 处 `--n-500` 压在浅灰底上 = **4.446:1 < 4.5** | 令牌可选但用法错；design-system 把 `--ds-color-ink-subtle` 改成实测 4.712:1，前端把 `app.css:439/466/476/494` 改用该令牌 | Lead 发现 + a11y 独立复核 → frontend-engineer 修 |
| 10 | 登录页 11 条可用性/无障碍缺陷（D-01…D-11） | 例：429 态焦点丢到 BODY（键盘用户要重新 Tab 整页才能重试）、390 宽首次部署主按钮 `bottom=956.5` 远在首屏之外、品牌栏有 181px 空洞（占 20.9%）、12–13px 文本占 78.6% | 独立评审官 design-research 实测 → login-page 逐条修 → 同口径复测：焦点→`#alert-title`、`bottom=802.6`、空洞→44px（5.6%）、12–13px 占比→37.0%；`e2e_login.py` 108/108 |
| 11 | **未登录深链在登录后丢目的地，并落到原始 JSON 错误体** | `app/web/login.js` 的 `appRouteFromHash()` 用 `"/app" + frag` 拼目标，而 `frag` 已被 `hash.slice(1)` 剥掉了 `#` → 生成**路径形式** `/app/report/<id>`；服务端没有这条路由（实测 404），浏览器渲染 `{"ok":false,…,"code":"NOT_FOUND"}`。受影响的是**所有** `/app#/…` 深链（report/dataset/history） | design-research 在真实浏览器里实测发现（修复前基线落路径形式）→ login-page 修（`app/web/login.js:605` 改 `return "/app#" + frag;`，并把前缀判定硬化为「只接受 `/app` 本身或 `/app#` 开头」）→ design-research 复测 `probe13_deeplink.py --tag postfix` **6/6 `verdict=True`**（含两条负向对照）→ Lead 用**完全独立**的装置复测 **22/22 PASS**（1440×900 与 390×844 两条深链逐字回到原目标、无 `NOT_FOUND`、退出后 `/app` 被 303 回登录、路径形式确实 404） |
| 12 | 图表条件数据 `point_count` 断言缺失（潜在误导：饼图 18 类各一条、差异极小的四值纵轴从 0 起） | 基线实测：饼图 18 个分组全部单独着色、接近值容易被看成"差异很大" | ux-architect 发现（数像素 + 渲染前后对比）→ 修：18 类合并为 6 块 + 「其他」并落脚注、四个接近值仍从 0 起并在图中写明"不截断纵轴"；新增 4 条断言（合并后 `point_count==6` 等）交 backend-tests 并入 `tests/` |
| 13 | **同屏时间自相矛盾：计数行用 UTC、别的行用本地时** | `app/web/app.js` 的 `fmtClock()` 用正则 `/T(\d{2}):(\d{2})/` 从**客户端自己的** `new Date().toISOString()`（UTC）里直接截 `HH:MM`，于是工作台 KPI 计数行显示「截至 04:36 · 24 条」，而同屏 `#asset-last-success` 走 `toLocaleString` 显示「2026/10/16 12:36:00」——**同屏差 8 小时**，会让人误判数据新鲜度 | **Lead 亲看冻结候选版截图时发现**（`v1440x900-workbench.png`）→ 临时解冻一次 → frontend-engineer 修（`app.js:2077-2089` 改为 `new Date(text)` + 本地 `getHours/getMinutes`，并从调用侧注释说明为什么不能截串）→ 红→绿：`.tmp/frontend3/probe_clock.py` 修复前 `-480.0` 分钟（2/4 passed）→ 修复后 `0.0` 分钟（**4/4 passed**，控制台错误 0）→ **字节级最小性证明** `.tmp/frontend3/prove_minimal_diff.py`：把新函数块反向替换回旧块后 SHA256 逐字节等于冻结基线 `1c3c5ae0…`，证明本次改动只动了这一块。**权威最终轮上的独立确认**：验收方在 `final-freeze3-1440x900/shots/03-workbench.png` 上量到同屏两处都是本地「截至 **13:36**」（与运行窗口一致），Lead **亲自打开该图目视复核**确认错位消失；且返工轮 A 只把 `app.js` 改了 3 字节（X-01 注释），`fmtClock` 函数体**未被触碰**（Lead 逐行读过 `app.js:2077-2089`） |
| 14 | **产品中文名在应用页写错，且与登录页不一致** | `app/web/index.html` 三处（`<title>`、顶栏 `.topbar__product-cn`、侧栏 `<strong>`）把产品中文名写成「AI 市场**行情**智能分析平台」，而任务书原文（`.tmp/task_spec_raw.md:19`）与登录页 `app/web/login.html:47` 用的都是「AI 市场**情报**与智能分析平台」——多了一个「行情」、少了一个「与」。这是**品牌名**，且出现在**每个应用页**的顶栏与侧栏上，直接扣"品牌识别"分（"行情"是证券行业的词，与本产品"市场情报与智能分析"的定位不符） | **独立评审官 design-research 在 `docs/ui-review.md` 里发现**（X-01，P2）→ Lead 核对任务书原文与登录页写法确认成立 → 派 task-22 由 frontend-engineer 修 4 处（`index.html:7`、`:99`（title 属性 + 文本）、`:158`、`app.js:2` 注释）→ **Lead 按字节 UTF-8 独立复核**：错误写法全仓 **0 命中**、正确写法 **4 命中** |
| 15 | 触控目标低于 WCAG 2.5.8 硬底线 | 表头排序按钮 `th-sort` 实测 **39×21.44 / 26×21.44**，低于 24×24 的下限；一批主要可点区域（导航项 211×37.09、用户菜单 98.03×32、顶栏链接 70.64×24、分页按钮 62×28、示例问题 chip 143×29.5 等）高度不足 44 | 独立评审官 design-research 报出（X-02，P2，95 处 <44px）→ frontend-engineer 在 `app.css` 新增「触控与点击目标」段 → **触控探针 27/69 → 70/70**（42 条同名校验由失败转通过、无一条变小、仍失败 0）：nav-item →211×44、user-btn →98.03×44、topbar-link →70.64×44、page-prev/next →62×44、chip →143×44、ask →105.64×44、`th-sort` →47×25.44（`app.css:1231` 设 24×24，`:1252` 在 ≤760px 提升到 `--ds-touch-min` 44px） |
| 16 | 登录页页脚分隔符对比度不足 | `p#foot-meta > span.foot__dot` 用 `rgb(143,143,138)` 压白底 = **3.25:1 < 4.5:1** | design-research 报出（X-05，P3）→ **Lead 裁定直接修、不走装饰豁免**（两个口径都干净）→ login-page 改用令牌 `var(--ds-color-ink-subtle)` → 真实 Chromium 实测 **5.05:1**（login 与 bootstrap 两页同值），全页最低对比度 5.05:1 |
| 17 | 两个前端文件里有非令牌字号字面量（11px / 11.5px / 11.96px / 12.5px / 12.88px / 9px） | `login.css` 的 `.pipe__num` / `.pipe__note` / `.pipe__out-text` 与 `app.css` 的 `.src` / `.listrow__mono` / `code` / `.th-sort::after`，绕过设计令牌体系 | design-research 报出（X-03，P3）→ 两个 owner 分别令牌化 → `login.css` 实测 `font-size:<数字>px` 字面量 **0 处**、登录页 <12px 文本条目 **0**（改前 sub12=12）。**诚实更正**：`app.css` **还剩 1 处** —— `app/web/app.css:769` `.dropzone__icon { font-size: 26px; }`（上传区图标字形尺寸，不是正文字号）。Lead 先前把结论写成"两个 CSS 里都 0 处"，是**只看 `login.css` 就外推**的错误；a11y-responsive 与 design-research **各自独立**指出该处仍在，Lead 复查 grep 确认它们对、**我错**。该处不影响 X-03 的结论（非正文、且是图标而非文字），但事实必须按实测写 |
| 18 | 4 处规则级裸色值 | `app.css:295-297` 的 `#4cc38a / #e0b25a / #e08a80`、`:854` 的 `#7a2b23` 未走令牌 | design-research 报出（X-04，P3）→ frontend-engineer 换成 `var(--ds-color-bar-dot-ok/-warn/-err)` 与 `var(--ds-color-danger-strong)`（`tokens.css:252-255`） |
| 19 | 两个页面都请求 `/favicon.ico` → 404 | `<head>` 里没有 icon 声明，浏览器按默认路径探测 | design-research 报出（X-06，P3）→ 两页各加 `<link rel="icon" href="data:,">`（**data URI，不新增静态文件**，`app/web/` 仍是白名单 7 个文件）。**诚实口径**：headless Chromium 不发 favicon 请求，因此**无法**用请求日志证明"不再 404"，只能证明 DOM 里已有该声明 |
| 20 | 390×844 下主按钮落在首屏之外 | `#ask` 按钮底边实测 **894.94px > 844px**，窄屏用户要先滚动才能点"开始分析" | frontend-engineer 在 task-22 顺带修（`@media (max-width:479px)` 收紧页头/卡片内边距 + 窄屏品牌名 ellipsis 兜底）→ **Lead 用第三套独立装置复测 390×844 底边 = 790.27px，余量 53.73px**（见下面第 21 条的读数澄清） |
| 21 | **1024×768 与 360×800 下主按钮掉出首屏（本次返工新引入的退化）** | task-22 为满足 WCAG 2.5.8 把主要可点区域统一抬到 44px 高，`#ask` 由 35.5px 变 44px，被向下推：**1024×768 底边 777.66 > 768（越界 9.66px）**、**360×800 底边 813.36 > 800（越界 13.36px）**。1024×768 属「普通笔记本」档，是任务书明确要求检查的四个宽度之一 | a11y-responsive 在最终审计轮发现（`PRIMARY_BELOW_FOLD` × 2）→ **Lead 用独立脚本 `.tmp/lead_ask/measure_ask.py` 复测，逐值复现**（777.66 / 813.36 / 44.00 高，与 a11y 报的完全一致）→ **定性为已知退化，如实记录、未修复**：修它要在 `#ask` 上方再收一轮垂直节奏，属冻结末尾的布局改动，且在 1024×768 上只差 9.66px（用户滚动约 10px 即可触达，不像 390 档那样是首屏关键操作） |
| 21b | （读数澄清）关于 390×844 的 `#ask` 底边，评价体系内部出现过两个互相矛盾的读数 | a11y-responsive 报 **842.27（余 1.73px）**，design-research 报 **790.27（余 53.73px）**，差 52px。**Lead 用第三套装置在同一次会话内、不滚动页面直接取 `getBoundingClientRect()` 判定**：390×844 = **790.27 / 余 53.73px**，即 **design-research 的读数正确，842.27 不可复现**（我复现了它另外两档 777.66 / 813.36，说明其方法本身可靠，仅 390 档那一次受了测量状态影响）。**我先前据此把"1.73px 极紧"写进报告，属采信了未复现的读数，此处更正** | 证据 `.tmp/lead_ask/ask_geometry.json`（含 `#ask` 祖先链逐层高度与 margin/padding）与 `ask_geometry_verified.json` |

---

## 10. 测试与构建结果（交付物 7）

- 改造前基线（Lead 亲测，项目根）：**123 failed, 686 passed, 12 errors**（821 collected）。
  唯一根因是今天新加的强制登录 + 33 处匿名 `TestClient` → 401；
  12 个 error 同源。诊断文档：`.tmp/regression_diagnosis.md`（6+1 节，含 9 条真实缺陷）。
- 修复原则：**不删、不 skip、不改期望值**，让测试拿到真实会话（`tests/auth_support.py`）。
- **全量汇总行（Lead 在代码冻结窗口亲自执行，项目根、`python -m pytest -q -p no:randomly`）**：
  ```
  ======================= 889 passed in 133.40s (0:02:13) =======================
  ```
  **0 failed / 0 error，退出码 0**；原始输出留档 `.tmp/freeze/pytest_full.txt`。
  收集数从 821 涨到 889：本轮新增 `tests/test_auth_api.py`(37)、`tests/test_datasets_api.py`(29)、
  `tests/auth_support.py` 等，**没有删除或跳过任何一个原有用例**（冻结版 `git status` 删除文件数 = 0）。
- **最终真树上的全量回归（Lead 亲自执行，返工轮 A/B 落盘之后）**：
  ```
  ======================= 889 passed in 110.60s (0:01:50) =======================
  ```
  原始输出留档 `.tmp/freeze/pytest_full_final.txt`。**与上一行的用例数完全一致（889 → 889）**，
  说明返工轮 A（应用页 5 条）与返工轮 B（登录页 3 条）**没有引入任何回归**；收集数不变也说明
  这一轮没有新增或删除任何测试。这次跑的是**最终哈希**那一版 `app.js`（含 `fmtClock` 修复；
  返工轮把它从 `918A891C…` 又改到 `91DCF193…`，差异只有一行注释里的「行情」→「情报与」，+3 字节，
  `fmtClock` 函数体本身**未被触碰** —— Lead 已逐行复核 `app.js:2077-2089`）。
- **最终交付树上的全量回归（Lead 亲自执行，task-28 四个缺陷修复之后）**：
  ```
  ======================= 902 passed in 109.49s (0:01:49) =======================
  ```
  **0 failed / 0 error，退出码 0**。`889 → 902` 的 **13 条全部来自两个新文件**
  （`tests/test_report_notice_has_no_internal_field_names.py` 6 条 +
  `tests/test_manage_account_password_rule_source.py` 7 条），
  **4 个被改的测试文件（`test_validation.py` / `test_report_llm.py` / `test_orchestrator.py` /
  `test_status_degradation.py`）用例数一个没增减**（逐文件 diff 为 0）——
  即改的是"钉子上的字"，不是"把钉子拔掉"。详见 §12.7。
- 定向子集（backend-tests）：`278 passed in 15.49s`（11 个文件，含新增 `tests/test_datasets_api.py` 29 条）。
- 前端契约测试：`pytest tests/test_frontend.py -q` → **19 passed**（白名单已扩到 7 个 `app/web` 文件，排序全等断言未弱化）。
- `scripts/check_docs.py` → rc=0（修复了 7 个**从来没成立过**的行号锚点 + 一个写死 `OWNER_ID = 1` 的猜测）。
- 前端无构建：用 `node --check` 做语法校验（0 错误）。

### 10.1 Lead 亲自跑的独立验证（不依赖队友的脚本与自述）

> 这一节是我（Lead）自己写脚本、自己跑出来的结果，装置全部隔离在 `.tmp/lead*` 下，绝不碰仓库里的 `data/db/auth.db`。任务书要求「不得把队友自评当成验收」，所以主链路的每条结论都至少有一份我的独立复现。

| 验证 | 脚本 | 结果 |
|---|---|---|
| 认证行为（真实 ASGI + 真实 SQLite + 真实 Cookie） | `.tmp/lead_auth_verify/verify_auth.py` | **36/36 PASS**：未登录受保护接口 401、`/api/health` 公开、bootstrap 只在空库可用、失败文案不区分用户名/密码、连错 5 次后 `[401,401,401,401,401,429]` + `Retry-After=301`、退出真失效、改密码踢掉旧会话、库内无明文密码/token |
| 多账号资源归属隔离 | `.tmp/lead_iso/verify_isolation.py` | **21/21 PASS**：A 上传 → B 读 A 的数据集/画像/任务/报告**全部 404 且文案与"真的不存在"逐字相同**；B 用 A 的 `dataset_id` 建分析任务同样 404；A 重新登录后自己的资源仍可读 |
| 登录页与路由（真实服务端响应） | `.tmp/lead/check_routes.py` | `/login` → **200**（16166 B，逐字含 `Turn Market Data Into Decisions.` 与中文副标题）；`/app` 未登录 → **303 → /login**（服务端保护，不是前端隐藏）；`/` → 200；`/api/auth/me` 匿名 → 200 `needs_bootstrap=true`。**⚠️ 口径已按 D-11 变更（2026-10-17）**：这一行的"逐字含英文主张句"是**当时的实测快照，保留作为证据链**；当前构建里登录页主显主张句是中文「把市场数据，变成能追溯的决策依据」，英文产品名降为次要行（`app/web/login.html` 品牌面板），裁定原文见 `docs/refactor/06-leader-decisions.md` D-11，落地记录见 `docs/COMMAND_CENTER_MIGRATION.md` §5。**重跑这条脚本时该断言会红，那是文档口径变了，不是路由坏了。** |
| 深链接缝（真实 uvicorn + 真实 Chromium，与队友脚本无关的第三套装置） | `.tmp/lead/deep_link_acceptance.py` | **22/22 PASS**：未登录 `/app#/report/<真实 id>` → `303 /login#/report/<id>` → 登录成功 → 最终 URL 逐字回到 `/app#/report/<真实 id>`；390×844 下 `/app#/dataset` 同理；页面无 `NOT_FOUND`、`h1` 存在；`POST /api/auth/logout` 后 `/app` 被 303 回登录；负向对照：路径形式 `/app/report/<id>` 服务端确实 404 |
| 契约测试（`start_analysis` 签名） | 项目 root 跑 `pytest tests/test_contracts_d3.py tests/test_contracts_d4.py tests/test_contracts_d5.py -q` | 修复前 **1 failed, 66 passed**（我独立复现的真实红）→ backend-tests 按「两步全等」修好后 **67 passed** |
| 令牌与对比度脚本 | `.tmp/design/token_parity.py`、`.tmp/design/contrast_check.py` | 两者 exit 0：文档 287 行令牌 ↔ 209 个令牌 0 处不一致、兼容层 71 个与 `app.css:21-113` 逐字同值；浅色 47 个配对 0 未达标 |
| **冻结版全量回归**（收到冻结哈希后重跑） | 项目根 `python -m pytest -q -p no:randomly` | **889 passed in 133.40s**，0 failed / 0 error / 退出码 0 |
| **最终真树全量回归**（返工轮 A/B 落盘后重跑） | 项目根 `python -m pytest -q -p no:randomly` | **889 passed in 110.60s**，0 failed / 0 error / 退出码 0；用例数与上一行完全一致 ⇒ 返工轮**零回归**。原始输出 `.tmp/freeze/pytest_full_final.txt` |
| **最终真树前端契约** | `pytest tests/test_frontend.py -q` | **19 passed in 1.40s**；`app/web/` 仍是白名单 7 个文件名（`login.html` 加 favicon 用的是 data URI，**没有新增静态文件**） |
| **最终真树导入冒烟** | `.\.venv\Scripts\python.exe -c "import app.main"` | `import ok` |
| **最终冻结哈希（返工轮后，Lead 亲自逐文件计算）** | `.tmp/freeze/web_hashes_final.txt` | 7 个文件的字节数 + mtime + 完整 SHA256，含与上一轮 `freeze2` 的逐文件差异说明 |
| **冻结版隔离复核**（第二次复跑） | `.tmp/lead_iso/verify_isolation.py` | **21/21 PASS**。诚实记录一个装置陷阱：该脚本**不会自动重置**自己的数据目录，第一次复跑用旧库得到 7 PASS / 9 FAIL（bootstrap 403 → 会话缺失 → 后续全部级联失败），清空 `.tmp/lead_iso/data` 后 21/21。这是**我的脚本缺陷，不是产品回归** —— 深链验收脚本会自动重置，所以它没有这个问题 |
| 冻结版哈希留档 | `.tmp/freeze/web_hashes.txt` | 7 个 `app/web` 文件的字节数 + 最后写入时间 + 完整 SHA256 |

### 10.2 本轮修改文件清单（交付物 9，`git` 生成，非人工回忆）

脚本 `.tmp/lead/change_inventory.py` → 完整清单 `.tmp/lead/change_inventory.txt`。基线 `HEAD=05fd9d9`。

> **⚠️ 两个口径，别混用**：
> ① **代码冻结那一刻的快照 = 30 改 / 20 新增 / 0 删除**
> （`git diff --stat` 为 `30 files changed, 4663 insertions(+), 453 deletions(-)`）；
> ② **当前工作树（task-28 之后）= 38 改 / 26 新增 / 0 删除**
> （`git diff --stat` 末行 `38 files changed, 4795 insertions(+), 503 deletions(-)`）。
> 差额 **8 改 + 6 新增**全部是冻结之后的收尾工作：task-28 的四个缺陷修复
> （`app/agents/validation.py`、`scripts/check_env.py`、`scripts/manage_account.py`、
> 4 个测试文件 + 2 个新测试文件）、task-28 连带改的 3 份文档
> （`docs/DECISIONS.md`、`docs/DEMO.md`、`docs/WORKFLOW.md`）、
> 以及 Lead 自己产出的 `docs/runbook.md`、`docs/runbook-evidence.md`、
> `docs/final-report.md`、`docs/ui-review.md`（终审）、`docs/a11y-audit.md`（定稿轮）等。
> **两个数字都对，只是时间点不同** —— 本报告下文凡引用"修改文件清单"，
> 以**当前 38 / 26 / 0** 为交付事实，以**冻结快照 30 / 20 / 0** 为"冻结时点了什么"。

**修改（冻结快照 30 个）**：`README.md`、`app/agents/charts.py`、`app/api/routes_analysis.py`、`app/api/routes_datasets.py`、
`app/api/routes_workflows.py`、`app/config.py`、`app/core/archive.py`、`app/data/history.py`、`app/data/store.py`、
`app/errors.py`、`app/main.py`、`app/schemas.py`、`app/schemas_dataset.py`、`app/schemas_workflow.py`、
`app/web/app.css`、`app/web/app.js`、`app/web/index.html`、`conftest.py`、`docs/HISTORY_ROBUSTNESS.md`、
`tests/test_analysis_api.py`、`tests/test_check_docs.py`、`tests/test_contracts_d4.py`、`tests/test_errors.py`、
`tests/test_frontend.py`、`tests/test_history_acceptance.py`、`tests/test_history_api.py`、
`tests/test_history_archive.py`、`tests/test_history_robustness.py`、`tests/test_history_store.py`、`tests/test_workflows_api.py`

**新增（冻结快照 20 个）**：`app/api/ownership.py`、`app/api/routes_auth.py`、`app/core/auth.py`、`app/data/users.py`、
`app/schemas_auth.py`、`app/web/login.css`、`app/web/login.html`、`app/web/login.js`、`app/web/tokens.css`、
`docs/a11y-audit.md`、`docs/design-directions.md`、`docs/design-research.md`、`docs/design-system.md`、
`docs/final-report.md`、`docs/limitations.md`、`docs/ui-review.md`、`docs/ux-spec-final.md`、
`tests/auth_support.py`、`tests/test_auth_api.py`、`tests/test_datasets_api.py`

**冻结之后追加的 8 改 + 6 新增**：
- 改：`app/agents/validation.py`（缺陷 A）、`docs/DECISIONS.md`、`docs/DEMO.md`、`docs/WORKFLOW.md`（连带文档）、
  `tests/test_validation.py`、`tests/test_report_llm.py`、`tests/test_orchestrator.py`、`tests/test_status_degradation.py`（同步字面量）
  （注：其中 `app/agents/validation.py` 等 8 个即上表差额；`docs/**` 的 `final-report`/`ui-review`/`a11y-audit` 属新增文件，不在此差额内）
- 新增：`docs/runbook.md`、`docs/runbook-evidence.md`、`scripts/check_env.py`、`scripts/manage_account.py`、
  `tests/test_report_notice_has_no_internal_field_names.py`、`tests/test_manage_account_password_rule_source.py`

**删除（0）**：本轮**没有删除任何原有文件**，也没有删除任何 API、任何测试（任务书 1.2 的「保护现有功能」由此可核对）。
`git show HEAD:<file> | Select-String '^\s*def test_'` 对比工作区，4 个被改的测试文件用例数差分**全为 0**。

> 说明：`.tmp/**`（所有脚本、截图、证据包）与 `.mplcache/` 都不进仓库，未列入上表。

---


## 11. 关键页面截图与证据包（交付物 8）

| 证据 | 位置 | 说明 |
|---|---|---|
| 端到端验收（**权威一轮 = `final-freeze4`**，2026-10-16 15:17:22–15:18:49） | `.tmp/acceptance/runs/final-freeze4-*/` + `.tmp/acceptance/REPORT.md` | **12 条流程 × 6 档视口 = 68 PASS + 4 N/A，FAIL 0、BLOCKED 0**（`cells={PASS:68,FAIL:0,BLOCKED:0,N/A:4}`）。`N/A` 是流程 12 在四个宽屏档（它的定义就是窄屏），**不计为通过**；流程 12 在 390x844 与 480x900 各 **5/5**，且 `evidence.viewport` 就是被测宽度（早期装置缺陷曾让 480 档报的其实是 390 的实测，已修）。**113 张截图**（`report.json.totals.screenshots = 113`）逐张记录像素 + 字节（1440x900=23、1280x800=16、1024x768=16、1920x1080=16、390x844=21、480x900=21）。跑前 7 个前端文件哈希比对 7/7 一致，内置比对，不一致就中止。**控制台 7 条全是同一句设计内 401**，`request_failed=0`、`page_errors=0`、非 401 的 4xx/5xx=0。**这一轮覆盖了 task-24 与 task-25 的落盘结果**：冻结集最后一次改动是 `login.html` 15:06:26，而本轮最早一次测量是 15:17:22（晚 10 分 56 秒）；各档 `server_startup_attempts=1`、`health_uptime_seconds` 0.20–0.33 s，说明应答的都是本轮新起的进程 |
| 端到端验收（**已被取代的三轮**，不作为交付判定） | `.tmp/acceptance/runs/final-20261016-123842/`、`final-20261016-124537/`、**`final-freeze3/`** | 同为 68 PASS + 4 N/A，但跑在**更早的哈希**上（`freeze3` 跑在 13:36，早于 task-24 的 `app.css` 14:10:15 与 task-25 的 `index.html`/`app.js`/`login.*` 15:04–15:06 落盘）→ 按证据纪律整轮标注「⛔ 已被取代」。**注意：`final-freeze3` 曾被本报告当作权威轮引用过，那是对的当时口径，现在已被 `final-freeze4` 取代** |
| 验收矩阵（人读版） | `.tmp/acceptance/runs/final-freeze4/MATRIX.md`、`RECEIPT.md`、`VERSIONS.md` | 逐格状态 + 每档控制台/401/失败请求/API 调用数 + 端口释放证据 + 20 个文件 SHA256（`pinned_versions` 20 条） |
| 核验轮可靠性（第三方生成，不是自查） | `.tmp/acceptance/runs/INDEX.md` | 101 轮的可信性索引，逐轮打标：★ 权威轮 = `final-freeze4` 及其 6 个兄弟档；⛔ 已被取代 = 前三轮各 7 行。**该文件由 `runs_index.py` 从各轮原始 `result.json`/日志独立生成**，能反证"哪一轮才是权威"不是我事后自己说的 |
| **验收方独立核对修复的可见结果** | `.tmp/acceptance/runs/final-freeze3-1440x900/shots/03-workbench.png`（**历史证据，非本轮权威轮**） | ① **时钟错位消失**：同屏两处都是本地「截至 **13:36**」（与运行窗口一致），缺陷 #13 在 freeze3 轮被独立确认修复（该文件哈希为 `918a891c`，即 fmtClock 修复版）；② **品牌名正确**：侧栏与顶栏都是「AI 市场**情报与**智能分析平台」，「行情」0 处。Lead 已**亲自打开这张图目视复核**，两条都成立。freeze4 权威轮复用同一 `app.js` 的 fmtClock 代码路径（`app.js:2077-2089` 自 12:43 起未再改动） |
| 登录页专项 | `.tmp/login/shots/` | 27 张：bootstrap/401/429/5xx/已登录跳转/390 移动端 |
| 独立评审截图 | `.tmp/review/shots/` | 评审官自己的 27 张（4 视口 × 多状态） |
| 四档全站截图（修复过程） | `.tmp/frontend3/shots-v2/` | task-16 的四档页面截图 + 真实操作链（冻结前版本） |
| 无障碍审计（**最终定稿**） | `.tmp/a11y/` + `docs/a11y-audit.md`（**944 行 / 94956 B / sha256 `987d003ac778…`**） | **定稿轮 §17**：48 组（8 页 × 6 断点）全部成功、失败 0 = **187 条：严重 0 / 轻微 125 / 提示 62 / 阻断 0**（主轮 189 条含 2 条严重，已被 task-24 修复）；详见下节与 §11.1 |
| 改造前后图表 | `.tmp/charts/before\|after/` | 同数据同图型对比 PNG（含数像素实测的主色十六进制） |
| **给用户直接看的精选截图** | `.tmp/present/` | 11 张（登录/工作台/数据集/画像/分析/报告图表/历史/设置 + 390 窄屏三张），全部取自上面这一轮权威验收 |

### 11.1 无障碍与响应式审计的真实数字（`docs/a11y-audit.md`，6 档断点 × 8 页）

> **口径（务必先读）**：`docs/a11y-audit.md` 有**主轮**与**定稿轮（§17）**两套数字。
> 定稿轮跑在 2026-10-16 15:17:14 启动的隔离实例上，**晚于全部前端文件写入**
> （最晚 `login.html`/`login.js` 15:06:26），且经两套独立装置确认**指纹稳定**
> （`run_audit.py` → `stable=true`；自写 `snapshot_hashes.py` 四次拍摄
> 15:16:48 / 15:19:13 / 15:22:18 / 15:30:26 全部 `all_match=true`）。
> **下表以定稿轮为准**；主轮数字只在"跨轮可比性"一行里作为历史对照出现。

| 项 | 实测结果（定稿轮 = §17） |
|---|---|
| 总发现数 | **187 条 = 严重 0 / 轻微 125 / 提示 62**，阻断 0。（主轮 189 条含严重 2，差距就是下表那两条 `PRIMARY_BELOW_FOLD`） |
| **⚠️ 跨轮可比性警告（审计方主动声明，Lead 采纳）** | **189 条与冻结前基线（中间态）的 503 条不可直接相减**：两轮工具版本不同（基线 `a11y_audit.py 1.0.0` / 最终轮 `1.2.1`），1.0.0 → 1.1.x 之间补上了 WCAG 2.5.8 的 Inline/Spacing 例外与 sr-only 例外。因此"严重 137 → 2"里**绝大部分是工具口径修正，不能算成产品改进**。跨轮可比的是**同一选择器的实测值**。基线轮还有 28/32 组采的是**错误态渲染层**、最终轮全是正常态，DOM 不同，条数更不可比 |
| **反证：两条严重项为什么消失（不是采集抖动）** | 六档**整页 `scrollH` 全部变短**，缩量按断点分层：1440×900 / 1280×800 **−101**、1024×768 / 768×1024 **−145**、390×844 **−164**、360×800 **−188**。与 task-24「基础规则在所有宽度生效 + 4 组媒体查询按断点生效」的结构吻合 → **布局是真的变短了** |
| 对比度 | 正文最低 **4.701:1**（`span.nav__flag`，rgb(111,111,106) 压 rgb(244,247,251)）；应用画布底 4.712:1、白底 5.051:1；`CONTRAST` 命中 **0**。纯装饰分隔点按 WCAG 1.4.3「纯装饰不适用」降级 |
| 之前的真实缺陷（已修） | 4 处 `--n-500` 文本规则 **4.446:1 → 4.712:1**；跨轮真正可比的项：`CONTRAST` **57 → 0**、`ELEM_OVERFLOW` **35 → 0**、`TRUNC_NO_HINT` **14 → 0**、`ARIA_PROBE_ERR` **7 → 0** |
| 横向溢出 | **48/48 组 `scrollWidth == clientWidth`**（六档断点都没有横向滚动） |
| 键盘可达性 | **48/48 组 `unreached=0`、`noRing=0`**（未到达 0、缺焦点环 0），Shift+Tab 可回退；第一个 Tab 停靠点是 `a.skip-link`（真实按键实测，不是读 CSS） |
| Escape / ARIA 闭环 | `#nav-toggle`、`#user-btn` 六档全部 `aria-expanded false→true`，Esc 后回 false **且焦点回触发按钮**；`#toggle-password` `aria-pressed false→true` |
| 减少动效 | **8/8 页通过**（`prefers-reduced-motion: reduce` 下运行中动画 0、无限动画 0、过渡 0） |
| 触控目标 | `TARGET_LT44` **77 条**、`TARGET_LT24` **36 条**。**口径裁定（审计方给出，Lead 采纳）**：合规底线用 **WCAG 2.2 AA 2.5.8 的 24×24（含 Inline/Spacing 例外）**，36 条全部命中例外故**不是 AA 失败项**；44×44 出自 **WCAG 2.1 AAA 2.5.5（Enhanced，增强级）**，只作项目自选的高标准单列改进建议。**两套判据不得混用**。实测：`#nav-toggle` 44×44、`#user-btn` 140.94×44、`a.topbar__link` 70.64×44、`a.nav__item` 211×44、`#upload` 79.64×44、`th-sort` 34×25.44 |
| **`#ask` 主按钮六档底边余量（定稿轮，全部在首屏内）** | 1440×900 **708.56**（余 191.44）｜1280×800 **708.56**（余 91.44）｜1024×768 **676.56**（余 91.44；主轮越界 −9.66 已修）｜768×1024 **755.66**（余 268.34）｜390×844 **698.08**（余 145.92）｜360×800 **698.08**（余 101.92；主轮越界 −65.36 已修） |
| 手机端全流程（定稿轮自建探针） | 真实跑通：UI 登录 → 上传 `sales_sample.csv`（12 行 × 5 列）→ 建任务 → 390×844 `SUCCESS（6/6）`；图表 `alt` 三条路径（实时/硬刷新/深链接）都非空（`按地区分组汇总销售额，从高到低取前 20 组`）且图片真加载；控制台 `errors=[]`；探针**自己重新播种**，未复用跨进程 workflow id |
| 初始化态（空库，独立服务） | 六档主按钮底边与主轮**逐值相同**（673.88 / 604.98 / 574.52 / 775.34 / 733.75 / 755.19），需滚动量比主轮更小（390→100px、360→165px） |
| **D6 三条诚实标注守护（双装置）** | 源码侧：`index.html` 整文件哈希变过（39212 B → 39258 B），但三条原文的**行号 325 / 395 / 501、片段 sha256_16、整行 sha256_16 全部不变**；渲染侧：7 页 × 3 条 = 21 组命中数与归一化文本 sha256 全一致 → **没有任何 D6 行被改** |
| 未验证（7 条，逐条写"没测"） | 真读屏器（NVDA/VoiceOver/JAWS）、Chromium 之外的浏览器、读屏实际朗读语义与顺序、**zoom 200% 文本放大 reflow**、forced-colors、hover/展开态对比度（计算样式+有效背景色，不做像素采样）、闪烁（2.3.1）；**未引入 axe-core 等第三方引擎做交叉验证**（硬约束禁新增依赖）。审计方明确声明：**「严重项 0」只代表本工具测到的检查项内没有严重项，不等于「全站符合 WCAG 2.1 AA」** |
| 工具自身的假阳性（8 类 + 1 类环境误读，全部留档并修正） | 最典型：采集脚本里的 `scrollIntoView` **改掉了 Chromium 的顺序焦点导航起点** → 登录页被误报 10 条「键盘不可达」；skip link 有 120ms 位移动画，聚焦瞬间读到 `y=-81.59`；把 `left:-9999` 与 `translateX(-100%)` 当成溢出；sr-only 的 `<label class="sr-only">` 被当成文字截断（工具 1.2.1 修）；旧 uvicorn 进程导致的 500 曾被当产品缺陷（重启后 200）。**「审计工具也会说谎」这件事本身写进了报告** |
| **两套读数不可混引（方法学规则）** | 同一现象在本仓库有**三套装置读数**：① 定稿轮 `a11y_audit.py` 现场 `getBoundingClientRect()` = **708.56**；② `docs/ui-review.md` §12.8 的 `collect_pages.py` METRICS 口径 = **687.13**；③ 主轮（同一工具、task-24 之前的代码）= **781.66**。差异来自**装置不同 / 被测版本不同，不是页面在变**。规则：**定稿只采用一套作为数字，其余必须标注口径，不得并列互相换算或混引** |

---

## 12. 最终验收结论（交付物 12）

### 12.1 一句话结论

**任务书的十二项交付物全部落地**；代码在**最终冻结哈希**（§12.6.7 的 7 个前端文件）上通过了
**全量回归 902 passed / 0 failed / 0 error**（§10 记的是代码冻结那一刻的 **889 passed**；
另 **13 条**是 task-28 为缺陷 A（报告首段泄露内部字段名）与缺陷 D（账号脚本自抄密码规则）
新增的守护测试，两个新文件、**4 个被改的测试文件用例数一个没增减** —— 见 §12.7）、
**六档视口 × 12 条流程的浏览器端到端验收（`final-freeze4`：68 PASS + 4 N/A、FAIL 0、BLOCKED 0）**、
**8 页 × 6 断点的无障碍审计（严重项 0、阻断 0）**，以及**独立评审官的七项 100 分制打分**。
核心四页（登录 86 / 工作台 85 / 数据集 89 / 市场分析 89，报告 87）**全部达到任务书要求的 ≥85 分**；
另两页（历史、设置）在 X-07 收口后的终审复评见 §12.9。

### 12.2 七项评分表（独立评审官 design-research，**未参与任何页面实现**）

评分表满分：品牌识别与整体美感 20 / 布局构图与信息层级 20 / 排版色彩与细节品质 15 /
数据可视化与分析体验 15 / 交互反馈与操作体验 10 / 设计系统一致性 10 / 响应式与可访问性 10。

| 页面 | 品牌 20 | 布局 20 | 排版 15 | 可视化 15 | 交互 10 | 一致性 10 | 响应式 10 | **总分** | 门槛 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|---|
| 登录页 | 18 | 18 | 14 | 8 | 10 | 10 | 8 | **86** | ✅ ≥85 |
| 工作台 | 17 | 17 | 14 | 13 | 7 | 9 | 8 | **85** | ✅ ≥85 |
| 数据集 | 17 | 18 | 15 | 14 | 7 | 9 | 9 | **89** | ✅ ≥85 |
| 市场分析（核心页） | 17 | 18 | 15 | 13 | 8 | 9 | 9 | **89** | ✅ ≥85 |
| 分析报告 | 17 | 17 | 15 | 14 | 7 | 9 | 8 | **87** | ✅ ≥85 |
| 历史任务 | 17 | 16 | 14 | 12 | 7 | 9 | 9 | **84** | ⚠️ 未达 → **85（§12.9.3 终审）** |
| 设置 | 17 | 16 | 14 | 11 | 8 | 9 | 9 | **84** | ⚠️ 未达 → **85（§12.9.3 终审）** |
| **全站均值** | | | | | | | | **86.3** → **86.6（§12.9.3 终审）** | （改造前 **82.0**） |

- **核心四页 86 / 85 / 89 / 87 全部 ≥85，且这四页当前均无 P2 缺陷** —— 任务书「核心页面内部验收目标 ≥85 分」达成。
- **历史任务 84、设置 84 是本节（§12.2 复评轮）的读数**：两页差的都是同一件事 —— X-07
  「开发者口径文案」里的 `index.html:578` 与 `:643-646`。这两条是 **P3 残留**，
  跨过 85 分需要**产品决策**（收进折叠区 / 改写成用户口径），不是实现缺陷。
  → 该决策已在 task-25 执行（15 处收口，§12.6），独立评审官在最终冻结哈希上终审后
  **两页各归还 1 分至 85，全站均值 86.6，七页全部 ≥85**（详见 §12.9.3）。
  **本表保留 84 是历史记录，不是当前结论。**
- 评审官明确声明：改前的 freeze2 分数**原样保留在 §4/§10 不覆盖**，§12 为复评结果；复评只用**它自己的装置**采集，不引用实现者自评。

### 12.3 交付物清点（任务书第十二部分 12 项）

| # | 交付物 | 状态 | 落点 / 证据 |
|--:|---|---|---|
| 1 | 可运行项目 | ✅ 已实现已测试已浏览器验证 | `pytest -q` **902 passed**（§10）；`uvicorn app.main:app` 可起；六个视图 + 登录页路由可达 |
| 2 | 真实登录认证（复用/新建说明） | ✅ 已实现已测试已浏览器验证 | §3；**是完整的服务端认证**（PBKDF2-HMAC-SHA256 哈希、HttpOnly Cookie、限流、会话失效），Lead 独立复验 **36/36 PASS** |
| 3 | `docs/design-research.md` | ✅ | 46 个 URL 全部真实请求过（0 个二手链接），20 条采用 / 14 条不采用 |
| 4 | `docs/design-system.md` | ✅ | **1439 行** / 17 章；`tokens.css` 209 个令牌；对比度与令牌奇偶校验脚本均 PASS |
| 5 | `docs/ui-review.md` | ✅ | **901 行 / 113879 B / sha256 `0f1154caf42cf35f…`** + **§12.9 终审**（独立评审官在最终冻结哈希上的复评）；含预评审 76 分 → 返工 → §12 复评 → X-07 收口 → 终审的完整闭环 |
| 6 | 运行与配置说明 | ✅ | `README.md`（596 行）含认证章节、环境变量、API 表（**22 行**）、已知边界；另有 **`docs/runbook.md`（452 行）** 从零上手手册 + **`docs/runbook-evidence.md`（173 行）** 命令实测证据 + 三个脚本 `scripts/check_env.py`、`scripts/manage_account.py`、`scripts/check_docs.py` |
| 7 | 测试与构建结果 | ✅ | §10；**902 passed / 0 failed / 0 error**；前端 `node --check` exit 0（无构建步骤） |
| 8 | 关键页面截图 | ✅ | §11；**权威轮 `final-freeze4` 共 113 张**（像素 + 字节逐张记录）+ 精选 11 张；另有登录页 27 张、评审 27 张 |
| 9 | 修改文件清单 | ✅ | §10.2；**冻结那一刻的快照 = 30 改 / 20 新增 / 0 删除**（`git` 生成，非人工回忆）。⚠️ 口径：该数字是**冻结时点**的事实；此后又新增了 `docs/runbook.md`、`docs/runbook-evidence.md`、`scripts/check_env.py`、`scripts/manage_account.py` 等交付物，当前 `git status --short` 为 **30 改 / 23 新增 / 0 删除**（task-27 实测）。**两个数字都对，只是时间点不同** |
| 10 | 已知限制与未完成功能 | ✅ | `docs/limitations.md`（41017 B）+ §9；含「不得宣称」护栏清单 |
| 11 | 多智能体实际使用情况 | ⚠️ **部分达成，如实说明** | §2；**8 个角色全部真实派生并留下可核验产物**。但**模型不是任务书建议的差异化配置**：本会话所有队友实际跑在 `deepseek-v4.1-flash` 上（详见 §2 的诚实说明），**没有使用不同厂商/不同能力的模型组合** |
| 12 | 最终验收结论 | ✅ | 本节 |

### 12.4 验收三态（任务书要求区分「已实现 / 已测试 / 已浏览器验证 / 尚未验证 / 暂未实现」）

**已实现 + 已测试 + 已浏览器验证**（三层证据齐全）：
真实认证与受保护路由、登录页与首次部署态、工作台、数据集页（含真实列表/排序/分页/画像）、
市场分析工作区（真实六阶段状态）、分析结果页与图表、报告阅读页、历史任务与归档、
设置页、多账号资源归属隔离（越权一律 404）、响应式六档、键盘可达与焦点环、reduced-motion。

**已实现 + 已测试，但浏览器验证不完整**：
- `/api/auth/me` 的 **5xx 注入分支**（验收方列为未验证项）；
- **bootstrap 首次部署态**在权威轮未逐条走（账号库非空）；另由登录页专项脚本覆盖（118/118）与 a11y 审计覆盖；
- 窄屏下的**报告长文滚动阅读 / 历史翻页 / 设置表单**未逐步验（只在宽屏档验过）。

**尚未验证（明确写"没测"，不写成通过）**：
真读屏器实际朗读（NVDA/VoiceOver/JAWS）、Chromium 之外的浏览器、**zoom 200% reflow**、
forced-colors、hover/展开态对比度、闪烁（2.3.1）、深色主题（**刻意不做**，理由见 `app.css` 注释）、
图表在"已填问题 + 分析中"态的几何、实例重启后的持久性、报告正文内部时间戳。

**暂未实现（本期不做，已写进 `docs/limitations.md`，且界面上不得出现对应入口）**：
自助注册、忘记密码/邮件重置、取消正在运行的任务、删除数据集、导出 PDF/Excel、
阶段级时间戳与阶段耗时（**永久禁止显示**，见 §8）、数据集持久化（在进程内存，重启即丢）、
多租户工作区、跨进程/多 worker 部署。

### 12.5 本轮的诚实缺口（不掩盖）

1. **多智能体第 11 项部分达成**：8 个角色都真实存在并产出了可核验的中间产物，
   但**所有队友使用的是同一个模型**（`deepseek-v4.1-flash`），没有实现任务书建议的"不同角色用不同模型/厂商"的差异化协作。
   这不影响产物的可核验性（每一份结论都由我独立复现或交叉验证），但**不能宣称做到了模型层面的异构协作**。
2. ~~**1024×768 与 360×800 的 `#ask` 掉出首屏**~~ → **已修复**（task-24）：
   `app/web/app.css:1495-1550` 新增响应式覆盖块（+57 行 / +3675 B），把 `#ask` 收进五档首屏。
   Lead 用**独立装置**复测 10/10 全部在首屏（`ALL_IN_FOLD = True`，exit 0），
   并在 `1024×768` 实测 `676.56 / 655.12`，**比实现方自报的 `707.13 / 743.13` 更严**（故非更宽容的装置）。
   证据：`.tmp/lead_ask/measure_ask_v2.py` + `ask_geometry_task24_v2.json`；补测 18 档断点
   `.tmp/lead_ask/probe_breakpoints.py` → `OVERFLOW_COUNT=0`、`ALL_IN_FOLD=True`。
   **⚠️ 但机制归因不完整（新发现，如实记录）**：断点生效时内边距只减 4px，`#ask` 底边却**下移 32px（1081→1080）
   与 80px（480→479）**，且 `scrollHeight` 同时变小 —— 断点处存在**方向相反、量级更大**的重排
   （另有 ≥2 处跳变 `900→768 +79.10`、`600→481 +23.10` 是**本项目既有响应式**，非本轮引入）。
   这不影响"已收进首屏"的结论，但**后续任何"分析页已稳定"的说法必须先解释这 32px**。
3. ~~**两个页面未达 85 分**（历史 84 / 设置 84）~~ → **X-07 已收口**（task-25，五轮迭代）：
   详见 §12.6。X-07 改掉的 15 处开发者口径文案 + 装置四轮扩展，均已成文并冻结。
   **独立评审官终审：两页各归还 1 分至 85，全站均值 86.6，七页全部 ≥85（§12.9.3）。**
4. **`app.css:769` 仍有一处 `font-size: 26px` 字面量**（图标字形）：我先前误报为"0 处"，已更正。
5. **favicon 只解决了一半**：隐式请求消失（DOM 已声明 `data:,`），但显式 `GET /favicon.ico` 仍 404；
   console 里那 4 条错误（2×404 + 2×401）来源**未完全定位**。
6. **`docs/a11y-audit.md` 的跨轮条数不可比**：审计方主动声明工具版本变化，我采纳并写入 §11.1。
7. **评审官的 `#ask` 390 档读数与 a11y 的不一致**：我用第三套装置判定 design-research 正确（790.27），
   并据此更正了我自己先前采信的错误表述（"只剩 1.73px"）。
8. **`app/web/app.js:1247` 保留一个不可达的 404 兜底分支**（防御性代码）：
   `app/api/routes_datasets.py:205` 已有 `@router.get("")`，且 `api()` 对 404 返回 `NOT_FOUND`
   而非该分支依赖的 `BAD_RESPONSE`，两个条件均不成立 → **该分支当前不可达**。
   本轮**只改文案、不删逻辑**（冻结前不扩大改动面）；清理死代码列入后续。
9. ~~**`docs/ui-review.md` 与 `docs/a11y-audit.md` 的部分口径 pin 在旧 `app.css` 哈希上**~~
   → **已收口**：`a11y-audit.md` 已重跑合格轮并以新哈希定稿；
   `ui-review.md` 已按"task-24 之后、task-25 之前"重新锚定并加明确的口径互斥声明（两份报告不可混引）。
10. **`frontend-engineer` 在最终轮自行回退了两处措辞**（`app.js` 的「正在读取归档库…」「归档库里还没有记录。」）——
    原因是 `probe_honesty` 的既有断言就是这两句。**宁可回退文案也不改探针**，我认可这一取舍；
    这两句本身也满足「用户要据此判断数字可不可信」，保留是对的。
11. **`task-25` 的写入范围含 `app.js`/`index.html`**，任务板状态一度滞后于落盘
    （`app.js` 15:04:27 已落盘，任务仍显示 `in_progress`）。这是**任务板状态与文件的时序差**：
    我按文件 mtime 核实后亲手置为 completed，并让 acceptance 用时间线证据（测量窗口晚于冻结集最后改动 10 分 56 秒）封住"会不会量到旧版本"的疑问。

---

## 12.6 X-07「开发者口径文案」收口（task-25，五轮迭代）与冻结漂移台账

### 12.6.1 起因

独立评审官在复评中给**历史任务 84 分 / 设置页 84 分**，两页差的都是同一件事：**X-07 开发者口径文案**
（`index.html:578` 的 `<code>limit</code>`/`<code>offset</code>`、`:643-646` 的环境变量清单与 `POST /api/settings`）。
两者都是 **P3 残留**，跨过 85 分需要产品决策，**不是实现缺陷**。

### 12.6.2 Lead 定下的常设判定原则（本轮由 `frontend-engineer` 执行、Lead 逐轮复核）

> 一条信息该不该出现在**用户可见文本**里，看它是否满足
> **(a) 用户要据此行动**、或 **(b) 用户要据此判断数字可不可信**。
> 不满足的实现细节 —— **接口路径、字段名、类名、库名、文件路径、环境变量名、技术栈名** —— 一律移出可见文本。
> **硬性例外**：确实是用户会遇到的限制**必须留下**，只是要用用户的话说
> （「重启后列表会空」✅；「数据集存在进程内存、`dataset_id` 会失效」❌）。

两条推论（本轮实际用到）：
1. **可查性必须真实** —— `title` 在触屏/键盘取不到，**不能把路径藏进 `title` 假装可查**；路径直接删，`title` 只放口径说明。
2. **粒度止于用户需要** —— 「服务端没有配置大模型」保留（用户要区分"模型没配"与"功能坏了"），
   **不再**抽象成"智能分析未开启"。

### 12.6.3 三轮里真实改掉的 **15 处**残留（位置｜载体｜改前 → 改后）

| # | 位置 | 载体 | 改前 → 改后 |
|--:|---|---|---|
| 1 | `app/web/index.html:48` | 会话门文案（`role="status"`） | `正在向服务端询问当前会话（GET /api/auth/me）。` → `正在向服务端确认你的会话状态…` |
| 2 | `app/web/index.html:118` | `title`（悬停/读屏） | `打开 FastAPI 自带的接口文档` → `打开自动生成的接口文档` |
| 3 | `app/web/index.html:528-531` | 报告目录 note | 删 `<code>HistoryItem</code>`/`<code>dataset_id</code>`/「归档接口」 |
| 4 | `app/web/app.js:973` | `title`（大模型徽章） | `服务端环境变量里配了 LLM_API_KEY，…` → `服务端已配置大模型，…` |
| 5 | `app/web/app.js:977` | `title`（同徽章另一态） | `没有配 LLM_API_KEY：…` → `服务端没有配置大模型：…` |
| 6 | `app/web/app.js:1247` | 404 兜底文案（**不可达分支**） | `服务端还没有列表接口（GET /api/datasets 返回 404）。…` → `服务端暂时没有返回数据集列表。上传、画像、分析都不受影响。` |
| 7 | `app/web/app.js:1327` | 按钮 `title` | `再问一次 GET /api/datasets` → `重新向服务端请求数据集列表` |
| 8 | `app/web/app.js:2136` | 可见正文（工作台 KPI 备注） | `来自画像接口的 row_count` → `由服务端统计的行数` |
| 9 | `app/web/app.js:2137` | 可见正文（同卡片） | `来自画像接口的 column_count` → `由服务端统计的列数` |
| 10 | `app/web/app.js:2171` | 可见正文（同卡片） | `来自画像接口的 memory_bytes` → `由服务端统计的内存占用` |
| 11 | `app/web/app.js:2317` | 可见正文（提交回执） | `已提交，workflow_id ` → `已提交，任务编号 ` |
| 12 | `app/web/app.js:2549` | 可见正文（报告中心空态） | `（服务端 has_report=false）` → `（服务端没有留下报告记录）` |
| 13 | `app/web/app.js:3080` / `:3156` / `:3251` / `:3252` | 可见正文（历史/报告空态） | `服务端没有配置归档库（DB_PATH 为空）…` / `归档库没有启用。` → **统一为** `这台服务没有开启任务归档，所以没有历史记录可读 / 没有报告可列。` |
| 14 | `app/web/login.js:535` / `:708` / `:769` | 登录页可见文本 | 去 `llm_used=0`；`读不到公开健康检查（/api/health）。` → `读不到服务自检结果。`；去 `/api/auth/me`（**保留响应码**，用户报障要引用它） |
| 15 | `app/web/login.html:296` | 登录页可见文本 | `会话是 HttpOnly Cookie，前端读不到原文` → `会话凭证只放在服务端设置的 Cookie 里，页面脚本读不到原文` |

另有 **3 处注释**同步口径（`index.html:35`、`app.js:1243`、`app.js:1244`），**不上屏**，只为"注释与界面口径一致"。

### 12.6.4 五个 D6 守护确认逐字未变（本轮**全程**反复验证）

`index.html:325`（阶段级耗时：后端暂未提供）、`:395`（缺失不等于 0，写成 0 就是在编数据）、
`:501`（阶段级耗时后端暂未提供）—— 三行整行 sha256 与 round-3 基线**逐位一致**，
且子串在全文各出现 **1 次**、round-3 的替换日志**不含**这三行（模式不匹配会直接 FAIL 中止）。

### 12.6.5 装置演进：**「扫描面 = 判定口径」**

这是本轮最重要的方法学结论。同一批文件，**每扩一次载体的覆盖，都抓到新问题**：

| 轮次 | 扫描面 | 抓到 |
|---|---|---|
| round-1/2 | 只扫 `section[data-view]` 的 `innerText`（两页） | 漏 `view=report`、全局顶栏/侧栏、`title`、播报区、登录页 |
| round-3 | 六视图 × 两视口 `innerText` + 顶栏/侧栏框架块 | 仍漏 `title`、`[role=status]`/`[aria-live]`（容器不可见时 `innerText` 为空）、`#auth-gate`、登录页 |
| round-4 | ＋**属性载体**（`title`/`aria-label`/`alt`/`placeholder`）＋**播报区**（`textContent`，隐藏也取）＋**源码层**（去注释后扫 JS 字面量与 HTML 文本/属性，A 桶＝含中文可见文本判红、B 桶＝无中文技术字面量只披露） | **立刻抓到 3 处**：`index.html:48`、`app.js:1247`、`app.js:1327` |
| round-5 | ＋**四个盲区**：①登录页（浏览器层＋源码层）②CSS `content:` ③`document.title` ④iframe/shadow DOM | **又抓到 4 处**（`login.js` 3、`login.html` 1） |

**五条教训（写入本报告作为后续同类工作的常设要求）**：
1. **只覆盖 `innerText` 的探针，宣称"零残留"本身就是不可信的** —— 它漏掉了 `title`、播报区、`<noscript>`、注释内路径。
2. **可见文本 ≠ 源码文本**：源码扫描**必须去注释**，并区分「含中文可见片段」与「`fetch` 常量 / 行尾注释」，否则大量假红。
3. **"本应用无此结构"也要有证据**：iframe/shadow 的关闭 ＝ 源码统计（`attachShadow=0 iframe=0 srcdoc=0`）＋运行时统计（每页 `iframe=0 shadow=0`），两类都为 0 才算关闭盲区。
4. **改口径不改机制**：芯片选择器与「截至 HH:MM · N 条」不动 → 保住 a11y 目标选择器与 `probe_clock` **4/4、差 0.0 分钟**。
5. **最强"无夹带"证明**：把改后文件**按登记表逐处还原成上一轮文本**，`size + sha256` **逐位回到基线** —— 本轮 4 个文件全部一致。这比任何断言都强，请保留为常设手法。

### 12.6.6 Lead 的独立复量（**不依赖实现方的任何探针结论**）

装置：**Lead 自有隔离实例** `http://127.0.0.1:8161`（`.tmp/lead_final/data2` 空库起，全隔离环境变量）。
> **为什么必须这样**：实现方的 8170 在 Lead 复量前**两次消失**（`net::ERR_CONNECTION_REFUSED`），
> 尽管它刚报 `HEALTH=200`。**队友进程随时可能被回收，关键验收必须用 Lead 自己的实例**，否则复量不可复现。

| 检查 | Lead 实测结果 |
|---|---|
| 7 文件哈希 | 亲算 `Get-FileHash`，与实现方自报**逐位一致**（见 §12.6.7） |
| 旧文本消失 | 11 条旧文本中 **10 条 0 命中**；唯一命中 `index.html:124` 的 `GET /api/auth/me` **在 `:31` 起多行 HTML 注释内**，不上屏 → 允许 |
| 新文本存在 | 10 条新文本**全部命中** |
| 浏览器三载体 | **17 个模式 × 7 个页面 × 3 类载体**（`innerText` / 播报区 / 属性）→ **全部 0 命中**；`console_errors = 0` |
| 登录页 | 源码级 `/api/` 命中 10 处，**逐条追查全部落在注释与 `API` 常量映射**（`login.js:30-33`）→ 可见文本**零残留** |
| D6 三条 | 亲读源码**逐字未变** |
| 字节账 | round-3→4：`index.html` −16、`app.js` −40；round-4→5：+62 / +125 / −43 / +31，**逐处之和 = 实测** |

Lead 复量记录：`.tmp/lead_final/LEAD_r5_verification.md`、`.tmp/lead_final/LEAD_residue_finding.md`。

### 12.6.7 最终冻结哈希（Lead 亲算，7 个前端文件）

```
app/web/index.html   39258 B  804843F5AE6AEBBF443B3BA34723D1389024A060BEECF8D9821EA199CC871306
app/web/app.css      59100 B  7980B2FA0FF9B0236E61F40D6E8CB9BE18ABF4F504EA17B94A6EF6EC5BC43DDE
app/web/app.js      133331 B  124E01F699B616B634F961E9D69ED34A583D922AF327A4445C7089222A2D3930
app/web/tokens.css   21620 B  4DE3E276F42731D9578158958680EEA072D5E62C2B0D04947891BC0041772149
app/web/login.html   20753 B  FDFB5A7F8F04D39ED44FCCF46008E47B85FD9E139368601B6ACD6FE8E46BC0FA
app/web/login.css    34002 B  F5A2F2C3BCAC5799BB07825CB7959F897DA01A2C829A0BE543EE5769DA79E0BD
app/web/login.js     47791 B  BB1C1E376E9EF0306050944B8C3B5A691E8C18A34A5AE6CED516913F185FC2E6
```

### 12.6.8 冻结漂移台账（Lead 实测，`Get-FileHash` 逐字节）

基准 ＝ `.tmp/freeze/web_hashes_final.txt`（与 acceptance `final-freeze3/VERSIONS.md` 一致）。

| 文件 | 冻结基准 | 当前 | 漂移 |
|---|---|---|---|
| `app/web/app.css` | 55425 B / `8730C1D495686269…` | **59100 B / `7980B2FA0FF9B023…`** | **✅ 有（task-24，唯一一个）** |
| `app/web/index.html` | 38946 B / `ACA8A8B4129931DA…` | 39258 B / `804843F5…` | ✅ 有（task-25） |
| `app/web/app.js` | 131353 B / `91DCF193ADECF192…` | 133331 B / `124E01F6…` | ✅ 有（task-25） |
| `app/web/tokens.css` | 21620 B / `4DE3E276F42731D9…` | 同 | ❌ 未动 |
| `app/web/login.html` | 20722 B / `17ADBA68CC0217A5…` | **20753 B / `FDFB5A7F…`** | ✅ 有（task-25） |
| `app/web/login.css` | 34002 B / `F5A2F2C3BCAC5799…` | 同 | ❌ 未动 |
| `app/web/login.js` | 47834 B / `2BE41FAA79E4295F…` | **47791 B / `BB1C1E37…`** | ✅ 有（task-25） |

**后端 13/13 MATCH，漂移 = 0**：`app/main.py`、`app/config.py`、
`app/api/{routes_auth,routes_analysis,routes_datasets,routes_workflows,routes_health,ownership}.py`、
`app/core/{auth,archive}.py`、`app/data/{history,users}.py`、`app/schemas_workflow.py`
—— 逐条对 `final-freeze3/VERSIONS.md` 核验。

**受漂移影响、已派作者修正口径的三份下游产物**（三份彼此内部一致，都 pin 了旧 `app.css`）：

| 产物 | 声明的口径 | 处置 |
|---|---|---|
| `.tmp/acceptance/runs/final-freeze3/VERSIONS.md` | `app.css 8730C1D4…` | **已解决**：freeze3 整轮已被 `final-freeze4` 取代（`INDEX.md` 标 ⛔），权威轮 `final-freeze4/VERSIONS.md` 重出了 20 个文件的 SHA256，pin 到 task-24/25 之后的版本 |
| `docs/a11y-audit.md:3`、`:26` | 自称「最终冻结版…mtime 13:26:01」 | 已派 `a11y-responsive` 修口径并跑合格轮 |
| `docs/ui-review.md:655` | 未 pin 哈希，analysis 89 分评的是旧 css | 已派 `design-research` 复核 |

### 12.6.9 Lead 抓到的第 13 条真实缺陷（fmtClock 时区语义错，**已修**）

Lead **亲自打开待冻结版 `v1440x900-workbench.png` 目视**时发现**同屏时间自相矛盾**：
KPI 计数行显示「截至 **04:36**」，而同屏「最近上传 **11:56:18**」「最近一次成功 **12:36:00**」—— **差 8 小时**。

- **机制**（Lead 读源码定位）：`app/web/app.js` 的 `fmtClock(iso)` 用正则 `/T(\d{2}):(\d{2})/`
  从**客户端自己的** `new Date().toISOString()`（**UTC**）里截时分；调用处 `:1819`（`#asset-total-note`）
  与 `:3044`（`#asset-task-note`）传的正是 UTC 串，而同屏 `#asset-last-success` 走 `toLocaleString` 是**本地时间**。
  `fmtClock` 全文件只出现 3 次（1819 调用 / 2082 定义 / 3043 调用）。
- **修法**：删正则捷径 → `var date = new Date(text); if (isNaN(date.getTime())) return "—";` + 本地 `getHours()/getMinutes()`。
- **红→绿证据**：`.tmp/frontend3/probe_clock.py` 修复前 delta **−480.0 分钟**（2/4 passed、exit 1）
  → 修复后 delta **0.0**（4/4、exit 0）。
- **最小性证明**：`.tmp/frontend3/prove_minimal_diff.py` 把新函数块反向替换回旧块
  → `131182 B / 1c3c5ae0b4e1e7fa…`（与冻结基线**逐字节相同**）。
- **权威轮独立确认**：acceptance 在 `final-freeze3-1440x900/shots/03-workbench.png` 里
  实测同屏两处都是本地「截至 **13:36**」，Lead 已**亲自目视复核**该截图。
  （该截图属于 `freeze3` 轮，按 `INDEX.md` 现标 ⛔；它仍是有效的**缺陷修复证据**，
  因为 fmtClock 代码路径此后未再改动，只是不再是"权威验收轮"。）

> **方法学价值**：这条缺陷是**任何自动化探针都没抓到**的 ——
> 所有脚本都只断言"元素存在、数字是数字"，没人断言"同屏两个时间要自洽"。
> **它是靠人眼看截图发现的。** 这条已写入 §12.6.5 的教训。

---

## 12.7 task-28：冻结之后又查出并修掉的四个缺陷（含一个**后端**上屏文案）

### 12.7.1 为什么冻结之后还改

代码冻结后，Lead 让 `backend-tests` 做了一次**只读文档一致性终检**（task-27），
又让独立评审官做了终审（task-26）。这两次检查各自查出了**冻结清单之外**的问题：

- **task-27** 逐条核 `requirements.txt` 时发现：`scripts/check_env.py` 把 `argon2`
  当**必需依赖**在查。而本项目的密码哈希真实实现是**标准库 `hashlib.pbkdf2_hmac`**
  （`app/core/auth.py:103 hash_password`，默认 **240000 轮 PBKDF2-SHA256**），
  `requirements.txt` 与 `requirements.lock.txt` **都没有 argon2**。
  本机能过，是因为 venv 带 `--system-site-packages`、argon2 装在系统级 Python 里。
  → **换一台干净机器，这一行会从 `[ok]` 变成 `[FAIL]`，并给出"认证不可用"的错误结论。**
- **task-26** 在真实浏览器里发现：`app/agents/validation.py` 的报告首段
  **会直接把内部字段名打给用户看**。

两处都不在 7 个前端文件里，**前端冻结哈希不受影响**（Lead 复算 7/7 MATCH，
且 7 个文件 mtime 全部早于 16:40 开工时间）。

### 12.7.2 缺陷 A：报告首段把 `llm_used` 字段名给用户看（**改前 / 改后**）

| 项 | 内容 |
|---|---|
| 位置 | `app/agents/validation.py:57`（改后为 `:63`） |
| 改前逐字 | `本次未使用大模型（llm_used=false），以下结论为工具结果的直接汇总` |
| 改后逐字 | `本次未使用大模型，以下结论为工具结果的直接汇总` |
| 为什么是缺陷 | 这句话**直接上屏**（报告页与历史详情抽屉都渲染 `FinalReport.summary`）。`llm_used` 是 API/DB 字段名，用户拿着它**既不能行动、也不能据此判断数字可不可信** —— 判断依据是"这次到底有没有用模型"这半句话本身 |
| 为什么之前没人发现 | X-07 那 15 处清单**全是前端文件**（`index.html`/`app.js`/`login.*`），这处在**后端**；`probe31` 那轮扫到的报告页是空态（`innerText` 仅 631 字符，正文未进 DOM） |
| 实证 | `.tmp/review/probe36_histllm.py` → `probe36_histllm__finx.{json,txt}` + 截图 `shots/histllm__finx.png`；深链 `/app#/history/ef226c6f…` → `app.js:819 applyDeepLink` → `openHistoryDetail` → `app.js:3347 renderReportInto` → `app.js:2831-2832` 渲染 `p.summary`，首段逐字上屏 |
| 同文件不一致 | 同在 `validation.py` 的 `LLM_PLANNED_NOTICE`（`:62`）**一直是用户视角措辞、没有字段名** → 这曾经是同一文件里两套写法 |

### 12.7.3 ⭐ 影响面：一句话被测试**用字面量钉在了 4 个文件、12 处**

这是本任务最有价值的发现。改这句话之前，Lead 自己 grep 了全仓，发现 `tests/` 里
有 **3 个文件各抄了一份常量副本**，外加 **1 处内联样本数据**，合计 4 个文件：

| 文件 | 行 | 性质 |
|---|---|---|
| `tests/test_orchestrator.py` | `:66` | 常量副本 |
| `tests/test_report_llm.py` | `:69` | 常量副本 |
| `tests/test_validation.py` | `:65` | 常量副本 |
| `tests/test_status_degradation.py` | `:74` | **内联样本数据**（`FinalReport(summary=…)` 夹具参数） |

外加 **8 处引用副本的断言**：`test_orchestrator.py:199`/`:779`、`test_report_llm.py:304`/`:511`、
`test_validation.py:226`/`:246`/`:261`，以及 **`test_validation.py:292` 的等式**
（`assert validation_module.TEMPLATE_NOTICE == TEMPLATE_NOTICE`）。

**这些字面量是「有意为之的防篡改钉子」，不是疏忽。** 作者注释原文
（`tests/test_orchestrator.py:188-192`）：

> 这句话为什么**写成字面量**而不是 `startswith(TEMPLATE_NOTICE)`：拿常量和它自己比，
> 是一句永远为真的断言 —— 有人把常量改成别的措辞（比如删掉"未使用大模型"这半句），
> 断言两边一起变，测试照样绿。这条测试要钉的恰恰是"用户看到的文字必须长这样"，
> 所以字必须写死在测试里。**这是变异测试抓出来的：改坏 `TEMPLATE_NOTICE` 时它当时没有变红。**

**因此 Lead 的处置是「保留钉子、只换钉子上的字」**，并明令禁止三种偷懒改法：
改成 `startswith(validation.TEMPLATE_NOTICE)`（那正是作者要防的）、删掉 `:292` 的等式、
以及删用例/加 skip。改完后 `:292` 的价值反而**凸显**：**谁只改源码不改测试副本，这条立刻红。**

> **方法学结论（新维度）**：改一条**用户可见文案**，不能只搜产品代码 ——
> 必须同时查"它在测试里被钉了几处"。范畴**不能按文件类型划定**，
> 要按「**哪些字符串会进用户可见渲染路径**」划定。

### 12.7.4 缺陷 B / C / D

| # | 缺陷 | 位置 | 改前 | 改后 |
|---|---|---|---|---|
| B | `argon2` 被当**必需**依赖 | `scripts/check_env.py` 依赖表 | `[ok] argon2 —— 密码哈希（认证必需）` | **整条删掉**（不是降级 —— 本项目根本不依赖它）；并在注释里写明"哪天真要改用 argon2，必须**同时**进 `requirements.txt` 与 `requirements.lock.txt`" |
| C | 输出流遇到不可编码字符会崩 | `scripts/check_env.py:43` | 警告文本含 GBK 编码不了的字符时抛 `UnicodeEncodeError`，**本该给的警告变成 traceback** | `scripts/check_env.py:57-86` 的 `_make_stdout_unencodable_safe()` 把两个流切到 `errors="replace"`，并用 `getattr(stream,"reconfigure",None)` 挡住"stdout 被重定向时没有 `reconfigure`"的第二条隐患 |
| D | 账号脚本**自己抄了一份**密码规则（Lead 裁定叫"危险的一致"） | `scripts/manage_account.py:67` | `min_len = 8` 硬编码；同处 `:65` 的 `from app.config import get_settings` 在 `:71` 被 `del` 掉（死代码）。数值恰好等于应用层的 `PASSWORD_MIN_LENGTH`（`app/schemas_auth.py:25`）**纯属巧合**；且那 8 **只量长度** → `"12345678"`（纯数字、在常见弱密码表里）以前能从脚本建出来，却过不了应用层的 `validate_password_strength` | 删掉 `min_len` 与死码，改成**函数里** `from app.schemas_auth import validate_password_strength` 后直接调用；判据**只会跟着应用层变** |

**缺陷 C 的一条方法学教训**：任务 27 的对照实验证明，这个崩溃
**不能**由「TEMP 不可写 + GBK 控制台」单独复现（TEMP 缺失时它正确降级为警告并跑完全程，
exit 0）。真正的崩溃点是那行 `print()`。→ **别急着把"看起来相关的条件"当成根因。**

### 12.7.5 验证（全部为 Lead 亲测或亲算，不采信自述）

- **全量回归（Lead 亲自执行，项目根、`-p no:randomly`）**：
  ```
  ======================= 902 passed in 109.49s (0:01:49) =======================
  ```
  **0 failed / 0 error，退出码 0**。`889 → 902` 的 **13 条全部来自两个新文件**
  （`test_report_notice_has_no_internal_field_names.py` 6 条 +
  `test_manage_account_password_rule_source.py` 7 条）；
  **4 个被改的测试文件用例数一个没增减**（逐文件 diff 为 0）。
- **前端冻结集未被扰动**：Lead 复算 7/7 **MATCH**，且 7 个文件的 mtime
  （`app.css` 14:10:15 / `tokens.css` 11:14:51 / `login.css` 13:15:52 / `login.js` 15:06:24 /
  `login.html` 15:06:26 / `index.html` 14:58:01 / `app.js` 15:04:27）**全部早于 task-28 开工**。
- **旧文案已从所有产品代码与测试里清零**：Lead 自己按 UTF-8 逐字节扫，
  `本次未使用大模型（llm_used=false）…` 在 `app/**`、`tests/**`、`scripts/**` 里
  **只剩守护测试 docstring 里那句"改前长这样"**（这是应该保留的历史记录）。
- **新文案落点 7 处**：`app/agents/validation.py:63` + 4 个测试文件 + `docs/ARCHITECTURE.md:915` + `docs/ui-review.md:852`。
- **守护测试有真牙**：`test_manage_account_password_rule_source.py` 分三层
  （AST 结构级"不许出现 `min_len = <值>`"、行为级"把应用层那把尺子换掉，脚本判据必须跟着变"、
  一致级"应用层拒的密码脚本也必须拒"），且**每条都有反向用例**（"永远拒绝"的实现过不了）。
  红→绿证据：把修复版换回原样 → **5 failed / 2 passed / exit=1**，
  红灯原文 `AssertionError: 替身规则拒绝了 abc12345，脚本却放过了 —— 说明它用的是自己抄的规则`。

### 12.7.6 连带修掉的**文档**引用（不改产品代码）

**同一类错误在本轮出现了两次，形态相同：改了某个事实，却漏掉了"逐字引用这个事实的地方"。**

**（一）`argon2` —— 写进了用户的**操作说明书**（最严重的一处）**

缺陷 B 修掉的是 `scripts/check_env.py` 里的假依赖，但**同一个假事实在 `docs/runbook.md`
里被写成了用户会照着做的指令**，共 4 处：

| 位置 | 原文问题 |
|---|---|
| `docs/runbook.md:91` | 自查命令 `pip list | Select-String '^(…\|argon2\|…)'` 里含 argon2 |
| `docs/runbook.md:97` | **预期输出列表第一行**就是 `argon2-cffi==25.x` |
| `docs/runbook.md:115` | 依赖表 `\| argon2-cffi \| 密码哈希（**绝不明文**） \| 认证不可用 \|` |
| `docs/runbook.md:349` | `本项目的认证是**真实的服务端认证**（argon2 哈希、…）` |

**为什么它比脚本里的那条更严重**：写在脚本里，它只是**自己报错**（打一条 `[FAIL]`）；
写进 runbook，它变成**用户会照着做的指令** —— 干净机器上执行 §3 时，
预期输出列表第一行不会出现 → 对照依赖表看到「不装会怎样：**认证不可用**」→
得出**「我的认证装不上」**的错误结论 → **去装一个本项目根本不需要的包**。
同一句假事实，写在代码注释里是噪音，写进 runbook 就变成会误导操作的指令。

**发现路径（可复用）**：修完脚本后，Lead **全仓 grep `argon`（不限定路径）** ——
一条命令、一秒，命中 `docs/runbook.md` 4 处。
教训：**范围要按「什么事实被改了」划，不能按「我改了什么文件」划。**

**（二）`TEMPLATE_NOTICE` —— 被三份文档逐字引用**

| 文档 | 行 | 性质 | 处置 |
|---|---|---|---|
| `docs/DECISIONS.md` | `:976` | "首段逐字 …" | 改为新文案，**并补一句说明**"原文曾带内部字段名 `（llm_used=false）`，task-28 缺陷 A 已去掉" |
| `docs/DEMO.md` | `:475` | 演示输出粘贴 | 改为新文案 |
| `docs/WORKFLOW.md` | `:367` | "预期报告片段（实测原文）"，**用户照着敲会对不上** | 改为新文案 |

**明确判为"不改"的**：`docs/ui-review.md:852`/`:860`/`:863`（它记的正是"改前长这样"，**正确**）、
`tests/test_report_notice_has_no_internal_field_names.py:5`（守护测试 docstring 引用，**正确**）、
`docs/ARCHITECTURE.md:1659`（写的是 `llm_used=false` 这个**字段值**，不是那句话的引用）、
**`docs/runbook-evidence.md:48` 的 `[ok] argon2 25.1.0`**（它是当时的**真实输出**，
**改掉它就是伪造证据** —— 更正只能放旁边，该文件 `:60-63` 已有更正说明）。
**一律保留**：`llm_used` 作为 **API 字段 / DB 列 / 标识符**在 `app/**` 的约 190 处 ——
**该改的是"把它写进给用户看的句子里"，不是"这个词本身"。**

> **方法学规则**：改一条被文档逐字引用的文案后，**必须把"引用它的地方"当成影响面的一部分**，
> 否则文档会变成"说得对但已经过期"—— 这比明显写错更难被发现。
> `docs/runbook-evidence.md` §6.1/§6.2 记了这件事的完整复盘，并提出下一步该做的机械核对：
> runbook 里每条「命令 + 预期输出」都是**可执行主张**，该跑一遍逐行 diff（本轮 4 处里 2 处正是这种，
> 而且**读起来完全正常**，人读审发现不了）。

---

## 13. 需要用户知道的事故记录（Lead 如实报告）

本轮工作中，队友 `design-research` 在清理自己抓取脚本产生的乱码目录时，
用「今天 10:00 之后创建的 `D:\` 目录」做筛选条件，**把同样落在该时间窗内的 `D:\Loomy` 的内容一并删除**。
Lead 独立核查的事实：`D:\Loomy` 目录仍存在但为空（`ChildCount=0`，创建时间 2026-10-16 10:57:30，
修改时间 11:00:53，无进程占用）；同日 `D:\` 根下 10:00 后新建的目录只有它一个；
`Remove-Item -Recurse` **不进回收站**，因此内容不可恢复。
**推断（时间戳证据，非确证）**：它的创建时间与抓取脚本的工作窗口重合，
极可能本身就是脚本产物而非用户的长期数据，但**这一点无法证实**。
事后 Lead 已向全部队友广播硬规则：禁止在项目目录之外创建/修改/删除/移动任何文件；
禁止 `Remove-Item -Recurse/-Force` 与"按时间/模式筛选后批量删除"；临时产物一律落 `.tmp/`。
此后未再发生同类事件。

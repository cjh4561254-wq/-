# 已知限制与未完成功能（交付物第 10 项）

> 目的：给最终报告提供一份**有代码证据、可被审计**的能力边界清单，用来区分
> 「已实现 / 已测试 / 已浏览器验证 / 尚未验证 / 暂未实现」。它同时是前端的硬约束：
> **后端没有的能力，界面上就不许出现对应入口。**
>
> 验证方式标注（全文统一）：
> - `实测`：本轮真的跑了命令，给了命令与原始输出；
> - `读代码`：给了 `文件:行`，结论是读出来的（未运行）；
> - `本轮未验证`：**没查过**，不等于"不存在"。
>
> 本文件只记录事实，不改任何代码。所有观测都在隔离数据目录（`.tmp/ux/iso/`）里做，
> **没有碰过 `data/db/auth.db` 与 `data/db/app.db`**。

---

## 00 总账（先看这一节）

| 维度 | 数量 |
|---|---|
| 已实现且有测试的能力（§01.1） | **20 条**（C1–C20，每条的验证方式已标注） |
| 明确不存在的接口与能力（§01.2） | **12 条**（N1–N12，每条给了证据 + 对 UI 的约束） |
| 活路由总数 | **20 条**（`/api` 下 17 条）；**`DELETE` / `PUT` / `PATCH` 一个都没有** |
| 对照任务书三态清单（§04） | **21 条** = 已实现 15 / 部分 6 / 完全未实现 0 |
| 「不得宣称」清单（§05） | **12 条**（每条带反面依据） |
| 本轮**未验证**项（§06） | **6 条**（明确标注，不许当成"不存在"） |
| 与既有文档表述不一致处（§07） | **1 处过时**（`docs/ARCHITECTURE.md:1346`）+ **1 处曾经的孤儿行风险**（`app/data/history.py:199-209` 注释警告的情形已随 §03 修复消除） |
| 发现并已修复的真实缺陷 | **1 条**（§03：`POST /api/analysis` 写路径缺认证与归属，2026-10-16 修复） |
| 修复后仍未同步的既有测试 | **1 条**（`tests/test_contracts_d4.py:40-43` 的签名契约，实跑 `1 failed, 4 passed`） |

**本轮最重要的一条**：§03 —— 隔离在写入端漏了一个接口（`POST /api/analysis` 无登录要求、
无归属校验、不写 owner），实测导致"作者本人也看不到自己的工作流（120 轮全 404）"。
**该缺陷已于 2026-10-16 修复**（`app/api/routes_analysis.py:59 / :77 / :95-96`，Lead 独立脚本 21/21 复验）；
§03 保留修复前的原始实测过程，并记录了"修复后复核"与一条**仍未同步的契约测试**
（`tests/test_contracts_d4.py:40-43`）。

---

## 01 后端能力边界表（逐条给证据）

### 1.1 已实现且有测试的能力

| # | 能力 | 接口 / 实现 | 证据 | 验证方式 |
|---|---|---|---|---|
| C1 | 首次部署创建第一个管理员 | `POST /api/auth/bootstrap`（`app/api/routes_auth.py:180` 附近） | 隔离实例上 `POST /api/auth/bootstrap` 返回 200；账号库非空时被拒（`needs_bootstrap=false`） | 实测 |
| C2 | 登录 / 登出 / 查当前身份 | `POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me` | 20 条活路由清单里三条都在；登录返回 `Set-Cookie: mis_session=...; HttpOnly` | 实测 |
| C3 | 会话是 HttpOnly Cookie，前端拿不到 token 原文 | `app/core/auth.py:395 session_cookie_kwargs(*, max_age)` | 实测响应头里 cookie 带 `HttpOnly`；`app/web/login.js` 全程不读 cookie 值 | 实测 + 读代码 |
| C4 | 会话有效期 8 小时（默认，可配） | `app/config.py:144` `auth_session_seconds: int = Field(default=8 * 3600, gt=60, le=30 * 24 * 3600)` | 同左 | 读代码 |
| C5 | 登录失败文案统一（不泄露账号是否存在） | `app/api/routes_auth.py`；`docs/design-directions.md` §01 同款结论 | 实测：不存在的用户名与错密码返回**同一句**「用户名或密码不正确」+ 401 | 实测 |
| C6 | 登录限流（429 + Retry-After） | `app/api/routes_auth.py` 限流分支 | 前端 `app/web/login.js:935` 有对应的倒计时锁定 UI；本轮**未主动打满限流**去验证 429 报文 | 读代码（429 报文本身：本轮未验证） |
| C7 | 改密码会踢掉该账号全部会话 | `app/api/routes_auth.py:308-316`；`app/data/users.py:491 delete_user_sessions(user_id)` | 同左 | 读代码 |
| C8 | 会话列表（含 current 标记） | `GET /api/auth/sessions`；`app/data/users.py:433 list_sessions`、`:526 count_sessions` | 同左 | 读代码 |
| C9 | 数据集上传（CSV / Excel）+ 扩展名白名单 + 大小/行数/列数三道闸 | `app/api/routes_datasets.py:122-200`；`ALLOWED_EXTENSIONS` 判定在 `:143`（**必须在任何写盘动作之前**） | 实测：上传真实样例 `data/samples/sales.csv`（141,028 B）→ 200，`row_count=2168`、`column_count=9`；`docs/ARCHITECTURE.md:1542` 记载行数上限 200000 / 列数上限 200，超限抛 `BAD_REQUEST` | 实测（正常路径）+ 读代码（超限分支） |
| C10 | 数据集画像（行列数、缺失值、重复行、内存占用、预览） | `GET /api/datasets/{dataset_id}/profile`；画像在 `app/data/profiler.py` | 上传返回体里能看到 `row_count/column_count/size_bytes/memory_bytes`；前端 `#dataset-summary` 实测渲染 `sales.csv · 2,168 行 × 9 列 · 137.7 KB` | 实测 |
| C11 | 数据集列表（分页 + 排序） | `GET /api/datasets`（`app/api/routes_datasets.py:205-214`）；`app/data/store.py:207 list_owned`、`:226 count_owned` | 实测：隔离实例上返回 `{"ok":true,"data":{"items":[],"total":0,"limit":50,"offset":0}}`（空是因为该实例没上传过）；无 session 时 401。读代码：`limit: int = Query(50, ge=1, le=200)`、`offset: int = Query(0, ge=0)`、`sort: Literal["uploaded_at","filename","rows"]`（默认 `uploaded_at`）、`order: Literal["asc","desc"]`（默认 `desc`） | 实测 + 读代码 |
| C12 | 分析工作流：提问 → 后台执行 → 轮询状态 | `POST /api/analysis`（202）、`GET /api/workflows/{workflow_id}` | 实测：提交返回 202 且只有 4 个字段（`workflow_id/dataset_id/status=PENDING/created_at`）；轮询能读到 `status/step/step_index/total_steps/tasks/charts/has_report` | 实测 |
| C13 | 六阶段真实状态（不是前端计时器伪造） | `app/agents/orchestrator.py:48 _STEPS`；`app/schemas_workflow.py:90-99 WorkflowStep`；前端 `app/web/app.js:60-62 STEP_ORDER` | task-18 核查结论：**13 个渲染元素 / 13 个有真实字段支撑 / 0 个伪造**；`app/web/app.js` 里 `setInterval` 只有一处（`:1951` 轮询）、`setTimeout` 只有一处（`:620` 会话门超时） | 实测 + 读代码 |
| C14 | 失败时进度不拉满（`step_index` 不动） | `app/agents/orchestrator.py:451-458` `_fail()` 注释 | 同左 | 读代码 |
| C15 | 图表：matplotlib 服务端渲染 PNG，尺寸 960×576、dpi 120 | `app/agents/charts.py`（`_DPI = 120`、`_FIGURE_SIZE = (8.0, 4.8)`） | task-7 的 before/after 画廊各 14 张全部实测 960×576；`tests/test_charts_coverage.py:47 EXPECTED_DPI = 120` 写死 | 实测 |
| C16 | 报告：结构化 `FinalReport`（title / summary / sections[cites,table] / data_quality_notes / citations / llm_used） | `GET /api/workflows/{workflow_id}/report`；`app/schemas_workflow.py` | 真实归档样例 `40ac2708365d4b4ba5305ed88e7057cd` 的 `report_text` 就是这份 JSON | 实测 |
| C17 | 校验阶段核对报告里的数字能追回工具调用 | `app/agents/validation.py:701` 起（`_check_report_numbers` 一类的实现）、`:894` 返回 `(核对过的数字个数, 找不到出处的原始写法, 问题列表)` | 真实报告里出现「数字已逐条核对（共核对 20 个数字）」；`app/schemas_workflow.py:175` 注释：「校验 Agent 的产出。**它是"报告里的数字能核对"这句话的实现**」 | 读代码 + 实测（报告文本） |
| C18 | 历史归档：终态后 UPSERT 进 SQLite，重启后仍在 | `app/core/archive.py:81 archive_if_terminal`；`app/data/history.py:316 archive` | 真实库 `data/db/app.db` 的 `workflow_history` 共 **476 条**（历史接口实测 `total`）；`docs/ARCHITECTURE.md:1316` 记「已做到」 | 实测 |
| C19 | 历史列表 / 详情（只读 SQLite，不读内存） | `GET /api/workflows/history`、`GET /api/workflows/history/{workflow_id}`；`app/api/routes_workflows.py:240-241` | 实测：`limit: int = Query(20, ge=1, le=100)`、`offset: int = Query(0, ge=0)`；隔离实例空库返回 `total: 0` | 实测 |
| C20 | 前端没有画出任何后端不存在的按钮 | `app/web/**` | grep `导出|删除|取消|重命名|注册|忘记密码|export|Delete|cancel` → **零命中** | 实测 |

### 1.2 明确不存在的接口与能力

> 「不存在」的判据有两种，每条都写清用的是哪一种：
> ① **实测**：对隔离实例发请求拿到 404 / 405；
> ② **读代码**：活路由清单里没有该路由，或代码里没有对应实现。
>
> 活路由清单（实测，导入 `app.main` 遍历 `app.routes`）：**共 20 条**，其中
> `/api` 下 17 条。**`DELETE` / `PUT` / `PATCH` 一个都没有**（只有 GET 与 POST）。

| # | 不存在的能力 | 精确表述 | 证据 | 对 UI 的约束 |
|---|---|---|---|---|
| N1 | 取消正在运行的分析 | 没有任何接口能中止一个已在后台线程里跑的工作流 | 读代码：活路由清单无 `POST /api/workflows/{id}/cancel`；`app/core/registry.py` 只有 `create/get/touch` 一类方法，没有 cancel；`app/core/background.py` 把 `run_workflow` 丢进 Starlette 线程池后**不持有可取消句柄**。实测：`POST /api/workflows/{id}/cancel` → 404 | **界面上不得出现「取消分析」按钮**。任务书 7.3 写的是"仅当后端确实支持安全取消时开放"——本条即"不支持" |
| N2 | 删除数据集 | 没有删除接口，也没有删除后清理磁盘文件的路径 | 读代码：活路由清单无 `DELETE /api/datasets/{id}`；`app/data/store.py` 无 `delete` 方法。实测：`DELETE /api/datasets/{id}` → 404 | **不得出现「删除数据集」按钮**。任务书 7.2「删除操作只有在后端支持并完成权限检查时才开放」——本条即"不支持" |
| N3 | 多账号自助注册 | 只有 `POST /api/auth/bootstrap`（**仅当账号库为空**时允许，创建第一个管理员）；没有开放注册接口 | 读代码：活路由清单只有 bootstrap/login/logout/me/password/sessions 六条认证路由 | 登录页**不得出现「注册」入口**；只有账号库为空时才出现「创建第一个管理员」 |
| N4 | 忘记密码 / 邮件重置 | 没有任何"凭邮箱重置"的路径 | 读代码：认证六路由里没有 reset；`app/data/users.py` 里没有邮箱字段相关实现 | **不得出现「忘记密码」链接**。改密码必须已登录且提供 `current_password` |
| N5 | 导出 PDF / Excel / CSV / 图片（除图表 PNG 外） | 报告的导出能力完全不存在 | 读代码：活路由清单无 export 路由；前端 grep `导出|export` **零命中**（所以现有界面也没违约）。唯一的"下载"是图表 PNG：`GET /api/workflows/{workflow_id}/charts/{filename:path}` | **不得出现任何导出按钮**（任务书 7.6 原文："导出功能只有在实际实现时才能展示。不得留下无效的 PDF、Excel 或其他导出按钮"） |
| N6 | 修改数据集元信息（改名 / 改备注 / 改归属） | 归属在登记那一刻定死、之后不变 | 读代码：`app/api/routes_datasets.py:133-135` 注释明写「归属在登记的那一刻就定下来、之后不再改变……所以不需要任何后续修改归属的接口」；无 `PATCH/PUT` 路由 | **不得出现「重命名 / 编辑数据集」按钮** |
| N7 | 阶段级时间戳与阶段耗时 | 每条阶段**没有** `started_at`/`finished_at`，因此**算不出每个阶段花了多久** | 读代码 + 实测：`WorkflowState` 只有工作流级的 `created_at` / `started_at` / `finished_at` / `heartbeat_at`（`app/schemas_workflow.py:264-269`），**没有** per-step 时间字段；`WorkflowStatusData`（`:320-372`）也不暴露它们。task-18 的实测：0.2–0.4 秒跑完一次分析，4 线程 × 25ms 并发轮询也只能抓到 `profiling` 与 `charting` 两个 step，`planning/execution/reporting/validation` 合计不足 94ms | **界面上不得显示"每阶段耗时 xx 秒"**（D6 已定案：阶段耗时禁止显示）。`#elapsed` 只能显示工作流级墙钟差 |
| N8 | 细粒度执行日志（每个 Agent 的输入输出日志流） | 没有日志接口；只有 `TaskBrief` 四个字段（`task_id/description/status/error`） | 读代码：`app/schemas_workflow.py:375-390`，注释明写**不给 `params`**（那是排查时才看的东西）；无日志路由 | 任务书 7.4「如果后端没有提供详细日志，不得编造执行步骤」——**不得虚构"Agent 思考过程"面板** |
| N9 | 跨进程 / 多 worker 支持 | 数据集与工作流状态都是进程内 `dict`，`--workers > 1` 会各存一份 | 读代码 + 文档：`app/data/store.py:30-34` 注释「多进程部署（`uvicorn --workers 2` 以上）会各存一份，进程重启就全丢」；`docs/ARCHITECTURE.md:1252`「启动命令固定为单进程（不加 `--workers`）」、`:1317`「仍然只能单进程」。**本轮未实跑 `--workers 2` 复现 404** | 部署命令**必须**写成单进程；不得把它描述成可水平扩展的服务 |
| N10 | 数据集元信息的持久化（重启后仍在） | 数据集**只**在进程内存；重启后 `GET /api/datasets/{id}` 必 404（磁盘上的原文件还在，但没有元信息就找不到它） | 读代码：`app/data/store.py:30-34`；`docs/ARCHITECTURE.md:1215`「`_frames: dict[str, pd.DataFrame]` + `_summaries: dict[str, DatasetSummary]` 也是进程内字典」。**实测旁证**：隔离实例重启后旧 `dataset_id` 全部 404 | **不得宣称"数据集已持久化"**。SQLite 只存**历史归档**（工作流级），不存数据集 |
| N11 | 工作流的断点续跑 / 重启后恢复 | 重启后正在跑的工作流**不会恢复、也不会被标成失败**，它只是从内存里消失 | 读代码 + 文档：`docs/ARCHITECTURE.md:1315`「SQLite 存的是"历史归档"，不是"断点续跑"」 | **不得把"持久化"说成"断点续跑"**；前端轮询到 404 只能提示"服务已重启，请重新提交" |
| N12 | ~~历史归档表按 owner 的**写入**归属~~ **（曾经缺失，已修复）** | 见 §03：这是本清单里唯一一条"设计已写、代码漏了"的限制，曾经也是主链路的断点；**2026-10-16 已修**（`app/api/routes_analysis.py:59 / :77 / :95-96`），§03 保留修复前的原始实测过程 | 实测 + 读代码 | 见 §03 与 §05 |

### 1.3 能力的验证深度分布（任务书第 12 项要求的五态）

| 深度 | 条目 |
|---|---|
| 已实现 + 已测试（pytest 覆盖） | C1–C20 的大部分；图表契约由 `tests/test_charts.py` / `test_charts_coverage.py` 钉住；认证由 `tests/` 下认证用例覆盖（**具体用例名本轮未逐条核对**：本轮未验证） |
| 已实现 + 已浏览器验证（Playwright 真机） | 登录 → 工作台 → 上传 → 提问 → 轮询 → 图表 → 报告这条链路（task-18 的 8 张实拍截图，1440×900）；`#dataset-summary` / `#steps` / `#workflow-state` / `#elapsed` 四个元素的真实文本 |
| 已实现但**只在隔离实例**验证 | `GET /api/datasets`（8143/8152/8154/8163 返回 401 而非 404，说明路由已生效；老实例 8128/8141 仍是 404 —— 那是旧进程） |
| 尚未验证 | `--workers 2` 下的 404 复现（N9 的实测证据）；登录限流 429 的真实报文（C6）；Excel（`.xlsx`）上传路径（本轮只传过 `.csv`） |
| 暂未实现 | N1–N11 全部（N12 曾经缺失，**已于 2026-10-16 修复**，见 §03） |

---

## 02 单用户 vs 多用户的真实边界

任务书要求："若暂时单用户，必须明确记录限制，**不得宣称多租户隔离**"。

### 2.1 owner 隔离实际做到了哪一步

| 资源 | 数据层 | 接口层 | 状态 |
|---|---|---|---|
| 数据集 | `Dataset.owner_user_id`（`app/schemas_dataset.py`）；登记时写入（`app/api/routes_datasets.py:192-200`）；`app/data/store.py:207 list_owned(owner_user_id)` 精确匹配 | 4 个数据集接口**全部**有 `Depends(require_user)`（`app/api/routes_datasets.py:123 / 207 / 265 / 279`） | ✅ 读路径已隔离 |
| 工作流状态 | `WorkflowState.owner_user_id`（`app/schemas_workflow.py:246-248`） | `GET /api/workflows/{id}` 经 `require_owned_workflow`（`app/api/ownership.py:76-84`） | ⚠️ 读路径已隔离，但**写入端从不设置 owner**（见 §03） |
| 历史归档 | `workflow_history` 表新增 `owner_user_id` / `owner_username` 两列（`app/data/history.py:115-136`）；`get_for_user()` / `list_recent()` / `count()` 的 `owner_user_id` 是**关键字必填**，过滤条件写在 SQL 里（`:348-402`） | `require_owned_history`（`app/api/ownership.py:87-112`） | ⚠️ 同上：归档值来自 `state.owner_user_id`（`app/core/archive.py:76-77`），而它一直是 `None` |

### 2.2 隔离的**设计**是正确且值得肯定的（实测逐字验证）

用两个账号（alice / bob）在隔离实例上实测，bob 访问 alice 的资源：

| 探测 | 结果 |
|---|---|
| `GET /api/datasets/{alice 的 id}` | 404 `NOT_FOUND`「数据集不存在」 |
| `GET /api/datasets/{alice 的 id}/profile` | 404 同上 |
| `GET /api/workflows/{alice 的 id}` | 404 `NOT_FOUND`「工作流不存在」 |
| `GET /api/workflows/{alice 的 id}/report` | 404 同上 |
| `GET /api/workflows/history/{alice 的 id}` | 404 `NOT_FOUND`「没有归档记录」 |
| bob 自己的 `GET /api/datasets` / `GET /api/workflows/history` | 200，且 `total: 0`（看不到 alice 的任何数据） |

**关键点：**「这个 id 不存在」与「这个 id 是别人的」两份响应，`status` / `code` / `message` / `detail` **四项逐字相同**（我做了字段级比对，四项全 `true`）。
这正是 `app/api/ownership.py` 模块 docstring 定的规则（`:16-27`：不返回 403，因为 403 承认了资源存在），两条文案写成模块级常量以保证唯一事实来源（`:39-55`）。**这部分是本轮质量最高的隔离实现。**

### 2.3 边界结论（写进最终报告的措辞）

- 本项目**不是**多租户系统：没有租户概念、没有配额、没有跨租户的管理员视图。owner 隔离是"同一个单进程服务里的多账号互不可见"，**仅此而已**。
- 隔离覆盖到：数据集（读 + 写：登记时定 owner）、工作流状态（读 **+ 写**：`POST /api/analysis` 已补认证与归属校验，见 §03）、历史归档（读写 SQL 过滤）。
- ~~**没**覆盖到：`POST /api/analysis` 的写入路径（无登录要求、无归属校验、不设 owner）。~~ **已修复**（2026-10-16，证据 `app/api/routes_analysis.py:59 / :77 / :95-96`）——详情与实测过程见 §03，那里保留了修复前的原始发现。
- 数据集与工作流都**不是持久资源**：进程重启即消失（N10/N11），所以"隔离"讨论的前提是"服务还活着"。

---

## 03 一条实测出来的真实缺陷：写路径没有隔离（单点根因）

> **状态：已修复（修复时间晚于本节实测）。**
> 本节保留**原始发现过程与当时的实测证据**（不改写、不删），并在每处补「当前代码状态」。
> 修复日期 2026-10-16（由 backend-tests 按 lead 派单实施）；本节于 2026-10-16 12:0x 复核过新代码。
>
> 这不是"未实现的功能"，是**已实现的隔离漏了一个写入端**。它同时曾是主链路的断点，
> 所以单独成节。**结论以代码为准。**

### 3.1 三个事实（互相独立地指向同一个根因）

**事实 1：`POST /api/analysis` 没有登录要求。**
`app/api/routes_analysis.py:55-57` 的签名是

```
def start_analysis(
    req: QuestionRequest, background: BackgroundTasks
) -> ApiResponse[AnalysisAccepted]:
```

——**没有** `user: SessionUser = Depends(require_user)`。对照：同目录的
`app/api/routes_datasets.py:123 / 207 / 265 / 279` 四个接口**都有**这个依赖。
所以这是"漏了一个接口"，不是有意的设计选择。

> **当前代码状态：已修复。** `app/api/routes_analysis.py:56-60` 现在是
> `def start_analysis(req: QuestionRequest, background: BackgroundTasks, user: SessionUser = Depends(require_user)) -> ApiResponse[AnalysisAccepted]`；
> 依赖由 `app/api/routes_analysis.py:34`（`from app.api.ownership import require_owned_dataset`）
> 与 `:36`（`from app.core.auth import SessionUser, require_user`）导入。未登录现在会被拦成 401。
> （复核方式：读代码，2026-10-16。）

**事实 2：它的归属校验是"全局存在"，不是"归属"。**
`app/api/routes_analysis.py:70` 只有 `if not store.exists(req.dataset_id): raise AppError(...)`。
`store.exists()` 是全局存在性判断，**不区分 owner**。

**实测证据（隔离实例，两个账号）：** bob 用 alice 的 `dataset_id` 发 `POST /api/analysis`
→ **202 受理**（HTTP 层完全同意）；而同一个会话里 `GET /api/datasets/{alice 的 id}` → 404。
**写路径没隔离、读路径隔离了。**

> **当前代码状态：已修复。** `app/api/routes_analysis.py:77` 现在是
> `require_owned_dataset(store.get(req.dataset_id), user.id)` —— 走
> `app/api/ownership.py` 的**唯一**判定入口，与 `GET /api/datasets/{id}` 的 404 文案逐字一致；
> 代码注释（`:70-76`）里明确写了原因，并**引用了本节的实测症状**（"实测就是这个症状
> （202 受理 + 轮询 120 次全 404）"）作为修复依据。

**事实 3：这个接口建的 `WorkflowState` 从不设置 owner。**
`app/api/routes_analysis.py:82-90` 的构造里字段是
`workflow_id / dataset_id / question / status / step / created_at / max_steps`
——**没有 `owner_user_id`**，于是它落回默认值 `None`。
而 task-11 定的语义是 **owner 为 `None` → 谁也不给看**：

- `app/api/ownership.py:78-84`：`if state is None or state.owner_user_id != user_id: raise 404`；
- `app/schemas_workflow.py:246-248` 的字段说明同款语义；
- `app/data/history.py:300-305` 注释：SQL 里 `NULL = ?` 的结果是 NULL，**NULL owner 的老记录永远不会被匹配上**。

> **当前代码状态：已修复。** `app/api/routes_analysis.py:87-97` 的 `WorkflowState(...)`
> 现在带 `owner_user_id=user.id, owner_username=user.username`；注释（`:83-86`）写明
> "必须在**创建时**就写死……漏了就等于『谁也不给看』"。归档路径
> （`app/core/archive.py:76-77`）因此能拿到真实 owner，不再产生孤儿行。

### 3.2 后果（**历史后果：以下全部是修复前实测到的**）

1. **alice 自己发起的分析，她自己轮询也是 404。** 隔离实例上 120 轮轮询（每 0.5 秒一次，
   共 60 秒）**全部** 404 `NOT_FOUND`「工作流不存在」，`alice_workflow_final_status`
   停在 `PENDING`（轮询接口返回的 `data` 始终是 `None`）。
   直接后果：**"上传 → 提问 → 看进度 → 看图 → 看报告"这条主链路在当时是断的。**
2. 终态后 `archive_if_terminal()` 会把 `state.owner_user_id`（= `None`）抄进归档表
   （`app/core/archive.py:76-77`），产生**所有人（包括作者本人）都查不到的孤儿行**——
   这正是 `app/data/history.py:199-209` 那段注释警告过的情形：
   > "忘记传 owner"的代码写出来并且不报错 —— 而那种记录会变成看不见的孤儿（NULL owner 谁也不给看）。

**修复后复核（新增，2026-10-16）**
- 主链路**已恢复**。Lead 用独立脚本 `.tmp/lead_iso/verify_isolation.py`（真实 ASGI +
  真实 SQLite + 真实 cookie）复验：**21/21 PASS**，其中包含上面两条直接对应的断言——
  ①「B 用 A 的 `dataset_id` 发 `POST /api/analysis` → **404**」（修复前是 202）；
  ②「A 自己发起的分析，A 能轮询到」（修复前是 120 轮全 404）。
  该脚本不在本文件的写范围内，**我没有运行它**，本行是引用 lead 的复验结论。
- 回归测试覆盖：⚠️ **本轮实测发现一处契约测试仍未同步**——
  `tests/test_contracts_d4.py:40-43` 冻结的 `start_analysis` 参数表仍然只有
  `("req", empty)` 与 `("background", empty)` 两项，而新签名多了第三个参数 `user`。
  实跑结果（命令与原始输出见 `.tmp/ux/isolation-contract-check.txt`）：
  `1 failed, 4 passed`，失败断言为
  `AssertionError: app.api.routes_analysis.start_analysis 签名偏离契约` /
  `Left contains one more item: ('user', Depends(dependency=<function require_user ...>))`。
  **修法**：在 `tests/test_contracts_d4.py:40-43` 的期望列表里补
  `("user", inspect.Parameter.empty 对应的 Depends 默认值)`，或按"参数名前缀"断言。
  这条已同步给 lead。

### 3.3 修复记录（当时的建议 vs 实际落地）

**当时提的修复方向（本节首次成文时的记录，保留原样）**

- `app/api/routes_analysis.py`：加 `user: SessionUser = Depends(require_user)`；
  把 `store.exists(req.dataset_id)` 换成 `require_owned_dataset(store.get(req.dataset_id), user.id)`
  （复用 `app/api/ownership.py` 的**唯一**判定入口，不要自己写比较）；
  构造 `WorkflowState` 时补 `owner_user_id=user.id`（建议同时补 `owner_username`，便于前端显示"谁跑的"）。
- 归属建议同时影响：`POST /api/analysis` 的 404 文案应与 `ownership.py` 的
  `_DATASET_NOT_FOUND_*` 常量一致（否则又会出现"文案漂移泄露存在性"）。
- 回归测试建议：两个账号交叉（bob 用 alice 的 dataset 发起分析必须 404）；
  alice 发起的分析，alice 轮询必须 200。

**实际落地（复核代码后的对照，2026-10-16）**

| 建议 | 落地情况 | 证据 |
|---|---|---|
| 加 `Depends(require_user)` | ✅ 已加 | `app/api/routes_analysis.py:59` |
| 换成 `require_owned_dataset(...)` | ✅ 已换（复用唯一判定入口） | `app/api/routes_analysis.py:77` |
| 补 `owner_user_id` | ✅ 已补 | `app/api/routes_analysis.py:95` |
| 补 `owner_username` | ✅ 已补 | `app/api/routes_analysis.py:96` |
| 404 文案与 `ownership.py` 常量一致 | ✅ 由 `require_owned_dataset` 保证（它抛的就是那两个模块级常量） | `app/api/ownership.py:39-43, 67-73` |
| 回归测试：B 用 A 的 dataset → 404 | ✅ 有（Lead 的独立脚本 21/21 PASS 含此条） | `.tmp/lead_iso/verify_isolation.py`（**我未运行，引用 Lead 结论**） |
| 回归测试：A 能轮询到自己的分析 | ✅ 有（同上） | 同上 |
| 既有契约测试同步 | ❌ **未同步**，见 §3.2 与下条 | `tests/test_contracts_d4.py:40-43` |

**遗留一条（需要测试归属方处理）**：`tests/test_contracts_d4.py:40-43` 冻结的
`start_analysis` 参数表仍是 `[("req", …), ("background", …)]`，与新增的第三个参数
`user` 不符；实跑 `1 failed, 4 passed`（原始输出 `.tmp/ux/isolation-contract-check.txt`）。
**这条我只报告、不修**（`tests/**` 不在本文件的写范围内）。

---

## 04 对照任务书第 578-762 行的三态清单

（摘录任务书原文 → 本期状态 → 证据 → 未实现原因与补救路径）

| # | 任务书原文（摘录） | 本期状态 | 证据 | 原因与补救 |
|---|---|---|---|---|
| S1 | 7.1「数据集、任务和报告的真实概览」「统计指标必须来自真实后端数据」 | ✅ 已实现 | 工作台的 KPI 全部来自上传返回体与画像接口；空态是引导式空状态（`screenshots` 里 `01-workbench.png` 实测） | — |
| S2 | 7.1「最近报告」 | ✅ 已实现 | 报告页 + 历史页的归档详情里能拿到归档的报告正文（`GET /api/workflows/history/{id}` 返回 `report`） | — |
| S3 | 7.1「不要机械堆砌统计卡片，每个模块都必须有明确的业务目的」 | ⚠️ 部分 | 现有工作台是"上传卡 + 数据概览卡 + 开始一次分析卡 + 最近分析任务卡"四块，`docs/design-directions.md` §03 的 P1 记录了"上传前后两栏高度反转"的问题 | 已进设计方向文档，改版留给前端排期 |
| S4 | 7.2「支持真实上传状态、文件校验、失败反馈」 | ✅ 已实现 | `#upload-state` 实测 `已上传：sales.csv · 2,168 行 × 9 列 · dataset_id d54784c9…`；`.txt` 上传返回 415 且页面显示中文错误（`state-upload-415.png`） | — |
| S5 | 7.2「显示真实文件名、文件类型、文件大小、上传时间」 | ⚠️ 部分 | 文件名/行×列/大小都有（`#dataset-summary` 实测）；**上传时间**这一项本轮**未在前端界面上确认到**，但后端能力齐备（`GET /api/datasets` 的 `sort="uploaded_at"` 选项本身证明有上传时间字段，`app/api/routes_datasets.py:207-210`） | 补齐属前端范围；「前端是否已显示」标注为**本轮未验证** |
| S6 | 7.2「删除操作只有在后端支持并完成权限检查时才开放」 | ✅ 符合（=不开放） | 见 N2：无删除接口，前端也无删除按钮 | 后端要做删除须同时做 owner 校验 + 磁盘文件清理 |
| S7 | 7.3「取消操作（仅当后端确实支持安全取消时开放）」 | ✅ 符合（=不开放） | 见 N1 | 需要 registry 持有可取消句柄 + 线程池；本轮明确不做 |
| S8 | 7.3「当前选择的数据集 / 最近使用的数据集」 | ✅ 已实现 | 数据集选择卡是真实的 `<select>`（选项来自 `GET /api/datasets`）；"最近使用"由该接口的默认排序 `sort=uploaded_at` + `order=desc` 覆盖（`app/api/routes_datasets.py:207-212`，读代码） | 不是独立功能，是列表默认排序；若产品要求"最近**分析过**的"则需新接口（后端没有按最近使用排序的字段） |
| S9 | 7.3「Agent 执行状态区域……每个阶段使用真实状态 Pending/Running/Completed/Failed/Skipped」 | ⚠️ 部分 | 数据理解/主控规划/数据分析/可视化/报告校验**五类**在界面上落地为**六个**阶段（`profiling/planning/execution/charting/reporting/validation`），每个阶段有真实四态（`waiting/active/done/failed`）；**"skipped" 阶段级不存在**（`app/web/app.js:85-92` 注释明写"任务级才真的有『已跳过』，阶段级别没有跳过这回事"） | 任务书的 5 个 Agent 名与实现的 6 个阶段名不是一一对应，最终报告应对齐为**实现口径**并说明差异 |
| S10 | 7.3「不得通过前端计时器伪造任务进度」 | ✅ 已实现 | task-18：13 个渲染元素 / 13 个有真实字段支撑 / 0 个伪造；`setInterval` 仅 1 处（轮询） | — |
| S11 | 7.3「如果后端目前只提供总任务状态，应先如实展示总状态，记录细粒度状态所需的后端改造」 | ✅ 已做到（且超出） | 后端已有六阶段 `step` + `step_index`；细粒度再往下（阶段耗时/阶段级时间戳）**需要后端改造**，见 N7 | 若要做阶段耗时：`_enter()` 里给每个 step 记 `datetime` |
| S12 | 7.4「任务名称 / 关联数据集 / 创建时间 / 当前状态 / 结果入口 / 失败原因」 | ⚠️ 部分 | 历史列表有 `question/created_at/status/has_report/llm_used`（`HistoryItem`），结果入口=归档详情；**关联数据集**在历史列表里**没有**（`workflow_history` 表没有 `dataset_id` 列）；失败原因有 `error_code` | 补 `dataset_id` 列需改库（`HISTORY_SCHEMA_VERSION` 相关逻辑在 `app/data/history.py`） |
| S13 | 7.4「运行中、成功、失败、取消等状态应统一设计」 | ⚠️ 部分 | 运行中/成功/失败/成功带告警/重试中都有；**取消态不存在**（见 N1） | 与 N1 同源 |
| S14 | 7.5「分析标题和基本信息 / 核心指标 / 主要发现 / 图表与趋势 / 详细数据表格 / 数据质量说明 / 分析局限性 / 完整报告入口」 | ✅ 已实现 | 真实报告 JSON 的 `title/summary/sections[cites,table]/data_quality_notes/citations/generated_at/llm_used` 全部有；归档样例 `40ac2708…` 可逐字核对 | — |
| S15 | 7.5「比率有正确的计算基准」「区分原始数据、计算结果与 AI 生成的解释」 | ✅ 已实现（有具体机制） | `llm_used` 字段区分规则撰写与模型撰写；每段 `cites` 指回 task_id；校验阶段的「核对 N 个数字」 | — |
| S16 | 7.5「图表应统一标题、图例、坐标轴、单位和数字格式」 | ✅ 已实现（task-7） | 左对齐标题、横向网格、去 top/right 轴线、`销售额_sum → 销售额（求和）`、y 轴 `2,487 万` 记法、柱顶数值、底部口径脚注；14 张 before/after 实测对比 | — |
| S17 | 7.5「优先复用现有图表实现。是否更换图表库必须基于明确的业务或技术理由」 | ✅ 符合 | 仍是 matplotlib，零新依赖 | — |
| S18 | 7.6「导出功能只有在实际实现时才能展示。不得留下无效的 PDF、Excel 或其他导出按钮」 | ✅ 符合（=不展示） | 见 N5：无导出接口，前端 grep `导出|export` 零命中 | 若要做导出：报告是结构化 JSON，导出 Markdown/HTML 成本低，PDF 需要新依赖（**明确不做**） |
| S19 | 7.7「只开放真实存在的设置能力……不允许为了让导航看起来完整而制造无效开关」 | ✅ 已实现 | 设置页自述只读，列出真实的键名与改法（`docs/ARCHITECTURE.md:1350`） | — |
| S20 | 7.7「如果设置能力有限，优先做好账户信息和退出登录」 | ✅ 已实现 | 账户信息（`GET /api/auth/me`）、改密码、会话列表、退出登录四个都有真实接口 | — |
| S21 | 7.1/7.2「合理的空状态和错误状态」「没有数据时提供清晰的下一步指引」 | ⚠️ 部分 | 工作台/历史页有真实空态；但 `docs/design-directions.md` §09.1 记录"三套原型都只画了有数据之后的状态，空态/加载中/上传失败/未配模型/限流五种状态缺失" | 五种状态属前端范围，已进设计文档 |

统计（21 条）：**已实现 / 符合 15 条**（S1、S2、S4、S6、S7、S8、S10、S11、S14、S15、S16、S17、S18、S19、S20）、**部分实现 6 条**（S3 卡片机械重复、S5 上传时间待前端确认、S9 五类 Agent 名与六阶段名的口径差、S12 历史列表缺关联数据集、S13 无取消态、S21 五种状态缺失）、**完全未实现 0 条**。
真正的"要求了但本期做不到"只剩 N 系列里那些**任务书本身允许不做**的项（取消 / 删除 / 导出三类，任务书用的措辞都是"仅在支持时才开放"）。

---

## 05 「不得宣称」清单（给最终报告当护栏）

以下每一句都**不能**出现在最终报告、简历或演示话术中，括号里是反面依据。

1. **不得宣称"已实现多用户数据隔离 / 多租户隔离"。**
   反面依据：**"写路径未隔离"曾经存在、现已修复并已有隔离断言覆盖**（§03，修复证据 `app/api/routes_analysis.py:59 / :77 / :95-96` + Lead 的 21/21 复验）——但这**不足以**推出"多用户隔离已完成"：仍然**没有自助注册**（N3）、**数据集仍是进程内存**重启即丢（N10）、**`owner_user_id` 为 NULL 的旧归档行对所有人不可见**（`app/data/history.py:300-305`），而且项目本身没有租户概念。可以说的是：「**实现了多账号之间数据与工作流的归属隔离，不存在/非本人的资源返回逐字相同的 404（含写入路径）**」——这句有实测与代码支撑。
2. **不得宣称"数据已持久化 / 重启后数据还在"。**
   反面依据：数据集与工作流状态都是进程内 `dict`（`app/data/store.py:30-34`、`docs/ARCHITECTURE.md:1215`）。SQLite 只归档**历史工作流**（476 条），`data/charts` 下的 PNG 也在磁盘，但重启后**没有元信息能找回它们**。准确说法：「工作流**历史记录**落 SQLite，重启后仍可查询；数据集与运行中的工作流重启即丢」。
3. **不得宣称"支持断点续跑 / 失败自动重试跑完"。**
   反面依据：`docs/ARCHITECTURE.md:1315` 明写"SQLite 存的是历史归档，不是断点续跑"；重启后正在跑的工作流不会被恢复、也不会被标成失败，它只是消失。
4. **不得宣称"支持任务取消"。**
   反面依据：N1——无取消路由，且后台线程句柄不可达。任务书本身也只要求"确实支持时才开放"。
5. **不得宣称"支持导出报告 / 导出 Excel"。**
   反面依据：N5——无任何导出路由，前端也没有导出按钮（这一点做对了）。
6. **不得宣称"数据集可删除 / 可管理"。**
   反面依据：N2、N6——无删除、无编辑元信息、无归属转让接口。
7. **不得宣称"有细粒度执行日志 / Agent 思考过程可回放"。**
   反面依据：N8——`TaskBrief` 只暴露四项，没有 `params`、没有日志流。可以说的是：「六阶段状态 + 每个分析任务的 T1/T2 编号 + 报告每段的 `cites`」。
8. **不得宣称"显示每个阶段耗时"。**
   反面依据：N7——后端没有阶段级时间戳（只有工作流级三个），本次实测跑完一次分析只要 0.2–0.4 秒，且 4 线程 × 25ms 并发轮询也只抓到 2 个 step。D6 已定案：**阶段耗时禁止显示**。
9. **不得宣称"可水平扩展 / 支持多 worker / 已容器化部署"。**
   反面依据：N9——固定单进程；`docs/ARCHITECTURE.md` 技术选型表里 Docker 明确写"不进七天排期"，Celery/Redis 同理。
10. **不得宣称"已接入大模型"。**
    反面依据：本次全部实测都在 `llm_configured: false` 下完成，走的是规则撰写（`llm_used=0`）。准确说法：「**LLM 为可选项**：配置了就走模型撰写，没配置走规则撰写并且报告里 `llm_used=false` / 界面显示"未配置大模型，将使用规则模式"」。
11. **不得宣称"图表支持深色主题"。**
    反面依据：图表是服务端 matplotlib 渲染的**白底 PNG**（`_SURFACE = "#ffffff"`），前端深色主题下也是白底图。深色主题图表本轮明确不做（Lead 已裁决）。
12. **不得把"上传过的文件名"当成"数据集已入库"。**
    反面依据：`data/uploads/` 下的原始文件确实在磁盘上，但 `Dataset` 元信息在内存里，重启后没有任何接口能列出它们（N10）。

---

## 06 本轮未验证的部分（不许当成"不存在"）

| 项 | 为什么没验证 | 怎么验证 |
|---|---|---|
| `--workers 2` 下数据集/工作流 404 的真实复现 | 本轮没起过多进程实例 | 起 `uvicorn app.main:app --workers 2 --port 8145`，上传后轮询到另一个 worker 即应 404 |
| 登录限流 429 的真实报文 | 本轮没主动打满限流 | 隔离实例上连发 10+ 次错误密码，看 `Retry-After` |
| `.xlsx` 上传路径 | 本轮只传过 `.csv` | 用 pandas 造一个小 xlsx 走 `POST /api/datasets/upload` |
| `tests/` 里与认证/隔离相关的用例名与覆盖范围 | 只读了文档结论，没逐条列用例 | `pytest --collect-only -q` 后按关键字筛选 |
| `GET /api/datasets` 的 `limit/offset` 上限 | 只看到默认 `limit=50` / `offset=0` | **已补验（读代码）**：`app/api/routes_datasets.py:207-208` 是 `Query(50, ge=1, le=200)` + `Query(0, ge=0)`；另支持 `sort`（`uploaded_at`/`filename`/`rows`）与 `order`（`asc`/`desc`）。剩下未验证的是**超限时的错误报文**（422 的 `BAD_REQUEST` 文案） |
| 前端设置页是否列出上传时间 | 本轮只在 `#dataset-summary` 上确认了文件名/行列/大小 | 浏览器打开设置页与数据集页逐项核对 |

---

## 07 与文档既有表述的差异（以代码为准）

| 文档位置 | 文档原表述 | 代码事实 | 结论 |
|---|---|---|---|
| `docs/ARCHITECTURE.md:1346` | "**不做「最近数据集」列表** —— 后端没有 `GET /api/datasets`" | `GET /api/datasets` 已实现（`app/api/routes_datasets.py`，task-11） | 该行**已过时**，以代码为准 |
| `docs/ARCHITECTURE.md:1215 / 1252 / 1317` | 只能单进程、重启即丢 | 与代码一致（`app/data/store.py:30-34`） | 一致，无需改 |
| `app/data/history.py:199-209` 注释 | 警告"忘记传 owner 会产生看不见的孤儿行" | **实际已经发生**：`POST /api/analysis` 就是那个"忘记传 owner"的调用方（§03） | 注释是对的，代码漏了；建议随 §3.3 一起修 |
| `docs/ux-spec-final.md`（task-18 追加章节） | "六个阶段的状态全部来自后端 `step` 字段" | 与代码一致（`app/web/app.js:1955-2031`） | 一致 |

---

*本文件为交付物第 10 项。每条结论的证据位置已给到 `文件:行` 或命令；标注 `本轮未验证` 的条目
不得被解读为"不存在"。发现自己引用的行号与实际代码不符时，**以代码为准并回报**。*

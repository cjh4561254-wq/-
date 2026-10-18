# 可访问性（a11y）与响应式审计报告

- 审计阶段：**主轮 = lead 复核的冻结版（服务 2026-10-16 13:36:33 启动，在文件写入之后；工具 a11y_audit.py 1.2.1；指纹稳定）。其后 task-24 改过 app.css（7980B2FA…）、task-25 改过 index.html/app.js —— 见 §0.1、§15、§16**
- 报告生成时间：2026-10-16T15:32:08
- 测量工具：`a11y_audit.py 1.2.1`（工具本体 `.tmp/a11y/a11y_audit.py`）
- 被测服务：`http://127.0.0.1:8141`（隔离实例，端口 8141，数据目录 `.tmp/a11y/data/`，不碰 `data/db/auth.db` 与用户真实数据）
- 矩阵规模：48 组 = 8 页面 × 6 断点；成功 48 / 失败 0

**被测代码指纹**（跑矩阵前后各拍一次，`run_audit.py` 自动核对；不一致说明测量期间有人在改前端，那一轮只能算中间态）：

| 文件 | sha256（前 16 位） | mtime | 跑前跑后 |
|---|---|---|---|
| `app/web/index.html` | aca8a8b4129931da | 2026-10-16T13:25:02 | 一致 |
| `app/web/app.js` | 91dcf193adecf192 | 2026-10-16T13:25:02 | 一致 |
| `app/web/app.css` | 8730c1d495686269 | 2026-10-16T13:26:01 | 一致 |
| `app/web/login.html` | 17adba68cc0217a5 | 2026-10-16T13:15:52 | 一致 |
| `app/web/login.js` | 2be41faa79e4295f | 2026-10-16T12:22:55 | 一致 |
| `app/web/login.css` | f5a2f2c3bcac5799 | 2026-10-16T13:15:52 | 一致 |
| `app/web/tokens.css` | 4de3e276f42731d9 | 2026-10-16T11:14:51 | 一致 |

- 测量期间指纹：**稳定**；矩阵耗时 289.4s。

**服务启动时刻与被测文件 mtime 的关系**（这是本项目最容易踩的坑）：

- 被测服务启动时刻：**2026-10-16T13:36:33**（`uvicorn` 未加 `--reload`，**不会**重载已 import 的 `.py`；前端 `app/web/*` 是每次请求从磁盘读的）
- 被测前端文件的最新 mtime：**2026-10-16T13:26:01**  → 服务是在**冻结文件写入之后**启动的，因此本轮前端与后端读到的都是本报告指纹表里的那一版。
- 六个断点共用**同一个进程**（2026-10-16T13:36:33 起，未再重启），同一次播种的数据集/任务在 6 档之间复用 —— 这样避免了跨进程用旧 `workflow_id` 造成的假红。

### 0.1 版本口径：这份报告的每个数字对应哪一版代码

> **只想看「最后冻结的那一版」的读者**：直接跳到 **§17（合格复核轮 = 定稿轮）** —— 那一轮指纹稳定、可作为结论，严重项 **0** 条；本报告第 1~14 节的数字属于**主轮**（task-24 之前的版本），两者并列保留、不互相覆盖。

- 主轮 = 本报告第 1~14 节（目录 `.tmp/a11y/run-final4`；阶段说明见文首第 3 行，含被测文件哈希与这份报告之后又被改了什么）。
- 复核轮（中间态） = 第 15 节（目录 `.tmp/a11y/run-final5`，阶段：task-24 之后、task-25 之前（app.css 7980b2fa…）：测量窗口内 app.js/index.html 仍在被改，混用两版文件。
- 复核轮（中间态·第二次） = 第 16 节（目录 `.tmp/a11y/run-final6`，阶段：task-25 改动正在落盘时（index.html 测量中途再次变化）：仍属中间态，不得当合格轮引用。
- 合格复核轮（定稿轮） = 第 17 节（目录 `.tmp/a11y/run-final7`，阶段：定稿轮 = lead 2026-10-16 15:2x 宣布的冻结集（7 文件哈希跑前/跑中/跑后三次拍摄一致）；服务 2026-10-16 15:17:14 启动，晚于全部前端文件写入（最晚 login.html/login.js 15:06:26）；工具 a11y_audit.py 1.2.1。

| 文件 | 主轮 sha256（前 16 位） | 复核轮（中间态） sha256（前 16 位） | 对比 |
|---|---|---|---|
| `app/web/index.html` | aca8a8b4129931da | aca8a8b4129931da | 字节一致 |
| `app/web/app.js` | 91dcf193adecf192 | 91dcf193adecf192 | 字节一致 |
| `app/web/app.css` | 8730c1d495686269 | 7980b2fa0ff9b023 | **改过** |
| `app/web/login.html` | 17adba68cc0217a5 | 17adba68cc0217a5 | 字节一致 |
| `app/web/login.js` | 2be41faa79e4295f | 2be41faa79e4295f | 字节一致 |
| `app/web/login.css` | f5a2f2c3bcac5799 | f5a2f2c3bcac5799 | 字节一致 |
| `app/web/tokens.css` | 4de3e276f42731d9 | 4de3e276f42731d9 | 字节一致 |

- 两轮之间有 **1** 个前端文件被改动：`app/web/app.css`；其余 6 个文件字节一致。**两轮数字并列保留，主轮数字不被覆盖**——这样读者能看出改动到底影响了什么。 ⚠️ 但复核轮**测量期间文件仍在变动**（指纹不稳定），该轮只能当自查参考，见 §15（§16 同样不稳定）；§17 这一轮指纹**稳定**，是合格复核轮。

每一轮各自实测到的版本（引用某一节的数字前先看这一行）：

- **复核轮（中间态）** 实测版本：`app.css` `7980b2fa0ff9b023`；测量期间指纹**不稳定** —— app/web/index.html: aca8a8b41299 -> 2dedaeaf2dc2；app/web/app.js: 91dcf193adec -> e7f37a40c584（目录 `.tmp/a11y/run-final5`）。
- **复核轮（中间态·第二次）** 实测版本：`app.css` `7980b2fa0ff9b023`；测量期间指纹**不稳定** —— app/web/index.html: 2dedaeaf2dc2 -> 63dc15f13548（目录 `.tmp/a11y/run-final6`）。
- **合格复核轮（定稿轮）** 实测版本：`app.css` `7980b2fa0ff9b023`；测量期间指纹**稳定**（跑前跑后 7 个文件字节一致）（目录 `.tmp/a11y/run-final7`）；总发现 **187 条 = 严重 0 / 轻微 125 / 提示 62**；`#ask` 主按钮 6 档底边 708.56 / 708.56 / 676.56 / 755.66 / 698.08 / 698.08（全部在首屏内）。

**口径互斥声明（防止下游把两份报告的数字并列引用）**：

- 本报告**主轮**（第 1~14 节）= `app/web/app.css` `8730c1d4…`，即 **task-24 之前**。
- 需要「**task-24 之后**」的数字，只能引用本报告的 §15（复核轮（中间态））、§16（复核轮（中间态·第二次））、§17（合格复核轮（定稿轮）），**不要**把主轮数字当成「task-24 之后」的结论。
- `docs/ui-review.md` §12.8 的口径是「task-24 之后」；**本报告与它的数字不可混引**（同一句话里并列两边的数字就会互相矛盾）。若在最终报告里看到有人这么引，请直接指出。

## 1. 结论摘要

共 **189** 条发现：严重 2、轻微 125、提示 62。

| 严重度 | 条数 |
|---|---|
| 严重 | 2 |
| 轻微 | 125 |
| 提示 | 62 |

**先说通过的硬指标**（这些是本项目 `docs/ux-spec-final.md` §5 自己定的验收口径）：

| 验收项 | 结果 | 证据 |
|---|---|---|
| T2 各断点都没有横向滚动条（本轮实测 6 档：1440x900、1280x800、1024x768、768x1024、390x844、360x800） | 48/48 组通过 | 各断点 `documentElement.scrollWidth == clientWidth`（见 §3 逐断点表） |
| 键盘：所有可聚焦元素都能 Tab 到 | 全部通过 | `keyboard.unreached` 为空（见 §5） |
| 焦点环：每个可聚焦元素都有可见焦点环 | 全部通过 | `keyboard.noRing` 为空（见 §5） |

N-01 首屏可见性（主按钮 / 必填字段 / 卡片边距 / `scrollH vs vh`，口径见 §3.1）：登录态在 6 档断点上 `scrollH` 都等于视口高，**不需要滚动**（各档实测：1440x900=0px、1280x800=0px、1024x768=0px、768x1024=0px、390x844=0px、360x800=0px）；**有 2 条越界/遮挡**，逐条见 §3.1。

首次部署（初始化）态（多出「显示名称 / 确认密码」两个字段）：390x844 需要滚动 122px；360x800 需要滚动 188px，但主按钮与全部必填字段仍在首屏内（逐档数值见 §3.1）。

### 1.1 与冻结前基线的对比

| 严重度 | 冻结前（改造中（中间态 · 非定稿）） | 本轮 |
|---|---|---|
| 阻断 | 0 | 0 |
| 严重 | 137 | 2 |
| 轻微 | 355 | 125 |
| 提示 | 11 | 62 |
| **合计** | 503 | 189 |

| 检查项 | 冻结前 | 本轮 |
|---|---|---|
| `ARIA_PROBE_ERR` | 7 | 0 |
| `CONTRAST` | 57 | 0 |
| `ELEM_OVERFLOW` | 35 | 0 |
| `HEADING_SKIP` | 8 | 12 |
| `NO_NAV` | 4 | 6 |
| `OFFSCREEN_LEFT_OK` | 0 | 56 |
| `PRIMARY_BELOW_FOLD` | 0 | 2 |
| `TARGET_LT24` | 96 | 36 |
| `TARGET_LT44` | 282 | 77 |
| `TRUNC_NO_HINT` | 14 | 0 |

⚠️ **警告：两轮用的不是同一个工具版本** —— 基线轮 `a11y_audit.py 1.0.0`，本轮 `a11y_audit.py 1.2.1`。1.0.0 → 1.1.x 之间补上了 WCAG 2.5.8 的 Inline / Spacing 例外（基线里那 84 条 `TARGET_LT24` 严重项、35 条 `ELEM_OVERFLOW`、7 条 `ARIA_PROBE_ERR`是工具口径问题或工具自身假阳性，**不是产品缺陷**）；1.2.1 又补上了「sr-only 视觉隐藏元素不算文字截断」（改前会把 `<label class="sr-only" for="dataset-search">`报成 `TRUNC_NO_HINT`）。因此**两轮的「条数」不可直接相减**：严重项从 137 降到 2 里，绝大部分是工具口径修正，不能算成产品改进。跨轮真正可比的是**同一选择器的实测值**（见 §11 的原始色值与比值）。

读表提醒：基线轮的环境是「有 28/32 组采到报错文案（analysis、dataset、history、report、report-detail、settings、workbench），这些页面测的是**错误态渲染层**」；本轮是「全部处于正常态」。错误态与正常态的 DOM 不一样（错误框会替换掉部分内容），所以**跨轮的「条数」变化不能直接读成「改好了 / 改坏了」**；跨轮可比的是同一选择器的实测值（见 §11 的原始色值与比值）。

读表提醒：两轮的矩阵规模不同 —— 基线轮 **32 组**（4 档 × 8 页），本轮 **48 组**（6 档 × 8 页）。凡是「按页 × 断点」重复计数的检查项（`HEADING_SKIP`、`NO_NAV`、`OFFSCREEN_LEFT_OK`、`TARGET_LT44`、`TRUNC_NO_HINT`…），条数会随组数等比变化，**同一检查项的条数增减不等于页面变好或变差**；要判断是否真的改善，看的是同一选择器的实测值（§11）。

### 1.2 结论分四类（按任务书口径）

**① 真实缺陷**（本轮实测到、且不是工具口径造成的）：**2 条**：`PRIMARY_BELOW_FOLD`×2，逐条证据见 §2 与对应章节。

　已修的缺陷（闭环对照见 §11，给出「修复前 → 修复后」的实测值，不是口头声明）：侧栏 4 处 12px 文字的对比度 `4.446:1 → 4.712:1`；窄屏抽屉 `#nav-toggle` 的 Escape `不关闭 → 关闭且焦点回到触发按钮`；登录失败时的字段级 `aria-invalid`（见 §11 表格的本轮实测列）。

**② 工具假阳性**（本报告数字里已经剔除、但必须留档）：共 8 类，含本 session 自己发现并修掉的 3 类（`KB_UNREACHABLE` / `ELEM_OVERFLOW` / `ARIA_PROBE_ERR`）与 2 类口径修正（WCAG 2.5.8 的 Inline / Spacing 例外、装饰性标点对比度），逐条写明「为什么误报、谁独立复现、怎么修」→ **§11.2**。早期基线轮里因此出现的 84 条严重项**不是产品缺陷**。

**③ 未验证项**（真读屏器 / 真机 / forced-colors / 200% 缩放 / 其它浏览器等）：逐条列在 **§12**，共 7 条，每条都写明「没测」而不是「通过」。

**④ 验证到的正面事实**（都有本轮实测数值）：

| 正面事实 | 本轮实测值 |
|---|---|
| T2 无横向滚动 | 48/48 组通过（6 档断点） |
| 键盘可达性 | 未到达元素 0 个（48 组走查） |
| 焦点环 | 缺环元素 0 个 |
| prefers-reduced-motion | 8/8 个页面：运行中动画 0、无限动画 0、过渡 0 |
| 正文对比度最小值（不含装饰性标点） | 4.701:1（`aside#sidebar > nav.nav > a.nav__item:nth-of-type(6) > span.nav__flag:nth-of-type(2)`，rgb(111, 111, 106) 压 rgb(244, 247, 251)） |

其余正面事实（ARIA 状态真实性、跳过导航链接、手机端核心操作、图表替代文本）分别见 §4 / §9 / §10 / §11.1。

## 2. 检查项分布（本轮）

| 检查项 code | 条数 | 严重度构成 |
|---|---|---|
| `TARGET_LT44` | 77 | 轻微 77 |
| `OFFSCREEN_LEFT_OK` | 56 | 提示 56 |
| `TARGET_LT24` | 36 | 轻微 36 |
| `HEADING_SKIP` | 12 | 轻微 12 |
| `NO_NAV` | 6 | 提示 6 |
| `PRIMARY_BELOW_FOLD` | 2 | 严重 2 |

## 3. 逐断点分开写（T2 无横向滚动 + 逐断点结果）

| 断点 | 发现数 | T2 横向滚动 | 严重度构成 | 文档宽度实测（首页样本） |
|---|---|---|---|---|
| **1440x900** | 31 条 | 8/8 无横向滚动 | 轻微 23 / 提示 8 | `scrollWidth=1440 clientWidth=1440` |
| **1280x800** | 31 条 | 8/8 无横向滚动 | 轻微 23 / 提示 8 | `scrollWidth=1280 clientWidth=1280` |
| **1024x768** | 32 条 | 8/8 无横向滚动 | 严重 1 / 轻微 23 / 提示 8 | `scrollWidth=1024 clientWidth=1024` |
| **768x1024** | 28 条 | 8/8 无横向滚动 | 轻微 20 / 提示 8 | `scrollWidth=768 clientWidth=768` |
| **390x844** | 33 条 | 8/8 无横向滚动 | 轻微 18 / 提示 15 | `scrollWidth=390 clientWidth=390` |
| **360x800** | 34 条 | 8/8 无横向滚动 | 严重 1 / 轻微 18 / 提示 15 | `scrollWidth=360 clientWidth=360` |

逐页面明细：

| 页面 | 1440x900 | 1280x800 | 1024x768 | 768x1024 | 390x844 | 360x800 |
|---|---|---|---|---|---|---|
| 市场分析（`analysis`） | 2 | 2 | 3 | 2 | 3 | 4 |
| 数据集（`dataset`） | 6 | 6 | 6 | 6 | 5 | 5 |
| 历史任务（`history`） | 2 | 2 | 2 | 2 | 3 | 3 |
| 登录页（`login`） | 6 | 6 | 6 | 3 | 3 | 3 |
| 报告中心（`report`） | 2 | 2 | 2 | 2 | 3 | 3 |
| 报告详情（`report-detail`） | 3 | 3 | 3 | 3 | 4 | 4 |
| 设置（`settings`） | 4 | 4 | 4 | 4 | 5 | 5 |
| 工作台（`workbench`） | 6 | 6 | 6 | 6 | 7 | 7 |

### 3.1 首屏可见性（N-01 新口径：主按钮 / 必填字段 / 卡片边距 / scrollH vs vh）

口径（lead 2026-10-16 裁决，替换原先「卡片底边必须在首屏内」的硬指标）：

① 主操作按钮**在首屏可见**（完整落在视口内）；② 必填字段**全部在首屏可见且不被遮挡**；③ 无横向滚动（见 §3 的 T2）；④ 补充性内容（折叠的「本期边界」、页脚元信息）**允许落在首屏之下，但必须能滚到、不能被裁掉**。

「能不能滚到」的判定是纯计算（元素高 ≤ 视口高 且 文档坐标底边 ≤ 文档总高），**工具不滚动页面** —— 实测过：本脚本里一旦滚动，Chromium 的「顺序焦点导航起点」会被挪走，第一次 Tab 会落到页面最后一个可聚焦元素上，把正常页面误报成键盘不可达（证据 `.tmp/a11y/probe_tabstart.py`：不滚动时 tab1=`a#skip-link`，滚动后 tab1=`a.foot__link`）。

| 断点 | 主按钮（登录态） | 必填字段 | 卡片到视口上/下边距（px） | scrollH vs vh | 补充内容 |
|---|---|---|---|---|---|
| **1440x900** | `#submit` y=522.64 / 底边 558.64 / 视口高 900（完整在首屏内） | 2/2 在首屏 | 上 149.11 / 下 149.12 | `scrollH=900 vs vh=900`（无需滚动） | 全部可达 |
| **1280x800** | `#submit` y=472.64 / 底边 508.64 / 视口高 800（完整在首屏内） | 2/2 在首屏 | 上 99.11 / 下 99.12 | `scrollH=800 vs vh=800`（无需滚动） | 全部可达 |
| **1024x768** | `#submit` y=433.16 / 底边 469.16 / 视口高 768（完整在首屏内） | 2/2 在首屏 | 上 126.5 / 下 126.5 | `scrollH=768 vs vh=768`（无需滚动） | 全部可达 |
| **768x1024** | `#submit` y=516.63 / 底边 560.63 / 视口高 1024（完整在首屏内） | 2/2 在首屏 | 上 205.97 / 下 291.03 | `scrollH=1024 vs vh=1024`（无需滚动） | 全部可达 |
| **390x844** | `#submit` y=467.03 / 底边 511.03 / 视口高 844（完整在首屏内） | 2/2 在首屏 | 上 168.38 / 下 120.58 | `scrollH=844 vs vh=844`（无需滚动） | 全部可达 |
| **360x800** | `#submit` y=467.03 / 底边 511.03 / 视口高 800（完整在首屏内） | 2/2 在首屏 | 上 168.38 / 下 76.58 | `scrollH=800 vs vh=800`（无需滚动） | 全部可达 |

| 断点 | 工作台主按钮 | 分析页主按钮 |
|---|---|---|
| **1440x900** | #upload y=529.22 / 底边 573.22 / vh=900（完整在首屏内） | #ask y=737.66 / 底边 781.66 / vh=900（完整在首屏内） |
| **1280x800** | #upload y=529.22 / 底边 573.22 / vh=800（完整在首屏内） | #ask y=737.66 / 底边 781.66 / vh=800（完整在首屏内） |
| **1024x768** | #upload y=603.11 / 底边 647.11 / vh=768（完整在首屏内） | ⚠ #ask y=733.66 / 底边 777.66 / vh=768（**越出 9.66px**） |
| **768x1024** | #upload y=622.91 / 底边 666.91 / vh=1024（完整在首屏内） | #ask y=812.75 / 底边 856.75 / vh=1024（完整在首屏内） |
| **390x844** | #upload y=571.31 / 底边 615.31 / vh=844（完整在首屏内） | #ask y=798.27 / 底边 842.27 / vh=844（完整在首屏内） |
| **360x800** | #upload y=571.31 / 底边 615.31 / vh=800（完整在首屏内） | ⚠ #ask y=821.36 / 底边 865.36 / vh=800（**越出 65.36px**） |

**本轮唯一的严重项（`PRIMARY_BELOW_FOLD`，2 条）**：

- `市场分析工作区` @1024x768 → `button#ask`：主操作按钮未完整落在首屏：y=733.66px，高 44px，底边 777.66px > 视口高 768px（越出 9.66px）。首屏内能看到的只是它的上沿。
- `市场分析工作区` @360x800 → `button#ask`：主操作按钮未完整落在首屏：y=821.36px，高 44px，底边 865.36px > 视口高 800px（越出 65.36px）。首屏内能看到的只是它的上沿。

读法：本项目自己的断点最窄到 **390×844**，在那一档 `#ask` 底边 842.27 ≤ 844 —— **完整落在首屏内，但只剩 1.73px 余量**（等于贴着底边）；本报告额外加的 **360×800** 档才真正越界 65.36px。所以这条的真实现象是「**分析页把主按钮放在首屏最底部，余量极小、再窄一点就会掉出去**」，建议在 `#ask` 上方收紧垂直节奏（或把问题输入区与按钮整体上移），而不是等更窄的机型出现才暴露 —— 它不属于当前声明断点内的失败，但属于稳健性风险。

同一张表在**首次部署（初始化）态**下的复测（账号库为空时页面会多出「显示名称 / 确认密码」两个字段，`html[data-auth-mode=bootstrap]`；数据来自 `.tmp/a11y/probe_bootstrap.py`，跑在独立的空账号库服务上，端口 8142）：

| 断点（初始化态） | 主按钮底边 / 视口高 | 必填字段在首屏 | 卡片上/下边距（px） | scrollH vs vh | 主按钮底边：空表单 → 填满 → 密码不一致 |
|---|---|---|---|---|---|
| **1440x900** | 673.88 / 900 | 3/3 | 上 49.77 / 下 49.78 | `scrollH=900 vs vh=900`（无需滚动） | 673.88 → 填满 673.88 → 错误态 674.7 |
| **1280x800** | 604.98 / 800 | 3/3 | 上 18.67 / 下 18.67 | `scrollH=800 vs vh=800`（无需滚动） | 604.98 → 填满 604.98 → 错误态 629.41 |
| **1024x768** | 574.52 / 768 | 3/3 | 上 17.14 / 下 17.14 | `scrollH=768 vs vh=768`（无需滚动） | 574.52 → 填满 574.52 → 错误态 600.47 |
| **768x1024** | 775.34 / 1024 | 3/3 | 上 205.97 / 下 76.31 | `scrollH=1024 vs vh=1024`（无需滚动） | 775.34 → 填满 775.34 → 错误态 806.44 |
| **390x844** | 733.75 / 844 | 3/3 | 上 168.38 / 下 -102.14 | `scrollH=966 vs vh=844`（**需滚动 122px**） | 733.75 → 填满 733.75 → 错误态 764.84 |
| **360x800** | 755.19 / 800 | 3/3 | 上 168.38 / 下 -167.58 | `scrollH=988 vs vh=800`（**需滚动 188px**） | 755.19 → 填满 755.19 → 错误态 786.28 |

表里「卡片到视口下边距」出现**负数**就表示卡片底边已经落到首屏之下（= 那一档需要滚动才能看全卡片）；但按 N-01 新口径，只要①②③成立、且④的补充内容能滚到，这就不算不通过。

初始化态的「填满」与「错误态」两列是同一颗按钮在三种真实状态下的底边坐标：空表单（按钮 disabled）→ 填满合法输入（按钮可点）→ 两次密码不一致（错误文案出现、页面被顶高）。三态的实测值说明：**启用按钮本身不改变版式**，而真实的错误提示会把按钮推低几十像素 —— 下面这张表就是那几个数，用来确认它没有把主按钮推出首屏。

| 断点 | 「两次输入的密码不一致」错误文案实测位置 |
|---|---|
| **1440x900** | y=599.61「两次输入的密码不一致，请重新输入。」 |
| **1280x800** | y=554.31「两次输入的密码不一致，请重新输入。」 |
| **1024x768** | y=525.38「两次输入的密码不一致，请重新输入。」 |
| **768x1024** | y=723.34「两次输入的密码不一致，请重新输入。」 |
| **390x844** | y=681.75「两次输入的密码不一致，请重新输入。」 |
| **360x800** | y=703.19「两次输入的密码不一致，请重新输入。」 |

说明：lead 提到「低于 1280×800 时需滚动约 86–110px」。按当前版本实测，**日常登录态在 6 档断点上 scrollH 都等于视口高（完全不需要滚动）**；需要滚动的只有**初始化态的 390×844**（`scrollH=966 vs vh=844`，需滚动 122px）。两者不矛盾：login.css 里 `html[data-auth-mode=bootstrap]` 的垂直节奏压缩规则（`app/web/login.css:809-826`）就是为收敛这个溢出加的，其注释记录的是**改动前**的实测值（1440 越 38.3px / 1280 越 138.3px / 1024 越 156.2px，数据在 `.tmp/login/`）。所以 86–110px 那组数对应的是更早的版本或另一处测量对象，本报告以本轮实测为准。

## 4. 键盘可达性（Tab 顺序 / 焦点环 / Shift+Tab 回退 / skip link）

| 页面 @ 断点 | Tab 可达/可聚焦 | Tab 到不了 | 无焦点环 | Shift+Tab 能回退 | 会绕圈 |
|---|---|---|---|---|---|
| 登录页 @1440x900 | 6/6 | 0 | 0 | 是 | 否 |
| 工作台 @1440x900 | 25/25 | 0 | 0 | 是 | 否 |
| 数据集 @1440x900 | 28/28 | 0 | 0 | 是 | 否 |
| 市场分析 @1440x900 | 17/17 | 0 | 0 | 是 | 否 |
| 报告中心 @1440x900 | 30/30 | 0 | 0 | 是 | 否 |
| 报告详情 @1440x900 | 10/10 | 0 | 0 | 是 | 否 |
| 历史任务 @1440x900 | 30/30 | 0 | 0 | 是 | 否 |
| 设置 @1440x900 | 12/12 | 0 | 0 | 是 | 否 |
| 登录页 @1280x800 | 6/6 | 0 | 0 | 是 | 否 |
| 工作台 @1280x800 | 25/25 | 0 | 0 | 是 | 否 |
| 数据集 @1280x800 | 28/28 | 0 | 0 | 是 | 否 |
| 市场分析 @1280x800 | 17/17 | 0 | 0 | 是 | 否 |
| 报告中心 @1280x800 | 30/30 | 0 | 0 | 是 | 否 |
| 报告详情 @1280x800 | 10/10 | 0 | 0 | 是 | 否 |
| 历史任务 @1280x800 | 30/30 | 0 | 0 | 是 | 否 |
| 设置 @1280x800 | 12/12 | 0 | 0 | 是 | 否 |
| 登录页 @1024x768 | 6/6 | 0 | 0 | 是 | 否 |
| 工作台 @1024x768 | 25/25 | 0 | 0 | 是 | 否 |
| 数据集 @1024x768 | 28/28 | 0 | 0 | 是 | 否 |
| 市场分析 @1024x768 | 17/17 | 0 | 0 | 是 | 否 |
| 报告中心 @1024x768 | 30/30 | 0 | 0 | 是 | 否 |
| 报告详情 @1024x768 | 10/10 | 0 | 0 | 是 | 否 |
| 历史任务 @1024x768 | 30/30 | 0 | 0 | 是 | 否 |
| 设置 @1024x768 | 12/12 | 0 | 0 | 是 | 否 |
| 登录页 @768x1024 | 6/6 | 0 | 0 | 是 | 否 |
| 工作台 @768x1024 | 25/25 | 0 | 0 | 是 | 否 |
| 数据集 @768x1024 | 28/28 | 0 | 0 | 是 | 否 |
| 市场分析 @768x1024 | 17/17 | 0 | 0 | 是 | 否 |
| 报告中心 @768x1024 | 30/30 | 0 | 0 | 是 | 否 |
| 报告详情 @768x1024 | 10/10 | 0 | 0 | 是 | 否 |
| 历史任务 @768x1024 | 30/30 | 0 | 0 | 是 | 否 |
| 设置 @768x1024 | 12/12 | 0 | 0 | 是 | 否 |
| 登录页 @390x844 | 6/6 | 0 | 0 | 是 | 否 |
| 工作台 @390x844 | 25/25 | 0 | 0 | 是 | 否 |
| 数据集 @390x844 | 28/28 | 0 | 0 | 是 | 否 |
| 市场分析 @390x844 | 17/17 | 0 | 0 | 是 | 否 |
| 报告中心 @390x844 | 30/30 | 0 | 0 | 是 | 否 |
| 报告详情 @390x844 | 10/10 | 0 | 0 | 是 | 否 |
| 历史任务 @390x844 | 30/30 | 0 | 0 | 是 | 否 |
| 设置 @390x844 | 12/12 | 0 | 0 | 是 | 否 |
| 登录页 @360x800 | 6/6 | 0 | 0 | 是 | 否 |
| 工作台 @360x800 | 25/25 | 0 | 0 | 是 | 否 |
| 数据集 @360x800 | 28/28 | 0 | 0 | 是 | 否 |
| 市场分析 @360x800 | 17/17 | 0 | 0 | 是 | 否 |
| 报告中心 @360x800 | 30/30 | 0 | 0 | 是 | 否 |
| 报告详情 @360x800 | 10/10 | 0 | 0 | 是 | 否 |
| 历史任务 @360x800 | 30/30 | 0 | 0 | 是 | 否 |
| 设置 @360x800 | 12/12 | 0 | 0 | 是 | 否 |

工作台 @1440x900 的真实 Tab 顺序（逐步实测，`focusVisiblePseudo` = 键盘焦点态生效）：

| 步 | 元素 | 可访问名 | outline | 焦点环可见 |
|---|---|---|---|---|
| 0 | html > body > a.skip-link | 跳到主内容 | 2px solid rgb(61, 111, 174) | 是 |
| 1 | html > body > header.topbar > div.topbar__right:nth-of-type(2) > a.topbar__link | 接口文档 | 2px solid rgb(61, 111, 174) | 是 |
| 2 | button#user-btn | a a11y_admin | 2px solid rgb(61, 111, 174) | 是 |
| 3 | aside#sidebar > nav.nav > a.nav__item:nth-of-type(1) | 工作台 | 2px solid rgb(61, 111, 174) | 是 |
| 4 | aside#sidebar > nav.nav > a.nav__item:nth-of-type(2) | 数据集 | 2px solid rgb(61, 111, 174) | 是 |
| 5 | aside#sidebar > nav.nav > a.nav__item:nth-of-type(3) | 市场分析 | 2px solid rgb(61, 111, 174) | 是 |
| 6 | aside#sidebar > nav.nav > a.nav__item:nth-of-type(4) | 分析报告 | 2px solid rgb(61, 111, 174) | 是 |
| 7 | aside#sidebar > nav.nav > a.nav__item:nth-of-type(5) | 历史任务 | 2px solid rgb(61, 111, 174) | 是 |
| 8 | aside#sidebar > nav.nav > a.nav__item:nth-of-type(6) | 设置 只读 | 2px solid rgb(61, 111, 174) | 是 |
| 9 | aside#sidebar > div.sidebar__foot > a | 健康检查 /api/health | 2px solid rgb(61, 111, 174) | 是 |
| 10 | input#file |  | 2px solid rgb(61, 111, 174) | 是 |
| 11 | button#upload | 上传 | 2px solid rgb(61, 111, 174) | 是 |
| 12 | section#view-workbench > div.grid.grid--main > section.card:nth-of-type(2) > div.card__head:nth-of-type(1) > a.card__action | 查看数据集详情 | 2px solid rgb(61, 111, 174) | 是 |
| 13 | section#view-workbench > section.panel:nth-of-type(2) > div.panel__head:nth-of-type(1) > a.card__action | 全部数据集 | 2px solid rgb(61, 111, 174) | 是 |
| 14 | div#asset-recent > button.listrow:nth-of-type(1) | sales_sample.csv .csv 35 | 2px solid rgb(61, 111, 174) | 是 |
| 15 | div#asset-recent > button.listrow:nth-of-type(2) | sales_sample.csv .csv 35 | 2px solid rgb(61, 111, 174) | 是 |
| 16 | div#asset-recent > button.listrow:nth-of-type(3) | sales_sample.csv .csv 35 | 2px solid rgb(61, 111, 174) | 是 |
| 17 | section#view-workbench > section.panel:nth-of-type(3) > div.panel__head:nth-of-type(1) > a.card__action | 全部历史 | 2px solid rgb(61, 111, 174) | 是 |
| 18 | div#recent-tasks > button.listrow:nth-of-type(1) | 各地区销售额对比，哪个地区最高？ 状态 成功 有 | 2px solid rgb(61, 111, 174) | 是 |
| 19 | div#recent-tasks > button.listrow:nth-of-type(2) | 各地区销售额对比，哪个地区最高？ 状态 成功 有 | 2px solid rgb(61, 111, 174) | 是 |
| 20 | div#recent-tasks > button.listrow:nth-of-type(3) | 各地区销售额对比，哪个地区最高？ 状态 成功 有 | 2px solid rgb(61, 111, 174) | 是 |
| 21 | div#recent-tasks > button.listrow:nth-of-type(4) | 焦点迁移实测：各地区的销售额汇总 状态 成功 有 | 2px solid rgb(61, 111, 174) | 是 |
| 22 | div#recent-tasks > button.listrow:nth-of-type(5) | 各地区的销售额汇总 状态 成功 有报告 规则模板 | 2px solid rgb(61, 111, 174) | 是 |
| 23 | section#view-workbench > section.panel:nth-of-type(4) > div.row:nth-of-type(2) > a.btn.btn--ghost:nth-of-type(1) | 进入市场分析 | 2px solid rgb(61, 111, 174) | 是 |
| 24 | section#view-workbench > section.panel:nth-of-type(4) > div.row:nth-of-type(2) > a.btn.btn--ghost:nth-of-type(2) | 查看分析报告 | 2px solid rgb(61, 111, 174) | 是 |
| 25 | body/none |  |  | 否 |

**skip link 是不是第一个 Tab 停靠点**：新开一个干净页面、载入后按第一次 Tab，落点是 `html > body > a.skip-link`（可访问名「跳到主内容」），位置 `x=12 y=12`，outline `rgb(61, 111, 174) solid 2px`，可见=True → **是，且它真的显示在左上角**。

## 5. 表单可访问性

矩阵共采到 **6** 个表单控件（去重后，按元素路径）：`input#username`、`input#password`、`input#file`、`input#dataset-search`、`select#dataset-select`、`textarea#question`

检查项 `FIELD_NO_LABEL`（无 label）、`DESCRIBEDBY_DANGLING`（aria-describedby 指向不存在的 id）、`ERRMSG_DANGLING`（aria-errormessage 指向不存在的 id）**本轮 0 命中**。

| 控件 | 类型 | label 文本 | label 数 | aria-label | aria-describedby |
|---|---|---|---|---|---|
| `input#username` | text | 用户名 | 1 | — | username-hint |
| `input#password` | password | 密码 | 1 | — | password-hint |
| `input#file` | file | 点击选择文件，或把文件拖到这里
表格第一行会被当作列名 | 1 | — | — |
| `input#dataset-search` | search | 按文件名搜索数据集 | 1 | — | — |
| `select#dataset-select` | select-one | 要用哪一份数据做分析 | 1 | — | — |
| `textarea#question` | textarea | 分析问题 | 1 | — | — |

### 5.1 提交后的焦点迁移（真实点击后立刻采 `document.activeElement`）

| 提交路径 | 提交前焦点 | 提交后焦点（立刻） | 按钮随即 disabled？ | 状态文案的 role / aria-live 兜底 |
|---|---|---|---|---|
| 提交登录表单（`#submit`） | — | 未覆盖（探针提交后直接等跳转，未采 activeElement；页面跳转本身会重置焦点） | — | 落地 `http://127.0.0.1:8141/app#/workbench` |
| 提交上传（`#upload`） | `button#upload` | `body`（焦点已不在任何控件上） | False | `#upload-state` role=status aria-live（未设置） |
| 提交分析任务（`#ask`） | `button#ask` | `body`（焦点已不在任何控件上） | False | `#ask-state` role=status aria-live（未设置）、`#workflow-state` role=status aria-live（未设置）、`#elapsed` （无 role）aria-live（未设置） |

**提交后焦点去向（实测）**：「上传、开始分析」这两次真实点击后，`document.activeElement` 都落到了 `body` —— 焦点离开了控件。实测两处按钮点完都**不是**因为「按钮被禁用」（`#ask` 点击后 `disabled=False`、`#upload` 点击后 `disabled=False`），而是这两处提交后**节点被重新渲染替换掉** —— 旧节点被移除，焦点没有可停留的宿主，于是回落到 `body`。按 lead 的口径，重渲染是真实行为、**不要求强行把焦点塞回按钮**；若要改进，低风险做法是提交后把焦点移到结果/状态区域（`#upload-state` / `#ask-state`，加 `tabindex="-1"` 后聚焦）——本报告只给建议，不改产品代码（app.js 的写入权限不在本任务）。

兜底机制（实测）：`#upload-state` 与 `#ask-state` 都带 `role=status`（隐含 `aria-live=polite`），所以「已上传…」「已提交，workflow_id …」会被朗读，不需要焦点停在按钮上。

进度通知（提交后 3 秒复查，实测）：`#ask-state`、`#workflow-state` 都带 `role=status`（隐含 `aria-live=polite`）；其中 `#workflow-state` 的文本在提交后**确实发生了变化**（`#ask-state`=False、`#workflow-state`=True）→ **分析中途的状态推进（`CHARTING` → `SUCCESS`、步骤 done）有可编程通知**，屏幕阅读器会被动播报，不需要焦点停在按钮上。

仍没有 role / aria-live 的是 `#elapsed`（文本是否变化：`#elapsed`=True）—— 它是「已用时 N 秒」这类**持续变化的计时文案**。这类计时器通常**故意**不加 live region（每个 tick 都播报会把屏幕阅读器刷屏），所以这里只记录现状、**不作为缺陷**；若要显式表达，可标 `aria-live="off"` 说明「此处刻意不播报」。

登录失败路径（填错口令后点提交）：错误容器 `#alert` 可见=True、`role=alert`、`tabindex=-1`、文案「用户名或密码不正确 用户名与密码不匹配。为避免用户名枚举，服务端不会说明具体是哪一项不对；连续多次失败会触发短时限制。 」；提交后 `document.activeElement` = `input#password`，**焦点是否移到错误容器 = False**。`role=alert` 隐含 `aria-live=assertive`，所以即使焦点不移动，错误也会被朗读。

| 字段 | aria-invalid | aria-describedby | 指向的 id 都存在 | aria-errormessage |
|---|---|---|---|---|
| `#username` | — | username-hint | True | — |
| `#password` | — | password-hint | True | — |

（`aria-invalid` 列写「—」= 该属性**不存在**，不是 `false`：失败态下也没有被设置，见下面的读表提醒。）
读表提醒：两个输入框都**没有** `aria-invalid`，`aria-describedby` 指向的是提示文案（`username-hint` / `password-hint`）而不是错误文案 —— 也就是说**字段粒度上没有「这个字段错了」的标记**，错误只挂在 `#alert` 上（`role=alert`，会朗读）。服务端刻意不说明是用户名还是口令不对（防用户名枚举，见 `#alert` 文案），所以字段级标记在当前产品决策下做不到；这里只记录机制，不写成「违反 3.3.1」。


## 6. prefers-reduced-motion

| 页面 | 媒体查询命中 reduce | 仍在跑的动画 | 无限动画 | 带过渡的元素 | 判定 |
|---|---|---|---|---|---|
| 登录页（`login`） | 是 | 0 | 0 | 0 | 通过 |
| 工作台（`workbench`） | 是 | 0 | 0 | 0 | 通过 |
| 数据集（`dataset`） | 是 | 0 | 0 | 0 | 通过 |
| 市场分析（`analysis`） | 是 | 0 | 0 | 0 | 通过 |
| 报告中心（`report`） | 是 | 0 | 0 | 0 | 通过 |
| 报告详情（`report-detail`） | 是 | 0 | 0 | 0 | 通过 |
| 历史任务（`history`） | 是 | 0 | 0 | 0 | 通过 |
| 设置（`settings`） | 是 | 0 | 0 | 0 | 通过 |

口径：`browser.new_context(reduced_motion='reduce')` 下逐页采集 —— **8/8 页通过**。

## 7. 不达标配色（原始色值 + 比值 + 文件:行）

本轮 `CONTRAST` **0 命中**：所有被判定的文本都达到项目口径（正文 ≥4.5:1、大字 ≥3.0:1，`docs/design-system.md` §13.2）。

覆盖规模：共核对 **4744** 组「前景色 × 有效背景色」配对（有效背景 = 逐层往上找到的第一个不透明祖先底色，不是简单取 body 颜色）。

## 8. 距离 WCAG 2.1 AA 还差什么

按「剩余问题 → 影响的成功准则 → 要改什么」列：

| 检查项 | 条数 | 对应 WCAG | 期望（工具口径） |
|---|---|---|---|
| `TARGET_LT44` | 77 | WCAG 2.5.5 Target Size (Enhanced)（AAA 级，非 AA 要求） / 项目规范 T1 | 推荐 ≥ 44×44px（项目规范 docs/ux-spec-final.md T1） |
| `OFFSCREEN_LEFT_OK` | 56 | WCAG 1.4.10 Reflow（本项通过） | 仅作记录：确认它没有把页面撑出横向滚动条 |
| `TARGET_LT24` | 36 | WCAG 2.5.8 Target Size (Minimum) | ≥ 24×24px |
| `HEADING_SKIP` | 12 | WCAG 1.3.1 | 标题层级不跳级 |
| `NO_NAV` | 6 | WCAG 1.3.1 / 2.4.1（本页可豁免） | 仅作记录：等改造后页面出现成组导航时再复查 |
| `PRIMARY_BELOW_FOLD` | 2 | 项目规范 docs/ux-spec-final.md §5 N-01（Lead 2026-10-16 裁决） | 主操作按钮完整落在首屏内（N-01 口径 ①） |

### 8.1 目标尺寸口径裁定：「审计门槛 44×44 vs WCAG AA 24×24」到底该用哪个

本轮工具同时报出两类数字：`TARGET_LT24` **36** 条（小于 24×24）、`TARGET_LT44` **77** 条（小于 44×44）。两者不是同一件事，裁定如下。

| 标准 / 依据 | 等级 | 门槛 | 在本项目里怎么用 |
|---|---|---|---|
| WCAG 2.2 SC 2.5.8 Target Size (Minimum) | **AA** | 24×24 CSS px | **本报告的合规判定标准**；带 4 条例外（Spacing / Inline / 用户代理控件 / 必要） |
| WCAG 2.2 SC 2.5.5 Target Size (Enhanced) | AAA | 44×44 CSS px | **不是 AA 要求**；本项目未声明对齐 AAA，故不作为 AA 失败项 |
| WCAG 2.1（本项目 `docs/design-system.md` 声明的对齐目标） | AA | **没有目标尺寸准则** | 因此「2.1 AA」口径下目标尺寸**不构成 AA 失败项**——这一点必须说清，否则会把一个 2.2 才有的要求写成「违反 2.1」 |
| 项目自定规范 `docs/ux-spec-final.md` §5 T1 | 项目推荐级 | ≥44×44px | 比 AA 更严的**项目自家门槛**，本报告报为 `TARGET_LT44`（轻微/建议），不当成 AA 不达标 |
| 项目渲染层现状 `app/web/app.css:1208-1252` | — | 主要可点元素 44px 地板；`.th-sort` 24×24（窄屏 44） | 实现方自己已经把 2.5.8 的 24×24 设为硬底线、把 T1 的 44px 设为主体标准，与本裁定一致 |

**哪些条目应当豁免（不再计为待修）**：

- `input#file` 这类 **sr-only（1×1px、`clip`）隐藏输入**：真正可点的是它所在的整块 `#dropzone`（`<label for="file">`，焦点环画在整块区域上）→ 2.5.8 的 **Inline / 等效控件** 情形，豁免；
- **正文流内的行内链接**（如 `p#foot-meta > a.foot__link`、设置页 `a`）：2.5.8 **Inline 例外**，豁免；
- **中心 24px 圆内没有其它目标**的目标（如 `table#dataset-table button.th-sort`、`aside#sidebar > div.sidebar__foot > a`）：2.5.8 **Spacing 例外**成立，豁免；
- 纯装饰元素、无交互元素：2.5.8 不适用。

**哪些应当继续报（不能豁免）**：

- 小于 24×24、且不落任何例外的可点目标 → 这是 **WCAG 2.2 AA 的真实失败项**，必须修；
- **主操作路径上的目标**（登录 `#submit`、上传 `#upload`、开始分析 `#ask`、窄屏抽屉开关 `#nav-toggle`）小于 44×44 → 按项目 T1 继续报（轻微，建议级）；
- 报「建议」时不要写成「违反 WCAG 2.1 AA」——本项目对齐的是 2.1，而 2.1 里没有这条准则。

一句话裁决：**合规判定以 WCAG 2.2 AA 的 24×24（含例外）为准；44×44 是项目 T1 的推荐级门槛，本报告继续以建议形式报出，两者不混为一谈。**

**触控目标的关键实测值**（尺寸取自本轮全部 48 组运行的 `metrics.targets` 原始采集）：

| 控件（实测路径） | 实测尺寸（按样本数） | 命中的检查项 |
|---|---|---|
| `a#skip-link` | 110×37.44px（6 组） | `TARGET_LT44`×6 |
| `aside#sidebar > div.sidebar__foot > a` | 115.19×24px（42 组） | `TARGET_LT44`×42 |
| `button#toggle-password` | 34×34px（3 组）、44×44px（3 组） | `TARGET_LT44`×3 |
| `input#file` | 1×1px（6 组） | `TARGET_LT24`×6 |
| `input#password` | 370×42px（2 组）、378×42px（1 组） | `TARGET_LT44`×3 |
| `input#username` | 370×42px（2 组）、378×42px（1 组） | `TARGET_LT44`×3 |
| `p#foot-meta > a.foot__link` | 48×19.8px（6 组） | `TARGET_LT24`×6 |
| `section#view-settings > section.card:nth-of-type(3) > div.card__body:nth-of-type(2) > p.state > a` | 34.67×17px（6 组） | `TARGET_LT24`×6 |
| `section#view-workbench > div.grid.grid--main > section.card:nth-of-type(2) > div.card__head:nth-of-type(1) > a.card__action` | 107.64×21.44px（6 组） | `TARGET_LT24`×6 |
| `section#view-workbench > section.panel:nth-of-type(2) > div.panel__head:nth-of-type(1) > a.card__action` | 81.64×21.44px（6 组） | `TARGET_LT24`×6 |
| `section#view-workbench > section.panel:nth-of-type(3) > div.panel__head:nth-of-type(1) > a.card__action` | 68.64×21.44px（6 组） | `TARGET_LT24`×6 |
| `table#dataset-table > thead > tr > th.is-num:nth-of-type(3) > button.th-sort` | 34×25.44px（4 组）、34×44px（2 组） | `TARGET_LT44`×6 |
| `table#dataset-table > thead > tr > th.is-num:nth-of-type(5) > button.th-sort` | 34×25.44px（4 组）、34×44px（2 组） | `TARGET_LT44`×6 |
| `table#dataset-table > thead > tr > th:nth-of-type(1) > button.th-sort` | 47×25.44px（4 组）、47×44px（2 组） | `TARGET_LT44`×4 |
| `table#dataset-table > thead > tr > th:nth-of-type(4) > button.th-sort` | 75.89×25.44px（4 组）、75.89×44px（2 组） | `TARGET_LT44`×4 |

读法：`input#file` 是 sr-only 的隐藏输入，真正可点的是它撑起的整块拖拽区 `#dropzone`（焦点环证据见 §9.2），属 2.5.8 的 Inline 例外；`button#nav-toggle`（窄屏抽屉开关）上游修复后宽度已达 44px、高度 34px；`th-sort`（表头排序按钮）是表内小目标，中心 24px 圆内没有别的目标 → 命中 Spacing 例外。以上都不会让页面「违反 AA」，但手指点按的舒适度仍建议按项目 T1（44×44px）收敛。

### 8.2 非令牌字面量复核（lead 点名第 3 条）

对冻结版三个 CSS 逐行正则扫描（`font-size:<数字>px` / `line-height:<数字>` / `#hex`）：

| 扫描项 | 命中数 | 位置 / 说明 |
|---|---|---|
| `font-size:<数字>px` 字面量 | 1 | `app/web/app.css:769` — .dropzone__icon { font-size: 26px; color: var(--brand-600); margin-bottom: var(- |
| `line-height:<数字>` 字面量 | 2 | `app/web/app.css:243` — .topbar__names { display: flex; flex-direction: column; min-width: 0; line-heigh；`app/web/app.css:1167` — th[aria-sort="descending"] .th-sort::after { content: "▲"; font-size: var(--fs-x |
| `#hex` 颜色字面量（逐文件） | 90 | tokens.css 50 处、app.css 38 处、login.css 2 处 |

读法：`#hex` 字面量集中在 `tokens.css`（令牌定义文件本身必须是字面量），`app.css` / `login.css` 里的少量字面量是注释里的说明文字或 `data:image/svg+xml` 内联图形，不是规则值 —— 上面逐行列出位置可核对。

**结论的口径边界（重要）**：本轮 **严重项 2 / 阻断项 0** 只说明「本工具测到的这些检查项里没有严重项」，**不等于「全站符合 WCAG 2.1 AA」**，因为：

- §12 里列出的部分**没有被测**（屏幕阅读器实际朗读、Firefox / WebKit、zoom 200% 文本放大、闪烁）；
- 对比度判定用的是**计算样式 + 逐层求得的有效背景色**，不做抗锯齿像素采样，也不覆盖 hover / 展开态；
- 键盘走查覆盖的是「可聚焦元素能否 Tab 到 + 焦点环是否可见」，**不覆盖读屏的朗读顺序与语义体验**；
- 每个断点只测了 8 个页面各一次，动态内容（真实数据集大小、任务数量）不同会改变结果。

所以正确的读法是：**「在本报告的测量口径与覆盖面内，没有留下严重项」**；要拿到「符合 AA」的结论，还需要上面这些未覆盖项各测一轮。

## 9. 390×844 手机端核心操作实测（真实点击，不是静态扫描）

| 操作 | 结果 | 证据 |
|---|---|---|
| UI 登录（真实填表 + 点提交） | 落地 `http://127.0.0.1:8141/app#/workbench` | 提交按钮 disabled：True → False（填完用户名+密码才可提交） |
| 上传 CSV | 已上传：sales_sample.csv · 12 行 × 5 列 · dataset_id ad1a5ba262e74 | 选文件后状态：已选择：sales_sample.csv，点「上传」开始 |
| 建分析任务并等终态 | 状态 SUCCESS · 步骤 done（6/6） | 步骤 6 个、任务 1 条、已用时 0.1 秒 |
| 抽屉导航（点汉堡 → 点导航项） | 已覆盖 | 见 §9.1 |

### 9.1 移动端抽屉（非模态）

探针 `.tmp/a11y/probe_drawer.py` + `probe_toggle_stability.py` 的实测结论（选择器为实测路径）：

- 几何：关闭时 `aside#sidebar` 左移出屏（`x=-306, width=300, right=-6`），打开时 `x=0, right=300`；没有遮罩层（`overlays=[]`）、`contentInert=false`、`contentAriaHidden=null`、body 未锁滚动 → **非模态抽屉**。
- `button#nav-toggle` 是**确定性 toggle**：连续点击 `aria-expanded` false→true→false→true，页面里只有 1 个匹配元素。
- 开合状态与 `aria-expanded` 一致；Escape 能关闭且焦点回到 `#nav-toggle`（见 §11）。
- 点抽屉内的导航链接会关闭抽屉，但**焦点留在刚点的那个 `a.nav__item` 上**；该元素此刻已在屏幕外（`x=-306`），键盘用户看不到焦点环 —— 见 §8 与下面的问题清单。
- 抽屉打开时点右侧空白区域**不会关闭**（`aria-expanded` 仍为 `true`）；没有焦点陷阱（连按 12 次 Tab 有 5 次落在抽屉外），因此**不构成 WCAG 2.1.2 键盘陷阱**。

### 9.2 焦点环逐元素实测 @1440x900（键盘语境）

| 选择器 | 元素 | 实测焦点样式 | 可见性证据 |
|---|---|---|---|
| `#upload` | 上传按钮 | outline `2px solid rgb(61, 111, 174)` + box-shadow `none` | 聚焦前后像素变化 有；环对页底对比度 4.797:1 |
| `#file` | 文件输入 sr-only（上传的实际 Tab 停靠点） | outline `2px solid rgb(61, 111, 174)` + box-shadow `none` | 聚焦前后像素变化 有；环对页底对比度**未采到**（该元素没有可对比的页面底色样本） |
| `#ask` | 开始分析按钮 | outline `2px solid rgb(61, 111, 174)` + box-shadow `none` | 聚焦前后像素变化 有；环对页底对比度 4.797:1 |
| `#question` | 分析问题输入框 | outline `3px none rgb(46, 46, 43)` + box-shadow `rgb(230, 237, 247) 0px 0px 0px 3px` | 聚焦前后像素变化 有；环对页底对比度 12.705:1 |
| `#nav-toggle` | 抽屉开关（仅窄屏可见） | outline `2px solid rgb(61, 111, 174)` + box-shadow `none` | 聚焦前后像素变化 有；环对页底对比度 4.797:1 |
| `#sidebar a.nav__item` | 侧栏导航项 | outline `2px solid rgb(61, 111, 174)` + box-shadow `none` | 聚焦前后像素变化 有；环对页底对比度 4.797:1 |
| `#dropzone` | 拖拽上传区（label，不可聚焦） | 未覆盖 | 元素当前不可聚焦（disabled 或非表单控件），focus() 无效 |

说明：`#file` 是 sr-only（1×1px、`clip`）的隐藏 input，却是「上传」真正的 Tab 停靠点。它的焦点环由 `.dropzone:focus-within`（`app.css:716-720`）画在整块拖拽区上 —— 实测聚焦 `#file` 时拖拽区**像素确实发生变化**，所以键盘用户看得到，此项通过。

### 9.3 reduced-motion 探针复测

- 未开启 reduce：`matchMedia('(prefers-reduced-motion: reduce)')` = `False`；声明了 animation 的元素 20 个（登录页 1 个）。
- 开启 reduce：`matchMedia('(prefers-reduced-motion: reduce)')` = `True`；声明了 animation 的元素 51 个（登录页 0 个）。

## 10. 图表的替代文本（WCAG 1.1.1）与刷新对照

| 访问路径 | 图表 DOM | 证据 |
|---|---|---|
| 分析刚跑完（同一会话内，真实点击链路） | `#charts figure`=1、`img`=1 | alt=['按地区分组汇总销售额，从高到低取前 20 组']，图片是否真的加载成功=True |
| **先跳到报告详情 URL、再按 F5 硬刷新** | `#charts figure`=1、`img`=1 | alt=['按地区分组汇总销售额，从高到低取前 20 组']，图片是否真的加载成功=True（刷新前 URL=`http://127.0.0.1:8141/app#/report/6da2699caf71451aa29bc096fb24d1ad` → 刷新后 URL=`http://127.0.0.1:8141/app#/report/6da2699caf71451aa29bc096fb24d1ad`） |
| 直接打开报告详情链接 `#/report/{id}` | `#charts figure`=1、`img`=1 | alt=['按地区分组汇总销售额，从高到低取前 20 组']，图片是否真的加载成功=True |

后端对照（探针 `probe_mobile_flow.py` 直连接口）：`GET /api/workflows/6da2699caf71451aa29bc096fb24d1ad` → HTTP 200，统一信封 `ok`=True，`data` 里 `charts` 字段存在=True，条数=1，workflow `status`=SUCCESS、`has_report`=True。

后端返回的那张图：`chart_id`=7c6f35efe665、`filename`=7c6f35efe665.png、`title`=「按地区分组汇总销售额，从高到低取前 20 组」、`chart_type`=bar、`x_field`=地区、`y_field`=销售额_sum、`point_count`=6。

**本轮实测：三条路径都有图**（详情 URL 硬刷新 `figure=1`、直接打开 `figure=1`），后端也是 HTTP 200 + `charts` 字段存在=True、条数=1。也就是说「深链接 / 刷新后图表区为空」这个渲染路径问题**在当前被测代码上已经不复现**（此前读代码时确实只在轮询终态里渲染图表）。本报告只陈述本轮实测；若后续代码再改动这条路径，需要重跑本节。

结论边界：矩阵 48 组的 `images` 采集全是 0，**不是采集漏了**，而是矩阵访问 `#/report` 时该页处于空态；图表只有在「刚跑完一次分析」的会话里才存在。所以 WCAG 1.1.1 的结论只由上面这条真实链路支撑。

## 11. 产品修复的闭环对照（冻结前 vs 本轮）

| 观测项 | 冻结前基线 | 本轮（冻结版） |
|---|---|---|
| 导航分组小标题 `.nav__group`（12px/600） | rgb(115, 115, 110) 压在 rgb(247, 247, 246) → **4.446:1**（63 处样本） | rgb(111, 111, 106) 压在 rgb(247, 247, 246) → **4.712:1**（84 处样本） |
| 侧栏页脚文字 `.sidebar__foot`（12px/400） | rgb(92, 92, 88) 压在 rgb(247, 247, 246) → **6.265:1**（21 处样本） | rgb(92, 92, 88) 压在 rgb(247, 247, 246) → **6.265:1**（28 处样本） |
| 导航项角标 `.nav__flag`（12px/400） | rgb(115, 115, 110) 压在 rgb(247, 247, 246) → **4.446:1**（18 处样本） | rgb(111, 111, 106) 压在 rgb(247, 247, 246) → **4.712:1**（24 处样本） |
| `CONTRAST` 命中数（工具判定不达标的条数） | 57 | 0 |
| 窄屏抽屉 `#nav-toggle` 的 Escape（检查项 `ESC_NOT_CLOSE`） | 未测量（该轮工具版本还没有这项检查） | 0 |
| 登录失败的字段级标记（`aria-invalid`） | 基线轮工具没有这项检查；同轮实测行为见 §5.1（两个字段都没有 `aria-invalid`） | 本轮实测：两个字段**仍无** `aria-invalid`（错误只由 `#alert` 的 `role=alert` 播报） |

Escape「修复前」另有 `.tmp/a11y/run-check` 一轮（工具假阳性修复校验，3 页 × 390x844）实测 `ESC_NOT_CLOSE` major ×2（`button#nav-toggle`）作为直接反例；本轮该计数为 0，下面 §11.1 给出「按 Escape 之后 aria-expanded 与焦点去向」的逐条实测值。

### 11.1 ARIA 状态真实性（真实点击 → 复查）

| 触发控件 | 页面 @ 断点 | aria-expanded 前→后 | Esc 之后 | Esc 后焦点 | 焦点回到触发元素 |
|---|---|---|---|---|---|
| `button#toggle-password`（aria-pressed） | @1440x900（1 个页面，如 login） | false → true | 未测（该控件不涉及 Esc） | — | — |
| `button#user-btn`（aria-expanded） | @1440x900（7 个页面，如 workbench） | false → true | false | `button#user-btn` | 是 |
| `button#toggle-password`（aria-pressed） | @1280x800（1 个页面，如 login） | false → true | 未测（该控件不涉及 Esc） | — | — |
| `button#user-btn`（aria-expanded） | @1280x800（7 个页面，如 workbench） | false → true | false | `button#user-btn` | 是 |
| `button#toggle-password`（aria-pressed） | @1024x768（1 个页面，如 login） | false → true | 未测（该控件不涉及 Esc） | — | — |
| `button#user-btn`（aria-expanded） | @1024x768（7 个页面，如 workbench） | false → true | false | `button#user-btn` | 是 |
| `button#toggle-password`（aria-pressed） | @768x1024（1 个页面，如 login） | false → true | 未测（该控件不涉及 Esc） | — | — |
| `button#user-btn`（aria-expanded） | @768x1024（7 个页面，如 workbench） | false → true | false | `button#user-btn` | 是 |
| `button#toggle-password`（aria-pressed） | @390x844（1 个页面，如 login） | false → true | 未测（该控件不涉及 Esc） | — | — |
| `button#nav-toggle`（aria-expanded） | @390x844（7 个页面，如 workbench） | false → true | false | `button#nav-toggle` | 是 |
| `button#user-btn`（aria-expanded） | @390x844（7 个页面，如 workbench） | false → true | false | `button#user-btn` | 是 |
| `button#toggle-password`（aria-pressed） | @360x800（1 个页面，如 login） | false → true | 未测（该控件不涉及 Esc） | — | — |
| `button#nav-toggle`（aria-expanded） | @360x800（7 个页面，如 workbench） | false → true | false | `button#nav-toggle` | 是 |
| `button#user-btn`（aria-expanded） | @360x800（7 个页面，如 workbench） | false → true | false | `button#user-btn` | 是 |

### 11.2 工具自身的假阳性与修正记录（测量方法学，一并公开）

本报告的判定全部由 `.tmp/a11y/a11y_audit.py` 自己实现（没有引第三方规则引擎），所以**工具本身也是被审计对象**。下面每一条都是本 session 里真实发生过的误报，写明它为什么会误报、怎么发现的、修法是什么 —— 而不是把误报悄悄从数字里删掉。

| 误报的检查项 | 为什么会误报 | 怎么发现 / 独立证据 | 修法 |
|---|---|---|---|
| `KB_UNREACHABLE`（早期版本在登录页报出 10 条阻断） | 采集脚本为了量「能不能滚到」对元素调了 `scrollIntoView()`。Chromium 的**顺序焦点导航起点**会被滚动改掉，于是走查的第一次 Tab 直接落到页面**最后一个**可聚焦元素，第二次 Tab 回到 `body` 判定为「循环结束」——剩下的元素全部被标成「Tab 到不了」。表面现象就是「Tab 只走了 2 步」。 | 证据 `.tmp/a11y/probe_tabstart.py` 三组对照（同一页面，唯一变量是跑不跑那段采集 JS）：不跑 → `tab1=a#skip-link`；跑了 → `tab1=a.foot__link`；跑完等 1.5s 再 Tab → 又回到 `a#skip-link`。 | 那段采集**完全不做任何滚动**（连 `scrollTo` 都不调），「能不能滚到」改成纯计算（`元素高 ≤ 视口高 且 文档坐标底边 ≤ 文档总高`）。 |
| `SKIP_NOT_FIRST`（同一批的伴随误报） | skip link 有 `transition: transform var(--ds-dur-fast)`（120ms）。**聚焦瞬间**读 `getBoundingClientRect()` 会读到动画起点（在视口上方），等 ≥200ms 才是最终位置 —— 拿这个坐标判「谁在最前面」就会判错。 | design-research 独立复测（`.tmp/review/probe17_taborder__freeze.json`）指出并复现了这条时序陷阱：聚焦瞬间测得 `[12, -81.6, 110, 37.4]`，300ms 后才是 `y=12`。本 session 另用 `.tmp/a11y/probe_skiplink_timing.py` 在最终冻结版上独立复现（1440×900 与 390×844 两档结果相同）：聚焦前 `y=-81.59`（`transform=matrix(1,0,0,1,0,-93.59)`）→ 按第一次 Tab 的**瞬间**仍是 `y=-81.59` → 300ms 后才是 `y=12`、`transform=none`；而 outline 在聚焦瞬间就已经是 `2px solid rgb(61,111,174)`（原始数据 `.tmp/a11y/skiplink-timing.json`）。 | 本工具的「第一个可聚焦元素」按**标记出的 DOM 顺序**判定，不依赖即时坐标；几何量在聚焦前/稳定后读取。该检查项在最终冻结轮为 0 命中。 |
| `ELEM_OVERFLOW`（早期每轮 35 条） | 把「主动挪到屏幕外」的标准写法当成了溢出：skip link 的 `left:-9999px`、移动端抽屉收起时的 `translateX(-100%)`（`x=-306`）。 | 同一批数据的 `documentElement.scrollWidth == clientWidth`（没有横向滚动条），与「真的溢出」自相矛盾。 | 只有 `right > 视口宽 + 1` 才算 `ELEM_OVERFLOW`；`left < -1 且 right <= 1` 改报 `OFFSCREEN_LEFT_OK`（提示级，并注明本项通过）。 |
| `ARIA_PROBE_ERR`（早期每轮 7 条，只在 390×844） | 走查结束时焦点停在 skip link 上，而 `.skip-link:focus { left: 0 }` 会让它真的显示在左上角，**正好压住汉堡按钮 `#nav-toggle`**，于是点击 `#nav-toggle` 被拦截超时 —— 是测量顺序造成的，不是控件坏了。原始报错：`Locator.click: Timeout 3000ms exceeded ... a.skip-link intercepts pointer events`。 | 报错文本里直接点名了拦截者就是 skip link。 | ARIA 复查开始前先 `blur()` + 等 250ms，skip link 回到屏外；该检查项在冻结轮为 0 命中。 |
| `TARGET_LT24`（早期 84 条严重项） | 只按尺寸判定 WCAG 2.5.8，没有实现规范自带的 **Inline / Spacing 两条例外**，把「小但周围留白足够」的目标误判成不达标。 | 逐条看样本：`input#file` 是 sr-only 的 1×1 隐藏输入（真正目标是被它撑起的整块拖拽区）、`th-sort` 与 `.card__action` 中心 24px 圆内没有任何其它目标 —— 都属于例外情形。 | 工具 1.1.0 实现了两条例外：以目标中心画直径 24px 圆，不与任何其它目标相交即豁免；文本块内的小目标按 Inline 例外降级。**本报告里所有 `TARGET_LT24` 命中都落在这两条例外内**。 |
| `CONTRAST`（装饰性标点被判不达标） | `p#foot-meta > span.foot__dot`（分隔圆点「·」，`rgb(143,143,138)` 压白，**3.249:1**）被当成正文对比度问题。 | 该组样本全部是 ≤2 字符的非字母数字标点，且该 span 是纯装饰。 | 全部样本都是装饰性短标点时降为「轻微」并注明；WCAG 1.4.3 不适用于纯装饰元素，但报告里仍给出数值与建议改法。 |
| `NO_NAV`（登录页被判缺少站内导航） | 登录页只有 2 个站内链接，被「站内链接 < 3 个」的规则命中。 | 该页是单任务页（登录），本来就没有站点导航。 | 站内链接 < 3 个时降为提示级，并写明「单任务页，不构成实际问题」。 |
| 后端 500（`/api/workflows/history`）曾被当成产品缺陷 | `HistoryStore.list_recent() missing 1 required keyword-only argument: 'owner_user_id'` —— **这是没加 `--reload` 的旧 uvicorn 进程**在跑旧代码，磁盘上的代码早已传了该参数。 | 重启隔离服务后同一请求返回 200；矩阵里的「读取失败 / 服务器内部错误」错误态文案同时从 28/32 组降到 0 组。 | 所有测量轮改为**重启服务后再跑**；报告里每一轮都标注服务启动时间与被测代码状态。 |

### 11.3 触控目标与字面量：改动前 → 改动后（真实数字）

| 检查项 | 改动前（上一轮冻结版（13:25 七文件哈希，服务启动 13:36）） | 改动后（本轮） |
|---|---|---|
| `TARGET_LT44`（<44×44，项目 T1 建议级） | 77 | 77 |
| `TARGET_LT24`（<24×24，WCAG 2.2 AA 门槛，已扣 Inline/Spacing 例外） | 36 | 36 |
| `CONTRAST` | 0 | 0 |
| `PRIMARY_BELOW_FOLD`（首屏主按钮） | 2 | 2 |
| 发现总数 | 195 | 189 |

关键控件实测尺寸（各自取「该轮全部运行里出现最多的那个尺寸」）：

| 控件 | 改动前（上一轮冻结版（13:25 七文件哈希，服务启动 13:36）） | 改动后（本轮） |
|---|---|---|
| `button#nav-toggle`（窄屏抽屉开关） | 44×44px（14 组） | 44×44px（14 组） |
| `button#user-btn`（顶栏用户菜单） | 140.94×44px（28 组） | 140.94×44px（28 组） |
| `a.topbar__link`（顶栏「接口文档」） | 70.64×44px（28 组） | 70.64×44px（28 组） |
| `aside#sidebar a.nav__item`（侧栏导航项） | 211×44px（84 组） | 211×44px（84 组） |
| `button.th-sort`（表头排序） | 34×25.44px（8 组） | 34×25.44px（8 组） |
| `button#upload`（工作台主按钮） | 79.64×44px（6 组） | 79.64×44px（6 组） |
| `button#ask`（开始分析） | 该轮未出现 | 本轮未出现 |
| `div.sidebar__foot > a`（健康检查链接） | 115.19×24px（42 组） | 115.19×24px（42 组） |
| `a.foot__link`（页脚行内链接） | 48×19.8px（6 组） | 48×19.8px（6 组） |

读法：严格说这两轮不是同一个工具版本采的（见 §1.1 的版本提醒），所以**只比较同一控件的实测尺寸**，不比较总数；尺寸字段两轮都是 `getBoundingClientRect()` 原始值，口径一致。

## 12. 未覆盖 / 不能下结论的部分（如实列出）

1. **浏览器矩阵只有 Chromium**（Playwright 自带的那一版）：没有跑 Firefox / WebKit，也没有跑真实 Safari、iOS VoiceOver、NVDA / JAWS 屏幕阅读器。本报告里所有「通过/不通过」都是**计算样式 + 真实键盘事件 + 像素比对**得出的，不等于屏幕阅读器的实际朗读效果。
2. **图表替代文本实测覆盖 3/3 条路径**（见 §10）：「同一会话刚跑完分析」`#charts figure=1`；「在报告详情 URL 上硬刷新」`#charts figure=1`；「直接打开 `#/report/{id}`」`#charts figure=1`。
   实测到的图表元素：`div#charts > figure` → `div#charts > figure > img`，`alt="按地区分组汇总销售额，从高到低取前 20 组"`（有 alt 属性=True），图片真的加载了=True（naturalWidth=960），`figcaption`「按地区分组汇总销售额，从高到低取前 20 组柱状图本图 6 个数据点横轴 地区纵轴 销售额_sum来自任务 T1图中数值」。
   三条路径都拿到了非空 `alt`，说明「图表替代文本」这件事在本轮被测代码上**三条路径都成立**（深链接 / 硬刷新路径此前读代码判断会丢图，本轮实测已被上游修好）。
   **仍未覆盖**的是：屏幕阅读器实际会怎么朗读这条 `alt`（本报告没有跑 NVDA / VoiceOver，只有 DOM 属性层面的事实）。
   **修正说明（测量设计）**：本探针早先版本的「硬刷新」直接 `page.reload()` 当前页，而分析跑完后 SPA 停在 `#/report`（报告中心列表页，本来就没有图表）——于是把「列表页没有图」误读成了「刷新丢图」。现已改为先显式跳到 `#/report/{id}`再刷新，并在结果里同时记录刷新前后的 URL（`urlBeforeReload` / `urlAfterReload`）。本轮的三个数是修正后测法得到的。
3. **错误态**：本轮 48 组运行里没有采到「读取失败 / 服务器内部错误」文案，因此各页面的 a11y 结论覆盖的是正常态渲染层。作为对照，早期确实落在错误态的两轮（`.tmp/a11y/run-mid`、`.tmp/a11y/run-final`）**测到过**错误态配色 —— 红字 `p#dataset-picker-state` `rgb(155, 44, 34)` 压白 = **7.562:1**、空/错误文案 `p.empty.empty--inline` `rgb(92, 92, 88)` 压 `rgb(251, 251, 250)` = **6.486:1**，都达标；但那是旧代码轮的数据，只能作参考、不能当本轮的结论。
4. **没有做工具层面的对比度图像采样**：判定用的是计算样式里的前景色与逐层求得的有效背景色，不做抗锯齿像素采样；半透明叠色（如 hover 态）只覆盖到静态可测的那部分。
5. **没有做 zoom 200% / 文本放大 200% 的 reflow 测试**（WCAG 1.4.4 / 1.4.10 的另一半）：T2 只验证了各档视口宽度下的横向滚动，没有模拟用户放大字体。
6. **动画/动效只覆盖 prefers-reduced-motion**：没有测闪烁（2.3.1）、没有做自动播放内容的暂停控制检查。
7. **没有新增第三方依赖**：没有引入 axe-core / pa11y 之类的成熟规则引擎做交叉验证，所以报告里的判定口径完全由 `.tmp/a11y/a11y_audit.py` 自己实现 —— 工具的规则本身也是被审计对象（本 session 共修掉 **8 类工具假阳性**，另有 1 类环境误读，逐条的「为什么误报 / 怎么发现 / 怎么修」见 §11.2）：`KB_UNREACHABLE`（采集时滚动挪走了顺序焦点导航起点）、`SKIP_NOT_FIRST`（120ms 位移动画期间读坐标）、`ELEM_OVERFLOW`（误报主动离屏的 skip link 与抽屉）、`ARIA_PROBE_ERR`（skip link 聚焦后遮挡汉堡按钮）、`TARGET_LT24`（未实现 WCAG 2.5.8 的 Inline/Spacing 例外）、`CONTRAST`（装饰性标点）、`NO_NAV`（单任务页）、`TRUNC_NO_HINT`（sr-only 元素被当成文字截断）；环境误读是「把旧 uvicorn 进程的 500 当成产品缺陷」。
8. **目标尺寸的判定口径**：`TARGET_LT44` 是「项目 T1 的 44×44 建议级」门槛，**不是 WCAG AA 失败项**（AA 对应的 2.5.8 门槛是 24×24，且带 Inline / Spacing 例外）——逐条裁定见 §8.1；因此本报告里 `TARGET_LT44` 的条数（含 `.sidebar__foot` 链接、`#skip-link`、表头排序按钮、`input#username/#password` 的 42px 高等）不能读成「违反 WCAG 2.1 AA」。
9. **版本口径（最容易误读的一条）**：第 1~14 节的数字对应 `app/web/app.css` `8730c1d495686269…`。这份报告之后该文件被改成 `7980b2fa0ff9b023…`（task-24；该改动块 = **不含媒体查询的基础规则（在所有宽度都生效）**+ **4 组媒体查询（按断点生效）**——所以不能说成「只有 ≤1080px 才受影响」，1440×900 下也有实测变化（前端复核 `docs/ui-review.md` §12.8 的测量口径，与本节我们自己量的绝对值不同）：`#ask` 底边 781.66 → 687.13、`scrollH` 1344 → 1221）——**这次改动没有被第 1~14 节覆盖**，只有 §15/§16/§17 的复核轮涉及它；其余 6 个前端文件在两轮之间字节一致。引用结论前请先核对哈希。 ⚠️ 同一断点下不同工具量出的绝对值可能不同（本报告 §15 在 1440×900 下量到 `#ask` 底边 708.56），**两组数字不要互相换算或混引**，只看各自报告内部的趋势。 同一现象在本仓库共出现**三套读数，来自三套不同装置**：① 本报告复核轮（`a11y_audit.py`，Playwright 现场 `getBoundingClientRect()`）量到 708.56；② 前端复核 `docs/ui-review.md` §12.8（`collect_pages.py` 的 `METRICS` 口径）量到 687.13；③ 本报告主轮（**同一个 `a11y_audit.py`，但被测代码是 task-24 之前**）量到 781.66。 差异来自**装置不同 / 被测代码版本不同，不是页面在变**。 **方法学规则：同一现象出现多装置读数不一致时，定稿只采用一套作为数字，其余必须标注口径，不得并列互相换算或混引。**

## 13. 怎么复现这份报告

前置：服务用**隔离数据目录**跑（绝不碰 `data/db/auth.db`）：

```powershell
$root = "<项目根>"
$env:AUTH_DB_PATH = "$root\.tmp\a11y\data\auth.db"
$env:DB_PATH      = "$root\.tmp\a11y\data\app.db"
$env:UPLOAD_DIR   = "$root\.tmp\a11y\data\uploads"
$env:CHARTS_DIR   = "$root\.tmp\a11y\data\charts"
$env:MPLCONFIGDIR = "$root\.tmp\mplcache"
& "$root\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8141
```

跑矩阵（带指纹核对，跑完能证明「测的是哪一版」）：

```powershell
$env:TEMP = "$root\.tmp\a11y\tmp"; $env:TMP = $env:TEMP
& "$root\.venv\Scripts\python.exe" ".tmp\a11y\run_audit.py" `
    --out .tmp/a11y/run-final4 --stage "主轮 = lead 复核的冻结版（服务 2026-10-16 13:36:33 启动，在文件写入之后；工具 a11y_audit.py 1.2.1；指纹稳定）。其后 task-24 改过 app.css（7980B2FA…）、task-25 改过 index.html/app.js —— 见 §0.1、§15、§16" `
    --pages login,workbench,dataset,analysis,report,report-detail,history,settings `
    --viewports 1440x900,1280x800,1024x768,768x1024,390x844,360x800 --seed
```

（`--seed` 会先用 API 造一份数据集并跑完一次分析，否则报告详情页只有空态可测；`run_audit.py` 的退出码：`0`=矩阵成功且指纹稳定、`3`=跑完但测量期间前端文件被改动（那一轮只能当中间态，不能作为定稿结论）。）

跑 390×844 手机端真实操作探针（图表 alt / 刷新对照 / 焦点迁移 / 焦点环 / 抽屉）：

```powershell
& "$root\.venv\Scripts\python.exe" ".tmp\a11y\probe_mobile_flow.py" `
    --port 8141 --user a11y_admin --password "A11y-Audit-2026!" `
    --wait-workflow 150 --out ".tmp\a11y\run-mobile-final"
```

两个方法学小探针（用来证明工具假阳性，不是产品缺陷）：

```powershell
# 顺序焦点导航起点：跑不跑滚动采集，第一次 Tab 落点是否改变
& "$root\.venv\Scripts\python.exe" ".tmp\a11y\probe_tabstart.py" --port 8141
# skip link 的 120ms 位移动画：聚焦瞬间读坐标 vs 等 300ms 再读
& "$root\.venv\Scripts\python.exe" ".tmp\a11y\probe_skiplink_timing.py" --port 8141 `
    --out ".tmp\a11y\skiplink-timing.json"
```

跑**首次部署（初始化）态**的首屏可见性实测（需要另起一个空账号库的服务，端口 8142）：

```powershell
$env:AUTH_DB_PATH = "$root\.tmp\a11y\data-bootstrap\auth.db"   # 全新、空账号库
$env:DB_PATH      = "$root\.tmp\a11y\data-bootstrap\app.db"
& "$root\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8142
# 另开一个终端：
& "$root\.venv\Scripts\python.exe" ".tmp\a11y\probe_bootstrap.py" --port 8142 `
    --out ".tmp\a11y\bootstrap.json"
```

重新生成本报告：

```powershell
& "$root\.venv\Scripts\python.exe" ".tmp\a11y\gen_final_report.py" `
    --matrix .tmp/a11y/run-final4 --baseline .tmp/a11y/run-mid `
    --baseline2 .tmp/a11y/run-final3 `
    --mobile .tmp/a11y/run-mobile-final2 --bootstrap .tmp/a11y/bootstrap.json `
    --matrix2 .tmp/a11y/run-final5 --matrix2-label "复核轮（中间态）" `
    --matrix2-stage "task-24 之后、task-25 之前（app.css 7980b2fa…）：测量窗口内 app.js/index.html 仍在被改，混用两版文件" `
    --matrix3 .tmp/a11y/run-final6 --matrix3-label "复核轮（中间态·第二次）" `
    --matrix3-stage "task-25 改动正在落盘时（index.html 测量中途再次变化）：仍属中间态，不得当合格轮引用" `
    --matrix4 .tmp/a11y/run-final7 --matrix4-label "合格复核轮（定稿轮）" `
    --matrix4-stage "定稿轮 = lead 2026-10-16 15:2x 宣布的冻结集（7 文件哈希跑前/跑中/跑后三次拍摄一致）；服务 2026-10-16 15:17:14 启动，晚于全部前端文件写入（最晚 login.html/login.js 15:06:26）；工具 a11y_audit.py 1.2.1" `
    --mobile4 .tmp/a11y/run-mobile-final7 --bootstrap4 .tmp/a11y/bootstrap17.json `
    --hashes-pre .tmp/a11y/hashes-pre17.json --hashes-mid .tmp/a11y/hashes-mid17.json --hashes-post .tmp/a11y/hashes-post17.json `
    --d6-baseline .tmp/a11y/tmp/d6-dom-143929-keep.json --d6-frozen .tmp/a11y/d6-dom.json `
    --out .tmp/a11y/final-report.md --stage "主轮 = lead 复核的冻结版（服务 2026-10-16 13:36:33 启动，在文件写入之后；工具 a11y_audit.py 1.2.1；指纹稳定）。其后 task-24 改过 app.css（7980B2FA…）、task-25 改过 index.html/app.js —— 见 §0.1、§15、§16" --server-start "2026-10-16T13:36:33"
```

## 14. 复核清单（lead 点名的 6 条，逐条给结论与证据）

这 6 条是 lead 在下达最终冻结时点名要独立复核的（他的判断来自代码走查与 grep），下面每条的结论都来自本轮真实测量或对冻结文件的逐行扫描，**与他的预期不一致的地方如实标出**。

| 复核项（lead 的说法） | 本轮结论 | 证据 / 实测值 |
|---|---|---|
| ① 4 处硬编码 `--n-500` → `--ds-color-ink-subtle`（他给的预期 5.05:1） | **已修复并达标** | `rgb(111, 111, 106)` 压 `rgb(247, 247, 246)` → **4.712:1**（108 处样本）；`rgb(111, 111, 106)` 压 `rgb(255, 255, 255)` → **5.051:1**（54 处样本）；`rgb(111, 111, 106)` 压 `rgb(244, 247, 251)` → **4.701:1**（6 处样本）。他给的 5.05:1 是**白底**上的值；应用画布底是 `rgb(247,247,246)`，在该底色上是 4.712:1 —— 两者都 ≥ AA 的 4.5:1，结论一致 |
| ② 触控目标统一 44px 地板、`.th-sort` 24×24（`app.css:1231`）、≤760px 走 `--ds-touch-min`（`app.css:1252`） | **已生效** | `button#nav-toggle` 44×44px；`button#user-btn` 140.94×44px；`a.topbar__link` 70.64×44px；`button.th-sort` 34×25.44px；`button#upload` 79.64×44px；`TARGET_LT44` 77 条、`TARGET_LT24` 36 条（后者全部落在 2.5.8 例外内） |
| ③「两个 CSS 里 `font-size:<数字>px` 字面量已 0 处」（他的 grep 结论） | **与实测不一致：还有 1 处** | `app/web/app.css:769` — .dropzone__icon { font-size: 26px; color: var(--brand-600); margin-bottom: var(-。这是**图标字形的尺寸**（不是正文/标题字号），不影响字号令牌化结论，但「0 处」这个说法按本轮逐行扫描不成立 |
| ④ 两个页面都加了空 favicon（抑制 `/favicon.ico` 的 404） | **已生效** | `app/web/index.html` L10；`app/web/login.html` L11 |
| ⑤ 390×844 下 `#ask` 底边 842.27、只剩 1.73px 余量（要求重新实测并如实记录） | **复现，且余量确实极小** | `#ask` y=798.27 / 底边 842.27 / 视口高 844 → 余量 **1.73px**（完整在首屏内）；同一按钮在 360×800 档底边越界（见 §3.1） |
| ⑥ `fmtClock` 已改为 `new Date(text)` + 本地 `getHours/getMinutes`（`app.js:2082-2089`） | **已生效** | `app/web/app.js:2086` 定义；函数体用 `date.getHours()/getMinutes()`（本地时间）；**未见**从 ISO 串直接截时分的 `slice(11,16)` 写法（行号随 task-25 的文案收口从 lead 复核时的 2082 位移到 2086；这里扫的是**定稿版 app.js**，不是主轮那一版） |

说明：① 与 ⑥ 属前端渲染层，本轮矩阵直接覆盖；② 与 ③ 的结论只依赖冻结文件本身与矩阵采集；④ 是静态文件扫描（不在矩阵的检查项里，因此**没有**进入 §2 的发现数）；⑤ 已在 §3.1 逐断点列出。

## 15. 复核轮（中间态）（主轮之后前端被改动）：逐页对照

主轮跑的是 `.tmp/a11y/run-final4`（第 1~14 节）；本节是 `.tmp/a11y/run-final5`（task-24 之后、task-25 之前（app.css 7980b2fa…）：测量窗口内 app.js/index.html 仍在被改，混用两版文件）。**两轮数字并列保留，旧轮不被覆盖** —— 这样才能看出「后来又改的那几行」到底影响了什么。

> ⚠️ **本节这一轮的指纹不稳定：测量期间有文件被改动**（`app/web/index.html: aca8a8b41299 -> 2dedaeaf2dc2`、`app/web/app.js: 91dcf193adec -> e7f37a40c584`）。也就是说这一轮**同时混用了改动前后的文件**，**不能作为定稿结论**，只能当自查参考。要下结论必须等下一次冻结后重跑一轮（跑前跑后指纹都稳定）。

| 项目 | 主轮 | 复核轮（中间态） |
|---|---|---|
| 矩阵目录 | `.tmp/a11y/run-final4` | `.tmp/a11y/run-final5` |
| 工具版本 | a11y_audit.py 1.2.1 | a11y_audit.py 1.2.1 |
| 生成时刻 | 2026-10-16 13:49:07 +0800 | 2026-10-16 14:19:12 +0800 |
| 组数（成功/失败） | 48 / 0 | 48 / 0 |
| 测量期间指纹 | 稳定 | **不稳定** |
| 总条数（严重/轻微/提示） | 189（2/125/62） | 187（0/125/62） |

- **其余 7 个页面（除分析页）的发现集合对照**：主轮 173 条唯一组合、复核轮（中间态） 173 条；只在主轮出现 0 条、只在复核轮（中间态）出现 0 条。
  → **逐条完全一致**，实测支持「这次改动没有波及分析页以外的页面」的判断（比对的是真实渲染结果，不只看 CSS 选择器前缀）。

- **分析页自身的发现集合**：主轮 16 条唯一组合、复核轮（中间态） 14 条；只在主轮 2 条、只在复核轮（中间态） 0 条。
  - 仅在主轮：`1024x768` `PRIMARY_BELOW_FOLD` `button#ask`（major）
  - 仅在主轮：`360x800` `PRIMARY_BELOW_FOLD` `button#ask`（major）

**分析页主按钮 `#ask` 的逐断点对照**（y / 底边 / 视口高，单位 px）：

| 断点 | 主轮 | 复核轮（中间态） | 首屏判定 | 余量（主轮 → 复核轮（中间态）） |
|---|---|---|---|---|
| 1440x900 | 737.66 / 781.66 / 900 | 664.56 / 708.56 / 900 | 首屏内 → 首屏内 | 118.34 → 191.44 |
| 1280x800 | 737.66 / 781.66 / 800 | 664.56 / 708.56 / 800 | 首屏内 → 首屏内 | 18.34 → 91.44 |
| 1024x768 | 733.66 / 777.66 / 768 | 632.56 / 676.56 / 768 | **越界** → 首屏内 | -9.66 → 91.44 |
| 768x1024 | 812.75 / 856.75 / 1024 | 711.66 / 755.66 / 1024 | 首屏内 → 首屏内 | 167.25 → 268.34 |
| 390x844 | 798.27 / 842.27 / 844 | 654.08 / 698.08 / 844 | 首屏内 → 首屏内 | 1.73 → 145.92 |
| 360x800 | 821.36 / 865.36 / 800 | 654.08 / 698.08 / 800 | **越界** → 首屏内 | -65.36 → 101.92 |

**两轮共同成立的硬指标**（逐轮独立测量）：

| 硬指标 | 主轮 | 复核轮（中间态） |
|---|---|---|
| 横向滚动条（T2） | **48/48 组无横向滚动** | **48/48 组无横向滚动** |
| 键盘不可达元素 | 未到达 **0** 个 / 无焦点环 **0** 个 | 未到达 **0** 个 / 无焦点环 **0** 个 |
| reduced-motion 通过页数 | **8/8** 页通过（running/infinite 动画均为 0） | **8/8** 页通过（running/infinite 动画均为 0） |

**按检查代码对照**（只列两轮中至少一轮非零的项）：

| 检查代码 | 主轮 | 复核轮（中间态） |
|---|---|---|
| `HEADING_SKIP` | 12 | 12 |
| `NO_NAV` | 6 | 6 |
| `OFFSCREEN_LEFT_OK` | 56 | 56 |
| `PRIMARY_BELOW_FOLD` | 2 | 0 |
| `TARGET_LT24` | 36 | 36 |
| `TARGET_LT44` | 77 | 77 |

## 16. 复核轮（中间态·第二次）（主轮之后前端被改动）：逐页对照

主轮跑的是 `.tmp/a11y/run-final4`（第 1~14 节）；本节是 `.tmp/a11y/run-final6`（task-25 改动正在落盘时（index.html 测量中途再次变化）：仍属中间态，不得当合格轮引用）。**两轮数字并列保留，旧轮不被覆盖** —— 这样才能看出「后来又改的那几行」到底影响了什么。

> ⚠️ **本节这一轮的指纹不稳定：测量期间有文件被改动**（`app/web/index.html: 2dedaeaf2dc2 -> 63dc15f13548`）。也就是说这一轮**同时混用了改动前后的文件**，**不能作为定稿结论**，只能当自查参考。要下结论必须等下一次冻结后重跑一轮（跑前跑后指纹都稳定）。

| 项目 | 主轮 | 复核轮（中间态·第二次） |
|---|---|---|
| 矩阵目录 | `.tmp/a11y/run-final4` | `.tmp/a11y/run-final6` |
| 工具版本 | a11y_audit.py 1.2.1 | a11y_audit.py 1.2.1 |
| 生成时刻 | 2026-10-16 13:49:07 +0800 | 2026-10-16 14:27:06 +0800 |
| 组数（成功/失败） | 48 / 0 | 48 / 0 |
| 测量期间指纹 | 稳定 | **不稳定** |
| 总条数（严重/轻微/提示） | 189（2/125/62） | 187（0/125/62） |

- **其余 7 个页面（除分析页）的发现集合对照**：主轮 173 条唯一组合、复核轮（中间态·第二次） 173 条；只在主轮出现 0 条、只在复核轮（中间态·第二次）出现 0 条。
  → **逐条完全一致**，实测支持「这次改动没有波及分析页以外的页面」的判断（比对的是真实渲染结果，不只看 CSS 选择器前缀）。

- **分析页自身的发现集合**：主轮 16 条唯一组合、复核轮（中间态·第二次） 14 条；只在主轮 2 条、只在复核轮（中间态·第二次） 0 条。
  - 仅在主轮：`1024x768` `PRIMARY_BELOW_FOLD` `button#ask`（major）
  - 仅在主轮：`360x800` `PRIMARY_BELOW_FOLD` `button#ask`（major）

**分析页主按钮 `#ask` 的逐断点对照**（y / 底边 / 视口高，单位 px）：

| 断点 | 主轮 | 复核轮（中间态·第二次） | 首屏判定 | 余量（主轮 → 复核轮（中间态·第二次）） |
|---|---|---|---|---|
| 1440x900 | 737.66 / 781.66 / 900 | 664.56 / 708.56 / 900 | 首屏内 → 首屏内 | 118.34 → 191.44 |
| 1280x800 | 737.66 / 781.66 / 800 | 664.56 / 708.56 / 800 | 首屏内 → 首屏内 | 18.34 → 91.44 |
| 1024x768 | 733.66 / 777.66 / 768 | 632.56 / 676.56 / 768 | **越界** → 首屏内 | -9.66 → 91.44 |
| 768x1024 | 812.75 / 856.75 / 1024 | 711.66 / 755.66 / 1024 | 首屏内 → 首屏内 | 167.25 → 268.34 |
| 390x844 | 798.27 / 842.27 / 844 | 654.08 / 698.08 / 844 | 首屏内 → 首屏内 | 1.73 → 145.92 |
| 360x800 | 821.36 / 865.36 / 800 | 654.08 / 698.08 / 800 | **越界** → 首屏内 | -65.36 → 101.92 |

**两轮共同成立的硬指标**（逐轮独立测量）：

| 硬指标 | 主轮 | 复核轮（中间态·第二次） |
|---|---|---|
| 横向滚动条（T2） | **48/48 组无横向滚动** | **48/48 组无横向滚动** |
| 键盘不可达元素 | 未到达 **0** 个 / 无焦点环 **0** 个 | 未到达 **0** 个 / 无焦点环 **0** 个 |
| reduced-motion 通过页数 | **8/8** 页通过（running/infinite 动画均为 0） | **8/8** 页通过（running/infinite 动画均为 0） |

**按检查代码对照**（只列两轮中至少一轮非零的项）：

| 检查代码 | 主轮 | 复核轮（中间态·第二次） |
|---|---|---|
| `HEADING_SKIP` | 12 | 12 |
| `NO_NAV` | 6 | 6 |
| `OFFSCREEN_LEFT_OK` | 56 | 56 |
| `PRIMARY_BELOW_FOLD` | 2 | 0 |
| `TARGET_LT24` | 36 | 36 |
| `TARGET_LT44` | 77 | 77 |

## 17. 合格复核轮（定稿轮）（主轮之后前端被改动）：逐页对照

主轮跑的是 `.tmp/a11y/run-final4`（第 1~14 节）；本节是 `.tmp/a11y/run-final7`（定稿轮 = lead 2026-10-16 15:2x 宣布的冻结集（7 文件哈希跑前/跑中/跑后三次拍摄一致）；服务 2026-10-16 15:17:14 启动，晚于全部前端文件写入（最晚 login.html/login.js 15:06:26）；工具 a11y_audit.py 1.2.1）。**两轮数字并列保留，旧轮不被覆盖** —— 这样才能看出「后来又改的那几行」到底影响了什么。

> ✅ **本节这一轮的指纹稳定**（跑前跑后 7 个前端文件字节一致）：这一轮是**合格复核轮**，它的数字可以作为「主轮之后那一版」的结论使用；与主轮的差异都应视为这两次改动造成的真实变化。

| 项目 | 主轮 | 合格复核轮（定稿轮） |
|---|---|---|
| 矩阵目录 | `.tmp/a11y/run-final4` | `.tmp/a11y/run-final7` |
| 工具版本 | a11y_audit.py 1.2.1 | a11y_audit.py 1.2.1 |
| 生成时刻 | 2026-10-16 13:49:07 +0800 | 2026-10-16 15:17:19 +0800 |
| 组数（成功/失败） | 48 / 0 | 48 / 0 |
| 测量期间指纹 | 稳定 | 稳定 |
| 总条数（严重/轻微/提示） | 189（2/125/62） | 187（0/125/62） |

- **其余 7 个页面（除分析页）的发现集合对照**：主轮 173 条唯一组合、合格复核轮（定稿轮） 173 条；只在主轮出现 0 条、只在合格复核轮（定稿轮）出现 0 条。
  → **逐条完全一致**，实测支持「这次改动没有波及分析页以外的页面」的判断（比对的是真实渲染结果，不只看 CSS 选择器前缀）。

- **分析页自身的发现集合**：主轮 16 条唯一组合、合格复核轮（定稿轮） 14 条；只在主轮 2 条、只在合格复核轮（定稿轮） 0 条。
  - 仅在主轮：`1024x768` `PRIMARY_BELOW_FOLD` `button#ask`（major）
  - 仅在主轮：`360x800` `PRIMARY_BELOW_FOLD` `button#ask`（major）

**分析页主按钮 `#ask` 的逐断点对照**（y / 底边 / 视口高，单位 px）：

| 断点 | 主轮 | 合格复核轮（定稿轮） | 首屏判定 | 余量（主轮 → 合格复核轮（定稿轮）） |
|---|---|---|---|---|
| 1440x900 | 737.66 / 781.66 / 900 | 664.56 / 708.56 / 900 | 首屏内 → 首屏内 | 118.34 → 191.44 |
| 1280x800 | 737.66 / 781.66 / 800 | 664.56 / 708.56 / 800 | 首屏内 → 首屏内 | 18.34 → 91.44 |
| 1024x768 | 733.66 / 777.66 / 768 | 632.56 / 676.56 / 768 | **越界** → 首屏内 | -9.66 → 91.44 |
| 768x1024 | 812.75 / 856.75 / 1024 | 711.66 / 755.66 / 1024 | 首屏内 → 首屏内 | 167.25 → 268.34 |
| 390x844 | 798.27 / 842.27 / 844 | 654.08 / 698.08 / 844 | 首屏内 → 首屏内 | 1.73 → 145.92 |
| 360x800 | 821.36 / 865.36 / 800 | 654.08 / 698.08 / 800 | **越界** → 首屏内 | -65.36 → 101.92 |

**两轮共同成立的硬指标**（逐轮独立测量）：

| 硬指标 | 主轮 | 合格复核轮（定稿轮） |
|---|---|---|
| 横向滚动条（T2） | **48/48 组无横向滚动** | **48/48 组无横向滚动** |
| 键盘不可达元素 | 未到达 **0** 个 / 无焦点环 **0** 个 | 未到达 **0** 个 / 无焦点环 **0** 个 |
| reduced-motion 通过页数 | **8/8** 页通过（running/infinite 动画均为 0） | **8/8** 页通过（running/infinite 动画均为 0） |

**按检查代码对照**（只列两轮中至少一轮非零的项）：

| 检查代码 | 主轮 | 合格复核轮（定稿轮） |
|---|---|---|
| `HEADING_SKIP` | 12 | 12 |
| `NO_NAV` | 6 | 6 |
| `OFFSCREEN_LEFT_OK` | 56 | 56 |
| `PRIMARY_BELOW_FOLD` | 2 | 0 |
| `TARGET_LT24` | 36 | 36 |
| `TARGET_LT44` | 77 | 77 |


**冻结集哈希：4 次独立拍摄的时间戳表**（装置 = `.tmp/a11y/snapshot_hashes.py` 自己算的 SHA-256，与 `run_audit.py` 自带指纹是两套独立实现，互为交叉校验）：

| 文件 | 跑前（2026-10-16 15:16:48） | 跑中（2026-10-16 15:19:13） | 跑后（2026-10-16 15:22:18） | 交付前（2026-10-16 15:30:26） |
|---|---|---|---|---|
| `app/web/index.html` | 804843F5AE6AEBBF / 39258 B | 804843F5AE6AEBBF / 39258 B | 804843F5AE6AEBBF / 39258 B | 804843F5AE6AEBBF / 39258 B |
| `app/web/app.css` | 7980B2FA0FF9B023 / 59100 B | 7980B2FA0FF9B023 / 59100 B | 7980B2FA0FF9B023 / 59100 B | 7980B2FA0FF9B023 / 59100 B |
| `app/web/app.js` | 124E01F699B616B6 / 133331 B | 124E01F699B616B6 / 133331 B | 124E01F699B616B6 / 133331 B | 124E01F699B616B6 / 133331 B |
| `app/web/tokens.css` | 4DE3E276F42731D9 / 21620 B | 4DE3E276F42731D9 / 21620 B | 4DE3E276F42731D9 / 21620 B | 4DE3E276F42731D9 / 21620 B |
| `app/web/login.html` | FDFB5A7F8F04D39E / 20753 B | FDFB5A7F8F04D39E / 20753 B | FDFB5A7F8F04D39E / 20753 B | FDFB5A7F8F04D39E / 20753 B |
| `app/web/login.css` | F5A2F2C3BCAC5799 / 34002 B | F5A2F2C3BCAC5799 / 34002 B | F5A2F2C3BCAC5799 / 34002 B | F5A2F2C3BCAC5799 / 34002 B |
| `app/web/login.js` | BB1C1E376E9EF030 / 47791 B | BB1C1E376E9EF030 / 47791 B | BB1C1E376E9EF030 / 47791 B | BB1C1E376E9EF030 / 47791 B |

- 4 次拍摄与 lead 的冻结集逐字节一致：**True**（每次 `all_match=true`、`mismatched=[]`）
- 四次拍摄跨越整个测量窗口（含矩阵运行期间与交付前各一次）：2026-10-16 15:16:48 + 2026-10-16 15:19:13 + 2026-10-16 15:22:18 + 2026-10-16 15:30:26 全部一致 → **「测量期间有人在写前端」这个坑被直接排除**；配合 `run_audit.py` 的两端指纹，共同构成「冻结—测量—交付」三段都不变的证据。
- `run_audit.py` 自带指纹（`fingerprint.json`）：`stable=True`、`changed_during_run=[]`、`matrix_exit_code=0`、耗时 `293.4s`。


**为什么两条严重项会消失：分析页整页高度（`scrollH` / 需滚动 `overflowPx`）对照**（装置与 §17 其余数字同一轮采集；若高度没变而只有首屏判定变了，那才是测量差异）：

| 断点 | 主轮（非定稿） | §17（定稿） | scrollH 差 |
|---|---|---|---|
| 1440x900 | 1344 / 444 | 1243 / 343 | -101 |
| 1280x800 | 1344 / 544 | 1243 / 443 | -101 |
| 1024x768 | 1353 / 585 | 1208 / 440 | -145 |
| 768x1024 | 1432 / 408 | 1287 / 263 | -145 |
| 390x844 | 1445 / 601 | 1281 / 437 | -164 |
| 360x800 | 1469 / 669 | 1281 / 481 | -188 |

- 六档的整页高度**都变短了**（-101 ~ -188px），且缩量随断点不同（≥1280 都是 -101；1024/768 为 -145；390/360 为 -164/-188）—— 与「基础规则在所有宽度生效 + 4 组媒体查询按断点生效」一致；因此 `#ask` 回到首屏内是**布局真的变短**，不是采集抖动。


**四轮并列：`#ask` 主按钮在六档的余量（⚠️ 只有最右列 §17 是定稿）**

| 断点 | 主轮（§1–14，**非定稿**） | §15（中间态） | §16（中间态） | **§17（定稿）** |
|---|---|---|---|---|
| 1440x900 | 底边 781.66 / 视口高 900 → 余量 118.34（首屏内） | 底边 708.56 / 视口高 900 → 余量 191.44（首屏内） | 底边 708.56 / 视口高 900 → 余量 191.44（首屏内） | 底边 708.56 / 视口高 900 → 余量 191.44（首屏内） |
| 1280x800 | 底边 781.66 / 视口高 800 → 余量 18.34（首屏内） | 底边 708.56 / 视口高 800 → 余量 91.44（首屏内） | 底边 708.56 / 视口高 800 → 余量 91.44（首屏内） | 底边 708.56 / 视口高 800 → 余量 91.44（首屏内） |
| 1024x768 | 底边 777.66 / 视口高 768 → 余量 -9.66（**越出视口 9.66**） | 底边 676.56 / 视口高 768 → 余量 91.44（首屏内） | 底边 676.56 / 视口高 768 → 余量 91.44（首屏内） | 底边 676.56 / 视口高 768 → 余量 91.44（首屏内） |
| 768x1024 | 底边 856.75 / 视口高 1024 → 余量 167.25（首屏内） | 底边 755.66 / 视口高 1024 → 余量 268.34（首屏内） | 底边 755.66 / 视口高 1024 → 余量 268.34（首屏内） | 底边 755.66 / 视口高 1024 → 余量 268.34（首屏内） |
| 390x844 | 底边 842.27 / 视口高 844 → 余量 1.73（首屏内） | 底边 698.08 / 视口高 844 → 余量 145.92（首屏内） | 底边 698.08 / 视口高 844 → 余量 145.92（首屏内） | 底边 698.08 / 视口高 844 → 余量 145.92（首屏内） |
| 360x800 | 底边 865.36 / 视口高 800 → 余量 -65.36（**越出视口 65.36**） | 底边 698.08 / 视口高 800 → 余量 101.92（首屏内） | 底边 698.08 / 视口高 800 → 余量 101.92（首屏内） | 底边 698.08 / 视口高 800 → 余量 101.92（首屏内） |

- 主轮（§1–14，**非定稿**） 的代码口径：主轮 = lead 复核的冻结版（服务 2026-10-16 13:36:33 启动，在文件写入之后；工具 a11y_audit.py 1.2.1；指纹稳定）。其后 task-24 改过 app.css（7980B2FA…）、task-25 改过 index.html/app.js —— 见 §0.1、§15、§16
- §15（中间态） 的代码口径：task-24 之后、task-25 之前（app.css 7980b2fa…）：测量窗口内 app.js/index.html 仍在被改，混用两版文件
- §16（中间态） 的代码口径：task-25 改动正在落盘时（index.html 测量中途再次变化）：仍属中间态，不得当合格轮引用
- **§17（定稿）** 的代码口径：定稿轮 = lead 2026-10-16 15:2x 宣布的冻结集（7 文件哈希跑前/跑中/跑后三次拍摄一致）；服务 2026-10-16 15:17:14 启动，晚于全部前端文件写入（最晚 login.html/login.js 15:06:26）；工具 a11y_audit.py 1.2.1

> 读法：最右列「**§17（定稿）**」是 2026-10-16 冻结集上的合格轮数字，写结论只引这一列；左边三列（主轮 / §15 / §16）分属 task-24 之前与测量中途被改动的中间态，**只用来观察趋势方向**（余量是变宽还是变窄、越界是否消失），不与定稿列做数值换算，也不单独引用成结论。

**定稿轮（§17）自己的手机端全流程探针**（真实点击，不是静态扫描；390×844 + 1440×900 焦点环）：

| 项 | 定稿轮（§17）实测 |
|---|---|
| 探针自身报错 | 0 条 |
| UI 登录落点 | http://127.0.0.1:8141/app#/workbench |
| 上传结果 | 已上传：sales_sample.csv · 12 行 × 5 列 · 数据编号 794e41bccc284fba92771c762f334e59 |
| 390×844 分析 | 状态 SUCCESS · 步骤 done（6/6）（步骤 6 段、任务列表 1 条） |
| 图表的替代文本 | 实时 figure=1/img=1、硬刷新 figure=1/img=1、深链接 figure=1/img=1；三条路径 alt 全非空 = **True**（本轮 alt：`按地区分组汇总销售额，从高到低取前 20 组`）；接口 charts=1 条、status=200 |
| 提交后焦点去向 | upload：button#upload → html > body；ask：button#ask → html > body |

- 本轮的 workflow id 是 `07ac7014033f4735aaa468fe88114aa9`：探针自己重新播种（`DatasetStore` 是进程内内存，重启后旧 id 一律 404，跨进程不能复用）。
- 与主轮（§9/§10）对照：探针脚本同一份、结论一致（登录链路可用、上传与分析成功、图表替代文本在实时/硬刷新/深链接三条路径都非空、提交后焦点回到 `html > body`）；主轮的原始读数在 §9/§10，两轮的 id/数据集编号必然不同（各自播种），不具可比性。


**定稿轮（§17）自己的初始化态探针**（空账号库、独立服务 8142）：

| 断点（初始化态） | 主按钮底边 / 视口高 | 必填字段在首屏 | scrollH vs vh | 需滚动 px（主轮 → 定稿轮） | 主按钮底边：空表单 → 填满 → 错误态 | 错误文案 y |
|---|---|---|---|---|---|---|
| **1440x900** | 673.88 / 900 | 3/3 | `scrollH=900 vs vh=900`（无需滚动） | 0 → 0 | 673.88 → 填满 673.88 → 错误态 674.7 | y=599.61 |
| **1280x800** | 604.98 / 800 | 3/3 | `scrollH=800 vs vh=800`（无需滚动） | 0 → 0 | 604.98 → 填满 604.98 → 错误态 629.41 | y=554.31 |
| **1024x768** | 574.52 / 768 | 3/3 | `scrollH=768 vs vh=768`（无需滚动） | 0 → 0 | 574.52 → 填满 574.52 → 错误态 600.47 | y=525.38 |
| **768x1024** | 775.34 / 1024 | 3/3 | `scrollH=1024 vs vh=1024`（无需滚动） | 0 → 0 | 775.34 → 填满 775.34 → 错误态 806.44 | y=723.34 |
| **390x844** | 733.75 / 844 | 3/3 | `scrollH=944 vs vh=844`（**需滚动 100px**） | 122 → 100 | 733.75 → 填满 733.75 → 错误态 764.84 | y=681.75 |
| **360x800** | 755.19 / 800 | 3/3 | `scrollH=965 vs vh=800`（**需滚动 165px**） | 188 → 165 | 755.19 → 填满 755.19 → 错误态 786.28 | y=703.19 |

- 六档「主按钮底边」（空表单态）与主轮逐值相同 = **True**；需要滚动的档位只有手机两档，且**滚动量比主轮更小**（见上表第五列）。
- 三个状态的底边（空表单 / 填满 / 密码不一致错误态）两轮逐值一致：说明 task-24（app.css 分析页命名空间块）与 task-25（文案）都没有改到初始化态页面的垂直节奏。


**D6 三条诚实标注：改动前 vs 定稿轮的独立复核**（装置：`.tmp/a11y/check_d6.py`；源码字节 + 真实浏览器文本节点两套，都不依赖行号定位）：

- `app/web/index.html` 整文件：`22FECF80C471697F…`（39212 B）→ `804843F5AE6AEBBF…`（39258 B）—— 文件整体变过是已知的（文案收口轮），要证明的是**这三段 D6 原文没被顺手动过**。

| D6 片段 | 行号 | 片段 sha256_16 | 整行 sha256_16 | 结论 |
|---|---|---|---|---|
| `L325_span_state` | [325] → [325] | DDEBD2FE7DCAF5E9 → DDEBD2FE7DCAF5E9 | FA2179CA7C54D42D → FA2179CA7C54D42D | ✅ 逐字未变 |
| `L395_note_missing` | [395] → [395] | 693DD1651680BB10 → 693DD1651680BB10 | B677F760AFF2F289 → B677F760AFF2F289 | ✅ 逐字未变 |
| `L501_note_steptime` | [501] → [501] | B6A60B9F8CC25E08 → B6A60B9F8CC25E08 | 6DCDEDA90AA45CD1 → 6DCDEDA90AA45CD1 | ✅ 逐字未变 |

- **渲染侧**（判据 = 归一化文本的 sha256，不看行号）：7 个页面 × 3 条 D6 文本 = 21 组对照，**命中数与文本逐字全部一致**
- **源码侧**：三条片段的「出现行号 + 片段 sha256_16 + 整行 sha256_16」全部一致（上表）
- 交叉印证：lead 用「亲读源码」这一不同装置复核，结论同为「逐字未变」——两套装置（字节/文本节点）结论一致。


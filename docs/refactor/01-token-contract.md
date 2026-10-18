# 01 令牌契约（Wave A · 方案 01「高端企业战略指挥中心」→ `app/web/tokens.css`）

> 本文档是方案 01 落地的**令牌层设计牌照**：规定哪些语义令牌新增、值从哪来、用在哪一处 UI、以及发现的矛盾。
> 消费窗口：Wave B（`app.css` / `index.html` / `app.js` 重构）按本契约引用 `--ds-*`，**组件里不许写裸 hex、裸毫秒数、裸位移像素**。
> 上游依据：`docs/design-options/option-01.md`（§4 色彩 / §5 布局 / §9 阶梯 / §11 动效）、`docs/design-options/00-brief-and-matrix.md` §4 视觉纪律。
> 本次改动仅 `app/web/tokens.css`；未触碰 `app.css` / `index.html` / `app.js` / `login.*` / 后端。

## 0. 铁律与执行结果

| 铁律（tokens.css 文件头 + 本次任务书） | 结果 |
| --- | --- |
| 兼容层现有 `--brand-*`/`--n-*`/`--ok-*` 等**一个 hex 不改** | ✅ 零修改，只追加 1B/1C 两段新令牌 |
| 品牌层只许 `var()` 引用兼容层，**零裸 hex**（含注释） | ✅ 第 2 段（236 行起）grep `#[0-9a-f]` 命中 0（见 §6 证据） |
| 不删除/改名任何现有 `--ds-*`（app.css 引用 88 处、login.css 200 处） | ✅ 138 个存量 `--ds-*` 全部保留，逐一核对（见 §6） |
| 动效时长/缓动进令牌 | ✅ 2.22/2.23 全部取自或指向令牌 |
| 故意不做暗色模式（文件头 32-36 行） | ✅ 未引入任何 dark 规则；侧栏深底是**结构面**不是主题 |

**核对结论（先核对再新增）**：方案 01 §4 色彩表的品牌阶 `#0B2239/#12314F/#1A4570/#22608F/#DCE7F0/#EFF4F9/#6E9CC2` 与兼容层 `--brand-900(#14263f)/800/700/600/100/050/400` **逐值核对后确认全部不存在**（现有 --brand-* 是更暖更浅的一套海军蓝；`#0B2239` ≠ `--brand-900 #14263f`）。墨阶 `--ink-*`、线 `--rule*`、纸 `--paper*`、状态 `--ok-700` 系在兼容层同样无同值令牌。因此**全部按新增处理，另起命名空间并存，不复用、不改值**。唯一可复用的白底是 `--n-0`（= 方案 §4 的 `--paper #FFFFFF`）。

## 1. 兼容层新增（第 1 段扩展：1B + 1C，共 38 个）

### 1B · 方案 01 色阶（唯一 hex 口径来源）

| 令牌 | 值 | UI 落点（方案 01 章节） |
| --- | --- | --- |
| `--navy-050` | `#eff4f9` | 亮面 hover 底（§4 主色·浅底） |
| `--navy-100` | `#dce7f0` | 角标底、运行态行底（§4/§7/§9） |
| `--navy-200` | `#a9c4da` | 图表第 4 序列（§4）；跳过段斜纹前景（§9，推断值，见 D4） |
| `--navy-400` | `#6e9cc2` | 侧栏分组小标题、深底次要字（§4 主色·标签）；图表序列 3 |
| `--navy-600` | `#22608f` | 焦点 outline、「运行中」语义、正在执行段（§4 主色·运行）；图表序列 2 |
| `--navy-700` | `#1a4570` | 链接、hover 强调、cites 角标（§4 主色·动作） |
| `--navy-800` | `#12314f` | 主按钮、当前导航底色、已完成阶段点（§4/§5/§9）；图表序列 1 |
| `--navy-900` | `#0b2239` | 左侧栏底 / 登录品牌面板深底（§4 主色·深、§5、§6） |
| `--bronze-500` | `#8a6a3b` | 图表第 5 序列·青铜（§4，区分用非状态） |
| `--ink-900` | `#101820` | 标题字（§4 墨阶端点） |
| `--ink-800` | `#1b2733` | 正文（§4 正文，白底 ≥4.5:1） |
| `--ink-500` | `#68737f` | 次要字/表头字（§4，压 `--paper-alt` ≥4.5:1） |
| `--ink-200` | `#cbd1d8` | 占位字（§4 墨阶端点，非文本） |
| `--rule` | `#dfe3e8` | 表格与分带 hairline（§4 线、§5） |
| `--rule-strong` | `#b7bfc8` | 表头上下界线（§4 线·强） |
| `--rule-soft` | `#ebeef1` | 最弱带内分隔（§4 线·弱） |
| `--paper-alt` | `#f7f8fa` | 页面底（§4 背景；≠ 暖灰 `--n-50`） |
| `--paper-sunk` | `#f1f3f6` | 表头底（§4 背景） |
| `--paper-deep` | `#e7eaee` | 进度轨底/未达段灰轨（§4 背景、§9） |
| `--ok-700` / `--ok-100` | `#1c6440` / `#e3efe8` | 已完成、上升 delta、健康点（§4/§7） |
| `--warn-700` / `--warn-100` | `#8a5b12` / `#f8efdd` | 带警告完成、质量提示（§4/§10） |
| `--err-700` / `--err-100` | `#9e2b2b` / `#f8e9e9` | 失败、下降 delta、错误态（§4/§7） |
| `--sidebar-w-cmd` | `240px` | 侧栏宽（§5；现值 `--sidebar-w:224px` 不动） |
| `--content-max-cmd` | `1220px` | 主内容宽（§5；现值 `--content-max:1320px` 不动） |

`--paper`（`#FFFFFF`）不新建：直接复用 `--n-0`。

### 1C · 存量裸 hex 迁登记（12 个，值为原样搬运，非新口径）

原品牌层 2.2 / 2.8b / 2.16 违反本文件铁律写了裸 hex，本次把 hex 迁到兼容层，品牌层令牌**名称与解析值逐字不变**（渲染零变化）：

| 新兼容令牌 | 值（不变） | 对应品牌层令牌（只改了引用形式） |
| --- | --- | --- |
| `--n-500-safe` | `#6f6f6a` | `--ds-color-ink-subtle` |
| `--bar-dot-ok/warn/err` | `#4cc38a/#e0b25a/#e08a80` | `--ds-color-bar-dot-ok/warn/err` |
| `--err-deep` | `#7a2b23` | `--ds-color-danger-strong` |
| `--viz-legacy-1..6` | `#3b6ea5/#c0504d/#2f7d6b/#a5701a/#5b5fa8/#8a5a8f` | `--ds-chart-1..6`（1/2 与后端 `charts.py:67-68` 对齐；原注释写 69-70，实际行号 67-68，已顺带修正引用） |

## 2. 品牌层新增（2.17–2.24 + 2.10 一处，共 78 个；全部零消费方，Wave B 启用）

| 域 | 令牌 → 引用链（→ 最终 hex） | UI 落点 |
| --- | --- | --- |
| 2.17 外壳 | `--ds-sidebar-w-cmd`→`--sidebar-w-cmd`(240px) | §5 左栏 |
| | `--ds-shell-content-max`→`--content-max-cmd`(1220px) | §5 主内容 |
| | `--ds-shell-side-bg`→`--navy-900`(#0B2239) | §5 侧栏底 / §6 登录品牌面板 |
| | `--ds-shell-side-ink`→`--n-0`；`-ink-muted`→`--navy-400`；`-line`→`--navy-800` | §5 深底文字、3px 分组线 |
| | `--ds-shell-side-active-bg`→`--navy-800`；`-active-marker`→`--navy-400`*；`-active-marker-w`→`--ds-rule-w`(3px)；`-hover-bg`→`--navy-800`* | §5 当前项底色+左竖标、hover（*=D4 推断） |
| | `--ds-shell-side-badge-bg`→`--navy-700`*；`-badge-ink`→`--n-0` | §5 计数徽标（数据集数/运行中数） |
| | `--ds-shell-bar-bg/line/ink/ink-muted`→`--n-0`/`--rule`/`--ink-900`/`--ink-500` | §5 顶部命令条（标题、「数据截至」、声明行） |
| | `--ds-shell-paper/-alt/-sunk/-deep`→`--n-0`/`--paper-*` | §4/§5 四级纸面 |
| 2.18 动作 | `--ds-cmd-action/-hover/-ink`→`--navy-800/700`/`--n-0` | §4 主按钮（命令条「发起新分析」） |
| | `--ds-cmd-link/-hover`→`--navy-700/800` | §4 链接 |
| | `--ds-cmd-focus`→`--navy-600` | §4 焦点 outline（「附」节：outline 不发光） |
| | `--ds-cmd-surface-hover`→`--navy-050`；`--ds-cmd-selected-surface`→`--navy-100` | §4 浅底 hover/运行态底 |
| 2.19 墨与线 | `--ds-ink-heading/body/secondary/placeholder`→`--ink-900/800/500/200`；`--ds-hairline/-strong/-soft`→`--rule*` | §4 画面主体与细线层级 |
| 2.20 态势带+指标 | `--ds-posture-cols:4`；`-row-line`→`--rule-soft`；`-question-ink`→`--ink-900`；`-delta-up/-down`→`--ok-700/--err-700`；`-evidence-ink/-bg`→`--navy-800/--navy-100` | §5/§7 4 列满宽带、升降 delta、证据数 |
| | `--ds-metric-cols:4`；`-rule`→`--rule`；`-value-ink`→`--ink-900`；`-label-ink`→`--ink-500` | §5 hairline 指标网格（禁投影） |
| 2.21 报表表格 | `--ds-table-rule/-emphasis/-head-ink/-cell-ink`→`--rule/--rule-strong/--ink-500/--ink-800`；`-hover-bg`→`--navy-050`；`-hover-dur`→`--dur-1`(120ms)；`-share-bar/-track`→`--navy-600/--paper-deep` | §5/§7 右对齐 tabular-nums 表、份额条（数字排印复用 2.9 `--ds-numeric`） |
| 2.22 阶段阶梯 | `--ds-rail-done`→`--navy-800`；`-running`→`--navy-600`；`-running-surface`→`--navy-100`；`-track`→`--paper-deep`；`-skip-ink`→`--navy-200`*；`-skip-stripe`→`--sp-2`(8px)；`-step-dur`→`--ds-dur-slow`(320ms)；`-pulse-dur:1200ms`；`-segment-gap`→`--sp-1` | §9 六段阶梯：实心/脉冲/灰轨/斜纹（斜纹渐变两色 = `-skip-ink` + `-track`，无魔法数） |
| 2.23 cites+动效+图表 | `--ds-cite-ink/-bg`→`--navy-700/--navy-100` | §10 cites: T1/T2 角标 |
| | `--ds-view-dur`→`--dur-2`(200ms，区间 120–200 取上限，纯淡入可用 `--ds-dur-fast`)；`-shift:6px`；`-ease`→`--ds-ease-out`；`--ds-skeleton-dur:1300ms`；`--ds-viz-grow-dur:500ms` | §11 全部动效参数 |
| | `--ds-viz-1..5`→`--navy-800/600/400/200/--bronze-500`；`--ds-viz-surface`→`--n-0` | §4 图表序列（**仅前端内联 SVG/CSS**；与后端 PNG 冲突见 D1） |
| 2.24 断点 | `--ds-bp-base:1440px`（设计基准口径）；`--ds-bp-side-collapse:768px` | §5 断点；沿用 `--ds-bp-lg:1080px`；`@media` 须写字面量 |

图表/状态互不混用纪律已写进 2.23 注释：`--ds-viz-*` 只进数据序列；状态色只进药丸/severity/delta；青铜 `--ds-viz-5` 不得用于状态。

## 3. 断点核对（任务书第 6 条）

| 方案 01 要求 | 现有令牌 | 缺口与处置 |
| --- | --- | --- |
| 1440 基准 | 无 | 新增 `--ds-bp-base`（非媒体查询，是设计基准与 JS 口径） |
| ≤1080 | `--ds-bp-lg:1080px` | **直接复用**，不改 |
| ≤768 | `--ds-bp-md:760px`（差 8px） | 新增 `--ds-bp-side-collapse:768px`；Wave B 用 768 还是统一改存量 760 → ~~待决策 D3~~ **已裁定，见 `06-leader-decisions.md` D-2：落地用 760，不新增断点**（`--ds-bp-side-collapse` 因此不进媒体查询，`@media` 必须写字面量） |
| （存量 480） | `--ds-bp-sm:480px` | 方案 01 未用，保留不动 |

## 4. 矛盾清单与待主控决策

> **【Wave D 回填 · 2026-10-17】本节 8 个"待决策"项里有 7 个已被主控裁定，讨论到此为止，不要再重开。**
> 裁定原文在 `docs/refactor/06-leader-decisions.md`，逐条结果见下面每一项开头的 `✅ 已裁定` 标注；
> 落地记录（含被驳回的建议）在 `docs/refactor/10-integration-log.md` 与 `docs/COMMAND_CENTER_MIGRATION.md`。
> 原文保留，因为"当时为什么会纠结"本身就是证据。

- **✅ 已裁定，见 `06-leader-decisions.md` D-1**（原文保留在下）。
- **D1（最高优先）图表色板三分裂**：方案 01 §4 要 `#12314F/#22608F/#6E9CC2/#A9C4DA/#8A6A3B`；前端存量 `--ds-chart-1..6` 是另一套（`#3b6ea5/#c0504d/...`，注释声称与后端对齐）；后端 `app/agents/charts.py:67-68` 条形/折线 `#3b6ea5/#c0504d`，`:143-150` 饼图 `_PIE_PALETTE` 又是**第三套** 6 色（`#1f4e9c/#a8621f/#9c8f5a/#4a7fc0/#7c7f96/#6f6a5f`，且经过色盲 ΔE≥19.6 计算验证）。报告页是内联 UI + 后端 PNG **同屏混排**，PNG 无法用 CSS 重着色 → 前端若采用方案 01 色板，图区必然双色分裂。选项：A) 改 `charts.py` 对齐方案 01 色板（须重跑色盲/对比度验证，**本任务未动后端**）；B) 方案 01 内联图表改用 `--ds-chart-*` 存量色（等于放弃 §4 色板）；C) 接受分裂。**当时待主控决策，本次仅落地 `--ds-viz-*` 并存，不重指任何存量令牌。**
  > **裁定结果（D-1）**：走 **B 的加强版** —— **以存量 `--viz-legacy-*` 为唯一口径**，`--ds-viz-1..5` 已重指到与之后端
  > PNG 同色（`--viz-legacy-1..5`），**后端色板不动**（零回归风险），并且**落地页面不自绘业务趋势图**：
  > 真实图表只来自后端 PNG。所以"三套并存"这个问题在界面上不再存在（前端内联图形与 PNG 同色），
  > 方案 01 §4 的那套 navy 图表色板**放弃**，只留在 `option-01.md` 里作为原型记录。
  > 连带后果记在这儿（否则后人会以为是漏改）：`--navy-100/200/400/600/800` 与 `--bronze-500` 仍在兼容层，
  > 但它们**不再作为图表序列色**被消费，只用于底、线、字与侧栏结构面。
- **✅ 已裁定，见 `06-leader-decisions.md` D-3（驳回了下面这条"建议 Wave B 删除"）。**
- **D2 `app.css:21-116` 自带一份与兼容层同值的 `:root` 副本**（91 个令牌逐字重复，历史债）：① 本次**未改**任何存量兼容值，故副本未失同步；② 新增的 1B/1C 令牌只存在于 tokens.css——tokens.css 先行加载，`var()` 链对 app.css 有效，**不需要**同步进 app.css 副本；③ 但风险常在：任何人在 Wave B 之后改任一侧兼容层值而忘改另一侧即分叉。**~~建议 Wave B 删除 `app.css` 的 `:root` 整块~~**（tokens.css 头部迁移状态注释已预告此步）。
  > **裁定结果（D-3）**：**保留，不删。** 这条建议被驳回，理由是测试依赖：`tests/test_frontend.py` 的禁用态按钮
  > 对比度断言是**直接静态解析 `app.css` 自己 `:root` 里的 hex** 去算对比度的（`:461` 起读块、`:547` 起读 `.btn--primary` 规则），
  > 把 hex 搬空 → 断言立刻红，而且是**假红**（配色本身没变）。所以"历史债"这个词以后**不许再出现在文档里**：
  > 这块副本是**被测试钉住的第二事实来源**。约束改成两条：① 副本与 `tokens.css` 的**同名同值必须一致**
  > （守卫：`tests/test_frontend_command_center.py::test_root_token_copies_stay_identical`）；
  > ② 新增令牌**只进 tokens.css**，不要往 app.css 副本里加（加了没人消费，只会扩大漂移面）。
  > 注：本条原文里的 `app.css:21-116` 也是快照，当前该块是 `app.css:18-105`（**这就是别按行号引用的活例**）。
- **✅ 已裁定，见 `06-leader-decisions.md` D-2。**
- **D3 768 vs 760**：8px 差决定存量登录页/抽屉规则是否连带回归。两套令牌已并存，媒体查询用哪套字面量待主控定。
  > **裁定结果（D-2）**：**沿用存量 480 / 760 / 1080，768 一律落地成 760**；不新增断点、不改测试。
  > 理由写得很硬：`test_frontend.py` 钉住了"某文件内只允许一条 `@media (max-width:480px)`"，顶栏重排会触发它；
  > 8px 差没有视觉后果。`--ds-bp-side-collapse:768px` 因此**不再是媒体查询口径**（`@media` 里必须写字面量，
  > 落地写的是 760）；`--ds-bp-base:1440px` 保留为设计基准与 JS 口径。本文 §3 表里那句"待决策 D3"到此关闭。
- **✅ 未被推翻（推断值即现行值）**，若主控日后单点改色，改的是 `tokens.css` 品牌层，不是组件里的裸值。
- **D4 方案 01 文档欠定义，本契约做了标注推断**（组件引用时即采用，主控可单点改色）：六级墨阶文档只确认 4 档（缺两档，未臆造）；侧栏当前项**竖标色**（推断 `--navy-400`）、侧栏 **hover 底**（推断=当前项底，靠竖标区分）、**计数徽标底色**（推断 `--navy-700`）、**跳过段斜纹前景色**（推断 `--navy-200`）、阶梯段高/命令条高度（未设令牌，Wave B 定）。
  > **回填**：四个推断值全部按原样落地（`--ds-shell-side-active-marker`、`-side-hover-bg`、`-side-badge-bg`、
  > `-rail-skip-ink`），无人驳回。Wave B 另补了两个当时"未设"的尺寸令牌：`--cmdbar-h: 60px`（命令条与粘性表头
  > 联动，两处都 `var()`）与 `--ds-rail-bar-h: 6px`、`--ds-skel-row-h: 52px`。**唯一被实测推翻的是分隔线**：
  > `--navy-800` 压在 `--navy-900` 上只有 **1.24:1**（等于不存在），所以新增 `--rule-on-navy: rgba(255,255,255,.16)`
  > + 品牌层 `--ds-shell-side-rule`（取 .16 而不是 .12，是为了过非文本图形 3:1）；
  > 旧名 `--ds-shell-side-line` 因为 `app.css` 已在引用而**保留**，两者用途不同。见 `10-integration-log.md` 批次 4。
- **✅ 已裁定为"并存不重指"，见 `06-leader-decisions.md` D-1 的同族处置思路（不动后端/不动存量渲染值）。**
- **D5 动效时长与存量不一致**：方案 01 脉冲 1.2s vs 存量 `--ds-dur-pulse:1100ms`（login.css 有消费方）；骨架 1.3s vs `--ds-dur-shimmer:1400ms`。处置：新增 `--ds-rail-pulse-dur:1200ms`、`--ds-skeleton-dur:1300ms` 并存，**不重指存量**（重指=改渲染）。是否统一待主控定。
  > **回填**：**并存就是终态**（1100/1400 与 1200/1300 四值现在都在 `tokens.css` 里，各自有消费方）。
  > 另外两处：骨架那套 `.skel .sl` + `@keyframes shimmer` 因为**没有任何消费方**已被删掉（裂缝 6），
  > 现在只有 `.posture__row--skeleton` 一条路径。顺带记一个编号漂移：`tokens.css` 的注释把这条矛盾写成
  > **D7**，而本文的编号是 **D5**（本文 §4 的编号才是准）；改注释要动 `app/web/`，不属于文档轮。
- **✅ 同 D5：并存即终态，`*-cmd` 令牌就是方案 01 用的那组。**
- **D6/D7 尺寸并存**：侧栏 240 vs 224、内容 1220 vs 1320——均新增 `*-cmd` 令牌、保留存量，Wave B 按视图选用。
  > **回填**：外框用 `--ds-sidebar-w-cmd`（240）与 `--ds-shell-content-max`（1220）；
  > 存量的 `--sidebar-w`（224）/ `--ds-sidebar-w` 别名与 `--ds-sidebar-w-compact`（190）在 `app.css` 里**已无引用**，
  > 属于"待清理但本轮没清"（清理会连带牵动 `:root` 副本的静态解析，见上面 D2 的裁定）。
  > `--ds-posture-cols: 4` 也是纯口径令牌：态势带列宽是非均分 `minmax(...)`，塞不进 `repeat()`，所以它**只作口径**存在。
- **✅ 已清偿，无待决项。**
- **D8 铁律违规史已清偿**：品牌层 12 处裸 hex 迁登记至 1C，是唯一一次「值形式」变更（hex→同值 var 引用），名称与解析值零变化。

## 5. Wave B 引用守则（摘录）

组件只写 `var(--ds-*)`；`@media` 字面量必须与 2.15/2.24 口径一致；指标网格与态势带**禁投影**（分隔用 `--ds-hairline*`，不用 `--ds-elevation-*`）；焦点用 `--ds-cmd-focus` 的 `outline`；hover 禁缩放；所有 ms/px 走令牌。

## 6. 验收证据

见 Wave A 汇报：品牌层裸 hex grep 为空、`--ds-*` 138→216（+78，零删除零改名、无重复定义）、app.css 88 / login.css 200 处引用全部可解析、`:root` 单块括号配平、文件 402→601 行（tokens.css 为未跟踪文件，`git diff --stat` 无输出，如实说明）。

> **Wave D 复量（2026-10-17）**：`tokens.css` 现在是 **642 行**，`--ds-*` 唯一定义 **222 个**
> （复现：`wc -l app/web/tokens.css`；`grep -o -- "--ds-[a-z0-9-]*:" app/web/tokens.css | sort -u | wc -l`）。
> **⚠️ 这两个数当下仍在变**：同一会话早先量到 641 行 / 225 个，之后并发 agent 在做"死令牌清理"，
> 令牌数已经掉了 3 个 —— 所以这里给的是**落盘时点**，不是台账。
> 增量来自 Wave B/C 补的 2.25a/b/c 三段（`--cmdbar-h`、`--ds-doc-measure`、`--ds-rail-bar-h`、`--ds-skel-row-h`、
> `--rule-on-navy` + `--ds-shell-side-rule`、禁用三件套等，见 `10-integration-log.md` 批次 1/4）。
> 上面那句"402→601"保留，它是 Wave A 那一版的取证快照。

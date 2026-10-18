# 端到端演示（`scripts/demo.py`）

一条命令把整个系统真跑一遍：**起真 uvicorn → 上传真数据 → 提问 → 看六步进度 →
下载真 PNG → 读报告 → 看数字校验 → 用标准库自己手算对账 → 关服务**。

这份文档里的输出**全部是真实运行结果**（跑完直接捕获 stdout，一个字都没改），
不是手写的"预期输出"。

---

## 1. 怎么跑

### 前置条件

- 仓库自带的虚拟环境（`.venv`，里面已经有 `fastapi` / `uvicorn` / `httpx` / `pandas` /
  `matplotlib`）。演示脚本**只用 `httpx` + 标准库**，不 import `app.*`。
- 示例数据 `data/samples/sales.csv`（已在仓库里：2168 行 × 9 列，141,028 字节）。
- 可选：大模型三项环境变量。**本项目不读 `.env`**，只认进程环境变量，
  而且每个字段认两个名字（`LLM_*` 优先，`DEEPSEEK_*` 兜底）：

  | 用途 | 变量名（二选一） | 默认 |
  |---|---|---|
  | 密钥 | `LLM_API_KEY` / `DEEPSEEK_API_KEY` | 空（= 未配置） |
  | 服务地址 | `LLM_BASE_URL` / `DEEPSEEK_BASE_URL` | 空（= 未配置） |
  | 模型名 | `LLM_MODEL` / `DEEPSEEK_MODEL` | `deepseek-flash` |

  **不配也能跑**：规划走规则兜底，报告里如实写 `llm_used=false`。
  配了才走模型规划（`plan.source="llm"`），报告里 `llm_used=true`。

### 命令

```powershell
# 在仓库根（脚本自己算仓库根，任何 cwd 都行）
& ".\.venv\Scripts\python.exe" scripts\demo.py
```

```bash
# Linux / macOS
./.venv/bin/python scripts/demo.py
```

### 预期耗时与退出码

| 情况 | 端到端耗时（不含启动） | 说明 |
|---|---|---|
| 规则兜底（没配 key） | 约 **1 秒** | 规划是本地规则，几乎不占时间 |
| 模型规划（配了 key） | 约 **3 秒** | 含一次真实的 LLM 调用（本次实测 planning 阶段约 2 秒） |

- 退出码 **0** = 十段全部通过；**非 0** = 某段断言失败（会打印原始响应体，不静默跳过）。
  （脚本自己的结语写的是"九段全部通过"，那是它的措辞习惯；实际编号是 **0–9 共十段**，
  第 0 段是等服务起来。贴出来的输出保留脚本原文，逐字不改。）
- 端口固定 **8128**（脚本自己起、自己关；**故意不用 8000** —— 那是别人可能在用的端口）。
- 服务日志写在 `.tmp/demo_server_8128.log`（`.tmp/` 是 gitignored）。
- **可以重复跑**：`workflow_id` / `dataset_id` 每次都新生成，图表按
  `data/charts/<workflow_id>/` 分目录，不会互相覆盖。

---

## 2. 真实运行输出（模型规划路径）

下面这段是 2026-10-14 在本机跑 `scripts/demo.py` 的**完整 stdout 原文**
（`DEEPSEEK_API_KEY` 从机器级环境变量注入，`llm_configured=true`）：

```text

==========================================================================
准备：环境与进程
==========================================================================
仓库根      : D:\个人项目01\ai-data-analyst-agent
解释器      : D:\个人项目01\ai-data-analyst-agent\.venv\Scripts\python.exe
示例数据    : D:\个人项目01\ai-data-analyst-agent\data\samples\sales.csv（141028 字节）
端口        : 8128
大模型三项（**只看长度与前缀，不打印值**）：
  LLM_API_KEY / DEEPSEEK_API_KEY      : len=35 前缀='sk-'
  LLM_BASE_URL / DEEPSEEK_BASE_URL    : len=24 前缀='htt'
  LLM_MODEL / DEEPSEEK_MODEL          : 未设置
  说明：本项目**不读 .env**，只认上面这些环境变量（`app/config.py`）。
  预期入口：模型规划（key 已配置）
已启动 uvicorn（pid=28064），日志 = D:\个人项目01\ai-data-analyst-agent\.tmp\demo_server_8128.log

==========================================================================
0. 等服务起来 + GET /api/health
==========================================================================
第 1 次探测就绪（间隔 0.5s）
GET /api/health -> 200
{
  "ok": true,
  "data": {
    "status": "healthy",
    "app_name": "AI Data Analyst Agent",
    "version": "0.1.0",
    "uptime_seconds": 0.29,
    "llm_configured": true,
    "upload_dir_ready": true
  },
  "error": null
}

==========================================================================
1. GET / 是真页面（不是 JSON 提示），静态资源与 data/ 未挂载
==========================================================================
GET / -> 200  content-type=text/html; charset=utf-8
  body 含 'id="question"': True
  body 含 'id="charts"': True
  body 含 'id="report"': True
  body 含 'id="steps"': True
  body 含 'charset': True
  首页长度 = 3695 字符
GET /static/app.js -> 200  ct=text/javascript; charset=utf-8  len=20686
GET /static/app.css -> 200  ct=text/css; charset=utf-8
GET /data/samples/sales.csv -> 404（必须不是 200：data/ 没被挂成静态资源）
GET /data/uploads/ -> 404（必须不是 200：data/ 没被挂成静态资源）

==========================================================================
2. 上传 data/samples/sales.csv（真 HTTP multipart）
==========================================================================
POST /api/datasets/upload -> 200（本地 141028 字节）
  dataset_id      = c3fb3628707341e39d6cada288ca550b
  行 × 列          = 2168 × 9
  stored_filename = c3fb3628707341e39d6cada288ca550b.csv
  sha256          = 956307d9f32a4c4b…  与本地重算一致 = True
GET /api/datasets/{id}/profile -> 200  列数=9  重复行=8
    日期     datetime     null=24    unique=336
    地区     categorical  null=0     unique=6
    商品     categorical  null=0     unique=30
    类别     categorical  null=0     unique=5
    销量     numeric      null=18    unique=390
    单价     numeric      null=0     unique=21
    销售额    numeric      null=18    unique=1343
    利润     numeric      null=18    unique=1529
    客户评分   numeric      null=27    unique=26

==========================================================================
3. POST /api/analysis（202 + 立刻返回 workflow_id）
==========================================================================
question = '各地区的销售额汇总，并看一下销售额和利润的关系'
POST /api/analysis -> 202（期望 202）
{
  "ok": true,
  "data": {
    "workflow_id": "cf2e30688795411d8329a9a5b1ae3910",
    "dataset_id": "c3fb3628707341e39d6cada288ca550b",
    "status": "PENDING",
    "created_at": "2026-10-14T02:49:40.681957Z"
  },
  "error": null
}

==========================================================================
4. 每秒轮询 GET /api/workflows/{id}（每次的 step / step_index 都打印）
==========================================================================
  +  0.00s  step=planning   step_index=1/6  status=PLANNING
            has_report=False  charts=0
  +  1.00s  step=planning   step_index=1/6  status=PLANNING
            has_report=False  charts=0
  +  2.00s  step=planning   step_index=1/6  status=PLANNING
            has_report=False  charts=0
  +  3.00s  step=done       step_index=6/6  status=SUCCESS
            has_report=True  charts=1

==========================================================================
5. 终态详情（接口 6 的字段形状）
==========================================================================
  总耗时         = 3.00s
  status         = SUCCESS
  step / index   = done / 6 of 6
  has_report     = True
  charts         = 1 张
  error          = None
  接口 6 顶层字段 = ['charts', 'created_at', 'dataset_id', 'error', 'finished_at', 'has_report', 'question', 'status', 'step', 'step_index', 'tasks', 'total_steps', 'workflow_id']
  step 变化序列：
    +  0.00s  planning  (PLANNING)
    +  3.00s  done  (SUCCESS)
  任务列表（TaskBrief：task_id / description / status / error，**没有 tool_name**）：
    T1  SUCCESS      按地区分组汇总销售额
    T2  SUCCESS      计算数值列两两之间的相关系数

==========================================================================
6. 图表：接口 8 真下载 PNG（content-type + 字节数 + 真字节魔数）
==========================================================================
  c7b6bf6f381e  bar   按地区分组汇总销售额
    url         = /api/workflows/cf2e30688795411d8329a9a5b1ae3910/charts/c7b6bf6f381e.png
    -> 200  ct=image/png  bytes=21214
    magic       = b'\x89PNG\r\n\x1a\n'  PNG=True

==========================================================================
7. 报告：GET /api/workflows/{id}/report（返回的是 ReportData 外壳）
==========================================================================
-> 200
  接口 7 顶层字段 = ['llm_used', 'report', 'results', 'validation']（必须是 report / validation / results / llm_used）
  llm_used          = True（外壳）  report.llm_used = True（报告）
  report_id         = 603f0be3
  title             = 分析报告：各地区的销售额汇总，并看一下销售额和利润的关系
  段落数            = 2
  validation 是否为 None = False
  results 条数      = 2
    T1  ok=True  tool=group_aggregate  rows=6     2ms  data 键=['columns', 'group_count', 'row_count', 'rows']  错误=None
    T2  ok=True  tool=correlation      rows=0     1ms  data 键=['columns', 'matrix', 'pairs']  错误=None

  summary:
    本次由大模型参与规划（选择了分析工具与参数），但报告正文是模板自动汇总的，以下结论仍为工具结果的直接汇总

针对问题「各地区的销售额汇总，并看一下销售额和利润的关系」，系统一共规划了 2 个分析任务，成功 2 个。

  --- 按地区分组汇总销售额  cites=['T1'] ---
    排在最前面的一组是 地区 = 华南，销售额_sum = 24,871,657.00（共 6 行结果）。（工具：group_aggregate，任务：T1）
    table: 6 行，列=['地区', '销售额_sum']
      {'地区': '华南', '销售额_sum': 24871657.0}
      {'地区': '东北', '销售额_sum': 15890839.0}
      {'地区': '华东', '销售额_sum': 11567345.0}
      {'地区': '华北', '销售额_sum': 8948124.0}
      {'地区': '西南', '销售额_sum': 8467097.0}
      ……（共 6 行，演示只打前 5 行）

  --- 计算数值列两两之间的相关系数  cites=['T2'] ---
    相关性最强的一对是 销售额 与 利润，相关系数 0.99（共比较了 1 对）。（工具：correlation，任务：T2）
    table: 1 行，列=['x', 'y', 'r']
      {'x': '销售额', 'y': '利润', 'r': 0.9872}

  data_quality_notes:
    - 检测到 8 行完全重复的记录，去重后还剩 2160 行
    - 有 5 列存在缺失值：日期、销量、销售额、利润、客户评分

==========================================================================
8. 数字校验（接口 7 的 validation 字段）
==========================================================================
{
  "passed": true,
  "checked_numbers": 4,
  "unsupported_numbers": [],
  "issues": [],
  "checked_at": "2026-10-14T02:49:43.147656Z"
}
  passed             = True
  checked_numbers    = 4
  unsupported_numbers= []
  issues             = 0 条

==========================================================================
9. 对账：报告里的六地区销售额 vs 纯 stdlib csv 手算（零容差）
==========================================================================
  我自己用 csv 模块手算（跳过空销售额行）：
    华南     24,871,657
    东北     15,890,839
    华东     11,567,345
    华北      8,948,124
    西南      8,467,097
    西北      7,989,816
    合计     77,734,878

  报告里那张表：'按地区分组汇总销售额'
  地区               报告值           我算的   一致
  华南        24,871,657    24,871,657   ✔
  东北        15,890,839    15,890,839   ✔
  华东        11,567,345    11,567,345   ✔
  华北         8,948,124     8,948,124   ✔
  西南         8,467,097     8,467,097   ✔
  西北         7,989,816     7,989,816   ✔

  报告多出的地区 = []    报告缺的地区 = []
  ✔ 六地区逐个相等（零容差），合计 77,734,878

==========================================================================
演示结束：九段全部通过
==========================================================================
  workflow_id = cf2e30688795411d8329a9a5b1ae3910
  dataset_id  = c3fb3628707341e39d6cada288ca550b
  llm_configured = True   llm_used = True
  端到端耗时  = 3.02s（不含服务启动）

[服务已停] uvicorn pid=28064 exit=1  端口 8128 已让出 = True
```

---

## 3. 这份演示证明了什么、没证明什么

### 证明了（每一条都由脚本自己断言，错了就非 0 退出）

1. **一条命令跑完全链路**：起服务 → 上传 → 提问 → 六步进度 → 真 PNG → 报告 → 校验 → 手算对账 → 关服务。退出码 0。
2. **接口契约是真的**：接口 6 的顶层字段恰好是 13 个契约字段（脚本断言**集合相等**，
   不是"包含"）；`tasks[]` 是 `TaskBrief`（`task_id` / `description` / `status` / `error`，
   没有 `tool_name`）；接口 7 是 `ReportData` 外壳（`report` / `validation` / `results` / `llm_used`）。
3. **数字可对账**：报告里那张六地区销售额表，与脚本用**纯标准库 `csv`** 独立手算的结果
   **逐个相等（零容差）**，合计 **77,734,878**；没有多出的地区、也没有缺的地区。
4. **数字校验链路真的在跑**：`validation.passed=true`、`checked_numbers=4`、
   `unsupported_numbers=[]` —— 报告文本里提到的数字都能在工具结果里找到出处。
5. **图表是真图**：接口 8 返回 `content-type: image/png`，字节数 > 1000，
   前 8 字节与 `b"\x89PNG\r\n\x1a\n"` **逐字节相等**（脚本比的是真字节，不是字符串）。
6. **`data/` 没被暴露**：`/` 是页面、`/static/app.js` 可用，而
   `/data/samples/sales.csv` 与 `/data/uploads/` 都是 **404**（上传件只能通过接口拿）。
7. **服务生命周期干净**：跑完脚本自查 `8128` 已无监听，且外部 `netstat` 也确认没有残留。
8. **两种模式都能跑**：没配 key → 规则兜底、`llm_used=false`；配了 key → 模型规划、
   `llm_used=true`，两条路径都出了图和报告。

### 没证明（免得把演示当性能/生产证据）

- **单进程、运行态在内存里**：数据集与工作流注册表都在进程内存中，服务一停就没了
  —— 重启后旧 `dataset_id` 一律 404，旧 `workflow_id` 调状态 / 报告 / 图表也一律 404。
  **例外**：跑到**终态**的工作流会归档进 SQLite，重启后能用 `GET /api/workflows/history`
  与 `/api/workflows/history/{workflow_id}` 查到（D6-SQLite 补做，见 D-24）。
  **本次演示脚本没有覆盖这条**（它跑完就关服务，不重启），所以这条不在"证明了"里。
  而且归档**只**提供只读的历史列表 / 详情，**不含"继续运行"**，也不改变"必须单进程"这个前提。
  这不是生产架构，演示只证明"单进程下能跑通"。
- **没验并发**：全程单用户、顺序请求，没有并发压测，也没有多 worker / 分布式状态。
- **图表只验了格式**：验的是"合法 PNG + 正确 content-type + 字节数"，
  没有做人眼或像素级比对（图好不好看、坐标轴合不合理不在这条链路上）。
- **校验的射程有限**：它只覆盖"报告文本里的数字 vs 工具结果"这条链路，
  工具自身算法对不对不在它射程内 —— 所以第 9 段才要用标准库**独立手算**交叉验证。
- **大模型输出不稳定**：同一个问题两次可能规划出不同的工具与字段
  （本次模型路径只算了 1 对相关性，规则路径算了 10 对；任务顺序也相反）。
  第 9 段依赖"计划里确实有按地区分组那张表"，**找不到时脚本直接失败，不静默跳过**。
- **没验**：`.xlsx` 上传、超限文件（>10MB）与非法扩展名的拒收、浏览器里的真实渲染
  （DOM 行为由另一套 Node 脚手架测试单独覆盖）、Windows 以外的平台。
- **密钥只用于模型路径**：脚本从不打印 key 的值（只打印 `len()` 与前缀），
  也不会把它写进日志或 Markdown。

---

## 4. 两个入口的差异（规则兜底 vs 模型规划）

两条路径都真实跑过。差异只在**规划**这一段，后面的执行 / 图表 / 报告 / 校验完全同构：

| | 规则兜底 | 模型规划 |
|---|---|---|
| 触发条件 | 没有可用的 key / base_url | 三项配置齐全且调用成功 |
| `/api/health` 的 `llm_configured` | `false` | `true` |
| 计划来源 | 本地规则（`plan_with_rules()`） | 一次真实 LLM 调用；失败/超时自动退回规则 |
| 规划的可见代价 | 几乎为 0（首个轮询就已在 `execution`） | 约 2 秒（`planning` 阶段持续约 3 次轮询） |
| 端到端耗时（本次实测） | **1.02s** | **3.02s** |
| 规划出的任务 | T1 `correlation` → T2 `group_aggregate` | T1 `group_aggregate` → T2 `correlation`（顺序相反） |
| 相关性任务的射程 | 全部数值列两两比较（**10 对**） | 模型只点了 `销售额` 与 `利润`（**1 对**） |
| 报告首段 | 模板汇总（"系统一共规划了 2 个分析任务…"） | 同样模板，但摘要里如实写明"本次由大模型参与规划…报告正文是模板自动汇总的" |
| `FinalReport.llm_used` | `false` | `true` |
| 报告正文/图表/校验 | 全部一致（正文由模板拼装，数字逐条校验） | 全部一致 |

> 一句话：**`llm_used` 描述的是"这次规划有没有用到大模型"，不是"报告是不是大模型写的"**。
> 报告正文永远是模板拼装 + 数字校验，这一点不要误解。

下面这段是**规则兜底路径**的完整原始输出（同一台机器，未注入 key）：

```text

==========================================================================
准备：环境与进程
==========================================================================
仓库根      : D:\个人项目01\ai-data-analyst-agent
解释器      : D:\个人项目01\ai-data-analyst-agent\.venv\Scripts\python.exe
示例数据    : D:\个人项目01\ai-data-analyst-agent\data\samples\sales.csv（141028 字节）
端口        : 8128
大模型三项（**只看长度与前缀，不打印值**）：
  LLM_API_KEY / DEEPSEEK_API_KEY      : 未设置
  LLM_BASE_URL / DEEPSEEK_BASE_URL    : len=24 前缀='htt'
  LLM_MODEL / DEEPSEEK_MODEL          : 未设置
  说明：本项目**不读 .env**，只认上面这些环境变量（`app/config.py`）。
  预期入口：规则兜底（未配置 key / base_url）
已启动 uvicorn（pid=14048），日志 = D:\个人项目01\ai-data-analyst-agent\.tmp\demo_server_8128.log

==========================================================================
0. 等服务起来 + GET /api/health
==========================================================================
第 1 次探测就绪（间隔 0.5s）
GET /api/health -> 200
{
  "ok": true,
  "data": {
    "status": "healthy",
    "app_name": "AI Data Analyst Agent",
    "version": "0.1.0",
    "uptime_seconds": 0.22,
    "llm_configured": false,
    "upload_dir_ready": true
  },
  "error": null
}

==========================================================================
1. GET / 是真页面（不是 JSON 提示），静态资源与 data/ 未挂载
==========================================================================
GET / -> 200  content-type=text/html; charset=utf-8
  body 含 'id="question"': True
  body 含 'id="charts"': True
  body 含 'id="report"': True
  body 含 'id="steps"': True
  body 含 'charset': True
  首页长度 = 3695 字符
GET /static/app.js -> 200  ct=text/javascript; charset=utf-8  len=20686
GET /static/app.css -> 200  ct=text/css; charset=utf-8
GET /data/samples/sales.csv -> 404（必须不是 200：data/ 没被挂成静态资源）
GET /data/uploads/ -> 404（必须不是 200：data/ 没被挂成静态资源）

==========================================================================
2. 上传 data/samples/sales.csv（真 HTTP multipart）
==========================================================================
POST /api/datasets/upload -> 200（本地 141028 字节）
  dataset_id      = 3f21971c97534d9d90b38bfa23cf2b52
  行 × 列          = 2168 × 9
  stored_filename = 3f21971c97534d9d90b38bfa23cf2b52.csv
  sha256          = 956307d9f32a4c4b…  与本地重算一致 = True
GET /api/datasets/{id}/profile -> 200  列数=9  重复行=8
    日期     datetime     null=24    unique=336
    地区     categorical  null=0     unique=6
    商品     categorical  null=0     unique=30
    类别     categorical  null=0     unique=5
    销量     numeric      null=18    unique=390
    单价     numeric      null=0     unique=21
    销售额    numeric      null=18    unique=1343
    利润     numeric      null=18    unique=1529
    客户评分   numeric      null=27    unique=26

==========================================================================
3. POST /api/analysis（202 + 立刻返回 workflow_id）
==========================================================================
question = '各地区的销售额汇总，并看一下销售额和利润的关系'
POST /api/analysis -> 202（期望 202）
{
  "ok": true,
  "data": {
    "workflow_id": "f664552c570541e89174f81de6a3312b",
    "dataset_id": "3f21971c97534d9d90b38bfa23cf2b52",
    "status": "PENDING",
    "created_at": "2026-10-14T02:49:35.932105Z"
  },
  "error": null
}

==========================================================================
4. 每秒轮询 GET /api/workflows/{id}（每次的 step / step_index 都打印）
==========================================================================
  +  0.00s  step=execution  step_index=2/6  status=EXECUTING
            has_report=False  charts=0
  +  1.02s  step=done       step_index=6/6  status=SUCCESS
            has_report=True  charts=1

==========================================================================
5. 终态详情（接口 6 的字段形状）
==========================================================================
  总耗时         = 1.02s
  status         = SUCCESS
  step / index   = done / 6 of 6
  has_report     = True
  charts         = 1 张
  error          = None
  接口 6 顶层字段 = ['charts', 'created_at', 'dataset_id', 'error', 'finished_at', 'has_report', 'question', 'status', 'step', 'step_index', 'tasks', 'total_steps', 'workflow_id']
  step 变化序列：
    +  0.00s  execution  (EXECUTING)
    +  1.02s  done  (SUCCESS)
  任务列表（TaskBrief：task_id / description / status / error，**没有 tool_name**）：
    T1  SUCCESS      计算数值列两两之间的相关系数（看哪些指标同涨同跌）
    T2  SUCCESS      按地区分组汇总销售额，从高到低取前 20 组

==========================================================================
6. 图表：接口 8 真下载 PNG（content-type + 字节数 + 真字节魔数）
==========================================================================
  9cd9acd9d54d  bar   按地区分组汇总销售额，从高到低取前 20 组
    url         = /api/workflows/f664552c570541e89174f81de6a3312b/charts/9cd9acd9d54d.png
    -> 200  ct=image/png  bytes=24020
    magic       = b'\x89PNG\r\n\x1a\n'  PNG=True

==========================================================================
7. 报告：GET /api/workflows/{id}/report（返回的是 ReportData 外壳）
==========================================================================
-> 200
  接口 7 顶层字段 = ['llm_used', 'report', 'results', 'validation']（必须是 report / validation / results / llm_used）
  llm_used          = False（外壳）  report.llm_used = False（报告）
  report_id         = 3dd11c48
  title             = 分析报告：各地区的销售额汇总，并看一下销售额和利润的关系
  段落数            = 2
  validation 是否为 None = False
  results 条数      = 2
    T1  ok=True  tool=correlation      rows=0     3ms  data 键=['columns', 'matrix', 'pairs']  错误=None
    T2  ok=True  tool=group_aggregate  rows=6     3ms  data 键=['columns', 'group_count', 'row_count', 'rows']  错误=None

  summary:
    本次未使用大模型，以下结论为工具结果的直接汇总

针对问题「各地区的销售额汇总，并看一下销售额和利润的关系」，系统一共规划了 2 个分析任务，成功 2 个。

  --- 计算数值列两两之间的相关系数（看哪些指标同涨同跌）  cites=['T1'] ---
    相关性最强的一对是 销售额 与 利润，相关系数 0.99（共比较了 10 对）。（工具：correlation，任务：T1）
    table: 10 行，列=['x', 'y', 'r']
      {'x': '销量', 'y': '单价', 'r': -0.0457}
      {'x': '销量', 'y': '销售额', 'r': 0.0461}
      {'x': '销量', 'y': '利润', 'r': 0.0353}
      {'x': '销量', 'y': '客户评分', 'r': -0.0004}
      {'x': '单价', 'y': '销售额', 'r': 0.9538}
      ……（共 10 行，演示只打前 5 行）

  --- 按地区分组汇总销售额，从高到低取前 20 组  cites=['T2'] ---
    排在最前面的一组是 地区 = 华南，销售额_sum = 24,871,657.00（共 6 行结果）。（工具：group_aggregate，任务：T2）
    table: 6 行，列=['地区', '销售额_sum']
      {'地区': '华南', '销售额_sum': 24871657.0}
      {'地区': '东北', '销售额_sum': 15890839.0}
      {'地区': '华东', '销售额_sum': 11567345.0}
      {'地区': '华北', '销售额_sum': 8948124.0}
      {'地区': '西南', '销售额_sum': 8467097.0}
      ……（共 6 行，演示只打前 5 行）

  data_quality_notes:
    - 检测到 8 行完全重复的记录，去重后还剩 2160 行
    - 有 5 列存在缺失值：日期、销量、销售额、利润、客户评分

==========================================================================
8. 数字校验（接口 7 的 validation 字段）
==========================================================================
{
  "passed": true,
  "checked_numbers": 4,
  "unsupported_numbers": [],
  "issues": [],
  "checked_at": "2026-10-14T02:49:36.014345Z"
}
  passed             = True
  checked_numbers    = 4
  unsupported_numbers= []
  issues             = 0 条

==========================================================================
9. 对账：报告里的六地区销售额 vs 纯 stdlib csv 手算（零容差）
==========================================================================
  我自己用 csv 模块手算（跳过空销售额行）：
    华南     24,871,657
    东北     15,890,839
    华东     11,567,345
    华北      8,948,124
    西南      8,467,097
    西北      7,989,816
    合计     77,734,878

  报告里那张表：'按地区分组汇总销售额，从高到低取前 20 组'
  地区               报告值           我算的   一致
  华南        24,871,657    24,871,657   ✔
  东北        15,890,839    15,890,839   ✔
  华东        11,567,345    11,567,345   ✔
  华北         8,948,124     8,948,124   ✔
  西南         8,467,097     8,467,097   ✔
  西北         7,989,816     7,989,816   ✔

  报告多出的地区 = []    报告缺的地区 = []
  ✔ 六地区逐个相等（零容差），合计 77,734,878

==========================================================================
演示结束：九段全部通过
==========================================================================
  workflow_id = f664552c570541e89174f81de6a3312b
  dataset_id  = 3f21971c97534d9d90b38bfa23cf2b52
  llm_configured = False   llm_used = False
  端到端耗时  = 1.02s（不含服务启动）

[服务已停] uvicorn pid=14048 exit=1  端口 8128 已让出 = True
```

---

## 5. 每一段分别在查什么（编号 `0.`–`9.`）

脚本按 `0.`–`9.` 编号打印，每段自带断言（不满足就打印原始响应体并非 0 退出）：

> 脚本最后的结语写的是「九段全部通过」—— 那是它的措辞习惯，实际编号是 **0–9 共十段**
> （第 0 段是等服务起来，第 1–9 段逐段验收）。这里保留脚本原文不改，
> 所以贴出来的输出与脚本逐字对应。

| 段 | 做的事 | 关键断言 |
|---|---|---|
| 0 | 等服务起来 + `GET /api/health` | 服务在 8128 上应答 `200` |
| 1 | `GET /` 与静态资源 | `/` 是 `text/html` 页面；`/static/app.js` 可用；`/data/samples/sales.csv`、`/data/uploads/` 都**不是 200** |
| 2 | 真 HTTP multipart 上传 `data/samples/sales.csv` | `200`；`2168 × 9`；返回的 `sha256` 与脚本本地重算一致；再取 `/profile` 得到列数 / 缺失值 |
| 3 | `POST /api/analysis` | 状态码 **`202`** 且立刻拿到 `workflow_id`（不阻塞） |
| 4 | 每秒轮询接口 6，打印每次的 `step` / `step_index` | 能观察到 `step` 变化；进入终态后**立刻停止**轮询 |
| 5 | 终态详情（接口 6 的字段形状） | 顶层字段**集合相等**于 13 个契约字段；`tasks[]` 每项只有 `TaskBrief` 的四个键；终态 `has_report` 正确 |
| 6 | 接口 8 真下载 PNG | `content-type: image/png`；字节数 > 1000；前 8 字节 **逐字节**等于 PNG 魔数；中文标题不乱码 |
| 7 | `GET /api/workflows/{id}/report` | 返回的是 **`ReportData` 外壳**（`report` / `validation` / `results` / `llm_used`）；`report.sections` 非空；`results[]` 里能看到工具名、行数、耗时 |
| 8 | 数字校验 | `validation.passed`、`checked_numbers`、`unsupported_numbers`、`issues` 的形状与取值 |
| 9 | **对账**：报告里那张六地区销售额表 vs 纯标准库 `csv` 手算 | 逐个地区**零容差**相等；没有多出/缺失的地区；合计一致 |

---

## 6. 脚本里刻意避开的坑（都是真踩过的）

这几条不是风格偏好，是**探针自己会撒谎**的实例 —— 写演示脚本时最容易在这里被骗：

1. **`TaskBrief` 没有 `tool_name`**。按 `t["tool_name"]` 取任务名会直接 `KeyError`。
   工具名在**报告接口的 `results[]`** 里（`tool_name` / `row_count` / `elapsed_ms`）。
   脚本第 5 段只打 `TaskBrief` 的四个键，第 7 段才打工具名。
2. **接口 7 返回的是 `ReportData`，不是裸 `FinalReport`**。少了这一层，
   `data["title"]` 会取到 `None` 或直接炸；正确写法是 `data["report"]["title"]`。
3. **f-string 里比较字节字面量会得到假的 `False`**。
   写 `f"{head[:4] == b'\x89PNG'}"` 打印出来的是转义文本，不是真正的比较结果 ——
   你会在"图明明是 PNG"的情况下看到 `False`。脚本先把结果存进变量
   （`png_ok = data[:8] == PNG_MAGIC`）再打印布尔值。
4. **别用 PowerShell 的 `Tee-Object` 存"原始输出"**：本机实测它写出的是
   **UTF-16LE + BOM + CRLF**（前 4 字节 `ff fe 0d 00`），贴进 Markdown 全是乱码。
   这份文档里的输出是走管道捕获、按 UTF-8 落盘、行尾统一成 LF 的。
5. **子进程 stdout 在 Windows 文本模式下会把 `\n` 变 `\r\n`**。内容没变但字节变了，
   `diff` 看不出来（同族教训：`docs/DECISIONS.md` 里那条"内容一样、字节不一样"）。

---

## 7. 排错

| 现象 | 原因与处理 |
|---|---|
| 打印"端口 8128 已经被占用"并退出 | 脚本**不会**去杀别人的进程。自己确认那是什么（`netstat -ano \| findstr :8128`）再决定 |
| 第 6 段图表 404 | 那条工作流没出图（`FAILED`）或计划里没有画图任务；先看第 5 段的 `status` / `error` 与第 7 段的 `results` |
| 配了 key 但 `llm_configured=false` | 变量名（`LLM_*` 优先、`DEEPSEEK_*` 兜底）；**项目不读 `.env`**；配置在进程启动时读取，需要重启服务 |
| 控制台中文乱码 | 设 `PYTHONIOENCODING=utf-8`（脚本对子进程已设），必要时 `chcp 65001` |
| 想换端口 / 看服务日志 | 改 `scripts/demo.py` 顶部常量；日志在 `.tmp/demo_server_8128.log`（gitignored） |
| 想确认"没留垃圾" | 跑完 `netstat -ano \| findstr :8128` 应为空；上传件在 `data/uploads/`（gitignored）、图表在 `data/charts/<workflow_id>/` |

> 演示脚本**故意不 import `app.*`** —— 它只用 `httpx` + 标准库，所以它验的是
> "跨进程的真实 HTTP 行为"，而不是"同一进程里的函数调用"。

---

## 8. 相关文件

- `scripts/demo.py` —— 本演示的唯一入口
- `data/samples/sales.csv` —— 演示数据（2168 行 × 9 列）
- `app/web/index.html` / `app/web/app.css` / `app/web/app.js` —— 第 1 段访问的页面
- `docs/ARCHITECTURE.md` §7 —— 接口 6/7/8 的契约（状态码、字段、规则）
- `docs/DECISIONS.md` —— D-05（逐文件 `git add`）、D-11（临时脚本进 `.tmp/`）、
  D-16（探针自己会撒谎）等纪律的出处

# docs/ARCHITECTURE.md 审核意见（Project Lead）

- 审核对象：`docs/ARCHITECTURE.md`（961 行 → 现 1015 行，含 architect 本轮更新）
- 审核人：Project Lead
- 关联任务：`task-6`（审核）、`task-1`（架构交付）
- 结论：**有条件通过 —— 需修订 7 条（其中 3 条为阻塞项）**

---

## 0. 总体评价

这份文档的质量超出了"第一天该有的架构文档"，特别是三件事做得对：

1. **§6.3 五类校验按顺序前置短路 + "同一个校验函数被 API 层和 Agent 层共用"** —— 这一条直接消灭了"两处各写一份规则、慢慢不一致"的典型 bug。这是本项目最值得在面试里讲的设计。
2. **§10 安全清单 12 条全部落到具体文件**，不是空泛的"要注意安全"。其中第 6 条"settings 里的超时/重试必须真的传进 openai 客户端"抓得很准——很多项目在配置里写了超时却没接上线。
3. **§11 每个依赖统一用「它的用途 / 替代方案 / 什么时候值得引入」三段式**，并且 §8.3 把升级触发条件写死。这正面回应了用户总提示词第六节的"避免过度工程化"和"必须解释必要性分析"。

**但有一个自相矛盾必须修掉**（见 A-1），否则第二阶段的代码契约是不确定的。

---

## A. 阻塞项（不修完不进入第二阶段编码）

### A-1 【自相矛盾】§7 的"业务错误一律 HTTP 200" 与同节表格里的 404/400 冲突

文档里同时存在两种说法：

- **§7 统一约定第 2 条**（第 718 行）："**业务错误一律 HTTP 200 + `ok:false`**"
- **§7 同节下面的框架异常表格**（第 720–727 行）：404 / 405 / 422 都用真实 HTTP 状态码；最后一行还专门写"需要精确业务码时（`FILE_TOO_LARGE` 等）→ **200**"
- **§7 接口表**（第 734–740 行）：接口 2 的 `UNSUPPORTED_FILE`、`FILE_TOO_LARGE`；接口 3/5/6/7/8 的 `NOT_FOUND`；接口 7 的 `BAD_REQUEST` —— 这些错误码**没有一处标明 HTTP 状态码**，读者只能靠第 2 条猜成 200

也就是说：同一个 `NOT_FOUND`，路径不存在时是 404，数据集不存在时是 200。同一个 `BAD_REQUEST`，请求体校验失败时是 422，报告没生成时是 200。**这不是设计，这是两套规则叠在一起。** backend-engineer 照着写会出现"上传接口返回 200、查询接口返回 404"这种前端根本无法统一处理的情况，而这恰恰是 §7 那条约定的初衷。

**Project Lead 拍板（最终决定，无需再讨论）：**

> **HTTP 状态码表达 HTTP 语义，并且必须是真的；`error.code` 表达业务语义。两者不冲突，同时给。**

| 情况 | HTTP 状态 | `error.code` |
|---|---|---|
| 请求成功（同步完成） | 200 | — |
| 请求已受理、后台执行（`POST /api/analysis`） | **202** | — |
| 参数格式/类型不对（`RequestValidationError`） | 422 | `BAD_REQUEST` |
| 业务参数非法（问题为空、问题过长） | 400 | `BAD_REQUEST` |
| 资源不存在（路径、dataset_id、workflow_id） | 404 | `NOT_FOUND` |
| 方法不对 | 405 | `BAD_REQUEST` |
| 扩展名不在白名单 / 文件伪装 | 415 | `UNSUPPORTED_FILE` |
| 文件超过 `max_upload_bytes` | 413 | `FILE_TOO_LARGE` |
| 文件内容解析失败（坏编码、空文件、只有表头、超宽） | 400 | `BAD_REQUEST` |
| 外部依赖不可用（模型未配置且无法兜底） | 503 | `BAD_REQUEST` |
| 工作流超时 | 200 | `WORKFLOW_TIMEOUT` |
| 服务器内部未预期错误 | 500 | `INTERNAL_ERROR` |

**为什么推翻"一律 200"**（这是我要给用户讲清楚的理由，不是偏好）：

1. **§7 那条约定的理由是"前端只写一套解析逻辑"，而这个理由在真实 HTTP 状态码下同样成立。** 前端用 `fetch`（不是 axios），4xx/5xx **不会抛异常**，`.json()` 照样能解析出 `{"ok":false,...}`。所以只要**响应体外壳统一**，前端永远只写一套 `if (!resp.ok) showError(...)`。既然理由不成立，就没有必要付出代价。
2. **代价是真的：用一个 200 去表示"你的文件有 70 MB 被拒绝了"，是在向 HTTP 层撒谎。** 打开 F12 看到一片 200，排查问题时必须点开每个响应体才能知道发生了什么；而 413 一眼就知道。
3. **`POST /api/analysis` 返回 202 更准确** —— 它确实没有"完成"任何事，只是"受理"了。
4. **最大的一类业务错误已经必然是 4xx 了**：`RequestValidationError` 的 422 和路径不存在的 404 是框架给的，改不成 200。既然 4xx 已经存在，前端就必须能处理 4xx，那"一律 200"从一开始就做不到，只会变成"一半一半"。

**落到代码的约束（写进 §7 和 task-3）**：所有错误响应体形状**永远**是 `{"ok":false,"data":null,"error":{"code","message","detail"}}`，只有 HTTP 状态码不同。**禁止**在路由里手写 `return JSONResponse(status_code=200, content={"error":...})` 这种绕过统一外壳的写法；业务错误统一用 `raise AppError(code=..., message=..., status_code=...)`，由 `app/main.py` 的 handler 翻译。`POST /api/analysis` 的 202 用 `@router.post(..., status_code=202)` 声明。

### A-2 【契约不稳】§7 接口表的"主要错误码"列没有 HTTP 状态码，且 `charts` 的返回结构前后不一致

- **问题 1**：接口表的错误码列必须补上 HTTP 状态码（按 A-1 的表），否则读者只能猜。
- **问题 2**：**接口 6 的响应里写了 `charts:[ChartResult]`，接口 8 却是 `GET /charts/{filename}` 返回 PNG 二进制。** 那么 `ChartResult.url` 到底存什么？前端 §9.1 的 E 区写的是 `<img src="{ChartResult.url}">`。三处对不上，FE 会写出 404 的图片链接。

  **要求写死**：`ChartResult` 字段为 `{chart_id, filename, title, chart_type, url}`，其中 `url` 是被**后端拼好的**完整相对路径 `/api/workflows/{workflow_id}/charts/{filename}`（前端不做字符串拼接），接口 6 直接返回这个列表，接口 8 只负责按 `filename` 送回 PNG。

### A-3 【目录设计冲突】架构文档的 `app/data/loader.py` 与已派发的 task-3 里的 `app/datasets.py` / `app/security/files.py` 不一致

task-3（第二天，即将派给 backend-engineer）写的是 `app/datasets.py` + `app/security/files.py`；架构文档 §2 写的是 `app/data/loader.py`。两边不能同时存在，否则会出现"同一个功能两个文件都在写"。

**Project Lead 拍板：以架构文档的 `app/data/` 为准**（理由：§2.1 已经定义了 `data/ → 只依赖 pandas/openpyxl` 的依赖方向，"读文件"和"算统计"分开后，`tools/` 才不会反向 import `api/`；这是一个能讲清的分层，而不是多余的抽象）。**task-3 的描述已同步改写**，最终文件清单：

| 文件 | 职责 | 不负责 |
|---|---|---|
| `app/data/loader.py` | `load_dataframe(path, ext) -> pd.DataFrame`；编码/分隔符兜底；行数/列数上限 | 不生成 dataset_id、不落盘、不碰 HTTP |
| `app/data/store.py` | `DatasetStore`：进程内 dict，存 DataFrame + `DatasetSummary`；负责落盘名生成与读回 | 不做统计计算 |
| `app/data/profiler.py` | `build_profile(df, dataset_id) -> DatasetProfile` | 不调 LLM、不做业务判断 |
| `app/errors.py` | `AppError` 异常 + `ERROR_STATUS` 映射 + `sanitize_detail()`（去绝对路径/用户名） | 不写 HTTP handler |
| `app/api/routes_datasets.py` | 3 个接口的编排 | 不写 pandas |

---

## B. 需修订项（不阻塞开工，但必须在第二阶段结束前落文）

### B-1 §4.5 `WorkflowState` 的定义位置分裂，对基础学习者不友好

§2 目录树写明 `app/core/registry.py` "WorkflowRegistry：workflow_id → WorkflowState"，而 §4.5 又说"`app/core/registry.py` 里定义、`schemas_workflow.py` 里声明"。这个拆分对已经熟悉分层的人是常识，对一个"正在学 FastAPI"的读者是纯粹的困惑源 —— 他搜 `class WorkflowState` 会搜不到预期的地方。

**改为**：`WorkflowState` / `WorkflowStatus` / `AnalysisTask` / `TaskPlan` / `ValidationResult` / `FinalReport` / `QuestionRequest` **全部定义在 `app/schemas_workflow.py`**（所有 Pydantic 模型集中，符合第一阶段已建立的 `app/schemas.py` 惯例）；`app/core/registry.py` **只存**（dict + Lock + create/get/touch），一行模型定义都没有。**好处**：`schemas_*.py` = 数据长什么样，`core/` = 数据放在哪、怎么同步，两个问题分开，可以在面试里用一句话讲清。

### B-2 §8 的线程安全说明自相矛盾，且漏了"出图必须登记回 state"

- **矛盾**：§8.2 零件 1 的注释写"带 `threading.Lock`（保护读-改-写，FastAPI 在线程池里跑同步函数）"，零件 2 又写"`tasks.add_task` 会把 `run_workflow` 放到 Starlette 的线程池里执行"。既然 `run_workflow` 只在一个后台线程里顺序执行、`state` 是同一个对象引用，那么**对 `state` 内部字段的写入不需要加锁**；需要锁的只有 `registry` 那个 dict 本身（创建/读取/遍历）。请把"锁保护什么、不保护什么"写成两句话，不要留一个含糊的 `threading.Lock` 让人以为改 `state.status` 也要加锁。

- **漏洞**：`state.charts` 由谁登记没写。如果 `charts.py` 只把 PNG 写进 `data/charts/` 而没把 `ChartResult` 追加到 `state.charts`，接口 8 按 `chart_id` 校验时会 404，而磁盘上却留着一堆孤儿 PNG。**要求写死**：`render_chart()` 返回 `ChartResult` 后，**由 `analysis.py`（任务执行者）追加进 `state.charts`**，即"写文件"和"登记"两步必须在同一个地方连续完成（`charts.py` 不许自己去改 `state`）。

- **补一条已知限制**：`DatasetStore` 也是进程内 dict，所以 §8.2 "只能单进程运行"的边界同样适用于数据集 —— 在 `--workers 4` 下上传和查询落到不同进程会 404。这条要和工作流一起写进 README 的"已知限制"。

### B-3 §10 第 7 条的"源码扫描测试"做法要改

原文建议"测试里读 `app/**/*.py` 文本，断言不出现 `eval(` 这类字符串（字符串里出现 `eval` 要写清是注释）"。**朴素子串扫描会误伤**：注释、文档字符串、以及 `test_security.py` 自己都会包含 `eval(`。

**改为**：扫描时先剥掉注释与文档字符串（用 `ast` 解析，遍历 `ast.Call` 节点判断 `func.id in {"eval","exec","compile","__import__"}`），比子串匹配更准且更好讲 —— 顺带是一个可以写进面试的"用 AST 而不是正则做代码检查"的亮点。

### B-4 §10 第 9 条 `sanitize_detail()` 需要自己的测试

"用正则把 `D:\...` 与 `C:\Users\...` 替换成 `<path>`"这条规则很好，但**没有任何测试守护**，而后端写 `detail` 时会频繁塞进异常原文（`FileNotFoundError: [Errno 2] No such file or directory: 'D:\\个人项目01\\...'`）。要求 `tests/test_security.py` 里加一条：构造一个含本机绝对路径的异常，断言响应体里不出现盘符与用户名。

### B-5 示例数据集命名冲突 + 排期条目要收敛

- 架构文档 §2 写 `data/samples/sales.csv`；task-4 描述写 `data/samples/sales_sample.csv`。**统一为 `data/samples/sales.csv`**（task-4 描述已同步改写）。
- §2 里标 `[D3][LEAD]` 的 `students.xlsx`（第二个演示数据）**从计划里去掉**：演示一个数据集足够讲清楚流程，多一份数据只增加演示时的选择成本。若第 6 天有富余再加。

---

## C. 已核对无误的部分（记录在案，避免重复审核）

| 检查项 | 结论 |
|---|---|
| 接口 1 的 `HealthData` 是否包含 460cf8c 新增的 `upload_dir_ready` | ✅ 已包含，第 733 行明确标注"含 460cf8c 新增的 `upload_dir_ready`" |
| 是否残留 `from app.config import settings` 这种已废弃写法 | ✅ 未发现（§11、§10 第 5 条用的都是 `Settings` / `config.get_settings()` 口径） |
| §7 封面表格是否已与本阶段统一错误外壳对齐 | ✅ 第 719–728 行的表格与 `tests/test_error_format.py` 守护的 404/405/422 三条路径一致 |
| §4 有无 "…" 占位符 | ✅ 按字段级给出，无占位 |
| 是否为每个"不引入"的技术给出触发条件 | ✅ §11 九个技术逐条给出，§8.3 四条升级路径 |

---

## D. 给 architect 的修订清单（做完回消息，不要动 `docs/` 以外的文件）

1. **[阻塞] A-1**：删掉 §7 "业务错误一律 HTTP 200"，改成本文 A-1 那张状态码表；接口表的错误码列补 HTTP 状态码；`POST /api/analysis` 改 202；补上"禁止绕过统一外壳"的约束。
2. **[阻塞] A-2**：写死 `ChartResult` 的 5 个字段与 `url` 由后端拼接；消除 §7 接口 6 / 接口 8 / §9.1 E 区三处对 `charts` 的不一致表述。
3. **[阻塞] A-3**：§2 目录树与 §3 各处按 `app/data/{loader,store,profiler}.py` + `app/errors.py` 收口，并在文档里删掉 `app/datasets.py`、`app/security/files.py` 的提法（如出现过）。
4. **B-1**：`WorkflowState` 等模型统一放 `app/schemas_workflow.py`，`core/registry.py` 只存不定义。
5. **B-2**：重写 §8.2 的锁说明（锁保护 registry dict，不保护 state 内部字段）；补"出图必须由 `analysis.py` 登记回 `state.charts`"；补 `DatasetStore` 的同一进程内限制。
6. **B-3**：§10 第 7 条改用 AST 扫描方案。
7. **B-4**：§10 第 9 条补测试要求。
8. **B-5**：`data/samples/sales.csv` 定名；去掉 `students.xlsx`。
9. 在 §14 追加一节"本轮的 7 项修订"，逐条标注已改/未改及原因。

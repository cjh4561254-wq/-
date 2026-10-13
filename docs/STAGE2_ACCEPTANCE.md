# 第二阶段验收说明

> 本文件记录**第二阶段（数据上传与数据理解）**到底交付了什么、谁验的、证据是什么、
> 以及哪些东西**没有**做。写它的理由：README 只写"怎么用"，决策记录只写"为什么"，
> 而"这一阶段算不算完成"需要一个能逐条对照的地方 —— 尤其是当验收人不是作者本人的时候。

**阶段范围**：最初（2026-10-13 第一次拍板）只做「上传 + 预览 + 数据画像」。
后来用户要求**把第 2 天补完**，于是本阶段追加了「可复现的示例数据集」。
**仍然不含**任何分析工具、**不含**大模型接入 —— 那些属于第三、四天。

**验收基线**：提交 `c15aa92`（示例数据集）之后的收尾提交，工作区干净。
**验收结果**：`149 passed`（仓库根与 `tests/` 子目录两个入口都是 149）。

---

## 一、交付了什么

| 能力 | 入口 | 提交 |
|---|---|---|
| 上传 CSV / xlsx，落盘、解析、登记 | `POST /api/datasets/upload` | `9441429` |
| 数据集详情（含前 5 行预览） | `GET /api/datasets/{dataset_id}` | `9441429` |
| 数据画像（缺失值 / 重复行 / 语义类型 / 五数概括） | `GET /api/datasets/{dataset_id}/profile` | `9441429` |
| 读文件层：编码探测、分隔符探测、最小清洗、行列上限 | `app/data/loader.py` | `34dd3a9` |
| 进程内数据集仓库（**不写盘**） | `app/data/store.py` | `34dd3a9` |
| 画像计算（**零大模型参与**） | `app/data/profiler.py` | `34dd3a9` |
| 启动时把临时目录钉进项目内 `.tmp/` | `app/runtime.py` + `app/main.py` lifespan | `24aeac1` |
| **可复现的示例数据集**（2168 行 × 9 列，含缺失值 / 重复行 / 异常值） | `data/samples/sales.csv` + `scripts/make_sample_data.py` | `c15aa92` |
| 示例数据的行尾策略（保证干净克隆后字节一致） | `.gitattributes` | 本次收尾提交 |

配套：`app/schemas_dataset.py`（数据契约）、`docs/ARCHITECTURE.md`（设计）、
`docs/DECISIONS.md`（15 条决策，每条都写了"为什么"）。

## 二、逐条对照总提示词的验收标准

| 要求 | 状态 | 证据 |
|---|---|---|
| 网页上传 CSV 和 Excel | 接口层 ✅ / 网页 ⏳ | 接口已通；前端在第六天做 |
| 上传文件格式与大小限制 | ✅ | 白名单 `ALLOWED_EXTENSIONS`（415）、`MAX_UPLOAD_MB`（413）、`MAX_ROWS`/`MAX_COLUMNS`（400） |
| 防止路径穿越 | ✅ | 落盘名由 `build_stored_filename` 生成（32 位 hex + 白名单双校验），路由侧再 `resolve()` 复核父目录；`tests/test_upload.py::test_original_filename_never_becomes_a_path` 钉住 |
| 不执行模型生成的代码 | ✅（本阶段无模型） | `app/` 与 `tests/` 下 `eval/exec/compile/__import__` 命中数为 0 |
| 密钥只从环境变量 | ✅ | `.env` 在 `.gitignore`；`llm_api_key` 字段 `repr=False`，实测 `repr(settings)` 不含密钥 |
| 失败响应统一 | ✅ | 四个 exception handler 覆盖 400/404/405/413/415/500/503/200，外壳恒为 `{ok,data,error}` |
| 关键错误能被捕获并清楚显示 | ✅ | 七种失败模式（空文件 / 只有表头 / 伪装扩展名 / 超体积 / 超行 / 超列 / 旧版 .xls）逐个真实请求验过，状态码与 `error.code` 全对 |
| 核心功能有自动化测试 | ✅ | 149 项，全部真实文件 + 真实 HTTP，不 mock |
| 不依赖写死的演示数据 | ✅ | 画像数字由 pandas 现算；手算对账见下 |
| 提供示例数据集 | ✅ | `data/samples/sales.csv`（2168 行 × 9 列）+ 固定 seed 的生成脚本，跑两次 md5 一致（`910d6723…`） |

## 三、独立验证（不是作者自证）

由 `qa-engineer` 在两轮里做**只读复验**，临时探针只写仓库外副本。

**第一轮 · task-9 · 数据层**：结论"可以开 API 层，无阻塞项"。关键项：
- 手算对数：`std = 1.6431676725154984`（= √2.7）、`std = 17.320508075688775`（= √300），
  命中 `ddof=1` 而**不**命中 `ddof=0`（numpy 默认 15.0）；`memory_bytes = 257` 与
  `df.memory_usage(deep=True).sum()` 逐列明细一致。
- 7 种失败模式下 `data/uploads` 的 `listdir` 增量**全是 0**（不留半个文件）。
- `resolve` 守卫**不是永假**：把 `build_stored_filename` monkeypatch 成返回
  `'../outside.csv'` 后，路由真的返回 400 且越界文件不存在。
- 不变式「磁盘上的文件名 == 元信息里的 `stored_filename`」成立。
- 反证：`json.dumps(..., allow_nan=False)` 在极端列（单值 / 全空 / inf）下不抛，
  无 NaN 漏出。

**第二轮 · task-11 · 收尾提交 `24aeac1`**：结论"第三阶段可以在这个基线上开工"，
阻塞项 0、重要问题 0、次要问题 1（一条注释里的跨平台声明与事实不符，**已改**）。
- NaT 修复有牙：在副本里把 `return "datetime", None` 改回 `return "datetime", column`
  → `1 failed, 15 passed`，红在语义行 `assert not any("无法解析为日期" ...)`。
  QA 诚实补充：两条新用例里**只有一条**对本次修复有判别力，另一条（CSV 路径）
  是防回归护栏 —— 因为 CSV 分支本来就没坏过。
- 临时目录三条独立证据全部亲手复现：真实 uvicorn 慢速上传 3MB，
  处理**期间**采样 36 次 → 仓库根 tmp* = 0 次 / `.tmp/` = 22 次；
  import 期只改 `MPLCONFIGDIR` 一个键；退出 lifespan 后环境与缓存都恢复。
- 反向反证：把 `names = ("TMPDIR","TEMP","TMP")` 改成 `names = ()` → `2 failed`。

## 四、真实 HTTP 端到端证据（原始响应）

起真实 uvicorn 后逐个请求，摘录（完整原文见各提交的提交信息）：

```
[200] 上传 3 行 3 列 CSV
      {"ok":true,"data":{"dataset_id":"a94cc120f910441f880b92433205db4e",
       "row_count":3,"column_count":3,"columns":["日期","地区","销售额"],
       "preview_rows":[{"日期":"2024-01-05","地区":"华东","销售额":"1200"}, ...]}}

[415] notes.txt   {"code":"UNSUPPORTED_FILE","message":"不支持的文件类型：txt"}
[413] 11,000,019 字节 > 10MB
      {"code":"FILE_TOO_LARGE","message":"文件太大，上限是 10 MB",
       "detail":"读到第 11000019 字节时超过上限（10485760 字节），没有写入磁盘"}
[400] empty.csv   {"code":"BAD_REQUEST","message":"文件是空的，里面没有任何内容"}
[400] header.csv  {"code":"BAD_REQUEST","message":"文件里只有表头，没有数据行"}
[404] 未登记的 id {"code":"NOT_FOUND","message":"数据集不存在"}
```

**画像数字可手算核对**（销售额 1200 / 3400 / 5600）：

| 指标 | 系统输出 | 手算 |
|---|---|---|
| `mean_value` | 3400.0 | (1200+3400+5600)/3 = 3400 ✓ |
| `std_value` | 2200.0 | 样本标准差 ddof=1 = 2200 ✓ |
| `median_value` | 3400.0 | 中位数 ✓ |
| `q1_value` / `q3_value` | 2300.0 / 4500.0 | ✓ |

**换一份数据，这些数字就跟着变** —— 没有任何写死的结果。

### 示例数据集（`c15aa92`）

`data/samples/sales.csv`：2168 行 × 9 列，2024 全年 × 6 个地区 × 30 个商品。
列：日期 / 地区 / 商品 / 类别 / 销量 / 单价 / 销售额 / 利润 / 客户评分。

**它是故意"脏"的**，因为"检查缺失值和重复记录"是演示问题之一：

| 脏数据 | 实际数量 |
|---|---|
| 缺失值 | 日期 24、销量 18、销售额 18、利润 18、客户评分 27 |
| 完全重复行 | 8 行 |
| 异常值 | 销售额最大 14999850，约为中位数 22267 的 673 倍 |

同时它保证 `销售额 = 销量 × 单价` **精确成立**（整数乘法，无浮点误差），
所以第 3 天可以用 Excel 手算一遍"各地区销售额汇总"来对照工具返回值。

**可复现性**：`scripts/make_sample_data.py` 用固定 seed、标准库 `csv` 写出
（LF 换行 + utf-8-sig），三次运行 md5 均为 `910D67236708A573F85398BBD2E82018`。
脚本是"**写完再用 pandas 读回磁盘上那个文件**"打印统计，所以打印的确实是文件的真实数字，
不是"我打算生成什么"。

**真实上传后的画像**：`datetime=1`（日期）/ `categorical=3`（地区、商品、类别）/
`numeric=5`（销量、单价、销售额、利润、客户评分）；`duplicate_row_count = 8`；
warnings 只有"检测到 8 行完全重复的记录，去重后还剩 2160 行"。
日期列那 24 个空字段**没有**被误报成"无法解析为日期"（这正是 `24aeac1` 修的那个 bug）。

**行尾策略**：本机 `core.autocrlf=true`，若不处理，干净克隆会把 `sales.csv`
的 LF 换成 CRLF（143197 字节 / md5 `6d4f6dad…`），三条逐字节断言会在别人的机器上红。
`.gitattributes` 里 `data/samples/*.csv text eol=lf` 修掉了它（五种写法的实测对比见 D-15）。

## 五、明确**没有**做的（别当成已完成）

- ❌ 前端网页（上传区、预览区、提问框、图表、报告）—— 第六天。
- ❌ 分析工具注册表（`app/tools/`）—— 第三天。
- ❌ 大模型接入与 Tool Calling —— 第四天。
- ❌ SQLite 历史归档、SSE —— 进阶功能，视时间决定。
- ❌ 数据集持久化：**数据存在服务进程内存里，服务重启后全部丢失**（已知限制，见
  `GET /api/datasets/{id}` 的 404 文案）。磁盘上只留上传的原始文件。

## 六、已知边界（不是缺陷，是明确接受的取舍）

| 边界 | 说明 | 记录位置 |
|---|---|---|
| 只收 `.csv` / `.xlsx` | 旧版 `.xls` 是二进制专有格式，pandas 需要已停止维护的 xlrd；拒绝时给"请另存为 .xlsx"的人话 | D-08 |
| 单进程 | 内存态 dict 是唯一事实来源，不能加 `--workers` | ARCHITECTURE §8 |
| 临时目录机制的前提 | 项目根必须可写；仓库若解压到 `%TEMP%` 下，连 pytest 都起不来 | D-07 |
| `encoding` 的语义 | 报的是**文件实际是什么编码**（回头看文件头），不是"哪个解码器读成功了" | D-12 |
| `_read_csv` 不接 `LookupError` | 编码名打错是我们的 bug，不该翻译成"你的文件有问题" | D-13 |
| 示例数据必须靠 `.gitattributes` | 不写它，干净克隆会把 CSV 换成 CRLF，逐字节断言会红 | D-15 |

---

## 七、怎么自己复现这一份验收

```powershell
# 0. 进项目、激活解释器（见 README 的"安装"）
cd D:\个人项目01\ai-data-analyst-agent
$env:PYTHONIOENCODING = "utf-8"   # 否则中文在控制台乱码

# 1. 全量测试（预期 149 passed）
& ".\.venv\Scripts\python.exe" -m pytest -q

# 1b. 示例数据集可复现（两次 md5 必须一致）
& ".\.venv\Scripts\python.exe" scripts\make_sample_data.py
Get-FileHash data\samples\sales.csv -Algorithm MD5   # 期望 910D67236708A573F85398BBD2E82018
& ".\.venv\Scripts\python.exe" scripts\make_sample_data.py
Get-FileHash data\samples\sales.csv -Algorithm MD5   # 必须与上面相同

# 2. 起服务
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --port 8000

# 3. 另开一个窗口，走一遍上传 → 详情 → 画像（见 README「手动跑一遍」）
```

对照本文件第二节的表逐条打勾；任何一条对不上，就说明这份验收说明过期了 ——
**以实际运行结果为准，不要以本文档为准**。

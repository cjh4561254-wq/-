/* ============================================================================
 * AI 市场情报与智能分析平台 —— 前端逻辑
 *
 * 三条必须守住的规矩（改代码前先读这三条）：
 *
 * 1. **绝不按 HTTP 状态码判断成功失败。** 工作流超时的 HTTP 状态码仍然是 200，
 *    失败信息在响应体里的 body.error.code。所以每个请求都先解析响应体，
 *    再看 body.ok / body.error，不用 fetch 响应对象自带的那个布尔值。
 *
 * 2. **不维护「错误码 → 中文」对照表。** 文案一律用后端给的 message，后端改了话术，
 *    页面跟着变，不用改这里。但错误码**不进主显位**：统一渲染成
 *    「中文话术 +（原值码：CODE）」，码只做可查证的标识，不当故障原因用。
 *    `error.detail` 是给开发者的原始诊断（422/500 实测整段是英文），
 *    一律不写进正文，只收进折叠的「原始诊断」区。
 *
 * 3. **轮询只打状态接口，不轮询报告接口。** 只有进终态、且 has_report 为真时，
 *    才去取一次报告（接口 7 的响应体比状态接口大得多）。
 *
 * 另外两条硬约束（有测试钉着）：
 * - 本文件里不得出现任何绝对网址，**注释里的举例也算**（离线可演示）。
 * - 页面显示的数字/状态必须来自后端真实字段；拿不到就写「尚未获取」，
 *   不用占位假数字，也不用定时器假装进度。
 * ========================================================================== */

(function () {
  "use strict";

  /* ========================================================== 接口地址表 */

  // 与后端逐条对应。改路径只改这里，别在别处硬编码。
  // 用模板字符串而不是拼接：tests/test_frontend.py 会把这几条路径当**字面量**核对，
  // 拼出来的字符串里没有完整的 `/api/datasets/${id}/profile`，那条用例会判红。
  const API = {
    health: "/api/health",
    // 会话三件事。**不缓存"我登录过"这种本地标记**：每次加载页面都拿 cookie
    // 去问服务端一次，因为本地标记只能让页面看起来登录着，拦不住任何人。
    me: "/api/auth/me",
    logout: "/api/auth/logout",
    password: "/api/auth/password",
    sessions: "/api/auth/sessions",
    upload: "/api/datasets/upload",
    // 数据集列表（后端 task-11 落地）。排序与分页都走后端参数，前端不假装排序。
    datasets: "/api/datasets",
    dataset: (id) => `/api/datasets/${id}`,
    profile: (id) => `/api/datasets/${id}/profile`,
    analysis: "/api/analysis",
    workflow: (id) => `/api/workflows/${id}`,
    report: (id) => `/api/workflows/${id}/report`,
    chart: (workflowId, filename) => `/api/workflows/${workflowId}/charts/${filename}`,
    history: "/api/workflows/history",
    historyDetail: (id) => `/api/workflows/history/${id}`,
  };

  /* ============================================================ 常量 */

  // 三个终态。少写一个，这类工作流就会永远转圈 —— 这是最容易犯的错。
  const TERMINAL = ["SUCCESS", "SUCCESS_WITH_WARNINGS", "FAILED"];

  // 只有这个码值得让用户点「重试」（服务暂时不可用）。
  const RETRYABLE = ["SERVICE_UNAVAILABLE"];

  // 轮询间隔（毫秒）。1 秒足够跟手，也不会把后端打满。
  const POLL_INTERVAL_MS = 1000;

  // 六个阶段的顺序，必须与后端 app/agents/orchestrator.py 的 _STEPS 一字不差。
  // 有测试逐项比对（tests/test_frontend.py），差一个字母圆点会安静地不亮。
  const STEP_ORDER = ["profiling", "planning", "execution", "charting", "reporting", "validation"];

  const STEP_LABELS = {
    profiling: "正在读取数据",
    planning: "正在制定分析计划",
    execution: "正在计算",
    charting: "正在生成图表",
    reporting: "正在撰写报告",
    validation: "正在校验数字",
  };

  // 阶段面板上**显示**用的中性名。STEP_LABELS 里的"正在…"是阶段词汇的中文说明
  // （既有测试逐条核对它），但直接当条目名用会出现「正在读取数据 · 等待执行」
  // 这种自相矛盾的一行 —— 一个还在排队的阶段不能同时"正在"做什么。
  // 所以列表里用中性动词短语，进行中的事实由右侧那格状态文字负责。
  var STEP_NAMES = {
    profiling: "读取数据",
    planning: "制定分析计划",
    execution: "计算",
    charting: "生成图表",
    reporting: "撰写报告",
    validation: "校验数字",
    // 后端一共给 7 个 step 值：六个阶段之外，orchestrator.py 的 _finish()/_fail()
    // 都会把 step 写成 "done"（表示"别再等它了"）。它不在 STEP_ORDER 里，
    // 但轮询状态行每天都见得到它 —— 没有这一条，界面上就会逐秒画出裸的 done。
    done: "已完成",
  };

  // 阶段面板上那行小字。等待 / 进行中 / 已完成 / 失败 / 未执行 —— 五种都写出来，
  // 因为颜色不能是唯一的信息载体（色盲用户、黑白打印都要能看懂）。
  var STEP_STATE_TEXT = {
    waiting: "等待执行",
    active: "正在执行",
    done: "已完成",
    failed: "执行失败",
    skipped: "未执行",
  };

  // 任务级状态来自后端的 TaskStatus 枚举（六个值，含 SKIPPED）。
  // 这里是真的能拿到「已跳过」的地方 —— 阶段级没有跳过这回事，别硬造。
  var TASK_STATUS_TEXT = {
    PENDING: "等待执行",
    RUNNING: "正在执行",
    SUCCESS: "已完成",
    FAILED: "执行失败",
    RETRYING: "重试中",
    SKIPPED: "已跳过",
  };

  // 图型中文名。后端 ChartType 是 bar/line/pie/scatter 四个值；
  // 认不出来的值**原样显示**，不猜。
  var CHART_TYPE_TEXT = {
    bar: "柱状图",
    line: "折线图",
    pie: "饼图",
    scatter: "散点图",
  };

  /* ================================================== 中文化：枚举 → 中文 */

  /* 口径：这一组表只改"显示"，不改"取值"—— 键必须是后端字面量（有测试逐条比对枚举值）。
     兜底：命中 → 中文主显 + title 写原值；未命中 → 中文"未识别的…" + 原值进可见的 .origin 小字。
     未命中绝不回落到"猜一个中文"（EXECUTING 猜成"计算中"就是撒谎）。
     逐处接法与实测取证见 docs/refactor/04-i18n-checklist.md。 */

  // 后端 WorkflowStatus 全 10 值（app/schemas_workflow.py）。
  var WORKFLOW_STATUS_TEXT = {
    PENDING: "等待中",
    PROFILING: "读取数据中",
    PLANNING: "制定计划中",
    EXECUTING: "计算中",
    CHARTING: "出图中",
    REPORTING: "写报告中",
    VALIDATING: "校验中",
    SUCCESS: "成功",
    SUCCESS_WITH_WARNINGS: "成功（有告警）",
    FAILED: "失败",
  };

  // pandas dtype 是开放集，这里只给常见 8 值；未命中走 zhEnum 兜底，不猜。
  var DTYPE_TEXT = {
    int64: "整数", int32: "整数", float64: "小数", float32: "小数",
    bool: "是/否", object: "文本或混合", str: "文本",
    "datetime64[ns]": "日期时间", datetime64: "日期时间",
  };

  // 校验问题码：app/agents/validation.py 里真实出现的 6 个。
  var VALIDATION_ISSUE_TEXT = {
    FAILED_TASK: "有任务失败",
    EMPTY_RESULT: "有任务没有结果",
    MISSING_CHART_FILE: "图表文件缺失",
    UNSUPPORTED_NUMBER: "数字找不到出处",
    UNKNOWN_COLUMN: "引用了不存在的列",
    SHORT_REPORT: "报告正文过短",
  };

  // 错误码 hint：只补一句"这个码大概是什么意思"，永远不改写后端给的 message。
  var ERROR_HINT = {
    BAD_REQUEST: "请求不符合接口要求",
    NOT_FOUND: "要找的东西不在服务端",
    METHOD_NOT_ALLOWED: "请求方法不支持",
    UNSUPPORTED_FILE: "文件类型不支持",
    FILE_TOO_LARGE: "文件超出大小上限",
    SERVICE_UNAVAILABLE: "服务暂时不可用",
    WORKFLOW_TIMEOUT: "分析超过时间上限",
    INTERNAL_ERROR: "服务端内部错误",
    UNAUTHORIZED: "需要登录",
    FORBIDDEN: "没有权限",
    RATE_LIMITED: "操作太频繁",
    UNKNOWN: "服务没有给出原因",
  };

  // 列名表：**只收我们工具自产的结构键**，绝不收用户自己的列名（那是猜语义）。
  var COLUMN_LABELS = {
    column: "列名", null_count: "缺失个数", null_ratio: "缺失比例",
    unique_count: "不重复取值数", row_count: "行数", group_count: "组数",
    pair_count: "比较的对数", valid_pair_count: "有效对数",
    count: "行数", x: "第一列", y: "第二列", r: "相关系数",
    value: "取值", name: "名称", size_bytes: "字节数",
  };

  // 聚合后缀的取值集合来自 app/tools/registry.py 的枚举，不是猜的。
  var AGG_SUFFIX_TEXT = {
    sum: "合计", mean: "平均", count: "行数",
    min: "最小", max: "最大", median: "中位",
  };

  function zhEnum(map, raw, label) {
    var known = Object.prototype.hasOwnProperty.call(map, raw);
    return {
      text: known ? map[raw] : "未识别的" + label,
      origin: known ? null : String(raw),
      raw: String(raw),
    };
  }

  function zhEnumNode(map, raw, label, className) {
    var info = zhEnum(map, raw, label);
    var node = el("span", className || null, info.text);
    node.title = "服务端原值：" + info.raw;
    if (info.origin) {
      node.appendChild(el("span", "origin", info.origin));
    }
    return node;
  }

  // 错误码降级成"原值码"：主显是中文话术，码只作标识。
  function errorCodeNode(code, className) {
    var raw = String(code || "UNKNOWN");
    var node = el("span", className || "state", "（原值码：" + raw + "）");
    node.title = "错误码是技术标识，不是故障原因；故障原因见上面那句。服务端原值：" + raw;
    return node;
  }

  // 归档里那条失败原因：中文说明在前（ERROR_HINT 只做提示增强，不改写后端给的 message），
  // 原码显形可查证 —— 触屏设备没有 hover，只放 title 等于把信息藏起来。
  function errorReasonNode(code) {
    var raw = String(code || "");
    var node = el("span", "error-text", ERROR_HINT[raw] || "失败原因（服务端未说明）");
    node.appendChild(el("span", "origin", raw));
    node.title = "服务端原值：" + raw;
    return node;
  }

  function rawTimeTitle(iso) {
    return iso ? "原始时间戳（UTC）：" + iso : "";
  }

  /* 列名展示名：只做无损的确定性格式化（分词、去 _id 后缀、派生后缀换中文括注、超长截断），
     含中文等非 ASCII 的键原样返回 —— 用户自己的列名不许机器翻译。
     取值一律继续用原始键，只有展示文本变（见 docs/refactor/02 §5.2 的 data-col/data-label）。 */
  function displayColumn(rawKey) {
    var key = String(rawKey === null || rawKey === undefined ? "" : rawKey);
    if (!key) {
      return "";
    }
    if (/[^\x00-\x7F]/.test(key)) {
      return key;
    }
    if (Object.prototype.hasOwnProperty.call(COLUMN_LABELS, key)) {
      return COLUMN_LABELS[key];
    }
    var derived = key.match(/^(.*)_(sum|mean|count|min|max|median)$/);
    if (derived && derived[1]) {
      return displayColumn(derived[1]) + "（" + AGG_SUFFIX_TEXT[derived[2]] + "）";
    }
    var out = key.replace(/_id$/, " 编号").replace(/_/g, " ");
    if (out.length > 24) {
      out = out.slice(0, 22) + "…";
    }
    return out;
  }

  // 表头/单元格里"展示名 + 原始键"两个都要在：展示名给人看，原始键供排查与窄屏堆叠。
  function gridColumnCell(key) {
    var shown = displayColumn(key);
    var th = el("th", null, shown);
    th.setAttribute("scope", "col");
    th.setAttribute("data-col", String(key));
    if (shown !== String(key)) {
      th.title = "原始列名：" + key;
    }
    return { node: th, label: shown };
  }

  function timeNode(label, iso, className) {
    var node = el("span", className || null, label + fmtDateTime(iso));
    if (iso) {
      node.title = "原始时间戳（UTC）：" + iso;
    }
    return node;
  }

  // 图表横/纵轴上的值本身就是列名 → 走同一个 displayColumn 口径，不另建第二张表。
  function axisFieldNode(label, rawField) {
    var shown = displayColumn(rawField);
    var node = el("span", null, label + shown);
    if (shown !== String(rawField)) {
      node.title = "原始列名：" + rawField;
    }
    return node;
  }

  // 每个视图的面包屑与标题（#crumb / #cmdbar-title / #page-title / #page-sub）。
  var VIEWS = {
    command: {
      crumb: "战略态势",
      title: "战略态势带",
      sub: "最近完成的分析里，用户原话与报告首段结论并排放在一起；每条都能点开原文逐句核对",
    },
    workbench: {
      crumb: "工作台",
      title: "分析工作台",
      sub: "上传市场数据 → 用自然语言提问 → 真实计算 + 图表 + 可逐条核对的报告",
    },
    dataset: {
      crumb: "数据资产",
      title: "数据集概览",
      sub: "字段结构、数据预览与质量信息，全部来自服务端真实字段",
    },
    analysis: {
      crumb: "发起分析",
      title: "自然语言分析",
      sub: "把问题交给服务端的多步编排：规划任务 → 数据计算 → 出图 → 写报告 → 校验数字",
    },
    runs: {
      crumb: "Agent 运行",
      title: "Agent 运行过程",
      sub: "六个阶段的真实进度与任务明细：数据只来自服务端的 step / step_index / status，不做本地估算",
    },
    report: {
      crumb: "分析报告",
      title: "图表与分析报告",
      sub: "图表由服务端按真实数据生成；报告正文里每个数字都能回到某次工具调用的结果",
    },
    history: {
      crumb: "历史记录",
      title: "历史记录",
      sub: "跑过的分析都留在这里：关掉页面、重启服务，记录都还在",
    },
    settings: {
      crumb: "设置",
      title: "账号与运行状态",
      sub: "当前账号、服务状态与生效中的配置是只读的；可以改自己的密码，也可以看这个账号在哪些会话里登录着",
    },
  };

  /* 顶栏面包屑的分组前缀：与 index.html 里 .nav__group 的三段标题逐字一致，
     所以"你在哪"这件事在导航和顶栏是同一套说法（导航三段标题＝指挥 / 分析 / 成果；
     设置不在侧栏，入口在命令条的账户菜单里，前缀单独给"账户"）。 */
  var VIEW_GROUP = {
    command: "指挥",
    workbench: "指挥",
    dataset: "指挥",
    analysis: "分析",
    runs: "分析",
    report: "成果",
    history: "成果",
    settings: "账户",
  };

  // 首屏就是态势带（侧栏第一项、index.html 里唯一不带 hidden 的视图都是 view-command）。
  var DEFAULT_VIEW = "command";

  /* ============================================================ 状态 */

  var state = {
    datasetId: null,
    dataset: null, // 最近一次 GET /api/datasets/{id} 的数据
    profile: null, // 最近一次 GET /api/datasets/{id}/profile 的数据
    workflowId: null,
    pollTimer: null,
    uploading: false,
    asking: false,
    droppedFile: null, // 拖进来的文件（<input type=file> 的 files 不能被脚本改写）
    view: DEFAULT_VIEW,
    historyLoaded: false,
  };

  // 状态接口（GET /api/workflows/{id}）交回的 tasks：task_id / description / status / error。
  // 报告接口 7 里没有 description（它只给 results），而证据行要说"这次取数在算什么"，
  // 所以把**同一条运行**的状态响应里的 TaskBrief 原样留住，按 workflow_id 认人。
  // 服务重启后状态接口 404，这里就自然没有值 —— 证据行落回"工具明细读不到"那条分支，
  // 不会拿上一条运行的描述冒充这一条。
  var liveRunTasks = { workflowId: null, tasks: null };

  function captureLiveRunTasks(workflowId, tasks) {
    if (!workflowId || !Array.isArray(tasks)) {
      return;
    }
    if (liveRunTasks.workflowId !== workflowId) {
      liveRunTasks = { workflowId: workflowId, tasks: [] };
    }
    liveRunTasks.tasks = tasks;
  }

  function liveRunTasksFor(workflowId) {
    return liveRunTasks.workflowId && liveRunTasks.workflowId === workflowId ? liveRunTasks.tasks : null;
  }

  /* ====================================================== DOM 小工具 */

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, className, content) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (content !== undefined && content !== null) {
      node.textContent = String(content);
    }
    return node;
  }

  function clear(node) {
    if (!node) {
      return;
    }
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  // 内联 SVG 图标。为什么要走 innerHTML 拼这一小段静态字符串：
  // 用 createElementNS 需要写命名空间网址，而本文件里不允许出现任何绝对网址。
  // 这里拼进去的只有下面白名单里的名字，没有任何用户数据。
  var ICON_NAMES = [
    "upload", "dataset", "analysis", "report", "history", "settings",
    "check", "warn", "error", "info", "refresh", "close", "chevron",
    "help", "spark", "menu", "download", "external", "search",
  ];

  function icon(name) {
    if (ICON_NAMES.indexOf(name) < 0) {
      return null;
    }
    var holder = document.createElement("span");
    holder.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-' + name + '"></use></svg>';
    return holder.firstChild;
  }

  function emptyBlock(text, iconName) {
    var node = el("p", "empty empty--inline");
    var glyph = icon(iconName || "info");
    if (glyph) {
      node.appendChild(glyph);
    }
    node.appendChild(document.createTextNode(text));
    return node;
  }

  function setBusy(button, busy) {
    if (!button) {
      return;
    }
    button.disabled = !!busy;
    if (busy) {
      button.setAttribute("data-loading", "true");
    } else {
      button.removeAttribute("data-loading");
    }
  }

  /* ==================================================== 格式化小工具 */

  // 后端的时间都是 UTC，但 ISO 字符串有时不带时区后缀。
  // 不带后缀时浏览器会按**本地时区**理解，算出来的「已用时」会平白差 8 小时，
  // 所以这里只做一件事：没有时区信息就补一个 Z。
  function parseUtc(value) {
    if (!value || typeof value !== "string") {
      return null;
    }
    var text = value.trim();
    if (!text) {
      return null;
    }
    var hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(text);
    var ms = Date.parse(hasZone ? text : text + "Z");
    return isNaN(ms) ? null : ms;
  }

  function formatSeconds(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) {
      return "—";
    }
    if (seconds < 60) {
      return seconds.toFixed(1) + " 秒";
    }
    var minutes = Math.floor(seconds / 60);
    var rest = Math.round(seconds - minutes * 60);
    // 秒数四舍五入后可能正好进位到 60（实测 179.6 秒会显示成「2 分 60 秒」），
    // 这里把它归一化成「3 分 0 秒」——数字必须读得通，不能把取整误差直接摆给用户看。
    if (rest === 60) {
      minutes += 1;
      rest = 0;
    }
    return minutes + " 分 " + rest + " 秒";
  }

  function fmtInt(value) {
    if (value === null || value === undefined || value === "") {
      return "—";
    }
    var number = Number(value);
    if (isNaN(number)) {
      return String(value);
    }
    return number.toLocaleString("zh-CN");
  }

  function fmtBytes(value) {
    var number = Number(value);
    if (value === null || value === undefined || isNaN(number)) {
      return "—";
    }
    if (number < 1024) {
      return number + " 字节";
    }
    if (number < 1024 * 1024) {
      return (number / 1024).toFixed(1) + " KB";
    }
    return (number / 1024 / 1024).toFixed(1) + " MB";
  }

  function fmtPercent(ratio) {
    var number = Number(ratio);
    if (ratio === null || ratio === undefined || isNaN(number)) {
      return "—";
    }
    return (number * 100).toFixed(1) + "%";
  }

  function isNumericLike(value) {
    if (typeof value === "number") {
      return isFinite(value);
    }
    if (typeof value !== "string") {
      return false;
    }
    return /^-?\d[\d,]*\.?\d*$/.test(value.trim());
  }

  /* ======================================================= 请求封装 */

  // 统一的响应外壳：{ok, data, error}。任何情况下都返回这个形状，
  // 调用方只看 body.ok / body.error，不看 HTTP 状态码。
  async function api(path, options) {
    var res;
    try {
      res = await fetch(path, options);
    } catch (cause) {
      return {
        ok: false,
        data: null,
        error: {
          code: "NETWORK_ERROR",
          message: "连不上服务，请确认后端还在运行",
          detail: String((cause && cause.message) || cause || ""),
        },
      };
    }

    var body = null;
    try {
      body = await res.json();
    } catch (cause) {
      body = null;
    }

    // 会话失效：**这是全文件唯一一处看 HTTP 状态码的地方**，而且只用来回答
    // 一个问题——"我的身份还在不在"。它不参与任何业务成败判断：
    // 业务成功失败仍然只看 body.ok / body.error.code（工作流超时的 HTTP
    // 状态码是 200，那条规矩没有被动过）。
    //
    // 两个条件都要认：
    //   - body.error.code === "UNAUTHORIZED"：后端业务码，正常路径；
    //   - HTTP 401：框架层抛出的 401（比如以后加了别的鉴权中间件），
    //     它可能不带我们的统一外壳，只认业务码会漏掉。
    // 例外：`expectUnauthorized` 为真时不跳登录页 —— 改密码这类请求把 401 当**业务答案**
    // （服务端用同一个码表达"当前密码不正确"），跳走会把一句正常的拒绝变成整页登出。
    if (
      (res.status === 401 || (body && body.error && body.error.code === "UNAUTHORIZED")) &&
      !(options && options.expectUnauthorized)
    ) {
      sessionLost();
    }

    if (!body || typeof body !== "object" || Array.isArray(body)) {
      return {
        ok: false,
        data: null,
        error: {
          code: "BAD_RESPONSE",
          message: "服务返回了无法解析的响应（HTTP " + res.status + "）",
          detail: null,
        },
      };
    }
    return body;
  }

  function errorOf(result) {
    if (result && result.error && typeof result.error === "object") {
      return result.error;
    }
    return { code: "UNKNOWN", message: "出了点问题，但服务没有给出原因", detail: null };
  }

  function isRetryable(error) {
    return !!error && RETRYABLE.indexOf(error.code) >= 0;
  }

  // 就地错误框：主显是后端给的 message，码降级成"原值码"，
  // detail（原始诊断，422/500 实测整段是英文）收进折叠区，不占正文。
  function renderErrorBox(box, error, onRetry) {
    if (!box) {
      return;
    }
    clear(box);
    box.hidden = false;

    box.appendChild(el("strong", null, error.message || "请求失败"));
    box.appendChild(errorCodeNode(error.code));

    if (error.code === "WORKFLOW_TIMEOUT") {
      box.appendChild(el("p", "detail", "工作流超过了时间上限，已按超时结束（注意：这个错误的 HTTP 状态码仍然是 200）。"));
    }
    if (error.detail) {
      var diag = el("details", "diag");
      diag.appendChild(el("summary", null, "原始诊断（给开发者排查用，可能整段是英文）"));
      diag.appendChild(el("pre", "diag__raw", String(error.detail)));
      box.appendChild(diag);
    }

    if (typeof onRetry === "function") {
      var row = el("div", "row");
      var retry = el("button", "btn btn--ghost", "重试");
      retry.type = "button";
      retry.addEventListener("click", function () {
        hideErrorBox(box);
        onRetry();
      });
      row.appendChild(retry);
      box.appendChild(row);
    }
  }

  function hideErrorBox(box) {
    if (!box) {
      return;
    }
    box.hidden = true;
    clear(box);
  }

  /* ============================================ 会话门（真实登录态） */

  // 会话状态只活在内存里，而且**只以服务端的回答为准**：
  // GET /api/auth/me 说 authenticated=false，页面上就不该留下任何"像是登录着"的痕迹。
  // 这里刻意不写 localStorage/sessionStorage：本地标记拦不住任何人，
  // 只会让"没登录"看起来像"登录了"，那是比 401 更难查的故障。
  var session = {
    user: null,     // 服务端返回的 user（display_name / username / created_at）
    expiresAt: null, // session_expires_at：会话什么时候到期，如实显示
    lost: false,    // 已经因为会话失效跳过一次，防止连环跳
  };

  // 登录页地址。后端 /login 返回登录页（app/web/login.html），是**同一台服务**上的
  // 一个路径，不是外部站点 —— 本项目不允许任何外链（离线可演示）。
  var LOGIN_PATH = "/login";

  // 登录后要回到哪：pathname + search + hash 全带上。
  // 这个应用是 hash 路由（#/history、#/report 都是同一个文档），
  // 只带 pathname 的话登录回来一律落在工作台，"回跳"就是假的。
  var APP_PATH = "/app";

  function currentTarget() {
    var path = window.location.pathname;
    var hash = window.location.hash;
    // 根路径 "/" 提供的是同一个应用外壳（app/main.py 的 root() 故意不要求登录），
    // 但登录页校验 next 时只认 /app 开头的同源路径（login.js 的 safeNextTarget）。
    // 不归一化的话，从 "/#/report/<id>" 进来的人登录后会被丢到工作台 —— 深链接白给了。
    if (path === "/" || path === "" || path === "/index.html") {
      return APP_PATH + hash;
    }
    // next 绝不能再指回登录页自己（那会变成"登录成功 → 又回登录页"的循环）。
    if (path === LOGIN_PATH) {
      return APP_PATH + "#/" + DEFAULT_VIEW;
    }
    return path + window.location.search + hash;
  }

  // reason 只有三种：
  //   "bootstrap" —— 账号库还空着（服务端 needs_bootstrap=true），先去建第一个管理员；
  //   "anonymous" —— 没登录；
  //   "expired"   —— 请求过程中会话失效（401）。
  // 后两种带 next=，登录成功后由登录页负责回跳。
  function toLogin(reason) {
    var query;
    if (reason === "bootstrap") {
      query = "?bootstrap=1";
    } else {
      query = "?next=" + encodeURIComponent(currentTarget());
      // "会话失效"必须和"从来没登录过"分开：两者都跳登录页，但用户的心情完全不同，
      // 登录页得能说清"你是被踢出来的、刚才那一步没做完"。多带这一个参数，
      // 登录页才能给出正确的一句话（不带它就只能猜，或者什么都不说）。
      if (reason === "expired") {
        query += "&reason=expired";
      }
    }
    // 用 replace 而不是赋值 href：跳走之后按浏览器「后退」不该退回一个
    // 刚刚确认没权限的页面，否则用户看到的是"退回来又被弹走"的抖动。
    window.location.replace(LOGIN_PATH + query);
  }

  // 会话失效的统一出口。**不能只跳转**：内存里还留着上一个用户的
  // datasetId / workflowId / 图表 / 报告，同一台机器换个人登录就会先看到别人的数据。
  // 所以先把这些就地清空、停掉轮询，再跳。
  function sessionLost() {
    if (session.lost) {
      return;
    }
    session.lost = true;
    session.user = null;
    session.expiresAt = null;
    state.datasetId = null;
    state.dataset = null;
    state.profile = null;
    state.workflowId = null;
    stopPolling();
    // 态势带缓存的是**上一个用户的报告正文**：不清的话，换个人登录会先看到别人的结论。
    postureCache.clear();
    posturePending.clear();
    resetPosture();
    resetWorkflowPanel();
    clearCharts();
    clearReport();
    clearUserMenu();
    // 顺序：先把界面切成"正在去登录页"，再真的跳。
    // 跳转是异步的（新文档要下载），这中间有几百毫秒；如果不切，
    // 用户看到的是一个数据被清空、却没有解释的工作台 —— 那正是"假装还登录着"。
    showRedirectGate("会话已失效，正在前往登录页…");
    toLogin("expired");
  }

  // 会话门的白卡片。初始 HTML 里就有一个 .authgate__box；但 showStartupError /
  // showRedirectGate 会 clear 掉门里的内容，所以这里统一"取现有的，没有就建一个"，
  // 免得错误态的文案变成直接裸在页面上（丢掉卡片底色与边框）。
  function gateBox() {
    var gate = $("auth-gate");
    if (!gate) {
      return null;
    }
    var box = gate.querySelector(".authgate__box");
    if (!box) {
      box = el("div", "authgate__box");
      gate.appendChild(box);
    }
    return box;
  }

  function setText(id, text) {
    var node = $(id);
    if (node) {
      node.textContent = (text === null || text === undefined || text === "") ? "—" : String(text);
    }
  }

  // 会话到期时间来自 /api/auth/me 的 session_expires_at（ISO 字符串）。
  // 解析不了就写「—」：宁可不显示，也不显示一个编出来的时间。
  function fmtDateTime(iso) {
    if (!iso) {
      return "—";
    }
    var date = new Date(iso);
    if (isNaN(date.getTime())) {
      return "—";
    }
    return date.toLocaleString("zh-CN", { hour12: false });
  }

  // 把真实用户放进顶栏。显示名优先（人看的是名字），账号名进菜单与 tooltip。
  // 服务端没给 display_name 就回落到账号名 —— 回落到另一个真实字段，
  // 不是随手编一个"用户"。
  function renderUser() {
    var user = session.user;
    var button = $("user-btn");
    var name = user ? (user.display_name || user.username || "") : "";
    if (!user || !button || !name) {
      return;
    }
    var initial = $("user-initial");
    if (initial) {
      initial.textContent = Array.from(name)[0];
    }
    var display = $("user-display");
    if (display) {
      display.textContent = name;
    }
    button.hidden = false;
    button.title = "已登录：" + name +
      (user.username && user.username !== name ? "（" + user.username + "）" : "");

    setText("user-name", name);
    setText("user-account", user.username);
    setText("user-expiry", fmtDateTime(session.expiresAt));
    // 侧栏栏底的会话到期一行：**只写服务端给的那个真实时间戳**，
    // 前端不倒计时、不推算剩余时长（06 D-6/D-7：宁可少画一格也不造字段）。
    // 取不到 session_expires_at 就整块不显示 —— 空值比假值诚实。
    var sideSession = $("sidebar-session");
    if (sideSession) {
      if (session.expiresAt) {
        setText("sidebar-session-expires", fmtDateTime(session.expiresAt));
        sideSession.hidden = false;
        sideSession.title = "原始时间戳（UTC）：" + session.expiresAt;
      } else {
        sideSession.hidden = true;
      }
    }
    // 设置页的「当前账号」三行：同一份真实数据，换一处展示（id 不同，不重复用 id）。
    setText("account-name", name);
    setText("account-login", user.username);
    setText("account-expiry", fmtDateTime(session.expiresAt));

    var gate = $("auth-gate");
    if (gate) {
      gate.hidden = true;
    }
    // body 上的 data-auth 也是给 CSS 看的：确认登录态之前，
    // 工作台与顶栏整体不显示，避免"先闪一屏工作台再被弹走"。
    document.body.setAttribute("data-auth", "ready");
  }

  // 清掉顶栏的用户痕迹（退出 / 会话失效时调用）。不是"隐藏一下"：
  // 文本也换成占位符，别让下一个用这台机器的人看到上一个人的名字。
  function clearUserMenu() {
    var button = $("user-btn");
    if (button) {
      button.hidden = true;
      button.setAttribute("aria-expanded", "false");
    }
    var popover = $("user-popover");
    if (popover) {
      popover.hidden = true;
    }
    setText("user-display", "");
    setText("user-name", "");
    setText("user-account", "");
    setText("user-expiry", "");
    setText("account-name", "");
    setText("account-login", "");
    setText("account-expiry", "");
    var sideSession = $("sidebar-session");
    if (sideSession) {
      sideSession.hidden = true;
    }
  }

  // 启动阶段拿不到登录态时的处理。**不要把用户丢到登录页** ——
  // 那会把"服务端挂了 / 账号库 503"伪装成"你没登录"，用户会去反复输密码。
  // 就地写清真实原因，并给一个真的能重试的按钮。
  // 跳转途中（或跳转被拦下时）的过渡界面。给一个**真的链接**兜底：
  // location.replace 在个别内嵌浏览器里会被拦，那时候用户至少还有一条路可走，
  // 而不是盯着一句"正在跳转"等到怀疑人生。
  function showRedirectGate(message) {
    var gate = $("auth-gate");
    if (!gate) {
      return;
    }
    gate.hidden = false;
    gate.classList.remove("authgate--error");
    clear(gate);
    var box = gateBox();
    var spinner = el("span", "authgate__spinner");
    spinner.setAttribute("aria-hidden", "true");
    box.appendChild(spinner);
    box.appendChild(el("strong", null, message || "正在前往登录页…"));
    var link = el("a", "btn btn--primary authgate__link", "立即前往登录页");
    link.setAttribute("href", LOGIN_PATH);
    box.appendChild(link);
  }

  function showStartupError(error) {
    var gate = $("auth-gate");
    if (!gate) {
      return;
    }
    gate.hidden = false;
    gate.classList.add("authgate--error");
    clear(gate);
    var box = gateBox();
    box.appendChild(el("strong", null, "无法确认登录状态"));
    box.appendChild(el("p", null, (error && error.message) || "请求失败"));
    box.appendChild(errorCodeNode(error && error.code));
    var retry = el("button", "btn btn--primary", "重试");
    retry.type = "button";
    retry.addEventListener("click", function () {
      window.location.reload();
    });
    box.appendChild(retry);
  }

  // 会话门是整页的开关，**不能无限转下去**：后端端口开着但不响应（TCP 挂起）时
  // fetch 会一直 pending，页面上就是一个永远转的圈 —— 看起来像坏了，却没有任何信息。
  // 所以给这一步单独加超时，并且**只加在会话门上**：业务请求的合理时长各不相同
  // （上传大文件、工作流轮询），一刀切一个超时会把正常的长任务判成失败。
  var SESSION_TIMEOUT_MS = 8000;

  function withTimeout(promise, ms, code, message) {
    var timer = null;
    var expired = new Promise(function (resolve) {
      timer = setTimeout(function () {
        resolve({ ok: false, data: null, error: { code: code, message: message, detail: null } });
      }, ms);
    });
    return Promise.race([promise, expired]).then(function (result) {
      if (timer) {
        clearTimeout(timer);
      }
      return result;
    });
  }

  // 启动时第一件事：问服务端「我是谁」。返回值就是「能不能进工作台」。
  async function checkSession() {
    var result = await withTimeout(
      api(API.me),
      SESSION_TIMEOUT_MS,
      "SESSION_TIMEOUT",
      "询问登录状态超时（" + SESSION_TIMEOUT_MS / 1000 + " 秒无响应），后端可能已经卡住"
    );
    if (!result.ok) {
      // 401 已经被 api() 统一处理成"跳登录页"了（sessionLost），
      // 这里不能再盖一层错误框 —— 那会让人以为是自己操作错了。
      if (session.lost) {
        return false;
      }
      showStartupError(errorOf(result));
      return false;
    }
    var data = result.data || {};
    if (data.authenticated !== true) {
      toLogin(data.needs_bootstrap === true ? "bootstrap" : "anonymous");
      return false;
    }
    session.user = data.user || null;
    session.expiresAt = data.session_expires_at || null;
    if (!session.user) {
      // authenticated=true 却给不出用户对象（会话有效、账号刚被删的竞态）。
      // 不装作登录成功 —— 按会话失效处理。
      sessionLost();
      return false;
    }
    renderUser();
    return true;
  }

  /* ================================================== 顶栏用户菜单 */

  function toggleUserMenu(force) {
    var popover = $("user-popover");
    var button = $("user-btn");
    if (!popover || !button) {
      return;
    }
    var open = force === undefined ? popover.hidden : !!force;
    popover.hidden = !open;
    button.setAttribute("aria-expanded", open ? "true" : "false");
  }

  // 退出登录：真的调 POST /api/auth/logout（服务端销毁会话 + 清 cookie），
  // 成功之后整页跳 /login。不做"只改界面"的假退出 —— cookie 还在的话，
  // 刷新一下又进去了，那是比报错更糟的一种"看起来对"。
  //
  // 两个入口（右上角用户菜单、设置页「当前账号」）**共用这一个函数**，
  // 只是把按钮与状态行的 id 传进来。退出只有一条路径：复制第二份逻辑，
  // 迟早会分叉成"一边清了会话、一边没清"。
  async function logout(buttonId, stateId) {
    var button = $(buttonId || "logout");
    var status = $(stateId || "logout-state");
    if (button) {
      button.disabled = true;
      button.setAttribute("data-loading", "true");
    }
    if (status) {
      status.className = "state";
      status.textContent = "正在退出…";
    }

    var result = await api(API.logout, { method: "POST" });
    if (!result.ok) {
      // 退出失败**不能装作已退出**：会话可能还在，cookie 也可能还在。
      // 如实报错，用户还能再点一次。
      var error = errorOf(result);
      if (button) {
        button.disabled = false;
        button.removeAttribute("data-loading");
      }
      if (status) {
        status.className = "state error-text";
        status.textContent = "退出失败：" + error.message + "（" + error.code + "）";
      }
      return;
    }

    session.user = null;
    session.expiresAt = null;
    clearUserMenu();
    window.location.replace(LOGIN_PATH);
  }

  /* ====================================================== 视图与路由 */

  // 哈希里除了视图名，还可能带一个真实 id（task-14 的 D3 定案）：
  //   #/dataset/{dataset_id}   #/report/{workflow_id}   #/history/{workflow_id}
  // 为什么用路径而不是 ?id=：这种链接能被直接分享、刷新后还能回到同一条记录，
  // 而且 id 是服务端真实主键 —— 前端不去猜、不去建本地编号。
  function hashParts() {
    var raw = String(window.location.hash || "").replace(/^#\/?/, "");
    var segments = raw.split("?")[0].split("/");
    var name = segments[0] || "";
    var id = "";
    for (var i = 1; i < segments.length; i += 1) {
      if (segments[i]) {
        id = segments[i];
        break;
      }
    }
    return {
      name: Object.prototype.hasOwnProperty.call(VIEWS, name) ? name : DEFAULT_VIEW,
      id: id ? decodeURIComponent(id) : "",
      known: Object.prototype.hasOwnProperty.call(VIEWS, name),
    };
  }

  function viewFromHash() {
    return hashParts().name;
  }

  function go(name) {
    var target = Object.prototype.hasOwnProperty.call(VIEWS, name) ? name : DEFAULT_VIEW;
    if (viewFromHash() === target) {
      applyView(target);
    } else {
      window.location.hash = "#/" + target;
    }
  }

  // 深链接要拉的那一条记录。id 一律交给后端校验（不是自己的资源后端回 404），
  // 前端拿到什么就如实显示什么，不做"猜一个差不多的记录给你看"这种事。
  function applyDeepLink(name, entityId) {
    if (name === "dataset") {
      useDataset(entityId, {}); // 详情/画像接口自己会校验归属，404 会显示成"找不到这份数据"
    } else if (name === "report") {
      // 深链接 = 直接读这一份报告：把目录卡片收起来，别让它占掉首屏
      var catalog = $("report-catalog-card");
      if (catalog) {
        catalog.hidden = true;
      }
      state.workflowId = entityId;
      resetWorkflowPanel();
      clearCharts();
      // charts: true —— 这条链接是"直接打开某条任务的报告"，图表必须一起取
      // （报告接口不返回图表，不这样的话刷新/分享出来的链接永远是一个空图表区）。
      loadReport({ charts: true });
    } else if (name === "history") {
      openHistoryDetail(entityId);
    } else if (name === "runs") {
      // 深链到某一条运行的执行过程：先问状态接口（真实进度），答不到再问归档接口，
      // 两边都没有就照后端 404 的口径说"找不到/无权访问" —— 不做任何回落跳转。
      openRunDetail(entityId);
    } else {
      var box = $("dataset-list-state");
      if (box) {
        box.appendChild(
          el("p", "note", "这个链接带的记录编号在「" + VIEWS[name].title + "」里没有对应内容，已按普通页面打开。")
        );
      }
    }
  }

  function applyRoute() {
    var route = hashParts();
    applyView(route.name, route.id);
  }

  function applyView(name, entityId) {
    state.view = name;

    var sections = document.querySelectorAll(".view[data-view]");
    for (var i = 0; i < sections.length; i += 1) {
      var section = sections[i];
      section.hidden = section.getAttribute("data-view") !== name;
    }

    var links = document.querySelectorAll(".nav__item[data-view]");
    for (var j = 0; j < links.length; j += 1) {
      var link = links[j];
      if (link.getAttribute("data-view") === name) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    }

    var meta = VIEWS[name];
    var crumb = $("crumb");
    var barTitle = $("cmdbar-title");
    var title = $("page-title");
    var sub = $("page-sub");
    var crumbText = (VIEW_GROUP[name] || "") + (VIEW_GROUP[name] ? " / " : "") + meta.crumb;
    if (crumb) {
      // 面包屑 = 分组 / 页面，分组与左侧导航里真实的分组标题逐字一致
      // （"指挥 / 战略态势"），不是另写一套词。
      crumb.textContent = crumbText;
    }
    if (barTitle) {
      barTitle.textContent = meta.crumb;
    }
    if (title) {
      title.textContent = meta.title;
    }
    if (sub) {
      sub.textContent = meta.sub;
    }

    closeDrawer();
    // 归档/找不到的说明只属于那一次深链。切到别的页面、或回到不带编号的 Agent 运行视图，
    // 都把它收起来 —— 否则它会一直挂在执行过程面板上，变成一句过期的话。
    if (!entityId) {
      hideRunNote();
    }
    window.scrollTo(0, 0);
    renderRunBadge();

    if (name === "history") {
      loadHistory();
    } else if (name === "settings") {
      loadSettings();
    } else if (name === "command") {
      loadPosture();
    } else if (name === "workbench") {
      loadRecentTasks();
      loadAssets();
      if (postureHostedHere(name)) {
        loadPosture();
      }
    } else if (name === "dataset") {
      loadDatasets();
    } else if (name === "analysis") {
      // 列表还没成功拿到过（或上次失败）就补一次；已经拿到就用现有的选项，
      // 避免每次切视图都打一遍接口。
      if (!datasetList.loaded || datasetList.error) {
        loadDatasets();
      }
      // 下拉独立刷新一次：它要覆盖任意一份数据集，不只是当前这一页
      loadDatasetPicker();
    } else if (name === "report" && !entityId) {
      // 报告目录只在"报告"这个页面自己（无 id）出现；深链接进来是直接读某一份报告，
      // 目录会挡在前面反而碍事（见 applyDeepLink 里的隐藏）。
      var catalogCard = $("report-catalog-card");
      if (catalogCard) {
        catalogCard.hidden = false;
      }
      loadReportCatalog();
    }

    if (entityId) {
      applyDeepLink(name, entityId);
    }
  }

  // 抽屉里能按 Tab 到达的元素：抽屉是浮层，焦点不该跑到后面被盖住的内容上
  function drawerFocusables(sidebar) {
    var nodes = sidebar.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
    var list = [];
    for (var i = 0; i < nodes.length; i += 1) {
      if (nodes[i].offsetParent !== null) {
        list.push(nodes[i]);
      }
    }
    return list;
  }

  function openDrawer() {
    var sidebar = $("sidebar");
    var toggle = $("nav-toggle");
    if (sidebar) {
      sidebar.classList.add("is-open");
      // 焦点收进抽屉：键盘用户不会顺着 Tab 走到被抽屉盖住的内容上
      var items = drawerFocusables(sidebar);
      if (items.length) {
        items[0].focus();
      }
    }
    if (toggle) {
      toggle.setAttribute("aria-expanded", "true");
    }
  }

  function closeDrawer() {
    var sidebar = $("sidebar");
    var toggle = $("nav-toggle");
    if (sidebar) {
      sidebar.classList.remove("is-open");
    }
    if (toggle) {
      toggle.setAttribute("aria-expanded", "false");
    }
  }

  function toggleDrawer() {
    var sidebar = $("sidebar");
    if (sidebar && sidebar.classList.contains("is-open")) {
      closeDrawer();
    } else {
      openDrawer();
    }
  }

  /* ======================================================== 大模型状态 */

  // 状态徽章来自 GET /api/health 的 llm_configured（服务端现取配置）。
  // 命令条那个服务健康点**共用同一次 fetch**：为它再打一次接口是浪费，
  // 而让它停在静态的"正在检查服务…"更是假的。
  function renderHealthDot(healthy, detail) {
    var box = $("cmdbar-health");
    var label = $("health-text");
    if (!box && !label) {
      return;
    }
    var dot = box ? box.querySelector(".dot") : null;
    if (dot) {
      dot.className = "dot " + (healthy ? "dot--ok" : "dot--err");
    }
    if (label) {
      label.textContent = healthy ? "服务正常" : "服务异常";
      label.title = detail || (healthy ? "健康检查通过（GET /api/health）" : "健康检查没有通过");
    } else if (box) {
      box.title = detail || "";
    }
  }

  async function loadHealth() {
    var badge = $("llm-badge");
    var result = await api(API.health);
    if (!result.ok) {
      var error = errorOf(result);
      renderHealthDot(false, "健康检查失败：" + error.message);
      if (badge) {
        badge.className = "badge badge--grey";
        badge.textContent = "大模型状态未知";
      }
      return null;
    }
    var data = result.data || {};
    renderHealthDot(data.status === "healthy", "服务端返回的状态原值：" + String(data.status));
    if (badge) {
      if (data.llm_configured === true) {
        badge.className = "badge badge--ok";
        badge.textContent = "已配置大模型";
        badge.title = "服务端已配置大模型，报告正文会由大模型撰写";
      } else {
        badge.className = "badge badge--grey";
        badge.textContent = "未配置大模型，将使用规则模式";
        badge.title = "服务端没有配置大模型：分析照常做，报告正文改用规则模板撰写";
      }
    }
    return data;
  }

  /* ==================================================== 战略态势带取数 */

  /* 取数设计的论证与取证行号在 docs/refactor/05-posture-data-flow.md，这里只记三条实现约束：
     1. 正文只读接口 10（GET /api/workflows/history/{id}，读 SQLite 归档、恒 200）。
        不读接口 7：它读进程内注册表，服务重启后连 has_report=true 的记录都会 404。
        不读接口 6：get_workflow 里的 _expire_if_stale 会**写状态并落库**，看一眼就污染数据。
     2. 首屏最坏 1 + 3 = 4 个 GET，TTL 命中 0 个；不参与 1000 毫秒轮询。
     3. 一条都不许编：拿不到就说拿不到，而"还没有分析"、"取不到"、"归档功能关着"
        是三件事，文案互不通用。 */
  var POSTURE_MAX_ROWS = 3;
  var POSTURE_HISTORY_LIMIT = 12; // 后端 limit 允许 1–100，这里只用一页
  var POSTURE_BODY_BUDGET = 3;
  var POSTURE_OK_TTL_MS = 5 * 60 * 1000;
  var POSTURE_FAIL_TTL_MS = 30 * 1000;
  var POSTURE_ELIGIBLE_STATUS = ["SUCCESS", "SUCCESS_WITH_WARNINGS"];

  // 三句"正文是谁写的"标记，逐字来自 app/agents/validation.py 的
  // TEMPLATE_NOTICE / LLM_PLANNED_NOTICE / LLM_WROTE_NOTICE（Wave C 改后端文案要同步这里）。
  // 为什么不看 report.llm_used：它只回答"计划是不是模型给的"，
  // 于是"模型规划 + 模板正文"和"模型正文"在它眼里是同一个值 —— 拿它标注就会说假话。
  var POSTURE_SOURCE_NOTICES = [
    {
      key: "model-wrote",
      label: "大模型撰写正文",
      notice: "本次报告正文由大模型根据工具结果撰写，其中所有数字均来自下方引用的工具调用，可逐条核对",
    },
    {
      key: "model-planned",
      label: "模型规划 · 模板汇总",
      notice: "本次由大模型参与规划（选择了分析工具与参数），但报告正文是模板自动汇总的，以下结论仍为工具结果的直接汇总",
    },
    {
      key: "rules",
      label: "规则模板汇总",
      notice: "本次未使用大模型，以下结论为工具结果的直接汇总",
    },
  ];

  var ARCHIVE_DISABLED_NOTE = "这台服务没有开启任务归档，所以没有历史记录可读。";
  var NO_RECORD_NOTE = "还没有任何分析记录。做完第一次分析后，这里会出现它。";
  // 同一件事在两个地方出现（历史详情、态势带），就必须是同一条字符串，不许各写一套口径。
  var ARCHIVE_REPORT_UNPARSEABLE_NOTE =
    "这条记录标记了「有报告」，但归档里的报告文本解析不出来，所以正文无法显示。";

  var posture = {
    loading: false,
    loaded: false,
    enabled: null,
    ticket: 0,
    at: 0,
    updatedAt: null,
    error: null,
    rows: [],
    itemsCount: 0,
    emptyKind: null,
  };
  var postureCache = new Map(); // workflow_id -> {at, state, report, error}
  var posturePending = new Map(); // workflow_id -> Promise（同一 id 的并发请求合并成一条）

  /* ---- 首屏指标袋：只装真实可导出的字段 ---- */
  var metrics = {
    datasetTotal: null,
    datasetTotalAt: null,
    datasetAsOf: null,       // GET /api/datasets 里最大 uploaded_at（D-7 命令条口径）
    running: null,
    withReport: null,
    failed: null,
    sample: null,
  };

  function setSlotText(id, text, title) {
    var node = $(id);
    if (!node) {
      return false;
    }
    node.textContent = text;
    if (title !== undefined) {
      node.title = title;
    }
    return true;
  }

  function sessionRunningCount() {
    // 运行中的只有**这个页面正在跟的那一条**：后端没有"全部运行中"的列表接口，
    // 而归档库只收终态记录（archive_if_terminal 对非终态直接返回 False），
    // 所以任何"全站运行中 N 条"的说法都没有字段支撑，不许出现。
    return state.pollTimer !== null && state.workflowId ? 1 : 0;
  }

  // 态势带的壳归哪个视图由 index.html 决定：有 command 视图就用它，
  // 没有就留在 workbench（同一时间只有一个宿主，否则两个视图各发一遍那四个 GET）。
  function postureHostedHere(name) {
    return name === "command" || (name === "workbench" && !document.querySelector('.view[data-view="command"]'));
  }

  function renderRunBadge() {
    var count = sessionRunningCount();
    var badge = $("badge-runs");
    if (badge) {
      badge.textContent = String(count);
      badge.hidden = count === 0;
      badge.title = "本次会话里正在执行、页面正在跟着轮询的分析条数（服务端不提供全站运行中列表）";
    }
    // 侧栏角标与首屏那一格是同一个事实的两种画法，只在这里算一次。
    metrics.running = count;
    renderMetrics();
  }

  function renderMetrics() {
    var sample = metrics.sample === null ? null : fmtInt(metrics.sample);
    var sampleScope = sample === null ? "样本口径未获取" : "样本＝最近 " + sample + " 条归档记录";

    setSlotText(
      "metric-datasets",
      metrics.datasetTotal === null ? "—" : fmtInt(metrics.datasetTotal),
      "数据集总数：GET /api/datasets 响应里的 total（全库总数，不是本页条数）"
    );
    setSlotText(
      "metric-datasets-note",
      metrics.datasetTotal === null
        ? "正在读取…"
        : "来自接口 /api/datasets 的总数" + (metrics.datasetTotalAt ? " · 截至 " + fmtClock(metrics.datasetTotalAt) : ""),
      undefined
    );

    setSlotText(
      "metric-running",
      metrics.running === null ? "—" : fmtInt(metrics.running),
      "本次会话正在执行的分析条数（0 或 1）。口径：页面在跟的那条工作流。"
    );
    setSlotText(
      "metric-running-note",
      "只算本页面正在轮询的那一条；归档库只保存在终态收尾的记录，服务端没有全站「运行中」列表接口"
    );

    setSlotText(
      "metric-reports",
      metrics.withReport === null ? "—" : fmtInt(metrics.withReport),
      "有报告的记录条数。" + sampleScope + "（字段 report 计数：has_report）"
    );
    setSlotText("metric-reports-note", metrics.withReport === null ? "正在读取…" : sampleScope);

    setSlotText(
      "metric-failed",
      metrics.failed === null ? "—" : fmtInt(metrics.failed),
      "状态为「失败」的记录条数。" + sampleScope + "（字段：status=FAILED）"
    );
    setSlotText("metric-failed-note", metrics.failed === null ? "正在读取…" : sampleScope);
  }

  // 命令条上的"数据资产更新于"：取 /api/datasets 返回里最大的 uploaded_at。
  // 这是真实字段、真实可核对；不写"数据截至"，因为没有任何字段表达"数据覆盖到哪一天"。
  function fmtDay(iso) {
    var ms = parseUtc(iso);
    if (ms === null) {
      return "—";
    }
    var date = new Date(ms);
    return (
      date.getFullYear() +
      "-" +
      String(date.getMonth() + 1).padStart(2, "0") +
      "-" +
      String(date.getDate()).padStart(2, "0")
    );
  }

  function latestUploadedAt(items) {
    var best = null;
    for (var i = 0; i < items.length; i += 1) {
      var raw = datasetUploadedAt(items[i]);
      var ms = parseUtc(raw);
      if (ms !== null && (best === null || ms > best.ms)) {
        best = { ms: ms, raw: raw };
      }
    }
    return best;
  }

  function renderDatasetBadges(items) {
    var badge = $("badge-datasets");
    if (badge) {
      badge.textContent = metrics.datasetTotal === null ? "—" : fmtInt(metrics.datasetTotal);
      badge.hidden = !(metrics.datasetTotal > 0);
      badge.title = "服务端 /api/datasets 返回的数据集总数";
    }

    var latest = latestUploadedAt(items || []);
    metrics.datasetAsOf = latest ? { ms: latest.ms, raw: latest.raw } : null;
    var slot = $("cmdbar-asof");
    if (!slot) {
      return;
    }
    var bold = slot.querySelector("b");
    if (bold) {
      clear(slot);
      slot.appendChild(document.createTextNode("数据资产更新于 "));
      slot.appendChild(bold);
      bold.textContent = latest ? fmtDay(latest.raw) : "—";
    } else {
      slot.textContent = latest ? "数据资产更新于 " + fmtDay(latest.raw) : "数据资产更新于 —";
    }
    slot.title = latest
      ? "本次返回的 " + fmtInt((items || []).length) + " 条里最近一份数据的上传时间；原始值：" + latest.raw
      : "没有读到任何数据集记录 —— 这一格是「没查到」，不是「没有数据」";
  }

  /* ---- 纯函数（不发请求，可单测） ---- */

  function postureEligible(item) {
    // FAILED 不进态势带：_final_status() 的语义是"这份报告能不能直接用"，
    // 一切"有话说"的情况都降为 SUCCESS_WITH_WARNINGS，通往 FAILED 的那条流程没有正常收尾。
    // 反过来也不成立（心跳超时被 _expire_if_stale 判成 FAILED 的记录也可能 has_report=true），
    // 所以这里按 status 筛，而不是按 has_report 筛。
    return (
      !!item &&
      item.has_report === true &&
      POSTURE_ELIGIBLE_STATUS.indexOf(item.status) >= 0 &&
      typeof item.workflow_id === "string"
    );
  }

  // 只筛不排。顺序 = 归档库自己的"完成时间新→旧"（库里 ORDER BY 写死），
  // 后端没有任何重要度/权重/置顶字段，前端一旦自己排一遍就会出现跨页顺序错乱，
  // 还会替用户宣称"这一条更重要"——界面上没有任何字段能为这句话背书。
  function postureCandidates(items) {
    var picked = [];
    for (var i = 0; i < items.length && picked.length < POSTURE_MAX_ROWS; i += 1) {
      if (postureEligible(items[i])) {
        picked.push(items[i]);
      }
    }
    return picked;
  }

  // 问题文本 = 用户原话，不截不断句不改写（截断可能把 "12,463" 切成 "12,46"，
  // 那是把真话变成一个不存在的数）。换行按普通文本处理，连续空行不该把行高撑开。
  function postureQuestion(item) {
    var raw = item && item.question !== null && item.question !== undefined ? String(item.question) : "";
    if (!raw.trim()) {
      return { text: "（这条记录没有留下问题文本）", missing: true };
    }
    return { text: raw, missing: false };
  }

  function postureSourceTag(report) {
    var summary = report && typeof report.summary === "string" ? report.summary : "";
    var first = summary.split("\n\n")[0];
    for (var i = 0; i < POSTURE_SOURCE_NOTICES.length; i += 1) {
      if (first && first === POSTURE_SOURCE_NOTICES[i].notice) {
        return { key: POSTURE_SOURCE_NOTICES[i].key, label: POSTURE_SOURCE_NOTICES[i].label, matched: true };
      }
    }
    return { key: "unknown", label: "来源说明未识别", matched: false };
  }

  function postureConclusion(report) {
    var sections = report && Array.isArray(report.sections) ? report.sections : [];
    var first = sections.length && sections[0] && typeof sections[0] === "object" ? sections[0] : null;
    var text = first && typeof first.text === "string" ? first.text : "";
    return {
      heading: first && typeof first.heading === "string" ? first.heading : "",
      text: text.trim(),
      cites: first && Array.isArray(first.cites) ? first.cites : [],
      missing: !text.trim(),
    };
  }

  // 证据数唯一口径：citations 去重后的长度 = 这次成功产出了结果的任务数。
  // 今天后端已经去过重（_latest_results 按 task_id），这层 Set 只是防漂移。
  // sections[].cites 只做正文角标，不参与计数 —— 两个数字同屏出现必然误导。
  function postureEvidence(report) {
    var raw = report ? report.citations : null;
    if (!Array.isArray(raw)) {
      return { count: null, taskIds: [], known: false };
    }
    var ids = [];
    for (var i = 0; i < raw.length; i += 1) {
      var id = String(raw[i]);
      if (ids.indexOf(id) < 0) {
        ids.push(id);
      }
    }
    return { count: ids.length, taskIds: ids, known: true };
  }

  function postureTime(item) {
    // finished_at 为空就退回 created_at 并**换标签**：把创建时间说成完成时间是编造。
    if (item && item.finished_at) {
      return { label: "分析完成于 ", value: item.finished_at };
    }
    if (item && item.created_at) {
      return { label: "创建于 ", value: item.created_at };
    }
    return { label: "", value: null };
  }

  /* ---- 取数 ---- */

  function fetchPostureHistory() {
    return api(API.history + "?limit=" + POSTURE_HISTORY_LIMIT + "&offset=0");
  }

  function postureEntryTtl(entry) {
    return entry.state === "not_found" || entry.state === "error" ? POSTURE_FAIL_TTL_MS : POSTURE_OK_TTL_MS;
  }

  // 单一职责：取一条归档记录的报告正文。缓存、负缓存、并发合并都在这一层。
  // state: ok（200 且有报告）/ empty（200 但 report 为 null，归档正文解析不出来）/
  //        not_found（记录没了或不属于当前账号）/ error（其余）。
  // 这层不写任何重试循环：重试只能由用户点按触发（场景 11 那一行的"重试这一行"）。
  async function fetchPostureBody(workflowId, force) {
    if (!workflowId) {
      return { state: "error", report: null, error: { code: "NO_ID", message: "这条记录没有编号", detail: null }, at: Date.now() };
    }
    if (!force) {
      var cached = postureCache.get(workflowId);
      if (cached && Date.now() - cached.at < postureEntryTtl(cached)) {
        return cached;
      }
    }
    var pending = posturePending.get(workflowId);
    if (pending) {
      return pending;
    }

    var task = api(API.historyDetail(workflowId)).then(function (result) {
      var entry;
      if (result.ok) {
        var report = (result.data || {}).report;
        var usable = !!report && typeof report === "object";
        entry = { state: usable ? "ok" : "empty", report: usable ? report : null, error: null };
      } else {
        var error = errorOf(result);
        entry = {
          state: error.code === "NOT_FOUND" ? "not_found" : "error",
          report: null,
          error: error,
        };
      }
      entry.at = Date.now();
      posturePending.delete(workflowId);
      // 会话已经失效（api() 里那一条 401 分支干的）就别再往缓存里写：
      // 这些正文属于上一个用户，下一个登录的人不该看到。
      if (!session.lost) {
        postureCache.set(workflowId, entry);
      }
      return entry;
    });

    posturePending.set(workflowId, task);
    return task;
  }

  function resetPosture() {
    posture.loaded = false;
    posture.enabled = null;
    posture.error = null;
    posture.rows = [];
    posture.itemsCount = 0;
    posture.emptyKind = null;
    posture.updatedAt = null;
    metrics.withReport = null;
    metrics.failed = null;
    metrics.sample = null;
    metrics.running = null;
    renderMetrics();
    renderPosture();
  }

  async function loadPosture(force) {
    if (!$("posture-body")) {
      return;
    }
    if (!force && posture.loaded && Date.now() - posture.at < POSTURE_OK_TTL_MS) {
      renderPosture();
      return;
    }

    posture.ticket += 1;
    var ticket = posture.ticket;
    posture.loading = true;
    posture.error = null;

    // 已经有行就不重放骨架：刷新只替换变了的文字，闪回灰条等于把静默刷新伪装成重新加载。
    if (!posture.rows.length) {
      renderPostureSkeleton();
    }
    metrics.running = sessionRunningCount();
    renderMetrics();

    var requestedAt = new Date().toISOString();
    var result = await fetchPostureHistory();
    if (ticket !== posture.ticket || session.lost) {
      return; // 更新的请求在路上（或人已经被踢出登录），这次的响应直接作废
    }

    posture.loading = false;

    if (!result.ok) {
      var error = errorOf(result);
      posture.error = error;
      posture.emptyKind =
        error.code === "NETWORK_ERROR" ? "network" : error.code === "SERVICE_UNAVAILABLE" ? "unavailable" : "failed";
      posture.rows = [];
      posture.loaded = false;
      renderPosture();
      return;
    }

    var data = result.data || {};
    var items = Array.isArray(data.items) ? data.items : [];
    posture.enabled = data.enabled === false ? false : true;
    posture.itemsCount = items.length;
    posture.loaded = true;
    posture.at = Date.now();
    posture.updatedAt = requestedAt;

    metrics.sample = items.length;
    metrics.running = sessionRunningCount();
    metrics.withReport = data.enabled === false ? null : countBy(items, isReportedItem);
    metrics.failed = data.enabled === false ? null : countBy(items, isFailedItem);
    renderMetrics();

    if (data.enabled === false) {
      posture.emptyKind = "disabled";
      posture.rows = [];
    } else if (!items.length) {
      posture.emptyKind = "no-record";
      posture.rows = [];
    } else {
      var candidates = postureCandidates(items);
      if (!candidates.length) {
        posture.emptyKind = "no-report";
        posture.rows = [];
      } else {
        posture.emptyKind = candidates.length < POSTURE_MAX_ROWS ? "partial" : null;
        posture.rows = candidates.map(function (item) {
          var cached = postureCache.get(item.workflow_id);
          return {
            item: item,
            body: cached && Date.now() - cached.at < postureEntryTtl(cached) ? cached : null,
            node: null,
          };
        });
      }
    }
    renderPostureAsof();
    renderPosture();

    var budget = Math.min(POSTURE_BODY_BUDGET, posture.rows.length);
    for (var i = 0; i < budget; i += 1) {
      // 逐行填充：谁先回来谁先上屏，行位置按候选顺序固定，乱序不会让界面跳动。
      // 第 4 个及以后的候选一个请求都不发（POSTURE_BODY_BUDGET 是硬上限）。
      fillPostureRow(posture.rows[i], ticket, force);
    }
  }

  function countBy(items, predicate) {
    var n = 0;
    for (var i = 0; i < items.length; i += 1) {
      if (predicate(items[i])) {
        n += 1;
      }
    }
    return n;
  }

  function isReportedItem(item) {
    return !!item && item.has_report === true;
  }

  function isFailedItem(item) {
    return !!item && item.status === "FAILED";
  }

  async function fillPostureRow(row, ticket, force) {
    var entry = await fetchPostureBody(row.item.workflow_id, force);
    if (ticket !== posture.ticket || session.lost) {
      return;
    }
    row.body = entry;
    paintPostureRow(row);
  }

  /* ---- 渲染 ---- */

  function renderPostureAsof() {
    // 这一格说清楚是"列表的刷新时刻"，不是数据的截止时间：归档链路里没有 dataset_id，
    // 无法证明这三条结论各自用的是哪一份数据，所以不写"数据截至"。
    var text = posture.updatedAt ? "· 列表更新于 " + fmtClock(posture.updatedAt) : "· 列表更新于 —";
    var title = "这是结论列表的刷新时间，不是数据本身的截止时间。";
    if (!setSlotText("posture-asof-note", text, title)) {
      setSlotText("posture-asof", text, title);
    }
  }

  function renderPosture() {
    var host = $("posture-body");
    if (!host) {
      return;
    }
    host.setAttribute("aria-busy", posture.loading ? "true" : "false");

    if (posture.emptyKind && posture.emptyKind !== "partial") {
      clear(host);
      host.appendChild(renderPostureEmpty(posture.emptyKind));
      return;
    }
    if (!posture.rows.length) {
      if (!posture.loaded) {
        renderPostureSkeleton();
        return;
      }
      clear(host);
      host.appendChild(renderPostureEmpty("no-report"));
      return;
    }

    var ids = posture.rows.map(function (row) {
      return row.item.workflow_id;
    });
    var existing = host.querySelectorAll("[data-workflow-id]");
    var reusable = existing.length === ids.length;
    for (var k = 0; k < ids.length && reusable; k += 1) {
      if (existing[k].getAttribute("data-workflow-id") !== ids[k]) {
        reusable = false;
      }
    }
    if (!reusable) {
      clear(host);
    }
    posture.rows.forEach(function (row) {
      var article = reusable && row.node ? row.node : null;
      if (article && !host.contains(article)) {
        article = null;
      }
      if (!article) {
        article = el("article", "posture__row");
        article.setAttribute("data-workflow-id", row.item.workflow_id || "");
        host.appendChild(article);
        row.node = article;
      }
      paintPostureRow(row);
    });

    if (posture.emptyKind === "partial") {
      // 不足三条就照实说只有三条不到，不补占位假行。
      var tail = el("p", "posture__tail", "可用的真实结论只有 " + fmtInt(posture.rows.length) + " 条，所以这里只有 " + fmtInt(posture.rows.length) + " 行。");
      tail.setAttribute("role", "status");
      var existingTail = host.querySelector(".posture__tail");
      if (existingTail) {
        host.replaceChild(tail, existingTail);
      } else {
        host.appendChild(tail);
      }
    }
  }

  function renderPostureSkeleton() {
    var host = $("posture-body");
    if (!host) {
      return;
    }
    clear(host);
    host.setAttribute("aria-busy", "true");
    for (var i = 0; i < POSTURE_MAX_ROWS; i += 1) {
      var row = el("div", "posture__row posture__row--skeleton");
      row.setAttribute("aria-hidden", "true");
      row.appendChild(el("div", "skeleton"));
      row.appendChild(el("div", "skeleton"));
      row.appendChild(el("div", "skeleton"));
      host.appendChild(row);
    }
  }

  function postureAction(label, onClick, title) {
    var button = el("button", "btn btn--ghost btn--sm", label);
    button.type = "button";
    if (title) {
      button.title = title;
    }
    button.addEventListener("click", onClick);
    return button;
  }

  function openHistoryFromPosture(workflowId) {
    // 走历史详情（接口 10），不走 #/report/{id}：后者会打接口 7 的内存注册表，
    // 服务重启后这条记录明明在归档里，页面却说读不到。
    go("history");
    openHistoryDetail(workflowId);
  }

  function renderPostureEmpty(kind) {
    var block = el("div", "posture__empty");
    block.setAttribute("role", "status");
    var title = "";
    var note = "";
    var actions = [];

    if (kind === "network") {
      title = "此刻连不上服务";
      note = "请确认后端还在运行；下面这一句不是「没有结论」，是「没读到」。";
      actions.push(postureAction("重试", function () {
        loadPosture(true);
      }));
    } else if (kind === "unavailable") {
      title = "服务暂时不可用";
      note = ((posture.error && posture.error.message) || "服务暂时不可用") + "：稍后会自动恢复，可以先点重试。";
      actions.push(postureAction("重试", function () {
        loadPosture(true);
      }));
    } else if (kind === "failed") {
      var err = posture.error || {};
      title = "态势带读取失败：" + (err.message || "服务没有给出原因");
      note = "这一句不是「没有结论」，是「没读到」；其余页面不受影响，可以先去看历史记录。";
      actions.push(postureAction("重试", function () {
        loadPosture(true);
      }));
      actions.push(postureAction("查看历史记录", function () {
        go("history");
      }));
      var code = errorCodeNode(err.code, "origin");
      block.appendChild(code);
    } else if (kind === "disabled") {
      title = ARCHIVE_DISABLED_NOTE;
      note = "这台服务没有开启任务归档（部署开关关掉的），不是你没有分析过。做完的分析仍可在本次会话里查看。";
      actions.push(postureAction("去发起一次分析", function () {
        go("analysis");
      }));
    } else if (kind === "no-record") {
      title = NO_RECORD_NOTE;
      note = "第一个结论会在你完成第一次分析后出现在这里。";
      actions.push(postureAction("去发起一次分析", function () {
        go("analysis");
      }));
    } else {
      title = "最近的 " + fmtInt(POSTURE_HISTORY_LIMIT) + " 次分析还没有产出报告";
      note = "它们要么失败了，要么没跑到出报告的那一步 —— 这跟「还没做过分析」是两件事。逐条原因在历史记录里。";
      actions.push(postureAction("查看历史记录", function () {
        go("history");
      }));
    }

    block.insertBefore(el("h4", "posture__empty-title", title), block.firstChild);
    block.appendChild(el("p", "posture__empty-note", note));
    var row = el("div", "row row--tight");
    actions.forEach(function (action) {
      row.appendChild(action);
    });
    block.appendChild(row);
    return block;
  }

  // 报告文档里的角标：这是**可点、可键盘到达**的跳转入口（同一份文档底部有证据区）。
  // renderReportInto 会给它们补 tabindex/role/aria-label，init() 的事件委托只认标了
  // tabindex="0" 的那些。
  function docCiteSupers(cites) {
    var wrap = el("span", "doc__citeset");
    cites.forEach(function (taskId) {
      var sup = document.createElement("sup");
      sup.className = "cite";
      sup.setAttribute("data-task-id", String(taskId));
      sup.textContent = String(taskId);
      sup.title = "这一句结论引用的取数任务编号：点它跳到本页下方的证据行（Enter/空格同样能跳）";
      wrap.appendChild(sup);
    });
    return wrap;
  }

  // 态势带行内的引用标记：这是**静态标记，不是链接**。
  // Wave E 缺陷 2 的根因就在这里——它以前复用 .cite 类却没有证据区可跳，jumpToEvidence
  // 沿 parentElement 找不到 .src-line 就静默 no-op，看起来"能点"其实点了没反应。
  // 现在换成 .cite-mark：没有 tabindex/role、CSS 里 cursor 是 default，
  // title 明说这一行没有逐条明细、明细在那条运行的报告里。
  function postureCiteMarks(cites) {
    var wrap = el("span", "posture__cites");
    cites.forEach(function (taskId) {
      var sup = document.createElement("sup");
      sup.className = "cite-mark";
      sup.setAttribute("data-task-id", String(taskId));
      sup.textContent = String(taskId);
      sup.title = "这句结论引用的取数任务编号（静态标记，这里点不动）：逐条明细在那条运行的报告里";
      wrap.appendChild(sup);
    });
    return wrap;
  }

  // 只改这一行的内容：单行取回失败不许清空已经成功的另外两行。
  function paintPostureRow(row) {
    var host = $("posture-body");
    if (!host) {
      return;
    }
    if (!row.node || !host.contains(row.node)) {
      renderPosture();
      return;
    }
    var article = row.node;
    clear(article);

    var item = row.item;
    var body = row.body;
    var report = body && body.state === "ok" ? body.report : null;

    var question = el("div", "posture__q");
    question.appendChild(el("span", "eyebrow", "业务问题"));
    var qtext = postureQuestion(item);
    var q = el("p", "posture__question" + (qtext.missing ? " muted" : ""), qtext.text);
    if (!qtext.missing) {
      q.title = qtext.text;
    }
    question.appendChild(q);

    var meta = el("div", "posture__meta");
    var pill = zhEnumNode(WORKFLOW_STATUS_TEXT, item.status, "状态", "pill");
    meta.appendChild(pill);
    var time = postureTime(item);
    if (time.value) {
      meta.appendChild(timeNode(time.label, time.value));
    }
    meta.appendChild(el("span", "posture__mono", "任务编号 " + (item.workflow_id || "")));
    question.appendChild(meta);
    article.appendChild(question);

    // 第 2 列：报告首段结论（不是 summary —— summary 是"这次跑成了什么"的元说明）
    var answer = el("div", "posture__a");
    answer.appendChild(el("span", "eyebrow", "报告首段结论"));
    var conclusion = postureConclusion(report);
    var source = postureSourceTag(report);
    var stamp = el("span", "src src--" + (source.key === "model-wrote" ? "llm" : source.key === "model-planned" ? "ai" : "rules"), source.label);
    stamp.title = source.matched
      ? "这份报告正文的由来，按报告总述首段的逐字标记判定"
      : "报告总述的首段不是我们认识的三种标记之一（多半是文案改版），所以这里不下结论";
    answer.appendChild(stamp);

    if (!body) {
      answer.appendChild(el("p", "posture__pending", "正在读取这一段结论…"));
    } else if (body.state === "not_found") {
      answer.appendChild(el("p", "posture__missing", "这条记录的报告读不到了"));
      answer.appendChild(
        el("p", "posture__note", "归档里有这条记录，但报告正文取不回来（多半是服务重启前的记录，或它不属于当前账号）。")
      );
    } else if (body.state === "empty") {
      answer.appendChild(el("p", "posture__missing", "报告已归档但正文无法解析"));
      answer.appendChild(el("p", "posture__note", ARCHIVE_REPORT_UNPARSEABLE_NOTE));
    } else if (body.state === "error") {
      answer.appendChild(
        el("p", "posture__missing", "这条结论暂时取不到：" + ((body.error && body.error.message) || "请求失败"))
      );
      var retryRow = el("div", "row row--tight");
      retryRow.appendChild(
        postureAction("重试这一行", function () {
          postureCache.delete(item.workflow_id);
          row.body = null;
          paintPostureRow(row);
          fillPostureRow(row, posture.ticket, true);
        })
      );
      answer.appendChild(retryRow);
    } else if (conclusion.missing) {
      answer.appendChild(el("p", "posture__missing", "这条记录没有可显示的结论正文"));
      answer.appendChild(el("p", "posture__note", "不会拿报告总述顶替那一段 —— 总述说的是「这次跑成了什么」，不是业务结论。"));
    } else {
      if (conclusion.heading) {
        answer.appendChild(el("p", "posture__heading", conclusion.heading));
      }
      var verdict = el("p", "posture__verdict", conclusion.text);
      verdict.title = conclusion.text;
      answer.appendChild(verdict);
      if (conclusion.cites.length) {
        var citeRow = el("p", "posture__citerow");
        citeRow.appendChild(document.createTextNode("依据："));
        citeRow.appendChild(postureCiteMarks(conclusion.cites));
        answer.appendChild(citeRow);
      }
    }
    article.appendChild(answer);

    // 第 3 列：取数调用次数（唯一口径 citations，不出现第二个"证据数"）
    var evidence = el("div", "posture__e");
    evidence.appendChild(el("span", "eyebrow", "取数情况"));
    var ev = postureEvidence(report);
    if (!body) {
      evidence.appendChild(el("p", "posture__pending", "取数中…"));
    } else if (!ev.known) {
      evidence.appendChild(el("p", "posture__value", "—"));
      evidence.appendChild(el("p", "posture__note", "未提供引用信息"));
    } else {
      var count = el("p", "posture__value", "取数调用 " + fmtInt(ev.count) + " 次");
      count.title = "本报告汇总了 " + fmtInt(ev.count) + " 次成功的取数任务；点开报告可见逐条依据";
      evidence.appendChild(count);
      if (ev.count === 0) {
        evidence.appendChild(el("p", "posture__note", "这条记录没有汇总到任何成功的取数"));
      }
    }
    article.appendChild(evidence);

    // 第 4 列：动作（只有"查看报告/打开归档详情"，不放导出、分享、删除）
    var go = el("div", "posture__go");
    go.appendChild(
      postureAction("查看报告", function () {
        openHistoryFromPosture(item.workflow_id);
      }, "在历史记录里打开这条归档记录，正文、表格与质量提示都在那里")
    );
    article.appendChild(go);
  }

  /* ============================================================ 上传 */

  function pickFile() {
    var input = $("file");
    if (input && input.files && input.files.length) {
      return input.files[0];
    }
    return state.droppedFile;
  }

  async function handleUpload() {
    if (state.uploading) {
      return;
    }
    var status = $("upload-state");
    var button = $("upload");
    var file = pickFile();

    if (!file) {
      if (status) {
        status.className = "state error-text";
        status.textContent = "请先选择一个 .csv 或 .xlsx 文件";
      }
      return;
    }

    state.uploading = true;
    setBusy(button, true);
    if (status) {
      status.className = "state";
      status.textContent = "正在上传 " + file.name + " …";
    }

    var form = new FormData();
    form.append("file", file, file.name);

    var result = await api(API.upload, { method: "POST", body: form });

    state.uploading = false;
    setBusy(button, false);

    if (!result.ok) {
      // 上传失败的文案**就地**显示（上传区的问题不该弹到全局去）。
      // 最常见的两个码是 FILE_TOO_LARGE（413）和 UNSUPPORTED_FILE（415），
      // 但这里刻意不写「错误码 → 中文」对照表：后端的 message 已经把
      // 「文件超过 1 MB」还是「只支持 .csv/.xlsx」说清楚了，前端再翻译一遍
      // 就会出现两份文案各自漂移。其它码（含网络层 NETWORK_ERROR）也走这一条。
      var error = errorOf(result);
      if (status) {
        status.className = "state error-text";
        status.textContent = error.message;
      }
      return;
    }

    var dataset = result.data || {};
    state.datasetId = dataset.dataset_id;
    state.dataset = dataset;
    state.profile = null;
    state.workflowId = null;
    state.droppedFile = null;

    if (status) {
      status.className = "state ok-text";
      status.textContent =
        "已上传：" + (dataset.original_filename || file.name) +
        " · " + fmtInt(dataset.row_count) + " 行 × " + fmtInt(dataset.column_count) + " 列" +
        " · 数据编号 " + dataset.dataset_id;
    }

    resetWorkflowPanel();
    clearCharts();
    clearReport();

    await Promise.all([loadDataset(), loadProfile(), loadHealth()]);
    updateAnalysisContext();
    // 上传成功后列表与工作台「数据资产」都重新问一次后端。
    // 不在前端往数组里塞一条"刚上传的"：那会让列表与后端真实状态分叉。
    await refreshDatasetViews();
  }

  /* ============================== 数据集列表（真实接口 GET /api/datasets） */

  // 后端契约（task-11）：GET /api/datasets?limit&offset&sort&order
  //   响应外壳不变：{ok, data:{items:[...], total:N}, error}
  //   sort ∈ uploaded_at | filename | rows；order ∈ asc | desc
  //
  // 两条硬规矩：
  //   1) 排序和分页**全部交给后端参数** —— 前端自己排一遍、切一遍，
  //      翻到第二页就会露馅（那是"看起来对"）；
  //   2) 显示的数字必须来自后端字段：缺失单元格是 null 就写"未计算"，
  //      绝不能写 0 —— 0 是一个真实值，用它占位等于编数据。
  //
  // 字段名做两种兼容（created_at / uploaded_at、row_count / rows、
  // column_count / columns）：这一页要展示的是"后端真实给了我什么"，
  // 名字没定稿前硬绑一种，会让整张表整齐地显示"未提供"，看起来像坏了。
  var PAGE_SIZE = 20;
  // 分析工作区的下拉要能选到任意一份数据集，而列表一次只加载 20 条，
  // 所以下拉自己取一次（接口上限 200）。取不满时在状态行里如实说明还有多少没列出，
  // 不能让人以为"下拉里就是全部数据"。
  var PICKER_LIMIT = 200;
  var datasetPickerTotal = null;
  // 搜索框：输入停下来再发请求（每一次按键都打后端，是在拿别人的服务器当输入法）。
  // 350ms 是"打字停顿"的经验值；按回车则立刻搜，不等人。
  var DATASET_SEARCH_DEBOUNCE_MS = 350;

  var datasetList = {
    items: [],
    total: 0,
    offset: 0, // 正在请求（或下一次要请求）的起点
    shownOffset: 0, // 服务端已经交回来的那一页的起点：页码只认它，见 renderDatasetPager
    q: "", // 正在请求（或下一次要请求）的文件名子串
    shownQ: "", // 已经在屏幕上的那批数据对应的搜索词：空结果的两种文案靠它区分
    sort: "uploaded_at",
    order: "desc",
    loading: false,
    loaded: false, // 成功拿到过一次响应：用来区分"还没加载"和"加载完但一条都没有"
    error: null,
    ticket: 0, // 请求序号。翻页点得快时旧响应可能后到，只认最后一次，别让旧数据盖新页
    sortHonored: null, // 服务端有没有真的按 sort/order 返回：true/false/判断不了=null
  };

  /* ---- 数据集列表短 TTL 缓存（D-6 精神：与态势带的 workflow_id 缓存同一路子）----
     首屏与上传链路里，多个 loader 会在同一渲染批次各拉一次**同参数**的 GET：
       · init 末尾 loadAssets() + applyView("workbench") 里的 loadAssets()（同 ?limit=3）；
       · applyView("analysis") 直接 loadDatasetPicker() + loadDatasets() 尾部的 loadDatasetPicker()（同 ?limit=200）；
       · 上传成功后 refreshDatasetViews → loadDatasets 尾部 picker 与刷新链路再打一次 ?limit=200。
     这些不是功能，是重复请求。合并方式两层：
       · datasetsPending：同 query 的**并发**请求合并成一条（返回同一个 promise，天然去重）；
       · datasetsCache：≤ DATASETS_TTL_MS 的同参数**成功**响应直接复用。
     不删任何视图的数据获取：每个视图该拿的列表照样拿，只是同参数不各打一遍。
     写操作（上传成功）与用户主动刷新会作废缓存（invalidateDatasetsCache），
     绝不让"刚上传的那份"因为 TTL 而不显示 —— 只有**读**才吃缓存。 */
  var DATASETS_TTL_MS = 1200;
  var datasetsCache = new Map(); // queryKey -> {at, result}
  var datasetsPending = new Map(); // queryKey -> Promise<result>

  function invalidateDatasetsCache() {
    datasetsCache.clear();
  }

  function pickField(item, names) {
    for (var i = 0; i < names.length; i += 1) {
      var value = item ? item[names[i]] : null;
      if (value !== undefined && value !== null) {
        return value;
      }
    }
    return null;
  }

  function datasetName(item) {
    return String(pickField(item, ["original_filename", "stored_filename"]) || "（无文件名）");
  }

  function datasetRows(item) {
    var value = pickField(item, ["row_count", "rows"]);
    return typeof value === "number" && isFinite(value) ? value : null;
  }

  function datasetCols(item) {
    var value = pickField(item, ["column_count", "columns"]);
    if (typeof value === "number" && isFinite(value)) {
      return value;
    }
    if (Array.isArray(value)) {
      return value.length; // 现有 Dataset.columns 是列名数组，长度就是列数
    }
    return null;
  }

  function datasetUploadedAt(item) {
    return pickField(item, ["created_at", "uploaded_at"]);
  }

  function datasetTypeText(item) {
    var ext = pickField(item, ["file_ext", "extension"]);
    if (ext) {
      return "." + String(ext).replace(/^\./, "");
    }
    var name = datasetName(item);
    var dot = name.lastIndexOf(".");
    return dot > 0 ? name.slice(dot) : "—";
  }

  function datasetMissingText(item) {
    if (!item || item.missing_cells === null || item.missing_cells === undefined) {
      return "未计算";
    }
    var value = item.missing_cells;
    return typeof value === "number" && isFinite(value) ? fmtInt(value) : String(value);
  }

  function datasetCellsText(item) {
    var rows = datasetRows(item);
    var cols = datasetCols(item);
    if (rows === null && cols === null) {
      return "行列数未提供";
    }
    return (rows === null ? "?" : fmtInt(rows)) + " 行 × " + (cols === null ? "?" : fmtInt(cols)) + " 列";
  }

  // 服务端到底有没有按我们要的那一列排序？只能看它真实返回的顺序。
  // 三种返回值：true 确实按了、false 明显没按、null 判断不了（条数不够或字段缺失）。
  // 为什么要这个函数：后端的 sort/order 参数还在做（task-11），如果它还没实现，
  // 前端却把表头标成"当前降序"，那就是在骗人 —— 点了没变却说变了。
  function datasetSortValue(item, field) {
    if (field === "filename") {
      return datasetName(item);
    }
    if (field === "rows") {
      return datasetRows(item);
    }
    if (field === "size_bytes") {
      return pickField(item, ["size_bytes"]);
    }
    if (field === "uploaded_at") {
      return datasetUploadedAt(item);
    }
    return null;
  }

  function sortHonored(items, field, order) {
    if (!Array.isArray(items) || items.length < 2) {
      return null; // 一页只有一条：顺序对错都看不出来
    }
    var values = [];
    for (var i = 0; i < items.length; i += 1) {
      values.push(datasetSortValue(items[i], field));
    }
    var comparable = 0;
    var violations = 0;
    for (var j = 1; j < values.length; j += 1) {
      var prev = values[j - 1];
      var cur = values[j];
      if (prev === null || prev === undefined || cur === null || cur === undefined) {
        continue;
      }
      var cmp;
      if (field === "uploaded_at") {
        var left = Date.parse(prev);
        var right = Date.parse(cur);
        if (isNaN(left) || isNaN(right)) {
          continue;
        }
        cmp = left - right;
      } else if (typeof prev === "number" && typeof cur === "number") {
        cmp = prev - cur;
      } else {
        var a = String(prev);
        var b = String(cur);
        cmp = a < b ? -1 : a > b ? 1 : 0;
      }
      comparable += 1;
      if (order === "asc" ? cmp > 0 : cmp < 0) {
        violations += 1;
      }
    }
    if (!comparable) {
      return null;
    }
    return violations === 0;
  }

  function datasetsQuery(limit, offset, sort, order, q) {
    var query = "?limit=" + limit + "&offset=" + offset +
      "&sort=" + encodeURIComponent(sort) + "&order=" + encodeURIComponent(order);
    // 只有真的在搜索时才带 q：空搜索不能变成 q= 这种假参数，
    // 也让"没搜索"的请求 URL 与加搜索框之前逐字节一致（可对照、可复现）。
    if (q) {
      query += "&q=" + encodeURIComponent(q);
    }
    return query;
  }

  async function fetchDatasets(limit, offset, sort, order, q, force) {
    var url = API.datasets + datasetsQuery(limit, offset, sort, order, q);
    // force（例如上传成功/用户点刷新）先清掉这份缓存再打，保证拿到写后的真实状态；
    // 其余调用走"并发合并 + 短 TTL 复用"，同参数不再各打一遍。
    if (force) {
      datasetsCache.delete(url);
    } else {
      var cached = datasetsCache.get(url);
      if (cached && Date.now() - cached.at < DATASETS_TTL_MS) {
        return cached.result;
      }
      var pending = datasetsPending.get(url);
      if (pending) {
        return pending;
      }
    }

    var task = api(url).then(function (result) {
      datasetsPending.delete(url);
      // 只缓存成功响应：失败要能真的重试，不把错误冻进 TTL。
      if (result && result.ok) {
        datasetsCache.set(url, { at: Date.now(), result: result });
      }
      return result;
    });
    datasetsPending.set(url, task);
    return task;
  }

  // 这个兜底分支是后端还没有列表接口时留下的（那时 api() 对 404 给出 BAD_RESPONSE，响应不是统一外壳）。
  // 现在后端已有列表接口，分支保留为兜底；真走到这里，文案只如实说"列表暂时拿不到、其他功能不受影响"，不伪装成网络故障。
  function datasetsGapHint(error) {
    if (error && error.code === "BAD_RESPONSE" && String(error.message).indexOf("404") >= 0) {
      return "服务端暂时没有返回数据集列表。上传、画像、分析都不受影响。";
    }
    return "";
  }

  function datasetActionButton(label, title, onClick) {
    var button = el("button", "btn btn--ghost btn--sm", label);
    button.type = "button";
    button.title = title;
    button.addEventListener("click", onClick);
    return button;
  }

  function renderDatasetRows() {
    var table = $("dataset-table");
    var body = table ? table.querySelector("tbody") : null;
    if (!body) {
      return;
    }
    clear(body);

    datasetList.items.forEach(function (item) {
      var row = document.createElement("tr");

      var nameCell = document.createElement("td");
      var name = datasetName(item);
      var label = el("span", "cellname", name);
      label.title = name; // 完整文件名放 title：窄屏截断了也能看到全名
      nameCell.appendChild(label);
      row.appendChild(nameCell);

      row.appendChild(el("td", null, datasetTypeText(item)));
      row.appendChild(el("td", "is-num", fmtBytes(pickField(item, ["size_bytes"]))));
      row.appendChild(el("td", null, fmtDateTime(datasetUploadedAt(item))));

      var rows = datasetRows(item);
      var cols = datasetCols(item);
      row.appendChild(el("td", "is-num", rows === null ? "未提供" : fmtInt(rows)));
      row.appendChild(el("td", "is-num", cols === null ? "未提供" : fmtInt(cols)));
      row.appendChild(el("td", "is-num", datasetMissingText(item)));

      var actions = document.createElement("td");
      var actionsRow = el("div", "row row--tight");
      actionsRow.appendChild(
        datasetActionButton("查看画像", "把这一份设为当前数据集，并看字段结构、预览与质量", function () {
          useDataset(item.dataset_id, { goDatasetView: true });
        })
      );
      actionsRow.appendChild(
        datasetActionButton("分析它", "切到发起分析工作区，用这一份数据提问", function () {
          useDataset(item.dataset_id, { silent: true }).then(function () {
            go("analysis");
          });
        })
      );
      actions.appendChild(actionsRow);
      row.appendChild(actions);

      body.appendChild(row);
    });
  }

  function renderDatasetListState() {
    var box = $("dataset-list-state");
    var summary = $("dataset-list-summary");
    if (!box) {
      return;
    }
    clear(box);

    if (datasetList.error) {
      var error = datasetList.error;
      box.appendChild(
        emptyBlock("读取数据集列表失败：" + error.message + "（" + error.code + "）", "warn")
      );
      var hint = datasetsGapHint(error);
      if (hint) {
        box.appendChild(el("p", "note", hint));
      }
      box.appendChild(
        datasetActionButton("重试", "重新向服务端请求数据集列表", function () {
          loadDatasets();
        })
      );
    } else if (datasetList.loading || !datasetList.loaded) {
      // 搜索中的文案必须与"首次加载"不同：用户刚打完字，要知道系统正在按他给的词找，
      // 而不是以为列表在重新读一遍。表格里保留上一次的真实行，不做假骨架。
      box.appendChild(
        emptyBlock(
          datasetList.q
            ? "正在搜索文件名包含「" + datasetList.q + "」的数据集…"
            : "正在读取数据集列表…",
          "dataset"
        )
      );
    } else if (!datasetList.items.length) {
      if (datasetList.shownQ) {
        // "搜不到"与"一条都没有"是两件事：前者要能改词或清空，后者要引导上传。
        box.appendChild(
          emptyBlock(
            "没有文件名包含「" + datasetList.shownQ + "」的数据集。换个词，或者清空搜索框看全部。",
            "search"
          )
        );
        var clearSearch = el("button", "btn btn--ghost btn--sm", "清空搜索");
        clearSearch.type = "button";
        clearSearch.addEventListener("click", function () {
          clearDatasetSearch();
        });
        box.appendChild(clearSearch);
      } else {
        box.appendChild(
          emptyBlock(
            "这里没有数据集。数据集登记处活在服务进程里，服务重启后会清空；" +
              "你跑过的分析存在归档里，不受影响，可以从「历史记录」直接看报告。" +
              "上传一份 CSV / XLSX，这里就会出现它。",
            "dataset"
          )
        );
        var upload = el("a", "btn btn--ghost btn--sm", "去上传一份数据");
        upload.setAttribute("href", "#/dataset");
        upload.title = "上传卡在「数据资产」页（结构契约 02 §3.5：上传是数据资产的动作，不在态势带上）";
        box.appendChild(upload);
      }
    }

    if (summary) {
      if (datasetList.error) {
        summary.textContent = "读取失败";
      } else if (!datasetList.loaded) {
        summary.textContent = "正在读取…";
      } else if (datasetList.shownQ) {
        // 说清"这个数字是在哪个搜索词下数出来的"，否则用户会把过滤后的总数当成全部
        summary.textContent =
          "文件名含「" + datasetList.shownQ + "」的有 " + fmtInt(datasetList.total) + " 个" +
          (datasetList.total > datasetList.items.length
            ? "，本页 " + fmtInt(datasetList.items.length) + " 个"
            : "") +
          "（全部数据集另计）";
      } else {
        summary.textContent =
          "共 " + fmtInt(datasetList.total) + " 个数据集" +
          (datasetList.total > datasetList.items.length
            ? "，本页 " + fmtInt(datasetList.items.length) + " 个"
            : "");
      }
    }

    // 服务端没按我们要的列排序时，如实说出来。前端不会自己重排一遍
    // （那样分页就会错位：第 2 页被前端排过之后，跨页顺序不再连续）。
    if (datasetList.sortHonored === false) {
      box.appendChild(
        el(
          "p",
          "note",
          "服务端返回的顺序与请求的 sort=" + datasetList.sort + "&order=" +
            datasetList.order + " 不一致：这个排序参数后端可能还没实现，" +
            "所以表头不标注排序状态，也不会在前端自己重排。"
        )
      );
    }
  }

  function renderDatasetPager() {
    var pager = $("dataset-pager");
    var info = $("page-info");
    var prev = $("page-prev");
    var next = $("page-next");
    if (!pager || !info || !prev || !next) {
      return;
    }

    // 页码只反映"服务端已经交回来的那一页"（shownOffset），不反映"正在请求的那一页"。
    // 请求还在路上就把页码改成目标页，等于数据没到之前先替服务端表态；正在载入这件事
    // 由表格里的旧数据 + 这里的"· 正在载入…"后缀 + 两个按钮禁用来表达。
    var start = datasetList.total === 0 ? 0 : datasetList.shownOffset + 1;
    var end = datasetList.shownOffset + datasetList.items.length;
    info.textContent = datasetList.total === 0
      ? "没有可翻的页"
      : "第 " + fmtInt(start) + "–" + fmtInt(end) + " 条，共 " + fmtInt(datasetList.total) + " 条" +
        (datasetList.loading ? " · 正在载入…" : "");

    pager.hidden = datasetList.total <= PAGE_SIZE;
    prev.disabled = datasetList.shownOffset <= 0 || datasetList.loading;
    next.disabled = end >= datasetList.total || datasetList.loading;
  }

  function renderDatasetSortHeaders() {
    var heads = document.querySelectorAll("#dataset-table th[data-sort]");
    for (var i = 0; i < heads.length; i += 1) {
      var head = heads[i];
      var active = head.getAttribute("data-sort") === datasetList.sort;
      // 只有"服务端确实按这一列返回"时才标注排序状态；它没照做就不标。
      // 请求还在路上时也不标：这时候标出来等于替后端提前承诺了一个它可能没做的排序。
      var claimed = active && !datasetList.loading && datasetList.sortHonored !== false;
      if (claimed) {
        head.setAttribute(
          "aria-sort",
          datasetList.order === "asc" ? "ascending" : "descending"
        );
      } else {
        head.removeAttribute("aria-sort");
      }
      var button = head.querySelector("button");
      if (button) {
        button.setAttribute(
          "aria-label",
          head.textContent.trim() +
            (claimed
              ? datasetList.order === "asc"
                ? "（当前升序）"
                : "（当前降序）"
              : active
                ? "，服务端未按这一列排序"
                : "，点击排序")
        );
      }
    }
  }

  // ---- 文件名搜索（真实请求：q 交给后端筛，不在前端过滤这一页） ----
  //
  // 为什么不前端过滤：列表一次只有 20 条，前端过滤只能筛出"这一页里匹配的"，
  // 用户会以为总共就这么几条。q 由后端在"当前用户的全部数据集"上过滤，total 才是诚实的。
  var datasetSearch = { timer: null };

  function datasetSearchInput() {
    return $("dataset-search");
  }

  function renderDatasetSearch() {
    var input = datasetSearchInput();
    var clearButton = $("dataset-search-clear");
    if (input) {
      // 清空按钮只在真有字时出现：一个永远可点的"清空"是装饰性控件。
      if (clearButton) {
        clearButton.hidden = !input.value;
      }
      // 请求在飞 = 正在搜索。aria-busy 让屏幕阅读器也知道结果还没到。
      if (datasetList.loading && datasetList.q) {
        input.setAttribute("aria-busy", "true");
      } else {
        input.removeAttribute("aria-busy");
      }
    } else if (clearButton) {
      clearButton.hidden = true;
    }
  }

  function applyDatasetSearch(text) {
    // 纯空白 = 不筛（与后端同一口径：空/全空白视为没有搜索词）
    var next = String(text === null || text === undefined ? "" : text).trim();
    if (next === datasetList.q) {
      renderDatasetSearch();
      return;
    }
    datasetList.q = next;
    // 换搜索词必须回到第一页：留在第 3 页搜一个只有 2 条结果的词，
    // 会得到"空页"，看起来像"没有匹配"，其实是页码错位。
    datasetList.offset = 0;
    datasetList.shownOffset = 0;
    renderDatasetSearch();
    loadDatasets();
  }

  function clearDatasetSearch() {
    var input = datasetSearchInput();
    if (input) {
      input.value = "";
    }
    applyDatasetSearch("");
    if (input) {
      input.focus(); // 清空后焦点留在输入框：键盘用户不用重新找回来
    }
  }

  function onDatasetSearchInput(event) {
    var value = event.target.value;
    renderDatasetSearch(); // 清空按钮/aria 立刻跟着字走，不等防抖
    if (datasetSearch.timer) {
      clearTimeout(datasetSearch.timer);
    }
    // 这里的 setTimeout 是"等你不打字了再发请求"，不是用它假装进度：
    // 界面上"正在搜索…"只在请求真的发出去之后才出现（datasetList.loading）。
    datasetSearch.timer = setTimeout(function () {
      datasetSearch.timer = null;
      applyDatasetSearch(value);
    }, DATASET_SEARCH_DEBOUNCE_MS);
  }

  function onDatasetSearchKeydown(event) {
    if (event.key === "Enter") {
      event.preventDefault(); // 没有表单可提交，回车只用来"立刻搜"
      if (datasetSearch.timer) {
        clearTimeout(datasetSearch.timer);
        datasetSearch.timer = null;
      }
      applyDatasetSearch(event.target.value);
    } else if (event.key === "Escape" && event.target.value) {
      event.preventDefault();
      clearDatasetSearch();
    }
  }

  function renderDatasetList() {
    renderDatasetListState();
    renderDatasetRows();
    renderDatasetPager();
    renderDatasetSortHeaders();
    renderDatasetSearch();

    // 刷新时表格里仍是上一页的行（保留旧行比闪成空白好读），但必须让屏幕阅读器
    // 和任何读 DOM 的人都知道"这页正在更新"——不能让人把旧行当成新结果。
    var table = $("dataset-table");
    if (table) {
      if (datasetList.loading) {
        table.setAttribute("aria-busy", "true");
      } else {
        table.removeAttribute("aria-busy");
      }
    }
  }

  async function loadDatasets(options) {
    var opts = options || {};
    if (opts.sort) {
      datasetList.sort = opts.sort;
    }
    if (opts.order) {
      datasetList.order = opts.order;
    }
    if (typeof opts.offset === "number" && opts.offset >= 0) {
      datasetList.offset = opts.offset;
    }
    if (typeof opts.q === "string") {
      datasetList.q = opts.q;
    }

    datasetList.loading = true;
    datasetList.ticket += 1;
    var ticket = datasetList.ticket;
    var requested = datasetList.offset; // 这次响应回来时它对应的是哪一页
    var requestedQ = datasetList.q; // 以及它对应的是哪个搜索词
    renderDatasetList();

    var result = await fetchDatasets(
      PAGE_SIZE, requested, datasetList.sort, datasetList.order, requestedQ, opts.force
    );
    if (ticket !== datasetList.ticket) {
      return; // 已经有更新的请求在路上，这次结果作废
    }
    datasetList.loading = false;

    if (!result.ok) {
      datasetList.error = errorOf(result);
      datasetList.items = [];
      datasetList.total = 0;
      datasetList.sortHonored = null;
      renderDatasetList();
      fillDatasetPicker([], datasetList.error);
      return;
    }

    var data = result.data || {};
    var items = Array.isArray(data.items) ? data.items : [];
    datasetList.items = items;
    datasetList.total = typeof data.total === "number" ? data.total : items.length;
    datasetList.sortHonored = sortHonored(items, datasetList.sort, datasetList.order);
    datasetList.error = null;
    datasetList.loaded = true;
    datasetList.shownOffset = requested; // 响应落地之后，页码才可以变
    datasetList.shownQ = requestedQ; // 这一屏显示的到底是哪个搜索词的结果，只有响应说了算

    // 页码可能因为后端数据变化而落到空页（比如删过数据集），退回到最后一页重取一次
    if (!items.length && datasetList.offset > 0) {
      datasetList.offset = Math.max(0, datasetList.offset - PAGE_SIZE);
      datasetList.loading = true;
      renderDatasetList();
      return loadDatasets({ force: opts.force });
    }

    renderDatasetList();
    loadDatasetPicker(opts.force); // 下拉单独取一次全量，不复用当前这一页
  }

  function sortBy(field) {
    // 点同一列：升/降切换；换一列：从降序开始（"最近上传的"和"最大的"更常见）
    var order = datasetList.sort === field && datasetList.order === "desc" ? "asc" : "desc";
    loadDatasets({ sort: field, order: order, offset: 0 });
  }

  // 把某一行的数据集变成"当前数据集"。列表里的字段不完整（没有预览和列名），
  // 所以这里只记 id，详情仍然走 GET /api/datasets/{id} 与 /profile。
  async function useDataset(datasetId, options) {
    var opts = options || {};
    if (!datasetId) {
      return false;
    }
    state.datasetId = datasetId;
    state.dataset = null;
    state.profile = null;
    state.workflowId = null;
    resetWorkflowPanel();
    clearCharts();
    clearReport();

    var found = null;
    for (var i = 0; i < datasetList.items.length; i += 1) {
      if (datasetList.items[i] && datasetList.items[i].dataset_id === datasetId) {
        found = datasetList.items[i];
        break;
      }
    }

    if (!opts.silent) {
      var box = $("analysis-context");
      if (box) {
        clear(box);
        var glyph = icon("info");
        if (glyph) {
          box.appendChild(glyph);
        }
        box.appendChild(
          document.createTextNode(
            "正在载入《" + (found ? datasetName(found) : datasetId) + "》的详情…"
          )
        );
      }
    }
    updateAskButton();

    if (opts.goDatasetView) {
      go("dataset");
    }
    // 详情与画像一起取：这两条接口是真实存在的，失败会各自如实报错。
    await Promise.all([loadDataset(), loadProfile()]);
    updateAnalysisContext();
    loadDatasetPicker();
    return true;
  }

  // 下拉是独立的一次请求：数据集列表接口按页返回，而"当前数据集"可能是任意一份。
  // 失败就如实报错，不留下一个空下拉让人以为"一份数据都没有"。
  // force=true（上传成功后的刷新链路）绕开短 TTL 缓存，保证新数据集立刻进下拉。
  async function loadDatasetPicker(force) {
    var select = $("dataset-select");
    if (!select) {
      return;
    }
    var result = await fetchDatasets(PICKER_LIMIT, 0, "uploaded_at", "desc", "", force);
    if (!result.ok) {
      datasetPickerTotal = null;
      fillDatasetPicker([], errorOf(result));
      return;
    }
    var data = result.data || {};
    var items = Array.isArray(data.items) ? data.items : [];
    datasetPickerTotal = typeof data.total === "number" ? data.total : items.length;
    fillDatasetPicker(items, null);
  }

  function fillDatasetPicker(items, error) {
    var select = $("dataset-select");
    var stateBox = $("dataset-picker-state");
    if (!select) {
      return;
    }

    clear(select);
    var placeholder = document.createElement("option");
    placeholder.value = "";
    if (error) {
      placeholder.textContent = "数据集列表读取失败";
    } else if (items && items.length) {
      placeholder.textContent = "请选择数据集（共 " + fmtInt(items.length) + " 个可选）";
    } else {
      placeholder.textContent = "还没有数据集";
    }
    placeholder.disabled = true;
    select.appendChild(placeholder);

    if (items && items.length) {
      items.forEach(function (item) {
        var option = document.createElement("option");
        option.value = item.dataset_id || "";
        option.textContent = datasetName(item) + " · " + datasetCellsText(item);
        select.appendChild(option);
      });
      // 当前数据集不在这一页里也照样保留：它可能在第 2 页，或刚上传完还没来得及刷新
      if (state.datasetId) {
        var exists = false;
        for (var i = 0; i < select.options.length; i += 1) {
          if (select.options[i].value === state.datasetId) {
            exists = true;
            break;
          }
        }
        if (!exists) {
          var extra = document.createElement("option");
          extra.value = state.datasetId;
          extra.textContent =
            "当前数据集（不在本页列表）· " + state.datasetId.slice(0, 8);
          select.appendChild(extra);
        }
        select.value = state.datasetId;
      }
    }
    select.disabled = !(items && items.length);

    if (stateBox) {
      if (error) {
        stateBox.className = "state error-text";
        stateBox.textContent = "数据集列表读取失败：" + error.message + "（" + error.code + "）";
      } else if (!items || !items.length) {
        stateBox.className = "state";
        stateBox.textContent = "还没有数据集：先在「数据资产」页上传一份 CSV / XLSX，才能开始分析。";
      } else if (!state.datasetId) {
        stateBox.className = "state";
        stateBox.textContent = "选一个数据集，下面就会显示它的文件名、行列数与质量摘要。";
      } else {
        stateBox.className = "state";
        // 下拉只列了最近 PICKER_LIMIT 份：剩下的必须说清楚，否则看起来像"数据只有这些"
        stateBox.textContent =
          datasetPickerTotal !== null && datasetPickerTotal > items.length
            ? "下拉里是最近 " +
              fmtInt(items.length) +
              " 份，另有 " +
              fmtInt(datasetPickerTotal - items.length) +
              " 份没列出来（下拉一次最多 " +
              fmtInt(PICKER_LIMIT) +
              " 份，全部数据在数据集页）。"
            : "";
      }
      // 窄屏（≤479px）由 app.css 把这一行截成 1 行（N-01：主按钮要落在首屏内）。
      // 被截掉的部分用 title 兜住，否则窄屏用户没有任何途径读到完整值
      // （a11y 审计 TRUNC_NO_HINT）；宽屏没截断时不留 title，避免多余的悬浮提示。
      stateBox.title =
        stateBox.scrollHeight > stateBox.clientHeight + 1 ? stateBox.textContent.trim() : "";
    }
  }

  // 工作台「数据资产」：真实的 total + 最近 3 条。没有数据就如实说没有。
  // force=true（上传成功后）绕开短 TTL 缓存，让总数与"最近"立刻反映新数据。
  async function loadAssets(force) {
    var totalBox = $("asset-total");
    var listBox = $("asset-recent");
    var stateBox = $("asset-state");
    if (!totalBox || !listBox) {
      return;
    }

    totalBox.textContent = "…";
    clear(listBox);
    if (stateBox) {
      stateBox.className = "state";
      stateBox.textContent = "正在读取数据集列表…";
    }

    var result = await fetchDatasets(3, 0, "uploaded_at", "desc", "", force);
    if (!result.ok) {
      var error = errorOf(result);
      totalBox.textContent = "—";
      metrics.datasetTotal = null;
      metrics.datasetTotalAt = null;
      renderMetrics();
      renderDatasetBadges([]);
      listBox.appendChild(
        emptyBlock("读取失败：" + error.message + "（" + error.code + "）", "warn")
      );
      if (stateBox) {
        stateBox.className = "state";
        stateBox.textContent = datasetsGapHint(error);
      }
      return;
    }

    var data = result.data || {};
    var items = Array.isArray(data.items) ? data.items : [];
    var total = typeof data.total === "number" ? data.total : items.length;
    totalBox.textContent = fmtInt(total);
    metrics.datasetTotal = total;
    metrics.datasetTotalAt = new Date().toISOString();
    renderMetrics();
    renderDatasetBadges(items);

    // 计数行的口径与时刻：写清"这是什么时间的快照"，不写"实时"（没有轮询）。
    var totalNote = $("asset-total-note");
    if (totalNote) {
      // 前导「· 」是分隔符：口径标签（.src）与这行时刻在窄屏会拼成一句，
      // 没有分隔符会读成「…的数据集截至 14:39」。这里只改措辞，不碰 fmtClock。
      totalNote.textContent = "· 截至 " + fmtClock(new Date().toISOString()) + " · " + fmtInt(total) + " 条";
    }

    if (stateBox) {
      stateBox.className = "state";
      stateBox.textContent = total > items.length
        ? "显示最近 " + fmtInt(items.length) + " 个，共 " + fmtInt(total) + " 个"
        : "";
    }

    clear(listBox);
    if (!items.length) {
      listBox.appendChild(
        emptyBlock("还没有数据集。上传后这里会出现真实的文件名、行列数与上传时间。", "dataset")
      );
      return;
    }

    items.forEach(function (item) {
      var row = el("button", "listrow");
      row.type = "button";
      var main = el("div", "listrow__main");
      main.appendChild(el("div", "listrow__q", datasetName(item)));
      var meta = el("div", "listrow__meta");
      meta.appendChild(el("span", null, datasetTypeText(item)));
      meta.appendChild(el("span", null, fmtBytes(pickField(item, ["size_bytes"]))));
      meta.appendChild(el("span", null, datasetCellsText(item)));
      meta.appendChild(el("span", null, "缺失 " + datasetMissingText(item)));
      meta.appendChild(el("span", null, fmtDateTime(datasetUploadedAt(item))));
      // 这一格是内部编号：给个中文标签，别让它以裸 hash 的样子出现在行里
      // （与历史页的「任务编号」同一个口径，方便对着服务端日志核对）。
      meta.appendChild(el("span", "listrow__mono", "数据编号 " + (item.dataset_id || "")));
      main.appendChild(meta);
      row.appendChild(main);
      var chevron = icon("chevron");
      if (chevron) {
        row.appendChild(chevron);
      }
      row.addEventListener("click", function () {
        useDataset(item.dataset_id, { goDatasetView: true });
      });
      listBox.appendChild(row);
    });
  }

  async function refreshDatasetViews() {
    // 上传成功后调用：列表与工作台资产都重新问一次后端，不用前端自己往数组里塞。
    // force 绕开短 TTL 缓存——写后必须读到最新，不能让刚上传的数据集被缓存挡掉。
    await Promise.all([loadDatasets({ force: true }), loadAssets(true)]);
  }

  /* ==================================================== 数据集与画像 */

  async function loadDataset() {
    if (!state.datasetId) {
      return null;
    }
    var summary = $("dataset-summary");
    var result = await api(API.dataset(state.datasetId));
    if (!result.ok) {
      var error = errorOf(result);
      if (summary) {
        summary.className = "state error-text";
        // 404 的含义是"这份数据不存在或不属于当前账号"，不能含糊成"连不上服务"。
        summary.textContent =
          error.message + "（" + error.code + "）" +
          (error.code === "NOT_FOUND"
            ? " —— 它可能已随服务重启清空，或者不属于当前登录的账号。"
            : "");
      }
      return null;
    }

    var data = result.data || {};
    state.dataset = data;

    if (summary) {
      summary.className = "state";
      summary.textContent =
        (data.original_filename || "（无文件名）") +
        " · " + fmtInt(data.row_count) + " 行 × " + fmtInt(data.column_count) + " 列" +
        " · " + fmtBytes(data.size_bytes);
    }

    renderPreview(data);
    renderColumns(data);
    return data;
  }

  function renderPreview(data) {
    var table = $("preview");
    if (!table) {
      return;
    }
    clear(table);

    var rows = Array.isArray(data.preview_rows) ? data.preview_rows : [];
    var names = Array.isArray(data.columns) ? data.columns.slice() : [];
    if (!names.length && rows.length) {
      names = Object.keys(rows[0]);
    }
    if (!names.length) {
      var body = document.createElement("tbody");
      var tr = document.createElement("tr");
      var td = el("td", "muted", "这份数据没有可预览的列");
      tr.appendChild(td);
      body.appendChild(tr);
      table.appendChild(body);
      return;
    }

    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    var labels = [];
    names.forEach(function (name) {
      var head = gridColumnCell(name);
      labels.push(head.label);
      headRow.appendChild(head.node);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    if (!rows.length) {
      var emptyRow = document.createElement("tr");
      var emptyCell = el("td", "muted", "没有可预览的行");
      emptyCell.colSpan = names.length;
      emptyRow.appendChild(emptyCell);
      tbody.appendChild(emptyRow);
    } else {
      rows.forEach(function (row) {
        var line = document.createElement("tr");
        names.forEach(function (name, index) {
          var value = row ? row[name] : null;
          var text = value === null || value === undefined || value === "" ? "—" : value;
          var cell = el("td", isNumericLike(text) ? "num" : null, text);
          cell.setAttribute("data-col", String(name));
          cell.setAttribute("data-label", labels[index]);
          line.appendChild(cell);
        });
        tbody.appendChild(line);
      });
    }
    table.appendChild(tbody);
  }

  function renderColumns(data) {
    var list = $("columns");
    if (!list) {
      return;
    }
    clear(list);

    var columns = Array.isArray(data.columns) ? data.columns : [];
    if (!columns.length) {
      list.appendChild(el("li", "muted", "这份数据没有列信息"));
      return;
    }
    columns.forEach(function (name) {
      var li = document.createElement("li");
      li.appendChild(document.createTextNode(displayColumn(name)));
      var dtype = data.dtypes ? data.dtypes[name] : null;
      if (dtype !== null && dtype !== undefined && dtype !== "") {
        // 列名保留用户原文（它可能本来就是中文），只有**类型名**给中文；
        // 未收录的 dtype 主显仍是中文（"未识别的类型"），原值进小字与 title。
        var info = zhEnum(DTYPE_TEXT, dtype, "类型");
        li.appendChild(document.createTextNode("（" + info.text + "）"));
        if (info.origin) {
          li.appendChild(el("span", "origin", info.origin));
        }
        li.title = "原始列名：" + name + " · 服务端类型原值：" + info.raw;
      }
      list.appendChild(li);
    });
  }

  async function loadProfile() {
    if (!state.datasetId) {
      return null;
    }
    var missing = $("missing-top3");
    var warnings = $("profile-warnings");
    var result = await api(API.profile(state.datasetId));

    if (!result.ok) {
      if (missing) {
        clear(missing);
        missing.appendChild(el("li", "muted", errorOf(result).message));
      }
      return null;
    }

    var profile = result.data || {};
    state.profile = profile;

    // 缺失值最多的 3 列：按 null_ratio 降序，这个排序是前端做的展示排序，
    // 用的仍是后端字段，没有自己造数。
    var columns = Array.isArray(profile.columns) ? profile.columns.slice() : [];
    if (missing) {
      clear(missing);
      var withNulls = columns
        .filter(function (column) {
          return Number(column.null_count) > 0;
        })
        .sort(function (a, b) {
          return Number(b.null_ratio) - Number(a.null_ratio);
        });

      if (!withNulls.length) {
        missing.appendChild(el("li", "muted", "所有字段都没有缺失值"));
      } else {
        withNulls.slice(0, 3).forEach(function (column) {
          var shown = displayColumn(column.name);
          var line = el(
            "li",
            null,
            shown + "：" + fmtInt(column.null_count) + " 个缺失（" + fmtPercent(column.null_ratio) + "）"
          );
          if (shown !== String(column.name)) {
            line.title = "原始列名：" + column.name;
          }
          missing.appendChild(line);
        });
      }
    }

    if (warnings) {
      clear(warnings);
      var notes = Array.isArray(profile.warnings) ? profile.warnings : [];
      if (!notes.length) {
        warnings.appendChild(el("li", "muted", "画像没有给出额外提示"));
      } else {
        notes.forEach(function (note) {
          warnings.appendChild(el("li", null, note));
        });
      }
    }

    renderDedupNote(profile);
    renderKpis(state.dataset, profile);
    return profile;
  }

  // 重复行提示。注意这里**不做减法**：后端 DatasetProfile 里没有「去重后行数」
  // 这个字段，前端自己算 row_count - duplicate_row_count 就等于凭空造一个数。
  // 宁可少显示一个数，也不显示一个界面上没有出处的数。
  function renderDedupNote(profile) {
    var note = $("dedup-note");
    if (!note) {
      return;
    }
    var duplicates = Number(profile.duplicate_row_count);
    if (!isFinite(duplicates) || duplicates <= 0) {
      note.hidden = true;
      note.textContent = "";
      return;
    }
    note.hidden = false;
    note.textContent =
      "画像检测到 " + fmtInt(duplicates) + " 行完全重复的记录（整行取值完全一致）。" +
      "服务端不提供「去重后行数」字段，所以这里不做减法；分析用的仍是原始数据。";
  }

  /* 来源标签：界面上的每个数字、每段文字都要能看出它是怎么来的 —— 这是本项目
     "每个数字都能回到某次工具调用的结果"这个主张在界面上的最小表达。
     文案与 title 集中在这里，避免同一个来源在不同页面出现两种说法。 */
  var SRC_TEXT = {
    raw: ["原始数据", "来自你上传的文件本身：上传时服务端解析出的字段"],
    calc: ["计算结果", "由服务端对这份数据做确定性计算得出，不是估算"],
    ai: ["AI 解释", "由大模型根据已算出的任务结果撰写；数字本身仍来自计算结果"],
    rules: ["规则模板", "这次没有用大模型，正文由规则模板生成，内容来自真实计算结果"],
    llm: ["大模型撰写", "这次的正文由已配置的大模型撰写"],
    api: ["接口", "直接来自服务端接口响应"]
  };
  function srcTag(kind) {
    var known = Object.prototype.hasOwnProperty.call(SRC_TEXT, kind);
    var pair = known ? SRC_TEXT[kind] : SRC_TEXT.api;
    var tag = el("span", "src src--" + (known ? kind : "api"), pair[0]);
    tag.title = pair[1];
    return tag;
  }

  /* 短时刻（HH:MM）：计数行里写"截至 11:57"比写完整日期更省地方，
     但仍然是真实时间戳，不是"刚刚"。
     服务端给的是 UTC（Z / +00:00），这里必须先用 Date 解析再取本地时分 ——
     不要从 ISO 串里直接截 HH:MM，那会整整差一个时区（与同屏 toLocaleString
     出来的本地时间自相矛盾，会让人误判数据新鲜度）。 */
  function fmtClock(iso) {
    var text = String(iso || "");
    var date = new Date(text);
    if (isNaN(date.getTime())) {
      return "—";
    }
    return String(date.getHours()).padStart(2, "0") + ":" + String(date.getMinutes()).padStart(2, "0");
  }

  function kpiCard(label, value, unit, note, warn, source) {
    var card = el("div", warn ? "kpi kpi--warn" : "kpi");
    var head = el("div", "kpi__label");
    var glyph = icon(warn ? "warn" : "info");
    if (glyph) {
      head.appendChild(glyph);
    }
    head.appendChild(document.createTextNode(label));
    card.appendChild(head);

    var valueLine = el("div", "kpi__value", value);
    if (unit) {
      valueLine.appendChild(el("span", "kpi__unit", unit));
    }
    card.appendChild(valueLine);
    if (note || source) {
      var noteLine = el("div", "kpi__note");
      if (source) {
        noteLine.appendChild(srcTag(source));
      }
      if (note) {
        noteLine.appendChild(document.createTextNode(note));
      }
      card.appendChild(noteLine);
    }
    return card;
  }

  function renderKpis(dataset, profile) {
    var grid = $("kpi-grid");
    if (!grid) {
      return;
    }
    if (!dataset && !profile) {
      return;
    }
    clear(grid);

    var rowCount = profile ? profile.row_count : dataset.row_count;
    var columnCount = profile ? profile.column_count : dataset.column_count;

    grid.appendChild(kpiCard("数据行数", fmtInt(rowCount), "行", "由服务端统计的行数", false, profile ? "calc" : "raw"));
    grid.appendChild(kpiCard("字段数", fmtInt(columnCount), "列", "由服务端统计的列数", false, profile ? "calc" : "raw"));

    if (dataset && dataset.size_bytes !== undefined) {
      grid.appendChild(kpiCard("文件大小", fmtBytes(dataset.size_bytes), "", "上传时服务端记录的原始字节数", false, "raw"));
    }

    if (profile) {
      var duplicates = Number(profile.duplicate_row_count);
      grid.appendChild(
        kpiCard(
          "完全重复的行",
          fmtInt(profile.duplicate_row_count),
          "行",
          duplicates > 0 ? "整行取值完全一致；分析仍用原始数据" : "画像未发现整行完全重复的记录",
          duplicates > 0,
          "calc"
        )
      );

      var columns = Array.isArray(profile.columns) ? profile.columns : [];
      var withNulls = columns.filter(function (column) {
        return Number(column.null_count) > 0;
      });
      grid.appendChild(
        kpiCard(
          "存在缺失的字段",
          fmtInt(withNulls.length),
          "列",
          "共 " + fmtInt(columns.length) + " 列，其中 " + fmtInt(withNulls.length) + " 列含缺失值",
          withNulls.length > 0,
          "calc"
        )
      );

      grid.appendChild(kpiCard("画像内存占用", fmtBytes(profile.memory_bytes), "", "由服务端统计的内存占用", false, "calc"));
    }
  }

  /* 工作台计数行第 4 格：当前分析对象。名字与行列数来自 GET /api/datasets/{id}
     的真实响应（state.dataset）；只拿到 id 时如实显示 id 并说明正在读详情，
     不去列表里猜一个名字顶上。 */
  function renderAssetCurrent() {
    var box = $("asset-current");
    if (!box) {
      return;
    }
    var note = $("asset-current-note");
    if (!state.datasetId) {
      box.textContent = "—";
      box.title = "还没有选择数据集";
      if (note) {
        note.textContent = "还没有选数据集";
      }
      return;
    }
    var name = state.dataset ? datasetName(state.dataset) : state.datasetId;
    box.textContent = name;
    box.title = name;
    if (note) {
      clear(note);
      note.appendChild(srcTag(state.dataset ? "raw" : "api"));
      note.appendChild(
        document.createTextNode(
          state.dataset
            ? datasetCellsText(state.dataset) + " · " + datasetTypeText(state.dataset)
            : "正在读取数据集详情…"
        )
      );
    }
  }

  /* ======================================================== 提问与分析 */

  function updateAnalysisContext() {
    renderAssetCurrent();
    var box = $("analysis-context");
    if (!box) {
      return;
    }
    clear(box);
    var glyph = icon("info");
    if (glyph) {
      box.appendChild(glyph);
    }

    if (state.datasetId) {
      var name = state.dataset ? state.dataset.original_filename : "当前数据集";
      var rows = state.dataset ? fmtInt(state.dataset.row_count) : "?";
      var cols = state.dataset ? fmtInt(state.dataset.column_count) : "?";
      box.appendChild(
        document.createTextNode(
          "本次分析使用：" + name + " · " + rows + " 行 × " + cols + " 列 · 数据编号 " + state.datasetId
        )
      );
    } else {
      var strong = el("strong", null, "数据资产");
      box.appendChild(document.createTextNode("还没有选择数据集：请先在"));
      box.appendChild(strong);
      box.appendChild(document.createTextNode("上传一份表格。"));
    }
    updateAskButton();
  }

  function updateAskButton() {
    var button = $("ask");
    if (!button) {
      return;
    }
    var question = $("question");
    var text = question ? question.value.trim() : "";
    var running = state.pollTimer !== null;

    var reason = "";
    if (!state.datasetId) {
      reason = "请先上传数据";
    } else if (text.length < 2) {
      reason = "问题太短了，至少写 2 个字";
    } else if (running) {
      reason = "上一次分析还在执行中";
    }

    button.disabled = reason !== "" || state.asking;
    button.title = reason;
  }

  // 读取「规划步数上限」下拉，钳到后端允许的 1–8（app/schemas_workflow.py 的 ge=1, le=8）。
  // 下拉本身只有 3/5/6/8 四个真值，这里再兜一层是防有人改了 DOM 或注入出界值——
  // 越界请求后端会回 422，钳在发送前比让用户白等一次报错诚实。拿不到就回落默认 6。
  function readMaxSteps() {
    var node = $("max-steps");
    var parsed = node ? parseInt(node.value, 10) : 6;
    if (!isFinite(parsed)) {
      parsed = 6;
    }
    return Math.max(1, Math.min(8, parsed));
  }

  async function ask() {
    if (state.asking) {
      return;
    }
    var status = $("ask-state");
    var button = $("ask");
    var question = $("question");
    var text = question ? question.value.trim() : "";

    if (!state.datasetId) {
      if (status) {
        status.className = "state error-text";
        status.textContent = "请先上传数据";
      }
      return;
    }
    if (text.length < 2) {
      if (status) {
        status.className = "state error-text";
        status.textContent = "问题太短了，至少写 2 个字";
      }
      return;
    }

    state.asking = true;
    setBusy(button, true);
    if (status) {
      status.className = "state";
      status.textContent = "正在提交…";
    }

    var result = await api(API.analysis, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dataset_id: state.datasetId, question: text, max_steps: readMaxSteps() }),
    });

    state.asking = false;
    setBusy(button, false);

    if (!result.ok) {
      var error = errorOf(result);
      if (status) {
        status.className = "state error-text";
        status.textContent = error.message;
      }
      updateAskButton();
      return;
    }

    var accepted = result.data || {};
    state.workflowId = accepted.workflow_id;

    if (status) {
      status.className = "state ok-text";
      status.textContent = "已提交，任务编号 " + state.workflowId;
    }

    resetWorkflowPanel();
    clearCharts();
    clearReport();
    startPolling();
    // 受理成功后自动转到「Agent 运行」视图（方案 01 §8.3）：发起页只有一行"已提交"，
    // 六阶段阶梯只在 runs 视图可见，用户不该在发起页干等。切视图不打断轮询——
    // applyView("runs") 不触碰 pollTimer / workflowId，轮询继续跑，进终态再由
    // paintSteps 收尾那条 go("report")（条件已含 runs）。
    go("runs");
  }

  /* ======================================================== 六阶段进度 */

  function resetWorkflowPanel() {
    var list = $("steps");
    if (list) {
      clear(list);
      STEP_ORDER.forEach(function (step, index) {
        var item = document.createElement("li");
        item.setAttribute("data-step", step);
        item.setAttribute("data-index", String(index));
        // data-state 只由 paintSteps() 按后端真实 step/status 改写；CSS 也只认它。
        // 用 data 属性而不是类名，是为了让"这一格是什么状态"在 DOM 里可枚举、可断言。
        item.setAttribute("data-state", "waiting");
        item.appendChild(el("span", "dot"));
        item.appendChild(zhEnumNode(STEP_NAMES, step, "阶段", "steps__name"));
        item.appendChild(el("span", "steps__state", STEP_STATE_TEXT.waiting));
        list.appendChild(item);
      });
    }

    var elapsed = $("elapsed");
    if (elapsed) {
      // 还没提交过分析：写"尚未开始"而不是一个孤零零的破折号，破折号是"取不到"的意思。
      elapsed.textContent = "尚未开始分析";
    }
    var workflowState = $("workflow-state");
    if (workflowState) {
      workflowState.textContent = "";
    }
    // 阶梯与 #steps 是同一份事实的两种画法：复位了明细行就必须复位 .seg 与计数，
    // 否则上一条运行（或上一条深链）的六段点亮会留在屏上，对着一条还没开始的运行说谎。
    // Wave E 实跑取证：SPA 内从 #/runs/<已完成的> 切到 #/runs/<查不到的>，
    // #steps 已经回到 waiting，而 ladder-count 还写着 "6 / 6"、六段还是 is-done。
    var ladder = $("ladder");
    if (ladder) {
      var segs = ladder.querySelectorAll(".seg[data-step]");
      for (var s = 0; s < segs.length; s += 1) {
        segs[s].classList.remove("is-done", "is-run", "is-skip", "is-failed");
      }
      ladder.setAttribute("aria-label", "进度：已完成 — 步 / 共 " + fmtInt(STEP_ORDER.length) + " 步");
    }
    setSlotText("ladder-count", "— / " + fmtInt(STEP_ORDER.length));
    var tasks = $("task-list");
    if (tasks) {
      clear(tasks);
    }
    hideErrorBox($("workflow-error"));
    updateAskButton();
  }

  function paintSteps(step, status) {
    var list = $("steps");
    if (!list) {
      return;
    }
    var items = list.querySelectorAll("li");
    if (!items.length) {
      return;
    }

    var current = STEP_ORDER.indexOf(step);
    var terminal = TERMINAL.indexOf(status) >= 0;
    if (current < 0) {
      // 认不出来的 step（比如后端加了新阶段）：终态就当成全部走完，
      // 否则当成还没开始。绝不猜一个中间位置。
      current = terminal ? STEP_ORDER.length : 0;
    }

    for (var i = 0; i < items.length; i += 1) {
      var item = items[i];
      item.classList.remove("done", "active", "failed");
      var stateCell = item.querySelector(".steps__state");
      var text;
      var dataState;

      if (i < current) {
        item.classList.add("done");
        text = STEP_STATE_TEXT.done;
        dataState = "done";
      } else if (i === current) {
        if (status === "FAILED") {
          item.classList.add("failed");
          text = STEP_STATE_TEXT.failed;
          dataState = "failed";
        } else if (terminal) {
          item.classList.add("done");
          text = STEP_STATE_TEXT.done;
          dataState = "done";
        } else {
          item.classList.add("active");
          text = STEP_STATE_TEXT.active;
          dataState = "active";
        }
      } else {
        // 走到这里说明这一步还没轮到。工作流已经结束时它不会被再执行，
        // 但后端并没有「跳过」这个语义，所以写「未执行」而不是「已跳过」。
        text = terminal ? STEP_STATE_TEXT.skipped : STEP_STATE_TEXT.waiting;
        dataState = terminal ? "skipped" : "waiting";
      }

      if (stateCell) {
        stateCell.textContent = text;
      }
      // data-state 与上面那三个类名是**同一件事的两种写法**，必须一起改：
      // app.css 只认 `li[data-state=…]`（1119-1131 行那七条规则），一条类名选择器都没有。
      // 以前这里只写类名，六行明细的圆点和状态字就永远停在 waiting 的灰态 ——
      // 右侧文字写着「已完成」、左侧圆点写着「还没开始」，同屏两句互相打脸（Wave E 实跑取证）。
      item.setAttribute("data-state", dataState);
    }

    syncLadder(current, status, terminal);
  }

  // 阶段阶梯（.ladder）与 #steps 明细是同一份事实的两种画法：都只读 step / status，
  // 数字口径仍是 step_index（= 已完成阶段数）/ 共 6 步。后端不暴露阶段级耗时，
  // 所以这里没有任何百分比、没有时间戳、没有推算出来的进度。
  function syncLadder(current, status, terminal) {
    var ladder = $("ladder");
    if (ladder) {
      var segs = ladder.querySelectorAll(".seg[data-step]");
      for (var s = 0; s < segs.length; s += 1) {
        var seg = segs[s];
        seg.classList.remove("is-done", "is-run", "is-skip", "is-failed");
        if (s < current) {
          seg.classList.add("is-done");
        } else if (s === current) {
          if (status === "FAILED") {
            seg.classList.add("is-failed");
          } else if (terminal) {
            seg.classList.add("is-done");
          } else {
            seg.classList.add("is-run");
          }
        } else if (terminal) {
          seg.classList.add("is-skip");
        }
      }
      ladder.setAttribute("aria-label", "进度：已完成 " + fmtInt(current) + " 步 / 共 " + fmtInt(STEP_ORDER.length) + " 步");
    }
    setSlotText("ladder-count", fmtInt(current) + " / " + fmtInt(STEP_ORDER.length));
  }

  function renderTasks(tasks) {
    var list = $("task-list");
    if (!list) {
      return;
    }
    clear(list);
    if (!Array.isArray(tasks) || !tasks.length) {
      return;
    }

    tasks.forEach(function (task) {
      var item = document.createElement("li");
      item.appendChild(el("span", "tid", task.task_id || "?"));
      item.appendChild(el("span", null, task.description || ""));
      item.appendChild(zhEnumNode(TASK_STATUS_TEXT, task.status, "任务状态", "state"));

      if (task.error) {
        var message = task.error.message || task.error.code || "失败";
        item.appendChild(el("span", "tmsg", "（" + message + "）"));
        if (task.error.code && task.error.message) {
          item.appendChild(el("span", "origin", String(task.error.code)));
        }
      }
      list.appendChild(item);
    });
  }

  function elapsedText(data) {
    var start = parseUtc(data.created_at);
    if (start === null) {
      return "—";
    }
    var end = parseUtc(data.finished_at);
    var missing = end === null;
    var seconds = ((missing ? Date.now() : end) - start) / 1000;
    if (seconds < 0) {
      // 本机时钟比服务端慢（或时间戳本身次序不对）时会算出负数。
      // 负的"已用时"是错的，夹成 0 又像是在说"刚刚开始" —— 两种都是在编数字，
      // 所以这里不给数字，只如实说明算不出来的原因。
      return missing
        ? "已用时 —（本机时钟比服务端慢 " + formatSeconds(-seconds) + "，无法计算）"
        : "已用时 —（结束时间比创建时间早 " + formatSeconds(-seconds) + "，时间戳次序不对）";
    }
    return "已用时 " + formatSeconds(seconds);
  }

  function stopPolling() {
    if (state.pollTimer !== null) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    updateAskButton();
    renderRunBadge();
  }

  function startPolling() {
    stopPolling();
    pollOnce();
    state.pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
    updateAskButton();
    renderRunBadge();
  }

  async function pollOnce() {
    if (!state.workflowId) {
      stopPolling();
      return;
    }

    var result = await api(API.workflow(state.workflowId));
    if (!result.ok) {
      var error = errorOf(result);
      stopPolling();
      renderErrorBox(
        $("workflow-error"),
        error,
        isRetryable(error)
          ? function () {
              startPolling();
            }
          : null
      );
      var stopped = $("workflow-state");
      if (stopped) {
        stopped.textContent = "轮询已停止";
      }
      return;
    }

    var data = result.data || {};
    captureLiveRunTasks(state.workflowId, data.tasks);
    await applyWorkflowStatus(data, true);
  }

  // 把一次状态接口的响应画到页面上：轮询（pollOnce）与 runs 深链（openRunDetail）共用同一段
  // 逻辑，避免出现"两处各画一遍、一处说谎"。navigateReport 只在本次页面**自己跟着跑**的那条
  // 运行进终态时为 true —— 用户手动深链到一条已经跑完的运行，不该被半路拽去报告视图。
  async function applyWorkflowStatus(data, navigateReport) {
    paintSteps(data.step, data.status);
    renderTasks(data.tasks);

    var workflowState = $("workflow-state");
    if (workflowState) {
      // 这一行是运行中界面唯一每帧都在变的地方：状态与步骤都必须先过中文映射表。
      // 后端给的是 10 个状态值 + 7 个 step 值（六个阶段 + done），认不出的值
      // 走"未识别的… + 原值小字"，绝不把裸英文直接画上屏。
      clear(workflowState);
      workflowState.appendChild(document.createTextNode("状态 "));
      workflowState.appendChild(zhEnumNode(WORKFLOW_STATUS_TEXT, data.status, "状态"));
      workflowState.appendChild(document.createTextNode(" · 步骤 "));
      workflowState.appendChild(zhEnumNode(STEP_NAMES, data.step, "阶段"));
      workflowState.appendChild(
        document.createTextNode("（" + fmtInt(data.step_index) + "/" + fmtInt(data.total_steps) + "）")
      );
    }
    var elapsed = $("elapsed");
    if (elapsed) {
      elapsed.textContent = elapsedText(data);
    }

    if (data.error) {
      var flowError = data.error;
      renderErrorBox(
        $("workflow-error"),
        flowError,
        isRetryable(flowError)
          ? function () {
              startPolling();
            }
          : null
      );
    } else {
      hideErrorBox($("workflow-error"));
    }

    if (TERMINAL.indexOf(data.status) < 0) {
      return;
    }

    // 进终态：先停轮询，再渲染结果。顺序反了会出现「已经结束还在打后端」。
    stopPolling();
    // 失败且没有图表时，说"失败了"而不是"没有生成图表"——后者听起来像正常跑完了没出图。
    if (data.status === "FAILED" && !(Array.isArray(data.charts) && data.charts.length)) {
      showChartsEmpty("failed");
    } else {
      // renderCharts 会先 clear：失败/无报告的任务不会残留上一次的图。
      renderCharts(Array.isArray(data.charts) ? data.charts : []);
    }

    if (data.has_report === true) {
      await loadReport();
      // 发起（analysis）或跟着跑（runs）时，进终态自动打开报告；用户若已手动
      // 切去别处（history/report/settings）就不把他拽回来。
      // navigateReport=false 是 runs 深链那条路：用户点名要看的是"这一条运行的执行过程"，
      // 半路把他拽到报告视图等于没回答他问的东西。
      if (navigateReport === true && (state.view === "analysis" || state.view === "runs")) {
        go("report");
      }
    } else if (data.status === "FAILED") {
      clearReport("本次分析失败，没有生成报告。失败原因见上方错误框。");
    } else {
      clearReport("本次分析没有生成报告（服务端没有留下报告记录）。");
    }

    loadRecentTasks();

    // 刚跑完的分析应当自己升上态势带：把这条的正文缓存作废，
    // 态势带正看着就立刻重取一次，没在看就把历史缓存标记过期、等下次进入视图自然取。
    // 这里不新增任何定时器 —— 态势带始终不参与 1000 毫秒轮询。
    postureCache.delete(state.workflowId);
    if (postureHostedHere(state.view)) {
      loadPosture(true);
    } else {
      posture.at = 0;
    }
  }

  /* ---------- runs 视图深链：三种真实状态各说各的话（Wave E 缺陷 3） ----------
     ① 状态接口答得出这一条 → 阶梯、状态行、已用时全部按真实 step_index / step / status 画；
     ② 状态接口 404，但归档接口 10 里有这条记录 → 它已经结束并归档：
        归档**不保存阶段序号**，所以这里一段假的点亮都不画，把"为什么阶梯是空的"写清楚，
        再把归档里真有的字段（状态、结束时间、整条任务的墙钟）摆出来，给一个点得到报告的入口；
     ③ 两边都查不到 → 「找不到这条运行，或当前账号无权访问」。后端对越权就是回 404，
        文案与之一致；**不静默回落到列表页**（docs/DECISIONS.md §8.3.1 禁止 fallback 复活）。
     这一整段没有任何前端定时器，也没有按时间推算的百分比：阶梯只由服务端给的 step_index 点亮。 */

  var ARCHIVED_RUN_LADDER_NOTE =
    "这条运行已经结束并归档。阶段级进度只在运行期间由服务端的状态接口提供，归档记录里不保存阶段序号，" +
    "所以上面六段阶梯对这条深链一段都不点亮 —— 那是「这一条没有可查的阶段数据」，不是「这条运行没有跑过」。";

  function hideRunNote() {
    var note = $("run-note");
    if (note) {
      clear(note);
      note.hidden = true;
    }
  }

  async function openRunDetail(workflowId) {
    // 先抹掉静态壳里那句「尚未开始分析」：这条链接指向的是一条**已经存在**的运行，
    // 在服务端回答之前，页面没有任何依据说它"还没开始"（Wave E 缺陷 3 的那句假话）。
    var pendingElapsed = $("elapsed");
    if (pendingElapsed) {
      pendingElapsed.textContent = "—";
      pendingElapsed.title = "正在向服务端确认这条运行";
    }

    var note = $("run-note");
    if (note) {
      clear(note);
      note.hidden = false;
      note.appendChild(el("p", "run-note__title", "正在向服务端确认这条运行…"));
    }

    var live = await api(API.workflow(workflowId));
    if (live.ok) {
      // ①：这次运行还在这台服务端的注册表里（正在跑，或刚跑完还没重启）。
      if (state.pollTimer !== null && state.workflowId !== workflowId) {
        stopPolling(); // 页面现在看的是链接里这条，不再跟那一条；不留下"还在轮询"的假象
      }
      state.workflowId = workflowId;
      hideRunNote(); // 这一条有真实进度，不需要"为什么阶梯是空的"那段说明
      var data = live.data || {};
      await applyWorkflowStatus(data, false);
      if (TERMINAL.indexOf(data.status) < 0) {
        // 还在跑：交回给唯一的真实进度来源 —— 状态接口的 1000 毫秒轮询（不是前端推算）。
        startPolling();
      }
      return;
    }

    var archived = await api(API.historyDetail(workflowId));
    var item = archived.ok && archived.data ? archived.data.item : null;
    var ownsSlots = state.pollTimer === null; // 有另一条正在被轮询时，不抢它那块执行过程面板
    if (item) {
      // ②：已归档。先把面板复位成"没有任何真实进度"，再写归档里真有的那几个字段。
      if (ownsSlots) {
        resetWorkflowPanel();
        var stateLine = $("workflow-state");
        if (stateLine) {
          clear(stateLine);
          stateLine.appendChild(document.createTextNode("这条运行不在服务端的运行状态里 · 归档状态 "));
          stateLine.appendChild(zhEnumNode(WORKFLOW_STATUS_TEXT, item.status, "状态"));
        }
        var elapsed = $("elapsed");
        if (elapsed) {
          // created_at / finished_at 是归档里的真实字段：整条任务的墙钟可以照说（契约 §6.3 允许的口径）。
          // 少了 finished_at 就不算 —— 归档记录都是终态，缺结束时间只可能是数据本身不完整，
          // 拿"现在"去补等于用前端时钟编一个数。
          if (item.finished_at) {
            elapsed.textContent = elapsedText(item);
            elapsed.title = "整条任务的墙钟：归档里的 created_at → finished_at（不是阶段级时间）";
          } else {
            elapsed.textContent = "—";
            elapsed.title = "归档记录里没有结束时间，整条任务的用时无法给出";
          }
        }
      }
      if (note) {
        clear(note);
        note.hidden = false;
        renderArchivedRunNote(note, item, ownsSlots);
      }
      return;
    }

    // ③：状态接口与归档接口都没有这一条（或不属于当前账号）。
    if (ownsSlots) {
      resetWorkflowPanel();
      var idle = $("elapsed");
      if (idle) {
        // "—" 在本页的口径是"取不到"。这里取不到的是这条运行本身。
        idle.textContent = "—";
        idle.title = "服务端没有这条运行的状态，也没有它的归档记录";
      }
    }
    if (note) {
      clear(note);
      note.hidden = false;
      renderMissingRunNote(note, workflowId, errorOf(archived));
    }
  }

  // 归档运行那段话：写清楚为什么阶梯空着、真实字段是什么、报告从哪里能看到。
  // 「查看报告」用的是 openHistoryFromPosture —— 它走归档接口 10，重启后照样打得开
  // （换成 #/report/{id} 就会去打内存注册表的接口 7，那条链接对归档运行是死的）。
  function renderArchivedRunNote(note, item, ownsSlots) {
    note.appendChild(el("p", "run-note__title", "这条运行已经结束，并且已经归档"));
    note.appendChild(el("p", "run-note__body", ARCHIVED_RUN_LADDER_NOTE));
    if (!ownsSlots) {
      note.appendChild(
        el("p", "run-note__body", "另有一条分析正在本页跟随轮询，上面执行过程面板显示的是那一条，不是这条归档运行。")
      );
    }

    var meta = el("p", "run-note__meta");
    meta.appendChild(document.createTextNode("任务编号 "));
    var mono = el("span", "listrow__mono", item.workflow_id || "—");
    meta.appendChild(mono);
    meta.appendChild(document.createTextNode(" · 状态 "));
    meta.appendChild(zhEnumNode(WORKFLOW_STATUS_TEXT, item.status, "状态"));
    if (item.finished_at) {
      var ended = el("span", null, " · 结束于 " + fmtDateTime(item.finished_at));
      ended.title = rawTimeTitle(item.finished_at);
      meta.appendChild(ended);
    }
    note.appendChild(meta);

    var row = el("div", "row row--tight");
    if (item.has_report) {
      row.appendChild(
        postureAction("查看报告", function () {
          openHistoryFromPosture(item.workflow_id);
        }, "这份报告的正文在归档记录里，走的是重启后仍然可用的归档接口；点完会切到历史记录页的归档详情")
      );
    } else {
      row.appendChild(
        postureAction("打开归档详情", function () {
          openHistoryFromPosture(item.workflow_id);
        }, "这条记录归档了但没有报告，归档详情里有它的状态与失败原因")
      );
    }
    note.appendChild(row);
  }

  function renderMissingRunNote(note, workflowId, error) {
    note.appendChild(el("p", "run-note__title", "找不到这条运行，或当前账号无权访问"));
    note.appendChild(
      el(
        "p",
        "run-note__body",
        "运行状态接口与归档接口都没有交出链接里这一条。后端对「不是你的记录」和「记录不存在」" +
          "回的是同一个 404，所以这里无法区分是哪一种；页面不会把你领到别的记录上去。"
      )
    );
    var meta = el("p", "run-note__meta");
    meta.appendChild(document.createTextNode("链接里的任务编号 "));
    meta.appendChild(el("span", "listrow__mono", String(workflowId || "—")));
    if (error && error.code) {
      var code = el("span", "origin", "原值码：" + String(error.code));
      if (error.message) {
        code.title = error.message;
      }
      meta.appendChild(code);
    }
    note.appendChild(meta);
  }

  /* ============================================================ 图表 */

  // 图表区的空态有五种原因，每一种都要说清是哪一种。
  // 全站只用一句"分析完成后显示"，会让"任务失败了"和"这条链接还在跑"看起来一模一样。
  var CHARTS_EMPTY_TEXT = {
    idle: "还没有执行分析：图表会在分析跑完之后出现在这里。",
    running: "这条分析任务还在执行中，图表要等它进入终态才会生成。",
    none: "这次分析没有生成图表。",
    failed: "这条分析任务失败了，没有生成图表。",
    unknown: "取不到这条任务的状态（请求失败），无法确认它有没有图表。",
  };

  function showChartsEmpty(reason) {
    var box = $("charts");
    if (!box) {
      return;
    }
    clear(box);
    box.appendChild(emptyBlock(CHARTS_EMPTY_TEXT[reason] || CHARTS_EMPTY_TEXT.idle, "analysis"));
  }

  // 深链接 / 硬刷新打开某条任务时，图表区必须显示真实内容，而不是永远空着。
  // 唯一真实数据源是 GET /api/workflows/{id} 的 data.charts —— 报告接口不返回图表，
  // 所以"取报告"和"取图表"是两次请求，不能靠报告接口顺带把图带回来（它没有）。
  async function loadChartsFor(workflowId) {
    if (!workflowId) {
      showChartsEmpty("idle");
      return;
    }
    var result = await api(API.workflow(workflowId));
    if (state.workflowId !== workflowId) {
      return; // 用户已经切到别的任务：这次响应作废，否则会把上一条任务的图贴到新任务上
    }
    if (!result.ok) {
      showChartsEmpty("unknown");
      return;
    }
    var data = result.data || {};
    // 顺手留住这条运行的任务简报：证据行里的"任务描述"就来自这里的 tasks。
    captureLiveRunTasks(workflowId, data.tasks);
    var charts = Array.isArray(data.charts) ? data.charts : [];
    if (TERMINAL.indexOf(data.status) < 0) {
      // 还没跑完：不画一张空图冒充结果，改成跟着真实状态轮询，
      // 进终态后由终端分支把图与报告一起画出来。
      showChartsEmpty("running");
      startPolling();
      return;
    }
    if (data.status === "FAILED" && !charts.length) {
      showChartsEmpty("failed");
      return;
    }
    renderCharts(charts); // 空数组时 renderCharts 会写"这次分析没有生成图表。"
  }

  function clearCharts() {
    showChartsEmpty("idle");
  }

  function renderCharts(charts) {
    var box = $("charts");
    if (!box) {
      return;
    }
    clear(box);

    if (!Array.isArray(charts) || !charts.length) {
      box.appendChild(emptyBlock("这次分析没有生成图表。", "analysis"));
      return;
    }

    charts.forEach(function (chart) {
      var figure = document.createElement("figure");

      // 图片地址只用后端给的 url；为空才按 filename 拼一次兜底。
      // workflowId 也为空时干脆不创建 img —— 否则会去请求一个 /api/workflows/null/... 的地址。
      var src = chart.url || (state.workflowId ? API.chart(state.workflowId, chart.filename) : "");

      if (src) {
        var img = document.createElement("img");
        img.src = src;
        img.alt = chart.title || chart.filename || "分析图表";
        img.loading = "lazy";
        // 加载态是纯文本，不用灰色骨架：一块灰底很容易被当成「一张真实但空的数据图」。
        var failed = false;
        img.addEventListener("error", function () {
          if (failed) {
            return;
          }
          failed = true;
          clear(figure);
          figure.appendChild(emptyBlock("图表文件读不到：" + (chart.filename || "（无文件名）"), "warn"));
        });
        figure.appendChild(img);
      } else {
        figure.appendChild(emptyBlock("这张图没有可用的地址（url 与 filename 都为空）。", "warn"));
      }

      var caption = document.createElement("figcaption");
      caption.appendChild(el("div", "ctitle", chart.title || chart.filename || "（无标题）"));

      var meta = el("div", "cmeta");
      meta.appendChild(zhEnumNode(CHART_TYPE_TEXT, chart.chart_type, "图型"));
      // point_count 是**图上画了几个点**，不是「已核对的行数」。两者不比较、不做差。
      if (chart.point_count !== undefined && chart.point_count !== null) {
        meta.appendChild(el("span", null, "本图 " + fmtInt(chart.point_count) + " 个数据点"));
      }
      if (chart.x_field) {
        meta.appendChild(axisFieldNode("横轴 ", chart.x_field));
      }
      if (chart.y_field) {
        meta.appendChild(axisFieldNode("纵轴 ", chart.y_field));
      }
      if (chart.source_task_id) {
        meta.appendChild(el("span", null, "来自任务 " + chart.source_task_id));
      }
      caption.appendChild(meta);

      var hint = el("div", "cmeta");
      hint.appendChild(el("span", null, "图中数值请以报告区表格里的原值为准；完整分组名见报告正文。"));
      caption.appendChild(hint);

      figure.appendChild(caption);
      box.appendChild(figure);
    });
  }

  /* ============================================================ 报告 */

  function clearReport(message) {
    var box = $("report");
    if (!box) {
      return;
    }
    clear(box);
    box.appendChild(emptyBlock(message || "分析完成后显示", "report"));
  }

  async function loadReport(options) {
    var opts = options || {};
    if (!state.workflowId) {
      return;
    }
    // 报告接口不返回图表，所以需要图表的入口（深链接、硬刷新）在这里顺便取一次工作流状态。
    // 这一次**要等**：它只有一发不到 2KB 的状态确认，而证据行的"任务描述"活在它的 tasks 里
    // （报告接口 7 只给 results，没有 description）。不等就会出现同一条链接刷新两次、
    // 一次有任务描述一次没有的抖动（Wave E 实测复现过：desc 少了「按地区分组汇总销售额」）。
    // 图表 PNG 本身仍然不阻塞正文 —— renderCharts 只挂 img 地址，加载由浏览器自己排。
    if (opts.charts) {
      await loadChartsFor(state.workflowId);
    }
    var box = $("report");
    if (box) {
      clear(box);
      box.appendChild(emptyBlock("正在读取报告…", "report"));
    }

    // 报告接口只在这一处被调用（进终态且 has_report 为真时取一次）。
    var result = await api(API.report(state.workflowId));
    if (!result.ok) {
      // 接口 7 读的是服务进程内存里的注册表，重启后必然为空 —— 而归档库里还留着报告正文。
      // 所以这里回退到接口 10；两条都不交出内容才报错（06 D-6：归档态不冒充运行态）。
      var archived = await api(API.historyDetail(state.workflowId));
      var archivedReport = archived.ok && archived.data ? archived.data.report : null;
      if (archivedReport) {
        renderReport({ report: archivedReport, fromArchive: true });
        return;
      }
      if (box) {
        clear(box);
        renderErrorBox(box, errorOf(archived.ok ? result : archived), null);
      }
      return;
    }
    renderReport(result.data || {});
  }

  // 校验问题行：中文主显（问题码 → 一句话）+ 后端原文 message（本来就是中文），
  // 码本身降到可查证的 .origin / title，不再顶在句首当主显。
  function validationIssueLine(issue) {
    var line = el("p", "issue issue--" + String((issue && issue.severity) || "info"));
    line.appendChild(zhEnumNode(VALIDATION_ISSUE_TEXT, issue && issue.code, "校验提示"));
    line.appendChild(document.createTextNode("：" + ((issue && issue.message) || "")));
    return line;
  }

  // 数字溯源横幅：validation 为空、未通过、通过，三种都要说清楚。
  function renderValidation(container, validation) {
    if (!container) {
      return;
    }

    if (!validation) {
      var plain = el("div", "banner");
      plain.appendChild(el("h4", null, "本次没有校验结果"));
      plain.appendChild(el("p", null, "服务端没有返回校验结果，正文里的数字请自行对照下方表格。"));
      container.appendChild(plain);
      return;
    }

    var checked = fmtInt(validation.checked_numbers);
    var unsupported = Array.isArray(validation.unsupported_numbers) ? validation.unsupported_numbers : [];
    var issues = Array.isArray(validation.issues) ? validation.issues : [];

    if (validation.passed === false) {
      var warn = el("div", "banner banner--warn");
      var warnHead = el("h4");
      var warnIcon = icon("warn");
      if (warnIcon) {
        warnHead.appendChild(warnIcon);
      }
      warnHead.appendChild(
        document.createTextNode("校验未通过：有 " + unsupported.length + " 个数字在工具结果里找不到出处")
      );
      warn.appendChild(warnHead);
      warn.appendChild(el("p", null, "已核对 " + checked + " 个数字，其中下面这些没有依据："));

      if (unsupported.length) {
        var list = document.createElement("ul");
        unsupported.forEach(function (number) {
          list.appendChild(el("li", null, String(number)));
        });
        warn.appendChild(list);
      }

      issues
        .filter(function (issue) {
          return issue.severity === "error";
        })
        .forEach(function (issue) {
          warn.appendChild(validationIssueLine(issue));
        });
      container.appendChild(warn);
      return;
    }

    var ok = el("div", "banner banner--ok");
    var okHead = el("h4");
    var okIcon = icon("check");
    if (okIcon) {
      okHead.appendChild(okIcon);
    }
    okHead.appendChild(document.createTextNode("数字已逐条核对（共核对 " + checked + " 个数字）"));
    ok.appendChild(okHead);

    issues
      .filter(function (issue) {
        return issue.severity === "warning";
      })
      .forEach(function (issue) {
        ok.appendChild(validationIssueLine(issue));
      });
    container.appendChild(ok);
  }

  // 报告与归档详情里的数据表。表头 = 展示名，但**取值一律用原始键**（拿展示名去 row[key]
  // 会画出一整张全是破折号的表）；原始键同时挂在 data-col 与 title 上，谁要核对都能核对。
  function renderTable(rows) {
    var wrap = el("div", "table-wrap");
    var table = document.createElement("table");
    table.className = "dtable";
    var keys = Object.keys(rows[0]);
    var labels = [];

    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    keys.forEach(function (key) {
      var head = gridColumnCell(key);
      labels.push(head.label);
      headRow.appendChild(head.node);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    rows.forEach(function (row) {
      var line = document.createElement("tr");
      keys.forEach(function (key, index) {
        var value = row ? row[key] : null;
        var text = value === null || value === undefined || value === "" ? "—" : value;
        var cell = el("td", isNumericLike(text) ? "num" : null, text);
        cell.setAttribute("data-col", String(key));
        cell.setAttribute("data-label", labels[index]);
        line.appendChild(cell);
      });
      tbody.appendChild(line);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  // 把一份 FinalReport 渲染进容器。主报告区与历史详情共用。
  // DOM 契约：`.doc__head / .doc__body / .doc__sources` 三层必须都由这里产出（契约 §7.1），
  // `#report-id / #report-title / #report-created` 是被跨文件 query 的 id；`#report` 会被 clear()，
  // 所以静态壳里放不下这三个节点（Wave B/C 集成裂缝 17）。
  // 第三个参数 evidence 是**可选的补充明细**（Wave E 缺陷 1）：{results, tasks}，
  // 来自还活在那次运行里的接口 7 / 接口 6。传两个参数（归档链路）与不传都照样渲染 ——
  // 契约 §7.2 要的是"全站只有这一个报告渲染函数"，这一条没变；证据行的事实来源只是变多了。
  function renderReportInto(container, report, evidence) {
    var head = el("header", "doc__head");
    var eyebrow = el("p", "eyebrow");
    eyebrow.appendChild(document.createTextNode("分析报告 · 编号 "));
    var rid = el("span", "doc__rid", report.report_id || "—");
    rid.id = "report-id";
    eyebrow.appendChild(rid);
    head.appendChild(eyebrow);

    var title = el("h2", "doc__title", report.title || "（无标题报告）");
    title.id = "report-title";
    head.appendChild(title);

    var meta = el("p", "doc__meta");
    var created = el("b", null, report.generated_at ? fmtDateTime(report.generated_at) : "—");
    created.id = "report-created";
    var createdWrap = el("span", null, "生成时间 ");
    createdWrap.appendChild(created);
    meta.appendChild(createdWrap);
    // 正文来源用报告总述首段的逐字标记判断，不看 report.llm_used：
    // 那个字段只回答"计划是不是模型给的"，"模型规划 + 模板正文"这种中间态在它眼里
    // 和"模型写正文"完全一样 —— 用它标注会在中间态说出一句假话。
    var source = postureSourceTag(report);
    var stamp = el(
      "span",
      "src src--" + (source.key === "model-wrote" ? "llm" : source.key === "model-planned" ? "ai" : "rules"),
      source.label
    );
    stamp.title = source.matched
      ? "按报告总述首段的逐字标记判定正文是谁写的"
      : "报告总述的首段不是我们认识的三种标记之一（多半是文案改版），所以这里不下结论";
    meta.appendChild(stamp);
    head.appendChild(meta);
    container.appendChild(head);

    var body = el("div", "doc__body");
    if (report.summary) {
      body.appendChild(el("p", "doc__text summary", report.summary));
    }

    var sections = Array.isArray(report.sections) ? report.sections : [];
    sections.forEach(function (section) {
      var block = el("section", "doc__section block");
      block.appendChild(el("h3", null, section.heading || "（无小标题）"));
      if (section.text) {
        block.appendChild(el("p", "doc__text", section.text));
      }
      if (Array.isArray(section.table) && section.table.length) {
        block.appendChild(renderTable(section.table));
      }
      var cites = Array.isArray(section.cites) ? section.cites : [];
      if (cites.length) {
        var citeLine = el("p", "doc__cites cites");
        citeLine.appendChild(document.createTextNode("依据："));
        citeLine.appendChild(docCiteSupers(cites));
        block.appendChild(citeLine);
      }
      body.appendChild(block);
    });

    var notes = Array.isArray(report.data_quality_notes) ? report.data_quality_notes : [];
    if (notes.length) {
      var quality = el("section", "doc__quality block");
      quality.appendChild(el("h4", null, "数据质量提示"));
      notes.forEach(function (note) {
        quality.appendChild(el("p", null, note));
      });
      body.appendChild(quality);
    }
    container.appendChild(body);

    // 证据区：正文每个 sup.cite 的 task-id 与这里逐条 .src-line 一一对应（契约 §7.2 规则 1）。
    // citations 是"每次工具调用"的记录；拿不到就不画空壳。
    var cites = collectCiteTaskIds(report);
    if (cites.length) {
      var results = evidence && Array.isArray(evidence.results) ? evidence.results : null;
      var briefs = evidence && Array.isArray(evidence.tasks) ? evidence.tasks : null;
      var resultByTask = indexByTaskId(results);
      var briefByTask = indexByTaskId(briefs);
      var whereByTask = collectSectionHeadingsByCite(report);

      var foot = el("footer", "doc__sources");
      foot.appendChild(el("h4", null, "证据来源"));
      // 「取数调用 N 次」= 下面逐条 .src-line 的条数（cites 去重并集，与正文角标同源，
      // 所以这个数字和屏幕上看得见的证据行严格数得上；06 D-4 的口径词）。
      foot.appendChild(
        el("p", "note", "取数调用 " + fmtInt(cites.length) + " 次（下面每条对应正文里同编号的角标）")
      );
      if (results === null) {
        // 归档分支（Wave E 缺陷 1）：这条运行的工具明细**不在**我们能读到的那份响应里。
        // 归档接口 10 只还原 FinalReport，citations 就是裸编号数组，没有工具名也没有行数；
        // 明细活在接口 7 的 results（内存注册表），服务重启后 404。所以这里如实说明缺什么，
        // 而不是写一句"见本行的编号"这种什么都没说的事实空壳。
        foot.appendChild(el("p", "note", ARCHIVED_EVIDENCE_NOTE));
      }

      cites.forEach(function (taskId) {
        var line = el("div", "src-line");
        line.setAttribute("data-task-id", taskId);
        line.appendChild(el("span", "src-line__tid", taskId));

        var result = resultByTask[taskId];
        var where = whereByTask[taskId] || [];
        var whereText = where.length ? "正文出现在：" + where.join("、") : "正文里没有小节标到这个编号";

        if (result) {
          // 有明细：每一个字都来自 results[]（app/schemas_tool.py::ToolResult）。
          var brief = briefByTask[taskId];
          var bits = [];
          if (brief && brief.description) {
            bits.push(String(brief.description));
          }
          bits.push(
            result.ok === false
              ? "这次取数失败了：" + (result.error_message || result.error_code || "服务端没给原因")
              : "返回 " + fmtInt(result.row_count) + " 行"
          );
          if (Array.isArray(result.columns) && result.columns.length) {
            bits.push("列：" + result.columns.join("、"));
          }
          if (typeof result.elapsed_ms === "number") {
            // elapsed_ms 是**单次工具调用**自己的耗时。它不是阶段级数字，也不进阶梯 ——
            // 后端没有任何阶段级时间字段（契约 §6.3），所以这句话只描述这一个工具。
            bits.push(
              "这一次工具调用耗时 " + fmtInt(result.elapsed_ms) + " 毫秒（单次调用的时间，不是六阶段任何一个阶段的进度）"
            );
          }
          bits.push(whereText);
          line.appendChild(el("span", "src-line__desc", bits.join(" · ")));
          if (result.tool_name) {
            // 工具名保留服务端原值（七个白名单名之一），翻成中文就没法回头查代码了。
            var tool = el("span", "src-line__tool", String(result.tool_name));
            tool.title = "results[].tool_name 原值：工具白名单见 app/tools/registry.py";
            line.appendChild(tool);
          }
        } else if (results !== null) {
          // 明细在，但这一条不在里面 —— 那也是事实，不替它编一句"跑了什么"。
          line.appendChild(
            el("span", "src-line__desc", "服务端这次交回的工具结果里没有编号 " + taskId + " 的记录。" + whereText)
          );
        } else {
          line.appendChild(
            el("span", "src-line__desc", "归档只保存了编号 " + taskId + "，工具与行数要等该次运行还在服务端时才读得到。" + whereText)
          );
        }
        foot.appendChild(line);
      });
      container.appendChild(foot);

      // 让正文角标可点/可键盘跳到对应证据行：给**这一份文档容器里每一个** sup.cite 挂上
      // tabindex（Wave E 缺陷 2：以前只挂 .doc__cites 那一个段落，正文里别处出现的角标
      // 就静默地到不了键盘）。事件委托绑在 init()，只认标了 tabindex="0" 的 sup。
      var sups = container.querySelectorAll("sup.cite[data-task-id]");
      for (var s = 0; s < sups.length; s += 1) {
        sups[s].setAttribute("tabindex", "0");
        sups[s].setAttribute("role", "link");
        sups[s].setAttribute("aria-label", "跳到证据来源 " + sups[s].getAttribute("data-task-id"));
      }
    }
  }

  // 归档运行读不到工具明细时的那句话（证据区与 runs 深链共用同一条口径，两处不许各写一套）。
  var ARCHIVED_EVIDENCE_NOTE =
    "这条运行已经归档：服务端归档里只保存了引用编号（T1、T2…）与报告正文，" +
    "每次取数用的工具、返回行数、单次调用耗时存在该次运行的结果明细里，只有它还在服务端时才读得到（服务重启后就读不到了）。";

  // 工具结果 / 任务简报都是"按 task_id 索引"的数组，用同一个索引函数。
  function indexByTaskId(list) {
    var by = {};
    if (!Array.isArray(list)) {
      return by;
    }
    list.forEach(function (item) {
      if (item && item.task_id) {
        by[String(item.task_id)] = item;
      }
    });
    return by;
  }

  // 编号 → 引用它的那些小节标题。归档分支没有工具明细，这是仍然真实可定位的信息：
  // "T1 出现在哪几节"直接来自 sections[].cites，不是猜的。
  function collectSectionHeadingsByCite(report) {
    var sections = report && Array.isArray(report.sections) ? report.sections : [];
    var out = {};
    sections.forEach(function (section) {
      var list = section && Array.isArray(section.cites) ? section.cites : [];
      var heading = String((section && section.heading) || "（无小标题）");
      list.forEach(function (id) {
        var key = String(id);
        if (!out[key]) {
          out[key] = [];
        }
        if (out[key].indexOf(heading) < 0) {
          out[key].push(heading);
        }
      });
    });
    return out;
  }

  // sections[].cites 去重并集：正文 sup 与证据区一一对应的口径（06 D-4：citations 才是"取数调用"数）。
  function collectCiteTaskIds(report) {
    var sections = report && Array.isArray(report.sections) ? report.sections : [];
    var seen = {};
    var out = [];
    sections.forEach(function (section) {
      var list = section && Array.isArray(section.cites) ? section.cites : [];
      list.forEach(function (id) {
        var key = String(id);
        if (!seen[key]) {
          seen[key] = true;
          out.push(key);
        }
      });
    });
    return out;
  }

  // 正文角标 sup.cite → 跳到同一份文档里编号相同的证据行 .src-line[data-task-id]。
  // 从角标往上找**第一个同时装着这条证据行的祖先**（即这一份 doc/抽屉），
  // 只在它里面找目标，避免跳到另一份报告或态势带里的同名编号行。
  // 事件委托绑在 init()：只认被显式标了 tabindex="0" 的 sup（只有报告正文会标，
  // 态势带行内角标没有证据区可跳，保持不动）。
  function jumpToEvidence(sup) {
    var taskId = sup.getAttribute("data-task-id");
    if (!taskId) {
      return;
    }
    var selector = ".src-line[data-task-id=\"" + taskId + "\"]";
    var scope = sup.parentElement;
    var line = null;
    while (scope) {
      line = scope.querySelector(selector);
      if (line) {
        break;
      }
      scope = scope.parentElement;
    }
    if (!line) {
      return; // 这一份文档没有对应证据行（理论上角标集合=证据行集合，不应发生）：不动，
              // 好过把页面滚到一个不存在的位置假装"跳了"。
    }
    line.scrollIntoView({ behavior: "smooth", block: "center" });
    line.classList.add("is-jump");
    window.setTimeout(function () {
      line.classList.remove("is-jump");
    }, 1400);
  }

  function renderReport(data) {
    var box = $("report");
    if (!box) {
      return;
    }
    clear(box);

    if (data.fromArchive) {
      var fromArchiveNote = el("p", "note note--archive");
      fromArchiveNote.textContent =
        "这份报告读自归档库。服务进程已经不再有那次运行的内存状态，所以阶段进度与工具调用的逐条明细不在这里显示；" +
        "下面的数字仍可按编号逐条对照。";
      box.appendChild(fromArchiveNote);
    }

    renderValidation(box, data.validation);

    // 归档里的报告可能因为格式升级解析不出来，这时服务端返回 report=null。
    // 必须显式判 null —— 否则会渲染出一份「空标题的空报告」，看起来像报告坏了。
    if (!data.report || data.report.title === undefined) {
      var banner = el("div", "banner banner--warn");
      banner.appendChild(el("h4", null, "这份报告无法显示"));
      banner.appendChild(
        el("p", null, "归档记录里的报告解析不出来（多半是报告格式升级前存下的），其余信息仍然可用。")
      );
      box.appendChild(banner);
      return;
    }

    // evidence：接口 7 的 results（工具明细）+ 接口 6 交回过的任务简报（含 description）。
    // 两者都只在"那次运行还在服务端"时存在，所以缺任何一个都不影响另一条分支的诚实性。
    renderReportInto(box, data.report, {
      results: Array.isArray(data.results) ? data.results : null,
      tasks: liveRunTasksFor(state.workflowId),
    });

    var meta = el("div", "meta");
    if (Array.isArray(data.results)) {
      meta.appendChild(el("span", null, fmtInt(data.results.length) + " 个工具结果"));
    }
    if (data.llm_used !== undefined && data.report.llm_used !== data.llm_used) {
      // 两个字段不一致时如实说出来，但用中文口径写，不甩 "llm_used=true" 这种字段名=值。
      var flag = el("span", null, "是否用了大模型：" + (data.llm_used ? "是" : "否"));
      flag.title = "服务端原值：llm_used=" + String(data.llm_used) + "（报告正文里另有自己的标记）";
      meta.appendChild(flag);
    }
    if (meta.childNodes.length) {
      box.appendChild(meta);
    }
  }

  /* ======================================================== 历史任务 */

  // 跨版本的坏值会被后端原样透出（契约里是 WorkflowStatus | str），所以这里必须容忍任意字符串：
  // 认识的翻中文，不认识的**主显仍是中文**（"未识别的状态"），原值进 .origin 小字与 title。
  function historyStatusText(status) {
    return zhEnum(WORKFLOW_STATUS_TEXT, status, "状态").text;
  }

  function historyStatusNode(status) {
    return zhEnumNode(WORKFLOW_STATUS_TEXT, status, "状态");
  }

  function historyRow(item, onClick, actionText) {
    var row = el("button", "listrow");
    row.type = "button";

    var main = el("div", "listrow__main");
    main.appendChild(el("div", "listrow__q", item.question || "（无问题文本）"));

    var meta = el("div", "listrow__meta");
    var statusCell = el("span", null, "状态 ");
    statusCell.appendChild(historyStatusNode(item.status));
    meta.appendChild(statusCell);
    meta.appendChild(el("span", null, item.has_report ? "有报告" : "无报告"));
    // 这一格只回答"分析计划是谁给的"：report.llm_used 的口径是"计划是不是模型给的"，
    // 它不足以区分正文是谁写的（模型规划 + 模板正文在它眼里同样是 true）。
    var planner = el("span", null, item.llm_used ? "计划由大模型给出" : "计划由规则模板给出");
    planner.title = "服务端原值：llm_used=" + String(!!item.llm_used) + "（口径是分析计划，不是报告正文）";
    meta.appendChild(planner);
    if (item.created_at) {
      // 列表里显示本地时间（ISO 原文留给 title）：
      // 时间戳本身是后端真实值，只是把口径写清楚，不换算成"多久以前"。
      var when = el("span", null, "创建 " + fmtDateTime(item.created_at));
      when.title = item.created_at;
      meta.appendChild(when);
    }
    if (item.error_code) {
      meta.appendChild(errorReasonNode(item.error_code));
    }
    // 编号是这条记录的唯一标识（排查、对接口时要用），给个中文标签比甩一串十六进制好读。
    meta.appendChild(el("span", "listrow__mono", "任务编号 " + (item.workflow_id || "")));
    main.appendChild(meta);

    row.appendChild(main);
    if (actionText) {
      // 操作文案随用途变：历史页点开的是详情，报告目录点开的是报告本身。
      row.appendChild(el("span", "listrow__action", actionText));
    }
    var chevron = icon("chevron");
    if (chevron) {
      row.appendChild(chevron);
    }
    row.addEventListener("click", onClick);
    return row;
  }

  async function fetchHistory(limit, offset) {
    return api(API.history + "?limit=" + limit + "&offset=" + offset);
  }

  // 历史页的真分页。为什么要做：原来固定只取 20 条，第 21 条起用户永远看不到，
  // 而归档接口本来就支持 limit/offset —— 那就把真实的分页能力给出来，
  // 页码与总数都来自服务端返回的 total，不在前端算"大概还有多少条"。
  var HISTORY_PAGE_SIZE = 20;
  var historyList = {
    total: 0,
    offset: 0, // 正在请求（或下一次要请求）的起点
    shownOffset: 0, // 服务端已经交回来的那一页的起点：页码只认它，见 renderHistoryPager
    loaded: false,
    loading: false,
    enabled: null, // 服务端有没有配归档库；false 时不摆分页控件
    ticket: 0, // 与数据集列表同样的理由：翻页点得快时旧响应可能后到
  };

  function historyPageCount() {
    if (!historyList.total) {
      return 1;
    }
    return Math.max(1, Math.ceil(historyList.total / HISTORY_PAGE_SIZE));
  }

  function renderHistoryPager() {
    var pager = $("history-pager");
    if (!pager) {
      return;
    }
    var info = $("history-page-info");
    var prev = $("history-prev");
    var next = $("history-next");
    var pages = historyPageCount();
    // 只有一页就不摆分页控件：摆了也点不动，等于装饰性的假控件。
    pager.hidden = historyList.enabled === false || !historyList.loaded || pages <= 1;

    if (info) {
      // 与数据集页同一条规矩：页码与总数只说"服务端已经交回来的那一页"。请求在路上时
      // 显示的是上一页的真实页码，外加一个"正在载入…"后缀，绝不提前报目标页号。
      info.textContent =
        "第 " + (Math.floor(historyList.shownOffset / HISTORY_PAGE_SIZE) + 1) + " / " + pages +
        " 页 · 共 " + fmtInt(historyList.total) + " 条" +
        (historyList.loading ? " · 正在载入…" : "");
    }
    if (prev) {
      prev.disabled = historyList.loading || historyList.shownOffset <= 0;
    }
    if (next) {
      next.disabled =
        historyList.loading || historyList.shownOffset + HISTORY_PAGE_SIZE >= historyList.total;
    }
  }

  function goHistoryPage(offset) {
    loadHistory({ offset: Math.max(0, offset) });
  }

  async function loadRecentTasks() {
    var box = $("recent-tasks");
    if (!box) {
      return;
    }

    var result = await fetchHistory(5, 0);
    if (!result.ok) {
      clear(box);
      box.appendChild(emptyBlock(errorOf(result).message, "warn"));
      return;
    }

    var data = result.data || {};
    var items = Array.isArray(data.items) ? data.items : [];
    clear(box);

    // 工作台计数行里的任务数：复用这次归档响应的 total，不另开一次请求。
    var taskTotal = $("asset-task-total");
    if (taskTotal) {
      taskTotal.textContent =
        data.enabled === false || data.total === undefined || data.total === null
          ? "—"
          : fmtInt(data.total);
    }
    var taskNote = $("asset-task-note");
    if (taskNote) {
      taskNote.textContent =
        data.enabled === false
          ? "这台服务没有开启任务归档"
          : "· 截至 " + fmtClock(new Date().toISOString()) + " · " + fmtInt(data.total || 0) + " 条";
    }

    // 「最近一次成功」只认终态为成功的那条归档记录。归档按下发时间倒序，
    // 所以"最近 5 条里的第一条成功记录"就是全局最近的成功；一条都没有时
    // 必须说清是"最近 5 条里没有"，不能拿别的时间戳顶上。
    var successBox = $("asset-last-success");
    var successNote = $("asset-last-note");
    if (successBox) {
      var okStates = ["SUCCESS", "SUCCESS_WITH_WARNINGS"];
      var done = null;
      for (var i = 0; i < items.length; i += 1) {
        if (okStates.indexOf(items[i].status) >= 0) {
          done = items[i];
          break;
        }
      }
      if (done) {
        successBox.textContent = fmtDateTime(done.finished_at || done.created_at);
        if (successNote) {
          successNote.textContent = "状态 " + historyStatusText(done.status) + " · 来自归档记录";
          successNote.title = "服务端原值：" + String(done.status);
        }
      } else {
        successBox.textContent = "—";
        if (successNote) {
          successNote.textContent = "最近 " + fmtInt(items.length) + " 条里没有成功记录";
        }
      }
    }

    if (data.enabled === false) {
      box.appendChild(emptyBlock(ARCHIVE_DISABLED_NOTE, "info"));
      return;
    }
    if (!items.length) {
      box.appendChild(emptyBlock(NO_RECORD_NOTE, "history"));
      return;
    }

    items.forEach(function (item) {
      box.appendChild(
        historyRow(item, function () {
          go("history");
          openHistoryDetail(item.workflow_id);
        })
      );
    });
  }

  async function loadHistory(options) {
    var opts = options || {};
    var box = $("history-list");
    var metaBox = $("history-meta");
    if (!box) {
      return;
    }

    if (typeof opts.offset === "number" && opts.offset >= 0) {
      historyList.offset = opts.offset;
    }
    historyList.loading = true;
    historyList.ticket += 1;
    var ticket = historyList.ticket;
    var requested = historyList.offset; // 这次响应回来时它对应的是哪一页

    clear(box);
    box.appendChild(emptyBlock("正在读取归档库…", "history"));
    renderHistoryPager();

    var result = await fetchHistory(HISTORY_PAGE_SIZE, requested);
    if (ticket !== historyList.ticket) {
      return; // 又发起了更新的请求，这一次的响应作废，别拿旧数据盖新页
    }
    historyList.loading = false;

    if (!result.ok) {
      var error = errorOf(result);
      historyList.loaded = false;
      if (metaBox) {
        metaBox.textContent = "";
      }
      clear(box);
      box.appendChild(emptyBlock(error.message + "（" + error.code + "）", "warn"));
      var retry = el("button", "btn btn--ghost btn--sm", "重试");
      retry.type = "button";
      retry.addEventListener("click", function () {
        loadHistory();
      });
      box.appendChild(retry);
      renderHistoryPager();
      return;
    }

    var data = result.data || {};
    var items = Array.isArray(data.items) ? data.items : [];
    historyList.enabled = data.enabled === false ? false : true;
    historyList.total = typeof data.total === "number" ? data.total : items.length;
    historyList.loaded = true;
    historyList.shownOffset = requested; // 响应落地之后，页码才可以变
    clear(box);

    if (data.enabled === false) {
      if (metaBox) {
        metaBox.textContent = "";
      }
      box.appendChild(
        emptyBlock("这台服务没有开启任务归档，所以没有历史记录可读。", "info")
      );
      renderHistoryPager();
      return;
    }

    // 页码可能因为记录变化落到空页：退回上一页再问一次，别停在一个空列表上。
    if (!items.length && historyList.offset > 0) {
      historyList.offset = Math.max(0, historyList.offset - HISTORY_PAGE_SIZE);
      renderHistoryPager();
      return loadHistory();
    }

    if (metaBox) {
      // 口径是用户口径：总数 + 本页条数。真实请求参数（limit/offset）属于调试信息，
      // 在浏览器开发者工具与 /docs 里都能看到，不往用户页面上堆术语。数字都来自响应，不是估算。
      metaBox.textContent =
        "共 " + fmtInt(historyList.total) + " 条归档记录，本页显示 " + fmtInt(items.length) + " 条";
    }

    if (!items.length) {
      // 空态除了「还没有记录」，还说清下一步做什么（docs/ux-spec-final.md R7 的空态口径）。
      box.appendChild(emptyBlock("归档库里还没有记录。", "history"));
      box.appendChild(el("p", "note", "做完第一次分析后，它会出现在这里（去「发起分析」页提问即可）。"));
      renderHistoryPager();
      return;
    }

    items.forEach(function (item) {
      box.appendChild(historyRow(item, function () {
        openHistoryDetail(item.workflow_id);
      }));
    });
    state.historyLoaded = true;
    renderHistoryPager();
  }

  // ---- 报告目录（任务书 7.6「报告中心」的列表那一格）--------------------------------
  // 为什么复用归档接口而不是新建接口：报告本身就是归档记录的一种状态（has_report），
  // docs/ux-spec-final.md 的 R5 已定案数据源就是 GET /api/workflows/history。
  // 这里只做筛选与呈现：has_report=false 的记录不进目录（它们没有报告可读），
  // 但"归档里有记录却一条报告都没有"这种情况必须如实说出来，不能显示成空库。
  var reportCatalog = { loading: false, loaded: false, enabled: null, total: 0, withReport: 0 };

  async function loadReportCatalog() {
    var box = $("report-catalog");
    var stateBox = $("report-catalog-state");
    var button = $("report-catalog-refresh");
    if (!box || !stateBox) {
      return;
    }

    reportCatalog.loading = true;
    if (button) {
      setBusy(button, true);
    }
    box.setAttribute("aria-busy", "true");
    clear(box);
    stateBox.className = "state";
    stateBox.textContent = "正在读取报告目录…";
    box.appendChild(emptyBlock("正在读取归档记录…", "history"));

    var result = await api(API.history + "?limit=" + HISTORY_PAGE_SIZE + "&offset=0");
    reportCatalog.loading = false;
    if (button) {
      setBusy(button, false);
    }
    box.setAttribute("aria-busy", "false");
    clear(box);

    if (!result.ok) {
      var error = errorOf(result);
      reportCatalog.loaded = false;
      stateBox.className = "state error-text";
      stateBox.textContent = "报告目录读取失败：" + error.message + "（" + error.code + "）";
      var retry = el("button", "btn btn--ghost btn--sm", "重试");
      retry.type = "button";
      retry.addEventListener("click", function () {
        loadReportCatalog();
      });
      box.appendChild(retry);
      return;
    }

    var data = result.data || {};
    var items = Array.isArray(data.items) ? data.items : [];
    var shown = items.filter(function (item) {
      return item && item.has_report === true;
    });
    reportCatalog.enabled = data.enabled === false ? false : true;
    reportCatalog.total = typeof data.total === "number" ? data.total : items.length;
    reportCatalog.withReport = shown.length;
    reportCatalog.loaded = true;

    if (data.enabled === false) {
      stateBox.textContent = "这台服务没有开启任务归档，所以没有报告可列。";
      box.appendChild(emptyBlock("这台服务没有开启任务归档，所以没有报告可列。", "info"));
      return;
    }

    if (!shown.length) {
      reportCatalog.withReport = 0;
      if (reportCatalog.total === 0) {
        stateBox.textContent = "还没有任何分析记录，先做一次分析。";
        box.appendChild(emptyBlock("还没有任何分析记录，先做一次分析。", "history"));
        var goAsk = el("button", "btn btn--ghost btn--sm", "去发起分析");
        goAsk.type = "button";
        goAsk.addEventListener("click", function () {
          go("analysis");
        });
        box.appendChild(goAsk);
      } else {
        // 归档里有记录、但一条都没跑到"有报告"这一步 —— 这是真实情况，要说清楚
        stateBox.textContent =
          "归档里有 " + fmtInt(reportCatalog.total) + " 条记录，但都还没有报告" +
          "（报告是在任务跑到终态之后才写的）。";
        box.appendChild(
          emptyBlock("归档里 " + fmtInt(reportCatalog.total) + " 条记录都还没有报告。", "info")
        );
      }
      return;
    }

    stateBox.textContent =
      "共 " + fmtInt(reportCatalog.total) + " 条归档记录，其中最近 " + fmtInt(shown.length) +
      " 条有报告（只列这一页，完整列表在「历史记录」）。";
    shown.forEach(function (item) {
      var li = document.createElement("li");
      li.appendChild(historyRow(item, function () {
        // 阅读形态就是既有的深链接：换 hash 让路由自己走 applyDeepLink → loadReport
        window.location.hash = "#/report/" + item.workflow_id;
      }, "查看报告"));
      box.appendChild(li);
    });
  }

  async function openHistoryDetail(workflowId) {
    var card = $("history-detail-card");
    var box = $("history-detail");
    if (!card || !box) {
      return;
    }

    card.hidden = false;
    clear(box);
    box.appendChild(emptyBlock("正在读取归档详情…", "history"));

    var result = await api(API.historyDetail(workflowId));
    if (!result.ok) {
      clear(box);
      renderErrorBox(box, errorOf(result), null);
      return;
    }

    var data = result.data || {};
    var item = data.item || {};
    clear(box);

    var table = document.createElement("table");
    table.className = "kv";
    var body = document.createElement("tbody");
    // 口径：表头用中文标签而不是字段名；时间显示成本地时间，原始时间戳（UTC）放 title，
    // 既不丢真值，也不要求用户自己去换算。
    // 每行：[中文标签, 显示值(字符串或节点), 放 title 的服务端原值, 未识别时要显形的小字]
    var pairs = [
      ["任务编号", item.workflow_id],
      ["问题", item.question],
      ["状态", historyStatusNode(item.status), "服务端原值：" + String(item.status)],
      ["创建时间", item.created_at ? fmtDateTime(item.created_at) : "", rawTimeTitle(item.created_at)],
      ["结束时间", item.finished_at ? fmtDateTime(item.finished_at) : "", rawTimeTitle(item.finished_at)],
      ["有没有报告", item.has_report ? "是" : "否", "服务端原值：has_report=" + String(!!item.has_report)],
      // llm_used 的口径是"分析计划是不是模型给的"，不是"正文谁写的"（后者要读报告总述首段），
      // 所以这一行按前者写，不在这里承诺正文的来源。
      ["分析计划是谁给的", item.llm_used ? "大模型" : "规则模板", "服务端原值：llm_used=" + String(!!item.llm_used)],
      item.error_code
        ? [
            "失败原因",
            errorReasonNode(item.error_code),
            "服务端原值：" + item.error_code,
          ]
        : ["失败原因", "（无）"],
    ];
    pairs.forEach(function (pair) {
      var row = document.createElement("tr");
      row.appendChild(el("th", null, pair[0]));
      var value = pair[1];
      var cell;
      if (value && value.nodeType === 1) {
        cell = el("td", null, null);
        cell.appendChild(value);
      } else {
        cell = el("td", null, value === null || value === undefined || value === "" ? "—" : value);
      }
      if (pair[2]) {
        cell.title = pair[2];
      }
      row.appendChild(cell);
      body.appendChild(row);
    });
    table.appendChild(body);
    var wrap = el("div", "table-wrap");
    wrap.appendChild(table);
    box.appendChild(wrap);

    if (data.report) {
      var title = el("h3", "sub-title", "归档报告");
      box.appendChild(title);
      // 归档详情里**没有**工具明细可读（接口 10 只还原 FinalReport），所以第二个参数不给：
      // 证据区据此走"已归档"那条诚实分支。
      renderReportInto(box, data.report);
    } else if (item.has_report) {
      box.appendChild(el("p", "note", ARCHIVE_REPORT_UNPARSEABLE_NOTE));
    } else {
      box.appendChild(el("p", "note", "这次分析没有生成报告。"));
    }
  }

  /* ============================================================ 设置 */

  // 修改密码：POST /api/auth/password。
  // 为什么一定要把"当前密码"传给服务端（而不是前端自己比一下）：
  // 会话 cookie 被偷走时，光有 cookie 就能改密码等于把一次泄露升级成永久劫持。
  // 后端的规则是：改成功后**销毁该账号的全部会话**，再给当前这台设备重新签一条 ——
  // 所以文案要说清"别的设备都要重新登录、这台还留着"，不能含糊成"已退出"。
  async function changePassword() {
    var status = $("password-state");
    var button = $("password-save");
    var current = $("pwd-current");
    var fresh = $("pwd-new");
    var again = $("pwd-new2");

    function say(kind, text) {
      if (status) {
        status.className = "state " + kind;
        status.textContent = text;
      }
    }

    if (!current || !fresh) {
      say("error-text", "这个页面缺少输入密码的表单（设置页的「修改密码」卡片没渲染出来）。");
      return;
    }
    var currentValue = current.value || "";
    var freshValue = fresh.value || "";
    if (!currentValue || !freshValue) {
      say("error-text", "当前密码和新密码都要填。");
      return;
    }
    if (again && again.value !== freshValue) {
      say("error-text", "两次输入的新密码不一致。");
      return;
    }
    // 强度规则不在前端复述一遍（"至少 8 位、必须含字母和数字"那句话由服务端给），
    // 否则后端改了规则，页面就会拒绝一个其实合法的密码。
    if (button) {
      setBusy(button, true);
    }
    say("", "正在提交…");

    var result = await api(API.password, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: currentValue, new_password: freshValue }),
      // 这一条的 401 是"当前密码不正确"这个业务答案，不是会话没了 —— 不能让 api() 跳登录页
      expectUnauthorized: true,
    });

    if (button) {
      setBusy(button, false);
    }
    if (!result.ok) {
      var error = errorOf(result);
      say("error-text", error.message || "修改失败");
      if (status) {
        status.appendChild(document.createTextNode(" "));
        status.appendChild(errorCodeNode(error.code, "origin"));
        if (error.code === "UNAUTHORIZED") {
          // 也可能是这台设备的会话真的过期了：给一条明路，而不是让人卡在设置页。
          var link = el("a", "btn btn--ghost btn--sm", "重新登录");
          link.setAttribute("href", LOGIN_PATH);
          status.appendChild(link);
        }
      }
      return;
    }

    var data = result.data || {};
    if (data.user) {
      session.user = data.user;
    }
    if (data.session_expires_at) {
      session.expiresAt = data.session_expires_at;
    }
    current.value = "";
    fresh.value = "";
    if (again) {
      again.value = "";
    }
    renderUser();
    say("ok-text", "密码已改。这台设备还保持着登录（服务端刚重新签了一条会话），其他设备与浏览器的登录状态已全部退出，需要用新密码重新登录。");
    loadSessions();
  }

  // 会话列表：GET /api/auth/sessions。服务端只给元信息（创建/最近活动/到期时间 + 是否当前这条），
  // **不给 token**（库里存的也只是哈希），所以这里没有任何可被复制走的凭据。
  async function loadSessions() {
    var box = $("session-list");
    var status = $("session-state");
    var button = $("session-refresh");
    if (!box) {
      return;
    }

    if (button) {
      setBusy(button, true);
    }
    box.setAttribute("aria-busy", "true");
    clear(box);
    if (status) {
      status.className = "state";
      status.textContent = "正在读取会话列表…";
    }

    var result = await api(API.sessions);
    box.setAttribute("aria-busy", "false");
    if (button) {
      setBusy(button, false);
    }
    clear(box);

    if (!result.ok) {
      var error = errorOf(result);
      if (status) {
        status.className = "state error-text";
        status.textContent = "会话列表读取失败：" + error.message;
        status.appendChild(errorCodeNode(error.code, "origin"));
      }
      box.appendChild(
        postureAction("重试", function () {
          loadSessions();
        }, "重新向服务端问一次这个账号的会话列表")
      );
      return;
    }

    var rows = Array.isArray(result.data) ? result.data : [];
    if (status) {
      status.className = "state";
      status.textContent =
        "这个账号一共有 " + fmtInt(rows.length) + " 条登录会话" +
        "（会话到期后就再也用不了；下面是元信息，服务端从来不把登录凭据发给页面）。";
    }
    if (!rows.length) {
      box.appendChild(emptyBlock("没有读到有效的登录会话。", "info"));
      return;
    }

    var table = document.createElement("table");
    table.className = "kv";
    var body = document.createElement("tbody");
    rows.forEach(function (row, index) {
      var line = document.createElement("tr");
      var label = row && row.current ? "这台设备（当前会话）" : "第 " + fmtInt(index + 1) + " 条会话";
      line.appendChild(el("th", null, label));
      var cell = el("td", null, null);
      cell.appendChild(el("span", null, "登录于 " + fmtDateTime(row.created_at)));
      cell.appendChild(el("span", null, " · 最近活动 " + fmtDateTime(row.last_seen_at)));
      cell.appendChild(el("span", null, " · 到期 " + fmtDateTime(row.expires_at)));
      line.appendChild(cell);
      body.appendChild(line);
    });
    table.appendChild(body);
    var wrap = el("div", "table-wrap");
    wrap.appendChild(table);
    box.appendChild(wrap);
  }

  async function loadSettings() {
    var table = $("settings-health");
    if (!table) {
      return;
    }
    loadSessions();
    var body = table.querySelector("tbody") || document.createElement("tbody");
    clear(body);

    var result = await api(API.health);
    if (!result.ok) {
      var bad = document.createElement("tr");
      bad.appendChild(el("th", null, "读取失败"));
      bad.appendChild(el("td", null, errorOf(result).message));
      body.appendChild(bad);
      table.appendChild(body);
      return;
    }

    var data = result.data || {};
    // 口径：表头说人话、值也翻译成人话；服务端返回的原始值（healthy / true / false）放 title，
    // 谁想核对原值都能核对，但页面正文不再出现英文状态码。
    var rows = [
      ["服务状态", data.status === "healthy" ? "正常" : data.status, data.status],
      ["服务名", data.app_name],
      ["版本", data.version],
      ["已运行时长", formatSeconds(Number(data.uptime_seconds))],
      ["大模型", data.llm_configured === true ? "已配置" : "未配置（将使用规则模板）"],
      ["文件上传", data.upload_dir_ready === true ? "可用" : "不可用"],
    ];
    rows.forEach(function (pair) {
      var row = document.createElement("tr");
      row.appendChild(el("th", null, pair[0]));
      var cell = el("td", null, pair[1] === null || pair[1] === undefined || pair[1] === "" ? "—" : pair[1]);
      if (pair[2] !== undefined && pair[2] !== null && pair[2] !== "") {
        cell.title = "服务端原始值：" + pair[2];
      }
      row.appendChild(cell);
      body.appendChild(row);
    });
    table.appendChild(body);
  }

  /* ============================================================ 初始化 */

  async function init() {
    // 会话门放在最前面，而且 **await**：未登录时直接跳走 ——
    // 不让页面先闪一下工作台，也不让任何业务接口带着空 cookie 去打一轮 401。
    var allowed = await checkSession();
    if (!allowed) {
      return;
    }

    resetWorkflowPanel();
    clearCharts();
    clearReport();
    updateAnalysisContext();

    var toggle = $("nav-toggle");
    if (toggle) {
      toggle.addEventListener("click", toggleDrawer);
    }

    /* ---- 顶栏用户菜单与退出登录 ---- */

    var userButton = $("user-btn");
    if (userButton) {
      userButton.addEventListener("click", function (event) {
        // 必须阻止冒泡：下面那个 document 级的「点空白处收起」会立刻把它关掉，
        // 表现成"点了没反应"。
        event.stopPropagation();
        toggleUserMenu();
      });
    }

    var logoutButton = $("logout");
    if (logoutButton) {
      logoutButton.addEventListener("click", function () {
        logout("logout", "logout-state");
      });
    }

    // 设置页「当前账号」里的退出按钮：和上面走**同一个** logout()。
    var settingsLogout = $("logout-settings");
    if (settingsLogout) {
      settingsLogout.addEventListener("click", function () {
        logout("logout-settings", "logout-settings-state");
      });
    }

    var passwordSave = $("password-save");
    if (passwordSave) {
      passwordSave.addEventListener("click", changePassword);
    }

    var sessionRefresh = $("session-refresh");
    if (sessionRefresh) {
      sessionRefresh.addEventListener("click", loadSessions);
    }

    // 态势带的刷新：真的重新问一次归档（force 跳过 5 分钟缓存与 30 秒负缓存）。
    var postureRefresh = $("posture-refresh");
    if (postureRefresh) {
      postureRefresh.addEventListener("click", function () {
        loadPosture(true);
      });
    }

    // 浮层是绝对定位的，点别处 / 按 Esc 都得收起，否则它会一直盖着下面的内容。
    document.addEventListener("click", function (event) {
      var popover = $("user-popover");
      var button = $("user-btn");
      if (!popover || popover.hidden) {
        return;
      }
      if (popover.contains(event.target) || (button && button.contains(event.target))) {
        return;
      }
      toggleUserMenu(false);
    });
    document.addEventListener("keydown", function (event) {
      var sidebar = $("sidebar");
      var drawerOpen = !!(sidebar && sidebar.classList.contains("is-open"));

      // 抽屉打开时 Tab 在抽屉内部循环（浮层的标准键盘行为）
      if (event.key === "Tab" && drawerOpen) {
        var items = drawerFocusables(sidebar);
        if (items.length) {
          var first = items[0];
          var last = items[items.length - 1];
          var active = document.activeElement;
          if (!sidebar.contains(active)) {
            event.preventDefault();
            first.focus();
          } else if (event.shiftKey && active === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && active === last) {
            event.preventDefault();
            first.focus();
          }
        }
        return;
      }

      if (event.key === "Escape") {
        toggleUserMenu(false);
        // 和顶栏用户菜单同一套约定：Escape 关闭浮层，并把焦点还给触发它的那个按钮。
        // （移动端抽屉以前按 Escape 不关，两处行为不一致。）
        if (drawerOpen) {
          closeDrawer();
          var toggle = $("nav-toggle");
          if (toggle) {
            toggle.focus();
          }
        }
      }
    });

    var uploadButton = $("upload");
    if (uploadButton) {
      uploadButton.addEventListener("click", handleUpload);
    }

    var fileInput = $("file");
    if (fileInput) {
      fileInput.addEventListener("change", function () {
        state.droppedFile = null;
        var status = $("upload-state");
        if (status && fileInput.files && fileInput.files.length) {
          status.className = "state";
          status.textContent = "已选择：" + fileInput.files[0].name + "，点「上传」开始";
        }
      });
    }

    // 拖拽上传：后端支持 multipart 上传，所以这里把拖进来的文件存进 state，
    // 上传时优先用 <input> 里的文件（脚本不能改写 input.files，只能自己记一份）。
    var dropzone = $("dropzone");
    if (dropzone) {
      ["dragenter", "dragover"].forEach(function (name) {
        dropzone.addEventListener(name, function (event) {
          event.preventDefault();
          dropzone.classList.add("is-dragover");
        });
      });
      ["dragleave", "drop"].forEach(function (name) {
        dropzone.addEventListener(name, function (event) {
          event.preventDefault();
          dropzone.classList.remove("is-dragover");
        });
      });
      dropzone.addEventListener("drop", function (event) {
        var files = event.dataTransfer && event.dataTransfer.files;
        if (files && files.length) {
          state.droppedFile = files[0];
          var status = $("upload-state");
          if (status) {
            status.className = "state";
            status.textContent = "已拖入：" + files[0].name + "，点「上传」开始";
          }
        }
      });
    }

    var question = $("question");
    if (question) {
      question.addEventListener("input", updateAskButton);
    }

    var askButton = $("ask");
    if (askButton) {
      askButton.addEventListener("click", ask);
    }

    var examples = document.querySelectorAll("button.example[data-question]");
    for (var i = 0; i < examples.length; i += 1) {
      examples[i].addEventListener("click", function (event) {
        var text = event.currentTarget.getAttribute("data-question") || "";
        if (question) {
          question.value = text;
          question.focus();
        }
        updateAskButton();
      });
    }

    var historyRefresh = $("history-refresh");
    if (historyRefresh) {
      historyRefresh.addEventListener("click", function () {
        loadHistory();
      });
    }

    // 报告目录的刷新：和上面那排"刷新"一样，重新问一次真实接口，不动本地缓存
    var catalogRefresh = $("report-catalog-refresh");
    if (catalogRefresh) {
      catalogRefresh.addEventListener("click", function () {
        loadReportCatalog();
      });
    }

    // 历史页真分页：按服务端 total 与固定 limit 算上一页/下一页的 offset
    var historyPrev = $("history-prev");
    if (historyPrev) {
      historyPrev.addEventListener("click", function () {
        goHistoryPage(historyList.offset - HISTORY_PAGE_SIZE);
      });
    }
    var historyNext = $("history-next");
    if (historyNext) {
      historyNext.addEventListener("click", function () {
        goHistoryPage(historyList.offset + HISTORY_PAGE_SIZE);
      });
    }

    var historyClose = $("history-close");
    if (historyClose) {
      historyClose.addEventListener("click", function () {
        var card = $("history-detail-card");
        if (card) {
          card.hidden = true;
        }
      });
    }

    /* ---- 数据集列表：排序表头、翻页、刷新、选择 ---- */

    var sortButtons = document.querySelectorAll("#dataset-table th[data-sort] button[data-sort]");
    for (var k = 0; k < sortButtons.length; k += 1) {
      sortButtons[k].addEventListener("click", function (event) {
        sortBy(event.currentTarget.getAttribute("data-sort"));
      });
    }

    var datasetRefresh = $("dataset-refresh");
    if (datasetRefresh) {
      datasetRefresh.addEventListener("click", function () {
        // 用户点刷新 = 明确要最新数据：force 绕开短 TTL 缓存（刷新不是"清空条件"）。
        loadDatasets({ force: true });
        loadAssets(true);
      });
    }

    var datasetSearchField = $("dataset-search");
    if (datasetSearchField) {
      datasetSearchField.addEventListener("input", onDatasetSearchInput);
      datasetSearchField.addEventListener("keydown", onDatasetSearchKeydown);
      // 原生 search 输入框自带的那个 ✕（Chromium）不总在，自己给一个稳定的清空按钮
      datasetSearchField.addEventListener("search", onDatasetSearchInput);
    }

    var datasetSearchClear = $("dataset-search-clear");
    if (datasetSearchClear) {
      datasetSearchClear.addEventListener("click", function () {
        clearDatasetSearch();
      });
    }

    var pagePrev = $("page-prev");
    if (pagePrev) {
      pagePrev.addEventListener("click", function () {
        loadDatasets({ offset: Math.max(0, datasetList.offset - PAGE_SIZE) });
      });
    }

    var pageNext = $("page-next");
    if (pageNext) {
      pageNext.addEventListener("click", function () {
        loadDatasets({ offset: datasetList.offset + PAGE_SIZE });
      });
    }

    var datasetSelect = $("dataset-select");
    if (datasetSelect) {
      datasetSelect.addEventListener("change", function () {
        if (datasetSelect.value) {
          useDataset(datasetSelect.value, {});
        }
      });
    }

    // 证据区跳转（契约 §7.2 规则 1）：点正文 sup.cite 或键盘 Enter/Space →
    // 滚到同一份文档里编号相同的 .src-line。用事件委托：报告与历史详情抽屉都复用
    // renderReportInto，挂一次就都覆盖；只认被标了 tabindex="0" 的 sup（报告正文才有）。
    document.addEventListener("click", function (event) {
      var sup = event.target && event.target.closest
        ? event.target.closest("sup.cite[data-task-id]")
        : null;
      if (sup && sup.getAttribute("tabindex") === "0") {
        jumpToEvidence(sup);
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") {
        return;
      }
      var sup = event.target && event.target.closest
        ? event.target.closest("sup.cite[data-task-id]")
        : null;
      if (sup && sup.getAttribute("tabindex") === "0") {
        event.preventDefault();
        jumpToEvidence(sup);
      }
    });

    var navLinks = document.querySelectorAll(".nav__item[data-view]");
    for (var j = 0; j < navLinks.length; j += 1) {
      navLinks[j].addEventListener("click", function () {
        closeDrawer();
      });
    }

    window.addEventListener("hashchange", function () {
      applyRoute();
    });

    applyRoute();
    loadHealth();
    // 数据集那份计数是**全局**的：侧栏徽标、首屏指标格、命令条那句"数据资产更新于"都读它，
    // 所以不跟着视图走（落在哪个视图都得有值）。最近任务那张面板只在 workbench 视图里，
    // 交给 applyView 触发 —— 首屏不替一个看不见的容器打归档接口。
    loadAssets();
  }

  function run() {
    // init 现在是 async（第一件事要 await 会话门）。里面的 await 一旦抛异常，
    // 必须有人接住 —— 否则浏览器只在控制台留一条 unhandled rejection，
    // 页面上什么都不显示，看起来就是"卡在加载中"。
    init().catch(function (cause) {
      showStartupError({
        code: "INIT_FAILED",
        message: "页面初始化失败：" + String((cause && cause.message) || cause),
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();

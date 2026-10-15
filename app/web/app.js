/* ============================================================================
 * AI 市场行情智能分析平台 —— 前端逻辑
 *
 * 三条必须守住的规矩（改代码前先读这三条）：
 *
 * 1. **绝不按 HTTP 状态码判断成功失败。** 工作流超时的 HTTP 状态码仍然是 200，
 *    失败信息在响应体里的 body.error.code。所以每个请求都先解析响应体，
 *    再看 body.ok / body.error，不用 fetch 响应对象自带的那个布尔值。
 *
 * 2. **不维护「错误码 → 中文」对照表。** 文案一律用后端给的 message，
 *    前端只把 code 原样显示出来。后端改了话术，页面跟着变，不用改这里。
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
    upload: "/api/datasets/upload",
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

  // 每个视图的面包屑与标题（#crumb / #page-title / #page-sub）。
  var VIEWS = {
    workbench: {
      crumb: "工作台",
      title: "Market Intelligence Workspace",
      sub: "上传市场数据 → 用自然语言提问 → 真实计算 + 图表 + 可逐条核对的报告",
    },
    dataset: {
      crumb: "数据集",
      title: "数据集概览",
      sub: "字段结构、数据预览与质量信息，全部来自服务端真实字段",
    },
    analysis: {
      crumb: "市场分析",
      title: "自然语言分析",
      sub: "把问题交给后端的多步编排：规划任务 → pandas 计算 → 出图 → 写报告 → 校验数字",
    },
    report: {
      crumb: "分析报告",
      title: "图表与分析报告",
      sub: "图表由服务端 matplotlib 生成；报告正文里每个数字都能回到某次工具调用的结果",
    },
    history: {
      crumb: "历史任务",
      title: "历史任务",
      sub: "读服务端 SQLite 归档库，重启服务后这些记录仍在",
    },
    settings: {
      crumb: "设置",
      title: "运行状态（只读）",
      sub: "后端没有任何写配置的接口，所以这一页只显示状态，不摆放点了没反应的开关",
    },
  };

  var DEFAULT_VIEW = "workbench";

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
    "help", "spark", "menu", "download", "external",
  ];

  function icon(name) {
    if (ICON_NAMES.indexOf(name) < 0) {
      return null;
    }
    var holder = document.createElement("span");
    holder.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-' + name + '"></use></svg>';
    return holder.firstChild;
  }

  // 空状态块（虚线框 + 图标 + 一句话）。
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

  // 就地错误框：显示后端给的 message + code + detail。
  function renderErrorBox(box, error, onRetry) {
    if (!box) {
      return;
    }
    clear(box);
    box.hidden = false;

    box.appendChild(el("strong", null, error.message || "请求失败"));
    box.appendChild(el("span", "state", " [" + (error.code || "UNKNOWN") + "]"));

    if (error.code === "WORKFLOW_TIMEOUT") {
      // 这个坑值得在界面上说清楚：超时了，但 HTTP 状态码还是 200。
      box.appendChild(el("p", "detail", "工作流超过了时间上限，已按超时结束（注意：这个错误的 HTTP 状态码仍然是 200）。"));
    }
    if (error.detail) {
      box.appendChild(el("p", "detail", String(error.detail)));
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

  /* ====================================================== 视图与路由 */

  function viewFromHash() {
    var raw = String(window.location.hash || "").replace(/^#\/?/, "");
    var name = raw.split("?")[0].split("/")[0];
    return Object.prototype.hasOwnProperty.call(VIEWS, name) ? name : DEFAULT_VIEW;
  }

  function go(name) {
    var target = Object.prototype.hasOwnProperty.call(VIEWS, name) ? name : DEFAULT_VIEW;
    if (viewFromHash() === target) {
      applyView(target);
    } else {
      window.location.hash = "#/" + target;
    }
  }

  function applyView(name) {
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
    var title = $("page-title");
    var sub = $("page-sub");
    if (crumb) {
      crumb.textContent = meta.crumb;
    }
    if (title) {
      title.textContent = meta.title;
    }
    if (sub) {
      sub.textContent = meta.sub;
    }

    closeDrawer();
    window.scrollTo(0, 0);

    if (name === "history") {
      loadHistory();
    } else if (name === "settings") {
      loadSettings();
    } else if (name === "workbench") {
      loadRecentTasks();
    }
  }

  function openDrawer() {
    var sidebar = $("sidebar");
    var toggle = $("nav-toggle");
    if (sidebar) {
      sidebar.classList.add("is-open");
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
  async function loadHealth() {
    var badge = $("llm-badge");
    var result = await api(API.health);
    if (!result.ok) {
      if (badge) {
        badge.className = "badge badge--grey";
        badge.textContent = "大模型状态未知";
      }
      return null;
    }
    var data = result.data || {};
    if (badge) {
      if (data.llm_configured === true) {
        badge.className = "badge badge--ok";
        badge.textContent = "已配置大模型";
        badge.title = "服务端环境变量里配了 LLM_API_KEY，报告正文会由大模型撰写";
      } else {
        badge.className = "badge badge--grey";
        badge.textContent = "未配置大模型，将使用规则模式";
        badge.title = "没有配 LLM_API_KEY：分析照常做，报告正文改用规则模板撰写";
      }
    }
    return data;
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
        " · dataset_id " + dataset.dataset_id;
    }

    resetWorkflowPanel();
    clearCharts();
    clearReport();

    await Promise.all([loadDataset(), loadProfile(), loadHealth()]);
    updateAnalysisContext();
  }

  /* ==================================================== 数据集与画像 */

  async function loadDataset() {
    if (!state.datasetId) {
      return null;
    }
    var summary = $("dataset-summary");
    var result = await api(API.dataset(state.datasetId));
    if (!result.ok) {
      if (summary) {
        summary.className = "state error-text";
        summary.textContent = errorOf(result).message;
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
    names.forEach(function (name) {
      headRow.appendChild(el("th", null, name));
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
        names.forEach(function (name) {
          var value = row ? row[name] : null;
          var text = value === null || value === undefined || value === "" ? "—" : value;
          line.appendChild(el("td", isNumericLike(text) ? "num" : null, text));
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
      var dtype = data.dtypes ? data.dtypes[name] : null;
      list.appendChild(el("li", null, dtype ? name + "（" + dtype + "）" : name));
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
          missing.appendChild(
            el(
              "li",
              null,
              column.name + "：" + fmtInt(column.null_count) + " 个缺失（" + fmtPercent(column.null_ratio) + "）"
            )
          );
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

  function kpiCard(label, value, unit, note, warn) {
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
    if (note) {
      card.appendChild(el("div", "kpi__note", note));
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

    grid.appendChild(kpiCard("数据行数", fmtInt(rowCount), "行", "来自画像接口的 row_count"));
    grid.appendChild(kpiCard("字段数", fmtInt(columnCount), "列", "来自画像接口的 column_count"));

    if (dataset && dataset.size_bytes !== undefined) {
      grid.appendChild(kpiCard("文件大小", fmtBytes(dataset.size_bytes), "", "上传时服务端记录的原始字节数"));
    }

    if (profile) {
      var duplicates = Number(profile.duplicate_row_count);
      grid.appendChild(
        kpiCard(
          "完全重复的行",
          fmtInt(profile.duplicate_row_count),
          "行",
          duplicates > 0 ? "整行取值完全一致；分析仍用原始数据" : "画像未发现整行完全重复的记录",
          duplicates > 0
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
          withNulls.length > 0
        )
      );

      grid.appendChild(kpiCard("画像内存占用", fmtBytes(profile.memory_bytes), "", "来自画像接口的 memory_bytes"));
    }
  }

  /* ======================================================== 提问与分析 */

  function updateAnalysisContext() {
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
          "本次分析使用：" + name + " · " + rows + " 行 × " + cols + " 列 · dataset_id " + state.datasetId
        )
      );
    } else {
      var strong = el("strong", null, "工作台");
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
      body: JSON.stringify({ dataset_id: state.datasetId, question: text }),
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
      status.textContent = "已提交，workflow_id " + state.workflowId;
    }

    resetWorkflowPanel();
    clearCharts();
    clearReport();
    startPolling();
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
        item.appendChild(el("span", "dot"));
        item.appendChild(el("span", null, STEP_LABELS[step] || step));
        item.appendChild(el("span", "steps__state", STEP_STATE_TEXT.waiting));
        list.appendChild(item);
      });
    }

    var elapsed = $("elapsed");
    if (elapsed) {
      elapsed.textContent = "—";
    }
    var workflowState = $("workflow-state");
    if (workflowState) {
      workflowState.textContent = "";
    }
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

      if (i < current) {
        item.classList.add("done");
        text = STEP_STATE_TEXT.done;
      } else if (i === current) {
        if (status === "FAILED") {
          item.classList.add("failed");
          text = STEP_STATE_TEXT.failed;
        } else if (terminal) {
          item.classList.add("done");
          text = STEP_STATE_TEXT.done;
        } else {
          item.classList.add("active");
          text = STEP_STATE_TEXT.active;
        }
      } else {
        // 走到这里说明这一步还没轮到。工作流已经结束时它不会被再执行，
        // 但后端并没有「跳过」这个语义，所以写「未执行」而不是「已跳过」。
        text = terminal ? STEP_STATE_TEXT.skipped : STEP_STATE_TEXT.waiting;
      }

      if (stateCell) {
        stateCell.textContent = text;
      }
    }
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
      item.appendChild(el("span", "state", TASK_STATUS_TEXT[task.status] || task.status || ""));

      if (task.error) {
        var message = task.error.message || task.error.code || "失败";
        item.appendChild(el("span", "tmsg", "（" + message + "）"));
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
    var seconds = ((end === null ? Date.now() : end) - start) / 1000;
    return "已用时 " + formatSeconds(seconds);
  }

  function stopPolling() {
    if (state.pollTimer !== null) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    updateAskButton();
  }

  function startPolling() {
    stopPolling();
    pollOnce();
    state.pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
    updateAskButton();
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
    paintSteps(data.step, data.status);
    renderTasks(data.tasks);

    var workflowState = $("workflow-state");
    if (workflowState) {
      workflowState.textContent =
        "状态 " + (data.status || "?") + " · 步骤 " + (data.step || "?") +
        "（" + fmtInt(data.step_index) + "/" + fmtInt(data.total_steps) + "）";
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
    renderCharts(Array.isArray(data.charts) ? data.charts : []);

    if (data.has_report === true) {
      await loadReport();
      if (state.view === "analysis") {
        go("report");
      }
    } else if (data.status === "FAILED") {
      clearReport("本次分析失败，没有生成报告。失败原因见上方错误框。");
    } else {
      clearReport("本次分析没有生成报告（服务端 has_report=false）。");
    }

    loadRecentTasks();
  }

  /* ============================================================ 图表 */

  function clearCharts() {
    var box = $("charts");
    if (!box) {
      return;
    }
    clear(box);
    box.appendChild(emptyBlock("分析完成后显示", "analysis"));
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
      meta.appendChild(el("span", null, CHART_TYPE_TEXT[chart.chart_type] || chart.chart_type || "未知图型"));
      // point_count 是**图上画了几个点**，不是「已核对的行数」。两者不比较、不做差。
      if (chart.point_count !== undefined && chart.point_count !== null) {
        meta.appendChild(el("span", null, "本图 " + fmtInt(chart.point_count) + " 个数据点"));
      }
      if (chart.x_field) {
        meta.appendChild(el("span", null, "横轴 " + chart.x_field));
      }
      if (chart.y_field) {
        meta.appendChild(el("span", null, "纵轴 " + chart.y_field));
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

  async function loadReport() {
    if (!state.workflowId) {
      return;
    }
    var box = $("report");
    if (box) {
      clear(box);
      box.appendChild(emptyBlock("正在读取报告…", "report"));
    }

    // 报告接口只在这一处被调用（进终态且 has_report 为真时取一次）。
    var result = await api(API.report(state.workflowId));
    if (!result.ok) {
      if (box) {
        clear(box);
        renderErrorBox(box, errorOf(result), null);
      }
      return;
    }
    renderReport(result.data || {});
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
          warn.appendChild(el("p", null, issue.code + "：" + issue.message));
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
        ok.appendChild(el("p", null, issue.code + "：" + issue.message));
      });
    container.appendChild(ok);
  }

  function renderTable(rows) {
    var wrap = el("div", "table-wrap");
    var table = document.createElement("table");
    var keys = Object.keys(rows[0]);

    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    keys.forEach(function (key) {
      headRow.appendChild(el("th", null, key));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    rows.forEach(function (row) {
      var line = document.createElement("tr");
      keys.forEach(function (key) {
        var value = row ? row[key] : null;
        var text = value === null || value === undefined || value === "" ? "—" : value;
        line.appendChild(el("td", isNumericLike(text) ? "num" : null, text));
      });
      tbody.appendChild(line);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  // 把一份 FinalReport 渲染进容器。主报告区与历史详情共用。
  function renderReportInto(container, report) {
    container.appendChild(el("h2", "report-title", report.title || "（无标题报告）"));

    var meta = el("div", "meta");
    if (report.report_id) {
      meta.appendChild(el("span", null, "报告编号 " + report.report_id));
    }
    meta.appendChild(el("span", null, report.llm_used ? "大模型撰写" : "规则模板撰写（未调用大模型）"));
    if (report.created_at) {
      meta.appendChild(el("span", null, "生成时间 " + report.created_at));
    }
    container.appendChild(meta);

    if (report.summary) {
      container.appendChild(el("p", "summary", report.summary));
    }

    var sections = Array.isArray(report.sections) ? report.sections : [];
    sections.forEach(function (section) {
      var block = el("section", "block");
      block.appendChild(el("h3", null, section.heading || "（无小标题）"));
      if (section.text) {
        block.appendChild(el("p", null, section.text));
      }
      if (Array.isArray(section.table) && section.table.length) {
        block.appendChild(renderTable(section.table));
      }
      var cites = Array.isArray(section.cites) ? section.cites : [];
      if (cites.length) {
        block.appendChild(el("p", "cites", "依据：" + cites.join("、")));
      }
      container.appendChild(block);
    });

    var notes = Array.isArray(report.data_quality_notes) ? report.data_quality_notes : [];
    if (notes.length) {
      var quality = el("section", "block");
      quality.appendChild(el("h3", null, "数据质量提示"));
      var list = document.createElement("ul");
      notes.forEach(function (note) {
        list.appendChild(el("li", null, note));
      });
      quality.appendChild(list);
      container.appendChild(quality);
    }
  }

  function renderReport(data) {
    var box = $("report");
    if (!box) {
      return;
    }
    clear(box);

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

    renderReportInto(box, data.report);

    var meta = el("div", "meta");
    if (Array.isArray(data.results)) {
      meta.appendChild(el("span", null, fmtInt(data.results.length) + " 个工具结果"));
    }
    if (data.llm_used !== undefined && data.report.llm_used !== data.llm_used) {
      meta.appendChild(el("span", null, "llm_used=" + String(data.llm_used)));
    }
    if (meta.childNodes.length) {
      box.appendChild(meta);
    }
  }

  /* ======================================================== 历史任务 */

  function historyStatusText(status) {
    // 跨版本的坏值会被后端原样透出（契约里是 WorkflowStatus | str），
    // 所以这里必须容忍任意字符串：认识的翻中文，不认识的照原样显示。
    var known = {
      PENDING: "等待中",
      PROFILING: "读取数据",
      PLANNING: "制定计划",
      EXECUTING: "计算中",
      CHARTING: "出图中",
      REPORTING: "写报告中",
      VALIDATING: "校验中",
      SUCCESS: "成功",
      SUCCESS_WITH_WARNINGS: "成功（有告警）",
      FAILED: "失败",
    };
    return known[status] || String(status);
  }

  function historyRow(item, onClick) {
    var row = el("button", "listrow");
    row.type = "button";

    var main = el("div", "listrow__main");
    main.appendChild(el("div", "listrow__q", item.question || "（无问题文本）"));

    var meta = el("div", "listrow__meta");
    meta.appendChild(el("span", null, "状态 " + historyStatusText(item.status)));
    meta.appendChild(el("span", null, item.has_report ? "有报告" : "无报告"));
    meta.appendChild(el("span", null, item.llm_used ? "大模型撰写" : "规则模板"));
    if (item.created_at) {
      meta.appendChild(el("span", null, "创建 " + item.created_at));
    }
    if (item.error_code) {
      meta.appendChild(el("span", "error-text", "错误码 " + item.error_code));
    }
    meta.appendChild(el("span", "listrow__mono", item.workflow_id || ""));
    main.appendChild(meta);

    row.appendChild(main);
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

    if (data.enabled === false) {
      box.appendChild(
        emptyBlock("服务端没有配置归档库（DB_PATH 为空），所以没有历史记录可读。", "info")
      );
      return;
    }
    if (!items.length) {
      box.appendChild(emptyBlock("还没有任何分析记录。做完第一次分析后，这里会出现它。", "history"));
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

  async function loadHistory() {
    var box = $("history-list");
    var metaBox = $("history-meta");
    if (!box) {
      return;
    }

    clear(box);
    box.appendChild(emptyBlock("正在读取归档库…", "history"));

    var result = await fetchHistory(20, 0);
    if (!result.ok) {
      clear(box);
      box.appendChild(emptyBlock(errorOf(result).message, "warn"));
      return;
    }

    var data = result.data || {};
    var items = Array.isArray(data.items) ? data.items : [];
    clear(box);

    if (data.enabled === false) {
      if (metaBox) {
        metaBox.textContent = "";
      }
      box.appendChild(
        emptyBlock("服务端没有配置归档库（DB_PATH 为空），所以没有历史记录可读。", "info")
      );
      return;
    }

    if (metaBox) {
      metaBox.textContent =
        "共 " + fmtInt(data.total) + " 条归档记录，本页显示 " + fmtInt(items.length) + " 条" +
        "（limit=" + fmtInt(data.limit) + "，offset=" + fmtInt(data.offset) + "）";
    }

    if (!items.length) {
      box.appendChild(emptyBlock("归档库里还没有记录。", "history"));
      return;
    }

    items.forEach(function (item) {
      box.appendChild(historyRow(item, function () {
        openHistoryDetail(item.workflow_id);
      }));
    });
    state.historyLoaded = true;
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
    var pairs = [
      ["workflow_id", item.workflow_id],
      ["问题", item.question],
      ["状态", historyStatusText(item.status)],
      ["创建时间", item.created_at],
      ["结束时间", item.finished_at],
      ["有报告", item.has_report ? "是" : "否"],
      ["报告撰写方", item.llm_used ? "大模型" : "规则模板"],
      ["错误码", item.error_code || "（无）"],
    ];
    pairs.forEach(function (pair) {
      var row = document.createElement("tr");
      row.appendChild(el("th", null, pair[0]));
      row.appendChild(el("td", null, pair[1] === null || pair[1] === undefined || pair[1] === "" ? "—" : pair[1]));
      body.appendChild(row);
    });
    table.appendChild(body);
    var wrap = el("div", "table-wrap");
    wrap.appendChild(table);
    box.appendChild(wrap);

    if (data.report) {
      var title = el("h3", "sub-title", "归档报告");
      box.appendChild(title);
      renderReportInto(box, data.report);
    } else if (item.has_report) {
      box.appendChild(
        el("p", "note", "这条记录标记了「有报告」，但归档里的报告文本解析不出来，所以正文无法显示。")
      );
    } else {
      box.appendChild(el("p", "note", "这次分析没有生成报告。"));
    }
  }

  /* ============================================================ 设置 */

  async function loadSettings() {
    var table = $("settings-health");
    if (!table) {
      return;
    }
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
    var rows = [
      ["运行状态", data.status],
      ["应用名", data.app_name],
      ["版本", data.version],
      ["已运行", formatSeconds(Number(data.uptime_seconds))],
      ["已配置大模型", data.llm_configured === true ? "是" : "否（将使用规则模板）"],
      ["上传目录可用", data.upload_dir_ready === true ? "是" : "否"],
    ];
    rows.forEach(function (pair) {
      var row = document.createElement("tr");
      row.appendChild(el("th", null, pair[0]));
      row.appendChild(
        el("td", null, pair[1] === null || pair[1] === undefined || pair[1] === "" ? "—" : pair[1])
      );
      body.appendChild(row);
    });
    table.appendChild(body);
  }

  /* ============================================================ 初始化 */

  function init() {
    resetWorkflowPanel();
    clearCharts();
    clearReport();
    updateAnalysisContext();

    var toggle = $("nav-toggle");
    if (toggle) {
      toggle.addEventListener("click", toggleDrawer);
    }

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

    var historyClose = $("history-close");
    if (historyClose) {
      historyClose.addEventListener("click", function () {
        var card = $("history-detail-card");
        if (card) {
          card.hidden = true;
        }
      });
    }

    var navLinks = document.querySelectorAll(".nav__item[data-view]");
    for (var j = 0; j < navLinks.length; j += 1) {
      navLinks[j].addEventListener("click", function () {
        closeDrawer();
      });
    }

    window.addEventListener("hashchange", function () {
      applyView(viewFromHash());
    });

    applyView(viewFromHash());
    loadHealth();
    loadRecentTasks();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

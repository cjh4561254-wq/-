/* AI 数据分析助手 —— 前端逻辑（原生 JS，无框架、无 CDN）
 *
 * 三条贯穿全文件的原则
 * --------------------
 * 1. **绝不按 HTTP 状态码判断失败。** 工作流超时的 HTTP 状态码照样是 200
 *    （docs/ARCHITECTURE.md §7.0），失败信息在 body.error.code 里。
 *    所以每个请求都先解析 body，再看 body.ok / body.error ——
 *    绝不用 fetch 响应自带的 ok 字段（它只看 HTTP 状态码，正是我们要避开的那个判据）。
 * 2. **不维护「错误码 → 中文」对照表。** 后端每个码都带中文 message，
 *    前端再抄一份就是第二份事实来源（§9.2.1）。这里只在少数需要换行为的
 *    码上分支（能不能重试、要不要就地显示），文案一律用后端给的。
 * 3. **轮询只打状态接口**（接口 6，小），**不轮询报告接口**（接口 7，大）。
 *    报告只在进终态且 has_report === true 时请求一次。
 */

"use strict";

// ---------------------------------------------------------------- 常量

const API = {
  upload: "/api/datasets/upload",
  health: "/api/health",
  analysis: "/api/analysis",
  dataset: (id) => `/api/datasets/${id}`,
  profile: (id) => `/api/datasets/${id}/profile`,
  workflow: (id) => `/api/workflows/${id}`,
  report: (id) => `/api/workflows/${id}/report`,
  // 图表一律走后端接口 8（不是静态目录）：后端把 url 拼好了，这里只做兜底。
  chart: (workflowId, filename) => `/api/workflows/${workflowId}/charts/${filename}`,
};

// 终态三兄弟：SUCCESS_WITH_WARNINGS 也算跑完了（校验阶段发现数字对不上时会降级成它），
// 少写一个，这类工作流就会永远转圈 —— 所以三个都要在，且都要去取报告。
const TERMINAL = ["SUCCESS", "SUCCESS_WITH_WARNINGS", "FAILED"];

// 值得给「重试」按钮的错误码（服务侧暂时不可用，再点一次有意义）。
const RETRYABLE = ["SERVICE_UNAVAILABLE"];

// 后端 step → 中文。顺序与 STEP_ORDER 一致，就是页面上的 6 个圆点。
const STEP_LABELS = {
  profiling: "正在读取数据",
  planning: "正在制定分析计划",
  execution: "正在计算",
  charting: "正在生成图表",
  reporting: "正在撰写报告",
  validation: "正在校验数字",
};
const STEP_ORDER = ["profiling", "planning", "execution", "charting", "reporting", "validation"];

const POLL_INTERVAL_MS = 1000;

// ---------------------------------------------------------------- 状态

const state = {
  datasetId: null,
  workflowId: null,
  pollTimer: null,
};

// ---------------------------------------------------------------- 小工具

const $ = (id) => document.getElementById(id);

/** 把后端的时间戳当 UTC 解析。
 *
 * 后端所有 datetime 都是 UTC（`datetime.now(timezone.utc)`），但 ISO 字符串
 * 有时带 +00:00、有时不带；不带后缀时 `Date.parse` 会按**本地时区**理解，
 * 算出来的「已用时」会平白差 8 小时。这里只补后缀，不改语义。
 */
function parseUtc(value) {
  if (!value) return null;
  const text = String(value);
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(text);
  const ms = Date.parse(hasZone ? text : `${text}Z`);
  return Number.isNaN(ms) ? null : ms;
}

function formatSeconds(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  return `${seconds.toFixed(1)} 秒`;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function text(tag, content, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  // 报告正文来自大模型，一律 textContent 插入 —— 绝不用 innerHTML。
  node.textContent = content === null || content === undefined ? "" : String(content);
  return node;
}

// ---------------------------------------------------------------- 请求

/** 发一个请求并把统一外壳解析出来。
 *
 * 返回值一定是 `{ok: true, data} | {ok: false, error}` 形状：
 * 网络失败、非 JSON 响应也在这里被翻译成同样的形状，调用方只需要认一种结构。
 */
async function api(path, options) {
  let resp;
  try {
    resp = await fetch(path, options);
  } catch (err) {
    return {
      ok: false,
      error: { code: "NETWORK_ERROR", message: "连不上服务，请确认后端还在运行", detail: String(err) },
    };
  }

  let body = null;
  try {
    body = await resp.json();
  } catch (err) {
    body = null;
  }

  if (!body || typeof body !== "object") {
    // 非 JSON 响应只可能是代理 / 网关插进来的错误页，不是我们的外壳。
    return {
      ok: false,
      error: {
        code: "BAD_RESPONSE",
        message: `服务返回了无法解析的响应（HTTP ${resp.status}）`,
        detail: path,
      },
    };
  }
  return body;
}

function errorOf(result) {
  return (result && result.error) || { code: "UNKNOWN", message: "未知错误", detail: null };
}

/** 统一的错误块：错误码 + 中文 message + detail。
 *
 * **不查表翻译**：message 就是后端给的中文；code 原样显示，方便对着接口文档排查。
 * `onRetry` 非空时额外给一个「重试」按钮。
 */
function renderErrorBox(box, error, onRetry) {
  clear(box);
  box.hidden = false;
  const head = document.createElement("div");
  head.appendChild(text("strong", error.message || "请求失败"));
  head.appendChild(text("span", `  [${error.code || "UNKNOWN"}]`, "meta"));
  box.appendChild(head);

  if (error.detail) {
    box.appendChild(text("div", error.detail, "detail"));
  }

  // 超时是最容易误判的一种失败：HTTP 是 200，只有 code / status 能看出来。
  if (error.code === "WORKFLOW_TIMEOUT") {
    box.appendChild(
      text("div", "工作流超过了时间上限，已按超时结束（注意：这个错误的 HTTP 状态码仍然是 200）。", "detail")
    );
  }

  if (typeof onRetry === "function") {
    const retry = text("button", "重试", "primary");
    retry.type = "button";
    retry.addEventListener("click", onRetry);
    const row = document.createElement("div");
    row.className = "row";
    row.appendChild(retry);
    box.appendChild(row);
  }
}

function hideErrorBox(box) {
  box.hidden = true;
  clear(box);
}

function renderInline(container, error, onRetry) {
  clear(container);
  container.appendChild(text("span", error.message || "请求失败", "error-text"));
  container.appendChild(text("span", `  [${error.code || "UNKNOWN"}]`, "muted"));
  if (typeof onRetry === "function") {
    const retry = text("button", "重试");
    retry.type = "button";
    retry.addEventListener("click", onRetry);
    container.appendChild(retry);
  }
}

// ---------------------------------------------------------------- A 上传区

async function handleUpload() {
  const input = $("file");
  const file = input.files && input.files[0];
  const box = $("upload-state");

  if (!file) {
    clear(box);
    box.appendChild(text("span", "请先选择一个 .csv 或 .xlsx 文件", "warn-text"));
    return;
  }

  clear(box);
  box.appendChild(text("span", `正在上传 ${file.name} …`));

  const form = new FormData();
  form.append("file", file);

  const result = await api(API.upload, { method: "POST", body: form });
  if (result.ok !== true) {
    const error = errorOf(result);
    // 上传失败**就地**提示：FILE_TOO_LARGE / UNSUPPORTED_FILE 是最常见的两种，
    // 但其它码也走这里 —— 上传区的问题不该弹到全局去（§9.2.1）。
    renderInline(box, error, RETRYABLE.indexOf(error.code) >= 0 ? handleUpload : null);
    return;
  }

  const dataset = result.data;
  state.datasetId = dataset.dataset_id;
  clear(box);
  const parts = [
    `已上传：${dataset.original_filename}`,
    `${dataset.row_count} 行 × ${dataset.column_count} 列`,
    `dataset_id ${dataset.dataset_id}`,
  ];
  box.appendChild(text("span", parts.join(" · "), "ok-text"));

  $("ask-state").textContent = "";
  $("workflow-state").textContent = "";
  hideErrorBox($("workflow-error"));
  resetSteps();
  clear($("charts"));
  $("charts").appendChild(text("p", "分析完成后显示", "muted"));
  clear($("report"));
  $("report").appendChild(text("p", "分析完成后显示", "muted"));

  await Promise.all([loadDataset(), loadProfile(), loadHealth()]);
}

// ---------------------------------------------------------------- B 数据概览区

async function loadDataset() {
  if (!state.datasetId) return;
  const result = await api(API.dataset(state.datasetId));
  const summary = $("dataset-summary");
  if (result.ok !== true) {
    clear(summary);
    summary.appendChild(text("span", errorOf(result).message, "error-text"));
    return;
  }
  const data = result.data;
  clear(summary);
  summary.appendChild(
    text("span", `${data.original_filename} · ${data.row_count} 行 × ${data.column_count} 列 · ${data.size_bytes} 字节`)
  );

  renderPreview(data);
  renderColumns(data);
}

function renderPreview(data) {
  const table = $("preview");
  clear(table);
  const rows = data.preview_rows || [];
  if (rows.length === 0) {
    table.appendChild(text("tbody", "")); // 占位：没有预览时保持表格存在
    const tr = document.createElement("tr");
    tr.appendChild(text("td", "没有预览数据", "muted"));
    table.firstChild.appendChild(tr);
    return;
  }

  const columns = data.columns && data.columns.length ? data.columns : Object.keys(rows[0]);
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  columns.forEach((name) => headRow.appendChild(text("th", name)));
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    columns.forEach((name) => {
      const value = row[name];
      tr.appendChild(text("td", value === null || value === undefined || value === "" ? "—" : value));
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
}

function renderColumns(data) {
  const list = $("columns");
  clear(list);
  const columns = data.columns || [];
  if (columns.length === 0) {
    list.appendChild(text("li", "没有列", "muted"));
    return;
  }
  columns.forEach((name) => {
    const dtype = (data.dtypes && data.dtypes[name]) || "";
    list.appendChild(text("li", dtype ? `${name}（${dtype}）` : name));
  });
}

async function loadProfile() {
  if (!state.datasetId) return;
  const result = await api(API.profile(state.datasetId));
  const missing = $("missing-top3");
  const warnings = $("profile-warnings");
  clear(warnings);

  if (result.ok !== true) {
    clear(missing);
    missing.appendChild(text("li", errorOf(result).message, "error-text"));
    return;
  }

  const profile = result.data;
  const columns = (profile.columns || []).slice();
  // 按缺失比例排序，取前 3 —— 这就是「缺失值最多的 3 列」。
  columns.sort((a, b) => (b.null_ratio || 0) - (a.null_ratio || 0));
  clear(missing);
  const top = columns.slice(0, 3);
  if (top.length === 0) {
    missing.appendChild(text("li", "没有列", "muted"));
  }
  top.forEach((col) => {
    const ratio = ((col.null_ratio || 0) * 100).toFixed(1);
    missing.appendChild(text("li", `${col.name}：${col.null_count} 个缺失（${ratio}%）`));
  });

  (profile.warnings || []).forEach((warning) => {
    warnings.appendChild(text("li", warning));
  });
}

async function loadHealth() {
  const badge = $("llm-badge");
  const result = await api(API.health);
  clear(badge);
  if (result.ok !== true) {
    badge.className = "badge badge-grey";
    badge.appendChild(text("span", "大模型状态未知"));
    return;
  }
  const configured = result.data.llm_configured === true;
  badge.className = configured ? "badge badge-ok" : "badge badge-grey";
  badge.appendChild(
    text("span", configured ? "已配置大模型" : "未配置大模型，将使用规则模式")
  );
}

// ---------------------------------------------------------------- C 提问区

async function ask() {
  const questionEl = $("question");
  const statusEl = $("ask-state");
  const question = (questionEl.value || "").trim();

  clear(statusEl);
  if (!state.datasetId) {
    statusEl.appendChild(text("span", "请先上传数据", "warn-text"));
    return;
  }
  if (question.length < 2) {
    statusEl.appendChild(text("span", "问题太短了，至少写 2 个字", "warn-text"));
    return;
  }

  statusEl.appendChild(text("span", "正在提交…"));
  const result = await api(API.analysis, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dataset_id: state.datasetId, question }),
  });

  if (result.ok !== true) {
    const error = errorOf(result);
    renderInline(statusEl, error, RETRYABLE.indexOf(error.code) >= 0 ? ask : null);
    return;
  }

  state.workflowId = result.data.workflow_id;
  clear(statusEl);
  statusEl.appendChild(text("span", `已提交，workflow_id ${state.workflowId}`, "ok-text"));

  hideErrorBox($("workflow-error"));
  clear($("charts"));
  $("charts").appendChild(text("p", "分析完成后显示", "muted"));
  clear($("report"));
  $("report").appendChild(text("p", "分析完成后显示", "muted"));
  resetSteps();
  startPolling();
}

// ---------------------------------------------------------------- D 状态区

function resetSteps() {
  const list = $("steps");
  clear(list);
  STEP_ORDER.forEach((key, index) => {
    const item = document.createElement("li");
    item.dataset.step = key;
    item.dataset.index = String(index);
    item.appendChild(text("span", "", "dot"));
    item.appendChild(text("span", STEP_LABELS[key]));
    list.appendChild(item);
  });
  $("elapsed").textContent = "—";
}

function paintSteps(step, status) {
  const items = $("steps").children;
  let current = STEP_ORDER.indexOf(step);
  if (current === -1) {
    // step 可能是 "done"（所有任务跑完时后端会写这个值）或还没开始时的空值：
    // 终态按「全部完成」处理，其余按「还没开始」处理 —— 不要因为认不出 step 就卡住界面。
    current = TERMINAL.indexOf(status) >= 0 ? STEP_ORDER.length : 0;
  }
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    let cls = "";
    if (i < current) cls = "done";
    else if (i === current) cls = status === "FAILED" ? "failed" : "active";
    item.className = cls;
  }
}

function renderTasks(tasks) {
  const list = $("task-list");
  clear(list);
  (tasks || []).forEach((task) => {
    const suffix = task.error ? `（${task.error.message || task.error.code}）` : "";
    list.appendChild(text("li", `${task.task_id} ${task.description} — ${task.status}${suffix}`));
  });
}

function startPolling() {
  stopPolling();
  pollOnce();
  state.pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (state.pollTimer !== null) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function pollOnce() {
  if (!state.workflowId) return;

  const result = await api(API.workflow(state.workflowId));
  if (result.ok !== true) {
    const error = errorOf(result);
    // 状态接口本身失败了：停轮询，把错误显示出来（可重试的码给重试按钮）。
    stopPolling();
    renderErrorBox($("workflow-error"), error, RETRYABLE.indexOf(error.code) >= 0 ? startPolling : null);
    $("workflow-state").textContent = "轮询已停止";
    return;
  }

  const data = result.data;
  paintSteps(data.step, data.status);
  renderTasks(data.tasks);
  $("workflow-state").textContent = `状态 ${data.status} · 步骤 ${data.step}（${data.step_index}/${data.total_steps}）`;

  const startedAt = parseUtc(data.created_at);
  const endedAt = parseUtc(data.finished_at);
  if (startedAt !== null) {
    const seconds = ((endedAt === null ? Date.now() : endedAt) - startedAt) / 1000;
    $("elapsed").textContent = `已用时 ${formatSeconds(seconds)}`;
  }

  if (data.error) {
    // 失败信息只看 error.code / status，不看 HTTP 状态码（超时时 HTTP 仍是 200）。
    const error = data.error;
    const retryable = RETRYABLE.indexOf(error.code) >= 0;
    renderErrorBox($("workflow-error"), error, retryable ? startPolling : null);
  } else {
    hideErrorBox($("workflow-error"));
  }

  if (TERMINAL.indexOf(data.status) === -1) return;

  // 进终态：立刻停轮询（clearInterval），先画图，再按 has_report 决定要不要取报告。
  // has_report 就是为这件事加的（§7.1）：它 false 时报告接口必然 400，不该白请求。
  stopPolling();
  renderCharts(data.charts || []);
  if (data.has_report === true) {
    await loadReport();
  } else {
    clear($("report"));
    $("report").appendChild(
      text("p", data.status === "FAILED" ? "本次分析失败，没有生成报告。" : "本次没有生成报告（has_report=false）。", "muted")
    );
  }
}

// ---------------------------------------------------------------- E 图表区

function renderCharts(charts) {
  const box = $("charts");
  clear(box);

  if (!charts || charts.length === 0) {
    box.appendChild(text("p", "这次没有生成图表", "muted"));
    return;
  }

  charts.forEach((chart) => {
    const figure = document.createElement("figure");
    // 直接用后端拼好的 url（接口 8），不下载、不转 base64。
    const src = chart.url || API.chart(state.workflowId, chart.filename);
    const img = document.createElement("img");
    img.src = src;
    img.alt = chart.title || chart.filename || "图表";
    img.addEventListener("error", () => {
      clear(figure);
      figure.appendChild(text("p", `图表文件读不到：${chart.filename}`, "error-text"));
    });
    figure.appendChild(img);

    const caption = document.createElement("figcaption");
    caption.appendChild(text("div", chart.title || chart.filename));
    caption.appendChild(text("div", `${chart.chart_type || ""}`, "ctype"));
    figure.appendChild(caption);
    box.appendChild(figure);
  });
}

// ---------------------------------------------------------------- F 报告区

async function loadReport() {
  const box = $("report");
  const result = await api(API.report(state.workflowId));
  if (result.ok !== true) {
    clear(box);
    const error = errorOf(result);
    box.appendChild(text("p", `报告读取失败：${error.message} [${error.code}]`, "error-text"));
    return;
  }
  renderReport(result.data);
}

function renderValidation(box, validation) {
  // 校验横幅是「系统在自我检查」的证据：数字对不上就黄，全部核对通过就绿。
  const banner = document.createElement("div");

  if (!validation) {
    banner.className = "banner";
    banner.appendChild(text("h4", "本次没有校验结果"));
    box.appendChild(banner);
    return;
  }

  const checked = validation.checked_numbers || 0;

  if (validation.passed === false) {
    banner.className = "banner banner-warn";
    const unsupported = validation.unsupported_numbers || [];
    banner.appendChild(
      text("h4", `⚠ 校验未通过：有 ${unsupported.length} 个数字在工具结果里找不到出处`)
    );
    banner.appendChild(text("div", `已核对 ${checked} 个数字，其中下面这些没有依据：`));
    if (unsupported.length > 0) {
      const list = document.createElement("ul");
      unsupported.forEach((number) => list.appendChild(text("li", number)));
      banner.appendChild(list);
    }
    const issues = (validation.issues || []).filter((issue) => issue.severity === "error");
    if (issues.length > 0) {
      const list = document.createElement("ul");
      issues.forEach((issue) => list.appendChild(text("li", `${issue.code}：${issue.message}`)));
      banner.appendChild(list);
    }
    box.appendChild(banner);
    return;
  }

  banner.className = "banner banner-ok";
  banner.appendChild(text("h4", `✓ 数字已逐条核对（共核对 ${checked} 个数字）`));
  const warnings = (validation.issues || []).filter((issue) => issue.severity === "warning");
  if (warnings.length > 0) {
    const list = document.createElement("ul");
    warnings.forEach((issue) => list.appendChild(text("li", `${issue.code}：${issue.message}`)));
    banner.appendChild(list);
  }
  box.appendChild(banner);
}

function renderReport(data) {
  const box = $("report");
  clear(box);

  const report = data.report || {};
  renderValidation(box, data.validation);

  box.appendChild(text("h3", report.title || "分析报告", "report-title"));

  const meta = [];
  if (report.report_id) meta.push(`报告编号 ${report.report_id}`);
  meta.push(report.llm_used === true ? "大模型撰写" : "规则模板撰写（未调用大模型）");
  if (data.llm_used !== undefined && data.llm_used !== report.llm_used) {
    meta.push(`llm_used=${data.llm_used}`);
  }
  meta.push(`${(data.results || []).length} 个工具结果`);
  box.appendChild(text("p", meta.join(" · "), "meta"));

  if (report.summary) {
    box.appendChild(text("p", report.summary, "summary"));
  }

  (report.sections || []).forEach((section) => {
    const block = document.createElement("section");
    block.className = "block";
    block.appendChild(text("h3", section.heading || ""));
    block.appendChild(text("p", section.text || ""));

    if (section.table && section.table.length > 0) {
      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      const table = document.createElement("table");
      const columns = Object.keys(section.table[0]);
      const thead = document.createElement("thead");
      const headRow = document.createElement("tr");
      columns.forEach((name) => headRow.appendChild(text("th", name)));
      thead.appendChild(headRow);
      table.appendChild(thead);
      const tbody = document.createElement("tbody");
      section.table.forEach((row) => {
        const tr = document.createElement("tr");
        columns.forEach((name) => {
          const value = row[name];
          tr.appendChild(text("td", value === null || value === undefined ? "—" : value));
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      block.appendChild(wrap);
    }

    if (section.cites && section.cites.length > 0) {
      block.appendChild(text("p", `依据：${section.cites.join("、")}`, "cites"));
    }
    box.appendChild(block);
  });

  const notes = report.data_quality_notes || [];
  if (notes.length > 0) {
    const block = document.createElement("section");
    block.className = "block";
    block.appendChild(text("h3", "数据质量提示"));
    const list = document.createElement("ul");
    list.className = "plain warn-list";
    notes.forEach((note) => list.appendChild(text("li", note)));
    block.appendChild(list);
    box.appendChild(block);
  }
}

// ---------------------------------------------------------------- 启动

function init() {
  resetSteps();
  $("upload").addEventListener("click", handleUpload);
  $("ask").addEventListener("click", ask);
  document.querySelectorAll("button.example").forEach((button) => {
    button.addEventListener("click", () => {
      $("question").value = button.dataset.question || "";
    });
  });
  loadHealth();
}

document.addEventListener("DOMContentLoaded", init);

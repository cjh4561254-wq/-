/* =====================================================================
 * login.js —— 登录页唯一的行为脚本
 * =====================================================================
 * 这个文件只做四件事：
 *   1. 用真实接口问清楚"这是首次部署还是日常登录"，然后切页面形态；
 *   2. 把校验规则、禁用条件、错误文案收敛到一处，避免同一个判断散落三遍；
 *   3. 登录 / 首次部署的真实请求与失败处理（401 / 429 / 5xx / 网络）；
 *   4. 登录成功后的回跳 —— 服务端**不支持 next**（app/main.py:362 固定 303 跳 /login，
 *      注释里写明这是防开放重定向的有意设计），所以回跳必须在这里做，
 *      而且 next 必须先校验再使用（只接受 /app 开头、同源、不含 // 与 \ 的路径）。
 *
 * 安全边界（刻意不做的事）：
 *   - 不打读会话 Cookie：mis_session 是 HttpOnly + SameSite=Lax，JS 本来就读不到，
 *     而且"登录态"的唯一事实来源是 GET /api/auth/me，不是本地有没有 cookie；
 *   - 不保存密码：密码只进请求体，不进 localStorage / sessionStorage / URL；
 *   - 不伪造任何东西：没有假登录、没有假邮件验证、没有假的"记住我"。
 *
 * 为什么全程用 var / function 而不是 const / 箭头函数：
 * app/web/app.js 是 ES5 风格（var + function），这个页面要和它保持同一档语法，
 * 免得日后有人把这里的代码搬过去。另外注意：**不要写协议前缀 + 域名的绝对网址**
 * （前端源码有纯文本扫描测试），所有接口路径都是站内相对路径。
 * ===================================================================== */
(function () {
  "use strict";

  /* ---------------------------------------------------------------
   * 一、常量
   * ------------------------------------------------------------- */
  var API = {
    health: "/api/health",            // 公开，故意不要求登录（app/main.py:303-307）
    me: "/api/auth/me",               // 匿名也能访问，返回 authenticated / user / needs_bootstrap / session_expires_at
    login: "/api/auth/login",
    bootstrap: "/api/auth/bootstrap"  // 只在账号库为空时允许，库非空返回 403
  };

  var MODE = { PENDING: "pending", LOGIN: "login", BOOTSTRAP: "bootstrap" };

  // 登录成功回跳的兜底视图 = app.js 的 DEFAULT_VIEW（当前为 "command" 战略态势带）。
  // 这两个字面量必须互等（tests/test_frontend_command_center.py 有守卫钉住）：
  // 上一个 Wave 把 app.js 首屏改成 command、却没同步这里的兜底，导致登录后落到
  // 侧栏没有对应导航项的旧 #/workbench（命令条名不符、侧栏零高亮）。
  var APP_HASH_FALLBACK = "/app#/command";
  var NEXT_DISPLAY_FALLBACK = "/app#/command";

  var REQUEST_TIMEOUT_MS = 12000;     // 超时按"服务没响应"处理，而不是让转圈无限转下去
  var DEFAULT_API_ERROR = "服务端出错了，请稍后重试";

  // 校验规则必须和服务端可验证的规则一致（app/schemas_auth.py）：
  //   USERNAME_PATTERN  = ^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$  → 3~32 位、字母或数字开头
  //   PASSWORD_MIN_LENGTH = 8，且必须同时含字母和数字
  // 登录接口的规则**更宽**（用户名 1~64、密码 1~128，无 pattern）——
  // 所以这里分模式校验：登录时只要求"非空且不超长"，否则一个早期建的老用户名
  // 会被前端挡在门外，而服务端其实是认的。
  var USERNAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$/;
  var USERNAME_MIN = 3;
  var USERNAME_MAX = 32;
  var LOGIN_USERNAME_MAX = 64;
  var PASSWORD_MIN = 8;
  var PASSWORD_MAX = 128;

  // 会话锁定时长的兜底值：正常一律以服务端 Retry-After 头为准，只有拿不到时才用它。
  var LOCK_FALLBACK_SECONDS = 60;
  var LOCK_MAX_SECONDS = 3600;        // 服务端给的长度不该无限大，超过就封顶

  /* ---------------------------------------------------------------
   * 二、模块状态
   * ------------------------------------------------------------- */
  var win = window;
  var doc = document;

  var el = {};                        // DOM 引用（boot 时一次性收集）
  var state = {
    mode: MODE.PENDING,               // 当前页面形态
    busy: false,                      // 是否有提交在飞
    lockLeft: 0,                      // 429 倒计时剩余秒数
    lockActive: false,                // 本轮锁定是否已经把"标题"写进 alert
    lockTimer: 0,
    meFailed: false,                  // /api/auth/me 读不到（5xx / 网络）
    healthFailed: false,              // /api/health 读不到
    healthData: null,
    touched: { username: false, password: false, confirm: false },
    userInteracted: false,            // 用户是否真的动过表单（D-05：决定要不要先亮那句"请填写…"）
    alertFocused: false,              // 本轮 alert 是否已经接过焦点（429 倒计时每秒刷新时不许再抢）
    alertAction: null
  };
  var nextTarget = { raw: "", safe: "" };
  var sessionHint = "";               // /api/auth/me 返回 401/403 时的诚实说明
  var expiredReason = false;          // app.js 以 ?reason=expired 送来
  var bootstrapHint = false;          // app.js 以 ?bootstrap=1 送来
  var envNoticeText = "";

  /* ---------------------------------------------------------------
   * 三、小工具
   * ------------------------------------------------------------- */

  // 只在文本真的变了才写 DOM。role=alert 是 live region，把同样的字符串
  // 反复赋给 textContent 也算一次 DOM 变更，读屏可能重念一遍。
  function setText(node, value) {
    if (node && node.textContent !== value) {
      node.textContent = value;
    }
  }

  function show(node, visible) {
    if (!node) { return; }
    if (visible) {
      node.removeAttribute("hidden");
    } else {
      node.setAttribute("hidden", "");
    }
  }

  // 程序化焦点迁移（D-01）：错误/限流/在途状态出现时，键盘用户不该被丢回 BODY。
  // 目标节点都带 tabindex="-1"：能被脚本聚焦，但不占 Tab 序列。
  function focusStatus(node) {
    if (!node || node.hidden || typeof node.focus !== "function") { return; }
    try {
      node.focus();
    } catch (err) {
      // 焦点迁移失败不该打断提交/报错链路
    }
  }

  function markInteracted() {
    state.userInteracted = true;
  }

  // D-05：零交互的首访不先挨一句命令式文案。blur 也算"动过"，
  // 而 blur 不冒泡，所以监听它时要走捕获阶段（见 bindEvents）。
  function userInteracted() {
    return state.userInteracted === true ||
      state.touched.username || state.touched.password || state.touched.confirm;
  }

  function isFiniteNumber(value) {
    return typeof value === "number" && isFinite(value);
  }

  /* ---------------------------------------------------------------
   * 四、网络层：一个统一的取数函数
   * ------------------------------------------------------------- */
  // 返回 Promise<{status, ok, payload, res}>；真正的网络失败会 reject 一个
  // {kind:"network"|"timeout"} 标记对象 —— 让"服务端说不行"和"根本没连上"
  // 在调用处一眼可分。
  function fetchJson(url, init) {
    var options = init || {};
    var headers = { "Accept": "application/json" };
    var timeoutId = 0;
    var controller = win.AbortController ? new win.AbortController() : null;

    options.credentials = "same-origin";   // 会话 Cookie 必须带上（同源，不带它等于永远未登录）
    if (options.body) {
      headers["Content-Type"] = "application/json";
    }
    options.headers = headers;
    if (controller) {
      options.signal = controller.signal;
      timeoutId = win.setTimeout(function () {
        controller.abort();
      }, REQUEST_TIMEOUT_MS);
    }

    function clearTimer() {
      if (timeoutId) {
        win.clearTimeout(timeoutId);
        timeoutId = 0;
      }
    }

    return win.fetch(url, options).then(
      function (res) {
        clearTimer();
        return res.json().catch(function () {
          return null;                     // 非 JSON 响应不算崩溃，交给上层按 status 处理
        }).then(function (payload) {
          return { res: res, status: res.status, ok: res.ok, payload: payload };
        });
      },
      function (err) {
        clearTimer();
        var aborted = err && (err.name === "AbortError" || err.code === 20);
        throw { kind: aborted ? "timeout" : "network", cause: err };
      }
    );
  }

  // 把 {ok:false, data:null, error:{code,message,detail}} 这个错信封拆成文字。
  // 末尾两个 payload.detail 分支是**历史兜底**：422 现在由 app/main.py 的
  // RequestValidationError handler 翻成同一个外壳（error.message 已是中文），
  // 正常走不到这里；留着是为了万一绕过外壳直接拿到 FastAPI 原生形状时不显示空白。
  function readError(payload, fallbackText) {
    var info = { code: "", message: "", detail: "" };
    if (payload && typeof payload === "object") {
      var err = payload.error;
      if (err && typeof err === "object") {
        if (typeof err.code === "string") { info.code = err.code; }
        if (typeof err.message === "string") { info.message = err.message; }
        if (typeof err.detail === "string") {
          info.detail = err.detail;
        } else if (err.detail && typeof err.detail === "object") {
          info.detail = JSON.stringify(err.detail);
        }
      }
      if (!info.message && typeof payload.detail === "string") {
        info.message = payload.detail;
      }
      if (!info.message && Object.prototype.toString.call(payload.detail) === "[object Array]" && payload.detail.length) {
        var first = payload.detail[0];
        if (first && typeof first.msg === "string") { info.message = first.msg; }
      }
    }
    if (!info.message) { info.message = fallbackText || DEFAULT_API_ERROR; }
    return info;
  }

  // 状态码与错误码的收用处：主显永远是中文那句，码降级成「原值码：X」挂在后面。
  // 码不翻译（D-9）：译成"未授权"就跟后端日志对不上口径，出问题时没人能核。
  function codeSuffix(status, code) {
    return "（HTTP " + status + (code ? "，原值码：" + code : "") + "）";
  }

  // 429 的等待秒数：以服务端 Retry-After 响应头为唯一依据（同源响应可读全部响应头）。
  function retryAfterSeconds(res) {
    var raw = null;
    try {
      raw = res && res.headers ? res.headers.get("Retry-After") : null;
    } catch (err) {
      raw = null;
    }
    var seconds = raw ? parseInt(raw, 10) : NaN;
    if (!isFiniteNumber(seconds) || seconds <= 0) { return 0; }
    return Math.min(seconds, LOCK_MAX_SECONDS);
  }

  /* ---------------------------------------------------------------
   * 五、提示区（notice / alert / 按钮禁用原因）
   * ------------------------------------------------------------- */

  // 信息级提示（role=status）：会话过期、部署已有账号、401 这种"说明"走这里。
  function setNotice(text, warn) {
    if (!text) {
      show(el.notice, false);
      setText(el.noticeText, "");
      return;
    }
    setText(el.noticeText, text);
    // warn 时换左边框颜色：同一条提示槽要能表达"这是提醒，不是故障"
    if (el.notice) {
      if (warn) {
        el.notice.classList.add("notice--warn");
      } else {
        el.notice.classList.remove("notice--warn");
      }
    }
    show(el.notice, true);
  }

  // 错误/警告区（role=alert）：提交失败、读不到账号状态走这里。
  // action 传函数时显示一个文字级按钮（重试 / 切换到登录）——它刻意不做成主按钮，
  // 全页只有一个主按钮（提交）。
  function setAlert(options) {
    var opts = options || {};
    var kind = opts.kind || "error";
    if (el.alert) {
      el.alert.classList.remove("alert--warn", "alert--info");
      if (kind === "warn") { el.alert.classList.add("alert--warn"); }
      if (kind === "info") { el.alert.classList.add("alert--info"); }
    }
    setText(el.alertTitle, opts.title || "");
    setText(el.alertDetail, opts.detail || "");
    show(el.alertDetail, !!opts.detail);
    state.alertAction = typeof opts.action === "function" ? opts.action : null;
    if (el.alertRetry) {
      setText(el.alertRetry, opts.actionLabel || "重试");
      show(el.alertRetry, !!state.alertAction);
    }
    show(el.alert, true);
    // D-01/D-11：alert 从"隐藏 → 显示"的那一刻把焦点接过来（#alert 上带 tabindex="-1"）。
    // 只在首次显示时迁移：429 倒计时每秒只改明细行，不能每秒抢一次焦点。
    if (!state.alertFocused) {
      state.alertFocused = true;
      focusStatus(el.alert);
    }
  }

  // 只改明细行，不动标题 —— 429 倒计时每秒都要刷新这一行，
  // 标题里那句"登录尝试过于频繁"只说一次（#alert-detail 上是 aria-live="off"）。
  function setAlertDetail(text) {
    setText(el.alertDetail, text);
    show(el.alertDetail, !!text);
  }

  function clearAlert() {
    show(el.alert, false);
    state.alertFocused = false;         // 下一次新错误可以重新接一次焦点
    setText(el.alertTitle, "");
    setText(el.alertDetail, "");
    state.alertAction = null;
    show(el.alertRetry, false);
  }

  // 按钮为什么不能点：写在按钮下方，并用 aria-describedby 关联到按钮上。
  function setSubmitReason(text) {
    setText(el.submitReason, text || "");
    show(el.submitReason, !!text);
  }

  /* ---------------------------------------------------------------
   * 六、字段级校验
   * ------------------------------------------------------------- */
  function fieldErrorNodes(key) {
    if (key === "username") { return { input: el.username, box: el.usernameError }; }
    if (key === "password") { return { input: el.password, box: el.passwordError }; }
    return { input: el.passwordConfirm, box: el.passwordConfirmError };
  }

  // 每个字段的「提示 id / 错误 id」写在一起：读屏用户需要知道是**哪个字段**错了。
  // 表单级 role=alert 只能播报"整体错了"，不能定位；实测（a11y-responsive）两个输入框
  // 曾经是 aria-invalid=None、aria-describedby 只指向提示文案，错误文案反而没被关联。
  function fieldAriaIds(key) {
    if (key === "username") {
      return { input: el.username, hint: "username-hint", error: "username-error" };
    }
    if (key === "password") {
      return { input: el.password, hint: "password-hint", error: "password-error" };
    }
    return { input: el.passwordConfirm, hint: "password-confirm-hint", error: "password-confirm-error" };
  }

  function showFieldError(key, message) {
    var nodes = fieldErrorNodes(key);
    if (!nodes.box) { return; }
    var textNode = nodes.box.querySelector(".field__error-text");
    setText(textNode, message || "");
    show(nodes.box, !!message);
    var ids = fieldAriaIds(key);
    if (ids.input) {
      if (message) {
        ids.input.setAttribute("aria-invalid", "true");
        // 提示 + 错误一起关联，顺序与视觉顺序一致（先读到规则，再读到错在哪）
        ids.input.setAttribute("aria-describedby", ids.hint + " " + ids.error);
      } else {
        ids.input.removeAttribute("aria-invalid");
        ids.input.setAttribute("aria-describedby", ids.hint);   // 恢复：只关联提示
      }
    }
  }

  // 返回该字段的第一条错误（没有就返回空串）。只在失焦或提交时调用 ——
  // 边打字边判红是攻击性设计，人还没打完就先挨一句错。
  function validateField(key) {
    var value;
    if (key === "username") {
      value = el.username ? el.username.value.trim() : "";
      if (!value) { return "请输入用户名。"; }
      if (state.mode === MODE.BOOTSTRAP) {
        if (!USERNAME_PATTERN.test(value)) {
          return "用户名需要 " + USERNAME_MIN + "–" + USERNAME_MAX + " 位，以字母或数字开头，只能用字母、数字、下划线、点和连字符。";
        }
      } else if (value.length > LOGIN_USERNAME_MAX) {
        return "用户名最长 " + LOGIN_USERNAME_MAX + " 个字符。";
      }
      return "";
    }
    if (key === "password") {
      value = el.password ? el.password.value : "";
      if (!value) { return "请输入密码。"; }
      if (value.length > PASSWORD_MAX) { return "密码最长 " + PASSWORD_MAX + " 个字符。"; }
      if (state.mode === MODE.BOOTSTRAP) {
        // 只拦服务端白纸黑字写明的规则（长度 + 必须同时含字母和数字）。
        // "常见弱密码"那份名单在服务端（app/schemas_auth.py），前端复刻不出来，
        // 所以不在本地假装能判——交给服务端 422 的原话回显，比猜一个名单更诚实。
        if (value.length < PASSWORD_MIN) { return "密码至少 " + PASSWORD_MIN + " 位。"; }
        if (!/[A-Za-z]/.test(value) || !/[0-9]/.test(value)) { return "密码必须同时包含字母和数字。"; }
      }
      return "";
    }
    // confirm：纯本地校验，服务端 bootstrap 没有这个字段
    value = el.passwordConfirm ? el.passwordConfirm.value : "";
    if (!value) { return "请再输入一次密码。"; }
    if (el.password && value !== el.password.value) { return "两次输入的密码不一致，请重新输入。"; }
    return "";
  }

  function validateAll() {
    var firstKey = "";
    var keys = ["username", "password"];
    if (state.mode === MODE.BOOTSTRAP) { keys.push("confirm"); }
    for (var i = 0; i < keys.length; i += 1) {
      var message = validateField(keys[i]);
      showFieldError(keys[i], message);
      if (message && !firstKey) { firstKey = keys[i]; }
    }
    return firstKey;
  }

  function clearAllFieldErrors() {
    showFieldError("username", "");
    showFieldError("password", "");
    showFieldError("confirm", "");
  }

  /* ---------------------------------------------------------------
   * 七、按钮状态：四种禁用前置，每种都在按钮下方写出原因
   * ------------------------------------------------------------- */
  function updateSubmitState() {
    if (!el.submit) { return; }
    var disabled = false;
    var reason = "";

    if (state.busy) {
      disabled = true;
      reason = state.mode === MODE.BOOTSTRAP ? "正在创建管理员，请稍候…" : "正在提交，请稍候…";
    } else if (state.lockLeft > 0) {
      disabled = true;
      reason = "登录尝试过于频繁，请在 " + state.lockLeft + " 秒后重试。";
    } else if (state.meFailed) {
      disabled = true;
      reason = "读不到账号状态：无法确认这是首次部署还是日常登录，请先确认服务已启动（可点上方按钮重试）。";
    } else if (state.mode === MODE.PENDING) {
      disabled = true;
      reason = "正在检查登录状态…";
    } else {
      var emptyPrompt = false;         // 这条原因是不是"还没填"这类提示
      if (state.mode === MODE.BOOTSTRAP) {
        if (!(el.username && el.username.value.trim())) { reason = "请填写用户名。"; emptyPrompt = true; }
        else if (!(el.password && el.password.value)) { reason = "请填写密码。"; emptyPrompt = true; }
        else if (!(el.passwordConfirm && el.passwordConfirm.value)) { reason = "请再输入一次密码。"; emptyPrompt = true; }
        else {
          var firstBad = validateField("username") || validateField("password") || validateField("confirm");
          reason = firstBad;
        }
      } else {
        if (!(el.username && el.username.value.trim())) { reason = "请填写用户名。"; emptyPrompt = true; }
        else if (!(el.password && el.password.value)) { reason = "请填写密码。"; emptyPrompt = true; }
        else {
          reason = validateField("username") || validateField("password");
        }
      }
      disabled = !!reason;
      // D-05：零交互的首访不先给一句命令式的"请填写用户名。"——按钮本来就是灰的，
      // 用户还没开始做事就先被指挥一次，读起来像出错。等第一次输入/失焦/提交之后再写，
      // 那时这句话才是"按钮为什么还是灰的"的答案。
      if (emptyPrompt && !userInteracted()) { reason = ""; }
    }

    el.submit.disabled = disabled;
    setSubmitReason(disabled ? reason : "");
    return disabled;
  }

  function setBusy(busy, busyText) {
    state.busy = !!busy;
    // 先记下"点提交之前焦点是不是在按钮上"：按钮一旦 disabled，浏览器会把焦点丢回 BODY，
    // 所以这次迁移只能发生在禁用之后（D-01）。
    var hadSubmitFocus = doc.activeElement === el.submit;
    if (el.submit) {
      el.submit.classList.toggle("is-busy", !!busy);
      el.submit.setAttribute("aria-busy", busy ? "true" : "false");
    }
    if (busy) {
      setText(el.submitBusyText, busyText || "登录中…");
    }
    // D-06：在途冻结表单。慢网下改字段会让"提交的内容"和"屏幕上的内容"对不上，
    // 401 回来时用户就说不清自己到底提交了什么。用 readOnly 而不是 disabled：
    // disabled 会把已有焦点一起赶走，readOnly 不会。
    setFormFrozen(!!busy);
    updateSubmitState();
    if (busy && hadSubmitFocus) {
      focusStatus(el.submitReason);
    }
  }

  // 表单冻结开关：aria-busy 让读屏知道"这块在忙"，readOnly 防止在途改输入。
  function setFormFrozen(frozen) {
    if (el.form) {
      el.form.setAttribute("aria-busy", frozen ? "true" : "false");
    }
    [el.username, el.displayName, el.password, el.passwordConfirm].forEach(function (input) {
      if (input) { input.readOnly = frozen; }
    });
  }

  /* ---------------------------------------------------------------
   * 八、模式切换与提示
   * ------------------------------------------------------------- */
  function applyMode(mode) {
    state.mode = mode;
    doc.documentElement.setAttribute("data-auth-mode", mode);
    // 标签页标题主显中文全称；英文产品名不进标题（标签页宽度会截断，中文更易认）。
    doc.title = mode === MODE.BOOTSTRAP
      ? "首次部署 · AI 市场情报与智能分析平台"
      : "登录 · AI 市场情报与智能分析平台";

    // 密码字段的 autocomplete 要跟着模式变：登录是 current-password，
    // 新建是 new-password。写死的 autocomplete 会让密码管理器填错东西。
    if (el.password) {
      el.password.setAttribute("autocomplete", mode === MODE.BOOTSTRAP ? "new-password" : "current-password");
    }
    // 字段下方的提示文案按模式切换（两种模式的规则本来就不同）
    if (el.usernameHint) {
      setText(el.usernameHint, el.usernameHint.getAttribute(
        mode === MODE.BOOTSTRAP ? "data-hint-bootstrap" : "data-hint-login"
      ) || "");
    }
    if (el.passwordHint) {
      setText(el.passwordHint, el.passwordHint.getAttribute(
        mode === MODE.BOOTSTRAP ? "data-hint-bootstrap" : "data-hint-login"
      ) || "");
    }
    updateSubmitState();
  }

  // 所有说明性文字集中在一次渲染里，避免"先显示再被覆盖"的闪烁。
  function renderNotices(alreadyAuthenticated) {
    var parts = [];
    var warn = false;
    if (expiredReason) {
      // 首句用与 app.js / frontend-engineer 对齐的契约措辞（会话已过期，请重新登录），
      // 后面补上真实原因：改密码会让该账号全部会话失效（app/core/auth.py 的
      // delete_user_sessions）。少了后半句，用户会把"被踢"当成故障去报障。
      // 整条走 #notice（role=status、琥珀色、无警示图标），与 401 的 #alert
      //（role=alert、红色、三角图标）在文案/颜色/图标三方面都分得开。
      parts.push("会话已过期，请重新登录。会话失效的常见原因是改过密码 —— "
        + "改密码会让该账号的全部会话失效，如果刚改过密码，这是预期行为。");
      warn = true;
    }
    if (bootstrapHint && state.mode === MODE.LOGIN && !alreadyAuthenticated) {
      // app.js 是按它当时看到的 needs_bootstrap 跳过来的；如果这中间别人把账号建好了，
      // 这里要说实话，而不是让用户对着登录框猜。
      parts.push("这个部署已经有账号了，请直接登录。");
      warn = true;
    }
    if (sessionHint) {
      parts.push(sessionHint);
      warn = true;
    }
    setNotice(parts.join(" "), warn);
  }

  function renderEnvNotice(data) {
    var parts = [];
    var warn = false;
    if (data) {
      // 两个字段是 /api/health 里真实存在的（app/api/routes_health.py:45-46）
      if (data.llm_configured === false) {
        parts.push("本部署未配置大模型：报告会由规则模板撰写，分析结果仍然真实。");
      }
      if (data.upload_dir_ready === false) {
        parts.push("上传目录不可用，登录后无法上传数据。");
        warn = true;
      }
    }
    envNoticeText = parts.join(" ");
    if (el.envText) { setText(el.envText, envNoticeText); }
    if (el.envNotice) {
      if (warn) {
        el.envNotice.classList.add("notice--warn");
      } else {
        el.envNotice.classList.remove("notice--warn");
      }
    }
    show(el.envNotice, !!envNoticeText);
  }

  /* ---------------------------------------------------------------
   * 九、回跳目标校验（安全边界，别跳过这段）
   * ------------------------------------------------------------- */
  // 规则来自 docs/ux-spec-final.md §1.1：只接受以 /app 开头、同源、不含 // 与 \ 的值。
  // 为什么自己再抄一遍规则而不是"信 app.js 传来的参数"：query string 是用户可改的，
  // 一个开放重定向就是拿登录页当钓鱼跳板。
  // 防死循环：next 指回登录页自己时一律不接受。今天它被下面的"/app 前缀"挡在前面，
  // 是有意留的双保险 —— 以后一旦放宽前缀（比如多了 /console），这里仍然拦得住
  // /login?next=/login 这种自指，否则登录成功→回登录页→再登录。
  function pointsToLoginPage(raw) {
    var path = raw.split("?")[0].split("#")[0];
    return path === "/login" || path === "/login/";
  }

  function safeNextTarget(raw) {
    if (!raw) { return ""; }
    if (raw.charAt(0) !== "/") { return ""; }          // 必须是站内绝对路径
    if (raw.indexOf("//") !== -1) { return ""; }       // 以两个斜杠开头是协议相对地址（会跳到外部站点）
    if (raw.indexOf("\\") !== -1) { return ""; }       // 有些浏览器把 \ 当 / 处理
    if (raw.indexOf(":") !== -1) { return ""; }        // 挡掉 http: / javascript: 这类带协议的写法
    if (pointsToLoginPage(raw)) { return ""; }         // 不许回跳到登录页自己
    // 只接受 /app 本身或 /app# 的 hash 路由。为什么不用 raw.indexOf("/app") === 0：
    // 那样会同时放行 /app/report/x 这种**路径形式**（服务端没这条路由 → 404 + 原始 JSON）
    // 和 /application 这种同前缀的别的东西。这个函数既是安全边界也是格式边界。
    if (raw !== "/app" && raw.indexOf("/app#") !== 0) { return ""; }
    return raw;
  }

  // 首选 URLSearchParams：我们的路由是 hash 路由，app.js 传来的 next 里带 '#'
  // （被 encodeURIComponent 编成 %23），只有它会把 %23 原样还原成 #，
  // 也不会把路径里的 + 误当成空格。老浏览器没有它时退回手写解析（解码规则保持一致）。
  function parseQuery(search) {
    var out = { next: "", reason: "", bootstrap: "" };
    if (typeof win.URLSearchParams === "function") {
      var params = new win.URLSearchParams(search);
      out.next = params.get("next") || "";
      out.reason = params.get("reason") || "";
      out.bootstrap = params.get("bootstrap") || "";
      return out;
    }
    var pairs = search.replace(/^\?/, "").split("&");
    for (var i = 0; i < pairs.length; i += 1) {
      var pair = pairs[i].split("=");
      var key = decodeURIComponent(pair[0] || "");
      var value = decodeURIComponent((pair.slice(1).join("=") || "").replace(/\+/g, " "));
      if (key === "next" && !out.next) { out.next = value; }
      if (key === "reason") { out.reason = value; }
      if (key === "bootstrap") { out.bootstrap = value; }
    }
    return out;
  }

  // 已知的应用内路由前缀白名单（docs/ux-spec-final.md §1.1 的 hash 路由）。
  // 为什么用白名单而不是"只要以 / 开头就收"：fragment 是用户可改的，
  // #/../../ 这类怪路径不值得回跳；认不出来的一律回落工作台，用户只是回到首页，不会跳错页。
  var APP_ROUTE_PREFIXES = ["/workbench", "/dataset", "/analysis", "/report", "/history", "/settings"];

  function appRouteFromHash(hash) {
    if (!hash || hash.charAt(0) !== "#") { return ""; }
    var frag = hash.slice(1);                        // "#/report/abc" → "/report/abc"
    if (frag.charAt(0) !== "/") { return ""; }
    var path = frag.split("?")[0];
    for (var i = 0; i < APP_ROUTE_PREFIXES.length; i += 1) {
      var prefix = APP_ROUTE_PREFIXES[i];
      if (path === prefix || path.indexOf(prefix + "/") === 0) {
        // 拼回的是 **hash 路由**：/app + "#" + "/report/abc" = /app#/report/abc。
        // 这里少一个 "#" 会变成路径形式 /app/report/abc —— 服务端没有这条路由，
        // 浏览器会渲染原始 JSON 错误体（NOT_FOUND）。所以 # 必须原样拼回去。
        return "/app#" + frag;
      }
    }
    return "";
  }

  function readQuery() {
    var search = win.location.search || "";
    var params = (search && search !== "?") ? parseQuery(search) : { next: "", reason: "", bootstrap: "" };
    if (params.next && !nextTarget.raw) { nextTarget.raw = params.next; }
    if (params.reason === "expired") { expiredReason = true; }
    if (params.bootstrap === "1") { bootstrapHint = true; }
    // 服务端 303 看不见 fragment，物理上拼不出 hash：未登录打开 /app#/report/<id> 时，
    // app/main.py 只回 Location: /login，浏览器按 RFC 7231 把 fragment 继承下来，
    // 落地 URL 变成 /login#/report/<id>（query 里没有 next=）。不认这个 hash 的话，
    // 深链接用户登录后会掉到工作台、丢掉他本来要看的那一页。
    // 只在"确实没有 next=" 时兜底，有 next= 时行为完全不变。
    if (!nextTarget.raw) { nextTarget.raw = appRouteFromHash(win.location.hash || ""); }
    nextTarget.safe = safeNextTarget(nextTarget.raw);
    if (nextTarget.raw && el.nextLine) {
      show(el.nextLine, true);
      setText(el.nextTarget, nextTarget.safe || NEXT_DISPLAY_FALLBACK);
    }
  }

  function redirectTarget() {
    return nextTarget.safe || APP_HASH_FALLBACK;
  }

  /* ---------------------------------------------------------------
   * 十、启动：先问健康自检，再问"我是谁"
   * ------------------------------------------------------------- */
  function boot() {
    el.skipLink = doc.getElementById("skip-link");
    el.form = doc.getElementById("auth-form");
    el.username = doc.getElementById("username");
    el.usernameHint = doc.getElementById("username-hint");
    el.usernameError = doc.getElementById("username-error");
    el.displayName = doc.getElementById("display-name");
    el.password = doc.getElementById("password");
    el.passwordHint = doc.getElementById("password-hint");
    el.passwordError = doc.getElementById("password-error");
    el.passwordConfirm = doc.getElementById("password-confirm");
    el.passwordConfirmError = doc.getElementById("password-confirm-error");
    el.togglePassword = doc.getElementById("toggle-password");
    el.submit = doc.getElementById("submit");
    el.submitBusyText = doc.getElementById("submit-busy-text");
    el.submitReason = doc.getElementById("submit-reason");
    el.notice = doc.getElementById("notice");
    el.noticeText = doc.getElementById("notice-text");
    el.alert = doc.getElementById("alert");
    el.alertTitle = doc.getElementById("alert-title");
    el.alertDetail = doc.getElementById("alert-detail");
    el.alertRetry = doc.getElementById("alert-retry");
    el.envNotice = doc.getElementById("env-notice");
    el.envText = doc.getElementById("env-text");
    el.nextLine = doc.getElementById("foot-next");
    el.nextTarget = doc.getElementById("next-target");

    if (!el.form || !el.submit) { return; }

    readQuery();
    bindEvents();
    // 先把按钮锁上：在 /api/auth/me 回来之前，谁也不知道该发登录还是发初始化。
    applyMode(MODE.PENDING);
    loadState();
  }

  function loadState() {
    state.meFailed = false;
    state.healthFailed = false;
    show(el.alert, false);
    updateSubmitState();

    fetchJson(API.health, { method: "GET" }).then(function (outcome) {
      if (outcome.ok && outcome.payload && outcome.payload.ok) {
        state.healthData = outcome.payload.data || null;
        state.healthFailed = false;
      } else {
        state.healthFailed = true;
      }
      renderEnvNotice(state.healthData);
      if (state.healthFailed && !state.meFailed) {
        setAlert({
          kind: "warn",
          title: "服务自检未通过",
          detail: "读不到服务自检结果。这不影响账号本身，只是页面上少了一层服务状态说明。",
          actionLabel: "重试",
          action: loadState
        });
      }
    }, function () {
      state.healthFailed = true;
      renderEnvNotice(null);
      if (!state.meFailed) {
        setAlert({
          kind: "warn",
          title: "连不上服务",
          detail: "健康检查请求没有到达服务端：请确认服务已启动、端口正确。登录请求同样会失败，所以可以先点下面的重试。",
          actionLabel: "重试",
          action: loadState
        });
      }
    });

    fetchJson(API.me, { method: "GET" }).then(function (outcome) {
      handleMe(outcome);
    }, function (err) {
      // 网络层失败：这是"读不到账号状态"，按安全边界**不**默认显示创建管理员
      state.meFailed = true;
      setAlert({
        kind: "error",
        title: "读不到账号状态",
        detail: (err && err.kind === "timeout"
          ? "账号状态请求在 " + Math.round(REQUEST_TIMEOUT_MS / 1000) + " 秒内没有响应。"
          : "连不上服务，请确认服务已启动。")
          + "在确认真实状态之前，提交按钮会保持不可用 —— 服务异常时不能猜。",
        actionLabel: "重试",
        action: loadState
      });
      applyMode(MODE.LOGIN);    // 形态先按日常登录画（不显示"创建管理员"）
      state.meFailed = true;
      updateSubmitState();
    });
  }

  function handleMe(outcome) {
    if (outcome.status >= 500) {
      var info = readError(outcome.payload, DEFAULT_API_ERROR);
      state.meFailed = true;
      setAlert({
        kind: "error",
        title: "读不到账号状态",
        detail: "服务端返回" + codeSuffix(outcome.status, info.code) + "：" + info.message
          + "。无法确认这是首次部署还是日常登录，因此提交按钮保持不可用。",
        actionLabel: "重试",
        action: loadState
      });
      applyMode(MODE.LOGIN);
      state.meFailed = true;
      updateSubmitState();
      return;
    }

    if (outcome.status === 401 || outcome.status === 403) {
      // 401 是服务端在明确回答"你现在不是任何人的会话"，不是故障：
      // 但这时我们也拿不到 needs_bootstrap，所以要如实说出来，而不是假装知道。
      sessionHint = "服务端未返回初始化状态（响应码 " + outcome.status + "），已按日常登录处理；如果这是全新部署，请刷新页面重试。";
      applyMode(MODE.LOGIN);
      renderNotices(false);
      return;
    }

    if (!outcome.ok || !outcome.payload || outcome.payload.ok !== true) {
      var info2 = readError(outcome.payload, DEFAULT_API_ERROR);
      state.meFailed = true;
      setAlert({
        kind: "error",
        title: "读不到账号状态",
        detail: "账号状态接口返回了无法解析的结果" + codeSuffix(outcome.status, "")
          + "：" + info2.message + "。提交按钮保持不可用。",
        actionLabel: "重试",
        action: loadState
      });
      applyMode(MODE.LOGIN);
      state.meFailed = true;
      updateSubmitState();
      return;
    }

    var data = outcome.payload.data || {};
    if (data.authenticated === true) {
      // 已经登录的人不该看到登录表单（docs/ux-spec-final.md §2.8 S5）
      applyMode(MODE.LOGIN);
      // 再把形态临时标成 redirecting：CSS 据此把表单整块收起（见 login.css 同名规则）。
      // 复用 applyMode 是因为标题、字段提示、autocomplete 还得按登录态算好，
      // 只在最后改这一个属性 —— 切走前的这一瞬间不该露出半个表单等人输入。
      doc.documentElement.setAttribute("data-auth-mode", "redirecting");
      setNotice("已登录，正在返回…", false);
      updateSubmitState();
      // 900ms 而不是立即跳：这句提示是给"已经在别处登录、又点回登录页"的人看的，
      // 一闪而过等于没提示（用户测试里 400ms 时人眼几乎读不到一行中文）。
      win.setTimeout(function () {
        win.location.replace(redirectTarget());
      }, 900);
      return;
    }

    applyMode(data.needs_bootstrap === true ? MODE.BOOTSTRAP : MODE.LOGIN);
    renderNotices(false);
    // 首次部署时让焦点直接落在第一个可填字段上；日常登录不自动聚焦，
    // 免得手机上键盘一上来就把品牌区顶走。
    if (state.mode === MODE.BOOTSTRAP && el.username) {
      el.username.focus();
    }
  }

  /* ---------------------------------------------------------------
   * 十一、事件绑定
   * ------------------------------------------------------------- */
  function bindEvents() {
    el.form.addEventListener("submit", function (event) {
      event.preventDefault();
      submit();
    });

    // D-05：只要用户碰过表单（打字、离开字段、按提交），"请填写…"这类原因就有资格出现了。
    // blur 不冒泡，所以那一条要挂在捕获阶段，否则 el.form 上永远收不到。
    el.form.addEventListener("input", markInteracted);
    el.form.addEventListener("blur", markInteracted, true);

    // 失焦与提交时才判红；输入过程中只更新按钮的可用性（不弹错误）。
    ["username", "password", "confirm"].forEach(function (key) {
      var input = key === "username" ? el.username : (key === "password" ? el.password : el.passwordConfirm);
      if (!input) { return; }
      input.addEventListener("blur", function () {
        state.touched[key] = true;
        showFieldError(key, validateField(key));
        updateSubmitState();
      });
      input.addEventListener("input", function () {
        if (state.touched[key]) {
          showFieldError(key, validateField(key));
        }
        updateSubmitState();
      });
    });

    if (el.togglePassword && el.password) {
      el.togglePassword.addEventListener("click", function () {
        var shown = el.password.getAttribute("type") === "text";
        el.password.setAttribute("type", shown ? "password" : "text");
        // aria-pressed 表达"密码当前是可见的"，同时换 aria-label 让读屏知道下一步动作
        el.togglePassword.setAttribute("aria-pressed", shown ? "false" : "true");
        el.togglePassword.setAttribute("aria-label", shown ? "显示密码" : "隐藏密码");
        el.togglePassword.setAttribute("title", shown ? "显示密码" : "隐藏密码");
        // 焦点留在按钮上：键盘用户按完空格还要接着往下走
        el.togglePassword.focus();
      });
    }

    if (el.alertRetry) {
      el.alertRetry.addEventListener("click", function () {
        var action = state.alertAction;
        if (typeof action === "function") { action(); }
      });
    }

    // 页面隐藏时停掉倒计时：切走标签页还在后台每秒改 DOM 是白烧电，
    // 回到页面时按时间戳重新算剩余秒数（倒计时本身以服务端 Retry-After 为准）。
    doc.addEventListener("visibilitychange", function () {
      if (doc.hidden) {
        stopLockTimer();
      } else if (state.lockLeft > 0) {
        startLockTimer();
      }
    });
  }

  /* ---------------------------------------------------------------
   * 十二、提交
   * ------------------------------------------------------------- */
  function submit() {
    if (state.busy || state.lockLeft > 0 || state.meFailed || state.mode === MODE.PENDING) {
      updateSubmitState();
      return;
    }
    var firstBad = validateAll();
    if (firstBad) {
      var nodes = fieldErrorNodes(firstBad);
      if (nodes.input) { nodes.input.focus(); }
      updateSubmitState();
      return;
    }

    clearAlert();
    var isBootstrap = state.mode === MODE.BOOTSTRAP;
    setBusy(true, isBootstrap ? "正在创建管理员…" : "登录中…");

    // 请求体逐个字段手拼，**不用** new FormData(form)：
    // 确认密码（#password-confirm，name="password_confirm"）只是本地比对，
    // 后端 bootstrap 没有这个字段 —— 整表序列化会把它一起发出去。
    var body = {
      username: el.username.value.trim(),
      password: el.password.value
    };
    if (isBootstrap) {
      // display_name 是后端真实的可选字段（默认空串），空的时候干脆不发这个键
      var displayName = el.displayName ? el.displayName.value.trim() : "";
      if (displayName) { body.display_name = displayName; }
    }

    var url = isBootstrap ? API.bootstrap : API.login;
    fetchJson(url, { method: "POST", body: JSON.stringify(body) }).then(function (outcome) {
      setBusy(false);
      if (outcome.ok && outcome.payload && outcome.payload.ok === true) {
        onAuthSuccess(outcome.payload.data || {});
        return;
      }
      handleSubmitFailure(outcome);
    }, function (err) {
      setBusy(false);
      if (err && err.kind === "timeout") {
        setAlert({
          kind: "error",
          title: "服务没有及时响应",
          detail: "请求发出去了，但 " + Math.round(REQUEST_TIMEOUT_MS / 1000) + " 秒内没有回音。请确认服务是否卡住，然后重试。",
          actionLabel: "重试",
          action: submit
        });
      } else {
        setAlert({
          kind: "error",
          title: "连不上服务，请确认服务已启动",
          detail: "请求没有到达服务端（可能是服务未启动、端口不对或网络中断）。确认后再点重试。",
          actionLabel: "重试",
          action: submit
        });
      }
    });
  }

  function handleSubmitFailure(outcome) {
    var info = readError(outcome.payload, DEFAULT_API_ERROR);

    if (outcome.status === 401) {
      // 401 的标题直接用服务端原话（「用户名或密码不正确」，app/api/routes_auth.py）：
      // 不区分"用户不存在/密码错误"，否则这个接口就成了用户名枚举机。
      if (el.password) { el.password.value = ""; }     // 清空密码、保留用户名
      showFieldError("password", "");
      setAlert({
        kind: "error",
        title: info.message,
        detail: "用户名与密码不匹配。为避免用户名枚举，服务端不会说明具体是哪一项不对；连续多次失败会触发短时限制。"
      });
      // 焦点留在密码框（**不要**照抄 429 的"把焦点移进 alert"）：
      // 401 之后用户下一步就是重打密码，焦点跑回页面顶部反而多按几次 Tab；
      // 而 429 期间提交按钮被禁用、页面没有下一步可做，所以那边才把焦点交给 alert 标题。
      // 这两种处理是有意区分的（a11y-responsive 的实测口径也是按这个期望核的）。
      if (el.password) { el.password.focus(); }
      updateSubmitState();
      return;
    }

    if (outcome.status === 429) {
      var wait = retryAfterSeconds(outcome.res);
      beginLockout(wait > 0 ? wait : LOCK_FALLBACK_SECONDS, wait > 0);
      return;
    }

    if (outcome.status === 403 && state.mode === MODE.BOOTSTRAP) {
      // 首次部署的竞态：别人先建好了账号。这不是"服务器错误"，要给出出路。
      setAlert({
        kind: "warn",
        title: "这个部署已经有账号了",
        detail: "服务端拒绝再次初始化（账号库已经存在管理员）。请改用那个账号登录。",
        actionLabel: "切换到登录",
        action: function () {
          clearAlert();
          clearAllFieldErrors();
          applyMode(MODE.LOGIN);
          setNotice("这个部署已经有账号了，请直接登录。", true);
          if (el.username) { el.username.focus(); }
        }
      });
      updateSubmitState();
      return;
    }

    if (outcome.status === 422) {
      setAlert({
        kind: "error",
        title: info.message,
        detail: "服务端按它自己的规则拒绝了这次提交" + codeSuffix(outcome.status, info.code)
          + "。请检查用户名与密码后重试。"
      });
      updateSubmitState();
      return;
    }

    if (outcome.status >= 500) {
      // 5xx（含 503 服务不可用）：这一态没有"改一下输入再试"的出路，只能等或服务恢复，
      // 所以标题按运维口径写，重试按钮留在服务端原话之后。
      setAlert({
        kind: "error",
        title: "服务暂时不可用",
        detail: "请联系管理员或稍后重试。服务端原话：" + info.message
          + codeSuffix(outcome.status, info.code) + "。",
        actionLabel: "重试",
        action: submit
      });
      updateSubmitState();
      return;
    }

    // 其余 4xx：不吞掉服务端的原话与状态码——那是我们唯一能给出的线索
    setAlert({
      kind: "error",
      title: "请求被拒绝了",
      detail: "服务端原话：" + info.message + codeSuffix(outcome.status, info.code)
        + "。反复出现这一句时，请把它原样给维护的人看。",
      actionLabel: "重试",
      action: submit
    });
    updateSubmitState();
  }

  function onAuthSuccess() {
    clearAllFieldErrors();
    setNotice("登录成功，正在进入工作台…", false);
    setBusy(true, "正在进入…");
    var target = redirectTarget();
    // 用 replace 而不是 assign：登录页不该留在浏览历史里，用户按"后退"
    // 会回到一个必然再次跳走的页面（app.js:440 也是同样的选择）。
    win.location.replace(target);
  }

  /* ---------------------------------------------------------------
   * 十三、429 锁定：真实倒计时
   * ------------------------------------------------------------- */
  function beginLockout(seconds, fromHeader) {
    state.lockLeft = seconds;
    state.lockActive = false;
    // 标题只写一次：alert 是 aria-live=assertive，每秒重写会被读屏念满整轮锁定
    if (!state.lockActive) {
      state.lockActive = true;
      setAlert({
        kind: "warn",
        title: "登录尝试过于频繁",
        detail: "",
        actionLabel: "",
        action: null
      });
    }
    renderLockDetail(fromHeader);
    updateSubmitState();
    startLockTimer();
  }

  function renderLockDetail(fromHeader) {
    var text = "尝试次数过多，请在 " + state.lockLeft + " 秒后重试。"
      + (fromHeader
        ? "秒数取自服务端 Retry-After 响应头。"
        : "服务端这次没有给出 Retry-After，按 " + LOCK_FALLBACK_SECONDS + " 秒兜底。")
      + "等待期间提交按钮不可用；时间到会自动恢复。";
    setAlertDetail(text);
  }

  function startLockTimer() {
    stopLockTimer();
    state.lockTimer = win.setInterval(function () {
      state.lockLeft = Math.max(0, state.lockLeft - 1);
      if (state.lockLeft <= 0) {
        stopLockTimer();
        state.lockActive = false;
        setAlert({ kind: "info", title: "可以再次尝试了", detail: "等待时间已结束，现在可以重新提交登录。" });
        updateSubmitState();
        return;
      }
      renderLockDetail(true);
      updateSubmitState();
    }, 1000);
  }

  function stopLockTimer() {
    if (state.lockTimer) {
      win.clearInterval(state.lockTimer);
      state.lockTimer = 0;
    }
  }

  /* ---------------------------------------------------------------
   * 十四、入口
   * ------------------------------------------------------------- */
  // <script defer> 保证脚本执行时 DOM 已经解析完，所以这里直接跑，不必等事件。
  if (doc.readyState === "loading") {
    doc.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}());

/* =====================================================================
 * 十五、"记住账号"（方案 05 新增，追加在原有 IIFE 之后，不改动上面的登录逻辑）
 * ---------------------------------------------------------------------
 * 边界（如实声明，页面脚注里也写了）：
 *   - 只记住**用户名**，写在 localStorage 的一个键里；密码永不进客户端存储，
 *     也不写 Cookie、不写 sessionStorage。
 *   - 本逻辑不参与登录校验；登录成功与否完全由上面的 login.js 主流程决定，
 *     这里只负责"提交时存 / 取消勾选时清 / 载入时回填"。
 *   - localStorage 在隐私模式或存储被禁用时会抛异常：全部包在 try/catch 里，
 *     失败就等同于"没有记住账号"，不打扰用户。
 * 风格沿用本文件的 ES5 写法（var + function），不为一个小功能引入新语法。
 * ===================================================================== */
(function () {
  "use strict";

  var KEY = "marketlens:last-username"; // 命名空间前缀，避免和同源其他键冲突
  var doc = document;
  var docEl = doc.documentElement;
  var storage = null;                   // 惰性探测：不可用时保持 null

  try {
    storage = window.localStorage;
    // iOS Safari 隐私模式：对象存在但一写就抛，所以真正探一次。
    var probeKey = "marketlens:storage-probe";
    storage.setItem(probeKey, "1");
    storage.removeItem(probeKey);
  } catch (err) {
    storage = null;
  }

  function readSaved() {
    if (!storage) {
      return "";
    }
    try {
      var value = storage.getItem(KEY);
      return typeof value === "string" ? value : "";
    } catch (err) {
      return "";
    }
  }

  function writeSaved(username) {
    if (!storage) {
      return;
    }
    try {
      storage.setItem(KEY, username);
    } catch (err) {
      /* 存不进就算了：功能降级为"没记住"，不影响登录。 */
    }
  }

  function clearSaved() {
    if (!storage) {
      return;
    }
    try {
      storage.removeItem(KEY);
    } catch (err) {
      /* 同上，静默降级。 */
    }
  }

  /* 只有登录/待定形态才回填用户名；首次部署要新建管理员，回填旧用户名只会误导。 */
  function isLoginShape() {
    var mode = docEl.getAttribute("data-auth-mode");
    return mode === "login" || mode === "pending";
  }

  function fillUsername() {
    if (!isLoginShape()) {
      return;
    }
    var input = doc.getElementById("username");
    var checkbox = doc.getElementById("remember-account");
    var saved = readSaved();
    if (!input || !saved) {
      return;
    }
    // 只填空输入框：如果用户/浏览器已经填了（含密码管理器自动填充），不覆盖。
    if (input.value === "") {
      input.value = saved;
    }
    if (checkbox) {
      checkbox.checked = true;
    }
  }

  function onFormSubmit() {
    var checkbox = doc.getElementById("remember-account");
    var input = doc.getElementById("username");
    if (!checkbox || !input) {
      return;
    }
    if (!isLoginShape()) {
      return; // 首次部署不记忆"账号"
    }
    var username = input.value.replace(/^\s+|\s+$/g, "");
    if (checkbox.checked && username) {
      writeSaved(username);
    } else {
      clearSaved();
    }
  }

  function onCheckboxChange() {
    var checkbox = doc.getElementById("remember-account");
    if (checkbox && !checkbox.checked) {
      clearSaved(); // 取消勾选 = 立刻忘掉，不等下一次提交
    }
  }

  function bind() {
    var form = doc.getElementById("auth-form");
    var checkbox = doc.getElementById("remember-account");
    if (form) {
      form.addEventListener("submit", onFormSubmit, false);
    }
    if (checkbox) {
      checkbox.addEventListener("change", onCheckboxChange, false);
    }

    // 形态由主流程切（pending → login/bootstrap），回填要等形态定了再做。
    if (window.MutationObserver) {
      var observer = new window.MutationObserver(fillUsername);
      observer.observe(docEl, { attributes: true, attributeFilter: ["data-auth-mode"] });
    }
    fillUsername();
  }

  if (doc.readyState === "loading") {
    doc.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
}());

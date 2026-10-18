"""Wave B「指挥中心」新结构的防倒退守卫（Wave C 新增，2026-10-17）。

`tests/test_frontend.py` 钉住的是重构前就存在的东西（17 个旧 id、步骤文案、
接口路径字面量、按钮对比度）。Wave B 把顶栏改成命令条、侧栏改成 3 组 6 项、
新增战略态势带 / 阶段阶梯 / 中文映射兜底 / 设置页改密与会话——**这些新结构
一条都没被钉住**，本文件补上。

判据来源：
- `docs/refactor/02-structure-contract.md`（结构契约 §1/§2/§5/§6/§7/§10/§11）
- `docs/refactor/06-leader-decisions.md`（裁定 D-1..D-12，特别是 D-4/D-5/D-6/D-9/D-10）

这些用例全部是**静态文件扫描**（读 `app/web/*` 文本 + 解析 CSS 规则），
不依赖 TestClient —— 它们防的是"结构改名/入口回潮/枚举裸奔"这类
肉眼看不出来的倒退。断言风格（读文件、正则切片、`:root` hex 对比度计算）
沿用 `tests/test_frontend.py` 的既有机制。

⚠️ 本文件有**明知会红的用例**（docstring 里标了「当前红」）：那是代码真缺陷
的取证，按主控要求保留红灯，不许为了让数字好看而放宽断言。
"""

import re
from pathlib import Path
from typing import get_args

import pytest

from app import config
from app.schemas_workflow import TaskStatus, ValidationIssue, WorkflowStatus

WEB_DIR = config.PROJECT_ROOT / "app" / "web"

# 与 test_frontend.py 的白名单同源；这里写死是因为本文件的视觉纪律扫描要覆盖**全部七件**
# （test_frontend 的外部链接扫描只覆盖 index.html/app.css/app.js 三件，login.* 与 tokens.css
# 是 Wave B 重写的新文件，恰恰是最容易"顺手引个 CDN"的地方）。
ALL_WEB_FILES = (
    "app.css",
    "app.js",
    "index.html",
    "login.css",
    "login.html",
    "login.js",
    "tokens.css",
)

# 方案 01 的定义性禁区：这些"入口"要么后端没有对应接口，要么违反契约（无导出/下载/打印），
# 一旦以**可点击元素**的形态回潮就是假功能。扫的是 button/a/select 的标签与文案，
# 不是全文件文本——设置页横幅里"还没有数据集删除、报告导出"这类**否定说明**是实话，
# 不是入口，不许误判成违规（提示词明确要求区分这一点）。
FORBIDDEN_ENTRY_TERMS = (
    "导出",
    "下载",
    "打印",
    "删除",
    "重命名",
    "通知",
    "保存视图",
    "协作",
    "成本模型",
    "取消",
    "重跑",
)

SIDEBAR_VIEW_KEYS = {"command", "dataset", "analysis", "runs", "report", "history"}


# ------------------------------------------------------------------ 文本工具


def _read(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def _strip_html_comments(html: str) -> str:
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


def _strip_js_comments(src: str) -> str:
    """去掉 `/* */` 与 `//` 注释，但**字符串字面量原样保留**。

    为什么字符串必须保留：被禁文案（按钮 label、`issue.code + "："` 这类上屏拼接）
    恰恰活在字符串里；而注释里的"不放导出、分享、删除"是设计说明，不是入口。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in ("\"", "'"):
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    break
                j += 1
            out.append(src[i:min(j + 1, n)])
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _slice_between(text: str, start: str, ends: tuple[str, ...]) -> str:
    """从 start 标记切到最先出现的结束标记；end 找不到就切到文件尾。"""
    s = text.index(start)
    stop = len(text)
    for e in ends:
        j = text.find(e, s + len(start))
        if j >= 0:
            stop = min(stop, j)
    return text[s:stop]


_FUNC_START_RE = re.compile(r"\n  (?:async )?function ")


def _js_func(js: str, name: str) -> str:
    """取一个顶层缩进（两空格）函数的函数体文本。"""
    sig = js.index("function " + name)
    m = _FUNC_START_RE.search(js, sig + 10)
    nxt = m.start() if m else len(js)
    # 再吃掉下一个区块注释头（函数后面常紧跟注释，不属于函数）。
    tail = js.find("\n  /*", sig + 10)
    if 0 <= tail < nxt:
        nxt = tail
    return js[sig:nxt]


def _js_table_keys(js: str, decl: str) -> list[str]:
    """把 `var X = { ... };` 的键列表抽出来（按声明顺序，去重）。"""
    block = _slice_between(js, decl, ("\n  };",))
    return re.findall(r'^\s*([A-Za-z_][\w-]*)\s*:', block, re.M)


def _views_keys(js: str) -> list[str]:
    block = _slice_between(js, "var VIEWS = {", ("\n  };",))
    return re.findall(r'^    ([a-z_]+):\s*\{', block, re.M)


# ---------------------------------------------------------------- 侧栏与命令条


def _nav_block(html: str) -> str:
    m = re.search(r'<nav class="nav"[^>]*>(.*?)</nav>', html, re.S)
    assert m is not None, "index.html 里找不到侧栏 <nav class=\"nav\">"
    return m.group(1)


def _cmdbar_block(html: str) -> str:
    m = re.search(r'<header class="cmdbar">(.*?)</header>', html, re.S)
    assert m is not None, "index.html 里找不到 <header class=\"cmdbar\">"
    return m.group(1)


def test_sidebar_has_three_groups_of_six_items() -> None:
    """侧栏必须恰好是 3 组 6 项（指挥/分析/成果 × 每组 2 项），契约 §1.2。"""
    html = _strip_html_comments(_read("index.html"))
    nav = _nav_block(html)

    wraps = re.findall(r'<div class="nav__group-wrap"', nav)
    assert len(wraps) == 3, f"侧栏分组数 {len(wraps)}，契约规定 3 组"

    titles = re.findall(r'class="nav__group"[^>]*>([^<]+)<', nav)
    assert titles == ["指挥", "分析", "成果"], f"分组标题必须是 指挥/分析/成果，实际 {titles}"

    groups = re.split(r'<div class="nav__group-wrap"', nav)[1:]
    for index, group in enumerate(groups):
        items = re.findall(r'class="nav__item"', group)
        assert len(items) == 2, f"第 {index + 1} 组有 {len(items)} 项，契约规定每组 2 项"

    items = re.findall(r'<a class="nav__item"[^>]*href="#/([a-z_]+)"[^>]*data-view="([a-z_]+)"', nav)
    assert len(items) == 6, f"侧栏导航项 {len(items)} 个，契约规定 6 项"
    for href_view, data_view in items:
        assert href_view == data_view, f"href #/{href_view} 与 data-view={data_view} 漂移"


def test_nav_and_sections_stay_in_sync_with_js_view_registry() -> None:
    """导航/视图容器与 app.js 的 VIEWS、VIEW_GROUP 三处必须同集合，防两边漂移。

    结构说明（契约 §0.1 + Wave B 落地）：VIEWS 是 8 键 —— 侧栏 6 项
    （command/dataset/analysis/runs/report/history）+ 不在侧栏的 2 个视图
    （workbench 走首屏「下一步」链接、settings 走命令条账户菜单）。
    判据：侧栏 ⊆ VIEWS；`section.view[data-view]` 的集合 **全等于** VIEWS 键集；
    VIEW_GROUP 的键集也全等于 VIEWS（注释里那句"与 .nav__group 三段标题一致"
    靠 VIEW_GROUP 值 ∈ {指挥,分析,成果,账户} 钉住）。
    """
    html = _strip_html_comments(_read("index.html"))
    js = _read("app.js")

    nav_views = set(re.findall(r'class="nav__item"[^>]*data-view="([a-z_]+)"', _nav_block(html)))
    assert nav_views == SIDEBAR_VIEW_KEYS, f"侧栏 data-view 集合漂移：{sorted(nav_views)}"

    section_views = set(
        re.findall(r'<section class="view" id="view-[a-z_]+" data-view="([a-z_]+)"', html)
    )
    for sid, dview in re.findall(r'<section class="view" id="view-([a-z_]+)" data-view="([a-z_]+)"', html):
        assert sid == dview, f"视图容器 id={sid} 与 data-view={dview} 不一致（app.js 的 view-<key> 规则）"

    views = _views_keys(js)
    assert set(section_views) == set(views), (
        f"视图容器 {sorted(section_views)} 与 VIEWS 键集 {sorted(views)} 漂移——"
        "applyView 会让其中一个集合永远 hidden（点不进去的视图）"
    )
    assert nav_views <= set(views), f"侧栏出现了 VIEWS 之外的视图：{sorted(nav_views - set(views))}"
    assert {"command", "runs"} <= set(views), "VIEWS 必须包含 Wave B 新增的 command 与 runs"

    groups = _js_table_keys(js, "var VIEW_GROUP = {")
    assert set(groups) == set(views), f"VIEW_GROUP {sorted(groups)} 与 VIEWS {sorted(views)} 不同集"
    group_values = set(re.findall(r'^\s*[a-z_]+: "([^"]+)"', _slice_between(js, "var VIEW_GROUP = {", ("\n  };",)), re.M))
    assert group_values <= {"指挥", "分析", "成果", "账户"}, f"VIEW_GROUP 出现了第四种分组标题：{group_values}"

    assert '.view[data-view]' in js and '.nav__item[data-view]' in js, (
        "applyView 的选择器语义（契约 §0.2）不许改名"
    )


def test_new_command_center_ids_coexist_with_legacy_required_ids() -> None:
    """Wave B 新结构的锚点 id 与被钉住的 17 个旧 id 必须**同时**存在。

    旧 id 由 test_frontend.REQUIRED_IDS 钉着；这里从同一张表取，防止两处清单漂移。
    缺任何一个新 id，app.js 的 `$("…")` 就会静默拿到 null（本项目栽过的最隐蔽的坑）。
    """
    from tests.test_frontend import REQUIRED_IDS

    html = _read("index.html")
    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in html, f"旧 id={element_id} 被搬丢了"

    new_ids = (
        "frame", "sidebar", "nav-toggle", "cmdbar-title", "cmdbar-asof", "cmdbar-note",
        "cmdbar-health", "health-text", "user-btn", "user-popover",
        "posture", "posture-body", "posture-asof-note",
        "metrics-grid", "metric-datasets", "metric-running", "metric-reports", "metric-failed",
        "ladder", "ladder-count", "view-runs", "badge-datasets", "badge-runs",
    )
    for element_id in new_ids:
        assert f'id="{element_id}"' in html, f"Wave B 新结构缺少 id={element_id}（app.js 会拿到 null 并静默降级）"


def test_command_bar_holds_no_navigation_or_global_search() -> None:
    """命令条里不许出现导航 `<nav>`、不许出现任何全局搜索框 —— 方案 01 的定义性区别。

    契约 §2.1：06 与 01 的关键区别就是"顶栏是不是一级导航"。这条一旦破，
    方案 01 就退化成方案 06，而页面看上去仍然"正常"，肉眼发现不了。
    页内搜索 `#dataset-search` 在 main 里（数据资产视图），不在此列（契约 §2.1 明文豁免）。
    """
    html = _strip_html_comments(_read("index.html"))
    bar = _cmdbar_block(html)

    assert "<nav" not in bar, "命令条里出现了 <nav> —— 命令条不是导航（契约 §2.1，方案 01 与 06 的分界线）"
    assert 'role="search"' not in bar, "命令条里出现了 role=\"search\" 的搜索地标"
    assert 'type="search"' not in bar, "命令条里出现了搜索输入框 —— 全局搜索后端不存在，这是假功能"

    assert html.count("<nav") == 1, "全页只允许侧栏一个 <nav>，且它必须在侧栏"
    assert '<nav class="nav"' in html, "唯一的那个 <nav> 必须是侧栏主导航"


def test_settings_entry_lives_in_account_popover() -> None:
    """侧栏没有「设置」项，设置唯一入口是命令条账户浮层（契约 §2.2 不许丢）。"""
    html = _strip_html_comments(_read("index.html"))
    bar = _cmdbar_block(html)

    assert 'href="#/settings"' in bar, "命令条账户浮层里的设置入口（.userpop__settings）被删了 —— 设置视图将无法到达"
    assert 'data-view="settings"' not in _nav_block(html), "侧栏不该有设置项（3 组 6 项是定稿）"
    assert 'id="view-settings"' in html, "settings 视图容器必须还在（VIEWS 键与深链 #/settings 依赖它）"


# ---------------------------------------------------------------- 禁止入口


def _clickable_surfaces(html: str) -> list[str]:
    """返回每个可点击元素的「开标签属性 + 去掉子标签后的文案」。"""
    surfaces = []
    for m in re.finditer(r"<(button|a|select)\b([^>]*)>(.*?)</\1>", html, re.S):
        inner = re.sub(r"<[^>]+>", " ", m.group(3))
        surfaces.append(m.group(2) + " " + inner)
    return surfaces


def test_no_forbidden_entry_points_in_clickable_elements() -> None:
    """index.html 的**可点击元素**里不许出现禁止入口（导出/下载/打印/删除/重命名/
    通知铃铛/保存视图/协作/成本模型选择器/取消或重跑）。

    扫描面故意收窄到 button/a/select：设置横幅那段「还没有数据集删除、报告导出」是
    **否定说明**（实话），不是入口；把两者混为一谈的断言会把正确的文案改成撒谎。
    """
    html = _strip_html_comments(_read("index.html"))
    surfaces = _clickable_surfaces(html)
    assert surfaces, "一个可点击元素都没扫到 —— 提取正则失效，本条必须视为红"

    for surface in surfaces:
        for term in FORBIDDEN_ENTRY_TERMS:
            assert term not in surface, f"出现了禁止入口（含文案「{term}」）：{surface[:120]!r}"


def test_no_forbidden_entry_terms_in_app_js() -> None:
    """app.js（去注释后）不许凭空造出禁止入口的文案。

    注释里写"不放导出、分享、删除"是契约引用，不算入口，所以先剥注释。
    """
    js = _strip_js_comments(_read("app.js"))
    for term in FORBIDDEN_ENTRY_TERMS:
        assert term not in js, f"app.js 代码里出现禁止入口文案「{term}」"


def test_download_icon_is_defined_but_never_used() -> None:
    """sprite 里的 #i-download 可以保留定义（契约 §7.2），但任何地方都不许引用它。"""
    html = _strip_html_comments(_read("index.html"))
    js = _strip_js_comments(_read("app.js"))
    assert 'id="i-download"' in html
    assert 'href="#i-download"' not in html, "#i-download 被 use 了 —— 导出/下载回潮"
    assert 'icon("download")' not in js, "app.js 用 ICON_NAMES 白名单画出了下载图标"


# ---------------------------------------------------------------- 阶段耗时红线


def test_ladder_reads_only_progress_counts() -> None:
    """阶段阶梯只读 step/status（换算成 current）与 step_index/total_steps。

    红线来源：契约 §6.3 / option-01 §9 —— 后端没有任何阶段级耗时字段，
    出现"按时间推算的百分比"就是编数据。同步点：
      - `syncLadder` 只改类名/aria-label/#ladder-count，不碰宽度、不碰时间；
      - 渲染状态行的字段只允许 step_index / total_steps（D-10 修的那一行）。
    """
    js = _strip_js_comments(_read("app.js"))

    sync = _js_func(js, "syncLadder")
    assert sync, "找不到 syncLadder —— 阶梯与 #steps 的同步点消失了"
    for banned in ("Date", "elapsed", "style", "width", "%", "parseInt"):
        assert banned not in sync, f"syncLadder 里出现了 {banned!r} —— 阶梯只许改类名与计数文字"
    assert "is-done" in sync and "is-run" in sync and "is-skip" in sync, (
        "契约 §6.2 的三态类名 is-done/is-run/is-skip 不许另造第三套词"
    )

    status_slice = _slice_between(js, "zhEnumNode(WORKFLOW_STATUS_TEXT, data.status", ("var elapsed = $(\"elapsed\")",))
    assert "step_index" in status_slice and "total_steps" in status_slice, (
        "轮询状态行的进度数字必须来自 step_index/total_steps（契约 D-10）"
    )
    for banned in ("阶段耗时", "stageDuration", "stepDuration", "stage_duration", "step_duration", "duration_ms"):
        assert banned not in js, f"app.js 里出现了阶段耗时字段 {banned}"

    css = _strip_css_comments(_read("app.css"))
    for banned in ("阶段耗时", "stage-duration"):
        assert banned not in css, f"app.css 里出现了阶段耗时渲染 {banned}"


# ---------------------------------------------------------------- 中文映射兜底


def test_enum_maps_cover_the_backend_vocabularies_exactly() -> None:
    """映射表与后端枚举逐值互等（契约 §6.3 末条 + D-10）。

    - WORKFLOW_STATUS_TEXT == WorkflowStatus 全部 10 值；
    - TASK_STATUS_TEXT == TaskStatus 全部 6 值（含 SKIPPED）；
    - STEP_NAMES ⊇ STEP_ORDER 六阶段 + 第 7 个值 done（orchestrator 收尾态，
      D-10 点名"没有它界面就逐秒画裸 done"）；
    - STEP_STATE_TEXT 五词齐全（颜色不许是唯一载体）；
    - severity 三档在 app.css 里三色三类都有落点。
    """
    js = _read("app.js")

    workflow_keys = set(_js_table_keys(js, "var WORKFLOW_STATUS_TEXT = {"))
    assert workflow_keys == {s.value for s in WorkflowStatus}, (
        f"WORKFLOW_STATUS_TEXT 与后端 WorkflowStatus 漂移：表侧缺 {sorted({s.value for s in WorkflowStatus} - workflow_keys)}，"
        f"多 {sorted(workflow_keys - {s.value for s in WorkflowStatus})}"
    )

    task_keys = set(_js_table_keys(js, "var TASK_STATUS_TEXT = {"))
    assert task_keys == {s.value for s in TaskStatus}, f"TASK_STATUS_TEXT 与 TaskStatus 漂移：{sorted(task_keys)}"
    assert "SKIPPED" in task_keys

    step_name_keys = set(_js_table_keys(js, "var STEP_NAMES = {"))
    m = re.search(r'const STEP_ORDER = \[(.*?)\];', js, re.S)
    assert m is not None
    step_order = set(re.findall(r'"([A-Za-z_]+)"', m.group(1)))
    assert step_order <= step_name_keys, f"STEP_NAMES 少了阶段：{sorted(step_order - step_name_keys)}"
    assert "done" in step_name_keys, "STEP_NAMES 必须覆盖第 7 个 step 值 done（D-10）"
    assert "done" not in step_order, "done 不是六阶段之一，不许混进 STEP_ORDER"

    state_keys = set(_js_table_keys(js, "var STEP_STATE_TEXT = {"))
    assert state_keys == {"waiting", "active", "done", "failed", "skipped"}, f"STEP_STATE_TEXT 五词漂移：{sorted(state_keys)}"

    css = _read("app.css")
    severities = set(get_args(ValidationIssue.model_fields["severity"].annotation))
    assert severities == {"error", "warning", "info"}
    for sev in sorted(severities):
        assert f".issue--{sev}" in css, f"app.css 缺少 severity 档位 .issue--{sev}"
        assert f".sev--{sev}" in css, f"app.css 缺少 severity 档位 .sev--{sev}"


def test_unknown_enum_falls_back_to_chinese_with_visible_origin() -> None:
    """映射表未命中时的兜底：中文主显（「未识别的…」）+ 原值进可见的 .origin。

    D-9 的判据是"中文主显 + 原值可查证"；未命中回落到英文原值 = 撒谎变露馅，
    回落到"猜一个中文" = 撒谎，两者都要拦。
    """
    js = _strip_js_comments(_read("app.js"))
    zh = _js_func(js, "zhEnum")
    assert '"未识别的" + label' in zh, "zhEnum 的未命中兜底必须先是中文，再不是回落原值"
    node = _js_func(js, "zhEnumNode")
    assert '"origin"' in node, "未命中的原值必须显形在 .origin 小字里（触屏没有 hover，只放 title 等于藏起来）"
    assert "node.title" in node, "原值同时要有 title 供核对"


def test_no_raw_enum_fallback_to_screen() -> None:
    """`|| 原值` 直接把后端枚举上屏的写法不得存在（D-9 的红线）。"""
    js = _strip_js_comments(_read("app.js"))

    patterns = (
        r"\|\|\s*(?:data|item|task|row|report|result)\.(status|step|severity|dtype)\b",
        r"createTextNode\(\s*(?:data|item|task|row|report)\.(status|step|severity)\s*\)",
        r'textContent = (?:data|item|task|row|report)\.(status|step|severity)\b',
    )
    for pattern in patterns:
        hit = re.search(pattern, js)
        assert hit is None, f"枚举原值直接上屏的写法回潮：{hit.group(0) if hit else ''!r}"

    # 逐处接法：轮询状态行与任务行必须走 zhEnumNode，不许退回字符串拼接。
    assert "zhEnumNode(WORKFLOW_STATUS_TEXT, data.status" in js
    assert "zhEnumNode(STEP_NAMES, data.step" in js
    assert "zhEnumNode(TASK_STATUS_TEXT, task.status" in js
    assert "zhEnum(DTYPE_TEXT" in js, "列类型中文兜底（D-10 的 :1974 真漏点）必须还在"


def test_validation_ok_banner_must_use_issue_mapping() -> None:
    """【当前红 · 代码缺陷】校验通过的横幅把 warning 的英文码顶在句首当主显。

    `renderValidation` 未通过分支走的是 `validationIssueLine`（中文主显 + 原值可查证，
    符合 D-9），但通过分支（约 app.js:3895）写的是
    `el("p", null, issue.code + "：" + issue.message)` —— `UNKNOWN_COLUMN：…`
    这种英文枚举原值直接顶屏，正是 D-9 禁止的形态。
    保留红灯，等主控裁定（修法预期：通过分支也走 validationIssueLine）。
    """
    js = _strip_js_comments(_read("app.js"))
    render = _js_func(js, "renderValidation")
    assert 'issue.code + "' not in render, (
        "校验横幅把英文问题码直接拼上屏（D-9 违规）。"
        "warning 行必须改走 validationIssueLine（VALIDATION_ISSUE_TEXT 中文主显 + 原值进 .origin）。"
    )


# ---------------------------------------------------------------- 表头


def test_render_table_emits_raw_col_and_display_label() -> None:
    """renderTable 必须同时产出原始列名（data-col）与展示名（th 文本 + td data-label）。

    契约 §5.2：th[data-col]=后端原始键（永不显示），th 文本=展示名，
    td[data-label]=展示名（窄屏 ::before 消费）。少一边：要么 CSS/测试失去定位点，
    要么窄屏堆叠出来的行没有标签。
    """
    js = _strip_js_comments(_read("app.js"))

    cell = _js_func(js, "gridColumnCell")
    assert '"data-col"' in cell, "th 丢了 data-col 原始列名"
    assert 'setAttribute("scope", "col")' in cell

    table = _js_func(js, "renderTable")
    assert "gridColumnCell(" in table, "renderTable 不走统一表头构造 —— data-col/data-label 契约失去单一实现点"
    assert '"data-col"' in table and '"data-label"' in table, "td 必须同时带原始键与展示名"
    assert 'table.className = "dtable"' in table


def test_display_column_never_translates_user_chinese_columns() -> None:
    """展示名解析顺序：非 ASCII 键**原样返回**，先于任何查表。

    用户自己的中文列名不许被机器翻译/改写（D-9：不猜语义）；
    COLUMN_LABELS 只收后端自产的结构键，所以键必须全 ASCII。
    """
    js = _strip_js_comments(_read("app.js"))
    display = _js_func(js, "displayColumn")

    guard = re.search(r"/\[\^\\x00-\\x7F\]/\.test\(key\)", display)
    assert guard is not None, "displayColumn 少了非 ASCII 早退 —— 中文列名会被后面的规则改写"
    lookup = display.find("COLUMN_LABELS")
    assert guard.start() < lookup, "非 ASCII 早退必须在 COLUMN_LABELS 查表**之前**"

    labels = _slice_between(js, "var COLUMN_LABELS = {", ("\n  };",))
    for key in re.findall(r'^\s*([A-Za-z_][\w-]*)\s*:', labels, re.M):
        assert key.isascii(), f"COLUMN_LABELS 混进了非 ASCII 键 {key} —— 那张表只收后端结构键"


# ---------------------------------------------------------------- 态势带取数


def test_posture_fetch_contract() -> None:
    """态势带取数（D-6）：只走归档接口、只读、请求预算 ≤ 4 个 GET。

    - 列表：`API.history + "?limit=12&offset=0"` 一次；
    - 正文：`GET /api/workflows/history/{id}`（模板字符串字面量要钉住，这是
      "恒 200、读 SQLite、重启不 404"的接口选择本身）；
    - 逐条正文最多 3 个（POSTURE_BODY_BUDGET=3，Math.min 硬上限）→ 总共 ≤4；
    - 不许出现 API.report / API.workflow（内存注册表 + 会写状态的接口）；
    - 不许带 method（一律 GET）；
    - has_report===true 且状态在 TERMINAL 合格集内才请求正文。
    """
    js = _strip_js_comments(_read("app.js"))

    assert "historyDetail: (id) => `/api/workflows/history/${id}`" in js, (
        "归档详情接口的模板字符串被改 —— D-6 定的取数入口本身"
    )

    consts = _slice_between(_read("app.js"), "战略态势带取数", ("function postureTime",))  # 标记在注释头里，取原文
    assert "POSTURE_MAX_ROWS = 3;" in consts
    assert "POSTURE_BODY_BUDGET = 3;" in consts
    assert re.search(r"POSTURE_ELIGIBLE_STATUS = \[[^\]]*\];", consts), "合格状态集必须还在"

    hist = _js_func(js, "fetchPostureHistory")
    assert 'API.history + "?limit=" + POSTURE_HISTORY_LIMIT + "&offset=0"' in hist
    assert "method" not in hist

    body = _js_func(js, "fetchPostureBody")
    assert "api(API.historyDetail(" in body
    for banned in ("API.report", "API.workflow(", "method"):
        assert banned not in body, f"态势带正文取了禁止接口或带了写语义：{banned}"

    load = _js_func(js, "loadPosture")
    for banned in ("API.report", "API.workflow(", 'method: "POST"'):
        assert banned not in load, f"loadPosture 里出现了禁止调用：{banned}"
    assert "Math.min(POSTURE_BODY_BUDGET" in load, "逐条取正文必须有 3 的硬预算"

    eligible = _js_func(js, "postureEligible")
    assert "has_report === true" in eligible, "has_report=false 的记录不许被请求正文"


def test_report_endpoint_stays_single_call_site() -> None:
    """态势带不许给报告接口开第二处调用点（契约 §11#9 的字面量计数）。

    test_frontend 钉的是 `API.report(` 恰好一次；这里补上态势带一侧的因果：
    首屏正文走的是归档接口，而不是把 report 调用挪进来又数一遍。
    """
    js = _read("app.js")
    assert js.count("API.report(") == 1
    site = js.index("API.report(")
    fn_start = js.index("function loadReport")
    fn_end = _FUNC_START_RE.search(js, fn_start + 10)
    end = fn_end.start() if fn_end else len(js)
    assert fn_start < site < end, (
        "API.report 的唯一调用点不在 loadReport 里 —— 态势带（或别的区域）私自开了第二处取数口，D-6 违规"
    )


# ---------------------------------------------------------------- 设置页


def test_password_request_body_carries_no_session_material() -> None:
    """改密请求体只允许 {current_password, new_password}，不许携带任何会话材料。

    会话凭据活在 HttpOnly cookie 里；把 token 塞进 body 等于亲手把凭据复制进
    前端内存/请求日志（D-9"不在浏览器里留副本"的同一口径）。
    """
    js = _strip_js_comments(_read("app.js"))
    fn = _js_func(js, "changePassword")
    assert fn

    m = re.search(r"body: JSON\.stringify\(\{([^}]*)\}\)", fn)
    assert m is not None, "changePassword 没有发 JSON body —— 契约被改，请回 docs/refactor 核对"
    keys = set(re.findall(r'(\w+):', m.group(1)))
    assert keys == {"current_password", "new_password"}, f"改密请求体键集漂移：{sorted(keys)}"

    for banned in ("token", "session_id", "cookie"):
        assert banned not in fn.lower(), f"改密逻辑里出现了会话材料 {banned!r}"


def test_sessions_view_renders_metadata_only() -> None:
    """`/api/auth/sessions` 的渲染只许出现元信息，不许出现 token 字段名。

    后端库里存的是哈希、接口从来不发 token；前端一旦渲染 `row.token`，
    说明接口被改成了发凭据——那是安全缺陷，不是样式问题。
    """
    js = _strip_js_comments(_read("app.js"))
    fn = _js_func(js, "loadSessions")
    assert fn

    assert "token" not in fn.lower(), "会话列表渲染触碰了 token 字段名"
    for field in ("created_at", "last_seen_at", "expires_at", "current"):
        assert field in fn, f"会话列表少了元信息字段 {field} 的渲染"
    assert "API.sessions" in fn and 'method' not in fn, "会话列表只读 GET"


def test_settings_password_and_session_dom_contract() -> None:
    """【当前红 · 代码缺陷】设置页改密与会话卡片：app.js 引用了 DOM，index.html 里没有。

    app.js 的 changePassword()/loadSessions()/事件绑定引用了
    password-state / password-save / pwd-current / pwd-new / pwd-new2 /
    session-list / session-state / session-refresh 八个 id，但 Wave B 重写后的
    index.html 设置视图里**一个都没有**（页面上仍写"本页只读"）。
    结果是 VIEWS.settings.sub 承诺"可以改自己的密码、可以看会话"，
    界面上却没有任何对应表单——按钮不存在，监听器挂不上，功能是假的。
    保留红灯等主控裁定（修法预期：index.html 补两张卡，或删掉 app.js 的半截实现）。
    """
    html = _read("index.html")
    js = _read("app.js")
    settings_ids = (
        "password-state", "password-save", "pwd-current", "pwd-new", "pwd-new2",
        "session-list", "session-state", "session-refresh",
    )
    for element_id in settings_ids:
        assert f'$("{element_id}")' in js, f"内部不一致：app.js 不再引用 {element_id}，请同步删掉本断言"
    missing = [element_id for element_id in settings_ids if f'id="{element_id}"' not in html]
    assert not missing, (
        f"app.js 引用了这些设置页元素但 index.html 里没有：{missing} —— "
        "改密与会话功能在界面上不可达（$() 拿到 null，静默死代码）"
    )


# ---------------------------------------------------------------- 视觉纪律 / 令牌


def test_visual_discipline_across_all_seven_files() -> None:
    """`app/web` 全部七件文件的视觉硬约束：
    无 backdrop-filter / text-shadow / @font-face / @import at-rule / 任何绝对网址。

    test_frontend.py 的外链扫描只覆盖 index.html/app.css/app.js 三件；
    login.* 与 tokens.css 是 Wave B 重写的文件，这里补全（D-11 改了 login 三件，
    最容易"顺手引个 CDN 字体"）。`@import` 只禁 at-rule 形态
    （`@import url(`/`@import "`），注释里"不要用 @import"这句提醒不算。
    """
    for name in ALL_WEB_FILES:
        text = _read(name)
        assert "backdrop-filter" not in text, f"{name} 出现 backdrop-filter（毛玻璃是方案 01 明令禁止的装饰）"
        assert "text-shadow" not in text, f"{name} 出现 text-shadow"
        assert "@font-face" not in text, f"{name} 出现 @font-face（引外部字体=离线不可演示）"
        assert re.search(r"@import\s*(url\(|[\"'])", text) is None, f"{name} 出现 @import at-rule"
        assert "http://" not in text and "https://" not in text, f"{name} 出现绝对网址（离线硬约束）"


def _root_decls(css_name: str) -> dict[str, str]:
    """解析第一个 `:root { … }` 块里的 `--name: value;`（去掉块内注释）。"""
    css = _read(css_name)
    m = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    assert m is not None, f"{css_name} 里找不到 :root 块"
    block = _strip_css_comments(m.group(1))
    return {k: v.strip() for k, v in re.findall(r"--([\w-]+)\s*:\s*([^;]+);", block)}


def test_root_token_copies_stay_identical() -> None:
    """app.css 的 :root 是 tokens.css 的同值副本（裁定 D-3 的"值必须一致"）。

    D-3 驳回了"删副本"，条件是两边同值——本条把那个条件钉住：
    任何一边单独改色，对比度守卫（读 app.css）与真实渲染（读 tokens.css 优先）
    就会算出两个不同的世界。只比对**两边都定义了的十六进制令牌**。
    """
    tokens = _root_decls("tokens.css")
    copy = _root_decls("app.css")
    shared = {k for k in tokens if k in copy
              and tokens[k].startswith("#") and copy[k].startswith("#")}
    assert shared, "两个 :root 之间没有可比的 hex 令牌 —— 副本机制被拆了，先回 D-3 核对"
    drift = {k: (copy[k], tokens[k]) for k in sorted(shared) if copy[k].lower() != tokens[k].lower()}
    assert not drift, f"app.css 副本与 tokens.css 值漂移（前=app.css，后=tokens.css）：{drift}"


# ---------------------------------------------------------------- 对比度守卫扩展


def _iter_css_rules(css: str):
    """把 CSS 摊平成 (选择器原文, 声明块) 序列（注释已剥；@media 内规则一并取出）。"""
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", _strip_css_comments(css)):
        yield m.group(1), m.group(2)


def _rule_prop(css: str, selector: str, prop: str) -> str:
    """按源码顺序取该选择器**最后一次**声明的属性值（级联语义）。"""
    value = None
    for sel_text, body in _iter_css_rules(css):
        parts = [p.strip() for p in sel_text.split(",")]
        if selector in parts:
            m = re.search(r"(?:^|;)\s*" + re.escape(prop) + r"\s*:\s*([^;]+)", body)
            if m:
                value = m.group(1).strip()
    assert value is not None, f"app.css 里找不到 {selector} 的 {prop} 声明"
    return value


_TOKENS = None


def _token_map() -> dict[str, str]:
    global _TOKENS
    if _TOKENS is None:
        merged = _root_decls("app.css")
        merged.update(_root_decls("tokens.css"))
        _TOKENS = merged
    return _TOKENS


def _resolve_color(css_value: str) -> str:
    """把 `var(--x)` 沿令牌表解析到 hex（链条 ≤10 跳）。"""
    tokens = _token_map()
    seen = set()
    value = css_value
    for _ in range(10):
        if value.startswith("#") and len(value) == 7:
            return value.lower()
        m = re.match(r"var\(--([\w-]+)\)", value)
        if not m or m.group(1) in seen:
            break
        seen.add(m.group(1))
        value = tokens.get(m.group(1), "")
        assert value, f"引用了未定义令牌 --{m.group(1)}"
    raise AssertionError(f"值 {css_value!r} 解析不出 hex（rgba/named color 不在静态对比度计算范围）")


def _relative_luminance(hex_color: str) -> float:
    raw = hex_color.lstrip("#")
    channels = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(fg: str, bg: str) -> float:
    a, b = _relative_luminance(fg), _relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


CONTRAST_CASES = (
    # (标签, 文字选择器, 背景选择器) —— 背景取各自规则里的 background
    ("侧栏未选中导航字（深底次要字）", ".nav__item", ".sidebar"),
    ("侧栏分组小标题", ".nav__group", ".sidebar"),
    ("侧栏选中导航字", '.nav__item[aria-current="page"]', '.nav__item[aria-current="page"]'),
    ("侧栏计数徽标", ".nav__badge", ".nav__badge"),
    ("命令条视图标题", ".cmdbar__title", ".cmdbar"),
    ("命令条数据资产更新于", ".cmdbar__asof", ".cmdbar"),
    ("态势带业务问题", ".posture__question", ".posture"),
    ("态势带结论（报告首段）", ".posture__verdict", ".posture"),
    ("cites 正文角标", "sup.cite", "sup.cite"),
    ("状态药丸·默认", ".pill", ".pill"),
    ("状态药丸·成功", ".pill--ok", ".pill--ok"),
    ("状态药丸·告警", ".pill--warn", ".pill--warn"),
    ("状态药丸·失败", ".pill--err", ".pill--err"),
    ("状态药丸·运行中", ".pill--run", ".pill--run"),
)


@pytest.mark.parametrize("label,fg_sel,bg_sel", CONTRAST_CASES, ids=[c[0] for c in CONTRAST_CASES])
def test_command_center_color_pairs_meet_wcag_aa(label: str, fg_sel: str, bg_sel: str) -> None:
    """Wave B 新组件的关键文字/底色组合，静态算对比度 ≥4.5（沿用 test_frontend 的机制）。

    与既有禁用态按钮用例同一套算法与理由：**用 opacity 糊过去的色算不出真值**，
    所以这里要求 color/background 都走 `var(--令牌)` 且解析到 hex。
    侧栏深底那组是方案 01 的招牌（navy-900 上放 navy-400 文字，实测 5.53:1，
    是精算过的——把它钉住，防止有人"觉得浅"就把 --navy-400 改淡）。
    """
    css = _read("app.css")
    fg = _resolve_color(_rule_prop(css, fg_sel, "color"))
    bg = _resolve_color(_rule_prop(css, bg_sel, "background"))
    ratio = _contrast(fg, bg)
    assert ratio >= 4.5, f"{label}：对比度只有 {ratio:.2f}（{fg} on {bg}），需要 ≥4.5"


# ---------------------------------------------------------------- 颜色不是唯一载体


def test_status_indicators_pair_color_with_text() -> None:
    """状态元素旁边必须有文字节点：色+形+字三重编码（方案 01 的可访问性主张）。

    - 六行步骤 li：`.dot`（色/形）+ `.steps__state`（中文状态字）；
    - 阶梯六段：`.seg` 只有类名不行，每段要带 `.seg__lb` 中文段名；
    - 命令条健康点：dot 与 #health-text 同现；
    - app.js 侧：STEP_STATE_TEXT 五词全中文、syncLadder 重写 aria-label。
    """
    html = _strip_html_comments(_read("index.html"))
    js = _strip_js_comments(_read("app.js"))

    lis = re.findall(r'<li data-step="([a-z_]+)"[^>]*>(.*?)</li>', html, re.S)
    assert len(lis) == 6, f"#steps 明细行 {len(lis)} 条，应为 6"
    for step, inner in lis:
        assert 'class="dot"' in inner, f"步骤 {step} 的 li 少了圆点"
        m = re.search(r'<span class="steps__state">([^<]+)</span>', inner)
        assert m and m.group(1).strip(), f"步骤 {step} 的 li 少了状态文字"
        assert re.search(r"[\u4e00-\u9fff]", m.group(1)), f"步骤 {step} 状态文字必须中文：{m.group(1)!r}"

    segs = re.findall(r'<div class="seg" data-step="([a-z_]+)">(.*?)</div>', html, re.S)
    assert len(segs) == 6, "阶梯必须六段"
    for step, inner in segs:
        m = re.search(r'<span class="seg__lb">([^<]+)</span>', inner)
        assert m and re.search(r"[\u4e00-\u9fff]", m.group(1)), f"阶梯段 {step} 少了中文段名"

    assert re.search(r'id="cmdbar-health".{0,300}class="dot dot--ok"', html, re.S), "健康点与状态文字不同现"
    assert 'id="health-text"' in html

    states = re.findall(r'^\s*\w+: "([^"]+)"', _slice_between(js, "var STEP_STATE_TEXT = {", ("\n  };",)), re.M)
    assert states and all(re.search(r"[\u4e00-\u9fff]", s) for s in states), "STEP_STATE_TEXT 五词必须全中文"
    assert 'setAttribute("aria-label"' in _js_func(js, "syncLadder"), "阶梯状态变化必须同步 aria-label（读屏拿得到进度）"


# ------------------------------------------------------- 登录兜底视图与默认视图互等


def test_login_fallback_view_equals_app_default_view() -> None:
    """login.js 登录回跳兜底的视图，必须与 app.js 的 DEFAULT_VIEW 逐字相等。

    根因（端到端 🔴1）：Wave B 把 app.js 首屏改成 `DEFAULT_VIEW="command"`，
    却没同步 login.js 的 `APP_HASH_FALLBACK`（仍是旧的 `/app#/workbench`）。
    登录后落到侧栏没有导航项的旧视图 → 侧栏零高亮、命令条名不符。
    这两处字面量分居两个文件，之前没有任何用例交叉比对过，所以漂移没被抓到。
    本条把"默认视图只有一个事实来源"钉死，就是这次漂移的根因守卫。
    """
    js = _read("app.js")
    m = re.search(r'var DEFAULT_VIEW = "([a-z_]+)"', js)
    assert m is not None, "app.js 里找不到 DEFAULT_VIEW 字面量（守卫失去参照）"
    default_view = m.group(1)

    login = _read("login.js")
    fb = re.search(r'var APP_HASH_FALLBACK = "/app#/([a-z_]+)"', login)
    assert fb is not None, "login.js 里 APP_HASH_FALLBACK 不是 /app#/<视图> 的形态（回跳兜底被改坏）"
    fallback_view = fb.group(1)

    assert fallback_view == default_view, (
        f"登录兜底落到 #/{fallback_view}，但 app.js 的 DEFAULT_VIEW 是 {default_view} —— "
        "登录后落地页会与侧栏高亮错位（端到端 🔴1 的复发）"
    )

    disp = re.search(r'var NEXT_DISPLAY_FALLBACK = "([^"]*)"', login)
    assert disp is not None, "login.js 里找不到 NEXT_DISPLAY_FALLBACK"
    assert disp.group(1) == fb.group(0).split("= ")[1].strip('";'), (
        "NEXT_DISPLAY_FALLBACK（给用户看的兜底去向）与实际回跳的 APP_HASH_FALLBACK 不一致 —— "
        "展示与行为分叉就是撒谎"
    )


# ------------------------------------------------------- max_steps 请求体与值域


def test_analysis_request_body_carries_max_steps_in_one_to_eight() -> None:
    """发起分析的 POST body 必须真的带 max_steps，且值域被钳在后端允许的 1–8 内。

    方案 01 §8.2 承诺 max_steps 选择器（快速3/标准5/充分6/深度8），后端
    `QuestionRequest.max_steps` 是 ge=1/le=8（app/schemas_workflow.py）。重构一度把
    整条链路丢了（grep app/web 零命中）。本条同时钉三件事，缺一即红：
      1. app.js 的 POST body 字面里出现 `max_steps:`；
      2. app.js 在发送前把值钳进 [1,8]（Math.max(1 / Math.min(8）；
      3. index.html 里 `<select id="max-steps">` 的每个 option 值都是 1–8 的整数。
    """
    js = _strip_js_comments(_read("app.js"))

    ask = _js_func(js, "ask")
    m = re.search(r"body:\s*JSON\.stringify\(\{([^}]*)\}\)", ask)
    assert m is not None, "ask() 没有发 JSON body —— 发起分析的请求体结构被改，请回契约核对"
    assert "max_steps:" in m.group(1), (
        "发起分析的 POST body 里没有 max_steps（端到端 §4：UI 无法达到 max_steps，链路又断了）"
    )
    assert "dataset_id:" in m.group(1) and "question:" in m.group(1), "请求体至少还要有 dataset_id/question"

    read = _js_func(js, "readMaxSteps")
    assert read, "找不到 readMaxSteps —— max_steps 的取值/钳位没有单一实现点"
    assert "Math.max(1" in read and "Math.min(8" in read, (
        "readMaxSteps 必须把值钳进 [1,8]；越界后端会回 422，钳在发送前比让用户白等诚实"
    )

    html = _strip_html_comments(_read("index.html"))
    sel = re.search(r'<select id="max-steps"[^>]*>(.*?)</select>', html, re.S)
    assert sel is not None, "index.html 缺少 <select id=\"max-steps\"> —— max_steps 控件没交付"
    option_values = re.findall(r'<option value="(\d+)"', sel.group(1))
    assert option_values, "max-steps 下拉没有任何 <option>"
    for raw in option_values:
        value = int(raw)
        assert 1 <= value <= 8, f"max-steps option 值 {value} 越出后端允许的 1–8"



# ------------------------------------------- 证据区必须写事实（Wave E 缺陷 1 · 核心卖点）


def test_evidence_rows_carry_real_backend_fields() -> None:
    """报告证据行的两条分支各写什么，是被这条钉死的（主控实跑发现的 🔴 空壳文案）。

    根因：归档接口 10 只还原 FinalReport，`citations` 就是裸编号数组 `["T1","T2"]`，
    工具明细活在接口 7 的 `results[]`（`app/schemas_tool.py::ToolResult`）+ 接口 6 的
    `tasks[]`（`description`）里，且**只在本次运行还活着时可读**。
    旧实现无论有没有明细都画同一句「对应这次取数的任务描述与工具，见正文角标与本行的编号」
    —— 那句话什么都没说（"见本行的编号"是自我循环），等于核心卖点落空。

    本条只加强不放宽：证据集合仍由 `collectCiteTaskIds(report)` 产出（与正文角标同源，
    契约 §7.2 规则 1 的一一对应没被动过）。
    """
    js = _strip_js_comments(_read("app.js"))
    fn = _js_func(js, "renderReportInto")

    # ① 那句圆话在所有交付文件里都不许再出现（原文级判据，最直接的回归守卫）。
    for name in ALL_WEB_FILES:
        assert "见正文角标与本行的编号" not in _read(name), f"{name} 里那句自我循环的圆话回潮了"

    # ② 有明细的分支：每一个字都来自后端真实字段名。
    assert "evidence" in fn, "renderReportInto 没有 evidence 入参 —— 明细进不到证据行"
    for field in ("tool_name", "row_count", "elapsed_ms", "columns", "description", "error_message"):
        assert field in fn, f"有 results 的分支没写后端真实字段 {field}"
    assert 'resultByTask[taskId]' in fn and "indexByTaskId(results)" in fn, (
        "明细必须按 task_id 对到 cites 上，不是整段贴一遍"
    )

    # ③ elapsed_ms 可以用来写单次调用耗时，但必须自带口径限定（D-6 / 契约 §6.3：不是阶段级进度）。
    assert "这一次工具调用耗时" in fn, "elapsed_ms 的落点文案没写出来"
    assert "不是六阶段任何一个阶段的进度" in fn, (
        "单次调用耗时必须注明它不是阶段级数字，否则会被读成阶段进度"
    )

    # ④ 没有明细的分支：老实说归档里只有编号，并给出仍然真实可定位的信息（小节标题）。
    assert "ARCHIVED_EVIDENCE_NOTE" in fn, "无 results 分支没有走那条统一的归档说明"
    assert "归档里只保存了引用编号" in js and "只有它还在服务端时才读得到" in js, (
        "归档分支必须说明缺什么、什么时候能读到"
    )
    assert "collectSectionHeadingsByCite" in fn and "正文出现在：" in fn, (
        "归档分支仍要给每行编号对应的正文小节标题（这是真实可定位信息）"
    )

    # ⑤ 一一对应的口径不许被顺手改掉（契约 §7.2 规则 1）。
    assert "var cites = collectCiteTaskIds(report);" in fn
    assert "cites.forEach(function (taskId)" in fn, "证据行仍要逐条对应正文角标集合"


# -------------------------------- 文档内角标全部可键盘到达，态势带标记明确不可点（Wave E 缺陷 2）


def test_doc_cites_are_focusable_and_posture_marks_are_static() -> None:
    """两种引用标记的语义必须分清楚：文档内的可点可跳，态势带行内的**不可点**。

    根因（实测）：态势带结论里的 `sup.cite` 共 5 个，既没有 tabindex 也没有 role，
    而 jumpToEvidence 只沿 parentElement 找 `.src-line` —— 态势带里没有，于是静默 no-op；
    同时挂属性的选择器 `.doc__cites sup.cite` 只覆盖报告文档里那一个段落。
    修法：态势带换成 `sup.cite-mark`（静态），文档内把作用域放宽到整个容器。
    """
    js = _strip_js_comments(_read("app.js"))

    marks = _js_func(js, "postureCiteMarks")
    assert 'sup.className = "cite-mark"' in marks, "态势带行内标记没换成静态类名"
    for banned in ("tabindex", "role", "jumpToEvidence"):
        assert banned not in marks, f"态势带的静态标记拿到了交互语义 {banned!r} —— 又变成假可点"
    assert "postureCiteMarks(" in _js_func(js, "paintPostureRow"), "态势带行没改用静态标记渲染"

    doc = _js_func(js, "docCiteSupers")
    assert 'sup.className = "cite"' in doc, "报告文档内的角标仍是 .cite（可点的那一支）"
    assert "docCiteSupers(" in _js_func(js, "renderReportInto")

    # 文档内每一个 sup.cite 都要挂上可访问属性：作用域是**整个容器**，不是某一个段落类。
    fn = _js_func(js, "renderReportInto")
    assert 'container.querySelectorAll("sup.cite[data-task-id]")' in fn, (
        "挂 tabindex 的选择器又被收窄了 —— 文档里任何一处角标都必须键盘可达"
    )
    assert ".doc__cites sup.cite" not in js, "旧的窄作用域选择器不许回来"
    assert 'setAttribute("tabindex", "0")' in fn and 'setAttribute("role", "link")' in fn
    assert "postureCites(" not in js, "共用的 postureCites 已拆成两支，混用会让语义再次分叉"

    # 事件委托仍只认真正标了 tabindex 的那些（两条：click 与 Enter/Space）。
    assert js.count('sup.getAttribute("tabindex") === "0"') == 2, (
        "跳转委托的 tabindex 判据数量变了：静态标记必须进不了这条路径"
    )

    css = _strip_css_comments(_read("app.css"))
    assert "sup.cite[tabindex] { cursor: pointer; }" in css, "手型指针必须跟着 tabindex 走"
    assert "sup.cite-mark { cursor: default; }" in css, "静态标记要明确写 cursor: default"
    assert ".doc__cites sup.cite[tabindex]" not in css, "CSS 侧的窄作用域同样不许回来"


# ------------------------------ 深链到已归档运行：三种真实状态各说各的话（Wave E 缺陷 3）


def test_archived_run_deep_link_is_honest_about_ladder() -> None:
    """`#/runs/{id}` 指向一条已经结束并归档的运行时的文案，钉在这里。

    根因（实测）：轮询接口 GET /api/workflows/{id} 读内存注册表，归档运行拿不到
    step_index，页面于是画出 ladder-count="— / 6" + 「尚未开始分析」——
    对一条已经成功的运行说"尚未开始分析"是假话。
    定案的三态：① 正在跑（真实 step_index，正常画阶梯）② 已归档（说明阶梯为什么空着 +
    给一个真的能跳过去的报告入口）③ 查不到（后端 404 的口径：找不到或无权访问，**不回落列表页**）。
    """
    js = _strip_js_comments(_read("app.js"))

    deep = _js_func(js, "applyDeepLink")
    assert 'name === "runs"' in deep and "openRunDetail(entityId)" in deep, (
        "runs 深链没有分支 —— 又落回那句对归档运行说『尚未开始分析』的默认壳"
    )

    fn = _js_func(js, "openRunDetail")
    assert "尚未开始分析" not in fn, "归档/查不到的分支不许复用那句默认文案"
    assert fn.index("API.workflow(") < fn.index("API.historyDetail("), (
        "先问状态接口（有真实 step_index 就用它），答不到才问归档接口（D-6：恒 200）"
    )
    assert "resetWorkflowPanel()" in fn, "深链前必须复位阶梯，别让上一条运行的点亮留在屏上"
    assert "startPolling()" in fn, "还在跑的那一条要交回真实轮询，前端不自己推算进度"

    note = _js_func(js, "renderArchivedRunNote")
    assert "尚未开始分析" not in note
    assert "这条运行已经结束，并且已经归档" in note
    assert "查看报告" in note, "归档运行必须给一个真的能跳过去的报告入口"
    assert "openHistoryFromPosture(" in note, (
        "入口必须走归档详情（接口 10）：#/report/{id} 打的是内存注册表，归档运行点进去是 404"
    )
    assert '"#/report/"' not in note, "不许把用户领到一条对归档运行失效的报告深链上"

    body = _slice_between(js, "var ARCHIVED_RUN_LADDER_NOTE =", ("\n  async function openRunDetail",))
    assert "归档记录里不保存阶段序号" in body, "要说清楚阶梯为什么空着，而不是留一个空壳让人猜"

    missing = _js_func(js, "renderMissingRunNote")
    assert "找不到这条运行，或当前账号无权访问" in missing, (
        "越权与不存在后端都是 404，文案要与之一致（DECISIONS §8.3.1：不许 fallback 复活）"
    )
    for banned in ('go("', "location.hash", "applyView("):
        assert banned not in missing, f"查不到时不许静默把用户领去别的页面：{banned}"

    # 阶梯只能由真实 step_index 点亮：这三段说明里不许出现定时器或时间推算。
    for banned in ("setInterval", "Date.now", "performance.now", "syncLadder("):
        assert banned not in note and banned not in missing, f"说明区里出现了假装进度的 {banned}"

    # 复位必须两块一起复位：#steps 由 resetWorkflowPanel 重建，.ladder 也要一起清空。
    reset = _js_func(js, "resetWorkflowPanel")
    assert 'classList.remove("is-done", "is-run", "is-skip", "is-failed")' in reset, (
        "只复位 #steps 不复位 .ladder —— 上一条深链的六段点亮会留在屏上，对着新的运行说「已经跑完」"
    )
    assert 'setSlotText("ladder-count"' in reset, "ladder-count 也要回到「— / 6」这个取不到的口径"


# ---------------------------------------- 六行明细的状态必须写到 CSS 真认的那个钩子上（Wave E 附带取证）


def test_paint_steps_writes_the_css_state_hook() -> None:
    """`#steps li` 的着色只由 `[data-state]` 驱动；paintSteps 若只写类名，六行永远停在灰态。

    实跑取证（本 agent，`#/runs/9cfbcb3…` 这条**已经成功**的运行）：
    `.ladder .seg` 六段全部 `is-done`、`#ladder-count = 6 / 6`，
    但 `#steps li` 的 `data-state` 全是 `waiting` —— 右侧文字写着「已完成」、
    左侧圆点写着「还没开始」。根因：app.css 里 `.done/.active/.failed` **一条类名规则都没有**
    （只有 1119-1131 行那七条 `li[data-state=…]`），而 paintSteps 以前只写类名。
    契约 §6.2 与本文件 index.html 的注释都写着"data-state 只由 paintSteps() 改写"，
    所以这里是修实现、不是放宽断言。
    """
    js = _strip_js_comments(_read("app.js"))
    paint = _js_func(js, "paintSteps")
    assert 'item.setAttribute("data-state", dataState)' in paint, (
        "paintSteps 不再写 data-state —— 六行明细的圆点会全部失效（CSS 只认这个钩子）"
    )
    # 五个状态词都要有落点，且与 STEP_STATE_TEXT 的五词同源（不许第三个词）。
    assert 'dataState = "done"' in paint
    assert 'dataState = "active"' in paint
    assert 'dataState = "failed"' in paint
    assert 'dataState = terminal ? "skipped" : "waiting"' in paint
    # 类名写法是 D-8 钉住的存量机制，不许被"顺手清理"掉。
    for legacy in ('classList.remove("done", "active", "failed")', 'classList.add("done")', 'classList.add("active")', 'classList.add("failed")'):
        assert legacy in paint, f"paintSteps 的存量类名写法被改掉了：{legacy}"

    css = _strip_css_comments(_read("app.css"))
    assert re.search(r"\.steps li\[data-state=\"done\"\]", css), "CSS 侧的 done 钩子没了 —— 本条与上一条必须一起红"
    for state in ("done", "active", "failed", "skipped"):
        assert '.steps li[data-state="%s"]' % state in css, f"CSS 缺状态 {state} 的落点"
    for dead in (".steps li.done", ".steps li.active", ".steps li.failed"):
        assert dead not in css, f"CSS 里出现了类名版 {dead} —— 与 JS 侧的 data-state 会变成两套口径"


def test_report_deep_link_falls_back_to_the_archive() -> None:
    """`#/report/{id}` 在服务重启后必须还能打开自己账号的归档报告。

    根因（实测，2026-10-17）：`loadReport` 原来**只**调接口 7（内存注册表），
    而接口 7 的注释自己就写着「工作流状态存在服务进程的内存里，服务重启后全部丢失」。
    方案 01 把「查看报告」做成了首屏态势带与历史记录的一等入口，于是重启之后
    用户点自己那条已成功的分析 → 直接看到「工作流不存在（原值码：NOT_FOUND）」。
    这不是新 bug，是老限制被新入口放大成主路径故障。
    """
    raw = _read("app.js")
    js = _strip_js_comments(raw)

    assert js.count("API.report(") == 1, (
        "报告接口必须只有一处调用点（D-8 存量守卫），回退只能走归档接口，不许再开一处"
    )

    load = _js_func(js, "loadReport")
    assert "API.report(state.workflowId)" in load, "主路径仍是接口 7：活着的运行才有工具明细"
    assert load.index("API.report(state.workflowId)") < load.index("API.historyDetail("), (
        "必须先问注册表、答不到才回退归档 —— 反过来会让刚跑完的运行丢工具明细"
    )
    assert "renderReport({ report: archivedReport, fromArchive: true })" in load, (
        "归档回退必须带上 fromArchive，让正文与证据区走那条诚实分支"
    )
    assert "errorOf(archived.ok ? result : archived)" in load, (
        "两条路都不交出内容时报的是**归档**的错，不是把注册表的 404 当成唯一事实"
    )

    render = _js_func(js, "renderReport(data)")
    assert "data.fromArchive" in render, "归档来源必须可见，不许静默装作是运行态"
    assert "这份报告读自归档库" in render
    for promise in ("阶段进度", "工具调用的逐条明细", "按编号逐条对照"):
        assert promise in render, f"归档来源说明缺了「{promise}」这一句用户真正需要知道的边界"

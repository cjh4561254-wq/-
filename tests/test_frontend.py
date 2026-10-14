"""前端页面的测试：`GET /`、静态资源、以及"前端别把后端契约写歪"。

这些用例都跑在 `TestClient` 上，但**它们测的不是浏览器**（没有 JS 引擎），
所以分两类：

1. **真 HTTP 行为**：`/` 返回 HTML、`/static/*` 能取到、`data/` 没被挂成静态资源、
   `/api/health` 与 `/docs` 还在。这类断言是真的在请求应用。
2. **契约一致性**：把 `app/web/app.js` 当文本读，断言它引用的接口路径、
   6 个中文步骤名、三个终态与我们后端一致。这不是"测 JS 逻辑"，
   而是防"前端写死了一份过期的接口路径 / 少认一个终态"——
   这类错误在浏览器里表现为"一直在转圈"，靠肉眼很难发现。

   注意：真跑 JS（DOM 交互、轮询、渲染）只能靠真实 uvicorn 手工验，
   见 task-25 的验收标准 ⑤。
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.core import registry
from app.main import app

# 示例数据集（2168 行 × 9 列）。上传它才能证明"上传目录里真的有一个文件"，
# 而 `/data/uploads/<那个文件>` 依然取不到。
SAMPLE_CSV = config.PROJECT_ROOT / "data" / "samples" / "sales.csv"

WEB_DIR = config.PROJECT_ROOT / "app" / "web"

# 页面上必须存在的元素 id（六个区块 + 几个状态位）。
REQUIRED_IDS = (
    "file",
    "upload",
    "upload-state",
    "dataset-summary",
    "llm-badge",
    "preview",
    "columns",
    "missing-top3",
    "question",
    "ask",
    "ask-state",
    "steps",
    "elapsed",
    "workflow-state",
    "workflow-error",
    "charts",
    "report",
)


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """上传目录隔离 + 清缓存 + 清注册表 + 清掉 LLM 配置。

    进去之前也清一遍：单跑这个文件时不受别的测试文件残留影响，
    整个会话顺序调换也不会变红（与 tests/test_analysis_api.py 同一套做法）。
    """
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    config.reset_settings_cache()
    registry.clear()
    yield
    registry.clear()
    config.reset_settings_cache()


def teardown_function() -> None:
    """兜底：最后一条用例登记的状态也不能活到会话结束（D-16）。"""
    registry.clear()


def _client() -> TestClient:
    """必须是 `with ...`（上下文管理器）才会跑 lifespan，模块级 TestClient(app) 不会。"""
    return TestClient(app)


def _read(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def _upload_sample(client: TestClient) -> dict:
    with SAMPLE_CSV.open("rb") as handle:
        response = client.post(
            "/api/datasets/upload",
            files={"file": ("sales.csv", handle, "text/csv")},
        )
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ---------------------------------------------------------------- 真 HTTP 行为


def test_root_serves_the_frontend_page() -> None:
    """`/` 必须返回真页面（不是 JSON 提示），六个区块的元素都在。"""
    with _client() as client:
        response = client.get("/")

    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    assert "application/json" not in response.headers["content-type"]

    body = response.text
    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in body, f"页面里缺少 id={element_id}"


def test_page_declares_utf8_and_is_valid_utf8() -> None:
    """`<meta charset="utf-8">` 必须在，且文件本身是合法 UTF-8。

    少了这一行，Windows 上的浏览器会拿 GBK 去猜，页面上所有中文都变乱码 ——
    而这个项目的展示内容全中文，所以这条不是"锦上添花"。
    """
    raw = (WEB_DIR / "index.html").read_bytes()
    text = raw.decode("utf-8")  # 解不开就是失败，不需要再断言什么
    assert "<meta charset=" in text.lower()
    assert 'charset="utf-8"' in text.lower()


def test_static_assets_are_served() -> None:
    """CSS / JS 从 /static 下取得到，且 MIME 类型是对的。"""
    with _client() as client:
        js = client.get("/static/app.js")
        css = client.get("/static/app.css")

    assert js.status_code == 200, js.text
    assert "javascript" in js.headers["content-type"]
    assert css.status_code == 200, css.text
    assert "css" in css.headers["content-type"]


def test_page_links_its_own_assets_and_nothing_external() -> None:
    """页面引的是本地 /static 资源，且全部文件里没有任何外链（离线可演示）。"""
    html = _read("index.html")
    assert "/static/app.css" in html
    assert "/static/app.js" in html
    assert html.index("/static/app.css") < html.index("/static/app.js")

    for name in ("index.html", "app.css", "app.js"):
        text = _read(name)
        assert "http://" not in text, f"{name} 里有绝对 URL（不允许引 CDN）"
        assert "https://" not in text, f"{name} 里有绝对 URL（不允许引 CDN）"


def test_data_directory_is_not_mounted_as_static() -> None:
    """安全断言：`data/` 绝不能是静态资源。

    `data/uploads/` 里是用户上传的原始文件。一旦把 `data/` 挂成静态资源，
    任何人拼一个 URL 就能下载别人上传的表格（`data/samples/` 也会一起暴露）。
    所以这里先真实上传一个文件拿到磁盘文件名，再证明它取不到。
    """
    with _client() as client:
        dataset = _upload_sample(client)
        stored = dataset["stored_filename"]

        leaked = client.get(f"/data/uploads/{stored}")
        sample = client.get("/data/samples/sales.csv")

    assert leaked.status_code in (404, 405), (
        f"上传文件被当成静态资源暴露了：HTTP {leaked.status_code} {leaked.text[:200]}"
    )
    assert sample.status_code in (404, 405), f"示例数据被暴露了：HTTP {sample.status_code}"


def test_static_mount_does_not_escape_the_web_directory() -> None:
    """`/static/` 不能顺着 `..` 爬出 app/web/（StaticFiles 自己会拦，这里钉住）。"""
    with _client() as client:
        escaped = client.get("/static/../app/main.py")
        encoded = client.get("/static/..%2fapp%2fmain.py")

    assert escaped.status_code in (403, 404), escaped.text[:200]
    assert encoded.status_code in (403, 404), encoded.text[:200]


def test_api_and_docs_are_untouched_by_the_page() -> None:
    """把 `/` 让给页面，不能连带影响 API 与文档。"""
    with _client() as client:
        health = client.get("/api/health")
        docs = client.get("/docs")

    assert health.status_code == 200, health.text
    body = health.json()
    assert body["ok"] is True
    assert "llm_configured" in body["data"]
    assert docs.status_code == 200
    assert "text/html" in docs.headers["content-type"]


# ---------------------------------------------------------------- 契约一致性


def test_js_references_the_same_endpoints_as_the_backend() -> None:
    """前端引用的接口路径必须与后端一致（防"前端写死一份过期路径"）。"""
    js = _read("app.js")
    for path in (
        "/api/datasets/upload",
        "/api/datasets/",
        "/api/datasets/${id}/profile",
        "/api/analysis",
        "/api/workflows/",
        "/charts/",
        "/api/health",
    ):
        assert path in js, f"app.js 里没有引用 {path}"


def test_js_knows_all_six_step_labels() -> None:
    """6 个步骤的中文名一个都不能少（少一个界面上就会出现空圆点）。"""
    js = _read("app.js")
    for label in (
        "正在读取数据",
        "正在制定分析计划",
        "正在计算",
        "正在生成图表",
        "正在撰写报告",
        "正在校验数字",
    ):
        assert label in js, f"app.js 里缺少步骤文案：{label}"


def test_js_step_order_matches_the_backend_step_vocabulary() -> None:
    """6 个圆点必须用**后端那套 step 名**，一个字母都不能差。

    为什么单独立一条（这是把真实 app.js 跑起来之后才想到要钉的东西）：
    `paintSteps()` 用 `STEP_ORDER.indexOf(step)` 定位当前阶段，认不出来就退回
    "还没开始 / 全部完成"。名字差一个字母（`execution` 写成 `executing`），
    圆点会**安静地不亮**：HTTP 全正常、源码断言也照样写得出来，只有肉眼能发现。
    `app/agents/orchestrator.py` 的 `_STEPS` 是这件事的唯一事实来源，这里把两边钉死。
    """
    from app.agents.orchestrator import _STEPS

    js = _read("app.js")
    match = re.search(r"const STEP_ORDER = \[(.*?)\];", js, re.S)
    assert match is not None, "app.js 里找不到 STEP_ORDER"
    order = re.findall(r'"([A-Za-z_]+)"', match.group(1))
    assert order == list(_STEPS), f"前端圆点顺序 {order} 与后端 _STEPS {list(_STEPS)} 不一致"

    labels = js.split("const STEP_LABELS = {", 1)[1].split("};", 1)[0]
    for step in _STEPS:
        assert re.search(rf"^\s*{step}\s*:", labels, re.M), f"STEP_LABELS 里缺少 {step} 的中文文案"


def test_js_polls_the_status_endpoint_and_stops_at_every_terminal_status() -> None:
    """轮询契约：1 秒一次、三个终态都认、进终态停止、报告只在 has_report 为真时取。"""
    js = _read("app.js")

    assert "POLL_INTERVAL_MS = 1000" in js

    terminal_line = next(
        (line for line in js.splitlines() if line.strip().startswith("const TERMINAL")),
        None,
    )
    assert terminal_line is not None, "找不到 TERMINAL 声明"
    for status in ("SUCCESS", "SUCCESS_WITH_WARNINGS", "FAILED"):
        assert status in terminal_line, f"TERMINAL 里少了 {status}：{terminal_line}"

    # 进终态必须停轮询，否则界面会一直打后端。
    assert "clearInterval" in js
    # 报告接口的开关是接口 6 的 has_report，不是轮询报告接口。
    assert "has_report" in js
    # 只轮询状态接口：报告接口只在 loadReport 里出现一次调用。
    assert js.count("API.report(") == 1


def test_js_handles_the_four_edge_cases() -> None:
    """四个必须处理的边界都要在代码里留痕（上传失败 / 503 / 失败详情 / 超时）。"""
    js = _read("app.js")
    for marker in (
        "FILE_TOO_LARGE",
        "UNSUPPORTED_FILE",
        "SERVICE_UNAVAILABLE",
        "WORKFLOW_TIMEOUT",
        "data.error",
    ):
        assert marker in js, f"app.js 里没有处理 {marker}"


def test_js_does_not_branch_on_http_status() -> None:
    """**不许按 HTTP 状态码判断失败。**

    工作流超时的 HTTP 状态码是 200（§7.0），按状态码判断会把它当成成功。
    统一外壳已经给了 `ok` / `error.code`，前端只需要认这一套。
    """
    js = _read("app.js")
    assert "resp.ok" not in js, "不能用 resp.ok 判断（超时的 HTTP 状态码是 200）"
    assert "response.ok" not in js
    assert "status === 200" not in js
    assert "status_code" not in js


def test_upload_error_codes_match_the_backend(monkeypatch) -> None:
    """上传失败的两种业务码，后端确实是这么发的（拿真实响应核一遍）。

    前端就地提示靠的就是这两个码；后端哪天改名了，这条会先红，
    而不是等用户在页面上看到一句"未知错误"。
    """
    # 把上限压到 1 MB，再传一个 1.2 MB 的文件 —— 走的是真实的分块读 + 超限判定。
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    config.reset_settings_cache()
    payload = b"a,b\n" + b"1,2\n" * 300_000

    with _client() as client:
        bad_ext = client.post(
            "/api/datasets/upload",
            files={"file": ("notes.txt", b"not a table", "text/plain")},
        )
        too_big = client.post(
            "/api/datasets/upload",
            files={"file": ("big.csv", payload, "text/csv")},
        )

    # 状态码来自 app/errors.py 的 ERROR_STATUS 表：不支持的类型是 415、超限是 413。
    assert bad_ext.status_code == 415, bad_ext.text
    assert bad_ext.json()["error"]["code"] == "UNSUPPORTED_FILE"
    assert too_big.status_code == 413, too_big.text
    assert too_big.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_error_shell_shape_used_by_the_frontend() -> None:
    """前端只认一种响应结构：`{ok, data, error}`。这里把失败形状也钉住。"""
    with _client() as client:
        missing = client.get("/api/datasets/does-not-exist-0000")

    assert missing.status_code == 404
    body = missing.json()
    assert set(body) == {"ok", "data", "error"}
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "NOT_FOUND"
    assert isinstance(body["error"]["message"], str)
    # 让 JSON 往返一次，确保响应确实是合法 JSON（前端 parse 不会炸）。
    json.dumps(body, ensure_ascii=False)


def test_web_directory_has_no_stray_files() -> None:
    """app/web 里只放前端三件套。

    多出来的文件（编辑器备份、导出的截图、临时脚本）会被 StaticFiles 一起公开，
    而且不会有人注意到 —— 这条断言就是那道"看一眼"。
    """
    names = sorted(path.name for path in WEB_DIR.iterdir())
    assert names == ["app.css", "app.js", "index.html"], names
    assert Path(WEB_DIR / "index.html").is_file()

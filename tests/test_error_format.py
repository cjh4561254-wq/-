"""统一错误格式的测试（独立文件，与 test_health.py 分开关注点）。

为什么要专门测"错误格式"？
    前端只写一套解析逻辑：先看 ok。如果失败时后端换成 FastAPI 默认的
    {"detail": ...}，前端就会崩在解析那一步 —— 而这种 bug 在最需要
    提示用户的时刻才会暴露。所以它值得一条专门的回归测试。
"""

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import ErrorCode

client = TestClient(app)


def test_404_envelope():
    resp = client.get("/api/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == ErrorCode.NOT_FOUND.value
    assert body["error"]["detail"] == "/api/nope"


def test_405_envelope():
    """方法不对：405 + 统一外壳 + 业务码要能和方法错误对上。

    这里三条断言都是被 QA 逼出来的：
      · 最初 error.code 归到了 BAD_REQUEST，前端会把"你方法用错了"提示成
        "请检查你的输入" —— 提示指错方向，排查时要多绕一圈；
      · 最初响应里丢了 Allow 头，而 RFC 7231 §6.5.5 要求 405 必须带它。
        自定义异常处理器忘了转发 exc.headers 就会这样静默丢失。
    """
    resp = client.delete("/api/health")
    assert resp.status_code == 405
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == ErrorCode.METHOD_NOT_ALLOWED.value

    # Allow 头要列出这个路径允许的方法，客户端才知道该用什么
    allow = resp.headers.get("allow", "")
    assert "GET" in allow, f"405 响应必须带 Allow 头，实际是: {dict(resp.headers)}"


def test_422_envelope():
    """路径参数类型不对时，FastAPI 抛 RequestValidationError → 422 + 统一外壳。

    **这条测试的第一版是错的，值得记一笔。**
    它原来打的是 /api/datasets/not-a-number/profile，并断言
    `status_code in (404, 422)` 且 `code in (NOT_FOUND, BAD_REQUEST)` ——
    那个接口当时还不存在，实际返回的是 404，于是"422 到底有没有被正确翻译"
    根本没测到，而 404/422 写反了它也照样绿。

    正确做法：往真实的 app 对象上临时挂一条带类型化路径参数的路由。
    异常处理器是 per-app 的，挂到别的 FastAPI 实例上测不到真实实现。
    """
    path = "/__test_typed/{item_id}"

    @app.get(path)
    def _typed(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    try:
        resp = client.get("/__test_typed/not-an-int")
        assert resp.status_code == 422  # 严格断言，不再"404 也算过"
        body = resp.json()
        assert body["ok"] is False
        assert body["data"] is None
        assert body["error"]["code"] == ErrorCode.BAD_REQUEST.value
        assert "item_id" in body["error"]["detail"]  # 要告诉调用方是哪个参数错了

        # 顺带证明参数合法时这条路由真的能通（否则上面的 422 可能来自别的原因）。
        # 注意这条临时路由返回的是普通 dict，没有套 ApiResponse 外壳 ——
        # 所以这里读的是顶层，不是 data。
        assert client.get("/__test_typed/42").json() == {"item_id": 42}
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != path
        ]


def test_http_exception_from_route_uses_envelope():
    """路由里 `raise HTTPException(...)` 也要被翻译成统一外壳。

    做法：临时往应用上挂一条会抛错的路由，测完就撤掉，
    避免为了测试而污染正式代码。
    """
    @app.get("/__test_boom")
    def _boom():
        raise HTTPException(status_code=400, detail="我故意抛的错")

    try:
        resp = client.get("/__test_boom")
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["message"] == "我故意抛的错"
        assert body["error"]["code"] == ErrorCode.BAD_REQUEST.value
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != "/__test_boom"
        ]


def test_cors_allows_local_origin_only():
    """本机来源放行、外来源不回 ACAO 头。"""
    ok = client.get("/api/health", headers={"Origin": "http://localhost:8000"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:8000"

    bad = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in bad.headers

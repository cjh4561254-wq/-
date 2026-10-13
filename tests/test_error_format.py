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
    resp = client.delete("/api/health")
    assert resp.status_code == 405
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == ErrorCode.BAD_REQUEST.value


def test_422_envelope():
    """路径参数类型不对时，FastAPI 会抛 RequestValidationError。

    这里用 /api/datasets/{dataset_id} 做示例 —— 该接口还没实现，
    所以 404 和 422 都可能出现，我们只断言"不是 500、且格式统一"。
    """
    resp = client.get("/api/datasets/not-a-number/profile")
    assert resp.status_code in (404, 422)
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] is not None
    assert body["error"]["code"] in (
        ErrorCode.NOT_FOUND.value,
        ErrorCode.BAD_REQUEST.value,
    )


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

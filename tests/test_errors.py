"""测试 app/errors.py：业务错误 → HTTP 状态码映射 + 敏感信息清洗。

这个文件守护的是一条**跨全项目的接口约定**，所以它值得单独一个测试文件：
    所有错误响应体的形状必须一致（{"ok":false,"data":null,"error":{...}}），
    而 HTTP 状态码必须是真实的（404 就是 404，413 就是 413）。

第一阶段的教训：错误格式这种东西"看起来很简单"，但没有测试守护就会在
第二个接口那里走样。所以这里既测试"映射表本身"，也测试"真的经过 HTTP 之后
客户端收到什么"。

多数用例搭一个最小 app 来跑（需要的接口故意不存在于正式代码里）；
凡是"框架级行为"的用例（500 兜底、422 参数校验）则挂在**真实的 app 对象**上，
因为异常处理器是 per-app 的，挂在别处测不到真实实现。
"""

from http import HTTPStatus
import os

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import errors as errors_module
from app.errors import (
    ERROR_STATUS,
    AppError,
    sanitize_detail,
)
from app.main import app, app_error_handler, unexpected_exception_handler
from app.schemas import ApiError, ApiResponse, ErrorCode

# `reset_username_patterns_cache` 故意**不用 from ... import** 取进来：
# 它是"缓存复位"这种测试专用工具，而且这条测试的全部价值就在于
# **能在老的、把正则冻结在 import 时刻的实现上真的跑起来并失败**。
# 写成模块级 from-import 的话，换回老实现时整个测试文件会 ImportError 收集失败 ——
# 看着是红的，红的却是"测试引用了不存在的名字"，不是"行为回归了"。
# 只要能跑的失败才算证据，收集失败不算。
reset_username_patterns_cache = getattr(errors_module, "reset_username_patterns_cache", None)


@pytest.fixture
def error_app():
    """一个只用来测试错误处理的最小应用。

    为什么在 fixture 里现场注册 handler，而不是直接 import `app.main` 里的 app？
        第一，测试"异常处理器"这件事需要一个能主动抛错的接口；正常的业务应用
        不应该为了测试而多出 /boom 这种路由。
        第二，handler 从 `app.main` 导入，保证测的是**真实实现**，
        不是测试里另写一份 —— 否则测试通过只能证明测试里的那份是对的。
    """
    test_app = FastAPI()
    test_app.add_exception_handler(AppError, app_error_handler)

    @test_app.get("/boom/{code}")
    def boom(code: str, status_code: int | None = None, detail: str | None = None) -> None:
        """按传入的错误码抛出 AppError，让 handler 去翻译。"""
        raise AppError(
            code=ErrorCode(code),
            message=f"测试用错误: {code}",
            detail=detail,
            status_code=status_code,
        )

    @test_app.get("/wrapped")
    def wrapped() -> None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "内部错误",
            detail=r"打开失败: D:\个人项目01\data\uploads\x.csv",
        )

    return test_app


def test_error_status_table_is_exactly_as_designed():
    """映射表本身就是接口契约，先把它钉死。

    如果哪天有人把 FILE_TOO_LARGE 改成 400，这条测试会立刻失败并说明原因，
    而不是等到前端或面试官发现"上传超大文件返回的状态码看不懂"。
    """
    assert ERROR_STATUS == {
        ErrorCode.BAD_REQUEST: 400,
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.METHOD_NOT_ALLOWED: 405,
        ErrorCode.FILE_TOO_LARGE: 413,
        ErrorCode.UNSUPPORTED_FILE: 415,
        ErrorCode.SERVICE_UNAVAILABLE: 503,
        ErrorCode.WORKFLOW_TIMEOUT: 200,
        ErrorCode.INTERNAL_ERROR: 500,
    }


def test_app_error_reads_status_code_from_table():
    """AppError 不传 status_code 时，状态码必须来自映射表。"""
    assert AppError(ErrorCode.NOT_FOUND).status_code == 404
    assert AppError(ErrorCode.FILE_TOO_LARGE).status_code == 413
    assert AppError(ErrorCode.UNSUPPORTED_FILE).status_code == 415
    assert AppError(ErrorCode.BAD_REQUEST).status_code == 400
    assert AppError(ErrorCode.SERVICE_UNAVAILABLE).status_code == 503
    assert AppError(ErrorCode.WORKFLOW_TIMEOUT).status_code == 200
    assert AppError(ErrorCode.INTERNAL_ERROR).status_code == 500


def test_app_error_message_falls_back_to_default():
    """不传 message 时要有兜底文案，不能出现空 message。"""
    for code in ErrorCode:
        err = AppError(code)
        assert err.message, f"{code} 的兜底 message 不能为空"
        assert err.message != "请求失败", f"{code} 应该有专门的兜底文案"


def test_app_error_is_a_real_exception():
    """继承 Exception 才能被 raise 和 except 捕获（这是选 raise 而不是 return 的原因）。"""
    with pytest.raises(AppError) as caught:
        raise AppError(ErrorCode.NOT_FOUND, "数据集不存在")
    assert caught.value.code is ErrorCode.NOT_FOUND
    assert str(caught.value) == "数据集不存在"


@pytest.mark.parametrize(
    "code,expected_status",
    [
        ("BAD_REQUEST", 400),
        ("NOT_FOUND", 404),
        ("METHOD_NOT_ALLOWED", 405),
        ("FILE_TOO_LARGE", 413),
        ("UNSUPPORTED_FILE", 415),
        ("SERVICE_UNAVAILABLE", 503),
        ("WORKFLOW_TIMEOUT", 200),
        ("INTERNAL_ERROR", 500),
    ],
)
def test_app_error_over_http(error_app, code, expected_status):
    """真的走一遍 HTTP：状态码 + 响应体形状都要对。

    这一条是 A-1 那个决定（HTTP 状态码必须真实）的执行证明：
    如果哪天有人改回"业务错误一律 200"，这里会立刻红。
    """
    with TestClient(error_app) as client:
        resp = client.get(f"/boom/{code}")

    assert resp.status_code == expected_status
    body = resp.json()
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == code
    assert body["error"]["message"] == f"测试用错误: {code}"


def test_app_error_can_override_status_code(error_app):
    """同一个错误码需要不同状态码时，显式传 status_code 要能生效。

    真实场景：模型未配置导致无法分析 —— 业务码是 BAD_REQUEST，
    但想让 HTTP 返回 503（服务暂时不可用），因为它不是用户请求写错了。
    """
    with TestClient(error_app) as client:
        resp = client.get("/boom/BAD_REQUEST?status_code=503")

    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert resp.json()["error"]["code"] == "BAD_REQUEST"


def test_error_response_shell_shape_is_identical_everywhere(error_app):
    """错误外壳的字段集合必须完全一致，前端才可能只写一套解析逻辑。"""
    with TestClient(error_app) as client:
        body = client.get("/boom/NOT_FOUND").json()

    assert set(body.keys()) == {"ok", "data", "error"}
    assert set(body["error"].keys()) == {"code", "message", "detail"}
    assert ApiError(**body["error"]).code is ErrorCode.NOT_FOUND
    assert ApiResponse[None](**body).ok is False


def test_detail_never_leaks_local_path(error_app):
    """detail 里绝不允许出现本机绝对路径（截图/录屏/贴 issue 都会暴露它）。"""
    with TestClient(error_app) as client:
        resp = client.get("/wrapped")

    detail = resp.json()["error"]["detail"]
    assert "D:" not in detail
    assert "个人项目01" not in detail
    assert "<path>" in detail
    # 有价值的那部分信息（"打不开什么"）要保留下来，清洗不能把 detail 清空
    assert "打开失败" in detail


def test_sanitize_detail_covers_real_exception_text():
    """直接测清洗函数：覆盖真实异常原文的几种典型形态。"""
    # ① Windows 绝对路径（FileNotFoundError 的典型输出）
    text = (
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'D:\\个人项目01\\ai-data-analyst-agent\\data\\uploads\\a.csv'"
    )
    cleaned = sanitize_detail(text)
    assert "D:" not in cleaned
    assert "个人项目01" not in cleaned
    assert cleaned.startswith("FileNotFoundError")
    # 结尾的引号不能被一起吃掉（否则原文结构被破坏，看起来像我们自己的 bug）
    assert cleaned.endswith("'")

    # ① 含 Windows 用户名。
    #    用户名从环境变量取，不写死成具体某个人 —— QA 指出原来的写法把
    #    "jun" 硬编码进了测试，换台机器跑，那台机器的用户名就漏出去了，
    #    而断言还照样绿（因为测的是别人机器上不存在的字符串）。
    #
    #    但"取不到就编一个 nobody"同样是错的（QA 第三轮的新-2）：
    #    环境里没有用户名时，用户名正则本来就是空的，拿一个**环境里不存在的**
    #    用户名去断言"它必须被盖掉"，必然红 —— 那是在考一个不存在的功能。
    #    取不到就诚实地跳过，并在原因里写清为什么。
    username = os.environ.get("USERNAME") or os.environ.get("USER")
    if not username:
        pytest.skip("环境里没有 USERNAME/USER：用户名清洗分支本就不生效，无需断言")
    reset_username_patterns_cache()  # 确保用的是当前环境，而不是更早某次的结果

    cleaned = sanitize_detail(f"权限不足: C:\\Users\\{username}\\AppData\\Local\\Temp")
    assert username.lower() not in cleaned.lower()
    assert "<path>" in cleaned

    # ②b 没有盘符的用户名路径：换成 <user>（说明不了在哪个盘，但用户名必须盖掉）
    cleaned = sanitize_detail(f"读取失败: Users\\{username}\\notes.txt")
    assert username.lower() not in cleaned.lower()

    # ③ 相对路径（不含用户名，但没必要对客户端暴露我们的目录结构）
    cleaned = sanitize_detail("读取失败: data/uploads/f0e1d2c3.csv")
    assert "data/uploads" not in cleaned
    assert "<uploads>" in cleaned

    # ④ 边界：None 与空串原样返回，不能抛异常
    assert sanitize_detail(None) is None
    assert sanitize_detail("") == ""


def test_unexpected_exception_still_returns_unified_shell():
    """未被业务代码接住的异常：HTTP 500 + 统一外壳 + 不泄露路径。

    **这条测试的第一版写错了，说明写在下面（这是真实踩坑记录）。**

    第一版说"`TestClient(app, raise_server_exceptions=False)` 拿不到我们的外壳，
    它自己会兜底成纯文本 500"，于是绕道用了 httpx 的 ASGITransport。这个结论
    是**错的**：QA 在真实 app 上实测，只要注册了 Exception 处理器，
    `raise_server_exceptions=False` 返回的就是我们的统一外壳。

    第一版为什么看到纯文本 500？因为它探针里造的 `FastAPI()` 只注册了
    AppError 处理器、没注册 Exception 处理器 —— ServerErrorMiddleware 的
    handler 是 None，于是走了 Starlette 的默认纯文本。**测的是自己的探针，
    不是被测对象。**

    现在改成：往真实 app（app.main.app，四个处理器都齐）上临时挂一条会抛错
    的路由，用最直白的 TestClient 写法读响应。
    """
    path = "/__test_dirty"

    # 用真实的 app（app.main.app），不是自己现搭一个 FastAPI ——
    # 四种异常处理器都在它身上注册着，测的才是真实实现。
    async def _dirty() -> None:
        raise RuntimeError(
            r"找不到文件: D:\个人项目01\ai-data-analyst-agent\data\uploads\a.csv"
        )

    app.add_api_route(path, _dirty, methods=["GET"])

    try:
        # 装不上就是设计没落地，先在这里挡住，不要靠后面的响应体断言去猜。
        # 注意断言的是 unexpected_exception_handler：ServerErrorMiddleware 只接收
        # Exception / 500 处理器，AppError 的处理器进的是 ExceptionMiddleware。
        # （最初这里误写成 app_error_handler，任何正确实现都会挂 —— QA 抓出来的。）
        stack = app.build_middleware_stack()
        assert getattr(stack, "handler", None) is unexpected_exception_handler

        # 关键：抛 500 的兜底必须用 unexpected_exception_handler，
        # **不能图省事把 app_error_handler 同时注册给 Exception**。
        # 实测后果很隐蔽：ServerErrorMiddleware 调 app_error_handler(RuntimeError) 时，
        # 它去读 exc.code 直接抛 AttributeError 逃出中间件 —— 客户端拿到一个
        # 空 body、连 content-type 都没有的 500。这正是本测试存在的意义：
        # 两个 handler 分工不同（一个翻译 AppError，一个兜住未知异常），
        # 混用不会报错，只会静默退化。
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(path)
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != path
        ]

    assert resp.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert resp.headers["content-type"].startswith("application/json")
    parsed = resp.json()
    assert parsed["ok"] is False
    assert parsed["data"] is None
    assert parsed["error"]["code"] == "INTERNAL_ERROR"
    # 响应体不能把内部异常里的本机路径回给客户端
    assert "D:" not in parsed["error"]["detail"]
    assert "个人项目01" not in parsed["error"]["detail"]
    # 但服务端要能看到它（这里断言 message 是给用户看的固定文案）
    assert "服务端日志" in parsed["error"]["message"]


def test_sanitize_detail_is_applied_to_success_paths_too():
    """清洗函数是纯函数，不依赖 FastAPI 上下文 —— 工具/报告模块也能直接复用。"""
    assert sanitize_detail("正常文本") == "正常文本"
    assert "C:" not in sanitize_detail("C:\\Windows\\System32\\drivers\\etc\\hosts")


@pytest.fixture(autouse=True)
def _username_patterns_cache_is_never_leaked():
    """每条用例前后都把"用户名正则缓存"复位，消除用例之间的执行顺序依赖。

    为什么必须加这个 fixture（真实踩到的坑）：
        下面那条 username 测试会把缓存刷成 USERNAME="someone" 算出的那一份，
        然后 monkeypatch 在退出时把环境变量改回 "jun" —— 但**缓存不会跟着回退**。
        于是缓存里留着的是 "someone" 的规则，而真实环境是 "jun"。
        谁在它后面执行、谁就拿到一份和当前环境对不上的正则。
        本文件当前的顺序（doctest 排在最后）恰好没踩上，但那是运气，
        不是设计 —— 这种"靠文件顺序才绿"的测试，改个名字就会莫名其妙开始红。

    为什么前后都要复位：
        前面复位 = 别人留下的脏状态不影响我；
        后面复位 = 我不把自己的脏状态留给别人（尤其是 app/errors.py 的 doctest，
        它跑在最后且用的是 `\u4e2d\u6587` 这种与用户名无关的输入，但同样不该建立在脏缓存上）。
    """
    reset_cache = reset_username_patterns_cache
    if reset_cache is not None:
        reset_cache()
    yield
    if reset_cache is not None:
        reset_cache()


def test_username_patterns_are_resolved_at_call_time_not_import_time(monkeypatch):
    r"""用户名正则必须是"用的时候才取"，不能是"import 的时候取一次就冻住"。

    这是 QA 第三轮挖出来的真 bug（编号新-1），和第一阶段 D-2 是同一类毛病：
    老写法把 `_current_username_patterns()` 的结果算进**模块级变量**
    `_PATH_PATTERNS`，于是：

        进程在**没有** USERNAME 的环境里启动（Linux 容器、Windows 服务、
        计划任务、`env -i` 起的进程）→ 那一份快照里用户名正则是空的 →
        之后就算环境变量里有了用户名，无盘符的 `Users\<名字>\...` 也永远
        盖不住。不报错、不 500，只是**静默少了一道防护**。

    怎么测才不是"假绿"？
        **必须真的在运行期改变环境变量，再重新调用。** 如果只断言
        "设了 USERNAME 之后能盖住"，那么老写法照样能过（因为 pytest 进程
        启动时 USERNAME 本来就在，快照里已经有了）—— 那就是假绿。
        所以这里先把环境变量清干净、**并让缓存重新取一次**，模拟"启动时取不到"，
        再设上用户名重新调用。

    为什么要用模块级的 `reset_username_patterns_cache = getattr(...)`？
        因为这条测试存在的意义就是**在老的、有 bug 的实现上失败**。
        如果测试文件里直接 `from app.errors import reset_username_patterns_cache`，
        那么把实现换回老版本时，整个测试模块会 ImportError 收集失败 ——
        看起来"红了"，但红的原因是测试自己引用了不存在的名字，
        而不是"行为回归了"。**能跑的失败才算证据，收集失败的失败不算。**
    """
    # 第一步：模拟"启动时环境里没有用户名"，并让缓存重新取一次 ——
    # 这一步对应真实场景：进程启动时第一次调用就把"空的"那份缓存住了。
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    if reset_username_patterns_cache is not None:
        reset_username_patterns_cache()

    text = r"读取失败: Users\somebody\notes.txt"
    assert sanitize_detail(text) == text, (
        "环境里没有用户名时，这一条本来就不该生效（这是已知边界，不是 bug）"
    )

    # 第二步：运行期把用户名设上，再调用一次。
    monkeypatch.setenv("USERNAME", "someone")
    if reset_username_patterns_cache is not None:
        reset_username_patterns_cache()

    cleaned = sanitize_detail(r"读取失败: Users\someone\notes.txt")
    assert "someone" not in cleaned.lower(), (
        "运行期设上的用户名没有被清洗掉 —— 用户名正则被冻结在 import 时刻了"
    )
    assert cleaned == "读取失败: <path>"

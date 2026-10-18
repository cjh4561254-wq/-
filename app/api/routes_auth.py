"""认证路由：登录、退出、首次初始化、改密码、会话列表。

**这个文件只做 HTTP 翻译**：读请求、调 `app/core/auth.py`、把结果塞进统一响应外壳、
设置/清除 cookie。所有判断（密码对不对、会话有没有过期、要不要限流）都在核心层。
这样"认证逻辑"只有一份，不会出现"路由里的判断和核心层不一致"这种最难查的漏洞。

为什么登录失败一律回同一句话
--------------------------
`LoginFailure.reason` 里其实是分得清的（用户名不存在 / 密码不正确 / 账号已停用），
但**返回给用户的必须是同一句**「用户名或密码不正确」。
区分开来，登录接口就变成了一台"用户名枚举机"：任何人拿一份用户名字典就能
问出哪些账号真实存在，然后只针对这些账号做撞库。
`reason` 只写进服务端日志 —— 排查问题不需要牺牲这个。

响应状态码的选择（都写在 `app/errors.py` 的 ERROR_STATUS 里）：
    200  登录成功 / 退出成功 / 查询状态成功
    401  没登录、会话过期、密码不对
    403  账号库已经有账号了还想再初始化
    429  登录尝试过于频繁（带 `Retry-After`）
    503  账号库没有启用（`AUTH_DB_PATH` 为空）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response

from app.config import get_settings
from app.core.auth import (
    LoginFailure,
    SessionUser,
    authenticate,
    create_session,
    current_user_or_none,
    destroy_session,
    hash_password,
    login_locked_for,
    record_login_failure,
    record_login_success,
    require_user,
    session_cookie_kwargs,
    token_fingerprint,
)
from app.data.users import AuthStoreError, UserRecord, get_auth_store
from app.errors import AppError
from app.schemas import ApiResponse, ErrorCode
from app.schemas_auth import (
    AuthState,
    BootstrapRequest,
    ChangePasswordRequest,
    LoginRequest,
    SessionData,
    UserData,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


def _store():
    """取账号库。没启用就 503 —— 认证不可用必须是"用不了"，不能是"不用认证"。"""
    store = get_auth_store()
    if store is None:
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "账号库没有启用（检查 AUTH_DB_PATH 配置）",
        )
    return store


def _user_data(user: UserRecord) -> UserData:
    """数据库记录 → 响应模型。**在这里做白名单转换**，不是 `model_validate(user)`。

    用 `UserData.model_validate(user, from_attributes=True)` 也能跑通，
    但那样等于说"凡是 UserRecord 上有的字段都可以出去"。
    现在写成逐字段赋值：以后有人往 UserRecord 上加 `reset_token`，
    它**不会**自动出现在 API 响应里 —— 新增字段要显式加一次，
    而"显式加一次"这个动作本身就会让人想一下"这个字段该不该给前端"。
    """
    return UserData(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _needs_bootstrap() -> bool:
    """账号库是不是还空着。空着就允许创建第一个管理员。"""
    store = get_auth_store()
    if store is None:
        return False
    try:
        store.ensure_schema()
        return store.count_users() == 0
    except AuthStoreError:
        # 库坏了就当作"不需要初始化"，让用户去登录页看到真实错误，
        # 而不是把他引到一个必然失败的初始化表单上。
        return False


def _session_expiry(token: str | None, request: Request) -> str | None:
    """当前会话的到期时间（给前端显示"本次登录有效期至 …"）。"""
    if not token:
        return None
    store = get_auth_store()
    if store is None:
        return None
    try:
        session = store.get_session(token_fingerprint(token))
    except AuthStoreError:
        return None
    return session.expires_at if session else None


@router.get("/auth/me", response_model=ApiResponse[AuthState], summary="当前登录状态")
def auth_me(request: Request) -> ApiResponse[AuthState]:
    """**公开接口**：前端启动时第一件事就是问它。

    为什么公开：登录页自己需要知道"要不要显示初始化表单"。
    如果它也需要登录才能访问，就变成了先有鸡还是先有蛋。

    它泄露的信息只有一个布尔值"这个部署有没有账号" —— 这是登录页
    必须知道的东西，而且只要有登录页就藏不住（初始化表单出现与否本身就是信号）。
    账号名、密码、会话数量一概不返回。
    """
    settings = get_settings()
    user = current_user_or_none(request)
    if user is None:
        return ApiResponse[AuthState](
            ok=True,
            data=AuthState(
                authenticated=False,
                user=None,
                needs_bootstrap=_needs_bootstrap(),
            ),
        )
    store = get_auth_store()
    record = store.get_user(user.id) if store else None
    token = request.cookies.get(settings.auth_cookie_name)
    return ApiResponse[AuthState](
        ok=True,
        data=AuthState(
            authenticated=True,
            user=_user_data(record) if record else None,
            needs_bootstrap=False,
            session_expires_at=_session_expiry(token, request),
        ),
    )


@router.post("/auth/bootstrap", response_model=ApiResponse[AuthState], summary="创建第一个管理员账号")
def bootstrap(payload: BootstrapRequest, request: Request, response: Response) -> ApiResponse[AuthState]:
    """**只在账号库为空时可用**的初始化入口。

    为什么不预置一个默认管理员（`admin` / `admin123`）：
    写死的凭据等于没有认证 —— 它会被写进文档、被截图、被留在部署脚本里，
    而且没有人会记得改。真正的做法是"第一次启动时由人设定"。

    创建成功后**直接签发会话**（顺手把人登进去）：用户刚设完密码，
    再让他把刚输的用户名密码重新输一遍是没必要的仪式感。
    """
    store = _store()
    try:
        store.ensure_schema()
        if store.count_users() > 0:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "账号库已经有账号了，不能再执行初始化",
            )
        hashed = hash_password(payload.password)
        user = store.create_user(
            username=payload.username,
            display_name=payload.display_name or payload.username,
            password_hash=hashed.hash_hex,
            salt=hashed.salt_hex,
            iterations=hashed.iterations,
        )
    except AuthStoreError as exc:
        logger.warning("初始化账号失败: %s", exc)
        raise AppError(ErrorCode.INTERNAL_ERROR, "创建账号失败，请查看服务端日志") from exc

    settings = get_settings()
    token, _expires = create_session(user.id, user_agent=request.headers.get("user-agent"))
    response.set_cookie(**session_cookie_kwargs(max_age=settings.auth_session_seconds), value=token)
    logger.info("initialized first admin account: %s", user.username)
    return ApiResponse[AuthState](
        ok=True,
        data=AuthState(authenticated=True, user=_user_data(user), needs_bootstrap=False),
    )


@router.post("/auth/login", response_model=ApiResponse[AuthState], summary="登录")
def login(payload: LoginRequest, request: Request, response: Response) -> ApiResponse[AuthState]:
    """校验凭据并签发会话 cookie。

    顺序很重要：**先查限流，再验密码**。反过来的话，一个被锁定的账号仍然
    可以靠登录接口免费跑 PBKDF2 —— 那就等于给攻击者留了一个 CPU 耗尽入口。
    """
    settings = get_settings()

    locked_for = login_locked_for(payload.username)
    if locked_for > 0:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            f"登录尝试过于频繁，请在 {locked_for} 秒后重试",
            headers={"Retry-After": str(locked_for)},
        )

    _store()
    try:
        user = authenticate(payload.username, payload.password)
    except LoginFailure as exc:
        record_login_failure(payload.username)
        # reason 只进日志，不进响应 —— 理由见模块 docstring。
        logger.warning("login failed for %r: %s", payload.username, exc.reason)
        raise AppError(ErrorCode.UNAUTHORIZED, "用户名或密码不正确") from exc

    record_login_success(payload.username)

    store = get_auth_store()
    if store is not None:
        try:
            store.mark_login(user.id)
        except AuthStoreError as exc:
            # "记下登录时间"失败不该让一个密码正确的人登不进来。
            logger.warning("mark_login failed for %s: %s", user.username, exc)

    token, expires_at = create_session(user.id, user_agent=request.headers.get("user-agent"))
    response.set_cookie(**session_cookie_kwargs(max_age=settings.auth_session_seconds), value=token)
    return ApiResponse[AuthState](
        ok=True,
        data=AuthState(
            authenticated=True,
            user=_user_data(user),
            needs_bootstrap=False,
            session_expires_at=expires_at.isoformat(),
        ),
    )


@router.post("/auth/logout", response_model=ApiResponse[None], summary="退出登录")
def logout(request: Request, response: Response) -> ApiResponse[None]:
    """清会话 + 清 cookie。

    **故意不要求登录**：一个会话已经过期的用户点"退出"，如果这里回 401，
    前端就得处理"退出失败"这个本不该存在的状态。幂等比正确更重要。

    服务端销毁和客户端清 cookie 两件都要做：
      - 只清 cookie：token 还在库里活着，被人捡到照样能用；
      - 只销毁会话：cookie 还在浏览器里，下次请求带着一个死 token，
        前端在"看着还登录着"和"其实已退出"之间反复横跳。
    """
    settings = get_settings()
    token = request.cookies.get(settings.auth_cookie_name)
    destroy_session(token)
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )
    return ApiResponse[None](ok=True, data=None)


@router.post("/auth/password", response_model=ApiResponse[AuthState], summary="修改密码")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: SessionUser = Depends(require_user),
) -> ApiResponse[AuthState]:
    """改自己的密码。要求提供当前密码。

    为什么必须验当前密码：会话 cookie 被偷走时，光有 cookie 就能改密码
    等于把一次会话泄露升级成**永久**账号劫持。要求当前密码之后，
    攻击者还需要知道原密码。

    改完做两件事：
      ① **踢掉该账号的全部会话**（包括当前这条）—— 用户对"改密码"的预期
         就是"别的地方都下线了"，只改哈希而把旧会话留着是反直觉的；
      ② 给**当前设备**重新签发一条会话，否则用户改完密码立刻被弹回登录页，
         体感像"改密码失败了"。这两件事一起做才既安全又不别扭。
    """
    settings = get_settings()
    store = _store()

    record = store.get_user(user.id)
    if record is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "账号不存在或已被删除")

    try:
        authenticate(record.username, payload.current_password)
    except LoginFailure as exc:
        logger.warning("change_password rejected for %s: %s", record.username, exc.reason)
        raise AppError(ErrorCode.UNAUTHORIZED, "当前密码不正确") from exc

    hashed = hash_password(payload.new_password)
    try:
        store.update_password(
            record.id,
            password_hash=hashed.hash_hex,
            salt=hashed.salt_hex,
            iterations=hashed.iterations,
        )
        revoked = store.delete_user_sessions(record.id)
    except AuthStoreError as exc:
        logger.warning("change_password failed for %s: %s", record.username, exc)
        raise AppError(ErrorCode.INTERNAL_ERROR, "修改密码失败，请查看服务端日志") from exc

    token, expires_at = create_session(record.id, user_agent=request.headers.get("user-agent"))
    response.set_cookie(**session_cookie_kwargs(max_age=settings.auth_session_seconds), value=token)
    logger.info("password changed for %s; revoked %d session(s)", record.username, revoked)

    return ApiResponse[AuthState](
        ok=True,
        data=AuthState(
            authenticated=True,
            user=_user_data(record),
            needs_bootstrap=False,
            session_expires_at=expires_at.isoformat(),
        ),
    )


@router.get("/auth/sessions", response_model=ApiResponse[list[SessionData]], summary="我的登录会话")
def list_sessions(
    request: Request,
    user: SessionUser = Depends(require_user),
) -> ApiResponse[list[SessionData]]:
    """列出当前账号的会话（设置页的"登录设备"用）。

    **只返回元信息，不返回 token**（库里存的也只是 SHA-256，服务端自己都拿不出原文）。

    为什么这个接口有存在价值：会话列表是"我发现有人盗用我的账号"时
    唯一能看见的东西。没有它，用户能做的只有"改密码"这一件事。
    """
    settings = get_settings()
    store = _store()
    token = request.cookies.get(settings.auth_cookie_name)
    current_hash = token_fingerprint(token) if token else None

    try:
        rows = store.list_sessions(user.id)
    except AuthStoreError as exc:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "读取会话列表失败") from exc

    return ApiResponse[list[SessionData]](
        ok=True,
        data=[
            SessionData(
                created_at=row.created_at,
                last_seen_at=row.last_seen_at,
                expires_at=row.expires_at,
                current=(row.token_hash == current_hash),
            )
            for row in rows
        ],
    )

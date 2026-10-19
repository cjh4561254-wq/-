"""认证相关的请求/响应模型。

把校验规则放在 pydantic 里（而不是路由函数里手写 if），有三个实际好处：
1. 非法输入在**进入业务代码之前**就被挡住，路由函数里不用再关心"用户名格式对不对"；
2. 错误响应由 `app/main.py` 的 `validation_exception_handler` 统一翻译成项目外壳，
   前端只认一种格式；
3. **规则只有一份**。前端页面上写的提示文案、测试里断言的边界、
   后端实际的判定，说的必须是同一件事 —— 抄两份必然漂移。
   （`tests/test_auth.py` 里有用例直接对着这些常量断言，就是为了让漂移立刻变红。）
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

#: 用户名规则：3-32 位，字母/数字开头，之后允许字母、数字、下划线、点、连字符。
#: 为什么要求字母或数字开头：`.` 或 `-` 开头的用户名在命令行、日志、
#: 以及 `user@host` 这类语境里很容易被当成别的东西。
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")

#: 密码长度边界。上限 128 不是为了安全，是为了防止有人拿 10 MB 的"密码"
#: 让服务端跑 PBKDF2 —— 那是免费的 CPU 耗尽攻击。
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128

#: 常见弱密码。有字母也有数字、长度也够，但排在所有字典的第一页。
#: 不值得为此引一个几十万行的密码字典，挡住最烂的一批就够 ——
#: 剩下的靠长度下限与登录限流。
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty123",
        "qwertyuiop",
        "admin123",
        "administrator",
        "letmein1",
        "welcome1",
        "iloveyou1",
        "abc12345",
        "a1234567",
        "test1234",
        "changeme1",
        "deepseek1",
        "data1234",
    }
)


def _describe_password_rule() -> str:
    """把密码规则写成人话。**只在这里写一次** —— 前端提示、测试断言、文档都引它。"""
    return f"密码至少 {PASSWORD_MIN_LENGTH} 位，且必须同时包含字母和数字"


def validate_password_strength(value: str) -> str:
    """密码强度校验。合法就原样返回，不合法抛 `ValueError`（pydantic 会翻译）。"""
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(_describe_password_rule())
    if len(value) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"密码最多 {PASSWORD_MAX_LENGTH} 位")
    if value.lower() in _COMMON_PASSWORDS:
        raise ValueError("这个密码太常见了，换一个")
    has_letter = any(ch.isalpha() for ch in value)
    has_digit = any(ch.isdigit() for ch in value)
    if not (has_letter and has_digit):
        raise ValueError(_describe_password_rule())
    # 全是同一个字符（"aaaaaaaa" 有字母没数字，到不了这里；"11111111a" 会到），
    # 单独挡一下：`len(set(value)) == 1` 的极端情况。
    if len(set(value)) == 1:
        raise ValueError("密码不能是同一个字符重复")
    return value


def validate_username(value: str) -> str:
    """用户名格式校验。首尾空格会被去掉（用户从别处复制粘贴时很常见）。"""
    cleaned = value.strip()
    if not USERNAME_PATTERN.match(cleaned):
        raise ValueError("用户名需为 3-32 位，以字母或数字开头，可用字母、数字、下划线、点、连字符")
    return cleaned


class LoginRequest(BaseModel):
    """登录请求。

    密码字段**不做强度校验** —— 那是创建/修改密码时的事。
    登录时只需要"非空"：如果一个老账号的密码恰好不符合现在的规则，
    我们也不该在登录这一步把他拦住。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("username")
    @classmethod
    def _clean_username(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("请输入用户名")
        return cleaned


class BootstrapRequest(BaseModel):
    """创建**第一个**管理员账号。

    只在账号库为空时可用（`app/api/routes_auth.py` 里判定）。
    这是「首次部署」的入口：没有任何账号时，总得有办法创建第一个，
    而"预置一个默认管理员 + 默认密码"是明确被禁止的做法
    （写死的凭据等于没有认证）。
    """

    username: str
    display_name: str = Field(default="", max_length=64)
    password: str

    @field_validator("username")
    @classmethod
    def _check_username(cls, value: str) -> str:
        return validate_username(value)

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_password_strength(value)

    @field_validator("display_name")
    @classmethod
    def _clean_display_name(cls, value: str) -> str:
        return value.strip()


class ChangePasswordRequest(BaseModel):
    """修改自己的密码。必须提供当前密码 —— 否则一个被偷走的 cookie
    就能把账号彻底锁死（改掉密码，真正的主人再也进不来）。"""

    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_password_strength(value)


class UserData(BaseModel):
    """返回给前端的账号信息。

    **没有** `password_hash` / `salt` / `iterations` 三个字段，而且不是"忘了加" ——
    校验材料一旦出现在某个响应模型里，早晚会被某个 `model_dump()` 带出去。
    从模型定义上就没有，是唯一可靠的保证。
    """

    id: int
    username: str
    display_name: str
    created_at: str
    last_login_at: str | None = None


class AuthState(BaseModel):
    """`GET /api/auth/me` 的返回体。

    它同时回答三个问题，所以前端**一次请求**就能决定显示什么：
      - `authenticated`：有没有有效会话
      - `user`：是谁
      - `needs_bootstrap`：账号库是不是还空着（决定显示登录表单还是"创建第一个管理员"）
      - `demo_login`：这个部署是否允许「进入演示」一键登录（只影响按钮显不显示，
        真正的开关在后端：没打开时 `POST /api/auth/demo` 一律 403）
    """

    authenticated: bool
    user: UserData | None = None
    needs_bootstrap: bool = False
    session_expires_at: str | None = None
    demo_login: bool = False


class SessionData(BaseModel):
    """一条登录会话（设置页的"登录设备"列表用）。"""

    created_at: str
    last_seen_at: str
    expires_at: str
    current: bool = False

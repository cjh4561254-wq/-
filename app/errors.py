"""业务错误类型 + 错误码 → HTTP 状态码映射 + 敏感信息清洗。

**这个文件解决的问题：错误响应以前有两套规则。**

第一阶段收尾时我们发现，FastAPI 自己抛的错误（路径不存在 404、方法不对 405、
参数校验失败 422）走的是 `{"detail": ...}`，而我们业务代码返回的是
`{"ok": false, "data": null, "error": {...}}`。前端于是必须写两套解析逻辑。

修好之后，还剩一个坑：**业务错误的 HTTP 状态码该怎么定？**
曾经想过"业务错误一律返回 HTTP 200，用 ok:false 表达失败"。这个方案被否掉了，
理由是它会让 `POST /api/datasets/upload` 收到一个 70 MB 的文件时返回 200 ——
HTTP 状态码在说谎，排查问题时打开浏览器开发者工具满屏都是 200，
必须逐个点开响应体才能知道发生了什么。

**最终规则（记住这两句话就够了）：**

    HTTP 状态码表达"HTTP 层面发生了什么"（找不到 404 / 太大 413 / 格式不支持 415）；
    error.code 表达"业务层面发生了什么"（NOT_FOUND / FILE_TOO_LARGE / ...）。
    两者同时给出，不冲突。

前端仍然只需要一套解析逻辑：先看 ok，ok 为 false 就读 error.message。
（前端用 fetch 而不是 axios，4xx 响应不会抛异常，响应体照样能 json() 解析出来。）

**怎么用（业务代码里只需要写一行）：**

    from app.errors import AppError
    from app.schemas import ErrorCode

    if ds is None:
        raise AppError(ErrorCode.NOT_FOUND, f"数据集不存在: {dataset_id}")

状态码由 ERROR_STATUS 自动决定，不用每次手写；异常处理器（app/main.py）
会把它翻译成统一外壳。**不要在路由里手写 JSONResponse 拼错误响应**，
那样等于绕过统一外壳，早晚会出现漏掉某个字段的接口。
"""

import functools
import os
import re

from app.schemas import ErrorCode

# ---------------------------------------------------------------------------
# 错误码 → HTTP 状态码
# ---------------------------------------------------------------------------
# 为什么是 dict 而不是在每个 raise 处手写状态码？
#   ① 状态码是"全项目统一规则"，写在 10 个地方必然有 1 个写错；
#   ② 一处集中后，想调整（比如 413 改成 400）只改一行；
#   ③ 写测试时可以直接断言这张表，不需要把每个接口都调一遍。
ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.BAD_REQUEST: 400,          # 业务参数不合法且不属于下面几种
    ErrorCode.NOT_FOUND: 404,            # 数据集 / 工作流 / 图表不存在
    ErrorCode.METHOD_NOT_ALLOWED: 405,   # HTTP 方法不对（框架抛的，不是业务逻辑）
    ErrorCode.FILE_TOO_LARGE: 413,       # 请求体太大（Payload Too Large）
    ErrorCode.UNSUPPORTED_FILE: 415,     # 文件类型不支持（Unsupported Media Type）
    ErrorCode.SERVICE_UNAVAILABLE: 503,  # 依赖不可用（例：没配大模型密钥）
    ErrorCode.WORKFLOW_TIMEOUT: 200,   # 工作流超时不是"请求失败"，见下方说明
    ErrorCode.INTERNAL_ERROR: 500,     # 服务器内部错误
    # 认证三兄弟。为什么必须是不同的状态码（而不是一律 400）：
    #   401 —— 浏览器和前端框架**认识**这个码，会触发"该登录了"的分支；
    #         把它伪装成 400 就得靠解析 error.code 才知道该弹登录页。
    #   403 —— 与 401 的区别是"我知道你是谁"。前端据此显示"无权限"而不是"去登录"，
    #         否则一个已登录用户会被反复丢回登录页，怎么登都进不去。
    #   429 —— 带 `Retry-After` 才有意义，见下方登录限流的说明。
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.RATE_LIMITED: 429,
}

# 为什么 WORKFLOW_TIMEOUT 是 200 而不是 5xx？
#   因为工作流是**后台异步执行**的：提交请求（POST /api/analysis）早已返回 202，
#   超时发生在那之后的轮询里。轮询请求（GET /api/workflows/{id}）本身成功了，
#   它成功地告诉客户端"这个工作流已经失败"。所以 HTTP 层面是 200，
#   "工作流失败了"这件事由 data.status == "FAILED" 和 error.code 表达。
#
# 5xx 也没有出现在这张表的业务错误里：500 属于"我们没预料到的 bug"，
# 不应该由业务代码主动 raise（见 INTERNAL_ERROR 的说明）。


# 每个错误码的默认用户可读文案。
# 路由可以传更具体的 message 覆盖它；不传时用它兜底，避免出现空 message。
DEFAULT_MESSAGE: dict[ErrorCode, str] = {
    ErrorCode.BAD_REQUEST: "请求不合法",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.METHOD_NOT_ALLOWED: "请求方法不支持",
    ErrorCode.FILE_TOO_LARGE: "文件太大",
    ErrorCode.UNSUPPORTED_FILE: "文件类型不支持",
    ErrorCode.SERVICE_UNAVAILABLE: "服务暂时不可用，请稍后重试",
    ErrorCode.WORKFLOW_TIMEOUT: "分析超时",
    ErrorCode.INTERNAL_ERROR: "服务器内部错误",
    ErrorCode.UNAUTHORIZED: "请先登录",
    ErrorCode.FORBIDDEN: "没有权限执行这个操作",
    ErrorCode.RATE_LIMITED: "尝试过于频繁，请稍后再试",
}


class AppError(Exception):
    """业务错误。会被 app/main.py 的异常处理器翻译成统一响应外壳。

    三个参数的含义（和 ApiError 的三个字段一一对应）：
        code      业务错误码，决定 HTTP 状态码（查 ERROR_STATUS）
        message   给用户看的话。**不要塞技术细节**，写人话
        detail    给开发者排查用的话。可以塞异常原文，但会先被清洗掉本机路径
        status_code  一般不用传。只有"同一个错误码需要不同状态码"时才传
                     （例：模型未配置导致无法分析 → BAD_REQUEST 但想返回 503）
        headers   要挂在响应上的头。极少数情况才需要 —— 目前只有两处：
                     ① 401 的 `WWW-Authenticate`（RFC 7235 要求）；
                     ② 429 的 `Retry-After`（告诉客户端"过几秒再来"，
                        没有它前端只能瞎猜重试间隔）。
                  它存在的理由是 `StarletteHTTPException` 的处理器**已经**在转发
                  头了（见 app/main.py 里那句 "exc.headers 必须转发"），
                  而业务代码走的是 `AppError` 这条路径 —— 如果这里不转发，
                  同一个状态码经由两条路径会得到不同的响应头，这种不一致最难查。
                  默认 `None`，绝大多数 AppError 不设它。

    为什么继承 Exception 而不是直接 return 错误响应？
        raise 可以在**任意深度**的代码里抛出（工具函数、加载器、Agent 里），
        而且不会被"忘了 return"这种低级错误漏掉；统一在一个地方翻译，
        也保证了响应格式不会因为某人手写 JSON 而不一致。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        detail: str | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_MESSAGE.get(code, "请求失败")
        self.detail = detail
        self.status_code = status_code or ERROR_STATUS.get(code, 500)
        self.headers = headers
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# 敏感信息清洗
# ---------------------------------------------------------------------------
# 为什么需要它？
#   ApiError.detail 里经常会塞异常原文，而 Python 的异常原文里带绝对路径是常态：
#       FileNotFoundError: [Errno 2] No such file or directory:
#           'D:\\个人项目01\\ai-data-analyst-agent\\data\\uploads\\abc.csv'
#   这个项目要拿去面试演示（截图、录屏、把响应体贴进 README），
#   把本机目录结构和 Windows 用户名暴露出去没有必要，而且不好看。
#
# 用什么方式？分两步：固定片段用 str.replace，路径用正则。
#   盘符之后的内容长度不定（D:\a\b.csv 还是 D:\很长\的\目录\文件.csv），
#   固定字符串替换做不到；这里是正则真正该出场的地方。
#   写正则时用原始字符串 r"..."，反斜杠就不用一路转义成 \\\\ 了。
#
# 已知边界（写在文档里，不要假装它能防住一切）：
#   ① 只处理 Windows 路径（这个项目开发环境是 Windows 11）；
#   ② 它是一道**兜底防线**，不是许可证 —— 写 detail 时仍然不应该主动拼本机路径。
#   ③ 用户名靠 os.environ["USERNAME"] 动态取；取不到时（例如以后放进 Linux
#      容器）跳过用户名匹配，此时用户名会跟着盘符一起被 <path> 盖住。
#   ④ 用户名匹配**只在第一次用到时编译一次**（_username_patterns 的 lru_cache）。
#      取不到用户名的那次结果会被缓存住，所以启动后再设 USERNAME 需要显式
#      reset_username_patterns_cache()。正常情况下没人会这么干，这里写出来
#      是为了让测试能验证"运行期真的会重新取"，而不是靠 import 期的一份快照。

PATH_PLACEHOLDER = "<path>"
USER_PLACEHOLDER = "<user>"

_RELATIVE_PREFIXES = ("data\\uploads", "data/uploads")

# "路径可能结束"的字符集合：空白 / 引号 / 括号 / 中英文逗号分号都不要。
# 异常原文常写成 ("... 找不到: 'D:\\a.csv'")，不在这里停下就会把引号和
# 右括号一起吃掉，返回的文本连结构都坏了。
_PATH_END = r"[^\s\"'()\[\],;，；]"


def _current_username_patterns() -> list[str]:
    """拼出"当前这台机器上的用户名路径"的几种写法。

    为什么不在源码里写死用户名？
        写死等于把某一个人的机器信息提交进仓库；同事 clone 下来跑，
        他那台机器的路径照样会漏出去，而测试还会因为断言跟着写死而变绿。
        从环境变量取，代码跑到哪台机器就保护哪台机器。
    """
    username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not username:
        return []
    # 大小写不敏感：Windows 路径不区分大小写，用 [jJ][uU][nN] 这种范围写法
    # 比手写 "jun|Jun|JUN" 短得多，也不会漏掉大小写混写的组合。
    user_fold = "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else re.escape(c) for c in username)
    return [
        rf"(?i:users)[\\/]{user_fold}[\\/]{_PATH_END}*",
        rf"(?i:documents and settings)[\\/]{user_fold}[\\/]{_PATH_END}*",
    ]


# 为什么这个也要缓存，而不像 _current_username_patterns() 那样每次都重新拼？
#   正则编译不便宜，而 sanitize_detail 是错误路径上的函数 —— 它被调用时
#   往往已经有别的东西出问题了，不该再给它添开销。
#
# 为什么这个缓存不会重演"配置冻结在 import 时刻"的老毛病（第一阶段踩过）？
#   区别在**取值时机**：老写法是模块 import 时就把结果算进模块级变量，
#   之后无论环境怎么变都永远用那一份；现在改成了"第一次真正要用的时候才算"，
#   而第一次真要用，必然是某个请求出错的时候 —— 那时代码早就启动完了。
#   也就是说，它拿到的一定是"启动完成后的环境"，这正是我们想要的。
@functools.lru_cache(maxsize=1)
def _username_patterns() -> tuple[re.Pattern[str], ...]:
    """取（并缓存）跟用户名相关的路径正则。环境里没有用户名时返回空元组。"""
    return tuple(re.compile(p) for p in _current_username_patterns())


def reset_username_patterns_cache() -> None:
    """清掉用户名正则缓存，让下次调用重新读环境变量。

    正常业务流程里**不需要**调它（没有人会在服务跑起来之后改 USERNAME）。
    它存在的唯一理由是让测试能证明"运行期确实会重新取"，
    而不是像第一阶段那样绑死在 import 时刻。
    """
    _username_patterns.cache_clear()


# 不依赖环境变量的两条规则，模块级编译一次即可。
# "C:\..." 或 "d:/..."：一个字母 + 冒号 + 斜杠，后面全是路径字符。
_DRIVE_PATTERN = re.compile(rf"(?i)\b[a-z]:[\\/]{_PATH_END}*")
# UNC 路径 \\服务器\共享\文件
_UNC_PATTERN = re.compile(rf"\\\\{_PATH_END}+")


def sanitize_detail(text: str | None) -> str | None:
    r"""把 detail 里的本机绝对路径与 Windows 用户名替换成占位符。

    输入 None 或空字符串时原样返回（ApiError.detail 允许为空）。

    例子（字符串前面加 r 表示反斜杠不当转义符，Windows 路径要这样写）：

        >>> sanitize_detail(r"找不到 D:\个人项目01\data\uploads\a.csv")
        '找不到 <path>'
        >>> sanitize_detail("路径含用户名: C:\\Users\\jun\\a.csv")
        '路径含用户名: <path>'

    注意两点取舍：

    1. 清洗是**贪婪**的：盘符之后的内容（含目录与文件名）会被整段替换。
       这是有意的 —— 排查问题需要的是异常类型（FileNotFoundError），
       而不是本机目录结构；文件名在上传场景下本来也是 uuid，没有信息量。
    2. 不含盘符的用户名路径换成 `<user>` 而不是 `<path>`：`Users\jun\x.csv`
       没有盘符时说明不了"在哪个盘"，但用户名的泄露是确定的，所以单独兜住。
       这一条**只在环境变量里有 USERNAME/USER 时才生效**（详见文件顶部的边界③）。
    """
    if not text:
        return text

    result = text

    # 顺序有讲究：用户名规则排在前面，这样 "Users\jun\x.csv" 这种**没有盘符**的
    # 片段不会因为缺少盘符而漏掉；带盘符的路径先被用户名规则吃掉一段，
    # 剩下的盘符段仍会被盘符规则吃掉，最终整体变成 <path>。
    patterns = (*_username_patterns(), _DRIVE_PATTERN, _UNC_PATTERN)

    for pattern in patterns:
        # count=1 + 循环，而不是一次性 sub 全部：subn 会给出一共替换了几处，
        # 用它当循环条件比"再搜一遍看还有没有匹配"更直接。
        # 一段文本里可能同时有 C:\ 和 D:\ 两条路径，所以不能只替换一次。
        while True:
            replaced, count = pattern.subn(PATH_PLACEHOLDER, result, count=1)
            if count == 0:
                break
            result = replaced

    # 相对路径也要盖掉：data/uploads/xxx.csv 属于"我们的目录结构"，
    # 虽然不含用户名，但演示时没必要暴露。这是固定字符串，用 replace 就够了。
    for prefix in _RELATIVE_PREFIXES:
        result = result.replace(prefix, "<uploads>")

    return result

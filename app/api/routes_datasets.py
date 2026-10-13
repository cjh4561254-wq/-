"""数据集接口：上传、详情、数据画像。

这一层负责什么、不负责什么
--------------------------
负责（**只做接线**）：
    把 HTTP 请求翻译成"一串字节 + 一个扩展名"，按固定顺序走完上传的整个生命周期
    —— 校验扩展名 → 生成 id → 分块读并累计大小 → 算 sha256 → 落盘 → 解析 → 登记，
    最后把结果装进统一外壳 `ApiResponse`。

不负责：
    怎么读文件（`app/data/loader.py`）、数据放在哪（`app/data/store.py`）、
    指标怎么算（`app/data/profiler.py`）。所以这个文件里**一行 pandas 都不写**：
    三个模块各管一件事，接口层只决定"先调谁、后调谁、出错怎么办"。

错误处理只有一种写法：`raise AppError`。
    HTTP 状态码查 `app/errors.py` 的 `ERROR_STATUS` 表，响应外壳由 `app/main.py`
    的 handler 统一翻译。**这里不写 JSONResponse** —— 只要有一个人绕过外壳，
    前端就得为这一个接口单独写一套解析逻辑，统一外壳就白做了。
    上传字节的顺序依据是 docs/DECISIONS.md 的 D-09，状态码表是 D-01。
"""

import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, UploadFile

from app import config
from app.data.loader import load_dataframe
from app.data.profiler import build_profile
from app.data.store import build_stored_filename, store
from app.errors import AppError
from app.schemas import ApiResponse, ErrorCode
from app.schemas_dataset import ALLOWED_EXTENSIONS, Dataset, DatasetProfile

router = APIRouter(prefix="/datasets", tags=["datasets"])

# 一次读多少字节。1 MB 是个折中：太小会让 await 的往返次数暴涨（10 MB 要读 1 万次），
# 太大就失去"边读边计数"的意义 —— 我们要的正是"不用等整个文件读完就知道超限"。
CHUNK_SIZE = 1024 * 1024


def _extension_of(filename: str | None) -> str:
    """从用户文件名里取扩展名：小写、不带点；取不到就返回空串，交给白名单去拒。

    为什么不看 `UploadFile.content_type`：客户端报的 MIME 类型是**它自己说的**，
    把 exe 改名说成 text/csv 毫无难度。可用的证据只有"扩展名在白名单里"
    加上"内容真的能解析成功"（后者在 load_dataframe 里做），别的都不算证据。
    """
    if not filename:
        return ""
    return Path(filename).suffix.lstrip(".").lower()


def _unsupported_detail(ext: str) -> str:
    """不支持的类型要告诉用户"下一步怎么办"，不能只丢一句"不支持"。"""
    allowed = " / ".join(ALLOWED_EXTENSIONS)
    if ext == "xls":
        # 旧版 .xls 是用户最容易踩的坑（Excel 的"另存为"默认就是它），所以单独给话术。
        # 这句话在 app/data/loader.py 的防御性分支里还有一份 —— 那道是给"绕过路由
        # 直接调 loader"的情况兜底的，用户实际看到的是这里这句。改文案要两处一起改。
        return "暂不支持旧版 .xls 格式，请在 Excel 里另存为 .xlsx 后再上传"
    if not ext:
        return f"文件名里没有扩展名，只支持 {allowed}"
    return f"只支持 {allowed}，收到 .{ext}"


async def _read_with_limit(upload: UploadFile, settings: config.Settings) -> bytes:
    """分块读上传内容，边读边累计大小，一旦超限**立刻**抛 413。

    为什么不是"先整个读进内存、再 len() 判断"：
        那样超限的文件已经被完整吃进内存了，上限形同虚设 —— 上传 500 MB 会先
        把内存撑爆，然后才轮到我们说不。这里读一块、记一次数，超限之后**一个
        字节都不再读**，磁盘上也不会出现半个文件，于是"失败后清理磁盘"这条
        本身会失败的补偿动作就不需要存在了（docs/DECISIONS.md D-09）。

    为什么允许把没超限的字节先在内存里攒着：
        本项目上限 10 MB，这是**有意识的取舍**。如果以后把 MAX_UPLOAD_MB 提到
        几百 MB，这里必须改成"边读边写临时文件"，不能照抄。
    """
    limit = settings.max_upload_bytes
    content = bytearray()
    while True:
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        if len(content) + len(chunk) > limit:
            raise AppError(
                ErrorCode.FILE_TOO_LARGE,
                f"文件太大，上限是 {settings.max_upload_mb} MB",
                detail=(
                    f"读到第 {len(content) + len(chunk)} 字节时超过上限（{limit} 字节），"
                    "没有写入磁盘"
                ),
            )
        content += chunk
    return bytes(content)


def _remove_quietly(path: Path) -> None:
    """删掉刚落盘的、解析失败的文件；删不掉也不能把原来的错误盖掉。

    为什么必须 try：删除自己也会失败（Windows 上文件句柄没放开、权限不足）。
    这时用户需要知道的是"你的文件解析失败"，而不是一个"删除失败"的次生异常
    —— 后者会把真正的原因挡在后面，排查会从"文件格式"跑偏到"磁盘权限"。
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"[warn] 清理无法解析的文件失败：{path.name}（{exc!r}）")


@router.post("/upload", response_model=ApiResponse[Dataset], summary="上传 CSV / Excel")
async def upload_dataset(
    file: UploadFile = File(..., description="csv 或 xlsx 文件"),
) -> ApiResponse[Dataset]:
    """接收一个文件，落盘、解析、登记，返回元信息与前 5 行预览。

    七步的顺序不能换（docs/DECISIONS.md D-09）：前两步是安全，第三步是容量，
    后面是"先有文件、再解析、最后才登记"。每一步的位置都对应一个具体的失败场景，
    顺序写反了就会在磁盘上留下垃圾，或者留下"有元信息、却没文件"的假记录。
    """
    settings = config.get_settings()  # 配置在函数里现取，见 docs/ARCHITECTURE.md §4.0

    # ① 扩展名白名单。**必须在任何写盘动作之前**：不然攻击者能用 "evil.csv" 这个名字
    #    把任意内容先落进上传目录，再靠后续 400 把响应伪装成"只是解析失败"。
    original_filename = file.filename or ""
    ext = _extension_of(original_filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise AppError(
            ErrorCode.UNSUPPORTED_FILE,
            f"不支持的文件类型：{ext or '（没有扩展名）'}",
            detail=_unsupported_detail(ext),
        )

    # ② id 由服务端生成，固定是 uuid4().hex（32 位小写十六进制）。
    #    用带连字符的 str(uuid4()) 会被 build_stored_filename 拒掉 —— 这是故意的：
    #    落盘名的形态在 store 里有一道会炸的检查，不靠"调用方记得传对"。
    dataset_id = uuid4().hex

    # ③ 分块读 + 累计大小：超限在这里就抛 413，磁盘上什么都不会有。
    raw_bytes = await _read_with_limit(file, settings)

    # ④ sha256 算的是"用户原始上传的字节"（Dataset.sha256 就是这个语义），
    #    不是我们重新序列化出来的副本 —— 将来拿它判断"是不是同一个文件"才准。
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    # ⑤ 落盘。路径 = 上传目录 / {dataset_id}.{ext}，用户文件名一个字符都不参与。
    stored_filename = build_stored_filename(dataset_id, ext)
    target = settings.upload_path / stored_filename
    # 上传目录由 main.py 的 lifespan 在启动时建好；这里**只复核路径、不 mkdir**：
    # 再 mkdir 一次会把"目录建不出来"这种环境问题悄悄藏起来，
    # 而它应该在上传第一个文件时就暴露。
    if target.resolve().parent != settings.upload_path.resolve():
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "落盘路径不合法",
            detail="目标文件不在上传目录内",
        )
    target.write_bytes(raw_bytes)

    # ⑥ 真读一遍。解析失败（不是 CSV、坏 Excel、超行列上限……）→ 400，并且把刚写的
    #    文件删掉：解析不了的文件留在磁盘上只会越积越多，而用户重传时又会生成新的一份。
    try:
        frame, encoding = load_dataframe(target, ext)
    except Exception:
        # 这里必须宽捕再原样抛出：不管是 AppError（loader 给的 4xx）还是别的异常，
        # 先把磁盘清干净，再交给 main.py 的 handler 去翻译成响应外壳。
        _remove_quietly(target)
        raise
    print(
        f"[upload] {stored_filename} 解析成功："
        f"{frame.shape[0]} 行 {frame.shape[1]} 列，编码 {encoding or 'xlsx 自带'}"
    )

    # ⑦ 落盘成功之后才登记：写盘失败的话异常在第 ⑤ 步就抛了，字典里不会留下
    #    "有元信息、却没有文件"的假记录（这条顺序在 store.py 里也有同样的讲究）。
    dataset = store.register(
        dataset_id,
        frame,
        original_filename=original_filename,
        file_ext=ext,
        size_bytes=len(raw_bytes),
        sha256=sha256,
    )
    return ApiResponse[Dataset](ok=True, data=dataset, error=None)


# 下面两个接口用同步 def（不是 async def）：FastAPI 会把同步函数放进线程池执行。
# 画像要真的遍历每一列算 pandas 统计量，写成 async 会堵住事件循环 —— 一个用户点
# "生成画像"的期间，其他用户的请求全都排队等着。上传那个必须是 async，因为它要
# await 读文件。（Flask / Django 没有这个区别，这是 ASGI 框架特有的坑。）


@router.get("/{dataset_id}", response_model=ApiResponse[Dataset], summary="数据集详情")
def get_dataset(dataset_id: str) -> ApiResponse[Dataset]:
    """按 id 取元信息（含前 5 行预览）。没登记过 → 404。"""
    dataset = store.get(dataset_id)
    if dataset is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "数据集不存在",
            # detail 里**不回显** dataset_id：它是 URL 里来的、长度不受控，
            # 回显等于把用户的输入原样写进我们的响应体，没必要。
            detail="这个 dataset_id 没有登记过。数据存在服务进程的内存里，服务重启后全部丢失。",
        )
    return ApiResponse[Dataset](ok=True, data=dataset, error=None)


@router.get("/{dataset_id}/profile", response_model=ApiResponse[DatasetProfile], summary="数据画像")
def get_dataset_profile(dataset_id: str) -> ApiResponse[DatasetProfile]:
    """按需（惰性）计算画像，算完缓存起来，之后直接返回缓存。

    为什么不在上传时就算：上传接口要让用户马上看到预览，而画像是全表扫描
    （20 万行 × 200 列不便宜）。谁需要谁付这个钱 —— 不点"生成画像"就不算。
    缓存能安全地做，是因为 store 里 `_profiles` 和 `Dataset.has_profile` 是
    一起改的，前端据此就能区分"还没算"（has_profile=false）和"算完了"（true）。
    """
    cached = store.get_profile(dataset_id)
    if cached is not None:
        return ApiResponse[DatasetProfile](ok=True, data=cached, error=None)

    frame = store.get_frame(dataset_id)
    if frame is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "数据集不存在",
            detail="没法为不存在的 dataset_id 生成画像。数据存在内存里，服务重启后全部丢失。",
        )

    profile = build_profile(frame, dataset_id)
    store.set_profile(dataset_id, profile)
    return ApiResponse[DatasetProfile](ok=True, data=profile, error=None)

"""数据集登记处：进程内的一张"dataset_id → 数据"表。

这个模块负责什么
----------------
1. 记住一份数据的元信息（`Dataset`）和 DataFrame 本身，供接口按 id 取用。
2. 缓存画像（`DatasetProfile`），同时把 `Dataset.has_profile` 置成 True。
3. 提供 `build_stored_filename()`，让"落盘文件名只由 dataset_id + 扩展名决定"
   成为一条**有测试的代码**，而不是一句口头约定。

这个模块不负责什么
-----------------
- 不生成 `dataset_id` —— 由路由层生成（理由见下）。
- **不写任何文件** —— 上传的原始字节由路由层落盘（理由见下）。
- 不做任何统计计算 —— 画像在 `app/data/profiler.py`。
- 不解析文件 —— 解析在 `app/data/loader.py`。
- 不知道 HTTP 的存在：没有 request / response，也不知道状态码是从哪来的。
- 不校验扩展名白名单：那是路由层的事（只有它手里有 `UploadFile` 和文件字节）。

为什么 id 由路由层生成、落盘也在路由层（而不是 `store.create()` 一把做完）
----------------------------------------------------------------------------
`build_profile(df, dataset_id)` 需要 id，`Dataset` 里也要有 id，而这两件事都发生在
"文件已经写下去之后"。如果 id 由 store 在自己内部生成，路由层就得先拿到 `Dataset`
才能拼出画像参数，顺序会变成"store 落盘 → 返回 dataset → 路由再读一遍文件"，
多读一遍文件换不来任何好处。所以：**生成 id 是路由层的第一步，store 只拿它当键用。**

代价是"磁盘文件名必须等于 `{dataset_id}.{ext}`"这条性质不能只靠 store 自己保证，
于是把它抽成模块级函数 `build_stored_filename()`：路由层写盘前用它拼路径，
`register()` 内部用同一个函数填 `Dataset.stored_filename`，两边天然一致。

为什么数据放在进程内的字典里（而不是数据库 / Redis）
---------------------------------------------------
这是求职作品集项目，单进程 uvicorn 跑起来，数据活到进程结束就够了。
代价必须写清楚：**多进程部署（`uvicorn --workers 2` 以上）会各存一份，
进程重启就全丢**。docs/ARCHITECTURE.md §8 把这个边界记在"只能单进程"那一栏。

关于 `Dataset.sha256` 的含义（重要，改代码前先看这段）
------------------------------------------------------
它指的是**用户上传的那份原始字节**的 sha256，用来回答"这两个文件是不是同一份"。

路由层是"分块读进内存 → 对读到的字节算 sha256 → 再把同一串字节写盘"，
所以磁盘内容与 `sha256` / `size_bytes` 严格一致：落盘的不是"另存一份 DataFrame"，
中途也不做任何换行符 / 编码转换。
"""

import math
import re
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from app.errors import AppError
from app.schemas import ErrorCode
from app.schemas_dataset import ALLOWED_EXTENSIONS, Dataset, DatasetProfile

# 预览给前几行。前端表格只展示一小段，多了既占带宽又没人看。
PREVIEW_ROWS = 5

# dataset_id 的合法形态：uuid4().hex，32 位小写十六进制。
_DATASET_ID_PATTERN = re.compile(r"\A[0-9a-f]{32}\Z")

# 数据集列表的排序白名单：**接口参数名 → 取值函数**。
#
# 为什么不直接用 pydantic 的字段名（created_at / original_filename / row_count）：
# 接口要对前端友好，所以用 `uploaded_at` / `filename` / `rows` 这三个名字
# （前端 `app/web/app.js:982-1004` 已经按这三个名字在传参）。两套名字的对应
# 关系只在这里写一次，别处不许再抄一份 —— 抄出来的映射迟早会分叉。
#
# 为什么 `filename` 用小写做排序键：用户在界面上看到的是文件名列的字母序，
# 而 ASCII 里大写字母排在小写前面，直接按原串排会得到 "Zebra" 在 "apple" 之前
# 这种"看起来没排"的结果。中文文件名不受影响（没有大小写）。
_SORT_KEYS: dict[str, "Callable[[Dataset], object]"] = {
    "uploaded_at": lambda item: item.created_at,
    "filename": lambda item: item.original_filename.lower(),
    "rows": lambda item: item.row_count,
}


def build_stored_filename(dataset_id: str, file_ext: str) -> str:
    """拼出落盘文件名：`{dataset_id}.{file_ext}`，并**真的检查**这两个输入。

    路由层写盘和 `register()` 填 `Dataset.stored_filename` 都调这一个函数，
    所以"磁盘上的文件名"和"元信息里记的文件名"不可能不一致。

    这里为什么要做检查（QA 提过的一个很对的质疑）
    --------------------------------------------
    这个函数最初只写了一句 f-string，docstring 里用"dataset_id 是我们自己生成的
    32 位十六进制，天然安全"来解释它为什么不用校验。那句话**描述的是调用方的
    意图，不是这个函数的事实** —— 谁都可以传别的东西进来，而这里恰好是拼路径的
    地方。实测过：

        build_stored_filename('../../../evil', 'csv')        -> '../../../evil.csv'
        build_stored_filename('..\\\\..\\\\evil', 'csv')         -> '..\\\\..\\\\evil.csv'
        build_stored_filename('C:\\\\Windows\\\\Temp\\\\x', 'csv')   -> 'C:\\\\Windows\\\\Temp\\\\x.csv'
        build_stored_filename('', 'csv')                     -> '.csv'

    `original_filename` 不参与拼接是对的（它可能带 `../../`），但"只拼了 id"
    不等于"拼出来的东西安全"。所以这里两道：

    1. `dataset_id` 必须匹配 `\\A[0-9a-f]{32}\\Z`。路径分隔符、`..`、盘符、空串
       全都进不来 —— 这是**格式**上的第一道闸。
    2. `file_ext` 必须在 `ALLOWED_EXTENSIONS` 里。它也来自请求，同样不能默认它是干净的。

    两道都抛 `INTERNAL_ERROR`（不是 BAD_REQUEST）：`dataset_id` 是服务端自己生成的，
    走到这里还不合法说明是**我们的** bug，不是用户填错了什么。真正的边界防护不靠
    这里 —— 路由层写完盘还会用 `resolve()` 复核父目录（见 §10 安全清单），
    这一层是"让错误更早、更明确地炸"，不是唯一的防线。

    面试常问："你这个地方为什么要校验一个自己生成的值？"
        答：因为这是拼文件路径的地方。安全边界要放在**使用点**，不能放在
        "调用方应该传对"这个假设上；假设不会在代码里留下痕迹，检查会。
    """
    if not _DATASET_ID_PATTERN.match(dataset_id):
        # 注意 detail 里不能回显原始值：它可能很长，也可能带换行塞进日志。
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "数据集编号不合法",
            detail="dataset_id 必须是 32 位小写十六进制",
        )
    if file_ext not in ALLOWED_EXTENSIONS:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "扩展名不合法",
            detail=f"file_ext 必须是 {' / '.join(ALLOWED_EXTENSIONS)} 之一",
        )
    return f"{dataset_id}.{file_ext}"


class DatasetStore:
    """进程内的数据集仓库。用模块末尾的 `store` 单例访问，业务代码不要自己 `new`。"""

    def __init__(self) -> None:
        # 三个字典共用同一个 dataset_id 做键：
        #   _frames   真正的数据（DataFrame 只存在这里，见 schemas_dataset.py 的说明）
        #   _datasets 元信息（能直接返回给前端的那部分）
        #   _profiles 画像缓存（第一次请求画像时算出来，之后直接给）
        self._frames: dict[str, pd.DataFrame] = {}
        self._datasets: dict[str, Dataset] = {}
        self._profiles: dict[str, DatasetProfile] = {}

    def register(
        self,
        dataset_id: str,
        df: pd.DataFrame,
        original_filename: str,
        file_ext: str,
        size_bytes: int,
        sha256: str,
        *,
        owner_user_id: int | None = None,
        owner_username: str | None = None,
    ) -> Dataset:
        """登记一份新数据：记元信息 + 记住 DataFrame，返回 `Dataset`。

        调用顺序（路由层）：**先落盘、再 register**。文件写失败时异常在落盘那一步
        就抛出去了，根本走不到这里，所以字典里不会留下一条"有元信息、却没有文件"
        的假记录 —— 这是把落盘放在 store 外面之后顺带得到的好处。

        `dataset_id` 由调用方生成，`file_ext` 必须是 csv / xlsx（`Dataset` 的字段类型
        会校验，别的值在这里会变成 pydantic 的校验错误 —— 路由层在写盘之前就拒掉了）。
        撞上已存在的 id 抛 500：正常路径永远撞不上（uuid4 有 2^122 种可能），
        撞上就是调用方的 bug，不能悄悄覆盖掉别人的数据。

        `original_filename` 只作为**说明文字**存进元信息，绝不参与拼路径 ——
        用户上传的名字里可能有 `../../` 之类的东西。

        `owner_user_id` / `owner_username` 必须是**关键字参数**（所以前面有个 `*`）：
        它是数据隔离的依据，而位置参数一旦允许，就会有人按顺序传错一个 int
        而完全不报错 —— 把别人的数据标成自己的，是查不回来的事故。
        默认 `None`（没有归属）是给"改造前的老调用方"和测试留的退路，
        路由层**一定**显式传当前登录用户。

        返回的 `Dataset.has_profile` 一定是 False：画像要等 profiler 算完，
        再通过 `set_profile` 补上（这样前端能分辨"还没算"和"算完了是空的"）。
        """
        # 先查重再登记：覆盖别人的数据是查不回来的事故，宁可当场报错。
        if dataset_id in self._datasets:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "数据集编号重复",
                detail=f"dataset_id={dataset_id!r} 已经登记过，不会覆盖",
            )

        dataset = Dataset(
            dataset_id=dataset_id,
            original_filename=original_filename,
            row_count=int(df.shape[0]),
            column_count=int(df.shape[1]),
            created_at=datetime.now(timezone.utc),
            has_profile=False,
            owner_user_id=owner_user_id,
            owner_username=owner_username,
            stored_filename=build_stored_filename(dataset_id, file_ext),
            file_ext=file_ext,
            size_bytes=int(size_bytes),
            sha256=sha256,
            columns=[str(name) for name in df.columns],
            dtypes={str(name): str(dtype) for name, dtype in df.dtypes.items()},
            preview_rows=_preview_rows(df),
        )

        self._frames[dataset_id] = df
        self._datasets[dataset_id] = dataset
        return dataset

    def get(self, dataset_id: str) -> Dataset | None:
        """取元信息；不存在返回 None（由调用方决定是 404 还是别的处理）。

        **注意：这里不做归属判断**（"谁的数据"不在数据层的职责里，
        而且它拿不到登录用户）—— 归属判定在 `app/api/ownership.py`。
        任何直接调 `get()` 就返回给用户的写法都是越权漏洞。
        """
        return self._datasets.get(dataset_id)

    def get_frame(self, dataset_id: str) -> pd.DataFrame | None:
        """取 DataFrame；不存在返回 None。

        注意这里返回的是**同一个对象**，不是副本：DataFrame 可能有 20 万行，
        每次取都复制一份等于把内存翻倍。所以调用方只读，不要就地修改它。
        """
        return self._frames.get(dataset_id)

    def list_owned(
        self,
        owner_user_id: int,
        *,
        sort: str = "uploaded_at",
        order: str = "desc",
        query: str | None = None,
    ) -> list[Dataset]:
        """某个用户名下的数据集，排序后返回（分页在路由层切片）。

        为什么归属判定写成"精确匹配 user_id"、而不是"owner 为空也算我的"：
        进程内单例里可能有改造之前登记的、没有归属的数据。**没有归属 = 谁也不给看**，
        是这里唯一安全的选择 —— 反过来（无归属者视为公共数据）会让升级后
        老数据对所有登录用户可见，那正是本次改造要堵掉的洞。

        为什么返回新列表：调用方会切片。直接给出内部列表的引用，
        调用方一个 `pop()` 就能改掉仓库内部状态（`registry.list_all()` 同一个理由）。

        为什么排序参数放在**数据层**而不是路由层：路由层拿到的已经是切好片的
        一页。先切页再排序，只会在这一页内部重排 —— 翻到第 2 页顺序就乱了，
        而且没有报错（"看起来对"）。所以必须"先全量排、再切页"。

        `query` 是文件名的子串筛选（大小写不敏感，`None` / 空白串 = 不筛）。
        它同样必须在数据层做：路由层的 `total` 是 `count_owned()` 算的，
        若筛选只在路由层切页之后做，`total` 会与 `items` 对不上（前端页数就错了）。

        `sort` / `order` 的合法值由路由层用 `Literal` 校验（非法值 422），
        这里对未知值退化为默认（`uploaded_at`/`desc`），不抛异常：
        数据层不该依赖 HTTP 层的校验结果，也不该在排序上再发明一套错误。
        """
        owned = [
            item for item in self._datasets.values() if item.owner_user_id == owner_user_id
        ]
        if query is not None:
            needle = query.strip().lower()
            if needle:
                # 用户看到的是原始文件名（大小写混用），所以两边都转小写再比。
                owned = [item for item in owned if needle in item.original_filename.lower()]
        keys = _SORT_KEYS.get(sort, _SORT_KEYS["uploaded_at"])
        # 排序键里都追加 `dataset_id` 兜底：同一个排序列上取值相同的两条
        # （空库里的同名文件、同样行数的两个文件）若只按该列排，顺序取决于
        # dict 插入顺序 —— "分页跨页错位/同一页里顺序抖动"就是这么来的。
        # dataset_id 是 uuid4，全局唯一，所以它一进键，顺序就完全确定了。
        primary, reverse = keys, order != "asc"
        owned.sort(key=lambda item: (primary(item), item.dataset_id), reverse=reverse)
        return owned

    def count_owned(self, owner_user_id: int, *, query: str | None = None) -> int:
        """某个用户名下的数据集条数（列表接口的 `total` 用它，别用 `len(list_owned(...))`）。

        为什么单独一个方法：`total` 必须是**过滤后的总数**，而不是"这一页有几条"。
        前端靠它算页数；两者混在一起，第 2 页之后页码就会算错。

        `query` 的语义必须与 `list_owned()` **完全一致**，否则 `total` 会大于
        实际能翻到的条数（前端会显示出一堆空页）。为了不重抄一遍匹配规则，
        这里复用 `list_owned()` 的结果 —— 数据集是**进程内**的一小撮元信息，
        这点开销远小于"两处规则写岔"的风险。
        """
        return len(self.list_owned(owner_user_id, query=query))

    def exists(self, dataset_id: str) -> bool:
        """这个 id 登记过没有。路由层用它把"不存在的 id"提前变成 404。"""
        return dataset_id in self._datasets

    def set_profile(self, dataset_id: str, profile: DatasetProfile) -> None:
        """缓存画像，并把 `Dataset.has_profile` 置为 True。

        为什么两处必须一起改：`has_profile` 是给"数据集列表 / 详情"接口看的
        （前端靠它决定要不要显示"生成画像"按钮），`profile` 本体是画像接口的返回体。
        只改一个的话，前端会看到"has_profile=true，但画像接口返回 404"
        这种自相矛盾的状态。

        id 不存在时抛 404：能走到这一步说明调用方跳过了存在性检查，
        与其悄悄丢一个画像，不如当场把问题指出来。
        """
        if dataset_id not in self._datasets:
            raise AppError(ErrorCode.NOT_FOUND, f"数据集不存在：{dataset_id}")

        self._profiles[dataset_id] = profile
        self._datasets[dataset_id].has_profile = True

    def get_profile(self, dataset_id: str) -> DatasetProfile | None:
        """取画像；没算过就返回 None（路由层据此触发一次 profiler）。"""
        return self._profiles.get(dataset_id)


def _preview_rows(df: pd.DataFrame, limit: int = PREVIEW_ROWS) -> list[dict[str, str]]:
    """取前 `limit` 行做预览，**所有值统一转成字符串**。

    为什么全部转 str：缺失值直接进 JSON 会变成不合法的 `NaN`；
    `Timestamp` 会变成一长串带时区的字符串；`12.0` 和 `"12.0"` 在前端看起来一样、
    类型却不同。统一成字符串之后，前端只负责显示，不用为每种类型写一套分支。
    """
    preview: list[dict[str, str]] = []
    for row in df.head(limit).to_dict(orient="records"):
        preview.append({str(column): _cell_to_text(value) for column, value in row.items()})
    return preview


def _cell_to_text(value: Any) -> str:
    """把一个单元格变成字符串；缺失值变成空字符串而不是 "nan" / "NaT"。"""
    # 这里逐个 identity 判断而不是用 pd.isna(value)：单元格的值都是标量，
    # 三种缺失值（None / NaT / NA）覆盖全了，比 pd.isna 更直白。
    if value is None or value is pd.NaT or value is pd.NA:
        return ""
    # float("nan")：pandas 的数值列缺失值走的是这条（np.float64("nan") 也是 float 的子类）。
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value)


# 模块级单例：整个进程共用一份。docs/ARCHITECTURE.md §8 要求"进程内单例"，
# 并把它和 registry 一起记在"只能单进程运行"的边界里。
store = DatasetStore()

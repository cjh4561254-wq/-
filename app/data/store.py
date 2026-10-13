"""数据集登记处：进程内的一张"dataset_id → 数据"表。

这个模块负责什么
----------------
1. 生成 `dataset_id`（`uuid4().hex`，32 位十六进制，不可猜）。
2. 把**用户上传的原始字节**原样落盘到 `data/uploads/{dataset_id}.{ext}`。
3. 记住这份数据的元信息（`Dataset`）和 DataFrame 本身，供后面的接口按 id 取用。
4. 缓存画像（`DatasetProfile`），同时把 `Dataset.has_profile` 置成 True。

这个模块不负责什么
------------------
- 不做任何统计计算 —— 画像在 `app/data/profiler.py`。
- 不解析文件 —— 解析在 `app/data/loader.py`。
- 不知道 HTTP 的存在：没有 request / response，也不知道状态码是从哪来的。
- 不校验扩展名白名单：那是路由层的事（只有它手里有 `UploadFile`）。
  这里只保证一件事：**写出去的文件名一定是安全的**。

为什么数据放在进程内的字典里（而不是数据库 / Redis）
---------------------------------------------------
这是求职作品集项目，单进程 uvicorn 跑起来，数据活到进程结束就够了。
代价必须写清楚：**多进程部署（`uvicorn --workers 2` 以上）会各存一份，
进程重启就全丢**。docs/ARCHITECTURE.md §8 把这个边界记在"只能单进程"那一栏。

关于 `Dataset.sha256` 的含义（重要，改代码前先看这段）
------------------------------------------------------
它指的是**用户上传的那份原始字节**的 sha256，用来回答"这两个文件是不是同一份"。

- 路由层调用时永远带上 `raw_bytes`，磁盘内容就是原始字节，两者严格一致；
- 只有测试和手工调用会走 `raw_bytes=None`（拿 DataFrame 重新序列化），
  那时磁盘副本与 `sha256` / `size_bytes` 对不上 —— 但字段本身的含义不变，
  它描述的仍然是"原始上传内容"。
"""

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from app import config
from app.errors import AppError
from app.schemas import ErrorCode
from app.schemas_dataset import Dataset, DatasetProfile

# 预览给前几行。前端表格只展示一小段，多了既占带宽又没人看。
PREVIEW_ROWS = 5


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

    def create(
        self,
        df: pd.DataFrame,
        original_filename: str,
        ext: str,
        size_bytes: int,
        sha256: str,
        raw_bytes: bytes | None = None,
    ) -> Dataset:
        """登记一份新数据：落盘 + 记元信息 + 记住 DataFrame，返回 `Dataset`。

        `raw_bytes` 有值（**路由层永远这样调**）
            把用户上传的原始字节原样写到 `data/uploads/{dataset_id}.{ext}`。
            此时磁盘上的文件与 `sha256` / `size_bytes` 严格一致。

        `raw_bytes=None`（只有测试和手工调用会这样）
            退化成拿 `df` 重新序列化落盘：csv 用 `utf-8-sig`，
            xlsx 用 `df.to_excel(..., index=False)`。
            **注意：这条路上磁盘里的副本和 `Dataset.sha256` / `size_bytes` 不再一致**，
            因为那两个值描述的是"用户原始上传内容"。线上路径不会出现这种不一致。

        `original_filename` 只作为**说明文字**存进元信息，绝不参与拼路径 ——
        用户上传的名字里可能有 `../../` 之类的东西。

        返回的 `Dataset.has_profile` 一定是 False：画像要等 profiler 算完，
        再通过 `set_profile` 补上（这样前端能分辨"还没算"和"算完了是空的"）。
        """
        dataset_id = uuid4().hex
        stored_filename = f"{dataset_id}.{ext}"
        target = self._resolve_target(stored_filename)

        if raw_bytes is not None:
            target.write_bytes(raw_bytes)
        else:
            _write_dataframe(df, target, ext)

        dataset = Dataset(
            dataset_id=dataset_id,
            original_filename=original_filename,
            row_count=int(df.shape[0]),
            column_count=int(df.shape[1]),
            created_at=datetime.now(timezone.utc),
            has_profile=False,
            stored_filename=stored_filename,
            file_ext=ext,
            size_bytes=int(size_bytes),
            sha256=sha256,
            columns=[str(name) for name in df.columns],
            dtypes={str(name): str(dtype) for name, dtype in df.dtypes.items()},
            preview_rows=_preview_rows(df),
        )

        # 落盘成功之后才写字典。反过来写的话，磁盘满 / 没权限时
        # 字典里会留下一条"有元信息、却没有文件"的假记录，
        # 后面按 id 取数据时才发现文件不存在，很难查。
        self._frames[dataset_id] = df
        self._datasets[dataset_id] = dataset
        return dataset

    def get(self, dataset_id: str) -> Dataset | None:
        """取元信息；不存在返回 None（由调用方决定是 404 还是别的处理）。"""
        return self._datasets.get(dataset_id)

    def get_frame(self, dataset_id: str) -> pd.DataFrame | None:
        """取 DataFrame；不存在返回 None。

        注意这里返回的是**同一个对象**，不是副本：DataFrame 可能有 20 万行，
        每次取都复制一份等于把内存翻倍。所以调用方只读，不要就地修改它。
        """
        return self._frames.get(dataset_id)

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

    def _resolve_target(self, stored_filename: str) -> Path:
        """拼出要写的绝对路径，并复核它确实落在 upload_path 里面。

        `stored_filename` 里只有 uuid 和扩展名，本来就不可能穿越到别的目录；
        但"本来就不可能"是靠人记住的，写成一条机器检查才能防止以后有人
        顺手把用户文件名拼进来。`resolve()` 会把 `..` 和符号链接都解析掉，
        所以这条判断看的是最终的真实位置。
        """
        upload_dir = config.get_settings().upload_path
        upload_dir.mkdir(parents=True, exist_ok=True)

        target = (upload_dir / stored_filename).resolve()
        if target.parent != upload_dir.resolve():
            raise AppError(
                ErrorCode.BAD_REQUEST,
                "文件名不合法",
                detail=f"stored_filename={stored_filename!r} 不在上传目录内",
            )
        return target


def _write_dataframe(df: pd.DataFrame, target: Path, ext: str) -> None:
    """`raw_bytes` 没传时的兜底：把 DataFrame 重新序列化写盘。

    只在测试和手工调用时会走到这里（**线上路由层永远传原始字节**）：
    重新序列化既慢又吃内存，还会丢掉原始文件里的格式信息
    （比如 Excel 的单元格格式、日期显示样式）。
    这就是为什么它只能是兜底，不能是主路径。

    csv 写 `utf-8-sig`：带 BOM 的 UTF-8 是 Excel 双击打开中文 CSV 不出乱码的写法，
    符合"用户拿到文件还想用 Excel 看一眼"的直觉。
    """
    if ext == "csv":
        df.to_csv(target, index=False, encoding="utf-8-sig")
    elif ext == "xlsx":
        df.to_excel(target, index=False, engine="openpyxl")
    else:
        raise AppError(
            ErrorCode.UNSUPPORTED_FILE,
            f"不支持的文件类型：{ext}",
            detail=f"只支持 csv / xlsx，收到 ext={ext!r}",
        )


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

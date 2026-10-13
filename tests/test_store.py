"""app/data/store.py 的测试。

重点测三件事：

1. **落盘文件名只由 dataset_id 和扩展名决定**（`build_stored_filename`），
   用户上传的文件名绝不参与拼路径；
2. `register()` 记下的元信息和调用方传进去的值一致，并且**完全不碰磁盘**；
3. 画像缓存与 `Dataset.has_profile` 永远同步。

接口调整后的一个关键变化：store 不再负责落盘（原始字节由路由层写）。
所以下面用 `upload_dir` fixture 反过来证明"store 没有往上传目录里写任何东西"——
连目录本身都不该被创建。
"""

from datetime import datetime, timezone

import pandas as pd
import pytest

from app import config
from app.data.store import DatasetStore, build_stored_filename
from app.errors import AppError
from app.schemas import ErrorCode
from app.schemas_dataset import DatasetProfile

# 代表路由层生成的 uuid4().hex：32 位十六进制。
DATASET_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """每个测试前后清一次配置缓存（理由同 test_loader.py）。"""
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    """把上传目录指到临时目录，返回它的路径。

    这里的目的是"如果 store 还敢写盘，就让它写到临时目录里"，
    然后用断言把它抓住 —— 绝不往项目真实的 data/uploads/ 里写东西。
    """
    target = tmp_path / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(target))
    config.reset_settings_cache()
    return target


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame({"名称": ["A", "B"], "数量": [1, 2]})


def _register(
    basket: DatasetStore,
    df: pd.DataFrame,
    dataset_id: str = DATASET_ID,
    original_filename: str = "销售.csv",
    file_ext: str = "csv",
    size_bytes: int = 12,
    sha256: str = "sha-abc",
):
    """按 register 的签名登记一份数据，省得每个测试都写六个参数。"""
    return basket.register(dataset_id, df, original_filename, file_ext, size_bytes, sha256)


def _empty_profile(dataset_id: str) -> DatasetProfile:
    return DatasetProfile(
        dataset_id=dataset_id,
        row_count=2,
        column_count=2,
        columns=[],
        duplicate_row_count=0,
        memory_bytes=128,
        warnings=[],
        built_at=datetime.now(timezone.utc),
    )


def test_build_stored_filename_uses_only_id_and_ext():
    assert build_stored_filename(DATASET_ID, "csv") == f"{DATASET_ID}.csv"
    assert build_stored_filename(DATASET_ID, "xlsx") == f"{DATASET_ID}.xlsx"


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../../evil",  # POSIX 风格的穿越
        "..\\..\\evil",  # Windows 风格的穿越
        "C:\\Windows\\Temp\\x",  # 绝对路径
        "",  # 空串（拼出来就是 ".csv"）
        "0123456789abcdef0123456789abcde",  # 31 位：短一位
        "0123456789abcdef0123456789abcdef0",  # 33 位：长一位
        "0123456789abcdef0123456789abcdeZ",  # 非十六进制字符
        "0123456789ABCDEF0123456789ABCDEF",  # 大写：uuid4().hex 只会是小写
        "0123456789abcdef0123456789abc/ef",  # 分隔符藏在中间
    ],
)
def test_build_stored_filename_rejects_a_bad_dataset_id(bad_id):
    """拼路径的地方必须真的检查输入，不能只靠"调用方会传对"。

    QA 质疑过这条：`build_stored_filename` 原来只有一句 f-string，docstring 用
    "dataset_id 是我们自己生成的 32 位十六进制，天然安全"来解释它为什么不用校验。
    上面这些输入实测能拼出 `../../../evil.csv`、`C:\\Windows\\Temp\\x.csv`、
    `.csv` —— 也就是说那句"天然安全"描述的是调用方的意图，不是这个函数的事实。
    """
    with pytest.raises(AppError) as excinfo:
        build_stored_filename(bad_id, "csv")

    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
    # detail 里不能回显原始输入：它可能带换行，塞进日志会伪造出一行假日志。
    # 空串要单独排除，否则 `"" in 任何字符串` 恒为 True，这条断言等于没写。
    if bad_id:
        assert bad_id not in (excinfo.value.detail or "")
        assert bad_id not in excinfo.value.message
    assert "\n" not in (excinfo.value.detail or "")


@pytest.mark.parametrize("bad_ext", ["exe", "xls", "", "csv ", "CSV"])
def test_build_stored_filename_rejects_a_bad_extension(bad_ext):
    """扩展名同样来自请求，同样不能默认它是干净的。

    注意 `"CSV"` 与 `"csv "` 也必须在拒绝之列：白名单是精确匹配，
    大小写和不该有的空格都属于"没被规范化过的输入"，宁可拒掉也不要猜。
    """
    with pytest.raises(AppError) as excinfo:
        build_stored_filename(DATASET_ID, bad_ext)

    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR


def test_register_rejects_an_extension_outside_the_whitelist(upload_dir, frame):
    """白名单外的扩展名在 register 这一层就被拦下，且不留半条记录。

    实际抛的是我们自己那个 `AppError`（不是 pydantic 的 ValidationError）：
    `register()` 会用 `build_stored_filename()` 拼 `stored_filename`，而那个函数
    现在自己就校验扩展名，所以它先炸。这是**更好**的结果 —— 报错文案是中文的、
    带错误码的，能直接被统一外壳翻译成 500 响应，而不是一句英文字段校验信息。
    下面按实际行为断言，并把"顺序"这件事记在这里，以免以后有人以为它是 pydantic 在管。
    """
    basket = DatasetStore()

    with pytest.raises(AppError) as excinfo:
        _register(basket, frame, file_ext="exe")

    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
    assert basket.exists(DATASET_ID) is False  # 没有留下半条记录


def test_register_records_the_metadata_it_was_given(upload_dir, frame):
    basket = DatasetStore()

    dataset = _register(basket, frame)

    assert dataset.dataset_id == DATASET_ID
    assert dataset.stored_filename == f"{DATASET_ID}.csv"  # 和 build_stored_filename 同一个函数拼的
    assert dataset.original_filename == "销售.csv"
    assert dataset.file_ext == "csv"
    assert dataset.size_bytes == 12
    assert dataset.sha256 == "sha-abc"
    assert dataset.has_profile is False
    assert dataset.row_count == 2
    assert dataset.column_count == 2
    assert dataset.columns == ["名称", "数量"]
    assert dataset.dtypes == {"名称": "str", "数量": "int64"}


def test_register_does_not_touch_the_upload_dir(upload_dir, frame):
    """store 已经不负责落盘了：它连上传目录都不该创建。"""
    basket = DatasetStore()

    _register(basket, frame)

    assert upload_dir.exists() is False


def test_original_filename_never_becomes_a_path(upload_dir, frame):
    basket = DatasetStore()

    dataset = _register(basket, frame, original_filename="../../evil.csv")

    assert dataset.stored_filename == f"{DATASET_ID}.csv"  # 文件名里没有 evil 的影子
    assert "evil" not in dataset.stored_filename
    assert dataset.original_filename == "../../evil.csv"  # 只当说明文字存着，不参与拼路径
    assert upload_dir.exists() is False


def test_register_with_duplicate_id_raises_and_keeps_the_first_one(upload_dir, frame):
    basket = DatasetStore()
    first = _register(basket, frame)
    other = pd.DataFrame({"别的": [9]})

    with pytest.raises(AppError) as excinfo:
        _register(basket, other, sha256="sha-other")

    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
    assert basket.get(DATASET_ID) is first           # 旧的元信息没被覆盖
    assert basket.get_frame(DATASET_ID) is frame     # 旧的数据也没被覆盖


def test_get_get_frame_exists(upload_dir, frame):
    basket = DatasetStore()
    dataset = _register(basket, frame)

    assert basket.exists(DATASET_ID) is True
    assert basket.get(DATASET_ID) is dataset
    assert basket.get_frame(DATASET_ID) is frame  # 返回同一个对象，不复制

    assert basket.exists("not-a-real-id") is False
    assert basket.get("not-a-real-id") is None
    assert basket.get_frame("not-a-real-id") is None


def test_set_profile_also_marks_has_profile(upload_dir, frame):
    basket = DatasetStore()
    _register(basket, frame)
    profile = _empty_profile(DATASET_ID)

    basket.set_profile(DATASET_ID, profile)

    assert basket.get_profile(DATASET_ID) is profile
    assert basket.get(DATASET_ID).has_profile is True


def test_get_profile_returns_none_before_it_is_built(upload_dir, frame):
    basket = DatasetStore()
    _register(basket, frame)

    assert basket.get_profile(DATASET_ID) is None
    assert basket.get(DATASET_ID).has_profile is False


def test_set_profile_for_unknown_id_raises_not_found(upload_dir):
    basket = DatasetStore()

    with pytest.raises(AppError) as excinfo:
        basket.set_profile("not-a-real-id", _empty_profile("not-a-real-id"))

    assert excinfo.value.code is ErrorCode.NOT_FOUND


def test_preview_rows_are_all_strings_and_missing_becomes_empty(upload_dir):
    frame = pd.DataFrame(
        {
            "名称": ["A", None],
            "数量": [1.5, None],
            "日期": [pd.Timestamp("2024-01-05"), pd.NaT],
        }
    )
    basket = DatasetStore()

    dataset = _register(basket, frame, original_filename="preview.csv")

    assert dataset.preview_rows == [
        {"名称": "A", "数量": "1.5", "日期": "2024-01-05 00:00:00"},
        {"名称": "", "数量": "", "日期": ""},
    ]


def test_preview_shows_at_most_five_rows(upload_dir):
    frame = pd.DataFrame({"序号": list(range(1, 9))})
    basket = DatasetStore()

    dataset = _register(basket, frame, original_filename="many.csv")

    assert len(dataset.preview_rows) == 5
    assert dataset.row_count == 8  # 元信息里的行数仍然是全部行数

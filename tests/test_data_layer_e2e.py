"""数据层端到端：按路由层的顺序把 loader → store → profiler 串起来跑一遍。

`test_loader.py` / `test_store.py` / `test_profiler.py` 各自只盯一个模块，
这里补的是它们都覆盖不到的三件事：

1. **顺序**：规范化扩展名 → 生成 dataset_id → 拼落盘路径并复核 → 写盘 → 解析 →
   登记 → 算画像。这条链路要真的能跑通，而且**元信息里的文件名 == 磁盘上的文件名**；
2. **对数**：画像里的每个数字都和 pandas 自己算的（`df.describe()` / `nunique()` /
   `isna().sum()` / `duplicated().sum()` / `memory_usage()`）逐个对照。
   这是"画像里的数字是真算出来的、不是编的"的唯一证据；
3. **JSON 终点**：画像最后必须能被 `json.dumps(..., allow_nan=False)` 序列化，
   否则接口会 500（JSON 里没有 NaN 这种值）。

这个文件里的"路由层"是**手写的模拟**：真正的路由在 app/api/（task-3）。
不直接调路由，是因为路由还要起 HTTP 服务、构造 `UploadFile`，属于接口测试的事；
数据层自己的责任边界在这里就能验完。

临时文件一律用 pytest 的 `tmp_path`：这台机器写不进系统临时目录，
`tempfile.gettempdir()` 会静默回退到当前工作目录，`tempfile.mkdtemp()` 会在仓库根
留下一串 `tmpXXXXXXXX/`（见 tests/conftest.py 的说明）。
"""

import hashlib
import json
from uuid import uuid4

import pytest

from app.data.loader import load_dataframe
from app.data.profiler import build_profile
from app.data.store import DatasetStore, build_stored_filename

# 一份"真实世界会长这样"的销售表，故意把这几个坑都放进去：
#   1. 两列重名（销售额）—— pandas 默认会改成 销售额.1，我们约定改成 销售额_2；
#   2. 金额带千分位 "1,200" —— **必须写引号**，否则 CSV 会多出一个字段、整表错位；
#   3. 有缺失值（空单元格）；
#   4. 末尾有一行全空 —— 从 Excel 复制出来常常带这种行；
#   5. 有一行和前面完全重复。
SALES_CSV = (
    "订单号,日期,地区,销售额,备注,销售额\n"
    '1001,2024-01-05,华东,"1,200",客户要求周末送达，麻烦提前联系配送员,1200\n'
    '1002,2024-02-06,华北,"3,400",第二次回购需要开增值税专用发票,3400\n'
    "1003,2024-03-07,华东,,临时改地址,\n"
    "1004,2024-04-08,,5600,,5600\n"
    '1002,2024-02-06,华北,"3,400",第二次回购需要开增值税专用发票,3400\n'
    ",,,,,\n"
)


@pytest.fixture
def chain(tmp_path) -> dict:
    """走一遍完整上传链路，把每一步的产物都带出来给各个测试用。

    上传目录用 `tmp_path` 而不是项目里的 `data/uploads/`：
    测试绝不能往真实上传目录里写东西，那里是用户数据的地盘。
    """
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()

    raw = SALES_CSV.encode("utf-8")
    source = tmp_path / "销售.csv"
    source.write_bytes(raw)

    # ---- 以下七步照 task-3 定下的路由层顺序走 ----
    # ① 规范化扩展名（真正"拒掉非白名单类型"发生在路由层，这里只是取值）
    file_ext = source.suffix.lstrip(".").lower()
    # ② 生成 dataset_id
    dataset_id = uuid4().hex
    # ③ 拼落盘路径，并复核它确实落在上传目录里（§10 的兜底检查）
    stored_filename = build_stored_filename(dataset_id, file_ext)
    target = (upload_dir / stored_filename).resolve()
    # ④⑤ 读字节 + 写盘（真实路由是分块读、累计大小；这里数据是自己造的，一次写）
    target.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    # ⑥ 解析
    df, encoding = load_dataframe(target, file_ext)
    # ⑦ 登记（store 只登记，不碰磁盘）
    basket = DatasetStore()
    dataset = basket.register(dataset_id, df, source.name, file_ext, len(raw), digest)

    profile = build_profile(df, dataset.dataset_id)
    basket.set_profile(dataset.dataset_id, profile)

    return {
        "df": df,
        "encoding": encoding,
        "dataset": dataset,
        "profile": profile,
        "basket": basket,
        "target": target,
        "upload_dir": upload_dir,
        "raw": raw,
        "digest": digest,
        "dataset_id": dataset_id,
    }


def test_chain_writes_raw_bytes_and_registers_matching_metadata(chain):
    dataset = chain["dataset"]

    assert chain["target"].read_bytes() == chain["raw"]  # 落盘的就是上传的那串字节
    assert dataset.stored_filename == chain["target"].name  # 元信息里的名字 == 磁盘上的名字
    assert dataset.stored_filename == f"{chain['dataset_id']}.csv"  # 只由 id + 扩展名拼成
    assert dataset.sha256 == chain["digest"] == hashlib.sha256(chain["raw"]).hexdigest()
    assert dataset.size_bytes == len(chain["raw"])
    assert dataset.original_filename == "销售.csv"  # 原始文件名只当说明文字存着


def test_chain_leaves_only_the_stored_file_in_the_upload_dir(chain):
    assert [item.name for item in chain["upload_dir"].iterdir()] == [chain["dataset"].stored_filename]


def test_chain_cleans_the_table_the_way_we_promised(chain):
    df = chain["df"]

    assert df.shape == (5, 6)  # 末尾那行全空被删掉
    assert list(df.index) == [0, 1, 2, 3, 4]  # 行号抹平
    assert list(df.columns) == ["订单号", "日期", "地区", "销售额", "备注", "销售额_2"]
    assert chain["encoding"] == "utf-8-sig"  # 候选编码里第一个能读通的
    assert df.loc[0, "销售额"] == "1,200"  # 单元格原值没有被改写


def test_chain_profile_semantic_types_on_a_real_table(chain):
    semantics = {column.name: column.semantic_type for column in chain["profile"].columns}

    assert semantics["订单号"] == "numeric"  # 纯数字列不会被误判成日期
    assert semantics["日期"] == "datetime"
    assert semantics["地区"] == "categorical"
    assert semantics["销售额_2"] == "numeric"


def test_chain_profile_can_be_serialized_to_json(chain):
    profile = chain["profile"]

    payload = json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)

    assert "2024-01-05T00:00:00" in payload  # 日期变成了 ISO 字符串，不是 Timestamp 对象
    assert chain["basket"].get_profile(profile.dataset_id) is profile
    assert chain["basket"].get(profile.dataset_id).has_profile is True


def test_chain_profile_numbers_match_pandas(chain):
    """画像里的数字和 pandas 自己算的逐个对照。

    浮点数用 `pytest.approx` 比：求和顺序不同，最后一位有可能不一样，
    直接 `==` 会变成一个随机失败的测试。
    """
    df = chain["df"]
    by_name = {column.name: column for column in chain["profile"].columns}

    orders = df["订单号"].describe()
    assert by_name["订单号"].mean_value == pytest.approx(float(orders["mean"]))
    assert by_name["订单号"].std_value == pytest.approx(float(orders["std"]))  # ddof=1
    assert by_name["订单号"].min_value == float(orders["min"])
    assert by_name["订单号"].max_value == float(orders["max"])
    assert by_name["订单号"].median_value == pytest.approx(float(orders["50%"]))
    assert by_name["订单号"].q1_value == pytest.approx(float(orders["25%"]))
    assert by_name["订单号"].q3_value == pytest.approx(float(orders["75%"]))

    amounts = df["销售额_2"].describe()  # 这一列有缺失值，统计时要跳过它们
    assert by_name["销售额_2"].mean_value == pytest.approx(float(amounts["mean"]))
    assert by_name["销售额_2"].std_value == pytest.approx(float(amounts["std"]))

    assert by_name["地区"].unique_count == int(df["地区"].nunique())
    assert by_name["地区"].null_count == int(df["地区"].isna().sum())
    assert chain["profile"].duplicate_row_count == int(df.duplicated().sum())
    assert chain["profile"].memory_bytes == int(df.memory_usage(deep=True).sum())

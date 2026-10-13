"""数据集接口的测试：真实 HTTP（TestClient）+ 真实文件（tmp_path），不 mock。

覆盖四类：
1. 成功路径：上传 CSV / GBK CSV / xlsx → 元信息、预览、落盘文件名与字节；
2. 状态码：415 扩展名、413 太大、400 解析失败 / 空 / 只有表头、404 不存在；
3. 安全：原始文件名带 `../` 或盘符也不参与路径、被拒时不落盘；
4. 画像：上传时不算 → 首次 GET 真算（数字和手算对账）→ 第二次拿缓存。

两个刻意的写法：
- **必须 `with TestClient(app) as client`**：上传目录是 lifespan 启动时建的，
  不用上下文管理器就只拿到一个没跑过启动逻辑的 app（第一阶段的假绿测试栽在这）。
- 每个用例把 `UPLOAD_DIR` 指到 `tmp_path`，**绝不碰项目真实的 data/uploads/**。
  这也是不写 `tempfile.mkdtemp()` 的原因：本机系统临时目录不可写，tempfile 会
  悄悄退回"当前工作目录"，在仓库根留下 tmpXXXX/ 垃圾（见 conftest.py 的说明）。
"""

import hashlib

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

CSV_TEXT = "日期,地区,销售额\n2024-01-05,华东,100\n2024-01-06,华北,200\n2024-01-07,华东,300\n"
CSV_BYTES = CSV_TEXT.encode("utf-8")

# 第三行销售额留空：null_count / null_ratio / 均值 都能手算对账。
CSV_WITH_A_HOLE = "日期,地区,销售额\n2024-01-05,华东,100\n2024-01-06,华北,200\n2024-01-07,华东,\n"

# 6 行数据：证明"预览只给前 5 行，但 row_count 是全部 6 行"。
CSV_SIX_ROWS = "名称,数量\n" + "".join(f"行{i},{i}\n" for i in range(1, 7))

UPLOAD_URL = "/api/datasets/upload"
UNKNOWN_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """配置是 lru_cache 的：改了环境变量必须清缓存，否则读到的还是上一份。"""
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    """把上传目录指到 tmp_path：测试永不写项目里的 data/uploads/。"""
    target = tmp_path / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(target))
    config.reset_settings_cache()
    return target


@pytest.fixture
def client(upload_dir):
    with TestClient(app) as test_client:
        yield test_client


def _upload(client: TestClient, content: bytes, filename: str = "sales.csv"):
    return client.post(UPLOAD_URL, files={"file": (filename, content)})


def _stored_names(upload_dir) -> list[str]:
    """上传目录里的文件名（目录还没建就返回空列表）。"""
    if not upload_dir.exists():
        return []
    return sorted(path.name for path in upload_dir.iterdir())


# ---------------------------------------------------------------- 成功路径


def test_upload_csv_returns_metadata_and_preview(client, upload_dir):
    response = _upload(client, CSV_BYTES)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["error"] is None
    data = body["data"]
    assert data["original_filename"] == "sales.csv"  # 用户文件名只用来显示
    assert data["file_ext"] == "csv"
    assert data["row_count"] == 3 and data["column_count"] == 3
    assert data["columns"] == ["日期", "地区", "销售额"]
    assert data["preview_rows"] == [
        {"日期": "2024-01-05", "地区": "华东", "销售额": "100"},
        {"日期": "2024-01-06", "地区": "华北", "销售额": "200"},
        {"日期": "2024-01-07", "地区": "华东", "销售额": "300"},
    ]
    assert data["size_bytes"] == len(CSV_BYTES)
    assert data["sha256"] == hashlib.sha256(CSV_BYTES).hexdigest()
    assert data["has_profile"] is False  # 上传时不算画像
    # 落盘名是"服务端生成的 id + 扩展名"，和用户传的文件名无关
    assert data["stored_filename"] == f"{data['dataset_id']}.csv"


def test_server_generates_a_32_hex_id_and_writes_the_raw_bytes(client, upload_dir):
    data = _upload(client, CSV_BYTES).json()["data"]

    dataset_id = data["dataset_id"]
    assert len(dataset_id) == 32
    assert all(char in "0123456789abcdef" for char in dataset_id)

    assert _stored_names(upload_dir) == [f"{dataset_id}.csv"]
    stored = upload_dir / f"{dataset_id}.csv"
    # 落盘的必须是**用户原始上传的字节**：连 BOM、连空行都原样保留，
    # 这样 sha256 才真的是"这个文件的指纹"。
    assert stored.read_bytes() == CSV_BYTES


def test_upload_gbk_csv_keeps_chinese_readable(client, capsys):
    content = "名称,数量\n张三,1\n李四,2\n".encode("gbk")
    response = _upload(client, content, filename="gbk.csv")

    assert response.status_code == 200
    assert response.json()["data"]["preview_rows"] == [
        {"名称": "张三", "数量": "1"},
        {"名称": "李四", "数量": "2"},
    ]
    # 编码是 loader 自己猜的，猜中哪一条只能从日志看出来：这里断言它真的走了 gbk。
    assert "gbk" in capsys.readouterr().out


def test_upload_xlsx_reads_the_first_sheet(client, tmp_path):
    book = tmp_path / "台账.xlsx"
    pd.DataFrame({"名称": ["甲", "乙"], "数量": [1, 2]}).to_excel(
        book, index=False, engine="openpyxl"
    )

    response = _upload(client, book.read_bytes(), filename="台账.xlsx")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["file_ext"] == "xlsx"
    assert (data["row_count"], data["column_count"]) == (2, 2)
    assert data["stored_filename"].endswith(".xlsx")
    assert data["preview_rows"][0] == {"名称": "甲", "数量": "1"}


def test_preview_shows_five_rows_but_row_count_is_everything(client):
    data = _upload(client, CSV_SIX_ROWS.encode("utf-8"), filename="six.csv").json()["data"]

    assert data["row_count"] == 6
    assert len(data["preview_rows"]) == 5
    assert data["preview_rows"][0] == {"名称": "行1", "数量": "1"}


# ---------------------------------------------------------------- 415 / 413


@pytest.mark.parametrize(
    "filename",
    ["notes.txt", "台账.xls", "noextension", "archive.tar.gz", "book.CSV.bak"],
)
def test_upload_rejects_extensions_outside_the_whitelist(client, upload_dir, filename):
    response = _upload(client, CSV_BYTES, filename=filename)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE"
    # 415 发生在写盘之前：上传目录里一个文件都不该有（哪怕内容其实是合法 CSV）
    assert _stored_names(upload_dir) == []


def test_legacy_xls_gets_a_human_hint(client):
    response = _upload(client, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", filename="旧表.xls")

    assert response.status_code == 415
    assert "另存为 .xlsx" in response.json()["error"]["detail"]


def test_upload_over_the_size_limit_is_413_and_writes_nothing(client, upload_dir, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    config.reset_settings_cache()
    too_big = "名称\n".encode("utf-8") + b"A" * (1024 * 1024)

    response = _upload(client, too_big, filename="big.csv")

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "FILE_TOO_LARGE"
    assert "没有写入磁盘" in error["detail"]
    assert _stored_names(upload_dir) == []


def test_upload_exactly_at_the_size_limit_is_accepted(client, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    config.reset_settings_cache()
    limit = 1024 * 1024
    header = "名称\n".encode("utf-8")
    content = header + b"A" * (limit - len(header) - 1) + b"\n"
    assert len(content) == limit  # 恰好等于上限，不该被拒

    response = _upload(client, content, filename="exact.csv")

    assert response.status_code == 200
    assert response.json()["data"]["size_bytes"] == limit


# ---------------------------------------------------------------- 400


def test_upload_empty_file_is_400_and_cleaned_up(client, upload_dir):
    response = _upload(client, b"", filename="empty.csv")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "BAD_REQUEST"
    assert error["message"] == "文件是空的，里面没有任何内容"
    # 这行是关键：文件已经落盘了才去解析，解析失败必须把落盘的那份删掉。
    assert _stored_names(upload_dir) == []


def test_upload_header_only_file_is_400_and_cleaned_up(client, upload_dir):
    response = _upload(client, "名称,数量\n".encode("utf-8"), filename="header.csv")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["message"] == "文件里只有表头，没有数据行"
    # detail 必须是"哪个文件"，不能是兜底的请求路径。
    # 注意这里说的是**落盘名**（{dataset_id}.csv），不是用户上传时的 header.csv ——
    # loader 只拿到磁盘路径，原始文件名在它这一层根本不存在。
    # 这个落盘名对排错够用：拿着它能在 data/uploads/ 里找到那个文件。
    # 如果 AppError 没给 detail，app/main.py 会填成 request.url.path，
    # 用户看到的就是 "/api/datasets/upload" —— 一句没有信息量的话。
    assert error["detail"].startswith("file=")
    assert error["detail"].endswith(".csv")
    assert error["detail"] != "/api/datasets/upload"
    assert _stored_names(upload_dir) == []


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("fake.csv", b"\x00\x01\x02\x03"),  # 解不出任何数据行
        ("fake.xlsx", b"plain text, not a zip"),  # 扩展名骗人：其实不是 Excel
    ],
)
def test_disguised_files_are_400_and_cleaned_up(client, upload_dir, filename, content):
    response = _upload(client, content, filename=filename)

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "BAD_REQUEST"
    assert body["ok"] is False and body["data"] is None
    assert "Traceback" not in response.text  # 走的是统一外壳，不是裸异常
    assert _stored_names(upload_dir) == []


# ---------------------------------------------------------------- 路径安全


@pytest.mark.parametrize(
    "filename",
    ["../../evil.csv", "..\\..\\evil.csv", "C:\\Windows\\Temp\\evil.csv", "/etc/passwd.csv"],
)
def test_original_filename_never_becomes_a_path(client, upload_dir, filename):
    response = _upload(client, CSV_BYTES, filename=filename)

    assert response.status_code == 200
    data = response.json()["data"]
    # 回显给用户看的文件名取决于 multipart 客户端怎么传：实测 httpx 对
    # `C:\Windows\Temp\evil.csv` 只传 basename，对 `../../evil.csv` 则原样传。
    # 所以这里接受两种：原样 或 只剩最后的文件名（斜杠和反斜杠都算分隔符）。
    basenames = {filename, filename.rsplit("/", 1)[-1], filename.rsplit("\\", 1)[-1]}
    assert data["original_filename"] in basenames
    assert data["stored_filename"] == f"{data['dataset_id']}.csv"
    assert _stored_names(upload_dir) == [data["stored_filename"]]
    # 上传目录外面不许出现任何东西
    assert not (upload_dir.parent / "evil.csv").exists()
    assert not (upload_dir / "..").resolve().joinpath("evil.csv").exists()


# ---------------------------------------------------------------- 404


def test_get_dataset_round_trip(client):
    uploaded = _upload(client, CSV_BYTES).json()["data"]

    response = client.get(f"/api/datasets/{uploaded['dataset_id']}")

    assert response.status_code == 200
    detail = response.json()["data"]
    assert detail["dataset_id"] == uploaded["dataset_id"]
    assert detail["row_count"] == 3
    assert detail["columns"] == ["日期", "地区", "销售额"]
    assert detail["has_profile"] is False
    # 再取两次，拿到的还是同一份（内存里存的元信息，不是每请求重算）
    assert client.get(f"/api/datasets/{uploaded['dataset_id']}").json()["data"] == detail


@pytest.mark.parametrize("dataset_id", [UNKNOWN_ID, "not-a-real-id"])
def test_get_unknown_dataset_is_404_and_does_not_echo_the_input(client, dataset_id):
    response = client.get(f"/api/datasets/{dataset_id}")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "数据集不存在"
    # URL 里的东西原样回显进响应体是没必要的风险，这里断言它没被回显
    assert dataset_id not in error["message"]
    assert dataset_id not in error["detail"]


def test_profile_of_unknown_dataset_is_404(client):
    response = client.get(f"/api/datasets/{UNKNOWN_ID}/profile")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 画像


def test_profile_is_built_lazily_then_reused_from_cache(client):
    uploaded = _upload(client, CSV_WITH_A_HOLE.encode("utf-8")).json()["data"]
    dataset_id = uploaded["dataset_id"]
    assert uploaded["has_profile"] is False  # 上传时不算画像

    first = client.get(f"/api/datasets/{dataset_id}/profile")

    assert first.status_code == 200
    profile = first.json()["data"]
    assert profile["dataset_id"] == dataset_id
    assert profile["row_count"] == 3 and profile["column_count"] == 3
    assert profile["duplicate_row_count"] == 0
    # 和手算对账：销售额 = 100 / 200 / 空 → 缺 1 个，均值 (100+200)/2 = 150
    amount = next(column for column in profile["columns"] if column["name"] == "销售额")
    assert amount["semantic_type"] == "numeric"
    assert amount["null_count"] == 1
    assert amount["null_ratio"] == 0.3333
    assert amount["unique_count"] == 2
    assert amount["mean_value"] == 150.0
    assert amount["min_value"] == 100.0 and amount["max_value"] == 200.0
    # 缺 33% 的列要出人话警告
    assert any("销售额" in warning for warning in profile["warnings"])

    # 算完之后 has_profile 必须跟着变 True（store 里两处一起改）
    assert client.get(f"/api/datasets/{dataset_id}").json()["data"]["has_profile"] is True

    # 第二次请求命中的是缓存：built_at 一模一样（重算的话时间戳一定会变）
    second = client.get(f"/api/datasets/{dataset_id}/profile")
    assert second.json()["data"]["built_at"] == profile["built_at"]

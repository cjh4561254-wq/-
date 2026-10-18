"""数据集列表接口（`GET /api/datasets`）与归属隔离的测试。

覆盖四类：

1. **列表形态**：统一外壳 + `items` / `total` / `limit` / `offset`，每条 item 的
   字段与 `GET /api/datasets/{id}` 一致，另加 `rows` / `missing_cells`；
2. **分页与排序**：`limit` / `offset` 真的切片；`sort` 三种 × `order` 两种都真的排；
   非法 `sort` / `order` 是 422（不是静默忽略）；
3. **归属隔离**：别人的数据集在列表里**根本不出现**，直接按 id 取详情/画像是 404，
   而且"不存在"和"不是你的"响应**逐字相同**（否则可以用它枚举 id）；
4. **边界**：未登录 401（归属判定之前先过登录）、空列表是 `[]` 而不是报错。

两条刻意的写法：

- **两个用户各自一个账号库**（`tmp_path/auth.db`）：`conftest.py` 会把
  `AUTH_DB_PATH` 指到 `.tmp/auth-test/auth.db`，而 `AuthStore` 是按库路径缓存的
  单例、`DatasetStore` 是进程内单例。测试要造"第二个用户"，就必须让它的账号库和
  默认那个分开，否则 `bootstrap` 会因为"库非空"而 403。
- **每个用例先把数据集仓库清空**（`_empty_dataset_store`）：`DatasetStore` 是
  进程内单例，跨用例累计。不清的话，"一共 3 条"这种断言会随用例执行顺序变化 ——
  单独跑绿、整套跑红，是最难查的一类失败。
"""

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config
from app.data.store import store
from app.main import app
from tests import auth_support

LIST_URL = "/api/datasets"
UPLOAD_URL = "/api/datasets/upload"

# 三行、两列，第三行销售额留空 -> 画像的 missing_cells 手算就是 1。
CSV_HOLE = "日期,销售额\n2024-01-05,100\n2024-01-06,200\n2024-01-07,\n"
# 六行：用来区分"预览 5 行"与"真实 6 行"。
CSV_SIX_ROWS = "名称,数量\n" + "".join(f"行{i},{i}\n" for i in range(1, 7))
CSV_BIG = "值\n" + "".join(f"{i}\n" for i in range(40))


# --------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """配置是 lru_cache 的：改了环境变量必须清缓存，否则读到的还是上一份。

    这里**同时**做一件事：用例结束后把 `config.get_settings` 还原成原函数。
    本文件有几个用例用 `monkeypatch.setattr(config, "get_settings", 替身)` 来把
    账号库/Cookie 指向临时库；`monkeypatch` 只在用例结束时撤销，而路由层是按
    **库路径**缓存 store 的 —— 不还原、也不清缓存的话，下一个用例会对着上一个
    用例的临时库跑，报出"账号库已经有账号了（403）"这种完全联想不到的错。
    这类"跨用例串状态"是整个仓库最容易复现不出来的失败来源，所以宁可多一道兜底。
    """
    original_get_settings = config.get_settings
    config.reset_settings_cache()
    yield
    config.get_settings = original_get_settings
    config.reset_settings_cache()


@pytest.fixture(autouse=True)
def _empty_dataset_store():
    """保证每条用例开始时数据集仓库是**空的**，结束时恢复原样。

    为什么快照不够：`DatasetStore` 是进程内单例，而别的测试文件
    （`tests/test_analysis_api.py`、`tests/test_upload.py` …）也会往它里面登记数据集，
    那些登记**不会**被本文件的夹具清掉。实测（`pytest tests/test_workflows_api.py
    tests/test_analysis_api.py tests/test_datasets_api.py` 一起跑）本文件 9 条用例
    集体变红，报 `{'sales.csv': (1, 'test-admin'), ...}` —— 上一条命令里上传的
    `sales.csv` 活到了这里，"空列表"和"分页条数"这类断言全被它顶掉了。

    所以：进入时**清空**（而不是复制一份），退出时还原成进入前的样子 ——
    这样既不受别人污染，也不把污染留给后面的人。
    """
    frames = dict(store._frames)
    datasets = dict(store._datasets)
    profiles = dict(store._profiles)
    store._frames.clear()
    store._datasets.clear()
    store._profiles.clear()
    yield
    store._frames.clear()
    store._frames.update(frames)
    store._datasets.clear()
    store._datasets.update(datasets)
    store._profiles.clear()
    store._profiles.update(profiles)


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    """上传目录指到 tmp_path：测试永不写项目里的 data/uploads/。"""
    target = tmp_path / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(target))
    config.reset_settings_cache()
    return target


@pytest.fixture
def client(upload_dir):
    """已登录（`tests/auth_support.py` 造的 test-admin）的真实 HTTP 客户端。"""
    with TestClient(app) as test_client:
        # 把"这个客户端用哪个账号库"记在客户端身上：`_read_user_ids()` 靠它直接查库。
        # 为什么不用配置去推：`_ensure_owner_ids_differ()` 会在配置已还原成默认库之后
        # 被调用，那时读配置得到的不是这个客户端的库。
        test_client._dsh_auth_db_path = config.get_settings().auth_db_file
        yield test_client


def _upload(client: TestClient, content: str, filename: str):
    return client.post(UPLOAD_URL, files={"file": (filename, content.encode("utf-8"))})


def _uploaded_id(client: TestClient, content: str, filename: str) -> str:
    response = _upload(client, content, filename)
    assert response.status_code == 200, response.text
    return response.json()["data"]["dataset_id"]


def _names(body) -> list[str]:
    return [item["original_filename"] for item in body["data"]["items"]]


class _SecondUser:
    """第二个用户：自己的账号库 + 自己的会话 cookie。

    它是个**上下文管理器**：在 `with` 块里 `config.get_settings` 被换成"指向它的
    账号库"，于是这个 client 的每个请求（包括 conftest 补丁里的补会话、服务端
    `resolve_session` 查库）都作用在它自己的库上。出了 `with` 块自动还原。

    为什么必须用 with 而不是"建完就还原"：`AuthStore` 是**按库路径**缓存的，而
    `get_auth_store()` 读的是**当前**配置。还原之后再用这个 client 发请求，服务端
    会去默认库里查它那条会话 —— 查不到，于是受保护接口回 401（实测表现是上传报
    `401 请先登录`），或 `/api/auth/me` 回 `data: null`。
    """

    def __init__(
        self,
        client: TestClient,
        username: str,
        db_path,
        settings: object,
        install_override,
        restore,
    ) -> None:
        self.client = client
        self.user_id = 0
        self.username = username
        self.db_path = db_path
        self.settings = settings
        self._install_override = install_override
        self._restore = restore
        self._cookie_name = settings.auth_cookie_name  # type: ignore[attr-defined]

    def __enter__(self) -> "_SecondUser":
        self._install_override()
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self._restore()
        self.client.__exit__(None, None, None)
        return False


def _ensure_owner_ids_differ(client: TestClient, other: "_SecondUser") -> None:
    """保证两个用户的 user id 真的不同，并把这件事**断言出来**。

    为什么必须做：两个用户各自一个账号库，`bootstrap` 建的都是那个库里的第一个账号,
    id 一律是 1。而数据集仓库（`DatasetStore`）是**进程内单例**、归属只存一个整数 id ——
    于是"我的.csv / 他的.csv"两条记录的 `owner_user_id` 都是 1，按归属过滤的断言
    完全区分不出两个人。实测症状：二号用户的列表里出现了"我的.csv"，
    而生产代码其实是对的（它确实按 1 过滤了，只是两个人的 id 恰好撞了）。

    做法：把第二个用户的 id 从 1 改成 2（两个库各自独立，改哪个都行）——
    `bootstrap` 建的第一个账号只能是 1，所以只能事后改。**必须连它的会话行一起搬**
    （`_renumber_user` 现在两张表都改），否则改完这个用户的会话立刻失效、
    后续请求变 401。

    ⚠️ 这里**只查库、不发 HTTP**：这个函数在 `_second_user` 的 `with` 块外面调用，
    全局配置已经还原成默认库了，此时用任何一个 client 发请求都可能拿到 401
    （会话在另一个库里）。改完直接读库确认，比"发个请求看看"更可靠。
    `client` 参数保留是为了让调用点显式写出"这两个客户端属于哪两个用户"。
    """
    mine = _read_user_ids(client)
    theirs = _read_user_ids(other.client)
    if mine and theirs and mine[0] != theirs[0]:
        other.user_id = theirs[0]
        return
    new_id = _renumber_user(other.db_path, old_id=other.user_id, new_id=other.user_id + 1)
    assert _read_user_ids(other.client)[0] == new_id != mine[0], (
        "两个用户的 id 还是相同，按归属过滤的断言会失去意义："
        f"mine={mine} theirs={_read_user_ids(other.client)}"
    )
    other.user_id = new_id


def _read_user_ids(client: TestClient) -> list[int]:
    """从数据库里读出这个账号库当前有哪些 user id（升序）。

    为什么不用 `/api/auth/me`：这个辅助函数会在"全局配置已经指回默认库"之后被调用，
    那时 client 的会话在另一个库里，`/api/auth/me` 会返回 `data: null`
    （实测 `TypeError: 'NoneType' object is not subscriptable`）。
    直接查库与 HTTP 层无关，永远能看到真相。
    """
    path = getattr(client, "_dsh_auth_db_path", None)
    if path is None:
        return []
    with sqlite3.connect(str(path)) as conn:
        rows = conn.execute("SELECT id FROM auth_users ORDER BY id").fetchall()
    return [int(row[0]) for row in rows]


def _renumber_user(db_path, *, old_id: int, new_id: int) -> int:
    """把账号的 id 从 `old_id` 改成 `new_id`（只用于测试造数据）。

    为什么直接改库：产品里没有任何"改 id"的路径，而测试需要一个**确定的**不同 id
    （`bootstrap` 建的第一个账号只能是 1）。两个账号库互相独立，改其中一个不影响
    另一个。

    ⚠️ **两张表都要改**：`auth_sessions.user_id` 是指向 `auth_users` 的外键。
    只改 `auth_users` 的话，那条会话会指向一个不存在的 user_id，
    `resolve_session()` 的"账号还在吗"这一步直接判否 —— 表现为 `/api/auth/me`
    返回 `authenticated: False`、后续请求全部 401，而 `attach_session_cookie()`
    看到 jar 里已经有 cookie 也不会补新的，于是看起来像"会话凭空失效"。
    """
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE auth_sessions SET user_id = ? WHERE user_id = ?", (new_id, old_id))
        conn.execute("UPDATE auth_users SET id = ? WHERE id = ?", (new_id, old_id))
        conn.commit()
    return new_id


def _second_user(
    tmp_path,
    monkeypatch,
    username: str = "second-admin",
    db_name: str = "auth-second",
) -> _SecondUser:
    """造一个真实账号库并 bootstrap 出第二个账号（用 `with` 包住使用期）。

    为什么用 HTTP 调一次 `bootstrap`（而不是直接往库里插一行）：
    bootstrap 是产品里**唯一的**建号路径，它顺便验证了"空库可用 / 建完即登录"。
    直接插库会绕过它，将来它坏了这个测试也不会红。

    三个必须注意的点：
    - `bootstrap` 的响应体要在 `with` 里读完（客户端一关，再去读响应体会变 None）；
    - **建号前必须把 client 标成匿名**。conftest 的补丁在每次请求前都会"没有会话就补
      一条测试会话"，而补会话要先在**当前**账号库里建 `test-admin` 账号 —— 于是这个
      全新的库在建号之前就被塞了一个账号，bootstrap 直接 403
      "账号库已经有账号了，不能再执行初始化"。这个坑的表现是"裸 Python 探针能过、
      进 pytest 就 403"，因为补丁只活在 pytest 里。
    - `config.get_settings` 在这个 `with` 块里一直被换成指向这个库的版本，出了块才
      还原（见类 docstring 里"为什么不能建完就还原"）。
    """
    original_get_settings = config.get_settings
    db_path = tmp_path / db_name / "auth.db"
    settings = config.Settings(auth_db_path=str(db_path))
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    second = TestClient(app)
    second._dsh_auth_db_path = db_path  # 供 _read_user_ids() 直接查这个库
    second.__enter__()

    def install_override() -> None:
        from app.data.users import reset_auth_store_cache

        monkeypatch.setattr(config, "get_settings", lambda: settings)
        reset_auth_store_cache()

    def restore() -> None:
        from app.data.users import reset_auth_store_cache

        monkeypatch.setattr(config, "get_settings", original_get_settings)
        reset_auth_store_cache()

    user = _SecondUser(second, username, db_path, settings, install_override, restore)

    install_override()
    try:
        auth_support.mark_client_as_anonymous(second)
        response = second.post(
            "/api/auth/bootstrap",
            json={"username": username, "password": "second-admin-pass1"},
        )
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["user"]["username"] == username
        user.user_id = body["user"]["id"]
    except BaseException:
        # 建号失败时别把客户端和配置替身留在身后
        restore()
        second.__exit__(None, None, None)
        raise
    return user


def _login_cookie(client: TestClient) -> str:
    """从客户端 cookie jar 里取出会话 cookie 的**值**。

    为什么不写 `client.cookies.get(name)`：出现两条同名 cookie 时 httpx 会抛
    `CookieConflict`（conftest 的补丁和用例自己设的那条可能各占一个域）。
    """
    name = config.get_settings().auth_cookie_name
    for cookie in client.cookies.jar:
        if cookie.name == name and cookie.path == "/":
            return str(cookie.value)
    raise AssertionError(f"cookie jar 里没有 {name}")


# ------------------------------------------------- 1. 列表形态（外壳与字段）


def test_list_requires_login(client):
    """未登录是 401：归属判定之前先过登录（`main.py` 挂在 router 上的依赖）。"""
    auth_support.mark_client_as_anonymous(client)
    response = client.get(LIST_URL)
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_empty_list_is_an_empty_array_not_an_error(client):
    """一条数据都没有时是 `items: []` + `total: 0`，不是 404 / 500。"""
    response = client.get(LIST_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["error"] is None
    assert body["data"]["items"] == []
    assert body["data"]["total"] == 0
    # limit / offset 是**回显**（前端拿它确认服务端真的收到了自己的分页参数）
    assert body["data"]["limit"] == 50
    assert body["data"]["offset"] == 0


def test_list_item_shape_matches_detail_plus_three_numbers(client):
    """列表 item 的字段 = 详情接口那一套 + `rows` / `missing_cells`。

    这是 task-11 的硬契约：前端一套解析代码吃两种响应，字段少一个就会出现
    "列表里显示了、点进详情是空的"这类没人报错的错位。
    """
    dataset_id = _uploaded_id(client, CSV_HOLE, "销售.csv")

    detail = client.get(f"{LIST_URL}/{dataset_id}").json()["data"]
    item = client.get(LIST_URL).json()["data"]["items"][0]

    missing_in_item = set(detail) - set(item)
    assert missing_in_item == set(), f"列表少了字段: {sorted(missing_in_item)}"
    assert set(item) - set(detail) == {"rows", "missing_cells"}, (
        "列表多出来的字段只应该是 task 要求的这两个，多别的说明形状开始分叉了"
    )
    for key, value in detail.items():
        if key in ("columns", "dtypes", "preview_rows"):
            # 这三个是"预览"字段，列表里显式置空（省带宽），与详情不相等是对的
            continue
        assert item[key] == value, f"字段 {key} 在列表与详情里不一致"


def test_list_item_reports_rows_and_columns_and_missing_cells(client):
    """三个数字要对得上真实数据：行数、列数、缺失值总数（画像没算过就是 null）。"""
    _uploaded_id(client, CSV_HOLE, "带空洞.csv")
    _uploaded_id(client, CSV_SIX_ROWS, "六行.csv")

    by_name = {item["original_filename"]: item for item in client.get(LIST_URL).json()["data"]["items"]}

    assert by_name["带空洞.csv"]["rows"] == 3
    assert by_name["带空洞.csv"]["row_count"] == 3
    assert by_name["带空洞.csv"]["column_count"] == 2
    assert by_name["带空洞.csv"]["columns"] == ["日期", "销售额"], "columns 仍是列名数组"
    assert by_name["带空洞.csv"]["missing_cells"] is None, "还没算画像 -> null，不是 0"
    assert by_name["带空洞.csv"]["has_profile"] is False

    assert by_name["六行.csv"]["rows"] == 6, "row_count 是全部 6 行，不是预览的 5 行"
    assert by_name["六行.csv"]["column_count"] == 2


def test_missing_cells_is_filled_only_after_the_profile_was_built(client):
    """算过画像之后 `missing_cells` 才有值，而且是真实求和（不是 0 占位）。"""
    dataset_id = _uploaded_id(client, CSV_HOLE, "带空洞.csv")

    assert client.get(LIST_URL).json()["data"]["items"][0]["missing_cells"] is None

    profile = client.get(f"{LIST_URL}/{dataset_id}/profile").json()["data"]
    expected = sum(column["null_count"] for column in profile["columns"])
    assert expected == 1, "这份 CSV 只有一个空单元格"

    item = client.get(LIST_URL).json()["data"]["items"][0]
    assert item["missing_cells"] == expected == 1
    assert item["has_profile"] is True


# ------------------------------------------------------------ 2. 分页与排序


def test_pagination_slices_without_losing_rows(client):
    """翻页不重叠、不遗漏，`total` 是全量而不是本页条数。"""
    for index in range(5):
        _uploaded_id(client, CSV_HOLE, f"文件{index}.csv")

    first = client.get(LIST_URL, params={"limit": 2, "offset": 0}).json()["data"]
    second = client.get(LIST_URL, params={"limit": 2, "offset": 2}).json()["data"]
    last = client.get(LIST_URL, params={"limit": 2, "offset": 4}).json()["data"]

    assert len(first["items"]) == 2 and len(second["items"]) == 2 and len(last["items"]) == 1
    assert first["total"] == second["total"] == 5
    names = _names({"data": first}) + _names({"data": second}) + _names({"data": last})
    assert sorted(names) == sorted(f"文件{index}.csv" for index in range(5))
    assert len(set(names)) == 5, "翻页出现了重复条目"


def test_sort_by_filename_and_rows_and_uploaded_at(client):
    """三种排序都真的作用在**全量**数据上（不是只排当前这一页）。

    做法：造出"文件名顺序 ≠ 行数顺序 ≠ 上传顺序"的三条数据，然后按 limit=2 翻页，
    把两页拼起来看。如果排序发生在切页之后，第 2 页就会明显不对。
    """
    _uploaded_id(client, CSV_HOLE, "bbb.csv")  # 3 行
    _uploaded_id(client, CSV_BIG, "aaa.csv")  # 40 行
    _uploaded_id(client, CSV_SIX_ROWS, "ccc.csv")  # 6 行

    def paged(**params):
        params = {"limit": 2, **params}
        page0 = client.get(LIST_URL, params={**params, "offset": 0}).json()["data"]["items"]
        page1 = client.get(LIST_URL, params={**params, "offset": 2}).json()["data"]["items"]
        return [item["original_filename"] for item in page0 + page1]

    assert paged(sort="filename", order="asc") == ["aaa.csv", "bbb.csv", "ccc.csv"]
    assert paged(sort="filename", order="desc") == ["ccc.csv", "bbb.csv", "aaa.csv"]
    assert paged(sort="rows", order="desc") == ["aaa.csv", "ccc.csv", "bbb.csv"]
    assert paged(sort="rows", order="asc") == ["bbb.csv", "ccc.csv", "aaa.csv"]
    # 上传顺序：bbb -> aaa -> ccc，所以倒序是 ccc, aaa, bbb
    assert paged(sort="uploaded_at", order="desc") == ["ccc.csv", "aaa.csv", "bbb.csv"]
    assert paged(sort="uploaded_at", order="asc") == ["bbb.csv", "aaa.csv", "ccc.csv"]


def test_sort_is_stable_when_the_sort_key_ties(client):
    """排序键取值相同（同样行数）时顺序必须**确定**：靠 dataset_id 兜底。

    没有兜底键时顺序取决于 dict 插入顺序，翻页会出现"同一条出现两次、
    另一条一次都不出现"——这是最难查的一类分页 bug。
    """
    for index in range(4):
        _uploaded_id(client, CSV_SIX_ROWS, f"同样六行{index}.csv")

    first = client.get(LIST_URL, params={"sort": "rows", "order": "asc", "limit": 4}).json()["data"]
    second = client.get(LIST_URL, params={"sort": "rows", "order": "asc", "limit": 4}).json()["data"]
    assert _names({"data": first}) == _names({"data": second}), "同样的请求两次结果不一样"
    assert len(_names({"data": first})) == 4


def test_q_filters_by_filename_substring(client):
    """`q` 按文件名的子串筛选（命中 / 不命中 / 中文 / 只匹配文件名不匹配内容）。"""
    _uploaded_id(client, CSV_HOLE, "销售数据.csv")
    _uploaded_id(client, CSV_SIX_ROWS, "用户名单.csv")
    _uploaded_id(client, CSV_BIG, "库存清单.csv")

    hit = client.get(LIST_URL, params={"q": "名单"}).json()["data"]
    assert _names({"data": hit}) == ["用户名单.csv"]
    assert hit["total"] == 1, "total 必须是**筛选后**的总数，否则前端页数会算错"

    assert _names({"data": client.get(LIST_URL, params={"q": "不存在"}).json()["data"]}) == []
    assert client.get(LIST_URL, params={"q": "清单"}).json()["data"]["total"] == 1


def test_q_is_case_insensitive(client):
    """大小写不敏感：用户不会记得自己当初怎么打的大小写。"""
    _uploaded_id(client, CSV_HOLE, "SalesReport.csv")

    for needle in ("sales", "SALES", "salesreport", "Report"):
        body = client.get(LIST_URL, params={"q": needle}).json()["data"]
        assert _names({"data": body}) == ["SalesReport.csv"], needle


def test_blank_q_means_no_filter(client):
    """空白 `q`（空串 / 全是空格）等于不筛选，而不是"筛出零条"。"""
    _uploaded_id(client, CSV_HOLE, "一个.csv")
    _uploaded_id(client, CSV_HOLE, "两个.csv")

    for blank in ("", "   "):
        body = client.get(LIST_URL, params={"q": blank}).json()["data"]
        assert body["total"] == 2, f"q={blank!r} 不该筛掉任何东西"
        assert len(body["items"]) == 2


def test_q_combines_with_pagination_and_keeps_total_correct(client):
    """`q` + 分页：`total` 是筛选后的总数，翻页不重不漏。

    这是"筛选只做在 items 上、total 却算全量"那个经典 bug 的守门测试 ——
    症状是前端显示 3 页、实际翻到第 2 页就空了。
    """
    for index in range(5):
        _uploaded_id(client, CSV_HOLE, f"报表{index}.csv")
    _uploaded_id(client, CSV_HOLE, "别的.csv")

    first = client.get(LIST_URL, params={"q": "报表", "limit": 2, "offset": 0}).json()["data"]
    second = client.get(LIST_URL, params={"q": "报表", "limit": 2, "offset": 2}).json()["data"]
    third = client.get(LIST_URL, params={"q": "报表", "limit": 2, "offset": 4}).json()["data"]

    assert first["total"] == second["total"] == third["total"] == 5
    names = _names({"data": first}) + _names({"data": second}) + _names({"data": third})
    assert sorted(names) == sorted(f"报表{index}.csv" for index in range(5))
    assert len(set(names)) == 5, "翻页出现了重复条目"


def test_q_too_long_is_422(client):
    """`q` 有长度上限：子串匹配用不到超长输入，别让它变成免费的资源消耗。"""
    response = client.get(LIST_URL, params={"q": "长" * 201})
    assert response.status_code == 422, response.text
    assert "q" in response.text


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"sort": "id"}, "sort"),
        ({"sort": "missing_cells"}, "sort"),
        ({"order": "升序"}, "order"),
        ({"limit": 0}, "limit"),
        ({"limit": 201}, "limit"),
        ({"offset": -1}, "offset"),
    ],
)
def test_invalid_query_parameters_are_422(client, params, expected):
    """非法取值是 422 + 统一错误外壳，**不是**静默按默认值处理。

    为什么必须断言"字段名出现在错误里"：只断言状态码的话，
    一个 422 可能来自别的参数（比如 limit 写错），看起来还是会绿。
    """
    response = client.get(LIST_URL, params=params)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "BAD_REQUEST"
    assert expected in response.text, f"错误里应当指出 {expected} 这个参数"


def test_limit_boundaries_are_accepted(client):
    """1 和 200 是合法的（边界值最容易差一位）。"""
    _uploaded_id(client, CSV_HOLE, "一个.csv")
    assert client.get(LIST_URL, params={"limit": 1}).status_code == 200
    assert client.get(LIST_URL, params={"limit": 200}).status_code == 200


def test_upload_outside_the_page_still_counts_in_total(client):
    """`total` 不受 `limit` 影响：它决定前端有几页。"""
    for index in range(3):
        _uploaded_id(client, CSV_HOLE, f"分页{index}.csv")
    body = client.get(LIST_URL, params={"limit": 1}).json()["data"]
    assert len(body["items"]) == 1
    assert body["total"] == 3
    assert body["limit"] == 1


# ------------------------------------------------------------ 3. 归属隔离


def test_other_users_dataset_is_invisible_in_the_list(tmp_path, monkeypatch, client):
    """别人的数据集不出现在我的列表里，`total` 也不含它。

    `_ensure_owner_ids_differ` 是必须的：两个账本各自 bootstrap 出来的 id 都是 1，
    那两条记录的 `owner_user_id` 会撞在一起，这条断言就等于没测（见它的 docstring）。
    """
    mine = _uploaded_id(client, CSV_HOLE, "我的.csv")
    with _second_user(tmp_path, monkeypatch) as other:
        _ensure_owner_ids_differ(client, other)
        theirs = _uploaded_id(other.client, CSV_HOLE, "他的.csv")
        assert theirs != mine

        their_body = other.client.get(LIST_URL).json()["data"]
        assert _names({"data": their_body}) == ["他的.csv"]
        assert their_body["total"] == 1

    body = client.get(LIST_URL).json()["data"]
    assert _names({"data": body}) == ["我的.csv"]
    assert body["total"] == 1, "total 必须是过滤后的数量，否则会泄露别人有多少数据"


def test_other_users_dataset_id_is_404_and_response_is_byte_identical(
    tmp_path, monkeypatch, client
):
    """按 id 直接取别人的数据集：404，且与"这个 id 不存在"**逐字相同**。

    文案有差异 = 可以用它枚举出系统里存在哪些 id（我们的 id 是 uuid4，
    猜不到，但"猜不到"不该是唯一防线）。
    """
    with _second_user(tmp_path, monkeypatch) as other:
        # 必须先岔开 id：两个账号库各自的第一个账号都是 id=1，而归属只存这个整数。
        # 不岔开的话，"取他的 id 得到 404"这条断言会因为"他其实就是我"而**恒成立** ——
        # 一个永远绿的测试比没有测试更糟（测试代码本身也不该靠巧合通过）。
        _ensure_owner_ids_differ(client, other)
        theirs = _uploaded_id(other.client, CSV_HOLE, "他的.csv")
        never_existed = "0123456789abcdef0123456789abcdef"

        stolen = client.get(f"{LIST_URL}/{theirs}")
        missing = client.get(f"{LIST_URL}/{never_existed}")

        assert stolen.status_code == 404, stolen.text
        assert missing.status_code == 404
        assert stolen.json() == missing.json(), "两种情况的响应必须完全一致（否则可枚举 id）"
        assert theirs not in stolen.text, "响应里不许回显别人给你的 id"


def test_other_users_profile_is_404_too(tmp_path, monkeypatch, client):
    """画像是另一条读路径，也要一起判归属（漏一个就是越权）。"""
    with _second_user(tmp_path, monkeypatch) as other:
        # 同 test_other_users_dataset_id_is_404_and_response_is_byte_identical：
        # 不岔开 id 的话，"他的"其实还是"我的"，404 断言恒成立、测不出归属。
        _ensure_owner_ids_differ(client, other)
        theirs = _uploaded_id(other.client, CSV_HOLE, "他的.csv")

        response = client.get(f"{LIST_URL}/{theirs}/profile")
        assert response.status_code == 404, response.text
        assert response.json()["error"]["message"] == "数据集不存在"


def test_dataset_without_owner_is_invisible_to_everyone(client):
    """改造前登记的、没有归属的数据：谁也不给看（不是"公共可见"）。

    直接往仓库里塞一条 `owner_user_id=None` 的记录来模拟老数据 ——
    进程内单例在测试里就常有这种对象。
    """
    import pandas as pd

    orphan = store.register(
        "f" * 32,
        pd.DataFrame({"a": [1]}),
        "无主.csv",
        "csv",
        12,
        "0" * 64,
    )
    assert orphan.owner_user_id is None

    assert client.get(LIST_URL).json()["data"]["items"] == []
    assert client.get(f"{LIST_URL}/{orphan.dataset_id}").status_code == 404


def test_owner_is_recorded_on_upload(client):
    """上传时真的把当前登录用户写进了归属（而不是默认 None）。"""
    dataset_id = _uploaded_id(client, CSV_HOLE, "归属.csv")
    item = client.get(LIST_URL).json()["data"]["items"][0]
    assert item["dataset_id"] == dataset_id
    assert item["owner_username"] == auth_support.TEST_USERNAME
    assert isinstance(item["owner_user_id"], int)


def test_switching_cookie_switches_which_rows_you_see(client, tmp_path, monkeypatch):
    """同一进程里两个用户各自 bootstrap：谁的 cookie 就只看到谁的数据。

    这条考的是"归属判定用的是会话里的 user.id"，而不是任何客户端可控的东西。

    四个必须写清楚的地方：

    1. **服务端一次只看一个账号库**（`AUTH_DB_PATH` 是全局配置，`get_auth_store()`
       按它取库）。所以这个用例的写法是"把全局配置指向谁，谁就是服务端认的那个
       库"，两个用户各自在自己的库上被请求。也因此**不能用 `client` 夹具去查
       `/api/auth/me`** —— 它的会话在默认库里，服务端此刻认的却是别的库，会拿到
       `data: null`（实测 `TypeError: 'NoneType' object is not subscriptable`）。
    2. **两个库各自的第一个账号 id 都是 1**，而数据集仓库是进程内单例、归属只存一个
       整数 id —— 不把 id 岔开，"谁的 cookie 看谁的数据"这条断言会失去意义。
       所以下面要调 `_fork_user_id` 把其中一个用户改成 id=2。
    3. **建号前必须把 client 标成匿名**：conftest 的补丁在每次请求前都会"没有会话
       就补一条测试会话"，而补会话要先在当前库里建 `test-admin` —— 全新库会被它
       塞进一个账号，bootstrap 直接 403。裸脚本探针能过、进 pytest 就 403 就是这
       个原因（详细经过见 `.tmp/regression_diagnosis.md`）。
    4. 每条断言前都显式换 cookie，不依赖上一条请求留下的状态。
    """
    from app.data.users import reset_auth_store_cache

    original_get_settings = config.get_settings
    cookie_name = config.get_settings().auth_cookie_name
    clients: list[TestClient] = []

    def point_server_at(db_name: str) -> None:
        """把服务端的账号库指到 `tmp_path/db_name/auth.db`。"""
        db_path = tmp_path / db_name / "auth.db"
        monkeypatch.setattr(
            config, "get_settings", lambda: config.Settings(auth_db_path=str(db_path))
        )
        reset_auth_store_cache()

    def boot(db_name: str, username: str) -> tuple[TestClient, str, int]:
        """在独立的账号库里 bootstrap 一个用户，返回 (client, 会话 token, user id)。

        **不在这里上传数据集**：见下面「顺序」那段说明 —— 上传必须发生在
        id 岔开之后，否则归属记的是岔开前的 id。
        """
        point_server_at(db_name)
        fresh = TestClient(app)
        fresh._dsh_auth_db_path = tmp_path / db_name / "auth.db"
        fresh.__enter__()
        clients.append(fresh)
        auth_support.mark_client_as_anonymous(fresh)
        response = fresh.post(
            "/api/auth/bootstrap",
            json={"username": username, "password": f"{username}-pass1"},
        )
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["user"]["username"] == username
        return fresh, _login_cookie(fresh), body["user"]["id"]

    def renumber_and_move_sessions(db_name: str, *, old_id: int, new_id: int) -> int:
        """把账号 id 从 old_id 改成 new_id，**并且把它的会话一起搬过去**。

        为什么不能只改 `auth_users.id`：`auth_sessions.user_id` 是指向它的外键。
        只改前者的后果实测过 —— `/api/auth/me` 立刻回 `authenticated: False`
        （服务端按会话里的 user_id=1 去 `auth_users` 找，找不到就判定会话失效），
        而那一刻 `attach_session_cookie()` 又不会补一条新会话（jar 里已经有 cookie 了），
        于是**后续请求全部 401**。这个坑的表现是"断言莫名变成未登录"，而不是报错。
        """
        db_path = tmp_path / db_name / "auth.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE auth_sessions SET user_id = ? WHERE user_id = ?", (new_id, old_id))
            conn.execute("UPDATE auth_users SET id = ? WHERE id = ?", (new_id, old_id))
            conn.commit()
        return new_id

    def boot_and_upload(db_name: str, username: str, filename: str) -> tuple[TestClient, str, int]:
        """bootstrap（若需要）→ 上传一个文件 → 返回 (client, token, user id)。"""
        fresh, token, user_id = boot(db_name, username)
        _uploaded_id(fresh, CSV_HOLE, filename)
        return fresh, token, user_id

    try:
        # 顺序很重要，按下面这个顺序才对：
        #  ① 一号：建号（id=1）→ 上传 → 归属记 1
        #  ② 二号：建号（**也是 id=1**，另一个库）→ 把 id 改成 2 → 再上传 → 归属记 2
        # 反过来（先上传再改 id）就测不出东西了：那条记录的归属停在 1，
        # 而一号的 id 也是 1，于是"二号看不到一号的数据"这条断言恒成立 ——
        # 一个永远绿的测试比没有测试更糟（实测正是这么发现的）。
        first, first_token, first_id = boot_and_upload("auth-one", "user-one", "一号.csv")
        second, second_token, second_id = boot("auth-two", "user-two")
        if first_id == second_id:
            # 见 docstring 第 2 点：两个库各自的第一个账号 id 都是 1，必须岔开
            second_id = renumber_and_move_sessions("auth-two", old_id=second_id, new_id=2)
        assert first_id != second_id, "两个用户的 id 必须不同，否则断言无意义"
        point_server_at("auth-two")
        _uploaded_id(second, CSV_HOLE, "二号.csv")

        # 归属是**上传那一刻**写死的：先直接查仓库把这件事钉住，再谈 HTTP 过滤。
        # 只靠 HTTP 断言的话，"服务端根本没写归属（都是 NULL）"和"写对了"看起来一样。
        owners = {
            item.original_filename: (item.owner_user_id, item.owner_username)
            for item in store._datasets.values()
        }
        assert owners == {
            "一号.csv": (first_id, "user-one"),
            "二号.csv": (second_id, "user-two"),
        }, owners

        # ① 服务端认 auth-one：一号的 cookie 只看到一号的数据。
        point_server_at("auth-one")
        first.cookies.clear()
        first.cookies.set(cookie_name, first_token, path="/")
        assert _names({"data": first.get(LIST_URL).json()["data"]}) == ["一号.csv"]

        # ② 服务端改认 auth-two：一号的会话不在这个库里 -> 401（不是"看到二号的数据"）
        point_server_at("auth-two")
        first.cookies.clear()
        first.cookies.set(cookie_name, first_token, path="/")
        assert first.get(LIST_URL).status_code == 401, "别的库里的会话不该被认"

        # ③ 换上二号的 cookie -> 只看得到二号的数据
        first.cookies.clear()
        first.cookies.set(cookie_name, second_token, path="/")
        assert _names({"data": first.get(LIST_URL).json()["data"]}) == ["二号.csv"]

        # ④ 清空 cookie（匿名）则回到 401，而不是"默认看到全部"
        first.cookies.clear()
        auth_support.mark_client_as_anonymous(first)
        assert first.get(LIST_URL).status_code == 401
    finally:
        for probe in clients:
            probe.__exit__(None, None, None)
        monkeypatch.setattr(config, "get_settings", original_get_settings)
        reset_auth_store_cache()


def test_dataset_id_must_look_like_a_uuid_in_the_path(client):
    """路径里的 id 不做格式校验：任何字符串都走"查不到 -> 404"，不是 500。"""
    for path in ("/api/datasets/zzz", "/api/datasets/%20", "/api/datasets/.."):
        response = client.get(path)
        assert response.status_code in (404, 405), f"{path} -> {response.status_code}"


def test_response_does_not_leak_internal_paths(client):
    """列表响应里不许出现磁盘路径等内部信息。"""
    _uploaded_id(client, CSV_HOLE, "敏感.csv")
    text = client.get(LIST_URL).text
    assert "data/uploads" not in text and "data\\uploads" not in text
    assert re.search(r"[A-Za-z]:\\", text) is None, "响应里出现了盘符路径"

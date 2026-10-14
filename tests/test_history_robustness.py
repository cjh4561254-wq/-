"""P3 取证 + 修复回归：历史库里的**枚举外值**必须降级透出，不许打成 500。

历史
----
task-35/36/37 取证时，`app/api/routes_workflows.py` 的 `_item_from_record()` 用
`WorkflowStatus(record.status)` / `ErrorCode(record.error_code)` **硬转**库里的
TEXT 列。`WorkflowStatus` 是 `str` Enum，`WorkflowStatus("RUNNING")` 直接抛
`ValueError: 'RUNNING' is not a valid WorkflowStatus`；
`list_history` 的列表推导
`items=[_item_from_record(record) for record in records]` **没有任何逐行保护**，
于是一条坏行让整个响应构造失败，被 `app/main.py` 的兜底 handler 翻成
`500 INTERNAL_ERROR`、`data=null` —— 同页合法记录**一条都拿不到**。

task-38 时该缺陷**已修复**（Lead 改的产品代码，本文件负责把修复后的行为钉住）：
`_coerce_enum(enum_cls, raw)` 认不出来就**原样保留字符串**（降级，不抛异常），
并且 `HistoryItem.status` / `error_code` 的类型**必须同时放成 `... | str`** ——
只改构造侧不放开字段类型，降级出来的字符串仍会被 pydantic 拒收、继续 500
（这个坑真实发生过：第一轮只加了 `_coerce_enum`，11 种坏值**依然全部 500**，
失败点从 `ValueError` 挪到了模型的 enum 校验）。

本文件现在测的是**修复后的契约**：
  1. 坏行**出现在返回列表里**（不是被跳过），`status` 逐字等于库里那个值
     （含前导/尾随空格、空串）；
  2. `error_code` 空串透出为 `""`，与 `None`（无错误）**语义不同**，必须区分；
  3. 同页合法行**一条不少**（1 坏 + N 合法 -> N+1 行）；
  4. `total` 仍是 `store.count()`（**全库行数，含坏行**），坏行不能让分页错位；
  5. 两处构造（`:154` / `:159`）**必须同批修**，只修一处对"双坏行"零效果。

范围与前提（为什么毒行只能来自跨版本）
--------------------------------------
**正常产品路径产不出非法 status / error_code**，已由静态与运行时两层证据确认
（见 `test_normal_product_paths_cannot_write_an_invalid_status` 与
`..._error_code`）：所有写入点都赋枚举成员或 `None`。所以毒行的唯一来源是
**跨版本持久化**：将来枚举改名或删值，老库里的字符串就成了新代码不认识的值
（`test_a_renamed_enum_value_poisons_the_listing` 反向演示了这一点）。

失败模式与影响面（实测，非推断）
--------------------------------
- 触发：枚举改名/删值后读老数据；或人手改库。**正常路径产不出**。
- 后果：修复前一条坏行让整页 0 行 + 详情 500；修复后坏行原样透出、其余不受影响。

本文件只用 `tmp_path`，绝不碰仓库的 `data/db/`。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.core import archive, registry
from app.data import history
from app.data.history import reset_history_store_cache
from app.main import app
from app.schemas_workflow import WorkflowState, WorkflowStatus

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 修复后的期望值（D-16 第 3 条：写死，两边相同 = 探针无效）。
#: 这两个常量就是「坏行不再毒页」的契约 —— 一旦变回 500，本组用例成批变红。
_EXPECT_LIST_BAD_ROW = 200
_EXPECT_DETAIL_BAD_ROW = 200

_BAD_ID = "f" * 32


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db" / "app.db"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CHARTS_DIR", str(tmp_path / "charts"))
    config.reset_settings_cache()
    reset_history_store_cache()
    registry.clear()
    yield
    registry.clear()
    reset_history_store_cache()
    config.reset_settings_cache()


def _db_path() -> Path:
    return config.get_settings().db_file


def _seed_schema() -> None:
    """让 lifespan 建表（模块级 TestClient 不跑 lifespan）。"""
    with TestClient(app, raise_server_exceptions=False) as _client:
        pass


def _insert_raw(workflow_id: str, status: str, minutes_ago: int = 0,
                question: str = "毒行探针") -> None:
    """**原始 SQL 直插**，完全绕过 HistoryStore / archive_if_terminal。

    这是刻意的：产品代码路径产不出非法 status（pydantic 会先拦下来），
    毒行的唯一来源就是"老版本写进去、新版本读回来"，所以只能用直插来复现。
    """
    fin = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "INSERT INTO workflow_history (workflow_id, question, status, created_at,"
        " finished_at, has_report, llm_used, report_text, error_code, schema_version,"
        " archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (workflow_id, question, status, fin, fin, 0, 0, None, None, 1, fin),
    )
    conn.commit()
    conn.close()


def _legal_id(i: int) -> str:
    return "%032x" % (0x1000 + i)


def _client():
    """把服务端异常翻成 500 响应体，而不是让异常冒到测试里。"""
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------
# 供 `scripts/check_docs.py` 使用的「实测状态码」采集钩子
# --------------------------------------------------------------------------
# 为什么长在测试文件里，而不是让核对脚本自己去跑 HTTP：那段 harness
# （monkeypatch 环境变量 + registry.clear() + lifespan 建表）就在这里，
# 复刻一份必然漂移。核对脚本只调用 `_record_acceptance_status()`。
#
# 钩子只**观察**，不改行为：把用例**体**里发出的第一个请求的状态码记下来，
# 在请求发出的那一刻取值 —— 所以即使用例自己的断言后来失败了，
# 记录到的也还是它当时**真的看到**的那个数字。
_CAPTURED_STATUS: list[int] = []
_ORIGINAL_GET = TestClient.get


def _recording_get(self, *args, **kwargs):
    response = _ORIGINAL_GET(self, *args, **kwargs)
    if not _CAPTURED_STATUS:
        _CAPTURED_STATUS.append(response.status_code)
    return response


@contextmanager
def _capture_status():
    """用例期间的捕获窗口；`_client()` 只在窗口**内**才挂钩。"""
    TestClient.get = _recording_get
    try:
        yield _CAPTURED_STATUS
    finally:
        TestClient.get = _ORIGINAL_GET


def _record_acceptance_status(func):
    """以真实夹具执行 `func`，返回 `{参数化 id 或 "": 首个响应状态码}`。

    给 `scripts/check_docs.py` 用：文档的「投喂行」写死了
    `<用例名> -> 200`，本函数把**每个参数化实例**实测到的状态码交回去，
    让「文档说 200、用例其实已经是 500」这种腐烂变成一条红灯。

    夹具与 rest 用例逐字同源（`tmp_path` + 环境变量 + 缓存重置 + registry 清空）。
    """
    import tempfile

    from _pytest.monkeypatch import MonkeyPatch

    params = getattr(func, "pytestmark", [])
    argnames: list[str] = []
    argvalues: list[tuple] = [()]
    ids: list[str] = [""]
    for mark in params:
        if mark.name == "parametrize":
            # argnames 可能是 "a,b,c" 字符串，也可能是 ("a","b") 序列 ——
            # 直接 list() 一个字符串会把它拆成单个字符（'l','a','b'...），
            # 症状是 `got an unexpected keyword argument 'l'`。
            raw_names = mark.args[0]
            if isinstance(raw_names, str):
                argnames = [name.strip() for name in raw_names.split(",") if name.strip()]
            else:
                argnames = [str(name) for name in raw_names]
            argvalues = list(mark.args[1])
            if len(mark.args) == 3:
                ids = list(mark.args[2])
            break
    if not argvalues:
        argvalues = [()]
        ids = [""]

    recorded: dict[str, int] = {}
    for index, values in enumerate(argvalues):
        if not isinstance(values, tuple):
            values = (values,)
        # pytest 允许「一个参数收一个元组」的写法：`parametrize("pair", [("a", 1)])`
        # 时 argvalues 里是 `(("a", 1),)` —— 这种情况才需要拆一层。
        # 判据看**数量是否对得上**，不要无条件拆，否则 3 参数化会被拆坏。
        if len(values) == 1 and len(argnames) > 1 and isinstance(values[0], (tuple, list)):
            values = tuple(values[0])
        if len(values) != len(argnames):
            raise AssertionError(
                f"参数化解析失败：argnames={argnames!r} 与 values={values!r} 数量不一致"
            )
        if len(ids) == len(argvalues):
            param_id = ids[index]
        else:
            param_id = "-".join(repr(v) for v in values)

        mp = MonkeyPatch()
        tmp = Path(tempfile.mkdtemp(prefix="checkdocs_", dir=str(PROJECT_ROOT / ".tmp")))
        try:
            mp.setenv("DB_PATH", str(tmp / "db" / "app.db"))
            mp.setenv("UPLOAD_DIR", str(tmp / "uploads"))
            mp.setenv("CHARTS_DIR", str(tmp / "charts"))
            config.reset_settings_cache()
            reset_history_store_cache()
            registry.clear()
            _CAPTURED_STATUS.clear()
            with _capture_status():
                func(**dict(zip(argnames, values)))
            recorded[param_id] = _CAPTURED_STATUS[0] if _CAPTURED_STATUS else -1
        finally:
            registry.clear()
            reset_history_store_cache()
            config.reset_settings_cache()
            mp.undo()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
    return recorded


def _items(resp) -> list[dict]:
    """取出列表接口的成功载荷（顺带把"响应形状没变"钉住）。"""
    assert resp.status_code == 200, "期望 200，实际 %d：%s" % (resp.status_code, resp.text[:300])
    body = resp.json()
    assert body["ok"] is True, body
    assert body["error"] is None, body
    return body["data"]["items"]


def _item_of(resp, workflow_id: str) -> dict:
    """在列表里按 id 取出那一条 —— **坏行必须还在列表里**（不是被静默跳过）。

    "坏行出现在返回列表里"是本组用例的核心断言：如果实现改成"跳过坏行"，
    坏行就会查无此条，这条 helper 直接把它变成失败。
    """
    items = _items(resp)
    hits = [it for it in items if it["workflow_id"] == workflow_id]
    assert len(hits) == 1, (
        "坏行没出现在返回列表里（期望 1 条，实际 %d 条）：ids=%r"
        % (len(hits), [it["workflow_id"] for it in items])
    )
    return hits[0]


#: 端点间共用：一条坏行**不许**把同页合法行带走。返回（items, total）。
def _listing_health(resp, expect_rows: int) -> tuple[list[dict], int]:
    """断言列表既不 500 也不丢行：行数恰好 `expect_rows`（= 合法 + 坏）。"""
    items = _items(resp)
    assert len(items) == expect_rows, (
        "返回 %d 行，期望 %d 行（坏行不许吃掉同页的合法行，也不许自己被跳过）"
        % (len(items), expect_rows)
    )
    return items, resp.json()["data"]["total"]


# ==========================================================================
# ① 单条坏行：整页照常返回，坏行原样透出（原始状态码 + 原始响应体）
# ==========================================================================


def test_one_bad_row_no_longer_poisons_the_listing():
    """① 1 条合法 + 1 条非法 -> 列表 **200**，**两行都在**，坏行 `status` 逐字是 `"RUNNING"`。

    修复前的行为：500 + `data=null`，合法的 1 行也拿不到。
    现在合法行一条不少、坏行原样透出（前端据此显示"无法识别"，而不是白屏）。
    """
    _seed_schema()
    _insert_raw(_legal_id(0), "SUCCESS", minutes_ago=5)
    _insert_raw(_BAD_ID, "RUNNING", minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history?limit=100")

    print("\n[P3] GET /api/workflows/history -> %d" % r.status_code)
    print("[P3] 响应体 -> %s" % r.text)

    items, total = _listing_health(r, 2)
    assert total == 2, "total 是全库行数（含坏行），实际 %r" % total

    bad = _item_of(r, _BAD_ID)
    good = _item_of(r, _legal_id(0))
    # ★ 逐字相等：不是落成 None、不是被 strip、不是被吞
    assert bad["status"] == "RUNNING", "坏行 status 必须原样透出，实际 %r" % bad["status"]
    assert type(bad["status"]) is str, "应当是裸字符串，实际 %r" % type(bad["status"])
    assert good["status"] == "SUCCESS", "合法行没受影响：%r" % good["status"]
    # 坏行不能因为"看不出来"就丢掉它自己的其它字段
    assert bad["workflow_id"] == _BAD_ID
    assert set(bad) == set(good), "坏行与合法行的字段集合必须一致：%s" % (set(bad) ^ set(good))

    # 库里 2 行都在 —— 断言"行没丢"，而不是只断言状态码
    conn = sqlite3.connect(_db_path())
    rows = conn.execute("SELECT COUNT(*) FROM workflow_history").fetchone()[0]
    conn.close()
    assert rows == 2, "库里有 %d 行" % rows


def test_every_page_is_served_even_the_one_with_the_bad_row():
    """① 补：坏行**只**影响它自己那一页的数据内容，不再让任何一页 500。

    修复前：`limit=1&offset=0`（坏行）500、`limit=100`（全量）500，
    只有跳过坏行的那页 200。现在三种分页**都是 200**，且坏行在哪页就出现在哪页。
    """
    _seed_schema()
    _insert_raw(_legal_id(0), "SUCCESS", minutes_ago=5)
    _insert_raw(_BAD_ID, "RUNNING", minutes_ago=0)  # 最新，排第一

    with _client() as client:
        only_bad = client.get("/api/workflows/history?limit=1&offset=0")
        only_good = client.get("/api/workflows/history?limit=1&offset=1")
        all_rows = client.get("/api/workflows/history?limit=100")

    print("\n[P3] limit=1&offset=0（坏行）-> %d" % only_bad.status_code)
    print("[P3] limit=1&offset=1（跳过坏行）-> %d" % only_good.status_code)
    print("[P3] limit=100（全量）-> %d" % all_rows.status_code)

    assert only_bad.status_code == _EXPECT_LIST_BAD_ROW
    assert all_rows.status_code == _EXPECT_LIST_BAD_ROW
    assert only_good.status_code == 200, (
        "跳过坏行的分页应当正常，实际 %d：%s" % (only_good.status_code, only_good.text))

    # 坏行在自己那一页里被原样透出
    page0 = _items(only_bad)
    assert [it["workflow_id"] for it in page0] == [_BAD_ID]
    assert page0[0]["status"] == "RUNNING"
    assert only_good.json()["data"]["items"][0]["workflow_id"] == _legal_id(0)
    assert len(_items(all_rows)) == 2, "全量页两行都在"

    # ★ 分页不变式：total 是**全库行数**，不随页大小变（前端据此算总页数）
    assert only_bad.json()["data"]["total"] == 2
    assert only_good.json()["data"]["total"] == 2
    assert all_rows.json()["data"]["total"] == 2


# ==========================================================================
# ② 影响面量化：10 条合法 + 1 条非法
# ==========================================================================


def test_ten_legal_rows_plus_one_bad_row_all_eleven_are_returned():
    """② 10 条合法 + 1 条非法 -> **11 行全部返回**（修复前是 0 行）。

    关键数字：坏行:合法行 = 1:10 时，返回行数从 **0** 变成 **11**（= N+1），
    且 10 条合法行一条不少。
    """
    _seed_schema()
    for i in range(10):
        _insert_raw(_legal_id(i), "SUCCESS", minutes_ago=20 - i)
    _insert_raw(_BAD_ID, "RUNNING", minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history?limit=100")

    print("\n[P3] 库里 11 行（10 合法 + 1 非法）-> GET 状态码 %d" % r.status_code)
    print("[P3] 用户实际能拿到的行数 -> %d（修复前是 0，期望 11）"
          % len(_items(r)) if r.status_code == 200 else "[P3] 用户拿到 0 行")

    items, total = _listing_health(r, 11)
    assert total == 11
    assert items[0]["workflow_id"] == _BAD_ID and items[0]["status"] == "RUNNING"

    # 10 条合法行一条不少（按 id 集合核对，不靠数数）
    legal_ids = {_legal_id(i) for i in range(10)}
    returned_ids = {it["workflow_id"] for it in items}
    assert returned_ids == legal_ids | {_BAD_ID}, "少返回了：%r" % (legal_ids - returned_ids)
    assert all(it["status"] == "SUCCESS" for it in items[1:]), "合法行状态被改动了"

    conn = sqlite3.connect(_db_path())
    legal = conn.execute(
        "SELECT COUNT(*) FROM workflow_history WHERE status='SUCCESS'").fetchone()[0]
    conn.close()
    assert legal == 10, "库里合法行数 %d" % legal


# ==========================================================================
# ③ 大小写与乱值：一律**降级原样透出**，不做 strip / 大小写宽容 / 静默改写
# ==========================================================================


@pytest.mark.parametrize("bad_status", ["Success", "success", "bogus", "RUNNING",
                                        "SUCCESS_WITH_WARNING", " SUCCESS", ""])
def test_miscased_and_bogus_statuses_are_passed_through_verbatim(bad_status):
    """③ `"Success"`（大小写）/ `"bogus"`（乱值）/ 空串 / 带空格 —— 全部 200 且**逐字保留**。

    ⚠️ 这里同时钉住两件事，缺一不可：
      (a) 不 500（可用性）；
      (b) **不做模糊匹配** —— `"Success"` 不许被当成 `"SUCCESS"`、`" SUCCESS"`
          的前导空格不许被 strip、`""` 不许被改成 `None`。
    只断言 (a) 会让这条用例退化成"只要 HTTP 有响应就算过"的空壳。
    """
    _seed_schema()
    _insert_raw(_legal_id(0), "SUCCESS", minutes_ago=5)
    _insert_raw(_BAD_ID, bad_status, minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history?limit=100")

    print("\n[P3] status=%-24r -> %d" % (bad_status, r.status_code))
    assert r.status_code == _EXPECT_LIST_BAD_ROW, (
        "status=%r 拿到了 %d，与其它非法值不一致" % (bad_status, r.status_code))

    items, total = _listing_health(r, 2)
    assert total == 2
    bad = _item_of(r, _BAD_ID)
    assert bad["status"] == bad_status, (
        "逐字相等失败：注入 %r，返回 %r（做 strip / 大小写宽容都会在这里被抓到）"
        % (bad_status, bad["status"])
    )
    assert _item_of(r, _legal_id(0))["status"] == "SUCCESS"


# ==========================================================================
# ⑤ 详情接口是否同样炸
# ==========================================================================


def test_the_detail_endpoint_survives_the_same_bad_row():
    """⑤ 详情接口打**坏行自己**也 200，且 `item.status` 逐字透出。

    对照组：合法行同样 200 —— 两条路径都不许再炸。
    """
    _seed_schema()
    _insert_raw(_legal_id(0), "SUCCESS", minutes_ago=5)
    _insert_raw(_BAD_ID, "RUNNING", minutes_ago=0)

    with _client() as client:
        bad = client.get("/api/workflows/history/%s" % _BAD_ID)
        good = client.get("/api/workflows/history/%s" % _legal_id(0))

    print("\n[P3] 详情（坏行）-> %d | %s" % (bad.status_code, bad.text[:200]))
    print("[P3] 详情（合法行，对照）-> %d | %s" % (good.status_code, good.text[:200]))

    assert bad.status_code == _EXPECT_DETAIL_BAD_ROW
    assert bad.json()["ok"] is True
    assert bad.json()["data"]["item"]["status"] == "RUNNING", bad.json()["data"]["item"]
    # 详情载荷形状没变：item + report（这行 has_report=0，所以 report 是 None）
    assert bad.json()["data"]["report"] is None

    assert good.status_code == 200, "合法行详情应当 200，实际 %d" % good.status_code
    assert good.json()["data"]["item"]["status"] == "SUCCESS"


def test_the_bad_row_is_reachable_through_the_detail_endpoint():
    """⑤ 补：坏行现在**也能被单独打开** —— 列表看得到，拿 id 打详情也 200。

    修复前这条是"它自己永远看不回来"（列表 500 + 详情 500），
    用户既不知道它存在、也没法排查。现在两处都能拿到原样字符串。
    """
    _seed_schema()
    _insert_raw(_BAD_ID, "RUNNING", minutes_ago=0)

    # 库层：这行确实存在（get() 不经过枚举转换，读得出来）
    assert history.HistoryStore(_db_path()).get(_BAD_ID) is not None

    with _client() as client:
        listed = client.get("/api/workflows/history")
        detail = client.get("/api/workflows/history/%s" % _BAD_ID)

    assert listed.status_code == _EXPECT_LIST_BAD_ROW
    assert detail.status_code == _EXPECT_DETAIL_BAD_ROW
    assert _item_of(listed, _BAD_ID)["status"] == "RUNNING"
    assert detail.json()["data"]["item"]["status"] == "RUNNING"
    # 列表与详情对同一行给的值必须一致（两处都走 _item_from_record）
    assert (_item_of(listed, _BAD_ID)["status"]
            == detail.json()["data"]["item"]["status"])


def test_a_healthy_history_is_unaffected():
    """对照：**没有**坏行时两个接口都正常 —— 证明上面所有 500 都是坏行造成的。

    没有这条对照，前面的 500 也可能被误读成"接口本来就坏的"。
    """
    _seed_schema()
    for i in range(5):
        _insert_raw(_legal_id(i), "SUCCESS", minutes_ago=10 - i)

    with _client() as client:
        listed = client.get("/api/workflows/history?limit=100")
        detail = client.get("/api/workflows/history/%s" % _legal_id(0))

    print("\n[P3] 无坏行：列表 -> %d | 详情 -> %d"
          % (listed.status_code, detail.status_code))
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 5
    assert len(listed.json()["data"]["items"]) == 5
    assert detail.status_code == 200


# ==========================================================================
# ④ 前提：正常产品路径产不出非法 status
# ==========================================================================


def test_normal_product_paths_cannot_write_an_invalid_status():
    """④ P3 结论成立的前提：**所有**经 `build_record()` 的写入都是合法枚举值。

    静态证据（grep 出来的写入点）：
        app/agents/orchestrator.py:432    state.status = status          # _enter()
        app/agents/orchestrator.py:441    state.status = status          # _finish()
        app/agents/orchestrator.py:455    state.status = WorkflowStatus.FAILED  # _fail()
        app/api/routes_workflows.py:429   state.status = WorkflowStatus.FAILED  # 超时判定
        app/core/background.py:69         state.status = WorkflowStatus.FAILED  # 线程兜底
    `_enter` / `_finish` 的形参类型标注就是 `status: WorkflowStatus`，
    `_finish(state, _final_status(state))` 传的也是枚举成员。

    这里再加一层**运行时**证据：把所有 10 个成员逐个喂进去，确认零非法产出。
    """
    valid = {s.value for s in WorkflowStatus}
    produced = []
    for status in WorkflowStatus:
        state = WorkflowState(
            workflow_id="%032x" % len(produced),
            dataset_id="abcdef0123456789abcdef0123456789",
            question="前提验证",
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        produced.append(archive.build_record(state).status)

    print("\n[P3] build_record() 覆盖 %d 个枚举成员 -> %s"
          % (len(produced), produced))
    assert len(produced) == len(list(WorkflowStatus)) == 10
    assert set(produced) == valid, "有成员没被覆盖到：%s" % (valid - set(produced))
    assert not [p for p in produced if p not in valid], "产出了非法 status"


def test_a_renamed_enum_value_still_lists_after_the_rename():
    """④ 补：**跨版本**是毒行的唯一来源 —— 反向演示枚举改名后的后果**已被兜住**。

    把库里一个今天合法的值改成"新版才有的名字"，接口仍然 200 且原样透出，
    而不是 500。这就是修复要解决的现实场景：将来 `WorkflowStatus` 改名/删值，
    老库里的字符串就成了新代码不认识的值。
    """
    _seed_schema()
    _insert_raw(_legal_id(0), "SUCCESS_WITH_WARNINGS", minutes_ago=5)

    with _client() as client:
        before = client.get("/api/workflows/history")
    print("\n[P3] 存当前枚举认识的值 SUCCESS_WITH_WARNINGS -> %d" % before.status_code)
    assert before.status_code == 200, "这个值今天合法，应当 200：%s" % before.text
    assert _item_of(before, _legal_id(0))["status"] == "SUCCESS_WITH_WARNINGS"

    # 模拟"新版枚举把它改名成 PARTIAL，老数据没迁"
    conn = sqlite3.connect(_db_path())
    conn.execute("UPDATE workflow_history SET status='PARTIAL'")
    conn.commit()
    conn.close()

    with _client() as client:
        after = client.get("/api/workflows/history")
    print("[P3] 同一行改成新名 PARTIAL（老数据残留）-> %d | %s"
          % (after.status_code, after.text[:200]))
    assert after.status_code == _EXPECT_LIST_BAD_ROW
    assert _item_of(after, _legal_id(0))["status"] == "PARTIAL", (
        "改名后的老值必须原样透出，供前端显示'无法识别'"
    )


# ==========================================================================
# 数据层不背锅：坏行在 HistoryStore 层是**读得出来**的
# ==========================================================================


def test_the_store_layer_reads_bad_rows_fine():
    """数据层对非法 status 是宽容的（TEXT 列，不转枚举）—— 接口层同样宽容。

    这条划清责任边界：修的是 `_item_from_record`（API 层），
    `app/data/history.py` 按设计只存字符串、不做枚举校验，没有改动也不该改。
    """
    _seed_schema()
    _insert_raw(_BAD_ID, "RUNNING", minutes_ago=0)

    store = history.HistoryStore(_db_path())
    rec = store.get(_BAD_ID)
    print("\n[P3] store.get() -> status=%r（原样读出，不抛）" % rec.status)
    assert rec is not None
    assert rec.status == "RUNNING"
    assert store.count() == 1
    assert len(store.list_recent(limit=10)) == 1

    with _client() as client:
        r = client.get("/api/workflows/history")
    # 关键：库层读到的原样字符串，与接口层透出的原样字符串**逐字一致**
    assert r.status_code == _EXPECT_LIST_BAD_ROW
    assert _item_of(r, _BAD_ID)["status"] == rec.status == "RUNNING"


# ==========================================================================
# ============ task-36：ErrorCode 同类硬转 + 枚举硬转点清单 ============
# ==========================================================================
#
# task-36 只证明了 `status` 那一处（`routes_workflows.py:154`）。
# 同一个函数里**还有一处同款硬转**（`:159`）：
#
#     error_code=ErrorCode(record.error_code) if record.error_code is not None else None
#
# 本段把两处**同批修复后**的契约钉住，并用「逐处修复判别」用例证明：
# 只修一处对"双坏行"零效果 —— 所以两处必须一起修。
#
# 修复后的不变式：非法 `error_code` 原样透出（`""` 与 `None` 语义不同）；
# `error_code IS NULL` 的行完全不受影响。


#: 修复后的期望值（D-16 第 3 条：写死）。
_EXPECT_LIST_BAD_ERROR_CODE = 200
_EXPECT_DETAIL_BAD_ERROR_CODE = 200

#: 三种代表性非法 error_code：
#:   - `TIMEOUT_LEGACY` 模拟**老版本的名字**（跨版本持久化，唯一现实触发条件）
#:   - `bogus` 纯乱值
#:   - `""` 空串（"有值但无意义"）—— **必须与 NULL 区分**：空串 ≠ 无错误
_BAD_ERROR_CODES = ["TIMEOUT_LEGACY", "bogus", ""]


def _insert_raw_error_code(workflow_id: str, status: str, error_code: str | None,
                           minutes_ago: int = 0, question: str = "毒行探针") -> None:
    """**原始 SQL 直插**，可以指定 `error_code`（含非法值）。

    不复用 `_insert_raw`：它把 `error_code` 写死成 None，而本段要的正是非空非法值。
    两个 helper 都直插，都会绕过 `HistoryStore` / `archive_if_terminal`。
    """
    fin = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "INSERT INTO workflow_history (workflow_id, question, status, created_at,"
        " finished_at, has_report, llm_used, report_text, error_code, schema_version,"
        " archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (workflow_id, question, status, fin, fin, 0, 0, None, error_code, 1, fin),
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("bad_code", _BAD_ERROR_CODES)
def test_one_bad_error_code_no_longer_poisons_the_listing(bad_code):
    """① 非法 `error_code`（3 种）-> 列表 **200**，两条都在，坏码**逐字透出**。

    与 `status` 那条的机制完全相同（修复前 `ErrorCode(record.error_code)` 硬转抛
    `ValueError: '<值>' is not a valid ErrorCode`）。

    ★ 空串必须断言成 `""` 而**不是** `None`：`""` 是"有值但无法识别"，
    `None` 是"没有错误"。两者混同会让前端把一次失败显示成"无错误"。
    """
    _seed_schema()
    _insert_raw_error_code(_legal_id(1), "FAILED", bad_code, minutes_ago=0)
    _insert_raw_error_code(_legal_id(2), "SUCCESS", None, minutes_ago=5)

    with _client() as client:
        r = client.get("/api/workflows/history")

    print("\n[P3-error_code] error_code=%r -> %d" % (bad_code, r.status_code))
    assert r.status_code == _EXPECT_LIST_BAD_ERROR_CODE

    items, total = _listing_health(r, 2)
    assert total == 2, "total 是全库行数（含坏行），实际 %r" % total

    bad = _item_of(r, _legal_id(1))
    assert bad["error_code"] == bad_code, (
        "坏码必须逐字透出：注入 %r，返回 %r" % (bad_code, bad["error_code"]))
    assert type(bad["error_code"]) is str
    if bad_code == "":
        assert bad["error_code"] is not None, (
            "空串被写成了 None —— 空串 ≠ 无错误，这个区分必须保住"
        )
        assert bad["error_code"] == ""
    else:
        assert bad["error_code"] is not None

    # 同页那条合法的（error_code IS NULL，本来就不该炸）仍然完好
    ok = _item_of(r, _legal_id(2))
    assert ok["status"] == "SUCCESS"
    assert ok["error_code"] is None, "NULL 就是 None（无错误），不许被改成空串"


@pytest.mark.parametrize("bad_code", _BAD_ERROR_CODES)
def test_one_bad_error_code_no_longer_poisons_the_detail_endpoint(bad_code):
    """① 详情接口打**坏行自己** -> 200，`error_code` 逐字透出。

    对照：`test_the_detail_endpoint_still_serves_legal_rows` 证明打合法行也是 200。
    """
    _seed_schema()
    bad_id = _legal_id(7)
    _insert_raw_error_code(bad_id, "FAILED", bad_code, minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history/%s" % bad_id)

    print("\n[P3-error_code] 详情 error_code=%r -> %d | %s"
          % (bad_code, r.status_code, r.text[:220]))
    assert r.status_code == _EXPECT_DETAIL_BAD_ERROR_CODE
    assert r.json()["ok"] is True
    item = r.json()["data"]["item"]
    assert item["error_code"] == bad_code, (
        "详情里的坏码必须逐字透出：注入 %r，返回 %r" % (bad_code, item["error_code"]))
    assert item["status"] == "FAILED"



def test_the_detail_endpoint_still_serves_legal_rows():
    """① 对照：同一库里合法行仍然拿得到 200（载荷里 `error_code` 是 null）。

    这条防的是"把影响面说过头"：坏行毒的是"包含它的那一次请求"，
    不是"接口永久瘫痪"。
    """
    _seed_schema()
    bad_id, ok_id = _legal_id(7), _legal_id(8)
    _insert_raw_error_code(bad_id, "FAILED", "TIMEOUT_LEGACY", minutes_ago=0)
    _insert_raw_error_code(ok_id, "SUCCESS", None, minutes_ago=5)

    with _client() as client:
        r = client.get("/api/workflows/history/%s" % ok_id)

    print("\n[P3-error_code] 详情打合法行 -> %d | %s" % (r.status_code, r.text[:220]))
    assert r.status_code == 200
    assert r.json()["data"]["item"]["error_code"] is None


def test_null_error_code_rows_are_unaffected():
    """② 关键对比：`error_code IS NULL` 的行**完全不受影响**。

    这是 `ErrorCode` 与 `WorkflowStatus` 影响面差异的根源：

      - `status` 是 **NOT NULL 必填列**，每条记录都有值 -> 每条记录都可能变毒；
      - `error_code` 是**可空列**，只有失败的工作流才有值 -> 只有"失败且带非法码"的行才炸。

    而且产品路径写进去的 `error_code` 只可能是 `ErrorCode` 成员（见
    `test_normal_product_paths_cannot_write_an_invalid_error_code`），
    所以 `ErrorCode` 这条的实际暴露面比 `status` 那条**小得多**。
    """
    _seed_schema()
    for i in range(5):
        _insert_raw_error_code(_legal_id(i), "FAILED" if i % 2 else "SUCCESS", None,
                               minutes_ago=i)

    with _client() as client:
        r = client.get("/api/workflows/history")

    print("\n[P3-error_code] 5 条 error_code 全 NULL -> %d, total=%s"
          % (r.status_code, r.json().get("data", {}).get("total")))
    assert r.status_code == 200, "NULL 是合法取值（无错误），不得因此报错：%s" % r.text
    body = r.json()
    assert body["data"]["total"] == 5
    assert len(body["data"]["items"]) == 5
    assert all(item["error_code"] is None for item in body["data"]["items"])


def test_ten_legal_plus_one_bad_error_code_row_returns_nothing():
    """② 影响面量化：10 条合法 + 1 条非法 `error_code` -> **11 行全部返回**（修复前 0 行）。

    与 `status` 那条的量化数字一致（修复前都是"1 条毒死整页 -> 0 行"）；
    差别在**触发概率**：非法 `status` 让每条老记录都可能变毒，
    非法 `error_code` 只影响失败记录的一个子集（可空列）。
    """
    _seed_schema()
    for i in range(10):
        _insert_raw_error_code(_legal_id(i), "SUCCESS", None, minutes_ago=i + 1)
    _insert_raw_error_code(_legal_id(99), "FAILED", "TIMEOUT_LEGACY", minutes_ago=0)

    store = history.HistoryStore(_db_path())
    assert store.count() == 11, "库里确实是 11 行（10 合法 + 1 非法）"

    with _client() as client:
        r = client.get("/api/workflows/history")

    print("\n[P3-error_code] 库里 11 行（10 合法 + 1 非法）-> %d | %s"
          % (r.status_code, r.text[:200]))
    assert r.status_code == _EXPECT_LIST_BAD_ERROR_CODE

    items, total = _listing_health(r, 11)
    assert total == 11
    assert total == store.count(), "total 必须等于全库行数（含坏行）"
    legal_ids = {_legal_id(i) for i in range(10)}
    assert {it["workflow_id"] for it in items} == legal_ids | {_legal_id(99)}
    assert _item_of(r, _legal_id(99))["error_code"] == "TIMEOUT_LEGACY"


def test_error_code_rows_are_served_per_page_too():
    """② 补：修复后**每一页都正常**，且坏行落在哪页就在哪页原样出现。

    修复前：`limit=1&offset=0`（含坏行）500、默认 `limit=20` 500，
    换 offset 是唯一绕行方式（坏行最新时首页必坏）。
    现在没有"必坏的那一页"，`total` 在所有分页下都保持一致。
    """
    _seed_schema()
    for i in range(3):
        _insert_raw_error_code(_legal_id(i), "SUCCESS", None, minutes_ago=i + 1)
    _insert_raw_error_code(_legal_id(99), "FAILED", "bogus", minutes_ago=0)

    with _client() as client:
        page0 = client.get("/api/workflows/history?limit=1&offset=0")
        page1 = client.get("/api/workflows/history?limit=1&offset=1")
        default = client.get("/api/workflows/history")

    print("\n[P3-error_code] limit=1&offset=0（含坏行）-> %d" % page0.status_code)
    print("[P3-error_code] limit=1&offset=1（跳过坏行）-> %d" % page1.status_code)
    print("[P3-error_code] 默认 limit=20 -> %d" % default.status_code)
    assert page0.status_code == _EXPECT_LIST_BAD_ERROR_CODE
    assert page1.status_code == 200, "第二页仍能正常返回：%s" % page1.text
    assert default.status_code == _EXPECT_LIST_BAD_ERROR_CODE

    # 坏行最新 -> 第一页就是它，必须原样出现
    assert _item_of(page0, _legal_id(99))["error_code"] == "bogus"
    # 全量页 4 行一条不少；total 不随页变化（前端据此算总页数）
    assert len(_items(default)) == 4
    for r in (page0, page1, default):
        assert r.json()["data"]["total"] == 4, "total 不该随分页变化"


def test_a_row_bad_in_both_fields_blames_status_first():
    """③ 一行**同时**坏两个字段 -> 两个都降级，两个原样字符串都能看到。

    修复前这里只能看到 `"'RUNNING' is not a valid WorkflowStatus"`。
    这条用例同时盯住**两处构造**：只修 `:154` 则 `error_code` 仍硬转、只修 `:159`
    则 `status` 仍硬转，任一未修这条都会红。
    """
    _seed_schema()
    _insert_raw_error_code(_legal_id(1), "RUNNING", "TIMEOUT_LEGACY", minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history")

    print("\n[P3-error_code] 双坏字段 -> %d | %s" % (r.status_code, r.text[:220]))
    assert r.status_code == _EXPECT_LIST_BAD_ROW, (
        "双坏行必须整体可用；只修一处时这里必然 500"
    )

    item = _item_of(r, _legal_id(1))
    assert item["status"] == "RUNNING", "status 未降级：%r（:154 未修）" % item["status"]
    assert item["error_code"] == "TIMEOUT_LEGACY", (
        "error_code 未降级：%r（:159 未修）" % item["error_code"])


def test_a_row_bad_only_in_error_code_blames_error_code():
    """③ 隔离对照：`status` 合法、**只有** `error_code` 坏 -> 专盯 `:159`。

    与上一条合起来构成「逐处修复判别」：
      - 本条的坏处只在 `:159`，`:154` 修不修都不影响 -> 单独盯 `:159`；
      - 上一条双坏，任一处未修就红 -> 盯"两处都修"。
    """
    _seed_schema()
    _insert_raw_error_code(_legal_id(1), "FAILED", "TIMEOUT_LEGACY", minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history")

    print("\n[P3-error_code] 只有 error_code 坏 -> %d | %s" % (r.status_code, r.text[:220]))
    assert r.status_code == _EXPECT_LIST_BAD_ERROR_CODE

    item = _item_of(r, _legal_id(1))
    assert item["status"] == "FAILED", "合法 status 必须原样保留"
    assert item["error_code"] == "TIMEOUT_LEGACY", (
        "只有 error_code 坏时，这里单独暴露 :159 未修"
    )


def test_null_error_code_plus_bad_status_is_still_200_and_none():
    """② 补：**最常见的老数据形态** —— 非法 `status` + `error_code IS NULL`。

    现实里老库的绝大多数记录都是"没有错误"（`error_code` 为 NULL），
    所以枚举改名后最典型的毒行就是这一形态。修复后必须 200 且 `error_code is None`
    （不能被空串顶替、也不能因此丢掉 status 的原样值）。
    """
    _seed_schema()
    for i in range(4):
        _insert_raw(_legal_id(i), "RUNNING" if i % 2 else "SUCCESS", minutes_ago=10 - i)

    with _client() as client:
        r = client.get("/api/workflows/history?limit=100")

    print("\n[P3] 4 行（2 条坏 status，error_code 全 NULL）-> %d" % r.status_code)
    assert r.status_code == _EXPECT_LIST_BAD_ROW

    items, total = _listing_health(r, 4)
    assert total == 4
    assert all(it["error_code"] is None for it in items), "NULL 必须保持 None"
    statuses = [it["status"] for it in items]
    print("[P3] statuses -> %r" % statuses)
    assert statuses.count("RUNNING") == 2 and statuses.count("SUCCESS") == 2



def test_the_store_layer_reads_bad_error_codes_fine():
    """责任边界（同 `status` 那条）：`HistoryStore` 对非法 `error_code` 完全正常。

    500 只发生在 API 层，要修就修 `_item_from_record`，别动 `app/data/history.py`
    （它按设计只存字符串、不做枚举校验）。
    """
    _seed_schema()
    _insert_raw_error_code(_legal_id(1), "FAILED", "TIMEOUT_LEGACY", minutes_ago=0)

    store = history.HistoryStore(_db_path())
    rec = store.get(_legal_id(1))
    print("\n[P3-error_code] store.get() -> error_code=%r（原样读出，不抛）" % rec.error_code)
    assert rec is not None
    assert rec.error_code == "TIMEOUT_LEGACY"
    assert store.count() == 1
    assert len(store.list_recent(limit=10)) == 1


def test_normal_product_paths_cannot_write_an_invalid_error_code():
    """④ 前提：正常产品路径产不出非法 `error_code`（静态 + 运行时两层）。

    静态：全仓写历史库 `error_code` 列的只有 `app/core/archive.py` 的
    `build_record()`，表达式是
    `error_code=state.error.code.value if state.error is not None else None` ——
    `state.error` 是 `app/schemas.py:39` 的 `ApiError`，其 `code` 字段类型是
    **`ErrorCode`**（pydantic 保证），`.value` 取出来的必是枚举成员或 None。

    运行时：把 8 个 `ErrorCode` 成员逐个放进 `state.error` 调 `build_record()`，
    断言产出永远是合法值 —— 期望值写死，不写"要么合法要么非法"。
    """
    from app.schemas import ApiError, ErrorCode

    now = datetime.now(timezone.utc)
    legal_values = {m.value for m in ErrorCode}
    produced = []

    for member in ErrorCode:
        state = WorkflowState(
            workflow_id="a" * 32,
            dataset_id="b" * 32,
            question="正常路径探针",
            status=WorkflowStatus.FAILED,
            created_at=now,
            error=ApiError(code=member, message="探针"),
        )
        rec = archive.build_record(state)
        produced.append(rec.error_code)

    print("\n[P3-error_code] 8 个 ErrorCode 成员 -> build_record 产出 %r" % produced)
    assert len(produced) == len(list(ErrorCode)) == 8
    assert all(v in legal_values for v in produced), (
        "正常路径产出了非法 error_code：%r" % [v for v in produced if v not in legal_values]
    )

    # 无 error 时必须是 None（这正是"可空列"的来源）
    state_ok = WorkflowState(
        workflow_id="c" * 32, dataset_id="d" * 32, question="无错误探针",
        status=WorkflowStatus.SUCCESS, created_at=now, error=None,
    )
    print("[P3-error_code] state.error=None -> build_record.error_code=%r"
          % archive.build_record(state_ok).error_code)
    assert archive.build_record(state_ok).error_code is None


def test_a_renamed_error_code_still_lists_after_the_rename():
    """④ 补：`error_code` 的跨版本触发条件（与 status 那条同构）—— 修复后仍可读。

    把库里一个今天合法的码改成"老版本的名字"，接口**照常 200**
    并把那个字符串**原样透出**（修复前这里是 500、整页 0 行）。
    """
    _seed_schema()
    _insert_raw_error_code(_legal_id(0), "FAILED", "WORKFLOW_TIMEOUT", minutes_ago=5)

    with _client() as client:
        before = client.get("/api/workflows/history")
    print("\n[P3-error_code] 当前合法码 WORKFLOW_TIMEOUT -> %d" % before.status_code)
    assert before.status_code == 200, "这个码今天合法，应当 200：%s" % before.text

    conn = sqlite3.connect(_db_path())
    conn.execute("UPDATE workflow_history SET error_code='TIMEOUT_LEGACY'")
    conn.commit()
    conn.close()

    with _client() as client:
        after = client.get("/api/workflows/history")
    print("[P3-error_code] 改成老名字 TIMEOUT_LEGACY（老数据残留）-> %d | %s"
          % (after.status_code, after.text[:200]))
    assert after.status_code == _EXPECT_LIST_BAD_ERROR_CODE
    assert _item_of(after, _legal_id(0))["error_code"] == "TIMEOUT_LEGACY", (
        "老名字必须原样透出，不能变成 None / 空串 / 被丢掉"
    )


def test_the_enum_census_documents_exactly_two_hard_cast_sites():
    """④ 清单自证：硬转点**已被消除**，且没有以别处新增为代价。

    这条把清单里的静态结论钉死：`_item_from_record` 里**不再有**
    `WorkflowStatus(record.status)` / `ErrorCode(record.error_code)` 这种裸硬转
    （改成 `_coerce_enum(...)` 降级）。若以后有人把硬转写回来，这条会红。
    同时确认全仓只有这三个 Enum，`FileExt` / `ChartType` 仍是 `Literal`。
    """
    src = (PROJECT_ROOT / "app" / "api" / "routes_workflows.py").read_text(encoding="utf-8")

    import ast
    import io
    import tokenize

    # 只看**真实代码行**：注释与 docstring 里提到旧写法不算硬转点
    # （task-36 就踩过这个坑：注释/docstring 里的 `WorkflowStatus(record.status)` 曾被当成硬转点计数）
    # 用 tokenize 精确剔除 COMMENT 与 STRING 覆盖的行 —— 纯文本过滤做不到这件事。
    ignored_lines: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            ignored_lines.update(range(tok.start[0], tok.end[0] + 1))
    hard_casts = [
        ln.strip()
        for i, ln in enumerate(src.splitlines(), start=1)
        if ("WorkflowStatus(record" in ln or "ErrorCode(record" in ln)
        and i not in ignored_lines
    ]
    print("\n[census] routes_workflows.py 裸硬转点 -> %r" % hard_casts)
    assert hard_casts == [], (
        "硬转点又回来了（或清单没更新）：%r" % hard_casts
    )

    tree = ast.parse(src)
    call_names = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    print("[census] _coerce_enum 调用次数 -> %d" % call_names.count("_coerce_enum"))
    assert call_names.count("_coerce_enum") == 2, (
        "_coerce_enum 调用点数量变了（status / error_code 各一处）"
    )

    # FileExt / ChartType 是 Literal，不是 Enum —— 没有可调用构造器
    from app.schemas_dataset import FileExt
    from app.schemas_tool import ChartType

    for label, alias in (("FileExt", FileExt), ("ChartType", ChartType)):
        print("[census] %s -> %r（isinstance(type)=%s）" % (label, alias, isinstance(alias, type)))
        assert not isinstance(alias, type), (
            "%s 变成了真正的类，清单里「不是 Enum 硬转」的判据要重写" % label
        )

    # 全仓 Enum 仍然恰好 3 个（`class X(str, Enum)` —— `Enum` 是裸 Name，不是 Attribute）
    enum_classes = []
    for py in (PROJECT_ROOT / "app").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        enum_classes += [
            node.name
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.ClassDef)
            and any(
                (isinstance(b, ast.Name) and b.id == "Enum")
                or (isinstance(b, ast.Attribute) and b.attr == "Enum")
                for b in node.bases
            )
        ]
    print("[census] 全仓 Enum 类 -> %r" % sorted(enum_classes))
    assert sorted(enum_classes) == ["ErrorCode", "TaskStatus", "WorkflowStatus"], (
        "Enum 数量变了，清单要更新：%r" % sorted(enum_classes)
    )


# ==========================================================================
# 枚举硬转点清单（task-36 核心交付）
# ==========================================================================
#
# 判据（三条同时成立才算**真风险**）：
#   (1) 值来自**持久化**（库里读回来的字符串）或**用户输入**；
#   (2) 用 `Enum(...)` / `Literal` **强制构造**且**无兜底**（不带 try、不带默认值）；
#   (3) 非法值时**没有 pydantic 校验**先拦下来。
#
# | 文件:行                              | 表达式                                   | 来源字段                | 持久化/用户输入 | 会不会 500 | 现有测试覆盖 |
# |--------------------------------------|------------------------------------------|-------------------------|-----------------|-----------|--------------|
# | app/api/routes_workflows.py:154      | `WorkflowStatus(record.status)`          | 库 TEXT 列 status       | **持久化**      | **会**    | 是（task-35）|
# | app/api/routes_workflows.py:159      | `ErrorCode(record.error_code) if ...`    | 库 TEXT 列 error_code   | **持久化**      | **会**    | 是（本段）   |
# | app/api/routes_workflows.py:237      | `FinalReport.model_validate_json(...)`   | 库 TEXT 列 report_text  | **持久化**      | 见下⑤     | 否（未构造） |
# | app/schemas_workflow.py:110          | `chart_type: ChartType`（Literal 字段）  | 用户/LLM 计划参数       | 用户输入        | **不会**  | 是（pydantic）|
# | app/schemas_dataset.py:76            | `file_ext: FileExt`（Literal 字段）      | 上传文件扩展名          | 用户输入        | **不会**  | 是（pydantic）|
# | app/schemas.py:42                    | `code: ErrorCode`（Enum 字段）           | 各处构造 ApiError       | 内部            | **不会**  | 是（pydantic）|
# | app/schemas_workflow.py（state.status）| `status: WorkflowStatus`（Enum 字段）   | 内存状态对象            | 内部            | **不会**  | 是（pydantic）|
# | app/agents/orchestrator.py:227-277    | `task.status = TaskStatus.X`             | 内存 task 对象          | 内部常量        | **不会**  | 是           |
# | app/schemas_tool.py:52                | `ChartType = Literal[...]`（类型别名）   | 工具层图表类型          | 用户输入        | **不会**  | 是（pydantic）|
#
# **真风险 2 处**：`routes_workflows.py:154` 与 `:159`（同一函数，必须一起修）。
#
# **假警报（及判据）**：
#   - `TaskStatus` / `WorkflowStatus` / `ErrorCode` 作为**字段类型**出现在 pydantic
#     模型里（`schemas.py:42`、`schemas_workflow.py`、`schemas_tool.py`）——
#     非法值会被 pydantic 翻成 `ValidationError`（HTTP 422 或内部 500 之前就被拦），
#     不是"硬转"。
#   - `FileExt` / `ChartType` **不是 Enum**，是 `typing.Literal`，
#     `FileExt("csv")` 直接 `TypeError: Cannot instantiate typing.Literal` ——
#     它们**根本没有可调用的构造器**，不存在"枚举硬转"这种缺陷模式。
#   - 全仓只有 3 个 Enum：`ErrorCode` / `TaskStatus` / `WorkflowStatus`
#     （grep `class \w+\((str|int), ?Enum\)` 的结果）。
#   - `app/tools/validate.py:53` 的 `self.error_code = error_code` 是**普通 str 赋值**，
#     词汇是 `TOOL_NOT_FOUND` / `BAD_PARAMS` / `COLUMN_NOT_FOUND` / `BAD_OPERATION`
#     （与 `ErrorCode` **不相交**），且从不写进历史库的 `error_code` 列
#     （归档只取 `state.error.code.value`，而 `state.error` 是 `ApiError`）。
#
# 从清单看：**修一处不够，必须两处都修**（`154` + `159`）。


# ==========================================================================
# ⑤ 第三处同类风险：`routes_workflows.py:237` 的 `report_text` 解析
# ==========================================================================
#
# 这一处**曾是候选（未实证）**，task-37 已实证为**真缺陷**。与 154/159 的差别：
#   - 抛的不是 `ValueError`，是 pydantic 的 `ValidationError`；
#   - 只在**详情接口**上炸，列表接口不解析 `report_text`，完全不受影响；
#   - `report_text IS NULL` 的行被 `if record.report_text is not None` 短路，不受影响。
#
# **关键实测点（不许推断）**：`app/main.py` 的兜底 `@app.exception_handler(Exception)`
# 确实把 `ValidationError` 也翻成了 500 —— 已由**直接打接口**证明（task-37 时该路径
# 就是坏 `report_text` 的详情请求），不是靠"pydantic 异常一定被兜住"这种推理。
#
# ⚠️ task-38 修复 `:268` 之后，这条触发路径**消失了**（详情接口不再抛异常、返回 200）。
# 但"兜底 handler 不会把本机路径回给客户端"这个属性**仍然成立且仍然需要被钉住** ——
# 它现在由 `tests/test_errors.py` 的 `unexpected_exception_handler` 用例独立覆盖
# （用一条专门抛异常的路由，见该文件 `test_..._does_not_leak_...`）。
# 本文件保留的 `test_the_fallback_handler_still_guards_the_report_path` 改为断言
# **新契约**（200 + report 为 None + 服务端记了 warning），不再假装它还在测 500。

#: task-38 修复后：坏 `report_text` 的详情请求返回 200，`report` 降级为 `None`。
_EXPECT_DETAIL_BAD_REPORT = 200
_EXPECT_EMPTY_STRING_REPORT = 200

#: report_text 的坏值 → 期望在 detail 里出现的关键词（钉住是哪一类失败）。
_BAD_REPORTS = [
    ("not-json", "not json", "json_invalid"),
    ("title-not-a-string", '{"title": 123}', "string_type"),
    ("unknown-field-only", '{"unknown_field": 1}', "missing"),
]


def _insert_raw_report(workflow_id: str, report_text: str | None,
                       status: str = "SUCCESS", minutes_ago: int = 0) -> None:
    """原始 SQL 直插一条带 `report_text` 的行（`_insert_raw` 把该列写死 None）。"""
    fin = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "INSERT INTO workflow_history (workflow_id, question, status, created_at,"
        " finished_at, has_report, llm_used, report_text, error_code, schema_version,"
        " archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (workflow_id, "report_text 毒行探针", status, fin, fin, 1, 1, report_text, None, 1, fin),
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("label,payload,expect_token", _BAD_REPORTS,
                         ids=[c[0] for c in _BAD_REPORTS])
def test_a_bad_report_text_no_longer_breaks_the_detail_endpoint(label, payload,
                                                                expect_token):
    """⑤ 坏 `report_text` -> 详情接口 **200**，`report` 降级为 `None`（task-38 契约）。

    三种构造覆盖三类失败模式：JSON 语法错、字段类型错、必填字段缺失。
    真实场景：老版本写进去的报告 JSON 被新版本的 `FinalReport` 模型改得不兼容。

    ⚠️ `expect_token` 参数保留了 —— 它现在**不再**用来断言响应体（响应体里
    不会再有 pydantic 措辞），而是用来断言**服务端日志**里记了哪一类失败。
    见本用例末尾对 `caplog` 的断言：降级不等于吞错。
    """
    _seed_schema()
    wid = _legal_id(0xE000 + len(label))
    _insert_raw_report(wid, payload)

    with _client() as client:
        resp = client.get("/api/workflows/history/%s" % wid)

    body = resp.json()
    print("\n[P3-report %s] payload=%r -> HTTP %d" % (label, payload, resp.status_code))
    print("[P3-report %s] body = %s" % (label, resp.text[:300]))

    assert resp.status_code == _EXPECT_DETAIL_BAD_REPORT, (
        "坏 report_text 的行为又变了（修好了就该是 200）：%s" % resp.text[:300]
    )
    assert body["ok"] is True and body["error"] is None
    data = body["data"]
    # 记录本身**一条字段都不少**：坏报告不该让整条历史记录消失
    assert data["item"]["workflow_id"] == wid
    assert data["item"]["question"] == "report_text 毒行探针"
    assert data["item"]["status"] == "SUCCESS"
    assert data["item"]["has_report"] is True   # 库里写的是 1，如实回显，不顺着 report 被清掉
    assert data["item"]["llm_used"] is True
    assert data["item"]["created_at"] is not None
    # 报告降级成 None（而不是半截字符串）
    assert data["report"] is None
    # 关键：响应体里**不再**泄漏 pydantic 的内部措辞（这是 500 时代的产物）
    assert expect_token not in resp.text
    assert "ValidationError" not in resp.text
    assert "pydantic" not in resp.text.lower()


def test_a_bad_report_text_is_logged_not_swallowed(caplog):
    """⑤ 降级**不等于吞错**：解析失败必须留一条 warning 日志（含异常）。

    这是 `_parse_report` docstring 里"不吞错"那句的实现性断言。
    没有这条，`except ValidationError: return None` 与 `except Exception: pass`
    在测试上无法区分 —— 而后者会让跨版本不兼容彻底静默。
    """
    import logging as _logging

    _seed_schema()
    wid = _legal_id(0xE01F)
    _insert_raw_report(wid, "not json")

    with caplog.at_level(_logging.WARNING), _client() as client:
        resp = client.get("/api/workflows/history/%s" % wid)

    assert resp.status_code == 200
    texts = [r.getMessage() for r in caplog.records]
    print("\n[P3-report log] %r" % (texts,))
    assert any("归档报告无法解析" in t for t in texts), (
        "解析失败没有留 warning 日志，降级变成了静默吞错：%r" % (texts,)
    )


def test_an_empty_string_report_text_is_also_poison():
    """⑤ `report_text=''`（空串，非 NULL）也是坏数据，但**不再 500**。

    `''` 不是 None，所以 `if record.report_text is not None` 放行，
    交给 `model_validate_json('')` -> `json_invalid` -> 被 `_parse_report` 降级成 None。

    task-38 前：500。task-38 后：200 + `report is None`（记录本身照常返回）。
    """
    _seed_schema()
    wid = _legal_id(0xE00F)
    _insert_raw_report(wid, "")

    with _client() as client:
        resp = client.get("/api/workflows/history/%s" % wid)
    print("\n[P3-report empty] HTTP %d | %s" % (resp.status_code, resp.text[:200]))
    assert resp.status_code == _EXPECT_EMPTY_STRING_REPORT, resp.text[:300]
    # 空串**不是** NULL：记录里的 has_report 是库里写的 1，如实回显；
    # 但报告解析不出来 -> None。两者不同，正是"降级但不撒谎"的含义。
    data = resp.json()["data"]
    assert data["item"]["has_report"] is True
    assert data["item"]["status"] == "SUCCESS"
    assert data["report"] is None


def test_the_listing_is_unaffected_by_a_bad_report_text():
    """⑤ 对照组 1：列表与详情的**行为差**（task-38 之后仍要留着这条）。

    task-38 前：列表 200（不解析 `report_text`）、详情 500 —— 差在"解析"这一步。
    task-38 后：两者都 200，但**差别依然存在且依然可断言**：
      - 列表**根本不回** `report_text`（坏内容不外泄），也不需要解析；
      - 详情**解析**它并降级成 `report=None`。
    所以这条用例现在钉的是"列表不因坏报告受损"，而不是"详情会炸"。

    ⚠️ 反过来也成立：`report_text` 坏不影响列表，是 `:268` 与 `:181/:187`
    爆炸半径不同的原因（那两处一炸就是整页 0 行）。
    """
    _seed_schema()
    wid = _legal_id(0xE010)
    _insert_raw_report(wid, "not json")

    with _client() as client:
        listing = client.get("/api/workflows/history")
        detail = client.get("/api/workflows/history/%s" % wid)

    print("\n[P3-report 对照] 列表 HTTP %d | 详情 HTTP %d"
          % (listing.status_code, detail.status_code))
    assert listing.status_code == 200, listing.text[:200]
    payload = listing.json()["data"]
    assert payload["total"] == 1
    assert [it["workflow_id"] for it in payload["items"]] == [wid]
    # 列表根本不回 report_text，所以坏内容不外泄
    assert "report_text" not in payload["items"][0]
    # 列表里那条记录本身完全正常（status 合法、没被坏报告牵连）
    assert payload["items"][0]["status"] == "SUCCESS"
    assert payload["items"][0]["has_report"] is True
    # 详情：解析失败 -> 降级 None，但记录照常返回
    assert detail.status_code == _EXPECT_DETAIL_BAD_REPORT, detail.text[:300]
    assert detail.json()["data"]["item"]["workflow_id"] == wid
    assert detail.json()["data"]["report"] is None


def test_a_bad_report_text_row_does_not_poison_its_page():
    """⑤ 最强对照：坏 `report_text` 行与好行**混在同一页**时，整页都 200 且一条不少。

    这是 task-37 那条"混页"断言的**正向版本**（Lead 明确要求保留、不许删）。
    它同时钉住两件事：
      1. 坏报告**不会**牵连同页的其他记录（区别于 `status`/`error_code` 的整页 0 行）；
      2. `total` 仍是 `store.count()`（全库行数，含坏行）。
    """
    _seed_schema()
    bad = _legal_id(0xE020)
    good1 = _legal_id(0xE021)
    good2 = _legal_id(0xE022)
    _insert_raw_report(bad, "not json", minutes_ago=2)
    _insert_raw_report(good1, None, minutes_ago=1)          # NULL = 无报告（主流路径）
    _insert_raw_report(good2, '{"unknown_field": 1}', minutes_ago=0)

    with _client() as client:
        listing = client.get("/api/workflows/history")
        bad_detail = client.get("/api/workflows/history/%s" % bad)

    assert listing.status_code == 200, listing.text[:300]
    payload = listing.json()["data"]
    print("\n[P3-report 混页] 列表 HTTP %d | total=%d items=%d"
          % (listing.status_code, payload["total"], len(payload["items"])))
    # N+1 一条不少（这里 3 条里有 2 条坏报告 + 1 条 NULL）
    assert payload["total"] == 3
    assert len(payload["items"]) == 3
    assert {it["workflow_id"] for it in payload["items"]} == {bad, good1, good2}
    # 详情打坏行也是 200
    assert bad_detail.status_code == 200, bad_detail.text[:300]
    assert bad_detail.json()["data"]["report"] is None


def test_a_null_report_text_row_is_unaffected():
    """⑤ 对照组 2：`report_text IS NULL` 的行不受影响（被 `is not None` 短路）。

    正常产品路径里"没有报告"就是 NULL（`app/core/archive.py` 的
    `report_text=report.model_dump_json() if report is not None else None`），
    所以这条是**主流路径**，必须正常。
    """
    _seed_schema()
    wid = _legal_id(0xE011)
    _insert_raw_report(wid, None, status="FAILED")

    with _client() as client:
        listing = client.get("/api/workflows/history")
        detail = client.get("/api/workflows/history/%s" % wid)

    print("\n[P3-report NULL] 列表 HTTP %d | 详情 HTTP %d | %s"
          % (listing.status_code, detail.status_code, detail.text[:200]))
    assert listing.status_code == 200
    assert detail.status_code == 200
    assert detail.json()["data"]["item"]["has_report"] is True  # 库里写的就是 1，如实回显
    assert detail.json()["data"]["report"] is None


def test_a_healthy_report_still_round_trips_through_the_detail_endpoint():
    """⑤ 阳性对照：合法 `report_text` -> 200 且字段还原（证明探针不是永远 500）。"""
    from app.schemas_workflow import FinalReport, ReportSection

    _seed_schema()
    wid = _legal_id(0xE012)
    report = FinalReport(
        report_id=wid,
        title="阳性对照报告",
        summary="摘要",
        sections=[ReportSection(heading="第一节", text="正文")],
        generated_at=datetime.now(timezone.utc),
        llm_used=True,
    )
    _insert_raw_report(wid, report.model_dump_json())

    with _client() as client:
        detail = client.get("/api/workflows/history/%s" % wid)
    print("\n[P3-report 阳性] HTTP %d | %s" % (detail.status_code, detail.text[:200]))
    assert detail.status_code == 200
    assert detail.json()["data"]["report"]["title"] == "阳性对照报告"


def test_the_store_layer_reads_a_bad_report_text_fine():
    """⑤ `:237` 的锅在 API 层：`HistoryStore.get()` 对坏 `report_text` 原样读出，不抛。

    与 `154`/`159` 的取证方式一致 —— 数据层不背锅。
    """
    _seed_schema()
    wid = _legal_id(0xE013)
    _insert_raw_report(wid, "not json")

    store = history.HistoryStore(_db_path())
    rec = store.get(wid)
    print("\n[P3-report store] rec.report_text=%r" % (rec.report_text,))
    assert rec is not None
    assert rec.report_text == "not json"
    assert store.count() == 1


def test_the_report_path_no_longer_reaches_the_fallback_handler():
    """⑤ task-38 之后：坏报告的详情请求**不再**把 `ValidationError` 送进兜底 handler。

    这条用例的前身是 `test_fallback_handler_leaks_no_paths`（task-37）。它当年做两件事：
      (1) 实测 `app/main.py` 的 `@app.exception_handler(Exception)` **确实接住** pydantic
          `ValidationError`（不是 `ValueError` 子类，所以"接不接"必须打接口看，不许推断）；
      (2) 断言 500 响应体里不泄漏本机路径。

    ⚠️ task-38 修掉 `:268` 之后，这条触发路径**消失了** —— 详情接口不再抛异常。
    所以 (1) 已经**无处可测**（不是"改成 200 就完事"），(2) 也不能再靠这条路测。

    **处理方式与理由（这段是给后来者的判断留痕）**：
      - (2) "不泄漏路径"这个属性仍然成立、仍然重要，但它现在由
        `tests/test_errors.py` 的 `unexpected_exception_handler` 用例**独立覆盖**
        （该文件用一条专门抛 `RuntimeError` 的动态路由打真实 500，并断言
        `"D:" not in detail` / `"个人项目01" not in detail`）。所以本文件**不需要**
        再替它兜底测试 —— 保留一条已经打不到 500 的断言只会变成假证据。
      - 本用例保留的**唯一**价值是钉住"这条路径已不再 500"这个**新契约**，
        并显式断言响应体里没有任何 500 时代的产物。它**不假装**还在测兜底 handler。

    **如果将来又有人把 `_parse_report` 的 except 去掉**，本用例会红（见探针 M5），
    这正是它现在的意义 —— 它是回归哨兵，不是泄漏检查。
    """
    _seed_schema()
    wid = _legal_id(0xE014)
    _insert_raw_report(wid, '{"unknown_field": 1}')

    with _client() as client:
        resp = client.get("/api/workflows/history/%s" % wid)

    raw = resp.text
    print("\n[P3-fallback] HTTP %d | body=%s" % (resp.status_code, raw[:200]))

    # 新契约：正常返回，不是 500
    assert resp.status_code == _EXPECT_DETAIL_BAD_REPORT, raw[:300]
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["data"]["report"] is None

    # 500 时代的产物一个都不许出现（这部分原先是"泄漏扫描"的正面版本）
    for needle in ("INTERNAL_ERROR", "服务器内部错误", "ValidationError",
                   "Traceback", 'File "', "D:\\", "ai-data-analyst-agent",
                   "\\Users\\", "AppData"):
        hit = raw.count(needle)
        print("[P3-fallback] 残留扫描 %-22s 命中 %d" % (needle, hit))
        assert hit == 0, "响应体里出现了 500 时代的产物 %r：%s" % (needle, raw[:300])

    leaks = {
        "本机盘符 D:\\": "D:\\",
        "仓库目录名": "ai-data-analyst-agent",
        "用户名段 \\Users\\": "\\Users\\",
        "LOCALAPPDATA 片段": "AppData",
        "Traceback 字样": "Traceback",
        "堆栈 文件行号 字样": 'File "',
    }
    for label, needle in leaks.items():
        hit = raw.count(needle)
        print("[P3-fallback] 残留扫描 %-18s 命中 %d" % (label, hit))
        assert hit == 0, "响应体里出现了 %s：%s" % (label, raw[:300])


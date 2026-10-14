"""P3 取证：历史库里出现**枚举外的 status** 时，两个历史接口会整页 500。

这是**记录在案的缺陷**（task-35），不是期望行为。本文件的作用是把当前行为钉住，
让任何人改动 `_item_from_record` / `list_history` 时立刻看见自己改动了什么。

缺陷
----
`app/api/routes_workflows.py:154` 的 `_item_from_record()` 用
`WorkflowStatus(record.status)` **硬转**库里的 TEXT 列。`WorkflowStatus` 是个
`str` Enum，`WorkflowStatus("RUNNING")` 直接抛
`ValueError: 'RUNNING' is not a valid WorkflowStatus`。

`app/api/routes_workflows.py:198` 是
`items=[_item_from_record(record) for record in records]`
—— **列表推导里没有任何逐行保护**，所以一条坏行会让整个响应构造失败，
被 `app/main.py` 的兜底 handler 翻成 `500 INTERNAL_ERROR`。
`app/api/routes_workflows.py:243` 的详情接口复用同一个 `_item_from_record`，同样 500。

**修复后本文件应当这样改**：把下面 `_EXPECT_LIST_BAD_ROW` / `_EXPECT_DETAIL_BAD_ROW`
从 `500` 改成 `200`，并断言坏行被**跳过**（列表只返回合法行、`total` 仍如实报告），
而不是既断言 500 又断言 200 —— 那种"两面都对"的断言是无效探针。

范围与前提（为什么这是低危缺陷）
--------------------------------
**正常产品路径产不出非法 status**，这已由静态与运行时两层证据确认（见
`test_normal_product_paths_cannot_write_an_invalid_status`）：
`app/agents/orchestrator.py` 只有三个 `state.status` 写入点
（`:432` / `:441` / `:455`），全部把 `WorkflowStatus` 枚举成员赋进去；
`app/api/routes_workflows.py:429` 与 `app/core/background.py:69` 同样只写
`WorkflowStatus.FAILED`。所以毒行只能来自**跨版本持久化**：
将来 `WorkflowStatus` 改名或删值，老库里的字符串就成了新代码不认识的值
（`test_a_renamed_enum_value_poisons_the_listing` 反向演示了这一点）。

本文件只用 `tmp_path`，绝不碰仓库的 `data/db/`。
"""

from __future__ import annotations

import sqlite3
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

#: 期望值写死（D-16 第 3 条）：修好后这两个常量应当改成 200。
_EXPECT_LIST_BAD_ROW = 500
_EXPECT_DETAIL_BAD_ROW = 500

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


# ==========================================================================
# ① 单条坏行：整页 500（原始状态码 + 原始响应体）
# ==========================================================================


def test_one_bad_row_poisons_the_whole_listing():
    """① 1 条合法 + 1 条非法 -> `GET /api/workflows/history` 是 **500**，不是跳过坏行。

    这是记录在案的缺陷：合法的那个 1 行本来完全应该被返回。
    修复后本用例应改成断言 200 + 只返回合法行。
    """
    _seed_schema()
    _insert_raw(_legal_id(0), "SUCCESS", minutes_ago=5)
    _insert_raw(_BAD_ID, "RUNNING", minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history?limit=100")

    print("\n[P3] GET /api/workflows/history -> %d" % r.status_code)
    print("[P3] 响应体 -> %s" % r.text)
    assert r.status_code == _EXPECT_LIST_BAD_ROW, (
        "行为变了！现在是 %d：%s\n"
        "如果是 200，说明缺陷被修好了 —— 请把 _EXPECT_LIST_BAD_ROW 改成 200 并改为"
        "断言坏行被跳过（而不是把断言写成'要么 500 要么 200'）。" % (r.status_code, r.text))

    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INTERNAL_ERROR"
    # 原始异常信息被 sanitize 后透出，证明就是枚举硬转失败（而不是别的 500）
    assert "is not a valid WorkflowStatus" in body["error"]["detail"], body["error"]
    assert "'RUNNING'" in body["error"]["detail"], body["error"]

    # 关键：库里 2 行**都在**，合法的 1 行也在 —— 所以"500"不是"行没了"，是真被毒死
    conn = sqlite3.connect(_db_path())
    total = conn.execute("SELECT COUNT(*) FROM workflow_history").fetchone()[0]
    legal = conn.execute(
        "SELECT COUNT(*) FROM workflow_history WHERE status='SUCCESS'").fetchone()[0]
    conn.close()
    assert total == 2 and legal == 1, "库里有 %d 行（合法 %d 行）" % (total, legal)


def test_a_bad_row_poisons_the_page_that_contains_it():
    """① 补：毒杀是**按页**发生的 —— 坏行不在这一页时列表正常。

    这条把缺陷边界钉死：不是"库一坏就全坏"，而是"包含坏行的那一页必坏"。
    分页参数化下仍然不可救（坏行会出现在某些页里），所以影响面依然是"列表不可用"。
    """
    _seed_schema()
    _insert_raw(_legal_id(0), "SUCCESS", minutes_ago=5)
    _insert_raw(_BAD_ID, "RUNNING", minutes_ago=0)  # 最新，排第一

    with _client() as client:
        # limit=1 只取最新 1 条 = 坏行 -> 500
        only_bad = client.get("/api/workflows/history?limit=1&offset=0")
        # limit=1 跳过坏行 -> 200
        only_good = client.get("/api/workflows/history?limit=1&offset=1")
        # 全量 -> 500
        all_rows = client.get("/api/workflows/history?limit=100")

    print("\n[P3] limit=1&offset=0（坏行）-> %d" % only_bad.status_code)
    print("[P3] limit=1&offset=1（跳过坏行）-> %d" % only_good.status_code)
    print("[P3] limit=100（全量）-> %d" % all_rows.status_code)

    assert only_bad.status_code == _EXPECT_LIST_BAD_ROW
    assert all_rows.status_code == _EXPECT_LIST_BAD_ROW
    assert only_good.status_code == 200, (
        "跳过坏行的分页应当正常，实际 %d：%s" % (only_good.status_code, only_good.text))
    assert only_good.json()["data"]["items"][0]["workflow_id"] == _legal_id(0)


# ==========================================================================
# ② 影响面量化：10 条合法 + 1 条非法
# ==========================================================================


def test_ten_legal_rows_plus_one_bad_row_still_500():
    """② 10 条合法 + 1 条非法 -> 仍然 500，**10 条合法行一个都拿不到**。

    关键数字：坏行:合法行 = 1:10 时，接口返回的行数是 **0**，而不是 10。
    """
    _seed_schema()
    for i in range(10):
        _insert_raw(_legal_id(i), "SUCCESS", minutes_ago=20 - i)
    _insert_raw(_BAD_ID, "RUNNING", minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history?limit=100")

    print("\n[P3] 库里 11 行（10 合法 + 1 非法）-> GET 状态码 %d" % r.status_code)
    print("[P3] 响应体 -> %s" % r.text)
    print("[P3] 用户实际能拿到的行数 -> 0（期望本应是 10）")
    assert r.status_code == _EXPECT_LIST_BAD_ROW
    assert r.json()["data"] is None, "500 时 data 是 null，所以一条都拿不到"

    # 10 条合法行确实在库里
    conn = sqlite3.connect(_db_path())
    legal = conn.execute(
        "SELECT COUNT(*) FROM workflow_history WHERE status='SUCCESS'").fetchone()[0]
    conn.close()
    assert legal == 10, "库里合法行数 %d" % legal


# ==========================================================================
# ③ 大小写与乱值都不是被静默容错
# ==========================================================================


@pytest.mark.parametrize("bad_status", ["Success", "success", "bogus", "RUNNING",
                                        "SUCCESS_WITH_WARNING", " SUCCESS", ""])
def test_miscased_and_bogus_statuses_are_all_500(bad_status):
    """③ `"Success"`（大小写）/ `"bogus"`（乱值）/ 空串 / 带空格 —— 全部 500，无静默容错。

    注意 `""` 与 `" SUCCESS"` 也 500：说明没有做 strip / 忽略大小写的宽容匹配。
    """
    _seed_schema()
    _insert_raw(_legal_id(0), "SUCCESS", minutes_ago=5)
    _insert_raw(_BAD_ID, bad_status, minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history?limit=100")

    print("\n[P3] status=%-24r -> %d | %s"
          % (bad_status, r.status_code, r.text[:150]))
    assert r.status_code == _EXPECT_LIST_BAD_ROW, (
        "status=%r 拿到了 %d，与其它非法值不一致" % (bad_status, r.status_code))
    assert "is not a valid WorkflowStatus" in r.json()["error"]["detail"]


# ==========================================================================
# ⑤ 详情接口是否同样炸
# ==========================================================================


def test_the_detail_endpoint_explodes_on_the_same_bad_row():
    """⑤ 详情接口对**坏行**同样 500（它复用同一个 `_item_from_record`）。

    对照组：同一个接口对**合法行**是 200 —— 证明炸的是坏行本身，不是接口整体坏了。
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
    assert "is not a valid WorkflowStatus" in bad.json()["error"]["detail"]
    assert good.status_code == 200, "合法行详情应当 200，实际 %d" % good.status_code
    assert good.json()["data"]["item"]["status"] == "SUCCESS"


def test_the_bad_row_is_invisible_to_the_detail_endpoint():
    """⑤ 补：坏行 500 意味着**它自己永远看不回来** —— 用户既看不到历史列表，也打不开它。"""
    _seed_schema()
    _insert_raw(_BAD_ID, "RUNNING", minutes_ago=0)

    # 库层：这行确实存在（get() 不经过枚举转换，读得出来）
    assert history.HistoryStore(_db_path()).get(_BAD_ID) is not None

    with _client() as client:
        assert client.get("/api/workflows/history").status_code == _EXPECT_LIST_BAD_ROW
        assert client.get("/api/workflows/history/%s" % _BAD_ID).status_code \
            == _EXPECT_DETAIL_BAD_ROW


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


def test_a_renamed_enum_value_poisons_the_listing():
    """④ 补：**跨版本**是毒行的唯一来源 —— 反向演示枚举改名后的后果。

    把库里一个今天合法的值改成"新版才有的名字"，接口立刻 500。
    这就是 P3 的现实触发条件：将来 `WorkflowStatus` 改名/删值，
    老库里的字符串就成了新代码不认识的值。
    """
    _seed_schema()
    _insert_raw(_legal_id(0), "SUCCESS_WITH_WARNINGS", minutes_ago=5)

    with _client() as client:
        before = client.get("/api/workflows/history")
    print("\n[P3] 存当前枚举认识的值 SUCCESS_WITH_WARNINGS -> %d" % before.status_code)
    assert before.status_code == 200, "这个值今天合法，应当 200：%s" % before.text

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
    assert "'PARTIAL'" in after.json()["error"]["detail"]


# ==========================================================================
# 数据层不背锅：坏行在 HistoryStore 层是**读得出来**的
# ==========================================================================


def test_the_store_layer_reads_bad_rows_fine():
    """数据层对非法 status 是宽容的（TEXT 列，不转枚举）—— 500 只发生在 API 层。

    这条划清责任边界：要修就修 `_item_from_record`（API 层），
    不要动 `app/data/history.py`（它按设计只存字符串）。
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
        assert client.get("/api/workflows/history").status_code == _EXPECT_LIST_BAD_ROW


# ==========================================================================
# ============ task-36：ErrorCode 同类硬转 + 枚举硬转点清单 ============
# ==========================================================================
#
# task-35 只证明了 `status` 那一处（`routes_workflows.py:154`）。
# 同一个函数里**还有一处同款硬转**（`:159`）：
#
#     error_code=ErrorCode(record.error_code) if record.error_code is not None else None
#
# 本段补上它的实证，并给出「枚举硬转点清单」（见文件末尾的 docstring）。
#
# 修复后本段应当这样改：`_EXPECT_LIST_BAD_ERROR_CODE` / `_EXPECT_DETAIL_BAD_ERROR_CODE`
# 从 500 改成 200，并断言坏行被**跳过**（`error_code` 落成 None 或原始字符串），
# 而不是既断言 500 又断言 200。


#: 期望值写死（D-16 第 3 条）：修好后这两个常量应当改成 200。
_EXPECT_LIST_BAD_ERROR_CODE = 500
_EXPECT_DETAIL_BAD_ERROR_CODE = 500

#: 三种代表性非法 error_code：
#:   - `TIMEOUT_LEGACY` 模拟**老版本的名字**（跨版本持久化，唯一现实触发条件）
#:   - `bogus` 纯乱值
#:   - `""` 空串（"有值但无意义"，与 NULL 语义最接近、最容易被误当可容错）
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
def test_one_bad_error_code_poisons_the_whole_listing(bad_code):
    """① 非法 `error_code`（3 种）-> 列表 **500**，`data=null`，一条都拿不到。

    与 `status` 那条的机制完全相同：`ErrorCode(record.error_code)` 硬转抛
    `ValueError: '<值>' is not a valid ErrorCode`，被兜底 handler 翻成 500。

    注意"1 条合法 + 1 条非法"的构造：合法行的 `error_code` 是 **NULL**，
    它本身不炸（见 `test_null_error_code_rows_are_unaffected`），
    所以这里的 500 完全由那条非法行造成。
    """
    _seed_schema()
    _insert_raw_error_code(_legal_id(1), "FAILED", bad_code, minutes_ago=0)
    _insert_raw_error_code(_legal_id(2), "SUCCESS", None, minutes_ago=5)

    with _client() as client:
        r = client.get("/api/workflows/history")

    print("\n[P3-error_code] error_code=%r -> %d | %s"
          % (bad_code, r.status_code, r.text[:220]))
    assert r.status_code == _EXPECT_LIST_BAD_ERROR_CODE
    body = r.json()
    assert body["ok"] is False
    assert body["data"] is None, "整页挂掉：一条都拿不到（这正是缺陷的核心）"
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "is not a valid ErrorCode" in body["error"]["detail"]
    assert repr(bad_code) in body["error"]["detail"]


@pytest.mark.parametrize("bad_code", _BAD_ERROR_CODES)
def test_one_bad_error_code_poisons_the_detail_endpoint(bad_code):
    """① 详情接口打**坏行自己** -> 也 500（复用同一个 `_item_from_record`）。

    对照：`test_the_detail_endpoint_still_serves_legal_rows` 证明打合法行是 200 ——
    责任边界是"坏行自己"，不是"详情接口整体不可用"。
    """
    _seed_schema()
    bad_id = _legal_id(7)
    _insert_raw_error_code(bad_id, "FAILED", bad_code, minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history/%s" % bad_id)

    print("\n[P3-error_code] 详情 error_code=%r -> %d | %s"
          % (bad_code, r.status_code, r.text[:220]))
    assert r.status_code == _EXPECT_DETAIL_BAD_ERROR_CODE
    assert "is not a valid ErrorCode" in r.json()["error"]["detail"]


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
    """② 影响面量化：10 条合法 + 1 条非法 `error_code` -> 用户拿到 **0** 行。

    与 `status` 那条的量化数字一致（都是"1 条毒死整页"）；
    差别在**触发概率**：非法 `status` 让每条老记录都可能变毒，
    非法 `error_code` 只影响失败记录的一个子集。
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
    assert r.json()["data"] is None, "用户拿到 0 行（本应拿到 10 行合法记录）"


def test_error_code_poison_is_per_page_too():
    """② 补：毒杀**按页**发生（与 `status` 那条一致）。

    `limit=1&offset=0`（含坏行）500；`limit=1&offset=1`（跳过坏行）200。
    但默认 `limit=20` 且坏行落在页内时，**首页必坏** —— 换 offset 不是可用的绕行方案，
    因为坏行是"最新的失败记录"时它天然就在第一页。
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
    assert page1.status_code == 200, "跳过坏行的那页仍能正常返回：%s" % page1.text
    assert default.status_code == _EXPECT_LIST_BAD_ERROR_CODE


def test_a_row_bad_in_both_fields_blames_status_first():
    """③ 一行同时坏两个字段 -> **先炸 `status`**（`detail` 原文是 status 的）。

    `_item_from_record` 是关键字实参构造，按**字段声明顺序**求值：
    `HistoryItem` 先声明 `status`（`:154`）后声明 `error_code`（`:159`），
    所以 `WorkflowStatus(...)` 先抛，`ErrorCode(...)` 根本轮不到。

    **这条决定修复要覆盖两处**：只修 `:159` 是假修复 ——
    同一行的 `status` 仍然会把请求打成 500，只是换了个 `detail` 文案。
    """
    _seed_schema()
    _insert_raw_error_code(_legal_id(1), "RUNNING", "TIMEOUT_LEGACY", minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history")

    detail = r.json()["error"]["detail"]
    print("\n[P3-error_code] 双坏字段 -> %d | detail=%r" % (r.status_code, detail))
    assert r.status_code == _EXPECT_LIST_BAD_ROW
    assert detail == "'RUNNING' is not a valid WorkflowStatus", (
        "先炸 status（声明顺序在前）；若这条挂了说明求值顺序变了，"
        "修复覆盖范围要重新评估"
    )


def test_a_row_bad_only_in_error_code_blames_error_code():
    """③ 隔离对照：`status` 合法、只有 `error_code` 坏 -> `detail` 是 error_code 的。

    与上一条合起来证明"两处都会炸、谁先取决于 status 是否合法"，
    因此修复必须**两处都覆盖**。
    """
    _seed_schema()
    _insert_raw_error_code(_legal_id(1), "FAILED", "TIMEOUT_LEGACY", minutes_ago=0)

    with _client() as client:
        r = client.get("/api/workflows/history")

    detail = r.json()["error"]["detail"]
    print("\n[P3-error_code] 只有 error_code 坏 -> %d | detail=%r" % (r.status_code, detail))
    assert r.status_code == _EXPECT_LIST_BAD_ERROR_CODE
    assert detail == "'TIMEOUT_LEGACY' is not a valid ErrorCode"


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


def test_a_renamed_error_code_poisons_the_listing():
    """④ 补：`error_code` 的跨版本触发条件（与 status 那条同构）。

    把库里一个今天合法的码改成"新版才有的名字"，接口立刻 500。
    现实中这会发生：`ErrorCode` 改名/删值后，老库里的失败记录就成了毒行。
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
    assert "'TIMEOUT_LEGACY'" in after.json()["error"]["detail"]


def test_the_enum_census_documents_exactly_two_hard_cast_sites():
    """④ 清单自证：全仓 `Enum(...)` 硬转点**只有两处**，都在 `_item_from_record`。

    这条把清单里的静态结论钉死，防止以后新增硬转点却没人更新清单。
    与清单一起看：`FileExt` / `ChartType` 是 `Literal`（无构造器），
    `TaskStatus` 只作 pydantic 字段类型出现 —— 都不属于这一模式。
    """
    src = (PROJECT_ROOT / "app" / "api" / "routes_workflows.py").read_text(encoding="utf-8")
    hard_casts = [
        ln.strip() for ln in src.splitlines()
        if ("WorkflowStatus(record" in ln or "ErrorCode(record" in ln)
    ]
    print("\n[census] routes_workflows.py 硬转点 -> %r" % hard_casts)
    assert len(hard_casts) == 2, "硬转点数量变了，清单要更新：%r" % hard_casts

    # FileExt / ChartType 是 Literal，不是 Enum —— 没有可调用构造器
    from app.schemas_dataset import FileExt
    from app.schemas_tool import ChartType

    for label, alias in (("FileExt", FileExt), ("ChartType", ChartType)):
        print("[census] %s -> %r（isinstance(type)=%s）" % (label, alias, isinstance(alias, type)))
        assert not isinstance(alias, type), (
            "%s 变成了真正的类，清单里「不是 Enum 硬转」的判据要重写" % label
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


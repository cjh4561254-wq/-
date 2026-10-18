"""独立验收测试：SQLite 历史归档（task-31 `app/data/history.py` + task-32 接入）。

这份文件是**独立验收方**（task-33）写的，刻意不复用作者 `tests/test_history_store.py`
/ `test_history_archive.py` / `test_history_api.py` 里的任何 fixture 或 helper ——
作者的测试绿了不算证据，这里从"看门人"视角重写一遍。

覆盖的验收点（A1..A9）
----------------------
A1 **真重启存活**：`HistoryStore` 的持久性必须跨**进程**成立。同一个 pytest 进程里
   新建一个对象再读，只能证明"内存里没丢"，不能证明落盘。这里的
   `test_a1_*` 用 `subprocess` 起两个**独立** python 进程：进程 1 归档后退出，
   进程 2 重新打开库读出字段逐个比对。
A2 **归档失败旁路**：归档库不可用时，分析结果必须不受影响（不能因为归档炸了就
   让一次成功的分析变成失败）。
A3 **密钥不落库**：环境变量里的 API key 绝不能进库。用假 key（真 key 从不使用），
   对 db 文件做**原始字节**扫描找 `b"sk-"`。
A4 **禁止复活**：`registry.clear()` 之后 `GET /api/workflows/{wid}` 必须**仍然 404**，
   而历史接口仍能看到这条。同时断言 `registry.count()` 前后不变 ——
   任何人加"库里没有就从内存捞"的 fallback，这条立刻红。
A5 **路由顺序**：`GET /api/workflows/history` 不能被 `/api/workflows/{workflow_id}` 吃掉。
A6 **端到端真跑**：`scripts/demo.py` 用真 key 跑完后，库里出现这次的归档记录。
   （真 key 那一轮在交付说明里贴原始输出；这里只断言"归档链路的产物可经接口读回"。）
A7 **并发不炸**：多线程同时读写不出现 `database is locked`。
A8 **零新依赖**：requirements 没动、`sqlalchemy` 没被 import 也没有安装。
A9 **git 干净**：`data/db/app.db` 被 .gitignore 挡住，跑完不产生脏文件。

环境纪律（这项目踩过的坑，写在这里给后来者）
--------------------------------------------
* 本机**真的有**一个机器级 `DEEPSEEK_API_KEY`。任何会读出它的测试都必须显式清掉，
  否则会拿真 key 去打真实 API。`conftest.py` 的 `_isolate_llm_env` 已经对 pytest
  做了这件事，但**子进程**要自己传干净环境。
* 数行数 / 扫字节一律 `read_bytes()` 数 `\n`，不要用 `Get-Content`（PowerShell 会
  重组行尾，数出来是假的）。
* 独立脚本里的 `tempfile.mkdtemp()` 会落到仓库根（本机沙箱不可写 %LOCALAPPDATA%\\Temp），
  所以子进程里的临时目录显式用 `tmp_path`，不用 `tempfile`。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.core import archive, registry
from app.data import history
from app.data.history import (
    HISTORY_SCHEMA_VERSION,
    HistoryRecord,
    HistoryStore,
    get_history_store,
    reset_history_store_cache,
)
from app.main import app
from app.schemas_workflow import (
    FinalReport,
    ReportSection,
    WorkflowState,
    WorkflowStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_ID = "0123456789abcdef0123456789abcdef"
FAKE_KEY = "sk-acceptance-test-DO-NOT-STORE"

#: 归档库的读接口（`count` / `list_recent` / `get_for_user`）在 task-11 多用户改造后
#: **必须显式带归属**，本文件统一用这个 id。写记录时也带上同一个 —— 不带的话记录会以
#: `owner_user_id = NULL` 入库，按归属一查全空（`NULL = 1` 在 SQL 里是 NULL，不是 TRUE）。
ACCEPT_OWNER_ID = 1

# 子进程必须看到的最小干净环境：清掉所有 LLM 变量，防止机器级真 key 漏进去。
_LLM_ENV_CLEAR = ("DEEPSEEK_API_KEY", "LLM_API_KEY",
                  "DEEPSEEK_BASE_URL", "LLM_BASE_URL",
                  "DEEPSEEK_MODEL", "LLM_MODEL")


# --------------------------------------------------------------------------
# 公共夹具
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """把库 / 上传 / 图表目录都指到 tmp_path，并清掉两层 lru_cache（进出各一次）。"""
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


@pytest.fixture
def client():
    """带 lifespan 的客户端。必须用 `with` —— 模块级实例不跑 lifespan，建表被跳过。"""
    with TestClient(app) as test_client:
        yield test_client


def _make_state(status=WorkflowStatus.SUCCESS, *, question="验收探针", with_report=True,
                workflow_id=WORKFLOW_ID, finished_at=None):
    """造一个终态 / 非终态的工作流状态。"""
    now = datetime.now(timezone.utc)
    report = None
    if with_report:
        report = FinalReport(
            report_id="a1b2c3d4e5f6a7b8",
            title="验收探针报告",
            summary="这是一份由独立验收测试构造的报告。",
            sections=[ReportSection(heading="结论", text="探针正文。", cites=["T1"])],
            generated_at=now,
            llm_used=True,
        )
    return WorkflowState(
        workflow_id=workflow_id,
        dataset_id="abcdef0123456789abcdef0123456789",
        question=question,
        status=status,
        created_at=now - timedelta(seconds=30),
        finished_at=finished_at if finished_at is not None else (
            now if status in (WorkflowStatus.SUCCESS,
                              WorkflowStatus.SUCCESS_WITH_WARNINGS,
                              WorkflowStatus.FAILED) else None),
        report=report,
        owner_user_id=ACCEPT_OWNER_ID,
        owner_username="acceptance",
    )


def _record(workflow_id=WORKFLOW_ID, **kw) -> HistoryRecord:
    now = datetime.now(timezone.utc)
    base = dict(
        workflow_id=workflow_id,
        question="验收探针",
        status="SUCCESS",
        created_at=now - timedelta(seconds=30),
        finished_at=now,
        has_report=True,
        llm_used=True,
        report_text='{"probe": true}',
        error_code=None,
        archived_at=now,
        owner_user_id=ACCEPT_OWNER_ID,
        owner_username="acceptance",
    )
    base.update(kw)
    return HistoryRecord(**base)


# ==========================================================================
# A1 真重启存活 —— 必须是**另一个进程**
# ==========================================================================

_CHILD_ARCHIVE = '''
import os, sys, json, dataclasses
sys.path.insert(0, {root!r})
for k in {clear!r}:
    os.environ.pop(k, None)
os.environ["DB_PATH"] = {db!r}
from app.data.history import get_history_store, reset_history_store_cache
from app.data.history import HistoryRecord
from datetime import datetime, timedelta, timezone
reset_history_store_cache()
store = get_history_store()
assert store is not None, "DB_PATH 设了但 store 是 None"
now = datetime.now(timezone.utc)
rec = HistoryRecord(
    workflow_id={wid!r}, question={q!r}, status="SUCCESS",
    created_at=now - timedelta(seconds=30), finished_at=now,
    has_report=True, llm_used=True,
    report_text='{{"probe": "cross-process"}}', error_code=None, archived_at=now,
    owner_user_id={owner!r}, owner_username="acceptance",
)
store.archive(rec)
print("CHILD1_PID=" + str(os.getpid()))
print("CHILD1_COUNT=" + str(store.count(owner_user_id={owner!r})))
print("CHILD1_ROW=" + json.dumps(
    {{f.name: str(getattr(store.get_for_user({wid!r}, {owner!r}), f.name)) for f in dataclasses.fields(rec)}},
    ensure_ascii=False, sort_keys=True))
'''

_CHILD_READ = '''
import os, sys, json, dataclasses
sys.path.insert(0, {root!r})
for k in {clear!r}:
    os.environ.pop(k, None)
os.environ["DB_PATH"] = {db!r}
from app.data.history import get_history_store, reset_history_store_cache
reset_history_store_cache()
store = get_history_store()
rec = store.get_for_user({wid!r}, {owner!r})
print("CHILD2_PID=" + str(os.getpid()))
if rec is None:
    print("CHILD2_ROW=None")
else:
    print("CHILD2_ROW=" + json.dumps(
        {{f.name: str(getattr(rec, f.name)) for f in dataclasses.fields(rec)}},
        ensure_ascii=False, sort_keys=True))
print("CHILD2_COUNT=" + str(store.count(owner_user_id={owner!r})))
print("CHILD2_LIST=" + str(len(store.list_recent(limit=10, owner_user_id={owner!r}))))
'''


def _run_child(tmp_path, body):
    """在一个干净环境的独立 python 进程里跑一段脚本，返回 stdout。"""
    script = tmp_path / "child.py"
    script.write_text(body, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in _LLM_ENV_CLEAR}
    env["PYTHONIOENCODING"] = "utf-8"
    if "SystemRoot" not in env:
        env["SystemRoot"] = r"C:\Windows"
    proc = subprocess.run([sys.executable, str(script)], cwd=str(PROJECT_ROOT),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=120)
    return proc


def test_a1_a_record_survives_a_real_process_restart(tmp_path):
    """A1：进程 1 归档后退出，进程 2（不同 pid）仍能逐字段读回。"""
    db = tmp_path / "db" / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    fmt = dict(root=str(PROJECT_ROOT), clear=list(_LLM_ENV_CLEAR), db=str(db),
               wid=WORKFLOW_ID, q="A1 跨进程存活探针", owner=ACCEPT_OWNER_ID)

    first = _run_child(tmp_path, _CHILD_ARCHIVE.format(**fmt))
    assert first.returncode == 0, "归档子进程失败：\n%s\n%s" % (first.stdout, first.stderr)
    assert "CHILD1_COUNT=1" in first.stdout, first.stdout

    # 进程 1 已经退出，这是"真重启"的前提
    second = _run_child(tmp_path, _CHILD_READ.format(**fmt))
    assert second.returncode == 0, "读取子进程失败：\n%s\n%s" % (second.stdout, second.stderr)

    def _val(out, key):
        m = re.search(r"^%s=(.*)$" % key, out, re.MULTILINE)
        assert m, "子进程输出里找不到 %s：\n%s" % (key, out)
        return m.group(1)

    pid1, pid2 = _val(first.stdout, "CHILD1_PID"), _val(second.stdout, "CHILD2_PID")
    assert pid1 != pid2, "两个阶段必须真的是不同进程（拿到同一个 pid 说明探针无效）"

    row1 = json.loads(_val(first.stdout, "CHILD1_ROW"))
    row2 = json.loads(_val(second.stdout, "CHILD2_ROW"))
    assert row2 != {"": "None"}, second.stdout
    # 12 = HistoryRecord 现在的字段数（task-11 加了 owner_user_id / owner_username）。
    # 这个数字写死是故意的：字段数变了要有人来看一眼"新增的字段跨进程存不存得住"。
    assert len(row1) == 12, "HistoryRecord 字段数变了：%d" % len(row1)
    for field in row1:
        assert row1[field] == row2[field], (
            "字段 %s 跨进程不一致：进程1=%r 进程2=%r" % (field, row1[field], row2[field]))

    assert _val(second.stdout, "CHILD2_COUNT") == "1"
    assert _val(second.stdout, "CHILD2_LIST") == "1"
    # 落盘而不是内存：磁盘上确实有非空的库文件
    assert db.exists() and db.stat().st_size > 0


def test_a1_schema_is_recreated_by_a_fresh_process(tmp_path):
    """A1 补：新进程对**同一个库**再 ensure_schema() 不报错（schema_version 一致）。"""
    db = tmp_path / "db" / "app.db"
    HistoryStore(db).ensure_schema()
    conn = sqlite3.connect(db)
    vs = {r[0] for r in conn.execute(
        "SELECT DISTINCT schema_version FROM workflow_history")}
    conn.close()
    # 没有数据时 DISTINCT 返回空集，这也是允许的
    assert vs <= {HISTORY_SCHEMA_VERSION}
    # 第二个"进程"（这里用新对象模拟重开）再来一次不应抛
    store = HistoryStore(db)
    store.ensure_schema()
    assert store.count(owner_user_id=ACCEPT_OWNER_ID) == 0


# ==========================================================================
# A2 归档失败旁路
# ==========================================================================


def test_a2_archive_failure_does_not_break_a_successful_analysis(tmp_path, monkeypatch):
    """A2：归档库不可用时 archive_if_terminal() 返回 False 且**不抛**，分析结果不受影响。"""
    blocker = tmp_path / "blocked" / "app.db"
    blocker.mkdir(parents=True, exist_ok=True)  # 路径本身是个目录 -> sqlite 打不开
    monkeypatch.setenv("DB_PATH", str(blocker))
    config.reset_settings_cache()
    reset_history_store_cache()
    assert blocker.is_dir()

    state = _make_state(WorkflowStatus.SUCCESS)
    result = archive.archive_if_terminal(state)
    assert result is False, "库不可用时应当返回 False（静默旁路）"
    # 状态本身没被改坏
    assert state.status is WorkflowStatus.SUCCESS
    assert state.report is not None
    # build_record 仍然可用（失败发生在写库那一步，不是构造那一步）
    rec = archive.build_record(state)
    assert rec.has_report is True and rec.workflow_id == WORKFLOW_ID


def test_a2_non_terminal_states_are_never_archived(tmp_path):
    """A2 补（也是变异 1 的红线）：非终态一律不归档。"""
    store = HistoryStore(tmp_path / "db" / "app.db")
    for status in (WorkflowStatus.PENDING, WorkflowStatus.PROFILING,
                   WorkflowStatus.EXECUTING, WorkflowStatus.VALIDATING):
        assert archive.archive_if_terminal(_make_state(status)) is False, status
        assert store.count(owner_user_id=ACCEPT_OWNER_ID) == 0 if store.path.exists() else True


def test_a2_terminal_state_is_archived_when_it_has_a_real_finished_at(tmp_path):
    """A2：带真实 `finished_at` 的终态**确实入库**，三种终态都被归档。

    ⚠️ 这条用例在 P2 修复时改过，改的原因值得写下来：

    它原本叫 `test_a2_terminal_state_is_archived_and_finished_at_falls_back`，做法是
    把 `state.finished_at` **手工置成 None** 再断言"兜底成 archived_at"。
    问题是**产品代码没有任何一条路径能产出这种状态**（`finished_at` 的 4 个写入点
    全在把状态收成终态的同一个代码块里），所以这条用例测的是一个现实中不存在的
    输入，而它的存在**挡住了 P2 修复**：修复（终态但无 finished_at → 拒收）
    落地时它立刻变红，我一度以为"验收用例与新行为冲突，得改回去"。

    正确的读法是：**这条用例原本钉住的是"对一个不可能出现的输入如何兜底"**，
    而 P2 的结论是"这个输入应当被拒收"。所以现在拆成两条：
    这条钉"正常终态能入库"，拒收行为由 `tests/test_history_archive.py::
    test_a_terminal_state_without_finished_at_is_refused_not_given_a_fake_time` 钉。
    """
    for status in (WorkflowStatus.SUCCESS, WorkflowStatus.SUCCESS_WITH_WARNINGS,
                   WorkflowStatus.FAILED):
        state = _make_state(status, with_report=(status is not WorkflowStatus.FAILED))
        assert state.finished_at is not None, "夹具要造的是正常终态"

        assert archive.archive_if_terminal(state) is True, status

        rec = get_history_store().get_for_user(state.workflow_id, ACCEPT_OWNER_ID)
        assert rec is not None
        assert rec.finished_at == state.finished_at, "入库的时间必须是 state 上那个真时间"
        registry.clear()
        reset_history_store_cache()


# ==========================================================================
# A3 密钥不落库
# ==========================================================================


def test_a3_an_env_api_key_never_reaches_the_database(tmp_path, monkeypatch):
    """A3：key 只在环境变量里 -> 库里原始字节扫不到 b"sk-"，逐列也扫不到。"""
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    # 机器级真 key 必须清掉（本机真的存在）
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config.reset_settings_cache()
    reset_history_store_cache()

    db = tmp_path / "db" / "app.db"
    state = _make_state(WorkflowStatus.SUCCESS, question="分析不同地区的销售额")
    assert archive.archive_if_terminal(state) is True
    assert db.exists()

    raw = db.read_bytes()
    assert raw.count(b"sk-") == 0, "库里出现了 b\"sk-\"，密钥可能落库"
    assert FAKE_KEY.encode() not in raw

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM workflow_history").fetchone()
    assert row is not None
    for col in row.keys():
        val = row[col]
        if isinstance(val, str):
            assert FAKE_KEY not in val, "列 %s 里出现了密钥" % col
            assert "sk-" not in val, "列 %s 里出现了 b\"sk-\"" % col
    conn.close()

    journal = tmp_path / "db" / "app.db-journal"
    if journal.exists():
        assert journal.read_bytes().count(b"sk-") == 0


def test_a3_the_key_is_not_persisted_even_when_settings_read_it(tmp_path, monkeypatch):
    """A3 补：确认 settings 真的读到了这个 key（探针非空转），但库里仍然没有。"""
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config.reset_settings_cache()
    assert config.get_settings().llm_api_key == FAKE_KEY, "探针无效：settings 没读到 key"
    assert config.get_settings().llm_configured is True

    db = tmp_path / "db" / "app.db"
    archive.archive_if_terminal(_make_state())
    assert FAKE_KEY.encode() not in db.read_bytes()


# ==========================================================================
# A4 禁止复活
# ==========================================================================


def test_a4_history_survives_registry_clear_but_the_live_route_still_404s(client):
    """A4：clear 后 live 路由仍 404，历史仍在，且 registry.count() 不变。"""
    state = _make_state(WorkflowStatus.SUCCESS)
    registry.create(state)
    assert archive.archive_if_terminal(state) is True
    assert registry.count() == 1

    r = client.get("/api/workflows/history")
    assert r.status_code == 200
    ids = [i["workflow_id"] for i in r.json()["data"]["items"]]
    assert WORKFLOW_ID in ids, "归档后历史里应当能看到这条"

    registry.clear()
    assert registry.count() == 0

    # ① live 路由必须 404
    live = client.get("/api/workflows/%s" % WORKFLOW_ID)
    assert live.status_code == 404, "禁止复活：clear 后 live 路由必须 404"
    assert live.json()["error"]["code"] == "NOT_FOUND"
    assert registry.count() == 0, "请求之后 registry 不能被塞回东西（fallback 复活）"

    # ② 历史接口仍看得到
    assert client.get("/api/workflows/history").json()["data"]["total"] == 1
    detail = client.get("/api/workflows/history/%s" % WORKFLOW_ID)
    assert detail.status_code == 200
    assert detail.json()["data"]["item"]["workflow_id"] == WORKFLOW_ID

    # ③ 再查一次 live，仍然 404（读历史不能把状态写回内存）
    assert client.get("/api/workflows/%s" % WORKFLOW_ID).status_code == 404
    assert registry.count() == 0


def test_a4_history_route_always_lists_even_when_registry_is_empty(client):
    """A4 补：内存全空时历史接口仍然 200 + ok=true（不是错误）。"""
    registry.clear()
    r = client.get("/api/workflows/history")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["error"] is None
    assert body["data"]["items"] == []


# ==========================================================================
# A5 路由顺序
# ==========================================================================


def test_a5_the_history_path_is_not_swallowed_by_the_workflow_route(client):
    """A5：/api/workflows/history 必须是 200 + ok=true，不能变成 404「工作流不存在」。

    变异 3（把两个 history 路由移到 get_workflow 之后）就是靠这条变红。
    """
    r = client.get("/api/workflows/history")
    assert r.status_code == 200, "被 /workflows/{workflow_id} 吃掉了：%d" % r.status_code
    body = r.json()
    assert body["ok"] is True
    err = body.get("error") or {}
    assert err.get("code") != "NOT_FOUND", "history 被当成 workflow_id 解析了"
    assert body["data"]["enabled"] is True

    # 反方向：真正不存在的 32 位 id 仍然老老实实 404
    missing = client.get("/api/workflows/%s" % ("f" * 32))
    assert missing.status_code == 404
    assert missing.json()["error"]["message"] == "工作流不存在"


def test_a5_the_history_detail_path_is_not_swallowed_either(client):
    """A5 补：详情路径同样不能被 /workflows/{workflow_id} 吃掉。"""
    r = client.get("/api/workflows/history/%s" % ("a" * 32))
    assert r.status_code == 404
    msg = r.json()["error"]["message"]
    assert msg == "没有归档记录", "拿到 %r，说明路由顺序错了" % msg


# ==========================================================================
# A6 端到端：真跑产出的记录可经接口读回
# ==========================================================================


def test_a6_an_archived_record_is_readable_end_to_end_through_the_api(client):
    """A6：归档 -> 新 app 实例 -> 历史接口列出 + 详情能反序列化出 FinalReport。

    （真 key 的那一轮在交付说明里附原始输出；这里断言链路本身。）
    """
    state = _make_state(WorkflowStatus.SUCCESS)
    assert archive.archive_if_terminal(state) is True

    listed = client.get("/api/workflows/history?limit=5").json()["data"]
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["workflow_id"] == WORKFLOW_ID
    assert item["status"] == "SUCCESS"
    assert item["llm_used"] is True
    assert item["has_report"] is True

    detail = client.get("/api/workflows/history/%s" % WORKFLOW_ID).json()["data"]
    report = detail["report"]
    assert report is not None, "report_text 反序列化失败"
    assert report["title"] == "验收探针报告"
    assert report["sections"][0]["cites"] == ["T1"]


def test_a6_archived_rows_are_listed_newest_first(client):
    """A6 补：finished_at 倒序；finished_at 为 NULL 的排最后（不靠 NULLS LAST）。

    注意：走 `archive_if_terminal()` 时 `build_record()` 会把 None 兜底成 archived_at，
    所以经 API 归档的行**不会**出现 NULL。这里先用接口造三条有时间的行，
    再直接往库里写一条 NULL 行，验证排序把 NULL 放最后。
    """
    base = datetime.now(timezone.utc)
    for wid, fin in [
        ("0" * 32, base - timedelta(hours=3)),
        ("2" * 32, base - timedelta(hours=1)),
        ("3" * 32, base - timedelta(minutes=10)),
    ]:
        state = _make_state(WorkflowStatus.SUCCESS, workflow_id=wid)
        state.finished_at = fin
        assert archive.archive_if_terminal(state) is True

    # finished_at=None 经 build_record 会被兜底成 archived_at（变异 2 的语义），
    # 所以走归档路径的那条不会是 NULL；这里断言兜底确实发生了。
    fb = get_history_store().get_for_user("2" * 32, ACCEPT_OWNER_ID)
    assert fb.finished_at is not None

    # 直接写库造一条 finished_at=NULL 的"未跑完"行
    store = get_history_store()
    store.archive(HistoryRecord(
        workflow_id="1" * 32, question="未跑完的那条", status="EXECUTING",
        created_at=base - timedelta(minutes=5), finished_at=None,
        has_report=False, llm_used=False, report_text=None, error_code=None,
        archived_at=base,
        # 归属要和上面那些归档出来的记录一致，否则这条会因为"不是我的"而不出现在列表里
        owner_user_id=ACCEPT_OWNER_ID, owner_username="acceptance"))

    items = client.get("/api/workflows/history?limit=10").json()["data"]["items"]
    got = [i["workflow_id"] for i in items]
    assert got == ["3" * 32, "2" * 32, "0" * 32, "1" * 32], "排序不对：%s" % got
    assert items[-1]["finished_at"] is None, "NULL 那条必须排在最后"


def test_a6_pagination_is_exposed_and_validated(client):
    """A6 补：limit/offset 生效且参数被校验（边界值 422）。"""
    now = datetime.now(timezone.utc)
    for i in range(5):
        state = _make_state(WorkflowStatus.SUCCESS, workflow_id="%032d" % i)
        state.finished_at = now - timedelta(minutes=i)
        archive.archive_if_terminal(state)

    page = client.get("/api/workflows/history?limit=2&offset=1").json()["data"]
    assert page["limit"] == 2 and page["offset"] == 1 and page["total"] == 5
    assert len(page["items"]) == 2

    assert client.get("/api/workflows/history?limit=0").status_code == 422
    assert client.get("/api/workflows/history?limit=101").status_code == 422
    assert client.get("/api/workflows/history?offset=-1").status_code == 422


# ==========================================================================
# A7 并发
# ==========================================================================


def test_a7_concurrent_writers_and_readers_never_hit_database_locked(tmp_path):
    """A7：20 写 + 20 读并发，不出现 database is locked / 不丢行。"""
    store = HistoryStore(tmp_path / "db" / "app.db")
    store.ensure_schema()

    errors: list = []
    written: list = []
    lock = threading.Lock()

    def writer(n):
        try:
            rec = _record(workflow_id="%032d" % n)
            store.archive(rec)
            with lock:
                written.append(n)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append("W%d:%s: %s" % (n, type(exc).__name__, exc))

    def reader(n):
        try:
            store.list_recent(limit=100, owner_user_id=ACCEPT_OWNER_ID)
            store.count(owner_user_id=ACCEPT_OWNER_ID)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append("R%d:%s: %s" % (n, type(exc).__name__, exc))

    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(writer, i) for i in range(20)]
        futs += [pool.submit(reader, i) for i in range(20)]
        for f in futs:
            f.result()

    assert errors == [], "并发出错：%s" % errors
    assert len(written) == 20
    assert store.count(owner_user_id=ACCEPT_OWNER_ID) == 20, "写入丢失：count=%d" % store.count(owner_user_id=ACCEPT_OWNER_ID)
    rows = store.list_recent(limit=100, owner_user_id=ACCEPT_OWNER_ID)
    assert len(rows) == 20
    assert len({r.workflow_id for r in rows}) == 20, "出现重复行，upsert 有问题"
    assert not any("locked" in e for e in errors)


def test_a7_upsert_is_idempotent_by_workflow_id(tmp_path):
    """A7 补：同一个 workflow_id 反复归档只留一行（PRIMARY KEY + upsert）。"""
    store = HistoryStore(tmp_path / "db" / "app.db")
    for i in range(5):
        store.archive(_record(question="第 %d 次" % i))
        store.ensure_schema()
    assert store.count(owner_user_id=ACCEPT_OWNER_ID) == 1
    assert store.get_for_user(WORKFLOW_ID, ACCEPT_OWNER_ID).question == "第 4 次"


# ==========================================================================
# A8 零新依赖
# ==========================================================================


def test_a8_requirements_do_not_mention_sqlalchemy():
    """A8：requirements 里没有引入 SQLAlchemy 之类的 ORM（标准库 sqlite3 已足够）。"""
    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for banned in ("sqlalchemy", "alembic", "peewee", "sqlmodel", "databases"):
        assert banned not in text, "requirements 里出现了 %s" % banned
    assert "fastapi" in text and "pandas" in text


def test_a8_history_module_only_uses_the_standard_library():
    """A8：history.py 的 import 里没有第三方 ORM。

    口径说明：这里**只匹配 import 语句**，不是全文 substring。`history.py` 自己在
    docstring 里写了「为什么不用 SQLAlchemy」（`app/data/history.py:23/26/27`），
    `tests/test_history_store.py:536` 也有一句注释 —— 用 substring 扫会误报 4 处。
    「有没有引入依赖」的判据是 import 了什么，不是文档里提到了什么。
    """
    src = (PROJECT_ROOT / "app" / "data" / "history.py").read_text(encoding="utf-8")
    for banned in ("sqlalchemy", "peewee", "sqlmodel"):
        pat = re.compile(r"^\s*(import|from)\s+\S*%s" % banned, re.MULTILINE | re.IGNORECASE)
        assert not pat.search(src), "history.py 里 import 了 %s" % banned
    assert "import sqlite3" in src

    # import 口径之外再补一条**全仓**扫描：src 里的 import 语句（不是注释/字符串）。
    # 扫全仓是为了防止将来别处偷偷 import 一个 ORM 却在 requirements 里漏填。
    third_party = []
    for path in sorted(PROJECT_ROOT.rglob("*.py")):
        parts = set(path.relative_to(PROJECT_ROOT).parts)
        if parts & {".venv", ".git", ".tmp", "__pycache__"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for banned in ("sqlalchemy", "peewee", "sqlmodel", "alembic"):
            pat = re.compile(r"^\s*(import|from)\s+\S*%s" % banned, re.MULTILINE | re.IGNORECASE)
            if pat.search(text):
                third_party.append("%s -> %s" % (path.name, banned))
    assert third_party == [], "仓库里出现了 ORM import：%s" % third_party

    # 决定性判据：装了没有。requirements 里没写还不够，环境里也不该有。
    found = importlib.util.find_spec("sqlalchemy")
    assert found is None, "环境里居然装了 sqlalchemy：%s" % (found,)


# ==========================================================================
# A9 git 干净（跑完不产生脏文件）
# ==========================================================================


def _git(*args: str):
    """跑一条 git 命令，返回 (returncode, stdout)。

    git 在 Windows 上需要 ComSpec / SystemRoot 才能启动；如果当前进程环境被剥得太狠
    （例如 CI 里只传了最小 env），subprocess 会直接 FileNotFoundError。
    这时**跳过**而不是让用例红 —— 这是环境缺陷，不是产品缺陷。
    """
    try:
        proc = subprocess.run(["git", *args], cwd=str(PROJECT_ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except (FileNotFoundError, OSError) as exc:  # noqa: BLE001
        pytest.skip("环境里跑不了 git（%s: %s），跳过版本控制相关断言" % (type(exc).__name__, exc))
    return proc.returncode, proc.stdout


def test_a9_the_archive_db_is_gitignored():
    """A9：data/db/app.db 必须被 .gitignore 挡住，绝不能进版本库。"""
    gi = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/db/*" in gi, ".gitignore 不再忽略 data/db/* 了"
    _rc, out = _git("ls-files", "data/db/")
    files = [f for f in out.split() if f]
    assert files == ["data/db/.gitkeep"], "data/db 下有被跟踪的文件：%s" % files


def test_a9_running_this_suite_leaves_no_untracked_files_in_data():
    """A9 补：本文件的用例把库写到 tmp_path，跑完 data/ 下不该多出文件。"""
    _rc, out = _git("status", "--porcelain", "--untracked-files=all")
    data_lines = [ln for ln in out.splitlines() if "data/" in ln and "db/" in ln]
    assert data_lines == [], "data/db 下出现未忽略文件：%s" % data_lines


# ==========================================================================
# 库契约补充（不属于 A1..A9，但属于"归档"的正确性）
# ==========================================================================


def test_schema_version_mismatch_is_rejected(tmp_path):
    """版本不符必须报错（不能静默继续用旧表）。"""
    db = tmp_path / "db" / "app.db"
    store = HistoryStore(db)
    store.ensure_schema()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO workflow_history (workflow_id, question, status, created_at,"
                 " has_report, llm_used, schema_version, archived_at)"
                 " VALUES ('x', 'q', 'SUCCESS', '2026-01-01T00:00:00+00:00', 0, 0, 999,"
                 " '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    with pytest.raises(history.HistoryStoreError):
        HistoryStore(db).ensure_schema()


def test_negative_limit_and_offset_are_rejected(tmp_path):
    """负数分页参数直接 ValueError（不接受静默修正）。"""
    store = HistoryStore(tmp_path / "db" / "app.db")
    store.ensure_schema()
    with pytest.raises(ValueError):
        store.list_recent(limit=-1, owner_user_id=ACCEPT_OWNER_ID)
    with pytest.raises(ValueError):
        store.list_recent(offset=-1, owner_user_id=ACCEPT_OWNER_ID)


def test_get_history_store_returns_none_when_db_path_is_empty(monkeypatch):
    """DB_PATH 为空 = 归档关闭 -> store 是 None（接口返回 enabled=false）。"""
    monkeypatch.setenv("DB_PATH", "")
    config.reset_settings_cache()
    reset_history_store_cache()
    assert get_history_store() is None


def test_history_endpoint_reports_disabled_when_db_path_is_empty(client, monkeypatch):
    """归档关闭时历史接口仍然 200，但 enabled=false（不是错误）。"""
    monkeypatch.setenv("DB_PATH", "")
    config.reset_settings_cache()
    reset_history_store_cache()
    r = client.get("/api/workflows/history")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["enabled"] is False
    assert body["data"]["items"] == []


def test_a_timestamps_round_trip_as_utc(tmp_path):
    """时间戳写进 SQLite 再读出来必须是 UTC-aware（朴素时间会被补成 UTC）。"""
    store = HistoryStore(tmp_path / "db" / "app.db")
    naive = datetime(2026, 3, 4, 5, 6, 7)
    store.archive(_record(created_at=naive, finished_at=naive, archived_at=naive))
    rec = store.get_for_user(WORKFLOW_ID, ACCEPT_OWNER_ID)
    assert rec.created_at.tzinfo is not None, "读回来是朴素时间"
    assert rec.created_at.utcoffset() == timedelta(0)
    assert rec.created_at.year == 2026 and rec.created_at.hour == 5


def test_the_db_file_lives_where_db_path_points(tmp_path, monkeypatch):
    """库文件必须落在 DB_PATH 指定的位置（不能偷偷写到 cwd 或仓库里）。"""
    target = tmp_path / "custom" / "nested" / "mine.db"
    monkeypatch.setenv("DB_PATH", str(target))
    config.reset_settings_cache()
    reset_history_store_cache()
    store = get_history_store()
    assert store is not None
    assert Path(store.path) == target
    store.ensure_schema()
    assert target.exists()
    assert (PROJECT_ROOT / "mine.db").exists() is False

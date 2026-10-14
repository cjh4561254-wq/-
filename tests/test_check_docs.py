# -*- coding: utf-8 -*-
"""task-40：给 `scripts/check_docs.py`（文档票台账的自检脚本）建一支**变异测试**。

为什么要有这个文件
------------------
`docs/HISTORY_ROBUSTNESS.md` 靠 `scripts/check_docs.py` 自证"文档没腐烂"。可是
**谁来证明那个自检脚本自己还有牙？** 如果有人把 `check_docs.py` 的某个判据
改松（或者以后重构时不小心删掉一条分支），文档会重新开始无声骗人 ——
而那时恰好没有任何红灯。本文件把 `check_docs.py` 的每条判据都配上
"**故意改坏 → 必须红灯**"的用例，让"台账脚本有没有牙"从此一直有人守。

隔离原则（本文件最重要的一条）
------------------------------
所有变异**只在仓库 `.tmp/task40_mirror/` 下的私有镜像里**做，真仓库一个字节都不碰：

- 不依赖 pytest 的 `tmp_path`：从仓库根或 `tests/` 子目录启动时，其 basetemp 位置不同；每个测试用 uuid 创建独立镜像，并在 fixture teardown 清理；
- 镜像按**同样相对路径**复刻 `app/ tests/ scripts/ docs/` + `conftest.py`
  + `pytest.ini`（`check_docs.py` 的根目录是从
  `Path(__file__).resolve().parent.parent` 推的，所以相对路径必须一致）；
- 子进程 `cwd` = 镜像、`PYTHONPATH` = 镜像，于是它只会读镜像里的文件；
- 真仓库里没有被点名的源文件**只读**打开，从不写入。

怎么证明"确实没碰真仓库"：`test_mirror_never_touches_the_real_repository`
对 `tests/ scripts/ docs/ app/` 下每个源码/文档文件在跑完变异前后取 sha256 比对；排除
pytest/`tmp*` 运行目录、`__pycache__` 与 `.pyc`，因为它们是运行产物且可能被并发测试创建/清理。
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# 路径与常量
# --------------------------------------------------------------------------
# 本文件在 <root>/tests/ 下，所以 parents[1] 就是项目根。
ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
CHECK_DOCS = "scripts/check_docs.py"
DOC = "docs/HISTORY_ROBUSTNESS.md"
CASES = "tests/test_history_robustness.py"
ROUTES = "app/api/routes_workflows.py"

# 真仓库里"必须一个字节都不许变"的目录（源码/文档 sha256 比对用）。
GUARDED_DIRS = ("app", "scripts", "docs", "tests")
_IGNORED_RUNTIME_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tmp",
    ".coverage",
}

# 镜像要复刻的东西。相对路径必须与真仓库一致 —— 见文件头说明。
_MIRROR_ENTRIES = ("app", "tests", "scripts", "docs", "conftest.py", "pytest.ini")
_IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")

# 镜像的私有落点。**刻意不用 `tmp_path`**：虽然 conftest 把默认 basetemp
# 指到 `.tmp/pytest`，但调用者显式传入相对 `--basetemp` 时，pytest 按当前 cwd
# 解析它；从 `tests/` 启动可能把 tmp_path 放在 `tests/` 里。本机 `tempfile`
# 也会退回 cwd。把镜像放到仓库级 `.tmp/task40_mirror/` 可避开两类临时目录差异。
# 每个用例用 uuid 创建私有路径，避免并发误删。
_MIRROR_ROOT = ROOT / ".tmp" / "task40_mirror"

# `test_a_healthy_history_is_unaffected` 里第一处请求，用于"只发一个请求"的变异。
# 那一行在用例体里缩进 8 格（`with _client() as client:` 之内）。
_FIRST_REQUEST = '        listed = client.get("/api/workflows/history?limit=100")'
# 在第一个请求**之前**插入 `return`：一次请求都不会发出，于是
# `_record_acceptance_status` 记录不到任何状态码，交出哨兵值 -1。
_RETURN_EARLY = "        return  # task-40 变异：在任何请求之前短路\n" + _FIRST_REQUEST

# 目标行（`:215`）与它上方那行**声明**（`:214`）的原文。
# 注意声明行的引文里**原样包含**目标行的文本，所以改目标行时不能只按文本替换，
# 否则会同时命中声明行 —— 那会把"行号漂移"和"引文失配"两个维度搅在一起。
_TARGET_LINE = "        status=_coerce_enum(WorkflowStatus, record.status),"
_DECL_LINE = (
    "        # **:215** `        status=_coerce_enum(WorkflowStatus, record.status),`"
    "  ^{用例覆盖 test_one_bad_row_no_longer_poisons_the_listing}"
)


# --------------------------------------------------------------------------
# 镜像
# --------------------------------------------------------------------------
def _tree_hashes(root: Path) -> dict[str, str]:
    """对 `GUARDED_DIRS` 下每个文件取 sha256（用于"真仓库没被动过"的证明）。

    跳过 pytest/工具运行产物目录与字节码：它们不是源码，且并发会话可能
    在扫描期间创建或清理它们。源码、文档和其他受版本控制的文件仍逐字节哈希。
    """
    out: dict[str, str] = {}
    for rel in GUARDED_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        for file in sorted(base.rglob("*")):
            relative = file.relative_to(root)
            # On this Windows host, pytest launched from `tests/` may leave a
            # tmp_path directory directly under tests/ (for example
            # tests/tmpj2b3pm7a). Include the basename in this check so the
            # directory itself is skipped before it can be mistaken for a file.
            if any(
                part in _IGNORED_RUNTIME_DIRS
                or re.fullmatch(r"tmp[a-z0-9_-]{8}", part, flags=re.IGNORECASE)
                or part.startswith("pytest-of-")
                for part in relative.parts[1:]
            ):
                continue
            if file.is_dir() or not file.is_file():
                continue
            if file.suffix in {".pyc", ".pyo"}:
                continue
            out[file.relative_to(root).as_posix()] = hashlib.sha256(
                file.read_bytes()
            ).hexdigest()
    return out


def _mirror_dir(name: str) -> Path:
    """给一个用例分配**私有**的镜像根（见 `_MIRROR_ROOT` 处的说明）。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:48]
    path = _MIRROR_ROOT / f"{safe}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    return path


def _build_mirror(parent: Path) -> Path:
    """在 `parent` 里按同样相对路径复刻一份可独立运行的仓库。

    `check_docs.py` 会 `sys.path.insert(0, PROJECT_ROOT)` 再 `from tests import ...`，
    而 `PROJECT_ROOT` 由脚本自身位置推出 —— 所以镜像里必须有完整的
    `tests/`（包）与它要 import 的 `app/`。`conftest.py` 不必执行（我们不用
    pytest 跑 `check_docs.py`），但带上它可以让镜像更接近真仓库。
    """
    mirror = parent / "repo"
    mirror.mkdir()
    for rel in _MIRROR_ENTRIES:
        src = ROOT / rel
        dst = mirror / rel
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_IGNORED)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    # `_record_acceptance_status` 会往 PROJECT_ROOT/".tmp" 下 mkdtemp；
    # 镜像不是靠 pytest 跑的，conftest.py 的 TEMP 重定向不生效，所以得自己建。
    (mirror / ".tmp").mkdir(exist_ok=True)
    return mirror


def _child_env(mirror: Path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 让子进程只可能 import 到镜像里的 app/tests。
    env["PYTHONPATH"] = str(mirror)
    # 防止本机环境变量把被测行为带偏（与 conftest 的 _isolate_llm_env 同源考虑）。
    for name in ("DB_PATH", "UPLOAD_DIR", "CHARTS_DIR"):
        env.pop(name, None)
    return env


def _run_check_docs(mirror: Path) -> tuple[int, str]:
    """在镜像里跑一遍台账脚本，返回 (rc, 全部输出)。"""
    proc = subprocess.run(
        [str(PY), CHECK_DOCS],
        cwd=str(mirror),
        env=_child_env(mirror),
        capture_output=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")


def _output_tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


class _Mutation:
    """在镜像里做一次**字节级**破坏，并保证收尾时逐字节还原。

    为什么不用 `read_text()/write_text()`：仓库源文件是 LF，text 模式在
    Windows 上会把整文件转成 CRLF —— 那会让"还原"变成又一次破坏（本项目
    已确立的教训）。所以这里一律 `read_bytes()` / `write_bytes()`。
    """

    def __init__(self, mirror: Path, rel: str):
        self.path = mirror / rel
        self.original = self.path.read_bytes()

    def sub(self, old: str, new: str) -> None:
        text = self.original.decode("utf-8")
        count = text.count(old)
        assert count == 1, (
            "变异锚点在 %s 里应**唯一**匹配，实际匹配 %d 次：%r"
            % (self.path.name, count, old[:80])
        )
        self.path.write_bytes(text.replace(old, new).encode("utf-8"))

    def replace_line(self, lineno: int, new_text: str) -> None:
        """**按行号**替换第 lineno 行（1-based），其他行原样不动。

        为什么不能只按文本 `sub()`：`:215` 那对锚点的**声明行 `:214` 的引文里
        原样包含着 `:215` 那一行的文本**。只按文本替换会同时改掉声明行，
        于是"行号漂移"与"引文失配"两个维度被搅在一起，分不清谁抓到的。
        按行号改才是对这份文件结构的诚实做法。
        """
        lines = self.original.decode("utf-8").split("\n")
        assert 1 <= lineno <= len(lines), "行号越界：%d（共 %d 行）" % (lineno, len(lines))
        lines[lineno - 1] = new_text
        self.path.write_bytes("\n".join(lines).encode("utf-8"))

    def restore(self) -> None:
        self.path.write_bytes(self.original)
        assert self.path.read_bytes() == self.original, "镜像还原后与原始字节不一致"


@contextlib.contextmanager
def _mirror(name: str):
    """建好镜像、断言基线是绿的，退出时删干净（失败时也删，避免留垃圾）。

    基线不绿的话后面所有"改坏 → 红灯"的用例都会因为别的原因红，
    那些红灯就失去了证明力（假红和假绿一样危险）。所以这里先钉住基线。
    """
    if not PY.is_file():
        pytest.skip("找不到虚拟环境解释器：%s" % PY)
    parent = _mirror_dir(name)
    try:
        repo = _build_mirror(parent)
        rc, out = _run_check_docs(repo)
        assert rc == 0, (
            "镜像基线应当 rc=0，实际 %d。这不是被测脚本的问题，是镜像没搭对：\n%s"
            % (rc, _output_tail(out, 30))
        )
        yield repo
    finally:
        shutil.rmtree(parent, ignore_errors=True)


@pytest.fixture
def mirror(request: pytest.FixtureRequest):
    """每个用例一个私有镜像（名字带上用例名，便于排查残留）。"""
    with _mirror(request.node.name) as repo:
        yield repo


# ==========================================================================
# ⓪ 隔离性本身：镜像必须真的与真仓库物理隔离
# ==========================================================================
def test_mirror_is_structurally_isolated() -> None:
    """镜像不是软链接/同一文件 —— 否则"改坏"会打到真仓库。"""
    with _mirror("structurally_isolated") as repo:
        for rel in (CHECK_DOCS, DOC, CASES, ROUTES):
            assert (repo / rel).is_file(), "镜像缺 %s" % rel
            assert not (repo / rel).is_symlink(), "镜像里的 %s 是软链接，隔离不成立" % rel
            # 复制品必须落在镜像里，而不是指回真仓库。
            real, copy = ROOT / rel, repo / rel
            assert real.resolve() != copy.resolve(), "%s 指向了真仓库" % rel
            assert str(copy.resolve()).startswith(str(repo.resolve())), (
                "%s 不在镜像里" % rel
            )
        # 真仓库与镜像此刻内容一致（复制没走样）。
        for rel in (CHECK_DOCS, DOC, CASES, ROUTES):
            assert (ROOT / rel).read_bytes() == (repo / rel).read_bytes(), (
                "%s 复制后与真仓库内容不一致" % rel
            )


@pytest.mark.parametrize("view", [CHECK_DOCS, DOC, CASES, ROUTES])
def test_mirror_never_touches_the_real_repository(view: str) -> None:
    """**本文件最关键的一条**：在镜像里做一轮破坏，真仓库必须逐字节不变。

    没有这条，「变异测试」就可能变成「悄悄改坏真仓库再断言它红了」——
    那不但证明不了台账有牙，还会污染别人的工作区。
    """
    before = _tree_hashes(ROOT)
    with _mirror("never_touches_real_%s" % Path(view).name) as repo:
        # 在镜像里做一次规模最大的破坏：直接把这个文件截断。
        target = repo / view
        original = target.read_bytes()
        assert len(original) > 2, "%s 太短，这条用例证明不了什么" % view
        target.write_bytes(original[: len(original) // 2])

        after = _tree_hashes(ROOT)
        assert after == before, (
            "真仓库被改动了！下面这些文件的内容与跑变异之前不同：%s"
            % sorted(set(after) ^ set(before))
        )
        # 而且真仓库那个文件必须仍然完整（没被截断到镜像里的长度）。
        assert (ROOT / view).read_bytes() == original, (
            "真仓库的 %s 被改动了" % view
        )

    # 收尾之后真仓库仍然不变。
    assert _tree_hashes(ROOT) == before


# ==========================================================================
# ① 维度 1（行号锚点）有没有牙
# ==========================================================================
def test_code_anchor_line_drift_is_caught(mirror: Path) -> None:
    """代码里声明的行号漂移一位 → 必须红灯。

    `:215` 与 `:216` 那对锚点（声明紧挨目标行上方）是脆弱点：只要有人在它
    上面插一行注释，声明就指错了行。
    """
    mut = _Mutation(mirror, ROUTES)
    try:
        # 把第一处"行号锚点声明"里的 215 改成 216：声明与目标行不再对齐。
        mut.sub("# **:215**", "# **:216**")
        rc, out = _run_check_docs(mirror)
        assert rc == 1, "行号漂移没被抓住：\n%s" % _output_tail(out, 30)
        assert "不一致" in out
        assert "行号漂移" in out or "行号锚点" in out, _output_tail(out, 30)
    finally:
        mut.restore()


def test_code_anchor_quote_mismatch_is_caught(mirror: Path) -> None:
    """锚点**行号不变**、但那一行的内容被改掉（引文失配）→ 必须红灯。

    这是比"行号漂移"更隐蔽的一种腐烂：行号还是对的，读的人不会怀疑。
    这里用 `replace_line(215, ...)` 只改目标行本身 —— 声明行 `:214` 不动，
    所以报出来的必须是「声明 :215 是 X，实际是 Y」这种引文失配。
    """
    mut = _Mutation(mirror, ROUTES)
    try:
        mut.replace_line(215, _TARGET_LINE + "  # task-40 尾注")
        rc, out = _run_check_docs(mirror)
        assert rc == 1, "引文失配没被抓住：\n%s" % _output_tail(out, 30)
        assert "行号漂移" in out, _output_tail(out, 30)
        # 报的应当是"声明 X 实际 Y"，而不是"没有任何锚点认领这一行"。
        assert "实际是" in out, _output_tail(out, 30)
    finally:
        mut.restore()


# ==========================================================================
# ② 维度 2（用例存在性）有没有牙
# ==========================================================================
def test_renamed_fed_case_is_caught(mirror: Path) -> None:
    """被投喂的用例被改名（文档还写着旧名）→ 必须报干净的用例名问题。"""
    mut = _Mutation(mirror, CASES)
    try:
        mut.sub(
            "def test_a_healthy_history_is_unaffected():",
            "def test_a_healthy_history_is_unaffected_renamed():",
        )
        rc, out = _run_check_docs(mirror)
        assert rc == 1, "用例改名没被抓住：\n%s" % _output_tail(out, 30)
        assert "[用例名]" in out, _output_tail(out, 30)
        assert "找不到用例" in out, _output_tail(out, 30)
    finally:
        mut.restore()


def test_doc_line_anchor_pointing_at_nothing_is_caught(mirror: Path) -> None:
    """文档声明的行号在代码里没有任何锚点认领它 → 必须红灯。"""
    mut = _Mutation(mirror, DOC)
    try:
        mut.sub("行号锚点 `app/api/routes_workflows.py:215`",
                "行号锚点 `app/api/routes_workflows.py:999`")
        rc, out = _run_check_docs(mirror)
        assert rc == 1, "悬空行号锚点没被抓住：\n%s" % _output_tail(out, 30)
        assert "行号锚点" in out, _output_tail(out, 30)
    finally:
        mut.restore()


# ==========================================================================
# ③ 维度 3（断言方向）有没有牙
# ==========================================================================
def test_doc_expectation_flip_is_caught(mirror: Path) -> None:
    """文档把期望值 200 写成 500（而用例实测是 200）→ 必须红灯。

    这正是台账存在的理由：文档撒谎的那一刻就该红。
    """
    mut = _Mutation(mirror, DOC)
    try:
        mut.sub(
            "投喂行 `test_a_healthy_history_is_unaffected[instances=1,requests=2] -> 200`",
            "投喂行 `test_a_healthy_history_is_unaffected[instances=1,requests=2] -> 500`",
        )
        rc, out = _run_check_docs(mirror)
        assert rc == 1, "文档期望值翻转没被抓住：\n%s" % _output_tail(out, 30)
        assert "[断言方向]" in out, _output_tail(out, 30)
        assert "期望 500" in out, _output_tail(out, 30)
    finally:
        mut.restore()


def test_product_code_regression_that_the_case_pins_is_caught(mirror: Path) -> None:
    """★ 最贴近真实回归的一条：**产品代码**退回硬转，文档和用例都不动。

    把 `app/api/routes_workflows.py:215` 的 `_coerce_enum` 换回裸
    `WorkflowStatus(...)`，于是非法 `status` 重新抛出 `ValueError` → 500。
    台账脚本会真的去跑那条被投喂的用例，用例自己炸掉。

    这一条证明的是台账的**最终价值**：它守住的不只是文档，还有产品行为。

    用 `replace_line(215, ...)` 而不是文本替换：`:214` 那条声明行的引文里
    原样含有 `:215` 的文本，只按文本替换会同时改掉声明行，于是这一轮变异
    变成"行号漂移 + 用例炸"两件事混在一起，测的就不是单一东西了。
    """
    mut = _Mutation(mirror, ROUTES)
    try:
        mut.replace_line(215, "        status=WorkflowStatus(record.status),  # task-40 退回硬转")
        rc, out = _run_check_docs(mirror)
        assert rc == 1, "产品代码回归没被抓住：\n%s" % _output_tail(out, 40)
        assert "不一致" in out, _output_tail(out, 40)
        # 报的应当是"实测 500"这条干净判据（或一条明确指出用例执行失败的条目），
        # 而不是一段没人看得懂的裸栈。
        assert "[用例执行]" in out, _output_tail(out, 40)
        assert "AssertionError: 期望 200，实际 500" in out, _output_tail(out, 40)
        assert "'RUNNING' is not a valid WorkflowStatus" in out, _output_tail(out, 40)
    finally:
        mut.restore()


# ==========================================================================
# ④ 已知边界：这些**改坏了也不红** —— 钉住，免得后人误以为它们有牙
# ==========================================================================
def test_case_execution_failure_is_reported_cleanly(mirror: Path) -> None:
    """被投喂用例本身断言失败 → rc=1 且输出可定位的 Problem，不裸栈/假绿。"""
    mut = _Mutation(mirror, CASES)
    try:
        mut.sub(
            _FIRST_REQUEST,
            '        assert 1 == 2, "TASK40_FORCED_ASSERTION_FAILURE"\n' + _FIRST_REQUEST,
        )
        rc, out = _run_check_docs(mirror)
        assert rc == 1, "用例自身失败未被汇总：\n%s" % _output_tail(out, 30)
        assert "[用例执行]" in out, _output_tail(out, 30)
        assert "AssertionError: TASK40_FORCED_ASSERTION_FAILURE" in out, _output_tail(out, 30)
    finally:
        mut.restore()


def test_fixture_dependent_case_is_reported_cleanly(mirror: Path) -> None:
    """需要 pytest fixture 的投喂用例不能变成误导的裸栈，必须清楚报告。"""
    mut = _Mutation(mirror, DOC)
    try:
        mut.sub(
            "投喂行 `test_a_healthy_history_is_unaffected[instances=1,requests=2] -> 200`",
            "投喂行 `test_a_bad_report_text_is_logged_not_swallowed[instances=1,requests=1] -> 200`",
        )
        rc, out = _run_check_docs(mirror)
        assert rc == 1
        assert "[用例执行]" in out, _output_tail(out, 30)
        assert "TypeError: " in out, _output_tail(out, 30)
        assert "caplog" in out, _output_tail(out, 30)
    finally:
        mut.restore()


def test_partial_requests_are_caught_by_declared_request_count(mirror: Path) -> None:
    """只发出部分请求必须红：投喂行声明的每实例请求数不能静默缩水。"""
    mut = _Mutation(mirror, CASES)
    try:
        mut.sub(
            _FIRST_REQUEST,
            _FIRST_REQUEST + "\n        return  # task-40 变异：后续请求被短路",
        )
        rc, out = _run_check_docs(mirror)
        assert rc == 1, "删掉后续请求后台账仍假绿：\n%s" % _output_tail(out, 30)
        assert "文档声明每实例 2 个请求，实测 1 个" in out, _output_tail(out, 30)
    finally:
        mut.restore()


def test_removed_parametrized_instance_is_caught(mirror: Path) -> None:
    """参数实例由 3 减为 2，投喂行声明应有 3 个，因此必须报计数差异。"""
    mut = _Mutation(mirror, CASES)
    try:
        mut.sub(
            '_BAD_ERROR_CODES = ["TIMEOUT_LEGACY", "bogus", ""]',
            '_BAD_ERROR_CODES = ["TIMEOUT_LEGACY", "bogus"]',
        )
        rc, out = _run_check_docs(mirror)
        assert rc == 1
        assert "文档声明 3 个实例，实测 2 个" in out, _output_tail(out, 30)
    finally:
        mut.restore()


# ==========================================================================
# ⑤ 还原纪律：变异必须做到"改坏 → 还原 → 复绿"闭环
# ==========================================================================
def test_mutation_round_trip_leaves_the_mirror_green(mirror: Path) -> None:
    """改坏 → 红灯 → 逐字节还原 → 复跑必须重新变绿。

    没有这一步，"红灯"可能只是镜像被永久污染之后的假象。
    """
    mut = _Mutation(mirror, DOC)
    try:
        mut.sub(
            "投喂行 `test_a_healthy_history_is_unaffected[instances=1,requests=2] -> 200`",
            "投喂行 `test_a_healthy_history_is_unaffected[instances=1,requests=2] -> 500`",
        )
        rc_bad, _ = _run_check_docs(mirror)
        assert rc_bad == 1
    finally:
        mut.restore()
    rc_ok, out = _run_check_docs(mirror)
    assert rc_ok == 0, "还原之后镜像没复绿：\n%s" % _output_tail(out, 30)


def test_check_docs_has_a_stable_green_baseline_in_the_mirror(mirror: Path) -> None:
    """基线本身：未改坏时台账脚本必须 rc=0 且打印对账行。

    如果连基线都绿不了，后面所有"改坏就红"的用例都失去证明力。
    """
    rc, out = _run_check_docs(mirror)
    assert rc == 0, _output_tail(out, 30)
    assert re.search(r"文档与仓库一致", out), _output_tail(out, 30)
    assert re.search(r"行号锚点 \d+ 条", out), _output_tail(out, 30)

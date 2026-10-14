"""核对 `docs/HISTORY_ROBUSTNESS.md` 里的断言与仓库现状是否还对得上。

用法
----
    & ".\\.venv\\Scripts\\python.exe" scripts\\check_docs.py

退出码：`0` = 全部对得上；`1` = 有对不上的条目（逐条打印）。

为什么存在
----------
`docs/HISTORY_ROBUSTNESS.md` 是交接文档，它靠大量「用例 X 钉住了行为 Y」
「`文件.py:NNN` 是表达式 Z」来讲故事。这类断言**无法靠人肉长期复核**：
代码一改，行号漂移、用例改名、断言方向翻转，文档就开始骗人，
而读文档的人（接手人 / 面试官）没有理由怀疑它。本脚本把这些断言变成
**可执行的检查**，让「文档腐烂」变成一次红灯而不是一场无声的事故
（D-22 第 3 条）。

核对了哪些维度
--------------
1. **行号锚点**：文档里 `` `路径:NNN` `` 形式的引用，与代码里**显式标注了行号**
   的那一行比对 —— 代码里写着 ``# routes_workflows.py:207`` 或 ``# :207`` 的注释，
   必须落在 `路径` 的第 `NNN` 行，且该行文本与注释里 `# :NNN` 之后的内容一致。
   （判据：代码是真理来源 ⇒ 行号必须被代码自己锚定。）
2. **用例存在性**：文档里出现或作为行号锚点被代码标为「由 <用例> 覆盖」的每个
   `test_*`，必须真实存在于 `tests/` 里（用 `ast` 收集，不扫文本）。
3. **断言方向**：文档的降级契约写死了若干「注入值 → HTTP 期望值」，本脚本按
   `<用例名> -> <期望值>` 的锚点执行这些用例，核对**每个参数化实例的 HTTP 状态码**
   是否等于期望值。这能抓住「文档说 200、用例已改回 500」。

它**不保证**什么
----------------
- 只核对文档里**写了**的东西。文档漏写的属性（例如某个坏行不再返回、
  某个字段被悄悄丢掉）它一无所知 —— 它防腐烂，不防遗漏。
- 不核对散文：判断性叙述（「为什么修」「影响面多大」）无法机械复核。
- 只要代码里的行号锚点与文档的 `` `文件:NNN` `` 引用**同谋地一起改错**，
  它看不出来（两者一致它就绿）。防的是单边漂移。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC = PROJECT_ROOT / "docs" / "HISTORY_ROBUSTNESS.md"
TESTS_DIR = PROJECT_ROOT / "tests"
CODE_DIRS = ("app", "scripts")

# ---------------------------------------------------------------- 锚点格式 ----
#
# 代码侧（ASCII 星号是硬要求，避免踩到代码里既有的反引号注释）:
#     # **routes_workflows.py:207** `status=_coerce_enum(WorkflowStatus, record.status),`
#     # **:207** `status=_coerce_enum(...)`            <- 只在文件与上一个锚点相同时可省
#
# 文档侧:
#     投喂行 `tests/test_history_robustness.py::<用例名> -> 200`
LINE_ANCHOR_RE = re.compile(r"\*\*(?P<path>[A-Za-z0-9_./]+\.py)?:?(?P<line>\d+)\*\*")
# 反向引用（散文里提到"上面那个 `**:155**`"）不是声明，末尾没有反引号包住的引文。
# 声明形态必须是 `**:NNN** `<引文>``；引文**之后**允许跟 `^{用例覆盖 ...}` 之类的
# 尾注，所以这里不能锚 `$`。
LINE_ANCHOR_DECL_RE = re.compile(
    r"^\s*#?\s*\*\*(?P<path>[A-Za-z0-9_./]+\.py)?:?(?P<line>\d+)\*\*\s+`(?P<quote>.+?)`"
)
CASE_ANCHOR_RE = re.compile(r"^投喂行\s+`(?P<case>test_[A-Za-z0-9_]+)\s*->\s*(?P<status>\d+)`")
DOC_LINE_REF_RE = re.compile(r"`(?P<path>[A-Za-z0-9_./]+\.(?:py|md|txt|js|html)):(?P<line>\d+)`")
DOC_TEST_RE = re.compile(r"\btest_[A-Za-z0-9_]+\b")

# 哪些 `## ` 章节声称自己写的是**当前**行号。只在这些章节里，裸 `` `file.py:NNN` ``
# 才算「漏了锚点声明」；其余章节（缺陷证据链、修复前对照、方法学）里的行号是
# 历史快照，本来就该留在过去，抓它们只会把信号淹掉。
_CURRENT_SECTION_MARKERS = ("修复状态", "行号锚点", "当前")


@dataclass
class Problem:
    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 纯输出
        return f"[{self.kind}] {self.detail}"


@dataclass
class LineAnchor:
    path: str
    line: int
    text: str
    cases: tuple[str, ...]
    source: str  # "<file>:<行>" 用于报错定位


@dataclass
class CaseAnchor:
    case: str
    status: int
    source: str
    payload: str = ""


def _iter_py_files() -> list[Path]:
    files: list[Path] = []
    for rel in CODE_DIRS:
        directory = PROJECT_ROOT / rel
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.py")))
    return files


def _relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def collect_line_anchors(problems: list[Problem]) -> list[LineAnchor]:
    """从代码注释里收集行号锚点，并校验锚点自身是否成立。"""
    anchors: list[LineAnchor] = []
    for file in _iter_py_files():
        rel = _relative(file)
        if rel.startswith("scripts/"):
            continue  # 本脚本自己的 docstring 里有示例锚点，别自证
        lines = file.read_text(encoding="utf-8").splitlines()
        last_path: str | None = rel  # 省略式 `**:NNN**` 默认指本文件
        for index, line in enumerate(lines, start=1):
            if "#" not in line:
                continue
            comment = line.split("#", 1)[1].rstrip()
            # 只认**声明**形态：`**:NNN** `<引文>``。散文里的反向引用
            # （"上面那个 `**:155**` 被文档引用"）没有引文，忽略掉，
            # 否则它会被当成一条空锚点，报出假的「行号漂移」。
            if LINE_ANCHOR_DECL_RE.match(comment) is None:
                continue
            declaration = LINE_ANCHOR_DECL_RE.match(comment)
            assert declaration is not None  # 上面已判过
            match = LINE_ANCHOR_RE.search(comment)
            if match is None:
                continue
            declared = match.group("path")
            target_rel = declared or last_path
            if target_rel is None:
                problems.append(
                    Problem("锚点", f"{rel}:{index} 用了省略式 `**:NNN**` 但没有可继承的文件名")
                )
                continue
            last_path = target_rel
            expected_line = int(match.group("line"))
            # 引文取自**声明正则**捕获的 `...` 内容。不能从 LINE_ANCHOR_RE 的
            # match.end() 往后切 —— 那个正则只吃到 `**` 为止，会把反引号包着的
            # 引文连同后面的 `^{用例覆盖 ...}` 尾注一起当成"引文"，报假漂移。
            rest = declaration.group("quote").strip()
            target = PROJECT_ROOT / target_rel
            if not target.is_file():
                problems.append(
                    Problem("锚点", f"{rel}:{index} 指向不存在的文件 `{target_rel}`")
                )
                continue
            target_lines = target.read_text(encoding="utf-8").splitlines()
            if expected_line < 1 or expected_line > len(target_lines):
                problems.append(
                    Problem(
                        "行号漂移",
                        f"{rel}:{index} 声明 `{target_rel}:{expected_line}`，"
                        f"但该文件只有 {len(target_lines)} 行",
                    )
                )
                continue
            actual = target_lines[expected_line - 1].strip()
            if rest and rest != actual:
                problems.append(
                    Problem(
                        "行号漂移",
                        f"{rel}:{index} 声明 `{target_rel}:{expected_line}` 是 {rest!r}，"
                        f"实际是 {actual!r}",
                    )
                )
                continue
            cases = tuple(re.findall(r"<([A-Za-z0-9_]+)>", comment[match.end():]))
            anchors.append(
                LineAnchor(
                    path=target_rel,
                    line=expected_line,
                    text=actual,
                    cases=cases,
                    source=f"{rel}:{index}",
                )
            )
    return anchors


def _in_current_block(lines: list[str], index: int) -> bool:
    """判断第 index 行（1-based）是否落在「声称是当前行号」的章节里。

    判据取**章节标题**而不是逐行猜意图：一个章节要么整体在讲"现在的代码长什么样"
    （那它里的裸行号必须有人核对），要么整体在讲历史快照（那些数字就该留在过去）。
    区块从匹配的 `## ` 标题开始，到下一个 `## ` 标题为止。
    """
    current_owner = -1
    for line_no, line in enumerate(lines[:index], start=1):
        if not line.startswith("## "):
            continue
        current_owner = line_no if any(m in line for m in _CURRENT_SECTION_MARKERS) else -1
    return current_owner != -1


def collect_doc_anchors(problems: list[Problem]) -> tuple[list[LineAnchor], list[CaseAnchor]]:
    text = DOC.read_text(encoding="utf-8")
    lines = text.splitlines()

    line_anchors: list[LineAnchor] = []
    case_anchors: list[CaseAnchor] = []
    last_path: str | None = None
    for index, line in enumerate(lines, start=1):
        if "投喂行" in line:
            match = CASE_ANCHOR_RE.match(line.strip())
            if match is None:
                problems.append(
                    Problem("格式", f"docs/HISTORY_ROBUSTNESS.md:{index} 投喂行不符合 "
                                    f"`投喂行 `用例名 -> 200`` 格式：{line.strip()!r}")
                )
                continue
            case_anchors.append(
                CaseAnchor(
                    case=match.group("case"),
                    status=int(match.group("status")),
                    source=f"docs/HISTORY_ROBUSTNESS.md:{index}",
                )
            )
            continue

        stripped = line.strip()
        m = re.match(r"^行号锚点\s+`(?P<body>.+)`$", stripped)
        if m is not None:
            body = m.group("body")
            ref = re.match(r"^(?P<path>[A-Za-z0-9_./]+\.py):(?P<line>\d+)$", body)
            if ref is not None:
                last_path = ref.group("path")
                line_anchors.append(
                    LineAnchor(
                        path=body.split(":")[0],
                        line=int(ref.group("line")),
                        text="",
                        cases=(),
                        source=f"docs/HISTORY_ROBUSTNESS.md:{index}",
                    )
                )
                continue
            ref = re.match(r"^:(?P<line>\d+)v?(?P<rest>.*)$", body)
            if ref is not None and last_path is not None:
                rest = ref.group("rest").strip().strip("`").strip()
                line_anchors.append(
                    LineAnchor(
                        path=last_path,
                        line=int(ref.group("line")),
                        text=rest,
                        cases=(),
                        source=f"docs/HISTORY_ROBUSTNESS.md:{index}",
                    )
                )
                continue
            problems.append(
                Problem("格式", f"docs/HISTORY_ROBUSTNESS.md:{index} 行号锚点无法解析：{body!r}")
            )
            continue

        # 裸 `` `文件.py:NNN` `` 只在一个前提下才算「该被锚点管住」：
        # 它出现在本文写作时**声称是当前行号**的区块里（见下方 _CURRENT_MARKERS）。
        # 历史快照（"修复前 :154 是硬转"）里的数字本来就该留在过去，不该被抓。
        if _in_current_block(lines, index):
            for ref in DOC_LINE_REF_RE.finditer(line):
                if ref.group("path").endswith(".md"):
                    continue
                problems.append(
                    Problem(
                        "裸行号引用",
                        f"docs/HISTORY_ROBUSTNESS.md:{index} 在「当前行号」区块里出现 "
                        f"`{ref.group('path')}:{ref.group('line')}`，但它没有对应的"
                        f"「行号锚点」声明 —— 这类裸引用会随代码漂移且无人核对",
                    )
                )
    return line_anchors, case_anchors


def collect_test_cases() -> dict[str, ast.FunctionDef]:
    cases: dict[str, ast.FunctionDef] = {}
    for file in sorted(TESTS_DIR.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                cases[node.name] = node
    return cases


def _load_case_results(cases: list[str]) -> dict[str, dict[str, int]]:
    """跑一遍用例，返回 {用例名 -> {参数化 id 或 '': 状态码}}。

    实现在测试文件里的 `_record_acceptance_status`（用 monkeypatch 挂钩 TestClient）。
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from tests import test_history_robustness as module  # noqa: PLC0415

    total = len(cases)
    results: dict[str, dict[str, int]] = {}
    for index, case in enumerate(cases, start=1):
        print(f"  投喂 {index}/{total}: {case}")
        fn = getattr(module, case)
        results[case] = module._record_acceptance_status(fn)
    return results


def main() -> int:
    problems: list[Problem] = []

    if not DOC.is_file():
        print(f"[致命] 找不到 {DOC}")
        return 1

    code_anchors = collect_line_anchors(problems)
    doc_anchors, case_anchors = collect_doc_anchors(problems)
    code_by_ref = {(a.path, a.line): a for a in code_anchors}
    all_cases = collect_test_cases()

    # ---- 维度 1：文档的行号锚点 == 代码的行号锚点 -------------------------
    matched = 0
    for anchor in doc_anchors:
        expected = code_by_ref.get((anchor.path, anchor.line))
        if expected is None:
            problems.append(
                Problem(
                    "行号锚点",
                    f"{anchor.source} 声明 `{anchor.path}:{anchor.line}`，"
                    f"但代码里没有任何注释锚定这一行",
                )
            )
            continue
        if anchor.text and anchor.text.rstrip("…") not in expected.text:
            problems.append(
                Problem(
                    "行号锚点",
                    f"{anchor.source} 声称 `{anchor.path}:{anchor.line}` 是 {anchor.text!r}，"
                    f"实际是 {expected.text!r}",
                )
            )
            continue
        matched += 1

    # ---- 维度 2：用例存在性 ---------------------------------------------
    for anchor in doc_anchors:
        for case in code_by_ref.get((anchor.path, anchor.line), LineAnchor("", 0, "", (), "")).cases:
            if case not in all_cases:
                problems.append(
                    Problem("用例名", f"{anchor.source} 声称该行由 `{case}` 覆盖，但用例不存在")
                )
    for anchor in case_anchors:
        if anchor.case not in all_cases:
            problems.append(Problem("用例名", f"{anchor.source} 找不到用例 `{anchor.case}`"))

    named_in_doc = set(DOC_TEST_RE.findall(DOC.read_text(encoding="utf-8")))
    for case in sorted(named_in_doc):
        if case not in all_cases and not case.startswith("test_"):
            problems.append(Problem("用例名", f"文档提到 `{case}`，但用例不存在"))

    # ---- 维度 3：断言方向 ------------------------------------------------
    observable = [a for a in case_anchors if a.case in all_cases]
    checked_instances = 0
    if observable:
        print(f"执行 {len(observable)} 个用例核对断言方向……")
        results = _load_case_results(sorted({a.case for a in observable}))
        for anchor in observable:
            instances = results.get(anchor.case)
            if not instances:
                problems.append(
                    Problem("断言方向", f"{anchor.source} 用例 `{anchor.case}` 没有记录到任何请求")
                )
                continue
            for param, status in sorted(instances.items()):
                label = anchor.case + (f"[{param}]" if param else "")
                if status != anchor.status:
                    problems.append(
                        Problem(
                            "断言方向",
                            f"{anchor.source} 文档说 `{label}` 期望 {anchor.status}，"
                            f"实测 {status}",
                        )
                    )
                else:
                    checked_instances += 1

    # ---- 报告 ------------------------------------------------------------
    print()
    print("=" * 68)
    if problems:
        print(f"不一致 {len(problems)} 条：")
        for problem in problems:
            print(f"  {problem}")
        print("=" * 68)
        return 1
    print(
        f"文档与仓库一致：行号锚点 {matched} 条（代码锚点 {len(code_anchors)} 个）、"
        f"用例 {len(named_in_doc)} 个全部存在、断言方向 {checked_instances} 个参数化实例"
    )
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

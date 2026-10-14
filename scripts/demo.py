"""端到端演示：真 uvicorn + 真 HTTP + 真出图（一条命令跑完，跑完自己关服务）。

跑法（在仓库根，任何 cwd 都行）：

    & ".\\venv\\Scripts\\python.exe" scripts\\demo.py        # Windows PowerShell
    ./.venv/bin/python scripts/demo.py                       # Linux/macOS

它做什么：
    自己用 `subprocess.Popen` 起一个真 uvicorn（端口 8128，**不用 8000**），
    然后**只走真实 HTTP**（httpx）跑完九段：健康检查 → 首页 → 上传 → 提问 →
    每秒轮询状态 → 终态 → 下载图表 PNG → 取报告 → 数字校验 → **纯 stdlib csv
    手算六地区销售额对账**。最后在 `finally` 里 `terminate()` + `wait()`，
    并且自查端口是否已经没人监听。

它**不 import `app.*`**：直接调函数就不叫端到端了，也验不到路由、状态码、
响应头和静态挂载这些真正会出错的地方。

两条入口（都不配 key 也能跑）：
    - 没配 key：`llm_configured=false`，规划走规则兜底，报告 `llm_used=false`
    - 配了 key：规划走模型（`plan.source="llm"`），报告 `llm_used=true`

本脚本的三条"别重犯"（都是真踩过的坑）：
    1. 接口 6 的 `tasks[]` 是 `TaskBrief`，**没有 `tool_name`** —— 只能读
       `task_id` / `description` / `status` / `error`。
    2. 接口 7 返回的是 `ReportData` 外壳，不是裸报告 —— 必须
       `data["report"]` / `data["validation"]` / `data["results"]`。
    3. PNG 魔数必须用**真字节**常量比较。曾经在 f-string 里写
       `{head[:4] == b'\\\\x89PNG'}`：那是 7 个字符的转义文本，比较恒为 False，
       而同一行打印出来的魔数又是对的 —— 探针说谎的样子和真问题一模一样。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)
PORT = 8128
BASE = f"http://127.0.0.1:{PORT}"
SAMPLE = ROOT / "data" / "samples" / "sales.csv"
LOG = ROOT / ".tmp" / "demo_server_8128.log"
TERMINAL = {"SUCCESS", "SUCCESS_WITH_WARNINGS", "FAILED"}
POLL_SECONDS = 1.0
POLL_BUDGET_SECONDS = 180.0

# 真字节头（不是字符串里的转义序列）。见模块 docstring 第 3 条。
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def say(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}", flush=True)


def die(message: str, response: httpx.Response | None = None) -> None:
    """失败要响：把原始响应体打出来再抛，别静默跳过。"""
    print(f"\n!! 失败：{message}", flush=True)
    if response is not None:
        print(f"   {response.request.method} {response.request.url} -> {response.status_code}")
        print(f"   响应体（前 800 字符）：{response.text[:800]}")
    raise SystemExit(1)


def masked(name: str, value: str) -> str:
    """**绝不打印 key 的值** —— 只给长度和前缀。"""
    if not value:
        return "未设置"
    return f"len={len(value)} 前缀={value[:3]!r}"


def port_is_listening(port: int) -> bool:
    """端口上有没有服务在监听。

    判据用 `create_connection` + 明确的异常分支，**不用 `connect_ex` 的返回值**：
    `settimeout()` 之后 socket 是非阻塞的，Windows 上"连接正在进行"会返回
    WSAEWOULDBLOCK（10035），把它当成"有人在监听"会误报 —— 本脚本第一版
    在 `finally` 里就这么误报过一次（真实情况是服务已经退了、端口是空的）。
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_port_free(port: int, seconds: float = 5.0) -> bool:
    """等服务真把端口让出来（内核回收监听 socket 有一瞬间的延迟）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not port_is_listening(port):
            return True
        time.sleep(0.25)
    return not port_is_listening(port)


def api(body: dict, what: str) -> dict:
    """统一拆 `{ok, data, error}` 外壳，`ok=false` 就响。"""
    if not body.get("ok"):
        die(f"{what} 返回 ok=false：{json.dumps(body, ensure_ascii=False)[:400]}")
    return body["data"]


def region_totals(path: Path) -> dict[str, int]:
    """纯 stdlib `csv` 手算每个地区的销售额合计（不经 pandas、不经工具函数）。

    这样才对得上"报告里的数字到底是不是从原始数据算出来的"。
    空销售额的行跳过（与工具的一致口径）。
    """
    totals: dict[str, int] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("销售额") or "").strip()
            if not raw:
                continue
            region = (row.get("地区") or "").strip()
            totals[region] = totals.get(region, 0) + int(float(raw))
    return totals


def find_region_table(sections: list[dict]) -> tuple[str, dict[str, float]]:
    """在报告的段落表格里找"地区 → 数值"那张表。

    返回 (段落标题, {地区: 数值})；找不到就返回 ("", {})，由调用方决定怎么响。
    """
    for section in sections:
        table = section.get("table") or []
        rows: dict[str, float] = {}
        for row in table:
            keys = list(row.keys())
            region_key = next((k for k in keys if "地区" in k or "区域" in k or "region" in k.lower()), None)
            if region_key is None or len(keys) < 2:
                continue
            value_key = next(
                (k for k in keys if k != region_key and isinstance(row[k], (int, float))),
                None,
            )
            if value_key is None:
                continue
            rows[str(row[region_key])] = float(row[value_key])
        if rows:
            return str(section.get("heading", "")), rows
    return "", {}


def main() -> int:
    if not SAMPLE.is_file():
        die(f"示例数据不存在：{SAMPLE}")
    if port_is_listening(PORT):
        die(f"端口 {PORT} 已经有服务在监听。先关掉它再跑（本脚本不会去杀别人的进程）。")

    key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL") or ""
    model = os.environ.get("LLM_MODEL") or os.environ.get("DEEPSEEK_MODEL") or ""

    say("准备：环境与进程")
    print(f"仓库根      : {ROOT}")
    print(f"解释器      : {PY}")
    print(f"示例数据    : {SAMPLE}（{SAMPLE.stat().st_size} 字节）")
    print(f"端口        : {PORT}")
    print("大模型三项（**只看长度与前缀，不打印值**）：")
    print(f"  LLM_API_KEY / DEEPSEEK_API_KEY      : {masked('key', key)}")
    print(f"  LLM_BASE_URL / DEEPSEEK_BASE_URL    : {masked('base_url', base_url)}")
    print(f"  LLM_MODEL / DEEPSEEK_MODEL          : {masked('model', model)}")
    print("  说明：本项目**不读 .env**，只认上面这些环境变量（`app/config.py`）。")
    print(f"  预期入口：{'模型规划（key 已配置）' if (key and base_url) else '规则兜底（未配置 key / base_url）'}")

    LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG, "w", encoding="utf-8", newline="")
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    print(f"已启动 uvicorn（pid={proc.pid}），日志 = {LOG}")

    try:
        with httpx.Client(base_url=BASE, timeout=60.0) as client:
            # ---------------- 0. 健康检查 ----------------
            say("0. 等服务起来 + GET /api/health")
            health: dict = {}
            for attempt in range(60):
                if proc.poll() is not None:
                    die(f"uvicorn 提前退出（exit={proc.returncode}），日志见 {LOG}")
                try:
                    r = client.get("/api/health")
                except httpx.TransportError:
                    time.sleep(0.5)
                    continue
                if r.status_code == 200:
                    print(f"第 {attempt + 1} 次探测就绪（间隔 0.5s）")
                    print(f"GET /api/health -> {r.status_code}")
                    print(json.dumps(r.json(), ensure_ascii=False, indent=2))
                    health = api(r.json(), "接口 health")
                    break
                time.sleep(0.5)
            else:
                die(f"30 秒内 /api/health 没返回 200，日志见 {LOG}")
            if not isinstance(health.get("llm_configured"), bool):
                die("健康检查里没有布尔型 llm_configured 字段")

            # ---------------- 1. 首页是页面 ----------------
            say("1. GET / 是真页面（不是 JSON 提示），静态资源与 data/ 未挂载")
            r = client.get("/")
            print(f"GET / -> {r.status_code}  content-type={r.headers.get('content-type')}")
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                die("GET / 不是 200 + text/html", r)
            body = r.text
            for token in ('id="question"', 'id="charts"', 'id="report"', 'id="steps"', "charset"):
                hit = token in body
                print(f"  body 含 {token!r}: {hit}")
                if not hit:
                    die(f"首页缺少 {token!r}")
            print(f"  首页长度 = {len(body)} 字符")
            r_js = client.get("/static/app.js")
            print(f"GET /static/app.js -> {r_js.status_code}  ct={r_js.headers.get('content-type')}  len={len(r_js.text)}")
            r_css = client.get("/static/app.css")
            print(f"GET /static/app.css -> {r_css.status_code}  ct={r_css.headers.get('content-type')}")
            for probe in ("/data/samples/sales.csv", "/data/uploads/"):
                r_data = client.get(probe)
                print(f"GET {probe} -> {r_data.status_code}（必须不是 200：data/ 没被挂成静态资源）")
                if r_data.status_code == 200:
                    die(f"{probe} 竟然是 200 —— data/ 被暴露了", r_data)

            # ---------------- 2. 上传 ----------------
            say("2. 上传 data/samples/sales.csv（真 HTTP multipart）")
            raw = SAMPLE.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            r = client.post("/api/datasets/upload", files={"file": ("sales.csv", raw, "text/csv")})
            print(f"POST /api/datasets/upload -> {r.status_code}（本地 {len(raw)} 字节）")
            if r.status_code != 200:
                die("上传失败", r)
            ds = api(r.json(), "上传")
            dataset_id = ds["dataset_id"]
            print(f"  dataset_id      = {dataset_id}")
            print(f"  行 × 列          = {ds['row_count']} × {ds['column_count']}")
            print(f"  stored_filename = {ds['stored_filename']}")
            print(f"  sha256          = {ds['sha256'][:16]}…  与本地重算一致 = {ds['sha256'] == digest}")
            if ds["sha256"] != digest:
                die("服务端 sha256 与本地重算不一致")
            if (ds["row_count"], ds["column_count"]) != (2168, 9):
                die(f"示例数据的行列数不是 2168×9，而是 {ds['row_count']}×{ds['column_count']}")

            r = client.get(f"/api/datasets/{dataset_id}/profile")
            prof = api(r.json(), "概览")
            print(f"GET /api/datasets/{{id}}/profile -> {r.status_code}  列数={len(prof['columns'])}  重复行={prof['duplicate_row_count']}")
            for col in prof["columns"]:
                print(f"    {col['name']:<6} {col['semantic_type']:<12} null={col['null_count']:<5} unique={col['unique_count']}")

            # ---------------- 3. 提问 ----------------
            say("3. POST /api/analysis（202 + 立刻返回 workflow_id）")
            question = "各地区的销售额汇总，并看一下销售额和利润的关系"
            print(f"question = {question!r}")
            r = client.post("/api/analysis", json={"dataset_id": dataset_id, "question": question})
            print(f"POST /api/analysis -> {r.status_code}（期望 202）")
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            if r.status_code != 202:
                die("提交分析没有返回 202", r)
            workflow_id = api(r.json(), "提交分析")["workflow_id"]

            # ---------------- 4/5. 轮询到终态 ----------------
            say("4. 每秒轮询 GET /api/workflows/{id}（每次的 step / step_index 都打印）")
            started = time.monotonic()
            transitions: list[tuple[float, str, str]] = []
            final: dict = {}
            last_step = None
            while True:
                r = client.get(f"/api/workflows/{workflow_id}")
                if r.status_code != 200:
                    die("轮询状态接口没有返回 200", r)
                data = api(r.json(), "轮询")
                elapsed = time.monotonic() - started
                step, status = data.get("step"), data.get("status")
                print(
                    f"  +{elapsed:6.2f}s  step={step:<10} step_index={data.get('step_index')}/{data.get('total_steps')}"
                    f"  status={status}"
                )
                if step != last_step:
                    transitions.append((elapsed, str(step), str(status)))
                    last_step = step
                print(f"            has_report={data.get('has_report')}  charts={len(data.get('charts') or [])}")
                if status in TERMINAL:
                    final = data
                    break
                if elapsed > POLL_BUDGET_SECONDS:
                    die(f"{POLL_BUDGET_SECONDS:.0f} 秒还没进终态")
                time.sleep(POLL_SECONDS)

            say("5. 终态详情（接口 6 的字段形状）")
            total = time.monotonic() - started
            print(f"  总耗时         = {total:.2f}s")
            print(f"  status         = {final['status']}")
            print(f"  step / index   = {final['step']} / {final['step_index']} of {final['total_steps']}")
            print(f"  has_report     = {final['has_report']}")
            print(f"  charts         = {len(final['charts'])} 张")
            print(f"  error          = {final.get('error')}")
            print(f"  接口 6 顶层字段 = {sorted(final.keys())}")
            expected_status_fields = {
                "workflow_id",
                "dataset_id",
                "question",
                "status",
                "step",
                "step_index",
                "total_steps",
                "tasks",
                "charts",
                "error",
                "created_at",
                "finished_at",
                "has_report",
            }
            if set(final.keys()) != expected_status_fields:
                die(f"接口 6 字段集合与契约不符：多 {sorted(set(final) - expected_status_fields)}，少 {sorted(expected_status_fields - set(final))}")
            print("  step 变化序列：")
            for elapsed, step, status in transitions:
                print(f"    +{elapsed:6.2f}s  {step}  ({status})")
            print("  任务列表（TaskBrief：task_id / description / status / error，**没有 tool_name**）：")
            for task in final["tasks"]:
                if set(task.keys()) != {"task_id", "description", "status", "error"}:
                    die(f"TaskBrief 字段集合变了：{sorted(task.keys())}（按 t['tool_name'] 取值会 KeyError）")
                print(f"    {task['task_id']}  {task['status']:<12} {task['description'][:52]}")
                if task.get("error"):
                    print(f"        error = {task['error']}")
            if final["status"] == "FAILED":
                die(f"工作流失败：{json.dumps(final.get('error'), ensure_ascii=False)}")
            if "report" in final or "results" in final:
                die("接口 6 的响应里出现了 report / results —— 轮询接口不该带大字段")
            if not final["has_report"]:
                die("终态却没有报告（has_report=false）")

            # ---------------- 6. 图表 ----------------
            say("6. 图表：接口 8 真下载 PNG（content-type + 字节数 + 真字节魔数）")
            if not final["charts"]:
                die("没有图表可下载")
            for chart in final["charts"]:
                r = client.get(chart["url"])
                head = r.content[:8]
                # 真字节比较：head 是 bytes，PNG_MAGIC 也是 bytes 常量（见模块 docstring 第 3 条）。
                is_png = head == PNG_MAGIC
                print(f"  {chart['chart_id']}  {chart['chart_type']:<5} {chart['title'][:36]}")
                print(f"    url         = {chart['url']}")
                print(f"    -> {r.status_code}  ct={r.headers.get('content-type')}  bytes={len(r.content)}")
                print(f"    magic       = {head!r}  PNG={is_png}")
                if r.status_code != 200:
                    die("图表没有返回 200", r)
                if "image/png" not in r.headers.get("content-type", ""):
                    die(f"图表 content-type 不是 image/png：{r.headers.get('content-type')}")
                if not is_png or len(r.content) < 1000:
                    die(f"图表不是有效 PNG（magic={head!r}，{len(r.content)} 字节）")

            # ---------------- 7. 报告 ----------------
            say("7. 报告：GET /api/workflows/{id}/report（返回的是 ReportData 外壳）")
            r = client.get(f"/api/workflows/{workflow_id}/report")
            print(f"-> {r.status_code}")
            if r.status_code != 200:
                die("报告接口没有返回 200", r)
            payload = api(r.json(), "报告")
            print(f"  接口 7 顶层字段 = {sorted(payload.keys())}（必须是 report / validation / results / llm_used）")
            if set(payload.keys()) != {"report", "validation", "results", "llm_used"}:
                die("接口 7 的字段形状变了")
            report = payload["report"]
            print(f"  llm_used          = {payload['llm_used']}（外壳）  report.llm_used = {report['llm_used']}（报告）")
            if payload["llm_used"] != report["llm_used"]:
                die("接口 7 的 llm_used 与 report.llm_used 不一致")
            print(f"  report_id         = {report['report_id']}")
            print(f"  title             = {report['title']}")
            print(f"  段落数            = {len(report['sections'])}")
            print(f"  validation 是否为 None = {payload['validation'] is None}")
            print(f"  results 条数      = {len(payload['results'])}")
            for res in payload["results"]:
                print(
                    f"    {res['task_id']}  ok={res['ok']}  tool={res['tool_name']:<16}"
                    f" rows={res['row_count']:<5} {res['elapsed_ms']}ms"
                    f"  data 键={sorted(res['data'].keys())}  错误={res['error_code']}"
                )
            print(f"\n  summary:\n    {report['summary']}")
            for section in report["sections"]:
                print(f"\n  --- {section['heading']}  cites={section['cites']} ---")
                print(f"    {section['text']}")
                table = section.get("table") or []
                if table:
                    print(f"    table: {len(table)} 行，列={list(table[0].keys())}")
                    for row in table[:5]:
                        print(f"      {row}")
                    if len(table) > 5:
                        print(f"      ……（共 {len(table)} 行，演示只打前 5 行）")
            print("\n  data_quality_notes:")
            for note in report["data_quality_notes"]:
                print(f"    - {note}")

            # ---------------- 8. 数字校验 ----------------
            say("8. 数字校验（接口 7 的 validation 字段）")
            validation = payload["validation"]
            if validation is None:
                die("validation 是 None —— 报告没有经过校验")
            print(json.dumps(validation, ensure_ascii=False, indent=2))
            print(f"  passed             = {validation['passed']}")
            print(f"  checked_numbers    = {validation['checked_numbers']}")
            print(f"  unsupported_numbers= {validation['unsupported_numbers']}")
            print(f"  issues             = {len(validation['issues'])} 条")
            if validation["passed"] and validation["unsupported_numbers"]:
                die("passed=true 却列出了不支持的数字")
            if validation["checked_numbers"] <= 0:
                die("checked_numbers 为 0 —— 校验其实没核对任何数字")

            # ---------------- 9. 纯 stdlib 手算对账 ----------------
            say("9. 对账：报告里的六地区销售额 vs 纯 stdlib csv 手算（零容差）")
            mine = region_totals(SAMPLE)
            print("  我自己用 csv 模块手算（跳过空销售额行）：")
            for region, value in sorted(mine.items(), key=lambda kv: -kv[1]):
                print(f"    {region:<4} {value:>12,}")
            print(f"    {'合计':<4} {sum(mine.values()):>12,}")
            heading, reported = find_region_table(report["sections"])
            if not reported:
                die("报告的段落表格里找不到「地区 → 数值」的表，无法对账")
            print(f"\n  报告里那张表：{heading!r}")
            print(f"  {'地区':<6}{'报告值':>14}{'我算的':>14}   一致")
            for region in sorted(mine, key=lambda k: -mine[k]):
                got = reported.get(region)
                print(f"  {region:<6}{('—' if got is None else f'{got:,.0f}'):>14}{mine[region]:>14,}   {'✔' if got == mine[region] else '✘'}")
            extra = sorted(set(reported) - set(mine))
            missing = sorted(set(mine) - set(reported))
            print(f"\n  报告多出的地区 = {extra}    报告缺的地区 = {missing}")
            if missing or extra:
                die(f"地区集合不一致：缺 {missing}，多 {extra}")
            if len(mine) != 6:
                die(f"示例数据的地区数不是 6，而是 {len(mine)}：{sorted(mine)}")
            mismatched = {k: (reported[k], v) for k, v in mine.items() if reported[k] != v}
            if mismatched:
                die(f"零容差对账失败：{mismatched}")
            print(f"  ✔ 六地区逐个相等（零容差），合计 {sum(mine.values()):,}")

            say("演示结束：九段全部通过")
            print(f"  workflow_id = {workflow_id}")
            print(f"  dataset_id  = {dataset_id}")
            print(f"  llm_configured = {health['llm_configured']}   llm_used = {payload['llm_used']}")
            print(f"  端到端耗时  = {time.monotonic() - started:.2f}s（不含服务启动）")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        log.close()
        stopped = wait_port_free(PORT)
        print(f"\n[服务已停] uvicorn pid={proc.pid} exit={proc.returncode}  端口 {PORT} 已让出 = {stopped}")
        if not stopped:
            print(f"!! 端口 {PORT} 还有进程在监听，去看日志 {LOG}")


if __name__ == "__main__":
    sys.exit(main())

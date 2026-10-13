"""进程级运行时状态。

目前只有"启动时刻"一个值。以后工作流的内存态索引也会放这里。
单独建文件的理由：main.py 和路由都需要它，放在任何一边都会造成
"路由反过来依赖 main"的别扭结构。
"""

import time

# 模块第一次被导入的时刻 ≈ 服务启动时刻
STARTED_AT: float = time.time()


def uptime_seconds() -> float:
    """服务已运行的秒数。"""
    return round(time.time() - STARTED_AT, 2)

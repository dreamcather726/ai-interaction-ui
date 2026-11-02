"""将运行消息立即输出到启动终端，便于观察后台线程与子进程。"""

import threading
from datetime import datetime

_OUTPUT_LOCK = threading.Lock()


def console_log(message):
    """整条消息加时间输出；管道重定向时也不等待缓冲区填满。"""
    with _OUTPUT_LOCK:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"[{stamp}] [工作台] {message}", flush=True)

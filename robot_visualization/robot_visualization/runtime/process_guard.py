"""Linux 节点启动保护：工作台异常退出时，清理本节点的整个进程组。"""

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _has_group_children(group):
    """排除自身和僵尸进程，检查 ros2 包装进程退出后是否还有节点存活。"""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            # comm 字段可能含空格或括号，从最后一个右括号后解析固定字段。
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] not in ("Z", "X") and int(fields[2]) == group:
                return True
        except (OSError, IndexError, ValueError):
            continue
    return False


def supervise(command, parent_pid):
    """在独立会话内启动一个节点；父线程死亡或收到退出信号时逐级清理。"""
    if not sys.platform.startswith("linux") or os.getpgrp() != os.getpid():
        raise RuntimeError("节点保护进程必须由工作台在 Linux 独立会话内启动")

    stopping = False

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
        signal.signal(sig, request_stop)

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    libc.prctl.restype = ctypes.c_int
    # PR_SET_PDEATHSIG；与创建本进程的工作线程绑定，原生崩溃也能触发。
    if libc.prctl(1, signal.SIGUSR1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    # 补上父进程在 prctl 注册前已经退出的时间窗口。
    if stopping or os.getppid() != parent_pid:
        return 1

    child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    group = os.getpgrp()
    stages = ((signal.SIGINT, 3.0), (signal.SIGTERM, 1.0), (signal.SIGKILL, 0.3))
    stage = 0
    deadline = 0.0
    try:
        while True:
            child_running = child.poll() is None
            if not child_running and not _has_group_children(group):
                return child.returncode
            if not child_running:
                stopping = True
            now = time.monotonic()
            if stopping and now >= deadline and stage < len(stages):
                sig, timeout = stages[stage]
                stage += 1
                deadline = now + timeout
                os.killpg(group, sig)
            time.sleep(0.05)
    finally:
        # Python 异常同样不能遗留节点；SIGKILL 会连本保护进程一起结束。
        if child.poll() is None or _has_group_children(group):
            os.killpg(group, signal.SIGKILL)


def main():
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        raise SystemExit("usage: process_guard PARENT_PID -- COMMAND [ARG ...]")
    raise SystemExit(supervise(sys.argv[3:], int(sys.argv[1])))


if __name__ == "__main__":
    main()

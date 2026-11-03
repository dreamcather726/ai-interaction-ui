"""ROS 可执行入口：一个命令启动工作台与现有语音处理链路。"""

import faulthandler
import inspect
import signal
import sys

import rclpy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from robot_visualization.ros.worker import RosWorker
from robot_visualization.runtime.console import console_log
from robot_visualization.ui.theme import configure_font
from robot_visualization.ui.window import DashboardWindow


def build_dashboard():
    """装配窗口与通信线程，供实际入口和 Qt 交互验收共用。"""
    worker = RosWorker()
    window = DashboardWindow(worker.config)
    # GUI 接收方法必须在主线程排队执行，避免关闭窗口时跨线程访问 Qt 控件。
    queued = Qt.ConnectionType.QueuedConnection
    worker.record.connect(window.receive_record, queued)
    worker.availability.connect(window.receive_availability, queued)
    worker.control.connect(window.control_result, queued)
    worker.log.connect(window.append_log, queued)
    worker.draft.connect(window.receive_draft, queued)
    worker.catalog.connect(window.receive_catalog, queued)
    window.catalog_requested.connect(
        lambda operation, value: worker.command("catalog_" + operation, value)
    )
    window.draft_requested.connect(
        lambda operation, value: worker.command("draft_" + operation, value)
    )
    window.start_requested.connect(lambda: worker.command("start"))
    window.stop_requested.connect(lambda: worker.command("stop"))
    window.query_requested.connect(lambda value: worker.command("query", value))
    window.shutdown_requested.connect(
        lambda: (
            worker.requestInterruption()
            if worker.isRunning()
            else window.finish_close()
        )
    )
    worker.finished.connect(window.on_worker_finished, queued)
    return worker, window


def main(args=None):
    # 原生崩溃不能由 try/except 捕获；保留所有 Python 线程的现场供实际环境定位。
    if not faulthandler.is_enabled():
        faulthandler.enable(all_threads=True)
    console_log(f"正在启动语音工作台；Python={sys.executable}")
    console_log(f"界面代码：{inspect.getfile(DashboardWindow)}")
    console_log(
        "界面布局："
        + getattr(DashboardWindow, "LAYOUT_DESCRIPTION", "未标识，请核对是否加载旧版本")
    )
    rclpy.init(args=args)
    app = QApplication([sys.argv[0]])
    configure_font(app)
    worker, window = build_dashboard()
    # Qt 主线程接管窗口退出；工作线程完成清理后再销毁 ROS 上下文。
    signal.signal(signal.SIGINT, lambda signum, frame: window.close())
    signal.signal(signal.SIGTERM, lambda signum, frame: window.close())
    window.showFullScreen()
    console_log(
        "工作台窗口已全屏打开（F11 切换全屏，Esc 退出），正在启动后台；等待设备与 ASR 模型就绪"
    )
    worker.start()
    try:
        app.exec()
    finally:
        worker.requestInterruption()
        worker.wait()
        if rclpy.ok():
            rclpy.shutdown()
        console_log("语音工作台已关闭")

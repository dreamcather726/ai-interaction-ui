"""Read local interface state only; never ping or infer remote connectivity."""

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface


def read_active_interfaces():
    interfaces = []
    flags = QNetworkInterface.InterfaceFlag
    for interface in QNetworkInterface.allInterfaces():
        state = interface.flags()
        if (
            state & flags.IsLoopBack
            or not state & flags.IsUp
            or not state & flags.IsRunning
        ):
            continue
        addresses = [entry.ip() for entry in interface.addressEntries()]
        addresses = [
            address
            for address in addresses
            if not address.isNull() and not address.isLoopback()
        ]
        addresses.sort(
            key=lambda address: address.protocol()
            != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol
        )
        if addresses:
            interfaces.append(f"{interface.name()}: {addresses[0].toString()}")
    return sorted(interfaces)


class LocalNetworkStatus(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        self.refresh()

    def refresh(self):
        try:
            interfaces = read_active_interfaces()
            text = (
                f"本机活动接口（{len(interfaces)}）\n{interfaces[0]}\n端到端延迟：未测量"
                if interfaces
                else "未发现带 IP 的活动接口\n端到端延迟：未测量"
            )
            detail = (
                "\n".join(interfaces)
                if interfaces
                else "仅检测本机网卡状态，未发现带 IP 的活动接口。"
            )
            detail += "\nWSL 下显示 Ubuntu 虚拟网卡；接口活动不代表公网或机器人已连通。"
        except (OSError, RuntimeError) as exc:
            text, detail = "本机网络状态读取失败\n端到端延迟：未测量", str(exc)
        for fields in (
            self.window.hardware_labels,
            getattr(self.window, "hardware_page_labels", {}),
        ):
            if "网络连接" in fields:
                fields["网络连接"].setText(text)
                fields["网络连接"].setToolTip(detail)

    def stop(self):
        self.timer.stop()

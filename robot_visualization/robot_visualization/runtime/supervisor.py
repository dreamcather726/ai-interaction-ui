"""通过 ros2 run 启动现有节点；只关闭本进程创建的进程组。"""

import json
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def ros_command(package, executable, parameters=None, python=""):
    command = ["ros2", "run"]
    if python:
        command += ["--prefix", shlex.quote(python)]
    command += [package, executable]
    if parameters:
        command += ["--ros-args"]
        for key, value in parameters.items():
            command += ["-p", f"{key}:={json.dumps(value, ensure_ascii=False)}"]
    return command


class BackendSupervisor:
    def __init__(self, config):
        self.config = config
        self.children = {}
        self.readers = []
        self.lines = queue.Queue(maxsize=2000)
        self.lock_file = None
        self.log_file = None
        self.log_lock = threading.Lock()
        self.stopping = False

    def plans(self):
        c = self.config
        plans = [
            (
                "audio_receiver_node",
                ros_command(
                    "ai_interface",
                    "audio_receiver_node",
                    {"output_dir": str(c.audio_root)},
                ),
            ),
            (
                "parse_task_node",
                ros_command(
                    "ai_edge",
                    "parse_task_node",
                    {"model": c.model, "ollama_url": c.ollama_url},
                ),
            ),
            (
                "asr_node",
                ros_command(
                    "ai_edge",
                    "asr_node",
                    {
                        "audio_root": str(c.audio_root),
                        "model_path": c.model_path,
                        "device": "cpu",
                    },
                    c.asr_python,
                ),
            ),
            (
                "audio_processing_node",
                ros_command(
                    "ai_edge",
                    "audio_processing_node",
                    {
                        "audio_root": str(c.audio_root),
                        "enable_semantic": True,
                        "parse_timeout": 120.0,
                        "allowed_station_ids": c.stations,
                        "allowed_object_ids": c.objects,
                    },
                ),
            ),
        ]
        # 未配置的空列表不能被 ROS 自动推断为字符串数组，因此省略空白名单覆盖。
        if not c.stations or not c.objects:
            params = {
                "audio_root": str(c.audio_root),
                "enable_semantic": True,
                "parse_timeout": 120.0,
            }
            if c.stations:
                params["allowed_station_ids"] = c.stations
            if c.objects:
                params["allowed_object_ids"] = c.objects
            plans[-1] = (
                "audio_processing_node",
                ros_command("ai_edge", "audio_processing_node", params),
            )
        if c.start_microphone:
            plans.append(
                (
                    "microphone_sender_node",
                    ros_command(
                        "ai_interface", "microphone_sender_node", {"device": c.device}
                    ),
                )
            )
        return plans

    def emit(self, text):
        text = re.sub(r"\x1b\[[0-9;]*m", "", text).strip()
        if not text:
            return
        try:
            self.lines.put_nowait(text)
        except queue.Full:
            pass
        with self.log_lock:
            if self.log_file and not self.log_file.closed:
                self.log_file.write(text + "\n")
                self.log_file.flush()

    def start(self, existing):
        if not self.config.start_backend:
            self.emit("仅连接模式：不启动或接管已有后端进程")
            return
        import fcntl

        folder = Path.home() / ".local/state/ai_interaction"
        folder.mkdir(parents=True, exist_ok=True)
        self.lock_file = (
            folder / f"dashboard_{os.getenv('ROS_DOMAIN_ID', '0')}.lock"
        ).open("a")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.emit("另一工作台已负责启动后端；本窗口只连接已有服务")
            self.lock_file.close()
            self.lock_file = None
            return
        self.log_file = (
            folder / f"dashboard_{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}.log"
        ).open("a", encoding="utf-8")
        self.config.audio_root.mkdir(parents=True, exist_ok=True)
        self.ensure_ollama()
        for name, command in self.plans():
            if name in existing:
                self.emit(f"沿用已有节点：{name}，该进程继续使用原来的参数")
            else:
                self.spawn(name, command)

    def ensure_ollama(self):
        parts = urlsplit(self.config.ollama_url)
        if parts.hostname not in ("localhost", "127.0.0.1"):
            self.emit("使用外部 Ollama 地址，不启动本地模型服务")
            return
        try:
            with socket.create_connection(
                (parts.hostname, parts.port or 11434), timeout=0.3
            ):
                self.emit("Ollama 服务已在线，沿用已安装的模型")
                return
        except OSError:
            pass
        executable = shutil.which("ollama")
        if executable:
            env = dict(
                os.environ, OLLAMA_HOST=f"{parts.hostname}:{parts.port or 11434}"
            )
            self.spawn("ollama", [executable, "serve"], env)
        else:
            self.emit("未找到 Ollama，语义服务调用时会报告连接失败；请先安装 Ollama")

    def spawn(self, name, command, env=None):
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        self.children[name] = process
        reader = threading.Thread(
            target=self.read_output, args=(name, process), daemon=True
        )
        reader.start()
        self.readers.append(reader)
        self.emit(f"启动节点：{name}")

    def read_output(self, name, process):
        try:
            for line in process.stdout:
                self.emit(f"[{name}] {line.rstrip()}")
        finally:
            process.stdout.close()
            code = process.wait()
            if not self.stopping:
                self.emit(f"节点已退出：{name}，退出码 {code}；不自动重启")

    def drain(self):
        for _ in range(100):
            try:
                yield self.lines.get_nowait()
            except queue.Empty:
                break

    def close(self):
        self.stopping = True
        for sig, timeout in (
            (signal.SIGINT, 3.0),
            (signal.SIGTERM, 1.0),
            (signal.SIGKILL, 0.3),
        ):
            live = [p for p in self.children.values() if p.poll() is None]
            for process in live:
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + timeout
            while any(p.poll() is None for p in live) and time.monotonic() < deadline:
                time.sleep(0.03)
        for reader in self.readers:
            reader.join(timeout=0.2)
        with self.log_lock:
            if self.log_file:
                self.log_file.close()
        if self.lock_file:
            self.lock_file.close()

"""统一定位项目内的 ASR 模型，避免依赖个人缓存目录或启动位置。"""

from pathlib import Path


def default_model_path():
    """兼容源码运行和 colcon 安装后的入口，返回项目本地模型目录。"""
    for origin in (Path.cwd().resolve(), Path(__file__).resolve().parent):
        for parent in (origin, *origin.parents):
            for root in (parent, parent / "src"):
                if (root / "ai_edge/package.xml").is_file() and (
                    root / "robot_visualization/package.xml"
                ).is_file():
                    return root / "models/SenseVoiceSmall"
    return Path.cwd().resolve() / "models/SenseVoiceSmall"

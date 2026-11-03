"""浅色监控面板样式与中文字体加载。"""

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase


def configure_font(app):
    """WSLg 优先使用系统中文字体，也可加载 Windows 已有的微软雅黑。"""
    for path in ("/mnt/c/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttc"):
        if Path(path).is_file():
            QFontDatabase.addApplicationFont(path)
            break
    font = QFont()
    font.setFamilies(["Microsoft YaHei", "Noto Sans CJK SC", "sans-serif"])
    font.setPointSize(10)
    app.setFont(font)


STYLE = """
QMainWindow, QWidget#canvas { background: #f2f6fc; color: #233751; }
QWidget { font-size: 13px; color: #233751; }
QFrame#header { background: white; border-bottom: 1px solid #dbe5f3; }
QFrame#card { background: white; border: 1px solid #dce6f3; border-radius: 12px; }
QLabel#title { font-size: 23px; font-weight: 700; color: #142f54; }
QLabel#subtitle { color: #8a9bb2; font-size: 11px; }
QLabel#section { color: #183f72; font-size: 15px; font-weight: 700; }
QLabel#muted { color: #8797ad; }
QLabel#hero { font-size: 21px; font-weight: 600; color: #1d3c63; }
QLabel#mic { color: #2581fb; background: #edf5ff; border-radius: 34px;
    font-size: 31px; font-weight: 600; }
QLabel#badge { border-radius: 12px; padding: 5px 11px; font-size: 11px; }
QLabel#badge[tone="good"] { background: #eaf8f0; color: #218658; }
QLabel#badge[tone="idle"] { background: #edf2f8; color: #8291a6; }
QLabel#badge[tone="active"] { background: #eaf2ff; color: #2679e8; }
QLabel#badge[tone="bad"] { background: #fff0ee; color: #c85648; }
QLabel#badge[tone="warn"] { background: #fff7e6; color: #ac7c23; }
QLabel#step { background: #f2f5fa; color: #8a9ab0; border-radius: 7px; padding: 9px 6px; }
QLabel#step[tone="good"] { background: #eaf8f0; color: #218658; }
QLabel#step[tone="active"] { background: #eaf2ff; color: #2679e8; }
QLabel#step[tone="bad"] { background: #fff0ee; color: #c85648; }
QLabel#step[tone="warn"] { background: #fff7e6; color: #ac7c23; }
QPushButton { border: 1px solid #d9e4f2; border-radius: 7px; padding: 9px 16px;
    background: white; color: #456181; }
QPushButton:hover { background: #f0f6ff; border-color: #b3d0fa; }
QPushButton:disabled { color: #a6b1c2; background: #f3f5f8; border-color: #e8edf4; }
QPushButton#primary { background: #237df0; color: white; border-color: #237df0; font-weight: 600; }
QPushButton#primary:hover { background: #1269da; }
QPushButton#primary:disabled { background: #c9dcf7; border-color: #c9dcf7; color: white; }
QPushButton#stop { background: #fff4f2; color: #cc6253; border-color: #f2d4cf; }
QPushButton#stop:disabled { background: #f5f6f8; color: #adb5c2; border-color: #e5eaf0; }
QPushButton#small { padding: 5px 10px; font-size: 11px; }
QLineEdit { border: 1px solid #dce6f3; border-radius: 6px; padding: 8px; background: #fafcff; }
QTextEdit, QPlainTextEdit { border: none; background: #f7faff; border-radius: 7px;
    padding: 9px; selection-background-color: #d6e8ff; }
QTextEdit { font-size: 17px; color: #29486d; }
QPlainTextEdit { font-size: 11px; color: #64768e; }
QListWidget { border: none; background: transparent; outline: none; }
QListWidget::item { border-radius: 7px; padding: 12px 8px; margin-bottom: 5px; color: #657a96; }
QListWidget::item:selected { background: #eaf2ff; color: #2479e8; }
QListWidget::item:hover { background: #f0f5fc; }
QTabWidget::pane { border: none; padding-top: 8px; }
QTabBar::tab { background: #e9eff8; border-radius: 6px; padding: 8px 14px;
    margin-right: 4px; color: #627b99; }
QTabBar::tab:selected { background: #237df0; color: white; }
QLabel#pagedText { background: #f7faff; border-radius: 7px; color: #29486d; }
QTableWidget { background: #fafcff; border: 1px solid #dce6f3;
    gridline-color: #e4ebf5; selection-background-color: #d6e8ff; }
QHeaderView::section { background: #edf3fb; padding: 6px; border: none; }
QComboBox { background: #fafcff; border: 1px solid #dce6f3; padding: 5px; }
QToolTip { color: #28486d; background: white; border: 1px solid #d5e2f1; padding: 7px; }
"""

"""三栏监控布局；只创建与摆放控件，不访问 ROS 或执行机器人动作。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from robot_visualization.ui.catalog import CatalogPanel
from robot_visualization.ui.paging import (
    ElidedLabel,
    PagedList,
    PagedLog,
    PagedText,
    PageTabs,
)
from robot_visualization.ui.task_drafts import TaskDraftPanel
from robot_visualization.ui.widgets import Badge, Card, PipelineStrip, glyph, label

# 仅补充布局、间距和导航选中样式，颜色复用现有浅色主题；不修改 theme.py。
LAYOUT_STYLE = """
QFrame#navigation { background: white; border: 1px solid #dce6f3; border-radius: 12px; }
QPushButton#navItem { text-align: left; padding: 11px 6px; border: none;
    background: transparent; border-radius: 6px; }
QPushButton#navItem:checked { background: #eaf2ff; color: #2479e8; font-weight: 600; }
QLabel#mapPlaceholder { background: #f2f5fa; border: 1px solid #dce6f3; border-radius: 7px; }
QLabel#videoPlaceholder { background: #111827; color: #d2ddea; border-radius: 7px; }
QLineEdit#recordQuery { padding: 4px; }
QFrame#monitorCanvas QLabel#section { font-size: 14px; }
QFrame#monitorCanvas QLabel#step { padding: 6px 2px; font-size: 11px; }
QFrame#monitorCanvas QTabBar::tab { padding: 5px 9px; }
"""

NAVIGATION = (
    ("首页", QStyle.StandardPixmap.SP_DirHomeIcon),
    ("地图与定位", QStyle.StandardPixmap.SP_DirIcon),
    ("机器人状态", QStyle.StandardPixmap.SP_ComputerIcon),
    ("任务管理", QStyle.StandardPixmap.SP_FileDialogDetailedView),
    ("相机图像", QStyle.StandardPixmap.SP_MediaPlay),
    ("日志分析", QStyle.StandardPixmap.SP_FileDialogInfoView),
    ("系统设置", QStyle.StandardPixmap.SP_FileDialogContentsView),
)


class MapPlaceholder(QLabel):
    """装饰网格，无地图数据、机器人位置或路径。"""

    def paintEvent(self, event):
        # QLabel 仍负责文字与样式；背景网格仅作待接入占位。
        super().paintEvent(event)
        painter = QPainter(self)
        color = self.palette().midlight().color()
        color.setAlpha(90)
        painter.setPen(QPen(color, 1))
        for x in range(0, self.width(), 24):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), 24):
            painter.drawLine(0, y, self.width(), y)
        painter.end()


def compact_card(title, subtitle=""):
    card = Card(title, subtitle)
    card.body.setContentsMargins(10, 8, 10, 8)
    card.body.setSpacing(6)
    return card


def icon_heading(window, card, icon):
    picture = label()
    picture.setPixmap(window.style().standardIcon(icon).pixmap(18, 18))
    card.heading.insertWidget(0, picture)


def placeholder(title, text=None):
    card = compact_card(title)
    card.body.addWidget(label(text or f"{title}待接入", "muted", True))
    return card


def build_device_page(window, kind):
    """Independent views of the same telemetry; never move homepage widgets."""
    titles = {"map": "地图与定位", "robot": "机器人状态", "camera": "相机图像"}
    title = titles[kind]
    if kind == "robot":
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("机器人状态 · 数据", "section"))
        grid = QGridLayout()
        grid.setSpacing(8)
        window.hardware_page_labels = {}
        for index, name in enumerate(
            ("机器人状态", "底盘状态", "相机状态", "电池电量", "网络连接", "当前任务")
        ):
            card = compact_card(name)
            field = label(
                "等待消息" if name != "当前任务" else "尚无执行任务",
                "muted",
                True,
            )
            field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            window.hardware_page_labels[name] = field
            card.body.addWidget(field)
            if name == "电池电量":
                window.robot_page_battery = QProgressBar()
                window.robot_page_battery.setRange(0, 100)
                window.robot_page_battery.setValue(0)
                window.robot_page_battery.setFormat("等待电池消息")
                card.body.addWidget(window.robot_page_battery)
            card.body.addStretch()
            grid.addWidget(card, index // 2, index % 2)
        for column in (0, 1):
            grid.setColumnStretch(column, 1)
        for row in range(3):
            grid.setRowStretch(row, 1)
        layout.addLayout(grid, 1)
        return page

    from robot_visualization.ui.simulation import RobotImage

    card = compact_card(title, "数据 · 与首页共享消息，切页保留最新画面")
    view = RobotImage(
        "等待 /map 地图消息" if kind == "map" else "等待 /camera/image_raw 图像消息"
    )
    view.setObjectName("mapPlaceholder" if kind == "map" else "videoPlaceholder")
    info = ElidedLabel("等待地图与定位" if kind == "map" else "等待相机画面")
    if kind == "map":
        window.map_page_view, window.map_page_info = view, info
    else:
        window.camera_page_view, window.camera_page_info = view, info
    card.body.addWidget(view, 1)
    card.body.addWidget(info)
    return card


def build_monitor_layout(window):
    canvas = QFrame()
    canvas.setObjectName("monitorCanvas")
    canvas.setStyleSheet(LAYOUT_STYLE)
    window.setCentralWidget(canvas)
    outer = QVBoxLayout(canvas)
    outer.setContentsMargins(8, 8, 8, 8)
    outer.setSpacing(8)
    outer.addWidget(build_header(window))
    columns = QHBoxLayout()
    columns.setSpacing(8)
    columns.addWidget(build_navigation(window))
    window.pages = QStackedWidget()
    window.pages.setSizePolicy(
        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
    )
    window.page_indexes = {name: index for index, (name, _) in enumerate(NAVIGATION)}
    for page in (
        build_home(window),
        build_device_page(window, "map"),
        build_device_page(window, "robot"),
        build_drafts(window),
        build_device_page(window, "camera"),
        build_log_analysis(window),
        build_settings(window),
    ):
        window.pages.addWidget(page)
    columns.addWidget(window.pages, 1)
    columns.addWidget(build_sidebar(window))
    outer.addLayout(columns, 1)
    window.pages.currentChanged.connect(window.page_changed)
    window.page_changed(0)


def build_header(window):
    header = QFrame()
    header.setObjectName("header")
    header.setFixedHeight(64)
    row = QHBoxLayout(header)
    row.setContentsMargins(12, 6, 12, 6)
    robot = label()
    robot.setPixmap(glyph("robot", 42))
    row.addWidget(robot)
    titles = QVBoxLayout()
    titles.setSpacing(0)
    titles.addWidget(label("机器人可视化与监控平台", "title"))
    titles.addWidget(label("ROS2 智能搬运机器人", "subtitle"))
    row.addLayout(titles)
    row.addStretch()
    window.system_badge = Badge("● 系统在线", "good")
    window.system_badge.setToolTip("表示本地工作台已开启，不代表机器人硬件在线")
    window.connection_badge = Badge("等待 AI 编排连接")
    window.task_badge = Badge("● 任务状态 · 待命", "good")
    window.task_badge.setToolTip("根据语音任务处理状态更新，不代表机器人正在运动")
    for badge in (window.system_badge, window.connection_badge, window.task_badge):
        row.addWidget(badge)
    window.time_label = label("", "muted")
    window.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    window.time_label.setMinimumWidth(112)
    row.addWidget(window.time_label)
    return header


def build_navigation(window):
    navigation = QFrame()
    navigation.setObjectName("navigation")
    navigation.setFixedWidth(120)
    layout = QVBoxLayout(navigation)
    layout.setContentsMargins(4, 12, 4, 8)
    layout.setSpacing(8)
    window.navigation = QButtonGroup(window)
    window.navigation.setExclusive(True)
    window.nav_buttons = {}
    for index, (title, icon) in enumerate(NAVIGATION):
        button = QPushButton(title)
        button.setObjectName("navItem")
        button.setCheckable(True)
        button.setIcon(window.style().standardIcon(icon))
        window.navigation.addButton(button, index)
        window.nav_buttons[title] = button
        layout.addWidget(button)
    layout.addStretch()
    window.navigation.idClicked.connect(window.switch_page)
    return navigation


def build_home(window):
    page = QWidget()
    window.home_grid = QGridLayout(page)
    window.home_grid.setContentsMargins(0, 0, 0, 0)
    window.home_grid.setSpacing(8)
    for column in (0, 1):
        window.home_grid.setColumnStretch(column, 1)
    for row, stretch in ((0, 3), (1, 7), (2, 3)):
        window.home_grid.setRowStretch(row, stretch)
    window.home_grid.addWidget(build_map(window), 0, 0)
    window.home_grid.addWidget(build_camera(window), 0, 1)
    window.home_grid.addWidget(build_commands(window), 1, 0)
    window.home_grid.addWidget(build_progress(window), 1, 1)
    window.log_card = build_logs(window)
    window.home_grid.addWidget(window.log_card, 2, 0, 1, 2)
    window.log_location = "home"
    return page


def build_map(window):
    card = compact_card("SLAM 地图 / 机器人定位")
    icon_heading(window, card, QStyle.StandardPixmap.SP_DirIcon)
    from robot_visualization.ui.simulation import RobotImage

    view = RobotImage("Waiting for map")
    window.map_view = view
    view.setObjectName("mapPlaceholder")
    view.setAlignment(Qt.AlignmentFlag.AlignCenter)
    view.setMinimumHeight(50)
    view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
    card.body.addWidget(view, 1)
    controls = QHBoxLayout()
    for title, tooltip in (("+", "放大"), ("−", "缩小"), ("□", "全屏")):
        button = QPushButton(title)
        button.setObjectName("small")
        button.setEnabled(False)
        button.setToolTip(f"{tooltip}控件待实现；地图数据正常订阅")
        controls.addWidget(button)
    controls.addStretch()
    card.body.addLayout(controls)
    return card


def build_camera(window):
    card = compact_card("摄像头实时画面（前视相机）")
    icon_heading(window, card, QStyle.StandardPixmap.SP_MediaPlay)
    collapse = QPushButton("⌄")
    collapse.setObjectName("small")
    collapse.setEnabled(False)
    card.heading.addWidget(collapse)
    from robot_visualization.ui.simulation import RobotImage

    video = RobotImage("Waiting for camera")
    window.camera_view = video
    video.setObjectName("videoPlaceholder")
    video.setAlignment(Qt.AlignmentFlag.AlignCenter)
    video.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
    card.body.addWidget(video, 1)
    return card


def build_commands(window):
    card = compact_card("AI 指令 / 当前语音任务")
    icon_heading(window, card, QStyle.StandardPixmap.SP_MediaVolume)
    window.command_pages = PageTabs()
    source_page = QWidget()
    source_body = QVBoxLayout(source_page)
    source_body.setContentsMargins(0, 0, 0, 0)
    window.input_tabs = PageTabs()
    voice = QWidget()
    layout = QVBoxLayout(voice)
    layout.setContentsMargins(0, 2, 0, 0)
    layout.setSpacing(4)
    status = QHBoxLayout()
    window.recording_label = ElidedLabel("等待麦克风与模型连接")
    window.capture_badge = Badge("设备状态未确认")
    status.addWidget(window.recording_label, 1)
    status.addWidget(window.capture_badge)
    layout.addLayout(status)
    window.capture_hint = ElidedLabel("录音状态以设备广播为准")
    layout.addWidget(window.capture_hint)
    controls = QHBoxLayout()
    window.start_button = QPushButton("开始录音  Alt+R")
    window.start_button.setObjectName("primary")
    window.start_button.clicked.connect(window.start_recording)
    window.stop_button = QPushButton("停止并识别  Alt+S")
    window.stop_button.setObjectName("stop")
    window.stop_button.clicked.connect(window.stop_recording)
    for button in (window.start_button, window.stop_button):
        button.setStyleSheet("padding: 5px 8px;")
        controls.addWidget(button)
    layout.addLayout(controls)
    window.control_message = ElidedLabel()
    layout.addWidget(window.control_message)
    window.input_tabs.addTab(voice, "语音输入")
    window.input_tabs.addTab(label("文本指令待接入", "muted"), "文本指令")
    source_body.addWidget(window.input_tabs)
    source_body.addWidget(label("解析结果（结构化指令）", "section"))
    parsed = QHBoxLayout()
    parsed.setSpacing(6)
    window.asr_text, window.candidate_text = PagedText(), PagedText()
    for title, view, stretch in (
        ("ASR 识别文字", window.asr_text, 2),
        ("任务候选 / 动作预览", window.candidate_text, 3),
    ):
        column = QVBoxLayout()
        column.setSpacing(4)
        column.addWidget(label(title, "muted"))
        column.addWidget(view, 1)
        parsed.addLayout(column, stretch)
    source_body.addLayout(parsed, 1)
    window.dialogue_hint = ElidedLabel()
    window.dialogue_hint.hide()
    source_body.addWidget(window.dialogue_hint)
    window.home_add_candidate_button = QPushButton("加入当前候选到草稿")
    window.home_add_candidate_button.setObjectName("primary")
    window.home_add_candidate_button.setEnabled(False)
    source_body.addWidget(window.home_add_candidate_button)
    window.home_draft_hint = ElidedLabel(
        "加入后查看完整草稿；预检通过后由你确认整单执行"
    )
    window.home_draft_hint.setObjectName("muted")
    source_body.addWidget(window.home_draft_hint)
    window.command_pages.addTab(source_page, "语音与候选")
    draft_page = QWidget()
    draft_body = QVBoxLayout(draft_page)
    draft_body.setContentsMargins(0, 2, 0, 0)
    window.home_draft_identity = ElidedLabel("尚无任务草稿")
    draft_body.addWidget(window.home_draft_identity)
    window.home_draft_preview = PagedText(
        "识别后点击“加入当前候选到草稿”，在这里逐步核对。"
    )
    draft_body.addWidget(window.home_draft_preview, 1)
    window.home_edit_draft_button = QPushButton("编辑完整草稿（增删／调整顺序）")
    window.home_edit_draft_button.clicked.connect(
        lambda: window.switch_page(window.page_indexes["任务管理"])
    )
    draft_body.addWidget(window.home_edit_draft_button)
    draft_body.addWidget(
        label("确认入口在右侧任务流程卡片底部；执行端自主运行整份任务。", "muted", True)
    )
    window.command_pages.addTab(draft_page, "任务草稿 · 确认前核对")
    card.body.addWidget(window.command_pages, 1)
    return card


def build_progress(window):
    card = compact_card("任务流程 / 执行进度")
    window.cancel_simulation_button = QPushButton("取消执行")
    window.cancel_simulation_button.setEnabled(False)
    card.heading.addWidget(window.cancel_simulation_button)
    window.query_execution_button = QPushButton("查询原任务")
    window.query_execution_button.setObjectName("small")
    window.query_execution_button.clicked.connect(
        lambda: window.draft_requested.emit("query_execution", {})
    )
    card.heading.addWidget(window.query_execution_button)
    window.simulation_step_label = label(
        "等待已确认草稿；动作数量随草稿变化", "muted", True
    )
    card.body.addWidget(window.simulation_step_label)
    card.body.addWidget(label("当前语音处理流程", "section"))
    window.pipeline = PipelineStrip()
    card.body.addWidget(window.pipeline)
    details = QHBoxLayout()
    current = compact_card("当前步骤")
    window.stage_badge = Badge("等待音频")
    current.heading.addWidget(window.stage_badge)
    window.request_label = ElidedLabel("尚无处理记录")
    current.body.addWidget(window.request_label)
    window.result_message = PagedText()
    current.body.addWidget(window.result_message, 1)
    window.file_label = ElidedLabel(f"音频目录：{window.config.audio_root}")
    window.file_label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse
    )
    current.body.addWidget(window.file_label)
    info = compact_card("任务信息")
    window.task_information = label(
        "任务 ID：待接入\n开始时间：待接入\n预计完成：待接入", "muted", True
    )
    info.body.addWidget(window.task_information)
    window.material_state_label = ElidedLabel("持有物料：等待反馈")
    info.body.addWidget(window.material_state_label)
    window.simulation_scene_hint = ElidedLabel(
        "工位坐标与物料位置由执行端提供；等待状态"
    )
    window.simulation_scene_hint.setObjectName("muted")
    info.body.addWidget(window.simulation_scene_hint)
    info.body.addStretch()
    details.addWidget(current, 3)
    details.addWidget(info, 2)
    card.body.addLayout(details, 1)
    window.stage_progress = QProgressBar()
    window.stage_progress.setRange(0, 100)
    window.stage_progress.setValue(0)
    window.stage_progress.setFormat("任务执行进度未确认")
    card.body.addWidget(window.stage_progress)
    draft_actions = QHBoxLayout()
    window.home_draft_status = ElidedLabel("草稿未保存")
    draft_actions.addWidget(window.home_draft_status, 1)
    window.home_save_draft_button = QPushButton("保存草稿")
    window.home_save_draft_button.setObjectName("small")
    window.home_save_draft_button.setEnabled(False)
    draft_actions.addWidget(window.home_save_draft_button)
    window.home_confirm_draft_button = QPushButton("确认并整单执行")
    window.home_confirm_draft_button.setObjectName("primary")
    window.home_confirm_draft_button.setEnabled(False)
    draft_actions.addWidget(window.home_confirm_draft_button)
    window.retry_simulation_button = QPushButton("重试准备")
    window.retry_simulation_button.setObjectName("small")
    window.retry_simulation_button.setVisible(False)
    draft_actions.addWidget(window.retry_simulation_button)
    card.body.addLayout(draft_actions)
    return card


def build_logs(window):
    card = compact_card("系统日志 / 运行日志")
    icon_heading(window, card, QStyle.StandardPixmap.SP_FileDialogInfoView)
    window.query_input = QLineEdit()
    window.query_input.setObjectName("recordQuery")
    window.query_input.setPlaceholderText("输入音频 request_id")
    window.query_input.returnPressed.connect(window.query_record)
    window.query_button = QPushButton("查询记录")
    window.query_button.setObjectName("small")
    window.query_button.clicked.connect(window.query_record)
    window.clear_log_button = QPushButton("清空显示")
    window.clear_log_button.setObjectName("small")
    window.show_debug_checkbox = QCheckBox("显示调试日志")
    window.show_debug_checkbox.setChecked(False)
    window.show_debug_checkbox.toggled.connect(window.filter_logs)
    window.logs = PagedLog()
    window.logs.setMaximumBlockCount(1000)
    window.clear_log_button.clicked.connect(window.clear_logs)
    card.heading.addWidget(window.query_input, 1)
    card.heading.addWidget(window.query_button)
    card.heading.addWidget(window.clear_log_button)
    card.body.addWidget(window.show_debug_checkbox)
    row = QHBoxLayout()
    row.addWidget(window.logs, 7)
    source = PagedText(
        "数据来源说明\n服务徽章与语音处理阶段来自现有ROS接口。\n"
        "网络信息来自运行工作台的电脑；WSL 下显示 Ubuntu 虚拟网卡。\n"
        "地图、相机、底盘和电池来自当前部署的统一设备接口。\n"
        "语音阶段与任务执行进度分别展示；执行进度以反馈为准。"
    )
    row.addWidget(source, 3)
    card.body.addLayout(row, 1)
    return card


def build_drafts(window):
    card = compact_card("任务草稿与确认", "按实际顺序增删步骤，保存后逐步核对并确认。")
    window.draft_panel = TaskDraftPanel(window.config)
    window.draft_panel.set_catalog(window.catalog_snapshot)
    window.draft_panel.command_requested.connect(window.draft_requested.emit)
    window.draft_panel.log_message.connect(window.append_log)
    card.body.addWidget(window.draft_panel, 1)
    return card


def build_log_analysis(window):
    tabs = PageTabs()
    host = QWidget()
    window.log_analysis_layout = QVBoxLayout(host)
    window.log_analysis_layout.setContentsMargins(0, 0, 0, 0)
    tabs.addTab(host, "运行日志")
    history = compact_card(
        "最近记录", "最多100条；选择原请求查看已有结果，不重复识别。"
    )
    window.history = PagedList()
    window.history.currentItemChanged.connect(window.select_record)
    history.body.addWidget(window.history, 1)
    history.body.addWidget(window.history.pager)
    show = QPushButton("返回首页查看所选结果")
    show.clicked.connect(lambda: window.switch_page(window.page_indexes["首页"]))
    history.body.addWidget(show)
    tabs.addTab(history, "历史记录")
    window.analysis_tabs = tabs
    return tabs


def build_settings(window):
    tabs = PageTabs()
    window.settings_tabs = tabs
    tabs.addTab(
        placeholder("系统设置", "系统设置待接入；工位和物料可在相邻页签维护。"),
        "系统设置",
    )
    for kind, title in (("stations", "工位管理"), ("objects", "物料管理")):
        card = compact_card(
            title, "维护实际ID、名称和语音别名；停用资料后新任务不能再引用。"
        )
        panel = CatalogPanel(kind, window.catalog_snapshot)
        panel.command_requested.connect(window.catalog_requested.emit)
        window.catalog_panels[kind] = panel
        card.body.addWidget(panel, 1)
        tabs.addTab(card, title)
    window.catalog_details = PagedText(
        f"语义模型：{window.config.model}\n工位：{'、'.join(window.config.stations) or '未配置'}"
        f"\n物料：{'、'.join(window.config.objects) or '未配置'}"
    )
    tabs.addTab(window.catalog_details, "当前配置")
    return tabs


def build_sidebar(window):
    sidebar = QWidget()
    sidebar.setFixedWidth(260)
    layout = QVBoxLayout(sidebar)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)

    def sidebar_card(card):
        card.body.setContentsMargins(10, 6, 10, 6)
        card.body.setSpacing(4)
        card.heading.parentWidget().setFixedHeight(24)
        card.setStyleSheet("QLabel#muted { font-size: 11px; }")
        return card

    window.hardware_labels = {}
    for title, text in (
        ("机器人状态", "当前位置 / 线速度 / 角速度 / 运行模式：待接入"),
        ("底盘状态", "运动状态 / 速度 / 转向角：待接入"),
        ("电池电量", "电量 / 电压 / 续航：待接入"),
        ("网络连接", "正在读取本机网络接口；端到端延迟未测量"),
    ):
        card = sidebar_card(compact_card(title))
        window.hardware_labels[title] = label(text, "muted", True)
        card.body.addWidget(window.hardware_labels[title])
        layout.addWidget(card)
    services = sidebar_card(compact_card("服务状态"))
    window.service_badges = {}
    for name, title in (
        ("microphone", "麦克风"),
        ("receiver", "音频接收"),
        ("asr", "语音识别"),
        ("parser", "语义解析"),
        ("processor", "AI 编排"),
        ("drafts", "任务草稿"),
    ):
        row = QHBoxLayout()
        row.addWidget(label(title))
        row.addStretch()
        badge = Badge()
        badge.setFixedHeight(23)
        badge.setStyleSheet("padding: 3px 8px;")
        window.service_badges[name] = badge
        row.addWidget(badge)
        services.body.addLayout(row)
    layout.addWidget(services)
    current_task = sidebar_card(compact_card("当前任务"))
    window.hardware_labels["当前任务"] = label(
        "任务状态 / 类型 / 目标 / 任务 ID：待接入", "muted", True
    )
    current_task.body.addWidget(window.hardware_labels["当前任务"])
    layout.addWidget(current_task)
    layout.addStretch()
    return sidebar


def move_log_card(window, page_index):
    """Qt 控件只能属于一个布局；切页保留同一日志对象，分页适应新尺寸。"""
    target = "analysis" if page_index == window.page_indexes["日志分析"] else "home"
    if window.log_location == target:
        return
    if target == "analysis":
        window.home_grid.removeWidget(window.log_card)
        window.log_analysis_layout.addWidget(window.log_card)
    else:
        window.log_analysis_layout.removeWidget(window.log_card)
        window.home_grid.addWidget(window.log_card, 2, 0, 1, 2)
    window.log_card.show()
    window.log_location = target

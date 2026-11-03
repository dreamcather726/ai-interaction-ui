# ai交互界面

ROS 2 Humble AI 交互项目。程序与模型部署在 PC 的 Linux 虚拟机中，模块之间使用 ROS 2 Topic、Service 和 Action 通信。

当前已实现 PySide6 语音工作台、一键启动、长录音、真实 FunASR 识别、Ollama 语义解析，以及可变步骤草稿的编辑、保存、人工确认和取消。播报和机器人执行节点尚未实现，界面只展示已接入的功能。

| 目录 | 内容 |
|---|---|
| `robot_interfaces` | 公共消息、服务与动作定义 |
| `ai_interface` | 接收音频、保存文件、打包元数据并转交 AI 端；另提供设备侧麦克风发送入口 |
| `ai_edge` | 接收音频任务、真实 FunASR 识别、语义调度与可视化结果接口 |
| `robot_visualization` | PySide6 工作台、ROS 状态订阅、录音按钮和后端进程管理 |
| `docs/接口协议.md` | 接口清单、字段约定、成功／失败回复及验收要求 |

接口版本：`0.3.1`。每条自定义消息及 Service／Action 各部分均有 `builtin_interfaces/Time stamp`；每个业务响应至少有 `bool success` 和 `string message`，同时提供 `error_code` 与关联 ID。包含运行／部件状态、任务进度、软件急停及复位、错误报告和心跳；地图、定位、路径、相机、关节、电池和诊断复用标准 ROS 2 类型，话题与 QoS 见协议。新增字段要求所有接口使用方同步重建。

详见[接口协议](docs/接口协议.md)。仓库使用英文名称 `ai-interaction-ui`，中文项目名称为“ai交互界面”。

### 一键启动图形工作台

先构建所有包、加载工作空间，再安装一次 PySide6：

```bash
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
colcon build --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source install/setup.bash
/usr/bin/python3 -m pip install --user -r src/robot_visualization/requirements.txt
```

以后加载 ROS 和工作空间环境后，一个命令启动界面及全部现有语音功能：

```bash
ros2 run --prefix /usr/bin/python3 robot_visualization dashboard --ros-args \
  -p asr_python:=/usr/bin/python3
```

GUI 依赖固定为 PySide6 6.8.3，直接安装到系统 Python 的用户目录，无需虚拟环境。已安装 6.12.0 且出现 `none_dealloc` 崩溃时，先按[工作台使用说明](docs/前端工作台_使用说明.md#pyside6-依赖替换与崩溃排查)替换依赖。

窗口提供真实录音开始／停止、ASR 文字、语义候选、处理阶段、历史查询和运行日志。麦克风默认最长120秒自动停止，可通过 max_recording_seconds 配置为1～300秒。地图、相机、底盘和电池尚未接入，对应区域仅显示待接入占位。下文保留分节点调试命令；一键启动时无需另外手动打开这些节点。

完整参数、白名单、香橙派模式、模块说明及验证见 [前端工作台使用说明](docs/前端工作台_使用说明.md)。

支持同一会话中的多轮任务澄清：先保存缺参候选并追问，再结合下一轮回答补全，完整候选仍需后续准备和确认。实现逻辑、上下文边界及手动测试步骤见[多轮任务澄清](docs/多轮任务澄清.md)。

草稿将搬运展开为“导航→抓取→导航→放下”，支持逐段录音追加、增删和重排动作，保存在工作空间 `task_data/task_drafts.sqlite3`；确认只记录人工审阅结果，尚未连接机器人执行。接口升级后必须完整重建所有包，详见[任务草稿与长任务](docs/任务草稿与长任务.md)。

“工位管理”和“物料管理”页支持维护ID、名称、语音别名及启用状态，保存在 `task_data/catalog.sqlite3`。草稿使用分页选择框，语义解析读取名称／别名，服务端按当前有效资料重新校验。已有资料库不会被启动白名单覆盖。部署范围与手动复测见[工位与物料管理](docs/工位与物料管理.md)；代码审查各项处理与纠正见[修复清单](CODE_REVIEW_FIXES.md)。

主窗口采用[三栏监控布局](docs/三栏监控布局.md)：左侧七项导航、中间首页双列卡片、右侧常驻状态栏和顶部时钟。任务草稿在“任务管理”，工位／物料管理在“系统设置”，历史记录在“日志分析”。地图、相机、底盘、电池和网络仅显示待接入，现有语音与草稿功能保留。

### 源码目录导航

业务代码按职责放入 Python 子包，四个 ROS 功能包仍放在工作空间的 src 下：

```text
src/
├── ai_edge/
│   ├── ai_edge/
│   │   ├── audio/          # 音频输入、队列、流程调度、结果校验与存储
│   │   ├── asr/            # FunASR 模型、识别节点、输入适配与健康状态
│   │   ├── semantic/       # Ollama 模型、语义 Action、提示词与候选校验
│   │   └── visualization/  # 处理状态、结果广播与查询接口
│   ├── test/               # 规则与响应校验测试
│   ├── resource/           # ROS 包索引标记
│   ├── package.xml
│   └── setup.py
├── ai_interface/
│   ├── ai_interface/
│   │   ├── audio/          # PC 端音频接收、WAV 保存与任务转交
│   │   └── devices/        # 香橙派／WSL 真实麦克风发送程序
│   ├── resource/
│   ├── package.xml
│   └── setup.py
├── robot_interfaces/       # 公共 msg、srv、action 定义
├── robot_visualization/    # PySide6 工作台、ROS 通信与一键启动
├── docs/                   # 协议与可视化对接说明
└── README.md
```

`resource/ai_edge` 是 ROS 功能包发现所需的标记文件，保留为空即可；业务代码放在上面的 Python 子包中。各子包包含 `__init__.py`，由 setup.py 的 find_packages 自动安装。

目录调整后重新构建并加载工作空间，所有节点统一使用 `ros2 run` 启动：

```bash
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
colcon build --packages-up-to ai_interface ai_edge --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source install/setup.bash

# ASR 使用已有 FunASR 环境的解释器；参数沿用下文配置
ros2 run --prefix "$HOME/venvs/ros2_audio/bin/python" ai_edge asr_node
# 在另一个终端启动设备侧程序
ros2 run ai_interface microphone_sender_node
```

2026-11-02 目录整理验收：20 个迁移模块的业务 AST 保持一致；WSL Humble 临时工作空间 `/tmp/ai-layout-wYjgzT` 中三个包构建、全部子包导入、五个既有可执行入口和 16 项规则测试通过。使用已有真实录音验证了接口接收保存→AI 交接→真实 FunASR→千问 2B→文件、广播和查询，Action 的澄清、取消、忙碌拒绝、超时及错误恢复也通过；本次未采集新的硬件录音。

在已安装 ROS 2 Humble 的 Linux 虚拟机中，从工作空间根目录执行：

```bash
source /opt/ros/humble/setup.bash
colcon build --packages-select robot_interfaces
source install/setup.bash
ros2 interface show robot_interfaces/srv/PrepareTask
ros2 interface show robot_interfaces/action/ExecuteTask
```

此处工作空间根目录指包含 `src` 的 `ai_interaction_ws`，不是本仓库所在的 `src`。接口工程师包已提供下述音频接收入口。

### 接收香橙派音频

真实麦克风发送节点启动后，可直接请求开始／停止录音（接收节点负责保存和打包转交，不提供录音控制转发入口）：

```bash
ros2 service call /audio/start_recording std_srvs/srv/Trigger "{}"
ros2 service call /audio/stop_recording std_srvs/srv/Trigger "{}"
```

香橙派需提供 `/audio/start_recording` 和 `/audio/stop_recording`（均为标准 Trigger）：开始成功表示录音已启动；停止成功表示采集已停止并已发出完整音频片段，文件是否接收成功另看 `/ai/request_status`。命令行直接调用没有上述旧转发节点的三秒等待限制。

历史版本曾验证控制转发和三秒超时；当前简化版接收节点已移除此转发功能。真实香橙派录音控制尚待联调。

`ai_interface/ai_interface/audio/audio_receiver_node.py` 接收香橙派发布的 `/audio/segments`（`robot_interfaces/msg/AudioSegment`），校验后保存 WAV，并向 `/ai/request_status` 发布带原 request_id 的接收结果；随后由 `audio_forwarder.py` 将完整来源信息和 WAV 绝对路径提交给 ai_edge。

在 Linux 工作空间根目录执行：

```bash
source /opt/ros/humble/setup.bash
colcon build --packages-up-to ai_interface
source install/setup.bash
ros2 run ai_interface audio_receiver_node --ros-args -p output_dir:=received_audio
```

另一个已加载环境的终端查看回执：

```bash
ros2 topic echo /ai/request_status robot_interfaces/msg/RequestStatus
```

香橙派使用相同版本接口、相同 ROS_DOMAIN_ID 和可互通的 DDS 网络，按协议发送 `pcm_s16le / 16000 Hz / 单声道` 完整片段（不是 WAV 容器或 MP3），每段最多 300 秒。四个关联 ID 必填；起止时间、发布 stamp 必须有效且与实际帧数相符，两端时钟应同步。QoS 为 Reliable / Volatile / KeepLast(10)，应先启动接收端再发送。

文件名格式为 `audio_年月日_时分秒_微秒_UUID.wav`，例如 `audio_20261101_193000_123456_唯一标识.wav`。日期时间使用接收保存时的北京时间（UTC+8），不受 WSL 系统时区影响；UUID 避免重名。日志和回执包含 request_id 与实际路径。相对 output_dir 以启动目录解析。operation=/audio/segments 的成功仅表示音频已保存，ASR 和解析分别按各自 operation 回报。音频校验／保存失败返回 AUDIO_RECEIVE_FAILED。录音、识别文本和解析结果是运行数据，不应作为源码提交。

工作台、接收节点、AI 编排和 ASR 默认均使用相对目录 `received_audio`。从工作空间根目录启动时，该目录与 `src`、`build`、`install`、`log` 同级；WAV、同名 TXT 和 `.parse.json` 保存在一起。代码中的绝对路径只用于进程间文件交接，不再写死用户目录。各终端应从相同的工作空间根目录启动，或显式配置相同的音频目录。

2026-11-01 已在 WSL Ubuntu-22.04 / Humble 构建通过，并用本地 ROS 发布／订阅验证 1 秒合成 PCM 的 WAV 保存、关联回执、错误格式拒绝及错误后的继续接收；临时测试音频已清理。尚未验证香橙派跨设备 DDS 网络或真实麦克风音频。

### 使用 WSL 的真实麦克风

`microphone_sender_node.py` 使用已验证可用的 `parecord`，无需安装 sounddevice。系统依赖为 `pulseaudio-utils`。程序以 `--raw --format=s16le --rate=16000 --channels=1` 从麦克风读取原始 PCM，停止后沿用 `/audio/segments` 发送；不把 WAV 文件头当作 PCM。

先更新并在 WSL 工作空间构建（沿用普通安装方式）：

```bash
cd ~/ai-interaction-ui/src
git pull --ff-only origin main
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
colcon build --packages-up-to ai_interface --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source install/setup.bash
```

三个终端均先加载 Humble 和工作空间环境。终端一运行接收节点，终端二运行真实麦克风发送节点：

```bash
# 终端一
ros2 run ai_interface audio_receiver_node
# 终端二
ros2 run ai_interface microphone_sender_node
```

终端三开始录音，说完后停止并发送：

```bash
ros2 service call /audio/start_recording std_srvs/srv/Trigger "{}"
ros2 service call /audio/stop_recording std_srvs/srv/Trigger "{}"
```

默认使用 `@DEFAULT_SOURCE@`。可查看设备并指定名称：

```bash
pactl list short sources
ros2 run ai_interface microphone_sender_node --ros-args -p device:=RDPSource
```

开始回执成功只表示 parecord 进程已启动，设备初始化失败由节点日志报告。停止回执成功表示真实音频已发布；接收落盘仍查看 `/ai/request_status`。采集默认最多120秒，可配置为1～300秒，协议最多9,600,000字节，后台持续读取管道，达到上限自动停止并发送。停止总会终止采集：没有接收者或无音频时返回失败并丢弃，不伪造结果；重复停止不重发，节点退出会停止采集并丢弃未发送数据。尚未使用会话管理时默认生成 mic-session／mic-context 标识，仅用于独立录音链路测试；接入任务系统后用 session_id、input_context_id 参数传入当前有效上下文。

采集起止时间按停止时 ROS 时钟及实际帧数推算，不是硬件采样时间戳，可能包含设备缓冲／停止延迟；不可用作精确传感器同步。说话前可等待设备初始化，开始后立即停止可能尚未得到数据。

2026-11-01 已在 WSL 默认 RDPSource 实际调用 parecord 约 2 秒，收到 53100 字节非全零 PCM，ROS 接收端保存 WAV 后与原 PCM 完全一致（16000 Hz、单声道、16 位）。重复开始／停止、无效设备不生成假音频也已验证；临时录音已清理。未试听或验收 ASR，未验证香橙派硬件。


### 接口端打包转交，AI 端处理

```text
香橙派 microphone_sender_node：真实采集
  → /audio/segments
PC ai_interface/audio_receiver_node：接收、保存 WAV
  → audio_forwarder：打包全部来源元数据 + WAV 绝对路径
  → /ai/process_audio（ProcessAudio Service）
PC ai_edge/audio_processing_node：接收任务并校验文件
  → ai_edge/audio_pipeline：ASR → 语义解析 → 保存结果
```

ai_interface 不识别语音、不判断 ASR 文本、不调用 ParseTask、不配置模型白名单。ProcessAudio 请求包含 stamp、request_id、session_id、segment_id、input_context_id、capture_started_at、capture_ended_at、sample_rate、channels、encoding、audio_path；音频内容已经保存为 WAV，不在服务请求中重复复制。文件关闭后才提交请求，不扫描“最新文件”。

Response.receipt 复用 RequestStatus，仅表示 AI 接受／拒绝任务，不表示识别完成。ai_edge 独立发布后续 ASR 和语义状态，接口端不消费这些结果来触发下一阶段。两边必须共享同一绝对路径；跨机器无共享目录时，不能只传本机路径。

重新构建接口、接口包和 AI 包：

```bash
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
colcon build --packages-up-to ai_interface ai_edge --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source install/setup.bash
```

各终端加载环境。接口端保持原启动命令；AI 端另起一个终端运行：

```bash
# 接口端：只负责保存和转交
ros2 run ai_interface audio_receiver_node --ros-args \
  -p output_dir:=received_audio

# AI 端：接收任务并调度识别、语义处理
ros2 run ai_edge audio_processing_node --ros-args \
  -p audio_root:=received_audio \
  -p allowed_station_ids:="[station_a, station_b]" \
  -p allowed_object_ids:="[box_1]"
```

audio_root 必须覆盖接口保存目录，AI 端会拒绝目录外文件；白名单示例须换成项目真实配置。真实麦克风发送和开始／停止调用方式保持不变。只接收存档、不转交时，接口节点设置 `-p auto_forward:=false`。

AI 端的 audio_pipeline.py 调用 `/ai/recognize_audio`（RecognizeAudio Service）和 `/ai/parse_task`（ParseTask Action）。ASR 已接入 FunASR/SenseVoiceSmall，语义解析由下文的 Ollama/qwen3.5:2b 提供；ASR 权重放在本仓库的 `models/SenseVoiceSmall`，不作为源码提交。语音识别及后续处理都在 ai_edge 职责范围内，接口端没有 ASR 客户端。

RecognizeAudio.Response.result 使用完整 RecognizedText。AI 编排校验来源、时间、状态与实际文本；成功后写同名 .txt，失败／静音不进入语义解析。ParseTask 使用 audio:+segment_id、原会话／上下文与配置白名单，结果写同名 .parse.json。待澄清候选由 AI 编排按 session_id/input_context_id 保存，并在下一轮通过 context/partial_candidate 传给模型；字段经过规则合并及独立校验，结果不等于已准备、确认或执行任务。

`/ai/request_status` 按 request_id、source_node、operation 区分保存、AI受理、ASR和语义阶段；`/ai/asr_text` 由 AI 端发布，仅作结果输出，不触发第二条解析链。

接口侧异步等待 AI 服务／受理响应各最多 3 秒，超时保留 WAV 并标记交接未确认，不自动重试。接口发送队列最多 10 项。AI 侧独立维护最多 10 项任务和 1024 项进程内去重指纹：相同请求／来源不重复识别，内容冲突拒绝。重启不扫描旧文件，去重尚未跨重启持久化。AI 侧等待 ASR 默认185秒、解析默认300秒，参数 asr_timeout／parse_timeout 范围 (0,300]，Goal 等待 3 秒，超时不推进后续阶段。任务执行仍须经过原有准备和显式确认。

验证记录（2026-11-01）：WSL Humble 临时工作空间 /tmp/ai-role-split-csXsZ5 中三个包构建通过，接口打包→AI受理→ASR服务→语义Action 的实际 ROS 通信契约验证通过，覆盖静音、失败、重放、关联错误、澄清、白名单、拒绝、超时及恢复；所有后续识别阶段由 AI 节点发起。临时契约服务不是实际模型推理，测试结果已清理。


### 音频模块职责与依赖方向

| 文件 | 单一职责 |
|---|---|
| ai_interface/audio/audio_receiver_node.py | ROS 音频订阅、接收回执与调用转交组件 |
| ai_interface/audio/audio_storage.py | PCM 传输校验、带北京时间的 WAV 保存 |
| ai_interface/audio/audio_forwarder.py | 打包来源数据、异步提交 AI 受理服务 |
| ai_edge/audio/audio_processing_node.py | AI 端 ProcessAudio 服务入口与组件装配 |
| ai_edge/audio/audio_input.py | 共享文件路径、WAV 内容和来源元数据校验 |
| ai_edge/audio/audio_job_queue.py | 有界任务队列、指纹计算与进程内去重 |
| ai_edge/audio/audio_pipeline.py | ASR→语义阶段调度、ROS 客户端和超时控制 |
| ai_edge/audio/audio_result_validation.py | ASR／语义响应、任务候选及白名单校验 |
| ai_edge/audio/audio_result_store.py | 实际识别文本和解析 JSON 的持久化 |

表中路径相对于各功能包根目录（例如 ai_edge/audio/audio_pipeline.py 的仓库位置为 ai_edge/ai_edge/audio/audio_pipeline.py）。节点与流程层调用辅助模块，辅助模块不反向导入节点或调度器；存储、队列和校验函数不创建 ROS 节点或客户端。状态发布通过显式传入的回调交给编排器，不再要求编排器访问外层节点的 receipt_pub 属性。

本次仅重构职责边界，现有 Topic／Service／Action、参数和 ros2 run 入口不变。2026-11-01 已在 WSL Humble 构建两个业务包并重跑完整通信契约回归，保存／转交／ASR／语义、失败、静音、去重、白名单、超时恢复均通过。仍未运行真实模型推理。


### 代码排版和中文运行提示

音频处理源文件统一使用 4 空格缩进、88 字符目标行宽和分组导入，职责说明、关键步骤注释以及程序自身的运行提示采用中文。ROS 节点名、Topic／Service／Action 名称、字段名和稳定错误码保持原值；外部模型返回的识别文字、说明及底层系统错误不会被自动翻译。

麦克风发送节点通过已注册的 ROS 可执行入口启动，在工作空间根目录执行：

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 run ai_interface microphone_sender_node
```

该命令使用安装后的节点入口，仍需已构建的 robot_interfaces。ASR、语义解析、音频编排和接收节点同样通过 ros2 run 启动。

2026-11-01 已在 WSL Humble 验证两个业务包构建、完整通信契约回归、中文 INFO 输出、Python 直接启动和正常退出；本次未启动实际麦克风采集或真实模型推理。


### 真实 FunASR 识别：本次新增的启动步骤

后端为 SenseVoiceSmall，模型适配、文件校验、ROS 服务、模型健康及界面状态各自独立：

| 文件 | 职责 |
|---|---|
| ai_edge/ai_edge/asr/funasr_backend.py | 加载一次 FunASR 模型、真实 PCM 推理与文本标签清理 |
| ai_edge/ai_edge/asr/asr_audio.py | ASR 请求到已有共享目录／WAV 校验的适配 |
| ai_edge/ai_edge/asr/asr_node.py | /ai/recognize_audio 服务、后台加载与单任务推理、超时／并发处理 |
| ai_edge/ai_edge/asr/asr_health.py | ASR 就绪／忙碌／故障状态与节点心跳 |
| ai_edge/ai_edge/visualization/audio_visualization.py | 音频阶段、语义结果广播及晚加入查询 |

先在 WSL 更新并重建全部接口使用方（无需重新安装已可用的 FunASR 环境）：

```bash
cd ~/ai-interaction-ui/src
git pull --ff-only origin main
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
colcon build --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source install/setup.bash
```

终端一启动 ASR。先按 [本地模型说明](models/README.md) 准备完整模型；下面假设仓库位于工作空间的 `src`，并使用已安装 FunASR 的系统 Python：

```bash
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 run --prefix /usr/bin/python3 ai_edge asr_node --ros-args \
  -p audio_root:=received_audio \
  -p model_path:="$PWD/src/models/SenseVoiceSmall" \
  -p device:=cpu
```

路径不同的机器替换实际模型目录。默认从项目本地目录加载，不自动使用用户缓存；模型约 936 MB，只有完整的 `model.pt` 才能用于初始化，`.incomplete` 不能替代权重。日志提示“FunASR 模型已就绪”后再发起录音。`--prefix` 让 ros2 run 使用指定 Python 执行节点入口；若构建节点所用的 Python 本身已安装 FunASR，可省略它。

2026-11-02 已在 WSL Humble 隔离 ROS Domain 中实际通过 ros2 run 启动五个节点，并确认上述 ASR 命令加载真实模型后就绪；测试进程均已退出，本次没有启动麦克风录音。

终端二启动 AI 编排。只验收 ASR 时关闭语义步骤；完整语义链路见下一节：

```bash
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 run ai_edge audio_processing_node --ros-args \
  -p audio_root:=received_audio \
  -p enable_semantic:=false
```

接收节点、麦克风发送和开始／停止录音按前文运行。实际识别完成后收到 /ai/asr_text，WAV 旁生成同名 .txt，阶段为 ASR_COMPLETED。启动下文的 /ai/parse_task 后，恢复 enable_semantic:=true（默认值）并配置真实工位／物料白名单；没有语义模型时不要把其等待失败误认为 ASR 失败。

FunASR 服务参数：audio_root、model_path、device（默认 cpu）、language（默认 zh）、ncpu（默认 4）、recognition_timeout（默认 25 秒，范围 (0,300]）。编排器 asr_timeout 默认 30 秒，应大于服务端等待上限并留通信余量。模型只加载一次，推理在工作线程执行；有活动推理时新请求返回 ASR_BUSY。超时不生成文字，底层线程未结束时仍保持忙碌，不让新推理无限排队。仅全零 PCM 或模型未返回有效文字时报告 NO_SPEECH；模型未提供可校验置信度，使用 -1，不伪造分数。服务失败后节点继续运行。

后端使用 FunASR 集成实现、trust_remote_code=False，不执行下载仓库中的自定义 Python。现有 ros2_audio 环境已验证，不修改其 torch/torchaudio 安装。setup.py 提供可选 asr 依赖声明，便于新环境安装；新机器还需安装匹配的 PyTorch/torchaudio。你提供的 docs/requirements.txt 不由本次任务改写或提交。

可视化组的订阅、查询、字段、状态枚举及运行示例见 [音频接口对接说明](docs/可视化交互组_音频接口.md)。

2026-11-01 实测：WSL Ubuntu-22.04、Humble、ros2_audio/FunASR 1.4.19、CPU 上从真实录音识别出“喂喂喂，你好你好，123123。”，已验证接口交接→真实 ASR→文字保存、静音、缺文件、错误格式、模型就绪状态，以及推理期间可视化查询不被阻塞。语义阶段仍采用临时契约服务验证，未实现真实 LLM 推理。

实现依据：[FunASR](https://github.com/modelscope/FunASR)、[SenseVoice](https://github.com/FunAudioLLM/SenseVoice)。

### 使用 Ollama 千问 2B 完成语义解析

在已安装模型的 WSL 中，默认使用 `qwen3.5:2b`，不需要切换到 4B，也不需要额外安装 Ollama Python SDK。Ollama HTTP 仅在 AI 模型适配器内部使用；接口端、AI 编排与可视化端仍通过 ROS 2 通信。

先按上节更新并构建，在每个终端加载 `/opt/ros/humble/setup.bash` 和工作空间 `install/setup.bash`。先确认服务可达：

```bash
ollama list
curl http://127.0.0.1:11434/api/version
# 仅当服务未运行时，在独立终端执行；已有服务时无需重复启动
ollama serve
```

保留上节的 ASR 节点，另开终端启动语义节点：

```bash
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 run ai_edge parse_task_node --ros-args -p model:=qwen3.5:2b
```

停止之前设置了 enable_semantic:=false 的编排节点，然后只启动一个开启语义的编排节点：

```bash
ros2 run ai_edge audio_processing_node --ros-args \
  -p audio_root:=received_audio \
  -p enable_semantic:=true \
  -p parse_timeout:=120.0 \
  -p allowed_station_ids:="[station_a, station_b]" \
  -p allowed_object_ids:="[box_1]"
```

白名单必须替换为项目真实工位／物料；空白名单不会自动放行任意模型参数。接收节点、真实麦克风节点以及开始／停止录音命令保持原样，分别在其他终端运行。完整链路为：麦克风 → 接口端保存 WAV → AI 编排 → FunASR → 千问 2B → 同名 `.txt` 与 `.parse.json`。可视化继续订阅 `/ai/audio_state`、`/ai/asr_text`、`/ai/parse_result`，晚加入时调用 `/ai/query_audio`。

不录音也能单独验证真实语义 Action（示例白名单）：

```bash
ros2 action send_goal /ai/parse_task robot_interfaces/action/ParseTask \
  "{request_id: 'parse-demo-001', session_id: 'demo-session', input_id: 'text:001', input_context_id: 'demo-context', text: '导航到station_b，不需要返回', allowed_station_ids: [station_a, station_b], allowed_object_ids: [box_1]}" \
  --feedback
```

正常返回 Action SUCCEEDED、success=true、outcome=READY、destination_station_id=station_b。将文字换成“把box_1送到station_b”时应得到 NEED_CLARIFICATION、missing_fields=[pickup_station_id]；换成天气闲聊则 UNSUPPORTED/ABORTED。每次测试使用新的 request_id/input_id。单独调用 Action 只返回 Result/Feedback；音频编排才负责落盘并广播 TaskParseReport。

| 模块 | 职责 |
|---|---|
| `ai_edge/ai_edge/semantic/semantic_prompt.py` | 任务抽取提示词与少量示例 |
| `ai_edge/ai_edge/semantic/ollama_backend.py` | 调用本地 `/api/chat`，默认关闭 thinking、要求 JSON schema |
| `ai_edge/ai_edge/semantic/semantic_contract.py` | 独立检查类型、工位／物料白名单、互斥字段、缺参 |
| `ai_edge/ai_edge/semantic/semantic_input.py` | Goal 来源、历史、部分候选及输入总量校验 |
| `ai_edge/ai_edge/semantic/parse_task_node.py` | ROS Action、反馈、并发拒绝、取消与超时 |

节点参数为 model（qwen3.5:2b）、ollama_url（http://127.0.0.1:11434）、request_timeout（290.0 秒，范围 (0,290]）。冷启动可能需要加载模型，编排侧 parse_timeout 默认300秒，大于服务端290秒等待上限。单模型只处理一条请求，并发 Goal 被拒并广播 LLM_BUSY 回执。取消或超时后立即结束 ROS 等待，底层 HTTP 返回前保持忙碌并丢弃迟到内容，不自动重试。

UNKNOWN/不支持、缺参、未知 ID、无效 JSON、连接失败均不生成完整可执行候选。所有 ID 由程序关联，不交给模型生成。音频流程支持同一会话的多轮澄清；直接调用 Action 时仍由调用方传入 context 和 partial_candidate，语义服务自身不跨请求保存对话。查询、确认、取消等业务命令应由交互层分流，模型解析不能代替这些服务。READY 也必须经过任务准备和用户确认后才能执行。

规则校验回归（无需模型）：`python3 -m unittest discover -s src/ai_edge/test -v`。模型结构化输出和思考开关依据 [Ollama Chat API](https://docs.ollama.com/api/chat)、[结构化输出](https://docs.ollama.com/capabilities/structured-outputs)。

2026-11-02 实测：WSL Ubuntu-22.04/Humble 临时工作空间 `/tmp/ai-ollama-AGikMA` 构建通过；本机 Ollama 0.40.13 的 `qwen3.5:2b` 真实推理通过导航、搬运并返回、缺取货工位澄清、不支持输入，以及 ROS Action 并发拒绝、取消、超时、服务连接失败、模型不存在和恢复测试。已有真实录音经 FunASR 识别为“喂喂喂，你好你好，123123。”，2B 正确判为不支持的任务；同名文本／解析文件、状态广播和晚加入查询均验证通过。16 项独立规则／响应校验测试通过。临时音频输出已清理，原录音保留；本次未验收香橙派硬件、可视化界面或机器人执行。

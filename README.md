# ai交互界面

ROS 2 Humble AI 交互项目。程序与模型部署在 PC 的 Linux 虚拟机中，模块之间使用 ROS 2 Topic、Service 和 Action 通信。

本次先定义公共接口。当前未实现界面、ASR、模型推理、播报和任务执行节点。

| 目录 | 内容 |
|---|---|
| `robot_interfaces` | 公共消息、服务与动作定义 |
| `ai_interface` | 任务接口与交互逻辑的 Python 包骨架 |
| `ai_edge` | 语音识别、模型解析与播报的 Python 包骨架 |
| `robot_visualization` | 界面与可视化的 Python 包骨架 |
| `docs/接口协议.md` | 接口清单、字段约定、成功／失败回复及验收要求 |

接口版本：`0.2.0`。每条自定义消息及 Service／Action 各部分均有 `builtin_interfaces/Time stamp`；每个业务响应至少有 `bool success` 和 `string message`，同时提供 `error_code` 与关联 ID。包含运行／部件状态、任务进度、软件急停及复位、错误报告和心跳；地图、定位、路径、相机、关节、电池和诊断复用标准 ROS 2 类型，话题与 QoS 见协议。新增字段要求所有接口使用方同步重建。

详见[接口协议](docs/接口协议.md)。仓库使用英文名称 `ai-interaction-ui`，中文项目名称为“ai交互界面”。

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

香橙派或下面的模拟程序启动后，可直接请求开始／停止录音（当前接收节点仅负责订阅与保存，不提供控制转发入口）：

```bash
ros2 service call /audio/start_recording std_srvs/srv/Trigger "{}"
ros2 service call /audio/stop_recording std_srvs/srv/Trigger "{}"
```

香橙派需提供 `/audio/start_recording` 和 `/audio/stop_recording`（均为标准 Trigger）：开始成功表示录音已启动；停止成功表示采集已停止并已发出完整音频片段，文件是否接收成功另看 `/ai/request_status`。命令行直接调用没有上述旧转发节点的三秒等待限制。

历史版本曾验证控制转发和三秒超时；当前简化版接收节点已移除此转发功能。真实香橙派录音控制尚待联调。

`ai_interface/ai_interface/audio_receiver_node.py` 接收香橙派发布的 `/audio/segments`（`robot_interfaces/msg/AudioSegment`），校验后保存 WAV，并向 `/ai/request_status` 发布带原 request_id 的接收结果。

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

香橙派使用相同版本接口、相同 ROS_DOMAIN_ID 和可互通的 DDS 网络，按协议发送 `pcm_s16le / 16000 Hz / 单声道` 完整片段（不是 WAV 容器或 MP3），每段最多 10 秒。四个关联 ID 必填；起止时间、发布 stamp 必须有效且与实际帧数相符，两端时钟应同步。QoS 为 Reliable / Volatile / KeepLast(10)，应先启动接收端再发送。

文件使用接收端生成的随机名称；日志和回执包含 request_id 与实际路径。相对 output_dir 以启动目录解析。成功仅表示音频已保存，未执行 ASR 或任务下发；重复投递会保存另一份文件，本节点不负责业务去重。格式／字段错误返回 INVALID_REQUEST，写入失败返回 AUDIO_STORAGE_FAILED，节点继续等待。该目录中的录音是运行数据，不应作为源码提交。

2026-11-01 已在 WSL Ubuntu-22.04 / Humble 构建通过，并用本地 ROS 发布／订阅验证 1 秒合成 PCM 的 WAV 保存、关联回执、错误格式拒绝及错误后的继续接收；临时测试音频已清理。尚未验证香橙派跨设备 DDS 网络或真实麦克风音频。

### 模拟香橙派音频输出

测试程序：`ai_interface/ai_interface/audio_simulator_node.py`，仅用 Python 标准库生成 440 Hz 提示音，无需麦克风或额外音频依赖。这是测试音，不包含语音，不能用来验收 ASR 识别文字。请先重新构建 `ai_interface`，各终端均加载 ROS 2 和工作空间环境。

终端一运行上述接收节点；终端二运行：

```bash
ros2 run ai_interface audio_simulator_node
```

程序等待发现 `/audio/segments` 订阅者后，自动发送一次 1 秒的 PCM 测试音，并继续运行以提供控制服务。接收端日志会显示保存路径，生成 16 kHz、单声道、16 位 WAV。通过回执确认接收是否成功；模拟端打印 Sent 仅表示已发布。

可调整自动发送片段的时长与音调：

```bash
ros2 run ai_interface audio_simulator_node --ros-args -p duration:=2.0 -p frequency:=880.0
```

测试开始／停止录音时，关闭自动发送：

```bash
ros2 run ai_interface audio_simulator_node --ros-args -p auto_send:=false
# 在另一个已加载环境的终端执行，开始后等待一两秒再停止：
ros2 service call /audio/start_recording std_srvs/srv/Trigger "{}"
ros2 service call /audio/stop_recording std_srvs/srv/Trigger "{}"
```

手动模式按两次调用的间隔生成同等时长测试音，最长 10 秒时自动结束。重复开始不重置计时，重复停止不重复发送；无接收者时停止返回失败，达到上限则丢弃该段。自动模式 duration 范围 0.05～10 秒；frequency 范围 [20,8000) Hz。每个片段自动填写唯一请求／片段 ID 和有效的会话／上下文／采集时间。模拟 ID 仅用于链路测试，不能作为真实任务的授权上下文。运行模拟器时不要同时启动使用相同服务名称的真实香橙派节点。

2026-11-01 已在 Humble 构建并通过本地 ROS 收发验证：自动单次输出、非静音 WAV 的格式与帧数、开始／停止、重复调用、不同片段 ID 和十秒上限；测试音频在临时目录验证后清理。测试未使用香橙派或真实麦克风。

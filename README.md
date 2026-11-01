# ai交互界面

ROS 2 Humble AI 交互项目。程序与模型部署在 PC 的 Linux 虚拟机中，模块之间使用 ROS 2 Topic、Service 和 Action 通信。

本次先定义公共接口。当前未实现界面、ASR、模型推理、播报和任务执行节点。

| 目录 | 内容 |
|---|---|
| `robot_interfaces` | 公共消息、服务与动作定义 |
| `ai_interface` | 接收音频、保存文件、打包元数据并转交 AI 端；另提供设备侧麦克风发送入口 |
| `ai_edge` | 接收音频任务、ASR 与语义解析调度；真实模型适配待接入 |
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

真实麦克风发送节点启动后，可直接请求开始／停止录音（接收节点负责保存和打包转交，不提供录音控制转发入口）：

```bash
ros2 service call /audio/start_recording std_srvs/srv/Trigger "{}"
ros2 service call /audio/stop_recording std_srvs/srv/Trigger "{}"
```

香橙派需提供 `/audio/start_recording` 和 `/audio/stop_recording`（均为标准 Trigger）：开始成功表示录音已启动；停止成功表示采集已停止并已发出完整音频片段，文件是否接收成功另看 `/ai/request_status`。命令行直接调用没有上述旧转发节点的三秒等待限制。

历史版本曾验证控制转发和三秒超时；当前简化版接收节点已移除此转发功能。真实香橙派录音控制尚待联调。

`ai_interface/ai_interface/audio_receiver_node.py` 接收香橙派发布的 `/audio/segments`（`robot_interfaces/msg/AudioSegment`），校验后保存 WAV，并向 `/ai/request_status` 发布带原 request_id 的接收结果；随后由 `audio_forwarder.py` 将完整来源信息和 WAV 绝对路径提交给 ai_edge。

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

文件名格式为 `audio_年月日_时分秒_微秒_UUID.wav`，例如 `audio_20261101_193000_123456_唯一标识.wav`。日期时间使用接收保存时的北京时间（UTC+8），不受 WSL 系统时区影响；UUID 避免重名。日志和回执包含 request_id 与实际路径。相对 output_dir 以启动目录解析。operation=/audio/segments 的成功仅表示音频已保存，ASR 和解析分别按各自 operation 回报。音频校验／保存失败返回 AUDIO_RECEIVE_FAILED。录音、识别文本和解析结果是运行数据，不应作为源码提交。

2026-11-01 已在 WSL Ubuntu-22.04 / Humble 构建通过，并用本地 ROS 发布／订阅验证 1 秒合成 PCM 的 WAV 保存、关联回执、错误格式拒绝及错误后的继续接收；临时测试音频已清理。尚未验证香橙派跨设备 DDS 网络或真实麦克风音频。

### 使用 WSL 的真实麦克风

`microphone_sender_node.py` 使用已验证可用的 `parecord`，无需安装 sounddevice。系统依赖为 `pulseaudio-utils`。程序以 `--raw --format=s16le --rate=16000 --channels=1` 从麦克风读取原始 PCM，停止后沿用 `/audio/segments` 发送；不把 WAV 文件头当作 PCM。

先更新并在 WSL 工作空间构建（沿用普通安装方式）：

```bash
cd ~/ai_interaction_ws/src
git pull --ff-only origin main
cd ~/ai_interaction_ws
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

开始回执成功只表示 parecord 进程已启动，设备初始化失败由节点日志报告。停止回执成功表示真实音频已发布；接收落盘仍查看 `/ai/request_status`。采集最多 10 秒／320000 字节，后台持续读取管道，达到上限自动停止并发送。停止总会终止采集：没有接收者或无音频时返回失败并丢弃，不伪造结果；重复停止不重发，节点退出会停止采集并丢弃未发送数据。尚未使用会话管理时默认生成 mic-session／mic-context 标识，仅用于独立录音链路测试；接入任务系统后用 session_id、input_context_id 参数传入当前有效上下文。

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
cd ~/ai_interaction_ws
source /opt/ros/humble/setup.bash
colcon build --packages-up-to ai_interface ai_edge --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source install/setup.bash
```

各终端加载环境。接口端保持原启动命令；AI 端另起一个终端运行：

```bash
# 接口端：只负责保存和转交
ros2 run ai_interface audio_receiver_node --ros-args \
  -p output_dir:=/home/tao/ai_interaction_ws/received_audio

# AI 端：接收任务并调度识别、语义处理
ros2 run ai_edge audio_processing_node --ros-args \
  -p audio_root:=/home/tao/ai_interaction_ws/received_audio \
  -p allowed_station_ids:="[station_a, station_b]" \
  -p allowed_object_ids:="[box_1]"
```

audio_root 必须覆盖接口保存目录，AI 端会拒绝目录外文件；白名单示例须换成项目真实配置。真实麦克风发送和开始／停止调用方式保持不变。只接收存档、不转交时，接口节点设置 `-p auto_forward:=false`。

AI 端的 audio_pipeline.py 调用 `/ai/recognize_audio`（RecognizeAudio Service）和 `/ai/parse_task`（ParseTask Action）。AI 工程师仍需实现真实 ASR／LLM 模型适配节点；当前已有任务入口和编排逻辑，不含模型或固定答案。语音识别及后续处理都在 ai_edge 职责范围内，接口端没有 ASR 客户端。

RecognizeAudio.Response.result 使用完整 RecognizedText。AI 编排校验来源、时间、状态与实际文本；成功后写同名 .txt，失败／静音不进入语义解析。ParseTask 使用 audio:+segment_id、原会话／上下文与配置白名单，结果写同名 .parse.json。只有真实模型返回的内容才会保存；该结果不等于已准备、确认或执行任务。当前按独立单句处理，澄清问题只输出供交互层使用。

`/ai/request_status` 按 request_id、source_node、operation 区分保存、AI受理、ASR和语义阶段；`/ai/asr_text` 由 AI 端发布，仅作结果输出，不触发第二条解析链。

接口侧异步等待 AI 服务／受理响应各最多 3 秒，超时保留 WAV 并标记交接未确认，不自动重试。接口发送队列最多 10 项。AI 侧独立维护最多 10 项任务和 1024 项进程内去重指纹：相同请求／来源不重复识别，内容冲突拒绝。重启不扫描旧文件，去重尚未跨重启持久化。AI 侧 ASR／解析默认各等待 30 秒，参数 asr_timeout／parse_timeout 范围 (0,300]，Goal 等待 3 秒，超时不推进后续阶段。任务执行仍须经过原有准备和显式确认。

验证记录（2026-11-01）：WSL Humble 临时工作空间 /tmp/ai-role-split-csXsZ5 中三个包构建通过，接口打包→AI受理→ASR服务→语义Action 的实际 ROS 通信契约验证通过，覆盖静音、失败、重放、关联错误、澄清、白名单、拒绝、超时及恢复；所有后续识别阶段由 AI 节点发起。临时契约服务不是实际模型推理，测试结果已清理。

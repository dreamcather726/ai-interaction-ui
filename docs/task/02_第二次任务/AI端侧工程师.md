# 第二次任务：AI 端侧工程师

## 数据链路图：

```text
接口侧保存 WAV → /ai/process_audio 提交本段路径与来源信息
  → AI 编排受理 → /ai/recognize_audio → 本地真实 ASR
  → 保存同名文字、发布状态与识别结果 → 界面显示／查询
```

## 任务基本信息

- **任务名称：** 把单段 ASR 接成可连续接收的 ROS 2 功能
- **建议周期：** 第二周，5 个工作日；具体日程见[共同安排](README.md#三5-个工作日安排)
- **运行位置：** PC 机 Linux 虚拟机，ROS 2 Humble，模型沿用第一次已验证环境
- **岗位文件夹：** 沿用第一次 `~/ai_edge_engineer`；接入本项目时复用 `src/ai_edge`，模型与运行结果不入源码
- **对接人员：** 组内接口调用工程师，由其统一协调可视化接入

## 一、本次目标

在已完成的单文件识别基础上，接收接口工程师每次交付的音频路径与来源标识，自动调用真实 ASR，发布本段状态和文字。一段处理结束后节点继续运行，下一段不需要重启节点或修改音频参数。

交付物仍是可在 ROS 2 工作空间构建、通过 `ros2 run` 启动的功能包。本周复用现有模型和编排，不要求重新训练、更换 ASR 模型或开发新的通信系统。

音频采集与文件传输归接口侧；本岗位负责模型所需预处理、模型运行、ASR 调用、识别状态判定和文字保存。只完成 ASR，设置 `enable_semantic:=false`，不把大模型、TTS、候选任务或机器人执行混入本次验收。

## 二、数据与接口要求

### 向接口工程师要什么数据

获取至少两段不同内容的真实录音、对应的 WAV 路径和格式说明，以及 `request_id`、`session_id`、`segment_id`、`input_context_id` 和采集时间。可分别说“你好，机器人”和“查询当前状态”，也可现场说不同短句；这些是识别样例，不要求触发对应业务。

读取实际 WAV 参数核对 16000 Hz、单声道、16 位。若收到不合规格式，按当前契约返回明确错误并向接口工程师反馈；模型额外转换和预处理由本岗位实现，不能只改扩展名或参数标记。

### ROS 2 功能包与接口

| 接口 | 本岗位职责 |
|---|---|
| `/ai/process_audio`，`ProcessAudio` | 接收完整来源信息和绝对路径，校验后返回 `receipt`，表示受理／拒绝 |
| `/ai/recognize_audio`，`RecognizeAudio` | AI 编排内部调用；响应 `result` 为完整 `RecognizedText` |
| `/ai/audio_state`，`AudioProcessingState` | 发布排队、等待 ASR、识别中和终态，带来源、进程序号及错误 |
| `/ai/asr_text`，`RecognizedText` | 发布实际成功文字、无语音或失败，保持原来源标识 |
| `/ai/request_status`，`RequestStatus` | 分阶段说明受理、识别及具体拒绝原因 |
| `/ai/query_audio`，`QueryAudio` | 已知音频请求 ID 时提供当前进程快照 |
| 部件状态与心跳 | 发布 ASR 加载、就绪、忙碌与故障；活性不冒充模型就绪 |

完整类型与 QoS 见[共用接口清单](README.md#2-三方共用接口清单)。第一次练习中的 `std_srvs/srv/Trigger` 识别接口不再作为本周正式接口，不在同名服务下并存。

### 状态与结果规则

| 情况 | 必须给出的结果 |
|---|---|
| 合法任务进入队列 | 受理回执，不提前返回识别完成 |
| 正在识别 | `RECOGNIZING`；模型推理不阻塞查询和其他 ROS 回调 |
| 识别成功 | `RecognizedText.success=true`、`status=SUCCESS`、实际非空文字；编排终态为 `ASR_COMPLETED` |
| 无有效语音 | `status=NO_SPEECH` 和对应原因；不生成成功文字 |
| 文件、模型、格式或推理失败 | 明确失败、错误码与来源，不返回样例答案或旧文字 |

`confidence=-1` 表示没有可信分数，不改成 100%。WAV 旁保存同名 `.txt`，只写真实成功文字；失败不生成成功文本，路径与来源 ID 可相互核对。本周不要求生成 `.parse.json`。

阶段状态在变化时发布。`source_node`、`boot_id`、`event_seq` 用于区分乱序与重启；识别文字保留四个来源 ID 和采集／识别时间。队列与防重沿用现有实现：同一进程登记范围内完全相同的重复请求不再推理，同 ID 不同内容明确拒绝；重启或记录淘汰后的边界写入说明，不声称永久防重。

### 构建、启动与调用示例

以下假设本仓库位于 `~/ai-interaction-ui/src`。所有终端从工作空间根目录运行，先加载相同 ROS 环境和实际联调域。构建可由接口工程师统一执行：

```bash
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
colcon build --packages-up-to ai_edge ai_interface --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source install/setup.bash
export ROS_DOMAIN_ID=71
ros2 interface show robot_interfaces/srv/RecognizeAudio
```

`71` 为示例域，须与设备、接收端和窗口的现场登记值一致。终端一启动真实 ASR，解释器和模型路径替换为第一次已验证的实际位置：

```bash
ros2 run --prefix "$HOME/venvs/ros2_audio/bin/python" ai_edge asr_node --ros-args \
  -p model_path:="$HOME/ai-interaction-ui/src/models/SenseVoiceSmall" \
  -p audio_root:=received_audio -p recognition_timeout:=180.0
```

若构建时所用 Python 已具备正确的 FunASR 依赖，可省略 `--prefix`。不要求重新下载模型，也不覆盖原有可用推理环境；记录实际模型和依赖版本，等待 ASR 部件状态就绪后再测试。

终端二加载相同环境后启动 AI 编排：

```bash
ros2 run ai_edge audio_processing_node --ros-args \
  -p audio_root:=received_audio -p enable_semantic:=false -p asr_timeout:=185.0
```

终端三分别监听，按 Ctrl+C 结束一个后查看下一个：

```bash
ros2 topic echo /ai/audio_state robot_interfaces/msg/AudioProcessingState
ros2 topic echo /ai/asr_text robot_interfaces/msg/RecognizedText
ros2 param get /audio_processing_node enable_semantic
```

接口工程师启动接收和采集端，向 `/ai/process_audio` 提交真实任务；正式联合链路不由接口端或界面重复调用识别服务。正常应看到 `QUEUED → WAITING_ASR → RECOGNIZING → ASR_COMPLETED`、对应实际文字及同名 TXT。

## 三、需要做的事情

1. **核对第一次真实 ASR。** 记录模型、Python、依赖、文件格式和识别耗时，确认当前 ROS 服务使用自定义类型。第一天与接口工程师冻结路径、ID 和超时。
2. **接入每次音频任务。** 请求本身携带确定的路径，不再依靠启动参数固定某个 WAV。先校验来源、目录范围和实际文件，再返回受理；推理在异步流程执行，保留有界队列。
3. **发布和保存真实结果。** 连续识别两段不同实录，不改路径、不重启。受理、识别中、成功、无语音与失败分别给出清晰结果，由接口工程师交给窗口接入。
4. **补齐错误与恢复。** 覆盖缺文件、不合规格式、无有效语音、模型未就绪／不可用、忙碌和等待超时。超时后不采纳迟到结果，也不在底层推理仍运行时无限追加新推理；依赖恢复或上一推理结束后可处理新录音。
5. **验证防重与查询。** 重复提交同一合法请求不重复识别；冲突 ID 明确拒绝。推理期间查询可响应；未知 ID 返回未找到，不能把默认消息当真实排队。查询仅当前进程有效，重启恢复不编造历史。

主要负责共同验收 W2-01～W2-07 中 ASR、队列、结果和查询部分。超时或忙碌可用可控测试服务／模型替身验证，但须标注，不能代替两段真实模型识别。

## 四、提交与验收

**最终提交一份《第二次任务_AI端侧工程师_操作步骤.md》。功能包代码、模型与环境说明、实际识别结果、运行截图、启动和异常测试记录纳入文档，配套结果提供受控附件位置。**

每一步写清“执行什么、应看到什么、实际结果是什么”。至少记录两段录音的来源、四个关联 ID、文件路径、实际文字、状态变化、处理耗时及 TXT 位置；另附无语音、缺文件、格式、不可用、忙碌／超时、防重和查询的实际结果。模型、录音和运行结果不提交源码。

**验收标准：** 验收人员按文档启动同一套节点，接口每次提交一段音频即可连续获得对应真实文字，不重启、不改输入参数；文字与录音基本一致，标点差异可接受。受理不等于完成，失败不伪造文字，迟到结果不覆盖新任务；错误消除后仍可识别下一段。已知请求可查询真实快照，无记录明确说明。本周终态为 ASR 完成，不依赖大模型运行。

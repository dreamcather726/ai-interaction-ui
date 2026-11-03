# robot_simulator

ROS 2 Humble 的可选硬件消息模拟包，不访问底盘、摄像头、音频设备或 SLAM 硬件。
本仓库本身是工作空间的源码目录，包与 ai_edge、ai_interface 等四个包同级。
若你把仓库放在工作空间的 src/ 下，则本包自然位于 src/robot_simulator/。

## 构建与启动工作台

在 Ubuntu / ROS 2 Humble 终端中执行（不需要虚拟环境）：

```bash
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
colcon build --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source install/setup.bash
ros2 run --prefix /usr/bin/python3 robot_visualization dashboard --ros-args -p use_simulator:=true
```

此命令自动启动模拟地图、相机、底盘、电池和两个 Action 服务端，保留现有
ASR、语义解析、草稿服务，并默认启动真实 microphone_sender_node 供你直接录音。
地图、相机、底盘和任务执行仍使用模拟器。ASR/FunASR、本地模型和
Ollama 仍需要原来的依赖；模拟器不伪造 ASR 或 LLM 结果。
不传 use_simulator（默认 false）时保留原来的真实硬件启动行为。

先在“系统设置”维护 station_a（A 点）、station_b（B 点）和 box_1（一号箱子），
或使用已有真实资料 ID；不会自动改写你的工位、物料数据库。固定演示地图上的
A=(-2,0)、B=(2,0)，不代表现有资料库里的真实位置。

在“任务管理”手动添加 NAVIGATE A → PICK box_1 @ A → NAVIGATE B → PLACE box_1 @ B，
保存后点击“确认并模拟执行”。回到首页观察当前动作、右侧状态和执行进度。
动作数量来自已确认的草稿（最多 256 步），默认每步 2 秒；不会固定成六步或
使用 ASR 阶段冒充执行进度。点击“取消模拟执行”会请求取消，等待服务端终态。
取消、超时、失败不会自动重新发送；再次演示请修改并保存新草稿版本。

仅在 use_simulator=true 时启用画面订阅和模拟执行客户端。界面始终调用
/simulator/task/prepare 与 /simulator/task/execute，避免向真实 /task/execute 发任务。
默认录音控制使用真实 /audio/start_recording 和 /audio/stop_recording，沿用 device、
max_recording_seconds 参数。等待麦克风及 ASR 就绪后点击“开始录音”，说完点击
“停止并识别”，即可走真实语音输入 → 候选 → 草稿 → 模拟执行。
传入 simulator_audio_file 时改用 WAV 回放及 /simulator/audio/ 下的控制服务，
不再启动真实麦克风；状态广播只接受当前选中的音频来源。
如果需要关闭真实麦克风的自动启动，传 -p start_microphone:=false；仍可连接外部
已启动的真实录音节点，或只使用手动编排。
同一模拟节点已在外部运行时沿用外部节点，关闭窗口时保留；不完整的一组外部节点
会报告错误并跳过整组启动，防止重复节点。请先关闭残留节点或补齐启动。

## 可选：没有麦克风时回放 WAV

放一份你自己预录的 command.wav 到 robot_simulator/test_data/，要求 16000 Hz、
单声道、16bit PCM、非空、最长 300 秒。录音不会被 Git 提交，也不需要重新构建：

```bash
ros2 run --prefix /usr/bin/python3 robot_visualization dashboard --ros-args \
  -p use_simulator:=true \
  -p simulator_audio_file:="$PWD/robot_simulator/test_data/command.wav"
```

等待模型和模拟麦克风就绪后点击“开始录音”；回放按 0.5 秒读取，文件结束自动
发送，也可点击“停止并识别”。随后走真实 ASR → 语义解析 → 候选 → 草稿流程。
未指定 WAV 时默认使用真实麦克风，也能手动编排、确认并模拟执行。
需要别的回答内容时，用另一份 WAV 重启回放节点，不会把固定录音转换成虚构追问回答。

当前真实协议是 /audio/segments（robot_interfaces/AudioSegment），每条代表完整录音。
因此内部读取 0.5 秒块，结束时一次发布完整音频；没有新增 audio_data Topic 或更改接收端。

## 独立启动模拟器

另一个终端同样 source ROS 和 install/setup.bash 后：

```bash
ros2 launch robot_simulator simulator_all.launch.py
# 可配置相机与步骤耗时：
ros2 launch robot_simulator simulator_all.launch.py camera_width:=800 camera_height:=600 camera_fps:=10.0 step_seconds:=2.0
# 含自动循环 WAV 回放（auto_play:=false 可改为等待界面开始录音）：
ros2 launch robot_simulator simulator_with_fake_audio.launch.py \
  wav_file:="$PWD/robot_simulator/test_data/command.wav" auto_play:=true loop:=true
```

不要同时执行上述两种独立启动命令，二选一即可。独立 launch 不启动 AI 处理链，
完整语音处理需再启动工作台 use_simulator=true；它会发现并沿用这组外部模拟节点。
若外部启动的是含 WAV 回放的 launch，工作台也须传入相同的 simulator_audio_file
参数选择回放来源，避免同时启用真实麦克风。
可用单独的 ROS_DOMAIN_ID 区分模拟图和真实机器人图；所有相关终端需设置相同的值。

## 消息与验收

ROS 图中的地图、相机、底盘节点分别命名为 /map_node、/camera_node、
/robot_state_node；与独立 ros2 run 启动时保持一致，不带 simulator 后缀。
包名 robot_simulator、可执行文件名和 launch 文件名保持不变；界面仍标明模拟数据。

| 名称 | 类型 / 内容 |
|---|---|
| /map | nav_msgs/OccupancyGrid；200×200、0.05m/cell、原点(-5,-5)，可靠/TransientLocal/1 |
| /map_metadata | nav_msgs/MapMetaData；同样可靠/TransientLocal/1 |
| /camera/image_raw | sensor_msgs/Image；rgb8，默认640×480、10fps，时间文字与移动圆点；SensorData QoS |
| /odom | nav_msgs/Odometry；odom → base_link，A/B之间模拟巡航；SensorData QoS |
| /battery、/battery_state | sensor_msgs/BatteryState；100%缓慢降至20%，电压/电流有波动；SensorData QoS |
| /robot_status | std_msgs/String；JSON，simulator=true、时间、底盘/相机/网络状态 |
| /task/prepare、/simulator/task/prepare | robot_interfaces/PrepareTask；先传入完整TaskSpec，纯内存保存 |
| /task/execute、/simulator/task/execute | robot_interfaces/ExecuteTask；串行动作反馈、完成与取消 |
| /task/snapshot、/simulator/task/snapshot | robot_interfaces/TaskSnapshot；模拟步骤和进度 |
| /audio/play | robot_interfaces/PlayAudio；有效PCM等待1秒；取消返回已模拟帧数，不做TTS |
| /audio/segments | 可选 WAV 回放；robot_interfaces/AudioSegment |
| /audio/start_recording、/audio/stop_recording | 可选 WAV 回放的 Trigger 控制；另提供 /simulator/audio/ 下同名别名供模拟界面使用 |

ExecuteTask.Goal 本身没有步骤数组，因此须先 PrepareTask，再按响应的 task_id、revision、
confirmation_context_id 和有效时间发送 ExecuteTask。动作序号/总数/描述在 Feedback.task.step，
进度在 Feedback.task.progress（0～1）；没有改动现有 Action 定义。
模拟准备记录只在内存中保留，重启后应重新准备；这不是生产调度器或真实机器人授权服务。
底盘巡航与任务步骤为独立演示，不宣称已经完成实际导航、目标识别或抓取。

```bash
ros2 node list
ros2 topic list -t
ros2 topic echo /map_metadata --qos-durability transient_local --once
ros2 topic hz /camera/image_raw
ros2 topic echo /battery --qos-reliability best_effort --once
ros2 topic echo /robot_status --once
ros2 action list -t
rqt_graph
```

工作台验收：地图出现 A/B 和移动位置点；相机时间变化且圆点移动；右侧电量/位置/延迟变化；
保存并确认长草稿后按真实动作数推进；中途取消后显示取消终态且进度不变成100%；
关闭窗口检查由窗口启动的模拟进程退出，外部启动的保留；不传use_simulator时仍是原流程。

如需自行运行测试：`colcon test --packages-select robot_simulator`，随后执行
`colcon test-result --verbose`。纯数据测试覆盖地图形状、RGB尺寸/变化、搬运动作展开、
可变长度、返回动作、过长与不完整任务拒绝；ROS/Qt联调仍需在实际Humble环境验收。

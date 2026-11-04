# robot_simulator

ROS 2 Humble 的可选硬件消息模拟包，不访问底盘、摄像头、音频设备或 SLAM 硬件。
模拟器与香橙派共用上位机客户端和公开接口；完整接口、部署参数及人工验收见
[统一设备接口与部署](../docs/统一设备接口与部署.md)。本次仅静态检查，未运行节点、构建或测试。
本仓库本身是工作空间的源码目录，包与 ai_edge、ai_interface 等四个包同级。
若你把仓库放在工作空间的 src/ 下，则本包自然位于 src/robot_simulator/。

## 构建与启动工作台

在 Ubuntu / ROS 2 Humble 终端中执行（不需要虚拟环境）：

```bash
cd ~/ai-interaction-ui
source /opt/ros/humble/setup.bash
colcon build --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source install/setup.bash
ros2 launch robot_visualization dashboard_sim.launch.py
```

此命令自动启动模拟地图、相机、底盘、电池和两个 Action 服务端，保留现有
ASR、语义解析、草稿服务，并默认启动真实 microphone_sender_node 供你直接录音。
地图、相机、底盘和任务执行仍使用模拟器。ASR/FunASR、本地模型和
Ollama 仍需要原来的依赖；模拟器不伪造 ASR 或 LLM 结果。
真实设备改用 dashboard_real.launch.py；相同客户端和界面连接真实端，不启动模拟器。
两份 launch 默认分别使用 ROS_DOMAIN_ID=71/72。旧 use_simulator 参数仍支持，但旧 ros2 run 命令不自动隔离域。

先在“系统设置”的工位／物料管理中添加并启用资料，ID 可以是 `1`、`2`、`x`，
名称和语音别名可以是“1号点”“二号点”等。工作台自动把同一个 catalog_path 传给
模拟执行端，模拟器同步这些 ID、自动分配地图位置；新物料初始放在第一个启用工位。
这些位置仅用于仿真，不是现场标定数据，也不会写回真实工位资料。

语音解析后点击“加入当前候选到草稿”，首页会自动切到“任务草稿 · 确认前核对”页签。
这里显示完整的有序动作、草稿 ID 和版本；通过“编辑完整草稿”可进入任务管理增删、
调整动作。手动编排后同样先保存。保存后后台自动预检，预检不会启动任何动作。
预检通过后，核对完整草稿，再点击右侧“确认并整单执行”；审阅成功才发送一次整单
ExecuteTask。执行端自主推进全部动作，不需要上位机逐步下发或逐步确认。

例如编排 NAVIGATE 1 → PICK x @ 1 → NAVIGATE 2 → PLACE x @ 2；重名的工位和物料
应通过各自的 ID 区分。预检结果过期、草稿编辑、资料变化或草稿服务重启后，旧预检
不能用于新确认，需要重新保存／预检。仅收到一个确认响应不会绕过用户的执行点击。
动作数量来自已确认的草稿（最多 256 步）。NAVIGATE 按距离和速度移动，到站后才成功；
PICK / PLACE 默认耗时 2 秒，完成后才改变持物及物料位置。位置、物料和执行反馈
来自同一个状态机，进度按已完成动作及当前动作进展计算。取消、超时、失败后立即
停止后续动作，速度归零，保留当前位置、持物状态和最后进度；不会自动重发。
默认机器人初始位置 (0,0)。完成后物料留在放下的工位，下次应按实际位置编排，
不会把物料自动移回取货点。新增资料在任务结束后同步，已有物料位置和工位坐标保持。
仍持有的物料不能被移除，仍被物料占用的工位不能无处置地删除。重启执行节点会重置场景，
并丢失内存中的 PrepareTask 记录。再次演示仍须保存并确认新草稿版本。

两种模式均启用同一个任务和遥测客户端，默认使用公开 /task/prepare、/task/execute、/task/query。
由隔离的 ROS 域和部署配置选择设备，不再依赖模拟器私有端点或 simulator=true JSON。
默认录音控制使用真实 /audio/start_recording 和 /audio/stop_recording，沿用 device、
max_recording_seconds 参数。等待麦克风及 ASR 就绪后点击“开始录音”，说完点击
“停止并识别”，即可走真实语音输入 → 候选 → 草稿 → 模拟执行。
传入 simulator_audio_file 时改用 WAV 回放及 /simulator/audio/ 下的控制服务，
不再启动真实麦克风；状态广播只接受当前选中的音频来源。
如果需要关闭真实麦克风的自动启动，传 -p start_microphone:=false；仍可连接外部
已启动的真实录音节点，或只使用手动编排。
同一模拟节点已在外部运行时沿用外部节点，关闭窗口时保留；不完整的一组外部节点
会报告错误并跳过整组启动，防止重复节点。请先关闭残留节点或补齐启动。

## 执行失联、取消和准备重试

模拟执行客户端分别接收执行端在线状态与任务进度，不限制整份长任务的总时长。
进度默认每 1 秒汇总反馈，步骤变化及终态立即反馈；`feedback_period:=0.0` 可改为
仅在步骤变化及终态反馈。只要同一任务的有效执行端在线状态持续更新，长步骤就不会
因为没有进度反馈而被判定失联。上位机不回复“继续”，执行端不等待上位机才能推进。
连续 5 秒既无有效在线状态也无新任务反馈时，首页显示“执行结果未知”，保留进度及 ID，
禁止新执行，不把失联解释为已停止。同一任务的新反馈恢复后继续跟踪；迟到的
最终 Result 仍可确认完成或取消。重复事件序号不会刷新等待期限。

点击“取消执行”后，客户端检查取消答复并等待原 Action 的最终停止结果。
取消受理不等于已停；5 秒内仍无停止终态，或取消被拒绝／发送失败时，显示停止
未确认。等待停止期间不重复发送取消，也不会将进度补到 100%。

PrepareTask 明确失败、服务未就绪或准备响应超时，且尚未发送 ExecuteTask Goal 时，
首页为同一份已保存、未修改的草稿提供“重试准备”。这是人工操作，沿用原准备
request_id、task_id 和确认上下文；程序不自动重试。一旦执行 Goal 已发出或投递结果
未知，就不开放准备重试，避免重复执行。编辑草稿后需重新保存并审阅。

动态遥测默认 2 秒过期。相机停止发图后保留最后一帧并覆盖“画面已过期”；定位、
电池和持物状态分别标明过期／未确认，不用零速度或空手冒充真实状态。源时间戳
不变的旧消息不刷新期限；收到新的有效消息后恢复。地图作为静态背景保留。

手动检查下列项目时可先在 ROS_DOMAIN_ID=71 独立启动 simulator_all，再启动工作台使其沿用外部节点：

1. 添加工位 `1`、`2` 和物料 `x`，加入四步搬运草稿。首页应显示完整草稿；保存和预检
   后机器人仍待命，只有点击“确认并整单执行”后才开始。编辑草稿后旧确认入口失效。
2. 用 `feedback_period:=0.0` 验证长导航：只有阶段反馈时，在线状态仍使任务保持有效。
3. 关闭相机节点，约 2 秒后应显示画面过期；恢复节点后新帧应清除过期提示。
4. 执行中停止执行服务，约 5 秒后应显示结果未知并保持原进度，不能自动重发。
5. 用一个模拟世界中不存在的物料准备任务，失败后检查“重试准备”入口；修复准备
   条件后人工重试。需要改变任务内容时仍应编辑、保存新版本并重新确认。

这些检查需由你在 Humble 环境运行。本轮只进行了静态检查，没有运行程序或测试。
上位机会保存原投递及已确认终态，重连/重启后只读查询原任务，不重发 Goal。
模拟执行端的查询记录仍在内存中；该执行进程重启后记录丢失，返回未找到。
执行端持久化与断点自动执行恢复未实现；原服务无法恢复时，
需先人工核实其执行／停止状态，再重启模拟工作台及相关模拟节点，不直接重发旧任务。

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
# 与工作台工位／物料管理同步（推荐；工作台自动启动时已传此参数）：
ros2 launch robot_simulator simulator_all.launch.py catalog_path:="$PWD/task_data/catalog.sqlite3"
# 可配置相机与步骤耗时：
ros2 launch robot_simulator simulator_all.launch.py camera_width:=800 camera_height:=600 camera_fps:=10.0 step_seconds:=2.0
# 含自动循环 WAV 回放（auto_play:=false 可改为等待界面开始录音）：
ros2 launch robot_simulator simulator_with_fake_audio.launch.py \
  wav_file:="$PWD/robot_simulator/test_data/command.wav" auto_play:=true loop:=true
```

不要同时执行上述两种独立启动命令，二选一即可。独立 launch 不启动 AI 处理链，
完整语音处理需再启动工作台 use_simulator=true；它会发现并沿用这组外部模拟节点。
沿用的旧模拟节点保留原参数，不会被强行改为资料库模式；请先关闭旧模拟节点，再用
相同的 catalog_path 启动。没有 catalog_path 的独立模拟器仍用 station_a／station_b／box_1。
若外部启动的是含 WAV 回放的 launch，工作台也须传入相同的 simulator_audio_file
参数选择回放来源，避免同时启用真实麦克风。
可用单独的 ROS_DOMAIN_ID 区分模拟图和真实机器人图；所有相关终端需设置相同的值。

## 消息与验收

ROS 图中的地图、相机、底盘节点分别命名为 /map、/camera/camera_raw、
/robot_state_node；与独立 ros2 run 启动时保持一致，不带 simulator 后缀。
相机使用 /camera 命名空间和 camera_raw 节点名；图像 Topic 仍为 /camera/image_raw。
包名 robot_simulator、可执行文件名和 launch 文件名保持不变；界面仍标明模拟数据。

| 名称 | 类型 / 内容 |
|---|---|
| /map | nav_msgs/OccupancyGrid；200×200、0.05m/cell、原点(-5,-5)，可靠/TransientLocal/1 |
| /map_metadata | nav_msgs/MapMetaData；同样可靠/TransientLocal/1 |
| /camera/image_raw | sensor_msgs/Image；rgb8，默认640×480、10fps，时间文字与移动圆点；SensorData QoS |
| /odom | nav_msgs/Odometry；odom → base_link，转发统一调度的实际模拟位置/速度；SensorData QoS |
| /battery_state（兼容 /battery） | sensor_msgs/BatteryState；上位机统一订阅 /battery_state；100%缓慢降至20%，电压/电流有波动；SensorData QoS |
| /robot_status | std_msgs/String；旧内部兼容JSON，上位机不再消费；不模拟网络 |
| /robot/status、/robot/heartbeat | robot_interfaces/RobotStatus、NodeHeartbeat；独立业务状态与活性，2Hz |
| /robot/material_state | robot_interfaces/MaterialState；类型化持物及物料位置，10Hz |
| /tf_static | 显式的模拟 map→odom 单位变换；真实设备须提供真实TF |
| /simulator/world_state | std_msgs/String；执行节点唯一发布的世界状态，包含位置、物料、任务步骤和进度；可靠/TransientLocal/1 |
| /task/prepare、/simulator/task/prepare | robot_interfaces/PrepareTask；先传入完整TaskSpec，纯内存保存 |
| /task/execute、/simulator/task/execute | robot_interfaces/ExecuteTask；串行动作反馈、完成与取消 |
| /task/snapshot、/simulator/task/snapshot | robot_interfaces/TaskSnapshot；模拟步骤和进度 |
| /task/query、/task/cancel | robot_interfaces/QueryTask、CancelTask；原ID查询与恢复任务的显式取消 |
| /audio/play | robot_interfaces/PlayAudio；有效PCM等待1秒；取消返回已模拟帧数，不做TTS |
| /audio/segments | 可选 WAV 回放；robot_interfaces/AudioSegment |
| /audio/start_recording、/audio/stop_recording | 可选 WAV 回放的 Trigger 控制；另提供 /simulator/audio/ 下同名别名供模拟界面使用 |

ExecuteTask.Goal 本身没有步骤数组，因此须先 PrepareTask，再按响应的 task_id、revision、
confirmation_context_id 和有效时间发送 ExecuteTask。动作序号/总数/描述在 Feedback.task.step，
进度在 Feedback.task.progress（0～1）；没有改动现有 Action 定义。
模拟准备记录只在内存中保留，重启后应重新准备；这不是生产调度器或真实机器人授权服务。
底盘状态节点只转发世界状态；超过 2 秒未收到新状态时标记未就绪、速度归零，保留
最后已知位置。没有独立巡航。此处为二维直线运动及抓放状态模拟，未实现真实导航
避障、视觉目标识别或机械臂控制，不发布任何真实硬件控制指令。

## 统一调度的配置与自测

默认地图工位为 station_a=(-2,0)、station_b=(2,0)，默认物料 box_1 在 station_a。
无 catalog_path 时，上位机资料的 ID 与独立模拟场景 ID 需要一致；模型解析成功不代表
模拟世界中存在该物料。有 catalog_path 时，启用 ID 由同一资料库同步，自动坐标与新增
物料的起始位置仅用于演示。此模式以资料库为准，不使用 stations_json／objects_json 初始化 ID。
未知工位、物料位置不匹配、未到站就抓放、已持物又抓取、未持物就放下都会被拒绝。

可先在独立终端启动一组模拟节点，再开工作台，它会沿用外部节点：

```bash
# 正常执行：导航0.8m/s，抓放各2秒，每个步骤最多60秒。
ros2 launch robot_simulator simulator_all.launch.py linear_speed:=0.8 step_seconds:=2.0 step_timeout:=60.0
# 故障场景（与上一条二选一）：第3步失败，不继续第4步；已抓物料仍然持有。
ros2 launch robot_simulator simulator_all.launch.py fail_step:=3
```

需要自定义场景时，向 launch 传入 stations_json 和 objects_json，例如：

```bash
ros2 launch robot_simulator simulator_all.launch.py \
  stations_json:='{"station_a":[-2.0,0.0],"station_b":[2.0,0.0],"station_c":[0.0,1.0]}' \
  objects_json:='{"box_1":"station_a","box_2":"station_b"}'
```

坐标须在[-4.5,4.5]内；模型是直线插值，不检查地图障碍。修改场景参数后重新启动
模拟节点；不要同时启动多组。地图背景中的A/B栅格字母是默认场景标记，界面叠加的
工位点和物料点按配置显示。动作会按当前场景预检，执行前再次校验，且各步完成才推进。

手动验收正常四步：到A前不能持有箱子；抓取完成后箱子随机器人移动；到B后放下才
清除持物状态并达到100%。导航中取消应停在途中；抓取未完成时取消不能报告已抓取；
fail_step=3 时应停在第3步、保留已抓物料，最后放下步骤不执行。

```bash
ros2 node list
ros2 topic list -t
ros2 topic echo /map_metadata --qos-durability transient_local --once
ros2 topic hz /camera/image_raw
ros2 topic echo /battery_state --qos-reliability best_effort --once
ros2 topic echo /robot_status --once
ros2 action list -t
rqt_graph
```

工作台验收：地图出现 A/B 和移动位置点；相机时间变化且圆点移动；右侧电量/位置变化；
网络卡片显示本机实际活动接口和 IP（WSL 下为 Ubuntu 虚拟网卡），端到端延迟未测量；
保存并确认长草稿后按真实动作数推进；中途取消后显示取消终态且进度不变成100%；
关闭窗口检查由窗口启动的模拟进程退出，外部启动的保留；真实模式使用相同流程与界面。

如需自行运行测试：`colcon test --packages-select robot_simulator`，随后执行
`colcon test-result --verbose`。纯数据测试覆盖地图形状、RGB尺寸/变化、搬运动作展开、
可变长度、返回动作、过长与不完整任务拒绝，以及统一执行模型的位置/持物联动、
取消、故障、步骤超时、跨任务物料状态保留；ROS/Qt联调仍需在实际Humble环境验收。

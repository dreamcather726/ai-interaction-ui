# 代码修复清单 v2（2026-11-03 复查）

项目：ROS 2 Humble AI 语音交互工作台（Python）
本轮处理结论：已逐项核对并修改适用问题；原清单的部分判断不成立，不能直接照抄修复建议。下面保留原始审查描述，并在各项开头注明本轮复核结果。**仅完成源码审阅、编辑和格式整理，没有运行构建、界面、节点、模型或测试，不代表运行验收通过。**

新增功能详见[工位与物料管理](docs/工位与物料管理.md)：本地资料库、八页界面、名称／别名、启用／停用、版本冲突保护，以及草稿、提示词和服务端白名单联动。ROS 接口名称、既有字段和错误码字面值保持不变。

## 本轮改动定位

下表为本轮版本的代码入口行号；后续编辑可能使行号移动。

| 检查项 | 文件及入口 | 结果 |
|---|---|---|
| P0-1 | [audio_pipeline.py:398](ai_edge/ai_edge/audio/audio_pipeline.py#L398) | 正确保留 get_parameter_or；共享资料入口 allowed_ids |
| P0-2 | [asr_node.py:83](ai_edge/ai_edge/asr/asr_node.py#L83) | 使用同一个 now 区分节流与累计时间 |
| P0-3 | [store.py:82](ai_edge/ai_edge/task_drafts/store.py#L82) | 显式事务入口及提交／回滚 |
| P0-4 | [supervisor.py:162](robot_visualization/robot_visualization/runtime/supervisor.py#L162) | Linux 文件锁分支、非Linux进程清理 |
| P0-5 | [node.py:242](robot_visualization/robot_visualization/ros/node.py#L242) | 复核后保留字符串节点名判断 |
| P1-6 | [audio_job_queue.py:32](ai_edge/ai_edge/audio/audio_job_queue.py#L32) | 容量内去重和 LRU 淘汰 |
| P1-7 | [error_codes.py](ai_edge/ai_edge/common/error_codes.py)、[设备错误码](ai_interface/ai_interface/devices/error_codes.py) | 字面值不变、按依赖层归属整理 |
| P1-8 | [microphone_sender_node.py:177](ai_interface/ai_interface/devices/microphone_sender_node.py#L177) | 停止采集核心方法 |
| P1-9 | [asr_node.py:108](ai_edge/ai_edge/asr/asr_node.py#L108)、[parse_task_node.py:99](ai_edge/ai_edge/semantic/parse_task_node.py#L99)、[asr_health.py:24](ai_edge/ai_edge/asr/asr_health.py#L24) | 丢弃迟到结果、收尾健康状态 |
| P0-N1 | [paging.py:465](robot_visualization/robot_visualization/ui/paging.py#L465) | 翻组不加载草稿 |
| P1-N2 | [paging.py:276](robot_visualization/robot_visualization/ui/paging.py#L276)、[paging.py:345](robot_visualization/robot_visualization/ui/paging.py#L345) | 当前页选择与新增空选择 |
| P1-N3 / P2-N4 | [paging.py:181](robot_visualization/robot_visualization/ui/paging.py#L181) | 单词边界、UTF-16、首屏排版 |
| P2-N5 | [actions.py:75](ai_edge/ai_edge/task_drafts/actions.py#L75) | 必须先抓取再放下 |
| 基础资料 | [catalog.py:98](ai_edge/ai_edge/common/catalog.py#L98)、[管理界面:51](robot_visualization/robot_visualization/ui/catalog.py#L51)、[草稿选择:159](robot_visualization/robot_visualization/ui/task_drafts.py#L159) | 本地版本存储、编辑、有效项选择 |

---

## 一、原始 P0 检查项（以各项复核结论为准）

### P0-1 `get_parameter_or` 方法不存在（AttributeError）

**复核：原判断不成立，保留正确调用。** [Humble 官方源码](https://github.com/ros2/rclpy/blob/humble/rclpy/rclpy/node.py#L555)明确实现该方法，并为尚未初始化的类型参数提供回退；直接替换为 `get_parameter()` 反而可能在空白名单启动时抛 `ParameterUninitializedException`。编排现通过 `allowed_ids()` 读取共享资料库，未创建资料库的独立节点仍兼容原参数。

- **文件**：`ai_edge/ai_edge/audio/audio_pipeline.py`
- **位置**：第 261、264、353、354 行
- **现状**：仍是 `self.node.get_parameter_or("allowed_station_ids").value`
- **修复**：改为 `self.node.get_parameter("allowed_station_ids").value or []`。参数已在 `__init__` 声明，不能用 `get_parameter_or`（rclpy 没有这个方法）。

### P0-2 ASR 模型加载等待日志时间错误

**复核：原累计差值本身正确；已简化写法。** 原代码更新 `last_load_log` 为当前时间，再减 `load_started`，计算的正是累计等待时间。本轮改为一次读取 `now`，明确分开日志节流时间与累计时间，减少误读。

- **文件**：`ai_edge/ai_edge/asr/asr_node.py`
- **位置**：第 100-103 行 `_poll_model_loading`
- **现状**：第 99 行先更新 `self.last_load_log = time.monotonic()`，第 101 行却用 `self.last_load_log - self.load_started` 算耗时，显示的是"距上次日志的间隔"
- **修复**：日志里的 elapsed 直接用 `time.monotonic() - self.load_started`。

### P0-3 SQLite 事务与 `with self.db:` 混用

**复核：并非必然嵌套事务故障，已改为显式事务以便维护。** [Python 3.10 官方说明](https://docs.python.org/3.10/library/sqlite3.html#how-to-use-the-connection-context-manager)指出连接上下文管理器不会隐式开始新事务。现将连接设置为 `isolation_level=None`，所有写操作统一走 `transaction()` 的 BEGIN／COMMIT／ROLLBACK，业务异常及提交失败都回滚，幂等提前返回也由同一入口完成提交。

- **文件**：`ai_edge/ai_edge/task_drafts/store.py`
- **位置**：第 88-100 行 `invalidate()`、第 104-116 行 `expire()`、第 134-164 行 `apply()`
- **现状**：仍是 `with self.db:` 块内又 `self.db.execute("BEGIN IMMEDIATE")`
- **修复**：在 `__init__` 里把连接改成 `sqlite3.connect(str(path), isolation_level=None)`（自动提交模式），去掉所有 `with self.db:`，在需要事务的地方手动 `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`。

### P0-4 `fcntl` import 跨平台不安全

**处理：已修改。** 文件锁及导入限定在 Linux 分支；其他平台清理使用所拥有的 Popen 进程，不引用不存在的 `SIGKILL`／`os.killpg`。生产 ROS／录音运行目标仍是 Ubuntu／WSL，没有宣称 Windows 原生全链路已验证。

- **文件**：`robot_visualization/robot_visualization/runtime/supervisor.py`
- **位置**：第 154 行 `start()` 方法内
- **现状**：`import fcntl` 在 Linux 判断之前
- **修复**：把 `import fcntl` 移到 `if sys.platform.startswith("linux"):` 分支内，非 Linux 跳过文件锁。

### P0-5 receiver 在线判断方式可能永远返回 False

**复核：原判断不成立，保留字符串成员检查。** [Humble 官方实现](https://github.com/ros2/rclpy/blob/humble/rclpy/rclpy/node.py#L1647)返回 `List[str]`，不是含 `full_name` 的对象。`process_client` 是 AI 编排服务，不能冒充音频接收节点在线状态。本轮没有按错误建议替换。

- **文件**：`robot_visualization/robot_visualization/ros/node.py`
- **位置**：第 238 行
- **现状**：仍是 `"audio_receiver_node" in self.get_node_names()`
- **修复**：用 `any("audio_receiver_node" in n.full_name for n in self.get_node_names())`，或直接判断 `self.process_client.service_is_ready()`。

---

## 二、原始 P1 检查项与处理

### P1-6 去重缓存满了直接拒绝新任务

**处理：已修改。** 两个索引使用 OrderedDict，近期重复输入刷新顺序，超过1024条淘汰最旧记录；队列仍限制10项。去重保护范围为本进程最近1024项，淘汰后再次提交旧输入可能重新识别，不声称具有永久去重能力。

- **文件**：`ai_edge/ai_edge/audio/audio_job_queue.py` 第 71-72 行
- **修复**：`seen_requests`/`seen_inputs` 改用 `OrderedDict`，超过 1024 条时自动淘汰最老条目，不要返回 `RECOVERY_REQUIRED`。

### P1-7 错误码字符串硬编码散落

**处理：已整理。** AI 业务模块集中使用 `ai_edge/common/error_codes.py`；设备端三个回执前缀位于 `ai_interface/devices/error_codes.py`，避免接口端反向依赖 AI 业务包。线上字符串值保持不变，未将任务类型／状态常量误替换为错误码。

- **涉及文件**：audio_pipeline.py、asr_node.py、parse_task_node.py、ollama_backend.py、microphone_sender_node.py、semantic_contract.py、store.py
- **修复**：新建 `ai_edge/ai_edge/common/error_codes.py` 集中定义常量，不改变字面值。

### P1-8 麦克风自动停止路径走了 service callback 包装层

**处理：已修改。** service 回调和定时自动停止均调用 `_do_stop_capture(response)`，随后各自统一经过 health.response 广播真实结果。

- **文件**：`ai_interface/ai_interface/devices/microphone_sender_node.py` 第 139 行
- **修复**：抽出 `_do_stop_capture(response)` 核心方法，service callback 和自动停止都调它。

### P1-9 ASR/语义超时后 health 状态卡在"处理中"

**处理：已修改。** 超时／取消显式记录 discard，等待原线程退出期间保持占用，迟到结果不写入响应。ASR 与 parser 用已有 ComponentStatus 发布“超时收尾中”及 DEGRADED；界面展示该状态，不假装模型线程已被强行中断。收尾完成后才恢复受理槽位。语义服务就绪仅表示可受理，不等同于已验证 Ollama 模型可用。

- **文件**：`ai_edge/ai_edge/asr/asr_node.py`、`ai_edge/ai_edge/semantic/parse_task_node.py`
- **修复**：超时时标记 `discard=True`，worker 返回时丢弃结果不 set 到 response；health 区分"推理中"和"超时收尾中"。

---

## 三、本次新增 UI 分页组件引入的问题（paging.py / task_drafts.py）

### P0-N1 PagedCombo 翻页会误触发"选中草稿"（交互 bug）

**处理：已修改。** 翻页只更换可见组，不改实际选中 ID，也不 emit activated；仅明确选择下拉选项时触发加载。未保存草稿不会因浏览另一组而丢失。

- **文件**：`robot_visualization/robot_visualization/ui/paging.py` 第 441-445 行
- **现状代码**：
  ```python
  def _page_changed(self, page):
      self._current = page * 5 if self._items else -1
      self._render()
      if self._current >= 0:
          self.activated.emit(self._current)
  ```
- **问题**：用户点"上一页/下一页"按钮浏览草稿列表时，`_page_changed` 会直接 `activated.emit()`，而 `task_drafts.py` 第 71 行 `self.history.activated.connect(self.select_saved)` 会响应这个信号，导致用户只是翻页看列表，却被自动切换到新页第一项的草稿。
- **修复**：翻页时只更新 `self._current` 和显示，**不要** emit `activated`。`activated` 只应在用户真正点击下拉列表里某项时触发（即 `_activate` 方法里）。`_page_changed` 里删掉 `self.activated.emit(self._current)`。

### P1-N2 PagedTable/PagedList 隐藏行后当前选中行可能不可见

**处理：已修改。** 渲染后保证已有选择落在当前页。基础资料新增表单允许明确的空选择，避免分页重排时自动加载首条资料覆盖新输入。

- **文件**：`robot_visualization/robot_visualization/ui/paging.py` 第 263-266 行 `PagedTable._render`、第 326-329 行 `PagedList._render`
- **问题**：翻页时用 `setRowHidden` / `setItemHidden` 隐藏非当前页行，但当前选中行如果不在当前页范围内，它仍然是"选中"状态，视觉上看不到选中高亮，但键盘上下键会跳到隐藏行。
- **修复**：`_render` 后检查当前选中行是否在当前页范围内，不在的话把选中移到当前页第一行或最后一行。

### P1-N3 PagedText 按字符折行，英文长单词被从中间截断

**处理：已修改。** 使用 QTextLayout 的单词边界优先换行，放不下的单个长串才按可容纳位置折行；处理 UTF-16 偏移时保留表情等非BMP字符，不按 Python 字符索引错误切片。原文始终保留。

- **文件**：`robot_visualization/robot_visualization/ui/paging.py` 第 189-201 行 `_reflow`
- **问题**：二分查找按字符宽度折行，中文没问题，但英文长单词（如 URL、错误码、文件名）会被从中间劈开。
- **修复**：按空格/连字符先切 token，再按 token 折行；对没有空格的超长字符串保持现状（中文场景为主）。

### P2-N4 PagedText 初始化时 width=0 导致首屏只渲染一个字符

**处理：已修改。** 可见页面第一次显示时立即排版，尚无有效宽度时以400逻辑像素暂排，后续 resize 校正；隐藏页仍延后计算，避免后台日志在未显示时反复排版。

- **文件**：`robot_visualization/robot_visualization/ui/paging.py` 第 147 行、第 181 行
- **问题**：构造函数里 `self._timer.start(0)` 触发 `_reflow`，但此时 widget 还没布局，`contentsRect().width()` 可能 < 16，直接 return，等到 resizeEvent 才重新渲染。会闪一下空白。
- **修复**：`_reflow` 里 width < 16 时不要直接 return，而是用一个合理的默认宽度（如 400）先渲染，等 resizeEvent 再校正。

### P2-N5 validate_action_plan 不校验"未抓取就放下"

**复核及处理：原来允许“初始抓持状态未知”是显式设计，本轮按清单改为更严格的草稿策略。** 没有先在计划内抓取就放下会返回既有 INVALID_ACTION_ORDER；不再用未知初始抓持状态许可放下。真实机器人位置和抓持状态仍必须由后续执行层检查。

- **文件**：`ai_edge/ai_edge/task_drafts/actions.py` 第 82-104 行
- **问题**：状态机里 `carrying` 初始值是哨兵 `unknown`，第一步如果是 PLACE，`carrying is not unknown` 为 False，不会报错，直接走到 else 把 carrying 设为 None。语义上"没抓取就放下"应该报错。
- **修复**：PLACE 分支加判断：`if carrying is unknown: raise SemanticError("INVALID_ACTION_ORDER", "第{index}步没有先抓取物料就放下")`。

---

## 四、已修复/无变化的部分

- `semantic_contract.py`：与上版一致，无新问题。
- `task_drafts/actions.py` 的 TRANSPORT 展开逻辑：正确。
- `window.py` / `theme.py` / `widgets.py`：UI 调整，未发现新问题。
- `microphone_sender_node.py`、`audio_pipeline.py` 业务逻辑：与上版一致。

---

## 修复优先级建议

1. **先修 P0-N1**（翻页误选草稿）——这是用户每次用都会碰到的交互 bug。
2. **再修 P0-1 ~ P0-5**——这 5 个运行时问题迟早会触发。
3. **P1-N2/N3/N5** 和 P1-6~P1-9 一起做。
4. P2 项可排后面。

## 修复要求（同 v1）

- 不改 ROS topic/service/action 名称、消息字段名、错误码字面值。
- 保持 4 空格缩进、行宽 88、中文日志风格。
- 每项修完说明改了哪个文件哪几行。

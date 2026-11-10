# 第二次任务：AI 端侧工程师

**任务名称：** 在已有 ai_asr 中完成真实模型、连续识别与环境验证。

**周期：** 第二周，5 个工作日；先看[共同安排](README.md)。

**配套学习：** [第二周知识清单](../../知识点/第二周/AI%20端侧工程师｜第二周专业知识自学清单.md)。

## 一、从哪里继续

参考项目已有 `ai_asr` 的 ASRNode、FunAsrEngine 和结构化接口。默认模型为 `paraformer-zh`，可带 `fsmn-vad` 与 `ct-punc`。已有 ROS 联调采用固定结果，不能据此认定真实模型、性能或离线加载已通过。

本周在 `ai_interaction_ws/src/ai_asr/` 继续；复用模型封装，不换成此文档仓库的 SenseVoiceSmall，也不训练模型。`ai_edge` 目前是后续扩展框架，不要求为了包名一致迁移已有识别实现。

## 二、需要做的事情

1. **建立可复现环境。** 记录可同时导入 rclpy 和 FunASR 的 Python、FunASR／torch／torchaudio 及硬件版本。在目标机验证后锁定直接与关键依赖，说明安装顺序；不复制其他机器全部 pip freeze，也不盲目升级已能运行的环境。
2. **验证模型加载与复用。** 阅读[FunAsrEngine](https://github.com/RoboNeuxs/ros2-logistics-interaction/blob/b878b982a08bf2533292a2a6aa5ae8611c0e1b90/ai_interaction_ws/src/ai_asr/ai_asr/funasr_engine.py)，分别记录首次加载、首次推理和第二段推理耗时。预加载异常当前只写日志，服务仍可能出现；如需就绪提示必须增加真实依据，不能把服务可发现当模型就绪。
3. **完成两段真实识别。** 使用接口岗交付的合规 WAV，同一 ASR 进程连续处理，保留原来源字段；成功必须为真实非空文字，不输出固定答案。保存可核对的实际文字和耗时记录，个人录音与运行结果受控交付。
4. **验证离线依赖。** 主模型本地路径不意味着 VAD 和标点已离线可用；准备全部启用模型的本地位置，或明确禁用未准备项。按受控断网条件验证不下载即可加载，并说明测试模式与限制。
5. **验证错误与恢复。** 覆盖静音、缺文件、错误采样率／声道、模型不可用。保持 RecognizedText 与阶段一致；`confidence=-1` 保留不可用，不填百分之百。当前错误码如 AUDIO_NOT_FOUND／INVALID_AUDIO_REQUEST／ASR_FAILED 按实际结果记录。
6. **配合超时测试。** 当前 ASR 在服务回调同步推理，接口超时不取消模型。不得承诺已有并发队列和可响应查询；单请求测试，超时后先等待原终态或受控重启恢复。模型替身可用于可控超时，但单独标注，不能替代双真实录音。

## 三、启动与验证

优先由接口岗的统一 launch 启动 ASR 和转发，避免重复同名服务。按[共同安排](README.md)构建并加载工作空间后，本机复制 `interaction.yaml` 到受控配置位置，记录实际覆盖值；以下配置参数名来自当前参考项目：

```yaml
asr_node:
  ros__parameters:
    model_name: paraformer-zh
    model_path: /absolute/path/to/paraformer-zh  # 本地主模型，不是此仓库的 SenseVoice
    vad_model: /absolute/path/to/fsmn-vad       # 启用时也要准备本地资源
    punc_model: /absolute/path/to/ct-punc
    use_vad: true
    use_punc: true
    preload: true
    device: cpu
    max_audio_seconds: 300.0

audio_interface_node:
  ros__parameters:
    recognition_timeout: 120.0  # 当前客户端等待预算；不等于服务端推理硬截止
```

替换全部路径，配置文件保留本地；若本周禁用 VAD／标点，写清原因与对验收的影响。启动前在已验证的同一 Python 环境确认依赖：

```bash
python3 -c "import rclpy, funasr, torch, torchaudio; print('imports OK')"
ros2 launch interaction_interface interaction.launch.py config:=/absolute/path/interaction.local.yaml
```

收到 `/ai/asr_text` 后检查 `request_id`、`success`、`status`、`text`、`error_code`，并核对 `/interaction/asr_text` 内容一致。正式双录音走文件接收端，不在窗口另调一次 ASR。

仓库已有逻辑测试可回归：

```bash
# 参考项目根目录；该测试使用替身，不下载或验证真实模型
python3 -m unittest discover -s tests -v
```

WSL ROS 联调按参考项目 README 运行，测试环境可使用隔离 Domain 与 `ROS_LOCALHOST_ONLY=1`；这不能作为跨设备网络验证结论。

## 四、学习、提交与验收

| 时间 | 知识点 | 项目产物 |
|---|---|---|
| 第 1 天 | 环境、模型依赖、实际音频 | 解释器与模型清单 |
| 第 2 天 | 加载复用、结构化结果 | 真实单段识别与依赖锁定 |
| 第 3 天 | 来源与耗时 | 两段连续真实文字 |
| 第 4 天 | 静音、离线、失败、超时 | 实际异常与恢复记录 |
| 第 5 天 | 可复现说明 | 模型环境和个人操作文档 |

提交《第二次任务_AI端侧工程师_操作步骤.md》及知识笔记，链接[联合记录](验收记录模板.md)。写清环境、模型资源、实际文字、失败原因、耗时、断网结果和未验证项，不把已有固定推理测试记作新实测。

**通过条件：** 共同 W2-01～05、07 中 AI 相关项通过；两段真实录音在同一进程识别且来源对应，失败不伪造成功文字，恢复后新输入可用。断网加载与依赖锁定给出独立实际结论；未通过则保留待完成，不宣称具备离线交付能力。本周不实现 LLM、TTS、ProcessAudio 编排或 QueryAudio。

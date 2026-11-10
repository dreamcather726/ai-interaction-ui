# 第二次任务：AI 端侧工程师

**本周目标：部署并使用本地 LLM，将文字解析为经过校验的任务候选，通过 ROS 2 Action 提供能力。** 第一周已经安排 ASR，本周沿用并做回归检查，不再把“识别两份录音”作为新增主任务。

先读[LLM 知识清单](../../知识点/第二周/AI%20端侧工程师｜第二周专业知识自学清单.md)，接口与验收以[共同约定](README.md)为准。开发位置是参考项目的 `ai_edge` 包；当前仓库中的 `OllamaBackend`、提示词、候选校验器和 `ParseTaskNode` 可截取复用，但不要求一次搬入完整对话、草稿和执行模块。

## 一、部署真实模型并交付可复现环境

采用 Ollama 本机服务与项目已有默认模型 `qwen3.5:2b`。先确认 CPU/GPU、内存、磁盘和运行位置，记录 Ollama 版本、模型完整标签和实际加载情况；不根据模型文件大小推断运行内存，也不承诺固定机器上的速度。

Ollama 尚未安装时，按[官方 Linux 安装说明](https://docs.ollama.com/linux)准备；本任务不自动在组员机器上安装软件。服务已运行就复用，没有运行时才在独立终端执行 `ollama serve`。

```bash
ollama --version
ollama pull qwen3.5:2b
ollama list
ollama show qwen3.5:2b
curl --fail --silent --show-error http://127.0.0.1:11434/api/tags
```

首次下载完成后做一次真实 HTTP 调用：

```bash
curl --fail --silent --show-error --max-time 70 \
  http://127.0.0.1:11434/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5:2b","stream":false,"think":false,"messages":[{"role":"user","content":"用一句话说明你能帮助理解机器人任务。"}],"options":{"temperature":0,"num_ctx":8192,"num_predict":1024}}'
```

**交付标志：** 真实服务返回完整响应和内容，记录首次调用及后续两次调用的耗时。聊天调用只是部署检查，不能代替下面的任务抽取测试。模型标签可在[官方模型页](https://ollama.com/library/qwen3.5:2b)核对。

资源不足时调整模型或超时，并与接口同学同步；记录实际配置，重跑共同测试，不能只把等待时间拉长而不说明原因。

## 二、实现任务抽取与独立校验

### 模型调用层

在 `ai_edge` 中提供独立 Python 调用封装，使用 `/api/chat`、`stream=false`、JSON Schema 格式和明确的系统提示词。拆开 HTTP 外层响应解析与 `message.content` 内层 JSON 解析，检查响应完整结束、类型和长度，记录请求耗时。

沿用当前项目 `OllamaBackend.post()/generate()/parse()` 的分层方式；本周真实模型测试走纯任务抽取分支，不能用完整项目的显式指令编译器绕过模型后仍记作 LLM 通过。配置至少包括模型、URL、超时、温度、上下文与输出上限，不把地址写死在业务逻辑中。

### 提示词与候选校验层

本周做 NAVIGATE 和 TRANSPORT，支持明确的返回要求。输出字段与[共同候选约定](README.md#二先统一接口与数据)一致：未使用 ID 为空字符串，`steps=[]`，字段不允许缺失或额外增加执行标志。

程序独立检查以下内容：

- 对象结构、字段集合、字符串/布尔类型、任务类型；`"false"` 字符串不能当成布尔值。
- 导航和搬运各自的必填、互斥字段，返回要求与返回站是否一致。
- 工位与物料是否属于 Goal 本次传入的白名单，不能把未知 ID 换成默认工位。
- 参数不全时保留已知字段并列出缺项、生成追问，不猜测物料或起点。
- 不支持指令返回 UNSUPPORTED；非法 JSON、字段错误等返回明确失败，不能“尽力修复”成 READY。

**交付标志：** 一条文字可经真实模型调用获得原始响应、解析后候选与校验结果。完整通过共同 T01～T08，并用替身覆盖非法 JSON、错误类型、额外字段与未知 ID。运行记录中区分模型输出与程序派生的缺参/追问。

## 三、实现 ParseTask Action 服务

新增并注册 `ros2 run ai_edge parse_task_node`，使用现有 `robot_interfaces/action/ParseTask`，不重新定义近似接口。

| 部分 | 必须实现 |
|---|---|
| Goal | 校验文字、四个身份字段和白名单；本周 `context=[]`、默认空候选 |
| Feedback | 正常完整流程报告 QUEUED、INFERENCING、VALIDATING，保留 request_id |
| Result | 保留四个身份字段；填写 outcome、success、候选、缺项、追问或错误 |
| 终态 | 按共同表区分 SUCCEEDED、ABORTED、CANCELED；缺参不是传输失败 |
| Goal 拒绝 | 非法输入、模型忙碌明确拒绝，并在 `/ai/request_status` 发布 RequestStatus，不能等待一个不存在的自定义 Result |
| 忙碌 | 同时只运行一条推理；当前推理结束前不再启动第二条 |
| 超时/取消 | HTTP 超时可配置，Action 取消能及时终结；迟到结果丢弃，底层未结束时保留忙碌 |

耗时 HTTP 推理放到受控后台任务，ROS 回调仍能处理取消与状态。不要在单线程执行器中直接阻塞 `requests`/`urllib` 后声称支持及时取消。可参考当前项目“单工作任务＋Future＋定时轮询”的做法，不要求并发推理。

HTTP 404 的模型缺失、连接失败、推理超时、输出非法分别映射 `LLM_MODEL_UNAVAILABLE`、`LLM_UNAVAILABLE`、`PARSE_TIMEOUT`、`INVALID_MODEL_OUTPUT`；保留可定位的说明。本地取消 Future 不代表能强制停止 Ollama 计算。

## 四、与接口同学联调

1. 先让接口同学发送 T01、T04，分别检查 READY 与 NEED_CLARIFICATION，核对四个身份字段。
2. 完成 T01～T08，记录原始响应、校验后的字段、反馈、终态与耗时；修改提示词后整组复跑。
3. 在隔离测试配置中使用错误模型标签、不可达地址和慢响应替身；不要通过关闭同伴共享服务制造故障。
4. 与接口同学测试取消和忙碌拒绝，确认底层收尾期间不会再启动新推理。
5. 最后接回两段真实 ASR 文字，核对模型输入就是实际识别输出，窗口能展示候选或追问。

语音测试由接口岗位提供录音并复用第一周通路；本岗位不重复实现设备收发。

## 五、交付与验收

提交：模型部署与参数说明、HTTP/提示词/校验模块、Action 节点及入口、自动化逻辑测试、T01～T08 真实模型运行记录，以及三个学习问题的回答。

重点对应 W2-02、03、04、08、09，与接口/窗口共同完成 W2-05、11、12。部署记录写版本和模型标签，权重留在运行机；只有替身通过时写“逻辑测试通过、真实模型未验收”。不要求本周训练、微调、量化、多轮对话或机器人执行。

模型 HTTP 参数与结构化输出方式参见 [Ollama Chat API](https://docs.ollama.com/api/chat)、[结构化输出说明](https://docs.ollama.com/capabilities/structured-outputs)。

# 项目本地模型

SenseVoiceSmall 放在项目根目录的 `models/SenseVoiceSmall/`，工作台和单独启动的
`asr_node` 默认使用同一个目录，不自动回退到用户缓存或在线模型 ID。
模型文件保存在运行机器，不上传 GitHub。

```text
models/
├── README.md
└── SenseVoiceSmall/
    ├── model.pt
    ├── config.yaml
    ├── configuration.json
    ├── tokens.json
    ├── chn_jpn_yue_eng_ko_spectok.bpe.model
    └── am.mvn
```

`model.pt.incomplete` 表示下载尚未完成，不能当作权重使用，也不能直接改名。
缺少权重、权重为空或加载失败时，ASR 会报告故障，不能进入“已就绪”。

在新机器准备模型时，从项目根目录执行：

```bash
/usr/bin/python3 - <<'PY'
from modelscope import snapshot_download

snapshot_download(
    "iic/SenseVoiceSmall",
    revision="master",
    local_dir="models/SenseVoiceSmall",
)
PY
```

已有完整模型时，也可以将整个模型文件夹复制到上述位置，保留其配置、词表和权重。
启动前确认 `models/SenseVoiceSmall/model.pt` 存在。

从包含 `src`、`install` 的工作空间根目录启动节点，模型目录由默认路径配置定位：

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 run --prefix /usr/bin/python3 ai_edge asr_node --ros-args \
  -p audio_root:=received_audio
```

工作台直接使用同一默认目录；需要自定义存放位置时，用 `-p model_path:=绝对路径` 覆盖。
修改路径后应退出旧 ASR 节点再启动，已有进程不会自动重新加载权重。

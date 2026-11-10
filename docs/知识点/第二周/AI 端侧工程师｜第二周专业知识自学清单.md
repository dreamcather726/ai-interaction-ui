# AI 端侧工程师｜第二周专业知识自学清单

**学习目标：** 理解音频检查、模型调用和结果整理，配合[本周任务](../../task/02_第二次任务/AI端侧工程师.md)识别两份真实录音。

下面是独立编写的 Python 教学示例，不引用项目源码。每个代码块都能单独运行，使用 Python 标准库。标注 DEMO 的输出用于学习程序结构，不调用真实 ASR，不能代替任务中的真实录音验证。

## 一、识别流程框架

```mermaid
flowchart LR
    A[录音文件] --> B[检查文件与格式]
    B --> C[准备模型]
    C --> D[调用模型]
    D --> E[整理本次文字或错误]
    E --> F[交给窗口]
```

**图示说明：** 输入检查、模型准备、识别和结果整理是不同操作。某一步失败，要说明在哪一步失败。

## 二、知识点讲解

### 1. ASR 的输入与输出：用函数理解调用

ASR 把音频转成文字。函数有输入参数，也可以通过 `return` 返回结果，适合用来理解“给模型录音、取回文字”的过程。

```python
def demo_recognize(audio_name):
    # 教学演示：这里只整理字符串，没有读取音频、没有运行模型。
    return f"DEMO：收到文件名 {audio_name}"


text = demo_recognize("sample.wav")
print(text)
```

**注释：** `audio_name` 是参数，`text` 保存返回值。得到一个字符串不等于完成真实识别；任务中应由真实模型产生文字。

### 2. 文件检查：先确认音频在哪里

文件不存在或没有读取权限时，模型拿不到输入。`Path` 用来表示地址，`is_file()` 用来判断它是不是一个存在的文件。

```python
from pathlib import Path
from tempfile import TemporaryDirectory

with TemporaryDirectory() as folder:
    path = Path(folder) / "missing.wav"
    if path.is_file():
        print("可以继续读取文件")
    else:
        print("音频文件不存在，先检查输入路径")
```

**观察：** 本例特意没有创建文件，所以会走 `else` 分支。文件存在后，还需检查实际音频内容。

### 3. WAV 参数：采样率、声道与位深

采样率表示每秒采集多少次，声道表示音频通道数量，位深表示单个采样值使用多少位。任务使用 16 kHz、单声道、16 位录音，需要读取实际参数核对。

```python
import io
import wave

# 在内存中生成一秒静音 WAV，不保存文件，也不进行语音识别。
buffer = io.BytesIO()
with wave.open(buffer, "wb") as audio:
    audio.setnchannels(1)     # 单声道
    audio.setsampwidth(2)     # 2 字节 = 16 位
    audio.setframerate(16000)
    audio.writeframes(b"\x00\x00" * 16000)

buffer.seek(0)  # 回到文件开头，准备读取
with wave.open(buffer, "rb") as audio:
    print("采样率：", audio.getframerate())
    print("声道数：", audio.getnchannels())
    print("位深：", audio.getsampwidth() * 8)
    print("时长：", audio.getnframes() / audio.getframerate())
```

**观察：** 依次得到 `16000`、`1`、`16` 和 `1.0`。这是合法格式的静音，不能当作有真实语音的识别样本；改文件扩展名也不会改变这些参数。

### 4. 模型复用：准备一次，调用多次

模型加载像先打开听写工具，推理像让工具听写一份录音。连续识别时，通常先准备模型，再重复调用，避免每段都重新加载。

```python
class DemoRecognizer:
    def __init__(self):
        # __init__ 在创建对象时执行一次。
        print("DEMO：创建识别工具")

    def recognize(self, audio_name):
        # 这里只模拟调用，不加载权重，也不识别音频。
        return f"DEMO 调用：{audio_name}"


model = DemoRecognizer()  # 创建一个对象，保存在 model 中
print(model.recognize("a.wav"))
print(model.recognize("b.wav"))  # 继续使用同一个对象
```

**观察：** 创建提示出现一次，调用提示出现两次。`class` 定义一类工具，`self` 代表正在使用的这个对象；实际模型仍沿用任务中已准备的工具。

### 5. 字符串处理：判断空文字，不补写答案

`strip()` 去掉文字两端空白，`if` 根据条件选择处理分支。模型返回空文字时，不应该补一段预先写好的成功答案。

```python
def classify_text(text):
    cleaned = text.strip()
    if not cleaned:
        return {"success": False, "status": "NO_SPEECH", "text": ""}
    return {"success": True, "status": "SUCCESS", "text": cleaned}


print(classify_text("  DEMO 演示文字  "))
print(classify_text("   "))  # 只有空格，没有识别文字
```

**注释：** 这里只练习整理“已经得到的文字”。空文字如何判定由真实后端契约决定；非空文字也可能识别错误，需要和真实录音比较。此函数不能检测音频是否静音。

### 6. 错误与结果：失败原因也要带回去

一个结果可以包含编号、成功与否、文字和说明。编号让结果对应输入，异常处理让调用失败时仍有明确原因。

```python
def run_once(request_id, audio_name, recognize):
    try:
        text = recognize(audio_name)
    except RuntimeError as error:
        return {"request_id": request_id, "success": False,
                "text": "", "message": str(error)}
    return {"request_id": request_id, "success": bool(text.strip()),
            "text": text.strip(), "message": "调用已返回"}


def broken_recognizer(audio_name):
    raise RuntimeError("DEMO：模型没有准备好")


result = run_once("demo-001", "a.wav", broken_recognizer)
print(result["success"])
print(result["message"])
```

**观察：** 输出 `False` 和错误原因，不会拿上次文字补本次结果。这里的字典与字符串状态是教学表示，实际输出按任务书中的正式类型处理。

## 三、组合示例：两次输入保留各自结果

本例只演示结果组织和顺序调用，不读取录音或调用真实模型。

```python
def make_result(request_id, text):
    cleaned = text.strip()
    return {
        "request_id": request_id,
        "success": bool(cleaned),
        "text": cleaned,
        "message": "DEMO：有演示文字" if cleaned else "DEMO：没有文字",
    }


# 假设调用返回了下面的演示内容；不能拿它替代真实 ASR。
inputs = [("demo-001", "第一份演示文字"), ("demo-002", "第二份演示文字")]
results = []
for request_id, text in inputs:
    result = make_result(request_id, text)
    results.append(result)  # 保存每次自己的结果

for result in results:
    print(result["request_id"], result["text"])
```

**动手改一改：** 把第二项文字改成空字符串，观察第二次的 `success` 与 `message`，确认它没有取用第一项文字。

## 四、思考与练习

1. 示例中的 DEMO 返回字符串，与真实模型识别有什么不同？
2. 创建一次模型对象、调用两次，与创建两次对象有什么区别？
3. 文字为空或模型调用失败时，为什么要保留本次编号并说明原因？

将自己的解释、示例输出和任务操作步骤合并为一份说明。断网部署、性能比较和多任务同时处理放到后续学习。

## 五、参考资料

- [Python：wave 音频文件](https://docs.python.org/3.10/library/wave.html)。
- [Python：类与对象](https://docs.python.org/3.10/tutorial/classes.html)。

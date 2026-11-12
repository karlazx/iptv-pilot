# ASR 设备选择与 CPU 回退

Whisper 是模型，faster-whisper 是基于 CTranslate2 的推理实现。普通 CPU 使用
faster-whisper/int8，NVIDIA CUDA 使用 faster-whisper，Intel GPU 使用 OpenVINO GenAI。
同为 Whisper small，FP16/int8、解码实现和音频切分也可能改变输出，文本相似度不是准确率。

## 配置

```dotenv
ASR_BACKEND=auto
ASR_DEVICE=auto
ASR_MODEL=small
ASR_COMPUTE_TYPE=int8
ASR_OPENVINO_MODEL=/root/.cache/openvino/whisper-small-fp16-ov
```

`ASR_DEVICE=cpu` 强制 CPU；`auto` 或 `gpu` 优先探测可用 GPU，未找到兼容设备、缺少
运行库/驱动、模型不匹配、初始化失败或推理抛出异常时，自动回退 faster-whisper CPU。
GPU 推理失败时重新处理当前音频窗口，保留已完成窗口；回退后本进程继续用 CPU，避免反复失败。
`cuda` 只探测 CUDA。`ASR_BACKEND=faster_whisper` 只使用 CTranslate2，`openvino` 只探测
OpenVINO GPU；两者仍有 CPU 回退。`mock` 保留为不调用模型的演示模式。

CPU-only 部署继续使用 `-ai` 镜像，不需要额外驱动或 OpenVINO。
Intel 部署使用可选 `-asr-intel` 镜像，只升级 ASR：

```bash
docker compose -f docker-compose.yml -f docker-compose.asr-intel.yml up -d --no-deps asr
```

先配置 `ASR_INTEL_TAG=<版本>-asr-intel`、`ASR_BACKEND=auto` 和本地模型目录。
覆盖文件只映射 DRM 目录及对应设备类型；没有 DRM 设备时允许空目录，容器仍能启动并使用 CPU。
可用 `ASR_DRI_PATH` 指定宿主机 DRM 目录。宿主机内核驱动仍需由部署者安装。
普通 NVIDIA 使用 `-ai` 镜像还需提供兼容 CUDA/cuDNN 库及 NVIDIA 容器运行时；本项目
没有通过 Intel 测试来宣称 NVIDIA 已实测兼容。

OpenVINO 模型必须预先下载/转换，不能把 faster-whisper 的 CTranslate2 模型目录直接交给它。
运行时不在探测阶段自动下载模型，缺少本地模型时回退 CPU。
模型层数会与 `ASR_MODEL` 检查匹配；切换模型大小时需要准备相应的转换模型。
参考 [OpenVINO Whisper 模型](https://huggingface.co/OpenVINO/whisper-small-fp16-ov)。

CPU/GPU 均使用相同的 Silero VAD、有界音频窗口和原始时间轴，保留检查点、心跳与任务预算。
实际引擎、设备和回退原因写入录制任务/转录的 `asr_runtime`，检查点也保留逐段运行信息。
更改引擎、设备、模型路径、语言或精度配置会生成新的检查点指纹。
GPU 驱动导致进程被杀或永久阻塞时，无法在该进程内执行 Python 回退；已有心跳、过期领取和
尝试预算继续处理此类故障，不能把异常回退理解为对驱动死锁的硬超时保护。

## 验证方法

使用带人工参考文本的样本清单，比较字符错误率（CER），不把 CPU 输出当作真值。
普通话比较可统一繁简、全半角、空格和标点，避免把书写格式差异计为识别错误。
应同时报告未归一化差异、专有名词/数字、漏句、静音幻觉及时间戳有效性，不能只报告一个相似度。

```bash
python -m scripts.diagnostics.evaluate_asr samples.jsonl --device cpu \
  --simplified --output cpu-results.jsonl
python -m scripts.diagnostics.evaluate_asr samples.jsonl --device gpu \
  --openvino-model /models/whisper-small-fp16-ov --simplified --output gpu-results.jsonl
```

清单每行为 `{"id":"sample-1","audio":"/path/sample.wav","reference":"人工转录"}`。
繁简归一化需安装可选评测依赖 `opencc-python-reimplemented`；不进入生产依赖。
`--openvino-cpu-control` 可用于区分“推理引擎变化”与“同一 OpenVINO 模型在 CPU/GPU 上的变化”。
结果文件含音频参考与转录正文，只保存在本地，不提交公开仓库；标准输出只显示计数和汇总。
除了短语音标注集，还应对真实长录像检查内存上限、分段连续性和 CPU 回退。

Intel 核显使用共享内存，GPU 加速不意味着不占用系统内存。仍需保留容器内存限制。

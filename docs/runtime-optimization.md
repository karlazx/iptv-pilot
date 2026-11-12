# 紧凑部署、任务恢复与定向升级

## 三批优化

1. ASR 按 `ASR_CHUNK_SECONDS`（默认 300 秒）流式读取音频并保存分段结果。
   重试复用已完成分段，保留原始时间轴；检查点包含输入文件与模型配置指纹，成功后清理，7 天后过期。
   领取时消耗尝试预算，因此 OOM/SIGKILL 也不会无限重做。心跳与 claim 保护长任务和状态写入。
   总任务时间预算在段间检查，不是单段推理的硬中断。
2. 媒体基础镜像包含 FFmpeg、PyAV 和 NumPy，供 recorder、encoder、web 使用；只有 ASR 使用 Whisper 镜像。
   发布仍生成三个变体，部署可只选择一个服务。基础依赖、媒体依赖和模型依赖分层安装。
3. 紧凑模式将调度、EPG、质量检测、源同步、LLM、推送放入一个后台进程的独立线程。
   每个任务独立轮询和处理异常，复用数据库连接；录制、转码和转写继续独立运行。

系统设置页和 `/api/system/status` 展示队列待处理、处理中、等待上游、失败和停滞状态。
默认无进度超过 20 分钟、空闲队列等待超过 30 分钟会显示异常。
这是界面/API 告警，不会自动向外部邮箱发送通知。维护暂停会单独标识。

## 模式选择

在 `.env` 中选择一种，不能同时启用 compact 和 split：

```dotenv
COMPOSE_PROFILES=compact,ai
```

核心常驻服务是 mongodb、web、background、recorder、encoder、asr。
独立 worker 模式使用 `COMPOSE_PROFILES=split,ai`。
需要内置 `iptv-api` 时再添加 `iptv-api` profile。
`BACKGROUND_JOBS` 可选择后台任务；原本没有运行源同步的部署，应保持不启用 `channel-sync`。
M3U 和 XMLTV 地址由部署者提供，外部 EPG 源不要求内置 iptv-api。

向量搜索默认关闭，关键词搜索继续使用 MongoDB。
需要向量搜索时，同时启用向量生成后端、搜索配置文件和 profile：

```bash
docker compose -f docker-compose.yml -f docker-compose.search.yml --profile search up -d
```

停用搜索时先恢复核心 MongoDB 配置，重启 MongoDB 后再停止 mongot，保留搜索数据卷。

## 定向升级

版本可通过 `WEB_TAG`、`RECORDER_TAG`、`ENCODER_TAG`、`ASR_TAG`、`BACKGROUND_TAG` 分别固定。
应用 worker 不再隐式注入整份 `.env`，修改部署标签不会改变无关容器的环境。

脚本默认只展示计划：

```bash
python -m scripts.deploy --project-dir . --api http://localhost:8080 \
  --service encoder=ghcr.io/karlazx/iptv-pilot:0.1.0-rc.13
```

添加 `--apply` 才执行。API 令牌通过 `IPTV_DEPLOY_API_TOKEN` 提供，不放在命令参数中。
脚本只拉取指定镜像，设置维护暂停并等待当前任务结束，再用 `up -d --no-deps` 更新目标服务。
近期有录制任务时拒绝升级 recorder；超时不会强制杀掉任务。
首次从旧版本升级时，旧 worker 不支持维护暂停，需要人工检查活动任务，完成一次引导升级。

旧镜像通过本地 rollback 标签保留，配置和环境备份存入被 Git 忽略的 `.deploy-history`。
这些备份只保留在部署端，不能上传公开仓库。保留期内不要全量清理镜像。

```bash
python -m scripts.deploy --project-dir . --api http://localhost:8080 \
  --rollback .deploy-history/<部署编号>/state.json --apply
```

脚本回退用于已存在服务的镜像切换。首次迁移到 background 时，还需按保存配置恢复原服务拓扑。
镜像回退不能恢复已删除的原始媒体，也不会自动逆向迁移数据库；结构变更需另做备份和兼容性评估。

## Intel GPU 研究

默认生产后端仍是 faster-whisper CPU/int8；VAAPI 视频编码接口不能直接加速 Whisper 推理。
Intel 核显可评估 [OpenVINO WhisperPipeline](https://docs.openvino.ai/2026/api/genai_api/_autosummary/openvino_genai.WhisperPipeline.html)。
模型格式、驱动和运行时均需匹配，识别到 GPU 不代表速度优于 CPU。
OpenVINO 是隔离实验依赖，不加入默认发布镜像。

使用同一音频比较冷启动、预热后耗时、内存与输出质量，并考虑 FP16/int8 的差异：

```bash
python -m scripts.diagnostics.benchmark_asr sample.wav --backend faster-whisper --model small --device cpu
python -m scripts.diagnostics.benchmark_asr sample.wav --backend openvino --model /models/whisper-small-fp16-ov --device GPU
```

工具只输出耗时和字符数，不输出正文。RSS 不代表独立显卡显存，核显会占共享内存。
无论推理设备是什么，都应保留有界音频分段。

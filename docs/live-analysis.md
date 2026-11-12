# 录制期间的实时分析

实时分析只处理已创建的录制任务，不会对全部直播频道持续运行 ASR。
手工录制和订阅均可独立关闭「录制时转写」，保留原来的录后处理。

## 数据流

```text
直播源 ──► recorder ──► 原始 TS ──► encoder（录制、AI 均到终态后）
                         │
                         └─本地音频旁路─► 已封口 WAV + MongoDB 分段任务
                                            ├─短窗 ASR ─► 实时文字、摘要草稿
                                            └─长窗 ASR ─► 正式文字、分段笔记
                                                             └─正式摘要 ─► push
```

音频旁路追读本地正在增长的 TS，不额外拉一次直播源；它不向录制主进程施加
输出背压。音频解码或实时识别失败时继续保存录像，录后走原有 ASR 恢复路径。
没有新增常驻服务，compact 模式的摘要和推送仍由 background 承担。

## 草稿与正式结果

- 短窗默认 30 秒，包含两侧上下文及右侧等待，提交约 20 秒的新内容。
  文字出现时间还包含输入探测、排队和推理时间，不是固定 30 秒。
- 短窗上下文较少，准确率可能低于长窗。实时文字和预览摘要明确属于草稿，
  不写入正式摘要集合，也不触发通知。
- 正式通道默认每 300 秒处理一次已封口音频，与录后 ASR 保持相同的窗口长度、
  模型和解码配置。节目结束时封口剩余音频，不必等 post-roll 或整场转写才开始工作。
- 正式摘要只使用正式通道的笔记；草稿笔记与正式笔记的缓存隔离。长文本分层归纳，
  不再直接截掉超出输入上限的尾部。
- 详情页分别显示录制、转写、摘要状态。正式摘要完成时录像可能仍在录制后滚动，
  此时播放入口提示等待收尾。

双通道会增加 ASR 推理量和 LLM 请求数。默认每 180 秒归纳草稿；一场 30 分钟
节目约有 10 次草稿归纳、6 次正式分段归纳与 1 次正式汇总，长文本或失败重试可能更多。
应按机器能力调整间隔，或对不需要时效的订阅关闭实时分析。

## 恢复与资源优先级

`live_chunks` 保存封口分段、通道、序号、时间范围、状态、领取租约和重试次数；
`live_notes` 保存分通道的增量笔记。一次录制尝试有独立 epoch，旧任务不能成为新尝试的输入。
分段消费成功后删除 WAV，异常退出的任务在租约过期后恢复，连续失败回退录后处理。
临时音频每日回收；分段记录和笔记保留 90 天。

一个 ASR 进程共享一个模型，优先领取正式实时分段，短窗与历史任务有界交错处理。
不为每个录制创建独立模型。GPU 选择和 CPU 自动回退沿用原有配置。
历史 ASR 每完成一个长窗口让出执行机会，因此历史积压、CPU 回退或多路录制时的
文字延迟仍可能上升；首期没有承诺硬实时延迟。

系统设置展示实时队列，录像详情展示文字进度、落后采集时长、待处理段数、
当前积压估算处理时间和实际推理设备。磁盘低于预留空间时停止音频旁路，优先保存录像。

正式摘要带 generation 和发布就绪标志，只有成功发布的正式版本可被推送领取。
推送有领取租约和心跳，重试跳过已经投递成功的渠道。外部通知发送成功但本地
确认前进程退出时仍可能重复通知，不能将 SMTP 等外部服务视为恰好一次事务。

## 配置与升级

```dotenv
LIVE_ASR_ENABLED=1
LIVE_ASR_CHUNK_SECONDS=30
LIVE_SUMMARY_INTERVAL_SECONDS=180
LIVE_AUDIO_PATH=/nas/iptv/live-audio
LIVE_AUDIO_MIN_FREE_BYTES=1073741824
ASR_CHUNK_SECONDS=300
LLM_POLL_SECONDS=5
```

recorder 与 ASR 必须以相同路径挂载 `/data/live-audio`；ASR 的录像目录仍可只读。
使用最新基础 Compose 时挂载已包含在内。保留旧版定制 Compose 的部署可叠加
`docker-compose.live-analysis.yml`，Intel ASR 继续叠加其 GPU overlay。
更新 recorder、ASR、web 和 background（split 模式为 llm/push 等相应进程），
使用定向部署等待活动任务完成并保存旧镜像及配置。encoder、数据库和第三方源服务
无需为此重建或重新部署。回退时应同时恢复上述服务和实时分析配置。

将 `LIVE_ASR_ENABLED=0` 后重新创建相关容器可关闭实时处理；未完成的实时任务
在录制结束后回到原有离线链路。详细操作见[定向升级指南](runtime-optimization.md)。

## 验收

测试必须指向隔离数据库，禁用测试推送。项目提供两类诊断工具：

```bash
# 真实采集、ASR 与模拟摘要；--real-llm 需要另行配置已授权的 LLM 服务。
TEST_MONGODB_URI=mongodb://test-db:27017 python -m scripts.diagnostics.validate_live_analysis \
  /path/to/speech.wav --output /tmp/live-result.json --openvino-model /path/to/whisper-model \
  --programme-seconds 330 --capture-seconds 450 --summary-interval 180

# 人工参考文本 CER 对比；需要评测环境安装 OpenCC。
ASR_LANGUAGE=zh python -m scripts.diagnostics.validate_live_quality \
  /path/to/manifest.jsonl --output /tmp/live-quality.json
```

第一类记录首字、草稿、正式摘要、录制完成的时间以及上游请求数、内存和设备。
第二类比较原始离线窗口与正式实时窗口，默认要求字符错误率增幅不超过 2 个百分点。
评测语料与凭据应仅保存在部署环境，不提交到公开仓库。

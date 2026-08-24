# 系统架构

IPTVPilot 是一个 Docker Compose 应用，由 Web 进程、多个轮询 Worker、MongoDB、MongoDB Search 和外部 iptv-api 容器组成。

```text
用户授权的 M3U / XMLTV 源
             │
             ▼
          iptv-api ──► channel-sync ───────┐
             │                             │
             └──────► epg-engine ──────────┤
                                           ▼
浏览器 ◄──────── FastAPI Web ◄──────── MongoDB/Search
                    │                      ▲
                    ├── 直播代理           │
                    ├── 媒体缓存           │
                    └── 管理 API           │
                                           │
scheduler ──► recorder ──► ASR ──► LLM ──► push
                    │
                    └── 录像文件
```

## 主要组件

- `iptv_pilot.web`：FastAPI、静态管理台、直播代理、录像播放和搜索。
- `iptv_pilot.epg_engine`：XMLTV 导入及频道元数据同步。
- `iptv_pilot.scheduler`：将匹配的订阅和节目单转换为录制任务。
- `iptv_pilot.recorder`：使用 FFmpeg 录制，失败时回退到隔离的 PyAV 进程。
- `iptv_pilot.asr`：转写已完成的录像，并可选生成向量。
- `iptv_pilot.llm`：将转写内容转换为经过校验的结构化摘要。
- `iptv_pilot.push`：发送通知并记录投递事件。
- `iptv_pilot.live_normalizer`：可选的 Intel VAAPI HLS 规范化服务。
- `scripts/`：配置、诊断、备份、清理和冒烟测试工具。

## 数据流

MongoDB 是频道、节目单、订阅、任务、录像、转写、摘要、收藏、设置和推送事件的控制平面。媒体文件和模型保存在宿主机挂载目录中，不写入数据库。

Worker 通过 MongoDB 原子更新领取任务。每个阶段都会记录状态和重试信息，便于后续轮询恢复停滞任务。

## 存储边界

Web 容器以只读方式挂载原始录像。浏览器兼容的 MP4 和规范化 HLS 分片写入独立缓存目录。数据库备份和录像备份需要分别管理。

## 网络边界

应用容器共享私有 Compose 网络，只有 Web 管理台和 iptv-api 发布宿主机端口。该设计假设这些端口只可从可信局域网或 VPN 访问。

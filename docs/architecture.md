# 系统架构

IPTVPilot 是一个 Docker Compose 应用：一个 Web 进程、一组轮询 Worker、MongoDB 与 MongoDB Search，以及一个可选的第三方 `iptv-api` 容器。

```text
用户授权的 M3U / XMLTV 源
             │
             ▼
          iptv-api ──► channel-sync ─────────┐
             │                              │
             └──────► epg-engine ───────────┤
                                            ▼
浏览器 ◄──────── FastAPI Web ◄──────── MongoDB / MongoDB Search
                    │                       ▲
                    ├── 直播代理            │
                    ├── 媒体缓存            │
                    └── 管理 API            │
                                            │
scheduler ──► recorder ──► encoder ──► ASR ──► LLM ──► push
     │            │           │
     └── 订阅      └── TS 文件  └── 720p MP4
```

## 服务与镜像

三个镜像变体由同一个 `Dockerfile` 用不同构建参数生成，发布时共用 `<版本>` 前缀：

| 镜像后缀 | 构建参数 | 用于 |
| --- | --- | --- |
| （无） | `INSTALL_AI_DEPS=0 INSTALL_FFMPEG=1` | 除下列两项外的所有服务 |
| `-ai` | `INSTALL_AI_DEPS=1 INSTALL_FFMPEG=1` | `recorder`、`encoder`、`asr` |
| `-live-normalizer` | `INSTALL_SYSTEM_FFMPEG=1` | `live-normalizer`（`hardware-accel` profile） |

`-ai` 变体额外包含 faster-whisper 与 PyAV；单独成镜像，是为了让基础镜像的拉取或重建不会悄悄移除这些依赖。

### 常驻服务

| 服务 | 镜像 | 职责 | 内存上限 |
| --- | --- | --- | --- |
| `mongodb` | `mongodb-community-server` | 控制平面数据库，单节点副本集 `rs0` | 2 GB |
| `mongot` | `mongodb-community-search` | 摘要与转录的全文/向量检索 | 2 GB |
| `iptv-api` | `guovern/iptv-api` | 拉取用户授权的 M3U 订阅，产出频道清单与 EPG | 1 GB |
| `channel-sync` | 基础 | 读取 `iptv-api` 产出的 M3U，同步 `channels` 与 `sources`；不覆盖锁定源 | 512 MB |
| `stream-quality` | 基础 | 周期性探测每条线路的可用性与带宽，按策略决定是否切换 | 1 GB |
| `epg-engine` | 基础 | 抓取并合并 `EPG_URLS` 中的 XMLTV 源，写入 `epg_programs` | 1 GB |
| `scheduler` | 基础 | 把匹配的订阅与节目单转换成录制任务，按频道+开始时间去重 | 512 MB |
| `recorder` | `-ai` | 并发录制，claim fencing + 心跳租约，失败回退到隔离 PyAV 进程 | 2 GB |
| `encoder` | `-ai` | ASR/摘要完成后把 TS 转成 720p MP4，校验时长后删除原文件 | 2 GB |
| `asr` | `-ai` | 用 faster-whisper 转写录像，可生成向量 | 3 GB |
| `llm` | 基础 | 调用 LLM 把转录转成结构化摘要 | 1 GB |
| `push` | 基础 | 按数据库中的推送配置发送通知并记录投递事件 | 512 MB |
| `web` | 基础 | FastAPI、管理台、直播代理、录像播放、搜索与管理 API | 1 GB |
| `live-normalizer` | `-live-normalizer` | 可选的 Intel VAAPI HLS 规范化（`hardware-accel` profile） | 2 GB |

### 一次性任务

| 服务 | 触发 | 职责 |
| --- | --- | --- |
| `mongo-init` | 启动时 | 建库、建集合、建索引、创建应用用户 |
| `mongot-secrets` | 启动时 | 把 mongot 密码写入共享卷（文件权限 0600） |
| `search-init` | 启动时 | 在 `summaries`/`transcripts` 上创建向量索引 |
| `backup` | `maintenance` profile | `mongodump` 备份并按保留期清理 |
| `cleanup-recordings` | `maintenance` profile | 删除超过保留期且未被收藏的录像 |

## 模块职责

| 模块 | 说明 |
| --- | --- |
| `iptv_pilot.web` | FastAPI 应用、静态管理台、HLS 直播代理、录像转封装与缓存、搜索 |
| `iptv_pilot.sources` | 多源数据模型：每频道一条锁定首选 + 若干备用；解析、CRUD、M3U 导入 |
| `iptv_pilot.epg_engine` | 多源 XMLTV 抓取、合并（靠前的源优先）与过期检测 |
| `iptv_pilot.scheduler` | 订阅匹配与录制任务生成 |
| `iptv_pilot.recorder` | 录制主循环、并发与租约；`encoder` 为转码 Worker |
| `iptv_pilot.asr` | faster-whisper 转写，可选向量化 |
| `iptv_pilot.llm` | 多 Provider 摘要生成与结果校验 |
| `iptv_pilot.push` | 邮件等通知渠道；配置优先读数据库，环境变量作默认值 |
| `iptv_pilot.live_normalizer` | 可选的 VAAPI 硬件转码与 HLS 发布 |
| `iptv_pilot.common` | 数据库连接、配置、向量工具、直播路径 |
| `scripts/` | 运维脚本，见下方分组 |

## 录像处理流水线

```text
scheduler 生成 queued
        │
        ▼
recorder 认领（claim_id + 频道租约）
        │  写入 /data/recordings/<频道>/<节目>_<时间>_<id>.part.ts
        ▼
completed ──► ASR ──► asr_done ──► LLM ──► summarized
        │                                    │
        └────────────► encoder ◄─────────────┘
                          │
                          ▼
                  720p MP4，删除原 TS
```

`encoder` 只在 ASR/摘要到达终态后领取任务，避免与转写争用同一文件。转码失败不会阻塞摘要，原 TS 会保留。

## 数据模型

MongoDB 中每个集合的职责：

| 集合 | 内容 |
| --- | --- |
| `channels` | 频道元数据与当前生效源镜像（`m3u_url`、`stream_candidates`） |
| `sources` | 每频道的有序线路列表，含锁定/启用/质量/失败计数 |
| `epg_programs` | 节目单，按 `(channel_id, start_time)` 唯一，90 天 TTL |
| `subscriptions` | 关键词或频道时间订阅规则 |
| `recordings` | 录制任务与状态机、源快照、claim/租约、转码状态 |
| `transcripts` | 转录文本、分段与可选向量 |
| `summaries` | 结构化摘要、推送状态 |
| `bookmarks` | 收藏 |
| `push_events` | 每个渠道的投递审计 |
| `settings` | 管理台可改的配置（缓冲档位、推送、录制压缩） |

`channels.m3u_url` 与 `stream_candidates` 是 `sources` 的派生镜像，供尚未迁移的旧逻辑读取；新代码只读写 `sources`。

## 数据流与并发

Worker 通过 MongoDB 单文档原子更新领取任务，避免多个容器抢同一条记录。每个阶段记录状态、重试次数与下一次尝试时间，停滞任务可在下一轮被回收。

录制器额外使用 `claim_id` fencing 与频道级租约：旧 Worker 的终态更新会因为 `claim_id` 不匹配而被拒绝，崩溃后任务可被其他 Worker 接管。

## 存储边界

下方目录由宿主机挂载，媒体与模型不写入数据库：

| 主机目录（默认变量） | 容器内路径 | 用途 |
| --- | --- | --- |
| `MONGODB_DATA_PATH` | `/data/db` | 数据库 |
| `MONGOT_DATA_PATH` | `/data/mongot` | 搜索索引 |
| `RECORDINGS_PATH` | `/data/recordings` | 原始录像；Web 只读挂载 |
| `MEDIA_CACHE_PATH` | `/data/media-cache` | 浏览器播放缓存与规范化 HLS |
| `MODELS_PATH` | `/root/.cache` | ASR/嵌入模型 |
| `M3U_DATA_PATH` | `/data/m3u` | M3U 与 EPG 文件 |
| `BACKUP_PATH` | `/data/backup` | 数据库备份 |

## 网络边界

应用容器共享私有 Compose 网络，只有 `web` 与 `iptv-api` 发布宿主机端口。默认部署假设这些端口仅可从可信局域网或 VPN 访问，详见[安全政策](../.github/SECURITY.md)。

`mongodb`、`mongot` 与其余 Worker 不发布端口，只通过 Compose 网络通信。

## 相关文档

- [部署指南](deploy.md)
- [首次添加直播源](live-sources.md)
- [REST API](api.md)
- [多源管理说明](live-sources.md#多源与首选锁定)

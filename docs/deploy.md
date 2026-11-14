# 部署指南

IPTVPilot 面向运行 Docker Compose 的单机家庭服务器或 NAS，首个开源版本以 x86_64 Linux 为正式支持环境。其他架构可能需要替换媒体、AI 或数据库搜索依赖。

## 1. 准备存储目录

请分别准备以下主机目录：

- MongoDB 数据
- MongoDB Search 数据
- 录像
- 浏览器媒体缓存
- AI 模型
- M3U 与 EPG 数据
- iptv-api 配置
- 数据库备份

复制配置模板：

```bash
cp .env.example .env
chmod 600 .env
```

修改 `.env` 中的全部存储路径。不要对共享存储根目录递归修改权限。MongoDB 容器通常以 UID/GID 998 写入，只应为所选 MongoDB 数据目录授予相应权限。

## 2. 配置凭据

替换两个数据库占位密码，并保持 `MONGODB_URI` 中的密码与 `IPTV_DB_PASSWORD` 一致：

```dotenv
IPTV_DB_PASSWORD=使用随机长密码
MONGOT_PASSWORD=使用另一个随机长密码
MONGODB_URI=mongodb://iptv_user:使用随机长密码@mongodb:27017/iptv_pilot?authSource=iptv_pilot&replicaSet=rs0
```

不要将 `.env` 放入备份截图、Issue、群聊或命令输出中，也不要提交到 Git。

## 3. 配置 IPTV 与 EPG

仓库不包含可工作的频道、订阅或第三方 EPG 地址，全新部署显示 0 个频道是预期状态。只使用自有或已获授权的源。

批量导入前必须先启动一次 `iptv-api`，让它初始化完整的持久化配置；不要在首次启动前直接运行配置脚本。可先复制 `config/iptv-api/` 中的注释模板到仓库外的私有目录，实际导入步骤放在第 6 节。

`EPG_URLS` 接受逗号分隔的降级列表。默认只使用共享 M3U 目录中的本地 XMLTV 文件：

```dotenv
EPG_URLS=/data/m3u/epg/epg.xml
```

## 4. 配置可选 AI 功能

默认配置使用 mock 后端，基础应用镜像不安装大型 AI 依赖：

```dotenv
INSTALL_AI_DEPS=0
INSTALL_EMBEDDING_DEPS=0
ASR_BACKEND=mock
EMBEDDING_BACKEND=none
LLM_PROVIDER=mock
```

ASR 使用独立的 AI 镜像（`INSTALL_AI_DEPS=1`）；recorder、encoder 和 Web 使用包含 PyAV/FFmpeg 的媒体基础镜像。默认使用本地镜像并设置 AI `pull_policy: never`：

```dotenv
IPTV_PILOT_AI_IMAGE=iptv-pilot-ai
IPTV_PILOT_AI_TAG=local
```

首次启用或升级后构建 AI 镜像：

```bash
docker compose build asr
docker compose up -d asr
```

需要配置 faster-whisper 时设置：

```dotenv
ASR_BACKEND=faster_whisper
```

模型体积较大，第一次转写也会更慢；可通过 `MODELS_PATH` 复用本地 Hugging Face 缓存，并用 `HF_HUB_OFFLINE=1` 避免联网检查。使用已发布的 AI 镜像时，设置 `IPTV_PILOT_AI_IMAGE=ghcr.io/karlazx/iptv-pilot`、`IPTV_PILOT_AI_TAG=<版本>-ai`，并把 `IPTV_PILOT_AI_PULL_POLICY` 改为 `missing`（本地构建保持 `never`）。

需要 Intel GPU ASR 时，使用可选的 `-asr-intel` 镜像和 [ASR 加速说明](asr-acceleration.md) 中的设备配置。
自动模式在设备、驱动、模型或推理不可用时回退 CPU，不必升级其它服务。

Web 服务与其它 worker 共用基础镜像，其中已包含 imageio-ffmpeg，可直接在浏览器播放录像，不再需要单独的 `-web` 镜像。

需要生成真实摘要时，配置任一受支持的 LLM Provider 及其 API Key。OpenAI 兼容的自定义服务使用 `LLM_PROVIDER=custom`、`LLM_BASE_URL`、`LLM_MODEL` 和 `LLM_API_KEY`。

向量搜索默认关闭。启用 BGE-M3 需要设置 `INSTALL_EMBEDDING_DEPS=1`，会增加镜像构建时间、模型磁盘占用和运行内存。

通知默认关闭。可在管理台「通用设置 → 推送配置」里选择渠道并填写 SMTP，保存后即时生效；也可继续用环境变量 `PUSH_PROVIDER` / `SMTP_*` 作为初始默认值。

## 5. 验证并启动

Compose 默认构建本地镜像。使用已发布版本时，将 `IPTV_PILOT_IMAGE` 设置为 `ghcr.io/karlazx/iptv-pilot`，并将 `APP_TAG` 设置为不带前导 `v` 的版本号。

```bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

默认启动 MongoDB、副本集初始化和核心应用。后台任务由 background 承担；独立 worker 使用 split profile。
MongoDB Search、第三方源订阅器、硬件规范化均按需启用。模式选择、迁移与定向升级见[运行优化指南](runtime-optimization.md)。

查看启动日志：

```bash
docker compose logs -f mongodb mongo-init background web
```

验证 Web 服务和数据库：

```bash
curl http://127.0.0.1:${WEB_PORT}/health
docker compose exec mongodb mongosh --quiet --eval 'rs.status().ok'
docker compose exec mongodb mongosh --quiet --eval 'db.getSiblingDB("iptv_pilot").channels.countDocuments()'
```

## 6. 首次添加直播源

推荐先通过管理台添加一个频道，确认播放链路；需要导入自建频道清单或授权 M3U/TXT 订阅时，使用容器化导入助手。该助手兼容 `iptv-api` 首次初始化后配置目录归 `root` 所有的情况，无需递归修改 NAS 目录权限。

完整的手动添加、批量导入、EPG 配置、验证与排错步骤见[首次添加直播源](live-sources.md)。

## 7. 直播规范化服务

可选的 `live-normalizer` 服务使用 Intel VAAPI，并映射 `/dev/dri/renderD128`。默认启动不会包含该服务，其他频道继续使用普通同源代理。

主机支持 VAAPI 时，只列出确实需要时间戳和分片规范化的频道：

```dotenv
LIVE_NORMALIZE_CHANNELS=我的频道
LIVE_NORMALIZE_MAX_CHANNELS=1
```

启动硬件加速 profile：

```bash
docker compose --profile hardware-accel up -d --build
```

## 8. 内网使用边界

默认部署面向可信局域网，不强制设置 `WEB_API_TOKEN`。请勿在公网路由器上转发 5190 或 5181 端口。

确需远程访问时，优先使用 VPN；也可部署 HTTPS 反向代理并配置 `WEB_API_TOKEN`。启用令牌前，请阅读[安全政策](../.github/SECURITY.md)中关于媒体播放路径的限制。

MongoDB 和 MongoDB Search 端口不得发布到宿主机或公网。

## 9. 录制并发

录制器默认同时处理 2 路任务，可在 `.env` 调整：

```dotenv
RECORDING_CONCURRENCY=2          # 1-8，整个容器同时录制的任务数
RECORDING_MAX_PER_CHANNEL=2      # 同一频道最多同时录制几路
RECORDING_LEASE_SECONDS=120      # 认领租约；心跳丢失超过 2 倍即允许接管
RECORDING_HEARTBEAT_SECONDS=30
RECORDING_LATE_GRACE_SECONDS=300 # 节目结束后多久不再补录
RECORDING_MIN_WINDOW_SECONDS=60  # 剩余窗口不足时不再重试
```

`recorder` 默认限制 2 GB 内存。每路录制是 `-c copy` 转封装，不转码；4 路以上请先确认磁盘写入和内存余量。

同一频道允许 2 路重叠录制（udpxy/桥接支持多客户端）。达到上限的第三条任务会保持排队，若到窗口结束前仍无槽位则标记 `failed` 并写入 `skip_reason="overlap_wait_timeout"`。心跳停止或容器退出的任务由监督循环回收；终态更新带 `claim_id` fencing，旧 worker 不会覆盖新 worker 的结果。

升级后执行一次索引迁移（可先 dry-run）：

```bash
docker compose run --rm --no-deps recorder python -m scripts.migrations.migrate_recorder_concurrency --dry-run
docker compose run --rm --no-deps recorder python -m scripts.migrations.migrate_recorder_concurrency --apply --merge
```

### 录制压缩

录制器以原画质 `-c copy` 保存 TS；`encoder` 在采集结束后独立转成 720p MP4（H.264 CRF 26），与 ASR/LLM 并行。时长校验通过后发布 MP4，原 TS 等待 ASR 完成且读取租约释放后清理；中断片段保留原件。可在管理台「通用设置 → 录制压缩」或环境变量中调整：

```dotenv
RECORDING_ENCODE_ENABLED=true
RECORDING_ENCODE_HEIGHT=720      # 0 表示保持原分辨率
RECORDING_ENCODE_CRF=26
RECORDING_ENCODE_PRESET=veryfast
RECORDING_ENCODE_KEEP_ORIGINAL=false
RECORDING_ENCODE_CONCURRENCY=1
```

首次启动会把历史已完成、且尚未标记 `encode_status` 的录像排队转码（`RECORDING_ENCODE_BACKLOG=true`）。压缩比例取决于输入内容与画质设置。转码失败不会阻塞 ASR/摘要，原 TS 会保留。

## 10. 备份与清理

执行数据库备份：

```bash
docker compose --profile maintenance run --rm backup
```

清理过期且未收藏的录像：

```bash
docker compose --profile maintenance run --rm cleanup-recordings
```

应先完成一次手工备份和恢复演练，再配置计划任务。数据库备份不包含录像文件，录像需要独立的存储备份策略。

## 11. 升级流程

优先使用[定向升级工具](runtime-optimization.md)仅更新受影响的服务，等待活动任务、
保留回退镜像和 Compose 配置。实时分析的共享音频目录与旧 Compose 迁移见[实时分析部署](live-analysis.md#配置与升级)。

项目为基础镜像和配套服务提供了固定的默认版本，避免 `latest` 在无人确认时引入不兼容改动。尤其是 iptv-api 3.x 包含配置变化，升级前应先阅读其发布说明并在独立环境验证。

升级前：

1. 备份 MongoDB 和重要录像。
2. 阅读 `CHANGELOG.md` 中的迁移说明。
3. 验证新的 Compose 配置。
4. 重新构建镜像并启动服务。
5. 检查健康状态、Worker 日志、频道同步和短时录制。

```bash
git pull --ff-only
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

## 已知限制

- MongoDB 使用单节点副本集，不具备高可用能力。
- 直播规范化目前只面向 x86_64 Linux 上的 Intel VAAPI。
- 鉴权只有一个可选 Bearer Token，不是多用户身份系统。
- 录像和部分规范化直播路径尚未全部使用短期签名 URL。
- AI 功能可能占用较多内存、磁盘和构建时间。

## 12. 主机内存与 earlyoom

录制、转码和 ASR 都可能在短时间内占用较多内存。若宿主启用了 `earlyoom`，内存耗尽时它可能杀掉 `containerd` 本身，导致所有容器的进程消失、`docker ps` 却仍显示 `Up`——此时网页会直接打不开，`docker compose up -d` 还会因为 `mongo-init` 无法连接数据库而失败。

排查与恢复：

```bash
journalctl -u containerd --since "-1h" | grep -E "killed|Started"
journalctl --since "-1h" | grep -E "escalating to SIGKILL|sending SIGKILL"
docker compose up -d --force-recreate
```

Compose 已为所有常驻服务设置 `mem_limit`，但这只是单容器上限，不限制主机整体。建议：

- 把 `earlyoom` 的内存阈值从默认 10%/5% 调高，或把 `containerd`、`dockerd` 加入 `--avoid` 正则，避免容器运行时被优先杀掉。
- 与其它内存占用大的服务（媒体转码、相册索引等）错开运行时间。
- 用 `docker stats` 观察 `encoder` / `asr` 是否频繁接近上限。

# 极空间 Z4Pro 部署手册

## 1. 前置检查

- Docker Compose ≥ 2.21，NAS CPU 架构为 x86_64；本项目已在极空间 Compose 2.21.0 上实测通过。
- 建议 MongoDB 与 mongot 数据放 M.2 SSD，录制文件放机械盘阵列。
- 不要把 MongoDB 27017、mongot 27028/8080/9946 暴露到公网。
- 直播源、录制和转发只用于自有或已授权内容。

MongoDB 官方要求自托管 Search 使用 MongoDB 8.2+、副本集和独立 `mongodb/mongodb-community-search` 容器。本工程已按该接线生成 `mongod.conf`、`mongot.conf`、同步账号及搜索索引初始化任务。

## 2. 目录与配置

```bash
cp .env.example .env
```

编辑 `.env`：

1. 设置八个存储路径；推荐 `MONGODB_DATA_PATH`、`MONGOT_DATA_PATH`、`MODELS_PATH` 放 NVMe，`RECORDINGS_PATH`、`MEDIA_CACHE_PATH`、`M3U_DATA_PATH`、`IPTV_API_CONFIG_PATH`、`BACKUP_PATH` 放机械盘。
2. 修改 `IPTV_DB_PASSWORD`、`MONGOT_PASSWORD`，并同步修改 `MONGODB_URI` 中的应用密码。
3. 配置 `LLM_PROVIDER` 与对应 Key。
4. 主动推送使用 `PUSH_PROVIDER=email` 和 `PUSH_EMAIL_MODE=always`；通知投递到 Coze Agent 邮箱，由 Coze 负责后续提醒。

Phase 4 当前只启用邮件推送：`PUSH_PROVIDER=email` 与 `PUSH_EMAIL_MODE=always`。163 邮件使用 `smtp.163.com:465`、SSL 和客户端授权码，不使用登录密码；`SMTP_TO` 配置为 Coze Agent 邮箱。Coze API 会话与飞书等发布渠道相互隔离，因此 `CozeProvider` 代码仅保留备用，不作为主动通知通道。

真实秘密只写入 NAS `.env`。可以通过标准输入运行 `scripts/configure_phase4_from_stdin.py .env`，脚本会原子更新配置、保留其他 Key，并将权限保持为 `0600`；不要把 JSON 内容记录到 shell history 或群聊。

Phase 4 冒烟：

```bash
docker compose run --rm --no-deps push python -m scripts.smoke_phase4 --provider coze
docker compose run --rm --no-deps push python -m scripts.smoke_phase4 --provider email
```

扣子测试同时验证消息可回读及关联 Bot 的 API 会话列表可查询。API/Chat SDK 会话与扣子手机 App 渠道不是同一个可见性承诺，App 是否展示或通知需在客户端人工确认。

DeepSeek 使用 OpenAI 兼容的 `custom` Provider：`LLM_PROVIDER=custom`、`LLM_BASE_URL=https://api.deepseek.com`，并设置 DeepSeek 当前有效的 `LLM_MODEL` 与 `LLM_API_KEY`。可用 `scripts/configure_deepseek.py <source-file> .env` 从 `model_id: ...` / `api_key: ...` 格式的私有文件安全写入；脚本不会打印密钥。随后运行 `scripts/smoke_llm_api.py .env` 验证模型与 JSON Output。

也可用脚本一次性生成随机数据库密码并配置路径（只会覆盖空的 `.env`）：

```bash
./scripts/configure_env.sh /path/on/nvme/iptv /path/on/hdd/iptv
```

创建目录：`db`、`db-search`、`recordings`、`media-cache`、`models`、`m3u`、`iptv-api-config`、`backup`。官方 MongoDB 8.2 镜像以 UID/GID 998 运行，`db` 目录必须允许 `998:998` 写入；例如先精确确认路径，再通过 root 权限执行 `chown -R 998:998 <MONGODB_DATA_PATH>` 和 `chmod 750 <MONGODB_DATA_PATH>`。不要对共享盘根目录递归改权限。

现有部署升级到 Phase 6 时运行：

```bash
python3 scripts/configure_phase6.py --env-file .env
docker compose config -q
docker compose build web
docker compose up -d --no-deps web
docker compose exec -T web python scripts/smoke_phase6.py
```

`configure_phase6.py` 只补充非密钥媒体缓存配置并维持 `.env` 为 `0600`。Web 容器只读挂载原始录像，MP4 播放缓存写入独立的 `MEDIA_CACHE_PATH`；默认上限 20 GiB。

CCTV-1 的 50 fps 上游流存在异常 DTS/PTS，当前使用独立 `live-normalizer` 做服务端规范化。NAS 必须提供 Intel 渲染设备 `/dev/dri/renderD128`；服务通过 `h264_vaapi` 输出 1280×720、25 fps、约 3.5 Mbps、严格 2 秒分片的本地 HLS。升级或复验时执行：

```bash
docker compose build live-normalizer web
docker compose up -d --no-deps live-normalizer web
docker compose ps live-normalizer web
curl -sD - -o /dev/null http://127.0.0.1:${WEB_PORT}/api/channels/CCTV-1/live
```

最后一个请求应返回 `X-Live-Mode: normalized`。规范化清单超过 15 秒未更新时，Web 会自动回退原上游代理，不会把陈旧分片继续交给播放器。Web 使用独立精简镜像，不安装 ASR、PyAV 或 FFmpeg；核显转码依赖只存在于 `live-normalizer` 镜像。

## 3. 配置验证与启动

Phase 3 可用 `python3 scripts/configure_phase3.py --env-file .env --web-port 5190` 原子补齐非密钥设置；脚本保留现有数据库与 LLM 密钥，并维持 `.env` 为 `0600`。随后运行：

```bash
docker compose config
docker compose pull
docker compose build
docker compose up -d
docker compose ps
```

首次启动顺序：`mongodb` → `mongo-init` → `mongot` → `search-init` → 应用服务。`channel-sync` 每 5 分钟检测一次 iptv-api 的 `result.m3u`，有变化即 upsert 到 `channels`。查看日志：

```bash
docker compose logs -f mongodb mongo-init mongot search-init
```

`search-init` 会等待 mongot 就绪并创建两个 1024 维 cosine 向量索引。若移动标签导致版本不兼容，请根据 MongoDB 官方兼容表固定 `MONGOT_TAG`；不要盲目升级其中一个镜像。

## 4. 验收

```bash
curl http://127.0.0.1:${WEB_PORT}/health
curl http://127.0.0.1:5181/m3u
docker compose exec mongodb mongosh --quiet --eval 'rs.status().ok'
docker compose exec mongodb mongosh --quiet --eval 'db.getSiblingDB("iptv_manager").channels.countDocuments()'
```

向量检索升级复验使用独立客户端容器，避免 `mongosh` 占用 MongoDB 容器的内存配额：

```bash
docker run --rm --memory=512m --network iptv-manager_iptv \
  -v "$PWD/scripts/smoke_vector.js:/scripts/smoke_vector.js:ro" \
  --entrypoint mongosh mongodb/mongodb-community-server:8.2.0-ubi9 \
  'mongodb://mongodb:27017/iptv_manager?replicaSet=rs0' \
  --quiet --file /scripts/smoke_vector.js
```

iptv-api 当前官方镜像名为 `guovern/iptv-api`。本工程持久化其 `/iptv-api/config` 与 `/iptv-api/output`，分别映射到 `${IPTV_API_CONFIG_PATH}` 和 `${M3U_DATA_PATH}`，避免重建容器后丢失订阅、模板、EPG 与本地源。首次挂载前应先从镜像复制完整默认配置，不能挂载空目录。

仓库内 `config/iptv-api/official_international.txt` 只收录已在 NAS 完成真实解码测试、且发布方来源可确认的免费国际直播流。复制到 `${IPTV_API_CONFIG_PATH}/local/` 后运行：

```bash
python3 scripts/configure_iptv_api.py "$IPTV_API_CONFIG_PATH" .env \
  --include-community-domestic
docker compose up -d --force-recreate iptv-api
```

脚本会原子更新 `.env`，并在 `demo.txt` 中幂等维护国际频道受管区块。`--include-community-domestic` 是显式开关，会把 `config/iptv-api/community_domestic_subscriptions.txt` 写入 `subscribe.txt` 的受管区块；不带该参数时保留现状。当前国内列表和 BBC 入口均由用户明确选择接入，并标记为社区维护来源。仅返回空壳主清单的 ABC 旧地址不纳入配置。

Phase 2 使用 iptv-api 已映射的 `${M3U_DATA_PATH}/epg/epg.xml` 作为 EPG 首选，外部 EPG 作为降级备用。运行 `python3 scripts/configure_epg.py` 幂等更新 `.env`；`epg-engine` 会禁用只有 EPG 但没有播放地址的频道，避免误建录制任务。

当前 NAS 网络实测不支持 IPv6，因此默认设置 `IPTV_API_IPV_TYPE=ipv4` 和 `IPTV_API_IPV_TYPE_PREFER=ipv4`。如果以后确认 NAS 已获得可用 IPv6，再将两项改为 `all` / `ipv6` 并重新测速；不要在 IPv6 不可达时保留 IPv6 首选线路。

## 5. ASR 与资源

- 正式部署保留 `INSTALL_AI_DEPS=1`，首次构建和首次模型下载耗时较长。
- N305 建议 `ASR_MODEL=small`、`ASR_COMPUTE_TYPE=int8`、一次只运行一个 ASR worker。
- 当前 NAS 的 Hugging Face 直连不可达，ASR 使用 `MODEL_HTTP_PROXY=http://host.docker.internal:7890` 经 NAS 本机 Mihomo 下载模型；该代理只注入 ASR 容器。
- recorder 优先使用 `imageio-ffmpeg` 提供的静态 ffmpeg；若进程崩溃或输入流不兼容，会在剩余录制窗口内自动切换到隔离的 PyAV 子进程，不依赖 Debian apt 镜像。
- Web 直播通过同源 HLS 提供，`LIVE_PROXY_TIMEOUT_SECONDS` 默认 20 秒，`LIVE_TOKEN_TTL_SECONDS` 默认 900 秒；hls.js 已随静态资源本地化，不依赖外部 CDN。CCTV-1 优先使用 `live-normalizer` 生成的连续本地流，其他频道继续使用同源代理和约 20 秒稳定缓冲。
- `stream-quality` 每小时对有多条线路的频道做持续分片下载测试。只有候选线路真实可取分片且具备稳定余量时才切换；存在排队或正在录制任务的频道不会切换。可通过 `STREAM_QUALITY_*` 变量调整周期、样本数、超时和并发。
- BGE-M3 embedding 默认关闭，避免 Phase 3 基础镜像额外增加约 2.3GB；启用时同时设置 `INSTALL_EMBEDDING_DEPS=1` 与 `EMBEDDING_BACKEND=sentence_transformers` 后重建。
- 仅验证骨架时可设 `INSTALL_AI_DEPS=0`、`ASR_BACKEND=mock`、`EMBEDDING_BACKEND=none`；mock 输出不可作为真实摘要。

端到端复验：

```bash
python3 scripts/smoke_phase3.py \
  --base-url http://127.0.0.1:${WEB_PORT} \
  --channel-id CCTV-1 --duration 30 --asr-model base
```

## 6. 备份与清理

用 NAS 计划任务每日 03:00 执行：

```bash
docker compose --profile maintenance run --rm backup
docker compose --profile maintenance run --rm cleanup-recordings
```

建议先手工执行并完成恢复演练，再把以上两条命令分别加入 NAS 的 03:00 备份任务与每日清理任务。

录制文件不能使用 Mongo TTL 自动清理，因为必须先删文件。每日运行 `scripts/cleanup_recordings.py`，脚本会跳过收藏项，再删除超过 30 天的文件与记录。

## 7. 已知边界

- `vector=true` 的查询向量化需要单独安装 BGE-M3 依赖；默认关闭时 API 会明确返回提示，不伪装成语义检索。
- 主动通知目前只有邮件通道；Coze API 会话不会同步到飞书发布渠道。
- `WEB_API_TOKEN` 当前为空，仅适合可信局域网。启用 Bearer 鉴权后，原生 `<video>` 与下载链接还需要短期签名 URL 才能继续工作。
- MongoDB 单节点副本集没有高可用，必须依靠每日备份和 NAS 存储冗余。

# IPTVPilot

[![CI](https://github.com/karlazx/iptv-pilot/actions/workflows/ci.yml/badge.svg)](https://github.com/karlazx/iptv-pilot/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/karlazx/iptv-pilot)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-linux%2Famd64-blue)](ROADMAP.md)

> Smart IPTV subscriptions, channel management, and program digests.

IPTVPilot 是一套面向可信家庭内网的自托管 IPTV 智能管家，通过 Docker Compose 将频道与节目单管理、预约录制、浏览器播放、语音转写、AI 节目摘要和邮件通知整合在一起。

> [!IMPORTANT]
> IPTVPilot 不提供任何可用的频道地址、IPTV 订阅或第三方 EPG 源。请只配置自有或已获授权的内容源，并遵守所在地法律、内容许可和服务条款。

## 主要功能

- 在统一管理台中维护频道和多条候选线路。
- 导入 XMLTV 节目单，按关键词或频道时间创建订阅。
- 使用 FFmpeg 录制节目，并在失败时回退到 PyAV。
- 在浏览器中播放 HLS 直播及兼容格式的录像。
- 使用 faster-whisper 转写录像。
- 通过 OpenAI 兼容的 LLM 服务生成结构化节目摘要。
- 使用邮件发送摘要通知。
- 通过维护任务备份 MongoDB、清理过期录像。

## 运行要求

- Docker Engine 和 Docker Compose 2.21 或更高版本。
- x86_64 Linux 主机或 NAS。可选的直播规范化服务目前依赖 `/dev/dri/renderD128` 上的 Intel VAAPI。
- 足够存放 MongoDB 数据、媒体缓存、录像、模型和备份的磁盘空间。
- 用户自行提供且已获授权的 M3U/HLS 和 XMLTV 源。

首个开源版本只正式支持 `linux/amd64`。ARM64 和更通用的 CPU-only 部署计划见[路线图](ROADMAP.md)。

## 快速开始

```bash
git clone https://github.com/karlazx/iptv-pilot.git
cd iptv-pilot
cp .env.example .env
```

编辑 `.env`，至少完成以下配置：

1. 替换 `IPTV_DB_PASSWORD`、`MONGOT_PASSWORD`，并同步修改 `MONGODB_URI` 中的密码。
2. 将所有 `/nas/iptv/...` 存储路径改为主机上的实际目录。
3. 配置 LLM 服务；仅做本地功能检查时可保留默认的 `LLM_PROVIDER=mock`。

启动服务：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

打开 `http://你的_NAS_IP:5190`。配套的 iptv-api 服务默认使用端口 `5181`。

全新部署显示 0 个频道是预期状态。最快的验证方式是在“系统设置 → 频道管理”点击“添加频道”，填入一个自有或已获授权的 HLS 地址；已有频道清单或 M3U/TXT 订阅时，可使用批量导入助手。完整步骤见[首次添加直播源](docs/live-sources.md)。

默认配置仅适用于可信内网，不要将管理台或 iptv-api 直接暴露到公网。部署边界详见[安全政策](SECURITY.md)。

## 文档

- [部署指南](docs/deploy.md)
- [首次添加直播源](docs/live-sources.md)
- [REST API](docs/api.md)
- [系统架构](docs/architecture.md)
- [项目路线图](ROADMAP.md)
- [参与贡献](CONTRIBUTING.md)
- [安全政策](SECURITY.md)

本项目的管理台、API 提示和维护文档统一使用简体中文，暂不计划提供界面国际化。

## 本地开发

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt pytest ruff
python -m pytest
ruff check .
```

Python 包名为 `iptv_pilot`。Docker 镜像统一使用 `iptv-pilot` 仓库名，Compose 通过不同 tag 区分默认、Web 和直播规范化变体。

发布版本会将带 SBOM 和构建来源证明的镜像推送到 `ghcr.io/karlazx/iptv-pilot`。使用已发布镜像时，在 `.env` 中设置：

```dotenv
IPTV_PILOT_IMAGE=ghcr.io/karlazx/iptv-pilot
APP_TAG=0.1.0
```

Compose 仍保留本地构建定义，用户可以自行构建全部镜像。

## 项目状态

IPTVPilot 目前处于早期阶段。在首个稳定版本发布前，接口和部署默认值可能发生变化。升级前请备份数据库和重要录像。

## 开源协议

Copyright 2026 karlazx。项目采用 [Apache License 2.0](LICENSE) 开源。

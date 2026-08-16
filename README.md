# IPTV 智能管家

面向极空间 Z4Pro 的单机 Docker Compose 工程：直播源更新、EPG 索引、订阅调度、FFmpeg/PyAV 容错录制、Whisper 转写、LLM 摘要、邮件推送及支持直播与录像播放的 FastAPI 管理台。

## 快速开始

1. 复制 `.env.example` 为 `.env`，至少修改数据库密码与 LLM 配置。
2. 在 NAS 建立 `.env` 中 `IPTV_DATA_ROOT` 对应的目录。
3. 执行 `docker compose config` 检查配置。
4. 执行 `docker compose up -d --build`。
5. 打开 `http://NAS_IP:5190` 使用完整管理台；iptv-api 页面默认位于 `http://NAS_IP:5181`。

管理台包含节目单、订阅、录制、摘要、收藏、系统六个页面。录像首次播放会在独立缓存目录中生成浏览器兼容 MP4，原始录制保持只读；后续播放直接复用缓存。

部署细节见 `docs/deploy.md`，API 定义见 `docs/api.md`。

> 本项目不提供直播源。请只使用自有或已获授权的节目源，并遵守所在地法律、平台条款与内容许可要求。

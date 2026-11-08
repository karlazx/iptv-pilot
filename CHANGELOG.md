# 变更日志

IPTVPilot 的重要变化将记录在此文件中。首次发布标签后，项目将遵循语义化版本规则。

## 尚未发布

## 0.1.0-rc.9 - 2026-11-07

- EPG 引擎改为**合并所有配置的源**，不再只使用第一个成功的源；单个源失败只影响它自己。
- 重复的 `(频道, 开始时间)` 按配置顺序取先出现的源。
- 每个源的频道/节目数单独记入日志，便于发现某个源在缩水。
- `/api/system/status` 新增 `epg_upcoming_24h`、`epg_latest_start`、`epg_status`；未来 24 小时无节目时记录 error 日志。

## 0.1.0-rc.8 - 2026-11-06

- 修复频道检测无限制下载的问题：播放列表改为流式读取并限制 2 MiB，拒绝 `video/*` / `audio/*` 内容类型和非 HLS 响应；分片下载限制 64 MiB。
- 实测确认：1.85 GB 的 MP4 被误当作直播源时，修复前会使 1 GB 容器 OOM，修复后 0.2 秒拒绝、峰值 4.1 MB。
- 检测失败的提示改为可读原因（如 `not a playlist: content-type video/mp4`）。

## 0.1.0-rc.7 - 2026-11-06

- 仓库整理：根目录只保留构建入口、README、LICENSE、CHANGELOG 和配置样例；社区文档移入 `.github/`，路线图与第三方声明移入 `docs/`。
- `requirements-*.txt` 合并为 `requirements/` 目录（`base.txt` / `ai.txt` / `embedding.txt`）。
- `scripts/` 按用途分组为 `db/`、`migrations/`、`channels/`、`setup/`、`diagnostics/`、`smoke/`，Compose 与文档同步更新。
- 删除未被任何构建或 CI 使用的 `uv.lock`，以及已被管理台取代的 `configure_push_from_stdin.py`、`diagnose_coze_api.py`。
- 合并 `SUPPORT.md` 到 `.github/CONTRIBUTING.md`。
- 所有常驻服务补上 `mem_limit`；修复 `stream-quality` 在目录整理后指向旧模块路径、升级后反复重启的问题。
- 转码时长校验阈值从 0.98 放宽到 0.95，并可通过 `RECORDING_ENCODE_MIN_DURATION_RATIO` 配置。
- 部署文档新增「主机内存与 earlyoom」一节，记录 OOM 杀掉 `containerd` 后的现象与恢复步骤。

## 0.1.0-rc.6 - 2026-11-06

- 直播源管理改为「总源 → 频道 → 线路」两级结构，按官方组播 / IPTV-API / 第三方 M3U / 自定义 / 导入分组。
- 添加频道时可一次填写多条线路，第一条锁定为首选、其余作为备用；频道详情抽屉可直接新增、删除、设为首选。
- 新增 `PUT /api/channels/{id}/sources`，用提交的线路列表替换用户自建线路。
- 推送配置可在管理台修改（渠道、收件人、SMTP、密码），保存后即时生效；密码不回显，支持发送测试邮件。
- 通用设置页改为左右两栏：左侧配置、右侧推送事件与状态；推送事件列表限制高度并内部滚动。
- 新增 `encoder` 服务：录制完成后在后台转 720p / H.264 / AAC 的 MP4（CRF 26，可配），校验时长后删除原 TS；转码失败不影响 ASR/摘要。
- 录制列表与详情显示压缩状态、原始/压缩后体积。
- 移除独立的 `-web` 镜像变体，Web 服务复用基础镜像（含 imageio-ffmpeg）。
- 新增 `scripts/migrations/migrate_rc6.py` 与录制/推送相关索引。

## 0.1.0-rc.5 - 2026-11-05

- 录制器支持并发：默认 2 路（`RECORDING_CONCURRENCY`），同频道默认 2 路（`RECORDING_MAX_PER_CHANNEL`）。
- 认领任务增加 `claim_id` fencing、心跳和频道租约；worker 崩溃后可安全接管，旧 worker 不会覆盖新结果。
- 过期窗口不再补录；同频道排队到窗口关闭仍无槽位时标记 `overlap_wait_timeout`。
- 定时任务按 `(channel_id, start_time)` 去重，多订阅命中同一节目只录一份。
- `/api/system/status` 暴露运行中/排队录制和 worker 明细；录像页每 10 秒刷新。
- 新增 `scripts/migrations/migrate_recorder_concurrency.py` 索引与重复任务迁移脚本。
- `recorder` 容器限制 2 GB 内存。

## 0.1.0-rc.4 - 2026-11-05

- 新增 `sources` 集合：每个频道维护有序源列表，支持一个锁定的首选源和多条备用源。
- 首选源在 M3U 同步、质量刷新和直播规范化中只更新元数据，**不会被自动替换**；只有用户显式“设为首选”才会更换。
- 直播播放按优先级自动回退，首选恢复后自动切回；响应头增加 `X-IPTV-Source-Id` / `X-IPTV-Source-State`。
- 录制在认领任务时固定源，整段录像和重试都不会中途换源。
- 自定义源、备用 M3U 导入（先预览）、按 `tvg-id`/名称/归一化名称匹配频道。
- 管理台直播源页面按频道分组，支持锁定徽标、回退状态、临时切换、启停和导入预览。
- 新增 `scripts/migrations/migrate_sources.py` 和 `scripts/channels/restore_bridge_sources.py`。
- 修复频道线路接口的 ObjectId/datetime 序列化错误。

## 0.1.0-rc.3 - 2026-11-04

- 移除录制 ffmpeg 的无上限自动重连；上游彻底断流时不再挂起到录制窗口末尾，而是由读超时退出并交给时长校验/重试处理。
- PyAV 录制回退同步改为仅使用读超时。
- 保留实际时长校验和失败重试，避免把早退片段保存为完整录像。
- 其它 rc.2 内容保持不变，建议使用 rc.3 替代 rc.2。

## 0.1.0-rc.2 - 2026-11-04

- 录制增加实际媒体时长校验：实际时长显著短于请求窗口时判失败并重试，避免把早退片段当完整录像。
- 录制 ffmpeg 增加 HTTP 自动重连参数。
- 修复 Debian 12 上静态 ffmpeg 解析 MPEG-TS SDT 时的 gconv/iconv SIGSEGV（通过 `GCONV_PATH` 禁用不兼容模块）。
- 新增 `-ai` 镜像变体，ASR/recorder 使用独立 AI 镜像并固定 `av==18.1.0`，避免基础镜像更新移除 faster-whisper/PyAV。
- Web 镜像启用 imageio-ffmpeg，支持浏览器内播放录像；新增可配置的本地/registry 镜像 tag 与 pull policy。
- 增加录制早退检测单元测试，并更新部署文档。

## 0.1.0-rc.1 - 2026-08-25

- 项目更名为 IPTVPilot，Python 包名改为 `iptv_pilot`。
- 增加 Apache-2.0 许可证、版权声明和公开协作文档。
- 删除仓库内置的直播频道、订阅和第三方 EPG 地址。
- 从当前版本和 Git 历史中删除私人部署报告、主机地址及个人默认值。
- 增加自动化测试、CI、Dependabot 和仓库维护配置。
- 增加 GHCR 镜像发布、SBOM、构建来源证明和 GitHub Release 自动化。
- 固定 Python、Alpine、MongoDB Search 和 iptv-api 镜像版本，避免 `latest` 漂移。
- 记录 v0.1 之后的 ARM64 和通用部署路线图。
- 统一使用简体中文管理台、API 提示和项目文档。
- 增加首次直播源配置指南和容器化批量导入助手，并改进空数据管理台提示。

# REST API

Base URL：`http://YOUR_NAS_IP:${WEB_PORT}`。OpenAPI：`GET /docs`，健康检查：`GET /health`。若配置了 `WEB_API_TOKEN`，所有 `/api/*` 请求需携带 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/channels?category=` | 频道列表 |
| GET | `/api/channels/admin` | 管理视图中的完整频道列表 |
| POST/PATCH/DELETE | `/api/channels...` | 创建、修改、批量启停或删除自定义频道 |
| GET/POST/PATCH/DELETE | `/api/sources...` | 管理用户提供的直播线路 |
| POST | `/api/sources/test` | 测试用户提供的线路 |
| GET | `/api/channels/{channel_id}/live` | 获取同源代理后的频道 HLS 直播清单 |
| GET | `/api/live/proxy/{signed_token}` | 获取直播子清单、密钥或媒体分片；仅使用服务端签名 URL |
| GET | `/api/epg?channel=CCTV-1&date=2026-08-12` | 某频道某日节目单 |
| GET | `/api/epg/search?q=新闻` | EPG 标题/简介中文与英文子串搜索 |
| GET | `/api/summaries/latest?limit=10` | 最新摘要 |
| GET | `/api/summaries?date=2026-08-12` | 某日摘要 |
| GET | `/api/summaries/{id}` | 摘要与转录详情 |
| GET | `/api/search?q=关键词&vector=false` | 摘要/转录搜索；`vector=true` 仅在安装并启用 embedding 后生效 |
| POST | `/api/subscriptions` | 新建订阅 |
| GET | `/api/subscriptions` | 订阅列表 |
| PATCH | `/api/subscriptions/{id}` | 修改订阅 |
| DELETE | `/api/subscriptions/{id}` | 软停用订阅 |
| GET | `/api/recordings?status=` | 录制列表 |
| POST | `/api/recordings/manual` | 创建 10–600 秒手工录制任务，供验证或临时采集 |
| GET | `/api/recordings/{id}` | 查看录制、转录、摘要与推送事件的完整状态 |
| GET | `/api/recordings/{id}/play` | 浏览器播放；TS 首次访问会转封装为 MP4，支持 Range/拖动 |
| GET | `/api/recordings/{id}/download` | 下载原始录像文件 |
| GET | `/api/push/events` | 推送事件审计 |
| GET | `/api/system/status` | 频道、EPG、订阅和录制状态统计 |
| GET/PATCH | `/api/settings/buffer` | 查询或更新直播缓冲档位 |
| POST | `/api/bookmarks` | 新建或更新收藏 |
| GET | `/api/bookmarks?user=default` | 收藏列表 |
| DELETE | `/api/bookmarks/{id}` | 取消收藏 |

## 新建关键词订阅

```json
{
  "name": "新闻联播",
  "match_type": "keyword",
  "keyword": "新闻联播",
  "asr_enabled": true,
  "asr_model": "small",
  "llm_enabled": true,
  "push_enabled": true,
  "auto_bookmark": false,
  "enabled": true
}
```

## 新建频道时间订阅

```json
{
  "name": "每周五晚间节目",
  "match_type": "channel_time",
  "channel_id": "CCTV-1",
  "time_rule": {
    "weekdays": [4],
    "time": "20:00",
    "timezone": "Asia/Shanghai",
    "tolerance_min": 5
  },
  "asr_enabled": true,
  "llm_enabled": true,
  "push_enabled": true
}
```

## 通知通道

通知默认关闭。需要邮件通知时设置 `PUSH_PROVIDER=email` 并配置你自己的 SMTP 服务；可使用 `GET /api/push/events` 查询 `delivered/failed` 审计事件。Coze Provider 作为可选实验适配器保留，不属于默认部署要求。

## 录像播放说明

- 录制目录以只读方式挂载到 Web 容器，播放不会改写原始 `.ts` 文件。
- 首次播放按文件大小和修改时间生成独立 MP4 缓存；H.264 视频直接复制，MP2 等浏览器不兼容音频转为 AAC。
- 后续请求复用缓存。`MEDIA_CACHE_MAX_BYTES` 默认 20 GiB，按最近使用时间清理旧缓存。
- 不存在返回 404，录制未完成返回 409，无法转封装返回 422；页面始终保留原文件下载入口。
- 若将来启用 `WEB_API_TOKEN`，普通 API 可在管理台“系统”页保存会话令牌；原生视频与下载链接无法附加 Bearer Header，需另行增加短期签名播放 URL 后再启用受保护播放。

## 直播播放说明

- 管理台不把第三方直播地址直接交给浏览器；Web 服务读取频道当前优选 URL，重写 HLS 清单并通过带时效签名的同源 URL 转发子清单和媒体分片。
- Chrome/Edge 使用随应用本地部署的 hls.js，Safari 可使用其原生 HLS 能力；运行时不访问外部 CDN。
- 节目单、频道明确的订阅和录制记录均可打开直播播放器；全频道关键词订阅因为没有唯一频道，不显示直播按钮。
- 上游不可用返回 502，签名无效或过期返回 403；响应不暴露 MongoDB 或 NAS 文件路径。

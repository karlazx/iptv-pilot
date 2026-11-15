# 列表分页、筛选与刷新

录像、摘要、订阅、频道管理、直播源、收藏和系统页的推送事件支持服务端分页。
每页 50 项，可前后翻页，筛选保留在当前浏览器页面会话中。
检索针对整个列表，不只过滤当前页；支持标题/名称/频道等字段的字面匹配。
日期范围按浏览器当地日期解释，结束日期包含当天。频道管理额外的“本页分类”仅过滤当前页。
时间顺序支持最新在前和最早在前。列表不反复统计全表总数，显示本页数量与页码。

录像页定期刷新当前页活跃任务的状态，不重新下载全部历史记录；新出现的任务可用页面顶部刷新按钮读取。
收藏内容按页关联查询，不再为全部收藏逐项请求详情。
直播源页按线路分页，因此同频道多条线路可能跨页；检索频道可集中查看，频道详情仍可显示完整线路。

## API

`GET /api/library/{kind}` 返回：

```json
{"items": [], "has_more": false, "next_cursor": null}
```

`kind` 为 `recordings`、`summaries`、`subscriptions`、`channels`、`sources`、`bookmarks` 或 `push-events`。
参数：`limit`（1–100）、`cursor`、`q`、`channel`、`status`、`from`、`to`、`order`（asc/desc）。
`from`、`to` 须是带时区的 ISO 时间，范围为起点包含、终点不包含。
启用类列表状态为 enabled/disabled；录像和投递列表用其自身状态。

游标使用时间与 ID 稳定排序，并绑定筛选和排序条件。条件变化必须清空游标；无效游标返回 400。
不需要总数，不使用深度 skip。原列表 API 的数组响应继续兼容已有客户端。

`POST /api/recordings/status` 接受 `{"ids": ["...", "..."]}`，最多 100 个 ID，返回这些任务的精简状态。
`GET /api/bookmarks/lookup?item_type=recording&item_id=...` 查询单个默认用户收藏，避免读取整个收藏夹。

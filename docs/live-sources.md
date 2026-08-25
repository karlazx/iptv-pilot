# 首次添加直播源

IPTVPilot 的公开仓库和镜像不会附带任何可播放频道、IPTV 订阅或第三方 EPG 地址。因此，全新部署后频道数为 0、节目单为空是预期状态，并不表示部署失败。

请只使用自有或已获授权的直播源。不要把私人 URL、访问令牌或包含这些信息的 `.env`、日志和截图提交到 GitHub。

## 选择添加方式

- 只有少量频道，或希望先确认播放链路：使用管理台手动添加，通常一分钟内即可完成，推荐首次部署先走这条路径。
- 已有自建频道清单或服务商授权的 M3U/TXT 订阅：使用批量导入助手。
- 同一个频道有多条线路：先创建频道，再在直播源管理中添加并绑定备用线路。

## 方式一：在管理台添加首个频道

1. 打开 `http://你的_NAS_IP:5190`。
2. 进入“系统设置 → 频道管理”，点击“添加频道”。
3. 填写频道 ID、名称、直播源 URL 和分类。频道 ID 应保持稳定，例如 `MY-TV-1`。
4. 这里的直播源 URL 应是可直接播放的媒体地址，例如 HLS 的 `.m3u8` 地址，而不是包含多个频道的聚合订阅地址。
5. 保存后进入“直播源管理”，点击该线路旁的“测试”。测试会检查播放列表，并在适用时继续探测首个媒体分片。
6. 回到“节目单”选择频道即可尝试播放。没有配置 EPG 时仍可播放直播，只是不会显示节目表。

为同一频道增加备用线路时，进入“直播源管理 → 新增自定义源”，填写 URL，并在“绑定频道”中选择刚创建的频道。未绑定的自定义源不会自动成为某个频道的播放候选。

## 方式二：批量导入自有频道或授权订阅

批量导入依赖 `iptv-api` 的持久化配置。必须先完成一次启动，让容器初始化 `config.ini`、`demo.txt` 等默认文件：

```bash
docker compose up -d
docker compose exec iptv-api test -f /iptv-api/config/config.ini
```

第二条命令正常退出后再继续。不要直接修改仓库里的示例文件；将它们复制到不会提交到 Git 的私有目录，然后填入真实内容。

### 自建频道清单

复制 `config/iptv-api/channels.example.txt`，每行填写一个频道，使用英文逗号分隔：

```text
我的频道,https://stream.example.com/live/index.m3u8
另一个频道,https://stream.example.com/live/another.m3u8
```

频道名称同时用于 `iptv-api` 的模板匹配，请避免不必要地频繁改名。

### M3U/TXT 订阅

复制 `config/iptv-api/subscriptions.example.txt`，每行填写一个由你控制或已获授权的订阅 URL：

```text
https://provider.example.com/my-authorized-subscription.m3u
```

准备好任一文件后，在仓库根目录执行；两个参数可以只提供一个，也可以同时提供：

```bash
python3 scripts/import_live_sources.py \
  --channels-file /绝对路径/my-channels.txt \
  --subscriptions-file /绝对路径/my-subscriptions.txt
```

导入助手会读取 `.env` 中的 `IPTV_API_CONFIG_PATH`，通过临时容器写入持久化配置，将它管理的文件归还给当前 NAS 用户，并重启 `iptv-api` 与频道同步服务。这样即使配置目录由容器以 `root` 创建，也不需要对整个 NAS 存储目录递归改权限。真实 URL 只会写入你配置的持久化目录，不会写入 Git 仓库。

如果 `.env` 不在仓库根目录，可显式指定：

```bash
python3 scripts/import_live_sources.py --env-file /绝对路径/iptv-pilot.env \
  --subscriptions-file /绝对路径/my-subscriptions.txt
```

`iptv-api` 需要拉取、解析和测速，频道同步默认每 5 分钟检查一次结果。可观察日志：

```bash
docker compose logs -f --tail=100 iptv-api channel-sync
```

看到 `channel sync active=` 后，可在管理台刷新“频道管理”。如果 `result.m3u` 已生成但不想等待下一轮检查，可以执行：

```bash
docker compose restart channel-sync
```

## 配置 EPG

直播源和 EPG 是两套独立数据。没有 EPG 时直播仍可播放，但节目单、按节目订阅和定时录制无法正常工作。

默认配置读取 `iptv-api` 生成的本地 XMLTV 文件：

```dotenv
EPG_URLS=/data/m3u/epg/epg.xml
```

也可以把自有 XMLTV 文件放到 `M3U_DATA_PATH` 对应目录，或在 `EPG_URLS` 中添加已获授权的 XMLTV URL；多个地址用英文逗号分隔。修改 `.env` 后重启 EPG 服务：

```bash
docker compose restart epg-engine
docker compose logs -f --tail=100 epg-engine
```

频道 ID 或名称需要能与 XMLTV 中的频道标识对应，否则会出现直播可播放但节目单为空的情况。

## 验证清单

首次添加后，建议依次确认：

1. “频道管理”显示预期频道且状态为启用。
2. “直播源管理”的线路测试成功；HLS 源应能探测到首个媒体分片。
3. “节目单”可以打开直播。
4. 配置 EPG 后能看到节目，并检查频道映射是否正确。
5. 为 MongoDB 和 `IPTV_API_CONFIG_PATH` 建立备份。管理台手动添加的频道、备用线路保存在 MongoDB；批量导入的原始配置保存在 `IPTV_API_CONFIG_PATH`。

## 常见问题

### 导入后仍然是 0 个频道

先查看 `iptv-api` 日志。常见原因包括订阅文件为空、NAS 无法访问订阅地址、订阅格式不受支持，或上游返回登录页而不是 M3U/TXT。确认 `/iptv-api/output/result.m3u` 已生成后，重启 `channel-sync` 再刷新管理台。

### 添加频道成功但线路测试失败

从 NAS 所在网络确认该 URL 可以访问。部分服务要求特定请求头、来源网络或短时令牌；浏览器能打开网页不代表 NAS 能直接拉取媒体分片。不要在公开 Issue 中粘贴真实 URL 或令牌。

### 有频道但节目单为空

这通常是 EPG 文件尚未生成、EPG 服务无法读取，或 XMLTV 频道标识与直播频道不匹配。先检查 `epg-engine` 日志，再核对频道 ID 和名称。

### 重建容器后配置会丢失吗

只要 `.env` 中的 `IPTV_API_CONFIG_PATH`、`M3U_DATA_PATH` 和 MongoDB 数据路径指向持久化目录，普通的容器重建不会删除配置。更换目录、主机或 NAS 前仍应单独备份这些数据。

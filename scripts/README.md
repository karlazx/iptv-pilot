# 维护脚本

按用途分组的运维脚本。绝大多数脚本依赖容器内的环境变量（`MONGODB_URI` 等），推荐用 `docker compose run` 执行，例如：

```bash
docker compose run --rm --no-deps recorder python -m scripts.migrations.migrate_rc6 --dry-run
```

| 目录 | 用途 |
|---|---|
| `db/` | MongoDB/搜索初始化、备份、过期录像清理（Compose 直接调用） |
| `migrations/` | 数据与索引迁移，均支持 `--dry-run` |
| `channels/` | 频道与线路同步、质量刷新、M3U 导入、桥接源恢复 |
| `setup/` | 生成或修改 `.env` 的配置助手，不打印密钥 |
| `diagnostics/` | 运行时依赖检查、直播质量诊断、流水线监控 |
| `smoke/` | 端到端冒烟测试（需要运行中的服务和真实源） |

约定：

- 迁移脚本先 `--dry-run` 再 `--apply`，重复执行应保持幂等。
- 配置助手只写入缺失或指定字段，不覆盖已有密钥。
- 冒烟和诊断脚本会产生临时数据，运行后需自行清理。

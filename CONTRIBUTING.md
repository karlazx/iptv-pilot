# 参与贡献

感谢你帮助改进 IPTVPilot。

## 提交 Issue 前

- 搜索已有 Issue 和文档，确认问题尚未记录。
- 删除凭据、私人 URL、录像、转写、邮箱和主机信息。
- 提供最小复现步骤及相关软件版本。

## 开发环境

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt pytest ruff
python -m pytest
ruff check .
```

涉及 Docker 的改动还应通过：

```bash
cp .env.example .env
docker compose config --quiet
docker compose build web
```

## Pull Request 要求

- 保持改动聚焦，并说明对用户可见的行为变化。
- 行为发生变化时增加或更新测试。
- 用户需要采取操作时更新文档和 `CHANGELOG.md`。
- 不得加入可工作的第三方频道、订阅或 EPG URL。
- 不得提交生成的媒体、模型、数据库、日志、备份或 `.env`。
- 使用清晰的提交说明，每个提交都应便于审查。

提交贡献即表示你有权提供相关内容，并同意其按 Apache License 2.0 分发。

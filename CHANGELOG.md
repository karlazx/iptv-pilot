# 变更日志

IPTVPilot 的重要变化将记录在此文件中。首次发布标签后，项目将遵循语义化版本规则。

## 尚未发布

- 项目更名为 IPTVPilot，Python 包名改为 `iptv_pilot`。
- 增加 Apache-2.0 许可证、版权声明和公开协作文档。
- 删除仓库内置的直播频道、订阅和第三方 EPG 地址。
- 从当前版本和 Git 历史中删除私人部署报告、主机地址及个人默认值。
- 增加自动化测试、CI、Dependabot 和仓库维护配置。
- 增加 GHCR 镜像发布、SBOM、构建来源证明和 GitHub Release 自动化。
- 固定 Python、Alpine、MongoDB Search 和 iptv-api 镜像版本，避免 `latest` 漂移。
- 记录 v0.1 之后的 ARM64 和通用部署路线图。
- 统一使用简体中文管理台、API 提示和项目文档。

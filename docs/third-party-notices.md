# 第三方软件声明

IPTVPilot 通过 Python 包、容器镜像、可选 AI 模型和内置浏览器库使用第三方软件。每个组件仍受其各自许可证约束。

## 内置浏览器库

- hls.js 1.6.16：Apache License 2.0。随项目分发的许可证和归属信息位于 `iptv_pilot/web/static/hls.LICENSE.txt`。

## 运行时服务

- MongoDB Community Server 和 MongoDB Community Search
- guovern/iptv-api
- Python 容器镜像
- Alpine Linux 容器镜像

重新分发镜像前，应检查对应发布版本所使用镜像的许可证和分发条款。

## 可选模型与外部服务

模型权重、托管 LLM API、SMTP 服务、IPTV 源和 EPG 源不受 IPTVPilot 项目许可证覆盖。运营者必须自行审查并遵守所配置服务或模型的条款。

发布产物应包含自动生成的软件物料清单以及发布流水线生成的许可证报告。

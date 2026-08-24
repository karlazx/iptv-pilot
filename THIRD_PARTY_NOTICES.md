# Third-party notices

IPTVPilot uses third-party software through Python packages, container images, optional AI models, and a vendored browser library. Each component remains subject to its own license.

## Vendored browser library

- hls.js 1.6.16 — Apache License 2.0. The distributed license and attribution text is stored at `iptv_pilot/web/static/hls.LICENSE.txt`.

## Runtime services

- MongoDB Community Server and MongoDB Community Search
- guovern/iptv-api
- Python container images
- Alpine Linux container images

Review the license and distribution terms for the exact image versions used in a release before redistributing images.

## Optional models and services

Model weights, hosted LLM APIs, SMTP providers, IPTV sources, and EPG sources are not covered by the IPTVPilot license. Operators must review and comply with the terms for each service or model they configure.

Release artifacts should include a generated software bill of materials and the license reports produced by the release pipeline.

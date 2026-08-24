# Roadmap

This roadmap communicates direction rather than a delivery commitment. Priorities may change based on maintainer capacity and user feedback.

## v0.1

- Publish the first Apache-2.0 open-source release for `linux/amd64`.
- Provide local Docker Compose builds and GHCR release images.
- Support channel management, XMLTV ingestion, subscriptions, recording, playback, optional transcription, program digests, and email notifications.
- Keep all working IPTV and third-party EPG sources outside the repository.

## After v0.1

- Add and validate `linux/arm64` images.
- Separate MongoDB Search, AI, and hardware-transcoding requirements into more portable deployment profiles.
- Add a CPU-only live normalization option where practical.
- Expand integration tests for full Docker Compose startup, backup restoration, and media pipelines.
- Improve dependency and container image update automation.

## Under consideration

- Multi-user authentication and role-based access control.
- Stronger validation and network isolation for user-configured upstream URLs.
- Additional notification providers.
- Broader user-interface localization.

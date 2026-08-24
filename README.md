# IPTVPilot

> Smart IPTV subscriptions, channel management, and program digests.

IPTVPilot is a self-hosted IPTV assistant for trusted home networks. It combines channel and EPG management, scheduled recording, browser playback, speech-to-text, LLM-generated program digests, and email notifications in one Docker Compose stack.

> [!IMPORTANT]
> IPTVPilot does not include channel streams, IPTV subscriptions, or third-party EPG feeds. Configure only sources that you own or are authorized to use, and follow the laws and service terms that apply to you.

## Features

- Manage channels and multiple stream candidates from one web console.
- Import XMLTV EPG data and create keyword or channel-time subscriptions.
- Record programs with FFmpeg and a PyAV fallback.
- Play live HLS streams and browser-compatible recording copies.
- Transcribe recordings with faster-whisper.
- Generate structured digests with OpenAI-compatible LLM providers.
- Send digest notifications through email.
- Back up MongoDB and clean old recordings with maintenance profiles.

## Requirements

- Docker Engine and Docker Compose 2.21 or later.
- An x86_64 Linux host or NAS. The optional live normalizer currently expects Intel VAAPI at `/dev/dri/renderD128`.
- Sufficient storage for MongoDB, media cache, recordings, models, and backups.
- User-provided and authorized M3U/HLS and XMLTV sources.

## Quick start

```bash
git clone https://github.com/karlazx/iptv-pilot.git
cd iptv-pilot
cp .env.example .env
```

Edit `.env` and, at minimum:

1. Replace `IPTV_DB_PASSWORD`, `MONGOT_PASSWORD`, and the matching password inside `MONGODB_URI`.
2. Change every `/nas/iptv/...` storage path to a directory on your host.
3. Configure your own EPG and IPTV source files.
4. Configure an LLM provider, or use `LLM_PROVIDER=mock` for a local functional check.

Then start the stack:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Open `http://YOUR_NAS_IP:5190`. The companion iptv-api service is exposed on port `5181` by default.

The default configuration is intended for a trusted private network. Do not expose the management console or iptv-api directly to the public internet. See [SECURITY.md](SECURITY.md) for the deployment boundary.

## Documentation

- [Deployment guide](docs/deploy.md)
- [REST API](docs/api.md)
- [Architecture](docs/architecture.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt pytest ruff
python -m pytest
ruff check .
```

The Python package is named `iptv_pilot`. Docker images use the `iptv-pilot` repository name; Compose builds the default, web, and live-normalizer variants with separate tags.

Tagged releases publish signed build provenance and SBOM-enabled images to `ghcr.io/karlazx/iptv-pilot`. To use a published release instead of local image names, set:

```dotenv
IPTV_PILOT_IMAGE=ghcr.io/karlazx/iptv-pilot
APP_TAG=0.1.0
```

The Compose file retains build definitions so operators can always build the same variants locally.

## Project status

IPTVPilot is an early-stage self-hosted project. Interfaces and deployment defaults may change before the first stable release. Back up your database and recordings before upgrading.

## License

Copyright 2026 karlazx. Licensed under the [Apache License 2.0](LICENSE).

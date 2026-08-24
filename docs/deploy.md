# Deployment guide

IPTVPilot is designed for a single trusted home server or NAS running Docker Compose. It has been exercised on x86_64 hardware; other architectures may require different media and AI dependencies.

## 1. Prepare storage

Create dedicated host directories for:

- MongoDB data
- MongoDB Search data
- recordings
- browser media cache
- downloaded AI models
- M3U and EPG data
- iptv-api configuration
- backups

Copy the configuration template:

```bash
cp .env.example .env
chmod 600 .env
```

Update every storage path in `.env`. Do not recursively change ownership on a shared storage root. The MongoDB container normally writes as UID/GID 998, so grant that identity access only to the selected MongoDB data directory.

## 2. Configure credentials

Replace both database placeholder passwords and keep the password in `MONGODB_URI` synchronized with `IPTV_DB_PASSWORD`:

```dotenv
IPTV_DB_PASSWORD=use-a-long-random-password
MONGOT_PASSWORD=use-a-different-long-random-password
MONGODB_URI=mongodb://iptv_user:use-a-long-random-password@mongodb:27017/iptv_pilot?authSource=iptv_pilot&replicaSet=rs0
```

Keep `.env` outside backups, screenshots, issue reports, and shell transcripts. Never commit it.

## 3. Configure IPTV and EPG sources

The repository contains no working channel, subscription, or third-party EPG URLs. Start with the templates in `config/iptv-api/` and add only sources you are authorized to use.

After initializing the upstream iptv-api configuration directory, install user-provided files with:

```bash
python3 scripts/configure_iptv_api.py "$IPTV_API_CONFIG_PATH" .env \
  --channels-file /path/to/my-channels.txt \
  --subscriptions-file /path/to/my-subscriptions.txt
```

Both file arguments are optional. The script always updates `IPTV_API_CONFIG_PATH`; when files are supplied it copies them into the persistent iptv-api configuration.

`EPG_URLS` accepts a comma-separated fallback list. The default points only to the XMLTV file generated in the shared M3U directory:

```dotenv
EPG_URLS=/data/m3u/epg/epg.xml
```

## 4. Configure optional AI features

For a minimal functional deployment:

```dotenv
INSTALL_AI_DEPS=0
INSTALL_EMBEDDING_DEPS=0
ASR_BACKEND=mock
EMBEDDING_BACKEND=none
LLM_PROVIDER=mock
```

For production transcription, use `INSTALL_AI_DEPS=1` and configure faster-whisper. Model downloads can be large and the first transcription will be slower.

For LLM digests, configure one supported provider and its API key. OpenAI-compatible custom endpoints use `LLM_PROVIDER=custom`, `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY`.

Embedding search is optional. Enabling BGE-M3 requires `INSTALL_EMBEDDING_DEPS=1`, additional image build time, several gigabytes of model storage, and more memory.

## 5. Validate and start

By default, Compose builds local images. For a published release, set `IPTV_PILOT_IMAGE=ghcr.io/karlazx/iptv-pilot` and set `APP_TAG` to the release version without the leading `v`.

```bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

Startup order is MongoDB, replica-set initialization, MongoDB Search, search-index initialization, and then the application workers.

Inspect startup logs with:

```bash
docker compose logs -f mongodb mongo-init mongot search-init web
```

Validate the web service and database:

```bash
curl http://127.0.0.1:${WEB_PORT}/health
docker compose exec mongodb mongosh --quiet --eval 'rs.status().ok'
docker compose exec mongodb mongosh --quiet --eval 'db.getSiblingDB("iptv_pilot").channels.countDocuments()'
```

## 6. Live normalizer

The optional `live-normalizer` service uses Intel VAAPI and maps `/dev/dri/renderD128`. It is excluded from the default startup. Other channels continue through the standard same-origin proxy.

When VAAPI is available, list only channels that need timestamp and segment normalization:

```dotenv
LIVE_NORMALIZE_CHANNELS=My Channel
LIVE_NORMALIZE_MAX_CHANNELS=1
```

Start the hardware-accelerated profile with:

```bash
docker compose --profile hardware-accel up -d --build
```

## 7. Private-network boundary

The default stack is intended for a trusted LAN and does not require `WEB_API_TOKEN`. Do not forward ports 5190 or 5181 from an internet-facing router.

If remote access is required, place the services behind a VPN or an HTTPS reverse proxy and configure `WEB_API_TOKEN`. Review the playback limitations described in [SECURITY.md](../SECURITY.md) before enabling the token.

MongoDB and MongoDB Search ports must never be published to the host or internet.

## 8. Backups and retention

Run a database backup:

```bash
docker compose --profile maintenance run --rm backup
```

Clean expired, non-bookmarked recordings:

```bash
docker compose --profile maintenance run --rm cleanup-recordings
```

Schedule both operations only after performing a manual backup and restore test. Database backup does not include recording files; protect recordings with an independent storage backup policy.

## 9. Upgrade procedure

Before upgrading:

1. Back up MongoDB and important recordings.
2. Read `CHANGELOG.md` for migration notes.
3. Validate the new Compose configuration.
4. Rebuild images and restart the stack.
5. Check health, worker logs, channel sync, and a short recording.

```bash
git pull --ff-only
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

## Known limitations

- MongoDB runs as a single-node replica set and is not highly available.
- The live normalizer currently targets Intel VAAPI on x86_64 Linux.
- Authentication is a single optional bearer token, not a multi-user identity system.
- Signed playback URLs are not yet implemented for every recording and normalized-live path.
- AI features can require significant memory, disk space, and build time.

# Architecture

IPTVPilot is a Docker Compose application with one web process, several polling workers, MongoDB, MongoDB Search, and an external iptv-api container.

```text
Authorized M3U / XMLTV sources
             │
             ▼
          iptv-api ──► channel-sync ───────┐
             │                             │
             └──────► epg-engine ──────────┤
                                           ▼
Web browser ◄──── FastAPI web ◄──────── MongoDB/Search
                    │                      ▲
                    ├── live proxy         │
                    ├── media cache        │
                    └── management API     │
                                           │
scheduler ──► recorder ──► ASR ──► LLM ──► push
                    │
                    └── recordings
```

## Components

- `iptv_pilot.web`: FastAPI API, static management console, live proxy, recording playback, and search.
- `iptv_pilot.epg_engine`: XMLTV ingestion and channel metadata synchronization.
- `iptv_pilot.scheduler`: converts matching subscriptions and EPG programs into recording jobs.
- `iptv_pilot.recorder`: captures streams with FFmpeg and falls back to an isolated PyAV process.
- `iptv_pilot.asr`: transcribes completed recordings and optionally creates embeddings.
- `iptv_pilot.llm`: turns transcripts into validated structured digests.
- `iptv_pilot.push`: delivers notifications and records delivery events.
- `iptv_pilot.live_normalizer`: optional Intel VAAPI HLS normalization for problematic streams.
- `scripts/`: configuration, diagnostics, backup, cleanup, and smoke-test utilities.

## Data flow

MongoDB is the control plane for channels, EPG programs, subscriptions, jobs, recordings, transcripts, digests, bookmarks, settings, and push events. Media files and models stay on mounted host storage rather than in the database.

Workers claim jobs through atomic MongoDB updates. Each stage records status and retry metadata so a later worker iteration can recover stale work.

## Storage boundary

The web container mounts recordings read-only. Browser-compatible MP4 files and normalized HLS segments are written to a separate cache directory. Database backups and recording backups are independent responsibilities.

## Network boundary

Application containers share the private Compose network. Only the web console and iptv-api publish host ports. The design assumes those ports are reachable only from a trusted LAN or VPN.

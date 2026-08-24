# Security policy

## Supported versions

Until IPTVPilot reaches a stable release, security fixes are provided only for the latest commit on the `main` branch.

## Reporting a vulnerability

Please use GitHub private vulnerability reporting for this repository. Do not open a public issue containing credentials, private stream URLs, personal data, or working exploit details.

Include the affected commit, deployment topology, reproduction steps, expected impact, and any suggested mitigation. You should receive an acknowledgement within seven days.

## Deployment boundary

IPTVPilot is designed for a trusted private network. Its default deployment is not hardened for direct internet exposure and uses a single optional bearer token rather than user accounts or role-based access control.

- Do not expose ports 5190, 5181, MongoDB, or MongoDB Search directly to the internet.
- Prefer a VPN for remote access. Otherwise use an HTTPS reverse proxy and a strong `WEB_API_TOKEN`.
- Be aware that native recording playback and some media paths do not yet support bearer headers or universal signed URLs.
- Treat every configured stream, subscription, EPG, LLM, SMTP, and proxy endpoint as trusted configuration.
- Keep `.env` permissions restricted and redact it from logs and issue reports.
- Back up the database and recordings before upgrades.

The project intentionally does not bundle working IPTV or EPG sources. Users are responsible for the authorization and security of all configured sources.

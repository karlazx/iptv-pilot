# Contributing to IPTVPilot

Thank you for helping improve IPTVPilot.

## Before opening an issue

- Search existing issues and the documentation.
- Remove credentials, private URLs, recordings, transcripts, email addresses, and host details.
- Use a minimal reproduction and include relevant software versions.

## Development setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt pytest ruff
python -m pytest
ruff check .
```

Docker-related changes should also pass:

```bash
cp .env.example .env
docker compose config --quiet
docker compose build web
```

## Pull requests

- Keep changes focused and explain the user-visible behavior.
- Add or update tests for behavior changes.
- Update documentation and `CHANGELOG.md` when users need to act.
- Do not add working third-party channel, subscription, or EPG URLs.
- Do not commit generated media, models, databases, logs, backups, or `.env` files.
- Use clear commit messages. Squashing is not required, but each commit should be reviewable.

By contributing, you certify that you have the right to submit the work and that it may be distributed under the Apache License 2.0.

"""Push configuration stored in MongoDB, with environment variables as defaults.

The management UI writes ``settings.push_config``; the push worker reads it on
every iteration so changes apply without recreating containers.  Environment
variables remain authoritative until the first save, which keeps existing
deployments working unchanged.
"""

from __future__ import annotations

import os
from typing import Any

PUSH_CONFIG_KEY = "push_config"
SUPPORTED_PROVIDERS = {"none", "email"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_config() -> dict[str, Any]:
    raw = os.getenv("PUSH_PROVIDER", "none")
    channels = [value.strip().lower() for value in raw.split(",") if value.strip()] or ["none"]
    email = {
        "host": os.getenv("SMTP_HOST", ""),
        "port": _env_int("SMTP_PORT", 465),
        "username": os.getenv("SMTP_USERNAME", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "sender": os.getenv("SMTP_FROM", "") or os.getenv("SMTP_USERNAME", ""),
        "recipient": os.getenv("SMTP_TO", ""),
        "use_ssl": _env_bool("SMTP_USE_SSL", True),
    }
    return {"provider": channels[0], "channels": channels, "email": email}


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def load_config(database) -> dict[str, Any]:
    """Return the effective push configuration (database first, env fallback)."""
    base = env_config()
    if database is None:
        return base
    row = database.settings.find_one({"key": PUSH_CONFIG_KEY})
    stored = (row or {}).get("value") or {}
    if not stored:
        return base
    provider = _clean(stored.get("provider")) or base["provider"]
    channels = [provider]
    if provider == "none":
        channels = ["none"]
    email = dict(base["email"])
    incoming = stored.get("email") or {}
    for key in ("host", "username", "sender", "recipient"):
        if incoming.get(key) is not None:
            email[key] = _clean(incoming.get(key))
    if "port" in incoming:
        try:
            email["port"] = int(incoming["port"])
        except (TypeError, ValueError):
            pass
    if "use_ssl" in incoming:
        email["use_ssl"] = bool(incoming["use_ssl"])
    if incoming.get("password"):
        email["password"] = str(incoming["password"])
    return {"provider": provider, "channels": channels, "email": email}


def public_config(database) -> dict[str, Any]:
    """Configuration safe to return to the browser; the password is never echoed."""
    config = load_config(database)
    email = dict(config["email"])
    return {
        "provider": config["provider"],
        "supported_providers": sorted(SUPPORTED_PROVIDERS),
        "email": {
            "host": email.get("host", ""),
            "port": email.get("port", 465),
            "username": email.get("username", ""),
            "sender": email.get("sender", ""),
            "recipient": email.get("recipient", ""),
            "use_ssl": bool(email.get("use_ssl", True)),
            "has_password": bool(email.get("password")),
        },
    }


def save_config(database, provider: str, email: dict[str, Any]) -> dict[str, Any]:
    """Persist a new push configuration and return the masked public view.

    Fields the UI did not submit keep their stored value; an empty password
    keeps the stored password instead of clearing it.
    """
    provider = (provider or "none").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported push provider: {provider}")
    current = database.settings.find_one({"key": PUSH_CONFIG_KEY}) or {}
    stored = (current.get("value") or {}).get("email") or {}
    merged = {
        "host": _clean(email.get("host", stored.get("host", ""))),
        "port": int(email.get("port", stored.get("port", 465)) or 465),
        "username": _clean(email.get("username", stored.get("username", ""))),
        "sender": _clean(email.get("sender", stored.get("sender", ""))),
        "recipient": _clean(email.get("recipient", stored.get("recipient", ""))),
        "use_ssl": bool(email.get("use_ssl", stored.get("use_ssl", True))),
    }
    password = email.get("password")
    if password:
        merged["password"] = str(password)
    elif stored.get("password"):
        merged["password"] = stored["password"]
    from datetime import UTC, datetime

    database.settings.update_one(
        {"key": PUSH_CONFIG_KEY},
        {"$set": {"value": {"provider": provider, "email": merged}, "updated_at": datetime.now(UTC)}},
        upsert=True,
    )
    return public_config(database)

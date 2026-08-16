"""Signed, same-origin HLS proxy used by the Web management console."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx


class LiveProxyError(RuntimeError):
    """The upstream live stream could not be proxied safely."""


@dataclass(frozen=True)
class LiveAsset:
    content: bytes
    final_url: str
    media_type: str
    is_playlist: bool


class LiveTokenSigner:
    def __init__(self, secret: bytes | None = None, lifetime_seconds: int = 900):
        self.secret = secret or secrets.token_bytes(32)
        self.lifetime_seconds = lifetime_seconds

    def sign(self, url: str) -> str:
        # Keep the token stable within a time bucket. A sliding ``now + ttl``
        # expiry changed every time a live playlist refreshed, so hls.js saw
        # the same upstream segment as a new URL and downloaded it repeatedly.
        # Two buckets keep tokens valid across the boundary while retaining a
        # finite lifetime.
        lifetime = max(1, self.lifetime_seconds)
        expires = ((int(time.time()) // lifetime) + 2) * lifetime
        payload = f"{expires}\n{url}".encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(self.secret, encoded.encode(), hashlib.sha256).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        return f"{encoded}.{encoded_signature}"

    def verify(self, token: str) -> str:
        try:
            encoded, encoded_signature = token.split(".", 1)
            expected = hmac.new(self.secret, encoded.encode(), hashlib.sha256).digest()
            supplied = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
            if not hmac.compare_digest(expected, supplied):
                raise ValueError("bad signature")
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            expires_text, url = payload.split("\n", 1)
            if int(expires_text) < int(time.time()):
                raise ValueError("expired")
            if urlparse(url).scheme not in {"http", "https"}:
                raise ValueError("unsupported scheme")
            return url
        except Exception as exc:
            raise LiveProxyError("invalid or expired live stream token") from exc


URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')


def rewrite_hls_manifest(text: str, base_url: str, signer: LiveTokenSigner) -> str:
    def proxy_url(value: str) -> str:
        absolute = urljoin(base_url, value)
        return f"/api/live/proxy/{signer.sign(absolute)}"

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            lines.append(raw_line)
        elif line.startswith("#"):
            lines.append(URI_ATTRIBUTE.sub(lambda match: f'URI="{proxy_url(match.group(1))}"', raw_line))
        else:
            lines.append(proxy_url(line))
    return "\n".join(lines) + "\n"


LIVE_HEADERS = {
    "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, video/*, */*",
    "User-Agent": "IPTV-Manager/1.0",
}
MAXIMUM_LIVE_ASSET_BYTES = 64 * 1024 * 1024


def live_media_type(response: httpx.Response) -> str:
    media_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
    final_path = urlparse(str(response.url)).path.lower()
    if "mpegurl" in media_type.lower() or final_path.endswith((".m3u8", ".m3u")):
        return "application/vnd.apple.mpegurl"
    if media_type == "application/octet-stream" and final_path.endswith(".ts"):
        return "video/mp2t"
    return media_type


def response_is_playlist(response: httpx.Response) -> bool:
    return live_media_type(response) == "application/vnd.apple.mpegurl"


async def open_live_response(client: httpx.AsyncClient, url: str) -> httpx.Response:
    try:
        request = client.build_request("GET", url, headers=LIVE_HEADERS)
        response = await client.send(request, stream=True)
        response.raise_for_status()
        content_length = int(response.headers.get("content-length", "0") or 0)
        if content_length > MAXIMUM_LIVE_ASSET_BYTES:
            await response.aclose()
            raise LiveProxyError("live stream segment exceeds proxy limit")
        return response
    except LiveProxyError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise LiveProxyError("live stream upstream is unavailable") from exc


async def read_live_response(response: httpx.Response) -> bytes:
    try:
        content = await response.aread()
    finally:
        await response.aclose()
    if len(content) > MAXIMUM_LIVE_ASSET_BYTES:
        raise LiveProxyError("live stream segment exceeds proxy limit")
    return content


async def fetch_live_asset(
    url: str,
    timeout_seconds: float = 20.0,
    client: httpx.AsyncClient | None = None,
) -> LiveAsset:
    owned_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds)
    try:
        response = await open_live_response(client, url)
        content = await read_live_response(response)
    finally:
        if owned_client:
            await client.aclose()
    media_type = live_media_type(response)
    final_url = str(response.url)
    has_playlist_header = content.lstrip().startswith(b"#EXTM3U")
    declared_playlist = (
        "mpegurl" in media_type.lower()
        or urlparse(final_url).path.lower().endswith((".m3u8", ".m3u"))
    )
    if declared_playlist and not has_playlist_header:
        raise LiveProxyError("live stream upstream returned an invalid HLS playlist")
    playlist = has_playlist_header
    if playlist:
        media_type = "application/vnd.apple.mpegurl"
    elif media_type == "application/octet-stream" and urlparse(final_url).path.lower().endswith(".ts"):
        media_type = "video/mp2t"
    return LiveAsset(content, final_url, media_type, playlist)

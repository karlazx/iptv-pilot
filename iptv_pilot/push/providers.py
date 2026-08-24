from __future__ import annotations

import os
import smtplib
from abc import ABC, abstractmethod
from datetime import datetime
from email.message import EmailMessage

import httpx


class PushProvider(ABC):
    name: str

    @abstractmethod
    def push_summary(self, summary: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def push_text(self, text: str) -> bool:
        raise NotImplementedError


class EmailProvider(PushProvider):
    name = "email"

    def _send(self, subject: str, body: str) -> bool:
        host = os.getenv("SMTP_HOST", "")
        username = os.getenv("SMTP_USERNAME", "")
        password = os.getenv("SMTP_PASSWORD", "")
        sender = os.getenv("SMTP_FROM") or username
        recipient = os.getenv("SMTP_TO", "")
        if not all([host, sender, recipient]):
            raise RuntimeError("SMTP_HOST, SMTP_FROM/SMTP_USERNAME and SMTP_TO are required")
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = recipient
        message.set_content(body)
        port = int(os.getenv("SMTP_PORT", "465"))
        if os.getenv("SMTP_USE_SSL", "true").lower() in {"1", "true", "yes"}:
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        return True

    def push_summary(self, summary: dict) -> bool:
        title = summary.get("program_title") or "新节目摘要"
        return self._send(f"IPTVPilot：{title}", format_summary(summary))

    def push_text(self, text: str) -> bool:
        return self._send("IPTVPilot通知", text)


def format_summary(summary: dict) -> str:
    points = "\n".join(f"- {item}" for item in summary.get("key_points", [])) or "- 暂无"
    started = summary.get("start_time")
    if isinstance(started, datetime):
        started = started.astimezone().strftime("%Y-%m-%d %H:%M")
    return (
        f"📺 IPTVPilot\n"
        f"节目：{summary.get('program_title') or '未知节目'}\n"
        f"频道：{summary.get('channel_id') or '未知频道'}\n"
        f"时间：{started or '未知'}\n\n"
        f"摘要：\n{summary.get('summary_text', '')}\n\n"
        f"要点：\n{points}\n\n"
        f"跟进：{summary.get('follow_up') or '无'}"
    )


class CozeProvider(PushProvider):
    """Append an assistant message to a Bot-associated Coze API conversation."""

    name = "coze"

    def __init__(self) -> None:
        self.base_url = os.getenv("COZE_API_BASE", "https://api.coze.cn").rstrip("/")
        self.bot_id = os.getenv("COZE_BOT_ID", "").strip()
        self.api_token = os.getenv("COZE_API_TOKEN", "").strip()
        self.user_id = os.getenv("COZE_USER_ID", "iptv_pilot").strip() or "iptv_pilot"
        self.timeout = float(os.getenv("COZE_TIMEOUT_SECONDS", "30"))
        self.last_conversation_id: str | None = None
        self.last_message_id: str | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                params=params,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            try:
                error_body = exc.response.json()
            except ValueError:
                error_body = {}
            code = error_body.get("code")
            message = str(error_body.get("msg") or "")[:300]
            raise RuntimeError(
                f"Coze HTTP {exc.response.status_code} code={code} msg={message}"
            ) from None
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Coze request failed: {type(exc).__name__}") from None
        if body.get("code") not in {None, 0}:
            raise RuntimeError(f"Coze API error code={body.get('code')} msg={body.get('msg', '')[:300]}")
        return body

    def push_text(self, text: str) -> bool:
        if not self.bot_id or not self.api_token:
            raise RuntimeError("COZE_BOT_ID and COZE_API_TOKEN are required")
        conversation = self._request(
            "POST",
            "/v1/conversation/create",
            payload={
                "bot_id": self.bot_id,
                "meta_data": {"source": "iptv_pilot", "user_id": self.user_id},
            },
        )
        conversation_id = str((conversation.get("data") or {}).get("id") or "")
        if not conversation_id:
            raise RuntimeError("Coze conversation response missing data.id")
        message = self._request(
            "POST",
            "/v1/conversation/message/create",
            params={"conversation_id": conversation_id},
            payload={
                "role": "assistant",
                "type": "answer",
                "content": text,
                "content_type": "text",
                "meta_data": {"source": "iptv_pilot", "user_id": self.user_id},
            },
        )
        message_id = str((message.get("data") or {}).get("id") or "")
        if not message_id:
            raise RuntimeError("Coze message response missing data.id")
        self.last_conversation_id = conversation_id
        self.last_message_id = message_id
        return True

    def push_summary(self, summary: dict) -> bool:
        return self.push_text(format_summary(summary))

    def verify_last_message(self) -> bool:
        if not self.last_conversation_id or not self.last_message_id:
            return False
        response = self._request(
            "POST",
            "/v1/conversation/message/list",
            params={"conversation_id": self.last_conversation_id},
            payload={"limit": 20, "order": "desc"},
        )
        rows = response.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("messages") or []
        return any(str(row.get("id")) == self.last_message_id for row in rows)

    def verify_conversation_list(self) -> bool:
        if not self.last_conversation_id:
            return False
        response = self._request(
            "GET",
            "/v1/conversations",
            params={
                "bot_id": self.bot_id,
                "page_num": 1,
                "page_size": 20,
            },
        )
        rows = (response.get("data") or {}).get("conversations") or []
        return any(str(row.get("id")) == self.last_conversation_id for row in rows)


def configured_channels() -> list[str]:
    raw = os.getenv("PUSH_PROVIDER", "none")
    channels = [value.strip().lower() for value in raw.split(",") if value.strip()]
    return list(dict.fromkeys(channels or ["none"]))


def create_providers() -> list[PushProvider]:
    providers: list[PushProvider] = []
    for channel in configured_channels():
        if channel in {"none", "coze_query"}:
            continue
        if channel == "coze":
            providers.append(CozeProvider())
            continue
        if channel == "email":
            providers.append(EmailProvider())
            continue
        raise RuntimeError(f"unsupported PUSH_PROVIDER channel={channel}")
    return providers

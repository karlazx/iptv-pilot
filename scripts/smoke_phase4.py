#!/usr/bin/env python3
"""Run real Coze and SMTP delivery checks without printing credentials or message content."""

from __future__ import annotations

import argparse

from services.common.db import connect_database
from services.push.providers import CozeProvider, EmailProvider


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["coze", "email", "both"], default="both")
    args = parser.parse_args()
    database = connect_database()
    summary = database.summaries.find_one(sort=[("created_at", -1)])
    if not summary:
        raise SystemExit("no summary available for Phase 4 smoke test")
    recording = database.recordings.find_one({"_id": summary.get("recording_id")}) or {}
    summary.setdefault("start_time", recording.get("start_time"))

    if args.provider in {"coze", "both"}:
        provider = CozeProvider()
        provider.push_summary(summary)
        message_readback = provider.verify_last_message()
        conversation_listed = provider.verify_conversation_list()
        if not message_readback or not conversation_listed:
            raise SystemExit(
                f"Coze verification failed: message_readback={message_readback} "
                f"conversation_listed={conversation_listed}"
            )
        print(
            "COZE_OK "
            f"conversation_suffix={provider.last_conversation_id[-6:]} "
            f"message_suffix={provider.last_message_id[-6:]} "
            "message_readback=true conversation_listed=true"
        )

    if args.provider in {"email", "both"}:
        EmailProvider().push_summary(summary)
        print("EMAIL_OK smtp_server_accepted=true")


if __name__ == "__main__":
    main()

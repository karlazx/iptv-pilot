#!/usr/bin/env python3
"""Probe documented conversation payload variants without printing credentials."""

from __future__ import annotations

import os

import httpx


def main() -> None:
    base = os.getenv("COZE_API_BASE", "https://api.coze.cn").rstrip("/")
    token = os.getenv("COZE_API_TOKEN", "")
    bot_id = os.getenv("COZE_BOT_ID", "")
    if not token or not bot_id:
        raise SystemExit("Coze configuration missing")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    variants = [
        ("empty", {}),
        ("bot_only", {"bot_id": bot_id}),
        ("bot_metadata", {"bot_id": bot_id, "meta_data": {"source": "iptv_pilot"}}),
    ]
    for name, payload in variants:
        try:
            response = httpx.post(
                f"{base}/v1/conversation/create",
                headers=headers,
                json=payload,
                timeout=30,
            )
            body = response.json()
            data = body.get("data") or {}
            print(
                f"{name} http={response.status_code} code={body.get('code')} "
                f"id_present={bool(data.get('id'))} msg={str(body.get('msg', ''))[:120]}"
            )
        except Exception as exc:
            print(f"{name} transport_error={type(exc).__name__}")

    try:
        response = httpx.post(
            f"{base}/v3/chat",
            headers=headers,
            json={
                "bot_id": bot_id,
                "user_id": os.getenv("COZE_USER_ID", "iptv_pilot"),
                "stream": False,
                "auto_save_history": True,
                "additional_messages": [
                    {
                        "role": "user",
                        "type": "question",
                        "content_type": "text",
                        "content": "IPTV 智能管家推送链路验收，请仅回复：收到。",
                    }
                ],
            },
            timeout=30,
        )
        body = response.json()
        data = body.get("data") or {}
        error = data.get("last_error") or {}
        print(
            f"chat_v3 http={response.status_code} code={body.get('code')} "
            f"chat_id_present={bool(data.get('id'))} "
            f"conversation_id_present={bool(data.get('conversation_id'))} "
            f"status={data.get('status')} last_error_code={error.get('code')} "
            f"msg={str(body.get('msg') or error.get('msg') or '')[:160]}"
        )
    except Exception as exc:
        print(f"chat_v3 transport_error={type(exc).__name__}")

    try:
        response = httpx.get(
            f"{base}/v1/workspaces",
            headers=headers,
            params={"page_num": 1, "page_size": 50},
            timeout=30,
        )
        body = response.json()
        data = body.get("data") or {}
        workspaces = data.get("workspaces") or data.get("items") or []
        print(
            f"workspaces http={response.status_code} code={body.get('code')} "
            f"count={len(workspaces)} msg={str(body.get('msg', ''))[:120]}"
        )
        bot_candidates = []
        for workspace in workspaces:
            workspace_id = workspace.get("id") or workspace.get("workspace_id")
            if not workspace_id:
                continue
            bot_response = httpx.get(
                f"{base}/v1/bots",
                headers=headers,
                params={"workspace_id": workspace_id, "page_num": 1, "page_size": 100},
                timeout=30,
            )
            bot_body = bot_response.json()
            bot_data = bot_body.get("data") or {}
            bots = bot_data.get("bots") or bot_data.get("items") or []
            for bot in bots:
                bot_candidates.append(
                    {
                        "id": str(bot.get("id") or bot.get("bot_id") or ""),
                        "name": str(bot.get("name") or bot.get("bot_name") or ""),
                    }
                )
        print(
            f"accessible_bots count={len(bot_candidates)} "
            f"configured_found={any(row['id'] == bot_id for row in bot_candidates)}"
        )
        for row in bot_candidates[:10]:
            print(f"bot_candidate id={row['id']} name={row['name'][:80]}")
    except Exception as exc:
        print(f"workspace_bot_list transport_error={type(exc).__name__}")


if __name__ == "__main__":
    main()

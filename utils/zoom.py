"""
Zoom Server-to-Server OAuth integration.

Setup:
1. Go to https://marketplace.zoom.us/ → Develop → Build App → Server-to-Server OAuth
2. Add scopes: meeting:write:admin, meeting:read:admin, user:read:admin
3. Copy Account ID, Client ID, Client Secret into .env:
   ZOOM_ACCOUNT_ID=...
   ZOOM_CLIENT_ID=...
   ZOOM_CLIENT_SECRET=...
"""
import os
import base64
import requests
from dotenv import load_dotenv

from pathlib import Path
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ZOOM_ACCOUNT_ID    = os.getenv("ZOOM_ACCOUNT_ID", "")
ZOOM_CLIENT_ID     = os.getenv("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET", "")
ZOOM_TIMEZONE      = os.getenv("ZOOM_TIMEZONE", "Asia/Kolkata")


class ZoomError(Exception):
    pass


def _get_access_token() -> str:
    if not all([ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET]):
        raise ZoomError(
            "Zoom credentials not configured. "
            "Set ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET in .env"
        )

    creds = f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()
    basic = base64.b64encode(creds).decode()

    r = requests.post(
        "https://zoom.us/oauth/token",
        headers={"Authorization": f"Basic {basic}"},
        params={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
        timeout=15,
    )
    if r.status_code != 200:
        raise ZoomError(f"Failed to get Zoom access token: {r.text}")
    return r.json()["access_token"]


def create_meeting(
    topic: str,
    start_date: str,      # YYYY-MM-DD
    start_time: str,      # HH:MM (24h)
    duration: int = 45,   # minutes
) -> dict:
    token = _get_access_token()
    start_at = f"{start_date}T{start_time}:00"

    payload = {
        "topic": topic,
        "type": 2,  # scheduled meeting
        "start_time": start_at,
        "duration": duration,
        "timezone": ZOOM_TIMEZONE,
        "settings": {
            "join_before_host": False,
            "waiting_room": True,
            "host_video": True,
            "participant_video": True,
            "mute_upon_entry": True,
        },
    }

    r = requests.post(
        "https://api.zoom.us/v2/users/me/meetings",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    if r.status_code not in (200, 201):
        raise ZoomError(f"Zoom API error {r.status_code}: {r.text}")

    data = r.json()
    return {
        "id":        data.get("id"),
        "join_url":  data.get("join_url"),
        "start_url": data.get("start_url"),
        "password":  data.get("password"),
    }

"""Minimal Telegram Bot API client (sendMessage)."""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class TelegramError(Exception):
    pass


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        base_url: str = "https://api.telegram.org",
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        if not bot_token or not chat_id:
            raise TelegramError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
        self._url = f"{base_url.rstrip('/')}/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=20)
        self.sleep = sleep

    def send(self, html_text: str, attempts: int = 3) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": html_text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        last = ""
        for i in range(attempts):
            try:
                resp = self.client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                last = str(exc)
                self.sleep(2 * (i + 1))
                continue
            data = _json(resp)
            if resp.status_code == 200 and data.get("ok"):
                return
            last = data.get("description") or resp.text[:200]
            if resp.status_code == 429:
                self.sleep(float((data.get("parameters") or {}).get("retry_after", 5)))
                continue
            if resp.status_code >= 500:
                self.sleep(2 * (i + 1))
                continue
            break  # 4xx: wrong token / chat id / formatting – no point retrying
        # Never include the token (it is part of the URL) in errors/logs.
        raise TelegramError(f"Telegram sendMessage failed: {last}")


def _json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}

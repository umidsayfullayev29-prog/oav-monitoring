"""Telegram Bot API orqali xabar yuborish."""
import time
import logging
import requests

log = logging.getLogger("oav.tg")


class Telegram:
    def __init__(self, token, chat_id):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.last = 0.0

    def send(self, text, silent=False, preview=True):
        if not self.token or not self.chat_id:
            log.info("[TG o'chiq] %s", text.replace("\n", " | ")[:300])
            return True
        wait = 1.05 - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        payload = {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "disable_notification": silent,
            "link_preview_options": {"is_disabled": not preview},
        }
        for attempt in range(4):
            try:
                r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=payload, timeout=20)
            except requests.RequestException as e:
                log.warning("TG tarmoq xatosi: %s", e)
                time.sleep(3)
                continue
            self.last = time.monotonic()
            if r.status_code == 200:
                return True
            try:
                j = r.json()
            except ValueError:
                j = {}
            if r.status_code == 429:
                time.sleep(int(j.get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            if r.status_code == 400 and "parse" in str(j.get("description", "")).lower():
                # HTML xato bo'lsa — oddiy matn sifatida
                payload["parse_mode"] = None
                payload["text"] = _strip_tags(text)
                continue
            log.warning("TG xato %s: %s", r.status_code, j.get("description"))
            return False
        return False


def _strip_tags(s):
    import re
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", s))

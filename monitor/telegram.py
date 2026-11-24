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

    def send(self, text, silent=False, preview=True, reply_to=None):
        """Yuborilsa message_id (int), aks holda None."""
        if not self.token or not self.chat_id:
            log.info("[TG o'chiq] %s", text.replace("\n", " | ")[:300])
            return -1
        payload = {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "disable_notification": silent,
            "link_preview_options": {"is_disabled": not preview},
        }
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        res = self._call("sendMessage", payload)
        if res is None:
            return None
        return (res.get("message_id") if isinstance(res, dict) else None) or -1

    def edit(self, message_id, text):
        if not self.token or not self.chat_id:
            log.info("[TG o'chiq] EDIT %s: %s", message_id, text.replace("\n", " | ")[:200])
            return True
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        return self._call("editMessageText", payload) is not None

    def _call(self, method, payload):
        wait = 1.05 - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(4):
            try:
                r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=20)
            except requests.RequestException as e:
                log.warning("TG tarmoq xatosi: %s", e)
                time.sleep(3)
                continue
            self.last = time.monotonic()
            if r.status_code == 200:
                try:
                    return r.json().get("result") or {}
                except ValueError:
                    return {}
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
                payload["text"] = _strip_tags(payload["text"])
                continue
            log.warning("TG xato %s: %s", r.status_code, j.get("description"))
            return None
        return None


def _strip_tags(s):
    import re
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", s))

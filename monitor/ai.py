"""Gemini orqali Telegram postini tekshirish: post Milliy statistika qo'mitasi ma'lumotiga asoslanganmi.

Kalit so'z bo'lmasa ham (masalan, raqamlar rasmda, manba aytilmagan) qo'mita ma'lumoti ishlatilgan
postlarni topish uchun. Faqat config'dagi telegram_channels_ai ro'yxatidagi kanallarga qo'llanadi.
"""
import os
import json
import base64
import logging

import requests

log = logging.getLogger("oav.ai")
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODEL", "gemini-2.5-flash,gemini-2.0-flash").split(",") if m.strip()]
PROMPT = """Sen O'zbekiston Milliy statistika qo'mitasi matbuot xizmati uchun OAV monitoringi qilasan.
Quyidagi Telegram post (matn va rasm) qo'mita bilan bog'liqmi, aniqla.

"match": true bo'ladi, agar post:
- Milliy statistika qo'mitasi (avvalgi Statistika agentligi, Нацкомстат, stat.uz) ma'lumotiga tayansa — manba aytilgan yoki aytilmagan bo'lsa ham, O'zbekiston bo'yicha rasmiy statistik raqamlar keltirilsa (aholi, tug'ilish, nikoh, narxlar/inflyatsiya, YaIM, sanoat, qishloq xo'jaligi, tashqi savdo, ish haqi, bandlik, turizm, qurilish, korxonalar soni va h.k.);
- yoki qo'mita faoliyati haqida bo'lsa (aholini/biznesni/qishloq xo'jaligini ro'yxatga olish, qo'mita rahbarlari, stat.uz loyihalari).
"match": false — agar raqamlar boshqa davlat haqida bo'lsa, boshqa idoraning o'z ma'lumoti bo'lsa (Markaziy bank kursi, prognozlar, kompaniya hisobotlari), reklama, sport yoki statistikaga aloqasi bo'lmasa.

Faqat JSON qaytar: {"match": true/false, "reason": "o'zbekcha qisqa izoh", "evidence": "postdan qisqa iqtibos (rasmdagi yozuv bo'lishi mumkin)"}"""


def available():
    return bool(os.environ.get("GEMINI_API_KEY"))


def check_post(text, image_url=None, session=None, timeout=40):
    """(match: bool, reason, evidence) yoki None (xato)."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    s = session or requests.Session()
    parts = [{"text": PROMPT + "\n\nPOST MATNI:\n" + (text or "(matn yo'q, faqat rasm)")[:6000]}]
    if image_url:
        try:
            r = s.get(image_url, timeout=20)
            if r.status_code == 200 and len(r.content) < 4_000_000:
                parts.append({"inline_data": {"mime_type": r.headers.get("content-type", "image/jpeg").split(";")[0],
                                              "data": base64.b64encode(r.content).decode()}})
        except requests.RequestException:
            pass
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    for model in MODELS:
        try:
            r = s.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                       headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                       data=json.dumps(body), timeout=timeout)
        except requests.RequestException as e:
            log.info("Gemini xato (%s): %s", model, e)
            continue
        if r.status_code != 200:
            log.info("Gemini %s: %s %s", model, r.status_code, r.text[:200])
            continue
        try:
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            d = json.loads(txt)
            return bool(d.get("match")), str(d.get("reason", ""))[:300], str(d.get("evidence", ""))[:300]
        except Exception as e:  # noqa
            log.info("Gemini javobi o'qilmadi: %s", e)
            return None
    return None

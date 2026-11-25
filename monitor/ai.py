"""Gemini orqali Telegram postini tekshirish: post Milliy statistika qo'mitasi ma'lumotiga asoslanganmi.

Kalit so'z bo'lmasa ham (masalan, raqamlar rasmda, manba aytilmagan) qo'mita ma'lumoti ishlatilgan
postlarni topish uchun. Faqat config'dagi telegram_channels_ai ro'yxatidagi kanallarga qo'llanadi.
"""
import os
import json
import base64
import logging
import re
import time

import requests

log = logging.getLogger("oav.ai")
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").split(",") if m.strip()]
API = "https://generativelanguage.googleapis.com/v1beta"
_resolved = []   # ishlagan model birinchi turadi (jarayon davomida eslab qolinadi)
_cool = {}       # model -> shu vaqtgacha ishlatilmaydi (429/503 dan keyin)
_found = []      # ListModels natijasi (bir marta so'raladi)
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
    d = generate(body, s, timeout)
    if d is None:
        return None
    return bool(d.get("match")), str(d.get("reason", ""))[:300], str(d.get("evidence", ""))[:300]


def _discover(s, key):
    """Kalit uchun mavjud flash modellarni API'dan so'rab, yangisini birinchi qilib qaytaradi."""
    try:
        r = s.get(f"{API}/models", params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=20)
        ms = r.json().get("models", []) if r.status_code == 200 else []
    except Exception:  # noqa
        return []
    out = []
    for m in ms:
        n = m.get("name", "").split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if "flash" not in n or any(x in n for x in ("image", "tts", "live", "audio", "thinking", "exp")):
            continue
        ver = tuple(int(x) for x in re.findall(r"\d+", n.split("flash")[0])[:2]) or (0,)
        out.append(("lite" not in n, ver, "preview" not in n, n))
    out.sort(reverse=True)
    return [x[-1] for x in out]


def generate(body, session=None, timeout=60):
    """Gemini generateContent; JSON javobni dict qilib qaytaradi yoki None.
    Model topilmasa (404) xabardagi tavsiya va ListModels orqali boshqasini o'zi tanlaydi."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    s = session or requests.Session()
    queue = list(dict.fromkeys(_resolved + MODELS))
    tried, discovered = set(), bool(_found)
    if discovered:
        queue += [m for m in _found if m not in queue]
    now = time.time()
    while queue:
        model = queue.pop(0)
        if model in tried or _cool.get(model, 0) > now:
            continue
        tried.add(model)
        busy = False
        try:
            r = s.post(f"{API}/models/{model}:generateContent",
                       headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                       data=json.dumps(body), timeout=timeout)
        except requests.RequestException as e:
            log.info("Gemini xato (%s): %s", model, str(e)[:120])
            r, busy = None, True
        if r is not None and r.status_code == 404:
            log.info("Gemini %s: 404", model)
            hint = [h.rstrip(".") for h in re.findall(r"models/([\w.\-]+)", r.text)]
            queue[:0] = [h for h in hint if h != model and h not in tried]
        elif r is not None and r.status_code in (429, 500, 503):
            log.info("Gemini %s: %s %s", model, r.status_code,
                     " ".join(re.findall(r'"(?:quotaId|quotaMetric|quotaValue|retryDelay)": "?([^",}]+)', r.text))[:300])
            busy = True
            # kvota tugagan model bir muddat chetga
            _cool[model] = now + (300 if r.status_code == 429 else 60)
        elif r is not None and r.status_code != 200:
            log.info("Gemini %s: %s %s", model, r.status_code, r.text[:200])
        if r is None or r.status_code != 200:
            # boshqa model sinab ko'rish uchun ro'yxatni bir marta API'dan olamiz
            if not queue and not discovered:
                discovered = True
                _found[:] = _discover(s, key)
                queue = [m for m in _found if m not in tried]
                log.info("Gemini mavjud modellar: %s", queue[:6])
            if busy:
                time.sleep(1.5)
            continue
        if model in _resolved:
            _resolved.remove(model)
        _resolved.insert(0, model)
        try:
            cand = r.json()["candidates"][0]["content"]["parts"]
            txt = "".join(p.get("text", "") for p in cand if not p.get("thought"))
            txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(txt)
        except Exception as e:  # noqa
            log.info("Gemini javobi o'qilmadi (%s): %s", model, e)
            return None
    return None

"""@gazetalar_sharhi kanalidagi gazeta sahifalarini kuzatish (asosiy monitoringdan alohida).

Ishlash tartibi:
  1. Zaxira Telegram akkaunti (Telethon, TG_SESSION) orqali kanaldagi yangi postlarni o'qiydi.
  2. Har bir post (albom bo'lsa — butun albom) rasmlari va PDF fayllarini yuklab oladi.
  3. Sahifalarni Gemini'ga beradi: matnni o'qib (OCR), qo'mita yoki uning ma'lumotlari
     tilga olingan maqolalarni topadi.
  4. Topilganlarni @statistikaoav kanaliga (va nusxasini shaxsiy chatga) yuboradi.

Muhit: TG_API_ID, TG_API_HASH, TG_SESSION, GEMINI_API_KEY, TG_BOT_TOKEN, TG_CHAT_ID, ARCHIVE_CHAT.
Ixtiyoriy: GAZETA_TEST=N (oxirgi N ta postni tekshirib, natijani faqat shaxsiy chatga yuboradi),
GAZETA_SINCE=YYYY-MM-DD (birinchi ishga tushishda shu sanadan boshlab), RUN_BUDGET_SEC.
"""
import os
import re
import sys
import json
import time
import html
import base64
import asyncio
import logging
import datetime as dt
from pathlib import Path

import requests

from monitor import ai
from monitor.telegram import Telegram

log = logging.getLogger("oav.gazeta")
TZ = dt.timezone(dt.timedelta(hours=5))
ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "gazeta_state.json"
LOG_PATH = ROOT / "data" / "gazeta_found.jsonl"
CHANNEL = os.environ.get("GAZETA_CHANNEL", "gazetalar_sharhi")
BUDGET = int(os.environ.get("RUN_BUDGET_SEC", "1800"))
INLINE_MAX = 13_000_000          # base64 bilan 20 MB so'rov chegarasiga sig'ishi uchun
IMG_BATCH = 6                    # bitta so'rovda nechta sahifa rasmi
GEMINI_GAP = 7.0                 # bepul tarif daqiqalik cheklovi uchun so'rovlar orasidagi pauza
KW_RE = re.compile(r"(statisti\w*|статисти\w*|статкўм\w*|statqo\w*|нацкомстат\w*|stat\.uz)", re.I)

PROMPT = """Sen O'zbekiston Milliy statistika qo'mitasi matbuot xizmati uchun gazetalar monitoringini qilasan.
Senga gazeta sahifalari beriladi ({what}). Har bir sahifani diqqat bilan o'qi (matn rasmda bo'lishi mumkin).

Quyidagi maqolalarni top:
- Milliy statistika qo'mitasi (avvalgi Davlat statistika qo'mitasi / Statistika agentligi, Нацкомстат, Госкомстат, stat.uz)
  nomi tilga olingan yoki uning ma'lumotiga tayangan maqolalar;
- qo'mita faoliyati: aholini, biznesni, qishloq xo'jaligini ro'yxatga olish (perepis), qo'mita rahbarlari,
  hududiy statistika boshqarmalari;
- manbasi aytilmagan bo'lsa ham, O'zbekiston bo'yicha rasmiy statistik raqamlar (aholi, tug'ilish, nikoh,
  narxlar/inflyatsiya, YaIM, sanoat, qishloq xo'jaligi, tashqi savdo, ish haqi, bandlik, turizm va h.k.) keltirilgan maqolalar.
Topma: boshqa davlat statistikasi, Markaziy bank kursi, prognozlar, kompaniya hisobotlari, reklama, "statistika" so'zi
umumiy ma'noda (masalan, sport statistikasi) ishlatilgan joylar.

Faqat JSON qaytar:
{{"newspaper": "gazeta nomi (birinchi sahifadan)", "items": [{{"page": <{page_hint}>, "title": "maqola sarlavhasi",
"quote": "maqoladan aynan iqtibos, 1-3 gap, asl tilida", "reason": "o'zbekcha qisqa izoh"}}]}}
Hech narsa topilmasa: {{"newspaper": "...", "items": []}}"""


def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"last_id": 0, "pending": [], "sent": []}


def save_state(st):
    st["sent"] = st.get("sent", [])[-3000:]
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def _b64(b):
    return base64.b64encode(b).decode()


def pdf_chunks(data):
    """PDF'ni Gemini'ga sig'adigan bo'laklarga ajratadi: [(boshlang'ich_bet, bytes, mime)]."""
    if len(data) <= INLINE_MAX:
        return [(1, data, "application/pdf")]
    import fitz  # PyMuPDF
    src = fitz.open(stream=data, filetype="pdf")
    out, i, n = [], 0, src.page_count
    step = max(1, int(n * INLINE_MAX / len(data) * 0.8))
    while i < n:
        j = min(n, i + step)
        part = fitz.open()
        part.insert_pdf(src, from_page=i, to_page=j - 1)
        b = part.tobytes(garbage=3, deflate=True)
        if len(b) > INLINE_MAX and j - i > 1:
            step = max(1, step // 2)
            continue
        if len(b) > INLINE_MAX:   # bitta bet ham sig'masa — rasmga aylantiramiz
            pix = src[i].get_pixmap(dpi=130)
            out.append((i + 1, pix.tobytes("jpeg"), "image/jpeg"))
        else:
            out.append((i + 1, b, "application/pdf"))
        i = j
    return out


def ask_gemini(parts_media, what, page_hint, caption, session):
    text = PROMPT.format(what=what, page_hint=page_hint)
    if caption:
        text += "\n\nPOST IZOHI (kanal yozgan):\n" + caption[:3000]
    parts = [{"text": text}]
    for label, data, mime in parts_media:
        if label:
            parts.append({"text": label})
        parts.append({"inline_data": {"mime_type": mime, "data": _b64(data)}})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    d = ai.generate(body, session, timeout=240)
    time.sleep(GEMINI_GAP)
    if isinstance(d, list):
        d = {"items": d}
    return d if isinstance(d, dict) else None


def analyse_unit(unit, session):
    """unit: {"msgs": [{"id", "kind": photo|pdf, "data": bytes}], "caption"}.
    Topilmalar ro'yxati [{"msg_id", "page", "title", "quote", "reason", "newspaper"}] yoki xato bo'lsa None."""
    found = []
    photos = [m for m in unit["msgs"] if m["kind"] == "photo"]
    pdfs = [m for m in unit["msgs"] if m["kind"] == "pdf"]
    for k in range(0, len(photos), IMG_BATCH):
        batch = photos[k:k + IMG_BATCH]
        media = [(f"{i + 1}-rasm:", m["data"], "image/jpeg") for i, m in enumerate(batch)]
        d = ask_gemini(media, f"{len(batch)} ta rasm, har biri bitta gazeta sahifasi",
                       "rasm tartib raqami, 1 dan", unit["caption"], session)
        if d is None:
            return None
        for it in d.get("items") or []:
            try:
                idx = int(it.get("page") or 1) - 1
            except (TypeError, ValueError):
                idx = 0
            m = batch[min(max(idx, 0), len(batch) - 1)]
            found.append({**it, "msg_id": m["id"], "page": None, "newspaper": d.get("newspaper")})
    for m in pdfs:
        for start, data, mime in pdf_chunks(m["data"]):
            what = "PDF fayl" if start == 1 and mime == "application/pdf" else f"PDF faylning {start}-betdan boshlangan qismi"
            d = ask_gemini([("", data, mime)], what, "ushbu fayldagi bet tartib raqami, 1 dan", unit["caption"], session)
            if d is None:
                return None
            for it in d.get("items") or []:
                try:
                    pg = int(it.get("page")) + start - 1
                except (TypeError, ValueError):
                    pg = None
                found.append({**it, "msg_id": m["id"], "page": pg, "newspaper": d.get("newspaper")})
    return found


def post_text(link, date, items):
    first = items[0]
    title = html.escape((first.get("title") or "").strip(), quote=False) or "Gazeta sahifasida qo'mita ma'lumoti"
    lines = [html.escape(link), f"📰 <b>{title}</b>", f"🗓 {date.strftime('%d.%m.%Y')}"]
    q = []
    for it in items[:4]:
        head = ", ".join(x for x in [(it.get("newspaper") or "").strip(),
                                     f"{it['page']}-bet" if it.get("page") else ""] if x)
        quote = html.escape((it.get("quote") or "").strip(), quote=False)[:500]
        quote = KW_RE.sub(lambda mm: f"<b><u>{mm.group(0)}</u></b>", quote)
        q.append((f"📄 {html.escape(head, quote=False)}: " if head else "") + quote)
    lines.append("<blockquote>" + "\n\n".join(q)[:1500] + "</blockquote>")
    return "\n".join(lines)


async def fetch_units(client, entity, st, test_n, since):
    """Tekshirilishi kerak bo'lgan postlarni albomlar bo'yicha guruhlab qaytaradi (eskisidan yangisiga)."""
    msgs = []
    if test_n:
        async for m in client.iter_messages(entity, limit=test_n * 12):
            msgs.append(m)
        msgs.reverse()
    elif st.get("last_id"):
        async for m in client.iter_messages(entity, min_id=st["last_id"], reverse=True, limit=500):
            msgs.append(m)
    else:
        floor = since or (dt.datetime.now(TZ) - dt.timedelta(days=1))
        async for m in client.iter_messages(entity, limit=600):
            if m.date < floor:
                break
            msgs.append(m)
        msgs.reverse()
    units, by_group = [], {}
    for m in msgs:
        key = m.grouped_id or f"m{m.id}"
        if key not in by_group:
            by_group[key] = {"ids": [], "date": m.date, "caption": ""}
            units.append(by_group[key])
        u = by_group[key]
        u["ids"].append(m.id)
        if m.message:
            u["caption"] = (u["caption"] + "\n" + m.message).strip()
    if test_n:
        units = [u for u in units if u["ids"]][-test_n:]
    return units, {m.id: m for m in msgs}


def media_kind(m):
    if getattr(m, "photo", None):
        return "photo"
    doc = getattr(m, "document", None)
    if doc:
        mime = (doc.mime_type or "").lower()
        if mime == "application/pdf":
            return "pdf"
        if mime.startswith("image/"):
            return "photo"
    return None


async def main_async():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id, api_hash, sess = (os.environ.get(k, "").strip() for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"))
    if not (api_id and api_hash and sess):
        log.warning("TG_API_ID / TG_API_HASH / TG_SESSION secretlari qo'shilmagan — gazeta monitoringi o'tkazib yuborildi")
        return 0
    if not ai.available():
        log.warning("GEMINI_API_KEY yo'q — o'tkazib yuborildi")
        return 0
    test_n = int(os.environ.get("GAZETA_TEST", "0") or 0)
    since = os.environ.get("GAZETA_SINCE", "").strip()
    since = dt.datetime.fromisoformat(since).replace(tzinfo=TZ) if since else None
    if not test_n and not STATE_PATH.exists() and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        log.info("Gazeta monitoringi hali qo'lda ishga tushirilmagan (sinovdan keyin yoqiladi) — o'tkazib yuborildi")
        return 0
    t0 = time.monotonic()

    token = os.environ.get("TG_BOT_TOKEN")
    dm = Telegram(token, os.environ.get("TG_CHAT_ID"))
    chan = Telegram(token, os.environ.get("ARCHIVE_CHAT", "@statistikaoav"))
    st = load_state()
    sent = set(st.get("sent", []))
    session = requests.Session()
    stats = {"posts": 0, "pages": 0, "found": 0, "sent": 0, "err": 0}

    async with TelegramClient(StringSession(sess), int(api_id), api_hash) as client:
        try:
            entity = await client.get_entity(CHANNEL)
        except Exception as e:  # noqa
            log.error("Kanal ochilmadi (%s): %s", CHANNEL, e)
            return 1
        units, _ = await fetch_units(client, entity, st, test_n, since)
        # avvalgi safar xato bergan postlar — birinchi navbatda
        pend = [] if test_n else [p for p in st.get("pending", []) if p.get("tries", 0) < 3]
        queue = pend + [{"ids": u["ids"], "caption": u["caption"], "tries": 0} for u in units]
        new_pending, last_ok = [], st.get("last_id", 0)
        log.info("Navbatda: %d post (%d tasi qayta urinish)", len(queue), len(pend))

        for qi, u in enumerate(queue):
            if time.monotonic() - t0 > BUDGET:
                log.info("Vaqt tugadi — qolganlari keyingi safar")
                if not test_n:
                    new_pending += [x for x in queue[qi:] if x in pend]
                break
            ms = [m for m in await client.get_messages(entity, ids=u["ids"]) if m]
            items = []
            for m in ms:
                kind = media_kind(m)
                if not kind:
                    continue
                data = await client.download_media(m, file=bytes)
                if data:
                    items.append({"id": m.id, "kind": kind, "data": data})
            caption = u.get("caption") or ""
            if not items:
                if u not in pend:
                    last_ok = max(last_ok, max(u["ids"]))
                continue
            stats["posts"] += 1
            stats["pages"] += len(items)
            res = analyse_unit({"msgs": items, "caption": caption}, session)
            if res is None:
                stats["err"] += 1
                if not test_n:
                    new_pending.append({**u, "tries": u.get("tries", 0) + 1})
            else:
                by_msg = {}
                for it in res:
                    by_msg.setdefault(it["msg_id"], []).append(it)
                for mid, its in by_msg.items():
                    stats["found"] += 1
                    key = f"{CHANNEL}/{mid}"
                    msg = next((m for m in ms if m.id == mid), ms[0])
                    link = f"https://t.me/{CHANNEL}/{mid}"
                    text = post_text(link, msg.date.astimezone(TZ), its)
                    if test_n:
                        dm.send("🧪 Sinov (kanalga yuborilmadi)\n" + text, preview=False)
                        continue
                    if key in sent:
                        continue
                    ok = chan.send(text, silent=True, preview_url=link)
                    dm.send(("🗞 Gazeta" if ok else "⚠️ Kanalga yuborilmadi — gazeta") + "\n" + text, preview=False)
                    if ok:
                        sent.add(key)
                        st.setdefault("sent", []).append(key)
                        stats["sent"] += 1
                    with LOG_PATH.open("a") as f:
                        f.write(json.dumps({"key": key, "date": msg.date.isoformat(), "items": its,
                                            "sent": bool(ok)}, ensure_ascii=False) + "\n")
            if u not in pend:
                last_ok = max(last_ok, max(u["ids"]))
            if not test_n:
                st["last_id"], st["pending"] = last_ok, new_pending[-100:]
                save_state(st)

    if not test_n:
        st["last_id"], st["pending"] = last_ok, new_pending[-100:]
        save_state(st)
    else:
        dm.send(f"🧪 Gazeta sinovi tugadi: {stats['posts']} post, {stats['pages']} sahifa/fayl, "
                f"{stats['found']} topilma, xato {stats['err']}. Model: {', '.join(ai._resolved) or '—'}", preview=False)
    log.info("Gazeta: %s, model=%s", stats, ai._resolved)
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()

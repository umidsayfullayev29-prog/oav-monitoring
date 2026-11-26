"""@gazetalar_sharhi kanalidagi gazeta sahifalarini kuzatish (asosiy monitoringdan alohida).

Ishlash tartibi:
  1. Zaxira Telegram akkaunti (Telethon, TG_SESSION) orqali kanaldagi yangi postlarni o'qiydi.
  2. Har bir post (albom bo'lsa — butun albom) rasmlari va PDF fayllarini yuklab oladi.
  3. PDF ichidagi matndan qo'mita nomlarini kalit so'z bilan qidiradi (AI'siz, config/gazeta_keywords.yml).
     Eski shrift kodlashi bo'lsa tuzatadi (monitor/gazeta_text.py). Shunda ham o'qilmasa (skan) yoki post
     rasm bo'lsa — Gemini faqat matnni o'qiydi (OCR); qo'mita tilga olinganmi — baribir kalit so'z hal qiladi.
  4. Topilganlarni faqat shaxsiy chatga (TG_CHAT_ID) yuboradi — kanalga yubormaydi.

Asosiy monitoringdan to'liq ajratilgan: o'z boti (GAZETA_BOT_TOKEN), o'z Gemini kaliti/loyihasi
(GAZETA_GEMINI_KEY — kunlik limit asosiy monitoring bilan bo'linmaydi), o'z kodi (monitor/ai.py va
monitor/telegram.py ishlatilmaydi), o'z holati va workflow'i.

Muhit: TG_API_ID, TG_API_HASH, TG_SESSION, GAZETA_GEMINI_KEY, GAZETA_BOT_TOKEN, TG_CHAT_ID.
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
import yaml

from monitor import gazeta_text as gt

log = logging.getLogger("oav.gazeta")
KW = gt.Matcher(yaml.safe_load((Path(__file__).resolve().parent.parent / "config/gazeta_keywords.yml").read_text("utf-8")))
TZ = dt.timezone(dt.timedelta(hours=5))
ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "gazeta_state.json"
LOG_PATH = ROOT / "data" / "gazeta_found.jsonl"
RUNS_PATH = ROOT / "data" / "gazeta_runs.jsonl"      # har tekshiruv natijasi — kunlik/haftalik hisobot uchun
OCR_USED = {"n": 0}                                   # shu ishga tushishda Gemini OCR qilingan fayllar
CHANNEL = os.environ.get("GAZETA_CHANNEL", "gazetalar_sharhi")
BUDGET = int(os.environ.get("RUN_BUDGET_SEC", "1800"))
INLINE_MAX = 13_000_000          # base64 bilan 20 MB so'rov chegarasiga sig'ishi uchun
IMG_BATCH = 6                    # bitta so'rovda nechta sahifa rasmi
GEMINI_GAP = 7.0                 # bepul tarif daqiqalik cheklovi uchun so'rovlar orasidagi pauza
MAX_TRIES = 12                   # shuncha marta Gemini xato bersa — post tashlanadi va sizga xabar keladi
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODELS = [m.strip() for m in os.environ.get("GAZETA_GEMINI_MODEL", "gemini-3.5-flash,gemini-3.8-flash").split(",")
                 if m.strip()]

# Gemini faqat MATN O'QIYDI (OCR): qaror qabul qilmaydi. Qo'mita tilga olinganmi — kalit so'zlar hal qiladi.
OCR_PROMPT = """Senga gazeta sahifalari beriladi ({what}). Sen faqat matnni o'qib beruvchisan (OCR), izoh berma.
Sahifalardagi quyidagi so'zlardan birortasi bor HAR BIR joyni toping va o'sha gapni (1-3 gap) AYNAN,
asl tilida va asl yozuvida (lotin/kirill), tarjimasiz va o'zgartirmasdan ko'chiring:
statistika, статистика, statistik, статистик, qo'mita, қўмита, комитет, нацкомстат, госкомстат, stat.uz,
ro'yxatga olish, рўйхатга олиш, перепись, census.
Faqat JSON qaytar: {{"newspaper": "gazeta nomi (bosh sahifadan)", "date": "soni chiqqan sana dd.mm.yyyy (bo'lmasa bo'sh)",
"passages": [{{"page": <{page_hint}>, "text": "aynan ko'chirilgan gap"}}]}}
Bunday so'z bo'lmasa: {{"newspaper": "...", "date": "...", "passages": []}}"""


# ---------- Telegram Bot API (faqat shaxsiy chat) ----------

class Bot:
    def __init__(self, token, chat_id):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.last = 0.0

    def send(self, text):
        """Yuborilsa True."""
        if not self.token or not self.chat_id:
            log.info("[TG o'chiq] %s", text.replace("\n", " | ")[:300])
            return False
        payload = {"chat_id": self.chat_id, "text": text[:4096], "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}}
        for _ in range(4):
            wait = 1.05 - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
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
                payload["parse_mode"] = None
                payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", payload["text"]))
                continue
            log.warning("TG xato %s: %s", r.status_code, j.get("description"))
            return False
        return False


# ---------- Gemini (gazetaning o'z kaliti) ----------

_resolved = []   # ishlagan model birinchi turadi
_cool = {}       # model -> shu vaqtgacha ishlatilmaydi (429/503 dan keyin)
_found = []      # ListModels natijasi


def _discover(s, key):
    try:
        r = s.get(f"{GEMINI_API}/models", params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=20)
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


def gemini_generate(body, s, timeout=240):
    """generateContent; JSON javobni qaytaradi yoki None. 404/429/503 da boshqa modelga o'tadi."""
    key = os.environ.get("GAZETA_GEMINI_KEY", "").strip()
    if not key:
        return None
    queue = list(dict.fromkeys(_resolved + GEMINI_MODELS))
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
            r = s.post(f"{GEMINI_API}/models/{model}:generateContent",
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
                     " ".join(re.findall(r'"(?:quotaId|quotaValue|retryDelay)": "?([^",}]+)', r.text))[:300])
            busy = True
            _cool[model] = now + (300 if r.status_code == 429 else 60)
        elif r is not None and r.status_code != 200:
            log.info("Gemini %s: %s %s", model, r.status_code, r.text[:200])
        if r is None or r.status_code != 200:
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
            log.info("Gemini javob berdi: %s", model)
            return json.loads(txt)
        except Exception as e:  # noqa
            log.info("Gemini javobi o'qilmadi (%s): %s", model, e)
            return None
    return None


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


def ask_gemini(parts_media, what, page_hint, session):
    parts = [{"text": OCR_PROMPT.format(what=what, page_hint=page_hint)}]
    for label, data, mime in parts_media:
        if label:
            parts.append({"text": label})
        parts.append({"inline_data": {"mime_type": mime, "data": _b64(data)}})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    d = gemini_generate(body, session, timeout=240)
    time.sleep(GEMINI_GAP)
    if isinstance(d, list):
        d = {"items": d}
    return d if isinstance(d, dict) else None


def _hits_from_text(text, page, msg_id, how):
    out = []
    for word, st, en in gt.find_all(KW, text):
        sn = gt.snippet(text, st, en, 160).replace("<b>", "<b><u>").replace("</b>", "</u></b>")
        out.append({"msg_id": msg_id, "page": page, "word": word, "quote": sn, "how": how})
    return out


def _ocr_hits(d, msg_id, page_of):
    """Gemini o'qigan parchalardan faqat kalit so'z bilan tasdiqlanganlari."""
    out = []
    for ps in d.get("passages") or []:
        t = gt.clean_page(str(ps.get("text") or ""))
        if not t:
            continue
        for h in _hits_from_text(t, page_of(ps.get("page")), msg_id, "ocr")[:1]:
            out.append(h)
    return out


def analyse_unit(unit, session):
    """unit: {"msgs": [{"id", "kind": photo|pdf, "data": bytes}], "caption"}.
    1) PDF matni + kalit so'z (AI'siz); 2) eski shrift kodlashi tuzatiladi; 3) shunda ham o'qilmasa
    yoki rasm bo'lsa — Gemini faqat matnni o'qiydi, qarorni kalit so'zlar qiladi.
    Natija: (topilmalar, {"newspaper", "date"} — Gemini o'qigan bo'lsa) yoki Gemini xatosida None."""
    found, meta = [], {}
    photos = [m for m in unit["msgs"] if m["kind"] == "photo"]
    pdfs = [m for m in unit["msgs"] if m["kind"] == "pdf"]
    for m in pdfs:
        pages = []
        try:
            import fitz  # PyMuPDF
            pages = [gt.clean_page(p.get_text()) for p in fitz.open(stream=m["data"], filetype="pdf")]
        except Exception as e:  # noqa
            log.info("PDF ochilmadi (%s): %s", m["id"], e)
        pages, note = gt.fix_text(pages)
        if pages and gt.readable(pages):
            log.info("%s: matn (%d bet%s)", m["id"], len(pages), ", " + note if note else "")
            for i, t in enumerate(pages):
                found += _hits_from_text(t, i + 1, m["id"], "matn")
            continue
        log.info("%s: matn o'qilmadi (%s) — Gemini OCR", m["id"], note or "skan/bo'sh")
        OCR_USED["n"] += 1
        for start, data, mime in pdf_chunks(m["data"]):
            what = "PDF fayl" if start == 1 and mime == "application/pdf" else f"PDF faylning {start}-betdan boshlangan qismi"
            d = ask_gemini([("", data, mime)], what, "ushbu fayldagi bet tartib raqami, 1 dan", session)
            if d is None:
                return None
            meta = meta or {"newspaper": d.get("newspaper"), "date": d.get("date")}

            def page_of(pg, start=start):
                try:
                    return int(pg) + start - 1
                except (TypeError, ValueError):
                    return None
            found += _ocr_hits(d, m["id"], page_of)
    OCR_USED["n"] += len(photos)
    for k in range(0, len(photos), IMG_BATCH):
        batch = photos[k:k + IMG_BATCH]
        media = [(f"{i + 1}-rasm:", m["data"], "image/jpeg") for i, m in enumerate(batch)]
        d = ask_gemini(media, f"{len(batch)} ta rasm, har biri bitta gazeta sahifasi", "rasm tartib raqami, 1 dan", session)
        if d is None:
            return None
        meta = meta or {"newspaper": d.get("newspaper"), "date": d.get("date")}
        for ps in d.get("passages") or []:
            try:
                idx = int(ps.get("page") or 1) - 1
            except (TypeError, ValueError):
                idx = 0
            mm = batch[min(max(idx, 0), len(batch) - 1)]
            found += _ocr_hits({"passages": [ps]}, mm["id"], lambda pg: None)
    return found, meta


def paper_name(caption, fname):
    """Gazeta nomi: post izohidagi «...» yoki fayl nomi."""
    m = re.search(r"[«“\"]([^»”\"]{2,60})[»”\"]", caption or "")
    if m:
        return m.group(1).strip()
    stem = re.sub(r"\.pdf$", "", fname or "", flags=re.I)
    stem = re.sub(r"[_\-]+", " ", stem).strip()
    return stem[:60]


def paper_date(*texts):
    """Soni chiqqan sana: izoh/fayl nomidagi dd.mm.yyyy (yoki dd-mm-yy)."""
    for t in texts:
        m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b", t or "")
        if m:
            y = m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3)
            if 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31:
                return f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{y}"
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t or "")
        if m:
            return f"{int(m.group(3)):02d}.{int(m.group(2)):02d}.{m.group(1)}"
    return None


def post_text(link, name, issue, items):
    """Sarlavha: gazeta nomi va soni chiqqan sana; keyin bet va kalit so'z atrofidagi parcha."""
    lines = [html.escape(link), f"📰 <b>{html.escape(name or 'Gazeta', quote=False)}, {issue}</b>"]
    q = []
    for it in items[:4]:
        head = f"{it['page']}-bet" if it.get("page") else ""
        q.append((f"📄 {head}: " if head else "📄 ") + it["quote"][:600])
    lines.append("<blockquote>" + "\n\n".join(q)[:2500] + "</blockquote>")
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
    if not os.environ.get("GAZETA_GEMINI_KEY", "").strip():
        log.warning("GEMINI_API_KEY_GAZETA secreti qo'shilmagan — o'tkazib yuborildi")
        return 0
    if not os.environ.get("GAZETA_BOT_TOKEN", "").strip():
        log.warning("GAZETA_BOT_TOKEN secreti qo'shilmagan — o'tkazib yuborildi")
        return 0
    test_n = int(os.environ.get("GAZETA_TEST", "0") or 0)
    since = os.environ.get("GAZETA_SINCE", "").strip()
    since = dt.datetime.fromisoformat(since).replace(tzinfo=TZ) if since else None
    if not test_n and not since and not STATE_PATH.exists():
        log.info("Gazeta monitoringi hali qo'lda ishga tushirilmagan (sinovdan keyin yoqiladi) — o'tkazib yuborildi")
        return 0
    t0 = time.monotonic()

    dm = Bot(os.environ.get("GAZETA_BOT_TOKEN"), os.environ.get("TG_CHAT_ID"))
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
        pend = [] if test_n else [p for p in st.get("pending", []) if p.get("tries", 0) < MAX_TRIES]
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
                fname = ""
                for a in (getattr(getattr(m, "document", None), "attributes", None) or []):
                    fname = getattr(a, "file_name", "") or fname
                if data:
                    items.append({"id": m.id, "kind": kind, "data": data, "fname": fname})
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
                    tries = u.get("tries", 0) + 1
                    if tries < MAX_TRIES:
                        new_pending.append({**u, "tries": tries})
                    else:   # jim yo'qolmasin
                        link = f"https://t.me/{CHANNEL}/{min(u['ids'])}"
                        log.warning("Gemini %d marta xato — post tashlandi: %s", tries, link)
                        dm.send(f"⚠️ Gazeta: post {tries} marta tekshirilmadi (Gemini xatosi), tashlab yuborildi:\n"
                                + html.escape(link))
            else:
                res, meta = res
                by_msg, send_failed = {}, False
                for it in res:
                    by_msg.setdefault(it["msg_id"], []).append(it)
                for mid, its in by_msg.items():
                    stats["found"] += 1
                    key = f"{CHANNEL}/{mid}"
                    msg = next((m for m in ms if m.id == mid), ms[0])
                    link = f"https://t.me/{CHANNEL}/{mid}"
                    fname = next((x["fname"] for x in items if x["id"] == mid), "")
                    name = (meta.get("newspaper") or "").strip() or paper_name(caption, fname)
                    issue = (paper_date(caption, fname, meta.get("date") or "")
                             or msg.date.astimezone(TZ).strftime("%d.%m.%Y"))
                    text = post_text(link, name, issue, its)
                    if test_n:
                        dm.send("🧪 Sinov\n" + text)
                        continue
                    if key in sent:
                        continue
                    ok = dm.send("🗞 Gazeta\n" + text)
                    if ok:
                        sent.add(key)
                        st.setdefault("sent", []).append(key)
                        stats["sent"] += 1
                        with LOG_PATH.open("a") as f:
                            f.write(json.dumps({"key": key, "date": msg.date.isoformat(), "name": name,
                                                "issue": issue, "found_at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
                                                "items": its, "sent": True}, ensure_ascii=False) + "\n")
                    else:
                        send_failed = True
                if send_failed and not test_n:   # yuborilmadi — keyingi safar qayta (yuborilganlari takrorlanmaydi)
                    new_pending.append({**u, "tries": u.get("tries", 0) + 1})
            if u not in pend:
                last_ok = max(last_ok, max(u["ids"]))
            if not test_n:
                st["last_id"], st["pending"] = last_ok, new_pending[-100:]
                save_state(st)

    if not test_n:
        st["last_id"], st["pending"] = last_ok, new_pending[-100:]
        save_state(st)
        with RUNS_PATH.open("a") as f:
            f.write(json.dumps({"t": dt.datetime.now(TZ).isoformat(timespec="seconds"), "posts": stats["posts"],
                                "files": stats["pages"], "found": stats["found"], "sent": stats["sent"],
                                "err": stats["err"], "ocr": OCR_USED["n"]}) + "\n")
    else:
        dm.send(f"🧪 Gazeta sinovi tugadi: {stats['posts']} post, {stats['pages']} sahifa/fayl, "
                f"{stats['found']} topilma, xato {stats['err']}. Model: {', '.join(_resolved) or '—'}")
    log.info("Gazeta: %s, model=%s", stats, _resolved)
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()

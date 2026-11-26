"""Gazeta: AI'siz usulni sinash — PDF ichidagi matnni ajratib, kalit so'z qidirish (Gemini ishlatilmaydi).

@gazetalar_sharhi dagi postlar (PROBE_SINCE dan PROBE_MAX_ID gacha) yuklanadi; har PDF uchun:
matn bormi, o'qiladimi (buzuq emasmi), qaysi betda qo'mita tilga olingan. Natija: data/gazeta_probe.md
va shaxsiy chatga qisqa xabar (gazeta boti). Holat (gazeta_state.json) o'zgarmaydi, kanalga hech narsa ketmaydi.
"""
import os
import re
import sys
import html
import asyncio
import logging
import datetime as dt
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from monitor.gazeta import Bot, CHANNEL, TZ, media_kind  # noqa: E402
from monitor.textutil import Matcher, snippet  # noqa: E402

log = logging.getLogger("oav.gazeta.probe")
LETTERS = re.compile(r"[A-Za-zА-Яа-яЎўҚқҒғҲҳЁёЄєІіЇїÖöŞşĞğÇçʻʼ‘’']")
COMMON = re.compile(r"\b(va|bilan|uchun|ham|ва|билан|учун|ҳам|ва|и|в|на|не|что|для)\b", re.I)


UZ_RU = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯЎҚҒҲ")
HI = re.compile(r"[\u00c0-\u00ff]")
# eski o'zbek shriftlari: cp1251 da yo'q harflar bo'sh o'rinlarga qo'yilgan (cp1251 bo'yicha o'qilgandagi ko'rinishi)
LEGACY_MAP = {"ґ": "ў", "Ґ": "Ҳ", "і": "қ", "І": "Қ"}


def is_legacy(t):
    """Kirill matn cp1251 baytlari lotin-1 harflari bo'lib chiqqanmi (Ñêàíèðóéòå = Сканируйте)."""
    letters = [c for c in t if c.isalpha()]
    return len(letters) > 200 and len(HI.findall(t)) / len(letters) > 0.3


def decode_legacy(t, mapping=None):
    out = []
    for ch in t:
        b = None
        for enc in ("cp1252", "latin-1"):
            try:
                bb = ch.encode(enc)
                if len(bb) == 1 and bb[0] >= 0x80:
                    b = bb
                break
            except UnicodeEncodeError:
                continue
        out.append(b.decode("cp1251", errors="replace") if b else ch)
    s = "".join(out)
    for a, c in (mapping or {}).items():
        s = s.replace(a, c)
    return s


def odd_letters(t):
    """cp1251 dan keyin o'zbek/rus alifbosida yo'q harflar va misol so'zlar."""
    from collections import Counter, defaultdict
    cnt, ex = Counter(), defaultdict(list)
    for w in re.findall(r"\w+", t):
        for c in set(w):
            if c.isalpha() and c not in UZ_RU and not ("a" <= c.lower() <= "z"):
                cnt[c] += 1
                if len(ex[c]) < 4 and w not in ex[c]:
                    ex[c].append(w)
    return [(c, n, ex[c]) for c, n in cnt.most_common(14)]


def clean(t):
    t = re.sub(r"-\s*\n\s*", "", t)           # satr oxiridagi bo'g'in ko'chirish
    return re.sub(r"\s+", " ", t).strip()


def pdf_report(data, kw):
    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    pages, total, hits = doc.page_count, "", []
    raw = [clean(p.get_text()) for p in doc]
    legacy = is_legacy(" ".join(raw))
    odd = odd_letters(decode_legacy(" ".join(raw))) if legacy else []
    for i, t in enumerate(raw):
        if legacy:
            t = decode_legacy(t, LEGACY_MAP)
        total += " " + t
        m = kw.match(t, trusted=True)
        if m:
            hits.append((i + 1, m[1], re.sub(r"<[^>]+>", "", snippet(t, m[2], m[3], 160))))
    chars = len(total.strip())
    nonspace = len(re.sub(r"\s", "", total)) or 1
    letter_ratio = len(LETTERS.findall(total)) / nonspace
    common = len(COMMON.findall(total))
    if chars < 300 * max(pages, 1) * 0.1 and chars < 1500:
        status = "matn yo'q (skan)"
    elif letter_ratio < 0.6 or common < 20:
        status = "matn buzuq"
    else:
        status = "matn bor"
    if legacy and status == "matn bor":
        status = "matn bor (kodlash tuzatildi)"
    return {"pages": pages, "chars": chars, "ratio": round(letter_ratio, 2), "common": common,
            "status": status, "hits": hits, "sample": total.strip()[:160], "odd": odd}


async def main_async():
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id, api_hash, sess = (os.environ.get(k, "").strip() for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"))
    since = dt.datetime.fromisoformat(os.environ.get("PROBE_SINCE", "2026-11-25")).replace(tzinfo=TZ)
    max_id = int(os.environ.get("PROBE_MAX_ID", "0") or 0)
    kw = Matcher(yaml.safe_load((ROOT / "config/keywords.yml").read_text("utf-8")))
    dm = Bot(os.environ.get("GAZETA_BOT_TOKEN"), os.environ.get("TG_CHAT_ID"))
    rows = []
    async with TelegramClient(StringSession(sess), int(api_id), api_hash) as client:
        entity = await client.get_entity(CHANNEL)
        msgs = []
        async for m in client.iter_messages(entity, limit=600):
            if m.date < since:
                break
            if max_id and m.id > max_id:
                continue
            msgs.append(m)
        msgs.reverse()
        captions = {}
        for m in msgs:
            if m.message:
                captions[m.grouped_id or m.id] = m.message.strip().split("\n")[0][:80]
        for m in msgs:
            kind = media_kind(m)
            if not kind:
                continue
            cap = captions.get(m.grouped_id or m.id, "")
            fname = ""
            if getattr(m, "document", None):
                for a in m.document.attributes:
                    fname = getattr(a, "file_name", "") or fname
            row = {"id": m.id, "kind": kind, "caption": cap, "file": fname}
            if kind == "pdf":
                data = await client.download_media(m, file=bytes)
                try:
                    row.update(pdf_report(data, kw))
                except Exception as e:  # noqa
                    row.update({"status": f"ochilmadi: {e}"[:80], "hits": []})
            else:
                row.update({"status": "rasm (OCR kerak)", "hits": []})
            rows.append(row)
            log.info("%s %s %s hits=%d", m.id, row.get("status"), fname or cap[:40], len(row.get("hits", [])))

    from collections import Counter
    cnt = Counter(r["status"] for r in rows)
    with_hits = [r for r in rows if r.get("hits")]
    lines = ["# Gazeta: AI'siz sinov (PDF matni + kalit so'z)", "",
             f"Fayllar: {len(rows)} — " + ", ".join(f"{k}: {v}" for k, v in cnt.items()),
             f"Qo'mita tilga olingan fayllar: {len(with_hits)}", "", "| post | holat | bet | izoh / fayl |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['status']} | {r.get('pages', '')} | "
                     f"{(r['caption'] or r['file']).replace('|', '/')[:70]} |")
    lines += ["", "## Topilmalar", ""]
    for r in with_hits:
        for pg, word, sn in r["hits"]:
            lines.append(f"- https://t.me/{CHANNEL}/{r['id']} — {pg}-bet — «{word}»: {sn}")
    lines += ["", "## Kodlash: cp1251 dan keyin alifboda yo'q harflar (misol so'zlar)", ""]
    for r in rows:
        if r.get("odd"):
            lines.append(f"- {r['id']} {r['file'][:40]}: " + "; ".join(f"{c}×{n} {' '.join(e)}" for c, n, e in r["odd"]))
    lines += ["", "## Matn namunasi (buzuqmi — ko'rish uchun)", ""]
    for r in rows:
        if r.get("sample"):
            lines.append(f"- {r['id']} ({r['status']}): {r['sample'][:150]}")
    (ROOT / "data/gazeta_probe.md").write_text("\n".join(lines) + "\n", "utf-8")

    msg = ["🔎 <b>Gazeta: AI'siz sinov</b> (Gemini ishlatilmadi)",
           f"Fayllar: {len(rows)} — " + html.escape(", ".join(f"{k}: {v}" for k, v in cnt.items())),
           f"Qo'mita tilga olingan: <b>{len(with_hits)}</b> ta fayl", ""]
    for r in with_hits[:15]:
        pg, word, sn = r["hits"][0]
        name = html.escape((r["caption"] or r["file"])[:50])
        msg.append(f"• https://t.me/{CHANNEL}/{r['id']} — {name}, {pg}-bet: «{html.escape(word)}»")
    dm.send("\n".join(msg))
    print("\n".join(lines)[:6000])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
